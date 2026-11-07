"""v2.1.3：未完事的语义复核（`social/threads.py` 的 followup_review 一组）。

旧版 `loops_resolved_by` 是「关键词命中就关」：一句「面试就这样」就把线索关掉，
关掉之后再也不会回访——误关一次等于永久丢掉一件事。现在拆成两半：

* 插件只标出「她刚追问过、这是对方的回复」（48 小时窗口 + 首次复核），把
  `followup_review` / `review_hint` 交给同轮模型判 complete / edit；
* 真要关的时候用 `loops_resolved_by_review`：除了 `_closes_loop` 与关键词，
  还得有明确结果词（或调用方已 confident），没把握就不关。

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

from social import threads  # noqa: E402

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 86400.0


def _loop(about: str = "面试", **extra) -> dict:
    item = {"about": about, "due": NOW - 10 * HOUR, "at": NOW - 20 * HOUR}
    item.update(extra)
    return {"loops": [item]}


class LoopMetaReviewTest(unittest.TestCase):
    """`loop_meta`：默认输出一个字节都不能变；review=True 只是多一条提示，不判死。"""

    def test_plain_meta_unchanged(self):
        self.assertEqual(
            threads.loop_meta("面试"),
            {
                "category": "loop",
                "intent": "followup",
                "mode": "loop",
                "msg_type": "check_in",
                "msg_type_desc": "问一句TA之前提的那件事后来怎么样了",
                "style_hint": "要明确提到那件事是什么，别只说「那个呢」；问完就停",
                "about": "面试",
            },
        )

    def test_review_adds_flag(self):
        meta = threads.loop_meta("面试", review=True)
        self.assertIs(meta["followup_review"], True)

    def test_review_adds_hint(self):
        meta = threads.loop_meta("面试", review=True)
        hint = meta["review_hint"]
        self.assertIsInstance(hint, str)
        self.assertTrue(hint.strip())
        # 提示必须把「判 complete / 判 edit」两个出口都写清楚，否则模型只会保留。
        self.assertIn("complete", hint)
        self.assertIn("edit", hint)
        # 也得提醒别把「好的」「嗯嗯」机械当结束——这是参考实现里踩过的坑。
        self.assertIn("别把", hint)

    def test_review_keeps_other_fields(self):
        plain = threads.loop_meta("复查")
        review = threads.loop_meta("复查", review=True)
        for key, value in plain.items():
            self.assertEqual(review[key], value, key)
        self.assertEqual(set(review) - set(plain), {"followup_review", "review_hint"})


class ReviewPendingTest(unittest.TestCase):
    """`loop_review_pending`：48 小时窗口 + 追问之后还没复核过。"""

    def test_pending_within_window(self):
        user = _loop(last_followup_at=NOW - 2 * HOUR, followup_count=1)
        self.assertTrue(threads.loop_review_pending(user, "面试", NOW))

    def test_not_pending_without_followup(self):
        self.assertFalse(threads.loop_review_pending(_loop(), "面试", NOW))
        self.assertFalse(
            threads.loop_review_pending(_loop(last_followup_at=0.0), "面试", NOW)
        )

    def test_expires_after_48h(self):
        inside = _loop(last_followup_at=NOW - 48 * HOUR, followup_count=1)
        outside = _loop(last_followup_at=NOW - 48 * HOUR - 1, followup_count=1)
        self.assertTrue(threads.loop_review_pending(inside, "面试", NOW))
        self.assertFalse(threads.loop_review_pending(outside, "面试", NOW))

    def test_stops_after_review(self):
        """追问之后已经复核过（last_context_at 追上来了）就不再每轮塞提示。"""
        pending = _loop(
            last_followup_at=NOW - 2 * HOUR,
            last_context_at=NOW - 3 * HOUR,
            followup_count=1,
        )
        done = _loop(
            last_followup_at=NOW - 2 * HOUR,
            last_context_at=NOW - 1 * HOUR,
            followup_count=1,
        )
        self.assertTrue(threads.loop_review_pending(pending, "面试", NOW))
        self.assertFalse(threads.loop_review_pending(done, "面试", NOW))

    def test_unknown_about_or_bad_user(self):
        self.assertFalse(threads.loop_review_pending(_loop(), "体检", NOW))
        self.assertFalse(threads.loop_review_pending({}, "面试", NOW))
        self.assertFalse(threads.loop_review_pending(None, "面试", NOW))

    def test_future_timestamp_is_not_pending(self):
        """时钟回拨/脏数据：追问时间在「现在」之后，不能当成刚追问过。"""
        user = _loop(last_followup_at=NOW + HOUR, followup_count=1)
        self.assertFalse(threads.loop_review_pending(user, "面试", NOW))


class LoopSurfacedTest(unittest.TestCase):
    """`loop_surfaced`：三天冷却 + 最多三次，但复核期必须放行。"""

    def test_fresh_loop_can_be_surfaced(self):
        self.assertTrue(threads.loop_surfaced(_loop(), "面试", NOW))

    def test_cooldown_blocks(self):
        user = _loop(last_context_at=NOW - HOUR, context_count=1)
        self.assertFalse(threads.loop_surfaced(user, "面试", NOW))

    def test_cooldown_expires_after_three_days(self):
        fresh = _loop(last_context_at=NOW - 3 * DAY + 1, context_count=1)
        old = _loop(last_context_at=NOW - 3 * DAY, context_count=1)
        self.assertFalse(threads.loop_surfaced(fresh, "面试", NOW))
        self.assertTrue(threads.loop_surfaced(old, "面试", NOW))

    def test_count_cap(self):
        self.assertFalse(
            threads.loop_surfaced(_loop(context_count=3), "面试", NOW)
        )
        self.assertTrue(threads.loop_surfaced(_loop(context_count=2), "面试", NOW))

    def test_review_window_bypasses_cooldown_and_cap(self):
        """刚追问过、对方还没回：冷却和次数上限都不能拦，否则就是「追问完不管了」。"""
        user = _loop(
            last_followup_at=NOW - HOUR,
            last_context_at=NOW - 2 * DAY,
            context_count=9,
            followup_count=1,
        )
        self.assertTrue(threads.loop_surfaced(user, "面试", NOW))

    def test_review_window_expired_falls_back_to_cooldown(self):
        user = _loop(
            last_followup_at=NOW - 49 * HOUR,
            last_context_at=NOW - 2 * DAY,
            context_count=1,
            followup_count=1,
        )
        self.assertFalse(threads.loop_surfaced(user, "面试", NOW))

    def test_unknown_about(self):
        self.assertFalse(threads.loop_surfaced(_loop(), "体检", NOW))


class MarkFollowupSentTest(unittest.TestCase):
    """`mark_followup_sent`：只有回执确认送达后才写，写的是真实条目对象。"""

    def test_sets_timestamp_and_count(self):
        user = _loop()
        threads.mark_followup_sent(user, "面试", NOW)
        item = user["loops"][0]
        self.assertEqual(item["last_followup_at"], NOW)
        self.assertEqual(item["followup_count"], 1)

    def test_second_call_updates_and_counts(self):
        user = _loop()
        threads.mark_followup_sent(user, "面试", NOW)
        threads.mark_followup_sent(user, "面试", NOW + 600)
        item = user["loops"][0]
        self.assertEqual(item["last_followup_at"], NOW + 600)
        self.assertEqual(item["followup_count"], 2)

    def test_makes_review_pending(self):
        user = _loop()
        self.assertFalse(threads.loop_review_pending(user, "面试", NOW))
        threads.mark_followup_sent(user, "面试", NOW)
        self.assertTrue(threads.loop_review_pending(user, "面试", NOW + 60))

    def test_unknown_about_is_noop(self):
        user = _loop()
        before = {"loops": [dict(user["loops"][0])]}
        threads.mark_followup_sent(user, "体检", NOW)
        self.assertEqual(user, before)

    def test_zero_time_is_noop(self):
        user = _loop()
        before = {"loops": [dict(user["loops"][0])]}
        threads.mark_followup_sent(user, "面试", 0)
        self.assertEqual(user, before)

    def test_migrates_flat_loop(self):
        """老数据只有平铺字段：先搬进 loops 再写，否则写了个现造的 dict 等于没写。"""
        user = {"loop": "复查", "loop_due": NOW - HOUR, "loop_at": NOW - 20 * HOUR}
        threads.mark_followup_sent(user, "复查", NOW)
        self.assertIsInstance(user.get("loops"), list)
        self.assertEqual(user["loops"][0]["about"], "复查")
        self.assertEqual(user["loops"][0]["last_followup_at"], NOW)
        self.assertTrue(threads.loop_review_pending(user, "复查", NOW + 60))

    def test_bad_user_does_not_raise(self):
        for user in (None, {}, {"loops": "oops"}, {"loops": [None, 3]}):
            with self.subTest(user=user):
                threads.mark_followup_sent(user, "面试", NOW)


class LoopsResolvedByReviewTest(unittest.TestCase):
    """`loops_resolved_by_review`：误关保护。关掉就再也不会回访，宁可不关。"""

    def test_closes_with_result_word(self):
        user = _loop()
        self.assertEqual(threads.loops_resolved_by_review(user, "面试过了"), ["面试"])

    def test_keeps_open_without_result_word(self):
        """同一句旧函数会关、新函数不关——这就是要保护的那类误关。"""
        user = _loop()
        self.assertEqual(threads.loops_resolved_by(user, "面试就这样"), ["面试"])
        self.assertEqual(threads.loops_resolved_by_review(user, "面试就这样"), [])

    def test_keeps_open_when_not_closing(self):
        user = _loop()
        self.assertEqual(threads.loops_resolved_by_review(user, "面试还没消息"), [])

    def test_keyword_must_match_the_loop(self):
        """说了结果，但说的是另一件事（体检）→ 不能把面试那条关掉。"""
        user = _loop()
        self.assertEqual(threads.loops_resolved_by_review(user, "体检搞定了"), [])

    def test_confident_closes_without_result_word(self):
        """同轮模型判过「这句就是收尾」时，可以放行没有结果词的说法。"""
        user = _loop()
        self.assertEqual(
            threads.loops_resolved_by_review(user, "面试就这样", confident=True), ["面试"]
        )

    def test_does_not_mutate_user(self):
        user = _loop()
        before = {"loops": [dict(user["loops"][0])]}
        threads.loops_resolved_by_review(user, "面试过了")
        self.assertEqual(user, before)


if __name__ == "__main__":
    unittest.main()
