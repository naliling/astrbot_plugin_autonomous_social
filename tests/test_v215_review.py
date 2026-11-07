# -*- coding: utf-8 -*-
"""v2.1.5：生成后自评与证据校验（social/generator.py §5）。

钉五件事：

1. 自评是受约束选择题：只认 ok / rewrite / drop，别名归一（pass→ok、fix→rewrite）；
2. fail-open 是硬要求：调用异常、输出解析不了、开关关闭、空正文，一律 {"verdict": "ok"}，
   自评坏了不能连累发送；
3. rewrite 必须带正文：说改却不给正文，按 ok 放行（它其实没想改）；
4. 证据校验只盯「声称的具体事实」（数字 / 时间词 / 引号书名号）：无卡放行、无声称放行，
   一半以上有出处才算 ok；
5. 脏输入不抛：None、空串、非列表卡片都当放行。
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
    api.logger = logging.getLogger("autonomous_social_v215_review_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import generator  # noqa: E402


class TestParseReview(unittest.TestCase):
    def test_plain_ok(self):
        self.assertEqual(generator.parse_review('{"verdict":"ok"}')["verdict"], "ok")

    def test_rewrite_with_text(self):
        parsed = generator.parse_review(
            '{"verdict":"rewrite","text":"改好了","reason":"编了时间"}'
        )
        self.assertEqual(parsed["verdict"], "rewrite")
        self.assertEqual(parsed["text"], "改好了")
        self.assertEqual(parsed["reason"], "编了时间")

    def test_drop(self):
        parsed = generator.parse_review('{"verdict":"drop","reason":"时机不对"}')
        self.assertEqual(parsed["verdict"], "drop")

    def test_aliases_normalized(self):
        # 模型换个词说同一件事，不算没按要求做。
        self.assertEqual(generator.parse_review('{"verdict":"pass"}')["verdict"], "ok")
        parsed = generator.parse_review('{"verdict":"FIX","text":"x"}')
        self.assertEqual(parsed["verdict"], "rewrite")
        self.assertEqual(generator.parse_review('{"verdict":"reject"}')["verdict"], "drop")

    def test_unknown_verdict_is_none(self):
        self.assertIsNone(generator.parse_review('{"verdict":"maybe"}'))

    def test_garbage_is_none(self):
        self.assertIsNone(generator.parse_review("我觉得还行吧"))
        self.assertIsNone(generator.parse_review(""))
        self.assertIsNone(generator.parse_review(None))

    def test_think_block_and_fence(self):
        fence = chr(96) * 3
        raw = fence + 'json\n<think>想一想</think>\n{"verdict":"ok"}\n' + fence
        self.assertEqual(generator.parse_review(raw)["verdict"], "ok")

    def test_rewrite_without_text_is_ok(self):
        parsed = generator.parse_review('{"verdict":"rewrite","reason":"改一下"}')
        self.assertEqual(parsed["verdict"], "ok")
        self.assertEqual(parsed["reason"], "rewrite_without_text")


class TestReviewDraft(unittest.IsolatedAsyncioTestCase):
    async def test_normal_verdict_passes_through(self):
        seen = {}

        async def call(provider, prompt):
            seen["provider"] = provider
            seen["prompt"] = prompt
            return '{"verdict":"drop","reason":"太晚了会吵醒人"}'

        out = await generator.review_draft("p1", "睡了吗", {"angle": "问睡没睡"}, [], call_llm=call)
        self.assertEqual(out["verdict"], "drop")
        self.assertEqual(seen["provider"], "p1")
        self.assertIn("睡了吗", seen["prompt"])
        # 没有卡时要明说，否则模型会以为没出处也无所谓。
        self.assertIn("没有卡", seen["prompt"])

    async def test_call_exception_is_ok(self):
        async def call(provider, prompt):
            raise RuntimeError("boom")

        out = await generator.review_draft("p1", "在吗", None, None, call_llm=call)
        self.assertEqual(out["verdict"], "ok")
        self.assertEqual(out["reason"], "review_call_failed")

    async def test_unparsed_is_ok(self):
        async def call(provider, prompt):
            return "这条挺好的"

        out = await generator.review_draft("p1", "在吗", None, None, call_llm=call)
        self.assertEqual(out["verdict"], "ok")
        self.assertEqual(out["reason"], "review_unparsed")

    async def test_disabled_does_not_call(self):
        called = []

        async def call(provider, prompt):
            called.append(1)
            return '{"verdict":"drop"}'

        out = await generator.review_draft("p1", "在吗", None, None, call_llm=call, enabled=False)
        self.assertEqual(out["verdict"], "ok")
        self.assertEqual(out["reason"], "disabled")
        self.assertEqual(called, [])

    async def test_empty_text_is_ok(self):
        async def call(provider, prompt):
            raise AssertionError("空正文不该调模型")

        out = await generator.review_draft("p1", "   ", None, None, call_llm=call)
        self.assertEqual(out["verdict"], "ok")
        self.assertEqual(out["reason"], "empty_text")

    async def test_no_call_llm_is_ok(self):
        out = await generator.review_draft("p1", "在吗", None, None)
        self.assertEqual(out["verdict"], "ok")
        self.assertEqual(out["reason"], "no_call_llm")


class TestGroundedClaims(unittest.TestCase):
    def test_hit(self):
        cards = [{"title": "周六去图书馆还书", "detail": "顺便把借的书还了"}]
        out = generator.grounded_claims("周六去图书馆还书了", cards)
        self.assertTrue(out["ok"])
        self.assertEqual(out["missing"], [])
        self.assertEqual(out["ratio"], 1.0)

    def test_miss(self):
        cards = [{"title": "周六去图书馆还书"}]
        out = generator.grounded_claims("昨天在三里屯买了双新鞋", cards)
        self.assertFalse(out["ok"])
        self.assertTrue(out["missing"])
        self.assertEqual(out["ratio"], 0.0)

    def test_half_is_enough(self):
        cards = [{"detail": "周六下午三点在图书馆还书"}]
        text = "周六下午三点在图书馆还书。昨天在楼下买了杯咖啡。"
        out = generator.grounded_claims(text, cards)
        self.assertEqual(out["ratio"], 0.5)
        self.assertTrue(out["ok"])

    def test_no_cards_passes(self):
        out = generator.grounded_claims("昨天在三里屯买了双新鞋", [])
        self.assertTrue(out["ok"])
        self.assertEqual(out["ratio"], 1.0)

    def test_no_claims_passes(self):
        out = generator.grounded_claims("有点想你", [{"title": "别的"}])
        self.assertTrue(out["ok"])

    def test_question_is_not_a_claim(self):
        out = generator.grounded_claims("你今天还加班吗", [{"title": "别的"}])
        self.assertTrue(out["ok"])
        self.assertEqual(out["missing"], [])

    def test_dirty_input_passes(self):
        for text, cards in ((None, None), ("", "不是列表"), ("今天三点的会", 42)):
            self.assertTrue(generator.grounded_claims(text, cards)["ok"])


class TestGeneratorMethods(unittest.IsolatedAsyncioTestCase):
    def _gen(self):
        return generator.MessageGenerator(None)

    async def test_method_review_enabled(self):
        gen = self._gen()

        async def fake(provider, prompt, max_tokens=None):
            return '{"verdict":"drop","reason":"空话"}'

        gen._call_llm = fake
        out = await gen.review_draft("p1", "在吗", enabled=True)
        self.assertEqual(out["verdict"], "drop")

    async def test_method_review_defaults_on(self):
        # 钉住与 engine.py 的接线契约：engine 先读 draft_review_enabled 再调用，
        # 所以不传 enabled 时必须真的去问模型，否则开关打开也只是静默走旧路。
        gen = self._gen()
        calls = []

        async def fake(provider, prompt, max_tokens=None):
            calls.append(prompt)
            return '{"verdict":"drop","reason":"空话"}'

        gen._call_llm = fake
        out = await gen.review_draft("p1", "在吗")
        self.assertEqual(out["verdict"], "drop")
        self.assertEqual(1, len(calls))

    async def test_method_review_explicit_off(self):
        gen = self._gen()

        async def fake(provider, prompt, max_tokens=None):
            raise AssertionError("显式关闭时不该调模型")

        gen._call_llm = fake
        out = await gen.review_draft("p1", "在吗", enabled=False)
        self.assertEqual(out["verdict"], "ok")
        self.assertEqual(out["reason"], "disabled")


if __name__ == "__main__":
    unittest.main()
