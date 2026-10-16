"""念头模型：想不想找一个人，是慢慢攒出来的，不是每个周期抽签抽出来的。

旧做法是「每 2-9 分钟掷一次骰子，掷中了就发」，它的问题不在于概率算得不准，
而在于它根本没有「想不想」这个量：骰子对所有人一视同仁，也不管你们半小时前才
聊完、也不管你上一条发出去对方压根没回。所以发出来的东西必然像定时器。

这里换成一个可积累的量 urge（想说话的念头）：

    念头增速 = 在意程度 × 现在合不合适 × 我自己此刻的状态

- 越在意的人攒得越快，几天没说话的自然就会想到；不在意的人要攒很久。
- 刚聊过 → 念头直接清零。真人才不会在话刚说完之后又另起一句无关的。
- 我主动发了对方没回 → 念头增速被压低，连着几次就基本不会再主动找（真人逻辑）。
- 只在对方大概率醒着/在玩手机的时段攒得快，凌晨攒得极慢。

攒过阈值不等于就发：还要过 engine 里的规则闸门和模型的「该不该说」判断。
本模块只做纯计算，不碰磁盘也不碰 astrbot，方便单独验证。
"""

from __future__ import annotations

import math
import random
from typing import Any, Dict, Optional

# 念头攒到多少才算「想找 TA 说话」（提高阈值，降低主动频率）
#
# v1.19.0： 抬高了这一段：原来是 1.40~1.95，配合 2 小时的攒念头周期，一个会回消息的人
# 一天会收到 7~8 条——那不是「她想起我」，是节拍器。抬到 1.90~2.50 之后，同样的人在
# 16 个清醒小时里大约 3 条，而且**每一条发出去时念头都更高**（她确实攒了一阵），
# 提示词里给模型的状态也跟着更足。抬门槛不是让它更保守，是让每次开口都得挣到。
FIRE_THRESHOLD = 2.10
# 每次说完话重抽门槛时的抖动幅度：门槛落在 FIRE_THRESHOLD~FIRE_THRESHOLD+SPAN 之间。
# 判定时还会被 effective_gate 压到念头天花板之内，所以这个区间只是「节奏快慢」的
# 调节量，不再是「能不能发」的生死线。
FIRE_GATE_SPAN = 0.65
# 念头的绝对上限，避免长期不结算的人攒出一个离谱的值
URGE_CEILING = 3.0
# 被否决（模型说不用发）之后保留多少念头。清零式惩罚会让模型不敢说 NO（见 after_skip）
SKIP_KEEP_RATIO = 0.45
# 保留旧常量名以免外部引用断掉；新逻辑不再用它
SKIP_FALLBACK = 0.18
# 发送成功后念头清零
SENT_URGE = 0.0

# 在意度对增速的映射：interest=1 时约需 refill_hours 攒满，越低越慢
BASE_RATE = 0.25
INTEREST_RATE = 1.10

# 作息画像至少要有多少条消息才开始起作用
MIN_RHYTHM_SAMPLES = 4
# 命中/相邻/不命中的时机系数
RHYTHM_ON = 1.45
RHYTHM_NEAR = 0.95
RHYTHM_OFF = 0.48
# 还没有作息画像时的中性系数
RHYTHM_UNKNOWN = 1.10
# 直方图平滑权重：「晚上九点常在线」意味着八点十点也大概率醒着
RHYTHM_SMOOTH = (0.25, 0.5, 0.25)

