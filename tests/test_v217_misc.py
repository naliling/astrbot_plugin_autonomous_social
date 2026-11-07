"""v2.1.7 外围模块修复回归（审核报告 v2.1.6 的 P1-02 / P2-01~08·19 / P3-17~26）。

本文件只钉**行为**，不钉实现细节；每个用例对应报告里一条，并在 docstring 里
写清「旧实现错在哪、修完应该是什么样」。分模块：

* P1-02 `core_bridge._as_bool`：`bool("false")` 为真会让社交层整段静默，
  所以脏值一律偏向 False（未睡），并覆盖 bool/int/float/各类字符串/容器；
* P2-19 `core_bridge._text_list`：契约列表字段是字符串时不能逐字符展开；
* P3-25 `core_bridge` 自动定位缓存：Core 搬家后必须能重新搜索；
* P2-04~07 / P3-24 `timeintent`：下下周末、今晚复合词、晚上 12 点/1 点、
  「一点」误判；`promise_due_at` 死代码已删；
* P2-01 / P3-18 `verify`：日常聊天词不再被判破墙，标题判据不再被空格/单字母触发；
* P2-02 `pacing`：框架分段回复关掉时不允许自动拆两条；
* P2-03 `desire`：停机数天后的结算不能一步撞满念头天花板；
* P2-08 `sanitize`：用户粘贴的小写 HTML/代码不被吞；
* P3-21 `textutil`：缺右括号的「[图片」不吞后文；
* P3-22 `style_profile`：小数点/URL 里的点不算句号；
* P3-23 `closing`：异常分支也要把已收集的证据带给 closing_instruction；
* P3-26 `memory_bridge`：读缓存满仓时淘汰过期/最旧，而不是砍掉前一半；
* P3-17 `reasoning`：分布硬编码是设计意图，只做「权重全正、选择结果合法」的
  轻断言（报告本身也标注无 KeyError 风险）。
"""

from __future__ import annotations

import logging
import math
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = (
    _HERE.parent
    if (_HERE.parent / "_conf_schema.json").is_file()
    else _HERE.parent / "astrbot_plugin_autonomous_social"
)


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v217_misc_test")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social import (  # noqa: E402
    closing,
    core_bridge,
    desire,
    memory_bridge,
    pacing,
    reasoning,
    sanitize,
    style_profile,
    textutil,
    timeintent,
    verify,
)

TZ = 8 * 3600


def ts(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=timezone(timedelta(seconds=TZ))).timestamp()


NOW = ts(2026, 11, 7, 19, 15)  # 周六 19:15（与 test_v216_timeintent 同一基准）


