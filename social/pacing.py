"""发送节奏：把「怎么拆段、隔多久发」的裁定权交还 AstrBot 的 per-Bot 配置。

## 为什么有这一层

v1.26.1 及以前，插件用自己的一套开关决定拆不拆段（`allow_burst` / `burst_probability`
=0.62 / `max_burst_parts`=3），和 AstrBot 每个 Bot 各自的「分段回复」设置完全无关。
两种组合都出问题：

* Bot **没开**分段：插件自己把一条消息拆成 2~3 段逐条发 → 在主人眼里是「她连续说话」；
* Bot **开了**分段：插件照拆不说，主链路回复还会被框架**再拆一层** → 「直接连续说话
  加一大堆输出」。

而插件主动消息走 `context.send_message` → `platform.send_by_session`，**不经过框架的
result_decorate 阶段**——框架自己的 segmented_reply 对它本来就不生效。所以「跟随」
这件事只能在插件的发送侧自己做。

## 跟随规则（2026-10-03 定稿）

1. 读该会话的 `platform_settings.segmented_reply`（`context.get_config(umo)`，
   没有 umo 或读不到时退回框架默认值——`enable=False`）。
2. 框架**没开分段** → 插件也不拆：模型输出的多段合并成一条发出。
3. 框架**开了分段** → 用它的那套参数拆分与计时：`words_count_threshold`（超长整条
   直发）、`split_mode`+`regex`/`split_words`（拆法）、`interval_method`+`interval`/
   `log_base`（间隔）。
4. 插件自己的 `allow_burst` 退化为**总开关**：关掉时无论框架开没开都不拆。

按 `astrbot/core/pipeline/result_decorate/stage.py` 与
`respond/stage.py` 的**行为**对齐（三条规则：拆法、`words_count_threshold` 超长直发、
`interval_method` 计时），但**本环境没有 AstrBot 源码，这套对齐没有逐字比对过**：
参数名与默认值照框架文档/出厂配置写，若框架改版需以实际源码复核。
差别只有一处：框架把 interval 只用在 respond 的逐段发送上，
这里把它用在插件自己的逐段发送上。
"""

from __future__ import annotations

import math
import random
import re
from typing import Any, List, Optional, Sequence, Tuple

# 拆段上限。框架本身不限段数，这里保留上限是防模型把一条拆成十几段刷屏；
# 4 比原来的 3 多一格，因为框架用户自己可能把阈值调得很宽。
MAX_PARTS = 4


def _cap_parts(segments: List[str], limit: int = MAX_PARTS) -> List[str]:
    """超过上限时把多出来的并进最后一段，而不是丢掉——丢内容比刷屏更糟。"""
    if len(segments) <= limit:
        return segments
    return segments[: limit - 1] + ["".join(segments[limit - 1:])]

# 读不到配置时的默认值：按框架 `default.py` 的 segmented_reply 出厂值抄，
# 同样未在本环境逐字比对（见模块 docstring）。
_DEFAULT_REGEX = r".*?[。？！~…]+|.+$"
_DEFAULT_SPLIT_WORDS = ["。", "？", "！", "~", "…"]


def read_segmented_reply(context: Any, umo: str) -> dict:
    """读该会话（该 Bot）的分段回复配置。任何一步拿不到都退到「没开分段」。

    退到「没开」而不是「照拆」：拆段是带节奏的发信行为，消息在主人手机上连着弹，
    宁可少拆也不要在配置没读到时擅自拆。
    """
    getter = getattr(context, "get_config", None)
    cfg_holder: Any = None
    if callable(getter):
        try:
            cfg_holder = getter(umo) if umo else getter()
        except Exception:
            cfg_holder = None
    if cfg_holder is None:
        try:
            cfg_holder = getter() if callable(getter) else None
        except Exception:
            cfg_holder = None
    # AstrBotConfig 支持 .get(key) 两种形态：直接 get("platform_settings")
    section: dict = {}
    try:
        if cfg_holder is not None:
            ps = cfg_holder.get("platform_settings") or {}
            section = dict(ps.get("segmented_reply") or {})
    except Exception:
        section = {}
    if not section:
        return {"enable": False}
    return section


