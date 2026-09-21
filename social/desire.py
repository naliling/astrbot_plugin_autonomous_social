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

# 念头攒到多少才算「想找 TA 说话」
FIRE_THRESHOLD = 0.88
# 念头的绝对上限，避免长期不结算的人攒出一个离谱的值
URGE_CEILING = 3.0
# 被否决（模型说不用发）之后念头回落到哪里：不是清零，「想过，先算了」
# 调高一点：话多的人想过但忍住了，过会儿又想说
SKIP_FALLBACK = 0.55
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

# 连续被冷落的念头天花板：越攒不出去，越说明对方不打算接
# 调高一点：更像脸皮厚一点的人，被冷落了也还会想找
STREAK_CAP: Dict[int, float] = {0: 3.0, 1: 2.6, 2: 2.0, 3: 1.5, 4: 1.2}
# 天花板地板必须高于发出门槛：被冷落再多次，念头也得留一条能攒过 FIRE_THRESHOLD 的
# 缝隙，只是慢。压到门槛以下等于对这个人永久静默——那不是「脸皮厚」，是彻底不再找了，
# 也正是「几百小时不再发一次」的成因。
STREAK_CAP_FLOOR = FIRE_THRESHOLD + 0.12
# 冷落几次之后，没有「真有由头」时天花板收紧，但仍留在门槛之上（不再压到门槛以下）
STREAK_NEEDS_CUE = 4
# 被冷落封顶后，隔多久没有任何来往就把冷落计数往回退一格：真人晾了很久也会
# 「算了再找一次看看」，而不是从此当这个人不存在。默认 3 天退一次。
STREAK_DECAY_DAYS = 3.0


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


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
    a = 0.5 if affection is None else clamp(float(affection) / 100.0, 0.0, 1.0)
    msgs = int(user.get("message_count", 0) or 0)
    familiar = clamp(math.log1p(msgs) / math.log1p(80), 0.0, 1.0)

    if weigh_reply_rate:
        # 回复率做拉普拉斯收缩：只发过一次、对方当时没回，不能就把 TA 当成不想理人
        sent = int(user.get("proactive_sent", 0) or 0)
        replied = int(user.get("proactive_replied", 0) or 0)
        rate = (replied + 1.0) / (sent + 2.0)
        streak = int(user.get("no_reply_streak", 0) or 0)
    else:
        rate = 0.5      # 中性：不因为回没回而抬高或压低
        streak = 0

    raw = 0.12 + 0.34 * a + 0.30 * familiar + 0.24 * rate - 0.06 * streak
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
    prev = user.get("interest")
    if prev is None:
        value = clamp(target, 0.05, 1.0)
    else:
        value = clamp(float(prev) * (1 - alpha) + target * alpha, 0.05, 1.0)
    user["interest"] = round(value, 4)
    if now is not None:
        user["interest_at"] = now
    return value


def hour_weight(user: Dict[str, Any], hour: int) -> float:
    """取对方在这一小时的历史活跃权重（0 表示从没见过 TA 这个点说话）。"""
    hours = user.get("active_hours")
    if not isinstance(hours, list) or len(hours) != 24:
        return 0.0
    try:
        return float(hours[hour % 24] or 0)
    except (TypeError, ValueError):
        return 0.0


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
    samples = int(user.get("rhythm_samples", 0) or 0)
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


def urge_cap(user: Dict[str, Any], has_live_cue: bool) -> float:
    """念头能攒到多高。被冷落得越多，天花板越低——但永远不低于发出门槛。

    过去的实现会在 streak≥STREAK_NEEDS_CUE 且没由头时把天花板压到门槛以下（×0.98），
    于是这个人除非主动发消息（after_reply 把 streak 归零），否则永远攒不到门槛、
    永远不会再被主动找——那就是“几百小时不发”。现在天花板下限卡在
    STREAK_CAP_FLOOR（高于门槛），被冷落只会拖慢节奏，不会彻底封死。
    """
    streak = int(user.get("no_reply_streak", 0) or 0)
    cap = STREAK_CAP.get(streak, STREAK_CAP_FLOOR)
    if streak >= STREAK_NEEDS_CUE and not has_live_cue:
        # 没话找话时收紧到下限，但不再压到门槛以下：就算没具体由头，晾久了也还能慢慢攒一次
        cap = min(cap, STREAK_CAP_FLOOR)
    return max(cap, STREAK_CAP_FLOOR)


