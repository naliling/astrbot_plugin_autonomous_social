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

from .textutil import OUTSIDE_WORDS as _OUTSIDE_WORDS, outside_lead as _outside_lead

# 一个人身上同时挂着几条「还没用过的事」
ANCHOR_MAX = 5
# 一条由头文本最多多长。**拼装与存储必须用同一个上限**：拼装处若把上限卡得比它小，
# 句子末尾的模板（右引号、「后来呢」）会被切掉，产出一个未闭合的「引号
# （`你上次说的「明天面试`）。所以片段各自量好自己的尺寸，模板拼完不再二次截断。
ANCHOR_TEXT_MAX = 40
# 用过之后多久可以再用同一件事（秒）。它同时也是「已用记录」的寿命：冷却期内这条记录
# 既是避重依据、也不再是候选；冷却一过就淘汰，同一件事可以重新派生。
#
# 原来还有一个更长的 ANCHOR_HISTORY(45 天)，但它被有效期(≤24h)封着、从来活不到那天
# ——一个写了却永不生效的窗口比没有更误导人，收掉。
ANCHOR_COOLDOWN = 7 * 86400.0
# 一条由头最多试几次。**发出才算消费**；模型写砸了（被验收层拦下）只记一次失败，
# 下一轮还能再试——一次抽风不该让这个话题冷 7 天。但也不能无限重试。
ANCHOR_MAX_TRIES = 2

# 由头种类，按「有多硬」排
#
# 这里原来还有 KIND_PROMISE / KIND_LOOP / KIND_THREAD 三个，注释写着「她自己答应
# 过的」/「对方提过、还没下文」/「话说到一半断了」——但**全仓库没有任何地方产出
# 它们**。三个 kind 一直占着 TTL 表、优先级表、理由表和 `anchor_meta` 的 mode
# 分支，实际一条都没生成过。245 个真实用户的运行态实测分布是
# {derived: 839, miss: 250, greet: 109, weather: 1}，三者均为 0。
#
# 「承诺」与「话说到一半」这两种语义确实有用，但它们的**生产者不在这个模块**：
# `threads.py` 已经用一条更完整的链路发出 category=promise / presence 的线索
# （认领人、due、live_promises 都在那边）。anchors 这边再留一份同名的空壳，只会
# 让「由头」和「线索」两套机制看起来都管这件事、实际都没管。所以按「删掉永远不
# 为真的分支」处理，而不是在这里补一套重复的抽取逻辑。
#
# 老状态里若存过这三种 kind：`_KIND_TTL` 走 KIND_TTL_FALLBACK、`KIND_RANK` 走
# 默认 9（最低优先级）、`_REASON_BY_KIND` 走 KIND_DERIVED 的措辞，都能安全降级，
# 且它们的 TTL 最长 24h，不会长期滞留。
KIND_CUE = "cue"           # 对方说的时间锚点到期了
KIND_DERIVED = "derived"   # 从她自己的状态派生的一件具体的事
KIND_MISS = "miss"         # 想念：好感够、又想了一阵子（不是有正事要说）
KIND_GREET = "greet"       # 问候：没事干，随口说一句看看你回不回
KIND_HOLIDAY = "holiday"   # 节日：今天是个节，顺口提一句
KIND_WEATHER = "weather"   # 天气：外头变了样（下雨/降温），一句观察

# ── 一条用过的由头是**怎么结束的** ──────────────────────────────
#
# 原来「真发出去过」与「试满作废」在记录上完全一样（都只是把 `used_at` 写上），
# 运行指标与事后排障都分不出来：两条只在 `use_count` 上差一点，而跨用户撞车那一路
# 又另写一个 `reason` 字符串——三种去向三种记法。现在统一成一个字段、三选一。
RETIRED_SENT = "sent"          # 真发出去过（consume）
RETIRED_FAILED = "failed"      # 试满 ANCHOR_MAX_TRIES 都没写成（mark_failed）
RETIRED_CROSS = "cross_user"   # 这件事刚发给过别人，主动让位（drop_anchor）

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
    KIND_HOLIDAY: TTL_DAY_PART,
    KIND_WEATHER: TTL_SELF_EVENT,
}

# 表外的 kind 用的兜底寿命。
#
# 这里原来立着一个 `ANCHOR_TTL = 14 天`，看着像「派生由头的总寿命」，实际 9 种已知
# kind 在 `_KIND_TTL` 里全都有各自的值，它一次都不会生效——一个永不生效的数字比没有
# 更误导人（照着它调参、照着它排障，都调不到、也排不出）。
#
# 取**最短**的那一档（她这边刚发生的事，2h）：忘了登记的 kind 不该悄悄多活 24 小时、
# 白占着 ANCHOR_MAX 个位置。用例 `test_11_every_known_kind_has_a_ttl` 盯着「有无遗漏」，
# 这个兜底只是双保险。
KIND_TTL_FALLBACK = TTL_SELF_EVENT

