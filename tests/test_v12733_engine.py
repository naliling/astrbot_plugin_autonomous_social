"""v1.27.33：引擎边界审计的回归测试。

为什么单开一份：`social/engine.py` 是这个插件最大也最要紧的文件（心跳节拍、发送
熔断、冷却、候选选择、多 bot 隔离），但这些类常量此前只有零散几个被测到，**边界**
（正好等于、跨天、时钟回拨、数值跑飞）没有任何测试钉住。

这一份按「边界」而不是「功能」组织：每条都落在阈值上或其两侧，专门找三种失效形态
——永不触发、永远触发、数值跑飞。里面四条是真实缺陷的回归（见
`SendBreakerTest.test_huge_streak_does_not_overflow`、
`SendRetryTest.test_peer_unreachable_survives_the_unknown_path`、
`RelationBucketTest.test_zero_interest_is_not_promoted_to_the_default` 与
`GiveupTest.test_a_zero_interest_user_can_still_be_quiesced`）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

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
    api.logger = logging.getLogger("autonomous_social_engine_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import anchors, desire, engine as engine_mod  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import (  # noqa: E402
    COLD_SILENCE_SENT,
    FLUSH_INTERVAL_SECONDS,
    GIVEEUP_TRIES,
    MIN_HEARTBEAT_SECONDS,
    PENDING_IGNORE_MIN_HOURS,
    PRUNE_INTERVAL_SECONDS,
    SEND_BREAKER_BASE_SECONDS,
    SEND_BREAKER_MAX_SECONDS,
    SEND_BREAKER_TRIP,
    SEND_FAIL_COOLDOWN_BASE,
    SEND_FAIL_COOLDOWN_MAX,
    SEND_FAIL_PLATFORM_COOLDOWN,
    SEND_RETRY_COUNT,
    SEED_INTERVAL_SECONDS,
    SocialEngine,
    _SendFailed,
)

NOW = 1_800_000_000.0


class _Clock:
    """可控时间源。engine 的 `_time()` 会做单调钳制，所以回拨也测得了。"""

    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def rewind(self, seconds: float) -> None:
        self.now -= float(seconds)


def _engine(clock: _Clock | None = None, **cfg) -> SocialEngine:
    tmp = tempfile.mkdtemp()
    clock = clock or _Clock()
    eng = SocialEngine(
        context=None,
        config=SocialConfig(),
        state_path=str(Path(tmp) / "state.json"),
        data_dir=tmp,
        time_source=clock,
    )
    for key, value in cfg.items():
        setattr(eng.cfg, key, value)
    return eng


def _user(eng: SocialEngine, bid: str = "b1", uid: str = "u1", **fields):
    """在 state 里造一个用户条目。`state.user()` 会按默认值补齐。

    注意 `umo` 必须**直接赋值**：默认结构里它是 `""`（`state.py` 的
    `_default_user`），而 `settle_minds` 开头就是 `if not u.get("umo"): continue`
    ——用 `setdefault` 的话这个人会被整轮跳过，测试会以「念头没被结算」的假象失败。
    """
    user = eng.state.user(bid, uid)
    user["umo"] = "aiocqhttp:private:u1"
    user["__uid"] = uid
    user.update(fields)
    return user


class HeartbeatRangeTest(unittest.TestCase):
    """心跳区间的三种边界：下限、区间反了、正好相等。"""

    def test_floor_is_one_minute(self):
        eng = _engine(heartbeat_min_minutes=0, heartbeat_max_minutes=0)
        lo, hi = eng._heartbeat_range()
        self.assertEqual(lo, MIN_HEARTBEAT_SECONDS)
        # hi 由 max(lo, ...) 得出，不会低于 lo——否则 randint 会 ValueError
        self.assertGreaterEqual(hi, lo)

    def test_inverted_range_collapses_to_the_lower_bound(self):
        # 主人把下限填得比上限大：不能让 randint 拿到空区间
        eng = _engine(heartbeat_min_minutes=60, heartbeat_max_minutes=1)
        lo, hi = eng._heartbeat_range()
        self.assertEqual(lo, 3600)
        self.assertEqual(hi, 3600)

    def test_equal_bounds_give_a_fixed_interval(self):
        eng = _engine(heartbeat_min_minutes=5, heartbeat_max_minutes=5)
        self.assertEqual(eng._heartbeat_range(), (300, 300))

    def test_upper_bound_is_never_below_one_minute(self):
        eng = _engine(heartbeat_min_minutes=1, heartbeat_max_minutes=1)
        self.assertEqual(eng._heartbeat_range(), (60, 60))


class ClockTest(unittest.TestCase):
    """墙钟回拨：小回拨 `_time()` 单调不减（否则念头/冷却会算成负数）；
    大回拨（墙钟被改，> TIME_ROLLBACK_RESET_SECONDS）重置高水位、不冻结时间轴。"""

    def test_small_rollback_is_clamped(self):
        clock = _Clock()
        eng = _engine(clock)
        self.assertEqual(eng._time(), NOW)
        clock.rewind(200)          # NTP 微调级别，小于重置阈值
        self.assertEqual(eng._time(), NOW)

    def test_forward_time_advances(self):
        clock = _Clock()
        eng = _engine(clock)
        clock.advance(120)
        self.assertEqual(eng._time(), NOW + 120)

    def test_rollback_then_forward_does_not_go_backwards(self):
        clock = _Clock()
        eng = _engine(clock)
        clock.advance(10)
        self.assertEqual(eng._time(), NOW + 10)   # 先让水位线立起来
        clock.rewind(200)
        self.assertEqual(eng._time(), NOW + 10)
        # 回拨之后再往前走，必须**越过**水位线才重新前进（判据是 `t < high`，取严格小于）
        clock.advance(199)   # 墙钟 NOW+9，仍在水位线下
        self.assertEqual(eng._time(), NOW + 10)
        clock.advance(1)     # 墙钟 NOW+10，正好追平水位线，仍不前进
        self.assertEqual(eng._time(), NOW + 10)
        clock.advance(1)     # 越过水位线，恢复正常
        self.assertEqual(eng._time(), NOW + 11)

    def test_large_rollback_resets_the_frozen_timeline(self):
        """P3-09：回拨超过阈值（如系统时钟被改）不能把「现在」冻住一小时。"""
        clock = _Clock()
        eng = _engine(clock)
        self.assertEqual(eng._time(), NOW)
        clock.advance(600)
        self.assertEqual(eng._time(), NOW + 600)
        clock.rewind(7200)                   # 回拨两小时（远超阈值）
        self.assertEqual(eng._time(), NOW - 6600, "大回拨后时间轴还冻在旧水位")
        clock.advance(5)
        self.assertEqual(eng._time(), NOW - 6595, "重置高水位后时间没有继续前进")


class BeatSignalsTest(unittest.TestCase):
    def test_ttl_covers_two_full_heartbeat_cycles(self):
        # Core 侧的过期阈值比心跳上限大，否则心跳稍慢就被判成「社交插件停了」
        eng = _engine(heartbeat_min_minutes=1, heartbeat_max_minutes=10)
        seen = {}
        eng.signals.set_ttl_seconds = lambda s: seen.__setitem__("ttl", s)
        eng.signals.beat = lambda *a, **k: seen.__setitem__("beat", True)
        eng._beat_signals()
        self.assertTrue(seen.get("beat"))
        self.assertEqual(seen.get("ttl"), 1200.0)


class SendBreakerTest(unittest.TestCase):
    """发送侧熔断：连续失败才熔断，成功清零，指数封顶。"""

    def test_two_failures_do_not_trip(self):
        eng = _engine()
        for _ in range(SEND_BREAKER_TRIP - 1):
            eng._note_send_failure("b1", "adapter 没加载", False)
        self.assertEqual(eng.send_breaker_left("b1"), 0.0)

    def test_tripping_failure_opens_the_breaker_for_the_base_delay(self):
        eng = _engine()
        for _ in range(SEND_BREAKER_TRIP):
            eng._note_send_failure("b1", "adapter 没加载", False)
        self.assertEqual(eng.send_breaker_left("b1"), SEND_BREAKER_BASE_SECONDS)

    def test_each_extra_failure_doubles_the_delay(self):
        eng = _engine()
        for _ in range(SEND_BREAKER_TRIP + 1):
            eng._note_send_failure("b1", "adapter 没加载", False)
        self.assertEqual(eng.send_breaker_left("b1"), SEND_BREAKER_BASE_SECONDS * 2)

    def test_delay_is_clamped_at_the_maximum(self):
        eng = _engine()
        for _ in range(SEND_BREAKER_TRIP + 20):
            eng._note_send_failure("b1", "adapter 没加载", False)
        self.assertEqual(eng.send_breaker_left("b1"), SEND_BREAKER_MAX_SECONDS)

    def test_peer_unreachable_never_trips_the_breaker(self):
        # 平台说「不是好友」是这个人的事，不该让整个角色停发
        eng = _engine()
        for _ in range(SEND_BREAKER_TRIP * 3):
            eng._note_send_failure("b1", "不是好友", True)
        self.assertEqual(eng.send_breaker_left("b1"), 0.0)

    def test_a_success_clears_the_streak(self):
        eng = _engine()
        for _ in range(SEND_BREAKER_TRIP):
            eng._note_send_failure("b1", "adapter 没加载", False)
        eng._note_send_ok("b1")
        self.assertEqual(eng.send_breaker_left("b1"), 0.0)
        # 清零之后要重新数满 3 次才会再熔断
        eng._note_send_failure("b1", "adapter 没加载", False)
        self.assertEqual(eng.send_breaker_left("b1"), 0.0)

    def test_breakers_are_isolated_per_bot(self):
        eng = _engine()
        for _ in range(SEND_BREAKER_TRIP):
            eng._note_send_failure("b1", "adapter 没加载", False)
        self.assertGreater(eng.send_breaker_left("b1"), 0.0)
        self.assertEqual(eng.send_breaker_left("b2"), 0.0)

    def test_huge_streak_does_not_overflow(self):
        """回归：`2 ** (streak - TRIP)` 不封顶时，streak 上千会抛 OverflowError。

        抛点在 `_note_send_failure` 内部，而它由 `_send_with_retry` 的 except 分支
        调用——异常会冒泡出去，把这次失败的记账（streak / until / err）连同
        `_mark_send_failed`、`_note_giveup` 一起丢掉。
        """
        eng = _engine()
        eng._breaker_slot("b1")["streak"] = 5000
        eng._note_send_failure("b1", "adapter 没加载", False)  # 修复前这里 OverflowError
        self.assertEqual(eng.send_breaker_left("b1"), SEND_BREAKER_MAX_SECONDS)

    def test_streak_grows_without_a_ceiling_of_its_own(self):
        # streak 本身不封顶是刻意的（它记录真实失败次数），只是指数必须封顶
        eng = _engine()
        eng._breaker_slot("b1")["streak"] = 900
        eng._note_send_failure("b1", "adapter 没加载", False)
        self.assertEqual(eng._breaker_slot("b1")["streak"], 901)
        self.assertEqual(eng.send_breaker_left("b1"), SEND_BREAKER_MAX_SECONDS)


class SendRetryTest(unittest.IsolatedAsyncioTestCase):
    """`_send_with_retry` 的三元返回值决定「要不要隔离这个人」。"""

    async def test_unknown_failure_is_not_retried(self):
        eng = _engine()
        calls = []

        async def _boom(target, text):
            calls.append(text)
            raise _SendFailed("结果未知", unknown=True)

        eng._send_message = _boom
        ok, err, unreachable = await eng._send_with_retry("umo", "hi", "b1")
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)  # 未知结果重发会重复收到，所以不重试
        self.assertFalse(unreachable)

    async def test_peer_unreachable_survives_the_unknown_path(self):
        """回归：未知分支原来硬编码 `return False, last_err, False`。

        纯文本回退路径（`_send_message` 的 `except Exception`）会**同时**置
        `unknown=True` 与 `peer_unreachable=True`。丢掉后者意味着平台明说「不是
        好友 / 会话失效」时这个人永远不会被隔离：每轮心跳仍花一次 LLM 生成再失败，
        退避也走通用 1800~28800 秒而不是 24 小时隔离。
        """
        eng = _engine()

        async def _boom(target, text):
            raise _SendFailed("不是好友", unknown=True, peer_unreachable=True)

        eng._send_message = _boom
        ok, err, unreachable = await eng._send_with_retry("umo", "hi", "b1")
        self.assertFalse(ok)
        self.assertTrue(unreachable)

    async def test_known_failure_is_retried_once(self):
        eng = _engine()
        calls = []

        async def _boom(target, text):
            calls.append(text)
            raise _SendFailed("连接被拒", unknown=False)

        eng._send_message = _boom
        ok, err, unreachable = await eng._send_with_retry("umo", "hi", "b1")
        self.assertFalse(ok)
        self.assertEqual(len(calls), SEND_RETRY_COUNT + 1)
        self.assertFalse(unreachable)

    async def test_a_later_attempt_can_succeed(self):
        eng = _engine()
        calls = []

        async def _flaky(target, text):
            calls.append(text)
            if len(calls) == 1:
                raise _SendFailed("连接被拒", unknown=False)

        eng._send_message = _flaky
        ok, err, unreachable = await eng._send_with_retry("umo", "hi", "b1")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertFalse(unreachable)

    async def test_success_clears_the_breaker(self):
        eng = _engine()
        for _ in range(SEND_BREAKER_TRIP):
            eng._note_send_failure("b1", "adapter 没加载", False)

        async def _ok(target, text):
            return None

        eng._send_message = _ok
        ok, _, _ = await eng._send_with_retry("umo", "hi", "b1")
        self.assertTrue(ok)
        self.assertEqual(eng.send_breaker_left("b1"), 0.0)


class HourlyBudgetTest(unittest.TestCase):
    """每小时发送配额：0 表示不限，退款必须原样还给池子。"""

    def test_zero_cap_is_unlimited_and_keeps_no_ledger(self):
        eng = _engine(hourly_sends_cap=0)
        for _ in range(50):
            self.assertTrue(eng._hourly_reserve("b1"))
        self.assertFalse(eng._hourly.get("b1"))

    def test_reserve_stops_exactly_at_the_cap(self):
        eng = _engine(hourly_sends_cap=3)
        self.assertEqual([eng._hourly_reserve("b1") for _ in range(4)], [True, True, True, False])

    def test_refund_returns_the_slot(self):
        eng = _engine(hourly_sends_cap=2)
        eng._hourly_reserve("b1")
        eng._hourly_reserve("b1")
        self.assertFalse(eng._hourly_reserve("b1"))
        eng._hourly_refund("b1")
        self.assertTrue(eng._hourly_reserve("b1"))

    def test_entries_older_than_an_hour_are_dropped(self):
        eng = _engine(hourly_sends_cap=2)
        eng._hourly_reserve("b1")
        eng._hourly_reserve("b1")
        eng._time_source.advance(3601)
        self.assertTrue(eng._hourly_reserve("b1"))
        self.assertEqual(len(eng._hourly["b1"]), 1)

    def test_exactly_one_hour_frees_the_slot(self):
        # 窗口判据是 `now - ts < 3600`，边界取开区间：正好一小时的那条已经出去了
        eng = _engine(hourly_sends_cap=1)
        eng._hourly_reserve("b1")
        eng._time_source.advance(3600)
        self.assertTrue(eng._hourly_reserve("b1"))

    def test_one_second_short_of_an_hour_still_holds_the_slot(self):
        eng = _engine(hourly_sends_cap=1)
        eng._hourly_reserve("b1")
        eng._time_source.advance(3599)
        self.assertFalse(eng._hourly_reserve("b1"))

    def test_refund_on_an_empty_pool_is_harmless(self):
        eng = _engine(hourly_sends_cap=1)
        eng._hourly_refund("b1")  # 不该抛，也不该造出负数
        self.assertTrue(eng._hourly_reserve("b1"))

    def test_pools_are_isolated_per_bot(self):
        eng = _engine(hourly_sends_cap=1)
        self.assertTrue(eng._hourly_reserve("b1"))
        self.assertTrue(eng._hourly_reserve("b2"))


class PendingWindowTest(unittest.TestCase):
    def test_window_never_shrinks_below_the_ignore_floor(self):
        eng = _engine(reply_window_hours=1)
        self.assertEqual(eng._pending_window(), PENDING_IGNORE_MIN_HOURS * 3600)

    def test_a_longer_configured_window_wins(self):
        eng = _engine(reply_window_hours=PENDING_IGNORE_MIN_HOURS + 6)
        self.assertEqual(eng._pending_window(), (PENDING_IGNORE_MIN_HOURS + 6) * 3600)


class QuietHoursTest(unittest.TestCase):
    """安静时段：Core 的生物钟优先，配置兜底，跨午夜要能算对。"""

    def test_zero_length_window_is_never_quiet(self):
        eng = _engine(quiet_start=7, quiet_end=7)
        self.assertFalse(eng.cfg.in_quiet_hours(7))
        self.assertFalse(eng._quiet_now("b1", 3))

    def test_config_window_within_one_day(self):
        eng = _engine(quiet_start=13, quiet_end=15)
        self.assertFalse(eng._quiet_now("b1", 12))
        self.assertTrue(eng._quiet_now("b1", 13))
        self.assertTrue(eng._quiet_now("b1", 14))
        self.assertFalse(eng._quiet_now("b1", 15))

    def test_config_window_across_midnight(self):
        eng = _engine(quiet_start=23, quiet_end=7)
        self.assertTrue(eng._quiet_now("b1", 23))
        self.assertTrue(eng._quiet_now("b1", 3))
        self.assertFalse(eng._quiet_now("b1", 7))
        self.assertFalse(eng._quiet_now("b1", 12))

    def test_core_window_overrides_the_config(self):
        # 配置说 13-15 点安静；Core 说她 22 点到 6 点睡。
        # 14 点：配置说安静、Core 说清醒 —— 必须听 Core 的。
        eng = _engine(quiet_start=13, quiet_end=15)
        eng._remember_night("b1", {"night_window": [22, 6]})
        self.assertFalse(eng._quiet_now("b1", 14))
        self.assertTrue(eng._quiet_now("b1", 23))
        self.assertFalse(eng._quiet_now("b1", 12))

    def test_invalid_window_falls_back_to_the_config(self):
        eng = _engine(quiet_start=1, quiet_end=2)
        eng._remember_night("b1", {"night_window": [22, 6]})
        eng._remember_night("b1", {"night_window": None})
        self.assertFalse(eng._quiet_now("b1", 23))
        self.assertTrue(eng._quiet_now("b1", 1))

    def test_remember_night_wraps_hours_into_a_day(self):
        eng = _engine()
        eng._remember_night("b1", {"night_window": [25.5, -1]})
        self.assertEqual(eng._night_windows["b1"], (1.5, 23.0))

    def test_remember_night_ignores_an_empty_bot_id(self):
        eng = _engine()
        eng._remember_night("", {"night_window": [1, 2]})
        self.assertEqual(eng._night_windows, {})


class PruneTest(unittest.TestCase):
    def test_prune_runs_once_per_interval(self):
        eng = _engine()
        eng._last_prune = 0.0
        eng._prune_expired()
        first = eng._last_prune
        self.assertEqual(first, eng._time())
        eng._time_source.advance(PRUNE_INTERVAL_SECONDS - 1)
        eng._prune_expired()
        self.assertEqual(eng._last_prune, first)
        eng._time_source.advance(1)
        eng._prune_expired()
        self.assertEqual(eng._last_prune, first + PRUNE_INTERVAL_SECONDS)

    def test_seed_interval_gate(self):
        eng = _engine()
        eng._last_seed = eng._time()
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(eng._seed_tick())
        self.assertEqual(eng._last_seed, eng._time())
        eng._time_source.advance(SEED_INTERVAL_SECONDS)
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(eng._seed_tick())
        self.assertEqual(eng._last_seed, eng._time())


class UserLockPruneTest(unittest.TestCase):
    """锁表只增不减是内存泄漏：量级是「曾经见过的用户数」。"""

    def test_stale_unlocked_locks_are_dropped(self):
        eng = _engine()
        eng._user_lock("b1", "ghost")
        self.assertIn(("b1", "ghost"), eng._user_locks)
        self.assertEqual(eng._prune_user_locks(), 1)
        self.assertNotIn(("b1", "ghost"), eng._user_locks)

    def test_live_user_locks_survive(self):
        eng = _engine()
        _user(eng, "b1", "u1")
        eng._user_lock("b1", "u1")
        self.assertEqual(eng._prune_user_locks(), 0)
        self.assertIn(("b1", "u1"), eng._user_locks)

    def test_a_held_lock_is_never_dropped(self):
        # 丢掉正被持有的锁会让两个协程各拿一把新锁，互斥直接失效
        eng = _engine()
        lock = eng._user_lock("b1", "ghost")
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(lock.acquire())
        try:
            self.assertEqual(eng._prune_user_locks(), 0)
            self.assertIn(("b1", "ghost"), eng._user_locks)
        finally:
            lock.release()

    def test_prune_without_locks_is_a_noop(self):
        eng = _engine()
        self.assertEqual(eng._prune_user_locks(), 0)


class EagerUrgeTest(unittest.TestCase):
    """急切线：不夹到天花板就会恒假（settle 把 urge 夹在 3.0）。"""

    def test_not_eager_just_below_the_line(self):
        eng = _engine()
        # 急切线本身也被夹在天花板之内（gate 高时 `max(...)*ratio` 会超过 URGE_CEILING，
        # 不夹就永远追不上）
        line = min(
            desire.URGE_CEILING, max(desire.FIRE_THRESHOLD, desire.FIRE_THRESHOLD) * engine_mod.EAGER_URGE_RATIO
        )
        u = {"urge": line - 1e-6, "fire_gate": desire.FIRE_THRESHOLD}
        self.assertFalse(eng._urge_is_eager(u))

    def test_eager_at_exactly_the_line(self):
        eng = _engine()
        line = min(
            desire.URGE_CEILING, max(desire.FIRE_THRESHOLD, desire.FIRE_THRESHOLD) * engine_mod.EAGER_URGE_RATIO
        )
        self.assertTrue(eng._urge_is_eager({"urge": line, "fire_gate": desire.FIRE_THRESHOLD}))

    def test_a_gate_above_the_ceiling_stays_reachable(self):
        # gate 很高时，急切线被夹到 URGE_CEILING；不然它永远追不上
        eng = _engine()
        u = {"urge": desire.URGE_CEILING, "fire_gate": 99.0}
        self.assertTrue(eng._urge_is_eager(u))

    def test_broken_values_are_not_eager(self):
        eng = _engine()
        self.assertFalse(eng._urge_is_eager({"urge": "x", "fire_gate": "y"}))
        self.assertFalse(eng._urge_is_eager({}))


class GiveupTest(unittest.TestCase):
    """连着发不出去就不追了——但只对低档生效，且对方一开口就解除。"""

    def _low(self, eng, uid="u1"):
        u = _user(eng, "b1", uid, interest=0.0)
        u["__uid"] = uid
        tier, _, _ = eng._tier_of("b1", uid, u, None, NOW)
        return u, tier

    def test_low_tier_is_the_precondition(self):
        eng = _engine()
        _, tier = self._low(eng)
        self.assertEqual(tier, anchors.TIER_LOW)

    def test_two_giveups_quiesce_the_user(self):
        eng = _engine()
        u, _ = self._low(eng)
        eng._note_giveup(u, "b1", NOW)
        self.assertFalse(eng._quiesced(u))
        eng._note_giveup(u, "b1", NOW + 1)
        self.assertTrue(eng._quiesced(u))
        self.assertEqual(u["quiesced_at"], NOW + 1)

    def test_a_high_tier_user_is_never_quiesced(self):
        # 她常聊的人某天没回，不代表明天也不回
        eng = _engine()
        u = _user(eng, "b1", "u2", interest=1.0)
        u["__uid"] = "u2"
        tier, _, _ = eng._tier_of("b1", "u2", u, None, NOW)
        self.assertNotEqual(tier, anchors.TIER_LOW)
        for i in range(GIVEEUP_TRIES + 3):
            eng._note_giveup(u, "b1", NOW + i)
        self.assertFalse(eng._quiesced(u))

    def test_the_peer_speaking_lifts_quiescence(self):
        eng = _engine()
        u, _ = self._low(eng)
        for i in range(GIVEEUP_TRIES):
            eng._note_giveup(u, "b1", NOW + i)
        self.assertTrue(eng._quiesced(u))
        u["last_seen"] = NOW + 100
        self.assertFalse(eng._quiesced(u))

    def test_a_missing_quiesced_at_is_not_quiesced(self):
        eng = _engine()
        self.assertFalse(eng._quiesced({}))
        self.assertFalse(eng._quiesced({"quiesced_at": "x", "last_seen": "y"}))


class SendFailBackoffTest(unittest.TestCase):
    def test_platform_side_escalates_instead_of_a_flat_fixed_cooldown(self):
        from social.engine import SEND_FAIL_PLATFORM_COOLDOWN, SEND_FAIL_PLATFORM_MAX

        eng = _engine()
        u = _user(eng)
        delays = []
        for _ in range(9):
            eng._mark_send_failed(u, platform_side=True)
            delays.append(u["send_fail_until"] - NOW)
        # 第一次 30 分钟，之后 ×4 升档，12 小时封顶。原来是一档固定 5 分钟，
        # 而「umo 废了」不会自愈：每个心跳都把这个人选回来、白烧一次 LLM。
        self.assertEqual(delays[0], SEND_FAIL_PLATFORM_COOLDOWN)
        self.assertGreater(delays[1], delays[0])
        self.assertEqual(delays[-1], SEND_FAIL_PLATFORM_MAX)
        self.assertNotIn("send_fail_count", u, "平台侧失败不该累加这个人的退避计数")

    def test_user_side_backoff_doubles_and_clamps(self):
        eng = _engine()
        u = _user(eng)
        eng._mark_send_failed(u)
        self.assertEqual(u["send_fail_until"], NOW + SEND_FAIL_COOLDOWN_BASE)
        eng._mark_send_failed(u)
        self.assertEqual(u["send_fail_until"], NOW + SEND_FAIL_COOLDOWN_BASE * 2)
        for _ in range(20):
            eng._mark_send_failed(u)
        self.assertEqual(u["send_fail_until"], NOW + SEND_FAIL_COOLDOWN_MAX)


class BlockCategoryTest(unittest.TestCase):
    def test_the_longest_phrase_wins(self):
        # 「刚想过一次决定不说」必须落到具体那类，不能被更短的「刚跳过」先吃掉
        self.assertEqual(
            engine_mod.SocialEngine._block_category("刚想过一次决定不说"),
            engine_mod.SocialEngine._block_category("刚想过一次决定不说"),
        )

    def test_empty_reason_is_its_own_bucket(self):
        self.assertEqual(engine_mod.SocialEngine._block_category(""), "未说明")
        self.assertEqual(engine_mod.SocialEngine._block_category(None), "未说明")

    def test_unknown_reason_falls_into_other(self):
        self.assertEqual(engine_mod.SocialEngine._block_category("毫无线索的一句话"), "其它")

    def test_count_block_fills_the_metrics_table(self):
        eng = _engine()
        eng._count_block("毫无线索的一句话")
        eng._count_block("毫无线索的一句话")
        self.assertEqual(eng._m["blocked_reasons"]["其它"], 2)


class RelationBucketTest(unittest.TestCase):
    def test_buckets_are_monotonic(self):
        eng = _engine()
        last = -1
        for interest in (0.0, 0.19, 0.20, 0.31, 0.32, 0.44, 0.45, 0.57, 0.58, 0.71, 0.72, 1.0):
            bucket = engine_mod.SocialEngine._relation_bucket({"interest": interest})
            self.assertGreaterEqual(bucket, last)
            last = bucket

    def test_boundary_goes_to_the_higher_bucket(self):
        # `iv < floor` 才降档，所以正好等于 floor 时算高档
        self.assertEqual(engine_mod.SocialEngine._relation_bucket({"interest": 0.20}), 1)

    def test_broken_interest_uses_the_default(self):
        self.assertEqual(
            engine_mod.SocialEngine._relation_bucket({"interest": "x"}),
            engine_mod.SocialEngine._relation_bucket({"interest": 0.35}),
        )


class SettleMindsRobustnessTest(unittest.TestCase):
    """一个用户坏掉不能让整轮结算停摆。"""

    def _seed(self, eng, **fields):
        u = _user(eng, "b1", "u1", **fields)
        return u

    def test_broken_user_is_skipped_and_counted(self):
        eng = _engine()
        self._seed(eng, urge="not-a-number", last_seen="also-bad")
        ready = eng.settle_minds("b1", NOW, __import__("datetime").datetime(2026, 8, 22, 15, 0))
        self.assertIsInstance(ready, list)

    def test_ready_list_is_sorted_by_urge_desc(self):
        eng = _engine()
        self._seed(eng, urge=1.0)
        ready = eng.settle_minds(
            "b1", NOW, __import__("datetime").datetime(2026, 8, 22, 15, 0)
        )
        urges = [item[2] for item in ready]
        self.assertEqual(urges, sorted(urges, reverse=True))

    def test_urge_is_clamped_to_the_ceiling(self):
        eng = _engine()
        u = self._seed(eng, urge=desire.URGE_CEILING * 100)
        eng.settle_minds("b1", NOW, __import__("datetime").datetime(2026, 8, 22, 15, 0))
        self.assertLessEqual(float(u["urge"]), desire.URGE_CEILING)

    def test_unknown_bot_is_harmless(self):
        eng = _engine()
        self.assertEqual(
            eng.settle_minds("nope", NOW, __import__("datetime").datetime(2026, 8, 22, 15, 0)), []
        )


class IdHelperTest(unittest.TestCase):
    """事件字段缺失时不能抛，只能退到保守默认。"""

    class _Bare:
        pass

    def test_bot_id_falls_back_to_default(self):
        self.assertEqual(SocialEngine._get_bot_id(self._Bare()), "default")

    def test_sender_id_falls_back_to_empty(self):
        self.assertEqual(SocialEngine._get_sender_id(self._Bare()), "")

    def test_group_detection_falls_back_to_private(self):
        self.assertFalse(SocialEngine._detect_is_group(self._Bare()))

    def test_group_id_falls_back_to_empty(self):
        self.assertEqual(SocialEngine._get_group_id(self._Bare()), "")

    def test_explicit_group_type_wins(self):
        class _Ev:
            def get_message_type(self):
                return types.SimpleNamespace(name="GROUP_MESSAGE")

        self.assertTrue(SocialEngine._detect_is_group(_Ev()))

    def test_explicit_private_type_wins(self):
        class _Ev:
            def get_message_type(self):
                return types.SimpleNamespace(name="FRIEND_MESSAGE")

        self.assertFalse(SocialEngine._detect_is_group(_Ev()))

    def test_message_obj_group_id_marks_a_group(self):
        class _Ev:
            message_obj = types.SimpleNamespace(group_id="g1")

        self.assertTrue(SocialEngine._detect_is_group(_Ev()))

    def test_is_group_attribute_is_respected(self):
        class _Ev:
            is_group = True

        self.assertTrue(SocialEngine._detect_is_group(_Ev()))

    def test_group_id_from_message_obj(self):
        class _Ev:
            message_obj = types.SimpleNamespace(group_id="g9")

        self.assertEqual(SocialEngine._get_group_id(_Ev()), "g9")


class ColdSilenceGateTest(unittest.TestCase):
    """TA 一直没回时的上限：不能靠「更频繁地发」来唤起注意。"""

    def test_the_cap_is_a_positive_constant(self):
        self.assertGreater(COLD_SILENCE_SENT, 0)

    def test_flush_interval_is_below_the_heartbeat_floor(self):
        # 刷盘比最短心跳还慢就会攒一肚子脏数据
        self.assertLessEqual(FLUSH_INTERVAL_SECONDS, MIN_HEARTBEAT_SECONDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
