"""v1.27.33：决策链路上「脏数值」的回归测试。

审计 15 个决策模块时，真问题几乎全落在同一类上：**裸 `float()` / `int()` 放行 nan 与 inf**。
后果都不是崩溃，而是静默的错判，而且方向各不相同：

* `nan <= 0` 与 `now < nan` 同时为假 → 那条由头 / 那次回访**永远到点**；
* `int(nan)` → `ValueError` 带走该用户整轮决策（`inf` 抛 `OverflowError`）；
* `nan < 阈值` 恒假 → 坏数据被判成**最高档**（好感坏掉的人被当成最亲密）；
* 权重全变 nan → `random.uniform(0, nan)` 也是 nan → 所有 `r <= cumulative` 为假
  → **永远落到同一个兜底类型**（表现是「她只会分享想法」）。

所以这里的断言尽量写成「和字段缺失时的行为一致」或「不抛、且结果有限」，
而不是钉住某个具体数值——那些数值属于调参，会变。
"""
from __future__ import annotations

import logging
import math
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_decisions_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import anchors, clock, desire, groupflow, reasoning, textutil, threads, throttle, verify  # noqa: E402

NAN = float("nan")
INF = float("inf")


# ─── threads：回访 / 收场 / 对方最后一句 ───


class ThreadsNonFiniteTest(unittest.TestCase):
    def test_loop_with_unreadable_due_is_not_due(self):
        """`due=nan` 原来让 `due <= 0` 与 `now < due` 同时为假 → 那条永远「已经到点」。"""
        for bad in (NAN, INF, "abc", None):
            with self.subTest(due=bad):
                user = {"loops": [{"about": "面试", "due": bad, "at": 1.0}]}
                self.assertEqual(threads.live_loops(user, 9e9), [])

    def test_loop_tries_nan_does_not_raise(self):
        """`int(_num(nan))` 原来抛 ValueError，带走该用户整轮派生。"""
        user = {"loops": [{"about": "面试", "due": 1.0, "at": 1.0}], "loop_tries": NAN}
        threads.loop_entries(user)
        threads.live_loops(user, 9e9)

    def test_loop_retry_and_expire_dirty_does_not_raise(self):
        user = {
            "loops": [{"about": "面试", "due": 1.0, "at": 1.0, "retry_at": NAN, "expire_at": INF}]
        }
        threads.live_loops(user, 9e9)

    def test_closer_reason_survives_a_dirty_last_sent(self):
        """`float('abc')` 原来抛 ValueError；`nan` 会走到 `int(nan)` 再抛一次。"""
        for bad in ("abc", NAN, INF, None):
            with self.subTest(last_sent=bad):
                user = {"pending_result": "waiting", "last_sent": bad, "closer_for": 0}
                self.assertEqual(threads.closer_reason(user, 9e9, after_seconds=3600.0), "")

    def test_last_in_text_skips_unreadable_timestamps(self):
        """脏时间戳原来被当成「刚说的」，于是把很久以前那句当成对方的最新回复。"""
        user = {
            "conversation": [
                {"dir": "in", "text": "好时间戳", "ts": 900.0},
                {"dir": "in", "text": "坏时间戳", "ts": NAN},
            ]
        }
        self.assertEqual(threads.last_in_text(user, 1000.0, 3600.0), "好时间戳")

    def test_hours_falls_back_when_unreadable(self):
        for bad in (NAN, INF, "abc", None, -INF):
            with self.subTest(value=bad):
                self.assertEqual(threads._hours(bad, 2.5), 2.5)
        self.assertEqual(threads._hours(3.0, 2.5), 3.0)

    def test_loop_due_stays_finite_with_dirty_bounds(self):
        """`now + nan * 3600` 会让 stamp 变 nan → 这条回访永远不到点。"""
        for low, high in ((NAN, NAN), (INF, INF), ("abc", "abc"), (NAN, 5.0)):
            with self.subTest(low=low, high=high):
                stamp = threads._loop_due(1e9, min_hours=low, max_hours=high)
                self.assertTrue(math.isfinite(stamp))


