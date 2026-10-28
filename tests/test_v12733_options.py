"""v1.27.33：给那些「从没被测试按名点过」的配置项补上生效证明。

`_conf_schema.json` 有 107 个 key；其中不少在 tests/ 里从未被按名引用过。它们
要么是纯数值阈值（夹取对不对、边界会不会炸），要么是开关（关掉之后行为真的变了
吗）。这一层最容易悄悄坏掉：schema 改了、clamp 忘了跟，用户填的值被无声吃掉，
界面上还显示着他填的那个数——比报错更难发现。

所以每一项都问两个问题：

1. **我填进去的值，读出来还是不是那个值**（越界时有没有按文档夹到边界）；
2. **它真的改变了行为吗**（不是只躺在 dataclass 里）。

第 2 条尽量走真实代码路径：阈值走 `desire.mood_multiplier`、周末加成与精力阈值
走 `SocialEngine.settle_minds`（同一个 dt 只换配置，比 urge 的增量）、安静时段
走 `SocialEngine._quiet_now`、分段走 `pacing.plan_segments`、清理走
`SocialState.prune_expired`。测不到的（需要完整 AstrBot 宿主的那几个）在文件末尾
单列，并写明它们各自已经在哪个 file:line 被读过。
"""
from __future__ import annotations

import json
import logging
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"

README_PATH = PLUGIN_ROOT / "README.md"


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_options_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import desire, groupflow, pacing  # noqa: E402
from social.config import (  # noqa: E402
    CLOSER_AFTER_MAX,
    CLOSER_AFTER_MIN,
    CONTEXT_INJECT_MAX,
    CONTEXT_INJECT_MIN,
    ENERGY_THRESHOLD_MAX,
    ENERGY_THRESHOLD_MIN,
    FLOW_HOURLY_CAP_MAX,
    FLOW_HOURLY_CAP_MIN,
    FLOW_IGNORED_EXIT_MAX,
    FLOW_IGNORED_EXIT_MIN,
    FLOW_MAX_REPLIES_MAX,
    FLOW_MAX_REPLIES_MIN,
    FLOW_MIN_GAP_MAX,
    FLOW_MIN_GAP_MIN,
    FOLLOWUP_AFTER_MAX,
    FOLLOWUP_AFTER_MIN,
    FOLLOWUP_COOLDOWN_MAX,
    FOLLOWUP_COOLDOWN_MIN,
    FOLLOWUP_MAX_MAX,
    FOLLOWUP_MAX_MIN,
    GREETING_MIDDAY_END_DEFAULT,
    GREETING_MIDDAY_START_DEFAULT,
    GREETING_MORNING_END_DEFAULT,
    GREETING_MORNING_START_DEFAULT,
    GREETING_NIGHT_END_DEFAULT,
    GREETING_NIGHT_START_DEFAULT,
    GROUP_REF_PROMPT_MAX,
    GROUP_REF_PROMPT_MIN,
    GROUP_REF_SAMPLE_MAX,
    GROUP_REF_SAMPLE_MIN,
    HOUR_MAX,
    HOUR_MIN,
    LOOP_MAX_HOURS_CEIL,
    LOOP_MIN_HOURS_CEIL,
    LOOP_MIN_HOURS_FLOOR,
    PROBE_AFTER_MAX,
    PROBE_AFTER_MIN,
    QUIET_END_DEFAULT,
    QUIET_START_DEFAULT,
    REPLY_WINDOW_MAX,
    REPLY_WINDOW_MIN,
    SOCIAL_ENERGY_THRESHOLD_MAX,
    SOCIAL_ENERGY_THRESHOLD_MIN,
    TOPIC_MEMORY_MAX,
    TOPIC_MEMORY_MIN,
    USER_RETENTION_MAX,
    USER_RETENTION_MIN,
    SocialConfig,
)
from social.engine import PENDING_IGNORE_MIN_HOURS, WEEKEND_MOOD_BOOST, SocialEngine  # noqa: E402
from social.history_ingest import normalize_seed_entries  # noqa: E402
from social.reasoning import greeting_due, greeting_window_kind  # noqa: E402
from social.state import SocialState  # noqa: E402

SCHEMA = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

# 2026-08-22 是周六，2026-08-19 是周三——同一时刻只换配置，比 urge 的增量。
SATURDAY = datetime(2026, 8, 22, 15, 0)
WEDNESDAY = datetime(2026, 8, 19, 15, 0)


def _cfg(**overrides) -> SocialConfig:
    return SocialConfig.from_astrbot(dict(overrides))


