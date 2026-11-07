"""平台级发送熔断：账号被平台拒绝时先停手，而不是换个姿势再撞一次。

线上发生过的事：QQ 侧 retcode 1200 / eventchecker 拒收（账号被风控），旧代码把它当
「这条没发出去」处理——记一次失败，下一轮心跳照旧再发。于是每 8~30 分钟撞一次，
撞够几十次之后账号被静默得更久。问题不在这一条消息，而在这个账号此刻不能发。

所以先把「发不出去」拆成四类，只有**账号级拒绝**才熔断：

* `platform_refused`：平台明确拒绝（1200 / eventchecker / 风控 / 频率限制）。这不是
  这条消息的问题，是这个账号此刻不能发；继续试只会加重处罚。
* `target_gone`：不是好友 / 会话失效 / 群解散。换个人照样能发，账号没病，不该熔断。
* `unknown`：超时、连接抖动、空文本。**不知道到底发没发出去**——既不能当成功，
  也不能当失败记账，更不能据此熔断（可能已经发到了，重复发送比少发更难看）。
* `other`：其余错误，当作「这条没发出去」，记账但不熔断。

本模块全是纯函数：不读配置、不碰 IO、不做 async。调用方（engine）负责把事件存进
state、把 `circuit_state` 的结果落成闸门、把成功事件传进来清零。任何拿不准的输入
一律 fail-open——宁可少拦一次，也不要在她其实没病的时候彻底不说话。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple

__all__ = [
    "PLATFORM_REFUSAL_MARKERS",
    "TARGET_GONE_MARKERS",
    "UNKNOWN_SEND_MARKERS",
    "classify_send_error",
    "circuit_state",
    "circuit_allows",
]

# ─── 回执四态的匹配口径 ──────────────────────────────

# 账号级拒绝。注意 "1200" 是裸数字，错误文案里出现一个 1200 太容易了（价格、楼层、
# 字数），所以数字标记走 `_hit` 时要求**独立成词**（前后不接别的数字），
# 而 "retcode 1200" / "code=1200" 这种带上下文的写法照旧整串命中。
PLATFORM_REFUSAL_MARKERS: Tuple[str, ...] = (
    "1200",
    "qq_eventchecker",
    "eventchecker",
    "retcode 1200",
    "风控",
    "频率限制",
)

# 对方侧没了：这条发给谁都发不出去，但账号本身是好的。换成别的人照样能发，
# 所以**绝不能**据此熔断——否则一个人的号没了，她就对所有人闭嘴。
TARGET_GONE_MARKERS: Tuple[str, ...] = (
    "不是好友",
    "非好友",
    "好友关系不存在",
    "会话失效",
    "会话不存在",
    "会话已失效",
    "session invalid",
    "session expired",
    "群不存在",
    "群解散",
    "群已解散",
    "已解散",
    "group not found",
    "not in group",
    "user not found",
    "target not found",
)

# 「不知道发没发出去」。超时与连接类错误都可能发生在**已经交出去之后**，
# 所以归 unknown 而不是 not_sent：不记账、不重试、不熔断。
UNKNOWN_SEND_MARKERS: Tuple[str, ...] = (
    "timeout",
    "timed out",
    "time out",
    "超时",
    "connection",
    "connect",
    "reset",
    "econnreset",
    "broken pipe",
    "network",
    "网络",
    "断开",
    "temporarily unavailable",
    "暂时不可用",
)

# 纯数字标记的独立成词判断：前后都不接数字，也不接小数点后跟数字（1200.5 是耗时不是码）。
# 但 "retcode 1200." 这种句末的点要放过，所以小数用 `(?!\.\d)` 而不是干脆禁掉点。
_DIGITS_RE = re.compile(r"^\d+$")
_DIGIT_TOKEN_TMPL = r"(?<![\d.]){}(?!\d)(?!\.\d)"


def _hit(text: str, marker: str) -> bool:
    """标记是否命中。

    纯数字标记（"1200"）要求独立成词：错误文案里 "1200" 常常是价格/楼层/字数，
    而 retcode 一般都单独出现。带字母/汉字的标记直接做子串匹配（大小写不敏感），
    因为平台文案里的前缀千奇百怪（"retcode: 1200"、"RetCode=1200"）。
    """
    needle = marker.strip().lower()
    if not needle:
        return False
    if _DIGITS_RE.match(needle):
        return re.search(_DIGIT_TOKEN_TMPL.format(re.escape(needle)), text) is not None
    return needle in text


def _text_of(value: Any) -> str:
    """把任意回执对象归一成小写文本；认不出的当空串（= 不知道发没发）。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - decode 的 replace 不会抛，兜底而已
            return ""
    try:
        return str(value).strip().lower()
    except Exception:  # pragma: no cover - __str__ 抛异常属于脏数据
        return ""