# ─── desire：念头结算 / 作息 / 势头 ───


class DesireNonFiniteTest(unittest.TestCase):
    def test_rhythm_factor_treats_dirty_hours_as_unknown(self):
        """nan 原来让 `mine >= peak * 0.6` 恒假 → 静默降级成 RHYTHM_OFF，不是 UNKNOWN。"""
        user = {"rhythm_samples": 10, "active_hours": [NAN] + [0.0] * 23}
        self.assertEqual(desire.rhythm_factor(user, 12), desire.RHYTHM_UNKNOWN)

    def test_rhythm_factor_treats_infinite_peak_as_unknown(self):
        hours = [0.0] * 23 + [INF]
        user = {"rhythm_samples": 10, "active_hours": hours}
        self.assertEqual(desire.rhythm_factor(user, 12), desire.RHYTHM_UNKNOWN)

    def test_momentum_factor_with_dirty_last_spoken_is_neutral(self):
        """`hours = nan` 让两个守卫（`< 0` / `> WINDOW`）全为假 → 返回 nan。"""
        for bad in (NAN, INF, "abc"):
            with self.subTest(last_spoken=bad):
                self.assertEqual(desire.momentum_factor({"last_spoken": bad}, 1000.0), 1.0)

    def test_settle_never_writes_a_non_finite_urge(self):
        """`last_spoken=nan` 原来让 settle 返回 nan 并把 nan 写回 `user["urge"]`。"""
        user = {"urge": 0.0, "urge_at": 1000.0, "interest": 0.5, "last_spoken": NAN}
        out = desire.settle(
            user, 1000.0 + 3 * 3600, refill_hours=2.0, recent_talk_seconds=60.0
        )
        self.assertTrue(math.isfinite(out))
        self.assertTrue(math.isfinite(user["urge"]))

    def test_settle_survives_a_dirty_rhythm(self):
        for bad in (NAN, INF, "abc"):
            with self.subTest(rhythm=bad):
                user = {"urge": 0.5, "urge_at": 1000.0, "interest": 0.5}
                out = desire.settle(
                    user,
                    1000.0 + 3600,
                    refill_hours=2.0,
                    recent_talk_seconds=60.0,
                    rhythm=bad,
                )
                self.assertTrue(math.isfinite(out))

    def test_clamp_rejects_non_finite(self):
        """`lo if x < lo else hi if x > hi else x` 原来把 nan 原样放行。"""
        self.assertEqual(desire.clamp(NAN, 0.0, 1.0), 0.0)
        self.assertEqual(desire.clamp(INF, 0.0, 1.0), 0.0)
        self.assertEqual(desire.clamp("abc", 0.0, 1.0), 0.0)
        self.assertEqual(desire.clamp(0.5, 0.0, 1.0), 0.5)


# ─── throttle：开机头几分钟不能吞日志 ───


class ThrottleFirstCallTest(unittest.TestCase):
    def test_first_call_always_passes_even_on_a_fresh_clock(self):
        """`self._last.get(key, (0.0, 0))` 让 monotonic 小于 window 时首次调用返回 False。"""
        log = throttle.LogThrottle()
        with _monotonic(5.0):
            self.assertTrue(log.allow("k"))
            self.assertEqual(log.summary("k"), "")

    def test_second_call_inside_the_window_is_suppressed(self):
        log = throttle.LogThrottle()
        with _monotonic(5.0):
            self.assertTrue(log.allow("k"))
            self.assertFalse(log.allow("k"))
            self.assertIn("1", log.summary("k"))

    def test_key_is_available_again_after_the_window(self):
        log = throttle.LogThrottle()
        with _monotonic(5.0):
            self.assertTrue(log.allow("k"))
        with _monotonic(400.0):
            self.assertTrue(log.allow("k"))