class AsBoolTest(unittest.TestCase):
    """P1-02：跨插件布尔字段不能走 `bool()`。"""

    def test_true_values(self):
        for value in (True, 1, 2, -1, 0.5, 2.5, "true", "TRUE", " True ", "1", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(core_bridge.CoreBridge._as_bool(value))

    def test_false_values(self):
        # 重点是字符串 "false"/"0"：旧实现 bool("false") 为 True，她会被判成
        # 正在睡觉，整段安静时段不开口。
        for value in (
            False, 0, 0.0, "false", "FALSE", " False ", "0", "no", "off", "",
            None, [], {}, (), set(), "abc", object(),
        ):
            with self.subTest(value=value):
                self.assertFalse(core_bridge.CoreBridge._as_bool(value))

    def test_non_finite_numbers_are_false(self):
        for value in (float("nan"), float("inf"), float("-inf"), "nan", "inf"):
            with self.subTest(value=value):
                self.assertFalse(core_bridge.CoreBridge._as_bool(value))

    def test_wake_block_uses_as_bool(self):
        block = core_bridge.CoreBridge._wake_block(
            {
                "asleep": "false",
                "disturbed": "0",
                "wake_at": "坏值",
                "last_sleep_hours": "7.5",
                "inertia_minutes_left": "30",
            }
        )
        self.assertFalse(block["asleep"])
        self.assertFalse(block["disturbed"])
        self.assertIsNone(block["wake_at"])
        self.assertEqual(block["last_sleep_hours"], 7.5)
        self.assertEqual(block["inertia_minutes_left"], 30)

    def test_self_view_uses_as_bool(self):
        bridge = core_bridge.CoreBridge("auto", data_dir=None)
        view = bridge._build_self_view(
            {"contract": {"v": 1, "body": {"asleep": "false"}}}, "bot1"
        )
        self.assertFalse(view["asleep"])


class TextListTest(unittest.TestCase):
    """P2-19：契约列表字段是字符串时不能逐字符展开。"""

    def test_non_list_is_empty(self):
        for value in (None, "整理笔记", {"a": 1}, 3, True):
            with self.subTest(value=value):
                self.assertEqual(core_bridge.CoreBridge._text_list(value), [])

    def test_dirty_items_are_skipped(self):
        self.assertEqual(
            core_bridge.CoreBridge._text_list(
                [" 回邮件 ", "", None, {"x": 1}, ["嵌套"], ("元组",), {1}, 3, 2.5]
            ),
            ["回邮件", "3", "2.5"],
        )

    def test_self_view_lists_are_not_char_exploded(self):
        bridge = core_bridge.CoreBridge("auto", data_dir=None)
        view = bridge._build_self_view(
            {
                "contract": {
                    "v": 1,
                    "day": {"done": ["整理笔记", None], "next": "回邮件", "dreams": "一个梦"},
                    "wants": ["去书店"],
                    "ongoing": "写报告",
                }
            },
            "bot1",
        )
        self.assertEqual(view["day"]["done"], ["整理笔记"])
        self.assertEqual(view["day"]["next"], [])
        self.assertEqual(view["day"]["dreams"], [])
        self.assertEqual(view["day"]["wants"], ["去书店"])
        self.assertEqual(view["ongoing"], [])


class SearchHitInvalidationTest(unittest.TestCase):
    """P3-25：自动定位结果必须能在文件消失后失效重搜。"""

    def test_stale_hit_is_researched(self):
        bridge = core_bridge.CoreBridge("auto", data_dir=None)
        stale = str(Path(tempfile.gettempdir()) / "humanoid_core_gone" / "state.json")
        bridge._search_hit = stale
        bridge._searched = True
        bridge._searched_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="humanoid_core_") as root:
            found = str(Path(root) / "state.json")
            with mock.patch.object(
                core_bridge.os, "walk", return_value=[(root, [], ["state.json"])]
            ):
                self.assertEqual(bridge._search_for_state(), found)
        self.assertEqual(bridge._search_hit, found)

    def test_forget_only_matching_hit(self):
        bridge = core_bridge.CoreBridge("auto", data_dir=None)
        bridge._search_hit = "a/state.json"
        bridge._searched = True
        bridge._forget_search_hit("b/state.json")
        self.assertEqual(bridge._search_hit, "a/state.json")
        bridge._forget_search_hit("a/state.json")
        self.assertIsNone(bridge._search_hit)
        self.assertFalse(bridge._searched)