# 每一个已知 kind 都必须在这里有一档。漏了的话 `live_anchors` 的
# `KIND_RANK.get(kind, 9)` 会把它当成最低优先级——`KIND_GREET` 就漏过一次，
# 而 `derive_nag` 造的正是一堆 greet：它明明是最常见的一类，却排在**所有**派生类
# 之后（默认 9），比天气还低。
# 数字只用来排序，允许留空档（0 档随 KIND_PROMISE 一起删了）；
# `live_anchors` 里 `KIND_RANK.get(kind, 9)` 的默认 9 是给表外的 kind 的兜底。
KIND_RANK = {
    KIND_CUE: 1,
    KIND_HOLIDAY: 3,
    KIND_MISS: 4,
    KIND_GREET: 5,
    KIND_DERIVED: 6,
    KIND_WEATHER: 7,
}


def _f(x: Any) -> float:
    """容错取时间戳/计数：读不出来、或非有限值，一律当 0.0（等同字段缺失）。

    nan/inf 放行的后果是静默的，而且每个调用点表现都不同：
      * `live_anchors` 的 `int(_f(a["tries"]))` → ValueError 带走该用户整轮派生
      * `consume` 的 `int(_f(a["use_count"])) + 1`、`mark_failed` 的 `int(_f(a["tries"])) + 1` 同样抛
      * `now - used < ANCHOR_COOLDOWN`（`used_at=nan`）→ 恒 False，冷却形同不存在
      * `due and due > now`（`due=nan`）→ 恒 False，那条由头永远「已经到点」
      * `exp and exp <= now`（`expire_at=nan`）→ 恒 False，永不淘汰，长期占住 ANCHOR_MAX 的槽
    归零以后这些都变成**确定行为**：0 就是「这个字段没写」。用过的由头再被派一次的风险
    由独立台账 `_remembered` 兜（见 `_note_used`），不靠这条记录本身活着。
    """
    try:
        out = float(x or 0)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _finite(x: Any) -> Optional[float]:
    """读得出且有限就返回，否则 None。

    与 `_f` 的分工：`_f` 给「0 是个合法默认值」的场合（时间戳、计数）；
    `_finite` 给「没有值就该整段略过、不许编一个 0」的场合（渲染给模型看的话）。
    """
    try:
        out = float(x)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _corrupt(data: Dict[str, Any], key: str) -> bool:
    """这个字段「在、但读不出来」吗（坏字符串 / nan / inf）。

    `_f` 把读不出来的值归 0，而 0 在这些时间戳字段上的语义正好是「从没用过 / 没有约束」
    ——于是坏值等于**永久放行**：冷却失效、试次上限失效、那条由头永远「已经到点」。
    `_f` 的归零只在「字段整个缺失」（老数据）时才是对的，所以判断时要区分两者：
    字段缺失 → False（按 0 处理）；字段在但读不出来 → True（调用方跳过这条记录）。
    """
    if key not in data:
        return False
    return _finite(data.get(key)) is None


def _s(x: Any) -> str:
    try:
        return str(x or "").strip()
    except Exception:
        return ""