class _monotonic:
    """把 `time.monotonic` 换成可控值（throttle 的窗口是按 monotonic 算的）。"""

    def __init__(self, value: float):
        self.value = value

    def __enter__(self):
        import time

        self._patch = mock.patch.object(time, "monotonic", lambda: self.value)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


# ─── verify：只有符号的正文不算正文 ───


class VerifyPunctuationOnlyTest(unittest.TestCase):
    def test_symbol_only_bodies_are_rejected(self):
        """`_PUNCT_ONLY` 原来只覆盖中英文标点，`###` / `《》` / `@@@@` 全都过闸。"""
        for body in ("###", "《》〈〉", "〖〗〖〗", "@@@@", "|||", "***", "{{}}"):
            with self.subTest(body=body):
                ok, why = verify.verify_message(body)
                self.assertFalse(ok, f"{body!r} 应该被拒")
                self.assertTrue(why)

    def test_real_punctuation_is_still_rejected(self):
        for body in ("。", "。。。", "，、；", "...", "---", "!!!"):
            with self.subTest(body=body):
                self.assertFalse(verify.verify_message(body)[0])

    def test_emoji_only_body_still_passes(self):
        """emoji 是内容，不是标点——这条不能被上面的修复误伤。"""
        self.assertIsNone(verify._check_structure("😊😊😊"))

    def test_normal_text_passes_structure(self):
        self.assertIsNone(verify._check_structure("今天降温了，你那边冷不冷"))


# ─── anchors：由头池的冷却 / 试次 / 有效期 ───


def _anchor(**kw):
    item = {
        "kind": "greet",
        "about": "在干嘛呢",
        "due": 0.0,
        "used_at": 0.0,
        "use_count": 0,
        "expire_at": 9e12,
    }
    item.update(kw)
    return item


class AnchorsNonFiniteTest(unittest.TestCase):
    def test_unreadable_used_at_is_not_treated_as_never_used(self):
        """`used_at=nan` 原来让 `now - used < COOLDOWN` 恒假 → 冷却形同不存在。"""
        for bad in (NAN, INF, "abc"):
            with self.subTest(used_at=bad):
                u = {"anchors": [_anchor(used_at=bad)]}
                self.assertEqual(anchors.live_anchors(u, 1e9), [])

    def test_unreadable_tries_is_not_treated_as_no_tries(self):
        """`int(_f(nan))` 原来抛 ValueError；归零又等于「一次都没试过」。"""
        for bad in (NAN, INF, "abc"):
            with self.subTest(tries=bad):
                u = {"anchors": [_anchor(tries=bad)]}
                self.assertEqual(anchors.live_anchors(u, 1e9), [])

    def test_unreadable_due_does_not_mean_already_due(self):
        for bad in (NAN, INF, "abc"):
            with self.subTest(due=bad):
                u = {"anchors": [_anchor(due=bad)]}
                self.assertEqual(anchors.live_anchors(u, 1e9), [])

    def test_unreadable_expire_at_is_pruned(self):
        """`expire_at=nan` 原来永不淘汰，长期占住 ANCHOR_MAX 的槽。"""
        for bad in (NAN, INF, "abc"):
            with self.subTest(expire_at=bad):
                u = {"anchors": [_anchor(expire_at=bad)]}
                anchors.prune_anchors(u, 1e9)
                self.assertEqual(u["anchors"], [])

    def test_healthy_anchor_is_still_returned(self):
        """别把上面几条护栏做成「由头池永远空」——正常的必须照常返回。"""
        u = {"anchors": [_anchor()]}
        self.assertEqual([a["about"] for a in anchors.live_anchors(u, 1e9)], ["在干嘛呢"])

    def test_missing_tries_field_is_not_corruption(self):
        """`tries` 平时根本不存在（`add_anchor` 不写它），缺字段不算坏。"""
        u = {"anchors": [_anchor()]}
        self.assertEqual(len(anchors.live_anchors(u, 1e9)), 1)

    def test_consume_and_mark_failed_survive_dirty_counters(self):
        for bad in (NAN, INF, "abc"):
            with self.subTest(counter=bad):
                u = {"anchors": [_anchor(use_count=bad, tries=bad)]}
                anchors.consume(u, "在干嘛呢", 1e9)
                anchors.mark_failed(u, "在干嘛呢", 1e9)

    def test_add_anchor_never_stores_a_non_finite_due(self):
        """`_f(due) or now` 里 nan 是真值 → 存进 state 的 due 就是 nan（后两条的源头）。"""
        for bad in (NAN, INF, "abc"):
            with self.subTest(due=bad):
                u = {}
                item = anchors.add_anchor(u, "greet", "在干嘛呢", due=bad, now=1e9)
                self.assertIsNotNone(item)
                self.assertTrue(math.isfinite(item["due"]))

    def test_tier_note_never_prints_nan(self):
        """`nan` 原来会字面进提示词：「TA 对你的好感 nan」。"""
        for tier in ("high", "mid", "low"):
            with self.subTest(tier=tier):
                out = anchors.tier_note(tier, NAN, NAN)
                self.assertNotIn("nan", out.lower())

    def test_relation_tier_treats_unreadable_affection_as_unknown(self):
        """`aff < FLOOR` 对 nan 恒假 → 好感坏掉的用户被判成中档、按中档间隔找人。"""
        bad = anchors.relation_tier({"affection": NAN}, affection=NAN)
        unknown = anchors.relation_tier({}, affection=None)
        best = anchors.relation_tier({"affection": 95.0}, affection=95.0)
        self.assertEqual(bad, unknown)
        self.assertNotEqual(bad, best)

    def test_corrupt_helper_distinguishes_missing_from_broken(self):
        self.assertFalse(anchors._corrupt({}, "used_at"))
        self.assertFalse(anchors._corrupt({"used_at": 0.0}, "used_at"))
        self.assertTrue(anchors._corrupt({"used_at": NAN}, "used_at"))
        self.assertTrue(anchors._corrupt({"used_at": "abc"}, "used_at"))


