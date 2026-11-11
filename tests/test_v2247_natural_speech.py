# -*- coding: utf-8 -*-
"""v2.2.47：让她「想说就说」，别让口头禅和重复开头替她开口。

用户原话：「1的一天只能主动12条不行 要么没限制要么把这个删掉 我要的是那个人的主动
人是想说就说的」+「2的解决掉提升质量 3同样要提升质量」。
"""
import io
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub():
    if "astrbot" in sys.modules:
        return
    import logging

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social import config as config_mod  # noqa: E402
from social import engine as engine_mod  # noqa: E402


class UnlimitedGroupCapTest(unittest.TestCase):
    """一天说几条由她自己决定（0 = 不限）。"""

    def test_default_is_unlimited(self):
        self.assertEqual(config_mod.GROUP_DAILY_TOTAL_CAP_DEFAULT, 0)

    def test_zero_means_no_cap_in_engine(self):
        eng = engine_mod.SocialEngine.__new__(engine_mod.SocialEngine)
        state = types.SimpleNamespace(group_send_count=lambda bid, day: 999)
        eng.state = state
        eng.cfg = types.SimpleNamespace(group_daily_total_cap=0)
        self.assertFalse(engine_mod.SocialEngine._group_daily_exhausted(eng, "b1", "2026-11-11"))

    def test_positive_cap_still_works(self):
        eng = engine_mod.SocialEngine.__new__(engine_mod.SocialEngine)
        state = types.SimpleNamespace(group_send_count=lambda bid, day: 5)
        eng.state = state
        eng.cfg = types.SimpleNamespace(group_daily_total_cap=5)
        self.assertTrue(engine_mod.SocialEngine._group_daily_exhausted(eng, "b1", "2026-11-11"))


class FlowOpeningRotationTest(unittest.TestCase):
    """心流也要有开头轮换（v2.2.7 只接了破冰/起话头）。"""

    def test_flow_rejects_repeated_opening(self):
        openings = ["刚睡醒", "刚把茶喝完", "刚趴窗边看外头"]
        why = engine_mod._flow_quality_issue(
            ["刚用蹄子夹着杯子泡茶，洒了半桌"], openings
        )
        self.assertTrue(why)
        self.assertIn("刚", why)

    def test_flow_allows_other_opening(self):
        openings = ["刚睡醒", "刚把茶喝完", "刚趴窗边看外头"]
        why = engine_mod._flow_quality_issue(
            ["这壶茶泡得正好，我先喝一口"], openings
        )
        self.assertEqual(why, "")

    def test_flow_without_openings_still_checks_rest(self):
        why = engine_mod._flow_quality_issue(["你们说得对"], None)
        self.assertTrue(why)  # 纯附和仍被拦


class FillerHeadAtSendGateTest(unittest.TestCase):
    """口头禅在发送口摘掉——主动消息与群消息从来不过 tidy_outgoing。"""

    def _eng(self, recent):
        eng = engine_mod.SocialEngine.__new__(engine_mod.SocialEngine)
        eng.recent_spoken_texts = lambda count=6: list(recent)
        return eng

    def test_strips_when_overused(self):
        eng = self._eng(["唔……今天累", "唔 急什么", "唔哈 突然"])
        got = engine_mod._polish_filler_head(eng, "唔……最喜欢你了")
        self.assertEqual(got, "最喜欢你了")

    def test_keeps_when_rare(self):
        eng = self._eng(["今天累", "急什么", "突然用力"])
        got = engine_mod._polish_filler_head(eng, "唔……最喜欢你了")
        self.assertEqual(got, "唔……最喜欢你了")

    def test_keeps_action_prefix(self):
        eng = self._eng(["唔……今天累", "唔 急什么", "唔哈 突然"])
        got = engine_mod._polish_filler_head(eng, "（蹭了蹭）唔……我知道了")
        self.assertIn("我知道了", got)

    def test_never_empties(self):
        eng = self._eng(["唔……今天累", "唔 急什么", "唔哈 突然"])
        self.assertTrue(engine_mod._polish_filler_head(eng, "唔").strip())

    def test_stub_safe(self):
        got = engine_mod._polish_filler_head(types.SimpleNamespace(), "唔……你好")
        self.assertEqual(got, "唔……你好")

    def test_wired_into_send_gate(self):
        src = engine_mod.__file__
        raw = io.open(src, encoding="utf-8", newline="").read()
        self.assertIn("_polish_filler_head(self, text)", raw)
        idx = raw.index("_polish_filler_head(self, text)")
        self.assertLess(idx, raw.index("for attempt in range(1 + max(0, int(SEND_RETRY_COUNT)))"))


if __name__ == "__main__":
    unittest.main()
