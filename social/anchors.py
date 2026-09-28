"""由头（anchor）：每一次主动开口都得有一个具体的事。

为什么要有这个模块
------------------
v1.19 之前，触发一个主动消息的只有两种来源：

* **念头攒满了**（`contenders`）——那是个**计时器**，不是理由。提示词问的是
  「这一刻你到底想不想说点什么」，一个关于*说话*的问题；答案形态必然是
  「想表达此刻的孤独和饥饿感」这种第三人称祈使句，而它又能直接当正文发出去。
* **问候窗口**——有事由，但由头是 `_GREET_REASON_*` 那个**固定句池**，每天
  抽一句，池子不消耗。

真实 197 条发送记录把这两条都验死了：

* 同一个插件、同一个人（记录里的 `naliling`），`why` 是「刚热上饭，顺口问下
  人还在没」「话说到一半断了」的那几条，读起来完全像人；`why` 是「念头攒到
  2.80」的那 82 条，几乎全是「想表达…」「肚子饿…撒娇」。
* 某一个晚上 23 条全是「窝在沙发追剧…困了…晚安」——同一件事连说 23 遍。

所以：**念头不该是触发器，它只是节奏**。有具体的事才发，没有就不发。

## 由头的三个性质
1. **真实**：全部由插件已持有的数据派生（Core 契约、对方说过的话、她自己的承诺），
   不是让模型现编。
2. **一次性**：派生出来的**当场消费**（`used_at` 记下），同一个事由一辈子只出现
   一次。这一条专治「23 条沙发追剧」。
3. **不重复**：用过的进冷却（`ANCHOR_COOLDOWN`），冷却期内既不重发、也不重新派生
   出同一句。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

# 一个人身上同时挂着几条「还没用过的事」
ANCHOR_MAX = 5
# 用过之后多久可以再用同一件事（秒）
ANCHOR_COOLDOWN = 7 * 86400.0
# 用过的记录留多久才彻底忘掉（比冷却长：留着是为了别再派生出一模一样的一句）
ANCHOR_HISTORY = 45 * 86400.0
# 派生出来的由头最多留多久（过期的意义不大）
ANCHOR_TTL = 14 * 86400.0
# 一条由头最多试几次。**发出才算消费**；模型写砸了（被验收层拦下）只记一次失败，
# 下一轮还能再试——一次抽风不该让这个话题冷 7 天。但也不能无限重试。
ANCHOR_MAX_TRIES = 2

# 由头种类，按「有多硬」排
KIND_PROMISE = "promise"   # 她自己答应过的，到点兑现
KIND_CUE = "cue"           # 对方说的时间锚点到期了
KIND_LOOP = "loop"         # 对方提过、还没下文
KIND_THREAD = "thread"     # 话说到一半断了
KIND_DERIVED = "derived"   # 从她自己的状态派生的一件具体的事
KIND_MISS = "miss"         # 想念：好感够、又想了一阵子（不是有正事要说）
KIND_GREET = "greet"       # 问候：没事干，随口说一句看看你回不回

# ── 有效期：按性质分三类 ──────────────────────────────────────────
#
# 真人主动开口的时机几乎都在「状态刚发生变化」的那一刻，不是「每两小时检查一次够
# 多久」。所以由头带的是**有效期**而不是冷却：
#
#   A 她这边刚发生（做完一件事、体感变了、天气变了）——事情做完的那一刻顺嘴说一句，
#     过了两小时再说就奇怪了。
#   B 时段性的（午休、收工、天黑了）——到这个时段结束为止。
#   C 对方那边的（TA 说了新的话、有新的未完事项）——本来就可能隔一天才想起来问。
#
# 过期即作废，**不补发**。这跟以前不同：以前是「错过了就没有」，所以一天只发得出
# 两条；现在 Core 那边一天发生几次变化就有几次机会。
TTL_SELF_EVENT = 2 * 3600.0
TTL_DAY_PART = 3 * 3600.0
TTL_PEER_EVENT = 24 * 3600.0

_KIND_TTL = {
    KIND_DERIVED: TTL_SELF_EVENT,
    KIND_MISS: TTL_DAY_PART,
    KIND_GREET: TTL_DAY_PART,
    KIND_CUE: TTL_PEER_EVENT,
    KIND_LOOP: TTL_PEER_EVENT,
    KIND_PROMISE: TTL_PEER_EVENT,
    KIND_THREAD: TTL_DAY_PART,
}

KIND_RANK = {
    KIND_PROMISE: 0,
    KIND_CUE: 1,
    KIND_LOOP: 2,
    KIND_THREAD: 3,
    KIND_MISS: 4,
    KIND_DERIVED: 5,
}


def _f(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _s(x: Any) -> str:
    try:
        return str(x or "").strip()
    except Exception:
        return ""


# ── 关系热度分档 ──────────────────────────────────────────────────
#
# **为什么不按好感度绝对值分档。**
# 有运营为了让用户更好攻略，会把初始好感度设成 40 之类；聊久了直接飙到 80。
# 只看绝对值的话，这种人和「一直停在 80 但从不回话」的人是同一档——而该被多找的
# 恰恰是前者。Core 存着 `mood.base_affection`（基线）和 user 级 `first_met`（认识多久），
# 所以**涨幅**和**认识时长**是拿得到的，互动量与回复率则一直在插件自己账上。
#
# 三档只决定三件事：最小间隔、由头池多大、能不能主动发。低档另有一条「试两次不成即
# 停发」——真人不会一直追一个不回话的人。

TIER_HIGH = "high"
TIER_MID = "mid"
TIER_LOW = "low"

# 各档的最小间隔（分钟）。高档很近，因为她常聊；低档隔一两天，试试就好。
TIER_MIN_GAP_MINUTES = {TIER_HIGH: 30, TIER_MID: 120, TIER_LOW: 1440}
# 各档同时挂几件事。高档可以挂多点（一天的机会本来就多）
TIER_POOL_MAX = {TIER_HIGH: 12, TIER_MID: 8, TIER_LOW: 4}

# 涨幅：关系从起点长出来多少才算「真的在升温」
# 绝对好感低于这个值，且没涨过 → 封在中档以下
TIER_AFFECTION_FLOOR = 25.0
TIER_WARMTH_HIGH = 25.0
TIER_WARMTH_MID = 8.0
# 互动量：最近这些天对方说了多少句（按消息条数，粗一点够用）
TIER_TALK_HIGH = 20
TIER_TALK_MID = 8
# 回复率：她发出去的话，对方接了多少
TIER_REPLY_HIGH = 0.6
TIER_REPLY_MID = 0.3


def relation_tier(
    user: Dict[str, Any],
    *,
    affection: Optional[float] = None,
    base_affection: Optional[float] = None,
    first_met: Optional[float] = None,
    now: float = 0.0,
    recent_days: float = 7.0,
) -> str:
    """这个人跟她的关系有多热——三档之一。

    输入里最关键的是**涨幅**（现在的好感 - 基线），它不受「初始值被调高」影响。
    绝对好感只当参考，不单独决定档位。
    """
    aff = affection if affection is not None else 0.0
    try:
        aff = float(aff)
    except (TypeError, ValueError):
        aff = 0.0
    try:
        base = float(base_affection) if base_affection is not None else None
    except (TypeError, ValueError):
        base = None
    warmth = (aff - base) if base is not None else aff * 0.5

    # 最近这段时间对方说了多少句
    cutoff = now - recent_days * 86400.0 if now else 0.0
    talk = 0
    for item in _conversation(user):
        if str(item.get("dir", "")) == "out":
            continue
        try:
            ts = float(item.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts >= cutoff or not cutoff:
            talk += 1

    try:
        sent = int(user.get("proactive_sent", 0) or 0)
        replied = int(user.get("proactive_replied", 0) or 0)
    except (TypeError, ValueError):
        sent = replied = 0
    reply_rate = (replied / sent) if sent >= 3 else 0.0

    # 分档的主依据是**涨幅**与互动，绝对好感只作参考。但它仍有一条硬约束：
    # 一个人从来没对你有过好脸色（好感低且没涨），即使聊得多，也不能当成「很熟」——
    # 那会让模型拿着错误的关系判断去开口。
    if aff < TIER_AFFECTION_FLOOR:
        return TIER_LOW
    if warmth >= TIER_WARMTH_HIGH or (reply_rate >= TIER_REPLY_HIGH and talk >= TIER_TALK_HIGH):
        return TIER_HIGH
    if warmth >= TIER_WARMTH_MID or (reply_rate >= TIER_REPLY_MID and talk >= TIER_TALK_MID):
        return TIER_MID
    return TIER_LOW


def tier_note(tier: str, affection: Optional[float], warmth: Optional[float]) -> str:
    """把档位写成一句人话给模型看——它要靠这句判断「现在找 TA 合不合适」。"""
    label = {TIER_HIGH: "你们很熟", TIER_MID: "算熟", TIER_LOW: "其实没怎么说过话"}.get(tier, "关系一般")
    bits = [label]
    if affection is not None:
        try:
            bits.append(f"TA 对你的好感 {float(affection):.0f}")
        except (TypeError, ValueError):
            pass
    if warmth is not None:
        try:
            bits.append(f"比刚开始高了 {float(warmth):.0f}")
        except (TypeError, ValueError):
            pass
    return "、".join(bits)


# ── 池 ────────────────────────────────────────────────────────

def load_anchors(u: Dict[str, Any]) -> List[Dict[str, Any]]:
    """读出这个人身上挂着的全部由头（未用的 + 冷却中的）。"""
    raw = u.get("anchors")
    out: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and _s(item.get("about")):
                out.append(item)
    return out


def prune_anchors(u: Dict[str, Any], now: float) -> None:
    """丢掉彻底过期的（用够久的、或寿到了的）。

    判据是「用满冷却期」，**没用过的（used_at=0）也算过期**——这不是 bug，
    是由头作为**一次性队列**的机制：一条由头只发一次，发完就该让位，
    下一轮重新从当下状态派生新的。留着它们反而会堵死派生：

        上限 1  → 每轮都重新派生，6 轮拿到 18 条候选
        上限 12 → 池子里用过的还在，`add_anchor` 又不会重加同样的事，
                  候选逐轮变少（3→2→1），派生被饿死

    曾经试过把「没用过的」按寿命（expire_at）留着，发送量从 7 天 127 条掉到 19 条。
    """
    kept = [
        a for a in load_anchors(u)
        if (_f(a.get("used_at")) + ANCHOR_HISTORY > now) and (_f(a.get("expire_at")) or now + ANCHOR_TTL) > now
    ]
    if len(kept) != len(load_anchors(u)) or kept != u.get("anchors"):
        u["anchors"] = kept


def _remembered(u: Dict[str, Any], now: float) -> set:
    """最近已经用过的由头文本——派生时拿它避重。"""
    out = set()
    for a in load_anchors(u):
        used = _f(a.get("used_at"))
        if used and now - used < ANCHOR_HISTORY:
            out.add(_s(a.get("about")))
    return out


def add_anchor(
    u: Dict[str, Any],
    kind: str,
    about: str,
    *,
    due: float = 0.0,
    now: float = 0.0,
    ttl: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """挂一条由头。同一件事不重复挂。返回挂上去的那条（重复时返回 None）。

    有效期默认按种类走（见 _KIND_TTL），也可以显式给。
    """
    if ttl is None:
        ttl = _KIND_TTL.get(str(kind or KIND_DERIVED), ANCHOR_TTL)
    text = _s(about)[:40]
    if not text:
        return None
    prune_anchors(u, now)
    for a in load_anchors(u):
        if _s(a.get("about")) == text:
            return None
    item = {
        "kind": str(kind or KIND_DERIVED),
        "about": text,
        "due": _f(due) or (now if now else 0.0),
        "used_at": 0.0,
        "use_count": 0,
        "expire_at": (now if now else 0.0) + ttl,
    }
    # ⚠️ 必须**同时保留已用过的那些**。它们是「已消费记录」：派生时靠它避重，
    # 冷却期也靠它算。只留未用的会把消费记录抹掉，于是同一句由头每轮被重新派生
    # 出来一次——表现是消息一条发不出去、模型却一直白转（实测 5 天 135 次调用 0 条送达）。
    all_items = load_anchors(u)
    used_items = [a for a in all_items if _f(a.get("used_at"))]
    free_items = [a for a in all_items if not _f(a.get("used_at"))]
    free_items.append(item)
    # 未用的最多 ANCHOR_MAX 条，满了挤掉最老的（已用的不参与这个上限）。
    # 注意这是**存储**上限，而实际每轮能派生的条数更少——未用的会被 prune 当过期丢掉
    # （见 prune_anchors 的说明），所以真正起作用的是「一条用完就让位」。
    free_items.sort(key=lambda a: _f(a.get("due")) or now)
    while len(free_items) > ANCHOR_MAX:
        free_items.pop(0)
    u["anchors"] = used_items + free_items
    return item


def live_anchors(
    u: Dict[str, Any],
    now: float,
    limit: int = ANCHOR_MAX,
    kinds: Optional[Tuple[str, ...]] = None,
) -> List[Dict[str, Any]]:
    """现在能用（已到点、没在冷却、没过期）的由头，按「有多硬」再按到期排。

    `kinds` 限定只取某几类。念想通道必须这么限：它只说「关于 TA 的话」，
    而主路径的存量由头是「她今天在干什么」——两类混在一起时，念想通道会拿到一条
    自述由头，然后因为「正文里没有你」被自己的验收拦下，表现为「念想一条都发不出去」。
    """
    prune_anchors(u, now)
    out: List[Dict[str, Any]] = []
    for a in load_anchors(u):
        if kinds is not None and _s(a.get("kind")) not in kinds:
            continue
        used = _f(a.get("used_at"))
        if used:
            if now - used < ANCHOR_COOLDOWN:
                continue                      # 冷却中
        if int(_f(a.get("tries"))) >= ANCHOR_MAX_TRIES:
            continue                          # 试过了，写不出来
        due = _f(a.get("due"))
        if due and due > now:
            continue                          # 还没到点
        exp = _f(a.get("expire_at"))
        if exp and now > exp:
            continue
        out.append(a)
    out.sort(key=lambda a: (KIND_RANK.get(_s(a.get("kind")), 9), _f(a.get("due")) or now))
    return out[: max(1, int(limit))]


def consume(u: Dict[str, Any], about: str, now: float) -> None:
    """标记某条由头已用过。"""
    for a in load_anchors(u):
        if _s(a.get("about")) == _s(about):
            a["used_at"] = now
            a["tries"] = 0
            a["use_count"] = int(_f(a.get("use_count"))) + 1


# ── 派生 ──────────────────────────────────────────────────────

def _short(text: Any, n: int = 14) -> str:
    s = _s(text)
    return s[:n] if s else ""


def _derive_from_state(
    body: Dict[str, Any], u: Dict[str, Any], now: float, taken: set
) -> List[str]:
    """从她自己的真实状态里，派生出「一件具体的事」。

    只认插件**确实知道**的事实：Core 契约里的日程/天气/体感、她与对方的沉默时长。
    没有新的事实就返回空列表——那正是「没什么可说的」的正确表达。
    """
    out: List[str] = []
    body = body if isinstance(body, dict) else {}
    activity = body.get("activity") if isinstance(body.get("activity"), dict) else {}
    day = body.get("day") if isinstance(body.get("day"), dict) else {}

    def offer(text: str) -> None:
        text = _short(text)
        if not text or text in taken:
            return
        taken.add(text)
        out.append(text)

    doing = _short(day.get("doing") or activity.get("schedule_event"), 12)
    if doing:
        offer(f"刚忙完{doing}")

    nxt = day.get("next")
    if isinstance(nxt, list) and nxt:
        first = _short(nxt[0], 12)
        if first:
            offer(f"接下来是{first}")

    # 天气可能是 {"env": "外面在下雨"} 这种结构。先取字典再转字符串——顺序反了
    # 会得到 "{'env': '外面在" 这种字面量，混进由头里一路发给模型。
    weather = body.get("weather")
    if isinstance(weather, dict):
        weather = weather.get("env") or weather.get("weather")
    weather = _s(weather)
    if weather:
        offer(_short(weather, 12))

    # 沉默时长是一句真实的话，但**不能按天数逐天变**。写成 f"快{N}天没说话了" 的话，
    # 每天都是一句新的话：既不会被「已用过」挡掉（于是每天都能重新派生），
    # 又和上一句说的是同一件事——这正是真实记录里「某一晚 23 条沙发追剧」的成因。
    # 所以只用**几个固定的档位**，每档一辈子只出现一次，用完就没有了。
    # ⓪ 她今天**自己想做**的事。
    #
    # 这是最好的一类：她真想去某地、真想去看什么，说出来有内容、有画面，
    # 而且是她自己挑的——比天气有意思，也不像工作。
    wants = day.get("wants")
    if isinstance(wants, list):
        for item in wants[:2]:
            text = _s(item).strip()
            if not text:
                continue
            body_txt = _short(text, 12)
            # 别拼成「想去拐去那家书店」——模型给的 event 本身常带「去/想」。
            if body_txt.startswith(("去", "来", "拐去", "拐到", "过去")):
                offer(f"她今天{body_txt}")
            elif body_txt.startswith("想"):
                offer(f"她今天{body_txt}")
            else:
                offer(f"她今天想去{body_txt}")

    # ① 她手上还没做完的那件事（Core 那边跨天跟着的 `ongoing`）。
    #
    # 这是**最好**的一类：她今天确实在干这件事，说出来有内容、有上下文，
    # 而且明天还能接着说——不像天气（说完就没了）也不像沉默时长（说一次就过期）。
    for task in _ongoing_tasks(body):
        offer(f"还在{_short(task, 14)}")

    # ② 她突然想起 TA 说过的一件事。
    #
    # 真人主动开口很少是「我这边发生了什么」，更多是「诶你上次说的那个」。
    # 账本里的 `said` 就是这个用途，之前只在 prompt 里当背景给模型看，
    # 没有人拿它当由头——于是这类最自然的开口方式从来没被用过。
    for item in _said_recent(u, now):
        offer(f"想起你之前说的{item}")

    # ③ 今天特别闲。
    #
    # 有了它，「没什么事」也能成为开口的理由——真人闲下来确实会冒一句。
    # 不用它的话，一到没事做的时段她就只能彻底安静，而那正是「想找人说说话」
    # 真实发生的时候。
    #
    # 注意 `feelings` 在契约里是**列表**（`[{text, weight}]`），不是字典。
    # 之前按字典取，这一行一执行就抛 AttributeError，而调用方的 except 把它吞成
    # 空列表 —— 整条由头派生路径静默死掉（实测 222 次调用 0 产出）。
    if _is_idle(body):
        offer("今天挺空的")

    seen = _f(u.get("last_seen"))
    if seen and now > seen:
        hours = (now - seen) / 3600.0
        if hours >= 24 * 7:
            offer("很久没见了")
        elif hours >= 24 * 3:
            offer("好几天没联系了")
        elif hours >= 20:
            offer("昨天之后就没顾上了")
    return out


def _is_idle(body: Dict[str, Any]) -> bool:
    """她此刻闲不闲。

    `feelings` 在契约里是**列表**（`[{"text":…, "weight":…}]`），也有版本给的是
    字典（`{"spare": 0.8}`）。两种都认。
    """
    feelings = (body or {}).get("feelings")
    if isinstance(feelings, dict):
        try:
            return float(feelings.get("spare", 0.0) or 0.0) >= 0.72
        except (TypeError, ValueError):
            return False
    if isinstance(feelings, list):
        for item in feelings:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "") or "")
            if "很闲" in text or "挺闲" in text or "不忙" in text:
                return True
    return False


def _ongoing_tasks(body: Dict[str, Any]) -> List[str]:
    """Core 契约里「她还没做完的事」。

    接不到就返回空——那是没装 Core 或者她今天确实没有跨天的事，
    两种都不该编一个出来。
    """
    out: List[str] = []
    for holder in (body or {}).get("ongoing"), (body or {}).get("ongoing_tasks"):
        if isinstance(holder, list):
            for item in holder:
                text = _s(item if isinstance(item, str) else
                          (item or {}).get("what", "") if isinstance(item, dict) else "").strip()
                if text:
                    out.append(text)
    return out[:3]


def _said_recent(u: Dict[str, Any], now: float) -> List[str]:
    """TA 说过、而且还没有下文的那些话（够新，说出来才像刚想起）。"""
    window = 14 * 86400.0
    out: List[str] = []
    for item in u.get("said") or []:
        if not isinstance(item, dict):
            continue
        text = _s(item.get("said", "")).strip()
        if not text or len(text) > 24:
            continue
        try:
            seen = float(item.get("ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            seen = 0.0
        if seen and (now - seen) <= window:
            out.append(text)
    return out[:2]


def prepare_round_anchors(
    u: Dict[str, Any],
    body: Dict[str, Any],
    now: float,
    *,
    limit: int = 3,
    affection: Optional[float] = None,
    relational: bool = False,
    tier: str = "",
    warmth: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """本轮这个人可以用的由头：存量池 + 当场派生。

    `relational=True` 时只走关系向（想念/牵挂/续聊），用于「念想」通道——那一类
    说的必须是关于 TA，而不是她自己今天干了什么。`affection` 是好感度，想念那一档要用。
    """
    prune_anchors(u, now)
    if tier:
        try:
            limit = max(1, int(TIER_POOL_MAX.get(tier, ANCHOR_MAX)))
        except (TypeError, ValueError):
            pass
    RELATIONAL_KINDS = (KIND_MISS, KIND_GREET, KIND_LOOP, KIND_CUE, KIND_PROMISE)
    picked: List[Dict[str, Any]] = live_anchors(
        u, now, limit=limit,
        kinds=RELATIONAL_KINDS if relational else None,
    )

    used = _remembered(u, now)
    for a in picked:
        used.add(_s(a.get("about")))

    if relational:
        candidates = derive_relations(u, affection, now, used)
        kind = KIND_MISS
        # 手上真没有新事发生 —— 真人这时候会随口说一句，不至于硬找个话题。
        # 「你有多久没理我了」本身就是理由。
        if not candidates:
            gap_candidates = derive_nag(u, now, used, tier=tier)
            if gap_candidates:
                return [c for c in gap_candidates]
    else:
        candidates = _derive_from_state(body, u, now, used)
        kind = KIND_DERIVED

    for text in candidates:
        item = add_anchor(u, kind, text, now=now)
        if item is not None:
            # ⚠️ 这里**不**立刻标 used_at。真正消费发生在消息发出去的那一刻
            # （engine._speak 调 anchors.consume）。一上来就消费的话，模型这次写砸了
            # （被验收层拦下）这个话题就白派生一次、再等 7 天——一次抽风毁掉一个由头。
            item.setdefault("tries", 0)
            picked.append(item)
        if len(picked) >= limit:
            break
    return picked


def mark_failed(u: Dict[str, Any], about: str, now: float) -> None:
    """这条由头这轮没写成。记一次失败；到上限就作废，别让它一直占着候选。"""
    for a in load_anchors(u):
        if _s(a.get("about")) != _s(about):
            continue
        tries = int(_f(a.get("tries"))) + 1
        a["tries"] = tries
        if tries >= ANCHOR_MAX_TRIES:
            a["used_at"] = now
        return


# ── 关系向的由头 ────────────────────────────────────────────────
#
# 真实 197 条记录里最扎手的一条：**没有一条是关于「你」或「你们」的**。
# 全是「我今天饿了」「我刚忙完方案」「我困了」——她自己的日记。
# 而一个人主动找另一个人聊，动因绝大多数是**对方的**，不是自己的：
# 想你、惦记你说过的那件事、想接着聊。
#
# 三种关系向由头，全部有据可依，不让模型现编：
#   想念  —— 好感够 + 想了一阵子（这是真实的心理状态，不需要外部证据）
#   牵挂  —— 对方最近说过自己累/忙/难受（从账本里找）
#   续聊  —— 对方说过的具体事还没结果（cue/loop 已经覆盖一部分，这里补"上次那个"）

# 对方说过自己状态不好的词。用来派「牵挂」，不是用来评判对方。
_CARE_WORDS = (
    "累", "好累", "疲惫", "困", "睡不着", "失眠", "加班", "通宵", "熬夜",
    "忙", "好忙", "赶", "deadline", "压力", "烦", "难受", "不开心",
    "emo", "崩溃", "撑不住", "头疼", "生病", "感冒", "发烧", "胃疼",
    "焦虑", "紧张", "难过", "委屈", "孤独", "一个人",
)


def _conversation(u: Dict[str, Any]) -> List[Dict[str, Any]]:
    conv = u.get("conversation")
    out: List[Dict[str, Any]] = []
    if isinstance(conv, list):
        for item in conv:
            if isinstance(item, dict) and str(item.get("text", "") or "").strip():
                out.append(item)
    return out


def derive_relations(
    u: Dict[str, Any],
    affection: Optional[float],
    now: float,
    taken: set,
    *,
    care_hours: float = 24.0,
) -> List[str]:
    """派生出「关于 TA / 关于你们」的由头。"""
    out: List[str] = []
    turn_texts = [
        (str(m.get("text", "") or ""), str(m.get("dir", "") or "")) for m in _conversation(u)
    ]
    theirs = [t for t, d in turn_texts if d != "out"]

    def offer(text: str) -> None:
        text = _short(text, 16)
        if not text or text in taken:
            return
        taken.add(text)
        out.append(text)

    # 牵挂：对方最近说过自己不好
    try:
        since = now - _f(u.get("last_seen"))
    except (TypeError, ValueError):
        since = 0.0
    if theirs and 0 < since <= care_hours * 3600.0:
        joined = " ".join(theirs[-6:])
        for w in _CARE_WORDS:
            if w in joined:
                offer(f"你上次说{w}，后来好点没")
                break

    # 续聊：对方说过的、还没结果的具体事（排除已经问过的那件）
    for text in reversed(theirs[-8:]):
        for seg in re.split(r"[，。！？；、,.!?;\s]+", text):
            seg = seg.strip()
            if not seg or len(seg) < 2 or len(seg) > 14:
                continue
            if seg in taken:
                continue
            offer(f"你上次说的「{seg}」后来呢")
            break
        if out and out[-1].startswith("你上次说的"):
            break

    # 想念：好感够 + 安静了一阵子
    try:
        aff = float(affection) if affection is not None else 0.0
    except (TypeError, ValueError):
        aff = 0.0
    if aff >= 60.0 and since >= 5 * 3600.0:
        offer("有点想你了")
    return out


# ── 没有新事发生时的由头 ──────────────────────────────────────────
#
# 真人「没事干问候一句，看你回不回」是常态：他不需要一个由头，**安静本身**就是理由。
# 以前问候硬绑早/午/晚三个日历窗口，一天最多三次，还必须从固定句池抽一句——
# 池子不消耗，于是某一晚连着 23 条全是同一件事。
_NAG_BY_GAP = (
    (6 * 3600.0, "你好久没理我了"),
    (3 * 3600.0, "好半天没动静了，你还在吗"),
    (90 * 60.0, "刚才怎么没声了"),
)


def derive_nag(u: Dict[str, Any], now: float, taken: set, *, tier: str = "") -> List[str]:
    """安静了一阵子 → 随口问一句。分档只是为了换措辞。"""
    seen = _f(u.get("last_seen"))
    if seen <= 0:
        return []
    gap = max(0.0, now - seen)
    out: List[str] = []
    for need, text in _NAG_BY_GAP:
        if gap >= need and text not in taken:
            taken.add(text)
            out.append(text)
            break
    if not out:
        return []
    item = add_anchor(u, KIND_GREET, out[0], now=now, ttl=TTL_DAY_PART)
    if item is None:
        return []
    item.setdefault("tries", 0)
    return [item]


# ── 转成给模型看的一句「为什么现在说这个」 ───────────────────────

_REASON_BY_KIND = {
    KIND_PROMISE: "你答应过TA这件事",
    KIND_CUE: "TA之前说过的那个时间到了",
    KIND_LOOP: "TA提过、一直没下文的那件事",
    KIND_THREAD: "刚才那句话说到一半断了",
    KIND_MISS: "你最近一直在想TA",
    KIND_GREET: "你们有一阵子没说话了",
    KIND_DERIVED: "你自己这边刚发生的事",
}


def anchor_sentence(a: Dict[str, Any]) -> str:
    """把一条由头写成提示词里的一句「给定事实」。

    关键在于它是**陈述**，不是提问。模型不需要「去找话题」——它只需要就这件事
    写一句话。之前让模型自己回答「你有什么由头」，得到的必然是
    「想表达此刻的孤独」这种关于说话而不是关于内容的句子。
    """
    about = _s(a.get("about")) or "一件事"
    kind = _s(a.get("kind")) or KIND_DERIVED
    lead = _REASON_BY_KIND.get(kind, _REASON_BY_KIND[KIND_DERIVED])
    return f"{lead}：{about}"


def anchor_meta(a: Dict[str, Any]) -> Dict[str, Any]:
    """由头驱动的那一类的 preset 元数据。

    `about` 是那件事本身（会给到模型作「要说哪件事」的答案），不是让模型去
    找话题——之前让它自己回答「你有什么由头」，得到的必然是关于说话而不是
    关于内容的句子。
    """
    about = _s(a.get("about")) or "这件事"
    kind = _s(a.get("kind")) or KIND_DERIVED
    mode = {
        KIND_PROMISE: "promise",
        KIND_CUE: "loop",
        KIND_LOOP: "loop",
        KIND_THREAD: "presence",
        KIND_MISS: "miss",
    }.get(kind, "share")
    return {
        "category": "anchor",
        "intent": "anchor",
        "mode": mode,
        "kind": kind,
        "msg_type": "share_thought",
        "msg_type_desc": f"就那件事说句话：{about}",
        # 想念这一类：明说「这条是关于 TA 的」，因为它是唯一一个**没有正事**的通道，
        # 模型很容易滑回「我今天怎么样」。
        "style_hint": (
            "这条是关于TA的，不是讲你自己；就这件事说，别解释你为什么现在说它"
            if kind == KIND_MISS else
            "就这件事，别重新起话题，也别解释你为什么现在说它"
        ),
        "about": about,
        "anchor": dict(a),
        # 给提示词的那句「给定事实」
        "anchor_fact": anchor_sentence(a),
    }


def anchor_topic(a: Dict[str, Any]) -> str:
    """给模型的「#」行用：就是那件事本身，几个字。"""
    return _s(a.get("about")) or "这件事"
