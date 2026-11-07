# -*- coding: utf-8 -*-
"""v2.1.4：主动消息拆成「先规划、再写作」两段。

背景：原来 `decide()` 一次调用同时干三件事——判断要不要说、挑说哪件事、把话写出来。
模型于是总是先写出一句话再倒推理由，写完只剩正则清洗，没有任何一道能拦住「这件事
其实没发生过」。拆开之后：`plan()` 只回答要不要说 / 从哪件事说（明令不写正文），
`decide_with_plan()` 才去写作。

这个文件钉死四件事：

1. `_parse_plan()` 的宽容与不宽容：围栏/think 块/客套话要剥掉，但「说要发却既没
   切口也没事实」必须判成解析失败——拿一个空规划去写正文等于把拆分的意义抹掉。
2. 时间投影是**算好再给**：间隔读不出来必须写「不知道」，不能编；星期与时段以
   投影块为准，模型不许自己按日期换算。
3. `decide_with_plan()` 两条路径：skip 一次写作调用都不花，why_not 带原因码原文；
   send 才走 `_compose(decide=True)` 并追加【这次的说法】。
4. `max_tokens` 只在 provider 签名吃得下时才传；不吃就原样调用。这条路径绝不能
   把「试探性传参」变成「这一路候选被永久剔除」。

注意：本文件不碰 `_compose_group` / `_compose(decide=False)` / `generate()`，
那三条老路径的行为必须与 2.1.3 完全一致。
"""

from __future__ import annotations

