"""v2.1.8：engine 侧「说话质量」六项改动的回归测试。

为什么单开一份：这一版改的全是**用户能直接听出来**的东西——睡着时群聊还接不接、
破冰还问不问、私聊聊完一句会不会接着聊。这些行为横跨 groupflow 的纯函数闸门、
engine 的上下文拼装和 `_icebreak` 的生成重试，读代码看不出「改完到底像不像人」，
必须用断言把每条行为钉住。

构造方式沿用 tests/test_v217_engine.py：真 Engine + 可控时钟测 engine 辅助函数，
SimpleNamespace 桩测 `_icebreak` 这种依赖一堆外部件的长函数。
"""
from __future__ import annotations

import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime as _dt
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
    api.logger = logging.getLogger("autonomous_social_engine_v218_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import groupflow  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social.threads import thread_reason  # noqa: E402

NOW = 1_800_000_000.0


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

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


def _flow_group(now: float) -> dict:
    """一个「关注窗口开着、上一句很久以前、配额都空着」的群，专门用来测单道闸。"""
    return {
        "flow_open_until": now + 3600.0,
        "flow_last_reply_at": now - 9999.0,
        "flow_last_attempt_at": now - 9999.0,
        "blocked_until": 0.0,
        "flow_hold_until": 0.0,
        "flow_ignored": 0,
        "flow_replies": 0,
    }


def _flow_gate(group: dict, **kw):
    args = dict(
        max_replies=3, min_gap_seconds=60.0, hourly_cap=6, hour_count=0,
        ignored_exit=3, activity=1.0,
    )
    args.update(kw)
    return groupflow.flow_should_consider(group, NOW, **args)


class DrowsyFlowTest(unittest.TestCase):
    """用户原话：「群聊心流就不该有在睡觉必须不能接话的，谁说不能迷迷糊糊的」。

    睡着不再硬拦，但只跳过安静时段这一道；其余硬闸照旧，活跃度门槛还更高。
    """

    def test_asleep_no_longer_blocked_by_quiet_hours(self):
        ok, why = _flow_gate(
            _flow_group(NOW), is_quiet=True, asleep=True,
            activity=0.9, activity_min=0.6,
        )
        self.assertTrue(ok, why)

    def test_quiet_hours_still_block_when_awake(self):
        ok, why = _flow_gate(_flow_group(NOW), is_quiet=True, asleep=False)
        self.assertFalse(ok)
        self.assertIn("安静时段", why)

    def test_drowsy_needs_a_hotter_group(self):
        # 0.5 在清醒时够热（默认门槛远低于 0.5），但睡着时门槛被抬到 0.6。
        ok, why = _flow_gate(
            _flow_group(NOW), asleep=True, activity=0.5, activity_min=0.6,
        )
        self.assertFalse(ok)
        self.assertIn("迷糊", why)

    def test_drowsy_does_not_disable_other_hard_gates(self):
        g = _flow_group(NOW)
        g["blocked_until"] = NOW + 60.0
        ok, _why = _flow_gate(g, asleep=True, activity=0.9, activity_min=0.6)
        self.assertFalse(ok, "睡着只该跳过安静时段，发送隔离必须照拦")


class DrowsyThrottleTest(unittest.TestCase):
    """同一个群 30 分钟最多一条 drowsy——不然迷糊着连刷三条就是刷屏。"""

    def test_first_drowsy_reply_allowed(self):
        eng = _engine()
        self.assertTrue(eng._flow_drowsy_gap_ok({}, NOW))

    def test_second_drowsy_reply_within_thirty_minutes_blocked(self):
        eng = _engine()
        g = {"drowsy_last_reply_at": NOW - 60.0}
        self.assertFalse(eng._flow_drowsy_gap_ok(g, NOW))

    def test_thirty_minutes_later_allowed_again(self):
        eng = _engine()
        g = {"drowsy_last_reply_at": NOW - 1800.0}
        self.assertTrue(eng._flow_drowsy_gap_ok(g, NOW))

    def test_dirty_value_does_not_crash(self):
        eng = _engine()
        self.assertTrue(eng._flow_drowsy_gap_ok({"drowsy_last_reply_at": "坏值"}, NOW))


class IcebreakAsleepTest(unittest.TestCase):
    """破冰是「主动把群叫醒」，睡着时仍然不发起（与心流分开）。"""

    def test_asleep_blocks_icebreak(self):
        g = {
            "msg_count": 10,
            "last_seen": NOW - 7200.0,
            "icebreak_at": 0.0,
            "topic_at": 0.0,
            "last_bot_spoke": 0.0,
        }
        base = dict(
            idle_hours=1.0, daily_cap=1, stale_days=30,
            today="2026-11-08", is_quiet=False,
        )
        self.assertTrue(groupflow.icebreak_due(g, NOW, **base))
        self.assertFalse(groupflow.icebreak_due(g, NOW, asleep=True, **base))


class _FakeState:
    def __init__(self, q_pressure: float = 0.0) -> None:
        self.q_pressure = float(q_pressure)

    def question_pressure(self, _bid: str, _window: int = 3) -> float:
        return self.q_pressure

    def recent_group_self_others(self, *_a, **_k):
        return []

    def group_samples(self, *_a, **_k):
        return []

    def group(self, *_a, **_k):
        return {}

    def __getattr__(self, _name):
        # 发送成功后的记账（note_icebreak / open_flow / record_group_self_text …）
        # 不是本测试要验的东西，统一给个空实现；漏一个就 AttributeError 太脆。
        return lambda *_a, **_k: None


def _icebreak_stub(replies, *, q_pressure=0.0, topic_hint=""):
    """给 `_icebreak` 用的最小桩：只保留生成/拆段/验收/发送，外部依赖全替掉。"""
    stub = types.SimpleNamespace()
    stub.cfg = types.SimpleNamespace(
        group_topic_dedup_hours=0.0,
        allow_burst=False,
        group_topic_max_segments=0,
        max_message_length=0,
        group_ref_lib_enabled=False,
        group_mention_member=False,
        flow_window_minutes=30,
        group_ref_sample_size=20,
        group_store_message_text=False,
    )
    stub.context = None
    stub.state = _FakeState(q_pressure)
    stub._m = {}
    stub._last_persona = {}
    stub.sent = []
    stub.ctx_seen = []
    stub.log = lambda *_a, **_k: None
    stub.send_breaker_left = lambda _bid: 0
    stub._time = lambda: NOW
    stub._moment_of = lambda _bid, ts: _dt.fromtimestamp(float(ts))
    stub._build_group_ctx = lambda bid, gid, g: SocialEngine._build_group_ctx(
        stub, bid, gid, g
    )
    stub._group_topic_hint = lambda *_a, **_k: (topic_hint, "", [])
    stub._clock_offset_for = lambda _bid: 0.0
    stub._verify_all_segments = lambda *_a, **_k: ("", "")

    async def _persist_state():
        return True

    stub._persist_state = _persist_state

    async def _persona(_umo, _bid):
        return "", ""

    stub._persona = _persona

    async def _send(_umo, text, _bid=""):
        stub.sent.append(text)
        return True, "", False

    stub._send_with_retry = _send

    calls = []

    async def _group_message(_umo, _mode, ctx, _persona_prompt, **_kw):
        calls.append(dict(ctx))
        idx = min(len(calls) - 1, len(replies) - 1)
        return list(replies[idx])

    stub.generator = types.SimpleNamespace(group_message=_group_message)
    stub.calls = calls
    return stub


def _run_icebreak(stub):
    import asyncio

    return asyncio.run(
        SocialEngine._icebreak(
            stub, "b1", "g1", {"umo": "aiocqhttp:GroupMessage:g1", "samples": []}, NOW,
        )
    )


class IcebreakOutputTest(unittest.TestCase):
    """自然问句可以原样发送；NO 仍是模型可选的结果。"""

    def test_question_is_sent_unchanged_after_one_generation(self):
        question = "你们今天都干嘛呢？"
        stub = _icebreak_stub([[question]])
        self.assertTrue(_run_icebreak(stub))
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(stub.sent, [question])

    def test_no_choice_skips_sending(self):
        stub = _icebreak_stub([[]])
        self.assertFalse(_run_icebreak(stub))
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(stub.sent, [])


class GroupContextWindowTest(unittest.TestCase):
    """用户原话「群聊接不上话」：群路径至少 12 条，且点名信息要进上下文。"""

    def test_group_context_has_at_least_twelve_messages(self):
        eng = _engine(context_inject_count=5)
        g = {"samples": [{"name": "A", "text": f"m{i}"} for i in range(20)]}
        recent = eng._build_group_ctx("b1", "g1", g)["recent"]
        self.assertEqual(len(recent), 12)

    def test_addressed_name_is_detected(self):
        eng = _engine()
        g = {
            "samples": [
                {"name": "小明", "text": "昨天面试挂了"},
                {"name": "小明", "text": "小明 你那个面试怎么样了"},
            ]
        }
        ctx = eng._build_group_ctx("b1", "g1", g)
        self.assertEqual(ctx["addressed"], "小明")

    def test_no_address_means_none(self):
        eng = _engine()
        g = {"samples": [{"name": "小明", "text": "今天好热"}]}
        self.assertIsNone(eng._build_group_ctx("b1", "g1", g)["addressed"])


class ContinueAfterWindowTest(unittest.TestCase):
    """The configured continue_after window controls candidate eligibility."""

    def _candidate(self, gap_seconds):
        eng = _engine(
            followup_enabled=False,
            continue_enabled=True,
            continue_after_minutes=25,
            continue_window_hours=8,
        )
        rec = eng.state.user("b1", "u1")
        conversation = [
            ("in", "周末想看电影", 8 * 60),
            ("out", "我也有点想看", 6 * 60),
            ("in", "那周六呢", 4 * 60),
            ("out", "我看看安排", 2 * 60),
            ("in", "好呀", 60),
            ("out", "到时告诉你", 0),
        ]
        rec.update({
            "umo": "aiocqhttp:FriendMessage:u1",
            "last_seen": NOW - gap_seconds - 60,
            "last_spoken": NOW - gap_seconds,
            "last_message": "好呀",
            "last_spoken_text": "到时告诉你",
            "conversation": [
                {"dir": direction, "text": text, "ts": NOW - gap_seconds - offset}
                for direction, text, offset in conversation
            ],
        })
        # Isolate eligibility from unrelated quiet-hour and cooldown gates.
        eng.thread_gate = lambda *_args, **_kwargs: ""
        return SocialEngine._thread_pick(eng, "b1", NOW, None), eng._thread_windows()[4]

    def test_recent_exchange_below_configured_window_is_not_a_candidate(self):
        candidate, wait_seconds = self._candidate(6 * 60)
        self.assertEqual(wait_seconds, 25 * 60)
        self.assertIsNone(candidate)

    def test_candidate_is_allowed_at_configured_window(self):
        candidate, wait_seconds = self._candidate(25 * 60)
        self.assertEqual(wait_seconds, 25 * 60)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate[2], "continue")


