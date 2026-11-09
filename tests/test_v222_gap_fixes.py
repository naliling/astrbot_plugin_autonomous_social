# -*- coding: utf-8 -*-
"""v2.2.2 补齐的四条：破冰/话头质量闸、无 Core 兜底素材、睡眠概率化、群侧吞吐。"""

from __future__ import annotations

import datetime
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = (
    _HERE.parent
    if (_HERE.parent / "_conf_schema.json").is_file()
    else _HERE.parent / "astrbot_plugin_autonomous_social"
)


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v222_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import config, engine as engine_mod, groupflow  # noqa: E402

NOW = 1_762_560_000.0


class LocalMaterialsTest(unittest.TestCase):
    """没有 Core 时也得有素材——原来七个素材源全灭，起话头一句话都起不来。"""

    def test_every_hour_has_material(self):
        base = NOW - (NOW % 86400)
        for hour in range(24):
            lines = groupflow.local_materials(base + hour * 3600)
            self.assertTrue(lines, f"{hour} 点没有兜底素材")
            self.assertTrue(all(isinstance(x, str) and x.strip() for x in lines))

    def test_count_is_respected(self):
        self.assertEqual(len(groupflow.local_materials(NOW, count=1)), 1)
        self.assertLessEqual(len(groupflow.local_materials(NOW, count=2)), 2)

    def test_rotates_by_day(self):
        day = 86400.0
        self.assertEqual(
            groupflow.local_materials(NOW, count=2),
            groupflow.local_materials(NOW, count=2),
        )
        seen = {
            tuple(groupflow.local_materials(NOW + day * i, count=2))
            for i in range(4)
        }
        self.assertGreater(len(seen), 1, "四天都是同一句")

    def test_bad_clock_returns_empty(self):
        self.assertEqual(groupflow.local_materials("nope"), [])


class GroupProactiveGateTest(unittest.TestCase):
    """破冰/起话头原来只过 `_verify_all_segments`，纯评价照样进群。"""

    def test_empty_weather_greeting_is_rejected(self):
        why = engine_mod._group_proactive_issue(["早啊，今天风挺大"])
        self.assertTrue(why, "只有一句空话，不该发")

    def test_concrete_daily_event_passes(self):
        self.assertEqual(
            engine_mod._group_proactive_issue(["下午去银行回来路上买了花"]), ""
        )

    def test_self_action_passes(self):
        self.assertEqual(
            engine_mod._group_proactive_issue(["（把翅膀拢了拢）刚把水烧上"]), ""
        )

    def test_sideways_you_is_rejected(self):
        why = engine_mod._group_proactive_issue(["我这边刚收拾完，你那边呢"])
        self.assertIn("某个人", why)

    def test_pure_echo_is_rejected(self):
        self.assertTrue(engine_mod._group_proactive_issue(["是啊是啊"]))

    def test_empty_is_rejected(self):
        self.assertTrue(engine_mod._group_proactive_issue([]))


class _FakeState:
    def groups(self, _bid):
        return {"g1": {"umo": "u1", "msg_count": 10}}


class _FakeEngine:
    cfg = types.SimpleNamespace(
        group_icebreak_enabled=True,
        group_idle_hours=1.0,
        icebreak_daily_cap=2,
        group_stale_days=3,
    )

    def __init__(self):
        self.state = _FakeState()

    def _moment_of(self, _bid, _now):
        return datetime.datetime(2026, 11, 9, 3, 0, 0)

    def _group_daily_exhausted(self, _bid, _today):
        return False


class SleepChanceTest(unittest.TestCase):
    """睡着不再等于群侧全哑：低概率放行，但不会变成半夜刷屏。"""

    def test_constant_is_low(self):
        self.assertGreater(engine_mod.ASLEEP_PROACTIVE_CHANCE, 0.0)
        self.assertLess(engine_mod.ASLEEP_PROACTIVE_CHANCE, 0.5, "别变成半夜刷屏")

    def _pick(self, chance):
        with mock.patch.object(
            engine_mod, "_person_flow_state", lambda *_a, **_k: (False, True)
        ), mock.patch.object(
            engine_mod, "ASLEEP_PROACTIVE_CHANCE", chance
        ), mock.patch.object(
            groupflow,
            "icebreak_due",
            lambda _g, _now, **kw: not kw.get("asleep"),
        ):
            return engine_mod.SocialEngine._icebreak_pick(_FakeEngine(), "b1", NOW)

    def test_chance_one_treats_her_as_awake(self):
        self.assertEqual(len(self._pick(1.0)), 1)

    def test_chance_zero_keeps_the_gate_closed(self):
        self.assertEqual(self._pick(0.0), [])


class GroupDailyCapTest(unittest.TestCase):
    """71 个群 + 12 条/天 = 六天轮一圈，主人只看到「永远那几个群」。"""

    def test_default_cap_raised(self):
        self.assertEqual(config.GROUP_DAILY_TOTAL_CAP_DEFAULT, 20)


if __name__ == "__main__":
    unittest.main()
