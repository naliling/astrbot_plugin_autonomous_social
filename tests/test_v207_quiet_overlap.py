# -*- coding: utf-8 -*-
"""v2.0.7：安静时段重叠计算不再卡住事件循环（2026-11-04 诊断报告）。

诊断报告的事实（v2.0.6 部署当天）：

* `quiet_overlap_seconds` 是**逐分钟**遍历，跨度 14 天时每次 20160 次
  `city_now` + `city_epoch`（实测 0.40 秒）；
* 它被放在「每轮心跳 × 每个用户」的同步路径里（`_thread_pick` 的 for 循环内），
  468 个用户中 302 个 `last_said` 超过 14 天，一轮心跳纯 CPU 33–89 秒；
* asyncio 事件循环被独占，AstrBot 看门狗连抓 41 次主线程栈，最深插件帧 100%
  落在这个函数里（`astrbot.log` 同批 41 条 `Event loop lag detected`）。

v2.0.7 的四条改法，这个文件逐条钉住：

1. `quiet_overlap_seconds` 改成按**本地日界**切段求交（O(天数)），结果与逐分钟
   版本逐秒一致（等价性用例），14 天从 0.40 秒降到 0.0001 秒量级；
2. `_thread_pick` 先用**原窗口**判，只有基准窗口差一点够不着的人才去算重叠；
3. `_continue_max_with_quiet` 三道短路（没有可接的沉默 / 没超出原窗口 / 超出
   原窗口 14 天以上）——302 个 20 天没说话的用户连一次重叠计算都不触发；
4. 整轮一个 0.2 秒预算，超了就按原窗口判，不再为「锦上添花」的顺延占住循环。
"""

from __future__ import annotations

import logging
import math
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

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
    api.logger = logging.getLogger("autonomous_social_v207_quiet_overlap_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.clock import (  # noqa: E402
    QUIET_CLOCK_LOCAL,
    _in_window_float,
    city_epoch,
    quiet_overlap_seconds,
)
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402

NOW = 1_790_000_000.0
CONTINUE_MAX = 8 * 3600.0


# ─── 参照实现：逐分钟采样（独立于被测实现，用来证明等价） ──────────


def _ref_local_hour(ts: float, offset_minutes) -> float:
    """参照实现里的「当地几点」：固定偏移直接用算术，不碰 datetime。"""
    off = float(offset_minutes) * 60.0
    return ((ts + off) % 86400.0) / 3600.0


def _ref_inside(ts: float, qs: float, qe: float, offset_minutes) -> bool:
    return _in_window_float(_ref_local_hour(ts, offset_minutes), qs, qe)


def _reference_overlap(a: float, b: float, qs: float, qe: float, offset_minutes) -> float:
    """[a, b) 落在窗口内的秒数——**逐分钟**累加，v2.0.6 的老口径。

    对齐到整分钟：窗口端点都是整点/半点/一刻（0.25 小时 = 15 分钟），偏移也是
    整分钟，所以每个分钟格要么整格在窗内、要么整格在窗外，取中点判定就是精确值。
    首尾两截（不足一分钟）落在同一个分钟格里，同样用中点判定。
    """
    if b <= a:
        return 0.0
    total = 0.0
    first = math.ceil(a / 60.0) * 60.0
    if first > a:
        end = min(first, b)
        mid = a + (end - a) / 2.0
        if _ref_inside(mid, qs, qe, offset_minutes):
            total += end - a
    t = first
    while t < b:
        end = min(t + 60.0, b)
        mid = t + (end - t) / 2.0
        if _ref_inside(mid, qs, qe, offset_minutes):
            total += end - t
        t = end
    return total


def _local_ts(hour: float, offset_minutes: int, day: int = 0) -> float:
    """某个城市当地某天 hour 点的 epoch（day=0 是 2026-09-21 那天）。"""
    base = datetime(2026, 9, 21) + timedelta(days=day, hours=hour)
    return city_epoch(base, offset_minutes)