class TimeIntentTest(unittest.TestCase):
    """P2-04~07 / P3-24：时间解析必须落在说那句话的人本意上。"""

    def parse(self, text, now=NOW):
        return timeintent.parse_deadline(text, now, tz_offset=TZ)

    def at(self, text, now=NOW):
        return self.parse(text, now)["at"]

    def test_next_next_weekend_is_two_weeks(self):
        # 2026-11-07 是周六；「下下周末」= 11-21（两周后的周六）20:00，
        # 旧实现按一周算成 11-14，承诺早一周兑现。
        self.assertEqual(self.at("下下周末"), ts(2026, 11, 21, 20, 0))
        self.assertEqual(self.at("下周末"), ts(2026, 11, 14, 20, 0))

    def test_explicit_today_compound_words(self):
        # 21:00 说「今晚8点」和「今天晚上8点」必须同一种行为：今天没过完，
        # 给半小时缓冲；旧实现只认前者，后者被推到明天 20:00。
        late = ts(2026, 11, 7, 21, 0)
        self.assertEqual(self.at("今晚8点", late), late + 1800)
        self.assertEqual(self.at("今天晚上8点", late), late + 1800)

    def test_evening_twelve_is_next_midnight(self):
        # 旧实现 12 点不满足「<12」，被当成当天中午 12:00。
        self.assertEqual(self.at("晚上12点"), ts(2026, 11, 8, 0, 0))

    def test_evening_one_is_next_early_morning(self):
        # 旧实现给 1 点补 12 小时，变成当天 13:00。
        self.assertEqual(self.at("晚上1点"), ts(2026, 11, 8, 1, 0))

    def test_roll_applies_after_absolute_date(self):
        # 「3月5号晚上12点」= 3月6号 0 点：跨日偏移必须在「今年已过→明年」之后加。
        self.assertEqual(self.at("3月5号晚上12点"), ts(2027, 3, 6, 0, 0))

    def test_one_dian_as_degree_word_is_not_a_clock(self):
        for text in ("一点小事", "快一点", "慢一点", "再等我一点"):
            with self.subTest(text=text):
                self.assertIsNone(self.at(text))

    def test_one_dian_with_clear_clock_context_still_parses(self):
        # 「一点半/一点钟」是明确的钟点，不能被上下文判据误杀。
        self.assertEqual(self.at("一点半"), ts(2026, 11, 8, 1, 30))
        self.assertEqual(self.at("一点钟"), ts(2026, 11, 8, 1, 0))

    def test_promise_due_at_dead_code_removed(self):
        # P3-24：生产代码只用 parse_deadline；留着 promise_due_at 只是死代码。
        self.assertFalse(hasattr(timeintent, "promise_due_at"))


