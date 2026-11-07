"""v2.1.4：发送前四道闸（social.guards 的四个纯函数）。

对应 BATCH_SPEC_v214 §6。这里只测**判定语义与 fail-open**，不测接线
（接线在 generator/engine，属 social-gates / social-closing 的文件）。

用例分组：
- A 拒答：中英命中、大小写、长文本中间命中、近似但**不该**命中的句子、脏输入；
- B 自接自：她最后说+接话词 / 用户最后说 / 窗口外 / 长度闸 / 多段 / 三元组与对象形式 / 脏对话；
- C 在途重复：逐字 / 改写重合 / 无关 / 非在途状态 / 窗口外 / 阈值 / 脏输入；
- D 证据缺失：子串 / 标点差异 / 词序变化 / 全缺 / 空输入 / 脏输入；
- E fail-open：__str__ 抛异常的输入，四个函数都不许抛。
"""

from __future__ import annotations

import logging
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

# ─── 路径与最小 astrbot 桩（与 test_v213_group_closeout.py 同款，幂等） ───
_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


class _FakeMessageChain:
    def message(self, text):
        return ("chain", text)


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v214_guards_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _FakeMessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import guards  # noqa: E402

NOW = 1_700_000_000.0


class _Boom:
    """任何 str() 都炸的对象：用来验证 fail-open 不是嘴上说说。"""

    def __str__(self):
        raise RuntimeError("boom")


# ── A 组：拒答 ────────────────────────────────────────────────


class RefusalTest(unittest.TestCase):
    def test_default_terms_are_bounded_tuple(self):
        terms = guards.DEFAULT_REFUSAL_TERMS
        self.assertIsInstance(terms, tuple)
        self.assertGreaterEqual(len(terms), 10)
        self.assertTrue(all(isinstance(term, str) and term for term in terms))
        self.assertEqual(len(terms), len({term.casefold() for term in terms}))

    def test_chinese_phrase_is_detected(self):
        self.assertEqual(
            guards.looks_like_refusal("很抱歉，我无法继续这个话题。"), "很抱歉，我无法"
        )

    def test_ai_persona_disclaimer_is_detected(self):
        self.assertEqual(
            guards.looks_like_refusal("作为一个AI语言模型，我不能表达情绪。"),
            "作为一个AI语言模型",
        )

    def test_english_phrase_is_case_insensitive(self):
        self.assertEqual(
            guards.looks_like_refusal("I CAN'T HELP WITH THAT, sorry."),
            "I can't help with that",
        )

    def test_platform_submit_failure_is_detected(self):
        self.assertEqual(
            guards.looks_like_refusal("The prompt could not be submitted. Please retry."),
            "The prompt could not be submitted",
        )

    def test_phrase_in_the_middle_of_a_long_text_is_detected(self):
        text = "嗯……刚才想说的其实很多，" * 3 + "但我无法满足这个要求，" + "那就这样吧。" * 3
        self.assertEqual(guards.looks_like_refusal(text), "我无法满足")

    def test_ordinary_love_line_is_not_a_refusal(self):
        # 「我无法想象没有你的日子」是话，不是过滤器；词表只收整句话术。
        self.assertEqual(guards.looks_like_refusal("我无法想象没有你的日子。"), "")

    def test_ordinary_chat_is_not_a_refusal(self):
        self.assertEqual(guards.looks_like_refusal("刚洗完澡，头发还湿着。"), "")

    def test_empty_and_blank_text_is_safe(self):
        for value in ("", "   ", None, 0, [], {}):
            self.assertEqual(guards.looks_like_refusal(value), "")

    def test_returns_the_term_not_a_bool(self):
        hit = guards.looks_like_refusal("很抱歉，我无法帮你做这件事")
        self.assertIn(hit, guards.DEFAULT_REFUSAL_TERMS)


# ── B 组：自接自 ──────────────────────────────────────────────


def _dialogue_last_self(ts=NOW - 60):
    return [
        {"role": "user", "ts": NOW - 300, "text": "在吗"},
        {"role": "assistant", "ts": ts, "text": "在的，刚洗完澡。"},
    ]


