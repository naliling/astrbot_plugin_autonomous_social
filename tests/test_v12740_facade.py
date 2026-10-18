"""契约 v2 只读门面（社交侧）。

这一层要守住的是**失败安全**：拿不到门面时，一切行为必须与 1.27.38 逐字一致。
门面是「更好的一条路」，不是「必须走的路」——走不通就退回读 state.json。
"""

from __future__ import annotations

import sys
import types
import unittest

from social import core_facade
from social.config import SocialConfig
from social.engine import SocialEngine


class _FakeFacade:
    def __init__(self, offset: int = 300) -> None:
        self.offset = offset
        self.calls: list = []

    def api_version(self):
        return 2

    def capabilities(self):
        return ["brief", "clock", "events", "fingerprint"]

    def clock(self, role_id):
        self.calls.append(("clock", role_id))
        return {"utc_offset_minutes": self.offset, "epoch": 1.0}

    def brief(self, role_id, user_id="", **kw):
        self.calls.append(("brief", role_id, kw.get("task")))
        return {"v": 2, "task": kw.get("task") or "chat", "ongoing": []}

    def state_fingerprint(self, role_id):
        return "fp"


class _OldFacade:
    """缺方法的门面：当作「版本太旧」，必须退回读文件。"""

    def api_version(self):
        return 2

    def clock(self, role_id):
        return {}


def _install_module(name: str, factory) -> types.ModuleType:
    mod = types.ModuleType(name)
    if factory is not None:
        mod.get_core_facade = factory
    sys.modules[name] = mod
    return mod


def _meta(module_name: str, *, name="astrbot_plugin_humanoid_core", activated=True):
    star_cls = type("FakeStar", (), {})
    star_cls.__module__ = module_name
    return types.SimpleNamespace(name=name, activated=activated, star_cls=star_cls)


class _Ctx:
    def __init__(self, meta=None) -> None:
        self._meta = meta

    def get_registered_star(self, pid):
        return self._meta

    def get_all_stars(self):
        return [self._meta] if self._meta else []


class FindFacadeTest(unittest.TestCase):
    def setUp(self) -> None:
        core_facade.reset()

    def tearDown(self) -> None:
        core_facade.reset()

    def test_no_context_is_none(self):
        self.assertIsNone(core_facade.find_core_facade(None))
        self.assertIn("context", core_facade.status()["reason"])

    def test_no_registry_api_is_none(self):
        self.assertIsNone(core_facade.find_core_facade(object()))
        self.assertIn("get_all_stars", core_facade.status()["reason"])

    def test_core_without_factory_is_treated_as_old(self):
        _install_module("fake_core_old", None)
        ctx = _Ctx(_meta("fake_core_old"))
        self.assertIsNone(core_facade.find_core_facade(ctx, force=True))
        self.assertIn("旧版", core_facade.status()["reason"])

    def test_facade_is_found_through_the_registry(self):
        _install_module("fake_core_new", lambda: _FakeFacade())
        ctx = _Ctx(_meta("fake_core_new"))
        facade = core_facade.find_core_facade(ctx, force=True)
        self.assertIsNotNone(facade)
        self.assertEqual(core_facade.status()["api_version"], 2)
        self.assertTrue(core_facade.status()["connected"])

    def test_other_plugin_is_ignored(self):
        _install_module("fake_other", lambda: _FakeFacade())
        ctx = _Ctx(_meta("fake_other", name="astrbot_plugin_memory_companion"))
        self.assertIsNone(core_facade.find_core_facade(ctx, force=True))

    def test_inactive_plugin_is_reported(self):
        _install_module("fake_core_inactive", lambda: _FakeFacade())
        ctx = _Ctx(_meta("fake_core_inactive", activated=False))
        self.assertIsNone(core_facade.find_core_facade(ctx, force=True))
        self.assertIn("未激活", core_facade.status()["reason"])

    def test_facade_missing_methods_is_rejected(self):
        _install_module("fake_core_old_facade", lambda: _OldFacade())
        ctx = _Ctx(_meta("fake_core_old_facade"))
        self.assertIsNone(core_facade.find_core_facade(ctx, force=True))
        self.assertIn("缺方法", core_facade.status()["reason"])

    def test_factory_raising_is_swallowed(self):
        def boom():
            raise RuntimeError("boom")

        _install_module("fake_core_boom", boom)
        ctx = _Ctx(_meta("fake_core_boom"))
        self.assertIsNone(core_facade.find_core_facade(ctx, force=True))
        self.assertIn("抛异常", core_facade.status()["reason"])

    def test_negative_cache_avoids_repeated_lookup(self):
        calls = []

        def factory():
            calls.append(1)
            return _FakeFacade()

        _install_module("fake_core_cache", factory)
        ctx = _Ctx(_meta("fake_core_cache"))
        self.assertIsNotNone(core_facade.find_core_facade(ctx, force=True))
        # 命中后不再查表
        core_facade.find_core_facade(ctx)
        self.assertEqual(len(calls), 1)


def _engine(cfg: SocialConfig, ctx=None, facade=None) -> SocialEngine:
    """绕开完整构造：这一层只关心门面相关的几个属性。"""
    eng = object.__new__(SocialEngine)
    eng.cfg = cfg
    eng.context = ctx
    eng._facade = facade
    eng._facade_tried = True
    eng._city_offset = {}
    eng.log = lambda *a, **k: None
    return eng


class EngineFacadeSwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        core_facade.reset()

    def tearDown(self) -> None:
        core_facade.reset()

    def test_off_mode_never_touches_the_facade(self):
        _install_module("fake_core_off", lambda: _FakeFacade())
        ctx = _Ctx(_meta("fake_core_off"))
        eng = _engine(SocialConfig(use_core_facade="off"), ctx)
        self.assertIsNone(eng._core_facade())

    def test_auto_mode_falls_back_silently(self):
        eng = _engine(SocialConfig(use_core_facade="auto"), _Ctx(None))
        self.assertIsNone(eng._core_facade())

    def test_clock_prefers_facade(self):
        facade = _FakeFacade(offset=300)
        eng = _engine(SocialConfig(use_core_facade="auto"), _Ctx(None), facade)
        # 契约快照里给的是 480，门面给 300：应当采信门面（现算的）
        eng._remember_clock("bot1", {"clock_offset_minutes": 480})
        self.assertEqual(eng._city_offset["bot1"], 300)
        self.assertIn(("clock", "bot1"), facade.calls)

    def test_clock_falls_back_to_contract_without_facade(self):
        eng = _engine(SocialConfig(use_core_facade="off"), _Ctx(None))
        eng._remember_clock("bot1", {"clock_offset_minutes": 480})
        self.assertEqual(eng._city_offset["bot1"], 480)

    def test_status_line_reports_off_and_fallback(self):
        eng = _engine(SocialConfig(use_core_facade="off"))
        self.assertIn("已关闭", eng._facade_status_text())
        eng2 = _engine(SocialConfig(use_core_facade="auto"))
        self.assertIn("state.json", eng2._facade_status_text())

    def test_status_line_reports_connected(self):
        _install_module("fake_core_status", lambda: _FakeFacade())
        ctx = _Ctx(_meta("fake_core_status"))
        facade = core_facade.find_core_facade(ctx, force=True)
        eng = _engine(SocialConfig(use_core_facade="auto"), ctx, facade)
        text = eng._facade_status_text()
        self.assertIn("已连接", text)
        self.assertIn("v2", text)


if __name__ == "__main__":
    unittest.main()