def _engine(**overrides) -> SocialEngine:
    """引擎要的是**原始配置 dict**，不是 SocialConfig 实例。

    `SocialEngine.__init__` 里是 `self.cfg = SocialConfig.from_astrbot(config)`，而
    `from_astrbot` 是按 mapping 读的（`c.get(key, default)` 包在 try 里）。传一个
    dataclass 进去，`.get` 不存在、每一项都静默退回 schema 默认值——引擎照样起来，
    但所有选项都是默认的，测试会变成「在测默认值」而自己毫无察觉。
    """
    tmp = tempfile.mkdtemp()
    return SocialEngine(
        context=None,
        config=dict(overrides),
        state_path=str(Path(tmp) / "state.json"),
        data_dir=tmp,
    )


def _seed_user(engine: SocialEngine, bid: str = "bot1", uid: str = "u1", **fields) -> dict:
    user = {
        "umo": f"aiocqhttp:FriendMessage:{bid}-{uid}",
        "last_seen": 0.0,
        "last_sent": 0.0,
        "interest": 0.6,
        "urge": 0.0,
        # 没有 urge_at 时 `desire.settle` 只播种（dt=0，念头不涨），
        # 想观察「攒了多少」就必须先给它一个过去的基准。
        "urge_at": 0.0,
        "no_reply_streak": 0,
        "conversation": [],
        "topics": [],
    }
    user.update(fields)
    engine.state.bot(bid).setdefault("users", {})[uid] = user
    return user


# ── 追问链：三个时间点必须自洽 ──────────────────────────────────────


class FollowupChainOptionTest(unittest.TestCase):
    """`followup_after` / `followup_max` / `probe_after` / `followup_cooldown`
    是一组联动值：上限必须永远大于「多久算没声」，否则追问永远发不出去。"""

    def test_defaults(self):
        cfg = _cfg()
        self.assertEqual(cfg.followup_after_minutes, 30)
        self.assertEqual(cfg.followup_max_minutes, 70)
        self.assertEqual(cfg.probe_after_minutes, 12)
        self.assertEqual(cfg.followup_cooldown_minutes, 30)
        self.assertTrue(cfg.followup_enabled)

    def test_probe_after_clamped(self):
        self.assertEqual(_cfg(probe_after_minutes=0).probe_after_minutes, PROBE_AFTER_MIN)
        self.assertEqual(_cfg(probe_after_minutes=-5).probe_after_minutes, PROBE_AFTER_MIN)
        self.assertEqual(_cfg(probe_after_minutes=99999).probe_after_minutes, PROBE_AFTER_MAX)

    def test_followup_after_clamped(self):
        self.assertEqual(_cfg(followup_after_minutes=0).followup_after_minutes, FOLLOWUP_AFTER_MIN)
        self.assertEqual(_cfg(followup_after_minutes=99999).followup_after_minutes, FOLLOWUP_AFTER_MAX)

    def test_followup_cooldown_clamped(self):
        self.assertEqual(
            _cfg(followup_cooldown_minutes=0).followup_cooldown_minutes, FOLLOWUP_COOLDOWN_MIN
        )
        self.assertEqual(
            _cfg(followup_cooldown_minutes=99999).followup_cooldown_minutes, FOLLOWUP_COOLDOWN_MAX
        )

    def test_max_stays_above_after_even_when_user_asks_for_less(self):
        """用户把 after 顶到 240、max 留在最小 15 时，max 必须被抬到 245——
        不然「多久算没声」比「最晚多久追问」还晚，这条链路彻底失效。"""
        cfg = _cfg(followup_after_minutes=FOLLOWUP_AFTER_MAX, followup_max_minutes=FOLLOWUP_MAX_MIN)
        self.assertGreater(cfg.followup_max_minutes, cfg.followup_after_minutes)
        self.assertGreaterEqual(cfg.followup_max_minutes, cfg.followup_after_minutes + 5)

    def test_max_never_exceeds_its_own_ceiling(self):
        cfg = _cfg(followup_after_minutes=FOLLOWUP_AFTER_MAX, followup_max_minutes=99999)
        self.assertLessEqual(cfg.followup_max_minutes, FOLLOWUP_MAX_MAX)

    def test_followup_enabled_switch_survives(self):
        self.assertFalse(_cfg(followup_enabled=False).followup_enabled)
        self.assertTrue(_cfg(followup_enabled=True).followup_enabled)


# ── 回访窗口：loop_min < loop_max ─────────────────────────────────


