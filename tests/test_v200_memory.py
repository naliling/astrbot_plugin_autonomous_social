"""v2.0.0 记忆联动修复（审核 social v1.28.17：P1-3 / P2-06 / P2-07 / P2-14 / P2-15）。

本文件是**跨插件契约**的钉子，不是「我们内部函数能跑」的钉子：

* **P1-3（发布阻断）**：`compose_context(max_chars=)` 的语义是「整个注入块上限」，
  对面会先放一段 ~700-900 字符的固定骨架。这里用一个**行为与对面一致**的假桥：
  `max_chars < 1200` 时返回「记忆内容因预算不足未展开」壳、`>= 1800` 才返回正文，
  断言 `fetch_memory_text` 的**默认路径**真的拿到了正文（旧实现传 600，永远只有壳）。
* **P2-06**：`record_external_memory` 的失败是**正常返回** `{ok: False, state, error_code}`，
  不是异常；旧实现丢弃返回值、无条件记成功。这里断言被拒时 last_error 留痕、
  计数正确、且不会被 `_note_success` 清掉；兼容面（返回 None / 无 ok 键）仍按旧行为。
* **P2-07**：`status()` / `status_text()` / `reset()` / `bridge_alive()` 是引擎状态页与
  TTL 重试的接口，键名与语义必须稳定。
* **P2-14**：`probe_capability_snapshot()` 存在时校验 compose_context / record_external_memory；
  缺失按 signature 告警一次；没有 probe / 版本取不到时降级为「未校验」，读写不受影响。
* **P2-15**：读 6s / 写 8s 超时降级、群聊跳过读取、读缓存 60s（按 umo/uid/query/预算，
  且桥对象换了缓存失效）。

全部用假桥，不 import memory_companion 仓库（真实 E2E 另跑脚本佐证）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import time
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v200_memory_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import memory_bridge  # noqa: E402


#: 与 memory_companion 2.3.2 / 2.3.5 实测一致的「预算不足」兜底壳（229 字符左右）。
BUDGET_SHELL = (
    "【你记得的关于 TA 的事】"
    "（记忆内容因预算不足未展开：当前预算只够放下这段固定说明，"
    "更早的事这次没有空间放进来）"
)
BODY_TEXT = (
    "【你记得的关于 TA 的事】"
    "\n- TA 上周说猫吐了"
    "\n- TA 在准备面试"
)


class BudgetedBridge:
    """行为与 memory_companion 注入器一致的假桥：预算语义是**整块**，不是正文长度。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def compose_context(self, **kw):
        self.calls.append(kw)
        if int(kw.get("max_chars") or 0) < memory_bridge.MIN_USEFUL_MAX_CHARS:
            return BUDGET_SHELL
        return BODY_TEXT


class MemoryBridgeCase(unittest.IsolatedAsyncioTestCase):
    """每个用例前清一次模块状态：`status()` / 读缓存 / last_error 都是模块级的。"""

    def setUp(self) -> None:
        memory_bridge.reset()

    def tearDown(self) -> None:
        memory_bridge.reset()

    @staticmethod
    def _ctx(bridge):
        meta = types.SimpleNamespace(
            name="astrbot_plugin_memory_companion",
            display_name="我会牢牢记住你",
            root_dir_name="astrbot_plugin_memory_companion",
            module_path="",
            activated=True,
            module=types.SimpleNamespace(get_memory_companion_bridge=lambda: bridge),
        )
        return types.SimpleNamespace(get_all_stars=lambda: [meta])

    @staticmethod
    def _missing_ctx():
        return types.SimpleNamespace(get_all_stars=lambda: [])


# ─── P1-3：默认预算必须能拿到正文（发布阻断）──────────────────


