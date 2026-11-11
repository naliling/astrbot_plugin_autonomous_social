# -*- coding: utf-8 -*-
"""v2.2.30：主动消息要叫一声对方的名字，且提示词不许自相矛盾。

线上实测（`scripts/audit_name_loose.py`）：140 个有名字且有主动消息的对话方、
664 条主动消息里，含完整短名 103 条（15.5%），含名字前 3 字 3 条、前 2 字 0 条
——宽松上限 16.0%。而提示词里写着「开口要叫一声TA的称呼」，紧挨着的上一句却是
「TA 的名片名是「X」，你可以偶尔这么叫」。模型取了松的那句。
"""
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
        self.assertIn("TA 的名片名是「游鱼」", text)
        self.assertIn("开口要叫一声TA的称呼", text)
        self.assertNotIn("偶尔这么叫", text)

    def test_display_name_same(self):
        text = gen._bond_line({
            "_display_name": "爱筝挽起·冲击波冠军",
            "_call_name": "爱筝挽起·冲击波冠军",
            "_name_source": "display_name",
        })
        self.assertIn("TA 的名片名是「爱筝挽起」", text)
        self.assertIn("开口要叫一声TA的称呼", text)
        self.assertNotIn("偶尔这么叫", text)

    def test_nickname_still_says_the_habit(self):
        text = gen._bond_line({"_nickname": "小明", "_name_source": "nickname"})
        self.assertIn("你一直叫TA「小明」", text)
        self.assertIn("开口要叫一声TA的称呼", text)


class _FakeState:
    def __init__(self, name):
        self._name = name

    def user(self, bid, uid):
        return {"name": self._name}


class _FakeEngine:
    def __init__(self, name):
        self.state = _FakeState(name)


class ProactiveNameCounterTest(unittest.TestCase):
    def test_miss_counted(self):
        eng = _FakeEngine("小小心情爱娜莉灵·(=^▽^=)")
        SocialEngine._note_proactive_name(eng, "b1", "u1", "刚睡醒，外头冷")
        self.assertEqual(eng._pro_name_total, 1)
        self.assertEqual(eng._pro_name_miss, 1)

    def test_hit_not_counted(self):
        eng = _FakeEngine("游鱼")
        SocialEngine._note_proactive_name(eng, "b1", "u1", "游鱼，刚睡醒")
        self.assertEqual(eng._pro_name_total, 1)
        self.assertEqual(getattr(eng, "_pro_name_miss", 0), 0)

    def test_no_name_is_ignored(self):
        eng = _FakeEngine("")
        SocialEngine._note_proactive_name(eng, "b1", "u1", "刚睡醒")
        self.assertEqual(getattr(eng, "_pro_name_total", 0), 0)

    def test_never_raises(self):
        class Boom:
            def user(self, bid, uid):
                raise RuntimeError("boom")
        eng = types.SimpleNamespace(state=Boom())
        SocialEngine._note_proactive_name(eng, "b1", "u1", "x")


class WiringTest(unittest.TestCase):
    def test_call_site_present(self):
        src = io.open(os.path.join(PLUGIN_ROOT, "social", "engine.py"), encoding="utf-8").read()
        self.assertIn("_note_proactive_name(self, bid, uid, segments[0])", src)
        self.assertIn("from .textutil import short_call_name", src)


if __name__ == "__main__":
    unittest.main()
