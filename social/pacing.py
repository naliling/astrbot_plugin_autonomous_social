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

拆分与间隔的算法逐字对齐 `astrbot/core/pipeline/result_decorate/stage.py` 与
`respond/stage.py`，差别只有一处：框架把 interval 只用在 respond 的逐段发送上，
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

# 读不到配置时的默认值：与框架 `default.py` 的 segmented_reply 出厂值一致。
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


def split_segments(text: str, cfg: dict) -> List[str]:
    """按框架配置把一条文本拆成若干段。返回至少一段（拆不开就是它本身）。

    对齐 `result_decorate/stage.py` 的两条分支（regex / words），并同样遵守
    `words_count_threshold`：超过阈值的长消息框架也整条直发，这里保持一致。
    """
    body = str(text or "")
    if not body.strip():
        return []
    try:
        threshold = int(cfg.get("words_count_threshold", 150))
    except (TypeError, ValueError):
        threshold = 150
    if len(body) > threshold:
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
        return result if result else [body]

    regex = str(cfg.get("regex") or _DEFAULT_REGEX)
    try:
        found = re.findall(regex, body, re.DOTALL | re.MULTILINE)
    except re.error:
        found = re.findall(_DEFAULT_REGEX, body, re.DOTALL | re.MULTILINE)
    result = [s.strip() for s in found if isinstance(s, str) and s.strip()]
    return result if result else [body]


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
    segments = split_segments(body, cfg)
    segments = [s for s in segments if s.strip()][:MAX_PARTS]
    if len(segments) <= 1:
        return [body], []
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
        return plan_parts(segs[0], umo, context, allow_burst=allow_burst)
    if not allow_burst:
        return ["\n".join(segs)], []
    cfg = read_segmented_reply(context, umo)
    if not bool(cfg.get("enable", False)):
        return ["\n".join(segs)], []
    segs = segs[:MAX_PARTS]
    waits = [part_interval(seg, cfg) for seg in segs[1:]]
    return segs, waits
