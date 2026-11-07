"""v2.1.6：时间意图解析（social/timeintent.py）。

这个模块只干一件事：把「下周三」「3月5号」翻成绝对时刻。判错的代价不对称——
认不出来只是回落到旧的「20 小时后」，认**错**了则会让承诺在莫名其妙的时间被兑现
（把「下周三」当成「明天」）。所以测试的重点是边界，不是覆盖率：

* 跨年（12 月说「3月5号」说的是明年）、已过的日期、不带年份；
* 只有日期没有钟点、只有钟点没有日期、日期 + 时段 + 钟点的组合；
* 脏输入与异常输入（None / nan / inf / bytes / 超长 / 纯表情）一律不抛。

时区固定成东八区（tz_offset=28800），now 固定成 2026-11-07 19:15（周六），
这样每个断言都是确定的绝对时刻，不依赖跑测试的机器。
"""

from __future__ import annotations

import importlib.util
import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE.parent / "astrbot_plugin_autonomous_social"
_MODULE_PATH = PLUGIN_ROOT / "social" / "timeintent.py"

# 直接按文件路径加载：timeintent 零依赖，不必为了它去桩一个 astrbot 包
_spec = importlib.util.spec_from_file_location("_v216_timeintent", _MODULE_PATH)
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

TZ = 8 * 3600
NAN = float("nan")
INF = float("inf")


