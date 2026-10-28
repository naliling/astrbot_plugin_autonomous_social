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

#: 框架注入块的左右定界符：Humanoid Core 的身体事实块用「〔…〕」，
#: `<system_reminder>` 用尖括号。它们不是人说的话，却会因为「汉字够多」
#: 通过 `is_quotable` 的条数检查——线上真实由头里出现过
#: 「你上次说的「〔她的身体与生活 v10」后来呢」。
#: 判据用**成对出现**（左右都有）而不是单边：正文里出现单个「〔」很可能是
#: 正常书写，只有整段被这对符号包住才是注入块。
_FRAMEWORK_WRAPPERS = (
    ("〔", "〕"), ("【", "】"), ("<system_reminder>", "</system_reminder>"),
    ("<", ">"), ("[", "]"),
)


def _is_framework_wrapped(text: str) -> bool:
    """整段被框架注入块的定界符包着吗。"""
    body = text.strip()
    for left, right in _FRAMEWORK_WRAPPERS:
        if body.startswith(left) and body.endswith(right) and len(body) > len(left) + len(right):
            return True
    return False

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


#: 需要配对的括号/引号：左 → 右。`"` 不在这张表里——它自己就是一对，
#: 只能按出现顺序配对（见 `balance_brackets`）。
_BRACKET_PAIRS = {
    "（": "）", "(": ")",
    "「": "」", "『": "』", "【": "】",
    "[": "]", "{": "}",
    "“": "”",
}
_CLOSE_TO_OPEN = {right: left for left, right in _BRACKET_PAIRS.items()}
#: 对称引号：前后同一个字符，靠顺序两两配对。
_SYMMETRIC_QUOTES = frozenset({'"'})
_BRACKET_CHARS = frozenset(_BRACKET_PAIRS) | frozenset(_CLOSE_TO_OPEN) | _SYMMETRIC_QUOTES


def balance_brackets(text: Any) -> str:
    """删掉**不成对**的括号/引号字符，只留成对的；里面的内容一个不动。

    为什么需要这一层
    ----------------
    引文进提示词前几乎都要**截断**（`_peer_text` 截 80、`_short` 截 12、
    `extract_cue` 取 12 字上下文）。截断落在一个成对括号中间，就会切出只剩
    半边的残句。线上真实出现过的提示词片段（都在 `state.json` / prompt 里抓到）：

        TA最后说的是：「(慢慢躺在你的身边抱着你」。
        你上次说的「(慢慢躺在你的身边抱着你」后来呢
        你上次说那不奇怪了……(温柔地揉，后来好点没
        · 你们说好的：时钟发现很晚了)嘛…不过明天照样美好…

    模型会把这种残缺引文当成「对方就是这么说的」，照着它写断句——用户看到的
    就是病句。所以把它当成**所有引文片段的最后一道清洗**：截断之后再做，只删
    单边字符，成对的保持原样。

    不删成对括号是有意的：`（轻轻抱你）` 这种语C 动作是人格自己的说话方式，
    删掉它就是把角色抄平。

    判定规则（栈式配对，支持嵌套）：
    * 左括号按顺序入栈；右括号只在能找到自己那个左括号时才成对；
    * `"` 也是左括号，遇到下一个 `"` 才成对（所以单个 `"` 会被删）；
    * 落单的左括号、落单的右括号、落单的 `"` 一律删掉；
    * 其余字符（包括被括号包住的内容）逐字保留，顺序不变。

    空串原样返回；None 当空值；其他非 str 先 `str()`（`None` 不做成 "None"——
    它进提示词只会变成一句假话）。
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    stack: list = []      # [(字符, 下标)]：还在等自己右半边的左括号
    paired: set = set()   # 成对的下标（这些原样保留）
    for i, ch in enumerate(text):
        if ch in _SYMMETRIC_QUOTES:
            # 对称引号：栈里已经有一个同类就当它的右半边，否则自己开一个
            want = ch
            if not any(open_ch == ch for open_ch, _i in stack):
                stack.append((ch, i))
                continue
        elif ch in _CLOSE_TO_OPEN:
            want = _CLOSE_TO_OPEN[ch]
        elif ch in _BRACKET_PAIRS:
            stack.append((ch, i))
            continue
        else:
            continue
        # 从近到远找自己的左半边：正常嵌套时栈顶就是它；
        # 错序嵌套（「（他说"好」」）也能找回更外层的那个。
        hit = -1
        for depth in range(len(stack) - 1, -1, -1):
            if stack[depth][0] == want:
                hit = depth
                break
        if hit < 0:
            continue          # 找不到左半边 → 下面统一删掉
        _, left_i = stack.pop(hit)
        paired.add(left_i)
        paired.add(i)
    if len(paired) == sum(1 for ch in text if ch in _BRACKET_CHARS):
        return text
    return "".join(
        ch for i, ch in enumerate(text)
        if i in paired or ch not in _BRACKET_CHARS
    )


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
    # 框架注入块（Core 的身体事实块、<system_reminder>）：它们汉字够多、
    # 也不是应答词，光靠前两条拦不住——线上真实由头里出现过
    # 「你上次说的「〔她的身体与生活 v10」后来呢」。
    if _is_framework_wrapped(raw):
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
    * 纯英文/数字不动：`Alice` / `Bob` 本来就是全称，硬缩反而像系统截断。

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
