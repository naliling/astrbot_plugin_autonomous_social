"""阶段 0（社交侧）回归：`_is_idle` 读不到契约给的 `feelings`。

契约里 `feelings` 是 `list[str]`（`link.build_contract` 按显著度取前几条），而这里
原来只认 `list[dict]`，字符串元素全被 `isinstance(item, dict)` 跳过 → `_is_idle()` 恒
False → 「今天挺空的」这条由头**从上线起一次都没产出过**。它不会报错，只会在
`/自主社交状态` 里表现为「由头总是那几种」。

另一条 `_life_log_done()`（「刚做完 X」）已整条删除：它读的 `body["life_log"]` 在
`core_bridge` 里从不映射，而 Core 的账本也没有 `kind` 字段——留着只会让读代码的人
以为它在工作。删除原因写在 `anchors._derive_from_state` 的 ①-bis 注释里。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest

# 与其它 social 用例一致：只补 astrbot 的最小表面
if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("test")
    _astrbot.api = _api
    _event = types.ModuleType("astrbot.api.event")
    _event.MessageChain = object
    _api.event = _event
    sys.modules.update({"astrbot": _astrbot, "astrbot.api": _api, "astrbot.api.event": _event})

from social import anchors  # noqa: E402

NOW = 1_700_000_000.0


def _user(**kw):
    base = {
        "umo": "aiocqhttp:FriendMessage:1",
        "last_seen": NOW - 3600,
        "last_spoken": NOW - 7200,
        "conversation": [{"dir": "in", "text": "今天好累", "ts": NOW - 3600}],
        "topics": [],
    }
    base.update(kw)
    return base


class IdleFeelingsShapeTest(unittest.TestCase):
    """三种形状都得认，否则「她闲不闲」这件事根本读不到。"""

    def test_contract_shape_list_of_str(self):
        """契约实际给的就是这个形状。"""
        self.assertTrue(anchors._is_idle({"feelings": ["很闲"]}))
        self.assertTrue(anchors._is_idle({"feelings": ["手上没什么事，不忙"]}))

    def test_legacy_shape_list_of_dict(self):
        self.assertTrue(anchors._is_idle({"feelings": [{"text": "挺闲的"}]}))

    def test_legacy_shape_dict(self):
        self.assertTrue(anchors._is_idle({"feelings": {"spare": 0.9}}))

    def test_busy_or_empty_is_not_idle(self):
        for body in ({"feelings": ["正在开会"]}, {"feelings": []}, {}, {"feelings": None},
                     {"feelings": [123, None]}):
            self.assertFalse(anchors._is_idle(body), body)


class IdleAnchorReachesThePoolTest(unittest.TestCase):
    """不只函数返回 True——那条由头要真的出现在候选里。"""

    def test_idle_anchor_is_produced_from_contract_shape(self):
        picks = anchors.prepare_round_anchors(_user(), {"feelings": ["很闲"]}, NOW, limit=3)
        texts = [str(p.get("about") or "") for p in picks]
        self.assertIn("今天挺空的", texts, f"由头池里没有「今天挺空的」：{texts}")

    def test_no_idle_anchor_when_she_is_busy(self):
        picks = anchors.prepare_round_anchors(_user(), {"feelings": ["正在开会"]}, NOW, limit=3)
        texts = [str(p.get("about") or "") for p in picks]
        self.assertNotIn("今天挺空的", texts)


class DeletedLifeLogPathTest(unittest.TestCase):
    """删掉的那条派生不许以别的名字回来。"""

    def test_no_life_log_helper_left(self):
        self.assertFalse(hasattr(anchors, "_life_log_done"))
        self.assertFalse(hasattr(anchors, "LIFE_LOG_FRESH_SECONDS"))


if __name__ == "__main__":
    unittest.main()
