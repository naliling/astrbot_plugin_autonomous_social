"""v2.1.5：平台级发送熔断（`social/outbound.py`）。

这个模块本身不发消息，它决定的是「还发不发」。所以要钉的不是「发得出去」，
而是**判错的代价**：

* 把「风控拒绝」当成普通失败 → 每轮心跳继续撞，账号被罚得更久（线上事故）；
* 把「不是好友」当成账号拒绝 → 一个人没了，她对所有人闭嘴；
* 把「超时」当成没发出去 → 可能已经发到了，重发就是两条一样的话；
* 熔断状态被手改坏 / 时钟错乱 → 宁可照常发（fail-open），也不要永久静音。

因此测试分三块：四态分类、窗口熔断（含脏数据与成功清零）、fail-open 闸门。

按仓库惯例自带最小 astrbot 桩（照抄 tests/test_v213_followup_review.py 头部）。
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

from social import outbound  # noqa: E402

NOW = 1_700_000_000.0
MINUTE = 60.0
HOUR = 3600.0


class MarkersTest(unittest.TestCase):
    """常量本身是外部契约：engine 侧按这些名字做匹配，改名字要一起改。"""

    def test_platform_markers_cover_the_documented_set(self):
        for marker in ("1200", "qq_eventchecker", "eventchecker", "retcode 1200", "风控", "频率限制"):
            self.assertIn(marker, outbound.PLATFORM_REFUSAL_MARKERS)

    def test_other_marker_groups_are_non_empty(self):
        self.assertTrue(outbound.TARGET_GONE_MARKERS)
        self.assertTrue(outbound.UNKNOWN_SEND_MARKERS)


class ClassifyPlatformRefusedTest(unittest.TestCase):
    """账号级拒绝：这一类唯一会触发熔断，必须认得全。"""

    def test_retcode_1200(self):
        got = outbound.classify_send_error("retcode 1200")
        self.assertEqual(got, {"state": "not_sent", "code": "platform_refused", "platform_refused": True})

    def test_retcode_1200_with_separator(self):
        # 平台文案前缀千奇百怪："RetCode=1200"、"code:1200"，只认一种写法就会漏。
        for text in ("RetCode=1200", "code:1200", "errno 1200", "1200"):
            with self.subTest(text=text):
                self.assertTrue(outbound.classify_send_error(text)["platform_refused"])

    def test_eventchecker(self):
        for text in ("qq_eventchecker rejected", "eventchecker", "QQ_EVENTCHECKER"):
            with self.subTest(text=text):
                self.assertTrue(outbound.classify_send_error(text)["platform_refused"])

    def test_chinese_refusal_words(self):
        for text in ("发送失败：风控拦截", "触发频率限制", "账号被风控"):
            with self.subTest(text=text):
                self.assertEqual(outbound.classify_send_error(text)["code"], "platform_refused")

    def test_platform_refusal_wins_over_timeout_words(self):
        # 同一段文案里既有拒绝又有超时：按账号级处理，宁可停手。
        got = outbound.classify_send_error("eventchecker timeout after 10s")
        self.assertEqual(got["code"], "platform_refused")

    def test_number_inside_a_longer_number_is_not_a_retcode(self):
        # "12001" / "1200.5" 里的 1200 不是 retcode；误熔断一次就是两小时不说话。
        for text in ("error 12001", "cost 1200.5 ms"):
            with self.subTest(text=text):
                self.assertFalse(outbound.classify_send_error(text)["platform_refused"])

    def test_bare_number_in_an_error_string_counts(self):
        # 这个函数**只**被喂发送失败的文案，不是用户正文，所以裸 1200 就算 retcode：
        # 真实的 1200 拒绝有时只回一个数字。代价是「余额 1200 元不足」这类文案也会
        # 被当成拒绝——但那是平台侧的余额错误，本来也不该继续试。
        self.assertTrue(outbound.classify_send_error("余额 1200 元不足")["platform_refused"])


class ClassifyTargetGoneTest(unittest.TestCase):
    """对方侧没了：换个人照样能发，绝不能熔断。"""

    def test_not_friend(self):
        for text in ("不是好友", "非好友关系", "对方不是好友，无法发送"):
            with self.subTest(text=text):
                got = outbound.classify_send_error(text)
                self.assertEqual(got, {"state": "not_sent", "code": "target_gone", "platform_refused": False})

    def test_session_and_group_gone(self):
        for text in ("会话失效", "会话不存在", "群解散", "群已解散"):
            with self.subTest(text=text):
                self.assertEqual(outbound.classify_send_error(text)["code"], "target_gone")

    def test_english_target_gone(self):
        for text in ("session expired", "group not found", "user not found"):
            with self.subTest(text=text):
                self.assertEqual(outbound.classify_send_error(text)["code"], "target_gone")

    def test_target_gone_is_never_platform_refused(self):
        got = outbound.classify_send_error("群解散")
        self.assertFalse(got["platform_refused"])


class ClassifyUnknownTest(unittest.TestCase):
    """不知道发没发出去：既不算成功也不算失败，更不熔断。"""

    def test_empty_text_is_unknown(self):
        for text in ("", "   ", None):
            with self.subTest(text=repr(text)):
                got = outbound.classify_send_error(text)
                self.assertEqual(got, {"state": "unknown", "code": "unknown", "platform_refused": False})

    def test_timeout_is_unknown(self):
        for text in ("timeout", "Timed out", "请求超时"):
            with self.subTest(text=text):
                self.assertEqual(outbound.classify_send_error(text)["state"], "unknown")

    def test_connection_jitter_is_unknown(self):
        for text in ("connection reset by peer", "ECONNRESET", "网络断开"):
            with self.subTest(text=text):
                self.assertEqual(outbound.classify_send_error(text)["state"], "unknown")

    def test_unknown_code_matches_state(self):
        got = outbound.classify_send_error("timeout")
        self.assertEqual(got["code"], "unknown")
        self.assertFalse(got["platform_refused"])


class ClassifyOtherAndDirtyInputTest(unittest.TestCase):
    """其余错误只记账；脏输入一律不抛（回执是异常对象，什么都可能有）。"""

    def test_plain_error_is_other(self):
        got = outbound.classify_send_error("send failed: internal error")
        self.assertEqual(got, {"state": "not_sent", "code": "other", "platform_refused": False})

    def test_exception_object_is_accepted(self):
        self.assertEqual(
            outbound.classify_send_error(RuntimeError("qq_eventchecker 1200"))["code"],
            "platform_refused",
        )

    def test_bytes_are_decoded(self):
        self.assertEqual(
            outbound.classify_send_error("风控拦截".encode("utf-8"))["code"],
            "platform_refused",
        )

    def test_weird_objects_never_raise(self):
        class Weird:
            def __str__(self):
                raise RuntimeError("boom")

        for value in (Weird(), object(), 0, [], {"a": 1}):
            with self.subTest(value=type(value).__name__):
                got = outbound.classify_send_error(value)
                self.assertIn(got["state"], ("unknown", "not_sent"))
                self.assertIn("code", got)

    def test_result_shape_is_exactly_three_keys(self):
        got = outbound.classify_send_error("whatever")
        self.assertEqual(set(got), {"state", "code", "platform_refused"})
        self.assertIsInstance(got["platform_refused"], bool)


class CircuitStateTest(unittest.TestCase):
    """窗口熔断：2 次 / 10 分钟 → 冷却 2 小时（与参考实现同口径）。"""

    def test_defaults_match_the_reference(self):
        self.assertEqual(outbound.CIRCUIT_WINDOW_SECONDS, 600.0)
        self.assertEqual(outbound.CIRCUIT_THRESHOLD, 2)
        self.assertEqual(outbound.CIRCUIT_COOLDOWN_SECONDS, 7200.0)

    def test_no_events_is_closed(self):
        state = outbound.circuit_state([], NOW)
        self.assertFalse(state["open"])
        self.assertEqual(state["recent"], 0)
        self.assertEqual(state["until"], 0.0)

    def test_one_failure_is_below_threshold(self):
        state = outbound.circuit_state([(NOW - MINUTE, False)], NOW)
        self.assertFalse(state["open"])
        self.assertEqual(state["recent"], 1)

    def test_two_failures_open_the_circuit(self):
        state = outbound.circuit_state([(NOW - 5 * MINUTE, False), (NOW - MINUTE, False)], NOW)
        self.assertTrue(state["open"])
        self.assertEqual(state["recent"], 2)
        self.assertEqual(state["until"], (NOW - MINUTE) + 7200.0)

    def test_until_is_last_failure_plus_cooldown(self):
        state = outbound.circuit_state([(NOW - 9 * MINUTE, False), (NOW - 3 * MINUTE, False)], NOW)
        self.assertEqual(state["until"], (NOW - 3 * MINUTE) + 7200.0)

    def test_failures_outside_window_do_not_count(self):
        state = outbound.circuit_state([(NOW - 11 * MINUTE, False), (NOW - 9 * MINUTE, False)], NOW)
        self.assertFalse(state["open"])
        self.assertEqual(state["recent"], 1)

    def test_success_clears_everything(self):
        state = outbound.circuit_state([(NOW - 5 * MINUTE, False), (NOW - MINUTE, True)], NOW)
        self.assertFalse(state["open"])
        self.assertEqual(state["recent"], 0)
        self.assertEqual(state["until"], 0.0)

    def test_success_resets_the_count_for_later_failures(self):
        state = outbound.circuit_state(
            [(NOW - 5 * MINUTE, False), (NOW - 4 * MINUTE, False), (NOW - 3 * MINUTE, True), (NOW - MINUTE, False)],
            NOW,
        )
        self.assertFalse(state["open"])
        self.assertEqual(state["recent"], 1)

    def test_failures_after_success_accumulate_again(self):
        state = outbound.circuit_state(
            [(NOW - 5 * MINUTE, True), (NOW - 3 * MINUTE, False), (NOW - MINUTE, False)],
            NOW,
        )
        self.assertTrue(state["open"])

    def test_old_success_does_not_pardon_todays_refusals(self):
        # 三天前成功过一次，不代表今天这两次拒绝不算数。
        state = outbound.circuit_state(
            [(NOW - 3 * 24 * HOUR, True), (NOW - 2 * MINUTE, False), (NOW - MINUTE, False)],
            NOW,
        )
        self.assertTrue(state["open"])
        self.assertEqual(state["recent"], 2)

    def test_cooldown_expiry_closes_the_circuit(self):
        state = outbound.circuit_state(
            [(NOW - 20 * MINUTE, False), (NOW - 10 * MINUTE, False)],
            NOW,
            window_seconds=3600.0,
            cooldown_seconds=60.0,
        )
        # 窗口内两次 → 本该开，但 until 已经过去 → 自动关。
        self.assertEqual(state["recent"], 2)
        self.assertFalse(state["open"])

    def test_threshold_is_configurable(self):
        events = [(NOW - 3 * MINUTE, False), (NOW - 2 * MINUTE, False)]
        self.assertFalse(outbound.circuit_state(events, NOW, threshold=3)["open"])
        self.assertTrue(
            outbound.circuit_state(events + [(NOW - MINUTE, False)], NOW, threshold=3)["open"]
        )

    def test_dirty_events_are_skipped_not_fatal(self):
        events = [
            "garbage",
            (NOW - MINUTE, False, True),
            (None, False),
            (NOW - MINUTE, "yes"),
            {"ts": NOW, "ok": False},
            (NOW - 2 * MINUTE, False),
            (NOW - MINUTE, False),
        ]
        state = outbound.circuit_state(events, NOW)
        self.assertEqual(state["recent"], 2)
        self.assertTrue(state["open"])

    def test_future_events_are_ignored(self):
        state = outbound.circuit_state([(NOW + HOUR, False), (NOW + HOUR, False)], NOW)
        self.assertEqual(state["recent"], 0)
        self.assertFalse(state["open"])

    def test_numeric_ok_flags_are_accepted(self):
        state = outbound.circuit_state([(NOW - 5 * MINUTE, 0), (NOW - 4 * MINUTE, 1), (NOW - MINUTE, 0)], NOW)
        self.assertEqual(state["recent"], 1)
        self.assertFalse(state["open"])

    def test_events_none_or_not_iterable_is_closed(self):
        for events in (None, 42):
            with self.subTest(events=repr(events)):
                state = outbound.circuit_state(events, NOW)
                self.assertFalse(state["open"])

    def test_result_shape(self):
        state = outbound.circuit_state([], NOW)
        self.assertEqual(
            set(state),
            {"open", "until", "recent", "window_seconds", "threshold", "cooldown_seconds"},
        )
        self.assertIsInstance(state["open"], bool)
        self.assertIsInstance(state["recent"], int)
        self.assertIsInstance(state["until"], float)

    def test_invalid_parameters_raise_value_error(self):
        bad_calls = [
            {"window_seconds": 0},
            {"window_seconds": -1},
            {"window_seconds": "600"},
            {"threshold": 0},
            {"threshold": 1.5},
            {"threshold": True},
            {"cooldown_seconds": -1},
            {"cooldown_seconds": "7200"},
            {"cooldown_seconds": float("inf")},
            {"window_seconds": float("inf")},
        ]
        for kwargs in bad_calls:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    outbound.circuit_state([], NOW, **kwargs)

    def test_invalid_now_raises_value_error(self):
        for now in ("now", None, True, float("nan"), float("inf")):
            with self.subTest(now=repr(now)):
                with self.assertRaises(ValueError):
                    outbound.circuit_state([], now)


class CircuitAllowsTest(unittest.TestCase):
    """闸门：唯一允许说「不发」的地方，所以所有异常路径都必须放行。"""

    def _open_state(self, until):
        return {"open": True, "until": until, "recent": 2}

    def test_open_state_blocks_inside_cooldown(self):
        self.assertFalse(outbound.circuit_allows(self._open_state(NOW + HOUR), NOW))

    def test_open_state_allows_after_cooldown(self):
        self.assertTrue(outbound.circuit_allows(self._open_state(NOW - 1), NOW))
        self.assertTrue(outbound.circuit_allows(self._open_state(NOW), NOW))

    def test_closed_state_allows(self):
        self.assertTrue(outbound.circuit_allows({"open": False, "until": NOW + HOUR}, NOW))

    def test_bad_state_allows(self):
        for state in (None, {}, [], "open", 42, {"open": "yes"}, {"open": True}):
            with self.subTest(state=repr(state)):
                self.assertTrue(outbound.circuit_allows(state, NOW))

    def test_bad_until_allows(self):
        for until in (None, "soon", True, [], float("nan")):
            with self.subTest(until=repr(until)):
                self.assertTrue(outbound.circuit_allows({"open": True, "until": until}, NOW))

    def test_bad_now_allows(self):
        for now in ("now", None, True):
            with self.subTest(now=repr(now)):
                self.assertTrue(outbound.circuit_allows(self._open_state(NOW + HOUR), now))

    def test_never_raises(self):
        class Weird:
            def get(self, *args, **kwargs):
                raise RuntimeError("boom")

        self.assertTrue(outbound.circuit_allows(Weird(), NOW))


if __name__ == "__main__":
    unittest.main()
