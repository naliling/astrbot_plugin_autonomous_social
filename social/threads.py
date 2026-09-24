"""未完话题：三种「话没说完」不是同一件事，所以走三条不同的路。

混在一起做就会变成「隔半天问一句在吗」——那是最不像人的追问方式。拆开之后：

1. **这一场话还没完（thread）**：以「双方都不说话了多久」为触发。里面再分两种说法：
   - `probe`：对方答得很薄（「还行」「就那样」），或她上一句问了事没得到实答
     → 就着**那件事**再问一句。
   - `presence`：刚才确实在来回聊，突然安静了 → 问一句人还在不在。
   关键点：**不管最后一句是谁说的**。旧版要求「最后一句必须是对方的」，而主链路回过话
   之后最后一句永远是她自己的，于是这条最该触发的路径在实际聊天里几乎永远不成立——
   表现出来就是「用户随便发几句，模型不会回来追问」。
2. **隔一阵回访那件事（loop）**：对方提到一件有后续结果的事（面试、复查、搬家）却没说
   完。当时不适合追（人家在忙），过几小时再问「后来呢」才对。到点即发，不看 urge。
3. **没人回也不空着（closer）**：她主动找的话对方一直没回。旧版从此把这个人压住不再
   开口，真人却是过一阵自己冒一句把尴尬揭过去。一次 pending 只收一次场。

三条都不进 urge 模型：攒十几小时才开口那是「另起一个新话题」该有的样子，而话说到一半
的事，真人以分钟和小时计。
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Tuple

from .clock import city_epoch, city_now
from .reasoning import is_leaving, is_thin, last_direction, sleep_signal

# ─── 1. 这一场话还没完 ──────────────────────────────

_PROBE_REASONS: List[str] = [
    "TA就回了那么几个字，刚才那件事明显还没说完，想追问一句。",
    "你上一句问了TA一件事，TA答得很含糊，听着像是不好意思细说。",
    "话说到一半突然说得特别短，有点想知道后面怎么样了。",
    "TA提了个话头就收住了，你想把那个话头接回来。",
    "刚才那句回答太敷衍了，你想知道是真没事还是不想说。",
]

_PRESENCE_REASONS: List[str] = [
    "刚才还在聊，话说到一半TA那边没声了，不知道是不是被什么事打断。",
    "正说着呢突然安静下来，有点挂心，问一句还在不在。",
    "聊得好好的突然没人了，担心是自个儿哪句话把天聊死了。",
    "那段话停在TA那里，手机拿起来又放下了，还是问一句。",
]

# 至少来回过两句才算「在聊」，否则只是对方偶尔发了一句就走了
THREAD_CONTEXT_MIN = 2


def last_in_text(user: Dict[str, Any], now: float, within_seconds: float) -> str:
    """对方最近一句说了什么（跳过她自己说的话）。找不到就返回空串。

    判「回得敷衍」必须看**对方**那句，而账本里最后一条往往是她自己的回复。
    """
    for item in reversed(user.get("conversation") or []):
        if str(item.get("dir") or "") != "in":
            continue
        try:
            ts = float(item.get("ts", 0) or 0)
        except (TypeError, ValueError):
            continue
        if now - ts > within_seconds:
            return ""
        text = str(item.get("text", "") or "").strip()
        if text:
            return text
    return ""


def thread_reason(
    user: Dict[str, Any],
    now: float,
    *,
    probe_after_seconds: float,
    presence_after_seconds: float,
    max_seconds: float,
    context_seconds: float,
) -> Tuple[str, str]:
    """这一场话是不是说断了。返回 (种类, 动机)，种类为 probe / presence / ""。

    沉默从**最后一个人说话**算起，不要求那句是对方说的：主链路一说话，最后一条永远是
    她的，按旧规则判这条就永远不成立——那正是「用户随便发几句，模型不回来追问」的成因。

    两种说法的耐心不一样：追问那件事是有由头的，两三分钟就成立；光问「在吗」得再等会儿，
    不然像在催人。
    """
    seen = float(user.get("last_seen", 0) or 0)
    spoken = float(user.get("last_spoken", 0) or 0)
    last_said = max(seen, spoken)
    if last_said <= 0 or seen <= 0:
        return "", ""
    # 断点必须是**对方**那句还热着的时候。只看 max(seen, spoken) 的话，她在群里或跟
    # 别人说过话就会把沉默计时重置，于是隔了半天还在追问人家早上那句——真人不会这样。
    if now - seen > max_seconds:
        return "", ""
    silence = now - last_said
    if silence > max_seconds:
        return "", ""
    if float(user.get("thread_for", 0) or 0) == last_said:
        return "", ""
    if str(user.get("pending_result", "") or "") == "waiting":
        # 她上一条主动发的话还悬着：这时候再补一句就是追着人要回复
        return "", ""
    last_msg = str(user.get("last_message", "") or "")
    if is_leaving(last_msg) or sleep_signal(user, now):
        return "", ""
    # 得先「在聊」：断点之前还有话挨着，才叫聊到一半
    nearby = [
        item for item in (user.get("conversation") or [])
        if float(item.get("ts", 0) or 0) < last_said
        and last_said - float(item.get("ts", 0) or 0) <= context_seconds
    ]
    if len(nearby) + 1 < THREAD_CONTEXT_MIN:
        return "", ""

    # 追问的由头有两条：对方最近那句话几乎没带信息（「还行」「就那样」），
    # 或者她自己上一句在问事。两条都不要求「最后一句是谁说的」——主链路一回复，
    # 最后一条永远是她的，按谁最后说话判就会把这条路径堵死。
    recent_in = last_in_text(user, now, context_seconds) or last_msg
    thin = bool(recent_in) and is_thin(recent_in)
    # 「她上一句在问事」只用来把 presence 说得更像接话，不当作快速追问的理由：
    # 她问了、对方没回，隔三分钟再追一句就是催，那个得等。
    if thin and silence >= probe_after_seconds:
        return "probe", random.choice(_PROBE_REASONS)
    if silence >= presence_after_seconds:
        return "presence", random.choice(_PRESENCE_REASONS)
    return "", ""


def thread_meta(kind: str, about: str = "", asked: str = "") -> Dict[str, Any]:
    """把「说什么」交给生成器时的类型信息。

    `about` 是对方最后说的那句、`asked` 是她上一句问的那件——带着这两样才写得出
    一句接着话头的话，而不是干巴巴的「在吗」。
    """
    if kind == "probe":
        return {
            "category": "probe",
            "intent": "followup",
            "mode": "probe",
            "msg_type": "continue_topic",
        "msg_type_desc": "就着刚才那件事再问一句，问具体的那件，不是问TA在不在",
        # 纯文本列表：生成器直接拼进 prompt。这里曾经是 (文本, 档位) 元组，
        # 元组被原样印进提示词（「('后来呢', 0)」），既污染 prompt 又让示例失效。
        "msg_examples": [
            "所以到底咋回事",
            "后来呢",
            "那你现在打算怎么办",
            "真的假的",
            "严重吗",
        ],
            "about": about,
            "asked": asked,
        }
    return {
        "category": "presence",
        "intent": "followup",
        "mode": "presence",
        "msg_type": "check_in",
        "msg_type_desc": "把说到一半断掉的话接上，问一句还在不在、是不是被什么事打断了",
        "msg_examples": [
            "刚才是不是卡了",
            "人呢",
            "被啥事叫走了？",
            "你先忙，忙完说一声",
            "话没说完就没了？",
        ],
        "about": about,
        "asked": asked,
    }


# ─── 2. 隔一阵回访那件事 ────────────────────────────

# 提到这些多半意味着后面还有个结果没说出来，值得过几小时问一句。
# 只用两个字以上的词：单个「考」会把「考虑」也抓走。
_LOOP_WORDS = (
    "面试", "考试", "答辩", "汇报", "演讲", "宣讲", "培训", "复查", "体检", "手术",
    "看诊", "挂号", "打针", "拆线", "检查结果", "报告", "成绩", "分数", "录取",
    "offer", "入职", "离职", "跳槽", "转正", "评审", "上线", "交付", "验收",
    "搬家", "退租", "看房", "装修", "出差", "旅行", "旅游", "到家", "开学",
    "比赛", "约了", "在弄", "在改", "在写", "在等", "等结果", "等通知",
    "吵架", "分手", "表白", "闹翻", "冷战", "相亲", "复合", "摊牌",
    "坏了", "丢了", "找不到", "弄砸",
)

# 已经说了结果的，就别再问了
_LOOP_CLOSED = (
    "完了", "搞定", "结束", "黄了", "过了", "通过", "挂掉", "没通过", "算了",
    "不说了", "不提了", "就这样", "结果就是", "后来", "已经", "早就",
)

LOOP_MIN_HOURS = 2.5      # 当时追问不礼貌，至少隔这么久
LOOP_MAX_HOURS = 14.0     # 再晚就不像「记得」，像翻旧账
LOOP_LATE_HOURS = 40.0    # 过了到期点多久之内还算「想起来问」
LOOP_TEXT_MAX = 34

_LOOP_REASONS: List[str] = [
    "TA之前提过这么一句，一直没听到下文，想问问后来怎么样了。",
    "那件事到现在还没结果，你记着呢，想问一句。",
    "想起来TA说过有这么个事，不知道顺不顺利。",
    "这事过去半天了，TA没再提，你有点想知道后来。",
]


def extract_open_loop(
    text: str,
    now: float,
    clock_offset_minutes: Optional[int] = None,
    min_hours: float = LOOP_MIN_HOURS,
    max_hours: float = LOOP_MAX_HOURS,
) -> Tuple[Optional[str], float]:
    """对方话里有没有一件「还没说完结果」的事。

    与时间锚点（cue）互斥使用：说了「明天面试」的走 cue（对方自己给了钟点），
    只说「今天面试了」的走这里——没有锚点，但事没完。
    """
    body = str(text or "").strip()
    if len(body) < 4 or len(body) > 300:
        return None, 0.0
    if any(word in body for word in _LOOP_CLOSED):
        return None, 0.0
    hit: Optional[Tuple[int, str]] = None
    for word in _LOOP_WORDS:
        idx = body.find(word)
        if idx >= 0 and (hit is None or idx < hit[0]):
            hit = (idx, word)
    if hit is None:
        return None, 0.0
    start = max(0, hit[0] - 10)
    snippet = body[start: min(len(body), hit[0] + LOOP_TEXT_MAX)].strip()
    if not snippet or any(word in snippet for word in _LOOP_CLOSED):
        return None, 0.0
    # 顺嘴说要去洗澡/开会（一两小时就能完的事），拿它当「隔半天回访」不成立
    if is_leaving(body) and len(body) <= 24:
        return None, 0.0
    return snippet, _loop_due(now, clock_offset_minutes, min_hours, max_hours)


def _loop_due(
    now: float,
    clock_offset_minutes: Optional[int] = None,
    min_hours: float = LOOP_MIN_HOURS,
    max_hours: float = LOOP_MAX_HOURS,
) -> float:
    """回访时间：至少隔几小时，且落在她当地的白天。"""
    low = max(0.5, float(min_hours))
    high = max(low + 0.5, low * 2.2)
    stamp = now + random.uniform(low, min(high, 6.0 if low <= 6.0 else high)) * 3600.0
    local = city_now(stamp, clock_offset_minutes)
    if local.hour < 9:
        # 凌晨到点的事留到当天上午：真人不会半夜三点问「你面试咋样了」
        moved = local.replace(hour=10, minute=random.randint(0, 50), second=0, microsecond=0)
        stamp = city_epoch(moved, clock_offset_minutes)
    return min(stamp, now + max_hours * 3600.0)


def live_loop(user: Dict[str, Any], now: float) -> Optional[str]:
    """到点且还没回访过的那件事。"""
    text = str(user.get("loop", "") or "")
    try:
        due = float(user.get("loop_due", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not text or due <= 0 or now < due:
        return None
    if now - due > LOOP_LATE_HOURS * 3600.0:
        return None
    return text


def loop_meta(text: str) -> Dict[str, Any]:
    return {
        "category": "loop",
        "intent": "followup",
        "mode": "loop",
        "msg_type": "check_in",
        "msg_type_desc": "问一句TA之前提的那件事后来怎么样了",
        "msg_examples": [
            "对了你那天那个事后来咋样",
            "那个有结果了吗",
            "后来怎么样了",
            "那个还顺利吗",
        ],
        "about": text,
    }


def loop_reason(text: str) -> str:
    return f"TA之前说过「{text}」，一直没听到下文。" + random.choice(_LOOP_REASONS)


# ─── 3. 没人回也不空着 ──────────────────────────────

_CLOSER_REASONS: List[str] = [
    "上次你主动找TA，TA没接。隔了这么久，你不想让那句话一直悬着，随口收个尾。",
    "你之前那句发出去没人回，现在想起来有点好笑，自己接一句把这事揭过去。",
    "那句没人回的话你记着呢，说点什么把它盖过去，别搞得像在等人道歉。",
]

# 没人回之后至少隔多久才适合自己收场：太短就成了「你怎么不回我」
CLOSER_MIN_HOURS = 5.0
# 一共允许收几次场。之后这条路径对这个人关闭，直到对方重新接话。
MAX_CLOSERS = 3


def closer_reason(
    user: Dict[str, Any],
    now: float,
    *,
    after_seconds: float,
) -> str:
    """她主动发的话没人接，隔够了时间自己冒一句把话收掉。

    计时从**她上次主动说话**算起，而不是从 pending 结算算起：「没人回」要等 12 小时才会
    被结算成 ignored，收场那句不该排在结算之后——那会儿对方早就把这事忘了。
    """
    pend = str(user.get("pending_result", "") or "")
    if pend not in ("waiting", "ignored"):
        return ""
    sent = float(user.get("last_sent", 0) or 0)
    if sent <= 0:
        return ""
    if float(user.get("closer_for", 0) or 0) == sent:
        return ""
    if now - sent < max(after_seconds, CLOSER_MIN_HOURS * 3600.0):
        return ""
    if last_direction(user) != "out":
        # 对方后来又说了一句、只是没接她那句：那不算悬着，不用去收场
        return ""
    # 收过三次场还在没人回，那就不是尴尬是没兴趣了。没有这条会变成一个
    # 永远不接话的人每天被收一次场——收场那句本身会刷新 last_sent。
    if int(user.get("no_reply_streak", 0) or 0) >= MAX_CLOSERS:
        return ""
    return random.choice(_CLOSER_REASONS)


def closer_meta(about: str = "") -> Dict[str, Any]:
    """收场那句：明确不要求对方回复，否则又变成一次索取。"""
    return {
        "category": "closer",
        "intent": "closer",
        "mode": "closer",
        "msg_type": "share_thought",
        "msg_type_desc": "给自己上次那句没人接的话收个尾，说完整、不要求对方回",
        "msg_examples": [
            "没事 我就是随口一说",
            "刚想到个好玩的 不用回",
            "你忙你的",
            "也不是啥大事",
        ],
        "about": about,
    }