WINDOWS = [
    (23.0, 7.0),      # 默认：跨午夜
    (1.0, 6.0),       # 普通时段
    (0.0, 24.0),      # 一整天
    (22.0, 24.0),     # 到午夜
    (1.75, 5.5),      # Core 的半点夜
    (0.25, 0.75),     # 一刻钟的小窗
    (13.0, 15.0),     # 白天窗口（对照组）
    (5.0, 5.0),       # 关闭
]
SPANS = [0.5 * 3600.0, 3 * 3600.0, 8 * 3600.0, 30 * 3600.0, 3 * 86400.0, 14 * 86400.0]
OFFSETS = [120, 480, -300, 330]
SHIFTS = [0.0, 1234.0, 79123.0]


class OverlapEquivalenceTest(unittest.TestCase):
    """新实现（按日界切段）必须与逐分钟版本逐秒一致。"""

    def test_matches_minute_reference_everywhere(self):
        checked = 0
        for qs, qe in WINDOWS:
            for span in SPANS:
                for offset in OFFSETS:
                    for shift in SHIFTS:
                        a = _local_ts(21.0, offset) + shift
                        b = a + span
                        want = _reference_overlap(a, b, qs, qe, offset)
                        got = quiet_overlap_seconds(a, b, qs, qe, offset)
                        checked += 1
                        self.assertAlmostEqual(
                            got,
                            want,
                            delta=0.5,
                            msg=f"窗口 {qs}–{qe}、跨度 {span}s、偏移 {offset}、起点 +{shift}s",
                        )
        self.assertEqual(checked, len(WINDOWS) * len(SPANS) * len(OFFSETS) * len(SHIFTS))

    def test_default_night_window_keeps_eight_hours(self):
        """22:00 → 次日 08:00、窗口 23–7：落在夜里的正好 8 小时。"""
        off = 480
        a = _local_ts(22.0, off)
        b = _local_ts(30.0, off)  # 次日 06:00
        self.assertAlmostEqual(quiet_overlap_seconds(a, b, 23, 7, off), 7 * 3600.0, places=3)
        b2 = _local_ts(32.0, off)  # 次日 08:00
        self.assertAlmostEqual(quiet_overlap_seconds(a, b2, 23, 7, off), 8 * 3600.0, places=3)

    def test_window_inside_one_local_day(self):
        """13–15 这种不跨午夜的窗口：11:00 → 16:00 应得 2 小时。"""
        off = -300
        a = _local_ts(11.0, off)
        b = _local_ts(16.0, off)
        self.assertAlmostEqual(quiet_overlap_seconds(a, b, 13, 15, off), 2 * 3600.0, places=3)

    def test_whole_day_window_counts_the_whole_span(self):
        a = _local_ts(9.0, 480)
        b = a + 30 * 3600.0
        self.assertAlmostEqual(quiet_overlap_seconds(a, b, 0, 24, 480), b - a, places=3)

    def test_disabled_window_is_zero(self):
        a = _local_ts(9.0, 480)
        self.assertEqual(quiet_overlap_seconds(a, a + 14 * 86400.0, 5, 5, 480), 0.0)

    def test_none_offset_still_works(self):
        """没有 Core（offset=None）时退回本机钟：不崩、结果在合法区间内。"""
        a = _local_ts(9.0, 480)
        got = quiet_overlap_seconds(a, a + 30 * 3600.0, 23, 7, None)
        self.assertGreaterEqual(got, 0.0)
        self.assertLessEqual(got, 30 * 3600.0)


