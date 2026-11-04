"""时区换算：把 epoch 折算成「她所在城市」的墙上时间，反之亦然。

Humanoid Core 契约里的 ``utc_offset_minutes`` 是她所在城市对 UTC 的偏移（绝对值，
如北京 +480、加里宁格勒 +120）。要拿到她的挂钟时间必须从 **UTC 基准** 换算：
``UTC + 偏移``。以前用 ``datetime.fromtimestamp``（本机时区基准）再加偏移，等于把
插件所在容器的本地时区偏移也算了进去——容器在北京、她也在北京时会被多加 8 小时，
于是真实 15:00 被当成 23:00，主动消息大白天发「晚安」。这里统一改用 UTC 基准，
结果与插件容器时区无关。

offset 为 None 表示没有 Core（或关掉了 use_core_clock）：退回本机/内置时钟。
"""

from __future__ import annotations

import calendar
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple


def _offset(value: Optional[int]) -> Optional[float]:
    """把契约里拿来的时区偏移收成一个可信的分钟数，不可信就当作没有。

    `utc_offset_minutes` 是**跨插件**来的值（Core 契约 / 用户手改的 state），
    以前两处都直接拿它参与运算：`timedelta(minutes=nan)` 抛 ValueError、
    `nan * 60.0` 得到 nan 之后 `city_now(nan)` 再抛。时区读坏了不该让整轮心跳
    崩掉，退回本机时钟（也就是「没有 Core」的那条路）是能继续跑的降级。
    """
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def city_now(epoch: float, offset_minutes: Optional[int]) -> datetime:
    """把 epoch 换算成「她所在城市」的墙上时间（naive datetime）。

    offset_minutes=None → 退回本机时钟；否则用 UTC + 偏移，不受容器时区影响。
    """
    offset = _offset(offset_minutes)
    if offset is None:
        return datetime.fromtimestamp(epoch)
    utc = datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
    return utc + timedelta(minutes=offset)


def city_epoch(dt: datetime, offset_minutes: Optional[int]) -> float:
    """city_now 的逆运算：把一个「她所在城市」的墙上时间折回 epoch。"""
    offset = _offset(offset_minutes)
    if offset is None:
        return dt.timestamp()
    return float(calendar.timegm(dt.timetuple())) - offset * 60.0


def _wall_clock_token(value: Any, span: int) -> Optional[int]:
    """把墙上时间的一个分量收成可用整数；不合法就给 None（=「看不懂」）。"""
    try:
        out = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if 0 <= out < span else None

def _window_token(value: Any) -> Optional[int]:
    """安静时段窗口的端点（小时）：允许 **24**，表示「当天的 24:00」。

    和 `_wall_clock_token` 分开是因为两者语义不同：`hour` 永远是 0–23 的钟点，
    而窗口端点允许写 24——`0 → 24` 是「一整天」，`22 → 24` 是「到午夜」。
    `SocialConfig.in_quiet_hours` 一直就是这个口径（`start <= h < end`），
    v2.0.6 的 clock 助手如果把它当成越界丢掉，面板上填 0/24 的用户会看到
    「配置说安静、闸门说不安静」——同一个问题两套答案。
    """
    try:
        out = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if 0 <= out <= 24 else None


