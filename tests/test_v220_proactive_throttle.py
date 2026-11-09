"""v2.2.0：破冰/起话头「尝试节流」+ 安静时段守卫 + 门面 known=false。

v2.1.8 审计出的三条问题，改法横跨三处：state 记「试过」的时间戳、groupflow 的 due 闸门
把 try 时间算进冷却、engine 的轮转排序与门面合并。单看任何一个文件都验不出
「一个冷群还会不会每轮心跳重打一遍模型」，所以断言按**行为**写：

- 失败路径（模型决定不说 / 三次仍是问句）也要落 try 时间戳，但**不**消耗当天额度；
- 刚试过的群立刻再判 due 必须是 False，冷却过后才是 True；
- 两个都 due 的群：第一次挑 A，A 的 try_at 更新后第二次必须挑 B（否则就是饿死）；
- _quiet_span_intervals(start == end) 必须返回 []（否则安静时段一关，续聊顺延静默失效）；
- 门面 known=False / affection_source=config_default 时不能把配置初始值当好感。

构造方式沿用 tests/test_v218_engine.py：真 Engine 测状态与纯函数，SimpleNamespace 桩测
_icebreak 这种依赖一堆外部件的长函数。
"""
from __future__ import annotations

import asyncio
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
    api.logger = logging.getLogger("autonomous_social_v220_test")
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

NOW = 1_800_000_000.0
HOUR = 3600.0


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _engine(clock=None, **cfg) -> SocialEngine:
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


def _stub_icebreak(eng: SocialEngine, replies, *, topic_hint: str = ""):
    """给 _icebreak 用的最小桩：state 用**真**的（要验的就是它记的账）。"""
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
    stub.state = eng.state
    stub._m = {}
    stub._last_persona = {}
    stub.sent = []
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
        return list(replies[idx]) if replies[idx] else None

    stub.generator = types.SimpleNamespace(group_message=_group_message)
    stub.calls = calls
    return stub


def _run_icebreak(stub, *, mode: str = "icebreak", gid: str = "g1"):
    g = {"umo": "aiocqhttp:GroupMessage:" + gid, "samples": []}
    return asyncio.run(
        SocialEngine._icebreak(stub, "b1", gid, g, NOW, mode=mode)
    )


class AttemptRecordedOnFailureTest(unittest.TestCase):
    """核心修复：四条失败路径也必须落一次「尝试」时间，否则冷群永久 due。"""

    def test_model_says_nothing_still_records_attempt(self):
        eng = _engine()
        stub = _stub_icebreak(eng, [None])  # 模型决定这次不说
        self.assertFalse(_run_icebreak(stub), "模型说 NO 时本来就不该发")
        g = eng.state.group("b1", "g1")
        self.assertEqual(
            float(g.get("icebreak_try_at", 0) or 0), NOW,
            "失败也必须记「试过」——否则下一轮心跳又挑同一个群重打一遍模型",
        )
        self.assertEqual(float(g.get("icebreak_at", 0) or 0), 0.0, "失败不能算成发出去了")
        self.assertEqual(
            int(g.get("icebreak_count_day", 0) or 0), 0,
            "失败不该消耗当天的破冰额度（用户要的是「没话说就不发」）",
        )

    def test_three_questions_still_records_attempt(self):
        eng = _engine()
        stub = _stub_icebreak(eng, [["你们今天都干嘛呢？"]])
        self.assertFalse(_run_icebreak(stub))
        self.assertEqual(len(stub.calls), 3, "三次都问句，三次都真打了模型")
        g = eng.state.group("b1", "g1")
        self.assertEqual(float(g.get("icebreak_try_at", 0) or 0), NOW)
        self.assertEqual(int(g.get("icebreak_count_day", 0) or 0), 0)

    def test_topic_mode_records_topic_try_at(self):
        eng = _engine()
        stub = _stub_icebreak(eng, [None], topic_hint="我打算周末去爬山")
        self.assertFalse(_run_icebreak(stub, mode="topic"))
        g = eng.state.group("b1", "g1")
        self.assertEqual(float(g.get("topic_try_at", 0) or 0), NOW)
        self.assertEqual(
            float(g.get("icebreak_try_at", 0) or 0), 0.0,
            "起话头与破冰各记各的，不能互相串",
        )

    def test_success_still_records_both_attempt_and_send(self):
        """对照组：成功时 try_at 与 icebreak_at 一起更新，额度才 +1。"""
        eng = _engine()
        stub = _stub_icebreak(eng, [["早啊，我刚把窗户推开，风挺大"]])
        self.assertTrue(_run_icebreak(stub))
        g = eng.state.group("b1", "g1")
        self.assertEqual(float(g.get("icebreak_try_at", 0) or 0), NOW)
        self.assertEqual(float(g.get("icebreak_at", 0) or 0), NOW)
        self.assertEqual(int(g.get("icebreak_count_day", 0) or 0), 1)


