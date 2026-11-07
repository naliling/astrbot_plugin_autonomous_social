# -*- coding: utf-8 -*-
"""连发轮次与收口静默：连续主动什么时候该停一停。

为什么需要
----------
参考包把「连续主动」当成一个**有边界的轮次**，而不是一条无休止的流水线
（companion_v2/proactive/rhythm.py:164-235）：

* 一轮 = 最近一次「对方真正回了话」之后，她连续发出去的几条**已确认送达**的消息。
  只要有一条没确认送达（在途、失败、未知），这一轮就**立刻结束**：对方可能根本没
  收到上一句，再叠一条只会变成刷屏，而不是「多聊两句」。
* 轮次到达上限后也不是「永远闭嘴」，而是进入一段**收口静默**（同文件 :238-252）：
  连续主动达到阈值后，最后一条之后再压一段 grace，等对方接话，或等这段时间过去。

为什么是纯函数
--------------
这两条判据站在发送路径上。发送路径上读配置、查存储、做 async 调用，任何一次抖动都会
把「发不发」拖垮；比误拦更糟的是消息没发出去还没人知道。所以这里只吃调用方给的数据：
轮次列表 [{"at": float, "delivered": bool}, ...] 和当前时刻。脏数据跳过、参数非法、
任何异常一律 fail-open（宁可少拦，不可不发）。

阈值从哪来
----------
burst_state 的窗口/阈值/冷却都走显式参数（默认 3600s / 3 条 / 1800s）；closing_hold
的签名里只有 grace，所以「连续几条算收口」做成模块常量 CLOSING_CONTACTS_THRESHOLD——
参考包把它放在偏好里（proactive_closing_contacts_threshold），我们这一层不读配置，
等 engine 侧确认需要可调时再补参数。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

# 一轮的观察窗口：超过一小时的「连续」已经不是同一轮了。
DEFAULT_BURST_WINDOW_SECONDS = 3600.0
# 连发几条算「说多了」：三条是参考包默认档位，再往上就是明显的自说自话。
DEFAULT_BURST_THRESHOLD = 3
# 冷却：达到阈值后压多久再允许下一次主动。
DEFAULT_BURST_COOLDOWN_SECONDS = 1800.0
# 收口 grace：最后一条之后再静默 15 分钟，给对方接话的余地。
DEFAULT_CLOSING_GRACE_SECONDS = 900.0
# 连续几条算「这一轮该收口了」。签名里没有阈值参数，只能先钉在模块里。
CLOSING_CONTACTS_THRESHOLD = 3
# 允许一点时钟漂移：未来 5 分钟以内的时间戳仍按现在处理，再远就是脏数据。
FUTURE_TOLERANCE_SECONDS = 300.0
# 认得的决策类型。未知类型一律放行（fail-open）。
DECISION_KINDS = ("reply", "proactive", "followup")

__all__ = [
    "DEFAULT_BURST_WINDOW_SECONDS",
    "DEFAULT_BURST_THRESHOLD",
    "DEFAULT_BURST_COOLDOWN_SECONDS",
    "DEFAULT_CLOSING_GRACE_SECONDS",
    "CLOSING_CONTACTS_THRESHOLD",
    "burst_state",
    "closing_hold",
    "round_allows",
]


def _number(value: Any) -> Optional[float]:
    """把时间戳读成有限浮点；bool 是 int 的子类，必须单独挡掉。

    不接字符串：时间戳是内部数据，出现字符串只可能是存档被改坏了，按脏数据跳过比
    猜一个值更安全。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _clean_rounds(
    rounds: Any, now: float, window_seconds: Optional[float]
) -> List[Tuple[float, bool]]:
    """过滤脏数据并按时间升序排好。

    delivered 只认真正的 True：字符串 "true"、1、None 一律按「未确认送达」处理。
    这个方向是刻意选的——读不出来就当没收到，轮次更早结束，结果是更早恢复说话，
    不是更晚，符合 fail-open。
    """
    if not isinstance(rounds, (list, tuple)):
        return []
    items: List[Tuple[float, bool]] = []
    for item in rounds:
        if not isinstance(item, dict):
            continue
        at = _number(item.get("at"))
        if at is None:
            continue
        if at > now + FUTURE_TOLERANCE_SECONDS:
            continue
        if window_seconds is not None and at < now - window_seconds:
            continue
        items.append((at, item.get("delivered") is True))
    items.sort(key=lambda pair: pair[0])
    return items


def _consecutive_delivered(items: List[Tuple[float, bool]]) -> Tuple[int, float]:
    """从最新往回数连续已确认送达的条数；遇到未确认立即停。

    返回 (条数, 最新一条的时刻)。条数为 0 时时刻给 0.0，调用方不用再判空。
    """
    count = 0
    last_at = 0.0
    for at, delivered in reversed(items):
        if not delivered:
            break
        if count == 0:
            last_at = at
        count += 1
    return count, last_at


