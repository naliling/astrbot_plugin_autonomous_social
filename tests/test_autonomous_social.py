"""自主拟人社交插件的回归测试。

    python3 -m unittest discover -s tests -v

这些用例锁住的是 2026-09-26 那轮整改里六个「阻断级」缺陷的修复点：
它们全都是「静态读代码看不出来、跑起来才会发作」的那类问题，所以必须有测试钉住，
否则下一次改念头模型或发送路径时很容易被改回去。
"""

import asyncio
import json
import os
import sys
import logging
import tempfile
import types
import unittest
from unittest import TestCase as _TC
from pathlib import Path

# 插件根目录。两种摆法都认：
#   · 开发时：tests/ 在工作区根，插件在旁边 → parent.parent / "astrbot_plugin_autonomous_social"
#   · 打包后：tests/ 就在插件里面            → parent.parent 本身就是插件根
#     （Core 那边 473 项测试是随包发布的，social 这边也改成一样，所以两种摆法都得能跑）
_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent                     # tests/ 就在插件里
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


# ─── 最小 astrbot 桩 ──────────────────────────────────────────────
# 容器里没有 astrbot 包，而 social/ 下每个模块都 `from astrbot.api import logger`。
class _FakeMessageChain:
    """真实环境里 AstrBot 总会提供 MessageChain；它决定发送走链式还是纯文本分支。"""

    def message(self, text):
        return ("chain", text)


def _install_stub(message_chain=_FakeMessageChain):
    import logging

    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = message_chain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import (  # noqa: E402
    desire,
    anchors,
    history_ingest,
    memory_bridge,
    sanitize,
    verify as verify_module,
    generator,
    groupflow,
    reasoning,
    style_profile,
    threads,
)
from social.config import SocialConfig  # noqa: E402
from social import engine as engine_mod  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social import anchors as anchors_mod  # noqa: E402
from social import generator as generator_mod  # noqa: E402
from social.anchors import relation_tier, tier_note  # noqa: E402
from social.signals import SignalsWriter  # noqa: E402
from social.state import SocialState  # noqa: E402
from social.throttle import LogThrottle  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ─── 阻断①：念头天花板低于随机门槛导致自锁 ──────────────────────────

class TestFireGateDeadlock(unittest.TestCase):
    """streak 高时念头被夹在天花板上，而门槛是每次说完话重抽的随机值。

    两者一旦脱节（cap 在下、gate 在上），念头永远够不着门槛；而 gate 只在发送
    成功后才重抽，于是谁也发不出去。旧逻辑在 streak>=3 时约 70% 的组合锁死。
    """

    def test_never_unreachable_regardless_of_random_gate(self):
        for streak in range(0, 8):
            for i in range(400):
                gate = round(
                    desire.FIRE_THRESHOLD + desire.FIRE_GATE_SPAN * (i / 399.0), 3
                )
                u = {"no_reply_streak": streak, "fire_gate": gate, "urge": 99.0}
                cap = desire.urge_cap(u, has_live_cue=False)
                urge = min(u["urge"], cap)          # engine 里的 cap 夹紧
                threshold = desire.effective_gate(u, cap)
                self.assertGreaterEqual(
                    urge, threshold,
                    f"streak={streak} gate={gate} 时念头够不着门槛（自锁）",
                )

    def test_randomness_preserved_when_cap_is_high(self):
        """天花板高于门槛上界时，随机抖动仍然调节「等多久」，不能被压平成常量。"""
        u = {"no_reply_streak": 0, "fire_gate": desire.FIRE_THRESHOLD + 0.5}
        cap = desire.urge_cap(u, has_live_cue=False)
        self.assertAlmostEqual(
            desire.effective_gate(u, cap), desire.FIRE_THRESHOLD + 0.5, places=3
        )

    def test_streak_decays_from_last_active_contact_not_last_chat(self):
        """对方只是正常聊天、没回主动消息时，冷落计数也必须能衰减。

        锚点若取 max(last_sent, last_seen)，对方每次说话都会把它刷新，steps 恒为 0，
        于是这个人一旦被冷落够次数就再也回不到正常节奏。
        """
        day = 86400.0
        now = 1000 * day
        u = {
            "no_reply_streak": 4,
            "last_sent": now - 4 * day,   # 四天前我主动发过
            "last_seen": now - 60,       # 对方一分钟前刚说过话
        }
        desire.decay_streak(u, now)
        self.assertLess(u["no_reply_streak"], 4, "对方持续聊天不该冻结冷落计数")


# ─── 阻断②：由头/回访无限续命 ──────────────────────────────────────

class TestCueExpiry(unittest.TestCase):
    """推迟「重试」不能顺带刷新「寿命」。

    旧实现推的是 cue_due，而作废判据正是 now-due > 36h：每推一次就把「迟了多久」
    清零，同一件事可以每 6 小时重试一次、永不作废。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = SocialState(os.path.join(self.tmp, "state.json"))
        self.clock = 1_000_000.0
        self.engine = _FakeEngine(clock_ref=lambda: self.clock)

    def test_expire_at_anchored_and_not_drifted_by_retries(self):
        u = {}
        SocialState._note_cue(u, "明天面试", self.clock + 10 * 3600, self.clock)
        due0, expire0 = u["cue_due"], u["cue_expire_at"]
        for _ in range(2):
            self.engine.defer_anchor(u, "cue")
            self.clock = u["cue_retry_at"]
            self.assertEqual(u["cue_expire_at"], expire0, "寿命不该被重试推迟")
            self.assertEqual(u["cue_due"], due0, "锚点不该被重试改写")

    def test_cue_dies_after_lifetime(self):
        u = {}
        SocialState._note_cue(u, "明天面试", self.clock + 10 * 3600, self.clock)
        u["cue_retry_at"] = 0.0
        self.clock = u["cue_expire_at"] + 1
        self.assertIsNone(reasoning.live_cue(u, self.clock), "过期的由头仍被当成可用")

    def test_retry_backoff_blocks_immediate_resend(self):
        u = {}
        SocialState._note_cue(u, "明天面试", self.clock + 10 * 3600, self.clock)
        self.engine.defer_anchor(u, "cue")
        self.assertIsNone(reasoning.live_cue(u, self.clock + 60), "退避期内不该再被捡起来")

    def test_gives_up_after_max_tries(self):
        u = {}
        SocialState._note_cue(u, "明天面试", self.clock, self.clock)
        tries = 0
        while u.get("cue") and tries < 50:
            self.engine.defer_anchor(u, "cue")
            self.clock = u.get("cue_retry_at", self.clock)
            tries += 1
        self.assertEqual(u.get("cue"), "", "同一由头必须有次数上限，否则会无限重发")
        self.assertLessEqual(tries, 10, "上限没生效")

    def test_loop_anchor_gets_same_protection(self):
        u = {}
        SocialState._note_loop(u, "今天面试了", self.clock, self.clock)
        for _ in range(10):
            self.engine.defer_anchor(u, "loop")
            if not u.get("loop"):
                break
            self.clock = u["loop_retry_at"]
        self.assertEqual(u.get("loop"), "")


class _FakeEngine:
    """只需要 engine 的 defer / 指标行为时用的最小壳。"""

    def __init__(self, clock_ref, cfg=None):
        self._clock_ref = clock_ref
        self.state = _NullState()
        self.cfg = cfg or SocialConfig()

    def _time(self):
        return self._clock_ref()

    defer_anchor = SocialEngine._defer_anchor
    _min_gap_left = SocialEngine._min_gap_left
    _gap_left = SocialEngine._gap_left
    _quiesced = SocialEngine._quiesced
    _miss_floor = SocialEngine._miss_floor
    _metrics_text = SocialEngine._metrics_text
    _urge_is_eager = staticmethod(SocialEngine._urge_is_eager)
    _defer_loop = SocialEngine._defer_loop
    _drop_loop = staticmethod(SocialEngine._drop_loop)


class _NullState:
    def mark_dirty(self):
        pass


# ─── 阻断③：状态文件读不出来被静默清空并覆盖 ────────────────────────

class TestStateLoadFailure(unittest.TestCase):
    """读不出原来的记忆时，必须备份 + 报错 + 拒绝写盘，绝不能拿空状态盖回去。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state.json")

    def _make(self, name, write):
        p = os.path.join(self.tmp, name)
        write(p)
        st = SocialState(p)
        st.user("bot1", "u1")["last_message"] = "新消息"
        st.mark_dirty()
        st.flush()
        return st, p

    def test_valid_file_loads_normally(self):
        def write(p):
            with open(p, "w") as f:
                json.dump({"version": 2, "bots": {"bot1": {"users": {"u1": {}}}}}, f)
        st, p = self._make("ok.json", write)
        self.assertFalse(st._load_failed)
        self.assertTrue(os.path.exists(p))

    def test_broken_json_is_backed_up_and_blocks_writes(self):
        def write(p):
            open(p, "w").write('{"bots": {"bot1": ')
        st, p = self._make("broken.json", write)
        self.assertTrue(st._load_failed, "损坏文件必须上闸门")
        self.assertFalse(os.path.exists(p), "原文件已被备份，不该被空状态覆盖")
        backups = [f for f in os.listdir(self.tmp) if f.startswith("broken.json.unreadable")]
        self.assertTrue(backups, "必须留下可恢复的备份")

    def test_non_utf8_is_treated_as_unreadable(self):
        """UnicodeDecodeError 是 ValueError 子类，不会被 JSONDecodeError 捕获。"""
        def write(p):
            open(p, "wb").write(b'{"bots": {"b": {"n": "\xff\xfe"}}}')
        st, _p = self._make("bin.json", write)
        self.assertTrue(st._load_failed)

    def test_unexpected_shape_is_not_treated_as_empty(self):
        def write(p):
            with open(p, "w") as f:
                json.dump({"version": 1, "data": {}}, f)
        st, p = self._make("shape.json", write)
        self.assertTrue(st._load_failed)
        self.assertFalse(os.path.exists(p), "结构不对时同样不能被空状态覆盖")

    def test_migration_result_is_not_discarded(self):
        def write(p):
            with open(p, "w") as f:
                json.dump(
                    {"version": 2, "bots": {"bot1": {"users": {"u1": {"followup_for": 9.0}}}}},
                    f,
                )
        p = os.path.join(self.tmp, "mig.json")
        write(p)
        st = SocialState(p)
        self.assertTrue(st._dirty, "迁移改了数据却没标脏，下次启动会重跑一遍")
        st.flush()
        with open(p) as f:
            disk = json.load(f)
        self.assertNotIn("followup_for", disk["bots"]["bot1"]["users"]["u1"])

    def test_dirty_timestamp_does_not_break_outgoing(self):
        """state.json 里一条脏 ts 不该把整个发送流程抛穿（抛出去会作废整轮心跳）。"""
        st = SocialState(os.path.join(self.tmp, "d.json"))
        u = st.user("bot1", "u1")
        u["proactive_log"] = [{"ts": "2025/1/1", "text": "坏数据"}]
        st.record_outgoing("bot1", "u1", "你好", "share_thought")  # 不应抛异常


# ─── 阻断④：模型不按协议作答时把犹豫文字当正文发出 ──────────────────

class TestDecisionProtocol(unittest.TestCase):
    """decide 路径必须只认 SEND/NO。没给协议就把整段当正文发出去，
    等于把「我觉得现在不太合适」这种思考过程原样发给用户。"""

    def setUp(self):
        self.cfg = SocialConfig()
        self.parse = generator.MessageGenerator._parse_decision

    def _parse(self, text):
        return self.parse(text, 120, self.cfg)

    def test_prose_answer_is_never_sent(self):
        for text in (
            "我觉得现在不太合适",
            "我想了想\n还是算了",
            "OK啦今天真累",
            "抱歉我不太想发消息",
        ):
            d = self._parse(text)
            self.assertIsNotNone(d, f"{text!r} 应该被判为协议违例而不是 None")
            self.assertFalse(d.send, f"{text!r} 的犹豫文字被当正文发出来了")

    def test_send_with_body_still_works(self):
        for text, expect in (
            ("SEND\n今天好累啊", "今天好累啊"),
            ("SEND: 刚下班", "刚下班"),
            ("SEND：刚下班", "刚下班"),
            ("好的，我想想\nSEND\n今天好累", "今天好累"),
            ("<think>想了一下</think>\nSEND\n晚安", "晚安"),
        ):
            d = self._parse(text)
            self.assertTrue(d and d.send, f"{text!r} 应该发送")
            self.assertIn(expect, d.parts[0])

    def test_send_without_body_is_reported_as_protocol_issue(self):
        d = self._parse("SEND")
        self.assertIsNotNone(d)
        self.assertFalse(d.send)
        self.assertIn("SEND", d.why_not)
        self.assertNotIn("LLM", d.why_not, "协议问题不该被归因成 LLM 不可用")

    def test_no_still_works(self):
        for text in ("NO", "不发", "NO，刚聊过"):
            d = self._parse(text)
            self.assertFalse(d.send, f"{text!r} 应该判为不发")

    def test_generation_path_is_unaffected(self):
        """generate()/icebreak 不走协议解析，普通文本必须原样保留。"""
        split = generator.MessageGenerator._split_parts
        self.assertEqual(split("OK啦今天真累", 120, self.cfg), ["OK啦今天真累"])


# ─── 阻断⑤：LLM 调用挂住 / 异常被吞 / 无限重试 ──────────────────────

class TestLLMCall(unittest.TestCase):
    def setUp(self):
        self._orig_timeout = generator.LLM_TIMEOUT_SECONDS
        generator.LLM_TIMEOUT_SECONDS = 0.5

    def tearDown(self):
        generator.LLM_TIMEOUT_SECONDS = self._orig_timeout

    def test_hanging_provider_does_not_block(self):
        class Hanging:
            id = "hang"

            async def text_chat(self, **kw):
                await asyncio.sleep(60)

        gen = generator.MessageGenerator(_PlainCtx())
        result = _run(gen._call_llm(Hanging(), "prompt"))
        self.assertIsNone(result, "挂住的调用必须被超时放弃，而不是把主循环一起拖死")

    def test_exception_reason_is_preserved(self):
        class Boom:
            id = "b"

            async def text_chat(self, **kw):
                raise ValueError("401 Unauthorized: invalid api key")

        gen = generator.MessageGenerator(_PlainCtx())
        _run(gen._call_llm(Boom(), "p"))
        self.assertEqual(sum(gen._llm_fail_streak.values()), 1)
        text, err = generator._response_text(ValueError("401 Unauthorized: invalid api key"))
        self.assertIsNone(text)
        self.assertIn("401", err, "真实原因不能被覆盖成「返回非文本」")

    def test_role_err_is_attributed(self):
        text, err = generator._response_text(_Resp("", role="err"))
        self.assertIsNone(text)
        self.assertIn("role=err", err)

    def test_backoff_after_repeated_failures(self):
        class Boom:
            id = "b"

            async def text_chat(self, **kw):
                raise RuntimeError("boom")

        gen = generator.MessageGenerator(_PlainCtx())
        for _ in range(3):
            _run(gen._call_llm(Boom(), "p"))
        self.assertGreater(gen.llm_backoff_remaining(), 0, "连续失败后必须退避")
        gen._note_llm_ok("b")
        self.assertEqual(gen.llm_backoff_remaining(), 0.0, "一次成功就该解除退避")

    def test_compose_skips_while_backing_off(self):
        class Boom:
            id = "b"

            async def text_chat(self, **kw):
                raise RuntimeError("boom")

        gen = generator.MessageGenerator(_PlainCtx())
        for _ in range(3):
            _run(gen._call_llm(Boom(), "p"))
        prepared = _run(
            gen._compose("umo", {}, "ctx", "reason", None, "", None, decide=True)
        )
        self.assertIsNone(prepared, "退避期内不该再烧 token")


class _PlainCtx:
    """没有任何 AstrBot 方法的 context（取不到 provider，正是 LLM 失败用例要的环境）。"""


class _ProviderCtx:
    """能取到 provider 的 context，用来检查提示词内容。"""

    class _P:
        id = "p1"

        async def text_chat(self, **kw):
            return _Resp("NO")

    async def get_using_provider_async(self, umo=None):
        return self._P()


class _Resp:
    def __init__(self, text="", role="assistant"):
        self.completion_text = text
        self.role = role


# ─── 阻断⑥：发送重复投递 + 平台故障被当成「对方不可达」 ──────────────

class TestSendDelivery(unittest.TestCase):
    """重试间隔是真实等待，会把测试拖慢；这里压到 0，只测语义不测等待。"""

    def setUp(self):
        self._orig_delay = engine_mod.SEND_RETRY_DELAY
        engine_mod.SEND_RETRY_DELAY = 0.0

    def tearDown(self):
        engine_mod.SEND_RETRY_DELAY = self._orig_delay

    def _engine(self, ctx):
        return _SendEngine(ctx)

    def test_delivered_but_timeout_is_not_resent(self):
        """请求可能已经送达，只是回执没回来。再发一次就是给同一个人发两条。"""
        class Ctx:
            def __init__(self):
                self.calls = 0

            async def send_message(self, target, payload):
                self.calls += 1
                raise asyncio.TimeoutError("回执超时")

        ctx = Ctx()
        ok, _err, peer = _run(self._engine(ctx)._send_with_retry("umo", "你好"))
        self.assertEqual(ctx.calls, 1, f"发送被调用了 {ctx.calls} 次（叠加重试最多 6 条）")
        self.assertFalse(ok)
        self.assertFalse(peer)

    def test_platform_returning_false_does_not_block_user(self):
        """框架只说「没发出去」，没说是谁的問題，不能据此把用户隔离 24 小时。"""
        class Ctx:
            async def send_message(self, target, payload):
                return False

        ok, _err, peer = _run(self._engine(Ctx())._send_with_retry("umo", "x"))
        self.assertFalse(peer, "平台级失败被误判成「这个人发不了」")

    def test_platform_saying_not_friend_does_block_user(self):
        class Ctx:
            async def send_message(self, target, payload):
                raise RuntimeError("发送失败：请添加对方为好友")

        _ok, _err, peer = _run(self._engine(Ctx())._send_with_retry("umo", "x"))
        self.assertTrue(peer, "平台明说不是好友时应当隔离")

    def test_platform_capability_error_does_not_block_user(self):
        for msg in ("该平台不支持主动消息，如 qq_official", "session expired"):
            class Ctx:
                async def send_message(self, target, payload):
                    raise RuntimeError(msg)

            _ok, err, peer = _run(self._engine(Ctx())._send_with_retry("umo", "x"))
            self.assertFalse(peer, f"{msg!r} 属平台级问题，不该隔离用户")
            self.assertTrue(SocialEngine._platform_problem(err), f"{msg!r} 应识别为平台问题")

    def test_construction_failure_still_falls_back_to_text(self):
        """MessageChain 构造失败是真的没发出去，可以回退重试。"""
        class Ctx:
            def __init__(self):
                self.calls = 0

            async def send_message(self, target, payload):
                self.calls += 1
                if isinstance(payload, tuple):
                    raise TypeError("不接受 MessageChain")
                return True

        ctx = Ctx()
        ok, _err, _peer = _run(self._engine(ctx)._send_with_retry("umo", "x"))
        self.assertTrue(ok)
        self.assertEqual(ctx.calls, 2, "链式失败后应回退纯文本")


class _MetricsEngine:
    """dry-run 用得到的那几个依赖。"""

    dry_run = SocialEngine.dry_run
    _persist_metrics = SocialEngine._persist_metrics
    settle_minds = SocialEngine.settle_minds
    gate_reason = SocialEngine.gate_reason

    def __init__(self):
        import tempfile as _tf
        self.cfg = SocialConfig()
        self.state = SocialState(os.path.join(_tf.mkdtemp(), "s.json"),
                                  time_source=lambda: 1_700_000_000.0)
        self.core = _NullCore()
        self._m = {}
        self.calls = []
        self.sends = []
        for uid in ("u1", "u2"):
            self.state.user("bot1", uid).update(
                {"umo": f"aiocqhttp:FriendMessage:{uid}", "urge": 2.5, "interest": 0.7})

    def _time(self):
        return 1_700_000_000.0

    def _moment_of(self, bid, now):
        import datetime
        return datetime.datetime(2023, 11, 15, 14, 0)

    def log(self, m):
        pass