class WallCheckTest(unittest.TestCase):
    """P2-01：日常聊天词不再被判破墙，真破墙仍要拦。"""

    def test_daily_chat_words_pass(self):
        for text in (
            "你头像真好看",
            "游戏延迟好高",
            "我先下线了",
            "耳机降噪不错",
            "充电宝没电了",
            "服务器又抽风了",
            "系统更新完真难用",
        ):
            with self.subTest(text=text):
                self.assertIsNone(verify._check_wall(text))

    def test_real_fourth_wall_still_blocked(self):
        for text in (
            "我把手机按在胸口，深深吸了一口气。",
            "(起身去打开界面)真是的，这么着急呀。",
            "你这个APP又崩了吧，哈哈。",
            "作为AI，我不能陪你熬夜。",
            "我没法给你我的账号哦。",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(verify._check_wall(text))

    def test_tech_words_blocked_only_with_roleplay_action(self):
        self.assertIsNone(verify._check_wall("这个服务器延迟太离谱了"))
        self.assertIsNotNone(verify._check_wall("(我踢了踢服务器)这延迟真离谱"))


class TitlePatternTest(unittest.TestCase):
    """P3-18：标题判据不能把空格/单字母当成第二人称。"""

    def test_plain_title_with_space_or_letters(self):
        self.assertIsNone(verify._TITLE_NO_PEER.search("Hello world"))
        self.assertIsNone(verify._TITLE_NO_PEER.search("data 分析"))
        self.assertIsNone(verify._TITLE_NO_PEER.search("A/B 测试"))

    def test_real_second_person_still_matches(self):
        self.assertIsNotNone(verify._TITLE_NO_PEER.search("你好呀"))
        self.assertIsNotNone(verify._TITLE_NO_PEER.search("ta 怎么样"))
        self.assertIsNotNone(verify._TITLE_NO_PEER.search("今天真累。"))


class PacingSegmentedReplyTest(unittest.TestCase):
    """P2-02：框架分段回复关掉时，allow_burst 不能自己拆两条。"""

    TEXT = "今天下午我去公园散步了。看到好多花都开了，心情特别好。"

    class _Ctx:
        def __init__(self, enabled: bool):
            self.enabled = enabled

        def get_config(self, umo=""):
            return {
                "platform_settings": {
                    "segmented_reply": {"enable": self.enabled, "split_mode": "regex"}
                }
            }

    def test_framework_off_keeps_single_message(self):
        out, waits = pacing.plan_segments(
            [self.TEXT], "aiocqhttp:private:u1", self._Ctx(False), allow_burst=True
        )
        self.assertEqual(out, [self.TEXT])
        self.assertEqual(waits, [])

    def test_framework_on_still_auto_splits(self):
        out, waits = pacing.plan_segments(
            [self.TEXT], "aiocqhttp:private:u1", self._Ctx(True), allow_burst=True
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(len(waits), 1)

    def test_allow_burst_off_keeps_single_message(self):
        out, waits = pacing.plan_segments(
            [self.TEXT], "aiocqhttp:private:u1", self._Ctx(True), allow_burst=False
        )
        self.assertEqual(out, [self.TEXT])
        self.assertEqual(waits, [])


class DesireSettleTest(unittest.TestCase):
    """P2-03：停机太久不能把「插件没运行的时间」补算成想念。"""

    def test_long_outage_only_reseeds(self):
        user = {"urge": 1.0, "urge_at": NOW - 5 * 86400, "last_seen": NOW - 5 * 86400}
        urge = desire.settle(
            user, NOW, refill_hours=4.0, recent_talk_seconds=1800.0
        )
        self.assertEqual(urge, 1.0)
        self.assertEqual(user["urge_at"], NOW)

    def test_normal_gap_still_accumulates(self):
        user = {
            "urge": 0.0,
            "urge_at": NOW - 4 * 3600,
            "last_seen": NOW - 4 * 3600,
            "interest": 1.0,
        }
        urge = desire.settle(
            user, NOW, refill_hours=4.0, recent_talk_seconds=1800.0
        )
        self.assertGreater(urge, 0.0)
        self.assertLess(urge, desire.URGE_CEILING)
        self.assertEqual(user["urge_at"], NOW)


class SanitizeTest(unittest.TestCase):
    """P2-08：普通小写 HTML/代码块是用户正文，不能吞。"""

    def test_lowercase_html_is_kept(self):
        self.assertEqual(sanitize.sanitize_incoming("<div>你好</div>"), "<div>你好</div>")
        self.assertEqual(
            sanitize.sanitize_incoming("<pre>print(1)</pre>"), "<pre>print(1)</pre>"
        )

    def test_framework_shaped_blocks_are_removed(self):
        self.assertEqual(
            sanitize.sanitize_incoming(
                "<MemoryCompanion-Context>秘密</MemoryCompanion-Context>"
            ),
            "",
        )
        self.assertEqual(
            sanitize.sanitize_incoming("<FooBarPluginContext>秘密</FooBarPluginContext>"),
            "",
        )
        self.assertEqual(
            sanitize.sanitize_incoming("<system_reminder>别管</system_reminder>"), ""
        )


class TextUtilTest(unittest.TestCase):
    """P3-21：占位符必须成对才算，缺右括号是用户手打正文。"""

    def test_paired_placeholder_is_removed(self):
        self.assertEqual(textutil.strip_segments("我发了[图片]你看"), "我发了你看")

    def test_unclosed_bracket_does_not_swallow_text(self):
        text = "我发了[图片 你看这朵花真好看啊"
        self.assertEqual(textutil.strip_segments(text), text)


class StyleProfileDotTest(unittest.TestCase):
    """P3-22：小数点/URL/文件名里的点不是句号。"""

    def test_decimal_is_one_sentence(self):
        masked = style_profile._mask_protected("3.5 小时。")
        self.assertEqual(
            len([p for p in style_profile._SENTENCE_SPLIT.split(masked) if p.strip()]),
            1,
        )

    def test_average_length_unchanged_by_masking(self):
        text = "3.5 小时。"
        stats = style_profile._length_stats([text] * 3)
        self.assertEqual(stats[0], len("3.5 小时"))

    def test_url_dot_does_not_count_as_period(self):
        self.assertNotIn(
            "习惯用句号收尾", style_profile._habit_line("你", ["github.com"] * 3)
        )
        self.assertIn(
            "习惯用句号收尾", style_profile._habit_line("你", ["版本 2.1.7 发布了。"] * 3)
        )


class ClosingEvidenceTest(unittest.TestCase):
    """P3-23：异常分支也要带证据，否则 closing_instruction 看不见已收尾。"""

    def test_future_reply_keeps_evidence(self):
        future = NOW + 100
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[{"dir": "out", "text": "晚安", "ts": future}],
            at=NOW,
            window_seconds=1800,
        )
        self.assertEqual(decision["reason"], "history_after_input")
        self.assertEqual(decision["last_reply_at"], future)
        self.assertTrue(decision["categories"])

    def test_outside_window_keeps_evidence(self):
        old = NOW - 7200
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[{"dir": "out", "text": "晚安", "ts": old}],
            at=NOW,
            window_seconds=1800,
        )
        self.assertEqual(decision["reason"], "outside_window")
        self.assertEqual(decision["last_reply_at"], old)
        self.assertTrue(decision["categories"])


