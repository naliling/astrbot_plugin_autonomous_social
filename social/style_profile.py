"""说话习惯画像：从真实对话历史里统计特征，**不产出任何句子**。

为什么要有它
------------
以前生成时给模型的是现成例句（「刚想到一个事」「也没啥事 就是想说句话」…）。
那些句子都通用、彼此只差几个字，模型会直接复用，最后每轮都在这二十几句里轮着挑——
看着像人，其实在背稿子。群聊侧早就有一份「这个群平时怎么说话」的参考，私聊侧一直没有。

这里补上私聊侧的对应物，但换了个做法：**不给句子，给统计特征**。
「TA 一条平均 9 个字、三成是问句、习惯用句号收尾」这类数字和分类，
约束的是长度、语气、结构，模型必须自己造内容；而给句子等于让它抄。

和 Humanoid Core 的 wording 层是同一个思路：那里说「这里不写台词也不写规矩，
只描述状态」。这个文件做的是同一件事的另一半——描述「你们俩平时怎么说话」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .emoji import EMOJI as _EMOJI

# 样本少的时候给统计数字是噪声（「TA 平均 7.4 个字」可能只统计了两条）
MIN_SAMPLES = 3
# 统计用多少条最近的
WINDOW = 40

# emoji 习惯的四个档位。unknown 表示她自己没有足够样本可依（这时才去看人设原文）。
EMOJI_HEAVY = "heavy"
EMOJI_RARE = "rare"
EMOJI_NONE = "none"
EMOJI_UNKNOWN = "unknown"

_EMOJI_BAND_TEXT = {
    EMOJI_HEAVY: "爱用 emoji",
    EMOJI_RARE: "偶尔用 emoji",
    EMOJI_NONE: "不用 emoji",
}

# 半角句号 `.` 与半角分号 `;` 同样算句尾：`_PERIOD`（下面）把半角点当「句号收尾」，
# 这里若只认全角，同一批话会得出「习惯用句号收尾」却「平均 11 个字，最长 11 字」——
# 手机上打半角点的人句长整个失真。
_SENTENCE_SPLIT = re.compile(r"[。.！!？?\n；;…]+")
# 句尾问号，以及「吗/嘛」两个**只**用于疑问的语气词。
# ⚠️ 不要把「呢 / 吧 / 呗」算进来：「我忙着呢」是进行体陈述、「好吧」是同意、「走呗」是建议，
# 都不在提问。与 `reasoning._ASKING`（那边写的是 `[?？]|[吗]$`）是同一个口径：
# 问号任意位置都算，语气词只认句尾，且只认「吗」。两处产物都会进 prompt，
# 判错会让模型以为「TA 很爱问句」进而一直把话头递回去。
# 比 `_ASKING` 多收一个「嘛」：这里是口语画像，不是 `busy_signal` 那种「宁可漏判」
# 的代价函数，而「嘛」在句尾就是「吗」的口语写法（「今天忙嘛」）。测试两个都钉住了。
_QUESTION_TAIL = re.compile(r"[吗嘛？?]+$")
# 问号**任意位置**都算提问（「你几点回来？我等你」这种把问句夹在中间的写法）。
# 与 `reasoning.py` 的 `_ASKING` 同口径；那边是 `[?？]|[吗]$`，
# 这里因为拆分已经把标点吃掉了，才拆成 `_QUESTION_TAIL` + 本正则两件事——
# `_QUESTION_TAIL` 覆盖语气词与「吗？」，本正则覆盖吃掉的问号。
_ASKING_MARK = re.compile(r"[?？]")
_ELLIPSIS = re.compile(r"…|\.{3,}")
_EXCLAIM = re.compile(r"[！!]")
_PERIOD = re.compile(r"[。\.]")
_WAVE = re.compile(r"[~～]")
# `_EMOJI`（emoji + 变体选择符）由 `social/emoji.py` 统一提供：这份字符类原本在
# `style_profile` / `generator` / `groupflow` 各抄了一份逐字节相同的正则，
# 现在三处 import 的是同一个对象，不会再出现「改一处漏一处」。
# 只用来判断「有没有带称呼」，不把称呼本身写进 prompt——那等于递台词
_ADDRESS_TOKENS = (
    "宝儿", "宝", "老婆", "老公", "亲爱的", "宝宝", "崽崽", "崽", "丫头", "小朋友",
    "同学", "哥", "姐", "兄弟", "老板", "老师", "经理",
)
# 称呼必须落在**开口**（句首），且后面不能紧跟汉字——否则「宝贵」「珍贵」这类
# 普通词会被算成称呼。原来是 `k in t[:6]` 的子串匹配：最短的 token「宝」咬住
# 长词的前半截，于是说「时间很宝贵」的人被统计成「开口习惯带称呼」，
# 结论整段进 prompt，模型就真的每条都开始叫「宝」。
_ADDRESS_HEAD = re.compile(r"^(?:%s)(?![\u4e00-\u9fff])" % "|".join(_ADDRESS_TOKENS))
_CASUAL_TOKENS = ("喂", "诶", "哎", "嘿", "欸", "呀", "哦", "噢", "嗯", "唔")

# 问句占比落到这几档的描述。只写后半句，主语由调用方拼——
# 之前这里写死了「TA」，于是统计自己说话习惯时也会说成「TA 偶尔才问一句」。
_QUESTION_BANDS = (
    (0.55, "很爱问句，习惯把话头递出去"),
    (0.35, "问句不少，对面也可以顺着问回来"),
    (0.15, "偶尔才问一句，多数时候是在讲自己的事"),
    (0.00, "基本不问句，多半是陈述自己的事"),
)


def _texts(conversation: Sequence[Dict[str, Any]], *, want_out: bool) -> List[str]:
    out: List[str] = []
    for m in list(conversation or [])[-WINDOW:]:
        if not isinstance(m, dict):
            continue
        try:
            body = str(m.get("text") or "").strip()
        except Exception:
            continue
        if not body:
            continue
        is_out = str(m.get("dir", "")) == "out"
        if is_out == want_out:
            out.append(body)
    return out


def _stat_line(label: str, texts: Sequence[str]) -> str:
    """一句特征描述。样本不足返回空串。"""
    if len(texts) < MIN_SAMPLES:
        return ""
    lengths: List[int] = []
    questions = 0
    sentences = 0
    for t in texts:
        parts = [p.strip() for p in _SENTENCE_SPLIT.split(t) if p.strip()]
        # 与 `reasoning._ASKING`（`[?？]|[吗]$`）同口径：问号任意位置都算，
        # 语气词只认句尾的「吗/嘛」。「走吧 / 我忙着呢 / 好吧」三个都不该命中。
        #
        # ⚠️ 判据跑在**整条**消息上而不是切出来的段落上：`_SENTENCE_SPLIT` 把问号
        # 当边界**吃掉**了，逐段判会让「你几点回来？我等你」里的问号凭空消失
        # （段落变成「你几点回来」「我等你」，问号谁都看不到）。
        # 计数同样按整条算并夹在句数以内，避免一个问句被算成两处提问。
        n_marks = len([m for m in _QUESTION_TAIL.finditer(t)]) + len(
            [m for m in _ASKING_MARK.finditer(t)]
        )
        if n_marks:
            questions += min(n_marks, max(1, len(parts)))
        sentences += max(1, len(parts))
        for p in parts:
            lengths.append(len(p))
    if not lengths:
        return ""
    avg = round(sum(lengths) / len(lengths))
    longest = max(lengths)
    bits = [f"{label}一条平均 {avg} 个字，最长 {longest} 字"]
    ratio = questions / max(1, sentences)
    for floor, desc in _QUESTION_BANDS:
        if ratio >= floor:
            # 主语跟 label 一致：「你一条平均…」就不会被说成「TA 偶尔才问一句」
            bits.append(f"{label}{desc}" if label == "TA" else f"你自己{desc}")
            break
    return "；".join(bits)


def emoji_habit(texts: Sequence[str]) -> str:
    """她自己发出去的消息里带 emoji 的频率，落成四档之一。

    这是「她怎么说话」的一部分，不是可调开关：她平时爱用就让她爱用，平时不用就
    别硬塞。样本不足时返回 unknown，交给调用方去看人设原文。
    """
    usable = [t for t in texts if t]
    if len(usable) < MIN_SAMPLES:
        return EMOJI_UNKNOWN
    rate = sum(1 for t in usable if _EMOJI.search(t)) / len(usable)
    if rate >= 0.3:
        return EMOJI_HEAVY
    if rate > 0.0:
        return EMOJI_RARE
    return EMOJI_NONE


def _habit_line(label: str, texts: Sequence[str]) -> str:
    """标点/换行/emoji 习惯。有明显倾向才说。"""
    if len(texts) < MIN_SAMPLES:
        return ""
    n = len(texts)
    found = []
    def rate(pat) -> float:
        return sum(1 for t in texts if pat.search(t)) / n
    if rate(_ELLIPSIS) >= 0.3:
        found.append("爱用省略号")
    if rate(_EXCLAIM) >= 0.3:
        found.append("爱用感叹号")
    elif rate(_EXCLAIM) == 0.0:
        found.append("不用感叹号")
    if rate(_WAVE) >= 0.3:
        found.append("爱用波浪号")
    if rate(_PERIOD) >= 0.6:
        found.append("习惯用句号收尾")
    # emoji 与称呼分三档：只出现过一两次也是有用的约束（「偶尔用」照样能压住模型乱加）
    habit = emoji_habit(texts)
    if habit != EMOJI_UNKNOWN:
        found.append(_EMOJI_BAND_TEXT[habit])
    multi = sum(1 for t in texts if "\n" in t.strip())
    if multi / n >= 0.3:
        found.append("爱分两三段发")
    addressed = sum(1 for t in texts if _ADDRESS_HEAD.search(t[:6]))
    if addressed / n >= 0.3:
        found.append("开口习惯带称呼")
    elif addressed > 0:
        found.append("偶尔带称呼")
    else:
        found.append("说话不带称呼")
    if not found:
        return ""
    # `label` 后面还要跟空格的：原来是 `f"{label}" + "、".join(found)`，
    # 产出「TA不用感叹号」——中文没分词，模型读到的是把主语和第一个习惯并成一个词。
    return f"{label} " + "、".join(found)


def _tone_line(label: str, texts: Sequence[str]) -> str:
    """语气词密度：口头禅的统计特征（不写出具体是哪个词）。"""
    if len(texts) < MIN_SAMPLES:
        return ""
    hits = sum(1 for t in texts if any(k in t for k in _CASUAL_TOKENS))
    ratio = hits / len(texts)
    subject = label if label == "TA" else "你自己"
    if ratio >= 0.5:
        return f"{subject}语气词很多，口语很随意"
    if ratio <= 0.1:
        return f"{subject}几乎不用语气词，说话偏平实"
    return ""


@dataclass
class StyleProfile:
    """一次统计的结论：给人看的特征块，加上几个要拿去做判断的字段。"""

    text: str = ""

    # 她自己发出去的消息里的 emoji 习惯（EMOJI_* 之一）。唯一被读的字段：
    # `generator.py:1437` 用 `style.emoji` 决定这一轮允不允许用 emoji。
    emoji: str = EMOJI_UNKNOWN

    # ── 保留字段（截至 v1.27.27 没有任何读取点，不要当成已接线）──
    # 全仓库 grep：`avg_chars` 只在定义处出现；`.max_chars` 除定义外没有读取点
    # （`generator.py` 里成片出现的 `form.max_chars`、`memory_bridge.py:212` 的
    # `max_chars` 都是无关参数）。留着的原因见下：
    #   · 删字段 = 删对外可见能力，而「这一轮该写多长」本来就有真实用途——
    #     报告 #55 的建议是在 `generator` 里用 `style.avg_chars` 给 `max_len` 一个
    #     习惯上的上界（现在只由人设与 Core 的 `form.max_chars` 决定）。接上去要改
    #     `generator.py`（不归本单元），所以先保留契约并注明：
    #   · 这两个字段与 `_stat_line` 里那段「平均 N 个字」**同一份统计**，
    #     真接线时用字段比再解析 prompt 文字可靠。
    # 真实代价只是 `_length_stats`（下面）每次多算一遍，纯本地计算，不碰网络。
    # 她一条平均多少字；0 表示样本不足
    avg_chars: int = 0
    # 她最长的一条多少字；0 表示样本不足
    max_chars: int = 0


def build_style_profile(
    conversation: Sequence[Dict[str, Any]],
    own_recent: Sequence[str] = (),
) -> StyleProfile:
    """产出一段「怎么说话」的数字参考。

    Args:
        conversation: 会话账本（对方与自己都有，用 dir 区分）
        own_recent: 自己最近主动发过的消息（补充样本）

    Returns:
        StyleProfile；样本不足时 text 为空串、emoji 为 unknown
    """
    theirs = _texts(conversation, want_out=False)
    mine = _texts(conversation, want_out=True) + [
        str(t or "").strip() for t in (own_recent or []) if str(t or "").strip()
    ]
    lines: List[str] = []
    for text in (
        _stat_line("TA", theirs),
        _habit_line("TA", theirs),
        _tone_line("TA", theirs),
        _stat_line("你", mine),
        _habit_line("你", mine),
    ):
        if text and text not in lines:
            lines.append("- " + text)
    # avg/longest 填的是上面注释里那两个「保留字段」：与 `_stat_line` 里
    # 「平均 N 个字」同一口径，等 `generator` 真拿它约束长度时直接用。
    avg, longest = _length_stats(mine)
    if not lines:
        return StyleProfile("", emoji_habit(mine), avg, longest)
    return StyleProfile(
        "【你们平时怎么说话的·只抄感觉别抄句子，句子你自己写】\n" + "\n".join(lines),
        emoji_habit(mine),
        avg,
        longest,
    )


def _length_stats(texts: Sequence[str]) -> tuple:
    """自己发出去的消息的句长统计：(平均字, 最长字)。样本不足返回 (0, 0)。"""
    if len(texts) < MIN_SAMPLES:
        return 0, 0
    lengths: List[int] = []
    for t in texts:
        parts = [p.strip() for p in _SENTENCE_SPLIT.split(t) if p.strip()]
        lengths.extend(len(p) for p in parts)
    if not lengths:
        return 0, 0
    return round(sum(lengths) / len(lengths)), max(lengths)
