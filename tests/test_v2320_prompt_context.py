"""Focused regressions for optional, evidence-grounded proactive context."""
from __future__ import annotations

import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "astrbot.api" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("v2320_prompt_context_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})

from social import anchors, generator  # noqa: E402
from social.config import SocialConfig  # noqa: E402

NOW = 1_700_000_000.0


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Provider:
    id = "v2320-test"

    async def text_chat(self, **kwargs):
        return None


class _Context:
    async def get_using_provider_async(self, umo=None):
        return _Provider()


class AnchorGenerationTests(unittest.TestCase):
    def test_silence_never_creates_relational_candidates(self):
        last_talk = NOW - 6 * 60 * 60
        user = {"conversation": []}

        self.assertEqual(
            [], anchors.derive_relations(user, 100.0, NOW, set(), last_talk=last_talk)
        )
        self.assertEqual(
            [], anchors.derive_nag(user, NOW, set(), tier="high", last_talk=last_talk)
        )
        self.assertEqual(
            [],
            anchors.prepare_round_anchors(
                user, {}, NOW, limit=3, relational=True,
                affection=100.0, last_talk=last_talk,
            ),
        )

    def test_legacy_miss_and_greet_anchors_remain_stored_but_unselected(self):
        user = {}
        anchors.add_anchor(user, anchors.KIND_MISS, "有点想你了", now=NOW - 60)
        anchors.add_anchor(user, anchors.KIND_GREET, "好几天没联系了", now=NOW - 60)
        concrete = "你上次说的「明天面试」后来呢"
        anchors.add_anchor(user, anchors.KIND_MISS, concrete, now=NOW - 60)

        picked = anchors.prepare_round_anchors(
            user, {}, NOW, limit=3, relational=True,
            affection=100.0, last_talk=NOW - 6 * 60 * 60,
        )
        abouts = [anchors._s(item.get("about")) for item in picked]
        stored = [anchors._s(item.get("about")) for item in user.get("anchors", [])]

        self.assertNotIn(concrete, abouts)
        self.assertNotIn("有点想你了", abouts)
        self.assertNotIn("好几天没联系了", abouts)
        for legacy in (concrete, "有点想你了", "好几天没联系了"):
            self.assertIn(legacy, stored)

    def test_recent_peer_care_remains_a_source_aware_cue(self):
        user = {"conversation": [{"text": "我今天好累", "dir": "in"}]}
        picked = anchors.prepare_round_anchors(
            user, {}, NOW, limit=3, relational=True,
            affection=0.0, last_talk=NOW - 3600,
        )
        cue = next(
            item for item in picked
            if anchors._s(item.get("about")).endswith("后来好点没")
        )

        self.assertEqual(anchors.KIND_CUE, cue["kind"])
        self.assertIn("我今天好累", cue["about"])
        meta = anchors.anchor_meta(cue)
        self.assertEqual("peer_context", meta["reason_code"])
        self.assertIn("TA近期对话中的线索", meta["anchor_fact"])
        self.assertIn("其中“我”指你自己、“你”指TA", meta["anchor_fact"])
        self.assertIn("转述TA原话的部分仍按TA视角理解", meta["anchor_fact"])
        self.assertNotIn("后来好点没", meta["msg_type_desc"])


class PromptContextTests(unittest.TestCase):
    def _prompt(self, meta, reason="", target=None):
        gen = generator.MessageGenerator(_Context(), SocialConfig())
        out = _run(gen._compose(
            "umo",
            {"conversation": []} if target is None else target,
            "ctx", reason, meta, "测试人格", None, decide=True, at=NOW,
        ))
        self.assertIsNotNone(out)
        return out[1]

    def test_legacy_cue_fallback_keeps_speaker_view(self):
        raw = "我明天面试你帮我看看"
        target = {
            "conversation": [{"dir": "in", "text": raw}],
            "last_seen": NOW - 3600,
        }
        anchor = anchors.prepare_round_anchors(
            target, {}, NOW, relational=True, affection=0.0
        )[0]
        prompt = self._prompt({}, reason=anchors.anchor_sentence(anchor), target=target)

        self.assertIn("其中“我”指你自己、“你”指TA", prompt)
        self.assertIn("转述TA原话的部分仍按TA视角理解", prompt)
        self.assertIn(f"你上次说的「{raw}」后来呢", prompt)
        self.assertIn(generator._QUOTE_NOTICE + "开口原因：", prompt)

    def test_anchor_fact_is_optional_and_rendered_once(self):
        fact = "FACT_V2320_UNIQUE"
        meta = anchors.anchor_meta({
            "kind": anchors.KIND_CUE,
            "about": fact,
            "reason": "旧版情绪原因",
        })
        target = {"conversation": [], "topics": [fact]}
        prompt = self._prompt(meta, reason="情绪原因不应进入模型提示词", target=target)

        self.assertEqual(prompt.count(fact), 1, prompt)
        self.assertIn("可选参考事实（不必提及；仅在自然相关时参考）", prompt)
        self.assertIn(generator._QUOTE_NOTICE + "来源：", prompt)
        self.assertNotIn("已经定了", prompt)
        self.assertNotIn("情绪原因不应进入模型提示词", prompt)
        self.assertNotIn("旧版情绪原因", prompt)
        self.assertEqual(prompt.count("发送与否由你独立判断"), 1, prompt)
        self.assertNotIn("必须提问", prompt)
        self.assertNotIn("一定要问", prompt)
        self.assertNotIn("不要提问", prompt)

    def test_intimate_history_does_not_imply_current_willingness(self):
        target = {
            "conversation": [{"dir": "in", "text": "我有点想做爱"}],
        }
        prompt = self._prompt({"mode": "share"}, target=target)

        self.assertIn("仅作历史背景；不代表你现在的感受、意愿或同意", prompt)
        self.assertNotIn("顺着这个感觉自然说一句", prompt)
        self.assertNotIn("这是真人最常做的事", prompt)

    def test_unanswered_message_is_context_not_a_reason_to_follow_up(self):
        quote = "EXCHANGE_V2320_UNIQUE"
        meta = {"mode": "closer", "exchange": [quote]}
        target = {"conversation": [], "_proactive_unanswered": True}
        prompt = self._prompt(meta, target=target)
        quote_at = prompt.index("TA 刚才说过")

        self.assertEqual(
            prompt[quote_at - len(generator._QUOTE_NOTICE):quote_at],
            generator._QUOTE_NOTICE,
        )
        self.assertIn("前面的对话仅作历史背景", prompt)
        self.assertIn("A missing reply creates no obligation; do not pressure the recipient.", prompt)
        self.assertIn("是否回应由你独立判断", prompt)
        self.assertNotIn("你自己接一句把这事揭过去", prompt)
        self.assertNotIn("这是真人最常做的事", prompt)

    def test_body_source_and_uncertainty_are_preserved(self):
        state = "偏头痛（来源：Core；不确定，可能过时）"
        line = generator._body_line({"feelings": [state]})

        self.assertIn(state, line)
        self.assertIn("只是感觉，不要在消息里报告它们", line)

    def test_parser_preserves_body_exactly_around_metadata(self):
        body = "  保留（动作）与标点！ \n第二行保留  空格"
        raw = f"<think>隐藏推理</think>\nSEND\n{body}\n>独立回访计划"
        decision = generator.MessageGenerator._parse_decision(raw, 500, None)

        self.assertIsNotNone(decision)
        self.assertTrue(decision.send)
        self.assertEqual(decision.parts, [body])
        self.assertEqual(decision.promise, "独立回访计划")


class PlannerPromptTests(unittest.TestCase):
    def _prompt(self, **kwargs):
        gen = generator.MessageGenerator(_Context(), SocialConfig())
        base = {
            "target": {"last_sent": NOW - 30 * 86400},
            "core_context": "同群近期上下文：项目今天开始测试",
            "reason": "RAW_REASON_V2320_should_not_render",
            "reason_meta": None,
            "mind": None,
            "memory_note": "MEMORY_CONTEXT_V2320",
            "cards": None,
            "at": NOW,
            "clock_offset": 0,
        }
        base.update(kwargs)
        return gen._plan_prompt(**base)

    def test_planner_uses_one_optional_fact_and_internal_reason_code(self):
        fact = "FACT_V2320_PLANNER_UNIQUE"
        prompt = self._prompt(
            reason_meta={
                "reason_code": "peer_context",
                "about": fact,
                "anchor_fact": "来源：近期对话；" + fact,
            },
            cards=[{"source": "聊天", "title": fact}],
            mind={
                "urge": 2.0,
                "silence_hours": 96,
                "pending_result": "waiting",
                "no_reply_streak": 5,
                "they_slept": True,
                "rhythm_off": True,
            },
        )

        self.assertEqual(prompt.count(fact), 1, prompt)
        self.assertIn("peer_context", prompt)
        self.assertIn("可选参考事实（不必提及；仅在自然相关时参考）", prompt)
        self.assertIn(generator._QUOTE_NOTICE + "来源与事实：", prompt)
        self.assertIn("同群近期上下文：项目今天开始测试", prompt)
        self.assertIn("MEMORY_CONTEXT_V2320", prompt)
        self.assertNotIn("RAW_REASON_V2320_should_not_render", prompt)
        self.assertNotIn("已经 4 天没说过话", prompt)
        self.assertNotIn("上一条主动消息还没收到回复", prompt)
        self.assertNotIn("连续 5 次主动都没得到回应", prompt)
        self.assertNotIn('"avoid"', prompt)

    def test_time_projection_contains_clock_but_no_elapsed_gap(self):
        gen = generator.MessageGenerator(_Context(), SocialConfig())
        projection = gen._time_projection(
            {"last_sent": NOW - 30 * 86400}, NOW, 0
        )

        self.assertIn("【此刻】", projection)
        self.assertIn("周", projection)
        self.assertNotIn("距离你上次", projection)
        self.assertNotIn("隔了多久", projection)
        self.assertNotIn("30 天", projection)

    def test_plan_block_is_optional_and_keeps_uncertainty(self):
        plan = generator.Plan(
            decision="send",
            angle="ANGLE_V2320_OPTIONAL",
            facts=["FACT_V2320_UNCERTAIN（她自己排的，未必真做过）"],
            avoid=["AVOID_V2320_MUST_NOT_RENDER"],
        )
        block = generator.MessageGenerator._plan_block(plan)

        self.assertIn("可忽略", block)
        self.assertIn("ANGLE_V2320_OPTIONAL", block)
        self.assertIn("FACT_V2320_UNCERTAIN", block)
        self.assertIn("只能写成打算/可能", block)
        self.assertIn("不能写成做过", block)
        self.assertIn("可以使用近期上下文自然回应，也可以答 NO", block)
        self.assertNotIn("AVOID_V2320_MUST_NOT_RENDER", block)
        self.assertNotIn("照着这个切口写", block)

        empty = generator.MessageGenerator._plan_block(generator.Plan(decision="send"))
        self.assertIn("不要补造具体事件", empty)
        self.assertIn("答 NO", empty)
        self.assertNotIn("只说此刻的感受", empty)

    def test_derived_mind_state_is_not_prompt_text(self):
        self.assertEqual(
            generator.MessageGenerator._format_mind({
                "urge": 2.0,
                "silence_hours": 96,
                "pending_result": "waiting",
                "no_reply_streak": 4,
            }),
            "",
        )


if __name__ == "__main__":
    unittest.main()