def _talk_ts(u: Dict[str, Any], last_talk: Optional[float] = None) -> float:
    """「上次跟 TA 说话」的时刻。

    以 Core 的 `last_interaction`（每条消息都记）为准，没有才退回社交层自己的
    `last_seen`——后者只在社交层自己观察到消息时才更新，实测会落后 Core 几小时到
    几天，于是把「TA 刚说过话」误报成「好久没理你了」。
    """
    try:
        ts = float(last_talk)
    except (TypeError, ValueError):
        ts = 0.0
    return ts if ts > 0 else _f(u.get("last_seen"))


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
TIER_MIN_GAP_MINUTES = {TIER_HIGH: 20, TIER_MID: 60, TIER_LOW: 720}
# 各档同时挂几件事。**上限不许超过 ANCHOR_MAX**：池子本身只收 ANCHOR_MAX 条未用的
# 由头（见 `add_anchor`），原来写 12/8/4 的时候高档那两档是假的——`live_anchors(limit=12)`
# 永远取不满（池里最多 5 条），分档在这一维上等于没有；更糟的是派生循环会一路
# `picked.append` 到 12，而每加一条都可能把池子里最不急的那条挤掉，于是 `picked`
# 里留下**池子里已经没有的**悬空记录，`consume` / `mark_failed` 全都找不到它。
# 现在三档都在池子容量之内，高的那档就等于满池。
TIER_POOL_MAX = {TIER_HIGH: 5, TIER_MID: 4, TIER_LOW: 3}

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
    # 读不出来 / 非有限一律当 0：未知好感不是「很高」。原来裸 float 放行 nan，
    # 于是 `aff < TIER_AFFECTION_FLOOR` 恒为 False，一个好感度坏掉的用户会被判成
    # 中档、按中档的间隔去主动找人（这与 reasoning.relationship_tier 是同一个坑）。
    aff = _finite(affection)
    if aff is None:
        aff = 0.0
    base = _finite(base_affection)
    warmth = (aff - base) if base is not None else aff * 0.5

    # 最近这段时间对方说了多少句
    cutoff = now - recent_days * 86400.0 if now else 0.0
    talk = 0
    for item in _conversation(user):
        if str(item.get("dir", "")) == "out":
            continue
        ts = _f(item.get("ts", 0))
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
    # 用 _finite 而不是裸 float：nan 能通过 `float()`，于是 `f"{nan:.0f}"` 把字面
    # 「nan」直接写进提示词（实测 `tier_note('mid', nan, nan)` →
    # `'算熟、TA 对你的好感 nan、比刚开始高了 nan'`）。读不出来就整段略过，
    # 不许编一个 0 —— 「好感 0」和「不知道好感」对模型是两句完全不同的话。
    if affection is not None:
        value = _finite(affection)
        if value is not None:
            bits.append(f"TA 对你的好感 {value:.0f}")
    if warmth is not None:
        value = _finite(warmth)
        if value is not None:
            bits.append(f"比刚开始高了 {value:.0f}")
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
    """丢掉彻底过期的：用过的过了冷却，或任何一条过了自己的有效期。

    这里**不再把未用过的当过期**。原来的判据 `used_at + ANCHOR_HISTORY > now`
    对未用过的（`used_at=0`）恒为假，于是每调一次 `prune_anchors` 就把本轮刚派生的
    候选抹掉——而 `prune_anchors` 在 `add_anchor` / `live_anchors` / `prepare_round_anchors`
    里都会被调。后果是：一轮里派生了两条以上时，只有最后一条留在池子里，
    而引擎取的是 `picks[:1]`（第一条）——发出去后 `consume` 找不到它，`used_at` 永远
    写不上，下一轮同一件事又被重新派生；`mark_failed` 也失效，试次上限形同虚设。

    现在：未用的由头**留到自己的 expire_at**（派生类 2h、时段类 3h、对方那边 24h）；
    用过的留到冷却（避重靠它）——`consume` 会把它的 expire_at 推到冷却末，所以它不会
    像以前那样被那条短有效期提前抹掉，7 天冷却才真的成立。
    """
    kept = []
    for a in load_anchors(u):
        # 时间戳读不出来的记录直接扔掉：`_f` 归零后 `exp` 是 0，而 `if exp and exp <= now`
        # 对 0 恒假 —— 一条垃圾记录从此永不淘汰，长期占住 ANCHOR_MAX 的槽，把正常候选
        # 挤出去（实测 `expire_at=nan` 的记录 prune 之后还在）。正常记录都带这两个字段
        # （`add_anchor` 必写），读不出来就是坏的；`used_at` 坏掉时它的「用过了」也没法
        # 抄进台账（`_note_used` 需要那个时间戳），只能丢。
        if any(_corrupt(a, k) for k in ("used_at", "expire_at")):
            continue
        used = _f(a.get("used_at"))
        exp = _f(a.get("expire_at"))
        if used and used + ANCHOR_COOLDOWN <= now:
            continue
        if exp and exp <= now:
            # 用过的记录过期了，但它**还在冷却期内**——先把「这件事用过了」抄进独立
            # 台账（`_note_used`），再扔掉记录。老状态的记录带着当初那条短 TTL
            # （派生类 2h），池子一 prune 避重就跟着没了，同一件事两小时后又能重新
            # 派生、再发给同一个人。
            if used:
                _note_used(u, _s(a.get("about")), used, why=_s(a.get("retired")))
            continue
        kept.append(a)
    if len(kept) != len(load_anchors(u)) or kept != u.get("anchors"):
        u["anchors"] = kept


# 「最近用过的事」独立记一本账，不挂在由头池上。
#
# 避重（`_remembered`）原来只在 `u["anchors"]` 里找 `used_at` 还在冷却期里的记录，
# 于是避重成不成立，取决于那条记录**有没有在池子里活下来**：池子满了会被挤掉、
# 状态保留会把池子整个清空、老记录还带着当初的短 TTL 被 prune 掉——任何一次池外
# 淘汰都让避重整体失效，同一件事又能被重新派生一遍（「23 条沙发追剧」的成因）。
_ANCHOR_USED_KEY = "anchor_used"
_ANCHOR_USED_KEEP = 64


def _used_log(u: Dict[str, Any]) -> List[Dict[str, Any]]:
    log = u.get(_ANCHOR_USED_KEY)
    return log if isinstance(log, list) else []


def _note_used(u: Dict[str, Any], about: str, now: float, why: str = "") -> None:
    """把「这件事用过了」记进独立的那本账——池子怎么清都不影响避重。

    `why` 是结束方式（RETIRED_*），排障时不用再去池子里交叉比对（池子可能已经空了）。
    """
    text = _s(about)
    if not text:
        return
    log = [
        r for r in _used_log(u)
        if isinstance(r, dict)
        and _s(r.get("about")) != text
        and now - _f(r.get("at")) < ANCHOR_COOLDOWN
    ]
    log.append({"about": text, "at": now, "why": _s(why)})
    u[_ANCHOR_USED_KEY] = log[-_ANCHOR_USED_KEEP:]