class _SendEngine:
    _send_message = SocialEngine._send_message
    _send_with_retry = SocialEngine._send_with_retry
    _is_unreachable_error = staticmethod(SocialEngine._is_unreachable_error)
    _platform_problem = staticmethod(SocialEngine._platform_problem)
    # 熔断记账（v2.24.0 随 _send_with_retry 一起调用的）
    _note_send_failure = SocialEngine._note_send_failure
    _note_send_ok = SocialEngine._note_send_ok
    send_breaker_left = SocialEngine.send_breaker_left
    _breaker_slot = SocialEngine._breaker_slot
    _hourly_left = SocialEngine._hourly_left
    _hourly_take = SocialEngine._hourly_take

    def __init__(self, ctx):
        self.context = ctx
        self._send_breaker = {}
        self._hourly = {}
        self.cfg = SocialConfig()

    def _time(self):
        return 1000.0


# ─── 配置：默认值只有一个出处 ──────────────────────────────────────

class TestConfigSingleSource(unittest.TestCase):
    def test_defaults_come_from_schema(self):
        """面板显示的、新装用户拿到的都是 schema 的值。

        曾经 schema 与代码常量差 16 项，作者调了三天的节奏对新装用户一次都没生效。
        """
        with open(PLUGIN_ROOT / "_conf_schema.json", encoding="utf-8") as f:
            schema = json.load(f)
        cfg = SocialConfig.from_astrbot({})   # 空配置：全部走默认值
        for key, spec in schema.items():
            if not isinstance(spec, dict) or "default" not in spec:
                continue
            expected = spec["default"]
            if expected is None:
                continue
            actual = getattr(cfg, key, None)
            if isinstance(expected, bool):
                self.assertIs(actual, expected, f"{key}: 默认值不一致")
            elif isinstance(expected, (int, float)):
                self.assertAlmostEqual(
                    float(actual), float(expected), msg=f"{key}: 默认值不一致"
                )
            else:
                self.assertEqual(str(actual), str(expected), f"{key}: 默认值不一致")

    def test_heartbeat_range_survives_swapped_config(self):
        """面板里两个值填反了，不该变成 random.randint 抛异常打死后台循环。"""
        lo, hi = _heartbeat_range({"heartbeat_min_minutes": 15, "heartbeat_max_minutes": 8})
        self.assertLessEqual(lo, hi)
        lo, hi = _heartbeat_range({"heartbeat_min_minutes": 0, "heartbeat_max_minutes": 0})
        self.assertGreaterEqual(lo, 60, "下限必须有硬底，不能变成忙循环")

    def test_no_dead_or_fake_options(self):
        cfg = SocialConfig.from_astrbot({})
        self.assertFalse(hasattr(cfg, "global_cooldown_minutes"), "死配置还在")
        with open(PLUGIN_ROOT / "social" / "config.py", encoding="utf-8") as f:
            self.assertNotIn("humanoid", f.read().split("VALID_MODES")[1].split("\n")[0])

    def test_legacy_check_does_not_rewrite_user_config(self):
        """旧默认值只提示，不改写：无法区分「没动过」与「主动就想要这个值」。"""
        from social.config import migrate_legacy_defaults

        cfg = {"max_message_length": 60, "activity_level": 55}

        class _Cfg(dict):
            def __init__(self, d):
                super().__init__(d)
                self.saved = False

            def save_config(self):
                self.saved = True

        c = _Cfg(cfg)
        hints = migrate_legacy_defaults(c, tempfile.mkdtemp(), "test-version")
        self.assertEqual(cfg["max_message_length"], 60, "用户的值被插件擅自改了")
        self.assertEqual(cfg["activity_level"], 55)
        self.assertFalse(c.saved)
        self.assertTrue(hints, "应该提示有哪些项停在旧默认值上")


def _heartbeat_range(overrides):
    cfg = SocialConfig.from_astrbot(overrides)
    engine = _RangeEngine(cfg)
    return engine._heartbeat_range()


class _RangeEngine:
    _heartbeat_range = SocialEngine._heartbeat_range

    def __init__(self, cfg):
        self.cfg = cfg


# ─── 其它回归 ──────────────────────────────────────────────────────

class TestGroupQuietHours(unittest.TestCase):
    """群心流是事件驱动的，安静时段与睡眠闸门漏了就等于深夜照样插话。"""

    NOW = 1_700_000_000.0

    def _group(self):
        return {
            "flow_open_until": self.NOW + 600,
            "flow_replies": 0,
            "flow_last_reply_at": self.NOW - 600,
            "flow_ignored": 0,
        }

    def test_quiet_hours_blocks(self):
        ok, why = groupflow.flow_should_consider(
            self._group(), self.NOW, max_replies=3, min_gap_seconds=45,
            hourly_cap=6, hour_count=0, ignored_exit=2, is_quiet=True,
        )
        self.assertFalse(ok)
        self.assertIn("安静", why)

    def test_asleep_blocks(self):
        ok, why = groupflow.flow_should_consider(
            self._group(), self.NOW, max_replies=3, min_gap_seconds=45,
            hourly_cap=6, hour_count=0, ignored_exit=2, asleep=True,
        )
        self.assertFalse(ok)
        self.assertIn("睡", why)

    def test_normal_case_still_passes(self):
        ok, _why = groupflow.flow_should_consider(
            self._group(), self.NOW, max_replies=3, min_gap_seconds=45,
            hourly_cap=6, hour_count=0, ignored_exit=2,
        )
        self.assertTrue(ok)


class TestGroupSamples(unittest.TestCase):
    """群样本缓冲必须有固定上限；隐私开关要连 bot 自己的发言一起管住。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = SocialState(os.path.join(self.tmp, "s.json"))
        self.now = 1_700_000_000.0

    def test_samples_are_capped(self):
        for i in range(200):
            self.state.note_flow_reply("b", "g", self.now + i, f"插话{i}", sample_cap=30)
        n = len(self.state.group("b", "g").get("samples") or [])
        self.assertLessEqual(n, 40, f"样本涨到 {n} 条：上限被当成「当前长度」了，永不裁剪")

    def test_privacy_switch_covers_bot_own_text(self):
        self.state.note_flow_reply("b", "g1", self.now, "心流插话", store_text=False)
        self.state.record_group_self_text("b", "g1", "连发续句", self.now, store_text=False)
        self._record_group("g1", "bot 说的话", is_bot=True, store=False)
        self._record_group("g1", "群友说的话", is_bot=False, store=False)
        self.assertEqual(
            len(self.state.group("b", "g1").get("samples") or []), 0,
            "group_store_message_text=false 时不该有任何正文落盘（含 bot 自己说的）",
        )

    def _record_group(self, gid, text, is_bot, store):
        self.state.record_group_message(
            "b", gid, "umo", "群名", "bot" if is_bot else "路人", text, is_bot,
            self.now, store_text=store,
        )


class TestClockRollback(unittest.TestCase):
    """墙钟回拨时 `now - last_sent` 会变负，所有冷却同时成立 → 永久静默。"""

    def test_time_never_goes_backwards(self):
        clock = [1_700_000_000.0]
        engine = _ClockEngine(lambda: clock[0])
        self.assertEqual(engine._time(), 1_700_000_000.0)
        clock[0] += 600
        self.assertEqual(engine._time(), 1_700_000_600.0)
        clock[0] -= 7200                      # NTP 回拨两小时
        self.assertEqual(engine._time(), 1_700_000_600.0, "时间倒退了")
        self.assertGreaterEqual(engine._time() - (1_700_000_600.0 - 300), 0)


class _ClockEngine:
    _time = SocialEngine._time

    def __init__(self, source):
        self._time_source = source
        self._time_high = 0.0


class TestIntimateDetection(unittest.TestCase):
    """「裸辞」「价格敏感」「舔了舔嘴唇」不该被当成亲密对话而清空全部上下文。"""

    def test_daily_text_is_not_intimate(self):
        for text in ("我准备裸辞了", "这价格太敏感了", "他舔了舔嘴唇", "下面还有什么", "我没穿外套"):
            self.assertFalse(generator._looks_intimate(text), f"{text!r} 被误判为亲密")

    def test_explicit_text_is_intimate(self):
        for text in ("你把内裤脱了", "她全裸着", "我们做爱吧", "大腿内侧很敏感", "舔舐"):
            self.assertTrue(generator._looks_intimate(text), f"{text!r} 漏判了")


class TestIntimacyChannelGate(unittest.TestCase):
    """「想要了」通道：必须真的做过色色（ccb_done_count>0）且此刻欲望涨上来。

    原来只对白名单里的人开放、且「有过亲密史 或 欲望涨上来」二者其一即可——
    于是只要有过一次就会一直问。现在填 ID 不够、好感高也不够、有亲密史也不免检。
    """

    def _engine(self, done, libido, base_libido, affection=80.0):
        return _IntimacyEngine({
            ("bot1", "u1"): {
                "affection": affection,
                "mood": {"libido": libido, "base_libido": base_libido},
                "ccb_done_count": done,
            }
        })

    def _pick(self, engine):
        return [uid for uid, _u in engine._intimacy_pick("bot1", engine._time(), {}, {"stub": True})]

    def test_requires_actual_history(self):
        e = self._engine(done=0, libido=45.0, base_libido=34.0)
        self.assertEqual(self._pick(e), [], "没真做过色色，欲望再高也不该问")

    def test_requires_current_desire(self):
        e = self._engine(done=3, libido=34.0, base_libido=34.0)
        self.assertEqual(self._pick(e), [], "有过亲密史也不免检，欲望没涨就不该问")

    def test_picks_when_both_met(self):
        e = self._engine(done=1, libido=45.0, base_libido=34.0)
        self.assertEqual(self._pick(e), ["u1"])

    def test_affection_gate_still_applies(self):
        e = self._engine(done=1, libido=45.0, base_libido=34.0, affection=10.0)
        self.assertEqual(self._pick(e), [])


class TestModeNote(unittest.TestCase):
    """mode_note 在字段缺失时会渲染出「你自己上一句说的是：「」。」这种空引号。"""

    def test_no_empty_quotes(self):
        for mode, about, asked in (
            ("presence", "之前聊的那件事", ""),
            ("presence", "", "在吗"),
            ("probe", "那家店", ""),
            ("loop", "面试", ""),
            ("closer", "", "你睡了吗"),
        ):
            out = generator._mode_note(mode, about, asked)
            self.assertNotIn("「」", out, f"{mode} 渲染出了空引号：{out!r}")


class TestPromptNotContradictingGates(unittest.TestCase):
    """引擎闸门已放行之后，prompt 不该再列一遍「下面这些情况就别说」。"""

    def test_decide_prompt_has_no_veto_list(self):
        gen = generator.MessageGenerator(_ProviderCtx(), SocialConfig())
        prompt = _run(
            gen._compose("umo", {"conversation": []}, "ctx", "reason", None, "", None, decide=True)
        )
        self.assertIsNotNone(prompt, "退避或 provider 不可用时跳过是正常的，跳过本用例")
        _provider, text, _max_len, _emoji = prompt
        for veto in ("就别说", "很突兀", "别发", "不要发"):
            self.assertNotIn(veto, text, f"decide 提示词里仍有劝退条款：{veto}")


# ─── 手动触发：先验引擎在不在跑，失败要回退到下一个人 ────────────────

class TestManualTrigger(unittest.TestCase):
    """`/触发社交` 原来只检查 enabled，不检查引擎是否真的在跑。

    enabled=true 只说明「允许发」：插件刚重载、正在停机、或主循环已抛异常退出时，
    它仍然是 true。于是「插件已经不在正常工作」的时候手动触发照样把一整轮状态
    推着往前走——念头结算掉、冷却记上、结果还是发不出去。
    """

    def _engine(self, running=True, ranked=None, speak=None):
        return _TriggerEngine(running=running, ranked=ranked or [], speak=speak)

    def test_refuses_when_engine_not_running(self):
        ok, why = _run(self._engine(running=False)._trigger_precheck())
        self.assertFalse(ok)
        self.assertIn("没有在运行", why)

    def test_refuses_when_disabled(self):
        e = self._engine()
        e.cfg.enabled = False
        ok, why = _run(e._trigger_precheck())
        self.assertFalse(ok)
        self.assertIn("停用", why)

    def test_refuses_while_llm_backing_off(self):
        e = self._engine()
        e.generator._llm_skip_until['p1'] = e._time() + 180.0
        ok, why = _run(e._trigger_precheck())
        self.assertFalse(ok)
        self.assertIn("退避", why)

    def test_passes_when_healthy(self):
        ok, why = _run(self._engine()._trigger_precheck())
        self.assertTrue(ok, why)

    def test_falls_back_to_next_candidate_on_send_failure(self):
        ranked = _ranked()
        e = self._engine(ranked=ranked, speak=_speak({"u1": (False, "发送失败：网络不通")}))
        out = _run(e.trigger_once("bot1"))
        self.assertIn("u2", out, "第一个发不出去时应该自动换下一个人")
        self.assertIn("没轮到", out)

    def test_falls_back_when_first_candidate_raises(self):
        ranked = _ranked()

        async def speak(uid):
            if uid == "u1":
                raise RuntimeError("模型崩了")
            return True, f"发给 {uid} 了"

        out = _run(self._engine(ranked=ranked, speak=speak).trigger_once("bot1"))
        self.assertIn("u2", out, "单个人抛异常不该让整次触发报废")

    def test_reports_all_attempts_when_everyone_fails(self):
        ranked = _ranked()
        everyone = {uid: (False, "发送失败：网络不通") for uid, _u, _g in ranked}
        e = self._engine(ranked=ranked, speak=_speak(everyone))
        out = _run(e.trigger_once("bot1"))
        self.assertIn("没发出去", out)
        self.assertIn("u3", out, "三个候选都该被试到")


def _ranked():
    now = 1_700_000_000.0
    return [
        (uid, {"umo": f"aiocqhttp:FriendMessage:{uid}", "last_seen": now - 99999.0}, urge)
        for uid, urge in (("u1", 3.0), ("u2", 2.0), ("u3", 1.0))
    ]


def _speak(failures):
    async def speak(uid):
        if uid in failures:
            return failures[uid]
        return True, f"发给 {uid} 了"

    return speak


class _TriggerEngine:
    """只保留 trigger_once 用到的那几个依赖。"""

    _trigger_precheck = SocialEngine._trigger_precheck
    _remember_clock = SocialEngine._remember_clock
    _representative_umo = SocialEngine._representative_umo
    trigger_once = SocialEngine.trigger_once

    async def _get_provider(self, umo):
        return None

    def __init__(self, running=True, ranked=None, speak=None):
        import datetime
        import tempfile as _tf

        self.running = running
        self.cfg = SocialConfig.from_astrbot({})
        self.generator = _TriggerGen()
        self.core = _NullCore()
        self._tf_dir = _tf.mkdtemp()
        self.state = SocialState(os.path.join(self._tf_dir, "state.json"))
        self._time_high = 0.0
        self._last_flush = 1.0
        self._ranked = ranked
        self._speak_impl = speak
        for uid, _u, _g in ranked or []:
            self.state.user("bot1", uid)["umo"] = f"aiocqhttp:FriendMessage:{uid}"

    def _time(self):
        return 1_700_000_000.0

    def _moment_of(self, bid, now):
        import datetime

        return datetime.datetime(2023, 11, 15, 14, 0)

    def settle_minds(self, bid, now, dt, root, bot_state, min_urge=0.0):
        return self._ranked

    async def _speak(self, bid, uid, u, urge, now, allow_veto=False):
        return await self._speak_impl(uid)

    def log(self, _msg):
        pass


class _TriggerGen:
    """退避改成按 provider 分片之后，只保留 precheck 用到的那一个口径。"""

    def __init__(self):
        self._llm_skip_until = {}
        self._llm_fail_streak = {}

    def _scope_of(self, provider):
        return "p1"

    def llm_backoff_remaining(self, scope=""):
        if not self._llm_skip_until:
            return 0.0
        if scope:
            return max(0.0, self._llm_skip_until.get(scope, 0.0) - 1_700_000_000.0)
        return max(0.0, max(self._llm_skip_until.values()) - 1_700_000_000.0)


class _NullCore:
    def read_root(self):
        return None


class _SnapCore:
    def __init__(self, snapshots):
        self._snapshots = snapshots

    def load_snapshot(self, bid, uid, root=None):
        return self._snapshots.get((bid, uid))


class _IntimacyEngine:
    """只保留 _intimacy_pick 用到的那几个依赖。"""

    _intimacy_pick = SocialEngine._intimacy_pick

    def __init__(self, snapshots):
        import tempfile as _tf

        self.cfg = SocialConfig.from_astrbot({})
        self._tf_dir = _tf.mkdtemp()
        self.state = SocialState(os.path.join(self._tf_dir, "state.json"))
        self.core = _SnapCore(snapshots)
        self.state.user("bot1", "u1")["umo"] = "aiocqhttp:FriendMessage:u1"

    def _time(self):
        return 1_700_000_000.0

    def _moment_of(self, bid, now):
        import datetime

        return datetime.datetime(2023, 11, 15, 14, 0)

    def _quiet_now(self, bid, hour):
        return False

    def gate_reason(self, bid, uid, u, now, body):
        return ""

    def log(self, _msg):
        pass


# ─── 日志节流：持续故障不能把日志刷爆 ──────────────────────────────

class TestLogThrottle(unittest.TestCase):
    def test_sustained_failure_logs_once_per_window(self):
        """key 失效、磁盘满这类持续故障，几十次失败只该产生一条日志 + 末尾汇总。"""
        records = []

        class _Cap(logging.Handler):
            def emit(self, handler_record):
                records.append(handler_record.getMessage())

        handler = _Cap()
        logger_under_test = logging.getLogger("throttle_test")
        logger_under_test.setLevel(logging.DEBUG)
        logger_under_test.addHandler(handler)
        try:
            thr = LogThrottle()
            for _ in range(50):
                if thr.allow("k"):
                    logger_under_test.error("失败了" + thr.summary("k"))
            self.assertEqual(len(records), 1, f"50 次持续失败产生了 {len(records)} 条日志")
        finally:
            logger_under_test.removeHandler(handler)

    def test_summary_counts_suppressed_lines(self):
        thr = LogThrottle()
        self.assertTrue(thr.allow("k"))
        for _ in range(7):
            self.assertFalse(thr.allow("k"))
        self.assertIn("7", thr.summary("k"))

    def test_reset_allows_immediate_logging_again(self):
        """故障恢复后必须能立刻再报第一条，否则修好了日志仍然长时间静默。"""
        thr = LogThrottle()
        thr.allow("k")
        thr.allow("k")
        thr.reset("k")
        self.assertTrue(thr.allow("k"))


# ─── 拟人：插件只给事实与特征，不替 AI 说话 ──────────────────────

class TestNoScriptedContent(unittest.TestCase):
    """硬约束：插件提供身体值和参考值，**不替 AI 说话、不让 AI 念台词**。

    改造前，prompt 里有 45 条固定例句和 80 条虚构经历：
    - 例句（“刚想到一个事”“楼下的猫又在晒太阳”…）模型会直接复用，每轮在其中轮着挑；
    - 虚构经历（“楼下卖早餐的香味飘上来了”“食堂今天有我爱吃的菜”）被当作动机塞进去，
      模型顺着往下编，于是她讲的是根本没发生过的事——掷骰子决定她今天吃了什么。
    """

    # 曾经进过 prompt 的具体句子
    FORBIDDEN_PHRASES = (
        "刚想到一个事", "突然想起来个东西", "也没啥事 就是想说句话", "刚才走神了",
        "有点无聊", "想起之前你说的那个", "忙完了 终于可以歇了", "刚碰到个好笑的",
        "楼下的猫又在晒太阳", "今天的咖啡特别苦", "脑子里突然冒出个念头",
        "突然有点想你了", "冒个泡", "吃饭了没", "你说气不气", "最近睡得好吗",
        "早点休息 晚安好梦", "先睡了 明天再聊",
    )
    # 曾经的虚构经历（模型顺着编出来的都是这类）
    FICTIONS = (
        "楼下卖早餐的香味", "咖啡喝到第二杯", "食堂今天居然有", "想吃下午茶",
        "被领导叫住", "跟人吵架", "做了个奇怪的梦", "居然准点下班",
        "开了个没什么用的会", "工位旁边的同事",
    )

    def _generator(self):
        return generator.MessageGenerator(_ProviderCtx(), SocialConfig())

    def _compose(self, reason, meta, target=None):
        gen = self._generator()
        tgt = target or {
            "umo": "aiocqhttp:FriendMessage:u1",
            "conversation": [
                {"dir": "in", "text": "今天好累啊，加班到九点"},
                {"dir": "out", "text": "那你吃饭了吗"},
                {"dir": "in", "text": "随便扒了两口"},
                {"dir": "in", "text": "明天还要早起"},
            ],
            "last_message": "嗯嗯，晚安",
            "last_seen": 1_700_000_000.0 - 5 * 3600,
            "topics": ["加班"],
            "_affection": 62,
            "_recent_proactive": ["今天真的好累", "早点休息"],
            "_body": {
                "activity": {"name": "休息", "schedule_event": "改方案", "location": "家"},
                "day": {"doing": "改方案"},
                "feelings": ["眼皮有点沉"],
                "form": {"max_chars": 90, "question_bias": 0.12,
                         "long_reply_ok": False, "burst_ok": False},
                "weather": {"env": "在下雨"},
            },
        }
        prepared = _run(gen._compose(
            tgt["umo"], tgt, "能量: 62 社交能量: 71",
            reason, meta, "你是小娜", None,
            decide=True, at=1_700_000_000.0,
        ))
        self.assertIsNotNone(prepared, "compose 返回 None，退避或 provider 不可用")
        return prepared[1]

    def _assert_clean(self, text, label):
        hits = [f for f in self.FORBIDDEN_PHRASES if f in text]
        self.assertEqual(hits, [], f"{label} 的 prompt 里仍有例句：{hits}")
        fakes = [f for f in self.FICTIONS if f in text]
        self.assertEqual(fakes, [], f"{label} 的 prompt 里仍有虚构经历：{fakes}")

    def test_all_scenes_have_no_scripted_content(self):
        from social import reasoning as R
        from social import threads as T

        now = 1_700_000_000.0
        user = {
            "umo": "aiocqhttp:FriendMessage:u1",
            "last_seen": now - 5 * 3600,
            "last_message": "嗯嗯，晚安",
            "message_count": 30,
        }
        reason, meta = R.generate_reason(dict(user), now, 21)
        self.assertEqual(reason, "", "generate_reason 还在返回编好的动机")

        kind, thread_r = T.thread_reason(
            dict(user, last_seen=now - 20 * 60), now,
            probe_after_seconds=120, presence_after_seconds=240,
            max_seconds=4200, context_seconds=1800,
        )
        self.assertEqual(thread_r, "", "thread_reason 还在返回编好的心理活动")

        scenes = [
            ("另起话题", reason, meta),
            ("追问", thread_r, T.thread_meta(kind, about="加班到九点", asked="那你吃饭了吗")),
            ("回访", "", T.loop_meta("面试")),
            ("早安问候", "", R.greet_meta("morning")),
            ("晚安问候", "", R.greet_meta("night")),
            ("收场", "", T.closer_meta("今天真的好累")),
        ]
        for label, rs, mt in scenes:
            self.assertEqual(rs, "", f"{label} 仍有预写的动机")
            self._assert_clean(self._compose(rs, mt), label)

    def test_prompt_has_no_prewritten_intent_line(self):
        from social import reasoning as R

        now = 1_700_000_000.0
        reason, meta = R.generate_reason(
            {"last_seen": now - 3600, "message_count": 10}, now, 15
        )
        text = self._compose(reason, meta)
        self.assertNotIn("你为什么想说一句", text, "预写动机那一行还在")
        self.assertIn("你现在知道的", text, "素材块没接上")

    def test_material_block_gives_facts_not_stories(self):
        gen = self._generator()
        import datetime

        now = datetime.datetime(2026, 3, 4, 15, 30)
        block = gen._material_block(
            {
                "last_seen": 1_700_000_000.0 - 3 * 86400,
                "last_message": "我下周要面试",
                "cue": "面试",
                "loop": "",
            },
            {
                "activity": {"schedule_event": "改方案", "location": "家"},
                "day": {"doing": "改方案"},          # 与 schedule_event 同内容，应去重
                "weather": {"env": "在下雨"},
            },
            now, 1_700_000_000.0,
        )
        self.assertIn("3 天没跟你说话", block)
        self.assertIn("我下周要面试", block)
        self.assertIn("在下雨", block)
        self.assertEqual(block.count("改方案"), 1, "同一件事被列了两遍")
        self.assertIn("别编这里没有的事", block)

    def test_style_profile_only_outputs_features(self):
        from social.style_profile import build_style_profile

        conv = [{"dir": "in", "text": t} for t in
                ("在吗", "吃了吗", "你干嘛呢", "哦哦", "困了")]
        out = build_style_profile(conv).text
        self.assertTrue(out, "样本够了却没产出特征")
        for raw in ("在吗", "吃了吗", "你干嘛呢", "困了"):
            self.assertNotIn(raw, out, f"风格特征里泄漏了原句：{raw}")
        self.assertRegex(out, r"平均 \d+ 个字")

    def test_ai_produces_its_own_intent_line(self):
        """动机改由模型自己写：以 # 开头的那一行会被剥掉，不会发出去。"""
        G = generator.MessageGenerator
        cfg = SocialConfig()
        d = G._parse_decision(
            "SEND\n#想问他面试的事\n那面试的事你后来准备得怎么样了", 120, cfg
        )
        self.assertTrue(d and d.send)
        self.assertEqual(d.parts, ["那面试的事你后来准备得怎么样了"])
        self.assertNotIn("想问他面试的事", "".join(d.parts))

    def test_missing_intent_line_still_parses(self):
        G = generator.MessageGenerator
        d = G._parse_decision("SEND\n今天真的好累", 120, SocialConfig())
        self.assertTrue(d and d.send)
        self.assertEqual(d.parts, ["今天真的好累"])

    def test_message_types_have_no_examples(self):
        from social.reasoning import MESSAGE_TYPES

        for name, info in MESSAGE_TYPES.items():
            self.assertNotIn("examples", info, f"{name} 还带例句")
            self.assertTrue(info.get("style_hint"), f"{name} 缺写法要求")

    def test_core_form_fields_are_used(self):
        """Core 算好的 form 三项：问句倾向、能否连发、能否长回复。"""
        gen = self._generator()
        target = {
            "umo": "u", "conversation": [], "last_seen": 1_700_000_000.0,
            "_body": {"form": {"max_chars": 120, "long_reply_ok": False,
                              "burst_ok": False, "question_bias": 0.1}},
        }
        prepared = _run(gen._compose(
            "u", target, "", "", {"msg_type_desc": "说点什么"},
            "人设", None, decide=True, at=1_700_000_000.0,
        ))
        self.assertIsNotNone(prepared)
        _provider, text, max_len, _emoji = prepared
        self.assertLessEqual(max_len, generator.LONG_REPLY_CAP_WHEN_OFF)
        self.assertIn("不太想问句", text)

    def test_burst_disabled_by_core_form(self):
        gen = self._generator()
        target = {
            "umo": "u", "conversation": [], "last_seen": 1_700_000_000.0,
            "_body": {"form": {"max_chars": 120, "burst_ok": False}},
        }
        prepared = _run(gen._compose(
            "u", target, "", "", {"msg_type_desc": "说点什么"},
            "人设", None, decide=True, at=1_700_000_000.0,
        ))
        self.assertIsNotNone(prepared)
        _provider, text, _ml, _emoji = prepared
        self.assertNotIn("---", text, "Core 说这一轮别连发，提示里却还在教怎么拆")