class LoopAndCloserOptionTest(unittest.TestCase):
    def test_defaults(self):
        cfg = _cfg()
        self.assertEqual(cfg.loop_min_hours, 2.5)
        self.assertEqual(cfg.loop_max_hours, 14.0)
        self.assertEqual(cfg.closer_after_hours, 8)
        self.assertTrue(cfg.loop_enabled)
        self.assertTrue(cfg.closer_enabled)

    def test_loop_min_clamped(self):
        self.assertEqual(_cfg(loop_min_hours=0).loop_min_hours, LOOP_MIN_HOURS_FLOOR)
        self.assertEqual(_cfg(loop_min_hours=-3).loop_min_hours, LOOP_MIN_HOURS_FLOOR)
        self.assertEqual(_cfg(loop_min_hours=999).loop_min_hours, LOOP_MIN_HOURS_CEIL)

    def test_loop_max_stays_above_min(self):
        cfg = _cfg(loop_min_hours=20.0, loop_max_hours=1.0)
        self.assertGreater(cfg.loop_max_hours, cfg.loop_min_hours)
        self.assertGreaterEqual(cfg.loop_max_hours, cfg.loop_min_hours + 1.0)

    def test_loop_max_ceiling(self):
        self.assertEqual(_cfg(loop_max_hours=999).loop_max_hours, LOOP_MAX_HOURS_CEIL)

    def test_closer_after_hours_clamped(self):
        self.assertEqual(_cfg(closer_after_hours=0).closer_after_hours, CLOSER_AFTER_MIN)
        self.assertEqual(_cfg(closer_after_hours=-1).closer_after_hours, CLOSER_AFTER_MIN)
        self.assertEqual(_cfg(closer_after_hours=9999).closer_after_hours, CLOSER_AFTER_MAX)

    def test_switches(self):
        self.assertFalse(_cfg(loop_enabled=False).loop_enabled)
        self.assertFalse(_cfg(closer_enabled=False).closer_enabled)


# ── 安静时段：两个端点 + 引擎侧的 Core 优先 ───────────────────────


class QuietHoursOptionTest(unittest.TestCase):
    def test_default_window_wraps_midnight(self):
        cfg = _cfg()
        self.assertEqual((cfg.quiet_start, cfg.quiet_end), (QUIET_START_DEFAULT, QUIET_END_DEFAULT))
        self.assertTrue(cfg.in_quiet_hours(23))
        self.assertTrue(cfg.in_quiet_hours(0))
        self.assertTrue(cfg.in_quiet_hours(6))
        self.assertFalse(cfg.in_quiet_hours(7))
        self.assertFalse(cfg.in_quiet_hours(12))

    def test_same_endpoint_means_disabled(self):
        cfg = _cfg(quiet_start=8, quiet_end=8)
        for hour in range(24):
            self.assertFalse(cfg.in_quiet_hours(hour), f"{hour} 点不该安静")

    def test_daytime_window_is_half_open(self):
        cfg = _cfg(quiet_start=2, quiet_end=6)
        self.assertTrue(cfg.in_quiet_hours(2))
        self.assertTrue(cfg.in_quiet_hours(5))
        self.assertFalse(cfg.in_quiet_hours(6))
        self.assertFalse(cfg.in_quiet_hours(1))

    def test_engine_falls_back_to_the_option(self):
        engine = _engine(quiet_start=23, quiet_end=7)
        self.assertTrue(engine._quiet_now("bot1", 23))
        self.assertTrue(engine._quiet_now("bot1", 3))
        self.assertFalse(engine._quiet_now("bot1", 12))

    def test_engine_honours_a_disabled_option(self):
        engine = _engine(quiet_start=8, quiet_end=8)
        self.assertFalse(engine._quiet_now("bot1", 3), "端点相同＝关掉，不该还判安静")

    def test_core_night_window_wins_over_the_option(self):
        """Core 报了她今天几点睡，就用她的生物钟，不再看这个配置。"""
        engine = _engine(quiet_start=23, quiet_end=7)
        engine._night_windows["bot1"] = (1.0, 5.0)
        self.assertTrue(engine._quiet_now("bot1", 3))
        self.assertFalse(engine._quiet_now("bot1", 23), "Core 说了 1-5 点，23 点不该安静")


# ── 问候窗口：六个小时端点 ────────────────────────────────────────