class ReadBudgetContractTest(MemoryBridgeCase):
    def test_default_budget_is_1800(self):
        self.assertEqual(memory_bridge.DEFAULT_MAX_CHARS, 1800)
        self.assertGreaterEqual(
            memory_bridge.DEFAULT_MAX_CHARS,
            memory_bridge.MIN_USEFUL_MAX_CHARS,
            "默认预算低于「对面能给出正文」的下限，读路径等于没有",
        )

    def test_default_signature_uses_the_constant(self):
        """默认值必须来自常量：再写死一个 600 就是回归。"""
        default = inspect.signature(memory_bridge.fetch_memory_text).parameters[
            "max_chars"
        ].default
        self.assertEqual(default, memory_bridge.DEFAULT_MAX_CHARS)

    async def test_default_path_gets_the_body_not_the_shell(self):
        bridge = BudgetedBridge()
        text = await memory_bridge.fetch_memory_text(
            bridge, "aiocqhttp:private:u1", "u1", query="面试"
        )
        self.assertEqual(len(bridge.calls), 1)
        self.assertEqual(bridge.calls[0]["max_chars"], memory_bridge.DEFAULT_MAX_CHARS)
        self.assertIn("猫吐了", text, "默认预算下仍然只有壳 —— P1-3 没修好")
        self.assertNotIn("预算不足", text)
        self.assertEqual(memory_bridge.last_error(), "")

    async def test_low_explicit_budget_shell_is_not_injected(self):
        """调用方硬传 600：对面只会给壳，我们按「没有记忆」处理，不把壳塞进 prompt。"""
        bridge = BudgetedBridge()
        text = await memory_bridge.fetch_memory_text(
            bridge, "aiocqhttp:private:u2", "u2", max_chars=600
        )
        self.assertEqual(text, "")
        self.assertIn("budget", memory_bridge.last_error())
        self.assertEqual(memory_bridge.status()["read"]["budget_shell"], 1)

    async def test_contract_shape_matches_the_real_bridge(self):
        """假桥的预算行为必须真的镜像对面：<1200 壳、>=1800 正文。"""
        bridge = BudgetedBridge()
        low = await bridge.compose_context(query="", session_context={}, top_k=6, max_chars=600)
        high = await bridge.compose_context(query="", session_context={}, top_k=6, max_chars=1800)
        self.assertIn("预算不足", low)
        self.assertNotIn("猫吐了", low)
        self.assertIn("猫吐了", high)


# ─── P2-06：写入返回值必须被解析 ─────────────────────────────


class RecordReturnContractTest(MemoryBridgeCase):
    class _Rejecting:
        def __init__(self, error_code="external_records_disabled"):
            self.error_code = error_code
            self.calls: list = []

        async def record_external_memory(self, **kw):
            self.calls.append(kw)
            return {"ok": False, "state": "rejected", "error_code": self.error_code}

    class _Storing:
        async def record_external_memory(self, **kw):
            return {"ok": True, "state": "stored", "memory_id": "m1"}

    async def test_rejection_is_recorded_as_a_failure(self):
        bridge = self._Rejecting()
        await memory_bridge.write_proactive(bridge, "u1", "在吗", umo="umo", ts=1.0)
        self.assertEqual(len(bridge.calls), 1)
        self.assertIn("rejected", memory_bridge.last_error())
        self.assertIn("external_records_disabled", memory_bridge.last_error())
        self.assertIn("state=rejected", memory_bridge.last_error())
        self.assertEqual(memory_bridge.status()["write"]["rejected"], 1)
        self.assertEqual(memory_bridge.status()["write"]["ok"], 0)

    async def test_rejection_does_not_clear_the_error(self):
        """P2-06 的核心：`_note_success` 只在确认 ok 时调用。"""
        memory_bridge._LAST_ERROR = "exception: 上一次真的坏了"
        await memory_bridge.write_proactive(self._Rejecting(), "u1", "在吗", umo="umo")
        self.assertTrue(memory_bridge.last_error(), "被拒后 last_error 被清空 = 假成功")
        self.assertIn("rejected", memory_bridge.last_error())

    async def test_open_closed_write_checks_the_result_too(self):
        await memory_bridge.write_open_closed(
            self._Rejecting(error_code="user_id_required"), "u1", "面试结果", umo="umo"
        )
        self.assertIn("user_id_required", memory_bridge.last_error())
        self.assertEqual(memory_bridge.status()["write"]["rejected"], 1)

    async def test_ok_true_is_success_and_clears_the_error(self):
        memory_bridge._LAST_ERROR = "exception: 旧的"
        await memory_bridge.write_proactive(self._Storing(), "u1", "在吗", umo="umo", ts=1.0)
        self.assertEqual(memory_bridge.last_error(), "")
        self.assertEqual(memory_bridge.status()["write"]["ok"], 1)
        self.assertEqual(memory_bridge.status()["write"]["rejected"], 0)

    async def test_legacy_bridge_without_ok_key_keeps_old_behavior(self):
        """兼容面：对面 2.3.x 一定带 ok；但测试桩/更老实现返回 None 或 {"memory_id"} 时
        无法判定，按旧行为当成功（不能因为「没带 ok」就把正常写入记成失败）。"""
        class _NoOk:
            async def record_external_memory(self, **kw):
                return {"memory_id": "m1"}

        class _None:
            async def record_external_memory(self, **kw):
                return None

        memory_bridge._LAST_ERROR = "旧的"
        await memory_bridge.write_proactive(_NoOk(), "u1", "在吗", umo="umo")
        self.assertEqual(memory_bridge.last_error(), "")
        await memory_bridge.write_proactive(_None(), "u1", "在吗", umo="umo")
        self.assertEqual(memory_bridge.last_error(), "")

    async def test_write_wrapper_still_passes_the_contract_kwargs(self):
        bridge = self._Storing()
        seen = {}

        class _Spy:
            async def record_external_memory(self, **kw):
                seen.update(kw)
                return {"ok": True}

        await memory_bridge.write_open_closed(
            _Spy(), "u1", "面试结果", result="过了", kind="promise", umo="umo"
        )
        self.assertEqual(seen["user_id"], "u1")
        self.assertEqual(seen["tags"], ["promise"])
        self.assertIn("过了", seen["content"])
        self.assertIn("promise", seen["idempotency_key"])