def _word_count(text: str) -> int:
    """框架同款字数统计（`respond/stage.py::_word_cnt`）。"""
    if all(ord(c) < 128 for c in text):
        return len(text.split())
    return len([c for c in text if c.isalnum()])


# ── 分段边界保护 ─────────────────────────────────────────────────
#
# 为什么需要：拆段只看「这个位置有没有标点」，不看这个位置**在什么里面**。
# 于是「她说『好。我知道了』」被切成 \`她说「好\` / \`我知道了」\`，第二条单独看像
# 她在自言自语；\`23:00\`、\`https://…\`、\`安装包.zip\` 被从中间切开，用户收到的
# 是一串碎片。这类错误比不拆更难看，所以先标出「不能切」的区间，再让两处
# 拆段逻辑绕开它们。

_QUOTE_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"),
)
_BRACKET_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"), ("《", "》"), ("〈", "〉"),
)

# 这些模式命中的是一整个「不该拆开」的 token。
# ⚠️ URL 与文件名没有照抄 \`\S+\`：\`\S\` 会把中文标点一起吞掉，
# 「看这个 https://x.y/z。然后呢」会被整句保护起来，正常断点反而失效。
# 这里把可吃字符限制在「非空白、非句读」上，保护范围刚好是 token 本身。
_SPAN_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\d+:\d+"),                       # 23:00 / 16:9
    re.compile(r"https?://[^\s，。！？；、）)】」』]+"),
    re.compile(
        r"[^\s，。！？；、（）()【】「」『』]+\.(?:zip|rar|png|jpe?g|gif|pdf|docx?|xlsx?|pptx?|txt|py|json|md)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:[A-Za-z]\.){2,}"),            # U.S. / e.g. / a.m.
    re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.[a-z]{2,6}\b"),  # example.com
)


def _paired_spans(text: str, pairs: Tuple[Tuple[str, str], ...]) -> List[Tuple[int, int]]:
    """成对符号的区间；未闭合时从开符号一直保护到文末。

    未闭合为什么保护到文末：模型被截断时常常只留一个 `「`，后面半句仍是正文，
    此时「哪里能切」已经不可信——整条发出去，好过切成两个半截气泡。
    """
    spans: List[Tuple[int, int]] = []
    index, size = 0, len(text)
    while index < size:
        for opener, closer in pairs:
            if text[index] != opener:
                continue
            if closer == opener:
                end = text.find(opener, index + 1)
            else:
                end = text.find(closer, index + 1)
            if end < 0:
                spans.append((index, size))
                return spans
            spans.append((index, end + 1))
            index = end
            break
        index += 1
    return spans


