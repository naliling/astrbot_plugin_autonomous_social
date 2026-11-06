# -*- coding: utf-8 -*-
"""v2.0.6（issue #3）：安静时段是整轮的硬闸，而且判在**填配置的那个人的钟**上。

issue #3 的现象：面板写着 23:00–07:00，04:06 照样发出去了一条 continue_topic。
两个原因都在这里钉死：

1. 判定拿的是「她所在城市的几点」。主人按**本机作息**填 23 点，她在 UTC+2、宿主在
   UTC+8，本机 04:06 她那边才 22:06 —— 闸门算出「不是夜里」，整道闸对真人形同不存在。
2. 唯一的发信口（_speak_inner / _send_with_retry）上根本没有这道闸：只有「挑人」
   那一步查过，挑完到发出去之间还有一大段路可以绕开。

另外两条一起改的口径：

* Core 契约里的生物钟夜（night_window）与面板窗口**取并集**，不再顶替。旧口径下
  Core 报 00:15–05:30，面板的 23–7 就被整条抹掉，23:30 反而「不安静」。
* 窗口端点允许 24（quiet_start=0, quiet_end=24 = 一整天），也允许 start==end
  （= 关闭）。旧代码把 24 当越界丢掉，于是「配置说安静、闸门说不安静」两套答案。

这个文件覆盖：clock 的纯函数判定、两条来源的并集、end=24、发信口与手动触发两道闸、
「没有真实来回就不接话」。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import tempfile
import types
import unittest
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
    api.logger = logging.getLogger("autonomous_social_v206_quiet_gate_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import clock, threads  # noqa: E402
from social.clock import (  # noqa: E402
    QUIET_CLOCK_CORE,
    QUIET_CLOCK_LOCAL,
    in_window,
    normalize_quiet_clock,
    quiet_decision,
    quiet_overlap_seconds,
    seconds_until_quiet_end,
    window_span_hours,
)
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402

NOW = 1_800_000_000.0

BASE_KW = {
    "probe_after_seconds": 12 * 60.0,
    "presence_after_seconds": 30 * 60.0,
    "max_seconds": 70 * 60.0,
    "context_seconds": 70 * 60.0,
    "presence_floor_seconds": 30 * 60.0,
}
CONTINUE_KW = {
    "continue_after_seconds": 90 * 60.0,
    "continue_max_seconds": 8 * 3600.0,
}


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


def _ckw(**over):
    kw = dict(BASE_KW)
    kw.update(CONTINUE_KW)
    kw.update(over)
    return kw


def _u(**over) -> dict:
    """「她回完、对方就没声了」的基准状态（两小时前）。"""
    u = {
        "last_seen": NOW - 2 * 3600.0 - 60,
        "last_spoken": NOW - 2 * 3600.0,
        "last_message": "今天路上好堵",
        "last_spoken_text": "那你早点休息吧",
        "conversation": [
            {"dir": "in", "text": "今天路上好堵", "ts": NOW - 2 * 3600.0 - 600},
            {"dir": "out", "text": "那你早点休息吧", "ts": NOW - 2 * 3600.0},
        ],
        "pending_result": "replied",
    }
    u.update(over)
    return u


def _engine(**cfg) -> SocialEngine:
    tmp = tempfile.mkdtemp()
    eng = SocialEngine(
        context=None,
        config=SocialConfig(),
        state_path=str(Path(tmp) / "state.json"),
        data_dir=tmp,
        time_source=_Clock(),
    )
    # 默认把两道窗口都关掉：断言不能取决于跑测试时本机几点。
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


# ─── 1. 纯函数：这一刻到底算不算安静 ────────────────────────────


class QuietDecisionTest(unittest.TestCase):
    """issue #3 的那一分钟，以及「两条来源取并集」这件事。"""

    def test_issue_3_local_four_in_the_morning_is_quiet(self):
        """本机 04:06、她在 22 点：按操作者的作息，这就是夜里。"""
        is_quiet, why = quiet_decision(4, 22, 23, 7)
        self.assertTrue(is_quiet, "issue #3：本机 04 点必须算安静时段")
        self.assertIn("本机 04 点", why)
        self.assertIn("23:00", why)

    def test_clock_mode_core_is_the_old_broken_behaviour(self):
        """quiet_clock=core 就是 issue #3 的老口径：本机 04 点、她那边 22 点 → 放行。

        这个模式留着手动可选（有的主人就住在她那座城市），但不能是默认值。
        """
        self.assertFalse(quiet_decision(4, 22, 23, 7, clock_mode=QUIET_CLOCK_CORE)[0])
        self.assertTrue(quiet_decision(4, 1, 23, 7, clock_mode=QUIET_CLOCK_CORE)[0])

    def test_panel_window_covers_the_edges_of_night(self):
        self.assertTrue(quiet_decision(23, 23, 23, 7)[0])
        self.assertTrue(quiet_decision(6, 6, 23, 7)[0])
        self.assertFalse(quiet_decision(7, 7, 23, 7)[0])
        self.assertFalse(quiet_decision(22, 22, 23, 7)[0])

    def test_her_night_and_the_panel_window_are_a_union(self):
        """面板 13–15、Core 夜 22–6：两个窗口各管各的，谁也不抹掉谁。"""
        self.assertTrue(quiet_decision(14, 14, 13, 15, core_window=(22, 6))[0])
        self.assertTrue(quiet_decision(23, 23, 13, 15, core_window=(22, 6))[0])
        self.assertTrue(quiet_decision(3, 3, 13, 15, core_window=(22, 6))[0])
        self.assertFalse(quiet_decision(12, 12, 13, 15, core_window=(22, 6))[0])

    def test_a_nap_window_does_not_replace_the_panel_window(self):
        """v2.0.5 的口径是 Core 顶替面板：Core 报 16:45–18:15 的小睡，23 点就不安静了。"""
        is_quiet, why = quiet_decision(23, 17, 23, 7, core_window=(16.75, 18.25))
        self.assertTrue(is_quiet, "Core 报了别的窗口，也不能把面板的 23–7 抹掉")
        self.assertIn("安静时段", why)

    def test_a_degenerate_core_window_is_ignored_not_fatal(self):
        """Core 报 5–5（读坏了）：当作没有这条来源，不能凭空造出安静时段。"""
        self.assertFalse(quiet_decision(3, 5, 0, 0, core_window=(5, 5))[0])
        # 面板窗口照旧生效，跟这条坏数据无关
        self.assertTrue(quiet_decision(3, 5, 23, 7, core_window=(5, 5))[0])
        self.assertTrue(quiet_decision(3, 5, 23, 7, core_window=None)[0])

    def test_core_night_can_be_switched_off(self):
        self.assertTrue(quiet_decision(2, 2, 0, 0, core_window=(1, 5))[0])
        self.assertFalse(
            quiet_decision(2, 2, 0, 0, core_window=(1, 5), use_core_night=False)[0]
        )

    def test_same_start_and_end_disables_quiet_hours(self):
        """想夜里测效果就把两个值填成一样——文档里承诺过这条。"""
        self.assertFalse(quiet_decision(3, 3, 5, 5)[0])
        self.assertFalse(in_window(3, 5, 5))

    def test_end_of_day_is_allowed(self):
        """quiet_start=0, quiet_end=24 = 一整天都安静，不能被 % 24 折成「关闭」。"""
        self.assertTrue(in_window(0, 0, 24))
        self.assertTrue(in_window(23, 0, 24))
        self.assertFalse(in_window(0, 22, 24))
        self.assertEqual(window_span_hours(0, 24), 24.0)
        self.assertTrue(quiet_decision(3, 3, 0, 24)[0])
        self.assertTrue(quiet_decision(3, 3, 0, 24, core_window=(0, 24))[0])

    def test_normalize_quiet_clock_is_lenient(self):
        self.assertEqual(normalize_quiet_clock("CORE"), QUIET_CLOCK_CORE)
        self.assertEqual(normalize_quiet_clock("  core "), QUIET_CLOCK_CORE)
        self.assertEqual(normalize_quiet_clock("city"), QUIET_CLOCK_CORE)
        self.assertEqual(normalize_quiet_clock("local"), QUIET_CLOCK_LOCAL)
        self.assertEqual(normalize_quiet_clock("乱写"), QUIET_CLOCK_LOCAL)
        self.assertEqual(normalize_quiet_clock(None), QUIET_CLOCK_LOCAL)

    def test_seconds_until_quiet_end(self):
        self.assertEqual(seconds_until_quiet_end(3, 0, 23, 7), 4 * 3600)
        self.assertEqual(seconds_until_quiet_end(23, 30, 23, 7), 27000)
        self.assertEqual(seconds_until_quiet_end(7, 0, 23, 7), 0)
        self.assertEqual(seconds_until_quiet_end(12, 0, 23, 7), 0)


