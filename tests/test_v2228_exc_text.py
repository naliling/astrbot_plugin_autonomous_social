# -*- coding: utf-8 -*-
"""v2.2.28：LLM 调用异常不能打成一行空白。

线上日志实证：
    [2026-11-11 01:57:05.638] LLM 已连续失败 9 次（text_chat 抛出异常: ），暂停调用 15 分钟。
冒号后面什么都没有——超时/取消这类异常 `str()` 是空串。于是「连续失败 9 次」这条
日志除了次数什么都没告诉人，只能猜。
"""
import os
import sys
import types
import unittest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _log = types.ModuleType("astrbot.api.logger")
    _log.warning = lambda *a, **k: None
    _log.info = lambda *a, **k: None
    _log.error = lambda *a, **k: None
    _log.debug = lambda *a, **k: None
    _api.logger = _log
    _cmps = types.ModuleType("astrbot.api.message_components")
    for _n in ("Plain", "At", "Image", "Reply"):
        setattr(_cmps, _n, type(_n, (), {}))
    _api.message_components = _cmps
    _astrbot.api = _api
    for _n in ("astrbot.api.event", "astrbot.core", "astrbot.core.utils"):
        sys.modules.setdefault(_n, types.ModuleType(_n))
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api
    sys.modules["astrbot.api.logger"] = _log
    sys.modules["astrbot.api.message_components"] = _cmps

if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import social.generator as gen  # noqa: E402


class ExcTextTest(unittest.TestCase):
    def test_empty_message_gives_type(self):
        self.assertEqual(gen._exc_text(Exception()), "Exception")

    def test_timeout_gives_type(self):
        import asyncio
        self.assertEqual(
            gen._exc_text(asyncio.TimeoutError()), "TimeoutError"
        )

    def test_message_kept_with_type(self):
        self.assertEqual(
            gen._exc_text(ValueError("bad arg")),
            "ValueError: bad arg",
        )

    def test_none_is_not_blank(self):
        self.assertTrue(gen._exc_text(None).strip())

    def test_blank_message_not_blank_output(self):
        self.assertTrue(gen._exc_text(Exception("   ")).strip())


class ModuleLevelTest(unittest.TestCase):
    """必须是模块级函数：写在类里会 NameError（v2.2.28 第一版踩过）。"""

    def test_module_level_not_class_attr(self):
        self.assertTrue(callable(getattr(gen, "_exc_text", None)))
        self.assertFalse(hasattr(gen.MessageGenerator, "_exc_text"))

    def test_timed_staticmethod_can_reach_it(self):
        src = open(
            os.path.join(PLUGIN_ROOT, "social", "generator.py"), encoding="utf-8"
        ).read()
        self.assertIn("async def _timed(", src)
        # 类体外定义在前，两处裸名调用在后
        self.assertLess(src.index("def _exc_text(exc: Any)"), src.index("class MessageGenerator:"))


class SourceWiringTest(unittest.TestCase):
    def test_call_sites_use_helper(self):
        with open(os.path.join(PLUGIN_ROOT, "social", "generator.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("抛出异常: {_exc_text(r)}", src)
        self.assertIn("LLM 调用异常（{label}）: {_exc_text(e)}", src)


if __name__ == "__main__":
    unittest.main()
