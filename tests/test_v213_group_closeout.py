"""v2.1.3：群硬收口（`social.throttle.closeout_state` / `closeout_allows`）。

语义照 `companion_v2/conversation/group_high_intensity.py`：她连续被唤醒
`threshold` 轮（首尾落在 `window_seconds` 内）之后，接下来 `cooldown_seconds`
只放行真正的点名/回复。**状态从有界历史重算**，不落一份可变的「冷却到什么时候」
——跨进程、重放、时钟回拨、手改坏字段都不会让收口漂移（见 `closeout_state` 的注释）。

用例分组：
- A 组 burst：未达阈值 / 刚好达标 / 窗口边界 / 窗口外 / 冷却中 / 冷却到期 / 多段叠加；
- B 组 脏历史：未来时间、None、nan、inf、bool 一律跳过；
- C 组 参数校验：at / window / threshold / cooldown 越界抛 ValueError；
- D 组 放行：白名单 / 非白名单 / 未激活 / 坏 state fail-open / 与归因对齐。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与 test_p2_groupflow_pacing.py 同款，幂等） ───
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
    api.logger = logging.getLogger("autonomous_social_v213_closeout_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _FakeMessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import groupflow, throttle  # noqa: E402

NOW = 1_700_000_000.0
# 5 分钟内 3 轮 → 静默 15 分钟（与模块默认值一致，测试里显式写出来免得默认值改了才发现）
WINDOW = 300
THRESHOLD = 3
COOLDOWN = 900


def _burst_state(at=NOW, **kwargs):
    """构造一个「刚好在 NOW 前 200/100/0 秒各说一轮」的收口状态。"""
    rounds = [NOW - 200, NOW - 100, NOW]
    return throttle.closeout_state(rounds, at, **kwargs)


class BurstTest(unittest.TestCase):
    """A 组：burst 的边界。"""

    def test_below_threshold_is_inactive(self):
        """5 分钟内只说了 2 轮：还不到「密」，不收口。"""
        got = throttle.closeout_state([NOW - 100, NOW - 50], NOW)
        self.assertFalse(got["active"])
        self.assertEqual(got["recent_wakeups"], 2)
        self.assertEqual(got["until"], 0.0)
        self.assertEqual(got["remaining_seconds"], 0)

    def test_threshold_reached_activates(self):
        """刚好 3 轮且首尾 200 秒内：收口 15 分钟，截止时间 = 最后一轮 + 900。"""
        got = _burst_state()
        self.assertTrue(got["active"])
        self.assertEqual(got["until"], NOW + COOLDOWN)
        self.assertEqual(got["remaining_seconds"], COOLDOWN)
        self.assertEqual(got["recent_wakeups"], 3)

    def test_window_boundary_is_inclusive(self):
        """首尾正好隔 window 秒仍算一个 burst（<= 语义，与参考实现一致）。"""
        got = throttle.closeout_state([NOW - 300, NOW - 150, NOW], NOW)
        self.assertTrue(got["active"])
        self.assertEqual(got["until"], NOW + COOLDOWN)

    def test_burst_must_fit_window(self):
        """3 轮散在 10 分钟里：跨度超过窗口，不算「密」，不收口。"""
        got = throttle.closeout_state([NOW - 600, NOW - 300, NOW], NOW)
        self.assertFalse(got["active"])
        # 但「最近 5 分钟说了几轮」照样照实回显（日志/调参要看）
        self.assertEqual(got["recent_wakeups"], 2)

    def test_cooldown_still_running(self):
        """收口进行到一半：active 且剩余秒数递减。"""
        got = _burst_state(at=NOW + 450)
        self.assertTrue(got["active"])
        self.assertEqual(got["remaining_seconds"], 450)

    def test_cooldown_expires(self):
        """冷却到期：立刻恢复（不需要谁去「清」一个字段）。"""
        got = _burst_state(at=NOW + COOLDOWN + 1)
        self.assertFalse(got["active"])
        self.assertEqual(got["until"], 0.0)
        self.assertEqual(got["remaining_seconds"], 0)

    def test_multiple_bursts_take_the_latest_until(self):
        """两段 burst 叠加时取最晚的截止时间，而不是被后一段覆盖成更早的。

        取 max 是刻意的：否则「刚收口完又被叫了三次」会得到一个更早的 until，
        收口反而被缩短。
        """
        rounds = [NOW, NOW + 100, NOW + 200, NOW + 1000, NOW + 1100, NOW + 1200]
        got = throttle.closeout_state(rounds, NOW + 1200)
        self.assertTrue(got["active"])
        self.assertEqual(got["until"], NOW + 1200 + COOLDOWN)

    def test_zero_cooldown_never_stays_active(self):
        """cooldown=0 是合法配置（只统计不静默）：burst 结束的瞬间就不 active。"""
        got = _burst_state(cooldown_seconds=0)
        self.assertFalse(got["active"])
        self.assertEqual(got["until"], 0.0)

    def test_threshold_two_is_allowed(self):
        """threshold=2 是校验下界（1 轮就算密等于每条都收口）。"""
        got = throttle.closeout_state([NOW - 10, NOW], NOW, threshold=2)
        self.assertTrue(got["active"])
        self.assertEqual(got["threshold"], 2)

    def test_state_echoes_the_parameters_used(self):
        """参数回显给日志/配置面板：调参时能确认「这次判定用的是哪套值」。"""
        got = throttle.closeout_state(
            [NOW - 10, NOW], NOW, window_seconds=60, threshold=2, cooldown_seconds=30
        )
        self.assertEqual(got["window_seconds"], 60)
        self.assertEqual(got["threshold"], 2)
        self.assertEqual(got["cooldown_seconds"], 30)
        self.assertEqual(got["until"], NOW + 30)
        self.assertEqual(got["remaining_seconds"], 30)

    def test_recompute_is_deterministic_and_order_free(self):
        """同一段历史 → 同一个结论；输入顺序不影响（重放/重启安全）。"""
        rounds = [NOW - 200, NOW - 100, NOW]
        first = throttle.closeout_state(rounds, NOW)
        second = throttle.closeout_state(list(reversed(rounds)), NOW)
        third = throttle.closeout_state(rounds, NOW)
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        # 纯函数：不就地排序/修改调用方的列表
        self.assertEqual(rounds, [NOW - 200, NOW - 100, NOW])

    def test_recent_wakeups_counts_only_the_window(self):
        """recent_wakeups 只数最近一个窗口内的轮次。"""
        got = throttle.closeout_state([NOW - 400, NOW - 100, NOW - 50], NOW)
        self.assertEqual(got["recent_wakeups"], 2)
        self.assertFalse(got["active"])


class DirtyHistoryTest(unittest.TestCase):
    """B 组：历史里的脏值跳过、不抛。"""

    def test_dirty_history_values_are_skipped(self):
        """None / 字符串 / nan / inf / bool 都不是时间戳：跳过，其余照算。"""
        rounds = [None, "abc", float("nan"), float("inf"), True, NOW - 100, NOW - 50, NOW]
        got = throttle.closeout_state(rounds, NOW)
        self.assertTrue(got["active"])
        self.assertEqual(got["recent_wakeups"], 3)

    def test_future_rounds_are_ignored(self):
        """晚于 at 的时间戳（时钟抖动/手改）不算历史，否则会凭空造出 burst。"""
        got = throttle.closeout_state([NOW + 100, NOW + 50, NOW - 10], NOW)
        self.assertFalse(got["active"])
        self.assertEqual(got["recent_wakeups"], 1)

    def test_unreadable_rounds_container_is_empty(self):
        """rounds 不是可迭代对象时按「没有历史」处理，不抛。"""
        got = throttle.closeout_state(12345, NOW)
        self.assertFalse(got["active"])
        self.assertEqual(got["recent_wakeups"], 0)


class ValidationTest(unittest.TestCase):
    """C 组：参数越界抛 ValueError（照参考实现的校验）。"""

    def test_bad_at_raises(self):
        for bad in ("abc", None, float("nan"), float("inf"), True):
            with self.subTest(at=bad):
                with self.assertRaises(ValueError):
                    throttle.closeout_state([NOW], bad)

    def test_bad_window_raises(self):
        for bad in (0, -1, 1.5, True, "300"):
            with self.subTest(window=bad):
                with self.assertRaises(ValueError):
                    throttle.closeout_state([NOW], NOW, window_seconds=bad)

    def test_bad_threshold_raises(self):
        for bad in (1, 0, -3, 2.0, True, "3"):
            with self.subTest(threshold=bad):
                with self.assertRaises(ValueError):
                    throttle.closeout_state([NOW], NOW, threshold=bad)

    def test_bad_cooldown_raises(self):
        for bad in (-1, 1.5, True, "900"):
            with self.subTest(cooldown=bad):
                with self.assertRaises(ValueError):
                    throttle.closeout_state([NOW], NOW, cooldown_seconds=bad)


class AllowsTest(unittest.TestCase):
    """D 组：收口期内谁还能说话。"""

    def setUp(self):
        self.active = _burst_state()
        self.inactive = throttle.closeout_state([NOW - 5000], NOW)

    def test_every_bypass_reason_passes(self):
        """白名单里的理由在收口期内一律放行。"""
        for reason in throttle.BYPASS_REASONS:
            with self.subTest(reason=reason):
                self.assertTrue(throttle.closeout_allows(reason, self.active))

    def test_non_bypass_reasons_are_blocked(self):
        """疑问句、兴趣词、冷场起话头、复读、猜测一律不开口。"""
        for reason in ("question", "interest", "cold_open", "repeat", "guess", ""):
            with self.subTest(reason=reason):
                self.assertFalse(throttle.closeout_allows(reason, self.active))

    def test_inactive_state_allows_everything(self):
        """没在收口时，任何理由都放行。"""
        for reason in ("question", "interest", ""):
            with self.subTest(reason=reason):
                self.assertTrue(throttle.closeout_allows(reason, self.inactive))

    def test_reason_is_stripped_but_case_sensitive(self):
        """理由两端空白容错；大小写不做兼容——未知写法按「不放行」处理。"""
        self.assertTrue(throttle.closeout_allows(" mention ", self.active))
        self.assertFalse(throttle.closeout_allows("Mention", self.active))

    def test_bad_state_fails_open(self):
        """坏 state 一律 fail-open：收口是「少说一句」的优化，不是安全边界。

        宁可让她照常说话，也不能因为一个被手改坏的字段把她永久禁言。
        """
        for bad in (
            None,
            "active",
            [],
            {},
            {"active": "true"},
            {"active": 1},
            {"active": None},
        ):
            with self.subTest(state=bad):
                self.assertTrue(throttle.closeout_allows("question", bad))

    def test_minimal_active_state_still_blocks(self):
        """但真正的 True 仍然生效——fail-open 不能把收口本身弄没。"""
        self.assertFalse(throttle.closeout_allows("question", {"active": True}))

    def test_bad_reason_fails_open(self):
        """理由对象连 str() 都抛：fail-open，别让日志字段把她禁言。"""

        class _Boom:
            def __str__(self):
                raise RuntimeError("boom")

        self.assertTrue(throttle.closeout_allows(_Boom(), self.active))

    def test_mention_from_attribution_is_the_bypass_key(self):
        """归因结果可以直接当放行理由：`attribution(...)["reason"]` 喂进来。"""
        got = groupflow.attribution("在吗", at_ids=["10086"], bot_id="10086")
        self.assertTrue(throttle.closeout_allows(got["reason"], self.active))
        other = groupflow.attribution("小助手刚才说的那个", bot_names=["小助手"])
        self.assertEqual(other["reason"], groupflow.ATTR_THIRD_PERSON)
        self.assertFalse(throttle.closeout_allows(other["reason"], self.active))


if __name__ == "__main__":
    unittest.main()
