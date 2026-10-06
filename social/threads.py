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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .clock import city_epoch, city_now
from .reasoning import is_leaving, is_thin, last_direction, sleep_signal, busy_signal

# ─── 1. 这一场话还没完 ──────────────────────────────



# 至少来回过两句才算「在聊」，否则只是对方偶尔发了一句就走了
# 「还在聊」与「说断了」的分界：对方在这段时间里说过这么多条，就不是断点，
# 是在跟你一来一回。两个人隔着十分钟各说一句，那才可能是话头断了。
ACTIVE_CHAT_WINDOW = 30 * 60.0
ACTIVE_CHAT_MIN_MSGS = 2

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
    presence_floor_seconds: Optional[float] = None,
) -> Tuple[str, str]:
    """这一场话是不是说断了。返回 (种类, 动机)，种类为 probe / presence / ""。

    沉默从**最后一个人说话**算起，不要求那句是对方说的：主链路一说话，最后一条永远是
    她的，按旧规则判这条就永远不成立——那正是「用户随便发几句，模型不回来追问」的成因。

    两种说法的耐心不一样：追问那件事是有由头的，两三分钟就成立；光问「在吗」得再等会儿，
    不然像在催人。
    """
    # 「在吗」的门槛取 max(配置值, 两个最小间隔)。对方每小时都在说话的话，
    # 沉默最长也就一小时，永远够不到这个门槛——**每小时都在聊的人不需要被问
    # 在不在**。这比「每两小时问一次」合理得多。
    floor = presence_after_seconds
    if presence_floor_seconds:
        floor = max(floor, float(presence_floor_seconds))
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
    # 这个断点已经追过了吗？
    #
    # 判据**不能是「thread_for == last_said」**。last_said = max(对方说的, 她说的)，
    # 而她每次追问都会把 last_spoken 推到发送时刻——于是这个等式在第二次追问时必然
    # 不成立，守卫形同虚设。真实表现：一个每小时回一句的用户，7 天被追问 44 次。
    #
    # 正确的问法是「**追过一次之后，对方说过新话吗**」：说过 → 是新断点，可以再追；
    # 没说 → 还是那个断点，别追。跟时间戳是否相等无关。
    asked_at = float(user.get("thread_asked_at", 0) or 0)
    if asked_at > 0 and seen <= asked_at:
        return "", ""
    # 她**已经回过**、之后双方都没再说话 → 这段已经收尾了，不是新断点。
    # 以前用 max(seen, spoken) 判断点，把「她刚回完」也算成「对方的话还悬着」，
    # 于是每一轮主链路回复都会造出一个新断点。
    if spoken >= seen and (now - spoken) > presence_after_seconds:
        return "", ""
    if str(user.get("pending_result", "") or "") == "waiting":
        # 她上一条主动发的话还悬着：这时候再补一句就是追着人要回复
        return "", ""
    last_msg = str(user.get("last_message", "") or "")
    # T1：对方最近在忙（不一定是最后一句）就别追问——真忙与敷衍要分开，
    # 「在忙」之后回一个「嗯」不是敷衍，是在忙。
    if is_leaving(last_msg) or sleep_signal(user, now) or busy_signal(user, now):
        return "", ""
    # 「说断了」不是「对方停了一下」。
    #
    # 以前只判「对方沉默了多久」，而 probe_after_minutes 默认 2 分钟——于是对方每发
    # 一条消息、隔两分钟，她就去问「怎么突然没声了」。仿真里一个每小时回一句的
    # 用户，7 天被她追问了 44 次。
    #
    # 真正的断点是**这场话本来在继续、却停在这儿了**：对方最近还在连着说话
    # （半小时内两条以上），那就是聊得正热，不是断了。
    recent_in_count = sum(
        1 for item in (user.get("conversation") or [])
        if str(item.get("dir", "")) != "out"
        and 0 < now - float(item.get("ts", 0) or 0) <= ACTIVE_CHAT_WINDOW
    )
    if recent_in_count >= ACTIVE_CHAT_MIN_MSGS:
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
    # 第二项（reason）一律空串：动机由模型自己产生，插件只决定「这一条属于哪类」
    if thin and silence >= probe_after_seconds:
        return "probe", ""
    # 「在吗」是给「人没了」用的，判据必须是**对方**冷了多久，不是「最后一条距今多久」。
    # 用 last_said 的话，她自己刚发完的那条会把计时按住，而对方每小时都说话的人
    # 照样每两小时（最小间隔一过）就收到一句「在吗」——7 天 30 次。
    # 对方还在说话，就不是人没了。
    peer_silence = now - seen
    if peer_silence >= presence_floor_seconds and peer_silence < max_seconds:
        return "presence", ""
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
        "msg_type_desc": "就着刚才那件事再问一句",
        # 原来这里给五条现成的话（「后来呢」「真的假的」…）让模型照着挑。
        # 那是替她写台词：追问的说法就那么几种，模型每轮都在这几条里轮，
        # 同一个人的「追问」听起来永远一模一样。现在只给写法要求。
        "style_hint": "直接问那件事的进展，别绕成寒暄；一两句说完就停，别连着追问",
        "about": about,
            "asked": asked,
        }
    return {
        "category": "presence",
        "intent": "followup",
        "mode": "presence",
        "msg_type": "check_in",
        "msg_type_desc": "把说到一半断掉的话接上",
        "style_hint": "轻轻问一句还在不在就行；别质问、别要求对方解释为什么没回",
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


# 一个人身上同时记几件「提了但没说结果的事」。以前只记一件，新的直接覆盖旧的——
# 对方一次说了三件事，就只有最后一件会被跟下去。5 件是够用又不至于变成待办清单的上限。
LOOP_SLOTS_MAX = 5

# ─── 她自己许下的诺 ────────────────────────────────────────────────
# 最多同时记几件（按每个用户），以及单件字数上限。
PROMISE_SLOTS_MAX = 3
PROMISE_MAX_CHARS = 24
# 许诺之后多久算「该兑现了」。不是精确日期——模型不写日期，插件也不去猜，
# 到点她自己会挑一个合适的时机把这件事做了。
PROMISE_WAIT_SECONDS = 20 * 3600.0


def promise_entries(user: Dict[str, Any]) -> list:
    """她自己许下、还没兑现的那些事。老数据里没有这个字段，返回空列表。"""
    raw = user.get("promises")
    out: list = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and str(item.get("about", "") or "").strip():
                out.append(item)
    return out


def live_promises(user: Dict[str, Any], now: float, limit: int = PROMISE_SLOTS_MAX) -> list:
    """到点、还没兑现的诺，按到期先后排。"""
    out: list = []
    for item in promise_entries(user):
        due = _num(item.get("due"))
        if due <= 0 or now < due:
            continue
        out.append((due, str(item.get("about", "")).strip(), item))
    out.sort(key=lambda x: x[0])
    return out[:max(1, int(limit))]


def promise_meta(about: str) -> Dict[str, Any]:
    """兑现承诺那一类：模型要知道「当初答应的是什么」，并且**这次是要去做它**。"""
    return {
        "category": "promise",
        "intent": "promise",
        "mode": "promise",
        "msg_type": "share_daily",
        "msg_type_desc": "把之前答应过TA的那件事做了给他看",
        "style_hint": "别写成邀功（我说了我会做到吧），也别解释你为什么现在才做；"
                      "就当那件事本来就要做，做完顺手说一句",
        "about": about,
    }


def _num(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def loop_entries(user: Dict[str, Any]) -> list:
    """这件事的候选条目（老数据里是单条平铺字段，这里读出来统一成列表）。

    老结构（loop/loop_due/...）照旧能读：升级时不必改盘上的文件，跑到这一行才顺手搬。
    """
    out: list = []
    raw = user.get("loops")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and str(item.get("about", "") or "").strip():
                out.append(item)
    text = str(user.get("loop", "") or "").strip()
    if text and not any(str(i.get("about", "")).strip() == text for i in out):
        out.append({
            "about": text,
            "due": _num(user.get("loop_due")),
            "expire_at": _num(user.get("loop_expire_at")),
            "retry_at": _num(user.get("loop_retry_at")),
            "tries": int(_num(user.get("loop_tries"))),
            "at": _num(user.get("loop_at")),
        })
    return out


def live_loops(user: Dict[str, Any], now: float, limit: int = LOOP_SLOTS_MAX) -> list:
    """到点、没在重试冷却里、也没凉过头的那几件事，按到期先后排。"""
    out: list = []
    for item in loop_entries(user):
        due = _num(item.get("due"))
        if due <= 0 or now < due:
            continue
        retry_at = _num(item.get("retry_at"))
        if retry_at > 0 and now < retry_at:
            continue
        expire_at = _num(item.get("expire_at"))
        if now > (expire_at if expire_at > 0 else due + LOOP_LATE_HOURS * 3600.0):
            continue
        out.append((due, str(item.get("about", "")).strip(), item))
    out.sort(key=lambda x: x[0])
    return out[:max(1, int(limit))]


def live_loop(user: Dict[str, Any], now: float) -> Optional[str]:
    """到点且还没回访过、也没凉过头的那件事。分工同 live_cue。"""
    hits = live_loops(user, now, limit=1)
    return hits[0][1] if hits else None


def loops_resolved_by(user: Dict[str, Any], msg: str) -> List[str]:
    """T2：对方这条消息里带了某件未完事的**结果**——那件就不必再回访了。

    「问到了就记住」：不是把结果存成记忆，只是别让一个已经结束的话题继续挂着——
    对方明明已经说了「面试过了」，过几小时还去问「后来怎么样」。
    返回被关掉的那些 about（供调用方落账/日志）。
    """
    body = str(msg or "").strip()
    if not body or not any(w in body for w in _LOOP_CLOSED):
        return []
    out: List[str] = []
    for item in loop_entries(user):
        about = str(item.get("about", "")).strip()
        if not about:
            continue
        # 待回访那件事的关键词还在对方这句里 → 这句说的是它的结果
        if any(w in body for w in _LOOP_WORDS if w in about):
            out.append(about)
    return out


def loop_meta(text: str) -> Dict[str, Any]:
    return {
        "category": "loop",
        "intent": "followup",
        "mode": "loop",
        "msg_type": "check_in",
        "msg_type_desc": "问一句TA之前提的那件事后来怎么样了",
        "style_hint": "要明确提到那件事是什么，别只说「那个呢」；问完就停",
        "about": text,
    }


def loop_reason(text: str) -> str:
    """回访那件事的动机句。现在返回空串——素材里有「TA提过还没下文的事：xxx」，
    怎么问是模型自己的事；以前这里给四条现成问法，模型每轮都在里面轮。"""
    return ""


# ─── 3. 没人回也不空着 ──────────────────────────────


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
    # 到这里所有前置条件都过了，却还是 return ""——于是这个函数**永远**返回假值，
    # 「没人回就自己收个尾」整条链路是死代码：_closer_pick 恒为 None，收场那句
    # 一次也发不出去，而状态面板用的是另一套条件，照样显示「该收场」。
    # 返回值在这里只被当作「选不选这个人」的开关（reason 本身自 v1.16.0 起
    # 已不再进 prompt，动机由模型自己产），所以给一句人能读的说明即可。
    return f"她上次主动说的那句过了 {int((now - sent) / 3600)} 小时还没人接"


def recent_exchange(u: Dict[str, Any], limit: int = 3) -> List[str]:
    """最近这几句来回里，对方说过什么。

    「已读续接」要用：对方回过一句（正常聊天链路自己就回了，**不是插件的功劳**）
    之后又没声了，这时候补一句该接着那几句说，而不是干巴巴一句「没事我就是想说说话」。
    """
    out: List[str] = []
    for item in (u.get("conversation") or [])[-limit * 2:]:
        if not isinstance(item, dict) or str(item.get("dir", "")) != "in":
            continue
        text = str(item.get("text", "") or "").strip()
        if text:
            out.append(text)
    return out[-limit:]


def closer_meta(about: str = "", exchange: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """收场/续接那句：明确不要求对方回复，否则又变成一次索取。

    `exchange` 是对方最近说过的话。有它的时候，收场就变成**接着刚才那几句说**，
    而不是凭空找话——真人「对方回过一句然后又没声了」时，补的通常就是那件事的后续。
    """
    turns = [str(t).strip() for t in (exchange or []) if str(t).strip()]
    return {
        "category": "closer",
        "intent": "closer",
        "mode": "closer",
        "msg_type": "share_thought",
        "msg_type_desc": (
            f"顺着刚才那几句接着说（对方说过：{'；'.join(turns)}）"
            if turns else "给自己上次那句没人接的话收个尾"
        ),
        "style_hint": (
            "重点是轻：让TA不用回也没压力；别追问怎么没回，也别道歉。"
            + ("顺着对方刚才那句往下说，别另起话题" if turns else "")
        ),
        # 两边都填：模板用的是 {about}，而 _mode_note 的判据是「该模板实际用到的
        # 占位符有值」。只填一个时另一处会判成没值，把整句引文丢掉。
        "about": about,
        "asked": about,
        "exchange": turns,
    }
