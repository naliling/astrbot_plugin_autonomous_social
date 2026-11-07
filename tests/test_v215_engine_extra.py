# -*- coding: utf-8 -*-
"""v2.1.5 接线补丁测试：工具循环的记忆查询口 + 成稿事实核对（task-24）。

为什么单独一个文件：这两个都是**接线**缺口，不是新规则。
① generate_with_tools 之前没传 memory_query / memory_sources，工具层只能报
   unknown/no_provider——开关打开等于没用；
② generator.grounded_claims 一直是死代码（engine 还在用 guards.evidence_missing）。
模块自己的测试在 test_v215_tools.py / test_v215_review.py，这里只验 engine 有没有
真的把数据送进去、以及核对不通过时是不是「只记账不丢消息」。

所有依赖都是假对象：容器里没有 astrbot、没有 Core 包也能跑。
"""

import asyncio
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# 插件根：打包后 tests/ 在插件里；开发时 tests/ 与 social/ 同级。两种摆法都认。
_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub():
    """容器里没有 astrbot，而 social/ 每个模块都 from astrbot.api import logger。"""
    import logging

    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("social_v215_extra_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import engine as engine_mod  # noqa: E402
from social import tools as tools_mod  # noqa: E402
from social.engine import SocialEngine  # noqa: E402

NOW = 1_700_000_000.0

# 收尾提示会被追加进 memory_note；工具快照必须是追加前的那份。
MEMORY_TEXT = "她养了只橘猫，叫团子。上周刚搬完家。"
CLOSING_NOTE = "【对话收尾】对方在道晚安，简短收尾，不要再提问。"


# ─── 假对象 ──────────────────────────────────────────────────────
class _Recorder:
    def __init__(self):
        self.notes = []
        self.blocks = []

    def _blocked(self, code, text, **detail):
        self.blocks.append((code, text, detail))
        return False, text

    def _note_blocked(self, code, text, **detail):
        self.notes.append((code, text, detail))

    def log(self, message):
        pass

    def codes(self):
        return [code for code, _text, _detail in self.notes]


class _FakeGenerator:
    def __init__(self, parts=None, tools=None, tools_error=None, review=None, review_error=None):
        self.parts = parts
        self.tools = tools
        self.tools_error = tools_error
        self.review = review
        self.review_error = review_error
        self.provider = "fake-provider"
        self.generate_calls = []
        self.tools_calls = []
        self.review_calls = []

    async def generate(self, *args, **kwargs):
        self.generate_calls.append((args, kwargs))
        return list(self.parts or [])

    async def generate_with_tools(self, *args, **kwargs):
        self.tools_calls.append((args, kwargs))
        if self.tools_error:
            raise self.tools_error
        return self.tools

    def review_draft(self, *args, **kwargs):
        self.review_calls.append((args, kwargs))
        if self.review_error:
            raise self.review_error
        return self.review


_NEW_METHODS = (
    "_memory_tool_ctx",
    "_generate_parts",
    "_review_draft",
    "_note_grounded",
)


def _engine(**over):
    recorder = _Recorder()
    cfg = over.pop("cfg", SimpleNamespace())
    obj = SimpleNamespace(
        cfg=cfg,
        generator=over.pop("generator", _FakeGenerator()),
        _memory_bridge=over.pop("memory_bridge", lambda: None),
        log=recorder.log,
        _blocked=recorder._blocked,
        _note_blocked=recorder._note_blocked,
        _recorder=recorder,
    )
    for key, value in over.items():
        setattr(obj, key, value)
    for name in _NEW_METHODS:
        setattr(obj, name, types.MethodType(getattr(SocialEngine, name), obj))
    return obj


def _run(coro):
    return asyncio.run(coro)


def _cfg(**over):
    base = SimpleNamespace(memory_tools_enabled=True, draft_review_enabled=True)
    for key, value in over.items():
        setattr(base, key, value)
    return base


def _grounded_part(cards):
    """一条能对上证据卡的正文（时间+数字，会被 _claimed_facts 当成声称）。"""
    return ["明天 23:00 见。"]


# ─── 一、记忆查询口（_memory_tool_ctx）───────────────────────────
class MemoryToolCtxTest(unittest.TestCase):
    def test_no_bridge_returns_empty_ctx(self):
        eng = _engine(memory_bridge=lambda: None)
        self.assertEqual({}, eng._memory_tool_ctx(MEMORY_TEXT))

    def test_bridge_present_exposes_sync_query(self):
        eng = _engine(memory_bridge=lambda: object())
        ctx = eng._memory_tool_ctx(MEMORY_TEXT)
        self.assertIn("query", ctx)
        self.assertTrue(callable(ctx["query"]))
        self.assertEqual(MEMORY_TEXT, ctx["query"]())

    def test_query_accepts_query_and_limit_positional(self):
        eng = _engine(memory_bridge=lambda: object())
        ctx = eng._memory_tool_ctx(MEMORY_TEXT)
        # tools._run_query 先按 (query, limit) 调，TypeError 才退回单参。
        self.assertEqual(MEMORY_TEXT, ctx["query"]("猫", 5))

    def test_sources_single_card_with_title_and_source(self):
        eng = _engine(memory_bridge=lambda: object())
        ctx = eng._memory_tool_ctx(MEMORY_TEXT)
        self.assertEqual(1, len(ctx["sources"]))
        card = ctx["sources"][0]
        self.assertEqual("长期记忆", card["title"])
        self.assertEqual("memory_companion", card["source"])
        self.assertIn("团子", card["detail"])

    def test_sources_detail_truncated_to_600(self):
        eng = _engine(memory_bridge=lambda: object())
        ctx = eng._memory_tool_ctx("字" * 900)
        self.assertEqual(600, len(ctx["sources"][0]["detail"]))

    def test_empty_memory_yields_empty_sources(self):
        eng = _engine(memory_bridge=lambda: object())
        ctx = eng._memory_tool_ctx("")
        self.assertEqual([], ctx["sources"])
        self.assertEqual("", ctx["query"]())

    def test_bridge_lookup_error_is_fail_open(self):
        def _boom():
            raise RuntimeError("桥炸了")

        eng = _engine(memory_bridge=_boom)
        self.assertEqual({}, eng._memory_tool_ctx(MEMORY_TEXT))

    def test_ctx_feeds_execute_readonly_memory_query(self):
        eng = _engine(memory_bridge=lambda: object())
        ctx = eng._memory_tool_ctx(MEMORY_TEXT)
        out = tools_mod.execute_readonly(
            {"call_id": "c1", "action": "memory.query", "args": {"query": "猫"}}, ctx
        )
        self.assertEqual("ok", out["state"])
        self.assertIn("团子", out["result"])

    def test_ctx_feeds_execute_readonly_memory_sources(self):
        eng = _engine(memory_bridge=lambda: object())
        ctx = eng._memory_tool_ctx(MEMORY_TEXT)
        out = tools_mod.execute_readonly(
            {"call_id": "c2", "action": "memory.sources", "args": {}}, ctx
        )
        self.assertEqual("ok", out["state"])

    def test_empty_memory_query_still_reports_ok(self):
        eng = _engine(memory_bridge=lambda: object())
        ctx = eng._memory_tool_ctx("")
        out = tools_mod.execute_readonly(
            {"call_id": "c3", "action": "memory.query", "args": {"query": "猫"}}, ctx
        )
        # 「查了，但这份快照里没有」是 ok；不能报 unknown 让模型以为工具坏了。
        self.assertEqual("ok", out["state"])
        self.assertEqual("", out["result"])

    def test_no_bridge_reports_no_provider(self):
        eng = _engine(memory_bridge=lambda: None)
        ctx = eng._memory_tool_ctx(MEMORY_TEXT)
        out = tools_mod.execute_readonly(
            {"call_id": "c4", "action": "memory.query", "args": {"query": "猫"}}, ctx
        )
        self.assertEqual("unknown", out["state"])
        self.assertEqual("no_provider", out["error"])


# ─── 二、工具调用接线（_generate_parts）──────────────────────────
class ToolWiringTest(unittest.TestCase):
    def test_tools_off_goes_straight_to_generate(self):
        gen = _FakeGenerator(parts=["在"], tools=["不该用"])
        eng = _engine(generator=gen, cfg=_cfg(memory_tools_enabled=False))
        out = _run(eng._generate_parts(
            umo="u", target="t", core_context="c", reason="r", reason_meta=None,
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0,
            memory_note=MEMORY_TEXT, bid="b1",
        ))
        self.assertEqual(["在"], out)
        self.assertEqual(1, len(gen.generate_calls))
        self.assertEqual([], gen.tools_calls)

    def test_tools_on_without_bridge_passes_none(self):
        gen = _FakeGenerator(tools=["在"])
        eng = _engine(generator=gen, cfg=_cfg(memory_tools_enabled=True), memory_bridge=lambda: None)
        out = _run(eng._generate_parts(
            umo="u", target="t", core_context="c", reason="r", reason_meta=None,
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0,
            memory_note=MEMORY_TEXT, bid="b1",
        ))
        self.assertEqual(["在"], out)
        kwargs = gen.tools_calls[0][1]
        # 不传能力，让工具层如实报 no_provider，而不是喂一份假记忆。
        self.assertIsNone(kwargs["memory_query"])
        self.assertIsNone(kwargs["memory_sources"])

    def test_tools_on_with_bridge_passes_query_and_sources(self):
        gen = _FakeGenerator(tools=["在"])
        eng = _engine(generator=gen, cfg=_cfg(memory_tools_enabled=True), memory_bridge=lambda: object())
        _run(eng._generate_parts(
            umo="u", target="t", core_context="c", reason="r", reason_meta=None,
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0,
            memory_note=MEMORY_TEXT, bid="b1",
        ))
        kwargs = gen.tools_calls[0][1]
        self.assertTrue(callable(kwargs["memory_query"]))
        self.assertEqual(MEMORY_TEXT, kwargs["memory_query"]())
        self.assertEqual(1, len(kwargs["memory_sources"]))

    def test_memory_raw_wins_over_note_with_closing_hint(self):
        gen = _FakeGenerator(tools=["在"])
        eng = _engine(generator=gen, cfg=_cfg(memory_tools_enabled=True), memory_bridge=lambda: object())
        note = MEMORY_TEXT + "\n" + CLOSING_NOTE
        _run(eng._generate_parts(
            umo="u", target="t", core_context="c", reason="r", reason_meta=None,
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0,
            memory_note=note, memory_raw=MEMORY_TEXT, bid="b1",
        ))
        kwargs = gen.tools_calls[0][1]
        self.assertEqual(MEMORY_TEXT, kwargs["memory_query"]())
        self.assertNotIn("对话收尾", kwargs["memory_sources"][0]["detail"])
        # 写作层照旧拿到完整 note（含收尾提示），只有工具快照被剥掉。
        self.assertEqual(note, kwargs["memory_note"])

    def test_memory_raw_defaults_to_note(self):
        gen = _FakeGenerator(tools=["在"])
        eng = _engine(generator=gen, cfg=_cfg(memory_tools_enabled=True), memory_bridge=lambda: object())
        _run(eng._generate_parts(
            umo="u", target="t", core_context="c", reason="r", reason_meta=None,
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0,
            memory_note=MEMORY_TEXT, bid="b1",
        ))
        kwargs = gen.tools_calls[0][1]
        self.assertEqual(MEMORY_TEXT, kwargs["memory_query"]())

    def test_tools_raising_falls_back_to_generate(self):
        gen = _FakeGenerator(parts=["兜底"], tools_error=RuntimeError("工具炸了"))
        eng = _engine(generator=gen, cfg=_cfg(memory_tools_enabled=True), memory_bridge=lambda: object())
        out = _run(eng._generate_parts(
            umo="u", target="t", core_context="c", reason="r", reason_meta=None,
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0,
            memory_note=MEMORY_TEXT, bid="b1",
        ))
        self.assertEqual(["兜底"], out)
        self.assertEqual(1, len(gen.generate_calls))

    def test_tool_round_cap_is_noted(self):
        gen = _FakeGenerator(tools={"parts": ["在"], "rounds_capped": True})
        eng = _engine(generator=gen, cfg=_cfg(memory_tools_enabled=True), memory_bridge=lambda: object())
        out = _run(eng._generate_parts(
            umo="u", target="t", core_context="c", reason="r", reason_meta=None,
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0,
            memory_note=MEMORY_TEXT, bid="b1",
        ))
        self.assertEqual(["在"], out)
        self.assertIn("tool_round_cap", eng._recorder.codes())


# ─── 三、成稿事实核对（_review_draft + _note_grounded）───────────
class GroundedReviewTest(unittest.TestCase):
    def _review(self, parts, cards, gen=None, cfg=None, decision=None):
        gen = gen or _FakeGenerator(review={"verdict": "ok"})
        eng = _engine(generator=gen, cfg=cfg or _cfg())
        out = _run(eng._review_draft(parts, decision, cards))
        return out, eng

    def test_ungrounded_is_noted_but_message_kept(self):
        out, eng = self._review(["明天 23:00 见。"], [{"title": "天气", "detail": "昨天晴"}])
        self.assertEqual(["明天 23:00 见。"], out)
        self.assertEqual(["draft_ungrounded"], eng._recorder.codes())

    def test_note_carries_ratio_and_missing(self):
        out, eng = self._review(["明天 23:00 见。"], [{"title": "天气", "detail": "昨天晴"}])
        _code, _text, detail = eng._recorder.notes[0]
        self.assertEqual(0.0, detail["ratio"])
        self.assertIn("23:00", detail["missing"])

    def test_grounded_text_is_not_noted(self):
        cards = [{"title": "约定", "detail": "明天 23:00 见"}]
        out, eng = self._review(["明天 23:00 见。"], cards)
        self.assertEqual(["明天 23:00 见。"], out)
        self.assertEqual([], eng._recorder.codes())

    def test_no_cards_skips_grounding(self):
        out, eng = self._review(["明天 23:00 见。"], [])
        self.assertEqual(["明天 23:00 见。"], out)
        self.assertEqual([], eng._recorder.codes())

    def test_grounding_runs_even_when_review_disabled(self):
        gen = _FakeGenerator()
        out, eng = self._review(
            ["明天 23:00 见。"],
            [{"title": "天气", "detail": "昨天晴"}],
            gen=gen,
            cfg=_cfg(draft_review_enabled=False),
        )
        self.assertEqual(["明天 23:00 见。"], out)
        self.assertEqual([], gen.review_calls)
        self.assertEqual(["draft_ungrounded"], eng._recorder.codes())

    def test_drop_returns_none_and_skips_grounding(self):
        gen = _FakeGenerator(review={"verdict": "drop", "why": "重复"})
        out, eng = self._review(
            ["明天 23:00 见。"], [{"title": "天气", "detail": "昨天晴"}], gen=gen
        )
        self.assertIsNone(out)
        self.assertEqual([], eng._recorder.codes())

    def test_rewrite_is_reviewed_and_grounded(self):
        gen = _FakeGenerator(review={"verdict": "rewrite", "text": "明天 23:00 见。"})
        out, eng = self._review(
            ["原稿"], [{"title": "天气", "detail": "昨天晴"}], gen=gen
        )
        self.assertEqual(["明天 23:00 见。"], out)
        self.assertEqual(["draft_ungrounded"], eng._recorder.codes())

    def test_rewrite_grounded_passes_silently(self):
        gen = _FakeGenerator(review={"verdict": "rewrite", "text": "明天 23:00 见。"})
        out, eng = self._review(
            ["原稿"], [{"title": "约定", "detail": "明天 23:00 见"}], gen=gen
        )
        self.assertEqual(["明天 23:00 见。"], out)
        self.assertEqual([], eng._recorder.codes())

    def test_review_error_is_fail_open_and_grounded_still_runs(self):
        gen = _FakeGenerator(review_error=RuntimeError("自评炸了"))
        out, eng = self._review(
            ["明天 23:00 见。"], [{"title": "天气", "detail": "昨天晴"}], gen=gen
        )
        self.assertEqual(["明天 23:00 见。"], out)
        self.assertEqual(["draft_ungrounded"], eng._recorder.codes())

    def test_review_non_dict_result_keeps_parts(self):
        gen = _FakeGenerator(review="我觉得还行")
        out, _eng = self._review(["好的"], [], gen=gen)
        self.assertEqual(["好的"], out)

    def test_empty_parts_short_circuits(self):
        gen = _FakeGenerator(review={"verdict": "drop"})
        out, _eng = self._review([], [], gen=gen)
        self.assertEqual([], out)
        self.assertEqual([], gen.review_calls)

    def test_decision_none_passes_none_plan(self):
        gen = _FakeGenerator(review={"verdict": "ok"})
        self._review(["好的"], [], gen=gen, decision=None)
        args = gen.review_calls[0][0]
        self.assertEqual("fake-provider", args[0])
        self.assertIsNone(args[2])

    def test_grounded_module_missing_is_silent(self):
        with mock.patch.object(engine_mod, "_opt_module", return_value=None):
            out, eng = self._review(["明天 23:00 见。"], [{"title": "天气", "detail": "昨天晴"}])
        self.assertEqual(["明天 23:00 见。"], out)
        self.assertEqual([], eng._recorder.codes())

    def test_grounded_raising_is_silent(self):
        fake = types.SimpleNamespace(grounded_claims=mock.Mock(side_effect=RuntimeError("核对炸了")))
        with mock.patch.object(engine_mod, "_opt_module", return_value=fake):
            out, eng = self._review(["明天 23:00 见。"], [{"title": "天气", "detail": "昨天晴"}])
        self.assertEqual(["明天 23:00 见。"], out)
        self.assertEqual([], eng._recorder.codes())

    def test_grounded_non_dict_result_is_silent(self):
        fake = types.SimpleNamespace(grounded_claims=lambda text, cards: "???")
        with mock.patch.object(engine_mod, "_opt_module", return_value=fake):
            out, eng = self._review(["明天 23:00 见。"], [{"title": "天气", "detail": "昨天晴"}])
        self.assertEqual(["明天 23:00 见。"], out)
        self.assertEqual([], eng._recorder.codes())

    def test_missing_list_is_capped_at_three_and_120_chars(self):
        claims = ["第%d号 23:0%d 见" % (i, i) for i in range(1, 6)]
        text = "。".join(claims) + "。"
        out, eng = self._review([text], [{"title": "天气", "detail": "昨天晴"}])
        self.assertEqual([text], out)
        _code, _text, detail = eng._recorder.notes[0]
        parts = detail["missing"].split("；")
        self.assertEqual(3, len(parts))
        self.assertTrue(all(len(part) <= 120 for part in parts))

    def test_grounding_failure_never_blocks_message(self):
        # 这条是「只记不拦」的总闸：不管核对怎么不通过，正文都必须原样回去。
        out, eng = self._review(["明天 23:00 见。"], [{"title": "天气", "detail": "昨天晴"}])
        self.assertEqual(["明天 23:00 见。"], out)
        self.assertEqual([], eng._recorder.blocks)


if __name__ == "__main__":
    unittest.main()
