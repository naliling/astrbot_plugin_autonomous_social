"""v2.0.5：主动发出的消息补写进 AstrBot 会话历史，并避免同一句被注入两遍。

v2.0.4 的主动消息只活在自己的账本里：AstrBot 会话库没有这几句，WebUI 看不到、
记忆类插件也读不到。v2.0.5 在发送成功后补一条 assistant 条目（默认开，可用
`persist_proactive_history` 关掉）；写成功后把 pending 里对应的临时注入条目打上
`in_history` 标记，`on_llm_request` 不再把同一句塞临时块。

本文件钉住四件事：

1. 成功发出后会话历史末尾多一条 assistant；
2. 同一条重复写不会重复追加（末条同文跳过）；
3. 写入成功的那条不再被 `inject_proactive_context` 注入；没写成功的照旧注入；
4. 关掉 `persist_proactive_history` 时行为回到 v2.0.4（只靠临时注入）。

另外钉住「超过 200 字的长消息去重」：账本截 200、会话库截 300，旧代码用精确
文本比较，>200 字的主动消息在生成上下文里会出现两遍。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = (
    _HERE.parent
    if (_HERE.parent / "_conf_schema.json").is_file()
    else _HERE.parent / "astrbot_plugin_autonomous_social"
)


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v205_history_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.engine import SocialEngine, _dedupe_key  # noqa: E402
from social.state import CONVERSATION_TRUNCATE_LENGTH  # noqa: E402

BASE = 1_700_000_000.0
BID = "bot1"
UID = "u1"
UMO = f"aiocqhttp:FriendMessage:{BID}-{UID}"


class _FakeConversationManager:
    """conversation_mgr 的最小桩：history 是 JSON 字符串，写回整段覆盖。"""

    def __init__(self, history=None, cid: str = "cid1") -> None:
        self.history = [dict(m) for m in (history or [])]
        self.cid = cid
        self.updates = []
        self.calls = []
        self.fail_times = 0

    async def get_curr_conversation_id(self, umo):
        self.calls.append(("curr", umo))
        return self.cid

    async def get_conversation(self, umo, cid=None):
        self.calls.append(("get", umo, cid))
        return SimpleNamespace(history=json.dumps(self.history, ensure_ascii=False))

    async def update_conversation(
        self,
        umo,
        conversation_id=None,
        history=None,
        title=None,
        persona_id=None,
        token_usage=None,
    ):
        self.calls.append(("update", conversation_id))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("db busy")
        self.updates.append(
            {
                "cid": conversation_id,
                "history": [dict(m) for m in (history or [])],
                "title": title,
                "persona_id": persona_id,
                "token_usage": token_usage,
            }
        )
        self.history = [dict(m) for m in (history or [])]


def _engine(cm=None, **overrides) -> SocialEngine:
    tmp = tempfile.mkdtemp(prefix="as_v205_hist_")
    ctx = SimpleNamespace(conversation_manager=cm) if cm is not None else None
    return SocialEngine(
        context=ctx,
        config=dict(overrides),
        state_path=os.path.join(tmp, "state.json"),
        data_dir=tmp,
    )


def _record(engine: SocialEngine, text: str) -> None:
    engine.state.record_outgoing(BID, UID, text, store_text=True)


async def _drain(engine: SocialEngine) -> None:
    """等 fire-and-forget 的后台任务跑完（发送主链路不等它）。"""
    for _ in range(20):
        tasks = [t for t in list(engine._bg_tasks) if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def _install_main_stub() -> None:
    """给 main.py 造最小 astrbot 宿主（照 tests/test_v1273_review_fixes.py 的写法）。"""
    if "astrbot.api.star" in sys.modules:
        return
    api = sys.modules["astrbot.api"]
    star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    star.Context = Context
    star.Star = Star
    sys.modules["astrbot.api.star"] = star
    api.star = star

    filters = types.ModuleType("astrbot.api.event.filter")

    class EventMessageType:
        ALL = "all"

    def _passthrough(*_a, **_k):
        def _decorator(func=None):
            return func

        return _decorator

    filters.EventMessageType = EventMessageType
    filters.command = _passthrough
    filters.event_message_type = _passthrough
    filters.on_llm_request = _passthrough
    filters.after_message_sent = _passthrough
    sys.modules["astrbot.api.event.filter"] = filters

    event = sys.modules["astrbot.api.event"]
    event.filter = filters
    event.AstrMessageEvent = object
    api.AstrBotConfig = dict


def _load_main_module():
    """导入 main.py（相对导入 → 合成包 pluginpkg，映射回已导入的 social 模块）。"""
    if "pluginpkg.main" in sys.modules:
        return sys.modules["pluginpkg.main"]
    _install_main_stub()
    pkg = types.ModuleType("pluginpkg")
    pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules["pluginpkg"] = pkg
    sys.modules["pluginpkg.social"] = importlib.import_module("social")
    for name in ("config", "engine"):
        sys.modules[f"pluginpkg.social.{name}"] = importlib.import_module(
            f"social.{name}"
        )
    spec = importlib.util.spec_from_file_location(
        "pluginpkg.main", os.path.join(str(PLUGIN_ROOT), "main.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["pluginpkg.main"] = module
    spec.loader.exec_module(module)
    return module


class PersistProactiveHistoryTest(unittest.IsolatedAsyncioTestCase):
    """Proactive assistant history is persisted independently of request hooks."""

    async def test_successful_persist_appends_assistant_entry(self) -> None:
        cm = _FakeConversationManager([{"role": "user", "content": "在吗"}])
        engine = _engine(cm)
        self.assertTrue(engine.cfg.persist_proactive_history, "默认应为开")
        _record(engine, "刚下班，今天累死了")
        engine._persist_proactive_to_history(BID, UID, UMO, "刚下班，今天累死了")
        await _drain(engine)

        self.assertEqual(
            cm.history,
            [
                {"role": "user", "content": "在吗"},
                {"role": "assistant", "content": "刚下班，今天累死了"},
            ],
        )
        self.assertEqual(cm.updates[0]["cid"], "cid1")
        # title/persona/token 传 None：DB 层只更新非 None 字段，不能顺手清空。
        self.assertIsNone(cm.updates[0]["title"])
        self.assertIsNone(cm.updates[0]["persona_id"])
        self.assertNotIn("pending_proactive_context", engine.state.user(BID, UID))

    async def test_duplicate_write_is_skipped(self) -> None:
        cm = _FakeConversationManager([{"role": "user", "content": "在吗"}])
        engine = _engine(cm)
        _record(engine, "晚安")
        await engine._persist_proactive_to_history_inner(BID, UID, UMO, "晚安")
        self.assertEqual(len(cm.history), 2)
        self.assertEqual(len(cm.updates), 1)

        # 同一条再写一次：末条就是同文 assistant → 跳过，不产生第二次 update。
        await engine._persist_proactive_to_history_inner(BID, UID, UMO, "晚安")
        self.assertEqual(len(cm.history), 2)
        self.assertEqual(len(cm.updates), 1)

    async def test_transient_failure_retried_once(self) -> None:
        cm = _FakeConversationManager([{"role": "user", "content": "在吗"}])
        cm.fail_times = 1
        engine = _engine(cm)
        _record(engine, "还没睡呀")
        ok = await engine._persist_proactive_to_history_inner(BID, UID, UMO, "还没睡呀")
        self.assertTrue(ok)
        self.assertEqual(cm.history[-1], {"role": "assistant", "content": "还没睡呀"})
        self.assertEqual(len(cm.updates), 1)

    async def test_persist_disabled_does_not_queue_for_injection(self) -> None:
        cm = _FakeConversationManager([{"role": "user", "content": "在吗"}])
        engine = _engine(cm, persist_proactive_history=False)
        self.assertFalse(engine.cfg.persist_proactive_history)
        _record(engine, "晚安")
        engine._persist_proactive_to_history(BID, UID, UMO, "晚安")
        await _drain(engine)

        self.assertEqual(cm.history, [{"role": "user", "content": "在吗"}])
        self.assertNotIn("pending_proactive_context", engine.state.user(BID, UID))

    async def test_no_conversation_manager_is_quiet(self) -> None:
        engine = _engine(None)
        _record(engine, "晚安")
        ok = await engine._persist_proactive_to_history_inner(BID, UID, UMO, "晚安")
        self.assertFalse(ok)
        self.assertNotIn("pending_proactive_context", engine.state.user(BID, UID))

    async def test_history_persistence_does_not_create_an_injection_queue(self) -> None:
        cm = _FakeConversationManager()
        engine = _engine(cm)
        _record(engine, "主动历史保留")
        await engine._persist_proactive_to_history_inner(BID, UID, UMO, "主动历史保留")
        self.assertEqual(cm.history[-1], {"role": "assistant", "content": "主动历史保留"})
        self.assertNotIn("pending_proactive_context", engine.state.user(BID, UID))





class PassiveRequestIsolationTest(unittest.TestCase):
    """Passive LLM requests must not receive autonomous-social text."""

    def test_main_has_no_request_hook_or_request_mutation(self) -> None:
        main_mod = _load_main_module()
        with open(main_mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for forbidden in (
            "on_llm_request",
            "inject_proactive_context",
            "consume_pending_proactive_context",
            "extra_user_content_parts",
            "req.contexts",
        ):
            self.assertNotIn(forbidden, src)
        self.assertFalse(hasattr(main_mod.AutonomousSocial, "inject_proactive_context"))


class LongProactiveDedupeTest(unittest.TestCase):
    """>200 字的主动消息：账本截 200、会话库截 300，不能再被判成缺条目。"""

    def test_dedupe_key_truncates_both_sides(self) -> None:
        long_text = "长" * 500
        self.assertEqual(
            _dedupe_key("out", long_text),
            _dedupe_key("out", long_text[:CONVERSATION_TRUNCATE_LENGTH]),
        )
        self.assertEqual(
            _dedupe_key("out", long_text)[1],
            long_text[:CONVERSATION_TRUNCATE_LENGTH],
        )

    def test_generation_context_contains_long_message_once(self) -> None:
        long_text = "她" * 500
        engine = _engine(_FakeConversationManager())
        ledger = [
            {
                "dir": "out",
                "text": long_text[:CONVERSATION_TRUNCATE_LENGTH],
                "ts": BASE,
            }
        ]
        session = [{"dir": "out", "text": long_text[:300]}]
        conv = engine._generation_conversation({"conversation": ledger}, session)
        self.assertEqual(len(conv), 1, f"同一句被插了两遍：{conv}")
        self.assertEqual(conv[0]["text"], long_text[:300])


class SendPathWiringTest(unittest.TestCase):
    """发送成功点真的调了持久化，别只把方法写在那里没人用。"""

    def test_speak_and_burst_call_persist(self) -> None:
        speak = inspect.getsource(SocialEngine._speak_inner)
        burst = inspect.getsource(SocialEngine._send_burst_followup)
        self.assertIn("_persist_proactive_to_history", speak)
        self.assertIn("_persist_proactive_to_history", burst)


if __name__ == "__main__":
    unittest.main()
