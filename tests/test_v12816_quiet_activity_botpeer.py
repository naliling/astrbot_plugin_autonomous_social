"""v1.28.16：静音结束补评估 + 群活跃度抑制 + 兄弟 bot 可聊但防刷屏。

三件事各自独立，但都是「让她更像人、又别烦人」这一条线上的：

## A. 静音结束立即补一次评估

`run()` 每轮先睡 8–30 分钟再看一眼。如果这一觉正好跨过静音结束点，静音结束后
第一轮要等剩下那半段随机睡眠走完才评估——用户看到的就是「说好 8 点起床，
8 点半还在装睡」。`seconds_until_quiet_end` 是纯函数（支持跨午夜、起==止=不启用），
`_cap_delay_for_quiet_end` 把睡眠截到「结束点 + 0~120 秒抖动、至少 30 秒」。

## B. 群活跃度分数（只做抑制）

从 koishi-plugin-chatluna-proactive-trigger 的 `ActivityScorer` 移植
（refs/koishi-check/ptr/package/lib/index.js 的 activity 段）。**只允许抑制**：
分数低 = 群里刚安静下来，不硬接；分数高**不新增任何开口路径**。
`flow_should_consider` 的 `activity` 默认 1.0，与旧行为逐字等价。

## C. 兄弟 bot 可聊，但严格防刷屏

私聊观察不再把同进程的兄弟 bot 直接丢掉：照常 `record_incoming`，但不提取
cue/loop（不给 bot 排追问）。主动找它要过 `bot_peer_block_reason`：没聊过不发、
6 小时内不重复、每天最多一条。群聊侧不变（兄弟 bot 仍不触发心流）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = (
    _HERE.parent
    if (_HERE.parent / "_conf_schema.json").is_file()
    else _HERE.parent / "astrbot_plugin_autonomous_social"
)


def _install_stub() -> None:
    """astrbot 不在测试环境里：按仓库既有做法打最小桩，只补被 import 到的名字。"""
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v12816_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object

    star = types.ModuleType("astrbot.api.star")

    class _Context:
        pass

    class _Star:
        def __init__(self, *a, **k):
            pass

    star.Context = _Context
    star.Star = _Star

    class _EventMessageType:
        ALL = "ALL"

    filter_mod = types.ModuleType("astrbot.api.event.filter")

    def _pass_decorator(*_a, **_k):
        def _wrap(func):
            return func

        return _wrap

    filter_mod.EventMessageType = _EventMessageType
    filter_mod.command = _pass_decorator
    filter_mod.event_message_type = _pass_decorator
    event.filter = filter_mod
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    api.star = star
    api.event = event
    api.AstrBotConfig = dict
    astrbot.api = api
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.event.filter": filter_mod,
            "astrbot.api.star": star,
        }
    )


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social import engine as engine_mod  # noqa: E402
from social import groupflow  # noqa: E402
from social.clock import seconds_until_quiet_end  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import (  # noqa: E402
    BOT_PEER_DAILY_CAP,
    BOT_PEER_MIN_GAP_SECONDS,
    GROUP_ACTIVITY_KEEP_SECONDS,
    SocialEngine,
    bot_peer_block_reason,
    note_bot_peer_sent,
)
from social.state import SocialState  # noqa: E402


NOW = 1_800_000_000.0


class _Clock:
    """可控时间源。engine 的 `_time()` 做单调钳制，所以这里也支持回拨。"""

    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def set(self, now: float) -> None:
        self.now = float(now)

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


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


class _MsgObj:
    """够 `observe` / `_observe_group` 用的最小 message_obj。"""

    def __init__(self, group_id=None) -> None:
        self.raw_message = {}
        self.message = []
        self.group_id = group_id


class _Event:
    """假的 AstrBot 消息事件：只实现被观察链路读到的那些方法/属性。"""

    def __init__(
        self,
        *,
        bid: str = "bot1",
        uid: str = "",
        text: str = "",
        umo: str = "",
        gid: str = "",
        name: str = "",
        msg_type: str = "FriendMessage",
    ) -> None:
        self.message_obj = _MsgObj(group_id=gid or None)
        self.message_str = text
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = False
        self._bid = bid
        self._uid = uid
        self._gid = gid or ""
        self._name = name or uid
        self._msg_type = msg_type

    def get_self_id(self) -> str:
        return self._bid

    def get_sender_id(self) -> str:
        return self._uid

    def get_group_id(self) -> str:
        return self._gid

    def get_group_name(self) -> str:
        return "测试群"

    def get_sender_name(self) -> str:
        return self._name

    def get_message_type(self):
        return self._msg_type


def _private_event(uid: str, text: str = "你好呀", bid: str = "bot1") -> _Event:
    return _Event(
        bid=bid,
        uid=uid,
        text=text,
        umo=f"aiocqhttp:FriendMessage:{uid}",
        msg_type="FriendMessage",
    )


def _group_event(uid: str, gid: str = "g1", text: str = "群里聊起来了", bid: str = "bot1") -> _Event:
    return _Event(
        bid=bid,
        uid=uid,
        text=text,
        umo=f"aiocqhttp:GroupMessage:{gid}",
        gid=gid,
        msg_type="GroupMessage",
    )


# ═══════════════════════════════════════════════════════════════════
# A. 静音结束补评估
# ═══════════════════════════════════════════════════════════════════


class SecondsUntilQuietEndTest(unittest.TestCase):
    """纯函数：普通时段 / 跨午夜 / 起==止 / 边界与坏值。"""

    def test_normal_window(self):
        # 1:00–6:00，起点在内
        self.assertEqual(seconds_until_quiet_end(1, 0, 1, 6), 5 * 3600)
        self.assertEqual(seconds_until_quiet_end(3, 30, 1, 6), 2 * 3600 + 1800)
        # 结束前一分钟
        self.assertEqual(seconds_until_quiet_end(5, 59, 1, 6), 60)
        # 结束点本身已经不在安静时段（[start, end) 半开区间）
        self.assertEqual(seconds_until_quiet_end(6, 0, 1, 6), 0)
        self.assertEqual(seconds_until_quiet_end(0, 59, 1, 6), 0)
        self.assertEqual(seconds_until_quiet_end(12, 0, 1, 6), 0)

    def test_cross_midnight_window(self):
        # 默认 23:00–07:00
        self.assertEqual(seconds_until_quiet_end(23, 0, 23, 7), 8 * 3600)
        self.assertEqual(seconds_until_quiet_end(23, 59, 23, 7), 7 * 3600 + 60)
        self.assertEqual(seconds_until_quiet_end(0, 0, 23, 7), 7 * 3600)
        self.assertEqual(seconds_until_quiet_end(0, 30, 23, 7), 6 * 3600 + 1800)
        self.assertEqual(seconds_until_quiet_end(6, 59, 23, 7), 60)
        self.assertEqual(seconds_until_quiet_end(7, 0, 23, 7), 0)
        self.assertEqual(seconds_until_quiet_end(12, 0, 23, 7), 0)
        self.assertEqual(seconds_until_quiet_end(22, 59, 23, 7), 0)

    def test_start_equals_end_means_disabled(self):
        for hour in (0, 7, 12, 23):
            self.assertEqual(seconds_until_quiet_end(hour, 0, 23, 23), 0)
            self.assertEqual(seconds_until_quiet_end(hour, 0, 0, 0), 0)

    def test_return_type_is_int(self):
        value = seconds_until_quiet_end(2, 10, 1, 6)
        self.assertIsInstance(value, int)
        self.assertEqual(value, 4 * 3600 - 600)

    def test_bad_values_are_treated_as_disabled(self):
        bad = (
            (None, 0, 23, 7),
            ("x", 0, 23, 7),
            (12, None, 23, 7),
            (12, 0, None, 7),
            (12, 0, 23, None),
            (12, 0, "23", "7"),  # 字符串数字也不收：配置读出来本来就是 int
            (12, 60, 23, 7),     # 分钟越界
            (24, 0, 23, 7),      # 小时越界
            (12, 0, 24, 7),      # 起越界
            (12, 0, 23, -1),     # 止越界
            (float("nan"), 0, 23, 7),
            (12, 0, float("inf"), 7),
            (-1, 0, 23, 7),
        )
        for hour, minute, start, end in bad:
            with self.subTest(hour=hour, minute=minute, start=start, end=end):
                self.assertEqual(
                    seconds_until_quiet_end(hour, minute, start, end), 0
                )

    def test_full_day_quiet_window_would_be_start_equals_end(self):
        """24 小时静音不存在：起==止的语义是「不启用」，不是「全天」。"""
        self.assertEqual(seconds_until_quiet_end(12, 0, 0, 0), 0)

    def test_agrees_with_config_in_quiet_hours(self):
        """与 `SocialConfig.in_quiet_hours` 同一套语义（逐小时、逐分钟扫一遍）。"""
        cfg = SocialConfig()
        windows = [(cfg.quiet_start, cfg.quiet_end), (1, 6), (0, 0), (22, 23), (23, 0)]
        for start, end in windows:
            fake = SocialConfig(quiet_start=start, quiet_end=end)
            for hour in range(24):
                expected_in = fake.in_quiet_hours(hour)
                for minute in (0, 30, 59):
                    with self.subTest(start=start, end=end, hour=hour, minute=minute):
                        left = seconds_until_quiet_end(hour, minute, start, end)
                        self.assertEqual(
                            left > 0,
                            expected_in,
                            f"{hour}:{minute:02d} 在 [{start},{end}) 的判定与配置不一致",
                        )


class QuietCatchupDelayTest(unittest.TestCase):
    """`_cap_delay_for_quiet_end`：只算 sleep 的时长，不起循环。"""

    def test_outside_quiet_hours_keeps_the_random_delay(self):
        eng = _engine(_Clock(NOW), quiet_start=23, quiet_end=7)
        with patch("social.engine.city_now", return_value=datetime(2026, 1, 1, 15, 0)):
            self.assertEqual(eng._cap_delay_for_quiet_end(1800, jitter=0), 1800)
            self.assertEqual(eng._cap_delay_for_quiet_end(900, jitter=120), 900)

    def test_inside_quiet_hours_caps_a_long_sleep_at_quiet_end(self):
        eng = _engine(_Clock(NOW), quiet_start=23, quiet_end=7)
        # 凌晨 2:00 → 距 7:00 还有 5 小时
        with patch("social.engine.city_now", return_value=datetime(2026, 1, 1, 2, 0)) as clock:
            self.assertEqual(eng._cap_delay_for_quiet_end(10 * 3600, jitter=0), 5 * 3600)
            self.assertEqual(
                eng._cap_delay_for_quiet_end(10 * 3600, jitter=120), 5 * 3600 + 120
            )
            # 本来就比「到静音结束」短的睡眠不拉长
            self.assertEqual(eng._cap_delay_for_quiet_end(1800, jitter=60), 1800)
        # 走的是本机墙上时间（offset=None），不是某个角色的 Core 时区
        self.assertIsNone(clock.call_args[0][1])

    def test_last_minute_of_quiet_window_is_short(self):
        eng = _engine(_Clock(NOW), quiet_start=23, quiet_end=7)
        with patch("social.engine.city_now", return_value=datetime(2026, 1, 1, 6, 59)):
            self.assertEqual(eng._cap_delay_for_quiet_end(10 * 3600, jitter=0), 60)
            self.assertEqual(eng._cap_delay_for_quiet_end(10 * 3600, jitter=120), 180)

    def test_minimum_thirty_seconds(self):
        eng = _engine(_Clock(NOW), quiet_start=23, quiet_end=7)
        with patch("social.engine.city_now", return_value=datetime(2026, 1, 1, 6, 59)), \
                patch("social.engine.seconds_until_quiet_end", return_value=5):
            self.assertEqual(eng._cap_delay_for_quiet_end(10 * 3600, jitter=0), 30)

    def test_start_equals_end_disables_the_cap(self):
        eng = _engine(_Clock(NOW), quiet_start=22, quiet_end=22)
        with patch("social.engine.city_now", return_value=datetime(2026, 1, 1, 22, 30)):
            self.assertEqual(eng._cap_delay_for_quiet_end(10 * 3600, jitter=0), 10 * 3600)

    def test_default_jitter_is_bounded(self):
        eng = _engine(_Clock(NOW), quiet_start=23, quiet_end=7)
        with patch("social.engine.city_now", return_value=datetime(2026, 1, 1, 2, 0)):
            value = eng._cap_delay_for_quiet_end(10 * 3600)
        self.assertGreaterEqual(value, 5 * 3600)
        self.assertLessEqual(value, 5 * 3600 + 120)

    def test_broken_clock_or_config_falls_back_to_the_original_delay(self):
        eng = _engine(_Clock(NOW), quiet_start=23, quiet_end=7)
        with patch("social.engine.city_now", side_effect=RuntimeError("boom")):
            self.assertEqual(eng._cap_delay_for_quiet_end(1234, jitter=0), 1234)
        # 配置坏值：纯函数按「不启用」处理，睡眠原样不动
        eng.cfg.quiet_start = "不是数字"
        with patch("social.engine.city_now", return_value=datetime(2026, 1, 1, 2, 0)):
            self.assertEqual(eng._cap_delay_for_quiet_end(1234, jitter=0), 1234)

    def test_run_loop_uses_the_helper(self):
        src = inspect.getsource(SocialEngine.run)
        self.assertIn("_cap_delay_for_quiet_end(", src)
        self.assertIn("asyncio.sleep(delay)", src)
        # 旧写法（直接 sleep 随机值）不应该还在
        self.assertNotIn("asyncio.sleep(random.randint(lo, hi))", src)


# ═══════════════════════════════════════════════════════════════════
# B. 群活跃度分数 + 只做抑制的闸门
# ═══════════════════════════════════════════════════════════════════


class ActivityScoreTest(unittest.TestCase):
    """`groupflow.activity_score`：照 ActivityScorer 的口径。"""

    def test_empty_or_unusable_input_scores_zero(self):
        self.assertEqual(groupflow.activity_score([], NOW), 0.0)
        self.assertEqual(groupflow.activity_score(None, NOW), 0.0)
        self.assertEqual(groupflow.activity_score((), NOW), 0.0)
        self.assertEqual(groupflow.activity_score(["x", None, " "], NOW), 0.0)
        self.assertEqual(groupflow.activity_score([NOW], float("nan")), 0.0)
        self.assertEqual(groupflow.activity_score([NOW], "x"), 0.0)

    def test_one_fresh_message_is_below_the_suppression_line(self):
        score = groupflow.activity_score([NOW], NOW)
        self.assertAlmostEqual(score, 0.044322, places=6)
        self.assertLess(score, groupflow.FLOW_ACTIVITY_MIN)

    def test_two_fresh_messages_still_below_the_line(self):
        score = groupflow.activity_score([NOW, NOW - 1], NOW)
        self.assertAlmostEqual(score, 0.098108, places=6)
        self.assertLess(score, groupflow.FLOW_ACTIVITY_MIN)

    def test_three_fresh_messages_are_above_the_line(self):
        score = groupflow.activity_score([NOW, NOW - 1, NOW - 2], NOW)
        self.assertAlmostEqual(score, 0.21723, places=5)
        self.assertGreaterEqual(score, groupflow.FLOW_ACTIVITY_MIN)

    def test_busy_group_saturates_near_one(self):
        # 20 条挤在 20 秒内：瞬时速率拉满
        score = groupflow.activity_score([NOW - i for i in range(20)], NOW)
        self.assertEqual(score, 1.0)
        # 90 秒内 12 条：热，但不至于顶格
        score = groupflow.activity_score([NOW - i for i in range(12)], NOW)
        self.assertAlmostEqual(score, 0.820508, places=6)
        self.assertGreater(score, groupflow.FLOW_ACTIVITY_MIN)

    def test_burst_bonus_raises_the_score(self):
        quiet = groupflow.activity_score([NOW - 40, NOW - 38], NOW)
        burst = groupflow.activity_score(
            [NOW - 40, NOW - 38] + [NOW - i for i in range(8)], NOW
        )
        self.assertAlmostEqual(quiet, 0.032012, places=6)
        self.assertAlmostEqual(burst, 0.760788, places=6)
        self.assertGreater(burst, quiet + 0.2)

    def test_freshness_decay_makes_old_messages_cheaper(self):
        fresh = groupflow.activity_score([NOW], NOW)
        minute_old = groupflow.activity_score([NOW - 60], NOW)
        ten_minutes_old = groupflow.activity_score([NOW - 600], NOW)
        self.assertGreater(fresh, minute_old)
        self.assertGreater(minute_old, ten_minutes_old)
        # 半衰 60 秒：一分钟后新鲜度恰好 0.5（点值乘子在两项速率不变时体现）
        self.assertAlmostEqual(minute_old, 0.024466, places=6)
        self.assertAlmostEqual(ten_minutes_old, 0.014441, places=6)
        self.assertLess(ten_minutes_old, groupflow.FLOW_ACTIVITY_MIN)

    def test_bad_timestamps_are_skipped_not_counted(self):
        self.assertEqual(
            groupflow.activity_score(["x", None, float("nan"), float("inf"), NOW], NOW),
            groupflow.activity_score([NOW], NOW),
        )

    def test_future_timestamp_does_not_overflow(self):
        # Python 的 math.exp 负数过大时会 OverflowError；这里必须按「刚刚」处理
        score = groupflow.activity_score([NOW + 1000], NOW)
        self.assertAlmostEqual(score, groupflow.activity_score([NOW], NOW), places=9)

    def test_absurd_volume_clamps_to_one(self):
        score = groupflow.activity_score([NOW - 0.001 * i for i in range(1000)], NOW)
        self.assertEqual(score, 1.0)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_window_is_ninety_seconds(self):
        """90 秒窗口外的不算：整体推到 100 秒前，分数掉到 0.15 以下。"""
        for offset in (0, 60):
            with self.subTest(offset=offset):
                score = groupflow.activity_score(
                    [NOW - offset - i for i in range(12)], NOW
                )
                self.assertGreater(score, groupflow.FLOW_ACTIVITY_MIN)
        score = groupflow.activity_score([NOW - 100 - i for i in range(12)], NOW)
        self.assertLess(score, groupflow.FLOW_ACTIVITY_MIN)

    def test_suppression_line_constant(self):
        self.assertEqual(groupflow.FLOW_ACTIVITY_MIN, 0.15)


def _good_group() -> dict:
    """刚过完所有硬闸的群状态。"""
    return {
        "blocked_until": 0.0,
        "flow_open_until": NOW + 600,
        "flow_hold_until": 0.0,
        "flow_ignored": 0,
        "flow_replies": 0,
        "flow_last_reply_at": 0.0,
        "flow_last_attempt_at": 0.0,
    }


def _consider(group: dict, **overrides):
    kwargs = dict(
        max_replies=3,
        min_gap_seconds=120.0,
        hourly_cap=10,
        hour_count=0,
        ignored_exit=3,
        attempt_gap_seconds=60.0,
    )
    kwargs.update(overrides)
    return groupflow.flow_should_consider(group, NOW, **kwargs)


class FlowActivityGateTest(unittest.TestCase):
    """活跃度闸门：只抑制、理由正确、默认值不改变旧行为、不新增调用点。"""

    def test_low_activity_is_rejected_with_the_exact_reason(self):
        ok, why = _consider(_good_group(), activity=0.05)
        self.assertFalse(ok)
        self.assertEqual(why, "群里刚安静下来，不硬接")

    def test_boundary_is_strictly_below(self):
        self.assertEqual(_consider(_good_group(), activity=0.15)[0], True)
        ok, why = _consider(_good_group(), activity=0.149)
        self.assertFalse(ok)
        self.assertEqual(why, "群里刚安静下来，不硬接")

    def test_zero_activity_is_rejected(self):
        ok, _ = _consider(_good_group(), activity=0.0)
        self.assertFalse(ok)

    def test_default_activity_keeps_old_behaviour(self):
        self.assertEqual(_consider(_good_group())[0], True)
        self.assertEqual(_consider(_good_group(), activity=1.0)[0], True)
        self.assertEqual(_consider(_good_group(), activity=0.8)[0], True)

    def test_hard_gates_still_win_over_activity(self):
        """硬闸在活跃度之前：安静时段被拦时，理由不能变成「群里刚安静下来」。"""
        ok, why = _consider(_good_group(), is_quiet=True, activity=0.0)
        self.assertFalse(ok)
        self.assertEqual(why, "现在是安静时段")

    def test_activity_never_opens_a_new_path(self):
        """只抑制：activity=1.0 判 False 的状态，任何 activity 都必须还是 False。"""
        # (群状态覆盖, 闸门参数覆盖)：每一条在默认 activity=1.0 下都必须被拦。
        broken_states = [
            ({}, dict(asleep=True)),
            ({}, dict(is_quiet=True)),
            ({}, dict(interject_enabled=False, context_note="群里现在是 A 和 B 在来回聊")),
            ({"flow_last_reply_at": NOW - 1},
             dict(context_note="这句话在叫「小明」", interject_gap_seconds=600.0)),
            ({"flow_replies": 1}, dict(max_replies=1)),
            ({}, dict(hourly_cap=1, hour_count=1)),
            ({}, dict(min_gap_seconds=10 ** 12)),
            ({}, dict(attempt_gap_seconds=10 ** 12)),
            ({"flow_ignored": 1}, dict(ignored_exit=1)),
        ]
        for group_over, consider_over in broken_states:
            group = _good_group()
            group.update(group_over)
            base_ok, _base_why = _consider(group, **consider_over)
            self.assertFalse(
                base_ok, f"构造的坏状态本身应当被拦：{group_over}/{consider_over}"
            )
            for activity in (0.0, 0.05, 0.149, 0.5, 1.0):
                ok, why = _consider(group, activity=activity, **consider_over)
                self.assertFalse(
                    ok,
                    f"activity={activity} 把本应拒绝的状态放行了：{group_over}/{consider_over}",
                )
        # 反过来：只有低活跃度能让「本来可接」变成「不接」，反之永不成立
        for activity in (0.15, 0.2, 0.5, 0.8, 1.0):
            ok, _ = _consider(_good_group(), activity=activity)
            self.assertTrue(ok)

    def test_activity_matrix_does_not_change_existing_verdicts(self):
        """activity=1.0 与不传 activity 对每个既有闸门组合逐一等价。"""
        hard_gate_cases = [
            {},
            dict(asleep=True),
            dict(is_quiet=True),
            dict(max_replies=1),
            dict(hourly_cap=1),
            dict(min_gap_seconds=10 ** 9),
            dict(attempt_gap_seconds=10 ** 9),
            dict(ignored_exit=1),
            dict(interject_enabled=False, context_note="两人在来回聊"),
            dict(context_note="在叫「小明」", interject_gap_seconds=600.0),
        ]
        groups = [_good_group()]
        blocked = _good_group()
        blocked["blocked_until"] = NOW + 60
        groups.append(blocked)
        no_window = _good_group()
        no_window["flow_open_until"] = 0.0
        groups.append(no_window)
        hold = _good_group()
        hold["flow_hold_until"] = NOW + 60
        groups.append(hold)
        replied = _good_group()
        replied["flow_replies"] = 9
        groups.append(replied)
        ignored = _good_group()
        ignored["flow_ignored"] = 9
        groups.append(ignored)
        recent = _good_group()
        recent["flow_last_reply_at"] = NOW - 1
        groups.append(recent)
        attempted = _good_group()
        attempted["flow_last_attempt_at"] = NOW - 1
        groups.append(attempted)
        for case in hard_gate_cases:
            for group in groups:
                with self.subTest(case=case, group=group):
                    old = _consider(group, **case)
                    new = _consider(group, activity=1.0, **case)
                    self.assertEqual(old, new)
        # hour_count 单独盖一遍（不是 kwarg 覆盖能表达的）
        self.assertEqual(
            _consider(_good_group(), hour_count=99),
            _consider(_good_group(), hour_count=99, activity=1.0),
        )

    def test_bad_activity_values_do_not_change_behaviour(self):
        for bad in (None, "x", float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                self.assertEqual(
                    _consider(_good_group(), activity=bad),
                    _consider(_good_group()),
                )
        # 数字字符串是能读的，按数字处理
        self.assertEqual(_consider(_good_group(), activity="0.05")[1], "群里刚安静下来，不硬接")

    def test_no_new_call_sites(self):
        """两处调用点都必须传 activity，且不能有别处偷偷再调一次。"""
        src = inspect.getsource(engine_mod)
        self.assertEqual(src.count("groupflow.flow_should_consider("), 2)
        observe_src = inspect.getsource(SocialEngine._observe_group)
        inner_src = inspect.getsource(SocialEngine._flow_reply_inner)
        for label, text in (("_observe_group", observe_src), ("_flow_reply_inner", inner_src)):
            with self.subTest(path=label):
                self.assertIn("activity=self._group_activity(bid, gid, now)", text)
        # 活跃度分数只在 engine 的这两个辅助方法里算，群破冰/起话头不许用它开口
        self.assertEqual(src.count("groupflow.activity_score("), 1)


class GroupActivityTrackingTest(unittest.TestCase):
    """engine：只记真人（非 self、非兄弟 bot）群消息的时间戳，30 分钟裁掉。"""

    def test_human_messages_are_tracked(self):
        eng = _engine()
        asyncio.run(eng._observe_group(_group_event("u1"), "群里聊起来了"))
        asyncio.run(eng._observe_group(_group_event("u2"), "我也这么觉得"))
        asyncio.run(eng._observe_group(_group_event("u3"), "哈哈哈哈真的"))
        key = ("bot1", "g1")
        self.assertIn(key, eng._group_activity_ts)
        self.assertEqual(len(eng._group_activity_ts[key]), 3)
        self.assertGreater(eng._group_activity("bot1", "g1", eng._time()), 0.15)

    def test_self_and_sibling_bot_messages_are_not_tracked(self):
        eng = _engine()
        eng.state.note_bot_id("bot2")
        asyncio.run(eng._observe_group(_group_event("u1"), "群里聊起来了"))
        before = len(eng._group_activity_ts[("bot1", "g1")])
        # 自己说的
        asyncio.run(eng._observe_group(_group_event("bot1"), "我先说一句"))
        self.assertEqual(len(eng._group_activity_ts[("bot1", "g1")]), before)
        # 兄弟 bot 说的
        asyncio.run(eng._observe_group(_group_event("bot2"), "另一台 bot 也在"))
        self.assertEqual(len(eng._group_activity_ts[("bot1", "g1")]), before)
        # 但群里消息计数照常（那是既有行为，不能因为活跃度统计而改变）
        self.assertEqual(eng.state.group("bot1", "g1")["msg_count"], 3)

    def test_old_timestamps_are_trimmed(self):
        clock = _Clock(NOW)
        eng = _engine(clock)
        asyncio.run(eng._observe_group(_group_event("u1"), "第一句"))
        asyncio.run(eng._observe_group(_group_event("u2"), "第二句"))
        self.assertEqual(len(eng._group_activity_ts[("bot1", "g1")]), 2)
        clock.advance(GROUP_ACTIVITY_KEEP_SECONDS + 1)
        asyncio.run(eng._observe_group(_group_event("u3"), "很久以后的一句"))
        dq = eng._group_activity_ts[("bot1", "g1")]
        self.assertEqual(len(dq), 1, f"30 分钟前的群消息应当被裁掉：{list(dq)}")
        self.assertAlmostEqual(dq[0], clock.now, places=3)

    def test_unknown_group_activity_is_zero(self):
        eng = _engine()
        self.assertEqual(eng._group_activity("bot1", "never-seen", NOW), 0.0)

    def test_clock_rewind_does_not_reorder_the_deque(self):
        clock = _Clock(NOW)
        eng = _engine(clock)
        asyncio.run(eng._observe_group(_group_event("u1"), "第一句"))
        first = eng._group_activity_ts[("bot1", "g1")][-1]
        clock.set(NOW - 100)  # 小回拨（< 300s 阈值）：_time() 仍钳在 NOW
        asyncio.run(eng._observe_group(_group_event("u2"), "第二句"))
        dq = eng._group_activity_ts[("bot1", "g1")]
        self.assertEqual(list(dq), sorted(dq))
        self.assertGreaterEqual(dq[-1], first)

    def test_flow_gate_receives_the_computed_activity(self):
        """`_observe_group` 真把 activity 传进闸门（不只是源码里有那行字）。"""
        eng = _engine()
        eng.state.note_bot_id("bot2")
        eng.state.group("bot1", "g1")["flow_open_until"] = NOW + 600  # 窗口开着
        captured = []

        def _fake_gate(group, now, **kwargs):
            captured.append((now, kwargs.get("activity")))
            return False, "测试桩：不接"

        with patch(
            "social.engine.groupflow.flow_should_consider", side_effect=_fake_gate
        ):
            asyncio.run(eng._observe_group(_group_event("u1"), "群里聊起来了"))
            asyncio.run(eng._observe_group(_group_event("u2"), "我也这么觉得"))
            asyncio.run(eng._observe_group(_group_event("u3"), "哈哈哈哈真的"))
            humans_calls = len(captured)
            asyncio.run(eng._observe_group(_group_event("bot1"), "我自己说一句"))
            asyncio.run(eng._observe_group(_group_event("bot2"), "另一台 bot 也在"))
        self.assertEqual(humans_calls, 3, "三条真人消息各走一次闸门")
        self.assertEqual(len(captured), humans_calls, "自己/兄弟 bot 不该走到心流闸门")
        now, activity = captured[-1]
        self.assertAlmostEqual(
            activity, eng._group_activity("bot1", "g1", now), places=6
        )
        self.assertGreater(activity, groupflow.FLOW_ACTIVITY_MIN)

    def test_generation_recheck_also_receives_activity(self):
        """生成前重校（`_flow_reply_inner`）同样要传 activity。"""
        eng = _engine()
        eng.running = True
        state = eng.state.group("bot1", "g1")
        state["umo"] = "aiocqhttp:GroupMessage:g1"
        state["flow_open_until"] = NOW + 600
        captured = []

        def _fake_gate(group, now, **kwargs):
            captured.append((now, kwargs.get("activity")))
            return False, "测试桩：不接"

        with patch(
            "social.engine.groupflow.flow_should_consider", side_effect=_fake_gate
        ):
            asyncio.run(eng._observe_group(_group_event("u1"), "群里聊起来了"))
            asyncio.run(eng._observe_group(_group_event("u2"), "我也这么觉得"))
            asyncio.run(eng._observe_group(_group_event("u3"), "哈哈哈哈真的"))
            observed = len(captured)
            self.assertEqual(observed, 3)
            asyncio.run(SocialEngine._flow_reply_inner(eng, "bot1", "g1", NOW))
        self.assertEqual(len(captured), observed + 1, "重校也必须走一次闸门")
        now, activity = captured[-1]
        self.assertAlmostEqual(
            activity, eng._group_activity("bot1", "g1", now), places=6
        )
        self.assertGreater(activity, groupflow.FLOW_ACTIVITY_MIN)

    def test_stale_activity_is_below_suppression(self):
        """只在内存里算：很久没消息时分数自然落到抑制线以下。"""
        eng = _engine()
        asyncio.run(eng._observe_group(_group_event("u1"), "群里聊起来了"))
        asyncio.run(eng._observe_group(_group_event("u2"), "我也这么觉得"))
        asyncio.run(eng._observe_group(_group_event("u3"), "哈哈哈哈真的"))
        self.assertGreater(eng._group_activity("bot1", "g1", eng._time()), 0.15)
        self.assertLess(
            eng._group_activity("bot1", "g1", eng._time() + 600), 0.15
        )


# ═══════════════════════════════════════════════════════════════════
# C. 兄弟 bot 可聊但防刷屏
# ═══════════════════════════════════════════════════════════════════


class BotPeerBlockReasonTest(unittest.TestCase):
    """纯函数三态 + 边界 + 坏值。"""

    def test_never_chatted_is_blocked(self):
        for u in ({}, None, {"message_count": 0}, {"message_count": -3},
                  {"message_count": "x"}, "不是字典"):
            with self.subTest(u=u):
                self.assertNotEqual(bot_peer_block_reason(u, NOW, "2026-10-27"), "")

    def test_first_ever_message_is_allowed(self):
        u = {"message_count": 1}
        self.assertEqual(bot_peer_block_reason(u, NOW, "2026-10-27"), "")

    def test_six_hour_gap(self):
        u = {"message_count": 5}
        # 差一秒钟不到 6 小时：拦
        u["bot_peer_last_sent"] = NOW - BOT_PEER_MIN_GAP_SECONDS + 1
        self.assertNotEqual(bot_peer_block_reason(u, NOW, ""), "")
        # 正好 6 小时：放行（严格小于才拦）
        u["bot_peer_last_sent"] = NOW - BOT_PEER_MIN_GAP_SECONDS
        self.assertEqual(bot_peer_block_reason(u, NOW, ""), "")
        # 更早：放行
        u["bot_peer_last_sent"] = NOW - BOT_PEER_MIN_GAP_SECONDS - 1
        self.assertEqual(bot_peer_block_reason(u, NOW, ""), "")

    def test_daily_cap(self):
        today = "2026-10-27"
        u = {
            "message_count": 5,
            "bot_peer_last_sent": NOW - 7 * 3600,
            "bot_peer_sent_day": today,
            "bot_peer_sent_count": BOT_PEER_DAILY_CAP,
        }
        self.assertNotEqual(bot_peer_block_reason(u, NOW, today), "")
        # 计数还没到上限（坏字段口径）：放行
        u["bot_peer_sent_count"] = 0
        self.assertEqual(bot_peer_block_reason(u, NOW, today), "")
        # 隔天恢复：日期不是今天了，昨天那条不再占今天额度
        u["bot_peer_sent_count"] = BOT_PEER_DAILY_CAP
        u["bot_peer_sent_day"] = "2026-10-26"
        self.assertEqual(bot_peer_block_reason(u, NOW, today), "")

    def test_same_day_block_holds_even_after_six_hours(self):
        today = "2026-10-27"
        u = {
            "message_count": 5,
            "bot_peer_last_sent": NOW - 20 * 3600,
            "bot_peer_sent_day": today,
            "bot_peer_sent_count": 1,
        }
        self.assertIn("今天", bot_peer_block_reason(u, NOW, today))

    def test_bad_fields_do_not_raise(self):
        bad = {
            "message_count": 3,
            "bot_peer_last_sent": "x",
            "bot_peer_sent_day": None,
            "bot_peer_sent_count": "y",
        }
        self.assertEqual(bot_peer_block_reason(bad, NOW, "2026-10-27"), "")
        # now 坏掉也不抛
        for now in (float("nan"), "x", None):
            with self.subTest(now=now):
                bot_peer_block_reason({"message_count": 3}, now, "")
        # last_sent 在未来（时钟回拨）：按「还没到 6 小时」继续拦
        future = {"message_count": 3, "bot_peer_last_sent": NOW + 3600}
        self.assertNotEqual(bot_peer_block_reason(future, NOW, ""), "")

    def test_empty_today_skips_the_daily_branch(self):
        u = {
            "message_count": 1,
            "bot_peer_last_sent": NOW - 7 * 3600,
            "bot_peer_sent_day": "2026-10-27",
            "bot_peer_sent_count": 5,
        }
        self.assertEqual(bot_peer_block_reason(u, NOW, ""), "")


class BotPeerObserveTest(unittest.TestCase):
    """`observe`：兄弟 bot 照常进用户账本，但不排 cue/loop；真人行为不变。"""

    def test_sibling_bot_message_creates_a_user_without_cue_or_loop(self):
        eng = _engine()
        eng.state.note_bot_id("bot2")
        with patch(
            "social.engine.extract_cue", return_value=("面试", NOW + 3600)
        ) as cue, patch(
            "social.engine.extract_open_loop", return_value=("面试", NOW + 7200)
        ) as loop:
            asyncio.run(
                eng.observe(_private_event("bot2", "我明天有个面试，先睡啦"))
            )
        users = eng.state.bot("bot1")["users"]
        self.assertIn("bot2", users, "兄弟 bot 的私聊消息应当被记成用户")
        u = users["bot2"]
        self.assertEqual(u["message_count"], 1)
        self.assertEqual(u["umo"], "aiocqhttp:FriendMessage:bot2")
        self.assertEqual(u["cue"], "")
        self.assertEqual(u["loop"], "")
        self.assertEqual(u["anchors"], [])
        cue.assert_not_called()
        loop.assert_not_called()

    def test_sibling_bot_message_is_still_stored_as_conversation(self):
        eng = _engine()
        eng.state.note_bot_id("bot2")
        asyncio.run(eng.observe(_private_event("bot2", "在吗")))
        u = eng.state.bot("bot1")["users"]["bot2"]
        self.assertEqual(u["conversation"][-1]["text"], "在吗")
        self.assertEqual(u["conversation"][-1]["dir"], "in")
        self.assertEqual(u["last_message"], "在吗")

    def test_human_message_still_extracts_cue_and_loop(self):
        eng = _engine()
        with patch(
            "social.engine.extract_cue", return_value=("面试", NOW + 3600)
        ) as cue, patch("social.engine.extract_open_loop", return_value=("", 0.0)):
            asyncio.run(eng.observe(_private_event("u1", "我明天有个面试")))
        u = eng.state.bot("bot1")["users"]["u1"]
        self.assertEqual(u["cue"], "面试")
        self.assertGreater(u["cue_due"], 0)
        self.assertTrue(u["anchors"], "真人的时间锚点仍然挂进由头池")
        cue.assert_called_once()

    def test_sibling_bot_not_in_known_list_still_behaves_like_a_human(self):
        """判据只有 known_bot_ids：名单里没有它时就是普通用户（老行为不变）。"""
        eng = _engine()
        with patch("social.engine.extract_cue", return_value=("面试", NOW + 3600)), \
                patch("social.engine.extract_open_loop", return_value=("", 0.0)):
            asyncio.run(eng.observe(_private_event("bot2", "我明天有个面试")))
        u = eng.state.bot("bot1")["users"]["bot2"]
        self.assertEqual(u["cue"], "面试")

    def test_self_message_is_still_ignored(self):
        eng = _engine()
        asyncio.run(eng.observe(_private_event("bot1", "这是我自己说的话")))
        self.assertNotIn("bot1", eng.state.bot("bot1")["users"])

    def test_group_flow_does_not_consider_sibling_bot_as_a_speaker(self):
        """群聊侧保持现状：兄弟 bot 的消息仍不触发心流（不派后台任务）。"""
        eng = _engine()
        eng.state.note_bot_id("bot2")
        state = eng.state.group("bot1", "g1")
        state["flow_open_until"] = NOW + 3600  # 窗口开着
        before = len(eng._bg_tasks)
        asyncio.run(eng._observe_group(_group_event("bot2"), "另一台 bot 说了一句"))
        self.assertEqual(len(eng._bg_tasks), before, "兄弟 bot 的群消息不该派心流任务")


class BotPeerSpeakGateTest(unittest.TestCase):
    """`_speak_inner` 早段的兄弟 bot 闸 + 成功后的记账。"""

    def _peer_user(self, eng: SocialEngine, uid: str = "bot2", **fields):
        eng.state.note_bot_id(uid)
        u = eng.state.user("bot1", uid)
        u["umo"] = f"aiocqhttp:FriendMessage:{uid}"
        u.update(fields)
        return u

    @staticmethod
    def _local_day(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    def test_known_bot_peer_detection(self):
        eng = _engine()
        self._peer_user(eng)
        self.assertTrue(eng._is_known_bot_peer("bot2"))
        self.assertFalse(eng._is_known_bot_peer("u1"))
        self.assertFalse(eng._is_known_bot_peer(""))

    def test_broken_state_counts_as_human(self):
        eng = _engine()

        def _boom():
            raise RuntimeError("state 坏了")

        eng.state.known_bot_ids = _boom
        self.assertFalse(eng._is_known_bot_peer("bot2"))

    def test_gate_reason_for_a_bot_peer(self):
        eng = _engine()
        u = self._peer_user(eng, message_count=0)
        reason = eng._bot_peer_gate_reason("bot1", "bot2", u, eng._time())
        self.assertIn("没跟这台 bot 聊过", reason)
        u["message_count"] = 1
        self.assertEqual(eng._bot_peer_gate_reason("bot1", "bot2", u, eng._time()), "")

    def test_human_user_is_never_gated_by_bot_peer_rules(self):
        eng = _engine()
        u = eng.state.user("bot1", "u1")
        u["umo"] = "aiocqhttp:FriendMessage:u1"
        u["message_count"] = 5
        u["bot_peer_last_sent"] = NOW - 60
        u["bot_peer_sent_day"] = self._local_day(NOW)
        u["bot_peer_sent_count"] = 9
        self.assertEqual(eng._bot_peer_gate_reason("bot1", "u1", u, NOW), "")

    def test_same_day_second_attempt_blocked_then_next_day_recovers(self):
        # 用本地「当天 01:00」做基准：+6h 仍在同一天（07:00），+24h 一定是第二天
        dt = datetime.fromtimestamp(NOW)
        base = datetime(dt.year, dt.month, dt.day, 1, 0, 0).timestamp()
        clock = _Clock(base)
        eng = _engine(clock)
        u = self._peer_user(eng, message_count=1)
        today = eng._moment_of("bot1", base).strftime("%Y-%m-%d")
        self.assertEqual(eng._bot_peer_gate_reason("bot1", "bot2", u, base), "")
        note_bot_peer_sent(u, base, today)
        # 刚发完：6 小时闸先拦
        self.assertIn("6 小时", eng._bot_peer_gate_reason("bot1", "bot2", u, base + 60))
        # 过了 6 小时但仍是同一天：日上限拦
        same_day = base + 6 * 3600 + 60
        self.assertEqual(eng._moment_of("bot1", same_day).strftime("%Y-%m-%d"), today)
        self.assertIn("今天", eng._bot_peer_gate_reason("bot1", "bot2", u, same_day))
        # 第二天：恢复
        next_day = base + 24 * 3600
        self.assertNotEqual(eng._moment_of("bot1", next_day).strftime("%Y-%m-%d"), today)
        self.assertEqual(eng._bot_peer_gate_reason("bot1", "bot2", u, next_day), "")

    def test_speak_inner_blocks_bot_peer_before_touching_the_generator(self):
        class _BoomGenerator:
            def __getattr__(self, name):
                raise AssertionError("被兄弟 bot 闸门拦下时不应该碰生成器")

        eng = _engine()
        u = self._peer_user(eng, message_count=0)
        eng.generator = _BoomGenerator()
        sent, reason = asyncio.run(
            SocialEngine._speak_inner(eng, "bot1", "bot2", u, 1.0, NOW)
        )
        self.assertFalse(sent)
        self.assertIn("没跟这台 bot 聊过", reason)

    def test_speak_inner_lets_a_human_past_the_bot_peer_gate(self):
        """真人带同样的 bot_peer 字段也不会被这道闸拦（理由来自后面的闸）。"""
        eng = _engine()
        u = eng.state.user("bot1", "u1")
        u["umo"] = "aiocqhttp:FriendMessage:u1"
        u["message_count"] = 5
        u["bot_peer_last_sent"] = NOW - 60
        u["bot_peer_sent_day"] = self._local_day(NOW)
        u["bot_peer_sent_count"] = 9
        u["send_fail_until"] = NOW + 3600  # 后面那道「上次没发出去」的闸
        sent, reason = asyncio.run(
            SocialEngine._speak_inner(eng, "bot1", "u1", u, 1.0, NOW)
        )
        self.assertFalse(sent)
        self.assertIn("没发出去", reason)
        self.assertNotIn("bot", reason)

    def test_success_path_is_wired_to_bookkeeping(self):
        src = inspect.getsource(SocialEngine._speak_inner)
        gate_at = src.index("_bot_peer_gate_reason(")
        note_at = src.index("note_bot_peer_sent(")
        self.assertLess(gate_at, note_at)
        # 闸门必须在任何生成调用之前
        self.assertLess(gate_at, src.index("self.generator."))


class BotPeerBookkeepingTest(unittest.TestCase):
    """成功后更新 user 的 bot_peer_* 三字段（纯函数部分）。"""

    TODAY = "2026-10-27"

    def test_first_send_records_all_three_fields(self):
        u = {"message_count": 1}
        note_bot_peer_sent(u, NOW, self.TODAY)
        self.assertEqual(u["bot_peer_last_sent"], NOW)
        self.assertEqual(u["bot_peer_sent_day"], self.TODAY)
        self.assertEqual(u["bot_peer_sent_count"], 1)

    def test_same_day_second_send_increments(self):
        u = {"message_count": 1, "bot_peer_sent_day": self.TODAY, "bot_peer_sent_count": 1}
        note_bot_peer_sent(u, NOW + 100, self.TODAY)
        self.assertEqual(u["bot_peer_sent_count"], 2)
        self.assertEqual(u["bot_peer_sent_day"], self.TODAY)
        self.assertEqual(u["bot_peer_last_sent"], NOW + 100)

    def test_next_day_resets_the_counter(self):
        u = {
            "message_count": 1,
            "bot_peer_sent_day": "2026-10-26",
            "bot_peer_sent_count": 1,
            "bot_peer_last_sent": NOW - 86400,
        }
        note_bot_peer_sent(u, NOW, self.TODAY)
        self.assertEqual(u["bot_peer_sent_day"], self.TODAY)
        self.assertEqual(u["bot_peer_sent_count"], 1)

    def test_bad_input_does_not_raise(self):
        u = {}
        note_bot_peer_sent(u, float("nan"), self.TODAY)
        self.assertEqual(u["bot_peer_last_sent"], 0.0)
        self.assertEqual(u["bot_peer_sent_count"], 1)
        note_bot_peer_sent(u, "x", "")  # 不抛，日期为空时不更新 day
        note_bot_peer_sent("不是字典", NOW, self.TODAY)  # 不抛


class DefaultUserFieldsTest(unittest.TestCase):
    """`state._default_user` 补的默认值：新用户与老档案都要有。"""

    def test_default_user_has_the_three_fields(self):
        defaults = SocialState._default_user()
        self.assertEqual(defaults["bot_peer_last_sent"], 0.0)
        self.assertEqual(defaults["bot_peer_sent_day"], "")
        self.assertEqual(defaults["bot_peer_sent_count"], 0)

    def test_new_user_gets_them(self):
        tmp = tempfile.mkdtemp()
        st = SocialState(str(Path(tmp) / "s.json"))
        u = st.user("b", "u")
        self.assertEqual(u["bot_peer_last_sent"], 0.0)
        self.assertEqual(u["bot_peer_sent_day"], "")
        self.assertEqual(u["bot_peer_sent_count"], 0)

    def test_old_record_is_migrated(self):
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "s.json"
        path.write_text(
            '{"bots": {"b": {"users": {"u": {"message_count": 3, "umo": "x"}}}}}',
            encoding="utf-8",
        )
        st = SocialState(str(path))
        u = st.user("b", "u")
        self.assertEqual(u["message_count"], 3)
        self.assertEqual(u["bot_peer_last_sent"], 0.0)
        self.assertEqual(u["bot_peer_sent_day"], "")
        self.assertEqual(u["bot_peer_sent_count"], 0)


if __name__ == "__main__":
    unittest.main()
