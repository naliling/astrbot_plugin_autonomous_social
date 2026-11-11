"""v2.2.38：群消息落账时记住「这句用的是哪条素材」。

私聊主动消息一直有 `proactive_log[].about` 记由头；群这条链路原来只落正文，
于是「她为什么突然说这个」事后答不上来（Core 的 state 只存当前状态、不存历史）。
这里只补可追溯性，不改任何输出。
"""

from __future__ import annotations

import io
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE

if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("aso_v2238_stub")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social.state import SocialState  # noqa: E402

NOW = 1_800_000_000.0
BID, GID = "bot1", "group1"


def _state() -> SocialState:
    tmp = tempfile.mkdtemp(prefix="aso_v2238_")
    return SocialState(str(Path(tmp) / "state.json"))


def _src(name: str) -> str:
    return io.open(
        Path(PLUGIN_ROOT) / "social" / name, encoding="utf-8", errors="replace"
    ).read()


class AnnotateTest(unittest.TestCase):
    def test_sets_sources_on_last_self_sample(self):
        st = _state()
        st.record_group_self_text(BID, GID, "刚用蹄子夹着杯子泡了茶", NOW)
        ok = st.annotate_last_group_sample(BID, GID, ["用蹄子夹着杯子泡茶，洒了半桌"])
        self.assertTrue(ok)
        sample = st.group(BID, GID)["samples"][-1]
        self.assertEqual(sample["sources"], ["用蹄子夹着杯子泡茶，洒了半桌"])

    def test_skips_other_peoples_samples(self):
        st = _state()
        st.record_group_self_text(BID, GID, "我先说一句", NOW)
        st.record_group_message(BID, GID, "umo://g1", GID, "群友", "群友插一句", False, NOW + 1)
        self.assertTrue(st.annotate_last_group_sample(BID, GID, ["素材A"]))
        samples = st.group(BID, GID)["samples"]
        self.assertEqual(samples[-1].get("sources"), None)  # 群友那条不动
        self.assertEqual(samples[-2]["sources"], ["素材A"])  # 她自己那条被补上

    def test_no_self_sample_returns_false(self):
        st = _state()
        st.record_group_message(BID, GID, "umo://g1", GID, "群友", "只有群友说话", False, NOW)
        self.assertFalse(st.annotate_last_group_sample(BID, GID, ["素材A"]))

    def test_empty_sources_is_noop(self):
        st = _state()
        st.record_group_self_text(BID, GID, "她说过一句", NOW)
        self.assertFalse(st.annotate_last_group_sample(BID, GID, []))
        self.assertFalse(st.annotate_last_group_sample(BID, GID, ["   "]))
        self.assertIsNone(st.group(BID, GID)["samples"][-1].get("sources"))

    def test_caps_two_items_and_truncates(self):
        st = _state()
        st.record_group_self_text(BID, GID, "她说的话", NOW)
        st.annotate_last_group_sample(BID, GID, ["甲" * 200, "乙", "丙", "丁"])
        sources = st.group(BID, GID)["samples"][-1]["sources"]
        self.assertEqual(len(sources), 2)
        self.assertEqual(len(sources[0]), 60)

    def test_marks_dirty(self):
        st = _state()
        st.record_group_self_text(BID, GID, "她说的话", NOW)
        st.dirty = False if hasattr(st, "dirty") else None
        before = getattr(st, "_dirty", None)
        st.annotate_last_group_sample(BID, GID, ["素材A"])
        after = getattr(st, "_dirty", None)
        if before is not None:
            self.assertTrue(after)


class WiringTest(unittest.TestCase):
    def test_engine_calls_it_after_life_accounting(self):
        src = _src("engine.py")
        idx_life = src.index("self.state.note_life_used(")
        idx_ann = src.index("annotate_last_group_sample")
        self.assertGreater(idx_ann, idx_life)
        self.assertLess(idx_ann - idx_life, 1200)

    def test_state_has_the_method(self):
        self.assertTrue(callable(getattr(SocialState, "annotate_last_group_sample", None)))


if __name__ == "__main__":
    unittest.main()