# 被冷落后的克制：每被晾一次，开口门槛就抬高一点——越攒不出去，越要攒得更满才开口。
# 注意克制体现在**门槛**上，不是念头天花板：天花板会被 effective_gate 的 min() 夹住，
# 压低它反而让门槛更低、更容易发（方向是反的）。封顶值不高于 URGE_CEILING，
# 避免「被冷落够次数后永久不再主动」的自锁。时间上的克制另由
# engine._gap_left（最小间隔随 streak 拉长）承担，两者互补。
STREAK_GATE_STEP = 0.2
STREAK_GATE_MAX_STREAK = 4
# `interest_level` 里冷落惩罚项 -0.03*streak 的封顶格数。取 8 的依据有两条：
#   1. 与门槛那侧的克制封顶（STREAK_GATE_MAX_STREAK=4）同源，惩罚允许比门槛更早封顶，
#      但都在「连着 4~8 次没回」这个真人量级内；而 streak 本身每次被晾 +1、衰减要
#      STREAK_DECAY_DAYS(3 天) 才退一格，对一个从不回复的人可以涨到几十。
#   2. 0.03*8 = 0.24，正好等于回复率那一项的权重（0.24）。不封顶时 streak≈12 就把
#      interest 顶上 0.05 这个下限——而两条注释都说清了：streak 与 rate 量的是同一个
#      人际信号，惩罚不该重到把「聊过两年」整个抹掉、更不该变成永久出不来。
STREAK_INTEREST_PENALTY_CAP = 8
# 被冷落封顶后，隔多久没有任何来往就把冷落计数往回退一格：真人晾了很久也会
# 「算了再找一次看看」，而不是从此当这个人不存在。默认 3 天退一次。
STREAK_DECAY_DAYS = 3.0
# 熟络度满格需要的消息条数（chat messages）。用它把「聊过多少」和「最近回不回我」
# 混成好感度，避免近期热度把相处时长整段抹掉。
FAMILIARITY_SATURATION = 200


def clamp(x: float, lo: float, hi: float) -> float:
    # nan/inf 一律退回下界：这两个函数夹的是评分，非有限值不是「很大」而是「没意义」，
    # 直接放行会让调用点拿到 nan，再顺着比较（恒为 False）静默变成「永远不发」。
    # 连数值都不是的（"abc"/None）走同一条路：`math.isfinite` 对它们本身就会抛 TypeError，
    # 而这个函数的调用点全在决策链路上，不该在这里把整轮带走。
    try:
        value = float(x)
    except (TypeError, ValueError):
        return lo
    if not math.isfinite(value):
        return lo
    return lo if value < lo else hi if value > hi else value


# ─── 容错读取：这一层读的全是 state.json / Core 契约里的字段 ──────────────
#
# 它们可能被手改、被跨版本迁移写坏、被 Core 写成字符串（"12.5"/"false"）。读到脏值时
# **不能抛**：调用点 `engine.settle_minds` 的 per-user 循环里一次 ValueError 就会带走
# 这个角色这一轮的全部用户（那边有异常隔离兜底，但不该靠它兜）。统一走下面这几个
# helper，口径只有一套。

def _num(value: Any) -> Optional[float]:
    """能读成有限浮点就读，否则 None。

    nan/inf 一律当「读不出来」：它们在这几个评分里不是「很大」而是「没有意义」——
    nan 参与比较恒为 False，会把闸门静默变成「永远不发」而不是报错。
    """
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _float(value: Any, default: float = 0.0) -> float:
    out = _num(value)
    return default if out is None else out


def _int(value: Any, default: int = 0) -> int:
    """容错取整：`"12.5"` → 12、`True` → 1，读不出来才退回 default。"""
    out = _num(value)
    if out is None:
        return default
    try:
        return int(out)
    except (OverflowError, ValueError):
        return default


# 布尔字段的解析词表：与 Humanoid Core 的 `_as_bool`（humanoid/config.py）是**同一套**
# （"1/true/yes/y/on/是/开" 与 "0/false/no/n/off/否/关"）。Core 的布尔字段经常是字符串
# ——配置面板、手改 state.json、老版本迁移都能写出来——而 `if body.get("asleep")` 这种
# 真值判断会把 `"false"` 判成「她正在睡觉」，于是她一整天一条都不发。
_TRUE_STRINGS = {"1", "true", "yes", "y", "on", "是", "开"}
_FALSE_STRINGS = {"0", "false", "no", "n", "off", "否", "关"}