class GreetingWindowOptionTest(unittest.TestCase):
    def test_defaults(self):
        cfg = _cfg()
        self.assertTrue(cfg.greeting_enabled)
        self.assertEqual(
            cfg.greeting_windows(),
            (
                (GREETING_MORNING_START_DEFAULT, GREETING_MORNING_END_DEFAULT),
                (GREETING_MIDDAY_START_DEFAULT, GREETING_MIDDAY_END_DEFAULT),
                (GREETING_NIGHT_START_DEFAULT, GREETING_NIGHT_END_DEFAULT % 24),
            ),
        )

    def test_night_window_runs_up_to_midnight(self):
        """默认夜窗是 (21, 0)：21 点到午夜，**不含** 0 点那一小时。"""
        morning, midday, night = _cfg().greeting_windows()
        self.assertEqual(greeting_window_kind(21, morning, night, midday), "night")
        self.assertEqual(greeting_window_kind(23, morning, night, midday), "night")
        self.assertIsNone(greeting_window_kind(0, morning, night, midday))
        self.assertIsNone(greeting_window_kind(20, morning, night, midday))

    def test_a_wrapping_window_still_wraps_when_asked_to(self):
        """真跨午夜的窗口（22→6）照样要能判。"""
        morning, _midday, _night = _cfg().greeting_windows()
        wrapped = (22, 6)
        self.assertEqual(greeting_window_kind(23, morning, wrapped, None), "night")
        self.assertEqual(greeting_window_kind(3, morning, wrapped, None), "night")
        self.assertIsNone(greeting_window_kind(6, morning, wrapped, None))

    def test_morning_window_edges(self):
        morning, midday, night = _cfg().greeting_windows()
        self.assertEqual(greeting_window_kind(7, morning, night, midday), "morning")
        self.assertIsNone(greeting_window_kind(11, morning, night, midday), "右端是开区间")
        self.assertIsNone(greeting_window_kind(6, morning, night, midday))

    def test_midday_can_be_switched_off_by_equal_endpoints(self):
        cfg = _cfg(greeting_midday_start=12, greeting_midday_end=12)
        _morning, midday, _night = cfg.greeting_windows()
        self.assertIsNone(midday, "起止相同＝关掉午间问候")

    def test_hours_are_clamped_to_a_real_day(self):
        cfg = _cfg(greeting_morning_start=-4, greeting_morning_end=99)
        morning, _midday, _night = cfg.greeting_windows()
        self.assertGreaterEqual(morning[0], HOUR_MIN)
        self.assertLessEqual(morning[0], HOUR_MAX)
        self.assertLessEqual(morning[1], HOUR_MAX + 1)

    def test_midday_is_a_fallback_not_an_extra(self):
        """午间是「前面两个窗口都没赶上」的兜底，不能变成一天第三顿。"""
        already_greeted = {"greet_day": "2026-08-22", "greet_kind": "morning"}
        self.assertFalse(greeting_due(already_greeted, "2026-08-22", "midday"))
        self.assertTrue(
            greeting_due({}, "2026-08-22", "midday"),
            "今天一次都没问候过时，午间正是那次兜底",
        )

    def test_same_window_is_not_repeated_the_same_day(self):
        user = {"greet_day": "2026-08-22", "greet_kind": "morning"}
        self.assertFalse(greeting_due(user, "2026-08-22", "morning"))
        self.assertTrue(greeting_due(user, "2026-08-23", "morning"))

    def test_greeting_enabled_switch(self):
        self.assertFalse(_cfg(greeting_enabled=False).greeting_enabled)


# ── 精力/社交精力阈值：真的改了 mood ─────────────────────────────


class EnergyThresholdOptionTest(unittest.TestCase):
    def test_energy_threshold_moves_the_cut(self):
        low_cut = desire.mood_multiplier(10.0, 80.0, energy_threshold=15.0, social_threshold=20.0)
        high_cut = desire.mood_multiplier(10.0, 80.0, energy_threshold=5.0, social_threshold=20.0)
        self.assertLess(low_cut, high_cut, "阈值调低后，同样的精力不该还被判成「太累」")

    def test_social_energy_threshold_moves_the_cut(self):
        strict = desire.mood_multiplier(80.0, 18.0, energy_threshold=15.0, social_threshold=20.0)
        loose = desire.mood_multiplier(80.0, 18.0, energy_threshold=15.0, social_threshold=10.0)
        self.assertLess(strict, loose)

    def test_thresholds_clamped(self):
        self.assertEqual(_cfg(energy_threshold=-10).energy_threshold, ENERGY_THRESHOLD_MIN)
        self.assertEqual(_cfg(energy_threshold=999).energy_threshold, ENERGY_THRESHOLD_MAX)
        self.assertEqual(
            _cfg(social_energy_threshold=-10).social_energy_threshold,
            SOCIAL_ENERGY_THRESHOLD_MIN,
        )
        self.assertEqual(
            _cfg(social_energy_threshold=999).social_energy_threshold,
            SOCIAL_ENERGY_THRESHOLD_MAX,
        )

    def test_thresholds_reach_settle_minds(self):
        """同一个人、同一时刻，只换阈值——攒出来的念头必须不一样。"""
        now = WEDNESDAY.timestamp()
        strict = _engine(energy_threshold=90, social_energy_threshold=90)
        loose = _engine(energy_threshold=0, social_energy_threshold=0)
        for engine in (strict, loose):
            _seed_user(engine, interest=0.9, urge_at=now - 4 * 3600)
        body = {"energy": 20.0, "social_energy": 25.0}
        strict.settle_minds("bot1", now, WEDNESDAY, bot_state=body)
        loose.settle_minds("bot1", now, WEDNESDAY, bot_state=body)
        strict_urge = strict.state.bot("bot1")["users"]["u1"]["urge"]
        loose_urge = loose.state.bot("bot1")["users"]["u1"]["urge"]
        self.assertLess(strict_urge, loose_urge, "阈值没进到 settle_minds 里")
        self.assertGreater(loose_urge, 0.0, "宽松阈值下应当攒出念头来")


