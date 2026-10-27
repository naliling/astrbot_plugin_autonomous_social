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
from typing import Any, Optional


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
    qs = _wall_clock_token(start, 24)
    qe = _wall_clock_token(end, 24)
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
