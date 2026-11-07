"""私聊收尾沉默守卫：双方互相「晚安」时，这一轮不该再回。

线上那个循环：她说「晚安」→ 对方也回「晚安」→ 她又补一句「晚安啦」→ 对方再回……
两边都觉得自己只是在礼貌收尾，结果谁也没睡成。这条判据的全部意义就是认出「收尾
循环里多余的那一半」，其余情况一律照常回。

测试重点不是词表本身，而是**边界**：带问号/带时间/带图/超长都不能被吞；她只回过
一句「好的」时对方第一次说晚安仍然要回；时间倒挂、时间读不出来、窗口外、历史坏掉
全部 fail-open。
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE.parent / "astrbot_plugin_autonomous_social"
sys.path.insert(0, str(PLUGIN_ROOT))

from social.closing import (  # noqa: E402
    DEFAULT_WINDOW_SECONDS,
    MAX_BOT_CLOSING_CHARS,
    MAX_CLOSING_CHARS,
    closing_categories,
    closing_decision,
    closing_instruction,
    is_closing,
    normalize,
    role_closed,
)

# 固定一个「现在」，所有用例都相对它算，避免测试自己变成时间依赖。
NOW = 1_700_000_000.0


def _line(direction: str, text, ts) -> dict:
    return {"dir": direction, "text": text, "ts": ts}


def _decide(current_text, dialogue, at=NOW, **kwargs) -> dict:
    return closing_decision(current_text=current_text, dialogue=dialogue, at=at, **kwargs)


def _closed_exchange(role_text="晚安", role_ago=60.0, user_text="今天好累", user_ago=120.0):
    """她窗口内说过一句收尾，对方上一句是普通的、不带收尾词的一句话。

    注意别拿「我先去睡了」当默认值：它含「去睡了」，本身就是收尾词，会把
    prior_user_closing 点亮成 True，那是实现正确、测试数据错。
    """
    return [
        _line("in", user_text, NOW - user_ago),
        _line("out", role_text, NOW - role_ago),
    ]


class NormalizeTest(unittest.TestCase):
    def test_drops_punctuation_and_space(self) -> None:
        self.assertEqual(normalize(" 晚安！ "), "晚安")

    def test_keeps_digits(self) -> None:
        # 数字必须留：「23:00 晚安」靠它才不会被当成纯收尾。
        self.assertEqual(normalize("23:00 晚安"), "2300晚安")

    def test_drops_media_placeholder(self) -> None:
        self.assertEqual(normalize("[图片] 晚安"), "晚安")
        self.assertEqual(normalize("【语音 3秒】晚安"), "晚安")

    def test_casefolds_english(self) -> None:
        self.assertEqual(normalize("GoodNight"), "goodnight")

    def test_emoji_only_is_empty(self) -> None:
        # 表情属于符号类，纯表情不是收尾（也不是任何词）。
        self.assertEqual(normalize("🌙✨"), "")

    def test_none_is_empty(self) -> None:
        self.assertEqual(normalize(None), "")


class CategoriesTest(unittest.TestCase):
    def test_pure_goodnight(self) -> None:
        self.assertEqual(closing_categories("晚安"), ("goodnight",))

    def test_goodnight_with_particle(self) -> None:
        # 语气词不能把收尾词顶掉：「晚安啦」仍然是晚安。
        self.assertEqual(closing_categories("晚安啦"), ("goodnight",))

    def test_multiple_categories_in_first_seen_order(self) -> None:
        self.assertEqual(closing_categories("晚安，谢谢"), ("goodnight", "thanks"))

    def test_question_mark_kills_closing(self) -> None:
        self.assertEqual(closing_categories("晚安？"), ())
        self.assertEqual(closing_categories("晚安?"), ())

    def test_time_prefix_is_not_closing(self) -> None:
        # 「23:00 晚安」是「我 23 点睡」，带信息量，不能静默。
        self.assertEqual(closing_categories("23:00 晚安"), ())

    def test_long_text_is_not_closing(self) -> None:
        self.assertEqual(closing_categories("晚安" * 25), ())

    def test_boundary_48_chars_is_closing(self) -> None:
        text = "晚安" * (MAX_CLOSING_CHARS // 2)
        self.assertEqual(len(normalize(text)), MAX_CLOSING_CHARS)
        self.assertEqual(closing_categories(text), ("goodnight",))

    def test_ack_has_its_own_category(self) -> None:
        self.assertEqual(closing_categories("好的"), ("ack",))

    def test_is_closing_matches_categories(self) -> None:
        self.assertTrue(is_closing("拜拜"))
        self.assertFalse(is_closing("今天好累"))
        self.assertFalse(is_closing(""))


class RoleClosedTest(unittest.TestCase):
    def test_plain_goodnight(self) -> None:
        self.assertTrue(role_closed("晚安"))

    def test_with_leading_body(self) -> None:
        self.assertTrue(role_closed("今天辛苦了，好好休息，晚安"))

    def test_with_trailing_address(self) -> None:
        self.assertTrue(role_closed("晚安宝贝"))

    def test_rejects_question(self) -> None:
        self.assertFalse(role_closed("晚安？"))
        self.assertFalse(role_closed("你睡了吗"))

    def test_rejects_ack(self) -> None:
        # 应声不是收尾：她只说了「好的」，对方第一次说晚安仍该得到一句回应。
        self.assertFalse(role_closed("好的"))
        self.assertFalse(role_closed("嗯嗯"))

    def test_rejects_long_essay_with_goodnight_tail(self) -> None:
        text = "今天" * (MAX_BOT_CLOSING_CHARS // 2 + 5) + "晚安"
        self.assertGreater(len(normalize(text)), MAX_BOT_CLOSING_CHARS)
        self.assertFalse(role_closed(text))

    def test_rejects_empty(self) -> None:
        self.assertFalse(role_closed(""))
        self.assertFalse(role_closed(None))
        self.assertFalse(role_closed("   "))


class ClosingDecisionTest(unittest.TestCase):
    def test_suppresses_after_role_closed(self) -> None:
        decision = _decide("晚安", _closed_exchange())
        self.assertTrue(decision["suppress_reply"])
        self.assertEqual(decision["action"], "silent")
        self.assertEqual(decision["reason"], "role_already_closed")
        self.assertEqual(decision["last_reply_at"], NOW - 60)
        self.assertTrue(decision["last_assistant_closing"])
        self.assertFalse(decision["prior_user_closing"])
        self.assertEqual(decision["categories"], ("goodnight",))
        self.assertEqual(decision["window_seconds"], DEFAULT_WINDOW_SECONDS)

    def test_suppresses_goodnight_with_particle(self) -> None:
        decision = _decide("晚安啦～", _closed_exchange())
        self.assertTrue(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "role_already_closed")

    def test_question_always_replies(self) -> None:
        decision = _decide("晚安？", _closed_exchange())
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["action"], "reply")
        self.assertEqual(decision["reason"], "not_a_closing")

    def test_time_prefix_replies(self) -> None:
        decision = _decide("23:00 晚安", _closed_exchange())
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "not_a_closing")

    def test_media_input_replies_even_for_pure_closing(self) -> None:
        decision = _decide("晚安", _closed_exchange(), has_media=True)
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "media_input")

    def test_long_message_replies(self) -> None:
        decision = _decide("晚安" * 30, _closed_exchange())
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "not_a_closing")

    def test_ack_from_role_does_not_silence(self) -> None:
        dialogue = _closed_exchange(role_text="好的", user_text="今天好累")
        decision = _decide("晚安", dialogue)
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "role_did_not_close")
        self.assertFalse(decision["last_assistant_closing"])

    def test_outside_window_replies(self) -> None:
        dialogue = _closed_exchange(role_ago=DEFAULT_WINDOW_SECONDS + 1)
        decision = _decide("晚安", dialogue)
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "outside_window")
        self.assertEqual(decision["last_reply_at"], NOW - (DEFAULT_WINDOW_SECONDS + 1))

    def test_window_boundary_still_suppresses(self) -> None:
        dialogue = _closed_exchange(role_ago=float(DEFAULT_WINDOW_SECONDS))
        decision = _decide("晚安", dialogue)
        self.assertTrue(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "role_already_closed")

    def test_custom_window_is_respected(self) -> None:
        dialogue = _closed_exchange(role_ago=120.0)
        self.assertEqual(
            _decide("晚安", dialogue, window_seconds=60)["reason"], "outside_window"
        )
        self.assertEqual(
            _decide("晚安", dialogue, window_seconds=600)["reason"], "role_already_closed"
        )

    def test_time_inversion_replies(self) -> None:
        # 她的「回复」时间晚于当前输入：历史脏了，不猜，照常回。
        dialogue = [_line("in", "我先去睡了", NOW - 120), _line("out", "晚安", NOW + 60)]
        decision = _decide("晚安", dialogue)
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "history_after_input")

    def test_repeated_user_closing_silences(self) -> None:
        # 她只应了一声「好的」，但对方上一句已经在道晚安：这就是第二次收尾。
        dialogue = [_line("in", "晚安", NOW - 120), _line("out", "好的", NOW - 60)]
        decision = _decide("晚安", dialogue)
        self.assertTrue(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "repeated_user_closing")
        self.assertTrue(decision["prior_user_closing"])

    def test_prior_user_not_closing_replies(self) -> None:
        dialogue = [_line("in", "今天好累", NOW - 120), _line("out", "好的", NOW - 60)]
        decision = _decide("晚安", dialogue)
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "role_did_not_close")
        self.assertFalse(decision["prior_user_closing"])

    def test_no_history_replies(self) -> None:
        for dialogue in ([], None, ()):
            decision = _decide("晚安", dialogue)
            self.assertFalse(decision["suppress_reply"])
            self.assertEqual(decision["reason"], "no_role_reply")

    def test_history_without_role_reply_replies(self) -> None:
        decision = _decide("晚安", [_line("in", "晚安", NOW - 60)])
        self.assertEqual(decision["reason"], "no_role_reply")

    def test_unknown_history_time_replies(self) -> None:
        dialogue = [_line("in", "我先去睡了", NOW - 120), _line("out", "晚安", None)]
        decision = _decide("晚安", dialogue)
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "history_time_unknown")

    def test_unknown_current_time_replies(self) -> None:
        decision = _decide("晚安", _closed_exchange(), at=None)
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "history_time_unknown")

    def test_disabled_replies(self) -> None:
        decision = _decide("晚安", _closed_exchange(), enabled=False)
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["action"], "reply")
        self.assertEqual(decision["reason"], "disabled")

    def test_invalid_window_falls_back_to_default(self) -> None:
        # 脏配置不该让私聊永远不回；窗口读不出来就按默认 30 分钟算。
        for bad in (0, -5, None, "abc", float("inf")):
            decision = _decide("晚安", _closed_exchange(role_ago=100.0),
                               window_seconds=bad)
            self.assertEqual(decision["window_seconds"], DEFAULT_WINDOW_SECONDS)
            self.assertTrue(decision["suppress_reply"])

    def test_non_dict_history_items_are_ignored(self) -> None:
        dialogue = [None, "x", 3, _line("in", "我先去睡了", NOW - 120),
                    _line("out", "晚安", NOW - 60)]
        decision = _decide("晚安", dialogue)
        self.assertTrue(decision["suppress_reply"])

    def test_malformed_dialogue_fails_open(self) -> None:
        for bad in (42, object(), {"dir": "out"}):
            decision = _decide("晚安", bad)
            self.assertFalse(decision["suppress_reply"])
            self.assertEqual(decision["action"], "reply")

    def test_none_current_text_replies(self) -> None:
        decision = _decide(None, _closed_exchange())
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "not_a_closing")

    def test_decision_has_all_contract_keys(self) -> None:
        keys = {
            "suppress_reply", "action", "reason", "last_reply_at", "categories",
            "last_assistant_closing", "prior_user_closing", "window_seconds",
        }
        for decision in (_decide("晚安", _closed_exchange()), _decide("今天好累", [])):
            self.assertTrue(keys.issubset(decision.keys()), decision.keys())

    def test_decision_does_not_mutate_dialogue(self) -> None:
        dialogue = _closed_exchange()
        snapshot = copy.deepcopy(dialogue)
        _decide("晚安", dialogue)
        self.assertEqual(dialogue, snapshot)


class ClosingInstructionTest(unittest.TestCase):
    def test_empty_when_suppressed(self) -> None:
        self.assertEqual(closing_instruction(_decide("晚安", _closed_exchange())), "")

    def test_prompts_when_role_closed_but_current_has_content(self) -> None:
        # 她已收尾，但对方这句带了新内容：这轮必须回，只是别再补一句晚安。
        decision = _decide("今天好累", _closed_exchange())
        self.assertEqual(decision["reason"], "not_a_closing")
        self.assertTrue(decision["last_assistant_closing"])
        text = closing_instruction(decision)
        self.assertIn("【对话收尾】", text)
        self.assertIn("上一轮", text)

    def test_prompts_when_prior_user_closed(self) -> None:
        dialogue = [_line("in", "晚安", NOW - 120), _line("out", "好的", NOW - 60)]
        decision = _decide("我到家了", dialogue)
        self.assertEqual(decision["reason"], "not_a_closing")
        self.assertTrue(decision["prior_user_closing"])
        self.assertIn("上一句", closing_instruction(decision))

    def test_empty_for_plain_reply(self) -> None:
        dialogue = [_line("in", "今天好累", NOW - 120), _line("out", "怎么啦", NOW - 60)]
        decision = _decide("工作太多了", dialogue)
        self.assertEqual(closing_instruction(decision), "")

    def test_empty_for_disabled(self) -> None:
        decision = _decide("今天好累", _closed_exchange(), enabled=False)
        self.assertEqual(closing_instruction(decision), "")

    def test_empty_for_bad_input(self) -> None:
        self.assertEqual(closing_instruction(None), "")
        self.assertEqual(closing_instruction({}), "")
        self.assertEqual(closing_instruction("x"), "")


if __name__ == "__main__":
    unittest.main()