class WeekendBoostOptionTest(unittest.TestCase):
    def test_default_is_on(self):
        self.assertTrue(_cfg().weekend_boost)

    def test_boost_changes_the_weekend_urge(self):
        now = SATURDAY.timestamp()
        on = _engine(weekend_boost=True)
        off = _engine(weekend_boost=False)
        for engine in (on, off):
            _seed_user(engine, interest=0.9, urge_at=now - 4 * 3600)
        on.settle_minds("bot1", now, SATURDAY)
        off.settle_minds("bot1", now, SATURDAY)
        self.assertGreater(
            on.state.bot("bot1")["users"]["u1"]["urge"],
            off.state.bot("bot1")["users"]["u1"]["urge"],
        )

    def test_no_boost_on_a_weekday(self):
        """周末加成只对周末生效：周三开与不开应当完全一样。"""
        now = WEDNESDAY.timestamp()
        on = _engine(weekend_boost=True)
        off = _engine(weekend_boost=False)
        for engine in (on, off):
            _seed_user(engine, interest=0.9, urge_at=now - 4 * 3600)
        on.settle_minds("bot1", now, WEDNESDAY)
        off.settle_minds("bot1", now, WEDNESDAY)
        self.assertAlmostEqual(
            on.state.bot("bot1")["users"]["u1"]["urge"],
            off.state.bot("bot1")["users"]["u1"]["urge"],
            places=9,
        )

    def test_the_constant_is_the_documented_one(self):
        self.assertGreater(WEEKEND_MOOD_BOOST, 1.0)


# ── 群心流四个旋钮：夹取 + 真的传给 groupflow ─────────────────────


class FlowKnobsOptionTest(unittest.TestCase):
    def test_defaults(self):
        cfg = _cfg()
        self.assertEqual(cfg.flow_min_gap_seconds, 120)
        self.assertEqual(cfg.flow_max_replies_per_window, 3)
        self.assertEqual(cfg.flow_hourly_cap, 6)
        self.assertEqual(cfg.flow_ignored_exit, 2)

    def test_clamped(self):
        self.assertEqual(_cfg(flow_min_gap_seconds=0).flow_min_gap_seconds, FLOW_MIN_GAP_MIN)
        self.assertEqual(_cfg(flow_min_gap_seconds=99999).flow_min_gap_seconds, FLOW_MIN_GAP_MAX)
        self.assertEqual(
            _cfg(flow_max_replies_per_window=0).flow_max_replies_per_window, FLOW_MAX_REPLIES_MIN
        )
        self.assertEqual(
            _cfg(flow_max_replies_per_window=999).flow_max_replies_per_window, FLOW_MAX_REPLIES_MAX
        )
        self.assertEqual(_cfg(flow_hourly_cap=0).flow_hourly_cap, FLOW_HOURLY_CAP_MIN)
        self.assertEqual(_cfg(flow_hourly_cap=999).flow_hourly_cap, FLOW_HOURLY_CAP_MAX)
        self.assertEqual(_cfg(flow_ignored_exit=0).flow_ignored_exit, FLOW_IGNORED_EXIT_MIN)
        self.assertEqual(_cfg(flow_ignored_exit=999).flow_ignored_exit, FLOW_IGNORED_EXIT_MAX)

    def test_max_replies_reaches_groupflow(self):
        now = 1_700_000_000.0
        group = {"flow_open_until": now + 600, "flow_replies": 3, "flow_last_reply_at": 0.0}
        common = dict(min_gap_seconds=1, hourly_cap=10, hour_count=0, ignored_exit=2, is_quiet=False)
        tight, _ = groupflow.flow_should_consider(group, now, max_replies=3, **common)
        loose, _ = groupflow.flow_should_consider(group, now, max_replies=4, **common)
        self.assertFalse(tight, "已经回满 3 条，上限 3 就该停")
        self.assertTrue(loose, "上限调到 4 之后同一状态应当还能接")

    def test_min_gap_reaches_groupflow(self):
        now = 1_700_000_000.0
        group = {"flow_open_until": now + 600, "flow_replies": 0, "flow_last_reply_at": now - 10}
        common = dict(max_replies=3, hourly_cap=10, hour_count=0, ignored_exit=2, is_quiet=False)
        wait, _ = groupflow.flow_should_consider(group, now, min_gap_seconds=120, **common)
        ready, _ = groupflow.flow_should_consider(group, now, min_gap_seconds=5, **common)
        self.assertFalse(wait)
        self.assertTrue(ready)

    def test_hourly_cap_reaches_groupflow(self):
        now = 1_700_000_000.0
        group = {"flow_open_until": now + 600, "flow_replies": 0, "flow_last_reply_at": 0.0}
        common = dict(max_replies=3, min_gap_seconds=1, ignored_exit=2, is_quiet=False)
        capped, _ = groupflow.flow_should_consider(group, now, hourly_cap=6, hour_count=6, **common)
        room, _ = groupflow.flow_should_consider(group, now, hourly_cap=6, hour_count=5, **common)
        self.assertFalse(capped)
        self.assertTrue(room)

    def test_ignored_exit_reaches_groupflow(self):
        """被无视几次就退出群聊场——计数存在群状态里（`flow_ignored`），不是入参。"""
        now = 1_700_000_000.0
        group = {
            "flow_open_until": now + 600,
            "flow_replies": 0,
            "flow_last_reply_at": 0.0,
            "flow_ignored": 2,
        }
        common = dict(max_replies=3, min_gap_seconds=1, hourly_cap=10, is_quiet=False)
        out, _why = groupflow.flow_should_consider(
            group, now, ignored_exit=2, hour_count=0, **common
        )
        alive, _why = groupflow.flow_should_consider(
            group, now, ignored_exit=3, hour_count=0, **common
        )
        self.assertFalse(out, "已经被无视 2 次、阈值就是 2，该退出")
        self.assertTrue(alive, "阈值抬到 3 之后同一状态应当还留着")