import asyncio
import datetime
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
    api.logger = logging.getLogger("autonomous_social_v214_plan_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import generator  # noqa: E402
from social.config import SocialConfig  # noqa: E402

# 2026-11-07 20:31（UTC+8）；那天是周六——测试里按真实星期断言，别照抄 spec 的示例。
NOW = datetime.datetime(2026, 11, 7, 12, 31, tzinfo=datetime.timezone.utc).timestamp()
OFFSET = 480
FENCE = chr(96) * 3


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    """provider 的返回形状：只需要 completion_text / role 两个属性。"""

    def __init__(self, text: str = "", role: str = "assistant"):
        self.completion_text = text
        self.role = role


class _Provider:
    """按顺序回话的假 provider，顺带记录每次调用收到的 max_tokens。"""

    id = "p1"

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []
        self.tokens: list = []

    async def text_chat(self, prompt: str = "", max_tokens=None, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        self.tokens.append(max_tokens)
        if not self.replies:
            return _Resp("NO")
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return _Resp(text)


class _NoTokenProvider:
    """签名里没有 max_tokens 的老 provider：传了就 TypeError。"""

    id = "p2"

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    async def text_chat(self, prompt: str = ""):
        self.calls += 1
        return _Resp(self.text)


class _Ctx:
    def __init__(self, provider):
        self._provider = provider

    async def get_using_provider_async(self, umo=None):
        return self._provider


class _PlainCtx:
    """没有任何取 provider 的方法：`_get_provider()` 返回 None。"""


def _make(provider=None, cfg=None) -> generator.MessageGenerator:
    ctx = _Ctx(provider) if provider is not None else _PlainCtx()
    return generator.MessageGenerator(ctx, cfg or SocialConfig())


def _plan_prompt_args(**kwargs):
    base = {
        "target": {},
        "core_context": "她刚睡醒，心情不错",
        "reason": "想起她昨天提过面包",
        "reason_meta": None,
        "mind": None,
        "memory_note": "",
        "cards": None,
        "at": NOW,
        "clock_offset": OFFSET,
    }
    base.update(kwargs)
    return base


class TestPlanDataclass(unittest.TestCase):
    def test_defaults(self):
        plan = generator.Plan()
        self.assertEqual(plan.decision, "skip")
        self.assertEqual(plan.confidence, 0.5)
        self.assertEqual(plan.angle, "")
        self.assertEqual(plan.facts, [])
        self.assertEqual(plan.avoid, [])
        self.assertEqual(plan.skip_reason, "")
        self.assertEqual(plan.raw, "")

    def test_skip_reasons_are_the_six_fixed_codes(self):
        self.assertEqual(
            generator.SKIP_REASONS,
            (
                "insufficient_context",
                "no_natural_opening",
                "purpose_mismatch",
                "repetitive",
                "respect_user_boundary",
                "other",
            ),
        )

    def test_decision_carries_plan_by_default_none(self):
        decision = generator.Decision(send=False, why_not="quiet_hours")
        self.assertIsNone(decision.plan)

    def test_output_tokens_table(self):
        self.assertEqual(generator.OUTPUT_TOKENS["plan"], 320)
        self.assertEqual(generator.OUTPUT_TOKENS["proactive"], 384)
        self.assertEqual(generator.OUTPUT_TOKENS["group"], 512)
        self.assertEqual(generator.OUTPUT_TOKENS["chat"], 512)
        self.assertEqual(generator.OUTPUT_TOKENS["structured"], 1024)


class TestParsePlan(unittest.TestCase):
    def setUp(self):
        self.gen = _make()

    def test_send_with_angle_and_facts(self):
        plan = self.gen._parse_plan(
            '{"decision":"send","confidence":0.8,"angle":"面包那件事",'
            '"facts":["她昨天说面包难吃"],"avoid":["别问吃没吃"]}'
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan.decision, "send")
        self.assertAlmostEqual(plan.confidence, 0.8)
        self.assertEqual(plan.angle, "面包那件事")
        self.assertEqual(plan.facts, ["她昨天说面包难吃"])
        self.assertEqual(plan.avoid, ["别问吃没吃"])
        self.assertEqual(plan.skip_reason, "")

    def test_skip_keeps_reason(self):
        plan = self.gen._parse_plan('{"decision":"skip","skip_reason":"repetitive"}')
        self.assertIsNotNone(plan)
        self.assertEqual(plan.decision, "skip")
        self.assertEqual(plan.skip_reason, "repetitive")

    def test_fenced_json_is_accepted(self):
        text = FENCE + 'json\n{"decision":"send","angle":"切口"}\n' + FENCE
        plan = self.gen._parse_plan(text)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.angle, "切口")

    def test_think_block_is_stripped(self):
        plan = self.gen._parse_plan('<think>要不要说呢</think>{"decision":"send","angle":"a"}')
        self.assertIsNotNone(plan)
        self.assertEqual(plan.decision, "send")

    def test_leading_prose_is_tolerated(self):
        plan = self.gen._parse_plan('好的，这是结果：{"decision":"skip","skip_reason":"other"} 完毕')
        self.assertIsNotNone(plan)
        self.assertEqual(plan.decision, "skip")

    def test_junk_returns_none(self):
        self.assertIsNone(self.gen._parse_plan("今天天气不错，适合散步"))
        self.assertIsNone(self.gen._parse_plan(""))
        self.assertIsNone(self.gen._parse_plan(None))

    def test_unknown_decision_returns_none(self):
        self.assertIsNone(self.gen._parse_plan('{"decision":"maybe","angle":"a"}'))

    def test_send_without_angle_or_facts_returns_none(self):
        # 「说要发」却什么都没说清：不能拿一个空规划去写正文
        self.assertIsNone(self.gen._parse_plan('{"decision":"send"}'))
        self.assertIsNone(self.gen._parse_plan('{"decision":"send","facts":[]}'))

    def test_send_with_only_facts_is_ok(self):
        plan = self.gen._parse_plan('{"decision":"send","facts":["她说明天考试"]}')
        self.assertIsNotNone(plan)
        self.assertEqual(plan.facts, ["她说明天考试"])

    def test_confidence_is_clamped(self):
        self.assertEqual(self.gen._parse_plan('{"decision":"send","angle":"a","confidence":1.7}').confidence, 1.0)
        self.assertEqual(self.gen._parse_plan('{"decision":"send","angle":"a","confidence":-2}').confidence, 0.0)
        self.assertEqual(self.gen._parse_plan('{"decision":"send","angle":"a","confidence":"高"}').confidence, 0.5)
        self.assertEqual(self.gen._parse_plan('{"decision":"send","angle":"a"}').confidence, 0.5)

    def test_unknown_skip_reason_falls_back_to_other(self):
        plan = self.gen._parse_plan('{"decision":"skip","skip_reason":"今天不想说"}')
        self.assertEqual(plan.skip_reason, "other")

    def test_facts_are_truncated_and_capped(self):
        facts = [str(i) + "x" * 200 for i in range(8)]
        plan = self.gen._parse_plan(
            '{"decision":"send","facts":[' + ",".join('"%s"' % f for f in facts) + "]}"
        )
        self.assertEqual(len(plan.facts), 6)
        self.assertTrue(all(len(f) <= 120 for f in plan.facts))

    def test_angle_is_capped_at_200(self):
        plan = self.gen._parse_plan('{"decision":"send","angle":"%s"}' % ("切" * 500))
        self.assertEqual(len(plan.angle), 200)

    def test_raw_keeps_model_output_minus_think_blocks(self):
        # raw 是排查用的原始输出：围栏保留（那是模型真实写法），但 <think> 要剥掉，
        # 否则日志里每次都要翻过一整段推理才看得到 JSON。
        plan = self.gen._parse_plan(FENCE + '\n{"decision":"skip","skip_reason":"other"}\n' + FENCE)
        self.assertIn('skip_reason', plan.raw)
        think_plan = self.gen._parse_plan('<think>想了想</think>{"decision":"skip"}')
        self.assertNotIn("想了想", think_plan.raw)


class TestTimeProjection(unittest.TestCase):
    def setUp(self):
        self.gen = _make()

    def test_contains_clock_weekday_period_and_gap(self):
        text = self.gen._time_projection({"last_sent": NOW - 37 * 60}, NOW, OFFSET)
        self.assertIn("2026-11-07 20:31", text)
        self.assertIn("周六", text)
        self.assertIn("傍晚", text)
        self.assertIn("37 分钟", text)
        self.assertIn("不要自己按日期换算", text)

    def test_unknown_gap_says_so_instead_of_guessing(self):
        text = self.gen._time_projection({}, NOW, OFFSET)
        self.assertIn("不知道", text)
        self.assertNotIn("分钟", text)

    def test_future_timestamp_is_treated_as_unknown(self):
        text = self.gen._time_projection({"last_sent": NOW + 3600}, NOW, OFFSET)
        self.assertIn("不知道", text)

    def test_prefers_the_later_of_two_timestamps(self):
        target = {"last_sent": NOW - 120 * 60, "last_proactive_sent": NOW - 30 * 60}
        text = self.gen._time_projection(target, NOW, OFFSET)
        self.assertIn("30 分钟", text)

    def test_minutes_helper_returns_none_for_bad_target(self):
        self.assertIsNone(self.gen._minutes_since_last_sent(None, NOW))
        self.assertIsNone(self.gen._minutes_since_last_sent({"last_sent": "坏值"}, NOW))
        self.assertEqual(
            self.gen._minutes_since_last_sent({"last_sent": NOW - 59}, NOW), 0
        )


class TestPlanPrompt(unittest.TestCase):
    def setUp(self):
        self.gen = _make()

    def test_forbids_writing_the_message(self):
        prompt = self.gen._plan_prompt(**_plan_prompt_args())
        self.assertIn("这次只做规划，不写正文", prompt)
        self.assertIn("不要写出", prompt)

    def test_lists_all_skip_reasons(self):
        prompt = self.gen._plan_prompt(**_plan_prompt_args())
        for reason in generator.SKIP_REASONS:
            self.assertIn(reason, prompt)

    def test_renders_evidence_cards(self):
        cards = [{"title": "面包", "detail": "她说难吃", "source": "聊天"}]
        prompt = self.gen._plan_prompt(**_plan_prompt_args(cards=cards))
        self.assertIn("[聊天] 面包：她说难吃", prompt)

    def test_says_explicitly_when_there_are_no_cards(self):
        prompt = self.gen._plan_prompt(**_plan_prompt_args(cards=[]))
        self.assertIn("没有一件能指认的具体事", prompt)

    def test_includes_time_projection_when_enabled(self):
        prompt = self.gen._plan_prompt(**_plan_prompt_args())
        self.assertIn("【此刻】", prompt)

    def test_time_projection_can_be_disabled(self):
        cfg = SocialConfig()
        cfg.time_projection_enabled = False
        gen = _make(cfg=cfg)
        prompt = gen._plan_prompt(**_plan_prompt_args())
        self.assertNotIn("【此刻】", prompt)

    def test_includes_reason_and_core_context(self):
        prompt = self.gen._plan_prompt(**_plan_prompt_args())
        self.assertIn("想起她昨天提过面包", prompt)
        self.assertIn("她刚睡醒", prompt)

    def test_card_lines_cap_at_five(self):
        cards = [{"title": "事%d" % i, "detail": "细节"} for i in range(8)]
        lines = self.gen._card_lines(cards)
        self.assertEqual(len(lines), 5)

    def test_plan_block_lists_facts_and_forbids_new_ones(self):
        plan = generator.Plan(
            decision="send", angle="面包", facts=["她昨天说面包难吃"], avoid=["别问吃没吃"]
        )
        block = self.gen._plan_block(plan)
        self.assertIn("【这次的说法】", block)
        self.assertIn("切口：面包", block)
        self.assertIn("- 她昨天说面包难吃", block)
        self.assertIn("别问吃没吃", block)

    def test_plan_block_without_facts_forbids_inventing_events(self):
        block = self.gen._plan_block(generator.Plan(decision="send", angle="就说想她"))
        self.assertIn("不要编具体事件", block)


class TestPlanCall(unittest.TestCase):
    def test_returns_plan_from_json(self):
        provider = _Provider('{"decision":"send","confidence":0.9,"angle":"面包"}')
        gen = _make(provider)
        plan = _run(gen.plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET))
        self.assertIsNotNone(plan)
        self.assertEqual(plan.angle, "面包")

    def test_uses_plan_token_budget(self):
        provider = _Provider('{"decision":"skip","skip_reason":"other"}')
        gen = _make(provider)
        _run(gen.plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET))
        self.assertEqual(provider.tokens, [generator.OUTPUT_TOKENS["plan"]])

    def test_garbage_output_returns_none(self):
        provider = _Provider("我觉得可以聊聊天")
        gen = _make(provider)
        self.assertIsNone(
            _run(gen.plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET))
        )

    def test_disabled_does_not_call_llm(self):
        cfg = SocialConfig()
        cfg.planning_enabled = False
        provider = _Provider('{"decision":"send","angle":"a"}')
        gen = _make(provider, cfg)
        self.assertIsNone(
            _run(gen.plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET))
        )
        self.assertEqual(provider.calls, 0)

    def test_no_provider_returns_none(self):
        gen = _make()
        self.assertIsNone(
            _run(gen.plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET))
        )

    def test_provider_without_max_tokens_still_works(self):
        provider = _NoTokenProvider('{"decision":"send","angle":"面包"}')
        gen = _make(provider)
        plan = _run(gen.plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET))
        self.assertIsNotNone(plan)
        self.assertEqual(provider.calls, 1)

    def test_backoff_skips_the_call(self):
        provider = _Provider('{"decision":"send","angle":"a"}')
        gen = _make(provider)
        # 退避看的是本实例的 `_now()`（真实时钟），不是测试里的 NOW，
        # 所以这里必须用 `gen._now()` 算，否则会写出一个已经过去的时刻。
        gen._llm_skip_until[gen._scope_of(provider)] = gen._now() + 600
        self.assertIsNone(
            _run(gen.plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET))
        )
        self.assertEqual(provider.calls, 0)