def _merge_spans(spans: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """重叠或相接的区间合并成一个（例如「（他说『好』）」这种多层嵌套）。"""
    merged: List[List[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(int(start), int(end)) for start, end in merged]


def _protected_spans(text: str) -> List[Tuple[int, int]]:
    """返回「不该被切开」的区间 [(start, end), ...]，已排序、已合并。

    覆盖：成对引号与括号（未闭合保护到文末）、时间与比例、URL、文件名、
    英文缩写里的点、域名里的点。
    """
    body = str(text or "")
    if not body:
        return []
    spans = _paired_spans(body, _QUOTE_PAIRS) + _paired_spans(body, _BRACKET_PAIRS)
    for pattern in _SPAN_PATTERNS:
        spans.extend((match.start(), match.end()) for match in pattern.finditer(body))
    return _merge_spans(spans)


def _inside_spans(spans: Sequence[Tuple[int, int]], pos: int) -> bool:
    """pos 是否落在某个保护区间的**内部**；贴边不算（边界正好在两端时可以切）。"""
    return any(start < pos < end for start, end in spans)


def _merge_protected(segments: List[str]) -> List[str]:
    """把被保护区间切开的相邻段并回去，返回并好的段列表。

    偏移按段累加：第 k 段的起点是前面所有段的长度之和。段边界落在保护区间
    内部时，这一刀会把引号/括号/时间切开，于是把它并进上一段（可连续合并）。

    合到只剩一段也照合并结果返回：保护的目的就是「宁可不拆」，退回原分段
    等于把半截气泡又还回去。
    """
    segs = [seg for seg in (segments or []) if isinstance(seg, str)]
    if len(segs) <= 1:
        return segs
    spans = _protected_spans("".join(segs))
    if not spans:
        return segs
    merged: List[str] = []
    current = ""
    position = 0
    for seg in segs:
        start = position
        position += len(seg)
        if not current:
            current = seg
        elif _inside_spans(spans, start):
            current += seg
        else:
            merged.append(current)
            current = seg
    if current:
        merged.append(current)
    return merged


def split_segments(text: str, cfg: dict) -> List[str]:
    """按框架配置把一条文本拆成若干段。返回至少一段（拆不开就是它本身）。

    对齐 `result_decorate/stage.py` 的两条分支（regex / words），并同样遵守
    `words_count_threshold`：超过阈值的长消息框架也整条直发，这里保持一致。

    ⚠️ 阈值比的是 `_word_count`（ASCII 按空格分词、非 ASCII 数字母）而**不是**
    `len(text)`：原来这里用字符数，于是「你好呀？？？」这种 6 个字符、3 个字的消息
    在阈值 5 下被判成「超长→整条直发」，而 `part_interval` 的 log 间隔法对同一条
    文本算的却是 3 ——同一份配置在同一文件里出现两套口径，阈值附近结论正好相反。
    """
    body = str(text or "")
    if not body.strip():
        return []
    try:
        threshold = int(cfg.get("words_count_threshold", 150))
    except (TypeError, ValueError):
        threshold = 150
    if _word_count(body) > threshold:
        return [body]

    mode = str(cfg.get("split_mode", "regex") or "regex")
    if mode == "words":
        words: Sequence[str] = cfg.get("split_words") or _DEFAULT_SPLIT_WORDS
        words = [str(w) for w in words if str(w)]
        if not words:
            return [body]
        pattern = re.compile(
            "(.*?(" + "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True)) + ")|.+$)",
            re.DOTALL,
        )
        result: List[str] = []
        for seg in pattern.findall(body):
            if not isinstance(seg, tuple):
                if seg and seg.strip():
                    result.append(seg)
                continue
            content = seg[0]
            if not isinstance(content, str):
                continue
            for word in words:
                if content.endswith(word):
                    content = content[: -len(word)]
                    break
            if content.strip():
                result.append(content)
        return _merge_protected(result) if result else [body]

    regex = str(cfg.get("regex") or _DEFAULT_REGEX)
    try:
        found = re.findall(regex, body, re.DOTALL | re.MULTILINE)
    except re.error:
        found = re.findall(_DEFAULT_REGEX, body, re.DOTALL | re.MULTILINE)
    result = [s.strip() for s in found if isinstance(s, str) and s.strip()]
    return _merge_protected(result) if result else [body]


def part_interval(text: str, cfg: dict, *, rng: random.Random | None = None) -> float:
    """两段之间的等待秒数。对齐 `respond/stage.py::_calc_comp_interval`。"""
    rnd = rng or random
    method = str(cfg.get("interval_method", "random") or "random")
    if method == "log":
        try:
            base = float(cfg.get("log_base", 2.6) or 2.6)
        except (TypeError, ValueError):
            base = 2.6
        base = max(1.01, min(10.0, base))
        wc = _word_count(text)
        i = math.log(wc + 1, base)
        return rnd.uniform(i, i + 0.5)
    interval = cfg.get("interval") or "1.5,3.5"
    try:
        if isinstance(interval, str):
            low, high = (float(t) for t in interval.replace(" ", "").split(",", 1))
        else:
            low, high = (float(t) for t in list(interval)[:2])
    except Exception:
        low, high = 1.5, 3.5
    if low > high:
        low, high = high, low
    return rnd.uniform(low, high)


