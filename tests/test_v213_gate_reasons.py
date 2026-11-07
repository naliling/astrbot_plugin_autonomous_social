# -*- coding: utf-8 -*-
"""v2.1.3：每一个「没发出去」都要留一条机器可读的原因码。

背景：engine.py 里有二十多道闸，每道只返回一句中文（「现在是安静时段……」
「离上一条才过 12 分钟……」）。人读得懂，机器读不了：排查「这一轮为什么一条
都没发」只能翻日志、对着文案数；文案改一个字，之前统计出来的分类就全漂了。

这个文件钉死四件事：

1. _blocked() 的**对外契约一个字都不变**：还是 (False, 原文案)，老调用点与
   老测试不受影响；码只往 _gate_trace / _gate_counts 里记。
2. 环形缓冲有上限（GATE_TRACE_MAX），落盘更短（GATE_TRACE_PERSIST）——原因码是
   排查用的样本，不是审计账本，不能让它把 state.json 撑大。
3. state.json 往返：重启后当天的计数与最近的轨迹都还在；文件被改坏时读侧容错，
   不能因为一个坏存档就让插件起不来（fail-open）。
4. 真闸门路径：熔断、安静时段、最小间隔、引擎未运行，四条都是真实入口打出来的
   码，不是手工塞 _gate_trace 造的假数据。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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
    api.logger = logging.getLogger("autonomous_social_v213_gate_reasons_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.clock import QUIET_CLOCK_LOCAL  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import (  # noqa: E402
    GATE_REPORT_HOURS,
    GATE_TRACE_MAX,
    GATE_TRACE_PERSIST,
    SocialEngine,
)
from social.state import SocialState  # noqa: E402

NOW = 1_800_000_000.0


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


def _run(coro):
    return asyncio.run(coro)


def _engine(now: float = NOW, path: str = "", **cfg) -> SocialEngine:
    """造一台引擎。默认把安静时段关掉：断言不能取决于跑测试时本机几点。"""
    tmp = path or str(Path(tempfile.mkdtemp()) / "state.json")
    eng = SocialEngine(
        context=None,
        config=SocialConfig(),
        state_path=tmp,
        data_dir=str(Path(tmp).parent),
        time_source=_Clock(now),
    )
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


def _u(**over) -> dict:
    """一条正常的私聊用户状态：会话地址齐全，两小时前聊过、已回。"""
    u = {
        "umo": "aiocqhttp:FriendMessage:u1",
        "last_seen": NOW - 2 * 3600.0 - 60,
        "last_spoken": NOW - 2 * 3600.0,
        "last_message": "今天路上好堵",
        "last_spoken_text": "那你早点休息吧",
        "conversation": [
            {"dir": "in", "text": "今天路上好堵", "ts": NOW - 2 * 3600.0 - 600},
            {"dir": "out", "text": "那你早点休息吧", "ts": NOW - 2 * 3600.0},
        ],
        "pending_result": "replied",
        "silent_sent": 0,
    }
    u.update(over)
    return u


def _codes(eng: SocialEngine):
    return [row.get("code") for row in eng._gate_trace]


# ─── 1. 记账本身：契约不变、有上限、坏状态不崩 ──────────────────


class BlockedRecorderTest(unittest.TestCase):
    def test_returns_exactly_the_old_tuple(self):
        """对外必须还是 (False, 原文案)：上层 if not ok 与老测试都靠它。"""
        eng = _engine()
        text = "现在是安静时段（本机 03 点），不打扰"
        out = eng._blocked("quiet_hours", text)
        self.assertEqual(out, (False, text))
        self.assertIs(out[0], False)

    def test_records_code_text_at_and_detail(self):
        eng = _engine()
        eng._blocked("breaker_open", "发送侧熔断中", left_seconds=600.0)
        self.assertEqual(len(eng._gate_trace), 1)
        row = eng._gate_trace[0]
        self.assertEqual(row["code"], "breaker_open")
        self.assertEqual(row["text"], "发送侧熔断中")
        self.assertEqual(row["at"], NOW)
        self.assertEqual(row["detail"], {"left_seconds": 600.0})

    def test_counts_accumulate_per_code(self):
        eng = _engine()
        eng._blocked("cooldown", "离上一条才过 5 分钟")
        eng._blocked("cooldown", "离上一条才过 6 分钟")
        eng._blocked("quiet_hours", "现在是安静时段")
        self.assertEqual(eng._gate_counts["cooldown"], 2)
        self.assertEqual(eng._gate_counts["quiet_hours"], 1)

    def test_trace_is_a_bounded_ring(self):
        """64 条上限：原因码是排查样本，不能无限长在内存里。"""
        eng = _engine()
        for i in range(GATE_TRACE_MAX + 36):
            eng._blocked("cooldown", f"第 {i} 次")
        self.assertEqual(len(eng._gate_trace), GATE_TRACE_MAX)
        # 留下的是最近的：最后一条就是最后一次记的
        self.assertEqual(eng._gate_trace[-1]["text"], f"第 {GATE_TRACE_MAX + 35} 次")
        # 计数不受环形缓冲影响，照旧累加
        self.assertEqual(eng._gate_counts["cooldown"], GATE_TRACE_MAX + 36)

    def test_blank_code_falls_back_to_unspecified(self):
        """码写错/写空不能变成一条空键：那样统计表里会出现一个看不见的分组。"""
        eng = _engine()
        eng._blocked("", "某个没起名字的闸")
        self.assertEqual(_codes(eng), ["unspecified"])

    def test_broken_state_does_not_break_the_gate(self):
        """记账失败绝不能让闸门本身抛异常——被拦下这件事比记账重要。"""
        eng = _engine()
        eng.state.data = None  # 存档结构被改坏
        out = eng._blocked("no_session", "没有可用的会话来源")
        self.assertEqual(out, (False, "没有可用的会话来源"))
        self.assertEqual(eng._gate_counts["no_session"], 1)


# ─── 2. gate_report()：状态页/容器读的那份汇总 ────────────────


class GateReportTest(unittest.TestCase):
    def test_report_ranks_by_count(self):
        eng = _engine()
        for _ in range(3):
            eng._blocked("breaker_open", "发送侧熔断中")
        eng._blocked("quiet_hours", "现在是安静时段")
        rep = eng.gate_report()
        self.assertEqual(rep["top"][0], ["breaker_open", 3])
        self.assertEqual(rep["top"][1], ["quiet_hours", 1])
        self.assertEqual(rep["total_today"], 4)
        self.assertEqual(rep["window_hours"], GATE_REPORT_HOURS)
        self.assertEqual(rep["trace_size"], 4)

    def test_report_shape_is_plain_json_types(self):
        """容器侧要直接 json.dumps 它：返回里不能有 deque/自定义对象。"""
        eng = _engine()
        eng._blocked("cooldown", "离上一条才过 5 分钟", gap_seconds=300.0)
        rep = eng.gate_report()
        json.dumps(rep)  # 不抛就说明可序列化
        self.assertIsInstance(rep["top"], list)
        self.assertIsInstance(rep["counts_today"], dict)
        self.assertIsInstance(rep["recent"], list)
        self.assertIsInstance(rep["recent"][0]["detail"], dict)

    def test_report_window_drops_old_rows(self):
        """30 小时前那条不算进「最近 24 小时」。"""
        eng = _engine()
        eng._blocked("cooldown", "离上一条才过 5 分钟")
        eng._gate_trace[-1]["at"] = NOW - 30 * 3600.0
        rep = eng.gate_report()
        self.assertEqual(rep["top"], [])

    def test_report_falls_back_to_today_counts_when_trace_is_gone(self):
        """trace 被挤空（刚重启）时退回当天计数，别显示成「一道闸都没拦过」。"""
        eng = _engine()
        eng._gate_counts["send_failed"] = 7
        rep = eng.gate_report()
        self.assertEqual(rep["top"], [["send_failed", 7]])


# ─── 3. state.json 往返：重启还在、写坏了不崩 ──────────────────


class GateStateRoundTripTest(unittest.TestCase):
    def test_trace_and_counts_survive_restart(self):
        eng = _engine()
        eng._blocked("breaker_open", "发送侧熔断中", left_seconds=600.0)
        eng._blocked("cooldown", "离上一条才过 5 分钟")
        eng.state.flush()
        path = eng.state.path

        again = _engine(path=path)
        self.assertEqual(_codes(again), ["breaker_open", "cooldown"])
        self.assertEqual(again._gate_counts.get("cooldown"), 1)
        self.assertEqual(again._gate_trace[0]["detail"], {"left_seconds": 600.0})

    def test_only_the_last_rows_are_persisted(self):
        """落盘只留 32 条：state.json 是每次心跳都在写的热文件，不能越写越胖。"""
        eng = _engine()
        for i in range(GATE_TRACE_PERSIST + 18):
            eng._blocked("cooldown", f"第 {i} 次")
        eng.state.flush()

        again = _engine(path=eng.state.path)
        self.assertEqual(len(again._gate_trace), GATE_TRACE_PERSIST)
        self.assertEqual(
            again._gate_trace[-1]["text"], f"第 {GATE_TRACE_PERSIST + 17} 次"
        )

    def test_counts_start_over_on_a_new_day(self):
        """按天重置：昨天 40 次安静时段不该挂在今天的「最近 24 小时」里。"""
        eng = _engine()
        eng._blocked("quiet_hours", "现在是安静时段")
        eng.state.flush()

        tomorrow = _engine(now=NOW + 86400.0 + 10.0, path=eng.state.path)
        self.assertEqual(tomorrow._gate_counts, {})
        self.assertEqual(tomorrow.gate_report()["total_today"], 0)

    def test_corrupt_state_file_is_tolerated(self):
        """state.json 被改坏：插件照常起来，只是没有历史原因码（fail-open）。"""
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        tmp.write_text("{ 这不是 json", encoding="utf-8")
        eng = _engine(path=str(tmp))
        self.assertEqual(len(eng._gate_trace), 0)
        rep = eng.gate_report()
        self.assertEqual(rep["top"], [])
        # 读坏了也得能继续记账
        eng._blocked("no_session", "没有可用的会话来源")
        self.assertEqual(eng._gate_counts["no_session"], 1)

    def test_garbage_gate_rows_are_skipped_one_by_one(self):
        """单条脏数据只丢那一条，不能把整段历史丢掉。"""
        eng = _engine()
        eng.state.data["_gate_trace"] = [
            "这不是字典",
            {"code": "", "text": "没码", "at": NOW},
            {"code": "cooldown", "text": "好的那条", "at": NOW, "detail": {}},
            {"code": "quiet_hours", "text": "at 不是数字", "at": "坏"},
        ]
        eng._load_gate_state()
        self.assertEqual(_codes(eng), ["cooldown"])


# ─── 4. 真闸门路径：从真实入口打出来的码 ──────────────────────


class RealGatePathsTest(unittest.TestCase):
    def test_breaker_gate_reports_breaker_open(self):
        """发送侧熔断：文案逐字不变，码是 breaker_open。"""
        eng = _engine()
        eng._breaker_slot("b1")["until"] = NOW + 600.0
        ok, why = _run(eng._speak_inner("b1", "u1", _u(), 3.0, NOW))
        self.assertFalse(ok)
        self.assertEqual(why, "发送侧熔断中（还剩 10 分钟），本轮不生成任何内容")
        self.assertEqual(_codes(eng), ["breaker_open"])
        self.assertEqual(eng._gate_trace[0]["detail"]["left_seconds"], 600.0)

    def test_quiet_gate_reports_quiet_hours(self):
        """安静时段整轮硬闸：码 quiet_hours，detail 里带原因文案。"""
        eng = _engine(quiet_start=0, quiet_end=24)
        ok, why = _run(eng._speak_inner("b1", "u1", _u(), 3.0, NOW))
        self.assertFalse(ok)
        self.assertIn("安静时段", why)
        self.assertEqual(_codes(eng), ["quiet_hours"])
        self.assertIn("安静", eng._gate_trace[0]["detail"]["why"])

    def test_cooldown_gate_reports_cooldown(self):
        """最小间隔：一分钟前刚说过、又没由头，码是 cooldown。"""
        eng = _engine()
        u = _u(last_sent=NOW - 60.0, last_spoken=NOW - 60.0, last_seen=NOW - 3600.0)
        ok, why = _run(eng._speak_inner("b1", "u1", u, 0.0, NOW))
        self.assertFalse(ok)
        self.assertIn("分钟", why)
        self.assertEqual(_codes(eng), ["cooldown"])
        self.assertGreater(eng._gate_trace[0]["detail"]["gap_seconds"], 0)

    def test_no_session_gate_reports_no_session(self):
        eng = _engine()
        u = _u()
        u.pop("umo")
        ok, _why = _run(eng._speak_inner("b1", "u1", u, 3.0, NOW))
        self.assertFalse(ok)
        self.assertEqual(_codes(eng), ["no_session"])

    def test_trigger_precheck_reports_engine_not_running(self):
        """预检那条路也走同一套记账（它不是 _speak_inner，容易漏）。"""
        eng = _engine()
        eng.running = False
        ok, why = _run(eng._trigger_precheck("b1"))
        self.assertFalse(ok)
        self.assertIn("没有在运行", why)
        self.assertEqual(_codes(eng), ["engine_not_running"])

    def test_gate_trace_does_not_change_the_public_answer(self):
        """同一道闸连拦两次：文案逐字相同（记账不污染返回值）。"""
        eng = _engine()
        eng._breaker_slot("b1")["until"] = NOW + 600.0
        first = _run(eng._speak_inner("b1", "u1", _u(), 3.0, NOW))
        second = _run(eng._speak_inner("b1", "u1", _u(), 3.0, NOW))
        self.assertEqual(first, second)
        self.assertEqual(eng._gate_counts["breaker_open"], 2)


# ─── 5. 状态页：/自主社交状态 能看到 top 原因码 ────────────────


class StatusTextTest(unittest.TestCase):
    def test_status_text_lists_top_codes(self):
        eng = _engine()
        eng._breaker_slot("b1")["until"] = NOW + 600.0
        _run(eng._speak_inner("b1", "u1", _u(), 3.0, NOW))
        text = _run(eng.status_text())
        self.assertIn("原因码", text)
        self.assertIn("breaker_open", text)

    def test_status_text_survives_a_broken_report(self):
        """汇总炸了只是少一段，整页状态不能跟着崩。"""
        eng = _engine()
        eng.gate_report = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸了"))
        text = _run(eng.status_text())
        self.assertIsInstance(text, str)
        self.assertTrue(text)


# ─── 6. 接线（第 5 项）：收尾守卫 / 半截话 / 群收口 / 追问复核 ─────


def _wire_full_send(eng: SocialEngine, reply: str = "晚安") -> None:
    """把生成、会话库、人格、记忆、平台换成桩，让 _speak_inner 真跑一遍。

    这四个接线点都在真实链路上（生成前算收尾、生成后拦、发送后记回执），
    只手工塞 trace 测不到「接没接上」，所以按 tests/test_v1289_continue.py
    同款桩让它走到该走的闸门。
    """
    decision = types.SimpleNamespace(
        raw_text="SEND\n" + reply, send=True, parts=[reply], promise=""
    )

    async def _decide(*_a, **_k):
        return decision

    async def _history(_umo):
        return []

    async def _memory(_umo, _uid, query=""):
        return ""

    async def _persona(_umo, _bid):
        return ("p", "")

    async def _noop(*_a, **_k):
        return None

    async def _send(_target, _text, bid=""):
        return True, "", False

    eng.generator = types.SimpleNamespace(decide=_decide)
    eng._load_session_history = _history
    eng._fetch_memory_note = _memory
    eng._persona = _persona
    eng.input_state = types.SimpleNamespace(start=_noop, stop=_noop)
    eng._send_with_retry = _send


class ClosingGuardWiringTest(unittest.TestCase):
    """她上一条已经道过别、主人也只回了一句「晚安」时不该再补一句。"""

    def test_a_second_farewell_is_silenced(self):
        eng = _engine()
        _wire_full_send(eng)
        u = _u(
            last_seen=NOW - 60.0,
            conversation=[
                {"dir": "in", "text": "那我先睡了", "ts": NOW - 360.0},
                {"dir": "out", "text": "晚安啦，做个好梦", "ts": NOW - 300.0},
                {"dir": "in", "text": "晚安", "ts": NOW - 60.0},
            ],
        )
        ok, why = _run(eng._speak_inner("b1", "u1", u, 0.0, NOW))
        self.assertFalse(ok)
        self.assertIn("收尾", why)
        self.assertEqual(_codes(eng), ["closing_guard"])
        detail = eng._gate_trace[0]["detail"]
        self.assertEqual(detail.get("action"), "silent")
        self.assertEqual(detail.get("reason"), "role_already_closed")

    def test_a_real_message_still_goes_out(self):
        """收尾守卫只压「道别」，正常话题必须照常发（fail-open 的反面）。"""
        eng = _engine()
        _wire_full_send(eng, reply="公司楼下那家面馆，改天一起去吃吃看？")
        u = _u(
            last_seen=NOW - 60.0,
            conversation=[
                {"dir": "in", "text": "明天要早起", "ts": NOW - 360.0},
                {"dir": "out", "text": "那你早点睡", "ts": NOW - 300.0},
                {"dir": "in", "text": "公司楼下新开了家面馆", "ts": NOW - 60.0},
            ],
        )
        ok, _why = _run(eng._speak_inner("b1", "u1", u, 0.0, NOW))
        self.assertTrue(ok)
        self.assertNotIn("closing_guard", _codes(eng))


class UnfinishedWiringTest(unittest.TestCase):
    """半截话只拦半小时内的新信号，旧信号不能继续禁言。"""

    def test_a_fresh_unfinished_signal_holds_the_reply(self):
        eng = _engine()
        u = _u(
            await_unfinished="await_unfinished:unfinished",
            await_unfinished_at=NOW - 60.0,
        )
        ok, why = _run(eng._speak_inner("b1", "u1", u, 0.0, NOW))
        self.assertFalse(ok)
        self.assertIn("没说完", why)
        self.assertEqual(_codes(eng), ["await_unfinished"])
        self.assertEqual(
            eng._gate_trace[0]["detail"].get("wait"), "await_unfinished:unfinished"
        )

    def test_a_stale_unfinished_signal_no_longer_blocks(self):
        eng = _engine()
        u = _u(
            await_unfinished="await_unfinished:unfinished",
            await_unfinished_at=NOW - 3600.0,
        )
        _run(eng._speak_inner("b1", "u1", u, 0.0, NOW))
        self.assertNotIn("await_unfinished", _codes(eng))


class GroupCloseoutWiringTest(unittest.TestCase):
    """群里连着三轮没人接，非点名理由就该闭嘴并记账。"""

    def test_a_plain_interjection_is_closed_out(self):
        eng = _engine()
        eng.running = True
        g = eng.state.group("bot1", "g1")
        g["umo"] = "aiocqhttp:GroupMessage:g1"
        g["samples"] = [
            {"ts": NOW - 90.0, "name": "阿明", "text": "今天加班到九点"},
            {"ts": NOW - 40.0, "name": "", "text": "是啊", "self": True},
            {"ts": NOW - 30.0, "name": "", "text": "早点睡吧", "self": True},
            {"ts": NOW - 20.0, "name": "", "text": "嗯嗯", "self": True},
        ]
        with patch(
            "social.engine.groupflow.flow_should_consider", return_value=(True, "")
        ):
            _run(eng._flow_reply_inner("bot1", "g1", NOW))
        self.assertEqual(_codes(eng), ["closeout_active"])
        self.assertEqual(eng._gate_trace[0]["detail"].get("reason"), "flow_interject")

    def test_closed_out_group_can_still_be_mentioned(self):
        """点名必须放行，否则收口会变成「叫了也不应」。"""
        eng = _engine()
        eng.running = True
        g = eng.state.group("bot1", "g1")
        g["umo"] = "aiocqhttp:GroupMessage:g1"
        g["samples"] = [
            {"ts": NOW - 90.0, "name": "阿明", "text": "今天加班到九点"},
            {"ts": NOW - 40.0, "name": "", "text": "是啊", "self": True},
            {"ts": NOW - 30.0, "name": "", "text": "早点睡吧", "self": True},
            {"ts": NOW - 20.0, "name": "", "text": "嗯嗯", "self": True},
            {"ts": NOW - 5.0, "name": "阿明", "text": "小满你也在吗"},
        ]
        with patch(
            "social.engine.groupflow.flow_should_consider", return_value=(True, "")
        ), patch(
            "social.engine.groupflow.attribution",
            return_value={"addressed": True, "reason": "mention", "targets": ()},
        ):
            _run(eng._flow_reply_inner("bot1", "g1", NOW))
        self.assertNotIn("closeout_active", _codes(eng))


class _NullCore:
    def read_root(self):
        return None

    def bot_self_state(self, bid, root=None):
        return None


class _NullSignals:
    def set_ignored_streak(self, *_a, **_k):
        return None

    def note_ignored(self, *_a, **_k):
        return None

    def set_role_count(self, *_a, **_k):
        return None

    def note_open_items(self, *_a, **_k):
        return None

    def beat(self):
        return None


class _LoopRoundEngine:
    """只开「追问」一条通道的假引擎，用来驱动真 try_once 的收集段。"""

    try_once = SocialEngine.try_once
    _user_lock = SocialEngine._user_lock
    _persist_metrics = SocialEngine._persist_metrics
    _is_infra_note = staticmethod(SocialEngine._is_infra_note)
    _candidate_sort_key = SocialEngine._candidate_sort_key

    def __init__(self) -> None:
        self.cfg = SocialConfig()
        self.cfg.enabled = True
        self.cfg.group_icebreak_enabled = False
        self.cfg.group_topic_enabled = False
        self.cfg.max_sends_per_round = 3
        self.cfg.batch_send_concurrency = 1
        self.state = SocialState(str(Path(tempfile.mkdtemp()) / "state.json"))
        self.core = _NullCore()
        self.signals = _NullSignals()
        self._user_locks = {}
        self._last_flush = 1.0
        self._m = {}
        self.generator = types.SimpleNamespace(
            llm_backoff_remaining=lambda scope="": 0.0
        )
        self.gates = []
        self.presets = []
        bot = self.state.bot("bot1")
        bot.setdefault("users", {})
        self.u = {
            "umo": "aiocqhttp:FriendMessage:u1",
            "loops": [
                {
                    "about": "明天的面试",
                    "last_followup_at": NOW - 3600.0,
                    "last_context_at": NOW - 7200.0,
                }
            ],
        }
        bot["users"]["u1"] = self.u
        self.state.mark_dirty()

    # 收集层：除追问外全部不产生候选
    def _promise_pick(self, *_a, **_k):
        return []

    def _miss_pick(self, *_a, **_k):
        return []

    def _intimacy_pick(self, *_a, **_k):
        return []

    def settle_minds(self, *_a, **_k):
        return []

    def _anchor_pick(self, *_a, **_k):
        return []

    def _thread_pick(self, *_a, **_k):
        return None

    def _loop_pick(self, *_a, **_k):
        return ("u1", self.u, "明天的面试", 60.0)

    def _closer_pick(self, *_a, **_k):
        return None

    def _greet_pick(self, *_a, **_k):
        return []

    def _miss_mark_today(self, _u, _day):
        return 1

    def _count_block(self, *_a, **_k):
        return None

    def _note_decision(self, *_a, **_k):
        return None

    def _note_blocked(self, code, text, **detail):
        self.gates.append((code, text, detail))

    def log(self, *_a, **_k):
        return None

    def _time(self):
        return NOW

    def _moment_of(self, _bid, _now):
        return datetime.datetime(2026, 1, 1, 12, 0)

    def _open_items(self, _bid, _now):
        return []

    def _beat_signals(self):
        return None

    def _core_last_interaction(self, _root, _bid, _uid):
        return 0.0

    def _relation_bucket(self, _u):
        return 0

    async def _speak(
        self,
        bid,
        uid,
        u,
        urge,
        now,
        *,
        allow_veto=True,
        preset=None,
        require_about_peer=False,
    ):
        self.presets.append(preset)
        return True, "ok"


class FollowupReviewWiringTest(unittest.TestCase):
    """48 小时内问过、对方还没回音的追问，这一轮要标成复核语气。"""

    def test_a_pending_followup_is_marked_for_review(self):
        eng = _LoopRoundEngine()
        _run(eng.try_once())
        self.assertIn("followup_review", [code for code, _t, _d in eng.gates])
        metas = [p[1] for p in eng.presets if p]
        self.assertTrue(any(m.get("followup_review") for m in metas))

    def test_a_fresh_loop_is_not_marked_for_review(self):
        eng = _LoopRoundEngine()
        eng.u["loops"][0].pop("last_followup_at", None)
        _run(eng.try_once())
        self.assertNotIn("followup_review", [code for code, _t, _d in eng.gates])


class NewCodesReportTest(unittest.TestCase):
    """四个接线码必须能被 gate_report 读到（面板靠它显示）。"""

    def test_wiring_codes_are_counted(self):
        eng = _engine()
        for code in (
            "closing_guard",
            "closeout_active",
            "followup_review",
            "await_unfinished",
        ):
            eng._blocked(code, "接线新码")
        top = dict(eng.gate_report()["top"])
        for code in (
            "closing_guard",
            "closeout_active",
            "followup_review",
            "await_unfinished",
        ):
            self.assertEqual(top.get(code), 1)


if __name__ == "__main__":
    unittest.main()