class CooldownTest(unittest.TestCase):
    """刚试过的群必须被冷却挡住；冷却过后才允许再试。"""

    def _g(self, **kw):
        base = {
            "umo": "g",
            "msg_count": 20,
            "blocked_until": 0.0,
            "last_seen": NOW - 12 * HOUR,
            "last_bot_spoke": 0.0,
            "icebreak_at": 0.0,
            "topic_at": 0.0,
            "icebreak_try_at": 0.0,
            "topic_try_at": 0.0,
            "icebreak_day": "2026-10-06",
            "icebreak_count_day": 0,
            "topic_day": "",
            "topic_count_day": 0,
        }
        base.update(kw)
        return base

    def _icebreak(self, g, **kw):
        args = dict(
            idle_hours=1.0, daily_cap=2, stale_days=30,
            today="2026-11-08", is_quiet=False,
        )
        args.update(kw)
        return groupflow.icebreak_due(g, NOW, **args)

    def _topic(self, g, **kw):
        args = dict(
            gap_hours=1.0, daily_cap=2, stale_days=30,
            today="2026-11-08", is_quiet=False,
        )
        args.update(kw)
        return groupflow.group_topic_due(g, NOW, **args)

    def test_icebreak_just_tried_is_not_due(self):
        """原来会怎样：失败不写任何时间戳，60 秒后照样 due，又打一遍模型。"""
        self.assertTrue(self._icebreak(self._g()), "前置条件先成立")
        self.assertFalse(self._icebreak(self._g(icebreak_try_at=NOW - 60)))

    def test_icebreak_due_again_after_cooldown(self):
        # idle_hours=1 → 冷却 = max(600, 1*1800) = 1800 秒
        self.assertFalse(self._icebreak(self._g(icebreak_try_at=NOW - 1799)))
        self.assertTrue(self._icebreak(self._g(icebreak_try_at=NOW - 1801)))

    def test_topic_just_tried_is_not_due(self):
        self.assertTrue(self._topic(self._g()), "前置条件先成立")
        self.assertFalse(self._topic(self._g(topic_try_at=NOW - 60)))

    def test_topic_due_again_after_cooldown(self):
        # gap_hours 的单位是「小时」（见 _conf_schema.json）：1 小时 → 冷却 3600 秒。
        # 旧代码这里乘的是 1800，把「小时」当成了「半小时」，用户设 8 小时实际只冷却 4 小时。
        self.assertFalse(self._topic(self._g(topic_try_at=NOW - 3599)))
        self.assertTrue(self._topic(self._g(topic_try_at=NOW - 3601)))

    def test_topic_gap_hours_means_hours(self):
        """面板写「群内起话头间隔（小时）」，8 就该是 8 小时，不是 4 小时。"""
        self.assertFalse(self._topic(self._g(topic_try_at=NOW - 7 * HOUR), gap_hours=8.0))
        self.assertTrue(self._topic(self._g(topic_try_at=NOW - 8 * HOUR - 1), gap_hours=8.0))

    def test_try_timestamp_of_the_other_channel_also_counts(self):
        """破冰与起话头是「同一件事」：一边试过，另一边也该退避。"""
        self.assertFalse(self._icebreak(self._g(topic_try_at=NOW - 60)))
        self.assertFalse(self._topic(self._g(icebreak_try_at=NOW - 60)))

    def test_cooldown_floor_is_ten_minutes(self):
        """冷场阈值很小时也不能退化成「每轮都试」：下限 600 秒。"""
        g = self._g(icebreak_try_at=NOW - 300)
        self.assertFalse(self._icebreak(g, idle_hours=0.1))


