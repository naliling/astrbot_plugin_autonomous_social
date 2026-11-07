# -*- coding: utf-8 -*-
"""v2.1.5：只读工具循环（social/tools.py §6）。

钉六件事：

1. 解析要容错但不多认：引号可无、标签大小写不敏感、夹带解释也能捞出来；坏 JSON 丢弃；
2. 上限：每轮最多 2 个调用，多出来的不执行；
3. 白名单：只认 memory.query / memory.sources，其余一律 failed/unsupported_action；
4. 语义分得清：没给执行能力是 unknown/no_provider（≠ 查了是空），执行异常也是 unknown；
5. 截断：单条结果最多 4000 字，回灌进提示词再压到 1200 字；
6. generator 接线：开关关闭时零额外调用、完全走旧路；开启时第 2 轮不再给工具，
   任何一环失败都回退旧路一次，绝不抛。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与其它测试同款，幂等） ───
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
    api.logger = logging.getLogger("autonomous_social_v215_tools_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import generator, tools  # noqa: E402


class TestParseToolCalls(unittest.TestCase):
    def test_single(self):
        text = '<tool name="memory.query">{"query":"面试","limit":2}</tool>'
        calls = tools.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["action"], "memory.query")
        self.assertEqual(calls[0]["args"]["query"], "面试")
        self.assertEqual(calls[0]["call_id"], "call_1")

    def test_two(self):
        text = (
            '<tool name="memory.query">{"query":"面试"}</tool>\n'
            '<tool name="memory.sources">{"limit":3}</tool>'
        )
        calls = tools.parse_tool_calls(text)
        self.assertEqual([c["action"] for c in calls], ["memory.query", "memory.sources"])
        self.assertEqual([c["call_id"] for c in calls], ["call_1", "call_2"])

    def test_three_keeps_two(self):
        text = "".join(
            '<tool name="memory.query">{"query":"q%d"}</tool>' % i for i in range(3)
        )
        self.assertEqual(len(tools.parse_tool_calls(text)), 2)

    def test_bad_json_dropped(self):
        text = '<tool name="memory.query">{不是 json}</tool>'
        self.assertEqual(tools.parse_tool_calls(text), [])

    def test_unquoted_name_and_case(self):
        text = '<TOOL NAME=memory.sources>{"limit":2}</TOOL>'
        calls = tools.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["action"], "memory.sources")

    def test_no_calls(self):
        self.assertEqual(tools.parse_tool_calls("在吗"), [])
        self.assertEqual(tools.parse_tool_calls(None), [])

    def test_args_wrapper(self):
        text = '<tool name="memory.query">{"args":{"query":"面试"}}</tool>'
        self.assertEqual(tools.parse_tool_calls(text)[0]["args"]["query"], "面试")

    def test_junk_around_json(self):
        text = '<tool name="memory.query">查一下 {"query":"面试"} 这样</tool>'
        self.assertEqual(tools.parse_tool_calls(text)[0]["args"]["query"], "面试")


class TestToolPrompt(unittest.TestCase):
    def test_render(self):
        prompt = tools.tool_prompt(
            [
                {"call_id": "call_1", "action": "memory.query", "state": "ok", "result": "他说周五面试"},
                {
                    "call_id": "call_2",
                    "action": "memory.sources",
                    "state": "unknown",
                    "error": "no_provider",
                },
            ]
        )
        self.assertIn("【上一轮工具结果】", prompt)
        self.assertIn("call_1", prompt)
        self.assertIn("state=ok", prompt)
        self.assertIn("state=unknown", prompt)
        self.assertIn("他说周五面试", prompt)
        self.assertIn("不要再说你要去查", prompt)

    def test_empty(self):
        self.assertEqual(tools.tool_prompt([]), "")

    def test_result_clipped(self):
        prompt = tools.tool_prompt(
            [{"call_id": "call_1", "action": "memory.query", "state": "ok", "result": "字" * 5000}]
        )
        self.assertLess(len(prompt), 1600)


class TestExecuteReadonly(unittest.TestCase):
    def test_query_callback(self):
        out = tools.execute_readonly(
            {"call_id": "call_1", "action": "memory.query", "args": {"query": "面试"}},
            {"query": lambda q, limit: ["他说周五面试"]},
        )
        self.assertEqual(out["state"], "ok")
        self.assertIn("他说周五面试", out["result"])

    def test_query_single_arg_callback(self):
        out = tools.execute_readonly(
            {"action": "memory.query", "args": {"query": "面试"}},
            {"query": lambda q: "他说周五面试"},
        )
        self.assertEqual(out["state"], "ok")
        self.assertIn("周五", out["result"])

    def test_query_memories_filter(self):
        out = tools.execute_readonly(
            {"action": "memory.query", "args": {"query": "面试"}},
            {"memories": ["他说周五面试", "今天下雨了"]},
        )
        self.assertEqual(out["state"], "ok")
        self.assertIn("面试", out["result"])
        self.assertNotIn("下雨", out["result"])

    def test_sources_list(self):
        out = tools.execute_readonly(
            {"action": "memory.sources", "args": {"limit": 2}},
            {"sources": [{"title": "私聊"}, {"title": "群聊"}, {"title": "第三"}]},
        )
        self.assertEqual(out["state"], "ok")
        self.assertIn("私聊", out["result"])
        self.assertNotIn("第三", out["result"])

    def test_unsupported_action(self):
        out = tools.execute_readonly({"action": "memory.write", "args": {}}, {})
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["error"], "unsupported_action")

    def test_no_provider(self):
        # 关键语义：没能力 ≠ 查了是空。
        out = tools.execute_readonly({"action": "memory.query", "args": {"query": "x"}}, {})
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["error"], "no_provider")

    def test_exception_is_unknown(self):
        def boom(q, limit):
            raise RuntimeError("db down")

        out = tools.execute_readonly(
            {"action": "memory.query", "args": {"query": "x"}}, {"query": boom}
        )
        self.assertEqual(out["state"], "unknown")
        self.assertNotEqual(out["error"], "")

    def test_truncated(self):
        out = tools.execute_readonly(
            {"action": "memory.query", "args": {"query": "x"}},
            {"query": lambda q, limit: "字" * 9000},
        )
        self.assertEqual(out["state"], "ok")
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["result"]), tools.MAX_RESULT_CHARS)

    def test_dirty_call(self):
        self.assertEqual(tools.execute_readonly(None, None)["state"], "failed")


class TestGenerateWithTools(unittest.IsolatedAsyncioTestCase):
    def _gen(self):
        return generator.MessageGenerator(None)

    async def test_disabled_uses_legacy_and_no_llm(self):
        gen = self._gen()
        llm_calls = []
        compose_calls = []

        async def fake_llm(provider, prompt, max_tokens=None):
            llm_calls.append(prompt)
            return "不该被调用"

        async def fake_compose(*args, **kwargs):
            compose_calls.append(1)
            return ("p", "PROMPT", 120, False)

        async def legacy():
            return ["旧路"]

        gen._call_llm = fake_llm
        gen._compose = fake_compose
        out = await gen.generate_with_tools(
            "umo", {}, "ctx", "reason", enabled=False, legacy=legacy
        )
        self.assertEqual(out, ["旧路"])
        self.assertEqual(llm_calls, [])
        self.assertEqual(compose_calls, [])

    async def test_default_enabled_matches_engine_contract(self):
        # 钉住与 engine.py 的接线契约：engine 只在 memory_tools_enabled 为真时才调用，
        # 且调用时不传 enabled；此时必须真的走工具循环，不能静默退回旧路。
        gen = self._gen()
        prompts = []
        first = '<tool name="memory.query">{"query":"面试"}</tool>'

        async def fake_llm(provider, prompt, max_tokens=None):
            prompts.append(prompt)
            return first if len(prompts) == 1 else "好"

        async def fake_compose(*args, **kwargs):
            return ("p", "BASE", 120, False)

        async def legacy():
            raise AssertionError("开关打开时不该退回旧路")

        gen._call_llm = fake_llm
        gen._compose = fake_compose
        out = await gen.generate_with_tools(
            "umo", {}, "ctx", "reason",
            memory_query=lambda q: ["他说周五面试"],
            legacy=legacy,
        )
        self.assertEqual(2, len(prompts))
        self.assertEqual(["好"], out)

    async def test_two_rounds(self):
        gen = self._gen()
        prompts = []
        first = '<tool name="memory.query">{"query":"面试","limit":2}</tool>'
        second = "那你面试加油呀"

        async def fake_llm(provider, prompt, max_tokens=None):
            prompts.append(prompt)
            return first if len(prompts) == 1 else second

        async def fake_compose(*args, **kwargs):
            return ("p", "BASE PROMPT", 120, False)

        async def legacy():
            raise AssertionError("工具循环成功时不该回退")

        gen._call_llm = fake_llm
        gen._compose = fake_compose
        out = await gen.generate_with_tools(
            "umo", {}, "ctx", "reason",
            memory_query=lambda q, limit: ["他说周五面试"],
            enabled=True,
            legacy=legacy,
        )
        self.assertEqual(len(prompts), 2)
        self.assertIn("【可以查一次记忆】", prompts[0])
        self.assertIn("【上一轮工具结果】", prompts[1])
        self.assertIn("不要再调用工具", prompts[1])
        self.assertNotIn("【可以查一次记忆】", prompts[1])
        self.assertTrue(out and out[0].startswith("那你面试加油"))

    async def test_no_tool_call_returns_text(self):
        gen = self._gen()

        async def fake_llm(provider, prompt, max_tokens=None):
            return "直接回复就好"

        async def fake_compose(*args, **kwargs):
            return ("p", "BASE", 120, False)

        gen._call_llm = fake_llm
        gen._compose = fake_compose
        out = await gen.generate_with_tools("umo", {}, "ctx", "reason", enabled=True)
        self.assertEqual(out, ["直接回复就好"])

    async def test_first_empty_falls_back(self):
        gen = self._gen()

        async def fake_llm(provider, prompt, max_tokens=None):
            return None

        async def fake_compose(*args, **kwargs):
            return ("p", "BASE", 120, False)

        async def legacy():
            return ["回退"]

        gen._call_llm = fake_llm
        gen._compose = fake_compose
        out = await gen.generate_with_tools("umo", {}, "ctx", "reason", enabled=True, legacy=legacy)
        self.assertEqual(out, ["回退"])

    async def test_exception_falls_back(self):
        gen = self._gen()

        async def fake_llm(provider, prompt, max_tokens=None):
            raise RuntimeError("boom")

        async def fake_compose(*args, **kwargs):
            return ("p", "BASE", 120, False)

        async def legacy():
            return ["回退"]

        gen._call_llm = fake_llm
        gen._compose = fake_compose
        out = await gen.generate_with_tools("umo", {}, "ctx", "reason", enabled=True, legacy=legacy)
        self.assertEqual(out, ["回退"])


if __name__ == "__main__":
    unittest.main()