class OwnStyleTest(unittest.TestCase):
    """「声音统一」：给她最近 4 条真实回复当口吻锚，且排除主动消息原文。"""

    def test_recent_replies_are_used_newest_first(self):
        eng = _engine()
        rec = eng.state.user("b1", "u1")
        rec["conversation"] = [
            {"dir": "in", "text": "在吗", "ts": NOW - 50},
            {"dir": "out", "text": "在呀", "ts": NOW - 40},
            {"dir": "out", "text": "刚睡醒呢", "ts": NOW - 30},
            {"dir": "out", "text": "在呀", "ts": NOW - 20},
        ]
        self.assertEqual(
            SocialEngine._own_style_texts(eng, "b1", "u1", NOW),
            ["在呀", "刚睡醒呢"],
        )

    def test_proactive_texts_are_excluded(self):
        eng = _engine()
        rec = eng.state.user("b1", "u1")
        rec["conversation"] = [
            {"dir": "out", "text": "主动发的那句", "ts": NOW - 30},
            {"dir": "out", "text": "聊天里的一句", "ts": NOW - 20},
        ]
        eng.state.recent_proactive = lambda *_a, **_k: [{"text": "主动发的那句", "ts": NOW - 30}]
        self.assertEqual(
            SocialEngine._own_style_texts(eng, "b1", "u1", NOW),
            ["聊天里的一句"],
        )


if __name__ == "__main__":
    unittest.main()