def _remembered(u: Dict[str, Any], now: float) -> set:
    """最近已经用过的由头文本——派生时拿它避重。

    池内记录与独立台账两处都看：前者是既有状态（老数据里只有它），后者是
    「记录已经不在池子里了，但这件事还在冷却里」的那部分。
    """
    out = set()
    for a in load_anchors(u):
        used = _f(a.get("used_at"))
        if used and now - used < ANCHOR_COOLDOWN:
            out.add(_s(a.get("about")))
    for r in _used_log(u):
        if not isinstance(r, dict):
            continue
        at = _f(r.get("at"))
        if at and now - at < ANCHOR_COOLDOWN:
            text = _s(r.get("about"))
            if text:
                out.add(text)
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
        ttl = _KIND_TTL.get(str(kind or KIND_DERIVED), KIND_TTL_FALLBACK)
    # 截到 ANCHOR_TEXT_MAX，与 `offer` 用的是**同一个**上限：两边不一致的话，返回给
    # 调用方的文本和存进池子的文本会不一样，`consume` / `mark_failed` 按文本查找就
    # 找不到对象了。
    text = _s(about)[:ANCHOR_TEXT_MAX]
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
    #
    # 淘汰必须从**最不急的那条**开始（`due` 最大的先走）。原来按 `due` 升序 `pop(0)`
    # 挤掉的是 `due` 最近、最该先发的那条：刚派生的新由头 `due=now` 会被自己立即挤出
    # 池子，而这里仍把它返回给调用方 → 悬空记录（`consume` / `mark_failed` 都找不到它），
    # 同一句话每轮重新派生、白烧 LLM。池里若已有 5 条带未来 `due` 的由头
    # （如 cue 由头），这个人此后任何新由头都发不出去。
    while len(free_items) > ANCHOR_MAX:
        victim = max(
            (a for a in free_items if a is not item),
            key=lambda a: _f(a.get("due")) or now,
            default=None,
        )
        if victim is None:
            break          # 池里只剩刚加入的这条，不能再淘汰
        free_items.remove(victim)
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
        # 状态坏掉的记录一律跳过，不拿它去开口。四个字段都用 `_corrupt` 判，而不是靠
        # `_f` 归零：归零在 `used_at` 上等于「从没用过」（冷却失效）、在 `due` 上等于
        # 「已经到点」（永远到点）、在 `expire_at` 上等于「永不过期」。字段整个缺失的
        # 老数据不算坏（`tries` 平时就不存在）。它自己的 `expire_at` 到点会由
        # `prune_anchors` 收走，所以跳过不会把它永久留在池子里。
        if any(_corrupt(a, k) for k in ("used_at", "tries", "due", "expire_at")):
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
    """标记某条由头已用过：**真发出去了**。

    同时把它的 expire_at 推到冷却末：这条记录从此是「避重依据」而不再是候选，寿命该跟着
    冷却走，而不是被当初那条短有效期（派生类 2h）提前抹掉——不然 7 天冷却形同虚设。

    记账分两处写：池内记录（`retired=sent`，看得见来源）+ 独立台账（池子被清也还在）。
    池内那条找不到也照样记台账——消息确实发出去了，这件事就该被避开。
    """
    _note_used(u, about, now, why=RETIRED_SENT)
    for a in load_anchors(u):
        if _s(a.get("about")) == _s(about):
            a["used_at"] = now
            a["tries"] = 0
            a["use_count"] = int(_f(a.get("use_count"))) + 1
            a["expire_at"] = now + ANCHOR_COOLDOWN
            a["retired"] = RETIRED_SENT


# ── 派生 ──────────────────────────────────────────────────────

def _short(text: Any, n: int = ANCHOR_TEXT_MAX) -> str:
    s = _s(text)
    return s[:n] if s else ""


def _clause(text: Any, n: int) -> str:
    """按停顿截到 n 字以内，别在句子中间硬切。

    给**整句原文**用（Core 的天气 `env`、对方说过的原话片段）：那种句子没有模板
    兜着，硬切出来的「外面下着大雨，风也很」就是一句断话，而「你上次说今天加班到
    十一点整个人，后来好点没」也一样。模板拼接的片段各自量好了尺寸，不需要它。
    """
    s = _s(text)
    if not s or len(s) <= n:
        return s
    head = re.split(r"[，。！？；、,.!?;]", s, maxsplit=1)[0]
    return head if 0 < len(head) <= n else s[:n]


