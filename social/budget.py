"""未决投递占位预算：**没确认送达的也算占名额**。

为什么要有
----------
发送不是原子的：消息交出去之后，回执可能停在 pending / dispatched / accepted / unknown。
v2.1.5 之前只看「已送达」，于是同一分钟内连算三次，三次都觉得「今天还很空」，
三条一起发出去——用户看到的是刷屏，配额面板上却只记了一条。
参考包 companion_v2/proactive/budget.py:64-90、:193-205 的口径：
未决四态一律占预算，delivered 只有落在窗口内才算，not_sent 立刻释放。
宁可少发一条，不可多刷一条。

为什么是纯函数、不读配置、不碰 IO
--------------------------------
调用方（engine）自己从投递账本构造 entries，这里只做算术。账本脏了、参数越界
一律 fail-open——宁可放行一条，也不要因为账本形状变了把消息全卡住：群里被永久
闭嘴，比多发一条难看得多。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

# 没确认送达的四态：占名额，且**不设时间窗**——未决就是未决，昨天没回执的也还欠着。
OCCUPYING_STATES = frozenset({"pending", "dispatched", "accepted", "unknown"})
# 释放态：delivered 超出窗口才释放（窗口内算 used），not_sent 立刻释放。
RELEASING_STATES = frozenset({"delivered", "not_sent"})

DEFAULT_WINDOW_SECONDS = 3600.0
DEFAULT_LIMIT = 6


def _finite_number(value: Any) -> float | None:
    """只接受有限数字；bool 不算（True 当 1 用会让脏账本悄悄占一个名额）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _entry_value(entry: Any, key: str) -> Any:
    """entries 里的一条可能是 dict，也可能是带属性的对象（测试桩/账本 dataclass）。"""
    if isinstance(entry, dict):
        return entry.get(key)
    return getattr(entry, key, None)


def _state_of(entry: Any) -> str:
    """把 state 归一成小写字符串；拿不到就是空串（空串按占用算，见 occupancy）。"""
    value = _entry_value(entry, "state")
    try:
        return str(value or "").strip().casefold()
    except Exception:
        return ""


def _as_limit(limit: Any) -> int:
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return cap if cap >= 0 else DEFAULT_LIMIT


def _as_window(window_seconds: Any) -> float:
    number = _finite_number(window_seconds)
    if number is None or number < 0:
        return DEFAULT_WINDOW_SECONDS
    return number


def _empty_state(limit: int) -> Dict[str, Any]:
    """fail-open 用的空账本：什么都不占，调用方照常发。"""
    return {
        "used": 0,
        "reserved": 0,
        "occupied": 0,
        "limit": limit,
        "remaining": limit,
        "full": False,
    }


def occupancy(
    entries: Sequence[Any] | None,
    now: float,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    limit: int = DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """算当前占了多少名额。

    - used：窗口内**已送达**的条数（窗口外的是上一小时的账，释放掉）；
    - reserved：还没确认送达的条数（四态 + 认不出的 state，从严都算占用）；
    - occupied = used + reserved；remaining 不小于 0；full = occupied >= limit。

    脏数据（不是 dict/对象、at 不是有限数字）直接跳过：账本里一条坏记录
    不该让整批发送停摆。
    """
    cap = _as_limit(limit)
    window = _as_window(window_seconds)
    stamp = _finite_number(now)
    if stamp is None:
        # 没有可信的 now 就不敢判「窗口内」，按空账本放行（fail-open）
        return _empty_state(cap)

    used = 0
    reserved = 0
    try:
        for entry in entries or ():
            moment = _finite_number(_entry_value(entry, "at"))
            if moment is None:
                continue
            state = _state_of(entry)
            if state == "delivered":
                if stamp - moment <= window:
                    used += 1
                continue
            if state in RELEASING_STATES:
                continue
            # 四态 + 空串/未知态：一律占用（宁可少发一条，不可多刷一条）
            reserved += 1
    except Exception:
        return _empty_state(cap)

    occupied = used + reserved
    remaining = cap - occupied
    if remaining < 0:
        remaining = 0
    return {
        "used": used,
        "reserved": reserved,
        "occupied": occupied,
        "limit": cap,
        "remaining": remaining,
        "full": occupied >= cap,
    }


def budget_allows(occupancy_state: Dict[str, Any], *, reserve_for_reply: bool = True) -> bool:
    """名额还够不够发一条。

    语义（v2.1.5 §4）：只有「已满 + 要预留 + 剩余为 0」才拦，其余一律放行。
    reserve_for_reply=False 时永远放行：被动回复是用户先开口，那条必须发，
    预算只该管主动消息——调用方用它区分两类发送。
    坏 state 一律 True（fail-open）：宁可多发一条，不可因为账本形状变了就不回话。
    """
    try:
        if not isinstance(occupancy_state, dict):
            return True
        full = bool(occupancy_state.get("full"))
        remaining = occupancy_state.get("remaining")
        if full and bool(reserve_for_reply) and remaining == 0:
            return False
        return True
    except Exception:
        return True