def classify_send_error(text: Any) -> Dict[str, Any]:
    """把发送失败的报错文案判成四态之一。

    为什么要分类：四种「没发出去」需要的反应完全不同。平台拒绝要停手两小时，
    对方没了要换个目标，超时要什么都不做（可能已经发到了），其余只记一条账。
    旧代码一律当「失败」处理，于是最严重的那类（风控）被当成最普通的那类。

    Args:
        text: 异常文案、retcode 字符串或异常对象；None/空/认不出都按 unknown。

    Returns:
        `{"state": "not_sent"|"unknown", "code": "platform_refused"|"target_gone"|"unknown"|"other",
        "platform_refused": bool}`

        * state="unknown" 只表示「不知道发没发出去」，不表示「失败」；
        * 空文本按 unknown 而不是 other：没拿到任何信息时，最安全的假设是她可能
          已经发出去了（重复发送比漏发更伤）。
    """
    raw = _text_of(text)
    if not raw:
        return {"state": "unknown", "code": "unknown", "platform_refused": False}

    if any(_hit(raw, marker) for marker in PLATFORM_REFUSAL_MARKERS):
        return {"state": "not_sent", "code": "platform_refused", "platform_refused": True}

    if any(_hit(raw, marker) for marker in TARGET_GONE_MARKERS):
        return {"state": "not_sent", "code": "target_gone", "platform_refused": False}

    if any(_hit(raw, marker) for marker in UNKNOWN_SEND_MARKERS):
        return {"state": "unknown", "code": "unknown", "platform_refused": False}

    return {"state": "not_sent", "code": "other", "platform_refused": False}


# ─── 账号级熔断 ─────────────────────────────────────

# 参考实现（proactive/platform_circuit.py:10-13）：10 分钟窗口内 2 次账号级拒绝
# → 冷却 2 小时。窗口短是因为风控是连续的：真要停手，两次就够判断了。
CIRCUIT_WINDOW_SECONDS = 600.0
CIRCUIT_THRESHOLD = 2
CIRCUIT_COOLDOWN_SECONDS = 7200.0

# 未来时间戳的容忍度（秒）。时钟回拨/时区错乱时会出现「比 now 还晚」的事件，
# 那类记录不能算进窗口，否则一条脏数据就能把熔断撑到很久以后。
_FUTURE_TOLERANCE_SECONDS = 300.0