def _derive_from_state(
    body: Dict[str, Any], u: Dict[str, Any], now: float, taken: set,
    *, last_talk: Optional[float] = None,
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
        # 上限与 `add_anchor` 的存储上限是同一个数：碎片各自量好尺寸再拼，模板
        # （引号、「后来呢」）永远不会被这一层切掉。原来这里统一 `_short(text, 14)`，
        # 于是**每一条**派生由头都被从尾巴上剁掉一截——不是零星几条，是所有。
        text = _short(text, ANCHOR_TEXT_MAX)
        if not text or text in taken:
            return
        taken.add(text)
        out.append(text)

    doing = _short(day.get("doing") or activity.get("schedule_event"), 12)
    # 「忙手里的活」「忙自己的事」这种 doing 是空话：拼成「刚忙完忙手里的活」等于没说，
    # 而模型拿到这种由头只会顺着编一句更空的。真实记录里这类由头出现过 11 次
    # （「刚忙完忙自己的事」×5、「刚忙完醒神」×6）。
    if doing and not is_vague_doing(doing):
        offer(f"刚忙完{doing}")

    nxt = day.get("next")
    if isinstance(nxt, list) and nxt:
        first = _short(nxt[0], 12)
        if first and not is_vague_doing(first):
            offer(f"接下来是{first}")

    # 天气（v1.27.41 起加闸）：只在**变了**、且距上次说天气够久时才当由头。
    # 原来这里是「有天气就给」——而天气永远有，于是它成了最稳定的一条由头；
    # 用户那边的真实记录就是「不是天气就是天气」。
    weather = weather_worth_telling(u, body, now)
    if weather:
        before = len(out)
        offer(weather)
        if len(out) > before:  # 真进了池子才算「说过」
            mark_weather_told(u, body, now)

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

    # ①-bis 她最近**做过**的事：**已删除**（v1.27.39）。
    #
    # 原来这里调 `_life_log_done(body, now)`，读 `body["life_log"]` 里 `kind=="did"`
    # 的条目。但它永远不可能命中，两处断在同一个地方：
    #   1. `core_bridge` 从不映射 `life_log` 这个键（Core 侧存在 `self.life_log`，
    #      契约里没有它）→ 拿到的永远是 None；
    #   2. 就算映射了，Core 的经历账本条目是 `{text, uid, at, until, src}`，**没有
    #      `kind` 字段**，而 Core 也没有任何一处写「did」条目。
    # 与其留一条永远不产出、却让读代码的人以为「刚做完X」在工作的死路，不如删掉。
    # 要恢复它，先让 Core 在账本里写 `kind`，再在 `core_bridge` 里映射——两步都得做。

    # ② 她突然想起 TA 说过的一件事。
    #
    # 真人主动开口很少是「我这边发生了什么」，更多是「诶你上次说的那个」。
    # 账本里的 `said` 就是这个用途，之前只在 prompt 里当背景给模型看，
    # 没有人拿它当由头——于是这类最自然的开口方式从来没被用过。
    for item in _said_recent(u, now):
        # 片段自己量好尺寸（模板「想起你之前说的」7 字 + 24 = 31 ≤ ANCHOR_TEXT_MAX）。
        # 原来这里一个字的余量都没留，全靠 offer 的 14 字硬切——切出来的正是句子中间。
        offer(f"想起你之前说的{_short(item, 24)}")

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

    seen = _talk_ts(u, last_talk)
    if seen and now > seen:
        hours = (now - seen) / 3600.0
        if hours >= 24 * 7:
            offer("很久没见了")
        elif hours >= 24 * 3:
            offer("好几天没联系了")
        elif hours >= 20:
            offer("昨天之后就没顾上了")
    return out


# 原句自己已经交代了方位时，不要再补一层前缀。Core 的 `weather.env` 给的就是
# 「外面在下雨」这种**整句**，硬加「外头」得到的是「外头外面在下雨」。
# 判据与词表搬到了 `social/textutil.py`：`groupflow.icebreak_topics` 原来各写了一份
# `f"外头{text}"`，同一个毛病在破冰链路上又犯一次。这里保留私有别名，
# 名字不变、行为不变（`_OUTSIDE_WORDS` / `_outside_lead` 见上面的 import）。


def _notable_weather(body: Dict[str, Any]) -> str:
    """外头「变了样」才值得专门说一句：下雨下雪、大热大冷。

    多云、晴、气温 20℃ 这种太平常，拿它当由头等于没话找话。
    """
    weather = (body or {}).get("weather")
    if isinstance(weather, dict):
        weather = weather.get("env") or weather.get("weather")
    text = _s(weather)
    if not text:
        return ""
    if any(w in text for w in ("雨", "雪", "雷", "雹", "雾", "霾")):
        return _clause(_outside_lead(text), 16)
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*[℃°]", text)
    if m:
        try:
            temp = float(m.group(1))
        except ValueError:
            temp = None
        if temp is not None and (temp <= 5.0 or temp >= 33.0):
            return _clause(_outside_lead(text), 16)
    return ""


def derive_extras(
    u: Dict[str, Any], body: Dict[str, Any], now: float, taken: set
) -> List[Tuple[str, str]]:
    """A1/L2：再补两类由头——节日 与 天气。返回 [(kind, text)]。

    节日来自 Core 契约（可预测的客观事实，不让模型现编）；天气只在「变了样」时才有
    意义（见 `_notable_weather`）。
    """
    out: List[Tuple[str, str]] = []

    def offer(kind: str, text: str) -> None:
        # 同 `_derive_from_state.offer`：模板自己量好，这一层只兜底、不二次截断。
        text = _short(text, ANCHOR_TEXT_MAX)
        if not text or text in taken:
            return
        taken.add(text)
        out.append((kind, text))

    body = body if isinstance(body, dict) else {}
    holiday = _short(body.get("holiday"), 12)
    if holiday:
        offer(KIND_HOLIDAY, f"今天是{holiday}")
    wx = _notable_weather(body)
    if wx:
        offer(KIND_WEATHER, wx)
    return out


# 「忙手里的活」「忙自己的事」这种 doing 是空话：拼成「刚忙完忙手里的活」等于没说，
# 模型拿到这种由头只会顺着编一句更空的。线上真实记录里这类由头出现过 11 次
# （「刚忙完忙自己的事」×5、「刚忙完醒神」×6），消息读起来就是「刚忙完事」。
_VAGUE_DOING = (
    "忙手里的活", "忙自己的事", "忙点事", "忙事情", "处理事情", "手头的事",
    "手边的事", "自己的事", "发呆", "休息", "闲着", "在家", "没什么", "没事",
)


def is_vague_doing(text: str) -> bool:
    """这句「在做什么」是不是等于什么都没说。

    只按**词表**判，不按长度：「追剧」「加班」「通勤」都是两个字，但它们是具体的事。
    """
    t = _s(text).strip()
    # 一个字不构成「在做什么」（「嗯」「忙」）；两个字可能是具体的（「追剧」「加班」）。
    if len(t) <= 1:
        return True
    return any(word in t for word in _VAGUE_DOING)


# ── 天气：只在**变了**的时候才值得说 ────────────────────────────
#
# 天气是唯一「永远有、永远具体」的素材：别的由头（手上在做的事、没做完的事、想做的事）
# 都可能空，天气不会空。于是它变成了默认台词——线上真实记录就是「不是天气就是天气」：
# 「天亮了」「今早阴冷」「天还阴着」「你那边也冻透了罢」连着几条。
#
# 加两道闸：① 和上次说过的**一字不差**就不说；② 距上次说天气不足 6 小时也不说
# （天气偶尔一小时里变一次，不该因此又开一次口）。
WEATHER_SEEN_KEY = "weather_seen"
WEATHER_SEEN_AT_KEY = "weather_seen_at"
WEATHER_REPEAT_MIN_SECONDS = 6 * 3600.0


def _weather_text(body: Any) -> str:
    """从契约的 weather 里取一句人话。取不到返回空串（不编）。"""
    weather = (body or {}).get("weather") if isinstance(body, dict) else None
    if isinstance(weather, dict):
        weather = weather.get("env") or weather.get("weather")
    return _s(weather).strip()


def weather_worth_telling(u: Dict[str, Any], body: Any, now: float) -> str:
    """这次值得说的天气；不值得返回空串。"""
    text = _weather_text(body)
    if not text:
        return ""
    last = _s((u or {}).get(WEATHER_SEEN_KEY))
    if last == text:
        return ""
    try:
        at = float((u or {}).get(WEATHER_SEEN_AT_KEY) or 0.0)
    except (TypeError, ValueError):
        at = 0.0
    if at > 0 and now - at < WEATHER_REPEAT_MIN_SECONDS:
        return ""
    return _short(text, 16)


def mark_weather_told(u: Dict[str, Any], body: Any, now: float) -> None:
    """记下「这次说过天气了」。

    记在**派生**的时候而不是发送成功之后：这一层的目的是别让天气变成默认台词，
    宁可偶尔少说一次，也不要因为「没发出去」而下一轮再说一遍同样的天气。
    """
    text = _weather_text(body)
    if not text or not isinstance(u, dict):
        return
    u[WEATHER_SEEN_KEY] = text
    try:
        u[WEATHER_SEEN_AT_KEY] = float(now or 0.0)
    except (TypeError, ValueError):
        u[WEATHER_SEEN_AT_KEY] = 0.0


def _is_idle(body: Dict[str, Any]) -> bool:
    """她此刻闲不闲。

    `feelings` 有三种形状，都得认：

    * `list[str]`——**契约实际给的就是这个**（`link.build_contract` 的 `feelings`
      是 `[str]`，按显著度取前几条）。以前这里只认 `list[dict]`，于是字符串元素全被
      `isinstance(item, dict)` 跳过，「今天挺空的」这条由头**一次都没产出过**；
    * `list[dict]`——旧版/别的写方给 `[{"text":…, "weight":…}]`；
    * `dict`——`{"spare": 0.8}` 这种老形状。
    """
    feelings = (body or {}).get("feelings")
    if isinstance(feelings, dict):
        try:
            return float(feelings.get("spare", 0.0) or 0.0) >= 0.72
        except (TypeError, ValueError):
            return False
    if isinstance(feelings, list):
        for item in feelings:
            if isinstance(item, dict):
                text = str(item.get("text", "") or "")
            elif isinstance(item, str):
                text = item
            else:
                continue
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


# 「她做过的事」当由头的那条派生**已删除**（v1.27.39）：Core 的账本没有 `kind`
# 字段、`core_bridge` 也不映射 `life_log`，那条路永远不产出。原因写在 `_derive_from_state`
# 的 ①-bis 注释里。恢复它需要先让 Core 写 `kind`，再在 bridge 里映射。


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
        seen = _f(item.get("ts", 0.0))
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
    last_talk: Optional[float] = None,
    missing_floor: float = 60.0,
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
    # `limit` 是「要几条」的愿望，池子只装得下 ANCHOR_MAX 条——再往上要就只能靠
    # 把池子里已有的挤出去来凑数，于是 (a) 返回的 `picked` 里混进**池子里已经
    # 没有的**悬空记录（`consume` / `mark_failed` 按文本找不到它），(b) 分档在上限
    # 这一维上纯属摆设。这里把上限收到池容量，三档才真的有区别：
    #   high 5 / mid 4 / low 3（变更前 12 / 8 / 4，而实际永远 ≤ 5）。
    limit = min(int(limit), ANCHOR_MAX)
    RELATIONAL_KINDS = (KIND_MISS, KIND_GREET, KIND_CUE)
    picked: List[Dict[str, Any]] = live_anchors(
        u, now, limit=limit,
        kinds=RELATIONAL_KINDS if relational else None,
    )

    used = _remembered(u, now)
    for a in picked:
        used.add(_s(a.get("about")))

    if relational:
        candidates = derive_relations(
            u, affection, now, used, last_talk=last_talk, missing_floor=missing_floor
        )
        kind = KIND_MISS
        # 手上真没有新事发生 —— 真人这时候会随口说一句，不至于硬找个话题。
        # 「你有多久没理我了」本身就是理由。
        if not candidates:
            gap_candidates = derive_nag(u, now, used, tier=tier, last_talk=last_talk)
            if gap_candidates:
                # 存量优先：`picked` 里已到点的关系向由头不能因为「当场也派生出了
                # 一句搭话」就整批丢掉——那会让 `_miss_pick` 本来能在旧由头上开口的
                # 机会白挂一轮，还把那句通用搭话消费进 7 天冷却。
                # 原来直接 `return gap_candidates` 就是这个后果。
                return list(picked) + list(gap_candidates)
    else:
        candidates = _derive_from_state(body, u, now, used, last_talk=last_talk)
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
    # A1/L2：再补节日/天气两类由头。只在正常主动那条路给——「念想」通道说的必须是
    # 关于 TA 的，插一句「今天是中秋」就跑题了。
    if not relational:
        for extra_kind, text in derive_extras(u, body, now, used):
            if len(picked) >= limit:
                # 到量就停：再 `add_anchor` 会把池子里最不急的一条挤出去，
                # 而那条可能正好在 `picked` 里——留下一条池中已无的悬空记录。
                break
            item = add_anchor(u, extra_kind, text, now=now)
            if item is not None:
                item.setdefault("tries", 0)
                picked.append(item)
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
            # 与 `consume` 同一口径：一旦作废，这条记录只剩「避重依据」这一个身份，
            # 寿命就该跟着 7 天冷却走。只写 `used_at` 的话，它会被当初那条短 TTL
            # （派生类 2h / 时段类 3h / 对方类 24h）删掉——7 天冷却与 `_remembered`
            # 避重同时失效，同一句话两小时后就能重新派生、再发给同一个人。
            a["expire_at"] = now + ANCHOR_COOLDOWN
            # 「试满作废」与「真发出去了」必须分得开：排障时要能一眼看出这条是
            # 模型三次都没写成、而不是发出去过了。
            a["retired"] = RETIRED_FAILED
            _note_used(u, about, now, why=RETIRED_FAILED)
        return


def drop_anchor(u: Dict[str, Any], about: str, now: float) -> None:
    """把这条由头立刻作废，让开位置。

    给「同一件事刚发给过别人」用：那种情况不是「这次没写成」，是**这件事本身**
    该换一件，所以不等 `ANCHOR_MAX_TRIES` 试满，当场让位。
    """
    kept = []
    for a in load_anchors(u):
        if _s(a.get("about")) == _s(about) and not _f(a.get("used_at")):
            a["used_at"] = now
            a["reason"] = "跨用户撞车，换一件事"
            a["retired"] = RETIRED_CROSS
            # 同上：作废记录活到冷却末，而不是被自己的短有效期提前抹掉。
            a["expire_at"] = now + ANCHOR_COOLDOWN
            _note_used(u, about, now, why=RETIRED_CROSS)
        kept.append(a)
    u["anchors"] = kept


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

# 单字状态词的「假朋友」：它们跟别的字组成完全无关的常用词，子串匹配会把
# 「帮忙 / 积累 / 困难 / 赶紧 / 麻烦」通通读成「对方状态不好」，于是派出一句
# 「你上次说忙，后来好点没」——而对方从没说过自己忙。双字以上（好累、加班、
# 撑不住…）没有这个问题，照旧子串匹配。
_CARE_FALSE_FRIENDS = (
    "帮忙", "急忙", "连忙", "赶忙",
    "积累", "累计",
    "困难", "困住", "围困",
    "赶紧", "赶路", "追赶", "赶车", "赶集",
    "麻烦", "烦琐", "繁琐",
)


def _care_hit(text: str) -> str:
    """在 text 里找第一个「对方说自己状态不好」的词；找不到返回空串。

    单字词命中后要再看一眼上下文：只要它和相邻那个字凑成 `_CARE_FALSE_FRIENDS`
    里的词，就跳过这一处、接着往后找（同一句里可能还有真的）。
    """
    for w in _CARE_WORDS:
        if len(w) > 1:
            if w in text:
                return w
            continue
        for m in re.finditer(re.escape(w), text):
            i = m.start()
            if text[i:i + 2] in _CARE_FALSE_FRIENDS:
                continue
            if text[max(0, i - 1):i + 1] in _CARE_FALSE_FRIENDS:
                continue
            return w
    return ""


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
    last_talk: Optional[float] = None,
    missing_floor: float = 60.0,
) -> List[str]:
    """派生出「关于 TA / 关于你们」的由头。"""
    out: List[str] = []
    turn_texts = [
        (str(m.get("text", "") or ""), str(m.get("dir", "") or "")) for m in _conversation(u)
    ]
    theirs = [t for t, d in turn_texts if d != "out"]

    def offer(text: str) -> None:
        # 与 `_derive_from_state.offer` 同理：模板先按 ANCHOR_TEXT_MAX 兜底，
        # 片段的尺寸由各自的构造点负责（见下面「你上次说的…」那句）。
        text = _short(text, ANCHOR_TEXT_MAX)
        if not text or text in taken:
            return
        taken.add(text)
        out.append(text)

    # 牵挂：对方最近说过自己不好
    try:
        since = now - _talk_ts(u, last_talk)
    except (TypeError, ValueError):
        since = 0.0
    if theirs and 0 < since <= care_hours * 3600.0:
        # 带**对方原句片段**，不是光一个状态词：原来输出的是裸词「你上次说忙，后来
        # 好点没」——对方到底说了什么，读这句话的模型根本不知道，只能顺着那个字编。
        # 从最近一条往上找第一条真的说了状态不好的消息。
        for text in reversed(theirs[-6:]):
            if not _care_hit(text):
                continue
            clause = _clause(text, 12)
            if clause:
                offer(f"你上次说{clause}，后来好点没")
            break

    # 续聊：对方说过的、还没结果的具体事（排除已经问过的那件）
    for text in reversed(theirs[-8:]):
        for seg in re.split(r"[，。！？；、,.!?;\s]+", text):
            seg = seg.strip()
            if not seg or len(seg) < 2:
                continue
            # 先量好再拼：模板「你上次说的「…」后来呢」一共 6+len(seg)+4 个字，
            # seg 截到 12 之后整句最长 22 字，ANCHOR_TEXT_MAX=40 装得下。
            # 原来这里 seg 放到 14、拼接完再统一 `_short(text, 14)`：整句被从
            # 第 14 个字剁掉，右引号和「后来呢」全没了，产出的是「你上次说的「明天面试」
            # 这种**未闭合的引号**——不是在说事，是在掉字。截断必须发生在片段上，
            # 不能发生在模板上（#4 与 #5 是同一个根因）。
            seg = _short(seg, 12)
            if len(seg) < 2 or seg in taken:
                continue
            offer(f"你上次说的「{seg}」后来呢")
            break
        if out and out[-1].startswith("你上次说的"):
            break

    # 想念：好感够 + 安静了一阵子。
    # 走 _finite：nan 会让下面两个 `aff >= floor` 恒为 False，一个好感度坏掉的用户
    # 连「有点想你了」和搭话都永远开不出口（静默少发，没有任何日志）。
    aff = _finite(affection)
    if aff is None:
        aff = 0.0
    floor = _finite(missing_floor)
    if floor is None:
        floor = 0.0
    if aff >= floor and since >= 5 * 3600.0:
        offer("有点想你了")
    # 搭话：没什么事，就是想找 TA 说句话。关系不算生、也安静了一会儿，就开个口。
    # 这是「她会主动打开话头」的那一类——**不需要对方先说过什么**。
    if aff >= floor and since >= 2 * 3600.0:
        for text in _CASUAL_OPENERS:
            if text not in taken:
                offer(text)
                break
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