# ─── groupflow：群状态里的计数与时间戳 ───


class GroupflowNonFiniteTest(unittest.TestCase):
    def test_dirty_fields_do_not_raise(self):
        """裸 `float()` / `int()` 原来抛 ValueError，群消息链路与心跳当场断掉。"""
        for bad in (NAN, INF, "abc", None):
            with self.subTest(value=bad):
                group = {
                    "blocked_until": bad,
                    "flow_open_until": bad,
                    "flow_hold_until": bad,
                    "flow_ignored": bad,
                    "flow_replies": bad,
                    "flow_last_reply_at": bad,
                }
                ok, why = groupflow.flow_should_consider(
                    group,
                    1e9,
                    max_replies=3,
                    min_gap_seconds=60.0,
                    hourly_cap=5,
                    hour_count=0,
                    ignored_exit=3,
                )
                self.assertIsInstance(ok, bool)
                self.assertIsInstance(why, str)

    def test_icebreak_and_topic_gates_do_not_raise(self):
        for bad in (NAN, INF, "abc", None):
            with self.subTest(value=bad):
                group = {
                    "blocked_until": bad,
                    "msg_count": bad,
                    "last_seen": bad,
                    "icebreak_at": bad,
                    "topic_at": bad,
                    "last_bot_spoke": bad,
                    "icebreak_count_day": bad,
                    "topic_count_day": bad,
                    "icebreak_day": "2026-08-22",
                    "topic_day": "2026-08-22",
                }
                groupflow.icebreak_due(
                    group,
                    1e9,
                    idle_hours=6.0,
                    daily_cap=2,
                    stale_days=30,
                    today="2026-08-22",
                    is_quiet=False,
                )
                groupflow.group_topic_due(
                    group,
                    1e9,
                    gap_hours=6.0,
                    daily_cap=2,
                    stale_days=30,
                    today="2026-08-22",
                    is_quiet=False,
                )

    def test_closed_attention_window_is_the_conservative_branch(self):
        """`flow_open_until=nan` 原来让关注窗口**恒开**（心流永远可接）。"""
        group = {"blocked_until": 0.0, "flow_open_until": NAN}
        ok, why = groupflow.flow_should_consider(
            group,
            1e9,
            max_replies=3,
            min_gap_seconds=60.0,
            hourly_cap=5,
            hour_count=0,
            ignored_exit=3,
        )
        self.assertFalse(ok)
        self.assertIn("关注窗口", why)

    def test_has_no_body_ignores_emoji_and_punctuation(self):
        for text in ("😊", "   ", "！？", "[图片]"):
            with self.subTest(text=text):
                self.assertTrue(groupflow._has_no_body(text))
        self.assertFalse(groupflow._has_no_body("你好"))