def truthy(value: Any, default: bool = False) -> bool:
    """按 Core 的口径把任意值解析成 bool；认不出来（含 list/dict）退回 default。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    return default


def familiarity(messages: int) -> float:
    """聊过多少条换算成 0~1 的熟络度。

    单独拿出来是因为它有第二个用处：在意程度里「TA 回不回我」占了一半，而回不回是
    近期状态——只用在意程度当好感度时，一个认识两年的人会被连着几次没回整段拉回
    「刚认识不久」。engine 把它按权重混进好感度，让关系是连续滑动的而不是跳档。

    满格用 200 条而不是几十条：log 在小样本段涨得很快，几十条就封顶的话
    聊过十句话的新人和聊过半年的熟人会是同一个熟络度。
    """
    try:
        msgs = max(0, int(messages))
    except (TypeError, ValueError):
        msgs = 0
    return clamp(math.log1p(msgs) / math.log1p(FAMILIARITY_SATURATION), 0.0, 1.0)


def interest_level(
    user: Dict[str, Any],
    affection: Optional[float] = None,
    *,
    weigh_reply_rate: bool = True,
) -> float:
    """这个人在我心里有多重要（0.05-1.0）。

    好感度、聊过的量、对方接不接我的话合成一个基线；被连续冷落会往下掉。
    返回的是瞬时值，调用方用 EMA 写回 user["interest"]，避免每轮跳来跳去。

    `weigh_reply_rate=False`（配置 adaptive_reply_rate=false）时，「接不接话」与
    「被冷落几次」都不参与：那两项会把一个只是最近很忙的人越算越边缘，有人明确不
    要这种自我降级。
    """
    # 没接 Core 时拿不到好感度：按「中性偏乐观」算，否则聊得再多的人也会被封在
    # 一个上不去的在意度里，主动消息会稀疏到看不出来插件在工作。
    # 读到脏值（Core 的快照、手改的 state.json）时同样退回中性 50，而不是抛。
    a = 0.5 if affection is None else clamp(_float(affection, 50.0) / 100.0, 0.0, 1.0)
    msgs = _int(user.get("message_count", 0), 0)
    familiar = familiarity(msgs)

    if weigh_reply_rate:
        # 回复率做拉普拉斯收缩：只发过一次、对方当时没回，不能就把 TA 当成不想理人
        sent = max(0, _int(user.get("proactive_sent", 0), 0))
        replied = max(0, _int(user.get("proactive_replied", 0), 0))
        rate = (replied + 1.0) / (sent + 2.0)
        streak = max(0, _int(user.get("no_reply_streak", 0), 0))
    else:
        rate = 0.5      # 中性：不因为回没回而抬高或压低
        streak = 0

    # streak 的权重原来和 rate 一样重，而两者量的是同一个人际信号（TA 回不理会你）。
    # 一起计的结果是：连着几次没回就直接把一个认识两年的人从「亲密」压到「刚认识
    # 不久」——消息一条没少，只是更冷，而那种落差本身就不像人。减半，并且熟络度
    # 单独给一条地板（见 engine 里的 affection 兜底），不让人际热度把时长抹掉。
    #
    # 惩罚项还要**封顶**（STREAK_INTEREST_PENALTY_CAP）：不封顶时 -0.03*streak 在
    # streak≈12 就把 raw 压到下限 0.05，而一个从不回复的人也正好能涨到十几二十次
    # ——结果是他被永久钉在最慢档，连「晾久了算了再找一次」的衰减都救不回来。
    penalty = 0.03 * min(streak, STREAK_INTEREST_PENALTY_CAP)
    raw = 0.12 + 0.34 * a + 0.30 * familiar + 0.24 * rate - penalty
    return clamp(raw, 0.05, 1.0)


def smooth_interest(
    user: Dict[str, Any],
    target: float,
    now: Optional[float] = None,
    alpha: float = 0.06,
) -> float:
    """把新的在意度平滑进旧值（第一次直接取值）。

    每个心跳都全量跟进会让 interest 跟着回复率小幅抖动；它应该是个慢变量，
    所以平滑系数拉得很低。
    """
    prev = _num(user.get("interest"))
    if prev is None:
        value = clamp(target, 0.05, 1.0)
    else:
        value = clamp(prev * (1 - alpha) + target * alpha, 0.05, 1.0)
    user["interest"] = round(value, 4)
    if now is not None:
        user["interest_at"] = now
    return value


def hour_weight(user: Dict[str, Any], hour: int) -> float:
    """取对方在这一小时的历史活跃权重（0 表示从没见过 TA 这个点说话）。"""
    hours = user.get("active_hours")
    if not isinstance(hours, list) or len(hours) != 24:
        return 0.0
    # 走 _float 而不是裸 float：nan/inf 会顺着 smoothed_weight 传到 rhythm_factor，
    # 那里 `mine >= peak * 0.6` 对 nan 恒为 False，于是静默退成 RHYTHM_OFF(0.48) ——
    # 本该是「样本不可信 → 中性 1.10」，结果这个人的主动念头被砍掉一半多，还没有日志。
    return _float(hours[hour % 24], 0.0)


def smoothed_weight(user: Dict[str, Any], hour: int) -> float:
    """该小时及其左右各一小时的加权活跃度。"""
    prev_w, this_w, next_w = RHYTHM_SMOOTH
    return (
        prev_w * hour_weight(user, hour - 1)
        + this_w * hour_weight(user, hour)
        + next_w * hour_weight(user, hour + 1)
    )


def rhythm_factor(user: Dict[str, Any], hour: int) -> float:
    """按对方的作息判断现在合不合适。

    真人找朋友说话会下意识挑对方在玩手机的时候。凌晨四点给对方发消息，本身就是
    「我是机器人」的最强证据 —— 所以不在对方活跃时段时念头攒得极慢。
    样本还不够时不做判断（中性），避免新认识的人被作息规则误伤。
    """
    samples = _int(user.get("rhythm_samples", 0), 0)
    if samples < MIN_RHYTHM_SAMPLES:
        return RHYTHM_UNKNOWN

    peak = max((smoothed_weight(user, h) for h in range(24)), default=0.0)
    if peak <= 0:
        return RHYTHM_UNKNOWN
    mine = smoothed_weight(user, hour)
    if mine >= peak * 0.6:
        return RHYTHM_ON
    if mine >= peak * 0.2:
        return RHYTHM_NEAR
    return RHYTHM_OFF


def decay_streak(user: Dict[str, Any], now: float) -> None:
    """被冷落封顶后晾了很久：把冷落计数往回退，让她「算了再找一次」。

    以「我最后一次主动开口」为起点：只要这段时间里没有再主动过，每过 STREAK_DECAY_DAYS
    就把 streak 降一格。锚点不能取 last_seen——对方只是正常聊天、没回主动消息时，
    每次说话都会刷新 last_seen，steps 恒为 0，冷落计数永远不衰减，于是这个人一旦
    被冷落够次数就再也回不到正常节奏。
    对方一旦重新接住主动消息，after_reply 会直接归零，这里只管“一直没人理”的情况。
    """
    streak = _int(user.get("no_reply_streak", 0), 0)
    if streak <= 0:
        return
    # 锚点必须是「最后一次被判定为没人理」，不能是 last_sent：只要她还在发，
    # now - last_sent 就永远小于衰减周期，冷落计数永远降不下来——对那些从不回复的
    # 联系人就变成无限期保持每天几条。老数据没有这个字段时退回 last_sent。
    anchor = _float(user.get("last_ignored_at", 0), 0.0)
    if anchor <= 0:
        anchor = _float(user.get("last_sent", 0), 0.0)
    if anchor <= 0:
        # 老数据没有 last_sent，退回「最后一次有来往」宁可慢退也不能不退
        anchor = _float(user.get("last_seen", 0), 0.0)
    if anchor <= 0:
        return
    window = STREAK_DECAY_DAYS * 86400.0
    if window <= 0:
        return
    steps = _int((now - anchor) // window, 0)
    if steps <= 0:
        return
    # 推进锚点：不推进的话 steps 会一直是 1，于是「每 3 天退一格」变成
    # 「过了 3 天之后每个心跳都退一格」，几个心跳内就清零了。
    user["last_ignored_at"] = anchor + steps * window
    new_streak = max(0, streak - steps)
    if new_streak != streak:
        user["no_reply_streak"] = new_streak


# 势头：刚聊开的时候攒得快，冷了一阵子会自己慢下来。
#
# 以前只有「被冷落」这一个方向——连着被无视之后会退。缺的是另一边：**聊开了是有惯性的**，
# 真人一旦热起来会连着说一阵子，而她现在是发完一条、下次照样等满一个周期，
# 于是「连发三条然后消失三天」这种最不像人的节奏照样出得来。
MOMENTUM_WINDOW_HOURS = 14.0
MOMENTUM_MAX = 1.45


def momentum_factor(user: Dict[str, Any], now: float) -> float:
    """最近聊得密 → 攒得快（最多 ×1.45）；隔了很久 → 回到 1.0。"""
    # 走 _num 而不是裸 float：nan 会让 hours 也是 nan，下面两个守卫
    # （hours < 0 / hours > MOMENTUM_WINDOW_HOURS）对 nan 全为 False，
    # 于是这里返回 nan，顺着 settle 写进 user["urge"]，这一轮的闸门静默永不通过。
    last_spoken = _num(user.get("last_spoken", 0.0))
    if last_spoken is None or last_spoken <= 0:
        return 1.0
    hours = (now - last_spoken) / 3600.0
    if not math.isfinite(hours) or hours < 0 or hours > MOMENTUM_WINDOW_HOURS:
        return 1.0
    # 刚说完的那会儿势头最足，几小时后淡掉
    ratio = 1.0 - (hours / MOMENTUM_WINDOW_HOURS)
    return 1.0 + (MOMENTUM_MAX - 1.0) * ratio


def settle(
    user: Dict[str, Any],
    now: float,
    *,
    refill_hours: float,
    recent_talk_seconds: float,
    mood_factor: float = 1.0,
    rhythm: Optional[float] = None,
    quiet: bool = False,
    live_cue: bool = False,
) -> float:
    """把 urge 结算到现在这一刻，返回结算后的念头值。

    惰性积分：只在被读到时按经过的时间补算，不需要为每个用户跑定时器。

    Args:
        user: 用户状态 dict（就地更新 urge / urge_at）
        now: 当前时间戳
        refill_hours: 最在意的人攒满一次念头需要的小时数
        recent_talk_seconds: 刚聊过多久之内念头清零
        mood_factor: 我自己此刻想说话的程度（精力/社交能量/是否被理）
        rhythm: 对方作息系数，None 表示自行判断
        quiet: 现在是否在安静时段
        live_cue: 是否有到点的由头（由头会额外推一把）

    Returns:
        结算后的 urge
    """
    urge = _float(user.get("urge", 0.0) or 0.0, 0.0)

    since = _float(user.get("urge_at", 0.0), 0.0)
    if since <= 0:
        # 没有 urge_at（老数据 / 从旧版本迁移过来 / 第一次见到这个人）时的播种。
        #
        # 以前是 `since = last_seen or now`。老用户的 last_seen 可能是几天前，于是升级
        # 后**第一次结算的 dt 长达数天**：urge 一步冲过 URGE_CEILING 并立即过闸，同一次
        # 结算里所有老用户一起 ready——全体老用户同时收到主动消息。那不是「她一直想着
        # 你」，是插件刚装好／刚升级，在补算它自己没运行的那段时间。
        #
        # 播种只用来记「从这一刻开始攒」，不补算过去。urge 本身是落盘的，老数据里已经
        # 攒下的那部分不会因为这里改口径而丢。
        user["urge_at"] = now
        since = now

    dt = now - since
    if dt < 0:
        # 系统时间被改过：不补算，只把基准挪到现在
        user["urge_at"] = now
        return urge

    # 刚聊过：话才说完，不需要再「主动」一次
    last_seen = _float(user.get("last_seen", 0), 0.0)
    if last_seen > 0 and now - last_seen < recent_talk_seconds:
        user["urge"] = 0.0
        user["urge_at"] = now
        return 0.0

    hours = dt / 3600.0
    if hours > 0:
        # interest 是个 0.05~1.0 的评分：落盘的脏值（手改/迁移写成 50 或 "0.4"）读不出来
        # 就退回 0.35，读出来则夹回量程。不夹的话 rate 会被放大几十倍，urge 一步到顶。
        interest = clamp(_float(user.get("interest", 0.35) or 0.35, 0.35), 0.0, 1.0)
        # 播种名单里的人（从没聊过）攒得慢一倍：礼貌问题，不是节奏问题
        scale = clamp(_float(user.get("urge_scale", 1.0), 1.0), 0.1, 2.0)
        rate = (BASE_RATE + INTEREST_RATE * interest) / max(refill_hours, 0.5) * scale
        # 两个入参都可能是外面算出来的脏值：rhythm 传进来 nan 会让 factor 变 nan，
        # 而 nan 一路走到 `user["urge"] = round(nan, 4)`，这一轮的闸门比较就恒为 False。
        rhythm_value = 1.0 if rhythm is None else _num(rhythm)
        if rhythm_value is None:
            rhythm_value = 1.0
        factor = rhythm_value * clamp(_float(mood_factor, 1.0), 0.0, 1.5)
        factor *= momentum_factor(user, now)
        if quiet:
            factor *= 0.12
        urge += hours * rate * factor
        if live_cue:
            # 由头到期是「想起来了」，不是慢慢攒出来的
            urge += 0.55

    urge = clamp(urge, 0.0, URGE_CEILING)
    user["urge"] = round(urge, 4)
    user["urge_at"] = now
    return urge


def new_fire_gate() -> float:
    """抽下一次要把念头攒到多高才算真想说。

    固定门槛会让间隔变成固定的：攒满→发出→清零→再攒满，周期精确得像闹钟。真人
    有时想到就说，有时拖两天，所以每次说完重抽一个门槛。
    """
    return round(FIRE_THRESHOLD + random.uniform(0.0, FIRE_GATE_SPAN), 3)


def effective_gate(user: Dict[str, Any], cap: float) -> float:
    """这一轮真正生效的发出门槛。

    门槛是每次说完话重抽的随机值，被冷落过之后逐次抬高（越晾越难开口）；
    最后压进念头天花板 cap 之内，避免门槛高过念头天花板导致永久发不出去的自锁。
    """
    # nan/inf 也算读不出来（`_float` 会拒掉）：nan 参与比较恒为 False，这个人会静默地
    # 永远发不出去——比抛异常更难查。
    gate = _float(user.get("fire_gate") or FIRE_THRESHOLD, FIRE_THRESHOLD)
    gate = max(gate, FIRE_THRESHOLD)
    streak = max(0, _int(user.get("no_reply_streak", 0), 0))
    gate += STREAK_GATE_STEP * min(streak, STREAK_GATE_MAX_STREAK)
    return min(gate, cap)


def after_send(user: Dict[str, Any], now: float, *, expect_reply: bool = True) -> None:
    """发出去之后：念头落地，重新抽下次的门槛。

    expect_reply=False 用于早安/晚安/收场这类**本来就不指望对方回**的话：
    它们不点「等一句回话」的计时器。以前早安也点，于是「从来没回过早安」被
    算成「连着被冷落」，念头天花板被永久压死；而 decay_streak 的锚点又是
    last_sent，每天问候都刷新它，衰减永远等不到那三天。
    """
    user["urge"] = SENT_URGE
    user["urge_at"] = now
    user["fire_gate"] = new_fire_gate()
    if expect_reply:
        user["pending_since"] = now
        user["pending_result"] = "waiting"


def after_reply(user: Dict[str, Any], now: float) -> None:
    """对方接了话：念头被满足，冷落计数归零。"""
    user["no_reply_streak"] = 0
    user["urge"] = SENT_URGE
    user["urge_at"] = now
    user["pending_since"] = 0.0
    user["pending_result"] = "replied"
    user["last_replied_at"] = now


def after_ignored(user: Dict[str, Any], now: float) -> None:
    """过了窗口对方没回：有点扫兴，下次再想找 TA 得攒更久。"""
    user["no_reply_streak"] = max(0, _int(user.get("no_reply_streak", 0), 0)) + 1
    user["urge"] = 0.0
    user["urge_at"] = now
    user["pending_since"] = 0.0
    user["pending_result"] = "ignored"
    # 冷落计数的衰减锚点用它，不是 last_sent（见 decay_streak）
    user["last_ignored_at"] = now
    # 轻微掋一下就好：后续的 interest_level/smooth_interest 会根据回复率与 streak 自行
    # 把基线拉回来，这里再重手只会把一个只是最近很忙的人越掋越边缘。地板拉高一点，
    # 避免 interest 被掋到 0.05 后念头慢到几乎不涨。
    interest = _float(user.get("interest") or 0.35, 0.35)
    user["interest"] = round(clamp(interest - 0.03, 0.12, 1.0), 4)


def forgive_late(user: Dict[str, Any], now: float) -> None:
    """过了窗口才回的那句，也算「TA 回我了」。

    为什么要单列一个：12 小时没回就会被 settle_pending 判成 ignored，于是 pending_since
    被清零。等到对方第二天早上回一句，那条记账已经不在了——after_reply 压根不会被调用，
    冷落计数一路涨、语气越来越冷。用户会明确感到「回复了也没用」。
    这里只把人叫回来：冷落清零、那件事不再悬着；**不进回复率分母**（分母是 2 小时
    那个口径，统计归统计）。
    """
    user["no_reply_streak"] = 0
    user["pending_since"] = 0.0
    user["pending_result"] = "replied"
    user["last_replied_at"] = now


def after_skip(user: Dict[str, Any], now: float) -> None:
    """想过，但决定不说。

    原来是压回 SKIP_FALLBACK(0.18)——清零式惩罚。那样「说不」其实很贵：模型会发现
    说一次 NO 会把攒了半天的念头作废，于是宁可硬挤一句也说 SEND。模型的判断于是不可信。

    改成**部分保留 + 抬高下次的门槛**：「想过但算了」本来就该让下一次开口更难一点
    （这是真实代价，不是惩罚），但不该把之前的积累作废。这样 decide 的 NO 才是可以
    放心说的话，攒下的念头也不会因为一次没发成而白扔。
    """
    urge = _float(user.get("urge") or 0.0, 0.0)
    user["urge"] = round(max(0.0, urge * SKIP_KEEP_RATIO), 4)
    user["urge_at"] = now
    user["last_skip_at"] = now
    base = _float(user.get("fire_gate") or FIRE_THRESHOLD, FIRE_THRESHOLD)
    bump = random.uniform(0.0, FIRE_GATE_SPAN * 0.5)
    user["fire_gate"] = min(base + bump, URGE_CEILING)


def mood_multiplier(
    energy: Optional[float],
    social_energy: Optional[float],
    *,
    energy_threshold: float = 15.0,
    social_threshold: float = 20.0,
    body: Optional[Dict[str, Any]] = None,
) -> float:
    """我自己此刻有多想说句话（clamp 到 0.05-1.65）。

    累和不想说话的时候，人不会到处找人聊天 —— 这比「概率打折」更贴近实际：
    它让念头攒得慢，而不是攒满了再被随机数否掉。

    接了 Humanoid Core v2.14 的契约时，`body` 里就不止精力两个标量：她是不是真的在睡、
    欠不欠觉、饿不饿、身上舒不舒服，以及最关键的——她自己攒了多少想说话的心思。
    没接契约时这些全部落回旧的两个标量，行为与 v1.7.4 一致。
    """
    factor = 1.0
    if energy is not None:
        e = clamp(_float(energy), 0.0, 100.0)
        if e < energy_threshold:
            factor *= 0.30
        elif e < 35:
            factor *= 0.65
        elif e > 75:
            factor *= 1.10
    if social_energy is not None:
        s = clamp(_float(social_energy), 0.0, 100.0)
        if s < social_threshold:
            factor *= 0.28
        elif s < 40:
            factor *= 0.70
        elif s > 75:
            factor *= 1.05

    if body:
        # 真的在睡觉的时候不是「概率打折」，就是不该开口。
        # 用 truthy 而不是直接 if body.get("asleep")：Core 契约里这个字段可能是字符串
        # （"false"/"0"/"否"），真值判断会把「她没睡」判成「她正在睡觉」，factor 被砍到
        # 1/16，而她一整天一条都不会发——而且没有任何日志能看出原因。
        if truthy(body.get("asleep")):
            factor *= 0.06
        pressure = _num(body.get("sleep_pressure"))
        if pressure is not None:
            if pressure >= 88:
                factor *= 0.25
            elif pressure >= 72:
                factor *= 0.55
            elif pressure >= 55:
                factor *= 0.85
        discomfort = _num(body.get("discomfort"))
        if discomfort is not None and discomfort >= 62:
            factor *= 0.55
        hunger = _num(body.get("hunger"))
        if hunger is not None and hunger >= 88:
            factor *= 0.75
        desire = _num(body.get("social_desire"))
        if desire is not None:
            # 她自己的心思是油门也是刹车：独处攒满了想说，刚聊过就攒不出去。
            factor *= 0.55 + 0.9 * (clamp(desire, 0.0, 100.0) / 100.0)
    return clamp(factor, 0.05, 1.65)