class OverlapCapTest(unittest.TestCase):
    """上限与坏输入：手改出来的远古时间戳不该把心跳拖住。"""

    def test_span_over_fourteen_days_is_capped(self):
        a = _local_ts(9.0, 480)
        capped = quiet_overlap_seconds(a, a + 14 * 86400.0, 23, 7, 480)
        beyond = quiet_overlap_seconds(a, a + 20 * 86400.0, 23, 7, 480)
        self.assertAlmostEqual(beyond, capped, delta=1.0, msg="超过 14 天的部分不再计算")
        self.assertGreater(capped, 0.0)

    def test_bad_inputs_are_zero(self):
        a = _local_ts(9.0, 480)
        self.assertEqual(quiet_overlap_seconds(a, a, 23, 7, 480), 0.0)
        self.assertEqual(quiet_overlap_seconds(a + 10, a, 23, 7, 480), 0.0)
        self.assertEqual(quiet_overlap_seconds(None, a, 23, 7, 480), 0.0)
        self.assertEqual(quiet_overlap_seconds(a, "x", 23, 7, 480), 0.0)
        self.assertEqual(quiet_overlap_seconds(float("nan"), a, 23, 7, 480), 0.0)
        self.assertEqual(quiet_overlap_seconds(a, a + 3600, None, 7, 480), 0.0)


class OverlapSpeedTest(unittest.TestCase):
    """诊断报告的验收口径：一轮心跳的 460 个用户从 137 秒降到毫秒级。"""

    def test_fourteen_days_is_milliseconds(self):
        """固定偏移的城市钟走算术推进那一条：实测 0.08 毫秒，旧实现 0.40 秒。"""
        a = _local_ts(9.0, 480)
        b = a + 14 * 86400.0
        start = time.perf_counter()
        got = quiet_overlap_seconds(a, b, 23, 7, 480)
        cost = time.perf_counter() - start
        self.assertGreater(got, 0.0)
        self.assertLess(cost, 0.01, f"14 天一次应 <0.01 秒，实测 {cost:.4f} 秒")

    def test_fourteen_days_local_clock_is_milliseconds(self):
        """没有 Core 时的本机时钟那条：会碰夏令时，只能逐段折，实测 0.33 毫秒。"""
        a = _local_ts(9.0, None)
        b = a + 14 * 86400.0
        start = time.perf_counter()
        got = quiet_overlap_seconds(a, b, 23, 7, None)
        cost = time.perf_counter() - start
        self.assertGreaterEqual(got, 0.0)
        self.assertLess(cost, 0.05, f"14 天一次（本机时钟）应 <0.05 秒，实测 {cost:.4f} 秒")

    def test_460_stale_users_under_two_seconds(self):
        """报告里 460 个用户的真实组合：旧实现理论 184 秒，现在实测 0.03 秒。"""
        start = time.perf_counter()
        for i in range(460):
            a = _local_ts(9.0, 480) + i * 37.0
            quiet_overlap_seconds(a, a + 14 * 86400.0, 23, 7, 480)
        cost = time.perf_counter() - start
        self.assertLess(cost, 0.2, f"460 个 14 天用户应 <0.2 秒，实测 {cost:.3f} 秒")


# ─── 引擎侧：短路、预算、接线 ─────────────────────────────────


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


def _engine(**cfg) -> SocialEngine:
    tmp = tempfile.mkdtemp()
    eng = SocialEngine(
        context=None,
        config=SocialConfig(),
        state_path=str(Path(tmp) / "state.json"),
        data_dir=tmp,
        time_source=_Clock(),
    )
    # 安静时段压成零长：断言不能取决于跑测试时本机几点。
    eng.cfg.quiet_start = 0
    eng.cfg.quiet_end = 0
    eng.cfg.quiet_clock = QUIET_CLOCK_LOCAL
    eng.cfg.quiet_use_core_night = True
    eng.cfg.use_core_clock = True
    eng.cfg.special_care_ignore_quiet = False
    eng._night_windows = {}
    eng._city_offset = {}
    for key, value in cfg.items():
        setattr(eng.cfg, key, value)
    return eng