# ── 群引用库两个旋钮 ─────────────────────────────────────────────


class GroupRefKnobsOptionTest(unittest.TestCase):
    def test_defaults(self):
        cfg = _cfg()
        self.assertEqual(cfg.group_ref_sample_size, 30)
        self.assertEqual(cfg.group_ref_prompt_count, 8)
        self.assertTrue(cfg.group_ref_lib_enabled)

    def test_sample_size_clamped(self):
        self.assertEqual(_cfg(group_ref_sample_size=0).group_ref_sample_size, GROUP_REF_SAMPLE_MIN)
        self.assertEqual(
            _cfg(group_ref_sample_size=99999).group_ref_sample_size, GROUP_REF_SAMPLE_MAX
        )

    def test_prompt_count_clamped(self):
        self.assertEqual(_cfg(group_ref_prompt_count=-3).group_ref_prompt_count, GROUP_REF_PROMPT_MIN)
        self.assertEqual(
            _cfg(group_ref_prompt_count=99999).group_ref_prompt_count, GROUP_REF_PROMPT_MAX
        )

    def test_switches(self):
        self.assertFalse(_cfg(group_ref_lib_enabled=False).group_ref_lib_enabled)
        self.assertFalse(_cfg(group_store_message_text=False).group_store_message_text)


# ── 话题与上下文条数 ─────────────────────────────────────────────


class TopicAndContextOptionTest(unittest.TestCase):
    def test_defaults(self):
        cfg = _cfg()
        self.assertEqual(cfg.topic_memory_count, 5)
        self.assertEqual(cfg.context_inject_count, 10)

    def test_topic_memory_clamped(self):
        self.assertEqual(_cfg(topic_memory_count=-1).topic_memory_count, TOPIC_MEMORY_MIN)
        self.assertEqual(_cfg(topic_memory_count=999).topic_memory_count, TOPIC_MEMORY_MAX)

    def test_context_inject_clamped(self):
        self.assertEqual(_cfg(context_inject_count=-1).context_inject_count, CONTEXT_INJECT_MIN)
        self.assertEqual(_cfg(context_inject_count=999).context_inject_count, CONTEXT_INJECT_MAX)

    def test_memory_bridge_switch(self):
        self.assertTrue(_cfg().memory_bridge_enabled)
        self.assertFalse(_cfg(memory_bridge_enabled=False).memory_bridge_enabled)


# ── 用户清理 ─────────────────────────────────────────────────────


