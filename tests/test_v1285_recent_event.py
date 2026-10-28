"""v1.28.5：刚刚发生的事进得了主动开口的由头。

## 为什么

原来的派生由头全看她**在做什么**（梦、doing、done、next、wants、ongoing），
于是她被骂了一句之后主动找你，说的还是天气和节日——她心里正过着一件事，
插件却不知道。

## 修法

Core 2.29 起契约多一个 `event` 块（`{phrase, age_seconds}`），表示「刚刚发生的
事够重」。这里三处接上：

1. `core_bridge._build_self_view` 读它（顶层 + `day` 各一份，同一个对象）；
2. `anchors._derive_from_state` 拿它派由头，kind 记 `KIND_RECENT`；
3. 档位与 TTL：和梦同级（rank 2、TTL 2h）——它和梦一样有时效。

## 本文件盯的

- 旧 Core（没有这个字段）不凭空多出由头；
- 脏数据（字符串/数字/列表/空 phrase）一律当「这会儿没有这样一件事」；
- kind 登记在 `_KIND_TTL` 与 `KIND_RANK` 里——漏登记的 kind 会被当成最低优先级。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

# 自己装 stub + 插路径：不依赖别的测试文件先跑（unittest 按字母序，
# 单独跑这一个文件时没有任何人替我们装过）。
_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE.parent


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    import logging
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_test")
    event = types.ModuleType("astrbot.api.event")

    class _Chain:
        def message(self, text):
            return ("chain", text)

    event.MessageChain = _Chain()
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import social.anchors as anchors  # noqa: E402
from social.core_bridge import CoreBridge  # noqa: E402


class _Bridge(CoreBridge):
    """只暴露 _build_self_view，不碰磁盘。"""

    def __init__(self):
        super().__init__(mode="standalone", data_dir=None)


def view(contract):
    return _Bridge()._build_self_view({"contract": contract}, "bot1")


class EventBlockReadTest(unittest.TestCase):
    def test_present(self):
        got = view({"v": 2, "event": {"phrase": "刚才被人敷衍了"}})
        self.assertEqual(got["event"], {"phrase": "刚才被人敷衍了"})

    def test_ignores_age_seconds(self):
        """Core 故意不给 age_seconds（每秒在变 → 每分钟重写 state.json）。

        万一对面给了，也不该进我们的视图——它会让指纹每分钟变一次。
        """
        got = view({"v": 2, "event": {"phrase": "刚才被人敷衍了", "age_seconds": 12.5}})
        self.assertEqual(got["event"], {"phrase": "刚才被人敷衍了"})

    def test_also_in_day(self):
        """groupflow 读 day.event，anchors 读顶层——两处都得有，且是同一个对象。"""
        got = view({"v": 2, "event": {"phrase": "刚才被人敷衍了", "age_seconds": 0.0}})
        self.assertEqual(got["day"]["event"], got["event"])
        self.assertIs(got["day"]["event"], got["event"])

    def test_event_comes_from_contract_top_level(self):
        """Core 把 event 放在契约**顶层**（与 ongoing/wants 同级），不在 day 里。

        这条盯的是一个差点漏掉的错：在 `day` 块里读 `day.get("event")`——
        Core 从来不往那儿写，于是由头永远空，而且不报错。
        """
        contract = {
            "v": 2,
            "event": {"phrase": "刚才被人敷衍了", "age_seconds": 5.0},
            # day 里**故意**放一个不同的值：如果代码读的是 day，就会拿到这个
            "day": {"event": {"phrase": "这是day里的", "age_seconds": 99.0}},
        }
        got = view(contract)
        self.assertEqual(got["event"]["phrase"], "刚才被人敷衍了")
        self.assertEqual(got["day"]["event"]["phrase"], "刚才被人敷衍了")

    def test_old_core_missing(self):
        self.assertEqual(view({"v": 2}).get("event"), {})

    def test_old_core_none(self):
        self.assertEqual(view({"v": 2, "event": None}).get("event"), {})

    def test_bad_shapes(self):
        for bad in ("字符串", 3, [1, 2], {"phrase": ""}, {"phrase": None},
                    {"phrase": "  "}, {"age_seconds": "x"}):
            got = view({"v": 2, "event": bad})
            self.assertEqual(got["event"], {}, msg=f"{bad!r} 该当没有")


class AnchorDeriveTest(unittest.TestCase):
    def _derive(self, event):
        body = {"day": {"event": event}, "activity": {}, "event": event}
        kinds: dict = {}
        out = anchors._derive_from_state(body, {}, 1_000_000.0, set(), kinds=kinds)
        return out, kinds

    def test_phrase_becomes_anchor(self):
        out, kinds = self._derive({"phrase": "刚才被人敷衍了", "age_seconds": 0.0})
        self.assertIn("刚才被人敷衍了", out)
        self.assertEqual(kinds.get("刚才被人敷衍了"), anchors.KIND_RECENT)

    def test_empty_event_no_anchor(self):
        out, _ = self._derive({})
        self.assertEqual(out, [])

    def test_bad_event_no_anchor(self):
        out, _ = self._derive("不是 dict")
        self.assertEqual(out, [])

    def test_kind_registered_in_ttl(self):
        """漏登记会被 KIND_TTL_FALLBACK 接管，但那是兜底不是应该。"""
        self.assertIn(anchors.KIND_RECENT, anchors._KIND_TTL)

    def test_kind_registered_in_rank(self):
        self.assertIn(anchors.KIND_RECENT, anchors.KIND_RANK)

    def test_recent_ranks_with_dream(self):
        """和梦同级：它和梦一样有时效，且是她心里正过着一件事。"""
        self.assertEqual(anchors.KIND_RANK[anchors.KIND_RECENT],
                         anchors.KIND_RANK[anchors.KIND_DREAM])

    def test_ttl_is_short(self):
        self.assertLessEqual(anchors._KIND_TTL[anchors.KIND_RECENT],
                             anchors._KIND_TTL[anchors.KIND_DERIVED])


# ─── P2-10：契约新鲜度（generated_at 过期 → 此刻状态不当作现在）──────
#
# 审核真实数据：state.json 里某个角色的契约仍是 14 天前的 generated_at，
# 而 social 的 humanoid_signals.json 里还有该 bid 的槽位（Core 重启后角色实例按
# 首条消息惰性创建，空窗期只能读到旧文件）。旧实现从不看 generated_at，于是主动
# 消息会基于几天前的 day.doing / wake / routine 生成，而且没有任何「这是旧的」标记。


def _contract(age_seconds: float, **extra) -> dict:
    contract = {
        "v": 2,
        "generated_at": time.time() - float(age_seconds),
        "body": {"energy": 70.0, "energy_text": "精神不错"},
        "day": {"doing": "写报告", "done": ["开会"], "next": ["吃饭"], "dreams": ["海"]},
        "wake": {"asleep": False, "inertia_minutes_left": 3},
        "routine": {"night_start_hour": 2.0, "night_end_hour": 10.0},
        "activity": {"name": "办公室", "location": "公司"},
        "event": {"phrase": "被夸了"},
        "ongoing": ["整理那份个案笔记"],
        "wants": ["去那家书店"],
        "identity": {"char_name": "小娜", "persona_label": "温柔"},
        "outfit": {"enabled": True, "text": "白毛衣", "source": "manual"},
        "weather": "晴",
    }
    contract.update(extra)
    return contract


def _write_state(tmp: str, root) -> None:
    path = os.path.join(tmp, "plugin_data", "humanoid_core", "state.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(root, f, ensure_ascii=False)


class ContractFreshnessTest(unittest.TestCase):
    NOW = 1_700_000_000.0

    def _info(self, contract, **kwargs):
        return _Bridge().contract_freshness(contract, now=self.NOW, **kwargs)

    def test_threshold_is_45_minutes(self):
        from social.core_bridge import CONTRACT_STALE_SECONDS

        self.assertEqual(CONTRACT_STALE_SECONDS, 45 * 60.0)

    def test_fresh_contract(self):
        info = self._info({"v": 2, "generated_at": self.NOW - 60.0})
        self.assertTrue(info["checked"])
        self.assertTrue(info["fresh"])
        self.assertFalse(info["stale"])
        self.assertAlmostEqual(info["age_seconds"], 60.0, places=2)

    def test_just_below_the_threshold_is_fresh(self):
        info = self._info({"v": 2, "generated_at": self.NOW - 44 * 60.0})
        self.assertTrue(info["fresh"])
        self.assertFalse(info["stale"])

    def test_older_than_the_threshold_is_stale(self):
        info = self._info({"v": 2, "generated_at": self.NOW - 46 * 60.0})
        self.assertFalse(info["fresh"])
        self.assertTrue(info["stale"])
        self.assertIn("generated_at", info["reason"])

    def test_14_days_old_is_stale(self):
        info = self._info({"v": 2, "generated_at": self.NOW - 14 * 86400.0})
        self.assertTrue(info["stale"])
        self.assertAlmostEqual(info["age_seconds"], 14 * 86400.0, delta=5.0)

    def test_missing_generated_at_is_unchecked_not_stale(self):
        """旧版 Core 没有 generated_at：按未校验处理，与加这个机制之前逐字等价。"""
        info = self._info({"v": 1, "day": {"doing": "写报告"}})
        self.assertFalse(info["checked"])
        self.assertIsNone(info["fresh"])
        self.assertFalse(info["stale"])
        self.assertIn("generated_at", info["reason"])

    def test_dirty_generated_at_is_unchecked(self):
        for bad in ("坏值", None, 0, -5.0, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                info = self._info({"v": 2, "generated_at": bad})
                self.assertFalse(info["checked"], f"{bad!r} 不该被当成有效时间戳")
                self.assertFalse(info["stale"])

    def test_future_timestamp_is_clamped_to_fresh(self):
        info = self._info({"v": 2, "generated_at": self.NOW + 120.0})
        self.assertTrue(info["checked"])
        self.assertTrue(info["fresh"])
        self.assertEqual(info["age_seconds"], 0.0)

    def test_max_age_override(self):
        info = self._info({"v": 2, "generated_at": self.NOW - 120.0}, max_age=60.0)
        self.assertTrue(info["stale"])

    def test_contract_stale_shortcut(self):
        bridge = _Bridge()
        self.assertTrue(
            bridge.contract_stale({"v": 2, "generated_at": self.NOW - 3600.0}, now=self.NOW)
        )
        self.assertFalse(
            bridge.contract_stale({"v": 2, "generated_at": self.NOW - 10.0}, now=self.NOW)
        )
        self.assertFalse(bridge.contract_stale(None))
        self.assertFalse(bridge.contract_stale("不是 dict"))

    def test_non_dict_contract_is_unchecked(self):
        for bad in (None, "契约", [1, 2], 3):
            with self.subTest(bad=bad):
                info = _Bridge().contract_freshness(bad)
                self.assertFalse(info["stale"])
                self.assertFalse(info["checked"])


class StaleContractViewTest(unittest.TestCase):
    def test_stale_contract_neutralizes_the_moment_blocks(self):
        got = view(_contract(14 * 86400.0))
        self.assertTrue(got["contract_stale"])
        self.assertFalse(got["contract_fresh"])
        self.assertGreater(got["contract_age_seconds"], 13 * 86400.0)
        self.assertEqual(
            got["day"],
            {"doing": "", "done": [], "next": [], "wants": [], "dreams": [],
             "event": {}, "wake": {}},
            "14 天前的 day 仍然被当作「今天」——P2-10 没修好",
        )
        for key in ("wake", "routine", "activity", "event", "outfit", "weather"):
            self.assertEqual(got[key], {}, f"过期契约的 {key} 没有被清空")
        self.assertIsNone(got["night_window"])
        self.assertEqual(got["ongoing"], [])

    def test_stale_contract_keeps_identity_and_body(self):
        """身份（她是谁）与身体标量是「最近已知值」，不因过期就丢掉。"""
        got = view(_contract(14 * 86400.0))
        self.assertEqual(got["identity"]["char_name"], "小娜")
        self.assertEqual(got["energy"], 70.0)
        self.assertEqual(got["contract_v"], 2)

    def test_fresh_contract_keeps_the_moment_blocks(self):
        got = view(_contract(30.0))
        self.assertFalse(got["contract_stale"])
        self.assertTrue(got["contract_fresh"])
        self.assertEqual(got["day"]["doing"], "写报告")
        self.assertEqual(got["ongoing"], ["整理那份个案笔记"])
        self.assertEqual(got["wake"]["inertia_minutes_left"], 3)
        self.assertEqual(got["outfit"]["text"], "白毛衣")
        self.assertEqual(got["night_window"], (2.0, 10.0))

    def test_contract_without_generated_at_keeps_the_moment_blocks(self):
        contract = _contract(0.0)
        contract.pop("generated_at")
        got = view(contract)
        self.assertFalse(got["contract_stale"])
        self.assertIsNone(got["contract_fresh"])
        self.assertIsNone(got["contract_age_seconds"])
        self.assertEqual(got["day"]["doing"], "写报告")

    def test_old_v1_contract_without_generated_at_is_unchanged(self):
        got = view({"v": 1, "day": {"doing": "写报告"}, "ongoing": ["整理笔记"]})
        self.assertFalse(got["contract_stale"])
        self.assertEqual(got["day"]["doing"], "写报告")
        self.assertEqual(got["ongoing"], ["整理笔记"])

    def test_stale_text_does_not_leak_into_compact(self):
        bridge = _Bridge()
        text = bridge.compact(view(_contract(14 * 86400.0)), include_weather=False)
        self.assertNotIn("写报告", text)
        self.assertNotIn("办公室", text)
        self.assertNotIn("整理那份个案笔记", text)


class StaleContractSnapshotTest(unittest.TestCase):
    """过期契约的**整条取数路径**：load_snapshot 里 user/relationship 照常可用。"""

    USER = {
        "mood": {"affection": 66.0, "base_affection": 40.0},
        "nickname": "小明",
        "display_name": "名片名·装饰",
        "said": [{"said": "明天面试", "at": 123.0}],
        "last_interaction": 456.0,
        "first_met": 100.0,
        "ccb_done_count": 2,
    }

    def _snapshot(self, contract):
        tmp = tempfile.mkdtemp()
        root = {
            "roles": {
                "b1": {
                    "self": {"energy": 55.0, "contract": contract},
                    "users": {"u1": self.USER},
                }
            }
        }
        _write_state(tmp, root)
        bridge = CoreBridge("auto", data_dir=tmp)
        return bridge.load_snapshot("b1", "u1")

    def test_14_day_old_contract_snapshot_is_marked_stale(self):
        snap = self._snapshot(_contract(14 * 86400.0))
        self.assertIsNotNone(snap)
        self.assertTrue(snap["contract_stale"])
        self.assertEqual(snap["day"]["doing"], "")
        self.assertEqual(snap["wake"], {})
        self.assertEqual(snap["ongoing"], [])

    def test_user_relationship_data_survives_a_stale_contract(self):
        """P2-10 只清「此刻状态」：好感/称呼/说过的话/亲密历史仍可用。"""
        snap = self._snapshot(_contract(14 * 86400.0))
        self.assertTrue(snap["contract_stale"])
        self.assertEqual(snap["affection"], 66.0)
        self.assertEqual(snap["base_affection"], 40.0)
        self.assertEqual(snap["call_name"], "小明")
        self.assertEqual(snap["said"], [{"said": "明天面试", "at": 123.0}])
        self.assertEqual(snap["first_met"], 100.0)
        self.assertEqual(snap["last_interaction"], 456.0)
        self.assertEqual(snap["ccb_done_count"], 2)

    def test_fresh_contract_snapshot_is_not_stale(self):
        snap = self._snapshot(_contract(30.0))
        self.assertFalse(snap["contract_stale"])
        self.assertEqual(snap["day"]["doing"], "写报告")
        self.assertEqual(snap["affection"], 66.0)


if __name__ == "__main__":
    unittest.main()