def _is_number(value: Any) -> bool:
    """真正的数字（bool 不算：True/False 是合法的 Python 数字，但在这里是脏数据）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_number(value: Any) -> bool:
    """有限数字（NaN / ±Inf 都不算）：它们能通过算术比较而永远不成立，是脏数据。"""
    if not _is_number(value):
        return False
    number = float(value)
    return number == number and number not in (float("inf"), float("-inf"))


def _positive_number(value: Any, name: str) -> float:
    if not _is_number(value):
        raise ValueError(f"{name} 必须是数字，收到 {value!r}")
    number = float(value)
    if not (number > 0.0) or number != number or number == float("inf"):
        raise ValueError(f"{name} 必须是有限正数，收到 {value!r}")
    return number


def _non_negative_number(value: Any, name: str) -> float:
    if not _finite_number(value):
        raise ValueError(f"{name} 必须是非负有限数，收到 {value!r}")
    number = float(value)
    if number < 0.0:
        raise ValueError(f"{name} 必须是非负有限数，收到 {value!r}")
    return number


def _event_pair(item: Any) -> "Tuple[float, bool] | None":
    """把一条事件读成 (ts, ok)；脏数据返回 None（跳过，不抛）。

    调用方把事件存在 state.json 里，读回来的东西可能是字符串、列表、半条记录，
    甚至被手改坏。这里逐条容错：坏的那条当没发生（fail-open），而不是让整段
    熔断逻辑崩掉——熔断崩了的后果是「所有主动消息发不出去」。
    """
    if isinstance(item, (str, bytes, dict)):
        return None
    try:
        pair = tuple(item)
    except TypeError:
        return None
    if len(pair) != 2:
        return None
    ts, ok = pair
    if not _is_number(ts):
        return None
    if not isinstance(ok, bool):
        # 0/1 也接受（JSON 里可能被写成数字），其余认不出当脏数据。
        if _is_number(ok) and ok in (0, 1):
            ok = bool(ok)
        else:
            return None
    return float(ts), bool(ok)


def circuit_state(
    events: Iterable[Sequence[Any]],
    now: float,
    *,
    window_seconds: float = CIRCUIT_WINDOW_SECONDS,
    threshold: int = CIRCUIT_THRESHOLD,
    cooldown_seconds: float = CIRCUIT_COOLDOWN_SECONDS,
) -> Dict[str, Any]:
    """按事件流算账号级熔断状态。

    事件口径（由调用方保证，本函数不再二次判断）：**只有 platform_refused 的失败
    才传进来**，成功事件传 `(ts, True)`。target_gone / unknown / other 不进来——
    那些不是账号的病，把它们混进来会让一个人的号没了就让她对所有人闭嘴。

    清零规则不是「窗口里有成功就清」，而是「只看最后一次成功之后的失败」：成功
    事件本身会清空失败计数（成功即清），但三天前那次成功不该赦免今天的两次拒绝。

    Args:
        events: `[(ts, ok_bool), ...]`，可为脏数据（逐条跳过）。
        now: 当前时间戳（秒）。
        window_seconds: 数失败的窗口，必须 > 0。
        threshold: 窗口内几次拒绝算熔断，必须 >= 1。
        cooldown_seconds: 熔断时长，必须 >= 0。

    Returns:
        `{"open", "until", "recent", "window_seconds", "threshold", "cooldown_seconds"}`。
        `until` 是「最后一次失败 + 冷却」，没有失败时是 0.0。

    Raises:
        ValueError: 参数非法（调用方传错参数属于编程错误，静默吞掉会让熔断永不生效）。
    """
    window = _positive_number(window_seconds, "window_seconds")
    cooldown = _non_negative_number(cooldown_seconds, "cooldown_seconds")
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        raise ValueError(f"threshold 必须是整数，收到 {threshold!r}")
    if threshold < 1:
        raise ValueError(f"threshold 必须 >= 1，收到 {threshold!r}")
    if not _finite_number(now):
        raise ValueError(f"now 必须是有限数字，收到 {now!r}")
    now_ts = float(now)

    cutoff = now_ts - window
    horizon = now_ts + _FUTURE_TOLERANCE_SECONDS
    last_success: "float | None" = None
    failures: List[float] = []

    try:
        iterator = iter(events) if events is not None else iter(())
    except TypeError:
        iterator = iter(())
    for item in iterator:
        parsed = _event_pair(item)
        if parsed is None:
            continue
        ts, ok = parsed
        if ts > horizon:
            # 未来时间当脏数据：一条就能把窗口撑到很久以后。
            continue
        if ok:
            if last_success is None or ts > last_success:
                last_success = ts
        else:
            failures.append(ts)

    if last_success is not None:
        # 成功即清：只看最后一次成功之后的失败。
        failures = [ts for ts in failures if ts > last_success]

    recent_ts = [ts for ts in failures if ts >= cutoff]
    recent = len(recent_ts)
    until = 0.0
    if recent_ts:
        until = max(recent_ts) + cooldown
    open_ = recent >= threshold
    if open_ and now_ts >= until:
        # 冷却已经走完（窗口比冷却长时会出现）：自动关，不需要人工复位。
        open_ = False

    return {
        "open": bool(open_),
        "until": float(until),
        "recent": int(recent),
        "window_seconds": window,
        "threshold": int(threshold),
        "cooldown_seconds": cooldown,
    }


def circuit_allows(state: Any, now: float) -> bool:
    """熔断闸门：现在能不能发。**任何异常/坏 state 一律放行**（fail-open）。

    熔断是为了少撞几次墙，不是为了让她闭嘴。state 被手改坏、字段类型不对、时钟
    对不上时，正确做法是照常发——少拦一次的代价远小于永远不说话。
    """
    try:
        if not isinstance(state, dict):
            return True
        if not state.get("open"):
            return True
        until = state.get("until")
        if not _finite_number(until):
            return True
        if not _finite_number(now):
            return True
        return float(now) >= float(until)
    except Exception:  # pragma: no cover - 兜底，理论上到不了
        return True
