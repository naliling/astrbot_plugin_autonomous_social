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

import sys
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


if __name__ == "__main__":
    unittest.main()
