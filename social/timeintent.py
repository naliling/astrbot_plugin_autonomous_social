"""v2.1.6：从一句话里认出「她说的是什么时候」，换算成绝对时刻。

为什么需要这一层：她答应「下周三给你看那个」，旧逻辑只会记下「20 小时后该兑现」。
于是承诺在第二天中午就被兑现，而对方说的是下周三——这不是记性不好，是插件根本没
在听「什么时候」。这个模块只做一件事：把**人类的时间说法**翻成一个 epoch 秒；
翻不出来就返回 None，调用方原样回落旧逻辑（fail-open，绝不因为一句话没看懂就不发）。

设计取舍：
- **纯函数**：不读配置、不碰 state、不做 IO。now 由调用方传进来（epoch 秒），
  tz_offset 是本地时区相对 UTC 的秒数（东八区 = 28800）；不传则用运行机器本地时区。
  测试靠这两个参数拿到确定性结果，线上不用关心。
- **只认明确说法**：宁可返回 None 让旧逻辑兜着，也不去猜「有空的时候」这种话——
  猜错的代价是承诺在莫名其妙的时间被兑现。
- **日期落在 20:00**：只说「明天」不说几点时，取当地 20:00。人在晚上兑现承诺的
  概率最高，而且 20:00 不会撞上午休/上班；已经过了 20:00 就顺延到 now+30 分钟，
  免得「今天」在 23:50 说出来后立刻算到期。
- **不带年份的日期**（「3月5号」）按今年算，今年这天已经过了就按明年——说话的人
  不会在 12 月说「3月5号」指的是九个月前。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

# ─── 各种时间说法 ────────────────────────────────────────────────────
# 「N 小时/分钟/天后」：相对说法最明确，优先级最高。
_REL = re.compile(
    r"(?P<num>半|\d+|[零〇一二两三四五六七八九十]+)\s*(?:个)?\s*"
    r"(?P<unit>小时|钟头|分钟|天|周|星期|礼拜)\s*(?:之后|以后|后)"
)
# 「3月5号」「三月五日」
_ABS_DATE = re.compile(
    r"(?P<m>\d{1,2}|[一二三四五六七八九十]+)\s*月\s*"
    r"(?P<d>\d{1,2}|[一二三四五六七八九十]+)\s*[号日]"
)
# 「下周三」「这周五」「星期天」「礼拜二」「下下周六」
_WEEKDAY = re.compile(
    r"(?P<pre>下下个?|下个?|这|本)?\s*"
    r"(?P<unit>周|星期|礼拜)\s*(?P<day>[一二三四五六日天])"
)
# 「周末」
_WEEKEND = re.compile(r"(?P<pre>下下个?|下个?|这|本)?\s*(?:周末|礼拜末|星期末)")
# 日期词：按长度降序匹配，否则「大后天」会被「后天」抢先
_DAY_WORDS: Tuple[Tuple[str, int], ...] = (
    ("大后天", 3), ("大前天", -3), ("后天", 2), ("前天", -2),
    ("明天", 1), ("明日", 1), ("明儿", 1),
    ("今天", 0), ("今日", 0), ("今儿", 0),
    ("昨天", -1), ("昨日", -1),
)
# 时段词 → 当天几点。只影响「日期 + 时段」组合里的钟点。
_PERIODS: Tuple[Tuple[str, int], ...] = (
    ("凌晨", 5), ("一早", 9), ("早上", 9), ("早晨", 9), ("上午", 10),
    ("中午", 12), ("下午", 15), ("傍晚", 18), ("晚上", 20), ("夜里", 22),
)
# 复合词：一个词同时带日期偏移和钟点。必须比 _DAY_WORDS / _PERIODS 先匹配，
# 否则「明早」会被拆成「明」+「早」、「今晚」会被当成「今晚」这个词。
_DAY_PERIOD_WORDS: Dict[str, Tuple[int, int]] = {
    "明天一早": (1, 9), "明天早上": (1, 9), "明天上午": (1, 10),
    "明天晚上": (1, 20), "今天早上": (0, 9), "今天晚上": (0, 20),
    "明早": (1, 9), "明晚": (1, 20),
    "今早": (0, 9), "今晚": (0, 20),
}
# 这些词明确指「今天」：算出来的时刻已经过了，就顺延半小时而不是推到明天。
# 「今天晚上/今天早上」这类复合词也必须进来：它们由 `_DAY_PERIOD_WORDS` 整体命中，
# 拿到的 day_hit 是整词而不是「今天」。旧实现只列了「今天/今早/今晚」，于是
# 21:00 说「今晚8点」给半小时缓冲，说「今天晚上8点」却推到明天 20:00（P2-05）。
_EXPLICIT_TODAY = (
    "今天", "今日", "今儿", "今早", "今晚", "今天早上", "今天晚上",
)
# 「N 点」「N 点半」
_CLOCK = re.compile(
    r"(?P<h>\d{1,2}|[一二三四五六七八九十]+)\s*(?:点|时)(?P<half>半)?"
)
# 「一点」的上下文判据：中文里「一点」大多不是钟点（快一点/一点小事/第一点/
# 这一点）。旧实现见到「一点」就按凌晨 1 点算，confidence 还有 0.7，于是
# 「再等我一点」会被当成承诺在 01:00 兑现（P2-07）。真正的钟点会带「半/钟/
# 整/左右」或前面不是程度词，所以按前后各一个字否决。
_CLOCK_ONE_BEFORE = set("快慢多少大小好坏差早晚近远高低长短再稍微有第这那哪每点了太更挺")
_CLOCK_ONE_AFTER = set("儿点小时事也都吧啊嘛的不没")
# 模糊说法：不精确，但比「20 小时后」贴近说话人本意。
_VAGUE: Tuple[Tuple[str, float], ...] = (
    ("一会儿", 30 * 60.0), ("一会", 30 * 60.0), ("待会儿", 60 * 60.0),
    ("待会", 60 * 60.0), ("等会儿", 60 * 60.0), ("等会", 60 * 60.0),
    ("晚点", 2 * 3600.0), ("晚些", 2 * 3600.0), ("过会儿", 2 * 3600.0),
    ("马上", 10 * 60.0), ("这就", 10 * 60.0),
)

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# 只说日期不说钟点时，落在当地几点（见模块头「日期落在 20:00」）
_DEFAULT_HOUR = 20
# 「今天 20:00 已经过了」时顺延多少
_ROLL_PAST_SECONDS = 30 * 60.0
# 认出来的时刻离谱到这个程度就当没认出来（防脏输入把诺言推到 2100 年）
_MAX_AHEAD_SECONDS = 400 * 86400.0


def _cn_to_int(text: str) -> Optional[int]:
    """把「3」「十二」「二十三」「两」这类数字串换成 int；换不出返回 None。

    只支持 0–99：时间说法里够用了（「十二点」「二十三号」），再大的数字
    （「一百天后」）本来就该交给 _REL 的阿拉伯数字分支。
    """
    s = str(text or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = 1 if not head else _CN_DIGITS.get(head)
        ones = 0 if not tail else _CN_DIGITS.get(tail)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    total = 0
    for ch in s:
        val = _CN_DIGITS.get(ch)
        if val is None:
            return None
        total = total * 10 + val
    return total


def _clock_is_non_time(src: str, m: Any) -> bool:
    """判断 `_CLOCK` 命中的「一点/1点」是不是程度或数量词，而不是钟点。"""
    if m.group("h") not in ("一", "1"):
        return False
    # 「一点半/一点钟/一点整/一点左右/一点零五」是明确的钟点，不否决
    after = src[m.end():m.end() + 1]
    if m.group("half") or after in ("钟", "整", "左", "零"):
        return False
    start = m.start()
    before = src[start - 1] if start > 0 else ""
    # 「一点」落在句尾/标点前时，中文里几乎都是「一点点」的意思（再等我一点 /
    # 好一点 / 慢一点），不是凌晨 1 点。真正指 1 点会带时段词（「晚上一点」）、
    # 日期词（「明天一点」）或「了/钟」（「一点了」），这些上下文出现时照常认。
    if after == "" or after in "，。！？；、,.!?;~～…\n":
        has_context = (
            any(word in src for word, _hour in _PERIODS)
            or any(word in src for word, _off in _DAY_WORDS)
            or any(word in src for word in _DAY_PERIOD_WORDS)
        )
        if not has_context:
            return True
    return before in _CLOCK_ONE_BEFORE or after in _CLOCK_ONE_AFTER


def _local_dt(now: float, tz_offset: Optional[float]) -> datetime:
    """把 epoch 秒换成「她所在时区」的朴素 datetime。"""
    if tz_offset is None:
        return datetime.fromtimestamp(now)
    tz = timezone(timedelta(seconds=float(tz_offset)))
    return datetime.fromtimestamp(now, tz=tz).replace(tzinfo=None)


def _to_epoch(dt: datetime, tz_offset: Optional[float]) -> float:
    """朴素 datetime 换回 epoch 秒，与 _local_dt 互为逆运算。"""
    if tz_offset is None:
        return dt.timestamp()
    tz = timezone(timedelta(seconds=float(tz_offset)))
    return dt.replace(tzinfo=tz).timestamp()


def _result(
    at: Optional[float], kind: str, text: str, confidence: float
) -> Dict[str, Any]:
    return {"at": at, "kind": kind, "text": text, "confidence": confidence}


def parse_deadline(
    text: Any, now: float, *, tz_offset: Optional[float] = None
) -> Dict[str, Any]:
    """从一句话里解析「什么时候」，返回绝对时刻。

    Args:
        text: 任意文本（承诺原文、聊天正文都行）；非字符串按 str() 处理。
        now: 当前时刻（epoch 秒），由调用方给，保证可测。
        tz_offset: 本地时区相对 UTC 的秒数（东八区 28800）；None 用本机时区。

    Returns:
        {"at": float|None, "kind": str, "text": str, "confidence": float}
        - at: 绝对时刻（epoch 秒）；解析不出为 None
        - kind: relative_seconds / absolute_date / weekday / weekend /
                relative_day / clock / vague / none
        - text: 命中的那段原文（便于日志排查「为什么算成这样」）
        - confidence: 0~1，调用方可以只信高置信度的

    永不抛异常：任何意外都退回 {"at": None, "kind": "none", ...}。
    """
    try:
        return _parse(text, now, tz_offset)
    except Exception:  # noqa: BLE001 —— 解析失败绝不能带走这一轮发送
        return _result(None, "none", "", 0.0)


def _parse(text: Any, now: float, tz_offset: Optional[float]) -> Dict[str, Any]:
    raw = str(text or "")
    # 承诺行本身很短（PROMISE_MAX_CHARS=24）；长文本只看前 200 字，避免对整段
    # 聊天记录做正则回溯。取前缀而不是整段：时间说法一般出现在句首/句中。
    src = raw[:200]
    if not src.strip():
        return _result(None, "none", "", 0.0)
    try:
        now_f = float(now)
    except (TypeError, ValueError):
        return _result(None, "none", "", 0.0)
    if now_f != now_f or now_f in (float("inf"), float("-inf")):
        return _result(None, "none", "", 0.0)
    base = _local_dt(now_f, tz_offset)

    # ① 相对时长：「一小时后」「30 分钟后」——最明确，命中就直接算
    m = _REL.search(src)
    if m:
        # 「半小时后」是口语里最常见的一种，单独认：半 = 0.5
        num = 0.5 if m.group("num") == "半" else _cn_to_int(m.group("num"))
        unit = m.group("unit")
        if num is not None and num > 0:
            mult = {
                "小时": 3600.0, "钟头": 3600.0, "分钟": 60.0,
                "天": 86400.0, "周": 7 * 86400.0,
                "星期": 7 * 86400.0, "礼拜": 7 * 86400.0,
            }[unit]
            return _result(
                _checked(now_f + num * mult, now_f),
                "relative_seconds", m.group(0), 0.9
            )

    # ② 日期 + 钟点：先各解析一次，再组合
    day_off: Optional[int] = None
    day_hit = ""
    period_hour: Optional[int] = None
    for word, (off, hour) in _DAY_PERIOD_WORDS.items():
        if word in src:
            day_off, day_hit, period_hour = off, word, hour
            break
    if day_off is None:
        for word, off in _DAY_WORDS:
            if word in src:
                day_off, day_hit = off, word
                break
    if period_hour is None:
        for word, hour in _PERIODS:
            if word in src:
                period_hour = hour
                break

    clock_h: Optional[int] = None
    clock_m = 0
    clock_hit = ""
    # 钟点跨日：「晚上12点」是次日 0 点，「晚上1点」是次日凌晨 1 点。
    # 旧实现只做「时段 >= 12 就给小于 12 的钟点 +12」，于是 12 点留在中午、
    # 1 点被加成 13 点（P2-06）。跨日偏移单独记下来，由各日期分支加进 target_day，
    # 不能在这里直接改 base，否则「周三晚上12点」的星期判断会错位。
    clock_day_roll = 0
    for m in _CLOCK.finditer(src):
        hh = _cn_to_int(m.group("h"))
        if hh is None or not (0 <= hh <= 24):
            continue
        if _clock_is_non_time(src, m):
            # 「一点小事」「快一点」「第一点」不是时间：跳过，继续找后面真正的钟点
            continue
        clock_h = hh % 24 if hh == 24 else hh
        clock_m = 30 if m.group("half") else 0
        clock_hit = m.group(0)
        break

    if clock_h is not None and period_hour is not None:
        if period_hour >= 18 or period_hour <= 5:
            # 傍晚/晚上/夜里/凌晨：12 点=午夜，1~5 点=凌晨，不再一律 +12
            if clock_h == 12:
                clock_h = 0
                clock_day_roll = 1
            elif 1 <= clock_h <= 5:
                # 白天/傍晚说的「晚上1点」指次日凌晨；凌晨 0~6 点说的指当天
                if base.hour >= 12:
                    clock_day_roll = 1
            elif 6 <= clock_h <= 11:
                clock_h += 12
        elif period_hour >= 12 and 0 < clock_h < 12:
            # 「下午三点」「中午十二点」：下午/中午的钟点小于 12 时补 12 小时。
            # 不补的话「下午3点」会被当成凌晨三点，承诺提前半天兑现。
            clock_h += 12

    # ③ 绝对日期（3月5号）：没有年份，今年这天过了就算明年
    m = _ABS_DATE.search(src)
    if m:
        month = _cn_to_int(m.group("m"))
        day = _cn_to_int(m.group("d"))
        if month and day and 1 <= month <= 12 and 1 <= day <= 31:
            year = base.year
            hour = clock_h if clock_h is not None else (
                period_hour if period_hour is not None else _DEFAULT_HOUR
            )
            try:
                target = datetime(year, month, day, hour, clock_m, 0)
            except ValueError:
                # 2月30号这种不存在的日期：不猜，回落旧逻辑
                return _result(None, "none", m.group(0), 0.0)
            if target < base:
                try:
                    target = target.replace(year=year + 1)
                except ValueError:      # 2月29号 → 次年不是闰年
                    return _result(None, "none", m.group(0), 0.0)
            # 「3月5号晚上12点」= 3月6号 0 点：跨日要在日期定下来之后再加，
            # 否则会被上面的「今年已过 → 明年」判断当成另一天。
            if clock_day_roll:
                target += timedelta(days=clock_day_roll)
            return _result(
                _checked(_to_epoch(target, tz_offset), now_f),
                "absolute_date", m.group(0), 0.9,
            )

    # ④ 星期：「下周三」「这周五」「星期天」
    m = _WEEKDAY.search(src)
    if m:
        wd = _WEEKDAY_INDEX.get(m.group("day"))
        if wd is not None:
            pre = m.group("pre") or ""
            week_start = base - timedelta(days=base.weekday())
            offset_weeks = 2 if pre.startswith("下下") else (1 if "下" in pre else 0)
            target_day = week_start + timedelta(days=offset_weeks * 7 + wd)
            hour = clock_h if clock_h is not None else (
                period_hour if period_hour is not None else _DEFAULT_HOUR
            )
            # 跨日偏移必须在「是否已过」判断之前加：周三 23:00 说「周三晚上1点」，
            # 指的是周四凌晨 1 点（还没到），先加一天才是对的；反过来先判过去再加 7 天
            # 会算成下周四。
            target = _at_hour(target_day + timedelta(days=clock_day_roll), hour, clock_m)
            # 不带前缀的「周三」说在周四：指的是**刚过去的**周三，但承诺不能落到
            # 过去，所以顺延到下周同一天；带「这/本」前缀同理（那是这一周的那天）。
            if target < base:
                target += timedelta(days=7)
            return _result(
                _checked(_to_epoch(target, tz_offset), now_f),
                "weekday", m.group(0), 0.85,
            )

    # ⑤ 周末：算作周六
    m = _WEEKEND.search(src)
    if m:
        pre = m.group("pre") or ""
        week_start = base - timedelta(days=base.weekday())
        # 「下下周末」是两周后，不是一周后。星期分支（第 266 行）一直写的是 2，
        # 周末分支旧实现漏了这个 2，于是「下下周末」的承诺早一周兑现（P2-04）。
        offset_weeks = 2 if pre.startswith("下下") else (1 if "下" in pre else 0)
        target = _at_hour(
            week_start + timedelta(days=offset_weeks * 7 + 5), _DEFAULT_HOUR, 0
        )
        if target < base:
            target += timedelta(days=7)
        return _result(
            _checked(_to_epoch(target, tz_offset), now_f),
            "weekend", m.group(0), 0.7,
        )

    # ⑥ 只有日期词（明天/后天…）或只有时段词（中午/晚上…）
    if day_off is None and period_hour is not None:
        # 「中午」= 今天中午。今天中午已经过了，说话的人指的就是明天中午。
        day_off, day_hit = 0, ""
    if day_off is not None:
        target_day = (base + timedelta(days=day_off)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        hour = clock_h if clock_h is not None else (
            period_hour if period_hour is not None else _DEFAULT_HOUR
        )
        target = _at_hour(target_day + timedelta(days=clock_day_roll), hour, clock_m)
        if target <= base:
            if day_hit in _EXPLICIT_TODAY:
                # 「今天」说在 21:00：今天确实还没过完，给半小时缓冲而不是推到明天
                target = base + timedelta(seconds=_ROLL_PAST_SECONDS)
            else:
                # 「中午」「明天早上 9 点但已经 10 点」这类：顺延到下一个同钟点
                target = _at_hour(target + timedelta(days=1), hour, clock_m)
        return _result(
            _checked(_to_epoch(target, tz_offset), now_f),
            "relative_day", day_hit or ("今天" if day_off == 0 else ""), 0.85,
        )

    # ⑦ 只有钟点（「三点」）：今天这个点过了就明天
    if clock_h is not None:
        target = _at_hour(base, clock_h, clock_m)
        if target <= base:
            target = _at_hour(base + timedelta(days=1), clock_h, clock_m)
        return _result(
            _checked(_to_epoch(target, tz_offset), now_f),
            "clock", clock_hit, 0.7,
        )

    # ⑧ 模糊说法：一会儿/晚点
    for word, delta in _VAGUE:
        if word in src:
            return _result(now_f + delta, "vague", word, 0.4)

    return _result(None, "none", "", 0.0)


def _at_hour(day: datetime, hour: int, minute: int = 0) -> datetime:
    """把 day 的钟点换成 hour:minute（不改变日期）。"""
    return day.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)


def _checked(at: float, now: float) -> Optional[float]:
    """离谱的未来时刻当没认出来：脏输入不能把诺言推到 2100 年。"""
    if at != at or at in (float("inf"), float("-inf")):
        return None
    if at - now > _MAX_AHEAD_SECONDS:
        return None
    return at