def burst_state(
    rounds: Any,
    now: Any,
    *,
    window_seconds: float = DEFAULT_BURST_WINDOW_SECONDS,
    threshold: int = DEFAULT_BURST_THRESHOLD,
    cooldown_seconds: float = DEFAULT_BURST_COOLDOWN_SECONDS,
) -> Dict[str, Any]:
    """连发轮次状态：最近一段连续已确认送达的消息够不够多。

    返回 active/count/until/window_seconds/threshold/cooldown_seconds/remaining_seconds。

    active 只看「窗口内的连续条数是否达到阈值」，不把冷却算进去：窗口一小时、冷却半小时，
    所以冷却过去之后 active 仍可能为 True，此时 remaining_seconds 已经是 0——这正是
    round_allows 放行「冷却后首条」的依据。窗口本身负责让轮次自然衰减：一小时后这些
    消息不在窗口里，count 归零，active 自然变回 False。

    参数非法（窗口/阈值/冷却不是正数、now 读不出来）一律返回 inactive，绝不抛异常。
    """
    window = _number(window_seconds)
    cooldown = _number(cooldown_seconds)
    try:
        limit = int(threshold)
    except (TypeError, ValueError):
        limit = DEFAULT_BURST_THRESHOLD
    if limit <= 0:
        limit = DEFAULT_BURST_THRESHOLD

    result: Dict[str, Any] = {
        "active": False,
        "count": 0,
        "until": 0.0,
        "window_seconds": window if window is not None else DEFAULT_BURST_WINDOW_SECONDS,
        "threshold": limit,
        "cooldown_seconds": cooldown if cooldown is not None else DEFAULT_BURST_COOLDOWN_SECONDS,
        "remaining_seconds": 0.0,
    }

    now_value = _number(now)
    if now_value is None or window is None or window <= 0 or cooldown is None or cooldown <= 0:
        return result

    items = _clean_rounds(rounds, now_value, window)
    count, last_at = _consecutive_delivered(items)
    if count <= 0:
        return result

    until = last_at + cooldown
    result["active"] = count >= limit
    result["count"] = count
    result["until"] = until
    result["remaining_seconds"] = max(0.0, until - now_value)
    return result


def closing_hold(
    rounds: Any,
    now: Any,
    *,
    grace_seconds: float = DEFAULT_CLOSING_GRACE_SECONDS,
) -> Dict[str, Any]:
    """收口静默：连续主动够多之后，最后一条之后再压一段 grace。

    返回 hold/until/remaining_seconds。

    这里不设观察窗口：收口看的是「这一轮连续说了几条」，只要中间没有未确认送达打断，
    就算连续；grace 过期后 hold 自然变 False。grace <= 0 表示关闭这条判据。

    until 在过期后仍然给出来（hold=False、remaining_seconds=0），调用方写日志/诊断时
    能看出「这一轮本来该静默到什么时候」，不用自己再算一遍。
    """
    result: Dict[str, Any] = {"hold": False, "until": 0.0, "remaining_seconds": 0.0}

    now_value = _number(now)
    grace = _number(grace_seconds)
    if now_value is None or grace is None or grace <= 0:
        return result

    items = _clean_rounds(rounds, now_value, None)
    count, last_at = _consecutive_delivered(items)
    if count < CLOSING_CONTACTS_THRESHOLD:
        return result

    until = last_at + grace
    result["until"] = until
    if now_value < until:
        result["hold"] = True
        result["remaining_seconds"] = until - now_value
    return result


def round_allows(decision_kind: Any, state: Any) -> bool:
    """这一轮该不该放行：reply 永远放行，proactive/followup 看轮次状态。

    放行口径：
    * reply 是对方先说话，任何时候都该回——收口静默不能把回复也吞掉；
    * state 读不出来（None、字符串、列表）或 decision_kind 不认识 → 放行；
    * hold 为 True → 拦；
    * burst active 且冷却还没走完（remaining_seconds > 0）→ 拦；
    * burst active 但冷却已过（remaining_seconds <= 0）→ 放行，这就是「冷却后首条」。

    任何异常都返回 True（fail-open）：这条判据只该让「说多了」少说一句，不该让
    整条回复链因为一个坏 state 停摆。
    """
    if not isinstance(decision_kind, str):
        return True
    kind = decision_kind.strip().lower()
    if kind == "reply" or kind not in DECISION_KINDS:
        return True
    if not isinstance(state, dict):
        return True

    try:
        if bool(state.get("hold")):
            return False
        if bool(state.get("active")):
            remaining = _number(state.get("remaining_seconds"))
            if remaining is None:
                return True
            return remaining <= 0.0
    except Exception:
        return True
    return True