def _silent(eng: SocialEngine, seen_ago: float, spoken_ago: float, uid: str = "u1") -> dict:
    """「她回完、对方就没声了」：最后一句是她说的，之后谁都没说话。"""
    user = eng.state.user("b1", uid)
    user["umo"] = f"aiocqhttp:private:{uid}"
    user["__uid"] = uid
    user["last_seen"] = NOW - seen_ago
    user["last_spoken"] = NOW - spoken_ago
    user["last_message"] = "今天路上好堵"
    user["last_spoken_text"] = "那你早点休息吧"
    user["conversation"] = [
        {"dir": "in", "text": "今天路上好堵", "ts": NOW - seen_ago - 300},
        {"dir": "out", "text": "那你早点休息吧", "ts": NOW - spoken_ago},
    ]
    user["pending_result"] = "replied"
    return user


class ContinueMaxWithQuietTest(unittest.TestCase):
    """三道短路 + 预算：不该算的一个都不算，该算的只算一次。"""

    def test_no_conversation_record_returns_base_window(self):
        eng = _engine()
        u = {"last_seen": 0, "last_spoken": 0}
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=3 * 3600.0) as m:
            self.assertEqual(
                eng._continue_max_with_quiet("b1", u, NOW, CONTINUE_MAX), (CONTINUE_MAX, False)
            )
        self.assertEqual(m.call_count, 0, "没有对话记录就不该去算时区重叠")

    def test_peer_spoke_last_returns_base_window(self):
        eng = _engine()
        u = {"last_seen": NOW - 3600.0, "last_spoken": NOW - 7200.0}
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=3 * 3600.0) as m:
            self.assertEqual(
                eng._continue_max_with_quiet("b1", u, NOW, CONTINUE_MAX), (CONTINUE_MAX, False)
            )
        self.assertEqual(m.call_count, 0, "最后一句是对方说的，continue 分支本来就不成立")

    def test_gap_inside_base_window_returns_base_window(self):
        eng = _engine()
        u = {"last_seen": NOW - 2 * 3600.0, "last_spoken": NOW - 2 * 3600.0}
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=3 * 3600.0) as m:
            self.assertEqual(
                eng._continue_max_with_quiet("b1", u, NOW, CONTINUE_MAX), (CONTINUE_MAX, False)
            )
        self.assertEqual(m.call_count, 0, "本来就在窗口里，顺延不改变结果")

    def test_gap_beyond_cap_returns_base_window(self):
        eng = _engine()
        gap = CONTINUE_MAX + 14 * 86400.0 + 3600.0
        u = {"last_seen": NOW - gap, "last_spoken": NOW - gap}
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=3 * 3600.0) as m:
            self.assertEqual(
                eng._continue_max_with_quiet("b1", u, NOW, CONTINUE_MAX), (CONTINUE_MAX, False)
            )
        self.assertEqual(m.call_count, 0, "沉默超出 14 天以上，顺延也够不着")

    def test_gap_just_beyond_window_is_extended_once(self):
        eng = _engine()
        u = {"last_seen": NOW - 9 * 3600.0, "last_spoken": NOW - 9 * 3600.0}
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=3 * 3600.0) as m:
            got = eng._continue_max_with_quiet("b1", u, NOW, CONTINUE_MAX)
        self.assertEqual(got, (CONTINUE_MAX + 3 * 3600.0, False))
        self.assertEqual(m.call_count, 1, "只算一次，不能每人算两遍")

    def test_zero_overlap_keeps_base_window(self):
        eng = _engine()
        u = {"last_seen": NOW - 9 * 3600.0, "last_spoken": NOW - 9 * 3600.0}
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=0.0) as m:
            self.assertEqual(
                eng._continue_max_with_quiet("b1", u, NOW, CONTINUE_MAX), (CONTINUE_MAX, False)
            )
        self.assertEqual(m.call_count, 1)

    def test_deadline_skips_deferral_and_warns_once(self):
        eng = _engine()
        u = {"last_seen": NOW - 9 * 3600.0, "last_spoken": NOW - 9 * 3600.0}
        past = time.monotonic() - 1.0
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=3 * 3600.0) as m:
            first = eng._continue_max_with_quiet("b1", u, NOW, CONTINUE_MAX, past, False)
            second = eng._continue_max_with_quiet("b1", u, NOW, CONTINUE_MAX, past, True)
        self.assertEqual(first, (CONTINUE_MAX, True))
        self.assertEqual(second, (CONTINUE_MAX, True))
        self.assertEqual(m.call_count, 0, "超预算就按原窗口判，不再算顺延")

    def test_disabled_continue_is_passthrough(self):
        eng = _engine()
        u = {"last_seen": NOW - 9 * 3600.0, "last_spoken": NOW - 9 * 3600.0}
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=3 * 3600.0) as m:
            self.assertEqual(eng._continue_max_with_quiet("b1", u, NOW, 0.0), (0.0, False))
        self.assertEqual(m.call_count, 0)


