# -*- coding: utf-8 -*-
"""v2.2.32：同一个群里，几个角色不抢话。

线上铁证（群 1124178010，2026-11-08）：天鼠 17:35:13 破冰「今天就想窝着喝
热牛奶」，3.8 秒后茉心在同一个群说「水池里的碗总算洗了」——群侧冷却原来按
`(bot, gid)` 各记一本账、互不相看，两个"人"同时开口，读起来就像同一个人在
自言自语。这里只挡**主动开口**（破冰/起话头），不挡回人话。
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("astrbot")
    _event = types.ModuleType("astrbot.api.event")
    _event.MessageChain = object
    _api.event = _event
    _astrbot.api = _api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api
    sys.modules["astrbot.api.event"] = _event

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social.state import SocialState  # noqa: E402

NOW = 1_800_000_000.0
BID = "bot1"
OTHER = "bot2"
GID = "group1"


def _state() -> SocialState:
    tmp = tempfile.mkdtemp(prefix="aso_v2232_")
    st = SocialState(str(Path(tmp) / "state.json"))
    st.data = {"bots": {}}
    return st


def _put_spoke(st: SocialState, bid: str, gid: str, ts: float) -> None:
    bots = st.data.setdefault("bots", {})
    bot = bots.setdefault(bid, {})
    groups = bot.setdefault("groups", {})
    g = groups.setdefault(gid, {})
    g["last_bot_spoke"] = ts


class GroupOtherBotSpokeTest(unittest.TestCase):
    def test_returns_other_bot_timestamp(self):
        st = _state()
        _put_spoke(st, OTHER, GID, NOW - 30.0)
        self.assertEqual(st.group_other_bot_spoke(BID, GID), NOW - 30.0)

    def test_ignores_self(self):
        st = _state()
        _put_spoke(st, BID, GID, NOW - 1.0)
        self.assertEqual(st.group_other_bot_spoke(BID, GID), 0.0)

    def test_takes_the_latest_among_several(self):
        st = _state()
        _put_spoke(st, "bot2", GID, NOW - 900.0)
        _put_spoke(st, "bot3", GID, NOW - 5.0)
        self.assertEqual(st.group_other_bot_spoke(BID, GID), NOW - 5.0)

    def test_other_group_is_not_counted(self):
        st = _state()
        _put_spoke(st, OTHER, "group2", NOW - 5.0)
        self.assertEqual(st.group_other_bot_spoke(BID, GID), 0.0)

    def test_bad_records_are_safe(self):
        st = _state()
        st.data = {
            "bots": {
                OTHER: {"groups": {GID: {"last_bot_spoke": "not-a-number"}}},
                "bot3": {"groups": "broken"},
                "bot4": "not-a-dict",
            }
        }
        self.assertEqual(st.group_other_bot_spoke(BID, GID), 0.0)


class IcebreakGuardTest(unittest.TestCase):
    """闸必须挡在生成之前：挡下时 `_build_group_ctx` 一次都不许被调用。"""

    def _engine(self, st: SocialState, gap: int):
        eng = types.SimpleNamespace()
        eng.cfg = types.SimpleNamespace(
            group_cross_bot_gap_seconds=gap,
            allow_group_chat=True,
        )
        eng.state = st
        eng.logs = []
        eng.log = lambda msg, *a, **k: eng.logs.append(str(msg))
        eng.send_breaker_left = lambda bid: 0
        eng.built = []

        def _build(bid, gid, g):
            eng.built.append(gid)
            raise RuntimeError("stop-after-guard")

        eng._build_group_ctx = _build
        return eng

    def _run(self, eng, gap_now):
        g = {"umo": "umo://group1"}
        return asyncio.run(
            SocialEngine._icebreak(eng, BID, GID, g, gap_now, mode="icebreak")
        )

    def test_recent_other_bot_blocks_before_generation(self):
        st = _state()
        _put_spoke(st, OTHER, GID, NOW - 3.8)
        eng = self._engine(st, 120)
        self.assertFalse(self._run(eng, NOW))
        self.assertEqual(eng.built, [])
        self.assertTrue(any("另一个角色" in s for s in eng.logs), eng.logs)

    def test_old_other_bot_does_not_block(self):
        st = _state()
        _put_spoke(st, OTHER, GID, NOW - 600.0)
        eng = self._engine(st, 120)
        with self.assertRaises(RuntimeError):
            self._run(eng, NOW)
        self.assertEqual(eng.built, [GID])

    def test_zero_disables_the_guard(self):
        st = _state()
        _put_spoke(st, OTHER, GID, NOW - 3.8)
        eng = self._engine(st, 0)
        with self.assertRaises(RuntimeError):
            self._run(eng, NOW)
        self.assertEqual(eng.built, [GID])


class ConfigAndWiringTest(unittest.TestCase):
    def test_config_default_is_120(self):
        self.assertEqual(SocialConfig().group_cross_bot_gap_seconds, 120)

    def test_schema_has_key_with_slider(self):
        import json

        raw = io.open(
            os.path.join(str(PLUGIN_ROOT), "_conf_schema.json"), encoding="utf-8"
        ).read()
        schema = json.loads(raw)
        item = schema["group_cross_bot_gap_seconds"]
        self.assertEqual(item["type"], "int")
        self.assertEqual(item["default"], 120)
        self.assertEqual(item["min"], 0)
        self.assertEqual(item["max"], 3600)
        self.assertIn("slider", item)

    def test_readme_mentions_the_key(self):
        raw = io.open(
            os.path.join(str(PLUGIN_ROOT), "README.md"), encoding="utf-8"
        ).read()
        self.assertIn("group_cross_bot_gap_seconds", raw)

    def test_engine_source_has_the_wiring(self):
        raw = io.open(
            os.path.join(str(PLUGIN_ROOT), "social", "engine.py"), encoding="utf-8"
        ).read()
        self.assertIn("group_other_bot_spoke", raw)
        self.assertIn("group_cross_bot_gap_seconds", raw)
        idx_guard = raw.index("group_other_bot_spoke")
        # `_build_group_ctx` 在文件里别处也调用过，这里只看守卫之后的那一处：
        # 闸必须挡在「开生成」之前，不能生成完了再丢。
        idx_build = raw.index(
            "group_ctx = self._build_group_ctx(bid, gid, g)", idx_guard
        )
        self.assertLess(idx_guard, idx_build)


if __name__ == "__main__":
    unittest.main()