def plan_parts(
    text: str,
    umo: str,
    context: Any,
    *,
    allow_burst: bool,
) -> Tuple[List[str], List[float]]:
    """一条要发的消息 → (分段列表, 每段后的等待秒数)。

    返回值语义：
    * 框架没开分段、或 `allow_burst=False`：`([整条], [])`——一条发出，不等待。
    * 框架开了：按它的参数拆段；每段（除最后一段）后面给一个间隔秒数。

    拆不出来（正则只中一段）时同样退化成整条，不硬拆。
    """
    body = str(text or "").strip()
    if not body:
        return [], []
    if not allow_burst:
        return [body], []
    cfg = read_segmented_reply(context, umo)
    if not bool(cfg.get("enable", False)):
        return [body], []
    segments = [s for s in split_segments(body, cfg) if s.strip()]
    if len(segments) <= 1:
        return [body], []
    segments = _cap_parts(segments)
    waits = [part_interval(seg, cfg) for seg in segments[1:]]
    return segments, waits


def plan_segments(
    parts: Sequence[str],
    umo: str,
    context: Any,
    *,
    allow_burst: bool,
) -> Tuple[List[str], List[float]]:
    """已经把模型输出拆好的段（`---` 分隔）→ 最终要发的段与间隔。

    与 `plan_parts` 的区别：前者从一整段文本开始拆，这里手里已经是模型显式分过的段。
    三个分支：

    * `allow_burst=False` 或框架没开分段 → **合并成一条**：模型分了段但这个 Bot 的
      主人没要分段，那就一块发（拆分权在主人手里，不在模型手里）；
    * 框架开了、且已有多个段 → 保留模型的念头切分，间隔用框架算法；
    * 框架开了、但模型只写了一段 → 按框架正则再拆一次（让框架的拆法生效）。
    """
    segs = [str(p or "").strip() for p in (parts or []) if str(p or "").strip()]
    if not segs:
        return [], []
    if len(segs) == 1:
        out, waits = plan_parts(segs[0], umo, context, allow_burst=allow_burst)
        # 自动拆两条也必须服从框架的分段开关。plan_parts 在框架没开分段时
        # 已经退回整条（out 只有一段），旧实现只看 len(out)==1 就继续
        # _auto_split，等于把「框架没开」又拆开了——正是 P2-02 说的
        # 「面板写跟随框架、实际照样连发」。
        if allow_burst and len(out) == 1:
            cfg = read_segmented_reply(context, umo)
            if bool(cfg.get("enable", False)):
                auto = _auto_split(out[0])
                if auto:
                    return auto, [random.uniform(2.0, 4.0) for _ in auto[1:]]
        return out, waits
    if not allow_burst:
        return ["\n".join(segs)], []
    cfg = read_segmented_reply(context, umo)
    if not bool(cfg.get("enable", False)):
        return ["\n".join(segs)], []
    segs = _cap_parts(segs)
    waits = [part_interval(seg, cfg) for seg in segs[1:]]
    return segs, waits

# 自动连发的下限：太短拆了像结巴，太长的才值得分两条说（线上真实主动消息中位数 20 字）。
AUTO_BURST_MIN_CHARS = 26
AUTO_BURST_MIN_PART = 6
_BREAK_CHARS = "。！？…～!?\n"


def _auto_split(text: str) -> List[str]:
    """一段话拆成两条（在自然断点处），拆不了返回空列表。

    为什么要这个：`plan_segments` 只在「框架开了分段 + 模型写了 `---`」时才连发，
    而模型几乎从不写 `---`，主人也从没在主动消息里见过连发。线上 20 条里 0 条连发。
    「一个人想到什么分两条发」是最像真人的动作，不该只靠模型自觉。
    """
    body = str(text or "").strip()
    if len(body) < AUTO_BURST_MIN_CHARS:
        return []
    # 从中间偏后找断点：太靠前第一条太短，太靠后第二条太短
    lo, hi = int(len(body) * 0.35), int(len(body) * 0.75)
    # 保护区间的断点不能用：在那里切就是半截引号/括号/URL（见 _protected_spans）
    spans = _protected_spans(body)
    best = -1
    for i in range(hi, lo - 1, -1):
        if body[i - 1] in _BREAK_CHARS and not _inside_spans(spans, i):
            best = i
            break
    if best < 0:
        return []
    first, second = body[:best].strip(), body[best:].strip()
    if len(first) < AUTO_BURST_MIN_PART or len(second) < AUTO_BURST_MIN_PART:
        return []
    return [first, second]