def ts(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> float:
    """东八区的某个本地时刻 → epoch 秒。"""
    return datetime(y, mo, d, h, mi, tzinfo=timezone(timedelta(seconds=TZ))).timestamp()


NOW = ts(2026, 11, 7, 19, 15)        # 周六 19:15


def parse(text, now=NOW):
    return T.parse_deadline(text, now, tz_offset=TZ)


def at_of(text, now=NOW):
    return parse(text, now)["at"]


class RelativeSecondsTest(unittest.TestCase):
    """「N 小时/分钟/天后」：最明确的一类，必须分毫不差。"""

    def test_one_hour(self):
        self.assertEqual(at_of("一小时后"), NOW + 3600)

    def test_thirty_minutes(self):
        self.assertEqual(at_of("30分钟后"), NOW + 1800)

    def test_two_hours(self):
        self.assertEqual(at_of("两小时后"), NOW + 7200)

    def test_three_days(self):
        self.assertEqual(at_of("3天后"), NOW + 3 * 86400)

    def test_one_week(self):
        self.assertEqual(at_of("一周后"), NOW + 7 * 86400)

    def test_kind_is_relative_seconds(self):
        got = parse("半小时以后")
        self.assertEqual(got["kind"], "relative_seconds")
        self.assertEqual(got["at"], NOW + 1800)

    def test_confidence_high(self):
        self.assertGreaterEqual(parse("一小时后")["confidence"], 0.9)


class AbsoluteDateTest(unittest.TestCase):
    """「N月N号」：不带年份时按今年，今年这天过了就按明年。"""

    def test_passed_date_rolls_to_next_year(self):
        self.assertEqual(at_of("3月5号"), ts(2027, 3, 5, 20))

    def test_passed_date_rolls_to_next_year_again(self):
        self.assertEqual(at_of("3月1号"), ts(2027, 3, 1, 20))

    def test_future_date_this_year(self):
        self.assertEqual(at_of("12月31号"), ts(2026, 12, 31, 20))

    def test_tomorrow_date_this_year(self):
        self.assertEqual(at_of("11月8号"), ts(2026, 11, 8, 20))

    def test_chinese_numerals(self):
        self.assertEqual(at_of("三月五日"), ts(2027, 3, 5, 20))

    def test_date_with_afternoon_clock(self):
        self.assertEqual(at_of("3月5号下午3点"), ts(2027, 3, 5, 15))

    def test_invalid_date_returns_none(self):
        self.assertIsNone(at_of("2月30号"))

    def test_feb29_next_year_not_leap_returns_none(self):
        # 2026 年的 2月29号 已过 → 明年 2027 不是闰年，宁可不认也不编一个日期
        self.assertIsNone(at_of("2月29号"))

    def test_kind_is_absolute_date(self):
        self.assertEqual(parse("3月5号")["kind"], "absolute_date")


class WeekdayTest(unittest.TestCase):
    """星期：本周/这周/下周/下下周，以及不带前缀的裸「周三」。"""

    def test_next_wednesday(self):
        self.assertEqual(at_of("下周三给你看那个"), ts(2026, 11, 11, 20))

    def test_bare_wednesday_means_upcoming(self):
        self.assertEqual(at_of("周三"), ts(2026, 11, 11, 20))

    def test_this_friday_already_passed_rolls_forward(self):
        # 11-06 的周五已过，说的是下周五 11-13
        self.assertEqual(at_of("这周五"), ts(2026, 11, 13, 20))

    def test_next_next_saturday(self):
        self.assertEqual(at_of("下下周六"), ts(2026, 11, 21, 20))

    def test_sunday_tomorrow(self):
        self.assertEqual(at_of("星期天"), ts(2026, 11, 8, 20))

    def test_tuesday_next_week(self):
        self.assertEqual(at_of("礼拜二"), ts(2026, 11, 10, 20))

    def test_monday_next(self):
        self.assertEqual(at_of("周一"), ts(2026, 11, 9, 20))

    def test_weekday_with_period(self):
        self.assertEqual(at_of("下周三晚上"), ts(2026, 11, 11, 20))

    def test_weekday_with_clock(self):
        self.assertEqual(at_of("下周三下午3点"), ts(2026, 11, 11, 15))

    def test_kind_is_weekday(self):
        self.assertEqual(parse("下周三")["kind"], "weekday")


class WeekendTest(unittest.TestCase):
    def test_weekend_on_saturday_is_today(self):
        # 今天就是周六，19:15 还没到 20:00，说的就是今晚
        self.assertEqual(at_of("周末"), ts(2026, 11, 7, 20))

    def test_next_weekend(self):
        self.assertEqual(at_of("下周末"), ts(2026, 11, 14, 20))

    def test_kind_is_weekend(self):
        self.assertEqual(parse("周末")["kind"], "weekend")


class RelativeDayTest(unittest.TestCase):
    """日期词与时段词，以及两者的组合。"""

    def test_tomorrow(self):
        self.assertEqual(at_of("明天给你看"), ts(2026, 11, 8, 20))

    def test_day_after_tomorrow(self):
        self.assertEqual(at_of("后天"), ts(2026, 11, 9, 20))

    def test_three_days_later(self):
        self.assertEqual(at_of("大后天"), ts(2026, 11, 10, 20))

    def test_tonight_is_today_evening(self):
        self.assertEqual(at_of("今晚"), ts(2026, 11, 7, 20))

    def test_tomorrow_morning(self):
        self.assertEqual(at_of("明早"), ts(2026, 11, 8, 9))

    def test_tomorrow_evening(self):
        self.assertEqual(at_of("明晚"), ts(2026, 11, 8, 20))

    def test_tomorrow_early(self):
        self.assertEqual(at_of("明天一早"), ts(2026, 11, 8, 9))

    def test_noon_passed_rolls_to_tomorrow(self):
        # 19:15 说「中午」，说的是明天中午
        self.assertEqual(at_of("中午"), ts(2026, 11, 8, 12))

    def test_tomorrow_afternoon_clock(self):
        self.assertEqual(at_of("明天下午3点"), ts(2026, 11, 8, 15))

    def test_today_lands_on_default_hour(self):
        # 19:15 说「今天」：今天还没到 20:00，就落在今晚 20:00
        self.assertEqual(at_of("今天"), ts(2026, 11, 7, 20))

    def test_today_after_default_hour_gets_buffer(self):
        # 21:00 说「今天」：今天确实还没过完，给半小时缓冲而不是推到明天
        self.assertEqual(at_of("今天", ts(2026, 11, 7, 21, 0)), ts(2026, 11, 7, 21, 30))

    def test_kind_is_relative_day(self):
        self.assertEqual(parse("明天")["kind"], "relative_day")


class ClockOnlyTest(unittest.TestCase):
    def test_noon_already_passed(self):
        self.assertEqual(at_of("12点"), ts(2026, 11, 8, 12))

    def test_half_past_eleven(self):
        self.assertEqual(at_of("十一点半"), ts(2026, 11, 8, 11, 30))

    def test_eight_oclock(self):
        self.assertEqual(at_of("八点"), ts(2026, 11, 8, 8))

    def test_evening_eight_is_today(self):
        self.assertEqual(at_of("晚上八点"), ts(2026, 11, 7, 20))

    def test_afternoon_three_is_tomorrow(self):
        # 19:15 的「下午3点」已经过了 → 明天 15:00
        self.assertEqual(at_of("下午3点"), ts(2026, 11, 8, 15))

    def test_kind_is_clock(self):
        self.assertEqual(parse("八点")["kind"], "clock")


class VagueTest(unittest.TestCase):
    def test_in_a_moment(self):
        self.assertEqual(at_of("一会儿"), NOW + 1800)

    def test_later(self):
        self.assertEqual(at_of("晚点"), NOW + 7200)

    def test_in_a_while(self):
        self.assertEqual(at_of("待会儿"), NOW + 3600)

    def test_right_away(self):
        self.assertEqual(at_of("马上"), NOW + 600)

    def test_vague_confidence_low(self):
        self.assertLess(parse("一会儿")["confidence"], 0.5)


class YearBoundaryTest(unittest.TestCase):
    """跨年：12 月 31 日 23:00 说「明天」是明年 1 月 1 日。"""

    def setUp(self):
        self.nye = ts(2026, 12, 31, 23, 0)

    def test_tomorrow_crosses_year(self):
        self.assertEqual(at_of("明天", self.nye), ts(2027, 1, 1, 20))

    def test_jan_first_is_next_year(self):
        self.assertEqual(at_of("1月1号", self.nye), ts(2027, 1, 1, 20))

    def test_march_rolls_to_next_year(self):
        self.assertEqual(at_of("3月5号", self.nye), ts(2027, 3, 5, 20))

    def test_weekday_crosses_year(self):
        # 2026-12-31 是周四；「下周一」= 2027-01-04
        self.assertEqual(at_of("下周一", self.nye), ts(2027, 1, 4, 20))


class NoMatchTest(unittest.TestCase):
    """认不出来就必须返回 None，绝不猜。"""

    def test_empty(self):
        self.assertIsNone(at_of(""))

    def test_blank(self):
        self.assertIsNone(at_of("   "))

    def test_plain_chat(self):
        self.assertIsNone(at_of("哈哈哈"))

    def test_unfinished_sentence(self):
        self.assertIsNone(at_of("我还没说完"))

    def test_emoji_only(self):
        self.assertIsNone(at_of("❤️❤️"))

    def test_unknown_year_sentence(self):
        self.assertIsNone(at_of("等你有空再说"))

    def test_kind_none(self):
        self.assertEqual(parse("哈哈哈")["kind"], "none")

    def test_none_confidence(self):
        self.assertEqual(parse("哈哈哈")["confidence"], 0.0)


class RobustnessTest(unittest.TestCase):
    """异常输入一律 fail-open：返回 at=None，不抛。"""

    def test_none_text(self):
        self.assertIsNone(T.parse_deadline(None, NOW, tz_offset=TZ)["at"])

    def test_number_text(self):
        got = T.parse_deadline(123, NOW, tz_offset=TZ)
        self.assertIn("at", got)

    def test_bytes_text(self):
        got = T.parse_deadline(b"\xe4\xbd\xa0\xe5\xa5\xbd", NOW, tz_offset=TZ)
        self.assertIsNone(got["at"])

    def test_nan_now(self):
        self.assertIsNone(T.parse_deadline("明天", NAN, tz_offset=TZ)["at"])

    def test_inf_now(self):
        self.assertIsNone(T.parse_deadline("明天", INF, tz_offset=TZ)["at"])

    def test_string_now(self):
        self.assertIsNone(T.parse_deadline("明天", "not-a-time", tz_offset=TZ)["at"])

    def test_none_now(self):
        self.assertIsNone(T.parse_deadline("明天", None, tz_offset=TZ)["at"])

    def test_long_text_does_not_raise(self):
        got = T.parse_deadline("下周三" + "啊" * 5000, NOW, tz_offset=TZ)
        self.assertEqual(got["kind"], "weekday")

    def test_no_tz_offset_does_not_raise(self):
        got = T.parse_deadline("明天", NOW)
        self.assertIsInstance(got["at"], float)

    def test_absurd_future_rejected(self):
        # 400 天以外的时刻当没认出来，防脏输入把诺言推到 2100 年
        self.assertIsNone(at_of("500天后"))

    def test_absurd_future_rejected_cn(self):
        self.assertIsNone(at_of("3年后"))

    def test_result_shape(self):
        got = parse("明天")
        self.assertEqual(set(got.keys()), {"at", "kind", "text", "confidence"})
        self.assertIsInstance(got["text"], str)

    def test_confidence_in_range(self):
        for text in ("明天", "下周三", "3月5号", "一会儿", "哈哈哈"):
            conf = parse(text)["confidence"]
            self.assertGreaterEqual(conf, 0.0)
            self.assertLessEqual(conf, 1.0)


class PromiseDueAtTest(unittest.TestCase):
    """promise_due_at 是 engine 唯一的调用口，返回 None 表示回落旧逻辑。"""

    def test_returns_float(self):
        got = T.promise_due_at("下周三给你看那个", NOW, tz_offset=TZ)
        self.assertIsInstance(got, float)
        self.assertEqual(got, ts(2026, 11, 11, 20))

    def test_returns_none_when_unparsed(self):
        self.assertIsNone(T.promise_due_at("给你看那个", NOW, tz_offset=TZ))

    def test_none_input(self):
        self.assertIsNone(T.promise_due_at(None, NOW, tz_offset=TZ))

    def test_nan_now(self):
        self.assertIsNone(T.promise_due_at("明天", NAN, tz_offset=TZ))

    def test_never_raises_on_garbage(self):
        for bad in (object(), [], {}, b"\xff", 3.14):
            self.assertIsNone(T.promise_due_at(bad, NOW, tz_offset=TZ))


class EngineWiringTest(unittest.TestCase):
    """engine 必须真的用了这一层，否则模块白写（防死代码）。"""

    def test_engine_calls_parser(self):
        engine = PLUGIN_ROOT / "social" / "engine.py"
        if not engine.is_file():
            self.skipTest("engine.py 不存在")
        text = engine.read_text(encoding="utf-8", errors="replace")
        self.assertIn("from .timeintent import parse_deadline", text)
        self.assertIn("_note_promise_deadline", text)

    def test_engine_stores_due_at(self):
        engine = PLUGIN_ROOT / "social" / "engine.py"
        if not engine.is_file():
            self.skipTest("engine.py 不存在")
        text = engine.read_text(encoding="utf-8", errors="replace")
        self.assertIn('"due_at"', text)



class EngineWritesDueAt(unittest.TestCase):
    """写侧接线：承诺落库之后，due 要按解析出的时刻改写。

    前面那些用例按文件路径加载 timeintent，是为了不为了一个零依赖模块去桩 astrbot；
    但「接线有没有接上」只有真对象能测，所以这里把 engine 导进来，用 object.__new__
    造空壳（__init__ 要 AstrBot 上下文），只补本方法用到的 log 与 _plugin_tz_offset。
    """

    @classmethod
    def setUpClass(cls):
        import logging
        import sys
        import types

        for name, attrs in (
            ("astrbot", {}),
            ("astrbot.api", {"logger": logging.getLogger("v216")}),
            ("astrbot.api.event", {"MessageChain": object}),
        ):
            if name in sys.modules:
                continue
            mod = types.ModuleType(name)
            for k, v in attrs.items():
                setattr(mod, k, v)
            sys.modules[name] = mod
        if str(PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(PLUGIN_ROOT))
        try:
            from social.engine import SocialEngine
        except Exception as e:      # 环境缺依赖就跳过，不误报红
            raise unittest.SkipTest(f"engine 导入失败：{e}")
        cls.Engine = SocialEngine

    def _engine(self):
        eng = object.__new__(self.Engine)
        eng.log = lambda *a, **k: None
        eng._plugin_tz_offset = lambda: 480      # 东八区，与 parse() 的 TZ 对齐
        return eng

    @staticmethod
    def _user(about, at):
        return {"promises": [{"about": about, "at": at, "due": at + 20 * 3600.0}]}

    def test_weekday_promise_moves_due(self):
        eng = self._engine()
        u = self._user("下周三给你看那个", NOW)
        eng._note_promise_deadline(u, "下周三给你看那个", NOW)
        item = u["promises"][0]
        self.assertEqual(item["due"], ts(2026, 11, 11, 20))
        self.assertEqual(item["due_at"], item["due"])
        self.assertEqual(item["due_kind"], "weekday")
        self.assertEqual(item["due_confidence"], 0.85)
        self.assertEqual(item["due_text"], "下周三")

    def test_promise_without_time_keeps_old_due(self):
        # 绝大多数承诺本来就没有时间词：必须原样不动，与 v2.1.5 完全一致
        eng = self._engine()
        u = self._user("把照片发你", NOW)
        before = dict(u["promises"][0])
        eng._note_promise_deadline(u, "把照片发你", NOW)
        item = u["promises"][0]
        self.assertEqual(item["due"], before["due"])
        self.assertNotIn("due_at", item)
        self.assertNotIn("due_kind", item)

    def test_only_the_matching_entry_is_rewritten(self):
        eng = self._engine()
        u = self._user("下周三给你看那个", NOW)
        u["promises"].append({"about": "别的事", "at": NOW, "due": NOW + 72000.0})
        eng._note_promise_deadline(u, "下周三给你看那个", NOW)
        self.assertEqual(u["promises"][1]["due"], NOW + 72000.0)
        self.assertNotIn("due_at", u["promises"][1])

    def test_no_such_entry_is_noop(self):
        eng = self._engine()
        u = {"promises": []}
        eng._note_promise_deadline(u, "下周三给你看那个", NOW)   # 不抛就算过
        self.assertEqual(u["promises"], [])

    def test_bad_text_fail_open(self):
        # 脏输入只许「什么都不做」，不许抛——写侧抛了整条回复就没了
        eng = self._engine()
        for bad in (None, 0, "", "……", chr(0x1F600), "a" * 300):
            u = self._user(str(bad), NOW)
            eng._note_promise_deadline(u, bad, NOW)
            self.assertGreater(u["promises"][0]["due"], 0)

    def test_tz_offset_seconds(self):
        eng = self._engine()
        self.assertEqual(eng._tz_offset_seconds(), 28800)
        eng._plugin_tz_offset = lambda: None        # auto：交给本机时区
        self.assertIsNone(eng._tz_offset_seconds())

    def test_tz_offset_seconds_survives_exception(self):
        eng = self._engine()

        def boom():
            raise RuntimeError("panel gone")

        eng._plugin_tz_offset = boom
        self.assertIsNone(eng._tz_offset_seconds())

    def test_end_to_end_due_reaches_live_promises(self):
        # 写对 due 的唯一意义：到点判断（threads.live_promises）认它
        from social import threads

        eng = self._engine()
        u = self._user("下周三给你看那个", NOW)
        eng._note_promise_deadline(u, "下周三给你看那个", NOW)
        due = ts(2026, 11, 11, 20)
        self.assertEqual(threads.live_promises(u, due - 60), [])
        self.assertEqual(len(threads.live_promises(u, due + 60)), 1)

    def test_tomorrow_promise_moves_due(self):
        # 「明天」是最常见的相对日：落在明天 20:00，而不是 20 小时后
        eng = self._engine()
        u = self._user("明天把照片发你", NOW)
        eng._note_promise_deadline(u, "明天把照片发你", NOW)
        item = u["promises"][0]
        self.assertEqual(item["due"], ts(2026, 11, 8, 20))
        self.assertEqual(item["due_kind"], "relative_day")


if __name__ == "__main__":
    unittest.main()
