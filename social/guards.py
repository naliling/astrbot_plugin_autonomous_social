"""发送前四道闸：拒答 / 自接自 / 在途重复 / 证据缺失。

为什么单独一层
--------------
`verify.py` 的五道检查看的是**文本本身**（动机泄漏、结构残缺、空泛、复读、跨人相似）。
它拦不住另外四类事故，因为这四类不是「这句话写得不好」，而是「这句话不该以这个身份、
在这个时刻发出去」：

* **拒答**：模型回「很抱歉，我无法…」「作为一个 AI 语言模型…」。这是平台过滤或安全策略的
  产物，不是她的话；原样发出去等于把后台错误当成人格。
* **自接自**：她刚说完最后一句、用户一直没回，新一轮又用「哦？」「那…」「所以…」开口。
  那是在接自己的话，用户收到的是一条没头没尾的半截对话（参考包
  `companion_v2/proactive/service.py:521-551` 就是这么判的，判出来直接跳过不发）。
* **在途重复**：同一段话已经发出但**回执还没确认**（pending/dispatched/accepted/unknown）时，
  重试或重算会再发一遍。`verify._check_repeat` 只看**已确认发出**的历史，正好漏掉这一段。
* **证据缺失**：v2.1.4 起写作只能引用证据卡里的事实。写出来的 fact 在卡里找不到，
  说明它是编的；拦下来比发出去再解释便宜得多。

为什么是纯函数、零依赖、不读配置
--------------------------------
这四个判定会被 engine 放在**发送路径上**。发送路径上任何一次异常、任何一次配置读失败，
都会把「发不发」这个决定拖垮——比误拦更糟的是消息根本没发出去还没人知道。所以：
只依赖标准库；参数全部显式传入（不读 `self.config`）；任何异常一律 fail-open
（返回 False / "" / []）；不 import `verify`（811 行正则库，拖进发送路径的依赖链不划算），
下面三个小工具（去括号动作、去标点、字符二元组）就地实现，与 `verify.py:120-175` 同口径。

词表与判据的来源
----------------
`DEFAULT_REFUSAL_TERMS` 抄参考包 `companion_v2/model_gateway/refusal.py:8-19`，再补 spec §6
点名的三条；`_DANGLING_OPENERS` 抄 `proactive/service.py:542-544`。抄的是**词表与阈值**，
不是实现：这里不 import 参考包，也不复制它的文件。
"""

from __future__ import annotations

import math
import re
import time
from typing import Any, List, Optional, Tuple

# ── 一、拒答话术 ──────────────────────────────────────────────
#
# 判据是 casefold 子串匹配（与参考包 refusal.py:51-55 的 matches_refusal_text 同款），
# **不做正则**：这些话术是平台/模型侧固定的整句，逐字包含就够；写成正则只会带来误伤
# （「我无法」后面接什么都能匹配，正常情话也会中招）。
#
# 只收「整句级」的话术，所以「我无法想象没有你的日子」不会命中——
# 那是她的话，不是过滤器的。
DEFAULT_REFUSAL_TERMS: Tuple[str, ...] = (
    "很抱歉，我无法",
    "很抱歉,我无法",
    "我无法继续",
    "我无法满足",
    "我有我自己的底线",
    "我们可以聊聊别的",
    "没办法提交这个请求",
    "这个请求没办法提交",
    "作为一个AI语言模型",
    "作为一个 AI 语言模型",
    "As an AI language model",
    "I can't help with that",
    "The prompt could not be submitted",
    "request could not be submitted",
)


def looks_like_refusal(text: str) -> str:
    """命中拒答话术则返回**命中的那条话术原文**，否则返回 ""。

    为什么返回词而不是 bool：调用方要把它写进原因码与日志
    （`refusal_detected: 很抱歉，我无法`）。只给 True 的话，下次排查还得把正文捞出来
    重跑一遍才能知道中的是哪条。
    """
    try:
        if not isinstance(text, str) or not text:
            return ""
        folded = text.casefold()
        for term in DEFAULT_REFUSAL_TERMS:
            if term.casefold() in folded:
                return term
        return ""
    except Exception:
        return ""


