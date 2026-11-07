"""v2.1.5 §4：未决投递占位预算（social.budget.occupancy / budget_allows）。

口径照参考包 companion_v2/proactive/budget.py:64-90、:193-205：
pending / dispatched / accepted / unknown 四态一律占名额（不设时间窗——未决就是
未决），delivered 只有落在窗口内才算 used，not_sent 立刻释放。

为什么这些用例重要：这里少算一条，用户就被多刷一条；这里多算一条，
用户就收不到本该收到的消息。所以两个方向都要钉住，另外脏账本必须 fail-open。

用例分组：
- A 组 四态占用与释放口径；
- B 组 脏数据与未知态（从严 / 跳过）；
- C 组 参数越界与 fail-open；
- D 组 budget_allows 的放行矩阵；
- E 组 常量契约与真实混合场景。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与 test_v213_group_closeout.py 同款，幂等） ───
_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


class _FakeMessageChain:
    def message(self, text):
        return ("chain", text)


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v215_budget_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _FakeMessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import budget  # noqa: E402

NOW = 1_700_000_000.0
WINDOW = 3600.0


def _entry(state, at=None):
    item = {"state": state}
    if at is not None:
        item["at"] = at
    return item


class OccupyReleaseTest(unittest.TestCase):
    """A 组：四态占用、delivered 窗口、not_sent 释放。"""

    def test_empty_is_not_full(self):
        state = budget.occupancy([], NOW)
        self.assertEqual(
            state,
            {
                "used": 0,
                "reserved": 0,
                "occupied": 0,
                "limit": budget.DEFAULT_LIMIT,
                "remaining": budget.DEFAULT_LIMIT,
                "full": False,
            },
        )

    def test_four_unresolved_states_occupy(self):
        for name in ("pending", "dispatched", "accepted", "unknown"):
            with self.subTest(state=name):
                state = budget.occupancy([_entry(name, NOW - 5)], NOW)
                self.assertEqual(state["reserved"], 1)
                self.assertEqual(state["used"], 0)
                self.assertEqual(state["occupied"], 1)

    def test_unresolved_ignores_time_window(self):
        # 昨天没回执的也还欠着：未决四态不设时间窗
        state = budget.occupancy([_entry("pending", NOW - 86_400 * 3)], NOW)
        self.assertEqual(state["occupied"], 1)

    def test_delivered_inside_window_counts_used(self):
        state = budget.occupancy([_entry("delivered", NOW - 60)], NOW)
        self.assertEqual(state["used"], 1)
        self.assertEqual(state["reserved"], 0)

    def test_delivered_at_window_edge_counts_used(self):
        state = budget.occupancy([_entry("delivered", NOW - WINDOW)], NOW, window_seconds=WINDOW)
        self.assertEqual(state["used"], 1)

    def test_delivered_outside_window_released(self):
        state = budget.occupancy([_entry("delivered", NOW - WINDOW - 1)], NOW, window_seconds=WINDOW)
        self.assertEqual(state["used"], 0)
        self.assertEqual(state["occupied"], 0)

    def test_not_sent_releases_immediately(self):
        state = budget.occupancy([_entry("not_sent", NOW - 1)], NOW)
        self.assertEqual(state["occupied"], 0)

    def test_case_and_space_normalized(self):
        state = budget.occupancy([{"state": "  Pending ", "at": NOW - 1}], NOW)
        self.assertEqual(state["reserved"], 1)

    def test_used_and_reserved_add_up(self):
        entries = [
            _entry("delivered", NOW - 10),
            _entry("delivered", NOW - 20),
            _entry("pending", NOW - 30),
        ]
        state = budget.occupancy(entries, NOW)
        self.assertEqual((state["used"], state["reserved"], state["occupied"]), (2, 1, 3))


class DirtyAndUnknownTest(unittest.TestCase):
    """B 组：脏数据跳过、未知态从严。"""

    def test_dirty_entries_skipped(self):
        entries = [None, 42, "pending", [], {}, {"at": None}, {"at": "soon"}, {"state": "pending"}]
        state = budget.occupancy(entries, NOW)
        self.assertEqual(state["occupied"], 0)

    def test_unknown_state_counted_as_occupying(self):
        state = budget.occupancy([_entry("whatever", NOW - 1)], NOW)
        self.assertEqual(state["reserved"], 1)

    def test_empty_state_counted_as_occupying(self):
        state = budget.occupancy([{"at": NOW - 1, "state": ""}], NOW)
        self.assertEqual(state["reserved"], 1)

    def test_nan_and_inf_at_skipped(self):
        entries = [
            {"at": float("nan"), "state": "pending"},
            {"at": float("inf"), "state": "pending"},
        ]
        self.assertEqual(budget.occupancy(entries, NOW)["occupied"], 0)

    def test_object_entries_supported(self):
        class Row:
            def __init__(self, at, state):
                self.at = at
                self.state = state

        state = budget.occupancy([Row(NOW - 1, "delivered")], NOW)
        self.assertEqual(state["used"], 1)

    def test_broken_property_fails_open(self):
        class Boom:
            at = NOW - 1

            @property
            def state(self):
                raise RuntimeError("boom")

        state = budget.occupancy([Boom()], NOW)
        self.assertFalse(state["full"])
        self.assertEqual(state["occupied"], 0)


class LimitAndParamTest(unittest.TestCase):
    """C 组：limit 边界与参数越界。"""

    def test_full_at_limit(self):
        entries = [_entry("pending", NOW - i) for i in range(3)]
        state = budget.occupancy(entries, NOW, limit=3)
        self.assertTrue(state["full"])
        self.assertEqual(state["remaining"], 0)

    def test_not_full_below_limit(self):
        entries = [_entry("pending", NOW - i) for i in range(2)]
        state = budget.occupancy(entries, NOW, limit=3)
        self.assertFalse(state["full"])
        self.assertEqual(state["remaining"], 1)

    def test_remaining_never_negative(self):
        entries = [_entry("pending", NOW - i) for i in range(10)]
        state = budget.occupancy(entries, NOW, limit=3)
        self.assertEqual(state["remaining"], 0)
        self.assertTrue(state["full"])

    def test_bad_limit_falls_back_to_default(self):
        for bad in ("x", -3, None):
            with self.subTest(limit=bad):
                self.assertEqual(
                    budget.occupancy([], NOW, limit=bad)["limit"], budget.DEFAULT_LIMIT
                )

    def test_bad_window_falls_back_to_default(self):
        entries = [_entry("delivered", NOW - 10)]
        for bad in ("x", -1, float("nan")):
            with self.subTest(window=bad):
                self.assertEqual(
                    budget.occupancy(entries, NOW, window_seconds=bad)["used"], 1
                )

    def test_bad_now_fails_open(self):
        entries = [_entry("pending", NOW - 1)]
        for bad in (None, "now", float("nan")):
            with self.subTest(now=bad):
                state = budget.occupancy(entries, bad)
                self.assertFalse(state["full"])
                self.assertEqual(state["occupied"], 0)

    def test_non_iterable_entries_fail_open(self):
        state = budget.occupancy(42, NOW)
        self.assertFalse(state["full"])


class BudgetAllowsTest(unittest.TestCase):
    """D 组：budget_allows 的放行矩阵。"""

    def test_blocks_when_full_and_zero_remaining(self):
        self.assertFalse(budget.budget_allows({"full": True, "remaining": 0}))

    def test_reply_send_always_allowed(self):
        self.assertTrue(
            budget.budget_allows({"full": True, "remaining": 0}, reserve_for_reply=False)
        )

    def test_allows_when_not_full(self):
        self.assertTrue(budget.budget_allows({"full": False, "remaining": 0}))

    def test_allows_when_remaining_positive(self):
        self.assertTrue(budget.budget_allows({"full": True, "remaining": 1}))

    def test_bad_state_fails_open(self):
        for bad in (None, "x", 42, [], {}, {"full": True}, {"full": "yes"}):
            with self.subTest(state=bad):
                self.assertTrue(budget.budget_allows(bad))


class ContractAndScenarioTest(unittest.TestCase):
    """E 组：常量契约 + 线上混合账本。"""

    def test_state_constants(self):
        self.assertEqual(
            set(budget.OCCUPYING_STATES),
            {"pending", "dispatched", "accepted", "unknown"},
        )
        self.assertEqual(set(budget.RELEASING_STATES), {"delivered", "not_sent"})

    def test_mixed_ledger(self):
        entries = [
            _entry("delivered", NOW - 10),
            _entry("delivered", NOW - 20),
            _entry("delivered", NOW - 30),
            _entry("delivered", NOW - WINDOW - 5),  # 窗口外，释放
            _entry("pending", NOW - 40),
            _entry("dispatched", NOW - 50),
            _entry("not_sent", NOW - 60),           # 释放
            _entry("mystery", NOW - 70),            # 未知态从严
        ]
        state = budget.occupancy(entries, NOW, window_seconds=WINDOW, limit=6)
        self.assertEqual(
            state,
            {
                "used": 3,
                "reserved": 3,
                "occupied": 6,
                "limit": 6,
                "remaining": 0,
                "full": True,
            },
        )
        self.assertFalse(budget.budget_allows(state))
        self.assertTrue(budget.budget_allows(state, reserve_for_reply=False))


if __name__ == "__main__":
    unittest.main()