# ─── v2.24.0：一次逻辑调用只打一次真实请求 ──────────────────────────

class _CountingProvider:
    """统计**每一个**真正被打出去的方法。回退链只要还活着就一定能看到痕迹。"""

    def __init__(self, outcome):
        self.id = "p1"
        self.outcome = outcome
        self.hits = []

    async def text_chat(self, **kw):
        self.hits.append("text_chat")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def chat(self, prompt, **kw):
        self.hits.append("chat")
        return "chat 路"

    async def generate(self, prompt, **kw):
        self.hits.append("generate")
        return "generate 路"

    async def complete(self, prompt, **kw):
        self.hits.append("complete")
        return "complete 路"

    async def ask(self, prompt, **kw):
        self.hits.append("ask")
        return "ask 路"


class _CountCtx:
    def __init__(self, provider):
        self._p = provider

    async def get_using_provider_async(self, umo=None):
        return self._p


class _ErrResp:
    role = "err"

    def __init__(self, detail=""):
        self.completion_text = detail


class TestOneRequestPerCall(unittest.TestCase):
    """旧实现把 llm_generate/text_chat/chat/generate/complete/ask 无条件串起来试，
    一次「生成一条消息」最多真的发出 5 次网络请求。key 失效时每一路都必然失败，
    等于把一次故障放大五倍，而且每个心跳重来一次。"""

    def _gen(self, provider):
        return generator.MessageGenerator(_CountCtx(provider), SocialConfig())

    def test_provider_error_does_not_fall_through_to_other_methods(self):
        p = _CountingProvider(_ErrResp("Error code: 401 - invalid api key"))
        gen = self._gen(p)
        out = _run(gen._call_llm(p, "prompt"))
        self.assertIsNone(out)
        self.assertEqual(p.hits, ["text_chat"], "一次失败后又去试了别的调用方式")

    def test_timeout_does_not_fall_through(self):
        p = _CountingProvider(RuntimeError("读超时"))
        gen = self._gen(p)
        self.assertIsNone(_run(gen._call_llm(p, "prompt")))
        self.assertEqual(p.hits, ["text_chat"], "超时后又去试了别的调用方式")

    def test_working_method_is_remembered(self):
        p = _CountingProvider(_Resp("SEND\n嗨"))
        gen = self._gen(p)
        for _ in range(4):
            self.assertEqual(_run(gen._call_llm(p, "prompt")), "SEND\n嗨")
        self.assertEqual(p.hits, ["text_chat"] * 4)
        self.assertEqual(set(gen._prefer.values()), {"text_chat"})

    def test_auth_failure_enters_backoff_on_first_strike(self):
        """鉴权/额度这类等再久也一样，不该连打三次才退。"""
        p = _CountingProvider(_ErrResp("401 invalid api key"))
        gen = self._gen(p)
        _run(gen._call_llm(p, "prompt"))
        self.assertEqual(sum(gen._llm_fail_streak.values()), 1)
        self.assertGreater(
            gen.llm_backoff_remaining(), 0,
            "第一次鉴权失败就该退避，而不是再打两次同样的请求",
        )

    def test_backoff_outlives_a_heartbeat(self):
        """退避比心跳短等于没退：key 挂着不动时仍然是每个心跳白打一次。"""
        p = _CountingProvider(_ErrResp("401 invalid api key"))
        gen = self._gen(p)
        _run(gen._call_llm(p, "prompt"))
        cfg = SocialConfig()
        heartbeat = cfg.heartbeat_max_minutes * 60
        self.assertGreater(
            gen.llm_backoff_remaining(), heartbeat,
            "鉴权失败的退避必须比一个心跳长，否则挡不住任何开销",
        )


# ─── v2.24.0：emoji 跟人设走，不是配置开关 ──────────────────────────

class TestEmojiFollowsPersona(unittest.TestCase):
    def test_her_own_history_decides(self):
        heavy = ["今天好累呀😮‍💨", "晚上吃啥🍜", "楼下那只猫好可爱🐱"]
        none = ["今天好累", "晚上吃啥", "楼下那只猫好可爱"]
        self.assertTrue(generator._emoji_policy(
            style_profile.emoji_habit(heavy), "")[0], "爱用 emoji 的人被砍平了")
        self.assertFalse(generator._emoji_policy(
            style_profile.emoji_habit(none), "")[0], "不用 emoji 的人被硬塞了")

    def test_persona_text_is_only_a_fallback(self):
        """自己没有样本时才看人设原文；自己有样本时以样本为准。"""
        self.assertTrue(
            generator._emoji_policy(style_profile.EMOJI_UNKNOWN, "你爱用 ✨ 这种")[0]
        )
        self.assertFalse(
            generator._emoji_policy(
                style_profile.emoji_habit(["没有", "emoji", "啊"]), "你爱用 ✨"
            )[0],
            "人设里带 emoji 就该压过她自己从不用 emoji 的历史",
        )

    def test_output_cleaning_follows_the_decision(self):
        keep = generator.MessageGenerator._clean_output("今天好累呀😮‍💨", 60, allow_emoji=True)
        drop = generator.MessageGenerator._clean_output("今天好累呀😮‍💨", 60, allow_emoji=False)
        self.assertIn("😮", keep)
        self.assertNotIn("😮", drop)

    def test_config_no_longer_has_emoji_switch(self):
        from social.config import SocialConfig as C
        self.assertFalse(
            hasattr(C(), "allow_emoji"),
            "allow_emoji 还在配置里：它会继续把人设的说话习惯拉平",
        )


# ─── v2.24.0：收场是死代码 / 问候污染冷落计数 / 由头被误吃 ──────────

class TestCloserAndGreetingBookkeeping(unittest.TestCase):
    def test_closer_reason_is_reachable(self):
        """closer_reason 以前每个分支都 return ''，最后一行也 return ''，
        于是「没人回就自己收个尾」整条链路一次都不会触发，而面板照常说该收场。"""
        now = 1_700_000_000.0
        u = {
            "pending_result": "ignored",
            "last_sent": now - 9 * 3600,
            "closer_for": 0.0,
            "no_reply_streak": 1,
            # 对方后来又说了一句就不算「悬着」，所以最后一句必须是她说出去的
            "conversation": [{"dir": "in", "text": "在吗"}, {"dir": "out", "text": "早"}],
        }
        self.assertTrue(
            threads.closer_reason(u, now, after_seconds=8 * 3600),
            "该收场时却返回空——收场功能整条是死代码",
        )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = SocialState(
            os.path.join(self.tmp, "state.json"),
            time_source=lambda: 1_700_000_000.0,
        )

    def test_greeting_does_not_arm_the_reply_timer(self):
        """早安/晚安本来就不要求回复。进了「等回话」计时器就会把「没回早安」
        算成「被冷落」，而衰减锚点又是 last_sent（每天问候都刷新），
        于是念头天花板被永久压死。"""
        self.state.record_outgoing("b", "u", "早安", "greeting", expect_reply=False)
        u = self.state.user("b", "u")
        self.assertNotEqual(u.get("pending_result"), "waiting")
        self.assertEqual(u.get("no_reply_streak", 0), 0)
        self.assertEqual(u.get("proactive_sent", 0), 0, "早安不该进回复率分母")
        self.assertTrue(u.get("last_sent"), "冷却锚点还是要刷的")

    def test_ordinary_message_still_arms_the_timer(self):
        self.state.record_outgoing("b", "u", "在干嘛")
        u = self.state.user("b", "u")
        self.assertEqual(u.get("pending_result"), "waiting")
        self.assertEqual(u.get("proactive_sent"), 1)

    def test_greeting_does_not_eat_a_due_cue(self):
        """一句早安把「TA 说明天面试」吃掉、那件事再也没人问——问候排在候选最前面。"""
        u = self.state.user("b", "u")
        u["cue"] = "面试"
        u["cue_due"] = 1_700_000_000.0 - 60
        self.state.record_outgoing("b", "u", "早安", "greeting", expect_reply=False)
        self.assertEqual(self.state.user("b", "u").get("cue"), "面试",
                         "不是由 cue 触发的消息不该消费到点的由头")


class TestCueConsumedOnlyByCueMessages(unittest.TestCase):
    def test_engine_clears_cue_only_for_cue_category(self):
        src = PLUGIN_ROOT / "social" / "engine.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn('elif category == "cue"', text)
        self.assertNotIn(
            'if u.get("cue_due") and float(u["cue_due"]) <= ts:',
            text,
            "record_outgoing 里还在无条件清 cue（state.py 那边已删，这里应是残留）",
        )


class TestLLMBackoffUsesEngineClock(unittest.TestCase):
    def test_backoff_follows_the_injected_clock(self):
        clock = {"t": 1000.0}
        gen = generator.MessageGenerator(_PlainCtx(), SocialConfig(),
                                         time_source=lambda: clock["t"])
        gen._llm_skip_until["s"] = 1600.0
        self.assertAlmostEqual(gen.llm_backoff_remaining("s"), 600.0)
        clock["t"] = 1700.0
        self.assertAlmostEqual(gen.llm_backoff_remaining("s"), 0.0)


# ─── v2.25.0 清洗链：不再把对的输出改坏 ──────────────────────────────

class TestCleaningDoesNotCorrupt(unittest.TestCase):
    def test_send_leading_needs_a_separator(self):
        """原来中文分支零宽匹配，于是「要发工资了吧」被削成「工资了吧」。"""
        for text in ("要发工资了吧", "发这条会不会太突然了", "可以发货了吗我的快递还没到"):
            self.assertEqual(
                generator.MessageGenerator._clean_output(text, 60, allow_emoji=True), text,
                f"正常句子被削掉了开头：{text}",
            )
        self.assertEqual(
            generator.MessageGenerator._clean_output("要发：今天真累", 60, allow_emoji=True),
            "今天真累", "带冒号的协议词仍然要剥",
        )

    def test_roleplay_is_kept(self):
        """括号动作是人格自己的说话方式；无差别删除会吃掉正常中文。"""
        for text in ("（笑）你好", "他去（上海）出差了", "这个（很重要）的事", "*摸摸头* 辛苦了"):
            self.assertEqual(
                generator.MessageGenerator._clean_output(text, 60, allow_emoji=True), text,
            )

    def test_line_breaks_survive(self):
        out = generator.MessageGenerator._clean_output("早\n\n起了", 120, allow_emoji=True)
        self.assertIn("\n", out, "换行被拼掉了——那等于把停顿也拼掉")

    def test_markdown_heading_is_not_mistaken_for_intent(self):
        d = generator.MessageGenerator._parse_decision("SEND\n# 今天天气真好", 120, None)
        self.assertTrue(d and d.send)
        self.assertEqual(d.parts, ["今天天气真好"])

    def test_promise_line_is_extracted(self):
        d = generator.MessageGenerator._parse_decision(
            "SEND\n#想起来了\n你那个体检后来咋样\n> 明天给你看体检报告", 120, None)
        self.assertTrue(d and d.send)
        self.assertEqual(d.promise, "明天给你看体检报告")
        self.assertNotIn(">", d.parts[0])

    def test_no_promise_when_model_does_not_write_one(self):
        d = generator.MessageGenerator._parse_decision("SEND\n#想起来了\n今天好累", 120, None)
        self.assertEqual(d.promise, "")

    def test_truncate_prefers_keeping_the_sentence(self):
        text = ("刚洗完澡躺在床上头发还湿着刷到一只橘猫趴在键盘上不肯走"
                "看了半天也没写一个字")
        self.assertGreater(len(text), 30)
        out = generator.MessageGenerator._clean_output(text, 30, allow_emoji=True)
        self.assertGreaterEqual(len(out), 30, "找不到句子边界就硬切，句子断了")
        self.assertIn("橘猫", out)

    def test_burst_separator_normalised(self):
        parts = generator.MessageGenerator._split_parts("我下班早——\n路上买了花", 60, None, True)
        self.assertEqual(parts, ["我下班早", "路上买了花"])


# ─── v2.25.0 关系：迟到的回复、冷落衰减、熟悉度连续 ─────────────────