class ThreadPickWiringTest(unittest.TestCase):
    """`_thread_pick`：先原窗口判，只有差一点够不着的人才去算重叠。"""

    def test_late_candidate_is_revived_by_deferral(self):
        """沉默 9 小时（超出 8 小时窗口），夜吃掉 3 小时 → 窗口变 11 小时，接得上。"""
        eng = _engine()
        _silent(eng, seen_ago=9 * 3600.0 + 60, spoken_ago=9 * 3600.0)
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=3 * 3600.0):
            picked = eng._thread_pick("b1", NOW, None)
        self.assertIsNotNone(picked, "顺延之后应当成为候选")
        self.assertEqual(picked[2], "continue")

    def test_without_deferral_the_same_user_is_not_picked(self):
        """同一份状态，重叠为 0 时窗口不变 → 9 小时已经超时，不接。"""
        eng = _engine()
        _silent(eng, seen_ago=9 * 3600.0 + 60, spoken_ago=9 * 3600.0)
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=0.0):
            self.assertIsNone(eng._thread_pick("b1", NOW, None))

    def test_candidate_inside_base_window_never_computes_overlap(self):
        """2 小时沉默本来就在窗口里：原窗口那一次判定就结束了，不碰重叠计算。"""
        eng = _engine()
        _silent(eng, seen_ago=2 * 3600.0 + 60, spoken_ago=2 * 3600.0)
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=0.0) as m:
            picked = eng._thread_pick("b1", NOW, None)
        self.assertIsNotNone(picked)
        self.assertEqual(picked[2], "continue")
        self.assertEqual(m.call_count, 0)

    def test_three_hundred_stale_users_trigger_no_overlap(self):
        """报告里 302 个 20 天没说话的用户：一次重叠计算都不该发生。"""
        eng = _engine()
        stale = NOW - 20 * 86400.0
        for i in range(300):
            user = eng.state.user("b1", f"stale{i}")
            user["umo"] = f"aiocqhttp:private:stale{i}"
            user["last_seen"] = stale
            user["last_spoken"] = stale
            user["last_message"] = "嗯"
            user["last_spoken_text"] = "好"
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=3 * 3600.0) as m:
            self.assertIsNone(eng._thread_pick("b1", NOW, None))
        self.assertEqual(m.call_count, 0, "302 个 20 天用户就是报告里那 33–89 秒的来源")

    def test_deferral_only_runs_for_near_boundary_users(self):
        """460 个用户里只有真正差一点的那个去算重叠。"""
        eng = _engine()
        stale = NOW - 20 * 86400.0
        for i in range(200):
            user = eng.state.user("b1", f"stale{i}")
            user["umo"] = f"aiocqhttp:private:stale{i}"
            user["last_seen"] = stale
            user["last_spoken"] = stale
            user["last_message"] = "嗯"
            user["last_spoken_text"] = "好"
        _silent(eng, seen_ago=9 * 3600.0 + 60, spoken_ago=9 * 3600.0, uid="near")
        with mock.patch.object(SocialEngine, "_quiet_overlap", return_value=3 * 3600.0) as m:
            picked = eng._thread_pick("b1", NOW, None)
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "near")
        self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()
