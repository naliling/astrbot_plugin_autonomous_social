"""v1.28.9：主动消息的「真回避」——对方连着几天没开口就**全面**停发。

用户原话：「对方连续 3 天没开口就全面停发（问候也算），等对方先开口才恢复」。

为什么原来九道闸没有一道看得见这件事：

* 问候走 `expect_reply=False`，不进 pending、也不进 `no_reply_streak`；
* `COLD_SILENCE_SENT` 只挡**非** greet 类；
* 于是「一句不回的人每天照样收早安」。本机 state.json 的真实记录：某人最后一次
  开口是 10-18 15:33，之后 10-19/21/22/23/25 各收到一条 cat=greeting 的主动消息
  （silent_sent=5、proactive_sent=0、no_reply_streak=0，没有任何一道闸看得见）。

本文件盯五件事：

1. `SocialEngine.silent_giveup_days` 的边界：没开口过 / 没发过 / 刚好到点 / 差一点 /
   配置 0 / 配置改成别的天数 / 脏数据；
2. 三道闸（`gate_reason` / `greet_gate` / `thread_gate`）都返回同一条回避原因
   ——判据只有一份（`_avoidance_note`），不是四处各写一遍；
3. `_speak_inner` 这个**唯一收口**真的 return False（问候/由头/收场都要经过它）；
4. 面板：`status_text` 的「回避」行与 `_thread_mark` 的「回避中（N 天没开口）」；
5. 回避原因在运行指标里单独成类（落进「其它」等于白记）。

按仓库惯例自带最小 astrbot 桩（照抄 `tests/test_v12733_engine_badvalues.py` 头部）。
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
    api.logger = logging.getLogger("autonomous_social_v1289_avoid_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.config import (  # noqa: E402
    SILENT_GIVEUP_DAYS_DEFAULT,
    SILENT_GIVEUP_DAYS_MAX,
    SILENT_GIVEUP_DAYS_MIN,
    SocialConfig,
)
from social.engine import SocialEngine  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400.0


class _Clock:
    """可控时间源（引擎的 `_time()` 会做单调钳制，所以固定时刻就够）。"""

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
    for key, value in cfg.items():
        setattr(eng.cfg, key, value)
    return eng


def _quiet_off(eng: SocialEngine) -> SocialEngine:
    """把安静时段压成零长：断言结果不能取决于跑测试时本机是几点。"""
    eng.cfg.quiet_start = 0
    eng.cfg.quiet_end = 0
    return eng


def _user(eng: SocialEngine, bid: str = "b1", uid: str = "u1", **fields):
    """在 state 里造一个用户条目（`umo` 必须直接赋值，默认结构里是空串）。"""
    user = eng.state.user(bid, uid)
    user["umo"] = "aiocqhttp:private:u1"
    user["__uid"] = uid
    user.update(fields)
    return user


# ─── 1. 判据本身：silent_giveup_days ──────────────────────────────


class SilentGiveupHelperTest(unittest.TestCase):
    """`silent_giveup_days` 是**唯一**判据，它的边界就是整条功能的边界。"""

    def test_no_record_of_the_peer_ever_speaking(self):
        """`last_seen <= 0`：从没记录过对方开口（播种进来/老数据）→ 不据此判死。"""
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=0.0, silent_sent=5)
        self.assertEqual(eng.silent_giveup_days(u, NOW), 0.0)

    def test_she_never_sent_anything_keeps_one_chance(self):
        """她这期间一条都没主动发过 → 还谈不上「追着一个不回的人」，先留一次机会。

        没有这条兜底的话，一个刚播种进来、对方三天没开口的人会从一开始就被判死。
        """
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=NOW - 5 * DAY, silent_sent=0)
        self.assertEqual(eng.silent_giveup_days(u, NOW), 0.0)

    def test_exactly_at_the_threshold_counts(self):
        """刚好 3 天：`>=` 而不是 `>`——差一分钟就是「还没到」，差一整天就是「到了」。"""
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=NOW - 3 * DAY, silent_sent=2)
        self.assertEqual(eng.silent_giveup_days(u, NOW), 3.0)

    def test_just_below_the_threshold_does_not(self):
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=NOW - 2.9 * DAY, silent_sent=2)
        self.assertEqual(eng.silent_giveup_days(u, NOW), 0.0)

    def test_returns_the_real_day_count(self):
        """返回的是**真实天数**而不是布尔：面板与拦截文案都要念「N 天」。"""
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=NOW - 4.5 * DAY, silent_sent=1)
        self.assertAlmostEqual(eng.silent_giveup_days(u, NOW), 4.5, places=6)

    def test_zero_disables_the_gate(self):
        """配置 0 = 关闭：10 天没开口也不回避（回旧行为）。"""
        eng = _quiet_off(_engine(silent_giveup_days=0))
        u = _user(eng, last_seen=NOW - 10 * DAY, silent_sent=9)
        self.assertEqual(eng.silent_giveup_days(u, NOW), 0.0)

    def test_the_threshold_is_configurable(self):
        eng = _quiet_off(_engine(silent_giveup_days=7))
        u = _user(eng, last_seen=NOW - 5 * DAY, silent_sent=2)
        self.assertEqual(eng.silent_giveup_days(u, NOW), 0.0, "没到 7 天不该回避")
        u["last_seen"] = NOW - 7 * DAY
        self.assertEqual(eng.silent_giveup_days(u, NOW), 7.0)

    def test_dirty_counters_do_not_raise(self):
        """状态文件是外部可改的：`silent_sent="abc"` 不能把整轮心跳抛穿。"""
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=NOW - 5 * DAY, silent_sent="abc")
        self.assertEqual(eng.silent_giveup_days(u, NOW), 0.0)

    def test_avoidance_note_is_empty_when_not_avoiding(self):
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=NOW - 60, silent_sent=3)
        self.assertEqual(eng._avoidance_note(u, NOW), "")


# ─── 2. 三道闸：同一条判据，同一条原因 ────────────────────────────


class AvoidanceGatesTest(unittest.TestCase):
    """`gate_reason` / `greet_gate` / `thread_gate` 的早期位置都要认这一条。

    只加在 `_speak_inner` 里也能拦住消息，但那样本轮 `blocked_reasons` 与面板上
    看不到原因（表现为「她好几天没动静，查不出为什么」）——所以闸与收口都要有。
    """

    def _avoided(self, **cfg):
        eng = _quiet_off(_engine(**cfg))
        u = _user(eng, last_seen=NOW - 4 * DAY, silent_sent=3, last_message="嗯")
        return eng, u

    def test_gate_reason_reports_it(self):
        eng, u = self._avoided()
        why = eng.gate_reason("b1", "u1", u, NOW, None)
        self.assertIn("没开口", why)
        self.assertIn("4 天", why)

    def test_greet_gate_reports_it(self):
        """问候是唯一绕过 pending / 冷落计数的路径，必须自己认这一条。"""
        eng, u = self._avoided()
        for kind in ("morning", "night", "midday"):
            with self.subTest(kind=kind):
                self.assertIn("没开口", eng.greet_gate("b1", "u1", u, NOW, None, kind=kind))

    def test_thread_gate_reports_it(self):
        eng, u = self._avoided()
        self.assertIn("没开口", eng.thread_gate("b1", "u1", u, NOW, None))

    def test_peer_speaking_again_lifts_it(self):
        """解除条件只有一个：对方先开口。TA 刚说话 → 三道闸都不再给这条原因。"""
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=NOW - 60, silent_sent=3, last_message="在吗")
        self.assertNotIn("没开口", eng.gate_reason("b1", "u1", u, NOW, None))
        self.assertNotIn("没开口", eng.greet_gate("b1", "u1", u, NOW, None, kind="morning"))
        self.assertNotIn("没开口", eng.thread_gate("b1", "u1", u, NOW, None))

    def test_disabled_config_keeps_the_old_behaviour(self):
        eng, u = self._avoided(silent_giveup_days=0)
        self.assertNotIn("没开口", eng.gate_reason("b1", "u1", u, NOW, None))
        self.assertNotIn("没开口", eng.greet_gate("b1", "u1", u, NOW, None, kind="morning"))
        self.assertNotIn("没开口", eng.thread_gate("b1", "u1", u, NOW, None))


# ─── 3. 唯一收口：_speak_inner ────────────────────────────────────


class SpeakInnerAvoidanceTest(unittest.IsolatedAsyncioTestCase):
    """所有主动消息（问候/由头/追问/回访/收场）都要经过 `_speak_inner`。

    闸落在这里才是「全面停发」；只放在 `gate_reason` 里的话，问候/追问/收场
    各自走自己的检查，根本不经过它（v1.24.1 的精力闸就踩过这个坑：实测 61 条里
    过了 57 条）。
    """

    async def test_speak_inner_refuses_while_avoiding(self):
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=NOW - 4 * DAY, silent_sent=3, last_message="嗯")
        ok, why = await eng._speak_inner("b1", "u1", u, 3.0, NOW)
        self.assertFalse(ok)
        self.assertIn("没开口", why)

    async def test_speak_inner_says_how_many_days(self):
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=NOW - 5 * DAY, silent_sent=2)
        _ok, why = await eng._speak_inner("b1", "u1", u, 3.0, NOW)
        self.assertIn("5 天", why)

    async def test_a_user_who_just_spoke_is_not_avoided(self):
        """对方刚说过话 → 这道闸不放行（后面是重活，测到闸为止）。"""
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=NOW - 60, silent_sent=3, last_message="在吗")
        try:
            _ok, why = await eng._speak_inner("b1", "u1", u, 3.0, NOW)
        except Exception:
            # 后面全是重活（会话史 / 人格 / LLM），测试里只装配到回避闸
            return
        self.assertNotIn("没开口", why)


# ─── 4. 面板 ─────────────────────────────────────────────────────


class AvoidancePanelTest(unittest.TestCase):
    """「她好几天没动静」时，面板是主人唯一能对上原因的地方。"""

    def test_status_line_is_on(self):
        eng = _quiet_off(_engine(silent_giveup_days=5))
        text = eng._avoidance_status_text()
        self.assertIn("回避", text)
        self.assertIn("5 天", text)
        self.assertIn("停发", text)

    def test_status_line_is_off(self):
        eng = _quiet_off(_engine(silent_giveup_days=0))
        text = eng._avoidance_status_text()
        self.assertIn("回避", text)
        self.assertIn("关", text)

    def test_status_page_actually_prints_the_line(self):
        src = inspect.getsource(SocialEngine.status_text)
        self.assertIn("self._avoidance_status_text()", src, "面板没把「回避」那一行拼进去")

    def test_full_status_page_carries_both_lines(self):
        """整页拼出来看一遍：配置行 + 「念头」段那个人身上的「回避中」。"""
        eng = _quiet_off(_engine())
        _user(eng, last_seen=NOW - 4 * DAY, silent_sent=3)
        text = asyncio.run(eng.status_text())
        self.assertIn("- 回避：开", text)
        self.assertIn("对方连续 3 天没开口就全面停发", text)
        self.assertIn("｜回避中（4 天没开口）", text)

    def test_thread_mark_flags_an_avoiding_user(self):
        eng = _quiet_off(_engine())
        _user(eng, last_seen=NOW - 4 * DAY, silent_sent=3)
        mark = eng._thread_mark("b1", "u1", NOW)
        self.assertIn("回避中", mark)
        self.assertIn("4 天没开口", mark)

    def test_thread_mark_is_quiet_for_a_normal_user(self):
        eng = _quiet_off(_engine())
        _user(eng, last_seen=NOW - 60, silent_sent=1)
        self.assertNotIn("回避中", eng._thread_mark("b1", "u1", NOW))


# ─── 5. 运行指标：回避要单独成类 ──────────────────────────────────


class AvoidanceBlockCategoryTest(unittest.TestCase):
    """`blocked_reasons` 按子串归类。新闸不登记就会落进「其它」——
    而「其它」恰恰是主人最想知道原因的那一栏。"""

    def test_avoidance_has_its_own_category(self):
        why = "TA 已经 4 天没开口，先不主动了（等 TA 先开口）"
        self.assertEqual(SocialEngine._block_category(why), "TA没开口")

    def test_avoidance_is_not_mistaken_for_other_gates(self):
        why = "TA 已经 4 天没开口，先不主动了（等 TA 先开口）"
        for wrong in ("没被接住", "冷却未过", "刚聊过", "其它"):
            self.assertNotEqual(SocialEngine._block_category(why), wrong)


# ─── 6. 配置默认值 / 范围 ────────────────────────────────────────


class SilentGiveupConfigTest(unittest.TestCase):
    def test_default_is_three_days(self):
        self.assertEqual(SocialConfig().silent_giveup_days, 3)
        self.assertEqual(SILENT_GIVEUP_DAYS_DEFAULT, 3)
        # 走 from_astrbot（面板路径）拿到的也必须是 schema 里那个 3
        self.assertEqual(SocialConfig.from_astrbot({}).silent_giveup_days, 3)

    def test_range_constants_match_the_schema(self):
        self.assertEqual(SILENT_GIVEUP_DAYS_MIN, 0)
        self.assertEqual(SILENT_GIVEUP_DAYS_MAX, 30)

    def test_values_are_clamped(self):
        self.assertEqual(SocialConfig.from_astrbot({"silent_giveup_days": -1}).silent_giveup_days, 0)
        self.assertEqual(SocialConfig.from_astrbot({"silent_giveup_days": 999}).silent_giveup_days, 30)
        self.assertEqual(SocialConfig.from_astrbot({"silent_giveup_days": 0}).silent_giveup_days, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