# ─── reasoning：分档 / 消息类型 / 由头 ───


class ReasoningNonFiniteTest(unittest.TestCase):
    def test_relationship_tier_does_not_promote_bad_data(self):
        """`nan < 每个阈值` 恒假 → 一路走到最亲密那一档。"""
        self.assertEqual(
            reasoning.relationship_tier(NAN), reasoning.relationship_tier(None)
        )
        self.assertNotEqual(
            reasoning.relationship_tier(NAN), reasoning.relationship_tier(95.0)
        )

    def test_energy_descriptors_do_not_promote_bad_data(self):
        for fn in (reasoning.energy_descriptor, reasoning.social_energy_descriptor):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(fn(NAN), fn(None))
                self.assertNotEqual(fn(NAN), fn(95.0))

    def test_select_message_type_is_not_stuck_on_the_fallback(self):
        """权重全 nan → `random.uniform(0, nan)` → 永远落到 share_thought。"""
        user_data = {
            "_affection": NAN,
            "message_count": NAN,
            "last_seen": NAN,
            "energy": NAN,
            "social_desire": NAN,
            "hunger": NAN,
            "discomfort": NAN,
        }
        keys = {reasoning.select_message_type(user_data, 1e9)[0] for _ in range(60)}
        self.assertTrue(keys)
        self.assertGreater(len(keys), 1, f"60 次只出现了 {keys}，说明权重没算出来")

    def test_select_message_type_and_reason_survive_dirty_strings(self):
        for bad in ("abc", NAN, INF, None):
            with self.subTest(value=bad):
                user_data = {"last_seen": bad, "message_count": bad, "_affection": bad}
                reasoning.select_message_type(user_data, 1e9)
                reasoning.generate_reason(user_data, 1e9)
                reasoning._detect_continuity(user_data, 1e9)

    def test_live_cue_with_unreadable_times_is_dead(self):
        """`due=nan` 让 `now < due` 恒假（当作已到点），`expire_at=nan` 又让它永不过期。"""
        for bad in (NAN, INF, "abc"):
            with self.subTest(value=bad):
                self.assertIsNone(reasoning.live_cue({"cue": "面试", "cue_due": bad}, 1e9))
                self.assertIsNone(
                    reasoning.live_cue(
                        {"cue": "面试", "cue_due": 1.0, "cue_expire_at": bad}, 9e9
                    )
                )

    def test_live_cue_still_fires_for_a_healthy_cue(self):
        user = {"cue": "面试", "cue_due": 100.0, "cue_expire_at": 0.0}
        self.assertEqual(reasoning.live_cue(user, 200.0), "面试")

    def test_optional_number_rejects_non_finite(self):
        self.assertIsNone(reasoning._optional_number(NAN))
        self.assertIsNone(reasoning._optional_number(INF))
        self.assertIsNone(reasoning._optional_number("abc"))
        self.assertEqual(reasoning._optional_number(1.5), 1.5)


# ─── clock：时区偏移 ───


