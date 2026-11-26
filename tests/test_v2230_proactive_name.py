# -*- coding: utf-8 -*-
"""可选称呼应自然使用，不按消息频率强制或统计。"""
import io
import os
import sys
import types
import unittest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

if "astrbot" not in sys.modules:
    _a = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None,
    )
    _ev = types.ModuleType("astrbot.api.event")
    _ev.MessageChain = object
    _a.api = _api
    sys.modules["astrbot"] = _a
    sys.modules["astrbot.api"] = _api
    sys.modules["astrbot.api.event"] = _ev

import social.generator as gen  # noqa: E402
from social.engine import SocialEngine  # noqa: E402


class BondLineWordingTest(unittest.TestCase):
    def test_local_name_keeps_the_instruction(self):
        text = gen._bond_line({"_call_name": "游鱼", "_name_source": "local"})
        self.assertIn("可选称呼是「游鱼」", text)
        self.assertIn("可选称呼是", text)
        self.assertIn("仅在自然时使用", text)

    def test_display_name_same(self):
        text = gen._bond_line({
            "_display_name": "爱筝挽起·冲击波冠军",
            "_call_name": "爱筝挽起·冲击波冠军",
            "_name_source": "display_name",
        })
        self.assertIn("可选称呼是「爱筝挽起」", text)
        self.assertIn("可选称呼是", text)
        self.assertIn("仅在自然时使用", text)

    def test_nickname_still_says_the_habit(self):
        text = gen._bond_line({"_nickname": "小明", "_name_source": "nickname"})
        self.assertIn("可选称呼是「小明」", text)
        self.assertIn("可选称呼是", text)


class WiringTest(unittest.TestCase):
    def test_name_usage_is_optional_without_frequency_metrics(self):
        with io.open(os.path.join(PLUGIN_ROOT, "social", "engine.py"), encoding="utf-8") as stream:
            src = stream.read()
        self.assertNotIn("_note_proactive_name", src)
        self.assertNotIn("_pro_name_total", src)
        self.assertFalse(hasattr(SocialEngine, "_note_proactive_name"))


if __name__ == "__main__":
    unittest.main()