class RotationTest(unittest.TestCase):
    """两个都 due 的群要轮着来，不能永远挑同一个（饿死）。"""

    class _Stub:
        _run_icebreaks = SocialEngine._run_icebreaks
        _run_group_topics = SocialEngine._run_group_topics

        def __init__(self, groups, *, mode):
            self.groups = groups
            self.mode = mode
            self.picked = []
            self.log = lambda *_a, **_k: None

        def _icebreak_pick(self, _bid, _now):
            return list(self.groups) if self.mode == "icebreak" else []

        def _group_topic_pick(self, _bid, _now):
            return list(self.groups) if self.mode == "topic" else []

        async def _icebreak(self, _bid, gid, g, now, *, mode="icebreak"):
            self.picked.append(gid)
            if mode == "topic":
                g["topic_try_at"] = now
            else:
                g["icebreak_try_at"] = now
            return False

    def _run(self, mode: str, rounds: int = 2):
        ga = {"last_seen": NOW - 20 * HOUR, "last_bot_spoke": NOW - 20 * HOUR,
              "icebreak_try_at": 0.0, "topic_try_at": 0.0}
        gb = {"last_seen": NOW - 10 * HOUR, "last_bot_spoke": NOW - 10 * HOUR,
              "icebreak_try_at": 0.0, "topic_try_at": 0.0}
        stub = self._Stub([("gA", ga), ("gB", gb)], mode=mode)
        runner = (
            SocialEngine._run_icebreaks if mode == "icebreak"
            else SocialEngine._run_group_topics
        )
        for _ in range(rounds):
            asyncio.run(runner(stub, ["b1"], NOW))
        return stub.picked

    def test_icebreak_rotates_to_the_next_group(self):
        picked = self._run("icebreak")
        self.assertEqual(picked, ["gA", "gB"], f"没轮转就是饿死：{picked}")

    def test_topic_rotates_to_the_next_group(self):
        picked = self._run("topic")
        self.assertEqual(picked, ["gA", "gB"], f"没轮转就是饿死：{picked}")


class QuietSpanGuardTest(unittest.TestCase):
    """安静时段 start == end = 未启用；缺守卫时会被当成整天。"""

    def test_start_equals_end_returns_empty(self):
        eng = _engine()
        self.assertEqual(
            eng._quiet_span_intervals(NOW, NOW + 86400, 0.0, 0.0, 0), [],
            "start == end 是「没开安静时段」，不是跨零点的 24 小时窗口",
        )

    def test_intersection_with_disabled_window_is_zero(self):
        self.assertEqual(
            SocialEngine._quiet_intersection_seconds(
                NOW, NOW + 86400, (0.0, 0.0, 0), (23.0, 7.0, 0)
            ),
            0.0,
            "关闭的窗口不该吞掉 Core 的夜窗——否则「续聊按安静时段顺延」静默失效",
        )

    def test_normal_window_still_works(self):
        eng = _engine()
        spans = eng._quiet_span_intervals(NOW, NOW + 86400, 23.0, 7.0, 0)
        self.assertTrue(spans, "23–7 是真窗口，必须切出区间")
        overlap = SocialEngine._quiet_intersection_seconds(
            NOW, NOW + 86400, (23.0, 7.0, 0), (22.0, 6.0, 0)
        )
        self.assertGreater(overlap, 0.0, "两个真窗口的交集不能因为新守卫变成 0")


class CoreBriefKnownTest(unittest.TestCase):
    """Core v3：known=false 时好感/欲望是配置初始值，不是关于他的事实。"""

    def _brief(self, **user):
        base = {
            "known": True,
            "affection": 50.0,
            "base_affection": 50.0,
            "libido": 25.0,
            "known_days": 3.0,
            "nickname": "小明",
            "attention": {"care": 0.7, "focus": 0.2, "spare": 0.1},
        }
        base.update(user)
        return {"user": base}

    def _apply(self, brief):
        target = {}
        SocialEngine._apply_core_brief(target, brief, None, {})
        return target

    def test_unknown_user_skips_affection_like_fields(self):
        target = self._apply(self._brief(known=False))
        for key in (
            "_affection", "_affection_base", "_attention", "_attention_block",
            "_libido", "_known_days",
        ):
            self.assertNotIn(key, target, f"未建档的人不该有 {key}")
        self.assertEqual(target.get("_nickname"), "小明", "称呼是结构事实，照旧采用")

    def test_config_default_source_also_skips(self):
        target = self._apply(self._brief(affection_source="config_default"))
        self.assertNotIn("_affection", target)

    def test_known_user_still_applies(self):
        target = self._apply(self._brief())
        self.assertEqual(target["_affection"], 50.0)
        self.assertEqual(target["_affection_base"], 50.0)
        self.assertEqual(target["_libido"], 25.0)
        self.assertEqual(target["_known_days"], 3.0)
        self.assertEqual(target["_attention"], 0.7)

    def test_old_core_without_known_field_behaves_as_before(self):
        target = self._apply({"user": {"affection": 62.0}})
        self.assertEqual(
            target["_affection"], 62.0,
            "老 Core（v2 及更早）没有 known 字段，必须按已知处理，行为不变",
        )


if __name__ == "__main__":
    unittest.main()