# 「没事也想搭一句话」的开场白。与 `_NAG_BY_GAP` 的区别：那些是抱怨（你不理我），
# 这些是**中性搭话**（在干嘛呢）。用过的会进由头冷却，不会天天同一句。
_CASUAL_OPENERS = (
    "在干嘛呢",
    "闲着没事，跟你说句话",
    "突然想找你聊两句",
)


def derive_nag(u: Dict[str, Any], now: float, taken: set, *, tier: str = "",
               last_talk: Optional[float] = None) -> List[str]:
    """安静了一阵子 → 随口问一句。分档只是为了换措辞。"""
    seen = _talk_ts(u, last_talk)
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
    KIND_CUE: "TA之前说过的那个时间到了",
    KIND_MISS: "你最近一直在想TA",
    KIND_GREET: "你们有一阵子没说话了",
    KIND_DERIVED: "你自己这边刚发生的事",
    KIND_HOLIDAY: "今天是个节日",
    KIND_WEATHER: "外头的天气变了样",
}

# 「这条由头是怎么结束的」给人看的话。原来是三种去向三种记法（`used_at`、
# `tries`、`reason` 各写各的），运行指标与面板都分不出「真发出去过」与
# 「试满作废」；现在一律看 `retired` 这一个字段。
_RETIRED_NOTE = {
    RETIRED_SENT: "已发出",
    RETIRED_FAILED: "试满作废",
    RETIRED_CROSS: "跨用户撞车，换一件事",
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
    retired = _s(a.get("retired"))
    mode = {
        KIND_CUE: "loop",
        KIND_MISS: "miss",
        KIND_HOLIDAY: "holiday",
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
        # 这条由头的去向，一栏说清："" 还没结束 / sent 真发出去了 /
        # failed 试满 ANCHOR_MAX_TRIES 没写成 / cross_user 让位给别人。
        # `reason` 保留给老面板看（跨用户撞车那一路原来把话写在这里）。
        "retired": retired,
        "retired_note": _RETIRED_NOTE.get(retired, ""),
        "reason": _s(a.get("reason")) or _RETIRED_NOTE.get(retired, ""),
        # 给提示词的那句「给定事实」
        "anchor_fact": anchor_sentence(a),
    }