# ─── P2-07：状态页 / TTL 重试接口 ────────────────────────────


class StatusAndRetryTest(MemoryBridgeCase):
    def test_status_keys_are_stable(self):
        info = memory_bridge.status()
        for key in (
            "state", "connected", "resolved", "failure", "reason", "last_error",
            "capability", "read", "write", "at", "retry_in",
        ):
            self.assertIn(key, info, f"status() 缺键 {key}（引擎状态页按它渲染）")
        self.assertEqual(info["state"], "unresolved")
        self.assertFalse(info["connected"])

    def test_missing_plugin_is_reported_and_retryable(self):
        memory_bridge.find_memory_bridge(self._missing_ctx())
        info = memory_bridge.status()
        self.assertEqual(info["state"], "missing")
        self.assertEqual(info["failure"], "missing")
        self.assertIn("memory_companion", info["reason"])
        self.assertGreaterEqual(info["retry_in"], 0.0)
        self.assertIn("未安装", memory_bridge.status_text())

    def test_disabled_bridge_is_reported_as_user_choice(self):
        module = types.SimpleNamespace(get_memory_companion_bridge=lambda: None)
        meta = types.SimpleNamespace(
            name="astrbot_plugin_memory_companion",
            display_name="我会牢牢记住你",
            root_dir_name="",
            module_path="",
            activated=True,
            module=module,
        )
        memory_bridge.find_memory_bridge(
            types.SimpleNamespace(get_all_stars=lambda: [meta])
        )
        info = memory_bridge.status()
        self.assertEqual(info["state"], "disabled")
        self.assertIn("private_companion_bridge.enabled", info["reason"])
        self.assertIn("对面关闭了桥接", memory_bridge.status_text())

    def test_connected_bridge_is_reported(self):
        memory_bridge.find_memory_bridge(self._ctx(BudgetedBridge()))
        info = memory_bridge.status()
        self.assertTrue(info["connected"])
        self.assertEqual(info["state"], "connected")
        self.assertIn("已连接", memory_bridge.status_text())

    def test_failure_is_not_permanently_cached(self):
        """P2-07：失败不永久缓存——下一次发现查得动就必须能连上。"""
        self.assertIsNone(memory_bridge.find_memory_bridge(self._missing_ctx()))
        bridge = BudgetedBridge()
        self.assertIs(memory_bridge.find_memory_bridge(self._ctx(bridge)), bridge)
        self.assertTrue(memory_bridge.status()["connected"])

    def test_disabled_status_text(self):
        self.assertIn("已关闭", memory_bridge.status_text(enabled=False))

    async def test_reset_clears_error_counters_and_cache(self):
        bridge = BudgetedBridge()
        await memory_bridge.fetch_memory_text(bridge, "aiocqhttp:private:u1", "u1")
        memory_bridge.find_memory_bridge(self._missing_ctx())
        memory_bridge.reset()
        info = memory_bridge.status()
        self.assertEqual(memory_bridge.last_error(), "")
        self.assertEqual(info["state"], "unresolved")
        self.assertEqual(info["read"]["ok"], 0)
        self.assertEqual(info["last_error"], "")

    def test_bridge_alive_reads_the_lifecycle_probe(self):
        class _Live:
            def bridge_lifecycle_status(self):
                return {"active": True}

        class _Dead:
            def bridge_lifecycle_status(self):
                return {"active": False}

        class _Broken:
            def bridge_lifecycle_status(self):
                raise RuntimeError("boom")

        self.assertTrue(memory_bridge.bridge_alive(_Live()))
        self.assertFalse(memory_bridge.bridge_alive(_Dead()))
        self.assertFalse(memory_bridge.bridge_alive(_Broken()))
        self.assertFalse(memory_bridge.bridge_alive(None))
        # 没有存活接口的旧桥/测试桩：按可用处理（本机制出现之前的行为）。
        self.assertTrue(memory_bridge.bridge_alive(object()))


