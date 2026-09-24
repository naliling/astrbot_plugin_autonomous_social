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
from datetime import datetime, timedelta, timezone
from typing import Optional


def city_now(epoch: float, offset_minutes: Optional[int]) -> datetime:
    """把 epoch 换算成「她所在城市」的墙上时间（naive datetime）。

    offset_minutes=None → 退回本机时钟；否则用 UTC + 偏移，不受容器时区影响。
    """
    if offset_minutes is None:
        return datetime.fromtimestamp(epoch)
    utc = datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
    return utc + timedelta(minutes=offset_minutes)


def city_epoch(dt: datetime, offset_minutes: Optional[int]) -> float:
    """city_now 的逆运算：把一个「她所在城市」的墙上时间折回 epoch。"""
    if offset_minutes is None:
        return dt.timestamp()
    return float(calendar.timegm(dt.timetuple())) - offset_minutes * 60.0