class TestRelationshipCont(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = SocialState(
            os.path.join(self.tmp, "state.json"), time_source=lambda: 1_700_000_000.0)

    def test_late_reply_is_forgiven(self):
        """隔夜才回的一句，12 小时后 pending 已被清零，after_reply 永远不会被调用。"""
        u = self.state.user("b", "u")
        u.update({"no_reply_streak": 4, "pending_result": "ignored", "pending_since": 0.0,
                  "proactive_sent": 9, "proactive_replied": 2})
        self.state.record_incoming("b", "u", "昨天太忙了抱歉", reply_window_seconds=2 * 3600,
                                  pending_window_seconds=12 * 3600, hour_override=9)
        u = self.state.user("b", "u")
        self.assertEqual(u["no_reply_streak"], 0, "人回来了就是回来了")
        self.assertEqual(u["pending_result"], "replied")
        self.assertEqual(u["proactive_replied"], 2, "回复率分母口径不变，不该被这笔算进去")

    def test_streak_decays_while_she_keeps_sending(self):
        """锚点原来是 last_sent：只要她还在发，衰减就永远等不到。"""
        u = {"no_reply_streak": 5, "last_sent": 1_700_000_000.0,
             "last_ignored_at": 1_700_000_000.0 - 9 * 86400}
        desire.decay_streak(u, 1_700_000_000.0)
        self.assertLess(u["no_reply_streak"], 5, "晾了 9 天还一条没减")

    def test_long_known_person_does_not_collapse(self):
        """冷落几次就从「亲密」塌到「刚认识不久」，那个落差本身不像人。"""
        far = round(
            (0.55 * desire.interest_level(
                {"message_count": 200, "proactive_sent": 20, "proactive_replied": 1,
                 "no_reply_streak": 8}, 0.5)
             + 0.45 * desire.familiarity(200)) * 100, 1)
        tier, _ = reasoning.relationship_tier(far)
        self.assertNotEqual(tier, "acquaintance",
                            f"聊了 200 条的人被当成刚认识（好感 {far}）")

    def test_still_cools_down_when_ignored(self):
        near = round(
            (0.55 * desire.interest_level(
                {"message_count": 200, "proactive_sent": 20, "proactive_replied": 18,
                 "no_reply_streak": 0}, 0.5)
             + 0.45 * desire.familiarity(200)) * 100, 1)
        far = round(
            (0.55 * desire.interest_level(
                {"message_count": 200, "proactive_sent": 20, "proactive_replied": 1,
                 "no_reply_streak": 8}, 0.5)
             + 0.45 * desire.familiarity(200)) * 100, 1)
        self.assertGreater(near, far, "被无视了却一点不变，那不是连续")


# ─── v2.25.0 触发：最小间隔 / 强度分档 / 午间兜底 ──────────────────

class TestTriggerPacing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = 1_700_000_000.0
        self.engine = _FakeEngine(clock_ref=lambda: self.clock)

    def test_min_gap_is_per_tier(self):
        """档位越高近得越理所当然——她常聊的那个人本来就该被多找一点。

        以前是所有人一个 min_gap_minutes，一刀切的结果是他一周只收到 2 条。
        """
        u = {"last_sent": self.clock - 40 * 60}
        high = self.engine._gap_left(u, "high", self.clock)
        mid = self.engine._gap_left(u, "mid", self.clock)
        low = self.engine._gap_left(u, "low", self.clock)
        self.assertEqual(high, 0.0, "高档隔 40 分钟就该能开口了")
        self.assertGreater(mid, 0.0, "中档不该这么近")
        self.assertGreater(low, mid, "低档应该更远")
        # v1.27.8（F4）：低档 1440→720 分钟（12 小时），整体更频繁。
        self.assertGreater(low, 10 * 3600.0, "低档应该明显更远")

    def test_gap_stretches_when_ignored(self):
        a = {"last_sent": self.clock - 30 * 60, "no_reply_streak": 0}
        b = {"last_sent": self.clock - 30 * 60, "no_reply_streak": 6}
        self.assertGreater(
            self.engine._gap_left(b, "mid", self.clock),
            self.engine._gap_left(a, "mid", self.clock),
            "被连着无视的人应该自己往后退",
        )

    def test_eager_bypasses_the_gap(self):
        u = {"last_sent": self.clock - 5 * 60, "urge": 99.0, "fire_gate": 1.0}
        self.assertTrue(self.engine._urge_is_eager(u), "很想说的时候不该还被间隔挡着")

    def test_no_daily_quota(self):
        """这一版刻意不做每日条数上限：靠门槛挣，不靠配额掐。"""
        src = PLUGIN_ROOT / "social" / "engine.py"
        text = src.read_text(encoding="utf-8")
        # 查的是「真的有一份每日额度」这件事，不是查有没有写过这几个字
        self.assertNotIn("_daily_quota", text)
        self.assertNotIn("daily_quota =", text)
        self.assertNotIn("cfg.daily", text)
        # 唯一带 daily_cap 的是群破冰的 icebreak_daily_cap，那是群侧的，与私聊节奏无关
        self.assertIn("icebreak_daily_cap", text)

    def test_midday_is_a_fallback_not_an_extra(self):
        """午间问候本来是「早安错过了才补」，每天各发一条会把自发那条挤掉。"""
        u = {"greet_day": "2023-11-15", "greet_kind": "morning"}
        self.assertFalse(reasoning.greeting_due(u, "2023-11-15", "midday"))
        self.assertTrue(reasoning.greeting_due(u, "2023-11-16", "midday"))


# ─── v2.25.0 多 Bot：进程内单例状态必须按角色/provider 拆 ────────────

class TestMultiBotIsolation(unittest.TestCase):
    def test_send_breaker_is_per_bot(self):
        e = _SendEngine(object())
        for _ in range(3):
            e._note_send_failure("A", "平台未就绪", False)
        self.assertGreater(e.send_breaker_left("A"), 0)
        self.assertEqual(e.send_breaker_left("B"), 0.0, "A 坏掉把 B 也停了")
        e._note_send_ok("B")
        self.assertGreater(e.send_breaker_left("A"), 0, "B 的成功把 A 的熔断清零了")

    def test_peer_unreachable_does_not_trip_breaker(self):
        e = _SendEngine(object())
        for _ in range(5):
            e._note_send_failure("A", "不是好友", True)
        self.assertEqual(e.send_breaker_left("A"), 0.0, "一个人的事不该停掉所有人")

    def test_llm_backoff_is_per_provider(self):
        gen = generator.MessageGenerator(_PlainCtx(), SocialConfig())
        gen._note_llm_failure("p1", "401 invalid api key", fatal=True)
        self.assertGreater(gen.llm_backoff_remaining("p1"), 0)
        self.assertEqual(gen.llm_backoff_remaining("p2"), 0.0, "A 的 key 失效把 B 也停了")
        gen._note_llm_ok("p2")
        self.assertGreater(gen.llm_backoff_remaining("p1"), 0, "B 的成功把 A 的退避抹了")

    def test_unusable_is_per_provider(self):
        class PA:
            id = "A"
            async def text_chat(self, **kw):
                raise TypeError("bad")
            async def chat(self, p):
                return "a"
        class PB:
            id = "B"
            async def text_chat(self, **kw):
                return "b"
        gen = generator.MessageGenerator(_PlainCtx(), SocialConfig())
        gen._unusable.setdefault("A", set()).add("text_chat")
        self.assertEqual([c[0] for c in gen._candidates(PB(), "p")], ["text_chat"],
                         "B 的 text_chat 被 A 牵连剔掉了")

    def test_signals_not_written_toplevel_when_multi_bot(self):
        tmp = tempfile.mkdtemp()
        w = SignalsWriter(os.path.join(tmp, "s.json"), time_source=lambda: 1_700_000_000.0)
        w.set_role_count(2)
        w.note_proactive("u1", 1_700_000_000.0, "A")
        w.set_ignored_streak(5, "A")
        w.set_ignored_streak(1, "B")
        with open(os.path.join(tmp, "s.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["last_proactive_at"], 0.0,
                         "多 Bot 写顶层会让每个角色都误以为刚说过话")
        self.assertEqual(data["by_bid"]["A"]["ignored_streak"], 5)
        self.assertEqual(data["by_bid"]["B"]["ignored_streak"], 1)

    def test_single_bot_keeps_writing_toplevel(self):
        tmp = tempfile.mkdtemp()
        w = SignalsWriter(os.path.join(tmp, "s.json"), time_source=lambda: 1_700_000_000.0)
        w.set_role_count(1)
        w.note_proactive("u1", 1_700_000_000.0, "A")
        with open(os.path.join(tmp, "s.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["last_proactive_at"], 1_700_000_000.0)

    def test_seeding_dedupes_by_bot_and_uid(self):
        """uid 只在单个平台内唯一：不能在 A 名下聊过的人，让 B 名下同名的人加不进来。

        两个真实 bot 都在时，播种会归到 default（见 seed_target_bid）。原来按裸 uid
        判重，A 名下的 123 会让 default 下的 123 被跳过——B 于是永远认不到这个人。
        """
        tmp = tempfile.mkdtemp()
        st = SocialState(os.path.join(tmp, "s.json"))
        st.bot("A").setdefault("users", {})["123"] = {"umo": "aiocqhttp:FriendMessage:123"}
        st.bot("B").setdefault("users", {})["999"] = {"umo": "aiocqhttp:FriendMessage:999"}
        target = history_ingest.seed_target_bid(st)
        added = history_ingest.apply_seed_list(
            st, ["123", "456"], "telegram", now=1_700_000_000.0)
        self.assertEqual(target, "default")
        self.assertEqual(added, 2, "跨 bot 的同名 uid 被当成同一个人跳过了")
        self.assertIn("123", st.bot("default").get("users", {}))
        self.assertIn("456", st.bot("default").get("users", {}))


# ─── v2.25.0 素材：未完话题多条 + 她自己的承诺 ────────────────────

class TestMaterials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.T = 1_000_000.0
        self.state = SocialState(
            os.path.join(self.tmp, "s.json"), time_source=lambda: self.T)

    def test_up_to_five_open_loops_per_user(self):
        u = self.state.user("b", "u")
        for i, txt in enumerate(["面试", "体检", "牙医", "搬家", "续签", "驾照"]):
            SocialState._note_loop(u, txt, self.T - (i + 1) * 3600, self.T)
        self.assertEqual(len(threads.loop_entries(u)), 5, "上限按每个用户 5 件")
        self.assertEqual(len(threads.loop_entries(self.state.user("b", "u2"))), 0,
                         "件数是按用户记的，不该串到别人身上")

    def test_old_single_loop_still_readable(self):
        old = {"loop": "体检", "loop_due": self.T - 100, "loop_expire_at": self.T + 99999,
               "loop_retry_at": 0.0, "loop_tries": 0}
        self.assertEqual(threads.live_loop(old, self.T), "体检")

    def test_asking_one_loop_keeps_the_others(self):
        u = self.state.user("b", "u")
        for txt in ("A", "B", "C"):
            SocialState._note_loop(u, txt, self.T - 3600, self.T)
        first = threads.live_loops(u, self.T, limit=1)[0][1]
        SocialEngine._drop_loop(u, first)
        self.assertEqual(len(threads.loop_entries(u)), 2, "问过一件就把整叠清空了")

    def test_promise_is_kept_and_kept_separate_from_loop(self):
        u = self.state.user("b", "u")
        SocialState._note_loop(u, "对方的体检", self.T - 3600, self.T)
        for t in ["明天给你看体检报告", "回头查签证", "周末一起看展", "把链接发你", "还你书"]:
            SocialState.note_promise(u, t, self.T)
        self.assertEqual(len(threads.promise_entries(u)), threads.PROMISE_SLOTS_MAX)
        self.assertEqual(threads.live_promises(u, self.T), [], "还没到点就兑现了")
        self.assertEqual(len(threads.live_promises(u, self.T + 21 * 3600)), 3)
        self.assertTrue(threads.loop_entries(u), "承诺和未完话题是两套，不该互相覆盖")
        n = len(threads.promise_entries(u))
        SocialState.note_promise(u, "周末一起看展", self.T)
        self.assertEqual(len(threads.promise_entries(u)), n, "同一个诺重复记了")

    def test_pure_symbols_are_not_thin(self):
        """「？」只是一个问号，不该触发「把那件事问清楚」——那件事就是个问号。"""
        for text in ("？", "😭", "!!!", "😢"):
            self.assertFalse(reasoning.is_thin(text), f"{text} 被当成敷衍了")
        for text in ("嗯", "好", "行"):
            self.assertTrue(reasoning.is_thin(text), f"{text} 本来就是敷衍")


# ─── v2.25.0 表达：记仇三道限 / 分享与求回应分开 ────────────────────

class TestExpression(unittest.TestCase):
    def _line(self, aggr, seen, recent):
        return generator._relationship_line({
            "_affection": 78, "_body": {"mood": {"aggression": aggr, "libido": 20}},
            "_aggr_high_n": seen, "_aggr_recently_acted": recent, "no_reply_streak": 0,
        })

    def test_grudge_needs_sustained_high(self):
        self.assertNotIn("记着这件事", self._line(20, 0, False), "平常不该有气")
        self.assertNotIn("记着这件事", self._line(40, 1, False), "单次跳高就算记仇了")
        self.assertIn("记着这件事", self._line(40, 2, False))
        self.assertNotIn("记着这件事", self._line(40, 3, True), "刚提过又提，无脑高情绪")
        self.assertNotIn("记着这件事", self._line(12, 5, False), "早消气了还记着")

    def test_grudge_never_becomes_a_scream(self):
        line = self._line(40, 2, False)
        self.assertIn("内部状态参考", line, "它只能是软描述，不该变成指令")
        self.assertNotIn("必须", line)
        self.assertNotIn("你要", line)

    def test_low_state_day(self):
        self.assertFalse(generator._low_state_day({"sleep_pressure": 70, "hunger": 20,
                                                   "discomfort": 10}))
        self.assertTrue(generator._low_state_day({"sleep_pressure": 70, "hunger": 80,
                                                  "discomfort": 10}))
        self.assertFalse(generator._low_state_day({}))

    def test_opening_memory_gives_a_conclusion(self):
        heads = generator._opening_neurons(
            ["诶你吃了吗", "诶今天好热", "诶我睡了", "刚下班", "对了昨天那个"])
        self.assertEqual(heads[0], "诶", "连着三条都以诶开头，结论里却没它")


# ─── v1.19.0 容器实测反馈：入口清洗 / provider 探测 / 可见性 / 规模 ──

class TestSanitizeIncoming(unittest.TestCase):
    """observe() 原样存 event.message_str，而里面夹着框架注入的内容。"""

    def test_core_body_block_is_cut(self):
        raw = "今天好累啊\n〔她的身体与生活 v10 uid=bot1〕\n小雨有点堵，饿了。"
        self.assertEqual(sanitize.sanitize_incoming(raw), "今天好累啊")

    def test_system_reminder_removed(self):
        self.assertEqual(
            sanitize.sanitize_incoming("今天真好\n<system_reminder>用户提到体检</system_reminder>"),
            "今天真好")

    def test_multiline_reminder(self):
        out = sanitize.sanitize_incoming("我在想那个事\n<system_reminder>\n多行\n内容\n</system_reminder>\n还有别的")
        self.assertEqual(out, "我在想那个事\n\n还有别的")

    def test_framework_only_message_recognised(self):
        self.assertTrue(sanitize.looks_like_framework_only("〔她的身体与生活 v10〕\n只有事实块"))
        self.assertFalse(sanitize.looks_like_framework_only("正常的一句话"))

    def test_migration_cleans_existing_state(self):
        tmp = tempfile.mkdtemp()
        st = SocialState(os.path.join(tmp, "s.json"))
        u = st.user("b", "u")
        u.update({
            "last_message": "今天好累\n〔她的身体与生活 v10〕\n状态",
            "conversation": [{"dir": "in", "text": "嗨\n<system_reminder>x</system_reminder>", "ts": 1}],
            "topics": ["好的", "〔她的身体与生活 v10〕", "面试"],
        })
        self.assertTrue(sanitize.sanitize_user_record(u))
        self.assertEqual(u["last_message"], "今天好累")
        self.assertEqual(u["conversation"][0]["text"], "嗨")
        self.assertNotIn("〔她的身体与生活 v10〕", u["topics"])


class TestSplitIdentityMerged(unittest.TestCase):
    """WebChat 每个会话 UUID 曾被当成一个新人。"""

    def test_stable_uid_drops_thread_uuid(self):
        a = history_ingest.stable_uid("webchat!naliling!<uuid-1>")
        b = history_ingest.stable_uid("webchat!naliling!<uuid-2>")
        self.assertEqual(a, b, "同一账号的两个会话应该是同一个人")
        self.assertEqual(a, "webchat!naliling")

    def test_normal_umo_unchanged(self):
        self.assertEqual(history_ingest.stable_uid("aiocqhttp:FriendMessage:12345"), "12345")

    def test_merge_keeps_the_right_side_of_each_field(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "s.json")
        T = 1_700_000_000.0
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"bots": {"bot1": {"users": {
                "webchat!n!a": {"umo": "webchat!n!a", "message_count": 20, "proactive_sent": 3,
                                "no_reply_streak": 4, "last_sent": T - 100, "urge": 0.4},
                "webchat!n!b": {"umo": "webchat!n!b", "message_count": 22, "proactive_sent": 2,
                                "no_reply_streak": 1, "last_sent": T - 200, "urge": 0.9},
            }}}}, f, ensure_ascii=False)
        st = SocialState(path)
        users = st.bot("bot1")["users"]
        self.assertEqual(len(users), 1, f"两个会话应当合并成一个，实际 {list(users)}")
        u = list(users.values())[0]
        self.assertEqual(u["message_count"], 42)
        self.assertEqual(u["proactive_sent"], 5)
        self.assertEqual(u["no_reply_streak"], 1, "冷落计数取了更宽松的那个")
        self.assertEqual(u["last_sent"], T - 100)
        self.assertEqual(u["urge"], 0.9, "念头被抹掉了")


class TestProviderProbeNoLongerKillsBot(unittest.TestCase):
    def test_probe_block_is_gone(self):
        src = PLUGIN_ROOT / "social" / "engine.py"
        text = src.read_text(encoding="utf-8")
        self.assertNotIn("available_bots", text,
                         "拿第一个用户探测 provider、失败就整个 bot 跳过的逻辑还在")
        self.assertNotIn("test_umo", text)


class TestRuntimeMetrics(unittest.TestCase):
    def test_metrics_rendered_before_first_heartbeat(self):
        e = _FakeEngine(clock_ref=lambda: 1_700_000_000.0)
        e._m = {"last_heartbeat": 0.0}
        self.assertIn("还没跑过", e._metrics_text())

    def test_dry_run_does_not_spend_or_send(self):
        e = _MetricsEngine()
        import asyncio as _a
        m = _a.run(e.dry_run())
        self.assertEqual(m["last_settled_users"], 2)
        self.assertEqual(len(e.calls), 0, "dry-run 调了 LLM")
        self.assertEqual(len(e.sends), 0, "dry-run 发了消息")


class TestHourlyCap(unittest.TestCase):
    def test_cap_holds_under_bulk_pressure(self):
        e = _SendEngine(object())
        cfg = SocialConfig()
        now = 1_700_000_000.0
        for i in range(cfg.hourly_sends_cap):
            e._note_send_ok("A")
            e._hourly_take("A", now)
        self.assertEqual(e._hourly_left("A", now), 0, "超预算后还放行")
        self.assertGreater(e._hourly_left("A", now + 3601), 0, "一小时后应恢复")

    def test_cap_is_per_bot(self):
        e = _SendEngine(object())
        e._hourly_take("A", 1_700_000_000.0)
        self.assertGreater(e._hourly_left("B", 1_700_000_000.0), 0, "A 超预算把 B 也停了")

    def test_cap_zero_disables(self):
        cfg = SocialConfig.from_astrbot({"hourly_sends_cap": 0})
        self.assertEqual(cfg.hourly_sends_cap, 0, "设 0 应该能关掉")


# ─── v1.20.0：真实 197 条记录带出来的六组测试 ───────────────────────

def _corpus():
    """真实发送记录的代表样本（tests/corpus/social_messages_197.jsonl）。"""
    import json
    path = (Path(__file__).resolve().parent / "corpus" / "social_messages_197.jsonl")
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class TestVerifyCorpus(unittest.TestCase):
    """验收层：真实记录里的坏样本必须被拦，好样本必须放行。"""

    def test_corpus_labels_match_verifier(self):
        rows = _corpus()
        self.assertGreater(len(rows), 30, "语料太少了，起不到回归作用")
        wrong_pass, wrong_block = [], []
        for r in rows:
            ok, why = verify_module.verify_message(r["text"], called="阿澈")
            if r["pass"] and not ok:
                wrong_block.append((r["text"][:30], why))
            if not r["pass"] and ok:
                wrong_pass.append(r["text"][:30])
        self.assertEqual(wrong_pass, [], f"这些坏样本被放行了：{wrong_pass}")
        self.assertEqual(wrong_block, [], f"这些好样本被误杀了：{wrong_block}")

    def test_the_five_named_defects_are_caught(self):
        """四个真实缺陷各至少抓一条，钉死方向。"""
        cases = {
            "动机泄漏": "想表达此刻的孤独和饥饿感",
            "括号残缺": "(把毯子裹得紧紧的",
            "只剩动作": "(裹紧毛毯，困倦地靠在沙发上)",
            "空泛": "刚醒",
        }
        for label, text in cases.items():
            ok, why = verify_module.verify_message(text, called="阿澈")
            self.assertFalse(ok, f"{label} 没被拦下：{text}")
            self.assertTrue(why, f"{label} 拦下了但没给原因")

    def test_near_duplicate_is_caught(self):
        """近似重复必须拦下。

        判据只能覆盖**近似重复**——真实记录里那 23 条「沙发追剧」措辞每次都不同，
        换成别的说法二元组重合度就掉到 0.05，抓不住。同一件事说两遍这件事由
        **由头的一次性规则**兜（见 TestNoAnchorNoSend），这一条只是兜底网。
        """
        sent = "刚忙完手头的事，窗外的阳光好舒服，想赖在你怀里晒太阳"
        again = "刚忙完手头的事，窗外阳光好舒服，想赖在你怀里晒晒太阳"
        ok, why = verify_module.verify_message(again, recent=[sent], called="阿澈")
        self.assertFalse(ok, "几乎原样重发没有被拦下")

    def test_anchor_rule_is_what_stops_paraphrase_repeat(self):
        """换个说法重说同一件事，靠由头一次性规则，不靠文本相似度。"""
        u = {"last_seen": 0.0}
        body = {"day": {"doing": "追剧"}}
        now = 1_700_000_000.0
        first = anchors.prepare_round_anchors(u, body, now, limit=5)
        self.assertTrue(first)
        again = anchors.prepare_round_anchors(u, body, now, limit=5)
        fresh = [a for a in again if a["about"] not in {x["about"] for x in first}]
        self.assertEqual(fresh, [], "同一件事被重新派生了")

    def test_normal_messages_are_never_killed(self):
        """阈值不能过紧：语料里正常消息两两最高相似度只有 0.30。"""
        good = [r["text"] for r in _corpus() if r["pass"]]
        self.assertGreaterEqual(len(good), 15)
        for text in good:
            ok, _ = verify_module.verify_message(text, called="阿澈")
            self.assertTrue(ok, f"正常消息被拦：{text}")


class TestTruncateRespectsBrackets(unittest.TestCase):
    """切点不得落在未闭合括号内部。

    真实记录里 `(整理好餐具，翅膀微微收拢`、`(把毯子裹得紧紧的` 这类有十几条。
    注意：清洗链里**没有**任何一处会删 `）`，真凶是这里。
    """

    def test_corpus_samples_no_longer_dangle(self):
        for raw in (
            "⏎ (整理好餐具，翅膀微微收拢，把阳光都挡住了一半) ⏎ 刚忙完手头那点杂事，想赖在你怀里晒太阳。",
            "睡前撒个娇，顺便道个晚安 ⏎ (裹紧毛毯，困倦地靠在沙发上)",
        ):
            out = generator.MessageGenerator._clean_output(raw, 40, allow_emoji=True)
            if out:
                self.assertTrue(verify_module.paren_balanced(out),
                                f"截断后括号仍不配平：{out!r}")

    def test_body_wiped_out_means_drop_it(self):
        raw = "(整理好餐具，翅膀微微收拢，把阳光都挡住了一半) ⏎ 刚忙完"
        out = generator.MessageGenerator._clean_output(raw, 20, allow_emoji=True)
        self.assertIn(out, ("", None), f"正文被截光却还留下了半截括号：{out!r}")


class TestNoAnchorNoSend(unittest.TestCase):
    """没有由头就不开口——这是本轮最核心的行为变更。"""

    def test_derived_anchors_are_one_shot(self):
        u = {"last_seen": 0.0}
        body = {"day": {"doing": "改方案", "next": ["晚饭"]}, "weather": "外面在下雨"}
        now = 1_700_000_000.0
        first = anchors.prepare_round_anchors(u, body, now, limit=5)
        self.assertTrue(first, "真实状态应该派得出由头")
        # 同一份状态再要一次：除了新出现的沉默档位，不该再有新由头
        second = anchors.prepare_round_anchors(u, body, now, limit=5)
        fresh = [a for a in second
                 if a["about"] not in {x["about"] for x in first}]
        self.assertEqual(fresh, [], f"同一份状态又派生出新由头：{fresh}")

    def test_silence_anchor_does_not_change_every_day(self):
        """按天数逐日变化的沉默由头会永不耗尽——那正是 23 条复读的成因。"""
        u = {"last_seen": 0.0}
        now = 1_700_000_000.0
        seen = set()
        for day in range(2, 15):
            u["last_seen"] = now - day * 86400
            for a in anchors.prepare_round_anchors(u, {}, now, limit=5):
                seen.add(a["about"])
        silence = [s for s in seen if "没" in s]
        self.assertLessEqual(len(silence), 3, f"沉默由头每天变新：{silence}")

    def test_weather_dict_does_not_become_a_dict_repr(self):
        u = {"last_seen": 0.0}
        body = {"weather": {"env": "外面在下雨"}}
        picks = anchors.prepare_round_anchors(u, body, 1_700_000_000.0, limit=5)
        for a in picks:
            self.assertNotIn("{", a["about"], f"字典被字符串化了：{a['about']!r}")

    def test_no_anchor_yields_no_candidate(self):
        u = {"last_seen": 0.0}
        self.assertEqual(anchors.prepare_round_anchors(u, {}, 1_700_000.0, limit=5), [],
                         "什么都不知道的时候不该硬编一个由头")


class TestFairRotation(unittest.TestCase):
    """从不回复的人不该垄断全部预算。

    真实记录里 129 条有 100 多条落在同样 6 个人身上：不回复的人念头会一路涨到
    封顶 2.80，每轮都排第一；会回复的人发完就清零，永远排后面。
    """

    def test_sort_puts_least_recently_contacted_first(self):
        import social.engine as engine_mod
        a = {"last_sent": 1_700_000_000.0, "urge": 2.8}   # 刚发过、念头封顶
        b = {"last_sent": 1_600_000_000.0, "urge": 2.1}   # 很久没联系
        c = {"last_sent": 1_650_000_000.0, "urge": 2.5}
        rows = [("bid", "ua", a, a["urge"], {}), ("bid", "ub", b, b["urge"], {}),
                ("bid", "uc", c, c["urge"], {})]
        rows.sort(key=lambda x: (float(x[2].get("last_sent", 0) or 0), -x[3]))
        self.assertEqual([r[1] for r in rows], ["ub", "uc", "ua"])

    def test_engine_source_has_no_urge_first_sort(self):
        import social.engine as engine_mod
        text = Path(engine_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn('anchored.sort(key=lambda x: -x[3])', text,
                         "还在按念头降序排——封顶的人会永远排第一")
        self.assertNotIn("contenders", text.replace(
            "# 原来的 contenders（念头攒满就发）", ""),
            "纯念头那个桶还在")


class TestPromptAsksForTopicNotSpeaking(unittest.TestCase):
    """decide 问的是「想不想说」（关于说话），模型只能答祈使句。"""

    def _prompt(self):
        gen = generator.MessageGenerator(_ProviderCtx(), SocialConfig())
        out = _run(gen._compose(
            "umo", {"conversation": []}, "ctx", "reason",
            {"msg_type_desc": "说点什么", "anchor_fact": "TA之前说过的那个时间到了：面试"},
            "人设", None, decide=True, at=1_700_000_000.0,
        ))
        self.assertIsNotNone(out, "provider 不可用时跳过是正常的")
        return out[1]

    def test_no_longer_asks_whether_to_speak(self):
        text = self._prompt()
        self.assertNotIn("你到底想不想说点什么", text)

    def test_asks_which_thing(self):
        self.assertIn("这次你要跟 TA 说的是哪一件事", self._prompt())

    def test_anchor_is_given_as_a_fact(self):
        text = self._prompt()
        self.assertIn("已经定了，你不用再去找话题", text)
        self.assertIn("面试", text)

    def test_hash_line_instruction_asks_for_a_noun_phrase(self):
        text = self._prompt()
        self.assertIn("名词短语", text)
        self.assertNotIn("写给自己看的，不会发出去", text,
                         "还在教模型「这只是草稿」——它就会照抄")

    def test_material_constraint_no_longer_squeezes(self):
        text = self._prompt()
        self.assertNotIn("只用上面列出来的", text,
                         "「只能用这些」会反向逼出复读（模型唯一能说的就只剩饿了）")
        self.assertIn("别把「饿了、困了」当成唯一可说的事", text)


class TestHistoryBackfill(unittest.TestCase):
    def test_collect_gives_conversation(self):
        content = [
            {"role": "user", "content": "我下周要面试"},
            {"role": "assistant", "content": "加油"},
            {"role": "user", "content": "我下周要面试"},
        ]
        turns = history_ingest.recent_turns(content, limit=8)
        self.assertEqual(len(turns), 3, "三条都要进账本（用户重复说也要记）")
        self.assertEqual([t["dir"] for t in turns], ["in", "out", "in"])
        st = _tmp_state()
        rows = history_ingest.apply_history_rows(
            st, [{"bid": "b", "uid": "u", "umo": "x:y:u",
                  "last_seen": 1.0, "urge_at": 1.0,
                  "conversation": turns}])
        self.assertEqual(rows, 1)
        self.assertTrue(st.bot("b")["users"]["u"].get("conversation"))

    def test_extract_anchors_finds_concrete_things(self):
        turns = [{"dir": "in", "text": "我下周要去面试了，有点慌"},
                 {"dir": "in", "text": "对了体检也约在同一天"},
                 {"dir": "in", "text": "哈哈哈"},
                 {"dir": "out", "text": "没事的"}]
        found = history_ingest.extract_anchors(turns)
        self.assertTrue(found, "该从账本里挖出面试/体检这种事")
        joined = " ".join(found)
        self.assertIn("面试", joined)
        self.assertIn("体检", joined)
        self.assertNotIn("哈哈哈", joined, "笑声不是可回访的事")


def _tmp_state():
    import tempfile as _tf
    return SocialState(os.path.join(_tf.mkdtemp(), "s.json"))


# ─── v1.21.0：好感高的人也得收得到；主动内容要关于对方 ──────────────

class TestUrgeNotWipedByContact(unittest.TestCase):
    """对方聊得越勤，她越永远攒不起来。

    仿真实测（好感度同样 0.75）：对方从不回复 → 7 天主动发 20 条；
    对方每小时发一条 → **7 天 0 条**。插件于是只主动找不理她的人。
    """

    def test_urge_path_is_for_continuation_only(self):
        """收到消息把念头清零，对**接续型**是对的：对方刚说了话，她没必要再主动找。

        这个设计本身没错，错的是它曾是**唯一**一条路径——于是「刚聊过」等于
        「永远不会收到主动消息」，好感 90% 的人一条都收不到。v1.21 补的第二条
        通道（念想）不依赖念头，也不受这一条约束。
        """
        st = SocialState(os.path.join(tempfile.mkdtemp(), "s.json"),
                         time_source=lambda: 1_700_000_000.0)
        u = st.user("b", "u")
        u.update({"umo": "x:private:u", "message_count": 300, "interest": 0.75,
                  "urge": 2.4, "no_reply_streak": 0})
        st.record_incoming("b", "u", "在吗", hour_override=12)
        # v1.27.8（F3）：念头不再**完全**清零，改成软清零（保留三成）——
        # 否则对方勤发消息就永远攒不起来，主动消息一次都不触发。
        got = float(u.get("urge", 0.0) or 0.0)
        self.assertLess(got, 2.4, "收到消息该把念头压下去")
        self.assertGreater(got, 0.0, "但不该完全清零")

    def test_miss_channel_reaches_an_active_user(self):
        """每小时都回话的人，靠念想通道 7 天内也必须能收到——这才是真正的保证。"""
        st = SocialState(os.path.join(tempfile.mkdtemp(), "s.json"),
                         time_source=lambda: 0.0)
        u = st.user("b", "u")
        u.update({"umo": "x:private:u", "message_count": 300, "interest": 0.75,
                  "no_reply_streak": 0, "urge": 0.0, "urge_at": 0.0,
                  "active_hours": [0] * 24, "miss_day": "", "last_sent": 0.0,
                  "conversation": [{"dir": "in", "text": "今天好累"}]})
        clock = 1_700_000_000.0
        day_seen = set()
        for hour in range(24 * 7):
            clock += 3600
            st.record_incoming("b", "u", "在吗", hour_override=hour % 24)
            day = str(hour // 24)
            if u.get("miss_day") == day:
                continue
            if u.get("last_sent") and clock - float(u["last_sent"]) < 7200:
                continue
            picks = anchors.prepare_round_anchors(
                u, {}, clock, limit=1, affection=90.0, relational=True)
            if picks:
                day_seen.add(day)
                u["miss_day"] = day
                u["last_sent"] = clock
        self.assertGreaterEqual(len(day_seen), 3,
                                "每小时都回话的人 7 天里一天都没被念想到——"
                                "这正是「好感90%却一条没收到」")


class TestRelationalAnchors(unittest.TestCase):
    """真实 197 条记录里没有一条是关于「你」的。现在必须有。"""

    def _user(self):
        return {
            "last_seen": 1_700_000_000.0 - 8 * 3600,
            "conversation": [
                {"dir": "in", "text": "今天加班到现在，好累"},
                {"dir": "out", "text": "快回去睡吧"},
                {"dir": "in", "text": "对了下周三体检"},
            ],
        }

    def test_miss_needs_affection(self):
        T = 1_700_000_000.0
        high = anchors.derive_relations(self._user(), 85.0, T, set())
        low = anchors.derive_relations(self._user(), 20.0, T, set())
        self.assertIn("有点想你了", high, "好感 85 应该派得出想念")
        self.assertNotIn("有点想你了", low, "好感 20 不该说想念")

    def test_care_anchor_from_their_own_words(self):
        got = anchors.derive_relations(self._user(), 85.0, 1_700_000_000.0, set())
        self.assertTrue(any("累" in g for g in got), f"该从对方说过的话里派牵挂：{got}")

    def test_continue_anchor_uses_real_history(self):
        got = anchors.derive_relations(self._user(), 85.0, 1_700_000_000.0, set())
        self.assertTrue(any("体检" in g for g in got), f"该续上对方说过的事：{got}")

    def test_miss_not_picked_right_after_talking(self):
        """刚聊过不派想念（那不是想念，是打断）。"""
        u = self._user()
        u["last_seen"] = 1_700_000_000.0 - 600
        got = anchors.derive_relations(u, 85.0, 1_700_000_000.0, set())
        self.assertNotIn("有点想你了", got)

    def test_relational_pool_ignores_body_state(self):
        """这一类不碰「她今天在干什么」——那正是 197 条里的毛病。"""
        u = self._user()
        picks = anchors.prepare_round_anchors(
            u, {"day": {"doing": "改方案"}}, 1_700_000_000.0,
            limit=3, affection=85.0, relational=True)
        for a in picks:
            self.assertNotIn("刚忙完", a["about"])


class TestMissChannelBypassesRecentTalk(unittest.TestCase):
    """「刚聊过」对接话是对的，对「想你了」是错的。"""

    def test_miss_gate_skips_recent_talk(self):
        src = Path(engine_mod.__file__).read_text(encoding="utf-8")
        i = src.index("def _miss_pick")
        body = src[i:src.index("def _affection_of", i)]
        self.assertNotIn("recent_talk_minutes", body,
                         "念想通道不该被「刚聊过」拦住")
        self.assertNotIn("gate_reason(", body,
                         "念想通道不该走接话那一整套闸门")
        self.assertIn("_quiet_now", body, "但安静时段仍然要守")
        self.assertIn("_min_gap_left", body, "最小间隔仍然要守")

    def test_miss_requires_about_peer(self):
        """这一类整条没有第二人称就拦下——那是在讲自己的日记。"""
        ok, why = verify_module.verify_message(
            "今天这咖啡太淡了，喝了一口就放那了", require_about_peer=True)
        self.assertFalse(ok, "讲自己的一天的消息被当成「想念」发出去了")
        self.assertTrue(
            verify_module.verify_message("你今天忙到几点啊", require_about_peer=True)[0])

    def test_about_peer_check_is_off_for_normal_channels(self):
        """别的通道不查：祈使句没写「你」也是说给 TA 听的。"""
        self.assertTrue(
            verify_module.verify_message("刚熬的汤，趁热喝了吧")[0],
            "普通通道被误伤了")


class TestCorpusRelationalRatio(unittest.TestCase):
    """语料里关系向的占比：这是「不像人」最直接的度量。"""

    def _ratio(self, rows):
        import re
        peer = re.compile(r"你|您|TA|ta|他|她|咱|大家|人呢")
        good = [r["text"] for r in rows if r["pass"]]
        hit = sum(1 for t in good if peer.search(verify_module.strip_actions(t)))
        return hit / float(len(good) or 1)

    def test_baseline_ratio_is_recorded(self):
        rows = _corpus()
        ratio = self._ratio(rows)
        self.assertGreater(ratio, 0.30,
                           f"关系向占比 {ratio:.0%} 低于基线——说明这一类没在增加")


# ─── v1.21.0 全面扫：追问/问候的刷屏与误判 ──────────────────────────

class TestNoFloodToActiveUsers(unittest.TestCase):
    """真实反馈：好感 90% 却一条没收到；从不回的人一周收 12 条早午晚安。

    两个方向同时出问题，根因是闸门之间互相打架。
    """

    def test_short_question_is_not_a_brush_off(self):
        """「在吗」是两个字的问句，不是敷衍。"""
        for t in ("在吗", "你睡了吗", "？"):
            self.assertFalse(reasoning.is_thin(t), f"{t!r} 被当成敷衍了")
        for t in ("嗯", "好", "还行", "就那样", "不知道"):
            self.assertTrue(reasoning.is_thin(t), f"{t!r} 应该是敷衍")

    def test_thin_words_are_not_substring_matched(self):
        """词表里有「行」「好」「是」「对」这种单字，子串匹配会误伤大量正常消息。"""
        for t in ("今天好累", "银行今天休息吗", "但是明天有雨", "不对吧", "刚好下班"):
            self.assertFalse(reasoning.is_thin(t), f"{t!r} 被误判成敷衍")

    def test_presence_needs_real_silence(self):
        """「在吗」是给「人没了」用的，对方每小时都在聊就不该问。"""
        T = 1_700_000_000.0
        hourly = {"last_seen": T - 3600, "last_spoken": T - 3600,
                  "pending_result": "replied", "last_message": "在吗",
                  "conversation": [{"dir": "in", "text": "你好", "ts": T - 7200},
                                   {"dir": "out", "text": "在的", "ts": T - 7100},
                                   {"dir": "in", "text": "在吗", "ts": T - 3600},
                                   {"dir": "out", "text": "我在呢", "ts": T - 3600}]}
        kind, _ = threads.thread_reason(
            hourly, T, probe_after_seconds=720, presence_after_seconds=1800,
            max_seconds=4200, context_seconds=4200, presence_floor_seconds=4 * 3600.0)
        self.assertNotIn(kind, ("presence", "probe"),
                         "对方刚说过话还要被追问")

    def test_greeting_fires_only_in_its_window_once_a_day(self):
        """问候只在早/午/晚窗口内发，且每天每窗口一次。

        v1.27.1 修的真 bug：`_greet_pick` 原来既不查窗口、也不读它自己写的
        `greet_day`/`greet_kind`，还漏了总开关 `greeting_enabled`——结果关不掉，
        用户完全不回时 5 天里 7 条全是问候、间隔 30~40 分钟。
        现在窗口判定与「每天各一次」用现成的 `reasoning.greeting_window_kind` /
        `greeting_due`（这两个函数早写好、也早被 import，却从没被调用）。
        """
        src_txt = Path(engine_mod.__file__).read_text(encoding="utf-8")
        i = src_txt.index("def _greet_pick")
        body = src_txt[i:src_txt.index("def loop_gate", i)]
        self.assertIn("greeting_window_kind", body, "问候没有按窗口触发")
        self.assertIn("greeting_due", body, "问候没有「每窗口每天一次」")
        self.assertIn("greeting_enabled", body, "问候的总开关没接线")
        self.assertIn("_gap_left", body, "问候没有按档位的间隔")
        self.assertIn("_quiesced", body, "问候没有「停发」这道限制")

    def test_greeting_window_kind_and_due(self):
        """窗口判定与「每天各一次」的语义（现成函数，直接测）。"""
        from social import reasoning
        # 默认窗口：早 7~11、晚安 21~24、午间 12~14；凌晨 0~7 不开窗口（不该道晚安）
        self.assertEqual(reasoning.greeting_window_kind(7, (7, 11), (21, 24), (12, 14)), "morning")
        self.assertEqual(reasoning.greeting_window_kind(12, (7, 11), (21, 24), (12, 14)), "midday")
        self.assertEqual(reasoning.greeting_window_kind(22, (7, 11), (21, 24), (12, 14)), "night")
        self.assertIsNone(reasoning.greeting_window_kind(1, (7, 11), (21, 24), (12, 14)),
                          "凌晨不在任何问候窗口")
        self.assertIsNone(reasoning.greeting_window_kind(15, (7, 11), (21, 24), (12, 14)))
        # 每天每窗口一次；午间是兜底，今天已问候过就不再补
        u = {"greet_day": "2023-11-15", "greet_kind": "morning"}
        self.assertFalse(reasoning.greeting_due(u, "2023-11-15", "morning"))
        self.assertFalse(reasoning.greeting_due(u, "2023-11-15", "midday"))
        self.assertTrue(reasoning.greeting_due(u, "2023-11-15", "night"))
        self.assertTrue(reasoning.greeting_due(u, "2023-11-16", "morning"))

    def test_miss_channel_is_reachable_in_try_once(self):
        """早退条件必须逐个列出所有候选桶——漏一个，那条通道就永远走不到。

        漏掉 misses 时它排在最后，前面全空 → 早退 → 好感 90% 的人一条都收不到。
        """
        src_txt = Path(engine_mod.__file__).read_text(encoding="utf-8")
        i = src_txt.index("if not greets and not anchored")
        line = src_txt[i:src_txt.index("\n", src_txt.index("and not promises", i))]
        for name in ("greets", "anchored", "threads", "loops", "closers",
                     "promises", "misses"):
            self.assertIn(name, line, f"早退条件漏了 {name}——那条通道永远走不到")

    def test_silent_except_is_not_used_to_hide_bugs(self):
        """之前 `except Exception` 把一个 NameError 静默吞了，表现为「念想一条都发不出」。"""
        src_txt = Path(engine_mod.__file__).read_text(encoding="utf-8")
        i = src_txt.index("def _miss_pick")
        body = src_txt[i:src_txt.index("def _affection_of", i)]
        self.assertIn("logger.warning", body, "派发失败只 log 一句，排查时看不到线索")

    def test_cold_silence_gate_lives_in_the_speak_choke_point(self):
        """TA 一直不回时，非问候的主动开口要在唯一收口处被挡下来。

        实测：完全不回时 2 天 6 条（每 30 分钟换一种由头）。根因是
        `pending_result=="waiting"` 只在 gate_reason/loop_gate 查，问候/念想/分享
        全绕过了它。这道闸必须落在所有通道的交汇处—`_speak`。
        """
        src_txt = Path(engine_mod.__file__).read_text(encoding="utf-8")
        i = src_txt.index("async def _speak")
        j = src_txt.index("\n    def _why_now", i)
        body = src_txt[i:j]
        self.assertIn("silent_sent", body, "_speak 没有「一直不回就静下来」的闸")
        self.assertIn("pending_result", body, "_speak 没有「上一条还没回」的闸")
        self.assertIn("is_greet", body, "问候应当豁免静默（早/晚安不指望回）")

    def test_icebreak_prompt_carries_her_own_recent_lines(self):
        """破冰 prompt 要带上她自己最近在群里说过的话，否则会逐字重复。"""
        src_txt = Path(generator_mod.__file__).read_text(encoding="utf-8")
        i = src_txt.index('if mode in ("icebreak", "topic")')
        body = src_txt[i:src_txt.index("# mode == flow", i)]
        self.assertIn("own_recent", body, "破冰没带「自己说过什么」的防重复")


# ─── v1.22：关系热度分档 · 有效期 · 问候 · 停发 ──────────────────


class TestRelationTier(unittest.TestCase):
    """分档看的是「这段关系有多热」，不是好感度这个数字。"""

    T = 1_700_000_000.0

    def _u(self, msgs=30, sent=20, replied=15):
        return {
            "conversation": [
                {"dir": "in", "text": "在吗", "ts": self.T - 3600 * (i + 1)} for i in range(msgs)
            ],
            "proactive_sent": sent,
            "proactive_replied": replied,
        }

    def test_high_baseline_that_climbed_is_close(self):
        """有运营把初始好感设成 40 让人更好攻略，聊完直接 80——这种人是该被多找的。"""
        tier = relation_tier(self._u(), affection=80.0, base_affection=40.0, now=self.T)
        self.assertEqual(tier, "high")

    def test_high_number_that_never_answers_is_not_close(self):
        """停在 80 但从不回话的人，不该拿到「你们很熟」。"""
        tier = relation_tier(
            self._u(msgs=1, sent=1, replied=0), affection=80.0, base_affection=80.0, now=self.T
        )
        self.assertEqual(tier, "low")

    def test_low_affection_caps_the_tier(self):
        """数值低且没涨过，就算互动多也不能叫「很熟」——模型会拿着错的关系去开口。"""
        tier = relation_tier(self._u(), affection=20.0, base_affection=40.0, now=self.T)
        self.assertNotEqual(tier, "high")

    def test_no_baseline_falls_back_to_half(self):
        """Core 没给基线时不能崩，退回保守估法。"""
        tier = relation_tier(self._u(), affection=60.0, base_affection=None, now=self.T)
        self.assertIn(tier, ("mid", "high"))

    def test_tier_note_reads_like_a_sentence(self):
        note = tier_note("high", 80.0, 40.0)
        self.assertIn("很熟", note)
        self.assertIn("80", note)
        self.assertIn("40", note)

    def test_stranger_is_low(self):
        tier = relation_tier(self._u(msgs=0, sent=0, replied=0), affection=5.0,
                             base_affection=0.0, now=self.T)
        self.assertEqual(tier, "low", "素未谋面的人该在低档")


class TestAnchorTtl(unittest.TestCase):
    """由头带有效期，不是冷却。过期即作废，不补发。"""

    def test_three_families(self):
        self.assertEqual(anchors_mod.TTL_SELF_EVENT, 2 * 3600.0, "她这边刚发生的只有 2 小时")
        self.assertEqual(anchors_mod.TTL_DAY_PART, 3 * 3600.0)
        self.assertEqual(anchors_mod.TTL_PEER_EVENT, 24 * 3600.0, "对方那边的事隔一天问也正常")

    def test_derived_expires_in_two_hours(self):
        u = {}
        anchors_mod.add_anchor(u, anchors_mod.KIND_DERIVED, "刚忙完方案", now=1000.0)
        live = anchors_mod.live_anchors(u, 1000.0 + 3600.0)
        self.assertEqual(len(live), 1, "两小时以内还在")
        self.assertEqual(anchors_mod.live_anchors(u, 1000.0 + 3 * 3600.0), [],
                         "过了就该作废，不能补发")

    def test_expired_is_pruned_not_resurrected(self):
        u = {}
        anchors_mod.add_anchor(u, anchors_mod.KIND_DERIVED, "刚忙完方案", now=1000.0)
        anchors_mod.prune_anchors(u, 1000.0 + 3 * 3600.0)
        self.assertEqual(anchors_mod.live_anchors(u, 1000.0 + 3 * 3600.0), [])

    def test_greeting_anchor_exists(self):
        self.assertIn(anchors_mod.KIND_GREET, anchors_mod._KIND_TTL)


class TestNagInsteadOfCalendar(unittest.TestCase):
    """没有新事发生时，真人会随口说一句——安静本身就是理由。"""

    def test_nag_derived_from_silence(self):
        u = {"last_seen": 1_000_000.0, "conversation": []}
        got = anchors_mod.derive_nag(u, 1_000_000.0 + 8 * 3600.0, set())
        self.assertTrue(got, "隔了 8 小时该有句话可说")
        self.assertEqual(got[0].get("kind"), anchors_mod.KIND_GREET)

    def test_no_nag_when_just_talked(self):
        u = {"last_seen": 1_000_000.0, "conversation": []}
        self.assertEqual(anchors_mod.derive_nag(u, 1_000_000.0 + 60.0, set()), [])

    def test_nag_is_one_shot(self):
        u = {"last_seen": 1_000_000.0, "conversation": []}
        anchors_mod.derive_nag(u, 1_000_000.0 + 8 * 3600.0, set())
        again = anchors_mod.derive_nag(u, 1_000_000.0 + 8 * 3600.0 + 300.0, set())
        self.assertEqual(again, [], "同一句问候一辈子只说一次")


class TestQuiesce(unittest.TestCase):
    """试两次发不出去就停掉，除非对方先开口。"""

    def test_quiet_after_giving_up(self):
        u = {"quiesced_at": 2000.0, "last_seen": 1500.0}
        self.assertTrue(SocialEngine._quiesced(u))

    def test_speaks_again_after_they_reach_out(self):
        u = {"quiesced_at": 2000.0, "last_seen": 2500.0}
        self.assertFalse(SocialEngine._quiesced(u), "对方先说话了就该解封")

    def test_never_quiet_without_the_flag(self):
        self.assertFalse(SocialEngine._quiesced({"last_seen": 0.0}))


class TestTierReachesTheModel(unittest.TestCase):
    """模型得知道现在这个人跟她什么关系，否则它没法判断该不该开口。"""

    def test_tier_note_is_passed_into_meta(self):
        src_txt = Path(engine_mod.__file__).read_text(encoding="utf-8")
        i = src_txt.index("def _speak")
        body = src_txt[i:src_txt.index("\n    def ", i + 10)]
        self.assertIn("tier_note", body, "模型看不到关系档")

    def test_anchor_pick_takes_tier(self):
        import inspect
        sig = inspect.signature(engine_mod.SocialEngine._anchor_pick)
        self.assertIn("tier", sig.parameters, "由头池要按档位给上限")
        self.assertIn("warmth", sig.parameters, "涨幅要传给模型")


class TestNoDailyCapForClosePeople(unittest.TestCase):
    """高好感不设日上限——量由她当天有几件事决定，不由人拍的节拍器决定。"""

    def test_no_daily_cap_kwarg_in_greet_path(self):
        src_txt = Path(engine_mod.__file__).read_text(encoding="utf-8")
        i = src_txt.index("def _greet_pick")
        body = src_txt[i:src_txt.index("def loop_gate", i)]
        self.assertNotIn("daily_cap", body, "问候路径还有日上限")

    def test_pool_scales_with_tier(self):
        self.assertGreater(anchors_mod.TIER_POOL_MAX["high"],
                           anchors_mod.TIER_POOL_MAX["mid"])
        self.assertGreater(anchors_mod.TIER_POOL_MAX["mid"],
                           anchors_mod.TIER_POOL_MAX["low"])

class TestGreetingDoesNotRequirePeerRef(unittest.TestCase):
    """分享一件刚发生的事，不该被「必须出现第二人称」拦掉。

    这条我第一版写错了：把「念想」通道的那道验收顺手加到了问候路径上，结果实测
    一天 155 个候选里 84 个被拦掉，量直接归零——而被拦的例句是
    「楼下便利店的关东煮今天有蟹棒诶」，这恰恰是真人会发的话。
    """

    @staticmethod
    def _engine():
        return Path(engine_mod.__file__).read_text(encoding="utf-8")

    def test_greet_path_has_no_require_about_peer(self):
        src_txt = self._engine()
        i = src_txt.index("in greets:")
        j = src_txt.index("in sorted(threads,", i)
        self.assertNotIn(
            "require_about_peer=True", src_txt[i:j],
            "问候/分享路径又加上了「必须提到对方」——真人分享自己的事不会句句带「你」",
        )

    def test_miss_path_still_requires_peer_ref(self):
        """念想通道那道必须留着：没有正事只是想起一个人，整条不提对方就是在讲日记。"""
        src_txt = self._engine()
        i = src_txt.index("in misses:")
        j = src_txt.index("in closers:", i)
        self.assertIn("require_about_peer=True", src_txt[i:j],
                      "念想通道丢了「必须指向对方」这道验收")


class TestVetoDoesNotBurnTheAnchor(unittest.TestCase):
    """模型否决 ≠ 验收拦下。前者不消耗由头，后者消耗。"""

    @staticmethod
    def _engine():
        return Path(engine_mod.__file__).read_text(encoding="utf-8")

    def test_veto_branch_has_no_mark_failed(self):
        src_txt = self._engine()
        i = src_txt.index("if not decision.send:")
        j = src_txt.index("parts = decision.parts", i)
        self.assertNotIn("mark_failed", src_txt[i:j],
                         "否决不该消耗由头——那是「这个人此刻不该被找」，不是「这件事她驾驭不了」")

    def test_veto_still_backs_off(self):
        """但也不能完全当无事发生，否则同一条由头每轮心跳重试一次。"""
        src_txt = self._engine()
        i = src_txt.index("if not decision.send:")
        j = src_txt.index("parts = decision.parts", i)
        self.assertIn("VETO_BACKOFF_SECONDS", src_txt[i:j], "否决没有退避")

    def test_verify_rejection_still_burns_the_anchor(self):
        src_txt = self._engine()
        i = src_txt.index("ok, why = verify_message(")
        j = src_txt.index("_send_with_retry", i)
        self.assertIn("mark_failed", src_txt[i:j], "验收拦下必须消耗由头")

    def test_both_counted_separately(self):
        src_txt = self._engine()
        self.assertIn("veto_total", src_txt)
        self.assertIn("rule_total", src_txt)


class TestReadReplyFollowup(unittest.TestCase):
    """已读续接：对方回过（走正常聊天链路）之后又没声了，补的一句要接着那几句说。"""

    def test_recent_exchange_picks_inbound_only(self):
        from social.threads import recent_exchange
        u = {"conversation": [
            {"dir": "in", "text": "面试定了"},
            {"dir": "out", "text": "那挺好"},
            {"dir": "in", "text": "后天"},
            {"dir": "out", "text": "加油"},
        ]}
        self.assertEqual(recent_exchange(u, 2), ["面试定了", "后天"])

    def test_empty_without_history(self):
        from social.threads import recent_exchange
        self.assertEqual(recent_exchange({"conversation": []}, 3), [])
        self.assertEqual(recent_exchange({}, 3), [])

    def test_meta_carries_the_exchange(self):
        from social.threads import closer_meta
        m = closer_meta("那你先歇着", ["面试后来怎么样了"])
        self.assertEqual(m["exchange"], ["面试后来怎么样了"])
        self.assertIn("面试后来怎么样了", m["msg_type_desc"])
        self.assertIn("顺着", m["style_hint"])

    def test_meta_without_exchange_still_works(self):
        from social.threads import closer_meta
        m = closer_meta("那你先歇着")
        self.assertEqual(m["exchange"], [])
        self.assertIn("收个尾", m["msg_type_desc"])

    def test_prompt_uses_it(self):
        src_txt = Path(generator_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("_exchange", src_txt, "提示词没把对方最近说的话交给模型")


class TestTierVetoIsUniversal(unittest.TestCase):
    """模型否决对谁都成立，不分关系好坏。"""

    def test_prompt_says_so(self):
        src_txt = Path(generator_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("好感高不构成必须发消息的理由", src_txt,
                      "提示词没有说清「好感高不等于非发不可」")
        self.assertIn("对谁都成立", src_txt)

    def test_tier_note_overrides_bare_affection(self):
        src_txt = Path(generator_mod.__file__).read_text(encoding="utf-8")
        i = src_txt.index("def _relationship_line")
        j = src_txt.index("\n    def ", i + 10)
        self.assertIn("_tier_note", src_txt[i:j],
                      "关系描述还在只看好感度绝对值")

# ─── v1.23：跨用户撞车 · miss 门槛 · 早退指标 · 通用块清理 ──────────


class TestCrossUserCollision(unittest.TestCase):
    """同一 bot 近两小时发给别人的消息，太像就拦。

    真实记录：16:10~16:51 这 1 小时 41 分里，12 个不同的人分别收到
    「刚忙完个案笔记」。那不是复读，是 12 个人读了同一行字幕——因为 Core 的日程
    挂在角色级（`mood` 在用户级、`daily_schedule` 在角色级），这句话对所有人逐字相同。
    跟同一个人复读那道检查结构上就抓不到：日志按 (bid,uid) 分开存。
    """

    def test_catches_the_anchor_collision(self):
        from social.verify import _check_cross_user
        a = "刚忙完个案笔记，脑子还嗡嗡响，今天多云，空气湿凉。你今天有什么安排？"
        b = "刚忙完个案笔记，脑子还嗡嗡响，多云，空气湿凉。你今天有什么安排？"
        self.assertIsNotNone(
            _check_cross_user(a, [("别人", b)], threshold=0.6),
            "换掉两三个字也该认出来是同一句",
        )

    def test_different_messages_pass(self):
        from social.verify import _check_cross_user
        a = "刚忙完个案笔记，脑子还嗡嗡响"
        b = "楼下便利店的关东煮今天有蟹棒诶"
        self.assertIsNone(_check_cross_user(a, [("别人", b)], threshold=0.6))

    def test_ignores_actions_when_comparing(self):
        """括号动作各人不同，不该拿它当相似度的证据。"""
        from social.verify import _check_cross_user
        a = "刚忙完个案笔记（伸了个懒腰，翅膀舒展）"
        b = "刚忙完个案笔记（收拢宽大的翅膀，指尖轻揉眼角）"
        self.assertIsNotNone(_check_cross_user(a, [("别人", b)], threshold=0.6))

    def test_empty_inputs_pass(self):
        from social.verify import _check_cross_user
        self.assertIsNone(_check_cross_user("随便一句", [], threshold=0.6))
        self.assertIsNone(_check_cross_user("", [("别人", "随便一句")], threshold=0.6))

    def setUp(self):
        self.clock = 1_700_000_000.0

    def test_state_returns_only_other_people(self):
        st = SocialState(os.path.join(tempfile.mkdtemp(), "s.json"))
        st.user("b1", "me")["proactive_log"] = [
            {"ts": self.clock - 60, "text": "我刚说的"}
        ]
        st.user("b1", "him")["proactive_log"] = [
            {"ts": self.clock - 60, "text": "他刚说的"}
        ]
        got = st.recent_proactive_others("b1", "me", self.clock, hours=2.0)
        self.assertEqual([t for _w, t in got], ["他刚说的"])
        self.assertEqual([w for w, _t in got], ["him"])

    def test_window_is_respected(self):
        st = SocialState(os.path.join(tempfile.mkdtemp(), "s.json"))
        st.user("b1", "me")
        st.user("b1", "him")["proactive_log"] = [
            {"ts": self.clock - 10 * 3600, "text": "十小时前的"}
        ]
        self.assertEqual(st.recent_proactive_others("b1", "me", self.clock, hours=2.0), [])

    def test_result_sorted_newest_first(self):
        st = SocialState(os.path.join(tempfile.mkdtemp(), "s.json"))
        st.user("b1", "me")
        st.user("b1", "him")["proactive_log"] = [
            {"ts": self.clock - 1800, "text": "较早"}, {"ts": self.clock - 60, "text": "较近"}
        ]
        got = [t for _w, t in st.recent_proactive_others("b1", "me", self.clock, hours=2.0)]
        self.assertEqual(got, ["较近", "较早"])

    def test_engine_wires_it_into_verification(self):
        src = Path(engine_mod.__file__).read_text(encoding="utf-8")
        i = src.index("ok, why = verify_message(")
        j = src.index("_send_with_retry", i)
        self.assertIn("_check_cross_user", src[i:j], "验收里没接跨用户撞车检查")

    def test_config_exists(self):
        cfg = SocialConfig()
        self.assertEqual(cfg.cross_user_repeat_ratio, 0.6)
        self.assertEqual(cfg.cross_user_repeat_hours, 2.0)
        self.assertFalse(cfg.cross_user_calibrate)

    def test_zero_really_disables_it(self):
        """面板上写着「设 0 关闭」，那就得真的认 0——钳到 0.5 就是一句骗人的话。"""
        class _C:
            @staticmethod
            def get(key, default=None):
                return {"cross_user_repeat_hours": 0}.get(key, default)
        cfg = SocialConfig.from_astrbot(_C())
        self.assertEqual(cfg.cross_user_repeat_hours, 0.0)
        src = Path(engine_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("cross_user_repeat_hours > 0", src, "设 0 时没关掉检查")


class TestMissFloorByTier(unittest.TestCase):
    """念想通道的门槛按关系档位给，不是一个统一的 55。

    实测 Core 里 386 个有数值用户，中位 30.4，只有 5.4% 过 55——统一门槛的话，
    这条专程为「好感高的人也收得到」开的通道对 94.6% 的人是永久关着的。
    """

    def test_high_tier_has_lower_floor(self):
        eng = _FakeEngine(clock_ref=lambda: 1_700_000_000.0)
        high = eng._miss_floor("high")
        mid = eng._miss_floor("mid")
        low = eng._miss_floor("low")
        self.assertLess(high, mid, "关系热的人门槛要更低")
        self.assertGreater(low, mid, "说「想你了」给没怎么说过话的人很怪")
        # 中档直接取配置值（默认 42）；不写死数字，否则改默认值就要改测试
        self.assertEqual(mid, float(eng.cfg.miss_affection_min), "配置值当中间档用")

    def test_high_tier_lets_mid_values_in(self):
        """基线被设成 40、现在 55 的人——关系真的热，不该被 55 挡在外面。"""
        eng = _FakeEngine(clock_ref=lambda: 1_700_000_000.0)
        self.assertLess(eng._miss_floor("high"), 55.0)

    def test_miss_pick_uses_the_tier_floor(self):
        src = Path(engine_mod.__file__).read_text(encoding="utf-8")
        i = src.index("def _miss_pick")
        j = src.index("\n    def ", i + 10)
        self.assertIn("_miss_floor", src[i:j], "念想通道还在用硬编码的门槛")
        self.assertNotIn("aff < self.cfg.miss_affection_min", src[i:j])


class TestMetricsSurviveQuietRounds(unittest.TestCase):
    """安静的时候仪表盘不能是死的。"""

    def test_early_return_persists_metrics(self):
        src = Path(engine_mod.__file__).read_text(encoding="utf-8")
        i = src.index("没有人攒够念头")
        j = src.index("return", i)
        self.assertIn("_persist_metrics", src[i:j],
                      "早退路径没落指标，last_heartbeat 会一直停在 0")


class TestGenericBlockSanitizing(unittest.TestCase):
    """逐个记插件名是打地鼠——这个容器里 8 个插件抢同一份 prompt。"""

    def test_known_block(self):
        from social.sanitize import sanitize_incoming
        self.assertEqual(
            sanitize_incoming("好累<MemoryCompanion-Context><x>y</x></MemoryCompanion-Context>"),
            "好累",
        )

    def test_unknown_block_name(self):
        """没见过的插件名也要剥掉。"""
        from social.sanitize import sanitize_incoming
        self.assertEqual(
            sanitize_incoming("累了<FooBarPluginContext uid=webchat>blabla</FooBarPluginContext>"),
            "累了",
        )

    def test_half_angle_brackets_survive(self):
        """<3、<= 不是框架块，别误伤。"""
        from social.sanitize import sanitize_incoming
        raw = "i <3 你 5<=3 都对"
        self.assertEqual(sanitize_incoming(raw), raw)

    def test_normal_message_untouched(self):
        from social.sanitize import sanitize_incoming
        self.assertEqual(sanitize_incoming("早啊  太阳不错"), "早啊  太阳不错")

    def test_frame_marker_truncates(self):
        """剥不掉的块，靠特征兜底。"""
        from social.sanitize import sanitize_incoming
        self.assertEqual(
            sanitize_incoming("这句还行 uid=webchat 后面是框架"),
            "这句还行",
        )

# ─── v1.23.1：协议残留 + 首行标题 ─────────────────────────────────


class TestProtocolResidue(unittest.TestCase):
    """正文里混进独立成行的 SEND/NO/> 直接不发。

    真实记录里抓到过两条，一条把模型的整个思考过程当众发给了用户：
        「你今天打算怎么过？\nNO\n现在是下午三点，不符合早上早安的设定要求…」
        「别总自己闷着\n>」
    `_SEND_LEADING` 只剥开头的 SEND，正文里的一个都不管。
    """

    def _check(self, text):
        from social.verify import verify_message
        return verify_message(text, recent=[])

    def test_real_bare_no_line_is_rejected(self):
        ok, why = self._check(
            "你今天打算怎么过？\nNO\n现在是下午三点，不符合早上早安的设定要求，需要重新想一个理由")
        self.assertFalse(ok, "把模型的思考过程发给了用户")
        self.assertIn("协议残留", why)

    def test_real_quote_mark_line_is_rejected(self):
        ok, why = self._check("别总自己闷着\n>")
        self.assertFalse(ok)
        self.assertIn("协议残留", why)

    def test_send_with_colon_rejected(self):
        self.assertFalse(self._check("早安\nSEND:")[0])

    def test_no_inside_a_sentence_is_fine(self):
        """正文里出现 NO 是要紧的事，只有独立成行才算残留。"""
        self.assertTrue(self._check("NO 这个字母怎么读")[0])
        self.assertTrue(self._check("我说了 no 不去，你非要我去")[0])


class TestTitleLineStripping(unittest.TestCase):
    """首行标题：把「为什么现在说这件事」当成了开场白。

    229 条真实记录里 26 条是这个形状，首行全是 anchor_fact 的原话：
    刚忙完醒神 / 早餐时刻 / 处理完个案笔记 / 刚忙完早餐的早晨……
    对话框里那行像系统消息，不像人开口。
    """

    REAL_TITLES = [
        "刚忙完醒神", "清晨醒神", "醒神时刻", "晨间早餐", "早餐时间",
        "早餐时刻", "早餐时光", "晨间日常", "早餐与晨光",
        "处理完个案笔记", "处理个案笔记", "刚忙完的个案", "忙完个案笔记",
    ]

    def _strip(self, t):
        from social.verify import strip_title_line
        return strip_title_line(t)

    def test_every_real_title_gets_stripped(self):
        body = "早安，一只深蓝。脑袋快生锈了。来陪我说说话？"
        for title in self.REAL_TITLES:
            out = self._strip(f"{title}\n{body}")
            # 判首行而不是「整条里还有没有这几个字」——「忙完个案笔记」是
            # 「刚忙完个案笔记」的子串，用 assertNotIn 会误报。
            self.assertFalse(
                out.split("\n")[0].strip() == title,
                f"「{title}」还在首行：{out!r}",
            )
            self.assertTrue(out.startswith(body[:6]), f"正文没保住：{out!r}")

    def test_title_with_action_line_in_between(self):
        out = self._strip("早餐时刻\n(翅膀舒展，晨光洒在毛发上)\n早安，无家可归的猫。我在吃早餐。")
        self.assertTrue(out.startswith("(翅膀舒展"))

    def test_greeting_itself_is_not_a_title(self):
        """「早安\n(伸个懒腰)」剥掉早安就只剩动作了。"""
        for head in ("早安", "晚安", "在吗", "你好"):
            text = f"{head}\n(伸个懒腰，翅膀舒展)"
            self.assertTrue(self._strip(text).startswith(head), f"「{head}」被误剥了")

    def test_line_break_is_a_pause_not_a_title(self):
        """「早\n起了」剥掉就只剩「起了」——那条换行本来就是停顿。"""
        self.assertIn("\n", self._strip("早\n起了"))

    def test_single_line_untouched(self):
        self.assertEqual(self._strip("早安呀"), "早安呀")

    def test_bracket_action_is_not_a_title(self):
        """括号开头是语C动作，剥掉就只剩正文了。"""
        text = "（伸了个懒腰）\n\n今天天气真好"
        self.assertTrue(self._strip(text).startswith("（伸了个懒腰"))

    def test_first_person_and_fillers_are_not_titles(self):
        """「短+无人称+无句末标点」这个判据本身太松，会把人在开口说话当成小标题。

        实测被误伤的：「我先说」「嗯嗯」「说起来」「对了」。加一道否决：
        以第一人称/语气词/连接词开头的一律不当标题。
        """
        cases = {
            "我先说\n\n刚才那件事我想了下，还是算了": "我先说",
            "嗯嗯\n\n那我先去忙了，晚点聊": "嗯嗯",
            "说起来\n\n你上次说的那家店叫什么来着": "说起来",
            "在吗\n\n你今天看起来有点累": "在吗",
        }
        for text, head in cases.items():
            self.assertTrue(self._strip(text).startswith(head), f"「{head}」被误剥了")

    def test_only_a_title_is_rejected_not_sent(self):
        """剥完还是标题 = 整条只有一句标题没有正文。退回没用，换个标题再来一遍。"""
        from social.verify import verify_message
        ok, why = verify_message("处理完个案笔记", recent=[])
        self.assertFalse(ok)
        self.assertIn("标题", why)

    def test_generator_applies_the_strip(self):
        src = Path(generator_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("strip_title_line", src, "生成层没接剥标题")


class TestBurstFollowsFrameworkConfig(unittest.TestCase):
    """拆段跟随 AstrBot 每个 Bot 的「分段回复」设置（v1.27.0）。

    旧行为：插件用 burst_probability=0.62 / max_burst_parts=3 自己掷骰子拆段，
    与框架配置无关——没开分段的 Bot 也拆（连续说话），开了分段的 Bot 被拆两层
    （连续说话 + 一大堆输出）。现在拆法与间隔全部交给 pacing。
    """

    class _Ctx:
        def __init__(self, seg):
            self._seg = seg
        def get_config(self, umo=None):
            return {"platform_settings": {"segmented_reply": self._seg}}

    def test_disabled_framework_keeps_one_message(self):
        from social import pacing
        ctx = self._Ctx({"enable": False})
        segs, waits = pacing.plan_parts("第一句。第二句。", "umo", ctx, allow_burst=True)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0], "第一句。第二句。")
        self.assertEqual(waits, [])

    def test_enabled_framework_splits_by_its_regex(self):
        from social import pacing
        ctx = self._Ctx({"enable": True, "split_mode": "regex",
                         "regex": r".*?[。？！~…]+|.+$",
                         "words_count_threshold": 150})
        segs, waits = pacing.plan_parts("第一句。第二句。", "umo", ctx, allow_burst=True)
        self.assertEqual(len(segs), 2)
        self.assertEqual(len(waits), 1)

    def test_plugin_switch_overrides_framework(self):
        from social import pacing
        ctx = self._Ctx({"enable": True, "split_mode": "regex",
                         "regex": r".*?[。？！~…]+|.+$"})
        segs, waits = pacing.plan_parts("第一句。第二句。", "umo", ctx, allow_burst=False)
        self.assertEqual(len(segs), 1, "插件总开关关掉后不该再拆")

    def test_long_text_not_split(self):
        from social import pacing
        ctx = self._Ctx({"enable": True, "split_mode": "regex",
                         "regex": r".*?[。？！~…]+|.+$",
                         "words_count_threshold": 5})
        segs, _w = pacing.plan_parts("这一句超长会被框架整条直发。", "umo", ctx, allow_burst=True)
        self.assertEqual(len(segs), 1)

    def test_generator_no_longer_rolls_dice(self):
        src = Path(generator_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("self._burst_chance()", src,
                         "插件还在自己掷骰子决定拆不拆，说明没跟随框架配置")
        self.assertNotIn("burst_probability", src)


# ─── 多 bot 隔离（v1.27.4） ───────────────────────────────────────

class At:
    """模拟 AstrBot 的 At 消息段：类名必须就是 At（解析靠 __name__）。"""

    def __init__(self, qq):
        self.qq = qq


class AtAll:
    """模拟 AstrBot 的 AtAll。"""


class _MsgObj:
    def __init__(self, chain):
        self.message = chain


class _AtEvent:
    """只带消息链的事件，足够验证 _at_targets / _mentions_bot。"""

    def __init__(self, chain):
        self.message_obj = _MsgObj(chain)


class TestAtTargets(unittest.TestCase):
    """@ 目标解析是「是不是在叫我」的唯一硬依据，多 bot 同群全靠它分家。"""

    def test_collects_and_dedups_targets(self):
        ev = _AtEvent([At("1001"), "你好", At("1002"), At("1001")])
        self.assertEqual(SocialEngine._at_targets(ev), ["1001", "1002"])

    def test_at_all_counts_as_a_target(self):
        ev = _AtEvent([AtAll(), "公告"])
        self.assertEqual(SocialEngine._at_targets(ev), ["all"])

    def test_no_at_is_empty(self):
        self.assertEqual(SocialEngine._at_targets(_AtEvent(["随便聊聊"])), [])

    def test_mentions_bot_matches_only_self(self):
        ev = _AtEvent([At("1002"), "在吗"])
        self.assertTrue(SocialEngine._mentions_bot(ev, "1002"))
        self.assertFalse(SocialEngine._mentions_bot(ev, "1001"),
                         "@ 的是别人时不该算「在叫我」")

    def test_mentions_bot_all_is_not_self(self):
        ev = _AtEvent([AtAll()])
        self.assertFalse(SocialEngine._mentions_bot(ev, "1001"),
                         "@全体不算 @ 了本 bot")


class TestSiblingBots(unittest.TestCase):
    """多台 bot 同进程时，别把兄弟 bot 当成群友。"""

    def test_sibling_set_excludes_self_and_default(self):
        e = _SiblingEngine()
        e.state.bot("botA")
        e.state.bot("botB")
        e.state.bot("default")
        self.assertEqual(e._sibling_bots("botA"), {"botB"})

    def test_single_bot_deployment_has_no_siblings(self):
        e = _SiblingEngine()
        e.state.bot("botA")
        self.assertEqual(e._sibling_bots("botA"), set())

    def test_sibling_message_does_not_open_flow(self):
        """兄弟 bot 发言不能触发本台的心流接话（这就是串台的起点）。"""
        e = _SiblingEngine()
        e.state.bot("botB")
        self.assertTrue(e._is_sibling("botB"))
        self.assertFalse(e._is_sibling("u1"))


class _SiblingEngine:
    _sibling_bots = SocialEngine._sibling_bots

    def __init__(self):
        self.state = SocialState(os.path.join(tempfile.mkdtemp(), "s.json"))

    def _is_sibling(self, uid):
        return uid in self._sibling_bots("botA")


# ─── 输入状态（正在输入）:主动消息生成期间上报 ───────────────────

class TestInputState(unittest.TestCase):
    """做法与 Core 同源，唯一区别是客户端要从 umo 反查平台实例。"""

    def _notifier(self, ctx, enabled=True):
        from social.input_state import InputStateNotifier
        cfg = type("C", (), {"input_state_enabled": enabled})()
        return InputStateNotifier(ctx, lambda: cfg, _QuietLog(), time_source=lambda: __import__("time").monotonic())

    def test_reports_private_napcat_and_stops(self):
        ctx = _FakeCtx(platform_name="aiocqhttp")
        n = self._notifier(ctx)
        n.interval = 0.01
        n.timeout = 5.0

        async def go():
            await n.start("aiocqhttp:FriendMessage:12345", "12345")
            await asyncio.sleep(0.05)
            await n.stop("aiocqhttp:FriendMessage:12345")

        asyncio.run(go())
        calls = ctx.inst.client.api.calls
        self.assertTrue(calls, "私聊 + NapCat 时应该上报")
        self.assertTrue(all(c["action"] == "set_input_status" for c in calls))
        self.assertTrue(all(c["user_id"] == "12345" for c in calls), "user_id 必须是字符串")
        self.assertTrue(all(c["event_type"] == 1 for c in calls))

    def test_group_and_other_platform_and_disabled_are_skipped(self):
        cases = [
            (_FakeCtx(platform_name="aiocqhttp"), "aiocqhttp:GroupMessage:999", True),
            (_FakeCtx(platform_name="qq_official"), "qq_official:FriendMessage:1", True),
            (_FakeCtx(platform_name="aiocqhttp"), "aiocqhttp:FriendMessage:1", False),
        ]
        for ctx, umo, enabled in cases:
            n = self._notifier(ctx, enabled=enabled)
            n.interval = 0.01

            async def go(n=n, umo=umo):
                await n.start(umo, "1")
                await asyncio.sleep(0.03)
                await n.stop(umo)

            asyncio.run(go())
            self.assertEqual(ctx.inst.client.api.calls, [], f"{umo}（enabled={enabled}）不该上报")
            self.assertEqual(n._tasks, {})

    def test_no_platform_instance_does_not_crash(self):
        ctx = _FakeCtx(platform_name="aiocqhttp", missing=True)
        n = self._notifier(ctx)
        n.interval = 0.01

        async def go():
            await n.start("aiocqhttp:FriendMessage:1", "1")  # 不该抛
            await n.stop("aiocqhttp:FriendMessage:1")

        asyncio.run(go())
        self.assertEqual(n._tasks, {})

    def test_speak_wrapper_stops_typing_on_every_path(self):
        """_speak 的 finally 要覆盖所有退出路：正常返回、抛异常，都要停一次。"""
        for mode in ("ok", "raise"):
            stopped = {"n": 0}

            class FakeInputState:
                async def start(self, umo, uid): pass
                async def stop(self, umo): stopped["n"] += 1

            class E:
                _speak = SocialEngine._speak

                def __init__(self):
                    self.input_state = FakeInputState()

                    async def inner(*a, **k):
                        if mode == "raise":
                            raise RuntimeError("炸了")
                        return False, "想过，但觉得现在不该说"

                    self._speak_inner = inner

            e = E()
            u = {"umo": "aiocqhttp:FriendMessage:1"}
            if mode == "ok":
                ok, _note = _run(e._speak("bot1", "1", u, 2.0, 1000.0))
                self.assertFalse(ok)
            else:
                with self.assertRaises(RuntimeError):
                    _run(e._speak("bot1", "1", u, 2.0, 1000.0))
            self.assertEqual(stopped["n"], 1, f"{mode}：不管走哪条路退出都要停一次")


class _QuietLog:
    def debug(self, *a): pass
    def info(self, *a): pass
    def warning(self, *a): pass
    def error(self, *a): pass


class _PlatformInst:
    def __init__(self, name):
        self._name = name
        self.client = _Client()

    def meta(self):
        return type("M", (), {"name": self._name})()

    def get_client(self):
        return self.client


class _Client:
    def __init__(self):
        self.api = self
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append({"action": action, **params})


class _FakeCtx:
    def __init__(self, platform_name="aiocqhttp", missing=False):
        self.inst = None if missing else _PlatformInst(platform_name)
        self._platform_name = platform_name

    def get_platform_inst(self, platform_id):
        return self.inst


# ─── v1.27.6 新增：关系档手动覆盖 / 主动消息预览 ───────────────────

class TestTierOverride(unittest.TestCase):
    """手动指定关系档：设了的人不再按好感涨幅推算。"""

    def test_parses_both_spellings(self):
        cfg = SocialConfig.from_astrbot({"tier_override": ["12345:high", "low:67890"]})
        self.assertEqual(cfg.tier_override_for("12345"), "high")
        self.assertEqual(cfg.tier_override_for("67890"), "low")
        self.assertEqual(cfg.tier_override_for("999"), "")

    def test_accepts_list_and_full_width_colon(self):
        cfg = SocialConfig.from_astrbot({"tier_override": ["777：mid"]})
        self.assertEqual(cfg.tier_override_for("777"), "mid")

    def test_ignores_garbage_entries(self):
        cfg = SocialConfig.from_astrbot(
            {"tier_override": ["不是一对", "123:不是档位", None]})
        self.assertEqual(cfg.tier_override_for("123"), "")

    def test_engine_tier_of_respects_override(self):
        """覆盖值直接进 _tier_of：低好感的人也能被指定成高档。"""

        class Core:
            def load_snapshot(self, bid, uid, root=None):
                return {"affection": 10.0, "base_affection": 10.0}

        e = _FakeEngine(clock_ref=lambda: 1_700_000_000.0)
        e.cfg = SocialConfig.from_astrbot({"tier_override": ["u1:high"]})
        e.core = Core()
        e._tier_of = SocialEngine._tier_of.__get__(e, _FakeEngine)
        root = {"roles": {}}  # 非空即会去读 snapshot
        tier, _aff, _warmth = e._tier_of("bot1", "u1", {}, root, 1_700_000_000.0)
        self.assertEqual(tier, "high", "手动指定的高档被低好感覆盖了")
        tier2, _aff2, _w2 = e._tier_of("bot1", "u2", {}, root, 1_700_000_000.0)
        self.assertEqual(tier2, "low", "没被指定的人应当按低好感落到低档")


class TestProactivePreview(unittest.TestCase):
    """`/主动消息预览`：只生成、不发送、不消费由头、不动冷却。"""

    def _engine(self, ranked=None, reply="预览内容"):
        e = _PreviewEngine(ranked=ranked or [], reply=reply)
        return e

    def test_preview_returns_text_and_does_not_send(self):
        e = self._engine(ranked=_ranked())
        out = _run(e.preview_once("bot1"))
        self.assertIn("预览", out)
        self.assertIn("预览内容", out)
        self.assertEqual(e.sent, [], "预览路径不允许发送任何消息")

    def test_preview_can_target_one_user(self):
        e = self._engine(ranked=_ranked())
        out = _run(e.preview_once("bot1", "u2"))
        self.assertIn("u2", out)
        self.assertNotIn("u1", out.split("\n")[0])

    def test_preview_reports_unknown_target(self):
        e = self._engine(ranked=_ranked())
        out = _run(e.preview_once("bot1", "nobody"))
        self.assertIn("nobody", out)

    def test_preview_does_not_consume_anchor(self):
        """验收不过的由头进冷却 7 天——预览连一次都不该消耗。"""
        e = self._engine(ranked=_ranked())
        u = e.state.user("bot1", "u1")
        anchors.add_anchor(u, anchors.KIND_MISS, "有点想你了", now=e._time())
        before = [dict(a) for a in anchors.load_anchors(u)]
        _run(e.preview_once("bot1"))
        after = [dict(a) for a in anchors.load_anchors(u)]
        self.assertEqual(before, after, "预览动了由头池")

    def test_preview_refuses_when_engine_not_running(self):
        e = self._engine()
        e.running = False
        out = _run(e.preview_once("bot1"))
        self.assertIn("没有在运行", out)

    def test_preview_uses_generate_when_gate_off(self):
        """关掉 llm_gate 时走 generate 分支，同样只拿文本不发送。"""
        e = self._engine(ranked=_ranked())
        e.cfg.llm_gate = False
        out = _run(e.preview_once("bot1"))
        self.assertIn("预览内容", out)
        self.assertEqual(e.generator.generate_calls, 1)
        self.assertEqual(e.generator.decide_calls, 0)


class _PreviewEngine:
    """只拼 preview_once/_preview_one 用到的那几个依赖。"""

    _trigger_precheck = SocialEngine._trigger_precheck
    _remember_clock = SocialEngine._remember_clock
    _remember_night = SocialEngine._remember_night
    _representative_umo = SocialEngine._representative_umo
    _user_lock = SocialEngine._user_lock
    _generation_conversation = SocialEngine._generation_conversation
    _load_session_history = SocialEngine._load_session_history
    _mind = SocialEngine._mind
    _memory_bridge = SocialEngine._memory_bridge
    _fetch_memory_note = SocialEngine._fetch_memory_note
    _tier_of = SocialEngine._tier_of
    preview_once = SocialEngine.preview_once
    _preview_run = SocialEngine._preview_run
    _preview_one = SocialEngine._preview_one
    _verify_all_segments = staticmethod(SocialEngine._verify_all_segments)

    def __init__(self, ranked, reply):
        import tempfile as _tf

        self.running = True
        self.cfg = SocialConfig.from_astrbot({})
        self.core = _NullCore()
        self.context = None
        self._last_persona = {}
        self._city_offset = {}
        self._night_windows = {}
        self._user_locks = {}
        self.state = SocialState(os.path.join(_tf.mkdtemp(), "state.json"))
        self._ranked = ranked
        self._reply = reply
        self.sent = []
        self._last_flush = 1.0
        self.generator = _PreviewGen(reply)
        for uid, _u, _g in ranked:
            self.state.user("bot1", uid)["umo"] = f"aiocqhttp:FriendMessage:{uid}"

    async def _get_provider(self, umo):
        return None

    def _time(self):
        return 1_700_000_000.0

    def _moment_of(self, bid, now):
        import datetime

        return datetime.datetime(2023, 11, 15, 14, 0)

    def _clock_offset_for(self, bid):
        return None

    async def _persona(self, umo, bid=""):
        return "", ""

    def settle_minds(self, bid, now, dt, root, bot_state, min_urge=0.0):
        return self._ranked

    def log(self, _msg):
        pass


class _PreviewGen:
    """预览路径的生成器替身：decide 与 generate 都直接返回预设内容。"""

    def __init__(self, reply):
        self.reply = reply
        self.decide_calls = 0
        self.generate_calls = 0

    class _D:
        def __init__(self, reply):
            self.send = True
            self.parts = [reply]
            self.why_not = ""

    def _scope_of(self, provider):
        return "p1"

    def llm_backoff_remaining(self, scope=""):
        return 0.0

    async def decide(self, *a, **k):
        self.decide_calls += 1
        return self._D(self.reply)

    async def generate(self, *a, **k):
        self.generate_calls += 1
        return [self.reply]


# ─── v1.27.7 回归：死配置 / 预览无副作用 / 逐段验收 / 兄弟 bot 名单 ─────────

class TestInputStateConfigure(unittest.TestCase):
    """面板上的 interval/timeout 要真的吃到，不然永远按构造默认值跑。"""

    def test_panel_values_reach_the_notifier(self):
        cfg = SocialConfig.from_astrbot(
            {"input_state_interval_seconds": 2.0, "input_state_timeout_seconds": 90.0}
        )
        from social.input_state import InputStateNotifier as N
        n = N(None, lambda: cfg, _QuietLog(), time_source=lambda: 0.0)
        # 引擎 __init__ 里就是这样把它对齐面板值的
        n.configure(cfg.input_state_interval_seconds, cfg.input_state_timeout_seconds)
        self.assertEqual(n.interval, 2.0)
        self.assertEqual(n.timeout, 90.0)

    def test_engine_init_calls_configure(self):
        src = Path(engine_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("self.input_state.configure(", src, "引擎构造时没把面板值传给上报器")


class _FlowEngine:
    """只拼 _verify_all_segments / _flow_reply_inner 验收所需的最小依赖。"""

    _verify_all_segments = staticmethod(SocialEngine._verify_all_segments)

    def __init__(self):
        self._last_persona = {}


class TestVerifyAllSegments(unittest.TestCase):
    """群心流/破冰的验收要盖住**每一段**，不只 parts[0]。"""

    def test_second_segment_gets_checked(self):
        e = _FlowEngine()
        segs = ["今天挺好的", "想表达此刻的孤独和饥饿感"]
        bad, why = e._verify_all_segments(segs, recent=[], called="")
        self.assertEqual(bad, segs[1], "后续段没被验收，动机泄漏会直接进群")
        self.assertIn("动机", why)

    def test_all_clean_returns_empty(self):
        e = _FlowEngine()
        bad, why = e._verify_all_segments(["今天天气不错呀，你那边呢", "你吃饭了没"], recent=[], called="")
        self.assertEqual((bad, why), ("", ""))

    def test_allow_short_only_for_group_flow(self):
        e = _FlowEngine()
        # 短应和默认被拦
        bad, _ = e._verify_all_segments(["嗯"], recent=[], called="")
        self.assertEqual(bad, "嗯")
        # 群心流开了 allow_short 就放行
        bad2, _ = e._verify_all_segments(["嗯"], recent=[], called="", allow_short=True)
        self.assertEqual(bad2, "")


class TestPreviewNoSideEffects(unittest.TestCase):
    """预览是 dry run：不能改念头/兴趣/冷落，也不能动由头。"""

    def test_preview_restores_user_subtree(self):
        e = _PreviewEngine(ranked=_ranked(), reply="预览内容")
        u = e.state.user("bot1", "u1")
        u["urge"] = 1.234
        u["interest"] = 0.5
        anchors.add_anchor(u, anchors.KIND_MISS, "想起你了", now=e._time())
        before = json.loads(json.dumps(e.state.bot("bot1").get("users", {})))

        # 模拟真实 settle_minds 那个**就地写回**：它会把 urge/interest 改掉、
        # 还可能推进冷落计数。预览必须把这些全部还原。
        def mutating(bid, now, dt, root, bot_state, min_urge=0.0):
            victim = e.state.user(bid, "u1")
            victim["urge"] = 9.9
            victim["interest"] = 0.99
            victim["no_reply_streak"] = 7
            return e._ranked

        e.settle_minds = mutating
        _run(e.preview_once("bot1"))
        after = e.state.bot("bot1").get("users", {})
        self.assertEqual(
            after.get("u1", {}).get("urge"), before.get("u1", {}).get("urge"),
            "预览改了念头（settle_minds 的就地副作用没被还原）",
        )
        self.assertEqual(
            after.get("u1", {}).get("interest"), before.get("u1", {}).get("interest")
        )
        self.assertEqual(after.get("u1", {}).get("no_reply_streak"), 0)
        self.assertEqual(
            [dict(a) for a in anchors.load_anchors(after.get("u1", {}))],
            [dict(a) for a in anchors.load_anchors(before.get("u1", {}))],
        )

    def test_preview_skips_hot_chat_user(self):
        """刚聊过的人真实不会开口，预览也不该拿它当样例。"""
        now = 1_700_000_000.0
        ranked = [("u1", {"umo": "aiocqhttp:FriendMessage:u1", "last_seen": now - 5.0}, 3.0)]
        e = _PreviewEngine(ranked=ranked, reply="预览内容")
        out = _run(e.preview_once("bot1"))
        self.assertNotIn("预览内容", out, "对刚聊过的人生成了内容")
        self.assertEqual(e.generator.generate_calls, 0)


class TestKnownBotIds(unittest.TestCase):
    """只混群、从不私聊的兄弟 bot 也要能被认出来。"""

    def test_note_and_read_known_bot_ids(self):
        st = SocialState(os.path.join(tempfile.mkdtemp(), "s.json"))
        st.note_bot_id("botB")
        self.assertIn("botB", st.known_bot_ids())
        st.note_bot_id("default")  # 占位名不记
        self.assertNotIn("default", st.known_bot_ids())

    def test_sibling_bots_includes_group_only_bot(self):
        e = _SiblingEngine()
        e.state.note_bot_id("botC")  # 只在群里见过、没私聊过 → 不在顶层 bots
        self.assertIn("botC", e._sibling_bots("botA"))


class TestTierOverrideListElement(unittest.TestCase):
    """列表元素里手填的「a:high,b:low」也要能拆开。"""

    def test_comma_inside_list_element(self):
        cfg = SocialConfig.from_astrbot({"tier_override": ["u1:high,u2:low"]})
        self.assertEqual(cfg.tier_override_for("u1"), "high")
        self.assertEqual(cfg.tier_override_for("u2"), "low")


# ─── v1.27.8 回归：指令识别 / 冷静期与气没消闸门 ─────────────────────

class TestCommandDetection(unittest.TestCase):
    """F1：别的插件的指令（含被剪掉 `/` 的）不该被当成聊天记进观察账本。"""

    @staticmethod
    def _ev(chain_text=None, wake=True):
        comps = [types.SimpleNamespace(text=chain_text)] if chain_text is not None else []
        return types.SimpleNamespace(
            message_obj=types.SimpleNamespace(message=comps),
            is_at_or_wake_command=wake,
        )

    def test_chain_slash_is_command(self):
        from social.engine import _looks_like_command
        # 框架把 `/` 从 message_str 剪掉后，文本里没有 `/`，但原始消息链里还在
        self.assertTrue(_looks_like_command(self._ev("/别的插件指令"), "别的插件指令", "别的插件指令"))

    def test_registered_name_with_wake_is_command(self):
        from social.engine import _looks_like_command
        self.assertTrue(_looks_like_command(self._ev(), "触发社交", "触发社交"))

    def test_registered_name_without_wake_is_not_command(self):
        from social.engine import _looks_like_command
        self.assertFalse(_looks_like_command(self._ev(wake=False), "触发社交", "触发社交"))

    def test_normal_chat_is_not_command(self):
        from social.engine import _looks_like_command
        self.assertFalse(_looks_like_command(self._ev(), "今天天气不错", "今天天气不错"))


class _CoolCore:
    """只回一个用户快照的假 Core 桥。"""

    def __init__(self, mood):
        self._mood = mood

    def load_snapshot(self, bid, uid):
        return {"mood": self._mood}


class TestCoreMoodGate(unittest.TestCase):
    """D4/C5：Core 那边她冷静期 / 气没消时，不主动找这个人。"""

    def _engine(self, mood):
        class E:
            gate_reason = SocialEngine.gate_reason

            def __init__(self):
                self.cfg = SocialConfig.from_astrbot({})
                self.core = _CoolCore(mood)
                self._city_offset = {}

            def _moment_of(self, bid, now):
                import datetime
                return datetime.datetime(2023, 11, 15, 14, 0)

            def _quiet_now(self, bid, hour):
                return False
        return E()

    def test_cooling_blocks(self):
        now = 1_700_000_000.0
        eng = self._engine({"cool_no_proactive_until": now + 3600})
        self.assertIn("气", eng.gate_reason("bot1", "u1", {}, now, None))

    def test_high_aggression_blocks(self):
        now = 1_700_000_000.0
        eng = self._engine({"aggression": 45.0, "base_aggression": 28.0})
        self.assertIn("气", eng.gate_reason("bot1", "u1", {}, now, None))

    def test_calm_passes(self):
        now = 1_700_000_000.0
        eng = self._engine({"aggression": 30.0, "base_aggression": 28.0})
        self.assertEqual(eng.gate_reason("bot1", "u1", {}, now, None), "")


class TestSegmentVerification(unittest.TestCase):
    """私聊主动：`parts[0]` 过了**不代表后续段也过**。

    模型常在结尾再补一句纯括号的内心（「（记住了 宝宝16岁…）」），拆段后那一段会
    单独发出去，看起来就是「内心独白被当成发言发了一条」，而且和上一条重复。
    旧版只验 parts[0]，这一路完全免检（v1.27.10 修）。
    """

    def test_later_pure_parenthetical_segment_rejected(self):
        bad, why = SocialEngine._verify_all_segments(
            ["记住了，下次不会再忘了", "（记住了 宝宝16岁 以后绝对不会再忘了 原谅人家嘛）"],
            recent=[], called="",
        )
        self.assertTrue(bad, "纯括号的后续段没被拦下")
        self.assertIn("括号", why)

    def test_all_speech_segments_pass(self):
        bad, why = SocialEngine._verify_all_segments(
            ["在忙吗", "刚忙完，想起你了"], recent=[], called="",
        )
        self.assertEqual(bad, "", f"正常两段被误拦：{why}")

    def test_require_about_peer_reaches_every_segment(self):
        bad, why = SocialEngine._verify_all_segments(
            ["想你了，在干嘛", "今天天气不错"], recent=[], called="",
            require_about_peer=True,
        )
        self.assertTrue(bad, "念想通道的第二段没有第二人称，该被拦")


class TestGroupOpenerPrompts(unittest.TestCase):
    """G1/G3/K1/K2：群聊几种开口模式的提示词要拼得出来，且带群聊边界。"""

    def _gen(self, **conf):
        return generator.MessageGenerator(_PlainCtx(), SocialConfig.from_astrbot(conf))

    def test_icebreak_carries_topic_hint(self):
        p, _ = self._gen()._compose_group(
            "icebreak", {"topic_hint": "- 今天是中秋节"}, "人设", 40)
        self.assertIn("中秋", p)
        self.assertIn("群聊", p)

    def test_topic_mode_is_framed_as_starting_a_topic(self):
        p, _ = self._gen()._compose_group("topic", {}, "人设", 40)
        self.assertIn("想开个新话头", p)

    def test_group_boundary_forbids_starting_intimate_topics(self):
        p, _ = self._gen()._compose_group(
            "flow", {"latest": {"name": "甲", "text": "在吗"}}, "人设", 40)
        self.assertIn("群聊", p)
        self.assertIn("色情", p)

    def test_member_names_shown_when_enabled(self):
        p, _ = self._gen(group_mention_member=True)._compose_group(
            "flow", {"members": ["小明", "小红"]}, "人设", 40)
        self.assertIn("小明", p)

    def test_member_names_hidden_when_disabled(self):
        p, _ = self._gen(group_mention_member=False)._compose_group(
            "flow", {"members": ["小明"]}, "人设", 40)
        self.assertNotIn("小明", p)


class TestMemoryBridge(unittest.TestCase):
    """主动消息接 memory_companion：查宿主注册表、读写、没装降级。"""

    def _meta(self, name, bridge=None, activated=True):
        module = types.SimpleNamespace(
            get_memory_companion_bridge=(lambda: bridge) if bridge is not None else None
        )
        return types.SimpleNamespace(
            name=name, display_name=name, root_dir_name=name, module_path="",
            activated=activated, module=module,
        )

    def _ctx(self, stars):
        return types.SimpleNamespace(get_all_stars=lambda: list(stars))

    def test_finds_bridge_via_registry(self):
        bridge = object()
        ctx = self._ctx([self._meta("astrbot_plugin_memory_companion", bridge)])
        self.assertIs(memory_bridge.find_memory_bridge(ctx), bridge)

    def test_none_when_no_registry_api(self):
        self.assertIsNone(memory_bridge.find_memory_bridge(types.SimpleNamespace()))
        self.assertIsNone(memory_bridge.find_memory_bridge(None))

    def test_none_when_not_installed(self):
        ctx = self._ctx([self._meta("astrbot_plugin_other", object())])
        self.assertIsNone(memory_bridge.find_memory_bridge(ctx))

    def test_none_when_inactive(self):
        ctx = self._ctx([self._meta("astrbot_plugin_memory_companion", object(), activated=False)])
        self.assertIsNone(memory_bridge.find_memory_bridge(ctx))

    def test_fetch_returns_text(self):
        class _B:
            async def compose_context(self, **kw):
                return "她记得 TA 上周提过猫吐了"

        text = _run(memory_bridge.fetch_memory_text(_B(), "umo", "u1"))
        self.assertIn("猫吐了", text)

    def test_fetch_degrades_on_error(self):
        class _B:
            async def compose_context(self, **kw):
                raise RuntimeError("boom")

        self.assertEqual(_run(memory_bridge.fetch_memory_text(_B(), "umo", "u1")), "")

    def test_write_passes_user_id_and_content(self):
        seen = {}

        class _B:
            async def record_external_memory(self, **kw):
                seen.update(kw)

        _run(memory_bridge.write_proactive(_B(), "u1", "在吗", umo="umo", ts=123.0))
        self.assertEqual(seen.get("user_id"), "u1")
        self.assertIn("在吗", seen.get("content", ""))
        self.assertEqual(seen.get("source_plugin"), "astrbot_plugin_autonomous_social")

    def test_engine_switch_off_returns_none(self):
        class _E:
            cfg = SocialConfig.from_astrbot({"memory_bridge_enabled": False})
            context = None

        self.assertIsNone(SocialEngine._memory_bridge(_E()))


if __name__ == "__main__":
    unittest.main()