def _window_hour_float(value: Any) -> Optional[float]:
    """窗口端点的浮点版（Core 的夜是 1.75 这种半点）：同样保留 24。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    if out < 0.0 or out > 24.0:
        out = out % 24.0
    return out


def seconds_until_quiet_end(hour: int, minute: int, start: int, end: int) -> int:
    """现在离「安静时段结束」还有多少秒；不在安静时段 / 没有安静时段时返回 0。

    `start`/`end` 是配置里的 quiet_start/quiet_end（小时，0–23），与
    ``SocialConfig.in_quiet_hours`` 同一套语义：

    * ``start == end`` 表示**没有安静时段**（面板上把两头调成一样就是关掉它）；
    * ``start < end`` 是普通时段（如 1→6）：``[start:00, end:00)``；
    * ``start > end`` 是跨午夜（默认 23→7）：``[start:00, 24:00) ∪ [0:00, end:00)``。

    存在的意义（v1.28.16）：心跳循环每轮先睡 8–30 分钟再看一眼。安静时段结束时
    如果那一轮正好睡在中间，就该睡到结束点附近再评估，而不是等整段随机睡眠走完
    —— 否则「早上 8 点静音结束」可能要拖到 8:20 才有第一个动作。

    返回值一律是 **int 秒**：0 既表示「不在安静时段」也表示「安静时段没启用」，
    调用方只需要 ``if left <= 0: 不处理``。坏的 hour/minute/start/end（手改配置、
    nan、越界）一律按 0 处理——宁可这一轮按原节奏睡，也不因为一个坏字段把心跳
    变成忙循环（那时 `sleep(负数)` 会立刻返回）。
    """
    h = _wall_clock_token(hour, 24)
    m = _wall_clock_token(minute, 60)
    qs = _window_token(start)
    qe = _window_token(end)
    if h is None or m is None or qs is None or qe is None:
        return 0
    if qs == qe:  # 起 == 止：不启用
        return 0
    now_min = h * 60 + m
    start_min = qs * 60
    end_min = qe * 60
    if qs < qe:
        # 普通时段：[start:00, end:00)
        if not (start_min <= now_min < end_min):
            return 0
        return (end_min - now_min) * 60
    # 跨午夜：[start:00, 24:00) ∪ [0:00, end:00)
    if now_min >= start_min:
        return (24 * 60 - now_min + end_min) * 60
    if now_min < end_min:
        return (end_min - now_min) * 60
    return 0


# ─── 安静时段：一个判定，两处来源，谁都替代不了谁 ──────────────────────────
#
# v2.0.6 之前的写法（engine._quiet_now）把两件事塞进同一个 if 里：
#
#     window = self._night_windows.get(bid)          # Core 的生物钟夜
#     if not window:
#         return self.cfg.in_quiet_hours(hour)       # 只有拿不到 Core 时才看配置
#     start, end = window
#     ...
#
# 后果有两个，都是线上真出过的：
#
# 1. **Core 的夜「顶替」了面板上配的安静时段**。面板上写 23→7 的人，只要 Core
#    给出一个 00:15–05:30 的夜，23:00–00:15 与 05:30–07:00 就重新变成可发时段；
#    Core 给出的夜如果是 16:45–18:15（一个午睡），那 23–7 就彻底不存在了；
#    而 Core 的夜若退化成 start==end，安静时段会被整体关掉。
# 2. **配置窗口是在「她所在城市」的时钟上判的**（调用方传的是 _moment_of().hour）。
#    她设定在加里宁格勒（UTC+2）而宿主在北京（UTC+8）时，本机 04:06 在她那边是
#    22:06，于是「现在是安静时段」不成立——issue #3 里凌晨 4 点那条 continue_topic
#    就是这么出去的。
#
# 所以判定拆成两个纯函数：窗口语义（in_window）与「用哪个时钟 + 取并集」
# （quiet_decision）。engine 只负责取数，不再自己写一遍区间比较。

QUIET_CLOCK_LOCAL = "local"
QUIET_CLOCK_CORE = "core"

_CORE_CLOCK_ALIASES = {
    "core", "city", "her", "character", "role", "persona",
    "她", "角色", "人物", "城市", "她那边",
}


def normalize_quiet_clock(value: Any) -> str:
    """把 quiet_clock 配置收成 local / core；看不懂的一律当 local。

    默认 local 是**修 issue #3 的关键**：面板上那个 23→7 是操作者按自己的作息
    写的，就该按插件所在机器的墙上时间判，而不是按角色的虚构城市。
    """
    try:
        text = str(value or "").strip().lower()
    except Exception:
        return QUIET_CLOCK_LOCAL
    return QUIET_CLOCK_CORE if text in _CORE_CLOCK_ALIASES else QUIET_CLOCK_LOCAL


def in_window(hour: Any, start: Any, end: Any) -> bool:
    """hour 是否落在 [start, end) 这段小时区间里（支持跨午夜、端点可写 24）。

    与 SocialConfig.in_quiet_hours 同一套语义：start == end 表示这段区间不启用
    （面板上把两头调成一样就是关掉它）。坏的入参一律 False。
    """
    h = _wall_clock_token(hour, 24)
    qs = _window_token(start)
    qe = _window_token(end)
    if h is None or qs is None or qe is None:
        return False
    if qs == qe:
        return False
    if qs < qe:
        return qs <= h < qe
    return h >= qs or h < qe


def window_span_hours(start: Any, end: Any) -> float:
    """这段窗口一共几个小时（跨午夜按加法算；起止相同 = 0 = 未启用）。"""
    qs = _window_token(start)
    qe = _window_token(end)
    if qs is None or qe is None or qs == qe:
        return 0.0
    # 端点允许 24：0 → 24 是一整天，不能折成 (24-0) % 24 = 0
    return float(qe - qs) if qs < qe else float((qe - qs) % 24)


def _hour_float(value: Any) -> Optional[float]:
    """Core 的 night_window 是浮点小时（01:45 = 1.75），收进 [0, 24)。"""
    try:
        out = float(value) % 24.0
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _in_window_float(hour: Any, start: float, end: float) -> bool:
    """浮点小时版本的 in_window（Core 的夜可能是 1.75 这种半点）。"""
    try:
        h = float(hour) % 24.0
    except (TypeError, ValueError):
        return False
    if not math.isfinite(h):
        return False
    if abs(start - end) < 1e-9:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


def quiet_decision(
    local_hour: Any,
    city_hour: Any,
    start: Any,
    end: Any,
    *,
    clock_mode: Any = QUIET_CLOCK_LOCAL,
    core_window: Optional[Any] = None,
    use_core_night: bool = True,
) -> Tuple[bool, str]:
    """现在是不是安静时段，以及为什么。

    两条来源取**并集**，谁也不顶替谁：

    * 面板配置 quiet_start / quiet_end：按 clock_mode 选时钟。
      local（默认）= 插件所在机器的墙上时间；core = 她所在城市。
    * Core 契约里的生物钟夜 night_window：按她所在城市的小时判。
      use_core_night=False 时不参与。

    返回 (是否安静, 原因)；原因串直接进闸门日志与 _round_skips，
    这样「为什么没发」在面板和日志里是可查的事实，而不是靠猜。
    """
    reasons: list = []

    qs = _window_token(start)
    qe = _window_token(end)
    if qs is not None and qe is not None and qs != qe:
        mode = normalize_quiet_clock(clock_mode)
        if mode == QUIET_CLOCK_CORE:
            h = _wall_clock_token(city_hour, 24)
            if h is not None and in_window(h, qs, qe):
                reasons.append(f"安静时段（{qs:02d}:00–{qe:02d}:00，她那边 {h:02d} 点）")
        else:
            h = _wall_clock_token(local_hour, 24)
            if h is not None and in_window(h, qs, qe):
                reasons.append(f"安静时段（{qs:02d}:00–{qe:02d}:00，本机 {h:02d} 点）")

    if use_core_night and core_window:
        try:
            raw_start, raw_end = core_window[0], core_window[1]
        except (TypeError, IndexError, KeyError):
            raw_start = raw_end = None
        cs = _window_hour_float(raw_start) if raw_start is not None else None
        ce = _window_hour_float(raw_end) if raw_end is not None else None
        if cs is not None and ce is not None and abs(cs - ce) >= 1e-9:
            h = _wall_clock_token(city_hour, 24)
            if h is not None and _in_window_float(h, cs, ce):
                reasons.append(f"她的夜里（{cs:04.2f}–{ce:04.2f}，她那边 {h:02d} 点）")

    return (bool(reasons), "；".join(reasons))


_OVERLAP_MAX_SPAN = 14 * 24 * 3600.0


def _window_edges(qs: float, qe: float) -> Tuple[Tuple[float, float], ...]:
    """安静窗口在**一天之内**的段（小时，24 = 当天结束）。

    不跨午夜是一段；跨午夜拆成 [qs, 24) 与 [0, qe) 两段。逐日求交时只按这个
    形状算，不再逐分钟走钟。
    """
    if qs < qe:
        return ((qs, qe),)
    return ((qs, 24.0), (0.0, qe))


def quiet_overlap_seconds(
    start_ts: Any,
    end_ts: Any,
    start: Any,
    end: Any,
    offset_minutes: Optional[int] = None,
) -> float:
    """[start_ts, end_ts) 这段时间里，有多少秒落在安静时段窗口内。

    存在的意义是 issue #3 的第二条建议：**跨过安静时段的接话窗口不该被安静时段
    白白烧掉**。原来 continue_window_hours=8 是一个绝对的 8 小时，人在 23:00
    睡着、07:00 醒，凌晨那 8 小时里窗口就过期了——她醒来时这段沉默已经「超时」，
    接话永远轮不上。用这个函数把落在静音里的秒数补回窗口长度，语义就变成
    「醒着的时间里给 8 小时」。

    start / end 是小时（可带小数，Core 的夜是 1.75 这种）；offset_minutes
    为 None 表示按本机墙上时间算，否则按 city_now 那个城市算。

    v2.0.7（2026-11-04 事件循环卡顿）：这里原来是**逐分钟**遍历，跨度 14 天时
    每次 20160 次 city_now + city_epoch（实测 0.40 秒），而它被放在「每轮心跳 ×
    每个用户」里——468 个用户、302 个 last_said 超过 14 天，一轮心跳纯 CPU
    33–89 秒，asyncio 事件循环被独占，AstrBot 看门狗连抓 41 次主线程栈，最深帧
    全落在这个函数里。窗口端点是整点（Core 夜窗最多带半点），完全不需要逐分钟：
    按**本地日界**切段，每天最多两段，14 天 = 28 次区间求交。结果与逐分钟版本
    逐秒一致（见 tests/test_v207_quiet_overlap.py 的等价性用例），耗时从 0.40 秒
    降到 0.08 毫秒（有 Core 的固定偏移钟）／0.33 毫秒（本机时钟）。
    固定偏移的城市钟没有夏令时，日界就是 86400 秒，所以那一支一天只折一次
    epoch、其余用算术推进；本机时钟会碰夏令时，才逐段折。
    """
    try:
        a = float(start_ts)
        b = float(end_ts)
    except (TypeError, ValueError):
        return 0.0
    if not (math.isfinite(a) and math.isfinite(b)) or b <= a:
        return 0.0
    qs = _window_hour_float(start)
    qe = _window_hour_float(end)
    if qs is None or qe is None or abs(qs - qe) < 1e-9:
        return 0.0
    # 与逐分钟版本同一个上限：最多数 14 天（手改出来的远古时间戳不该把心跳拖住）。
    if b - a > _OVERLAP_MAX_SPAN:
        b = a + _OVERLAP_MAX_SPAN
    try:
        first = city_now(a, offset_minutes)
    except (TypeError, ValueError, OSError, OverflowError):
        return 0.0
    day = first.replace(hour=0, minute=0, second=0, microsecond=0)
    edges = _window_edges(qs, qe)
    # 先把「每天那段安静窗口」折成 epoch 区间，再统一求交——两种时钟各生成一次，
    # 求交只写一遍。
    spans: list = []
    steps = int(math.ceil((b - a) / 86400.0)) + 2  # 跨度/一天 + 首尾两个半截日
    offset = _offset(offset_minutes)
    if offset is not None:
        # 有 Core 的城市钟是**固定偏移**，没有夏令时：一天就是 86400 秒，
        # 日界用算术推进，一天只折一次 epoch（原来每段都要 timegm 一次，
        # 14 天 = 80 次，0.4 毫秒；现在是 1 次 + 32 次加法）。
        day_epoch = city_epoch(day, offset)
        for _ in range(steps):
            if day_epoch >= b:
                break
            for ws_h, we_h in edges:
                spans.append((day_epoch + ws_h * 3600.0, day_epoch + we_h * 3600.0))
            day_epoch += 86400.0
    else:
        # 本机时钟会碰夏令时，日界不一定是 86400 秒，只能逐段折。
        for _ in range(steps):
            if city_epoch(day, offset_minutes) >= b:
                break
            for ws_h, we_h in edges:
                spans.append(
                    (
                        city_epoch(day + timedelta(hours=ws_h), offset_minutes),
                        city_epoch(day + timedelta(hours=we_h), offset_minutes),
                    )
                )
            day = day + timedelta(days=1)
    total = 0.0
    for lo, hi in spans:
        lo = max(a, lo)
        hi = min(b, hi)
        if hi > lo:
            total += hi - lo
    return total