class UserRetentionOptionTest(unittest.TestCase):
    def test_default_and_clamp(self):
        self.assertEqual(_cfg().user_retention_days, 30)
        self.assertEqual(_cfg(user_retention_days=-1).user_retention_days, USER_RETENTION_MIN)
        self.assertEqual(_cfg(user_retention_days=99999).user_retention_days, USER_RETENTION_MAX)

    def _state(self):
        tmp = tempfile.mkdtemp()
        return SocialState(str(Path(tmp) / "state.json"))

    def test_zero_means_never_prune(self):
        state = self._state()
        bot = state.bot("bot1")
        bot.setdefault("users", {})["u1"] = {
            "last_seen": 1.0,
            "urge": 0.0,
            "conversation": [{"text": "旧话"}],
        }
        self.assertEqual(state.prune_expired(0, now=1_800_000_000.0), 0)
        self.assertEqual(state.bot("bot1")["users"]["u1"]["conversation"], [{"text": "旧话"}])

    def test_window_actually_clears_the_stale_one(self):
        """清理是**抹掉历史正文**，不是删掉用户条目（条目要留着，免得再聊时像陌生人）。"""
        state = self._state()
        now = 1_800_000_000.0
        users = state.bot("bot1").setdefault("users", {})
        users["old"] = {
            "last_seen": now - 40 * 86400,
            "urge": 0.0,
            "conversation": [{"text": "很久以前聊的"}],
            "topics": ["旧话题"],
        }
        users["fresh"] = {
            "last_seen": now - 2 * 86400,
            "urge": 0.0,
            "conversation": [{"text": "前两天聊的"}],
            "topics": ["新话题"],
        }
        self.assertEqual(state.prune_expired(30, now=now), 1)
        self.assertEqual(state.bot("bot1")["users"]["old"]["conversation"], [])
        self.assertEqual(state.bot("bot1")["users"]["old"]["topics"], [])
        self.assertEqual(
            state.bot("bot1")["users"]["fresh"]["conversation"],
            [{"text": "前两天聊的"}],
            "窗口内的人不该被动",
        )


# ── 播种平台 ─────────────────────────────────────────────────────


class SeedPlatformOptionTest(unittest.TestCase):
    def test_default_platform(self):
        self.assertEqual(_cfg().seed_platform, "aiocqhttp")

    def test_blank_platform_falls_back(self):
        self.assertEqual(_cfg(seed_platform="   ").seed_platform, "aiocqhttp")
        self.assertEqual(_cfg(seed_platform="").seed_platform, "aiocqhttp")

    def test_custom_platform_is_kept(self):
        self.assertEqual(_cfg(seed_platform="telegram").seed_platform, "telegram")

    def test_platform_is_used_as_the_seed_default(self):
        self.assertEqual(
            normalize_seed_entries(["12345"], "telegram"),
            [("telegram", "12345", "private")],
        )

    def test_explicit_platform_in_the_entry_wins(self):
        self.assertEqual(
            normalize_seed_entries(["aiocqhttp:12345"], "telegram"),
            [("aiocqhttp", "12345", "private")],
        )


# ── 行为开关：只证明它们读得出来且默认正确 ───────────────────────


class BehaviourSwitchOptionTest(unittest.TestCase):
    def test_defaults(self):
        cfg = _cfg()
        self.assertFalse(cfg.respect_user_rhythm)
        self.assertTrue(cfg.use_core_clock)
        self.assertTrue(cfg.track_own_replies)
        self.assertTrue(cfg.adaptive_reply_rate)

    def test_switches_survive(self):
        self.assertTrue(_cfg(respect_user_rhythm=True).respect_user_rhythm)
        self.assertFalse(_cfg(use_core_clock=False).use_core_clock)
        self.assertFalse(_cfg(track_own_replies=False).track_own_replies)
        self.assertFalse(_cfg(adaptive_reply_rate=False).adaptive_reply_rate)

    def test_miss_daily_cap_clamped(self):
        self.assertEqual(_cfg(miss_daily_cap=-1).miss_daily_cap, 0)
        self.assertEqual(_cfg(miss_daily_cap=999).miss_daily_cap, 10)
        # v1.28.6：2 → 3（用户反馈「私聊发送没看到几个」）
        self.assertEqual(_cfg().miss_daily_cap, 3)

    def test_miss_affection_min_clamped(self):
        self.assertEqual(_cfg(miss_affection_min=-1).miss_affection_min, 0)
        self.assertEqual(_cfg(miss_affection_min=999).miss_affection_min, 100)
        self.assertEqual(_cfg().miss_affection_min, 42)

    def test_reply_window_clamped(self):
        self.assertEqual(_cfg(reply_window_hours=0).reply_window_hours, REPLY_WINDOW_MIN)
        self.assertEqual(_cfg(reply_window_hours=999).reply_window_hours, REPLY_WINDOW_MAX)
        self.assertEqual(_cfg().reply_window_hours, 2)


class PendingWindowOptionTest(unittest.TestCase):
    """`reply_window_hours` 只管统计窗口，判「没接话」时另有半天兜底。"""

    def test_small_window_is_lifted_to_the_floor(self):
        engine = _engine(reply_window_hours=REPLY_WINDOW_MIN)
        self.assertEqual(engine._pending_window(), PENDING_IGNORE_MIN_HOURS * 3600)

    def test_large_window_is_honoured(self):
        engine = _engine(reply_window_hours=48)
        self.assertEqual(engine._pending_window(), 48 * 3600)


