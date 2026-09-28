"""不依赖运行中服务的自测：事件解析、SSE 分帧、算术桩、插件 lint。

    python3 -m unittest discover -s tests -v
"""

import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astrbot_testkit.events import (  # noqa: E402
    TurnAccumulator,
    extract_tool_call,
    extract_tool_result,
)
from astrbot_testkit.client import AstrBotClient  # noqa: E402
from astrbot_testkit.config import Config  # noqa: E402
from astrbot_testkit.lint import lint_plugin  # noqa: E402
from astrbot_testkit.mock import _eval_arith  # noqa: E402


class TestToolCallExtraction(unittest.TestCase):
    def test_openai_nested_with_string_arguments(self):
        call = extract_tool_call({
            "id": "call_1", "type": "function",
            "function": {"name": "testkit_echo",
                         "arguments": '{"text": "hi", "n": 3}'},
        })
        self.assertIsNotNone(call)
        self.assertEqual(call.name, "testkit_echo")
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.arguments, {"text": "hi", "n": 3})

    def test_flat_style(self):
        call = extract_tool_call({
            "id": "call_2", "name": "testkit_add", "arguments": {"a": 1, "b": 2},
        })
        self.assertEqual(call.name, "testkit_add")
        self.assertEqual(call.arguments["a"], 1)

    def test_parameters_alias_and_tool_key(self):
        call = extract_tool_call({"id": "x", "tool_name": "t1",
                                  "parameters": '{"k": "v"}'})
        self.assertEqual(call.name, "t1")
        self.assertEqual(call.arguments, {"k": "v"})

        nested = extract_tool_call({"id": "y", "tool": {"name": "t2",
                                                        "arguments": {"z": 1}}})
        self.assertEqual(nested.name, "t2")
        self.assertEqual(nested.arguments, {"z": 1})

    def test_malformed_arguments_do_not_raise(self):
        call = extract_tool_call({"id": "z", "name": "t", "arguments": "{不是json"})
        self.assertEqual(call.arguments, {})

    def test_missing_name_returns_none(self):
        self.assertIsNone(extract_tool_call({"id": "z", "arguments": "{}"}))
        self.assertIsNone(extract_tool_call({}))

    def test_tool_result(self):
        res = extract_tool_result({"id": "call_1", "result": "ok"})
        self.assertEqual(res.result, "ok")
        self.assertEqual(res.text(), "ok")

        dict_res = extract_tool_result({"id": "c", "result": {"a": 1}})
        self.assertIn('"a": 1', dict_res.text())


class TestAccumulator(unittest.TestCase):
    def _feed(self, events):
        acc = TurnAccumulator(user_message="q")
        for e in events:
            acc.feed(e)
        return acc.finish()

    def test_full_tool_call_sequence(self):
        turn = self._feed([
            {"type": "session_id", "data": None, "session_id": "s1"},
            {"type": "user_message_saved", "data": {"id": "m1"}},
            {"type": "plain", "data": "先想想", "chain_type": "reasoning"},
            {"type": "plain",
             "data": json.dumps({"id": "c1", "name": "testkit_echo",
                                 "arguments": {"text": "x"}}),
             "chain_type": "tool_call"},
            {"type": "plain",
             "data": json.dumps({"id": "c1", "result": "回显"}),
             "chain_type": "tool_call_result"},
            {"type": "plain", "data": "最终", "chain_type": None},
            {"type": "agent_stats", "data": {"running_time": 1.0}},
            {"type": "message_saved", "data": {"id": "m2"}},
            {"type": "end", "data": ""},
        ])
        self.assertEqual(turn.session_id, "s1")
        self.assertEqual(turn.reasoning, "先想想")
        self.assertEqual(turn.text, "最终")
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(len(turn.tool_results), 1)
        self.assertEqual(turn.agent_stats["running_time"], 1.0)
        self.assertEqual(turn.error, "")
        self.assertEqual(turn.results_for(turn.tool_calls[0])[0].result, "回显")

    def test_error_event_captured(self):
        turn = self._feed([{"type": "error", "data": "WebChat run failed"}])
        self.assertFalse(turn.ok)
        self.assertIn("WebChat run failed", turn.error)

    def test_attachment_events(self):
        turn = self._feed([
            {"type": "attachment_saved", "data": {"id": "a1", "type": "image"}},
            {"type": "image", "data": "[IMAGE] /tmp/x.png"},
        ])
        self.assertEqual(len(turn.attachments), 2)

    def test_unknown_events_are_ignored(self):
        turn = self._feed([{"type": "brand_new_event", "data": {"x": 1}}])
        self.assertEqual(turn.text, "")
        self.assertEqual(turn.tool_calls, [])


class TestSSEParsing(unittest.TestCase):
    def test_parses_lines_and_skips_heartbeats(self):
        raw = (
            b": heartbeat\n"
            b"\n"
            b'data: {"type": "session_id", "session_id": "s9"}\n'
            b"\n"
            b'data: {"type": "plain", "data": "hi"}\n'
            b"\n"
            b"data: {bad json}\n"
            b"\n"
            b'data: [{"type": "end"}]\n'
            b"\n"
        )
        events = list(AstrBotClient._parse_sse(io.BytesIO(raw)))
        self.assertEqual(events[0]["session_id"], "s9")
        self.assertEqual(events[1]["data"], "hi")
        self.assertEqual(events[2]["type"], "end")

    def test_sse_line_ending_variants(self):
        raw = b'data: {"type": "end"}\r\n\r\n'
        events = list(AstrBotClient._parse_sse(io.BytesIO(raw)))
        self.assertEqual(events, [{"type": "end"}])


class TestConfig(unittest.TestCase):
    def test_headers_masking(self):
        cfg = Config(base_url="http://x/", api_key="abk_secret_value_1234")
        self.assertTrue(cfg.headers()["Authorization"].startswith("Bearer "))
        self.assertNotIn("secret_value", cfg.masked_key())
        self.assertIn("1234", cfg.masked_key())

    def test_chat_options_omit_empty(self):
        cfg = Config(provider="p", model="")
        self.assertEqual(cfg.chat_options(), {"selected_provider": "p"})


class TestSafeArith(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(_eval_arith("1+1"), 2)
        self.assertEqual(_eval_arith("17 + 25"), 42)
        self.assertEqual(_eval_arith("3*4"), 12)
        self.assertEqual(_eval_arith("10/4"), 2.5)
        self.assertEqual(_eval_arith("-5+2"), -3)

    def test_rejects_code_execution(self):
        for expr in ("__import__('os').system('id')", "open('/etc/passwd')",
                     "1; import os", "print(1)", "a.b", "'x' + 1"):
            self.assertIsNone(_eval_arith(expr), f"不应求值成功: {expr}")


class TestLint(unittest.TestCase):
    def test_valid_plugin_passes(self):
        plugin = Path(__file__).resolve().parent.parent / \
            "astrbot_testkit" / "plugin" / "astrbot_plugin_testkit" / "main.py"
        self.assertEqual(lint_plugin(plugin), [])

    def test_detects_bad_definitions(self):
        bad = Path(__file__).parent / "fixtures" / "bad_tool.py"
        problems = lint_plugin(bad)
        joined = " | ".join(problems)
        self.assertTrue(problems)
        self.assertIn("不在白名单内", joined)
        self.assertIn("缺少 'Args:' 块", joined)
        self.assertIn("未在 Args 块中声明", joined)
        self.assertIn("缺少 docstring", joined)


if __name__ == "__main__":
    unittest.main()
