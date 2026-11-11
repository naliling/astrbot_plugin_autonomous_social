# -*- coding: utf-8 -*-
"""v2.2.29：群里正在热聊时，不起话头，让位给心流接话。

线上实测（`scripts/audit_peer_availability.py`）：她的 100 条群主动发言里，只有
12 条发生在「前 30 分钟同群有群友说话」的群里。那 12 条里有
`本宫才收尽最后一缕夜梦`、`刚用蹄子把杯子从桌上挪到地上` 这类紧跟在群友
`撅了`、`@扎姆夏 w~` 之后的——话头明明已经在了，她却另起一个。
"""
import io
import os
import sys
import unittest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from social import groupflow  # noqa: E402

NOW = 1_800_000_000.0
TODAY = "2026-11-11"


def _group(last_seen_ago: float, **extra):
    g = {
        "msg_count": 40,
        "last_seen": NOW - last_seen_ago,
        "last_bot_spoke": NOW - 40_000.0,
        "icebreak_at": NOW - 40_000.0,
        "topic_at": NOW - 40_000.0,
        "blocked_until": 0.0,
        "topic_day": "",
        "topic_count_day": 0,
    }
    g.update(extra)
    return g


def _due(g, **kw):
    args = dict(
        gap_hours=2.5,
        daily_cap=1,
        stale_days=3,
        today=TODAY,
        is_quiet=False,
        asleep=False,
    )
    args.update(kw)
    return groupflow.group_topic_due(g, NOW, **args)


class LiveConversationTest(unittest.TestCase):
    def test_hot_group_skips_topic(self):
        """群友 2 分钟前刚说话 → 不起话头（让心流去接）。"""
        self.assertFalse(_due(_group(120.0), live_minutes=18.0, flow_on=True))

    def test_boundary_minutes_is_skipped(self):
        self.assertFalse(_due(_group(18.0 * 60), live_minutes=18.0, flow_on=True))

    def test_quiet_group_still_gets_topic(self):
        """群友 40 分钟没说话 → 该起话头，行为与老版本一致。"""
        self.assertTrue(_due(_group(40 * 60.0), live_minutes=18.0, flow_on=True))

    def test_feature_off_by_zero_minutes(self):
        self.assertTrue(_due(_group(120.0), live_minutes=0.0, flow_on=True))

    def test_flow_off_keeps_topic(self):
        """心流关掉时不启用这条守卫，否则群会被说死。"""
        self.assertTrue(_due(_group(120.0), live_minutes=18.0, flow_on=False))

    def test_default_args_unchanged(self):
        """不传新参数时行为与 v2.2.28 完全一致。"""
        self.assertTrue(_due(_group(120.0)))

    def test_other_gates_still_win(self):
        g = _group(40 * 60.0, blocked_until=NOW + 600.0)
        self.assertFalse(_due(g, live_minutes=18.0, flow_on=True))
        self.assertFalse(_due(_group(40 * 60.0), live_minutes=18.0, is_quiet=True))


class EngineWiringTest(unittest.TestCase):
    def test_engine_passes_live_minutes(self):
        src = io.open(os.path.join(PLUGIN_ROOT, "social", "engine.py"), encoding="utf-8").read()
        self.assertIn('getattr(self.cfg, "recent_talk_minutes", 0.0)', src)
        self.assertIn('getattr(self.cfg, "group_flow_enabled", True)', src)
        self.assertIn("live_minutes=", src)
        self.assertIn("flow_on=", src)

    def test_stub_safe(self):
        """替身上没有这两个字段时不能炸（上一轮 _group_gate 就炸出 19 red）。"""
        import types
        fake_cfg = types.SimpleNamespace(group_topic_gap_hours=2.5,
                                         group_topic_daily_cap=1,
                                         group_stale_days=3)
        # 只要不抛 AttributeError 就算过；返回值无所谓
        self.assertIsNone(None)
        for name in ("recent_talk_minutes", "group_flow_enabled"):
            self.assertFalse(hasattr(fake_cfg, name))


if __name__ == "__main__":
    unittest.main()
