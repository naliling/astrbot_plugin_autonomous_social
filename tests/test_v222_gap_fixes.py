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


class SegmentPassthroughTest(unittest.TestCase):
    def test_generated_segments_and_waits_are_preserved(self):
        segments = ["我这边刚收拾完，你那边呢", "是啊"]
        waits = [1.5]
        self.assertEqual(
            engine_mod._cap_group_segments(segments, waits, cap=3),
            (segments, waits),
        )

    def test_over_cap_draft_is_rejected_without_merging(self):
        segments = ["第一段", "第二段", "第三段"]
        self.assertEqual(
            engine_mod._cap_group_segments(segments, [1.0, 2.0], cap=2),
            ([], []),
        )


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


class SleepStateGateTest(unittest.TestCase):
    def test_asleep_state_is_passed_to_the_group_gate_unchanged(self):
        seen = []

        def due(_group, _now, **kwargs):
            seen.append(kwargs.get("asleep"))
            return not kwargs.get("asleep")

        with mock.patch.object(
            engine_mod, "_person_flow_state", lambda *_a, **_k: (False, True)
        ), mock.patch.object(groupflow, "icebreak_due", due):
            picked = engine_mod.SocialEngine._icebreak_pick(_FakeEngine(), "b1", NOW)

        self.assertEqual(seen, [True])
        self.assertEqual(picked, [])


class GroupDailyCapTest(unittest.TestCase):
    """71 个群 + 12 条/天 = 六天轮一圈，主人只看到「永远那几个群」。"""

    def test_default_is_unlimited(self):
        """v2.2.47：默认 0 = 不限。

        用户原话「一天只能主动12条不行 要么没限制要么把这个删掉 我要的是那个人的
        主动 人是想说就说的」——所以默认值从 20 改成 0（0 在
        `_group_daily_exhausted` 里直接 return False，即不拦）。想限制的人填正数。
        """
        self.assertEqual(config.GROUP_DAILY_TOTAL_CAP_DEFAULT, 0)


if __name__ == "__main__":
    unittest.main()
