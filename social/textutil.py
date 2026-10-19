"""跨模块共用的文本小工具。

现在只装一件东西：天气原句的方位前缀。

来由：Humanoid Core 的 `weather.env` 交过来时本身往往就是一整句
（「外面在下雨」/「窗外在下雪」），所以谁要在前面补一个「外头」，
都得先看一眼原句是不是已经交代过方位。原来 `anchors._outside_lead` 与
`groupflow.icebreak_topics` 各自硬写了一遍 `f"外头{text}"`，
于是同一个毛病在两条链路上各犯一次（实测产出「外头外面在下雨」）。
判据与词表放在这里，两边共用一份。
"""

from __future__ import annotations

import re
from typing import Any

#: 已经带了方位信息的词——原句里出现任意一个就不再补「外头」。
OUTSIDE_WORDS = ("外头", "外面", "外边", "户外", "窗外")


#: 消息段占位符：QQ/微信把图片、表情、转发卡片渲染成这种方括号文本。
#: 它会从会话账本一路流进提示词，模型照着念就变成「你发的图片我看到了」。
SEGMENT_PLACEHOLDER = re.compile(
    r"\[(?:图片|表情|语音|视频|文件|位置|转发|聊天记录|引用消息|回复|At|Image|ComponentType|"
    r"Poke|Face|Node|Record)[^\]\n]{0,80}\]?"
)

#: 纯应答词：任何语境下都不构成「那件事」，不能被当成由头引回去。
QUOTE_FILLERS = frozenset({
    "yep", "yes", "no", "ok", "okay", "嗯", "嗯嗯", "哦", "哦哦", "啊", "哈", "哈哈",
    "好", "好的", "好吧", "好呀", "行", "行吧", "可以", "不是", "没有", "怎么", "什么",
    "在", "在吗", "在不在", "打你", "讨厌", "笑死", "真的", "确实", "对啊", "是的",
    "晚安", "早安", "睡了", "吃了吗", "不知道", "随便", "都行", "等我", "稍等",
})

_PUNCT = "，。！？；、,.!?;~～…「」『』\"'“”‘’()（）[]【】 "


def strip_segments(text: Any) -> str:
    """去掉消息段占位符，返回可以给模型看/引用的文本。"""
    return SEGMENT_PLACEHOLDER.sub("", str(text or "")).strip()


def is_quotable(text: Any) -> bool:
    """这段能不能被当成「TA 上次说的那件事」引进提示词里。

    线上真实产出过：

        「你上次说的「yep」后来呢」
        「你上次说的「怎么」后来呢」
        「你上次说的「[图片][Image」后来呢」
        「TA最后说的是：「时间」。」

    把这种碎片捧回去，是整条消息里最假的地方：它像系统在复述输入，不像人在说话。
    判据只有三条，都不看语义（词表只看封闭类的纯应答词）：不是消息段占位符、
    不是纯应答词、至少两个汉字。
    """
    raw = str(text or "").strip()
    if not raw or SEGMENT_PLACEHOLDER.search(raw):
        return False
    core = raw.strip(_PUNCT)
    if not core or core.lower() in QUOTE_FILLERS:
        return False
    # 两个字的「体检」「面试」确实是一件真事，但同样也常常只是被切碎的一个词
    # （线上真实由头里就有「时间」「怎么」「不是」）。要求三个汉字是这条线上最省事的
    # 分界：代价是丢掉少数两个字的由头，收益是「你上次说的「时间」后来呢」这类
    # 一眼假的句子不再出现。由头本来就有别的来源，宁可少一条，不可假一条。
    return len(re.findall(r"[\u4e00-\u9fff]", core)) >= 3


# 她开口喊这个人时最多叫几个字。超过就只叫前一段。
_CALL_NAME_MAX = 6
_NAME_SPLIT = re.compile(r"[·•・|｜/\\．.\s]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def short_call_name(raw: Any) -> str:
    """她在消息里**喊**这个人时用的名字：太长的中文昵称只叫前一段。

    线上真实主动消息：

        (休息) 忙着吃饭，爱筝挽起·冲击波冠军，想你❤️

    真人不会把群名片一个字不落地念出来。规则只处理中文昵称：

    * ≤6 字：原样（「小明」「阿澈」「游鱼」）；
    * 带隔断符：取第一段（至少 2 字）——`爱筝挽起·冲击波冠军` → `爱筝挽起`；
    * 其余：取前 4 个字；
    * 纯英文/数字不动：`Regnator` / `hanhandouya` 本来就是全称，硬缩反而像系统截断。

    只影响「怎么称呼」，不动 Core 里存下来的昵称。
    """
    name = str(raw or "").strip()
    if not name or len(name) <= _CALL_NAME_MAX:
        return name
    if not _CJK_RE.search(name):
        return name
    parts = [p for p in _NAME_SPLIT.split(name) if p.strip()]
    if parts and 2 <= len(parts[0]) <= _CALL_NAME_MAX:
        return parts[0]
    return name[: _CALL_NAME_MAX - 2].strip() or name




def outside_lead(text: Any) -> str:
    """给天气这类整句补一个「外头」——原句自己已经说了方位就不补。

    空输入返回空串（调用点普遍把它拼进提示词，别拼出一个孤零零的「外头」）。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    if any(word in body for word in OUTSIDE_WORDS):
        return body
    return f"外头{body}"