class DanglingTest(unittest.TestCase):
    def test_opener_after_own_reply_is_dangling(self):
        self.assertTrue(guards.dangling_after_own_reply("哦？", _dialogue_last_self(), at=NOW))

    def test_question_opener_after_own_reply_is_dangling(self):
        self.assertTrue(
            guards.dangling_after_own_reply("那你想吃点什么呀", _dialogue_last_self(), at=NOW)
        )

    def test_causal_opener_after_own_reply_is_dangling(self):
        self.assertTrue(guards.dangling_after_own_reply("所以呢", _dialogue_last_self(), at=NOW))

    def test_user_spoke_last_is_not_dangling(self):
        dialogue = [
            {"role": "assistant", "ts": NOW - 60, "text": "在的。"},
            {"role": "user", "ts": NOW - 30, "text": "哦？"},
        ]
        self.assertFalse(guards.dangling_after_own_reply("哦？", dialogue, at=NOW))

    def test_outside_window_is_not_dangling(self):
        self.assertFalse(
            guards.dangling_after_own_reply("哦？", _dialogue_last_self(NOW - 2000), at=NOW)
        )

    def test_custom_window_can_include_it(self):
        self.assertTrue(
            guards.dangling_after_own_reply(
                "哦？", _dialogue_last_self(NOW - 2000), at=NOW, window_seconds=3600
            )
        )

    def test_long_opening_sentence_is_not_dangling(self):
        # >12 字：自带信息，不算半截对话。
        self.assertFalse(
            guards.dangling_after_own_reply(
                "今天路过那家咖啡店，想起你说想喝手冲", _dialogue_last_self(), at=NOW
            )
        )

    def test_second_segment_still_counts(self):
        self.assertTrue(
            guards.dangling_after_own_reply(
                "今天下班特别晚，路上堵了快一个小时\n嗯", _dialogue_last_self(), at=NOW
            )
        )

    def test_tuple_dialogue_is_supported(self):
        dialogue = [("user", NOW - 300, "在吗"), ("assistant", NOW - 60, "在的")]
        self.assertTrue(guards.dangling_after_own_reply("嗯", dialogue, at=NOW))

    def test_object_dialogue_is_supported(self):
        dialogue = [
            SimpleNamespace(role="user", ts=NOW - 300, text="在吗"),
            SimpleNamespace(role="assistant", ts=NOW - 60, text="在的"),
        ]
        self.assertTrue(guards.dangling_after_own_reply("确实", dialogue, at=NOW))

    def test_is_self_flag_is_honoured(self):
        dialogue = [{"is_self": True, "ts": NOW - 10, "text": "我先睡啦"}]
        self.assertTrue(guards.dangling_after_own_reply("也是", dialogue, at=NOW))

    def test_empty_dialogue_is_not_dangling(self):
        self.assertFalse(guards.dangling_after_own_reply("哦？", [], at=NOW))
        self.assertFalse(guards.dangling_after_own_reply("哦？", None, at=NOW))

    def test_unknown_time_is_not_dangling(self):
        # 时间不知道就不拦：宁可放过一条半截对话，不能因为字段缺失卡住正常消息。
        dialogue = [{"role": "assistant", "text": "在的"}]
        self.assertFalse(guards.dangling_after_own_reply("哦？", dialogue, at=NOW))

    def test_bad_at_is_not_dangling(self):
        for bad in (None, "now", _Boom(), float("nan")):
            self.assertFalse(
                guards.dangling_after_own_reply("哦？", _dialogue_last_self(), at=bad), bad
            )

    def test_future_timestamp_is_not_dangling(self):
        self.assertFalse(
            guards.dangling_after_own_reply("哦？", _dialogue_last_self(NOW + 120), at=NOW)
        )

    def test_dirty_dialogue_does_not_raise(self):
        dirty = [None, {}, {"role": None}, ("x",), 42, "abc"]
        self.assertFalse(guards.dangling_after_own_reply("哦？", dirty, at=NOW))

    def test_empty_text_is_not_dangling(self):
        self.assertFalse(guards.dangling_after_own_reply("", _dialogue_last_self(), at=NOW))
        self.assertFalse(guards.dangling_after_own_reply(None, _dialogue_last_self(), at=NOW))


# ── C 组：在途重复 ────────────────────────────────────────────


def _inflight(text, state="pending", ts=None):
    item = {"text": text, "state": state}
    if ts is not None:
        item["ts"] = ts
    return item


class DuplicateInflightTest(unittest.TestCase):
    def test_identical_inflight_text_is_duplicate(self):
        self.assertTrue(guards.duplicate_inflight("有点想你了", [_inflight("有点想你了")]))

    def test_rewritten_inflight_text_is_duplicate(self):
        self.assertTrue(guards.duplicate_inflight("有点想你了呀", [_inflight("有点想你了")]))

    def test_unrelated_text_is_not_duplicate(self):
        self.assertFalse(guards.duplicate_inflight("今天下雨了，记得带伞", [_inflight("晚上想吃火锅")]))

    def test_delivered_state_is_left_to_verify(self):
        self.assertFalse(
            guards.duplicate_inflight("有点想你了", [_inflight("有点想你了", state="delivered")])
        )

    def test_all_four_inflight_states_are_compared(self):
        for state in ("pending", "dispatched", "accepted", "unknown"):
            self.assertTrue(
                guards.duplicate_inflight("有点想你了", [_inflight("有点想你了", state=state)]),
                state,
            )

    def test_stale_inflight_is_ignored(self):
        old = _inflight("有点想你了", ts=time.time() - 4000)
        self.assertFalse(guards.duplicate_inflight("有点想你了", [old]))

    def test_fresh_inflight_is_compared(self):
        fresh = _inflight("有点想你了", ts=time.time() - 10)
        self.assertTrue(guards.duplicate_inflight("有点想你了", [fresh]))

    def test_threshold_can_be_raised(self):
        self.assertFalse(
            guards.duplicate_inflight("有点想你了呀", [_inflight("有点想你了")], threshold=0.99)
        )

    def test_plain_string_inflight_is_compared(self):
        self.assertTrue(guards.duplicate_inflight("有点想你了", ["有点想你了"]))

    def test_empty_inflight_list_is_not_duplicate(self):
        self.assertFalse(guards.duplicate_inflight("有点想你了", []))
        self.assertFalse(guards.duplicate_inflight("有点想你了", None))

    def test_empty_text_is_not_duplicate(self):
        self.assertFalse(guards.duplicate_inflight("", [_inflight("有点想你了")]))
        self.assertFalse(guards.duplicate_inflight(None, [_inflight("有点想你了")]))

    def test_dirty_inflight_does_not_raise(self):
        self.assertFalse(
            guards.duplicate_inflight("有点想你了", [None, 42, {"text": None}, {"text": 3}])
        )

    def test_single_dict_inflight_is_accepted(self):
        self.assertTrue(
            guards.duplicate_inflight("有点想你了", {"text": "有点想你了", "state": "pending"})
        )

    def test_bad_window_and_threshold_fall_back_to_defaults(self):
        self.assertTrue(
            guards.duplicate_inflight(
                "有点想你了", [_inflight("有点想你了")], window_seconds=None, threshold=None
            )
        )


