"""v1.27.32：和记忆插件的对接从「只往外发」变成「按由头检索 + 结束时交棒」。

三件事，各自钉住：

- **读**：`_fetch_memory_note` 把这次开口的**由头**当检索词。旧写法传空串，等于
  「把这个人的记忆按时间倒出来」——每次倒出同一批最久远的条目，跟这次要谈的事无关。
- **写**：主动消息带上由头，将来按由头检索时它就是「这件事她开过口」的证据。
- **交棒**：一件还开着的事有了结果、或她许的诺兑现了 → 落一条长期记忆。中间层
  （Core 的 `life_log`）到此为止，但这件事不该随账本一起消失。

记忆插件一个字节都不改：这里只验证**我们发出去的形状**对不对。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_memory_link_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import memory_bridge  # noqa: E402
from social.engine import SocialEngine  # noqa: E402


class _Recorder:
    """接住 `record_external_memory` 的每一次调用。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record_external_memory(self, **kw):
        self.calls.append(kw)
        return {"ok": True}


class _Composer:
    def __init__(self, text: str = "记得的事") -> None:
        self.kw: dict = {}
        self._text = text

    async def compose_context(self, **kw):
        self.kw = kw
        return self._text


class ProactiveCarriesTheTopicTest(unittest.IsolatedAsyncioTestCase):
    async def _call(self, **kwargs) -> dict:
        bridge = _Recorder()
        await memory_bridge.write_proactive(bridge, "u1", "在吗", **kwargs)
        self.assertEqual(len(bridge.calls), 1, "这条主动消息根本没被写出去")
        return bridge.calls[0]

    async def test_payload_carries_the_topic(self):
        call = await self._call(umo="umo", ts=1.0, about="他上次说的面试")
        self.assertIn("他上次说的面试", call["content"])
        self.assertIn("在吗", call["content"])

    async def test_payload_without_topic_is_unchanged(self):
        call = await self._call(umo="umo", ts=1.0)
        self.assertEqual(call["content"], "（她主动找 TA 说的）在吗")

    async def test_topic_does_not_break_the_idempotency_key(self):
        first = await self._call(umo="umo", ts=1.0, about="面试")
        second = await self._call(umo="umo", ts=1.0, about="面试")
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(first["idempotency_key"], "proactive:umo:1")


class OpenItemClosedTest(unittest.IsolatedAsyncioTestCase):
    async def _call(self, **kwargs) -> dict:
        bridge = _Recorder()
        await memory_bridge.write_open_closed(bridge, "u1", kwargs.pop("about", "面试结果"), **kwargs)
        self.assertEqual(len(bridge.calls), 1, "了结的事根本没被写出去")
        return bridge.calls[0]

    async def test_result_is_recorded_with_the_open_loop_tag(self):
        call = await self._call(result="过了", umo="umo")
        self.assertIn("面试结果", call["content"])
        self.assertIn("过了", call["content"])
        self.assertEqual(call["tags"], ["open_loop"])
        self.assertEqual(call["source_plugin"], memory_bridge.SOURCE_PLUGIN)

    async def test_a_fulfilled_promise_gets_its_own_tag(self):
        call = await self._call(kind="promise")
        self.assertEqual(call["tags"], ["promise"])

    async def test_it_weighs_more_than_a_greeting(self):
        call = await self._call()
        self.assertGreater(
            call["importance"], 0.5, "了结的事不该比一句寒暄还轻——它正是该长期记住的那类"
        )

    async def test_key_is_per_item_not_per_call(self):
        first = await self._call(umo="umo")
        second = await self._call(umo="umo")
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])

    async def test_different_items_get_different_keys(self):
        first = await self._call(about="面试结果", umo="umo")
        second = await self._call(about="那本书的封面", umo="umo")
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])

    async def test_empty_topic_writes_nothing(self):
        bridge = _Recorder()
        await memory_bridge.write_open_closed(bridge, "u1", "   ")
        self.assertEqual(bridge.calls, [])

    async def test_missing_method_is_reported_as_signature(self):
        class _NoMethod:
            pass

        await memory_bridge.write_open_closed(_NoMethod(), "u1", "面试结果")
        self.assertIn("record_external_memory", memory_bridge.last_error())


class FetchQueryTest(unittest.IsolatedAsyncioTestCase):
    async def test_query_reaches_compose_context(self):
        bridge = _Composer()
        await memory_bridge.fetch_memory_text(bridge, "umo", "u1", query="面试")
        self.assertEqual(bridge.kw["query"], "面试")

    async def test_default_query_is_empty(self):
        bridge = _Composer()
        await memory_bridge.fetch_memory_text(bridge, "umo", "u1")
        self.assertEqual(bridge.kw["query"], "")


class EngineFetchPassesTheReasonTest(unittest.IsolatedAsyncioTestCase):
    """引擎侧：由头必须真的走到检索里，而不是停在调用点。"""

    def _engine(self) -> SimpleNamespace:
        return SimpleNamespace(
            cfg=SimpleNamespace(memory_bridge_enabled=True),
            _memory_bridge=lambda: object(),
        )

    async def test_reason_becomes_the_query(self):
        seen: dict = {}

        async def _fake(bridge, umo, uid, **kw):
            seen.update(kw)
            return "记忆"

        orig = memory_bridge.fetch_memory_text
        memory_bridge.fetch_memory_text = _fake
        try:
            out = await SocialEngine._fetch_memory_note(
                self._engine(), "umo", "u1", query="他上次说的面试"
            )
        finally:
            memory_bridge.fetch_memory_text = orig
        self.assertEqual(out, "记忆")
        self.assertEqual(seen.get("query"), "他上次说的面试")

    async def test_no_bridge_means_no_lookup(self):
        engine = SimpleNamespace(
            cfg=SimpleNamespace(memory_bridge_enabled=True),
            _memory_bridge=lambda: None,
        )
        self.assertEqual(await SocialEngine._fetch_memory_note(engine, "umo", "u1"), "")


class HandoffSpawnsABackgroundWriteTest(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_loop_is_handed_to_the_long_term_layer(self):
        calls: list = []

        async def _fake(bridge, uid, about, **kw):
            calls.append((uid, about, kw))

        engine = SimpleNamespace(_memory_bridge=lambda: object(), _bg_tasks=set())
        orig = memory_bridge.write_open_closed
        memory_bridge.write_open_closed = _fake
        try:
            SocialEngine._remember_open_closed(engine, "umo", "u1", "面试", "过了", "loop")
            await asyncio.sleep(0)
        finally:
            memory_bridge.write_open_closed = orig
        self.assertEqual(
            calls, [("u1", "面试", {"result": "过了", "kind": "loop", "umo": "umo"})]
        )

    async def test_no_bridge_is_silent(self):
        engine = SimpleNamespace(_memory_bridge=lambda: None, _bg_tasks=set())
        # 没装记忆插件时这一路必须完全静默，不能抛。
        SocialEngine._remember_open_closed(engine, "umo", "u1", "面试", "过了")


if __name__ == "__main__":
    unittest.main()