# ─── P2-14：能力 / 版本门控 ────────────────────────────────


class CapabilityGateTest(MemoryBridgeCase):
    class _ProbeBridge(BudgetedBridge):
        def __init__(self, snapshot):
            super().__init__()
            self.snapshot = snapshot
            self.probe_calls = 0

        def probe_capability_snapshot(self):
            self.probe_calls += 1
            if isinstance(self.snapshot, BaseException):
                raise self.snapshot
            return self.snapshot

    def test_full_capability_snapshot_passes(self):
        bridge = self._ProbeBridge(
            {
                "available": True,
                "methods": ["compose_context", "record_external_memory", "search"],
                "contract_revision": "7",
            }
        )
        self.assertIs(memory_bridge.find_memory_bridge(self._ctx(bridge)), bridge)
        cap = memory_bridge.status()["capability"]
        self.assertTrue(cap["checked"])
        self.assertIs(cap["ok"], True)
        self.assertIn("compose_context", cap["methods"])
        self.assertFalse(cap["version_checked"], "桥对象没有版本字段时应标「版本未校验」")
        self.assertIn("能力校验通过", memory_bridge.status_text())

    def test_missing_required_method_is_signature_failure(self):
        bridge = self._ProbeBridge({"available": True, "methods": ["compose_context"]})
        self.assertIs(
            memory_bridge.find_memory_bridge(self._ctx(bridge)),
            bridge,
            "门控不通过也不该拒绝返回桥：读写自己的失败路径会记录原因",
        )
        cap = memory_bridge.status()["capability"]
        self.assertTrue(cap["checked"])
        self.assertIs(cap["ok"], False)
        self.assertTrue(any("record_external_memory" in p for p in cap["problems"]))
        self.assertIn("signature", memory_bridge.last_error())
        self.assertIn("能力校验未通过", memory_bridge.status_text())

    def test_version_is_reported_when_available(self):
        bridge = self._ProbeBridge(
            {"available": True, "methods": ["compose_context", "record_external_memory"]}
        )
        bridge.version = "2.3.5"
        memory_bridge.find_memory_bridge(self._ctx(bridge))
        cap = memory_bridge.status()["capability"]
        self.assertTrue(cap["version_checked"])
        self.assertEqual(cap["version"], "2.3.5")
        self.assertIn("v2.3.5", memory_bridge.status_text())

    def test_no_probe_degrades_to_unchecked(self):
        bridge = BudgetedBridge()  # 没有 probe_capability_snapshot
        self.assertIs(memory_bridge.find_memory_bridge(self._ctx(bridge)), bridge)
        cap = memory_bridge.status()["capability"]
        self.assertFalse(cap["checked"])
        self.assertIsNone(cap["ok"])
        self.assertFalse(cap["version_checked"])
        self.assertTrue(any("probe_capability_snapshot" in p for p in cap["problems"]))
        self.assertIn("未校验", memory_bridge.status_text())

    def test_probe_exception_degrades_without_raising(self):
        bridge = self._ProbeBridge(RuntimeError("probe 炸了"))
        self.assertIs(memory_bridge.find_memory_bridge(self._ctx(bridge)), bridge)
        cap = memory_bridge.status()["capability"]
        self.assertFalse(cap["checked"])
        self.assertTrue(any("RuntimeError" in p for p in cap["problems"]))

    def test_probe_bad_shape_degrades_without_raising(self):
        for bad in ("不是 dict", 3, [1, 2], {"available": True}, {"methods": "坏"}):
            with self.subTest(snapshot=bad):
                memory_bridge.reset()
                bridge = self._ProbeBridge(bad)
                self.assertIs(memory_bridge.find_memory_bridge(self._ctx(bridge)), bridge)
                self.assertFalse(memory_bridge.status()["capability"]["checked"])

    def test_probe_returning_a_coroutine_degrades_instead_of_warning(self):
        class _AsyncProbe(BudgetedBridge):
            def probe_capability_snapshot(self):
                async def _snapshot():
                    return {"methods": ["compose_context", "record_external_memory"]}

                return _snapshot()

        bridge = _AsyncProbe()
        self.assertIs(memory_bridge.find_memory_bridge(self._ctx(bridge)), bridge)
        self.assertFalse(memory_bridge.status()["capability"]["checked"])

    def test_gate_warning_is_emitted_only_once(self):
        """引擎每 5 分钟重查一次桥：同一条「缺方法」不能每次刷日志。"""
        import unittest.mock as mock

        bridge = self._ProbeBridge({"available": True, "methods": ["compose_context"]})
        with mock.patch.object(memory_bridge.logger, "warning") as warned:
            memory_bridge.find_memory_bridge(self._ctx(bridge))
            memory_bridge.find_memory_bridge(self._ctx(bridge))
            messages = [str(c.args[0]) for c in warned.call_args_list if c.args]
        gate_messages = [m for m in messages if "probe_capability_snapshot" in m]
        self.assertEqual(len(gate_messages), 1, f"能力门控告警被重复刷屏：{gate_messages}")