class ReadCacheEvictionTest(unittest.TestCase):
    """P3-26：满仓时先清过期，再淘汰最旧，不能一次砍掉前一半。"""

    def setUp(self):
        memory_bridge._READ_CACHE.clear()

    def tearDown(self):
        memory_bridge._READ_CACHE.clear()

    def test_expired_entries_are_purged_first(self):
        bridge = object()
        now = memory_bridge._now()
        for i in range(memory_bridge._READ_CACHE_MAX_ENTRIES):
            key = ("u", "1", f"q{i}", 1800)
            if i < 40:
                memory_bridge._READ_CACHE[key] = (now - 1.0, bridge, f"old{i}")
            else:
                memory_bridge._READ_CACHE[key] = (now + 60.0, bridge, f"new{i}")
        memory_bridge._read_cache_put(("u", "1", "fresh", 1800), bridge, "fresh")
        # 40 个过期项应全被清掉，24 个热条目一个不丢。
        self.assertEqual(len(memory_bridge._READ_CACHE), 25)
        for i in range(40, memory_bridge._READ_CACHE_MAX_ENTRIES):
            self.assertIn(("u", "1", f"q{i}", 1800), memory_bridge._READ_CACHE)

    def test_oldest_is_evicted_when_all_fresh(self):
        bridge = object()
        now = memory_bridge._now()
        keys = [("u", "1", f"q{i}", 1800) for i in range(memory_bridge._READ_CACHE_MAX_ENTRIES)]
        for key in keys:
            memory_bridge._READ_CACHE[key] = (now + 60.0, bridge, "text")
        memory_bridge._read_cache_put(("u", "1", "fresh", 1800), bridge, "fresh")
        self.assertEqual(len(memory_bridge._READ_CACHE), memory_bridge._READ_CACHE_MAX_ENTRIES)
        self.assertNotIn(keys[0], memory_bridge._READ_CACHE)
        self.assertIn(keys[-1], memory_bridge._READ_CACHE)
        self.assertIn(("u", "1", "fresh", 1800), memory_bridge._READ_CACHE)


class ReasoningDistributionTest(unittest.TestCase):
    """P3-17：分布硬编码是设计意图，只钉「权重合法、选择结果合法」。"""

    def test_all_weights_are_positive_finite(self):
        for key, info in reasoning.MESSAGE_TYPES.items():
            with self.subTest(key=key):
                weight = info["weight"]
                self.assertGreater(weight, 0)
                self.assertTrue(math.isfinite(weight))

    def test_selection_returns_declared_type(self):
        for _ in range(20):
            key, info = reasoning.select_message_type({}, NOW)
            self.assertIn(key, reasoning.MESSAGE_TYPES)
            self.assertEqual(info, reasoning.MESSAGE_TYPES[key])


if __name__ == "__main__":
    unittest.main()
