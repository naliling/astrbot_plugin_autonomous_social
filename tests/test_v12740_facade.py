"""契约 v2 只读门面（社交侧）。

这一层要守住的是**失败安全**：拿不到门面时，一切行为必须与 1.27.38 逐字一致。
门面是「更好的一条路」，不是「必须走的路」——走不通就退回读 state.json。

v2.0.0（P2-09）追加：缓存命中前的存活探测 + Core 热重载后的自动重发现。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest


def _install_stub() -> None:
    """自己装 astrbot stub：单独跑本文件时不能指望别人先装过。"""
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v12740_facade_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()

from social import core_facade  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402


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


# ─── P2-09：门面缓存的生命周期（Core 热重载后必须自己恢复）──────────
#
# 审核实测（degrade_check 场景 C）：Core 重载后旧门面绑的是已经 stop() 的
# RoleManager，role_ids() 永远为空，但 social 命中缓存后直接返回旧对象、状态页仍写
# 「已连接」，brief/clock 全空。这里钉住：缓存命中前要探活、失效要能自动重发现。


class _CurrentFacade(_FakeFacade):
    """Core 3.0.0 风格：is_current() 按插件实例代次判活。"""

    def __init__(self, current: bool = True) -> None:
        super().__init__()
        self.current = current
        self.probe_calls = 0

    def is_current(self) -> bool:
        self.probe_calls += 1
        return self.current

    def role_ids(self):
        return ["b1"] if self.current else []


class _RoleIdsFacade(_FakeFacade):
    """Core 2.28.x 风格：没有 is_current，只有 role_ids()。"""

    def __init__(self, roles=("b1",)) -> None:
        super().__init__()
        self.roles = list(roles)
        self.probe_calls = 0

    def role_ids(self):
        self.probe_calls += 1
        return list(self.roles)


class _OldFacadeFull(_FakeFacade):
    """更老的门面：两个存活接口都没有 → 无法判定，按可用处理。"""


def _factory_ctx(facades):
    """造一个「每次查表返回下一个门面」的宿主注册表；返回 (ctx, calls)。"""
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        index = min(calls["n"] - 1, len(facades) - 1)
        return facades[index] if facades else None

    _install_module("fake_core_p209", factory)
    return _Ctx(_meta("fake_core_p209")), calls


class FacadeLivenessProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        core_facade.reset()

    def tearDown(self) -> None:
        core_facade.reset()

    def test_probe_prefers_is_current(self):
        live = _CurrentFacade(current=True)
        self.assertTrue(core_facade.probe(live))
        self.assertEqual(live.probe_calls, 1)
        dead = _CurrentFacade(current=False)
        self.assertFalse(core_facade.probe(dead))
        self.assertEqual(core_facade.status()["alive"], False)

    def test_probe_falls_back_to_role_ids(self):
        self.assertTrue(core_facade.probe(_RoleIdsFacade(roles=["b1"])))
        self.assertFalse(
            core_facade.probe(_RoleIdsFacade(roles=[])),
            "Core 重载后旧 RoleManager 的 role_ids() 是空的，必须判死",
        )

    def test_unknown_old_facade_is_assumed_alive(self):
        """没有 is_current / role_ids 的旧门面无法判定：保持旧行为（可用），不能误杀。"""
        self.assertTrue(core_facade.probe(_OldFacadeFull()))
        self.assertTrue(core_facade.probe(_FakeFacade()))

    def test_none_is_dead(self):
        self.assertFalse(core_facade.probe(None))

    def test_alive_alias_exists(self):
        """引擎侧若写成 alive(facade) 也能工作（接口名不唯一）。"""
        self.assertTrue(callable(getattr(core_facade, "alive", None)))
        self.assertTrue(core_facade.alive(_CurrentFacade(current=True)))

    def test_status_keeps_old_keys_and_adds_probe_fields(self):
        info = core_facade.status()
        for old_key in ("connected", "api_version", "capabilities", "reason", "resolved"):
            self.assertIn(old_key, info, "P2-09 改动不能破坏状态页既有键")
        for new_key in ("alive", "probed_at", "dead_count"):
            self.assertIn(new_key, info)


class FacadeReloadRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        core_facade.reset()

    def tearDown(self) -> None:
        core_facade.reset()

    @staticmethod
    def _expire_probe() -> None:
        """把「上次探测时刻」拨到探测间隔之前，模拟 60s 后的一次调用。"""
        import time as _time

        core_facade._CACHE["checked_at"] = (
            _time.monotonic() - core_facade.PROBE_INTERVAL_SECONDS - 1.0
        )

    def test_cached_dead_facade_is_replaced_after_core_reload(self):
        old = _CurrentFacade(current=False)  # 被 remember 时不探测，先把旧对象缓存上
        new = _CurrentFacade(current=True)
        ctx, calls = _factory_ctx([old, new])
        self.assertIs(core_facade.find_core_facade(ctx, force=True), old)
        self._expire_probe()
        recovered = core_facade.find_core_facade(ctx)
        self.assertIs(recovered, new, "Core 重载后旧门面没有被替换，门面路径永久失效")
        self.assertEqual(calls["n"], 2, "失效后没有真的重新查注册表")
        self.assertEqual(core_facade.status()["dead_count"], 1)

    def test_dead_facade_without_a_replacement_returns_none(self):
        old = _CurrentFacade(current=False)
        ctx, calls = _factory_ctx([old, None])
        self.assertIs(core_facade.find_core_facade(ctx, force=True), old)
        self._expire_probe()
        self.assertIsNone(
            core_facade.find_core_facade(ctx),
            "Core 正在重载（门面还没就绪）时应当返回 None 并允许下一轮重试",
        )
        self.assertEqual(calls["n"], 2)
        self.assertFalse(core_facade.status()["connected"])

    def test_role_ids_empty_marks_the_cached_facade_dead(self):
        old = _RoleIdsFacade(roles=["b1"])
        new = _RoleIdsFacade(roles=["b1"])
        ctx, calls = _factory_ctx([old, new])
        self.assertIs(core_facade.find_core_facade(ctx, force=True), old)
        old.roles = []  # Core 重载：旧 RoleManager 清空
        self._expire_probe()
        self.assertIs(core_facade.find_core_facade(ctx), new)

    def test_probe_is_rate_limited_to_the_interval(self):
        live = _CurrentFacade(current=True)
        ctx, calls = _factory_ctx([live])
        self.assertIs(core_facade.find_core_facade(ctx, force=True), live)
        self.assertIs(core_facade.find_core_facade(ctx), live)
        self.assertIs(core_facade.find_core_facade(ctx), live)
        self.assertEqual(calls["n"], 1, "缓存有效期内不该重复查注册表")
        self.assertEqual(live.probe_calls, 0, "缓存有效期内不该每轮都探测")

    def test_force_rediscovers_even_when_the_cache_is_valid(self):
        first = _CurrentFacade()
        second = _CurrentFacade()
        ctx, calls = _factory_ctx([first, second])
        self.assertIs(core_facade.find_core_facade(ctx, force=True), first)
        self.assertIs(core_facade.find_core_facade(ctx, force=True), second)
        self.assertEqual(calls["n"], 2, "force=True 没有强制重查")

    def test_reset_clears_the_probe_state(self):
        live = _CurrentFacade(current=True)
        core_facade._CACHE.update(
            {"facade": live, "resolved": True, "checked_at": 123.0, "alive": True,
             "dead_count": 2}
        )
        core_facade.reset()
        info = core_facade.status()
        self.assertFalse(info["connected"])
        self.assertEqual(info["dead_count"], 0)
        self.assertEqual(info["probed_at"], 0.0)


if __name__ == "__main__":
    unittest.main()