# ─── P2-15：超时 / 群聊跳过 / 读缓存 ───────────────────────


class TimeoutAndGroupTest(MemoryBridgeCase):
    class _SlowReader:
        async def compose_context(self, **kw):
            await asyncio.sleep(5.0)
            return BODY_TEXT

    class _SlowWriter:
        async def record_external_memory(self, **kw):
            await asyncio.sleep(5.0)
            return {"ok": True}

    async def test_read_timeout_degrades_to_no_memory(self):
        old = memory_bridge.READ_TIMEOUT_SECONDS
        memory_bridge.READ_TIMEOUT_SECONDS = 0.02
        try:
            text = await memory_bridge.fetch_memory_text(self._SlowReader(), "umo", "u1")
        finally:
            memory_bridge.READ_TIMEOUT_SECONDS = old
        self.assertEqual(text, "")
        self.assertIn("timeout", memory_bridge.last_error())
        self.assertEqual(memory_bridge.status()["read"]["timeout"], 1)

    async def test_write_timeout_degrades_and_is_counted(self):
        old = memory_bridge.WRITE_TIMEOUT_SECONDS
        memory_bridge.WRITE_TIMEOUT_SECONDS = 0.02
        try:
            await memory_bridge.write_proactive(self._SlowWriter(), "u1", "在吗", umo="umo")
        finally:
            memory_bridge.WRITE_TIMEOUT_SECONDS = old
        self.assertIn("timeout", memory_bridge.last_error())
        self.assertEqual(memory_bridge.status()["write"]["timeout"], 1)

    async def test_group_reads_are_skipped_entirely(self):
        for umo in (
            "aiocqhttp:group:999",
            "aiocqhttp:GroupMessage:999",
            "qq:guild:1",
            "qq:guildmessage:1",
        ):
            with self.subTest(umo=umo):
                memory_bridge.reset()
                bridge = BudgetedBridge()
                text = await memory_bridge.fetch_memory_text(bridge, umo, "u1")
                self.assertEqual(text, "")
                self.assertEqual(bridge.calls, [], f"{umo} 竟然真去检索了")
                self.assertEqual(memory_bridge.status()["read"]["skipped_group"], 1)

    async def test_private_and_unknown_umo_still_read(self):
        for umo in ("aiocqhttp:private:u1", "aiocqhttp:FriendMessage:u1", "umo"):
            with self.subTest(umo=umo):
                memory_bridge.reset()
                bridge = BudgetedBridge()
                text = await memory_bridge.fetch_memory_text(bridge, umo, "u1")
                self.assertIn("猫吐了", text)
                self.assertEqual(len(bridge.calls), 1)

    def test_docstrings_state_the_boundaries(self):
        """对接文档要写实：仅私聊生效 + 写入的 embedding 成本。"""
        self.assertIn("仅私聊", memory_bridge.fetch_memory_text.__doc__)
        self.assertIn("embedding", memory_bridge.write_proactive.__doc__)
        self.assertIn("embedding", memory_bridge.write_open_closed.__doc__)


