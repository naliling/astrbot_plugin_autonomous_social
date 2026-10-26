"""v1.28.11：主动消息接上 Core 的 intimacy / want_line。

背景：Core 门面 brief 的 user 块里本来就带 intimacy（在不在场景、想不想、
亲近欲、initiative），社交侧之前把它整块丢掉了；「想要了」通道也只读
state.json，从不问门面。后果：Core 算好的 want_line 只出现在状态页探测行里，
从来没进过主动消息。

这里钉四件事：
1. 门面 intimacy 的容错封装（拿不到返回 {}，不抛）；
2. brief 合并把 intimacy 挂到生成目标上；
3. 生成侧只把它压成一句事实（不写指令）；
4. 场景中不把 want_line 当由头（不该打断那一场）。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest

if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("social_intimacy_test")
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api})

from social import core_facade  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social.generator import _intimacy_fact_line  # noqa: E402


class FacadeIntimacyWrapperTest(unittest.TestCase):
    def test_missing_facade(self):
        self.assertEqual(core_facade.intimacy(None, "b", "u"), {})

    def test_wrong_shape(self):
        class Bad:
            def intimacy(self, *_a):
                return "坏"

        self.assertEqual(core_facade.intimacy(Bad(), "b", "u"), {})

    def test_exception(self):
        class Boom:
            def intimacy(self, *_a):
                raise RuntimeError("boom")

        self.assertEqual(core_facade.intimacy(Boom(), "b", "u"), {})

    def test_dict_passed_through(self):
        class Fake:
            def intimacy(self, role_id, user_id):
                return {"want_line": "她这会儿有点想他", "in_scene": False}

        data = core_facade.intimacy(Fake(), "b", "u")
        self.assertEqual(data["want_line"], "她这会儿有点想他")


class BriefMergeIntimacyTest(unittest.TestCase):
    def test_merged_into_generation_target(self):
        target: dict = {}
        block = {"intimacy": {"in_scene": False, "want_line": "想他", "initiative": 0.8}}
        SocialEngine._apply_core_brief(target, {"user": block}, None, {})
        self.assertEqual(target["_intimacy"]["want_line"], "想他")

    def test_absent_does_not_create_key(self):
        target: dict = {}
        SocialEngine._apply_core_brief(target, {"user": {}}, None, {})
        self.assertNotIn("_intimacy", target)


class IntimacyFactLineTest(unittest.TestCase):
    def test_no_data(self):
        self.assertEqual(_intimacy_fact_line({}), "")
        self.assertEqual(_intimacy_fact_line({"user": {}}), "")

    def test_want_line(self):
        line = _intimacy_fact_line({"_intimacy": {"want_line": "她这会儿有点想他，忍着没说"}})
        self.assertIn("想他", line)

    def test_in_scene(self):
        line = _intimacy_fact_line({"_intimacy": {"in_scene": True, "satisfy": 60.0}})
        self.assertIn("亲密里", line)
        self.assertIn("60", line)

    def test_aftercare(self):
        line = _intimacy_fact_line({"_intimacy": {"aftercare_left": 300.0}})
        self.assertIn("余韵", line)

    def test_high_initiative(self):
        line = _intimacy_fact_line({"_intimacy": {"initiative": 0.9}})
        self.assertIn("主动", line)

    def test_low_initiative_silent(self):
        self.assertEqual(_intimacy_fact_line({"_intimacy": {"initiative": 0.1}}), "")

    def test_priority_pick_want_line_over_intimacy_dict(self):
        line = _intimacy_fact_line({"_want_line": "她这会儿想他", "_intimacy": {}})
        self.assertEqual(line, "")


class WantLineGatingTest(unittest.TestCase):
    def _obj(self, facade):
        obj = object.__new__(SocialEngine)
        obj._core_facade = lambda: facade
        return obj

    def test_returns_want_line(self):
        class Fake:
            def intimacy(self, role_id, user_id):
                return {"want_line": "她这会儿有点想他", "in_scene": False}

        self.assertEqual(self._obj(Fake())._ccb_want_line("b", "u"), "她这会儿有点想他")

    def test_in_scene_returns_empty(self):
        class Fake:
            def intimacy(self, role_id, user_id):
                return {"want_line": "不想被打断", "in_scene": True}

        self.assertEqual(self._obj(Fake())._ccb_want_line("b", "u"), "")

    def test_no_facade(self):
        self.assertEqual(self._obj(None)._ccb_want_line("b", "u"), "")


if __name__ == "__main__":
    unittest.main()