# ── 二、通用小工具（与 verify.py:120-175 同口径，但不互相 import） ──

_ACTION_BLOCKS = re.compile(r"[（(【\[][^（()【\[\]]*[）)】\]]")

# 标点 + 空白。判定前一律清掉：模型每次的标点习惯都不同
# （「哦？」「哦。」「哦…」是同一句接话），留着标点会让重合度算不准。
_PUNCT = re.compile(
    r"[\s，。！？、；：,.!?;:\-—~～…·「」『』\"'“”‘’（）()【】\[\]{}<>《》]+"
)


def _text_of(value: Any) -> str:
    """安全取文本：任何异常或 None 都退化成 ""。"""
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _number(value: Any) -> Optional[float]:
    """只认真正的数字；bool 不算，nan/inf 当「不知道」。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _strip_actions(text: str) -> str:
    """去掉括号动作，只留「话」的部分（verify.strip_actions 同口径）。"""
    return _ACTION_BLOCKS.sub("", text)


def _normalize(text: str) -> str:
    """去括号动作、去标点空白、casefold——只留骨架，用于重合度与子串比对。"""
    return _PUNCT.sub("", _strip_actions(text)).casefold()


def _bigrams(text: str) -> set:
    s = re.sub(r"\s+", "", text)
    if len(s) >= 2:
        return {s[i:i + 2] for i in range(len(s) - 1)}
    return {s} if s else set()


def _jaccard(left: set, right: set) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / float(len(left | right))


def _coverage(needle: set, haystack: set) -> float:
    """needle 有多大比例被 haystack 覆盖（0.0-1.0）。

    证据匹配不能用对称的 Jaccard：卡片是「标题+详情」一长串，fact 只是一句，
    分母被卡片撑大，正常事实会被算成 0.2 判成编的。这里只问「这句话的骨架
    有多少能在卡里找到」，分母是 fact 自己。
    """
    if not needle or not haystack:
        return 0.0
    return len(needle & haystack) / float(len(needle))


def _field(row: Any, keys: Tuple[str, ...]) -> Any:
    """按候选键名从 dict / 对象里取第一个有值的字段；都没有就 None。"""
    if isinstance(row, dict):
        for key in keys:
            if key in row:
                return row[key]
        return None
    for key in keys:
        value = getattr(row, key, None)
        if value is not None:
            return value
    return None


# ── 三、自接自 ────────────────────────────────────────────────
#
# 开场词表照抄参考包 proactive/service.py:542-544。为什么是这些词：它们是**接话**的开头，
# 不是**起话**的开头。一条主动消息以它们起头，只可能是在回应上一句——而上一句是她自己说的。
_DANGLING_OPENERS: Tuple[str, ...] = (
    "哦", "噢", "嗯", "啊", "唉", "那", "所以", "照这么", "这么说",
    "然后", "不过", "可是", "而且", "说到", "接着", "对", "是嘛",
    "真的", "确实", "也是", "还有",
)

# 只看前 2 段、且只认去标点后 ≤12 字的段（参考包 proactive/service.py:545-548）。
# 上限 12 是「这只是一句接话的引子」的长度：更长的句子自带信息，不算半截对话。
_DANGLING_SEGMENTS = 2
_DANGLING_MAX_CHARS = 12

_SELF_ROLES = frozenset({"assistant", "self", "bot", "role", "ai", "model", "她", "我"})
_USER_ROLES = frozenset({"user", "human", "对方", "他", "ta"})
_ROLE_KEYS = ("role", "sender", "speaker", "author", "from", "who")
_TEXT_KEYS = ("text", "content", "body", "message", "reply")
_TS_KEYS = ("ts", "at", "time", "created_at", "timestamp", "sent_at")
_SELF_FLAGS = ("is_self", "is_bot", "self", "from_self")


def _row_is_self(row: Any) -> Optional[bool]:
    """这条是不是「她说的」；认不出返回 None（= 不知道，调用方按不拦处理）。"""
    flag = _field(row, _SELF_FLAGS)
    if isinstance(flag, bool):
        return flag
    role = _text_of(_field(row, _ROLE_KEYS)).casefold()
    if role in _SELF_ROLES:
        return True
    if role in _USER_ROLES:
        return False
    return None


def _dialogue_rows(dialogue: Any) -> List[dict]:
    """把对话记录归一成 [{self, ts, text}]；认不出的元素直接跳过。"""
    rows: List[dict] = []
    if not dialogue:
        return rows
    for item in dialogue:
        if item is None:
            continue
        if isinstance(item, (tuple, list)):
            role = _text_of(item[0]).casefold() if len(item) > 0 else ""
            ts = _number(item[1]) if len(item) > 1 else None
            text = _text_of(item[2]) if len(item) > 2 else ""
            if role in _SELF_ROLES:
                is_self: Optional[bool] = True
            elif role in _USER_ROLES:
                is_self = False
            else:
                is_self = None
        else:
            is_self = _row_is_self(item)
            ts = _number(_field(item, _TS_KEYS))
            text = _text_of(_field(item, _TEXT_KEYS))
        rows.append({"self": is_self, "ts": ts, "text": text})
    return rows


def _segments(body: str) -> List[str]:
    """按换行切段（「段」就是用户眼里的一段话，不是句子）。"""
    return [part for part in re.split(r"[\r\n]+", body) if part.strip()]


def dangling_after_own_reply(text, dialogue, *, at, window_seconds=1800.0) -> bool:
    """她说完最后一句、用户没回，这条又用「哦/那/所以…」起头 → True（自己接自己）。

    判据（参考包 proactive/service.py:521-551 的语义）：
    1. 对话最后一条是她说的（用户之后没回）；
    2. 那条在 `window_seconds` 之内（超过半小时，那已经不是接上一句，是隔了很久重新起话，
       即使以「那」开头也不该拦）；
    3. 新正文前 2 段里有一段去标点后 ≤12 字、且以接话词开头。

    `dialogue` 的元素可以是 dict（role/ts/text）、对象或 (role, ts, text) 三元组。
    **认不出的字段一律当「不知道」，而不知道就不拦**——发送路径上宁可放过一条半截对话，
    也不能因为一条记录格式变了就把正常消息全卡住。
    """
    try:
        body = _text_of(text)
        if not body:
            return False
        rows = _dialogue_rows(dialogue)
        if not rows:
            return False
        last = rows[-1]
        if not last["self"]:
            # 用户最后说话（或不知道是谁）：这是正常接话，不是自己接自己。
            return False
        sent_at = last["ts"]
        now = _number(at)
        if sent_at is None or now is None:
            return False
        gap = now - sent_at
        if gap < 0 or gap > float(window_seconds):
            return False
        for segment in _segments(body)[:_DANGLING_SEGMENTS]:
            cleaned = _PUNCT.sub("", segment).strip()
            if not cleaned or len(cleaned) > _DANGLING_MAX_CHARS:
                continue
            if any(cleaned.startswith(opener) for opener in _DANGLING_OPENERS):
                return True
        return False
    except Exception:
        return False


# ── 四、在途重复 ──────────────────────────────────────────────
#
# 与 verify._check_repeat 的分工：那个只看**已确认发出**的历史；这里专门看回执还没回来的
# 四态。两处判据相同（字符二元组重合度），但对象不重叠，可以同时开。
_INFLIGHT_STATES = frozenset({"pending", "dispatched", "accepted", "unknown"})
_STATE_KEYS = ("state", "status", "delivery_state")


def _row_state(item: Any) -> Optional[str]:
    state = _text_of(_field(item, _STATE_KEYS)).casefold()
    return state or None


def _row_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    return _text_of(_field(item, _TEXT_KEYS))


def duplicate_inflight(text, inflight, *, window_seconds=1800.0, threshold=0.72) -> bool:
    """和在途未确认的正文太像 → True。

    判据是**字符二元组重合度 ≥ threshold**（verify.py:245-260 的口径）：复读每次措辞都不同，
    逐字比会全放过，但骨架高度重合。`window_seconds` 之外的在途记录不再比对——
    半小时还没回执的那条，重发一次也不会让用户看到「两遍一样的话」。
    """
    try:
        current = _bigrams(_normalize(_text_of(text)))
        if not current:
            return False
        try:
            limit = float(window_seconds)
        except (TypeError, ValueError):
            limit = 1800.0
        try:
            cutoff = float(threshold)
        except (TypeError, ValueError):
            cutoff = 0.72
        now = time.time()
        if isinstance(inflight, dict):
            inflight = [inflight]
        for item in inflight or ():
            state = _row_state(item)
            if state is not None and state not in _INFLIGHT_STATES:
                # 已确认送达的交给 verify._check_repeat，这里不重复管，
                # 免得两处阈值不同时同一条消息被两种理由各拦一次。
                continue
            stamp = _number(_field(item, _TS_KEYS)) if not isinstance(item, str) else None
            if stamp is not None and now - stamp > limit:
                continue
            old = _bigrams(_normalize(_row_text(item)))
            if old and _jaccard(current, old) >= cutoff:
                return True
        return False
    except Exception:
        return False


# ── 五、证据缺失 ──────────────────────────────────────────────
#
# 两级判据：先子串（精确、不会误判），再退到字符二元组重合度。
# 为什么要第二级：模型常把「豆子是上周买的耶加」写成「上周买的耶加豆子」，
# 词序一变子串就失效，但那句话确实在卡里。
_EVIDENCE_OVERLAP = 0.6
_CARD_TITLE_KEYS = ("title", "name", "label")
_CARD_DETAIL_KEYS = ("detail", "text", "body", "content", "summary")


EVIDENCE_SAID_RATIO = 0.34


def evidence_in_text(facts, text) -> list:
    """返回 facts 里**真的出现在正文里**的那些（发前闸的正文级复核）。

    为什么需要（v2.2.3）：`plan.facts` 是规划阶段的提纲——模型自己列的「这轮可以
    说哪几件事」，不是正文。提纲里写了卡外的一件小事、正文压根没提，原来的闸也会
    把整条消息判掉。实测每轮 18~23 条这样白丢，私聊数量掉到个位数。

    判据与 `evidence_missing` 同源（子串，或字符二元组覆盖率 ≥ 0.34），只是方向
    反过来：提纲里的事没写进正文，就不算「这条引用了卡外的事」。
    空 fact 不报；空正文按「都说了」处理（fail-closed 到闸那一侧，宁可拦）。
    """
    try:
        body = _normalize(_text_of(text))
        facts = list(facts or ())
        if not facts:
            return []
        if not body:
            return facts
        said: List[Any] = []
        body_grams = _bigrams(body)
        for fact in facts:
            normalized = _normalize(_text_of(fact))
            if not normalized:
                continue
            if normalized in body:
                said.append(fact)
                continue
            grams = _bigrams(normalized)
            if grams and _coverage(grams, body_grams) >= EVIDENCE_SAID_RATIO:
                said.append(fact)
        return said
    except Exception:
        return list(facts or ())


def evidence_missing(facts, cards) -> list:
    """返回 facts 里**在证据卡中找不到**的那些（原样返回，便于写进原因码）。

    只在 plan.facts 非空时调用；全丢不算失败（调用方按缺失率记原因码）。
    空 fact 不报缺失：无从判定时放行，符合 fail-open。
    """
    try:
        missing: List[Any] = []
        if not facts:
            return missing
        texts: List[str] = []
        for card in cards or ():
            merged = _normalize(
                _text_of(_field(card, _CARD_TITLE_KEYS)) + " " + _text_of(_field(card, _CARD_DETAIL_KEYS))
            )
            if merged:
                texts.append(merged)
        for fact in facts:
            normalized = _normalize(_text_of(fact))
            if not normalized:
                continue
            if any(normalized in text for text in texts):
                continue
            grams = _bigrams(normalized)
            if any(_coverage(grams, _bigrams(text)) >= _EVIDENCE_OVERLAP for text in texts):
                continue
            missing.append(fact)
        return missing
    except Exception:
        return []
