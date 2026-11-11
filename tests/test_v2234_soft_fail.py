"""v2.2.34：降级继续的地方要留痕。

背景：v2.2.22 那次 `_dedupe_same_event_segments` 漏了 import，`NameError` 被
`except Exception: pass` 吞掉，去重一直空转——**代码在跑、功能没跑**，单测全绿，
线上也看不出来。同类的还有 v2.2.31（破冰参数写错，被外层 except 吞成一行 WARN）。

所以给「出错了也不停」的关键位置加一个统一出口 `_note_soft_fail`：
同一个位置前 3 次、之后每 50 次记一条 warning，既不刷屏，也不会再无声降级。
"""
from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import types
import unittest

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("aso_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social import engine as engine_mod  # noqa: E402


class _Engine:
    """只借 `_note_soft_fail`，不构造完整引擎。"""

    _note_soft_fail = engine_mod.SocialEngine._note_soft_fail


class SoftFailLoggingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.eng = _Engine()
        logger = engine_mod.logger  # 引擎模块实际用的 logger（桩里是 astrbot.api.logger）
        self.records = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.records.append(record)  # type: ignore[assignment]
        self.old_level = logger.level
        logger.addHandler(self.handler)
        logger.setLevel(logging.WARNING)
        self.logger = logger

    def tearDown(self) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)

    def _feed(self, where: str, times: int) -> None:
        for _ in range(times):
            self.eng._note_soft_fail(where, ValueError("boom"))

    def test_first_three_are_logged(self) -> None:
        self._feed("私聊碎片合并", 3)
        self.assertEqual(len(self.records), 3)
        self.assertIn("私聊碎片合并", self.records[-1].getMessage())
        self.assertIn("ValueError", self.records[-1].getMessage())

    def test_fourth_is_quiet(self) -> None:
        self._feed("私聊碎片合并", 4)
        self.assertEqual(len(self.records), 3, "第 4 次应该被压住，不刷屏")

    def test_fiftieth_is_logged_again(self) -> None:
        self._feed("同次发言去重", 50)
        self.assertEqual(len(self.records), 4, "前 3 次 + 第 50 次")
        self.assertIn("第 50 次", self.records[-1].getMessage())

    def test_counter_is_per_where(self) -> None:
        self._feed("出口清理", 3)
        self._feed("回声提醒取数", 3)
        self.assertEqual(len(self.records), 6, "两个位置各自计数，互不影响")

    def test_logging_never_raises(self) -> None:
        # 就算是 None/怪对象也不能把调用方带崩
        self.eng._note_soft_fail(None, RuntimeError("x"))  # type: ignore[arg-type]
        self.eng._note_soft_fail("未知", None)  # type: ignore[arg-type]
        self.assertTrue(True)


class WiringTest(unittest.TestCase):
    def test_five_call_sites(self) -> None:
        src = io.open(Path(PLUGIN_ROOT) / "social" / "engine.py", encoding="utf-8").read()
        self.assertEqual(src.count("self._note_soft_fail("), 5)
        for where in ("出口「刚」开头统计", "出口清理", "回声提醒取数", "私聊碎片合并", "同次发言去重"):
            self.assertIn(where, src, f"缺少 {where} 的降级留痕")

    def test_no_bare_pass_in_those_four_spots(self) -> None:
        src = io.open(Path(PLUGIN_ROOT) / "social" / "engine.py", encoding="utf-8").read()
        for old in (
            "segments, waits = _merge_fragments(segments, waits)\n        except Exception:\n            pass",
            "segments, waits = _dedupe_same_event_segments(segments, waits)\n        except Exception:\n            pass",
        ):
            # 兼容 CRLF
            self.assertNotIn(old, src.replace("\r\n", "\n"))


if __name__ == "__main__":
    unittest.main()