# ── D 组：证据缺失 ────────────────────────────────────────────

CARDS = [
    {"title": "午后那杯手冲", "detail": "豆子是上周买的耶加，冲出来有点酸", "source": "生活素材"},
    {"title": "周三下午的评审", "detail": "三点开始，材料还没整理完", "source": "日程"},
]


class EvidenceMissingTest(unittest.TestCase):
    def test_fact_present_as_substring_is_found(self):
        self.assertEqual(guards.evidence_missing(["豆子是上周买的耶加"], CARDS), [])

    def test_punctuation_and_space_differences_still_match(self):
        self.assertEqual(guards.evidence_missing(["豆子是上周买的 耶加！"], CARDS), [])

    def test_word_order_change_still_matches(self):
        # 模型常把「豆子是上周买的耶加」写成「上周买的耶加豆子」，词序一变子串就失效。
        self.assertEqual(guards.evidence_missing(["上周买的耶加豆子"], CARDS), [])

    def test_fabricated_fact_is_reported(self):
        self.assertEqual(guards.evidence_missing(["今天去爬山了"], CARDS), ["今天去爬山了"])

    def test_only_the_fabricated_ones_are_reported(self):
        facts = ["豆子是上周买的耶加", "今天去爬山了", "三点开始"]
        self.assertEqual(guards.evidence_missing(facts, CARDS), ["今天去爬山了"])

    def test_no_cards_means_everything_is_missing(self):
        self.assertEqual(
            guards.evidence_missing(["豆子是上周买的耶加"], []), ["豆子是上周买的耶加"]
        )

    def test_empty_facts_is_empty_result(self):
        self.assertEqual(guards.evidence_missing([], CARDS), [])
        self.assertEqual(guards.evidence_missing(None, CARDS), [])

    def test_blank_fact_is_not_reported(self):
        self.assertEqual(guards.evidence_missing(["", "   ", None], CARDS), [])

    def test_dirty_cards_do_not_raise(self):
        self.assertEqual(
            guards.evidence_missing(["豆子是上周买的耶加"], [None, 42, "abc"]),
            ["豆子是上周买的耶加"],
        )

    def test_original_fact_string_is_returned(self):
        self.assertEqual(guards.evidence_missing(["今天去爬山了！"], CARDS), ["今天去爬山了！"])


# ── E 组：fail-open 与契约 ────────────────────────────────────


class FailOpenTest(unittest.TestCase):
    def test_refusal_never_raises(self):
        self.assertEqual(guards.looks_like_refusal(_Boom()), "")

    def test_dangling_never_raises(self):
        self.assertFalse(guards.dangling_after_own_reply(_Boom(), [_Boom()], at=_Boom()))

    def test_duplicate_never_raises(self):
        self.assertFalse(guards.duplicate_inflight(_Boom(), [_Boom()]))

    def test_evidence_never_raises(self):
        self.assertEqual(guards.evidence_missing([_Boom()], _Boom()), [])

    def test_non_iterable_inflight_never_raises(self):
        self.assertFalse(guards.duplicate_inflight("有点想你了", 42))

    def test_non_iterable_dialogue_never_raises(self):
        self.assertFalse(guards.dangling_after_own_reply("哦？", 42, at=NOW))


class ContractTest(unittest.TestCase):
    def test_module_exposes_the_four_gates(self):
        for name in (
            "looks_like_refusal",
            "dangling_after_own_reply",
            "duplicate_inflight",
            "evidence_missing",
        ):
            self.assertTrue(callable(getattr(guards, name)), name)

    def test_spec_examples_are_covered(self):
        examples = (
            "很抱歉，我无法",
            "我无法继续",
            "作为一个AI语言模型",
            "I can't help with that",
            "The prompt could not be submitted",
        )
        for example in examples:
            self.assertTrue(guards.looks_like_refusal(example), example)


if __name__ == "__main__":
    unittest.main()