class TestDecideWithPlan(unittest.TestCase):
    def test_skip_costs_no_writing_call(self):
        provider = _Provider('{"decision":"skip","skip_reason":"no_natural_opening"}')
        gen = _make(provider)
        decision = _run(
            gen.decide_with_plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET)
        )
        self.assertIsNotNone(decision)
        self.assertFalse(decision.send)
        self.assertEqual(decision.why_not, "no_natural_opening")
        self.assertIsNotNone(decision.plan)
        self.assertEqual(provider.calls, 1)

    def test_skip_without_reason_falls_back_to_other(self):
        provider = _Provider('{"decision":"skip"}')
        gen = _make(provider)
        decision = _run(
            gen.decide_with_plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET)
        )
        self.assertEqual(decision.why_not, "other")

    def test_send_path_appends_plan_block_and_parses_decision(self):
        provider = _Provider(
            '{"decision":"send","confidence":0.9,"angle":"面包","facts":["她昨天说面包难吃"]}',
            "SEND\n面包那事儿我还在想",
        )
        gen = _make(provider)
        decision = _run(
            gen.decide_with_plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET)
        )
        self.assertIsNotNone(decision)
        self.assertTrue(decision.send)
        self.assertEqual(decision.parts, ["面包那事儿我还在想"])
        self.assertIsNotNone(decision.plan)
        self.assertEqual(decision.plan.angle, "面包")
        # 第二次调用才是写作：提示词末尾必须带上规划，且带写作档的输出预算
        self.assertIn("【这次的说法】", provider.prompts[1])
        self.assertIn("切口：面包", provider.prompts[1])
        self.assertEqual(provider.tokens[1], generator.OUTPUT_TOKENS["proactive"])

    def test_send_path_keeps_time_projection_in_writing_prompt(self):
        provider = _Provider(
            '{"decision":"send","angle":"面包"}',
            "SEND\n嗨",
        )
        gen = _make(provider)
        _run(gen.decide_with_plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET))
        self.assertIn("【此刻】", provider.prompts[1])

    def test_compose_failure_returns_none(self):
        provider = _Provider('{"decision":"send","angle":"面包"}')
        gen = _make(provider)

        async def _no_compose(*args, **kwargs):
            return None

        gen._compose = _no_compose
        self.assertIsNone(
            _run(gen.decide_with_plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET))
        )
        self.assertEqual(provider.calls, 1)

    def test_plan_failure_returns_none_so_caller_can_fall_back(self):
        provider = _Provider("我觉")
        gen = _make(provider)
        self.assertIsNone(
            _run(gen.decide_with_plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET))
        )

    def test_disabled_returns_none_without_calls(self):
        cfg = SocialConfig()
        cfg.planning_enabled = False
        provider = _Provider('{"decision":"send","angle":"a"}')
        gen = _make(provider, cfg)
        self.assertIsNone(
            _run(gen.decide_with_plan("umo", {}, "ctx", "reason", None, "", None, at=NOW, clock_offset=OFFSET))
        )
        self.assertEqual(provider.calls, 0)


