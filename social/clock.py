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
from typing import Optional


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
