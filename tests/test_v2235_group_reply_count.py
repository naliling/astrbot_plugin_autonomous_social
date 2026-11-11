# -*- coding: utf-8 -*-
"""v2.2.35：群日额度那本账，要分得清「主动开口」和「接群友的话」。

线上 11-11 05:59 打过这么一行：
    今天群聊已经主动说了 13 条（上限 12），剩下的时间只回话、不再主动开口
13 比上限还大，读起来像她超额发言。查下来是**口径问题**：`note_group_send`
从 v2.1.2 起把「心流接话」（回群友的话）也算进同一本账，所以 12 条主动 + 1 条
接话 = 13，而闸只在主动入口检查，永远拦不住那第 13 条。

这个版本不动额度语义（接话仍然占额度，v2.1.2 的原始意图是压群聊总量），
只多记一本「接话」的账，并让日志说清楚这 N 条里有多少是回人话。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    import logging

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(ALL=1, GROUP_MESSAGE=2, FRIEND_MESSAGE=3)
    )
    api.event = event
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social.state import SocialState  # noqa: E402

NOW = 1_800_000_000.0
BID = "bot1"
DAY = "2030-10-11"


def _state() -> SocialState:
    tmp = tempfile.mkdtemp(prefix="aso_v2235_")
    return SocialState(str(Path(tmp) / "state.json"))


class ReplyCounterTest(unittest.TestCase):
    def test_proactive_only_does_not_touch_reply_counter(self) -> None:
        st = _state()
        st.note_group_send(BID, NOW, DAY)
        st.note_group_send(BID, NOW + 1, DAY)
        self.assertEqual(st.group_send_count(BID, DAY), 2)
        self.assertEqual(st.group_reply_count(BID, DAY), 0)

    def test_reply_counts_in_both(self) -> None:
        st = _state()
        st.note_group_send(BID, NOW, DAY)
        st.note_group_send(BID, NOW + 1, DAY, reply=True)
        self.assertEqual(st.group_send_count(BID, DAY), 2)
        self.assertEqual(st.group_reply_count(BID, DAY), 1)

    def test_day_rollover_zeroes_reply_counter(self) -> None:
        st = _state()
        st.note_group_send(BID, NOW, DAY, reply=True)
        self.assertEqual(st.group_reply_count(BID, "2030-10-12"), 0)
        self.assertEqual(st.group_send_count(BID, "2030-10-12"), 0)

    def test_broken_value_safe(self) -> None:
        st = _state()
        st.note_group_send(BID, NOW, DAY, reply=True)
        st.bot(BID)["group_reply_count_day"] = "坏值"
        self.assertEqual(st.group_reply_count(BID, DAY), 0)


class LogWordingTest(unittest.TestCase):
    def _src(self) -> str:
        return io.open(
            os.path.join(str(PLUGIN_ROOT), "social", "engine.py"), encoding="utf-8"
        ).read()

    def test_log_says_how_many_were_replies(self) -> None:
        src = self._src()
        self.assertIn("其中接群友的话", src)
        self.assertNotIn("今天群聊已经主动说了", src)

    def test_flow_reply_passes_reply_flag(self) -> None:
        src = self._src()
        idx = src.index("reply=True")
        window = src[max(0, idx - 400):idx + 80]
        self.assertIn("note_group_send", window)

    def test_missing_counter_does_not_break_gate(self) -> None:
        """替身/旧状态没有 group_reply_count 时，闸不能炸。"""
        from social import engine as engine_mod

        class _FakeState:
            def group_send_count(self, bid, day):
                return 12

        eng = object.__new__(engine_mod.SocialEngine)
        eng.cfg = types.SimpleNamespace(group_daily_total_cap=12)
        eng.state = _FakeState()
        self.assertTrue(eng._group_daily_exhausted(BID, DAY))


if __name__ == "__main__":
    unittest.main()
