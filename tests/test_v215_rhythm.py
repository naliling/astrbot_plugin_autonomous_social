# -*- coding: utf-8 -*-
"""v2.1.5：连发轮次与收口静默（social/rhythm.py）。

钉四件事：

1. 未确认送达**立刻结束本轮**：这是参考包 rhythm.py:164-235 的硬规则，也是这个模块
   存在的主要理由——它挡的不是「话多」，是「叠在没送达的消息后面继续说」。
2. 窗口、阈值、冷却三个边界：达到阈值才 active；窗口外的旧消息不参与；冷却决定
   remaining_seconds，冷却过完仍 active 时 round_allows 必须放行（冷却后首条）。
3. 收口 grace：连续够多之后压一段静默，过期自然解除；grace <= 0 等于关闭。
4. fail-open：脏数据、非法参数、坏 state 一律不拦，绝不抛异常。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与其它测试同款，幂等） ───
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
    api.logger = logging.getLogger("autonomous_social_v215_rhythm_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import rhythm  # noqa: E402

NOW = 1_800_000_000.0


def _round(at, delivered=True):
    return {"at": at, "delivered": delivered}


class TestBurstState(unittest.TestCase):
    def test_empty_rounds_is_inactive(self):
        state = rhythm.burst_state([], NOW)
        self.assertFalse(state["active"])
        self.assertEqual(state["count"], 0)
        self.assertEqual(state["until"], 0.0)
        self.assertEqual(state["remaining_seconds"], 0.0)

    def test_three_delivered_activates(self):
        rounds = [_round(NOW - 300), _round(NOW - 200), _round(NOW - 100)]
        state = rhythm.burst_state(rounds, NOW)
        self.assertTrue(state["active"])
        self.assertEqual(state["count"], 3)
        self.assertEqual(state["until"], NOW - 100 + 1800.0)
        self.assertEqual(state["remaining_seconds"], 1700.0)

    def test_below_threshold_stays_inactive(self):
        state = rhythm.burst_state([_round(NOW - 200), _round(NOW - 100)], NOW)
        self.assertFalse(state["active"])
        self.assertEqual(state["count"], 2)

    def test_unconfirmed_newest_ends_round(self):
        rounds = [_round(NOW - 300), _round(NOW - 200), _round(NOW - 100, delivered=False)]
        state = rhythm.burst_state(rounds, NOW)
        self.assertFalse(state["active"])
        self.assertEqual(state["count"], 0)

    def test_run_stops_at_first_unconfirmed(self):
        # 最新一条已送达，再往前一条没确认 → 轮次在那一刀切断，只算最新这条。
        rounds = [
            _round(NOW - 500),
            _round(NOW - 400),
            _round(NOW - 300, delivered=False),
            _round(NOW - 100),
        ]
        state = rhythm.burst_state(rounds, NOW)
        self.assertEqual(state["count"], 1)
        self.assertFalse(state["active"])

    def test_window_drops_old_rounds(self):
        rounds = [_round(NOW - 5000), _round(NOW - 4000), _round(NOW - 3700)]
        state = rhythm.burst_state(rounds, NOW)
        self.assertEqual(state["count"], 0)
        self.assertFalse(state["active"])

    def test_only_rounds_inside_window_count(self):
        rounds = [_round(NOW - 3700), _round(NOW - 100)]
        state = rhythm.burst_state(rounds, NOW)
        self.assertEqual(state["count"], 1)

    def test_cooldown_expired_keeps_active_but_zero_remaining(self):
        # 窗口一小时、冷却半小时：冷却过完之后仍可能 active，remaining 为 0。
        rounds = [_round(NOW - 2500), _round(NOW - 2400), _round(NOW - 2000)]
        state = rhythm.burst_state(rounds, NOW)
        self.assertTrue(state["active"])
        self.assertEqual(state["remaining_seconds"], 0.0)

    def test_dirty_rounds_are_skipped(self):
        rounds = [
            None,
            "x",
            {},
            {"at": "abc", "delivered": True},
            {"at": True, "delivered": True},
            {"at": float("nan"), "delivered": True},
            {"at": NOW + 600, "delivered": True},
            _round(NOW - 100),
            _round(NOW - 50),
        ]
        state = rhythm.burst_state(rounds, NOW)
        self.assertEqual(state["count"], 2)
        self.assertFalse(state["active"])

    def test_delivered_must_be_literal_true(self):
        # 读不出来的 delivered 按未确认处理 → 轮次更早结束（更早恢复说话）。
        state = rhythm.burst_state([{"at": NOW - 10, "delivered": 1}], NOW)
        self.assertEqual(state["count"], 0)

    def test_bad_params_fail_open(self):
        # 两条低于默认阈值 3：参数坏掉回退默认值时也不会误判成 active。
        rounds = [_round(NOW - 100), _round(NOW - 90)]
        for kwargs in (
            {"window_seconds": 0},
            {"threshold": 0},
            {"cooldown_seconds": -5},
            {"window_seconds": "x"},
            {"threshold": "x"},
        ):
            state = rhythm.burst_state(rounds, NOW, **kwargs)
            self.assertFalse(state["active"], kwargs)
        self.assertFalse(rhythm.burst_state(rounds, None)["active"])
        self.assertFalse(rhythm.burst_state(rounds, "now")["active"])

    def test_rounds_input_is_not_mutated(self):
        rounds = [_round(NOW - 100), _round(NOW - 300)]
        snapshot = [dict(item) for item in rounds]
        rhythm.burst_state(rounds, NOW)
        self.assertEqual(rounds, snapshot)

    def test_returns_all_keys(self):
        state = rhythm.burst_state([], NOW)
        self.assertEqual(
            set(state),
            {
                "active",
                "count",
                "until",
                "window_seconds",
                "threshold",
                "cooldown_seconds",
                "remaining_seconds",
            },
        )


class TestClosingHold(unittest.TestCase):
    def test_below_threshold_no_hold(self):
        state = rhythm.closing_hold([_round(NOW - 100), _round(NOW - 50)], NOW)
        self.assertFalse(state["hold"])
        self.assertEqual(state["until"], 0.0)
        self.assertEqual(state["remaining_seconds"], 0.0)

    def test_threshold_holds_for_grace(self):
        rounds = [_round(NOW - 300), _round(NOW - 200), _round(NOW - 100)]
        state = rhythm.closing_hold(rounds, NOW)
        self.assertTrue(state["hold"])
        self.assertEqual(state["until"], NOW - 100 + 900.0)
        self.assertEqual(state["remaining_seconds"], 800.0)

    def test_grace_expires(self):
        rounds = [_round(NOW - 3000), _round(NOW - 2900), _round(NOW - 1000)]
        state = rhythm.closing_hold(rounds, NOW)
        self.assertFalse(state["hold"])
        self.assertEqual(state["until"], NOW - 1000 + 900.0)
        self.assertEqual(state["remaining_seconds"], 0.0)

    def test_grace_zero_disables(self):
        rounds = [_round(NOW - 300), _round(NOW - 200), _round(NOW - 100)]
        state = rhythm.closing_hold(rounds, NOW, grace_seconds=0)
        self.assertFalse(state["hold"])

    def test_unconfirmed_newest_breaks_the_run(self):
        rounds = [_round(NOW - 300), _round(NOW - 200), _round(NOW - 100, delivered=False)]
        state = rhythm.closing_hold(rounds, NOW)
        self.assertFalse(state["hold"])

    def test_dirty_input_does_not_raise(self):
        for rounds in (None, "x", 0, [None, {}, {"at": "x"}, {"at": NOW + 900}]):
            state = rhythm.closing_hold(rounds, NOW)
            self.assertFalse(state["hold"])

    def test_empty_rounds(self):
        state = rhythm.closing_hold([], NOW)
        self.assertEqual(state, {"hold": False, "until": 0.0, "remaining_seconds": 0.0})


class TestRoundAllows(unittest.TestCase):
    def test_reply_always_allowed(self):
        self.assertTrue(rhythm.round_allows("reply", {"hold": True}))
        self.assertTrue(rhythm.round_allows("reply", {"active": True, "remaining_seconds": 999}))

    def test_proactive_blocked_by_hold(self):
        self.assertFalse(rhythm.round_allows("proactive", {"hold": True}))

    def test_proactive_blocked_while_cooldown_remains(self):
        state = {"active": True, "remaining_seconds": 120.0}
        self.assertFalse(rhythm.round_allows("proactive", state))
        self.assertFalse(rhythm.round_allows("followup", state))

    def test_proactive_allowed_after_cooldown(self):
        # 冷却已过 → 这就是「冷却后首条」，必须放行。
        state = {"active": True, "remaining_seconds": 0.0}
        self.assertTrue(rhythm.round_allows("proactive", state))
        self.assertTrue(rhythm.round_allows("followup", state))

    def test_unknown_kind_allowed(self):
        self.assertTrue(rhythm.round_allows("reminder", {"hold": True}))
        self.assertTrue(rhythm.round_allows(None, {"hold": True}))

    def test_bad_state_fail_open(self):
        for state in (None, "x", [], 0, True):
            self.assertTrue(rhythm.round_allows("proactive", state))

    def test_missing_remaining_seconds_fail_open(self):
        self.assertTrue(rhythm.round_allows("proactive", {"active": True}))

    def test_plain_allowed_when_nothing_pending(self):
        self.assertTrue(rhythm.round_allows("proactive", {"hold": False, "active": False}))

    def test_burst_state_feeds_round_allows(self):
        rounds = [_round(NOW - 300), _round(NOW - 200), _round(NOW - 100)]
        state = rhythm.burst_state(rounds, NOW)
        self.assertFalse(rhythm.round_allows("proactive", state))
        # 冷却走完之后同一条判据必须放行。
        later = rhythm.burst_state(rounds, NOW + 1800.0)
        self.assertTrue(rhythm.round_allows("proactive", later))


if __name__ == "__main__":
    unittest.main()