def _local_ts(hour: float) -> float:
    """本机墙上时间今天 hour 点的那个时间戳（测试不能假设跑在哪个时区）。"""
    d = clock.city_now(NOW, None)
    midnight = NOW - (d.hour * 3600 + d.minute * 60 + d.second)
    return midnight + hour * 3600


class QuietOverlapTest(unittest.TestCase):
    """issue #3 的建议二：接话窗口跨过夜里，被夜吃掉的那段要还回来。"""

    def test_a_whole_night_inside_the_window_is_counted(self):
        start = _local_ts(22)  # 本地 22:00 起，到第二天 07:00
        self.assertEqual(
            quiet_overlap_seconds(start, start + 9 * 3600, 23, 7), 8 * 3600.0
        )

    def test_only_the_part_inside_the_window_counts(self):
        start = _local_ts(22)  # 22:00 → 00:00，只有 23–24 这一小时落在夜里
        self.assertEqual(quiet_overlap_seconds(start, start + 2 * 3600, 23, 7), 3600.0)

    def test_daytime_overlap_is_zero(self):
        start = _local_ts(10)
        self.assertEqual(quiet_overlap_seconds(start, start + 3600, 23, 7), 0.0)

    def test_disabled_window_never_overlaps(self):
        self.assertEqual(quiet_overlap_seconds(NOW, NOW + 8 * 3600, 5, 5), 0.0)

    def test_bad_arguments_do_not_raise(self):
        self.assertEqual(quiet_overlap_seconds(NOW, NOW, 23, 7), 0.0)
        self.assertEqual(quiet_overlap_seconds("x", NOW, 23, 7), 0.0)
        self.assertEqual(quiet_overlap_seconds(NOW, NOW + 3600, None, 7), 0.0)