class ReadCacheTest(MemoryBridgeCase):
    async def test_same_key_is_served_from_cache(self):
        bridge = BudgetedBridge()
        first = await memory_bridge.fetch_memory_text(
            bridge, "aiocqhttp:private:u1", "u1", query="面试"
        )
        second = await memory_bridge.fetch_memory_text(
            bridge, "aiocqhttp:private:u1", "u1", query="面试"
        )
        self.assertEqual(first, second)
        self.assertEqual(len(bridge.calls), 1, "同一 key 60s 内重复检索")
        self.assertEqual(memory_bridge.status()["read"]["cache_hit"], 1)

    async def test_ttl_is_60_seconds(self):
        self.assertEqual(memory_bridge.READ_CACHE_TTL_SECONDS, 60.0)

    async def test_different_query_or_budget_is_not_cached_together(self):
        bridge = BudgetedBridge()
        await memory_bridge.fetch_memory_text(bridge, "aiocqhttp:private:u1", "u1", query="面试")
        await memory_bridge.fetch_memory_text(bridge, "aiocqhttp:private:u1", "u1", query="猫")
        self.assertEqual(len(bridge.calls), 2)
        await memory_bridge.fetch_memory_text(
            bridge, "aiocqhttp:private:u1", "u1", query="面试", max_chars=1600
        )
        self.assertEqual(len(bridge.calls), 3, "预算不同不能共用缓存")

    async def test_expired_entry_is_refetched(self):
        bridge = BudgetedBridge()
        key = ("aiocqhttp:private:u1", "u1", "面试", memory_bridge.DEFAULT_MAX_CHARS)
        await memory_bridge.fetch_memory_text(bridge, key[0], "u1", query="面试")
        self.assertEqual(len(bridge.calls), 1)
        expires_at, cached_bridge, text = memory_bridge._READ_CACHE[key]
        memory_bridge._READ_CACHE[key] = (time.monotonic() - 1.0, cached_bridge, text)
        await memory_bridge.fetch_memory_text(bridge, key[0], "u1", query="面试")
        self.assertEqual(len(bridge.calls), 2, "过期缓存没有重新检索")

    async def test_a_different_bridge_does_not_reuse_the_cache(self):
        first = BudgetedBridge()
        second = BudgetedBridge()
        await memory_bridge.fetch_memory_text(first, "aiocqhttp:private:u1", "u1")
        await memory_bridge.fetch_memory_text(second, "aiocqhttp:private:u1", "u1")
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1, "桥换了（对面重载）仍命中旧缓存")


if __name__ == "__main__":
    unittest.main()