# ── 分段：allow_burst 真的决定合并还是拆开 ───────────────────────


class AllowBurstOptionTest(unittest.TestCase):
    class _Ctx:
        def __init__(self, enable: bool):
            self._enable = enable

        def get_config(self, _umo):
            return {"platform_settings": {"segmented_reply": {"enable": self._enable}}}

    def _parts(self):
        return pacing.plan_parts("第一句。第二句。第三句。", "umo:1", None, allow_burst=True)[0]

    def test_burst_on_splits(self):
        ctx = self._Ctx(enable=True)
        parts, _delays = pacing.plan_segments(self._parts(), "umo:1", ctx, allow_burst=True)
        self.assertGreater(len(parts), 1)

    def test_burst_off_merges_into_one(self):
        ctx = self._Ctx(enable=True)
        parts, delays = pacing.plan_segments(self._parts(), "umo:1", ctx, allow_burst=False)
        self.assertEqual(len(parts), 1)
        self.assertEqual(delays, [])
        self.assertIn("第一句", parts[0])
        self.assertIn("第三句", parts[0], "合并不能丢内容")

    def test_platform_switch_off_merges_even_with_allow_burst(self):
        ctx = self._Ctx(enable=False)
        parts, _delays = pacing.plan_segments(self._parts(), "umo:1", ctx, allow_burst=True)
        self.assertEqual(len(parts), 1, "平台没开分段回复，插件不能自己拆")


# ── 文档口径：README 说的和 schema 说的必须一致 ──────────────────


class ReadmeMatchesSchemaTest(unittest.TestCase):
    def test_every_documented_default_is_the_schema_default(self):
        cfg = _cfg()
        drift = []
        for key, spec in SCHEMA.items():
            if not isinstance(spec, dict) or "default" not in spec:
                continue
            expected = spec["default"]
            if expected is None:
                continue
            got = getattr(cfg, key, "<missing>")
            if got != expected:
                drift.append((key, expected, got))
        self.assertEqual(drift, [], f"默认值与 schema 不一致：{drift}")

    @staticmethod
    def _readme_table() -> dict:
        """README「## 全部配置项」那张表：`key` → 表里写的默认值文本。

        表头写着「下表与 `_conf_schema.json` 同源，面板里看到的默认值与这里一致」，
        所以这两条不变量是 README 自己承诺的，不是我们额外加的规矩。
        """
        text = README_PATH.read_text(encoding="utf-8")
        start = text.index("## 全部配置项")
        end = text.find("\n## ", start + 5)
        table = text[start:] if end < 0 else text[start:end]
        out: dict = {}
        for m in re.finditer(
            r"^\|\s*`([a-z0-9_]+)`\s*\|\s*`?([^|`]*)`?\s*\|", table, re.M
        ):
            out[m.group(1)] = m.group(2).strip()
        return out

    def test_readme_table_lists_every_schema_key(self):
        """一项都不能漏：漏掉的那项用户根本不知道它存在，只能看面板猜。"""
        doc = self._readme_table()
        self.assertIn("enabled", doc, "README 的配置表没解析出来，先检查表头格式")
        missing = sorted(k for k in SCHEMA if k not in doc)
        self.assertEqual(missing, [], f"README 的配置表漏了这些项：{missing}")

    @staticmethod
    def _norm(value) -> str:
        """把两边都归一成同一个口径再比。

        两处差异是**写法**不是**值**，不抹平就是假阳性：
        - list 型键 schema 写 `None`（「没配」），README 写 `[]`（「空列表」）；
        - schema 是 JSON 数字 `120.0`，README 是人读的 `120`。
        """
        if value is None:
            return "[]"
        if isinstance(value, bool):
            return "true" if value else "false"
        text = str(value).strip()
        if text in ("", "None", "null"):
            return "[]"
        try:
            number = float(text)
        except ValueError:
            return text
        return str(int(number)) if number.is_integer() else text

    def test_readme_defaults_match_the_schema(self):
        """表里写的默认值必须等于 schema 的默认值——那是用户唯一能对照的一份。"""
        doc = self._readme_table()
        drift = []
        for key, spec in SCHEMA.items():
            if key not in doc or not isinstance(spec, dict) or "default" not in spec:
                continue
            want = self._norm(spec["default"])
            if want != self._norm(doc[key]):
                drift.append((key, want, doc[key]))
        self.assertEqual(drift, [], f"README 写的默认值与 schema 不一致：{drift}")


if __name__ == "__main__":
    unittest.main()