class TestMaxTokensPlumbing(unittest.TestCase):
    def test_output_tokens_respects_switch(self):
        gen = _make()
        self.assertEqual(gen._output_tokens("plan"), 320)
        self.assertIsNone(gen._output_tokens("不存在"))
        cfg = SocialConfig()
        cfg.max_output_tokens_enabled = False
        self.assertIsNone(_make(cfg=cfg)._output_tokens("plan"))

    def test_call_llm_passes_max_tokens_when_accepted(self):
        provider = _Provider("SEND\n嗨")
        gen = _make(provider)
        self.assertEqual(_run(gen._call_llm(provider, "p", max_tokens=384)), "SEND\n嗨")
        self.assertEqual(provider.tokens, [384])

    def test_call_llm_works_when_signature_rejects_it(self):
        provider = _NoTokenProvider("SEND\n嗨")
        gen = _make(provider)
        self.assertEqual(_run(gen._call_llm(provider, "p", max_tokens=384)), "SEND\n嗨")
        self.assertEqual(provider.calls, 1)

    def test_invoke_llm_swallows_type_error_and_retries_without(self):
        class _Strict:
            """签名说吃 **kwargs、实际不吃：装饰器包一层的那种 provider。"""

            def __init__(self):
                self.seen = []

            async def __call__(self, prompt="", **kwargs):
                self.seen.append(dict(kwargs))
                if "max_tokens" in kwargs:
                    raise TypeError("unexpected keyword argument")
                return "ok"

        func = _Strict()
        gen = _make()
        self.assertEqual(_run(gen._invoke_llm(func, (), {"prompt": "p"}, 128)), "ok")
        self.assertEqual(func.seen, [{"max_tokens": 128}, {}])

    def test_invoke_llm_without_budget_calls_plainly(self):
        class _Plain:
            def __init__(self):
                self.seen = []

            async def __call__(self, prompt=""):
                self.seen.append(prompt)
                return "ok"

        func = _Plain()
        gen = _make()
        self.assertEqual(_run(gen._invoke_llm(func, ("p",), {}, None)), "ok")
        self.assertEqual(func.seen, ["p"])

    def test_candidates_keep_their_shape(self):
        provider = _Provider("x")
        gen = _make(provider)
        candidates = gen._candidates(provider, "p", max_tokens=320)
        self.assertEqual([item[0] for item in candidates], ["text_chat"])
        self.assertTrue(callable(candidates[0][1]))


if __name__ == "__main__":
    unittest.main()