def decay_streak(user: Dict[str, Any], now: float) -> None:
    """被冷落封顶后晾了很久：把冷落计数往回退，让她「算了再找一次」。

    以「最后一次主动发 / 最后一次对方说话」中较近的那个为起点：只要这段时间内
    真的没任何来往，每过 STREAK_DECAY_DAYS 就把 streak 降一格，直到 0。
    对方一旦重新说话，after_reply 会直接归零，这里只管“一直没人理”的情况。
    """
    streak = int(user.get("no_reply_streak", 0) or 0)
    if streak <= 0:
        return
    anchor = max(
        float(user.get("last_sent", 0) or 0),
        float(user.get("last_seen", 0) or 0),
    )
    if anchor <= 0:
        return
    window = STREAK_DECAY_DAYS * 86400.0
    if window <= 0:
        return
    steps = int((now - anchor) // window)
    if steps <= 0:
        return
    new_streak = max(0, streak - steps)
    if new_streak != streak:
        user["no_reply_streak"] = new_streak


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
    try:
        urge = float(user.get("urge", 0.0) or 0.0)
    except (TypeError, ValueError):
        urge = 0.0

    try:
        since = float(user.get("urge_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        since = 0.0
    if since <= 0:
        since = float(user.get("last_seen", 0) or 0) or now
        user["urge_at"] = since

    dt = now - since
    if dt < 0:
        # 系统时间被改过：不补算，只把基准挪到现在
        user["urge_at"] = now
        return urge

    # 刚聊过：话才说完，不需要再「主动」一次
    if now - float(user.get("last_seen", 0) or 0) < recent_talk_seconds:
        user["urge"] = 0.0
        user["urge_at"] = now
        return 0.0

    hours = dt / 3600.0
    if hours > 0:
        interest = float(user.get("interest", 0.35) or 0.35)
        # 播种名单里的人（从没聊过）攒得慢一倍：礼貌问题，不是节奏问题
        scale = user.get("urge_scale", 1.0)
        try:
            scale = clamp(float(scale), 0.1, 2.0)
        except (TypeError, ValueError):
            scale = 1.0
        rate = (BASE_RATE + INTEREST_RATE * interest) / max(refill_hours, 0.5) * scale
        factor = (rhythm if rhythm is not None else 1.0) * clamp(mood_factor, 0.0, 1.5)
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


def fire_worth(urge: float, gate: float = FIRE_THRESHOLD) -> bool:
    """念头是否已经足够到「会掏出手机说一句」的程度。"""
    return urge >= gate


def new_fire_gate() -> float:
    """抽下一次要把念头攒到多高才算真想说。

    固定门槛会让间隔变成固定的：攒满→发出→清零→再攒满，周期精确得像闹钟。真人
    有时想到就说，有时拖两天，所以每次说完重抽一个门槛。
    """
    return round(FIRE_THRESHOLD + random.uniform(0.0, 0.55), 3)


def after_send(user: Dict[str, Any], now: float) -> None:
    """发出去之后：念头落地，开始等对方接，并重新抽下次的门槛。"""
    user["urge"] = SENT_URGE
    user["urge_at"] = now
    user["pending_since"] = now
    user["pending_result"] = "waiting"
    user["fire_gate"] = new_fire_gate()


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
    user["no_reply_streak"] = int(user.get("no_reply_streak", 0) or 0) + 1
    user["urge"] = 0.0
    user["urge_at"] = now
    user["pending_since"] = 0.0
    user["pending_result"] = "ignored"
    # 轻微掋一下就好：后续的 interest_level/smooth_interest 会根据回复率与 streak 自行
    # 把基线拉回来，这里再重手只会把一个只是最近很忙的人越掋越边缘。地板拉高一点，
    # 避免 interest 被掋到 0.05 后念头慢到几乎不涨。
    interest = float(user.get("interest", 0.35) or 0.35)
    user["interest"] = round(clamp(interest - 0.03, 0.12, 1.0), 4)


def after_skip(user: Dict[str, Any], now: float) -> None:
    """想过，但决定不说：念头回落一点，过阵子可能还想说。"""
    user["urge"] = min(float(user.get("urge", 0.0) or 0.0), SKIP_FALLBACK)
    user["urge_at"] = now
    user["last_skip_at"] = now


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
        e = clamp(float(energy), 0.0, 100.0)
        if e < energy_threshold:
            factor *= 0.30
        elif e < 35:
            factor *= 0.65
        elif e > 75:
            factor *= 1.10
    if social_energy is not None:
        s = clamp(float(social_energy), 0.0, 100.0)
        if s < social_threshold:
            factor *= 0.28
        elif s < 40:
            factor *= 0.70
        elif s > 75:
            factor *= 1.05

    if body:
        # 真的在睡觉的时候不是「概率打折」，就是不该开口。
        if body.get("asleep"):
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


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
