"""v2.2.40：把「每道闸挂在哪些发送路径上」固化成测试。

这一轮之前已经踩过两次同类坑——闸写好了但没接到调用点（v2.2.22 的
`_dedupe_same_event_segments` 忘了 import、v2.2.29 的 `live_minutes` 传错函数），
两次都是 3000+ 条单测全绿、线上却静默失效。

这个文件不复用任何业务代码，只对源码做 AST 扫描：
每道闸至少要有一个调用点；几条关键路径各自必须带哪几道闸；连发补充只能发
**已经验收过**的段（`segments[1:]`）。
"""

from __future__ import annotations

import ast
import io
import unittest
from pathlib import Path

from social.verify import verify_hard_safety

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE
ENGINE = PLUGIN_ROOT / "social" / "engine.py"
VERIFY = PLUGIN_ROOT / "social" / "verify.py"
MAIN = PLUGIN_ROOT / "main.py"

# Proactive send checks are limited to hard safety and protocol integrity.
ENGINE_GATES = [
    "verify_hard_safety",
    "_verify_all_segments",
    "_group_proactive_issue",
    "_cap_group_segments",
    "_check_same_event",
]

REQUIRED = {
    "_icebreak": {"_verify_all_segments", "_group_proactive_issue", "_cap_group_segments"},
    "_flow_reply_inner": {"_verify_all_segments", "_cap_group_segments"},
    "_speak_inner": {"_verify_all_segments"},
}


def _lines_by_owner(path: Path, names) -> dict:
    """{函数名: {闸名: 最早出现行号}}，用于断言闸在发送/生成之前。"""
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    ranges = [
        (node.name, node.lineno, getattr(node, "end_lineno", node.lineno))
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def owner(line: int) -> str:
        best, span = "", 10 ** 9
        for name, lo, hi in ranges:
            if lo <= line <= hi and (hi - lo) < span:
                best, span = name, hi - lo
        return best

    out: dict = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
        if name in names:
            out.setdefault(owner(node.lineno), {}).setdefault(name, node.lineno)
    return out


def _calls_by_owner(path: Path):
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ranges.append((node.name, node.lineno, getattr(node, "end_lineno", node.lineno)))

    def owner(line: int) -> str:
        best, span = "", 10 ** 9
        for name, lo, hi in ranges:
            if lo <= line <= hi and (hi - lo) < span:
                best, span = name, hi - lo
        return best

    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
        if name:
            out.setdefault(name, set()).add(owner(node.lineno))
    return out, io.open(path, encoding="utf-8").read()


class GateMatrixTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.src = _calls_by_owner(ENGINE)

    def test_every_gate_has_a_caller(self):
        orphans = [g for g in ENGINE_GATES if not self.calls.get(g)]
        self.assertEqual(orphans, [], f"这些闸没有任何调用点：{orphans}")

    def test_key_paths_carry_their_gates(self):
        for path, gates in REQUIRED.items():
            used = {g for g in gates if path in self.calls.get(g, set())}
            self.assertEqual(used, gates, f"{path} 缺闸：{sorted(gates - used)}")

    def test_verify_internal_predicate_is_used_inside_verify(self):
        calls, _src = _calls_by_owner(VERIFY)
        self.assertTrue(calls.get("_same_event_text"), "verify.py 里没人用同事件判据")

    def test_hard_safety_allows_natural_short_speech_and_rejects_coercion(self):
        for text in ("早安", "我今天有点累，想先歇会儿。", "窗外开始下雨了。"):
            self.assertEqual(verify_hard_safety(text), (True, ""))

        for text in (
            "我等了一晚上你都不回，快回我一句。",
            "到底想怎样，直说。",
            "正常句子\nNO",
            "今天好累 (Model response will be rendered here)",
        ):
            self.assertFalse(verify_hard_safety(text)[0], text)

    def test_hard_safety_rejects_metadata_intent_species_and_overlength(self):
        self.assertFalse(verify_hard_safety(
            "#intent: 想表达此刻的状态\n今天天气不错。"
        )[0])
        self.assertFalse(verify_hard_safety(
            "我刚醒手还有点僵。", species="天马"
        )[0])
        self.assertFalse(verify_hard_safety("早安", max_length=1)[0])
        self.assertEqual(verify_hard_safety("早安", max_length=2), (True, ""))

    def test_ordinary_reply_path_is_not_rewritten_or_injected(self):
        text = io.open(MAIN, encoding="utf-8").read()
        for forbidden in (
            "@on_decorating_result",
            "engine.tidy_outgoing",
            "reply_echo_hint",
            "on_llm_request",
            "inject_proactive_context",
            "consume_pending_proactive_context",
            "extra_user_content_parts",
            "req.contexts",
        ):
            self.assertNotIn(forbidden, text)
        tree = ast.parse(text)
        cls = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "AutonomousSocial"
        )
        method = next(
            node for node in cls.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "tidy_outgoing"
        )
        self.assertEqual(len(method.body), 2)
        self.assertIsInstance(method.body[0], ast.Expr)
        self.assertIsInstance(method.body[0].value, ast.Constant)
        self.assertIsInstance(method.body[1], ast.Return)
    def test_gates_run_before_the_send(self):
        """闸必须在 `_send_with_retry` 之前——放后面等于先说话再检查。"""
        watched = [
            "_verify_all_segments",
            "_group_proactive_issue",
            "_cap_group_segments",
            "_send_with_retry",
        ]
        marks = _lines_by_owner(ENGINE, watched)
        for path in ("_icebreak", "_flow_reply_inner", "_speak_inner"):
            send_at = marks.get(path, {}).get("_send_with_retry")
            self.assertIsNotNone(send_at, f"{path} 里没找到 _send_with_retry")
            late = [
                (gate, at) for gate, at in marks.get(path, {}).items()
                if gate != "_send_with_retry" and at > send_at
            ]
            self.assertEqual(late, [], f"{path} 里这些闸排在发送之后：{late}")

    def test_cross_bot_guard_runs_before_generation(self):
        """v2.2.29 的教训：闸若排在 LLM 调用之后，会白烧一次模型调用。"""
        start = self.src.index("async def _icebreak(")
        end = self.src.find("\n    async def ", start + 10)
        body = self.src[start:end if end > 0 else len(self.src)]
        guard = body.find("group_cross_bot_gap_seconds")
        gen = body.find("_build_group_ctx(")
        self.assertGreater(guard, 0, "破冰里没有跨角色守卫")
        self.assertGreater(gen, 0, "破冰里没有 _build_group_ctx")
        self.assertLess(guard, gen, "跨角色守卫排到了生成之后")

    def test_burst_paths_only_send_verified_segments(self):
        # 连发补充只发 segments[1:]，也就是已经过 _cap_group_segments + 验收的那些段；
        # 群、私聊各一处。若有人在这里新生成文本，这条会红。
        schedule_lines = [
            ln for ln in self.src.splitlines()
            if ("_schedule_group_burst(" in ln or "_schedule_burst(" in ln)
            and not ln.lstrip().startswith(("def ", "async def "))
        ]
        self.assertEqual(len(schedule_lines), 3, "群 2 处 + 私聊 1 处")
        for ln in schedule_lines:
            self.assertIn("segments[1:]", ln, f"连发补充不是发已验证段：{ln.strip()}")


if __name__ == "__main__":
    unittest.main()
