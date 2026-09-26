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
from typing import Any, Dict, List, Optional, Sequence

# 样本少的时候给统计数字是噪声（「TA 平均 7.4 个字」可能只统计了两条）
MIN_SAMPLES = 3
# 统计用多少条最近的
WINDOW = 40

_SENTENCE_SPLIT = re.compile(r"[。！？!?\n；;…]+")
_QUESTION_TAIL = re.compile(r"[吗呢吧呗嘛？?]+\s*$")
_ELLIPSIS = re.compile(r"…|\.{3,}")
_EXCLAIM = re.compile(r"[！!]")
_PERIOD = re.compile(r"[。\.]")
_WAVE = re.compile(r"[~～]")
_EMOJI = re.compile(
    "[" "\U0001F000-\U0001FAFF" "\U00002600-\U000027BF" "\U00002B00-\U00002BFF"
    "\uFE0F\u200D\u20E3" "]+"
)
# 只用来判断「有没有带称呼」，不把称呼本身写进 prompt——那等于递台词
_ADDRESS_TOKENS = (
    "宝", "宝儿", "老婆", "老公", "亲爱的", "宝宝", "崽", "崽崽", "丫头", "小朋友",
    "同学", "哥", "姐", "兄弟", "老板", "老师", "经理",
)
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
        sentences += max(1, len(parts))
        for p in parts:
            lengths.append(len(p))
            if _QUESTION_TAIL.search(p):
                questions += 1
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
    e_rate = rate(_EMOJI)
    if e_rate >= 0.3:
        found.append("爱用 emoji")
    elif e_rate > 0.0:
        found.append("偶尔用 emoji")
    else:
        found.append("不用 emoji")
    multi = sum(1 for t in texts if "\n" in t.strip())
    if multi / n >= 0.3:
        found.append("爱分两三段发")
    addressed = sum(1 for t in texts if any(k in t[:6] for k in _ADDRESS_TOKENS))
    if addressed / n >= 0.3:
        found.append("开口习惯带称呼")
    elif addressed > 0:
        found.append("偶尔带称呼")
    else:
        found.append("说话不带称呼")
    if not found:
        return ""
    return f"{label}" + "、".join(found)


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


def build_style_profile(
    conversation: Sequence[Dict[str, Any]],
    own_recent: Sequence[str] = (),
) -> str:
    """产出一段「怎么说话」的数字参考。

    Args:
        conversation: 会话账本（对方与自己都有，用 dir 区分）
        own_recent: 自己最近主动发过的消息（补充样本）

    Returns:
        可直接放进 prompt 的特征块；样本不足时返回空串
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
    if not lines:
        return ""
    return (
        "【你们平时怎么说话的·只抄感觉别抄句子，句子你自己写】\n" + "\n".join(lines)
    )
