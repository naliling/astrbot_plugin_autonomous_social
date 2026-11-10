"""v2.2.9：物种到底从哪儿取（真机缺口回归）。

## 为什么

v2.2.7 加了物种闸（蹄类不许说「手」、出口把「手」改「蹄子」），离线用例全绿。
但真机上这些闸**一次都没触发过**：`_species_of()` 只读 `users[*]["_brief"]`，
而那个键在生产环境的 state.json 里**连键都不存在**（线上 499 个用户，0 个有
`_brief`）。于是：

* `_check_species` 永远拿到空物种 → 永远放行；
* `local_materials(species=...)` 永远不换「蹄子」；
* `on_decorating_result` 出口改写永远不改。

线上证据：茉心（天马）21:08 回「指尖还带着刚才洗手落下的凉意」原样发出去。

物种其实一直拿得到：Core 门面 `brief["her"]["species"]`，线上实测返回「天马」
（humanoid_core 的 `roles/<bid>/self/species/label`，confidence 0.92）。提示词那条
路（`_brief_context_lines`）用的就是它——只有验收/素材/出口这三处读了死路径。

## 本文件盯的

* `_species_of` 从门面 brief 取物种（不是 `users[*]["_brief"]`）；
* 取到后按 bot 缓存，不每条消息都问一次门面；
* 门面拿不到时仍能回退旧路径，两边都没有就返回空串（不许抛）；
* 端到端：拿到物种后出口改写真的把「手」改「蹄子」、验收层真的拦；
* `_repair_fields` 把「5.0→5」算归一化，不再冒充「按默认值重置」。
"""

from __future__ import annotations

import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v229_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import social.engine as engine_mod  # noqa: E402
import social.verify as verify_mod  # noqa: E402
from social.state import SocialState  # noqa: E402

SocialEngine = engine_mod.SocialEngine


def _state() -> SocialState:
    tmp = tempfile.mkdtemp(prefix="aso_v229_")
    return SocialState(str(Path(tmp) / "state.json"))


def _bare_engine() -> object:
    """不跑 `__init__`：这些方法只用到 `state` 和门面。"""
    eng = object.__new__(SocialEngine)
    eng.state = _state()
    return eng


def _brief(species: str):
    return mock.patch.object(
        SocialEngine,
        "_core_brief",
        lambda self, bid, uid="", **_kw: {"her": {"species": species}},
    )


class SpeciesSourceTest(unittest.TestCase):
    def test_reads_species_from_facade_brief(self):
        eng = _bare_engine()
        with _brief("天马"):
            self.assertEqual(eng._species_of("b1"), "天马")

    def test_caches_per_bot(self):
        eng = _bare_engine()
        calls = {"n": 0}

        def _fake(self, bid, uid="", **_kw):
            calls["n"] += 1
            return {"her": {"species": "天马"}}

        with mock.patch.object(SocialEngine, "_core_brief", _fake):
            self.assertEqual(eng._species_of("b1"), "天马")
            self.assertEqual(eng._species_of("b1"), "天马")
        self.assertEqual(calls["n"], 1, "同一个 bot 十秒内不该反复问门面")

    def test_falls_back_to_user_brief(self):
        eng = _bare_engine()
        eng.state.bot("b1")["users"]["u1"] = {"_brief": {"her": {"species": "独角兽"}}}
        with mock.patch.object(SocialEngine, "_core_brief", lambda *a, **k: None):
            self.assertEqual(eng._species_of("b1"), "独角兽")

    def test_empty_when_nothing(self):
        eng = _bare_engine()
        with mock.patch.object(SocialEngine, "_core_brief", lambda *a, **k: None):
            self.assertEqual(eng._species_of("b1"), "")

    def test_facade_exception_is_safe(self):
        eng = _bare_engine()

        def _boom(*_a, **_k):
            raise RuntimeError("facade down")

        with mock.patch.object(SocialEngine, "_core_brief", _boom):
            self.assertEqual(eng._species_of("b1"), "")


class SpeciesGateEndToEndTest(unittest.TestCase):
    def test_tidy_outgoing_rewrites_hand_for_hoof(self):
        eng = _bare_engine()
        with _brief("天马"):
            out = eng.tidy_outgoing("我刚醒手还有点僵", "b1")
        self.assertIn("蹄", out)
        self.assertNotIn("手", out)

    def test_verify_blocks_hand_for_hoof(self):
        ok, reason = verify_mod.verify_message("我刚醒手还有点僵", species="天马")
        self.assertFalse(ok, f"蹄类说「手」应该被拒，实际放行：{reason!r}")

    def test_local_materials_no_hand_for_hoof(self):
        import social.groupflow as groupflow_mod

        lines = groupflow_mod.local_materials(1_800_000_000.0, count=4, species="天马")
        joined = "".join(lines)
        self.assertNotIn("手还是湿的", joined)
        self.assertNotIn("手", joined)


class RepairCountsTest(unittest.TestCase):
    def test_float_int_value_is_normalized_not_reset(self):
        rec = {"rhythm_samples": 5.0}
        got = SocialState._repair_fields(rec, {"rhythm_samples": 0})
        self.assertEqual(got, (0, 1))
        self.assertEqual(rec["rhythm_samples"], 5)
        self.assertIsInstance(rec["rhythm_samples"], int)

    def test_wrong_type_is_reset(self):
        rec = {"rhythm_samples": "abc"}
        got = SocialState._repair_fields(rec, {"rhythm_samples": 0})
        self.assertEqual(got, (1, 0))
        self.assertEqual(rec["rhythm_samples"], 0)


if __name__ == "__main__":
    unittest.main()