class ClockOffsetTest(unittest.TestCase):
    def test_dirty_offsets_fall_back_to_the_local_clock(self):
        """`timedelta(minutes=nan)` 原来直接抛；`nan * 60` 也是。"""
        wall = datetime(2026, 8, 22, 15, 0)
        for bad in (NAN, INF, "abc", None):
            with self.subTest(offset=bad):
                self.assertIsInstance(clock.city_now(1e9, bad), datetime)
                self.assertTrue(math.isfinite(clock.city_epoch(wall, bad)))

    def test_healthy_offset_is_still_applied(self):
        """北京 +480：UTC 零点就是她那边早上八点，反过来也对得上。"""
        self.assertEqual(clock.city_now(0.0, 480), datetime(1970, 1, 1, 8, 0))
        self.assertAlmostEqual(
            clock.city_epoch(datetime(1970, 1, 1, 8, 0), 480), 0.0, places=3
        )

    def test_missing_offset_uses_the_local_clock(self):
        self.assertIsInstance(clock.city_now(1e9, None), datetime)


# ─── textutil / emoji：小工具 ───


class SmallUtilsTest(unittest.TestCase):
    def test_outside_lead_does_not_double_up(self):
        self.assertEqual(textutil.outside_lead(""), "")
        self.assertEqual(textutil.outside_lead("外面在下雨"), "外面在下雨")
        self.assertEqual(textutil.outside_lead("下雨"), "外头下雨")

    def test_emoji_table_is_non_empty_and_compiled(self):
        from social import emoji as emoji_mod

        self.assertTrue(emoji_mod.EMOJI.pattern)


class DirtyConversationTest(unittest.TestCase):
    """`conversation` 不是 list 时，整条决策链原来会直接抛。

    这条路径每轮心跳都走：`reversed(nan)` 抛 TypeError、`[m for m in nan]` 抛
    TypeError、`reversed("abc")` 更糟——每个字符都被当成一条账，随后 `.get` 抛
    AttributeError。两个模块各加了一个 `_conversation()` 归一化，这里钉住它的口径。
    """

    DIRTY = (NAN, INF, -INF, "abc", None, 3.5, {})

    def test_threads_entry_points_survive_a_dirty_conversation(self):
        for bad in self.DIRTY:
            with self.subTest(conversation=bad):
                user = {"conversation": bad, "last_sent": 1.0, "pending_result": "waiting"}
                self.assertEqual(threads.last_in_text(user, 1e9, 3600.0), "")
                self.assertEqual(threads.recent_exchange(user), [])

    def test_reasoning_entry_points_survive_a_dirty_conversation(self):
        for bad in self.DIRTY:
            with self.subTest(conversation=bad):
                user = {"conversation": bad}
                self.assertEqual(reasoning.last_direction(user), "")
                self.assertFalse(reasoning.sleep_signal(user, 1e9))
                self.assertFalse(reasoning.busy_signal(user, 1e9))

    def test_only_dict_entries_count(self):
        """脏元素被跳过，真条目照常参与判定——不是「整份账作废」。"""
        user = {
            "conversation": [
                NAN,
                "字符串",
                None,
                {"dir": "in", "text": "她说的", "ts": 900.0},
                {"dir": "out", "text": "她自己说的", "ts": 950.0},
            ]
        }
        self.assertEqual(threads._conversation(user)[-1]["text"], "她自己说的")
        self.assertEqual(reasoning.last_direction(user), "out")
        self.assertEqual(threads.last_in_text(user, 1000.0, 3600.0), "她说的")

    def test_helpers_normalize_a_non_list(self):
        for bad in self.DIRTY:
            with self.subTest(conversation=bad):
                self.assertEqual(threads._conversation({"conversation": bad}), [])
                self.assertEqual(reasoning._conversation({"conversation": bad}), [])
        self.assertEqual(threads._conversation({}), [])
        self.assertEqual(reasoning._conversation({}), [])

    def test_healthy_conversation_is_unchanged(self):
        """归一化不该动正常数据：空文本条目仍然算一条（调用点自己筛）。"""
        user = {"conversation": [{"dir": "in", "text": "", "ts": 1.0}]}
        self.assertEqual(len(threads._conversation(user)), 1)
        self.assertEqual(len(reasoning._conversation(user)), 1)


if __name__ == "__main__":
    unittest.main()