# ─── 2. 引擎：唯一判定入口 + 两道闸 ──────────────────────────────


class EngineQuietStateTest(unittest.TestCase):
    """_quiet_state 是唯一入口：面板窗口按本机、Core 夜按她的城市、取并集。"""

    def test_all_day_window_is_always_quiet(self):
        eng = _engine(quiet_start=0, quiet_end=24)
        is_quiet, why = eng._quiet_state("b1", NOW)
        self.assertTrue(is_quiet)
        self.assertIn("安静时段", why)
        # v2.1.1：面板窗口按「插件时区」判（默认 UTC+8），文案给的是偏移；
        # 只有时区填 auto 才写「本机」。旧断言把 auto 的口径当成了默认。
        self.assertIn("UTC+8", why)

    def test_zero_window_is_never_quiet(self):
        eng = _engine()
        self.assertEqual(eng._quiet_state("b1", NOW), (False, ""))
        self.assertFalse(eng._quiet_at("b1", NOW))

    def test_core_night_is_a_second_source(self):
        eng = _engine()
        eng._night_windows["b1"] = (0.0, 24.0)
        is_quiet, why = eng._quiet_state("b1", NOW)
        self.assertTrue(is_quiet)
        self.assertIn("她的夜里", why)

    def test_core_night_cannot_delete_the_panel_window(self):
        """v2.0.5 的 bug：Core 报了夜窗，面板窗口就被顶掉了。"""
        eng = _engine(quiet_start=0, quiet_end=24)
        eng._night_windows["b1"] = (16.75, 18.25)
        is_quiet, why = eng._quiet_state("b1", NOW)
        self.assertTrue(is_quiet)
        self.assertIn("安静时段", why)

    def test_core_night_source_can_be_switched_off(self):
        eng = _engine(quiet_use_core_night=False)
        eng._night_windows["b1"] = (0.0, 24.0)
        self.assertFalse(eng._quiet_at("b1", NOW))

    def test_core_clock_mode_reads_her_city(self):
        """quiet_clock=core 时窗口按她那边的小时判（opt-in 的老口径）。"""
        eng = _engine(quiet_clock=QUIET_CLOCK_CORE)
        eng._city_offset["b1"] = 120
        her_hour = clock.city_now(NOW, 120).hour
        eng.cfg.quiet_start = her_hour
        eng.cfg.quiet_end = (her_hour + 2) % 24
        is_quiet, why = eng._quiet_state("b1", NOW)
        self.assertTrue(is_quiet)
        self.assertIn("她那边", why)

    def test_a_broken_city_clock_still_uses_the_panel_window(self):
        eng = _engine(quiet_start=0, quiet_end=24)
        with mock.patch.object(
            SocialEngine, "_moment_of", side_effect=RuntimeError("boom")
        ):
            self.assertTrue(eng._quiet_at("b1", NOW))

    def test_quiet_state_is_fail_open(self):
        """判定自己炸了就当不安静：静音是礼貌，不该变成能掐死所有通道的单点。"""
        eng = _engine(quiet_start=0, quiet_end=24)
        with mock.patch.object(
            SocialEngine, "_quiet_state_inner", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(eng._quiet_state("b1", NOW), (False, ""))
            self.assertFalse(eng._quiet_at("b1", NOW))

    def test_quiet_overlap_on_the_engine(self):
        eng = _engine(quiet_start=0, quiet_end=24)
        self.assertAlmostEqual(
            eng._quiet_overlap("b1", NOW - 3600, NOW), 3600.0, delta=60.0
        )
        eng.cfg.quiet_end = 0
        self.assertEqual(eng._quiet_overlap("b1", NOW - 3600, NOW), 0.0)


class EngineQuietGateTest(unittest.TestCase):
    """闸门装在「真要发出去」的地方，而不是只装在「挑人」那一步。"""

    def test_send_block_explains_and_refuses(self):
        eng = _engine(quiet_start=0, quiet_end=24)
        why = eng._quiet_send_block("b1", "")
        self.assertIn("安静时段", why)
        eng.cfg.quiet_end = 0
        self.assertEqual(eng._quiet_send_block("b1", ""), "")

    def test_send_block_is_fail_open(self):
        eng = _engine(quiet_start=0, quiet_end=24)
        with mock.patch.object(
            SocialEngine, "_quiet_state", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(eng._quiet_send_block("b1", ""), "")

    def test_every_send_funnel_asks_the_gate(self):
        """发信口一个都不能漏：漏一个就等于整道闸不存在。"""
        for name in (
            "_speak_inner",
            "_send_with_retry",
            "_send_burst_followup",
            "_send_group_burst_followup",
        ):
            body = inspect.getsource(getattr(SocialEngine, name))
            self.assertTrue(
                "_quiet_send_block" in body or "_quiet_state" in body,
                f"{name} 里没有安静时段这道闸",
            )

    def test_try_once_skips_the_whole_round(self):
        body = inspect.getsource(SocialEngine.try_once)
        self.assertIn("_quiet_state", body)
        self.assertIn("安静时段", body)

    def test_preview_is_not_blocked_but_manual_trigger_is(self):
        """预览是 dry run，任何时段都该能用；真发的那条路夜里要拦。"""
        eng = _engine(quiet_start=0, quiet_end=24)
        eng.running = True
        ok, why = asyncio.run(eng._trigger_precheck("b1"))
        self.assertTrue(ok, f"预览路径被安静时段拦住了：{why}")
        self.assertEqual(why, "")
        block = eng._quiet_trigger_block("b1")
        self.assertIn("安静时段", block)
        self.assertIn("/主动消息预览", block)
        self.assertIn(
            "_quiet_trigger_block", inspect.getsource(SocialEngine.trigger_once)
        )
        self.assertNotIn(
            "_quiet_trigger_block", inspect.getsource(SocialEngine.preview_once)
        )

    def test_manual_trigger_is_waived_for_special_care(self):
        eng = _engine(quiet_start=0, quiet_end=24, special_care_ignore_quiet=True)
        self.assertEqual(eng._quiet_trigger_block("b1"), "")
        eng.cfg.special_care_ignore_quiet = False
        self.assertIn("安静时段", eng._quiet_trigger_block("b1"))

    def test_quiet_trigger_block_is_fail_open(self):
        eng = _engine(quiet_start=0, quiet_end=24)
        with mock.patch.object(
            SocialEngine, "_quiet_state", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(eng._quiet_trigger_block("b1"), "")

    def test_remember_night_keeps_end_of_day(self):
        eng = _engine()
        eng._remember_night("b1", {"night_window": [0, 24]})
        self.assertEqual(eng._night_windows["b1"], (0.0, 24.0))
        self.assertTrue(eng._quiet_at("b1", NOW))
        eng._remember_night("b1", {"night_window": [float("nan"), 5]})
        self.assertNotIn("b1", eng._night_windows)

    def test_remember_night_tolerates_a_partial_engine(self):
        """只实现了部分引擎面的替身没有 _night_windows，不该 AttributeError。"""
        eng = _engine()
        del eng._night_windows
        eng._remember_night("b1", {"night_window": [1, 5]})  # 不抛就是对的


# ─── 3. 接话：没有真实来回就不接 ────────────────────────────────


class ContinueNeedsRealExchangeTest(unittest.TestCase):
    """v2.0.6（m00055）：接话必须接得上，不能只凭时间戳凑一句「继续」。"""

    def test_a_real_exchange_is_continued(self):
        self.assertEqual(threads.thread_reason(_u(), NOW, **_ckw()), ("continue", ""))

    def test_no_exchange_in_the_ledger_is_not_a_continue(self):
        """账本里只有她单方面的一句（对方没说过话）→ 没有可接的话头。"""
        u = _u(
            last_message="",
            last_spoken_text="好",
            conversation=[{"dir": "out", "text": "好", "ts": NOW - 2 * 3600.0}],
        )
        self.assertEqual(threads.thread_reason(u, NOW, **_ckw())[0], "")

    def test_her_own_line_being_a_farewell_is_not_a_continue(self):
        """她自己那句是道别，再接着说等于把人叫回来。"""
        u = _u(last_spoken_text="晚安，先睡啦")
        self.assertEqual(threads.thread_reason(u, NOW, **_ckw())[0], "")

    def test_continue_window_is_off_by_default(self):
        kw = dict(BASE_KW)
        self.assertEqual(threads.thread_reason(_u(), NOW, **kw)[0], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
