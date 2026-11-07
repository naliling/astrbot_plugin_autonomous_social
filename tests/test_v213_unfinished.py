"""v2.1.3：半截话三态判断（`social/threads.py` 的 `unfinished_signal`）。

对方发「跟你说个事…」的时候，旧版把这条当完整消息去回/去记，回复就抢了话。这个
纯函数只判断「这条消息说完了没有」，不合并、不缓存、不阻塞——等不等、怎么回是同
一轮模型的事。这里钉住三态边界：问号、长句、纯表情、英文、20 字分界、极短回应。

按仓库惯例自带最小 astrbot 桩（照抄 tests/test_p2_threads.py 头部）。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与 test_p2_threads.py 同款，幂等） ───
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
    api.logger = logging.getLogger("autonomous_social_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _FakeMessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import input_state, threads  # noqa: E402

STATES = {"complete", "unfinished", "uncertain"}


class ExplicitMarkerTest(unittest.TestCase):
    """对方**明说**还要补充：这时候回话就是抢话，必须判 unfinished。"""

    def test_chinese_marker(self):
        signal = threads.unfinished_signal("我还没说完")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "explicit_marker")
        self.assertIn("我还没说完", signal["markers"])

    def test_marker_inside_longer_sentence(self):
        """判据是「这句话里有没有这个说法」，不是整条必须等于它。"""
        signal = threads.unfinished_signal("等一下，我还没说完，你别急着回")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "explicit_marker")

    def test_ask_to_wait_before_reply(self):
        signal = threads.unfinished_signal("先别回，我马上补充")
        self.assertEqual(signal["state"], "unfinished")
        self.assertIn("先别回", signal["markers"])

    def test_english_marker(self):
        signal = threads.unfinished_signal("I'm not done yet")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "explicit_marker")

    def test_english_phrase_needs_word_boundary(self):
        """「someone second」里也含「one sec」：英文短语必须整词命中，否则正常英文全被误判。"""
        signal = threads.unfinished_signal("someone second")
        self.assertEqual(signal["state"], "complete")
        self.assertEqual(signal["reason"], "complete")

    def test_marker_beats_question(self):
        """明说「还没说完」压过问号：她要求的是听完再答。"""
        signal = threads.unfinished_signal("我还没说完，你吃了吗？")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "explicit_marker")


class DanglingTailTest(unittest.TestCase):
    """话停在悬空标点上：省略号、逗号、重复句号——写到这里就是还要往下说。"""

    def test_ellipsis_opener(self):
        signal = threads.unfinished_signal("跟你说个事…")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "dangling_tail")
        self.assertEqual(signal["markers"], ("…",))

    def test_ellipsis_after_hesitation(self):
        signal = threads.unfinished_signal("那个……")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "dangling_tail")

    def test_comma_tail(self):
        signal = threads.unfinished_signal("我今天碰到一个老同学，")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "dangling_tail")
        self.assertEqual(signal["markers"], ("，",))

    def test_repeated_period(self):
        """「好吧。。。」不是句号收尾，是话没说完的那种省略。"""
        signal = threads.unfinished_signal("好吧。。。")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "dangling_tail")

    def test_question_mark_wins_over_dangling_tail(self):
        """带问号的是在等她答，不是在等自己补下文。"""
        signal = threads.unfinished_signal("你猜…？")
        self.assertEqual(signal["state"], "complete")
        self.assertEqual(signal["reason"], "question")

    def test_twenty_chars_is_still_unfinished(self):
        """20 字是分界：以内按「刚开了个头」处理。"""
        signal = threads.unfinished_signal("我" * 19 + "，")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "dangling_tail")

    def test_twenty_one_chars_is_uncertain(self):
        """超过 20 字还停在省略号上，更像被截断：看不出会不会继续，只能 uncertain。"""
        signal = threads.unfinished_signal("我" * 20 + "，")
        self.assertEqual(signal["state"], "uncertain")
        self.assertEqual(signal["reason"], "truncated_ellipsis")


class DanglingHeadTest(unittest.TestCase):
    """话停在起头词上：「那个」「我想」「跟你说个事」——开头就是全部内容。"""

    def test_head_word_alone(self):
        signal = threads.unfinished_signal("那个")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "dangling_head")
        self.assertEqual(signal["markers"], ("那个",))

    def test_first_person_want(self):
        signal = threads.unfinished_signal("我想")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "dangling_head")

    def test_head_word_at_tail(self):
        signal = threads.unfinished_signal("嗯那个")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "dangling_head")

    def test_opener_phrase_alone(self):
        signal = threads.unfinished_signal("跟你说个事")
        self.assertEqual(signal["state"], "unfinished")
        self.assertEqual(signal["reason"], "dangling_head")

    def test_head_word_inside_sentence_not_triggered(self):
        """起头词出现在句中（不是在收尾）不算没说完。"""
        signal = threads.unfinished_signal("那个电影我看完了")
        self.assertEqual(signal["state"], "complete")
        self.assertEqual(signal["reason"], "complete")


class CompleteTest(unittest.TestCase):
    """说完了就是 complete：问句、完整陈述、纯表情、占位、空串都算。"""

    def test_questions_are_complete(self):
        for text in ("你吃了吗？", "在吗", "什么是日食？", "你猜…？"):
            with self.subTest(text=text):
                signal = threads.unfinished_signal(text)
                self.assertEqual(signal["state"], "complete")
                self.assertEqual(signal["reason"], "question")

    def test_plain_statement(self):
        signal = threads.unfinished_signal("今天下雨了")
        self.assertEqual(signal["state"], "complete")
        self.assertEqual(signal["reason"], "complete")

    def test_english_sentence(self):
        """英文单句没有中文句读，但也不该被当成「刚开了个头」。"""
        signal = threads.unfinished_signal("hello")
        self.assertEqual(signal["state"], "complete")
        self.assertEqual(signal["reason"], "complete")

    def test_emoji_only(self):
        signal = threads.unfinished_signal("😂😂😂")
        self.assertEqual(signal["state"], "complete")
        self.assertEqual(signal["reason"], "non_verbal")

    def test_placeholder(self):
        """「[表情]」「[图片]」是素材占位，不是她打到一半的话。"""
        signal = threads.unfinished_signal("[表情]")
        self.assertEqual(signal["state"], "complete")
        self.assertEqual(signal["reason"], "placeholder")

    def test_empty(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                signal = threads.unfinished_signal(text)
                self.assertEqual(signal["state"], "complete")
                self.assertEqual(signal["reason"], "empty")

    def test_single_period(self):
        signal = threads.unfinished_signal("。")
        self.assertEqual(signal["state"], "complete")
        self.assertEqual(signal["reason"], "non_verbal")


class UncertainTest(unittest.TestCase):
    """看不出会不会继续：不能替她等，也不能当完整消息硬回，只能标 uncertain。"""

    def test_long_ellipsis(self):
        signal = threads.unfinished_signal(
            "今天下午我去医院复查了，医生说还要再观察几天看看情况……"
        )
        self.assertEqual(signal["state"], "uncertain")
        self.assertEqual(signal["reason"], "truncated_ellipsis")

    def test_ellipsis_only(self):
        signal = threads.unfinished_signal("……")
        self.assertEqual(signal["state"], "uncertain")
        self.assertEqual(signal["reason"], "ellipsis_only")

    def test_short_reply_without_punct(self):
        """「好的」「嗯」也可能是完整回应——纯函数不猜，标 uncertain 交给模型看。"""
        for text in ("好的", "嗯"):
            with self.subTest(text=text):
                signal = threads.unfinished_signal(text)
                self.assertEqual(signal["state"], "uncertain")
                self.assertEqual(signal["reason"], "short_no_punct")

    def test_short_english_fragment(self):
        signal = threads.unfinished_signal("ok")
        self.assertEqual(signal["state"], "uncertain")
        self.assertEqual(signal["reason"], "short_no_punct")

    def test_three_char_fragment(self):
        signal = threads.unfinished_signal("我在想")
        self.assertEqual(signal["state"], "uncertain")
        self.assertEqual(signal["reason"], "short_no_punct")


class SignalShapeTest(unittest.TestCase):
    """返回值形状与 fail-open：调用方要能无条件读，不因一条怪消息炸掉整轮。"""

    def test_shape(self):
        signal = threads.unfinished_signal("跟你说个事…")
        self.assertIsInstance(signal, dict)
        self.assertEqual(set(signal), {"state", "reason", "markers"})
        self.assertIn(signal["state"], STATES)
        self.assertIsInstance(signal["reason"], str)
        self.assertIsInstance(signal["markers"], tuple)

    def test_never_raises(self):
        for value in (123, 0, object(), ["a"], {"x": 1}, True):
            with self.subTest(value=type(value).__name__):
                signal = threads.unfinished_signal(value)
                self.assertIn(signal["state"], STATES)

    def test_repeatable(self):
        """纯函数：同一输入两次结果一样，没有缓存/副作用。"""
        first = threads.unfinished_signal("那个……")
        second = threads.unfinished_signal("那个……")
        self.assertEqual(first, second)


class WaitReasonTest(unittest.TestCase):
    """input_state.wait_reason：只有 unfinished 才给「这一轮先等」的理由码。"""

    def test_unfinished_carries_reason_code(self):
        reason = input_state.wait_reason(
            {"state": "unfinished", "reason": "explicit_marker", "markers": ("我还没说完",)}
        )
        self.assertEqual(reason, "await_unfinished:explicit_marker")

    def test_unfinished_without_detail(self):
        self.assertEqual(
            input_state.wait_reason({"state": "unfinished"}), "await_unfinished"
        )

    def test_uncertain_does_not_wait(self):
        """uncertain 看不出会不会继续，等下去就是拿静默赌猜测——照常回。"""
        self.assertEqual(
            input_state.wait_reason({"state": "uncertain", "reason": "short_no_punct"}), ""
        )

    def test_complete_does_not_wait(self):
        self.assertEqual(input_state.wait_reason({"state": "complete"}), "")

    def test_bad_input_returns_empty(self):
        for value in (None, {}, {"state": None}, {"state": "nonsense"}, "x", 3):
            with self.subTest(value=value):
                self.assertEqual(input_state.wait_reason(value), "")

    def test_works_with_real_signal(self):
        reason = input_state.wait_reason(threads.unfinished_signal("我还没说完"))
        self.assertEqual(reason, "await_unfinished:explicit_marker")


if __name__ == "__main__":
    unittest.main()
