"""social v2.0.0 engine 域修复的回归测试（P1-1 防回归 + P2/P3 各项）。

对应审核报告（C:/文件审核social/审核报告-...v1.28.17.md）里的 engine 侧问题：
P1-1（tag 版 30 行特别关心闸门防回归）、P2-01（ignore_quiet）、P2-02（min_messages）、
P2-03（发送失败冷却统一）、P2-07（记忆桥 TTL + 状态页）、P2-08/P2-09（门面重试/存活）、
P2-12（on 模式不读 state.json）、P2-13（门面 brief 消费）、P3-06（最后通牒）、
P3-09（墙钟回拨）、P3-10（provider 预检）。

测试全部用本仓库既有的最小 astrbot 桩（不入网、不装依赖）。
"""

from __future__ import annotations

import inspect
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = (
    _HERE.parent
    if (_HERE.parent / "_conf_schema.json").is_file()
    else _HERE.parent / "astrbot_plugin_autonomous_social"
)


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v200_engine_test")
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

from social import anchors, core_facade, memory_bridge, verify  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine, _SendFailed  # noqa: E402
from social.state import SocialState  # noqa: E402

NOW = 1_800_000_000.0
ENGINE_SRC = (PLUGIN_ROOT / "social" / "engine.py").read_text(encoding="utf-8")


class _Clock:
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


def _bare_engine(clock: _Clock | None = None, **cfg) -> SocialEngine:
    """绕开完整构造：只测门面/记忆桥/状态文案这几条小路径。"""
    clock = clock or _Clock()
    eng = object.__new__(SocialEngine)
    eng.cfg = SocialConfig()
    for key, value in cfg.items():
        setattr(eng.cfg, key, value)
    eng.context = object()
    eng._facade = None
    eng._facade_probe_at = 0.0
    eng._facade_alive_cache = None
    eng._time = clock
    eng.log = lambda *a, **k: None
    return eng


def _user(uid: str, **extra):
    u = SocialState._default_user()
    u.update({"umo": f"p:FriendMessage:{uid}", "__uid": uid})
    u.update(extra)
    return u


def _quiet_everywhere(eng: SocialEngine, bid: str = "b1") -> None:
    """把该角色的安静时段设成「所有小时都安静」，不依赖跑测试机器的时区。"""
    eng._night_windows[bid] = (0.0, 24.0)


class _FakeFacade:
    def __init__(self, tag: str = "new") -> None:
        self.tag = tag
        self.calls: list = []

    def api_version(self):
        return 2

    def capabilities(self):
        return ["brief", "clock", "fingerprint", "user"]

    def role_ids(self):
        return ["b1"]

    def clock(self, role_id):
        self.calls.append(("clock", role_id))
        return {"utc_offset_minutes": 300}

    def brief(self, role_id, user_id="", **kw):
        self.calls.append(("brief", role_id))
        return {"v": 2, "task": kw.get("task") or "chat", "ongoing": []}

    def state_fingerprint(self, role_id):
        return "fp"


class _FakeSendCtx:
    """只记「框架发送 API 被调了几次」的 context 替身。"""

    def __init__(self) -> None:
        self.calls = 0

    async def send_message(self, target, payload):
        self.calls += 1
        return True


# ═══════════════════════════════════════════════════════════════════════
# P1-1：发布 zip 曾缺 tag v1.28.17 的 30 行「特别关心也不打断进行中对话」闸门
# ═══════════════════════════════════════════════════════════════════════


class P11SpecialCareGateRegressionTest(unittest.TestCase):
    def test_the_tag_version_gate_is_present_twice(self):
        count = ENGINE_SRC.count('return "刚聊上，不插话"')
        self.assertEqual(count, 2, f"tag 版的两处「刚聊上，不插话」闸门不在了（{count} 处）")

    def test_the_tag_version_test_is_present(self):
        test_src = (PLUGIN_ROOT / "tests" / "test_v12817_special_care.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "def test_special_care_does_not_interrupt_an_active_chat", test_src
        )

    def test_special_care_does_not_interrupt_a_hot_chat(self):
        eng = _engine(special_care_uids=["10086"])
        u = _user("10086", last_seen=NOW - 60)  # 一分钟前还在说话
        why = eng.gate_reason("b1", "10086", u, NOW, {})
        self.assertIn("刚聊上", why)


# ═══════════════════════════════════════════════════════════════════════
# P2-01：special_care_ignore_quiet=False 时不再豁免睡眠/静音硬闸
# ═══════════════════════════════════════════════════════════════════════


class P201SpecialCareIgnoreQuietTest(unittest.TestCase):
    def _eng(self, ignore_quiet: bool):
        return _engine(
            special_care_uids=["sc"], special_care_ignore_quiet=ignore_quiet
        )

    def test_default_still_skips_asleep_and_quiet(self):
        eng = self._eng(True)
        _quiet_everywhere(eng)
        sc = _user("sc")
        self.assertEqual(eng.gate_reason("b1", "sc", sc, NOW, {"asleep": True}), "")
        self.assertEqual(eng.gate_reason("b1", "sc", sc, NOW, {}), "")
        self.assertEqual(eng.thread_gate("b1", "sc", sc, NOW, {"asleep": True}), "")

    def test_disabled_switch_blocks_at_night(self):
        eng = self._eng(False)
        _quiet_everywhere(eng)
        sc = _user("sc")
        self.assertIn(
            "安静时段", eng.gate_reason("b1", "sc", sc, NOW, {}),
            "关掉静音豁免后，特别关心在安静时段仍被放行",
        )
        self.assertIn("睡", eng.gate_reason("b1", "sc", sc, NOW, {"asleep": True}))
        self.assertIn("安静时段", eng.thread_gate("b1", "sc", sc, NOW, {}))
        self.assertIn("睡", eng.thread_gate("b1", "sc", sc, NOW, {"asleep": True}))

    def test_disabled_switch_blocks_other_channels_too(self):
        eng = self._eng(False)
        _quiet_everywhere(eng)
        sc = _user("sc")
        body = {"asleep": True}
        self.assertIn("睡", eng.greet_gate("b1", "sc", sc, NOW, body))
        self.assertIn("睡", eng.loop_gate("b1", "sc", sc, NOW, body))
        self.assertIn("睡", eng.closer_gate("b1", "sc", sc, NOW, body))

    def test_enabled_switch_still_relaxes_those_channels(self):
        eng = self._eng(True)
        _quiet_everywhere(eng)
        sc = _user("sc")
        body = {"asleep": True}
        self.assertEqual(eng.greet_gate("b1", "sc", sc, NOW, body), "")
        self.assertEqual(eng.loop_gate("b1", "sc", sc, NOW, body), "")
        self.assertEqual(eng.closer_gate("b1", "sc", sc, NOW, body), "")

    def test_energy_gate_consults_the_switch(self):
        self.assertFalse(SocialEngine._special_care_ignore_quiet_on(self._eng(False)))
        self.assertTrue(SocialEngine._special_care_ignore_quiet_on(self._eng(True)))
        src = inspect.getsource(SocialEngine._speak_inner)
        self.assertIn("_sc_waive_energy", src, "能量闸没有接 special_care_ignore_quiet")
        self.assertIn("_special_care_ignore_quiet_on", src)


# ═══════════════════════════════════════════════════════════════════════
# P2-02：special_care_min_messages 不再是死配置
# ═══════════════════════════════════════════════════════════════════════


class P202SpecialCareMinMessagesTest(unittest.TestCase):
    def _snap(self):
        # 老 Core：没有 mood.turn_count，只能退回社交层自己数的 message_count
        return {"affection": 96.0, "mood": {}}

    def test_message_count_uses_min_messages(self):
        eng = _engine()  # min_messages 默认 60
        u = _user("u1", message_count=70)
        self.assertTrue(
            eng._special_care_effective("b1", "u1", u, snap=self._snap(), now=NOW),
            "70 条没有被认定：兜底门槛还在用 min_turns=100",
        )

    def test_below_min_messages_is_rejected(self):
        eng = _engine()
        u = _user("u1", message_count=59)
        self.assertFalse(
            eng._special_care_effective("b1", "u1", u, snap=self._snap(), now=NOW)
        )

    def test_explicit_min_messages_is_honored(self):
        eng = _engine(special_care_min_messages=100)
        u70 = _user("u1", message_count=70)
        u101 = _user("u2", message_count=101)
        self.assertFalse(
            eng._special_care_effective("b1", "u1", u70, snap=self._snap(), now=NOW)
        )
        self.assertTrue(
            eng._special_care_effective("b1", "u2", u101, snap=self._snap(), now=NOW)
        )

    def test_real_turn_count_still_uses_min_turns(self):
        eng = _engine()  # min_turns 默认 100
        u = _user("u1", message_count=70)
        snap = {"affection": 96.0, "mood": {"turn_count": 120}}
        self.assertTrue(eng._special_care_effective("b1", "u1", u, snap=snap, now=NOW))


# ═══════════════════════════════════════════════════════════════════════
# P2-03：发送失败冷却要在所有发送入口统一检查，平台侧失败也要落冷却
# ═══════════════════════════════════════════════════════════════════════


class P203SendFailureCooldownTest(unittest.IsolatedAsyncioTestCase):
    def test_send_fail_left_reads_the_per_user_cooldown(self):
        eng = _engine()
        self.assertEqual(eng._send_fail_left({"send_fail_until": NOW + 600}), 600.0)
        self.assertEqual(eng._send_fail_left({}), 0.0)
        self.assertEqual(eng._send_fail_left({"send_fail_until": NOW - 10}), 0.0)
        self.assertEqual(eng._send_fail_left("坏数据"), 0.0)

    def test_platform_failure_sets_a_cooldown_too(self):
        eng = _engine()
        u = _user("u1")
        eng._mark_send_failed(u, platform_side=True, reason="未找到匹配的会话平台")
        self.assertGreater(u["send_fail_until"], NOW)
        self.assertLessEqual(
            u["send_fail_until"] - NOW, 12 * 3600.0 + 1, "平台侧冷却没有 12h 封顶"
        )
        self.assertIn("未找到匹配", u["send_blocked_reason"])

    def test_clear_send_failure_resets_all_four_fields(self):
        u = {
            "send_fail_count": 3,
            "send_fail_until": NOW + 100,
            "platform_fail_count": 2,
            "send_blocked_reason": "x",
        }
        SocialEngine._clear_send_failure(u)
        self.assertEqual(
            (u["send_fail_count"], u["send_fail_until"], u["platform_fail_count"]),
            (0, 0.0, 0),
        )
        self.assertEqual(u["send_blocked_reason"], "")

    def test_observe_releases_the_long_cooldown_when_user_writes(self):
        src = inspect.getsource(SocialEngine.observe)
        self.assertIn("_clear_send_failure", src, "用户重新发消息没有解除发送长冷却")
        self.assertIn("send_fail_until", src)

    def test_speak_inner_no_longer_skips_platform_side_marking(self):
        src = inspect.getsource(SocialEngine._speak_inner)
        bad = (
            "if not (permanent_dead or platform_side):"
            + chr(10)
            + "                self._mark_send_failed"
        )
        self.assertNotIn(bad, src, "平台侧失败又被跳过 `_mark_send_failed` 了")
        self.assertIn("if not permanent_dead:", src)
        self.assertIn("self._mark_send_failed(u, platform_side=platform_side", src)

    def _burst_stub(self, sent: list, cooldown_until: float):
        stub = SimpleNamespace()
        stub.running = True
        stub.cfg = SimpleNamespace(enabled=True, store_message_text=False)
        stub.send_breaker_left = lambda bid: 0.0
        stub._time = lambda: NOW

        async def _send(target, text, bid=""):
            sent.append(text)
            return True, "", False

        stub._send_with_retry = _send
        stub.state = SimpleNamespace(
            user=lambda bid, uid: {
                "last_seen": 0.0,
                "umo": "aiocqhttp:FriendMessage:1",
                "send_fail_until": cooldown_until,
            },
            record_outgoing=lambda *a, **k: None,
            save=lambda: None,
        )
        stub.log = lambda msg: None
        return stub

    async def test_burst_followup_stops_when_user_is_cooling_down(self):
        sent: list = []
        stub = self._burst_stub(sent, NOW + 3600)
        await SocialEngine._send_burst_followup(stub, "b", "u", ["第二段"], [0.0], 0.0)
        self.assertEqual(sent, [], "连发补发绕过了按人发送失败冷却")

    async def test_burst_followup_still_sends_when_not_cooling_down(self):
        sent: list = []
        stub = self._burst_stub(sent, 0.0)
        await SocialEngine._send_burst_followup(stub, "b", "u", ["第二段"], [0.0], 0.0)
        self.assertEqual(sent, ["第二段"])


# ═══════════════════════════════════════════════════════════════════════
# V8 / P2-03 纵深防御：冷却检查下沉到所有底层发送入口
# ═══════════════════════════════════════════════════════════════════════


class P203SendEntryGuardTest(unittest.IsolatedAsyncioTestCase):
    UMO = "p:FriendMessage:u1"

    def _engine_with_user(self, until: float):
        eng = _engine()
        eng.context = _FakeSendCtx()
        u = eng.state.user("b1", "u1")
        u["umo"] = self.UMO
        u["send_fail_until"] = until
        return eng, u

    async def test_send_with_retry_skips_when_cooling_down(self):
        eng, _u = self._engine_with_user(NOW + 1800)
        ok, err, peer = await eng._send_with_retry(self.UMO, "你好", "b1")
        self.assertFalse(ok)
        self.assertIn("不再试", err)
        self.assertFalse(peer)
        self.assertEqual(eng.context.calls, 0, "冷却中仍然调用了框架发送 API")

    async def test_send_message_raises_when_cooling_down(self):
        eng, _u = self._engine_with_user(NOW + 1800)
        with self.assertRaises(_SendFailed):
            await eng._send_message(self.UMO, "你好")
        self.assertEqual(eng.context.calls, 0, "冷却中仍然调用了框架发送 API")

    async def test_send_with_retry_sends_after_the_cooldown(self):
        eng, _u = self._engine_with_user(NOW - 1)
        ok, err, _peer = await eng._send_with_retry(self.UMO, "你好", "b1")
        self.assertTrue(ok, err)
        self.assertEqual(eng.context.calls, 1, "冷却结束后底层入口没放行")

    def test_group_umo_resolves_to_nobody(self):
        eng, _u = self._engine_with_user(NOW - 1)
        self.assertIsNone(
            SocialEngine._resolve_send_user(eng, "p:GroupMessage:g1", "b1"),
            "群 umo 不该被误认成某个人",
        )


# ═══════════════════════════════════════════════════════════════════════
# V12 / P2-12 单一入口：_core_status_text 不再直接读 state.json
# ═══════════════════════════════════════════════════════════════════════


class P212CoreStatusSingleEntryTest(unittest.TestCase):
    def test_source_uses_the_single_entry(self):
        src = inspect.getsource(SocialEngine._core_status_text)
        self.assertIn("_read_core_root", src)
        self.assertNotIn("self.core.read_root()", src)

    def test_auto_mode_still_reads_and_reports_degraded(self):
        eng = _bare_engine()
        eng.state = SimpleNamespace(data={"bots": {"b1": {}}})
        root = {"roles": {"b1": {"self": {}, "users": {}}}}
        eng.core = SimpleNamespace(
            read_root=lambda: root,
            bot_self_state=lambda bid, root=None: {"contract_v": None},
        )
        text = eng._core_status_text()
        self.assertIn("降级", text, "auto 模式读不到契约时的旧结论没有保留")

    def test_missing_file_still_says_not_found(self):
        eng = _bare_engine()
        eng.state = SimpleNamespace(data={"bots": {}})
        eng.core = SimpleNamespace(read_root=lambda: None)
        self.assertIn("未找到 Core state.json", eng._core_status_text())


# ═══════════════════════════════════════════════════════════════════════
# P3-09：墙钟回拨 —— 小回拨只升不降，大回拨重置高水位
# ═══════════════════════════════════════════════════════════════════════


class P309ClockRollbackTest(unittest.TestCase):
    def test_small_rollback_is_clamped(self):
        clock = _Clock(NOW)
        eng = _engine(clock)
        self.assertEqual(eng._time(), NOW)
        clock.advance(600)
        self.assertEqual(eng._time(), NOW + 600)
        clock.set(NOW + 400)  # 小回拨 200s
        self.assertEqual(eng._time(), NOW + 600, "小回拨把时间倒退了")

    def test_large_rollback_resets_the_timeline(self):
        clock = _Clock(NOW)
        eng = _engine(clock)
        self.assertEqual(eng._time(), NOW)
        clock.advance(600)
        self.assertEqual(eng._time(), NOW + 600)
        clock.set(NOW - 3600)  # 回拨 4200s，超过 300s 阈值
        self.assertEqual(eng._time(), NOW - 3600, "大回拨后时间轴还被冻在旧水位")
        clock.advance(10)
        self.assertEqual(eng._time(), NOW - 3590)


# ═══════════════════════════════════════════════════════════════════════
# P3-06：最后通牒检查接进 verify_message
# ═══════════════════════════════════════════════════════════════════════


class P306UltimatumTest(unittest.TestCase):
    def test_audit_sample_is_blocked(self):
        ok, why = verify.verify_message("到底想怎样，直说")
        self.assertFalse(ok)
        self.assertIn("把话说死", why)

    def test_hard_phrases_are_blocked(self):
        texts = ["给个说法", "少来这套", "把话说清楚", "别装了"]
        for text in texts:
            with self.subTest(text=text):
                ok, _why = verify.verify_message(text)
                self.assertFalse(ok, "最后通牒没被拦：" + text)

    def test_normal_plain_talk_is_not_misjudged(self):
        texts = [
            "有话就直说，别憋着。",
            "我就是想把这件事跟你说清楚，不绕弯子。",
            "今天风好大，出门记得加件衣服",
        ]
        for text in texts:
            with self.subTest(text=text):
                ok, why = verify.verify_message(text)
                self.assertTrue(ok, "正常口语被误杀：" + text + "（" + why + "）")


# ═══════════════════════════════════════════════════════════════════════
# P2-07：记忆桥 TTL 重试 + 状态页记忆行
# ═══════════════════════════════════════════════════════════════════════


class P207MemoryBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock(NOW)
        self.eng = _bare_engine(self.clock)

    def test_success_is_reused_and_revalidated_after_five_minutes(self):
        bridge = object()
        with mock.patch.object(
            memory_bridge, "find_memory_bridge", return_value=bridge
        ) as find:
            with mock.patch.object(memory_bridge, "bridge_alive", return_value=True):
                self.assertIs(self.eng._memory_bridge(), bridge)
                self.clock.advance(299)
                self.assertIs(self.eng._memory_bridge(), bridge)
                self.assertEqual(find.call_count, 1)
                self.clock.advance(2)  # 超过 300s：先验活；活着就不必重查
                self.assertIs(self.eng._memory_bridge(), bridge)
                self.assertEqual(find.call_count, 1)

    def test_failure_retries_after_a_minute(self):
        with mock.patch.object(
            memory_bridge, "find_memory_bridge", return_value=None
        ) as find:
            self.assertIsNone(self.eng._memory_bridge())
            self.assertEqual(find.call_count, 1)
            self.clock.advance(30)
            self.assertIsNone(self.eng._memory_bridge())
            self.assertEqual(find.call_count, 1, "失败缓存不足 60 秒就重查了")
            self.clock.advance(31)
            self.eng._memory_bridge()
            self.assertEqual(find.call_count, 2, "失败 60 秒后没有重试")

    def test_dead_bridge_object_is_replaced(self):
        old = object()
        new = object()
        self.eng._memory_bridge_obj = old
        self.eng._memory_bridge_at = NOW - 301  # 已过成功 TTL，必须验活
        with mock.patch.object(memory_bridge, "find_memory_bridge", return_value=new):
            with mock.patch.object(memory_bridge, "bridge_alive", return_value=False):
                with mock.patch.object(memory_bridge, "reset") as reset:
                    self.assertIs(self.eng._memory_bridge(), new)
                    reset.assert_called_once()

    def test_status_text_prefers_the_bridge_module_line(self):
        with mock.patch.object(
            memory_bridge,
            "status_text",
            return_value="✅ 记忆：已连接（memory_companion）",
            create=True,
        ):
            self.assertEqual(
                self.eng._memory_status_text(), "✅ 记忆：已连接（memory_companion）"
            )

    def test_switch_off_reports_clearly(self):
        eng = _bare_engine(self.clock, memory_bridge_enabled=False)
        self.assertIn("已关闭", eng._memory_status_text())

    def test_fallback_reports_last_error(self):
        with mock.patch.object(memory_bridge, "status_text", None, create=True):
            with mock.patch.object(memory_bridge, "status", None, create=True):
                with mock.patch.object(
                    memory_bridge, "find_memory_bridge", return_value=None
                ):
                    with mock.patch.object(
                        memory_bridge, "last_error", return_value="missing: 注册表里没有"
                    ):
                        text = self.eng._memory_status_text()
        self.assertIn("未连接", text)
        self.assertIn("注册表里没有", text)


class P207StatusPageMemoryRowTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_page_has_a_memory_line(self):
        eng = _engine()
        text = await eng.status_text()
        self.assertIn("记忆", text, "状态页【联动】没有记忆桥一行")


# ═══════════════════════════════════════════════════════════════════════
# P2-08 / P2-09：门面不再一次性解析；旧门面失效后自动重连
# ═══════════════════════════════════════════════════════════════════════


class P208FacadeRetryTest(unittest.TestCase):
    def test_core_installed_later_is_picked_up_without_reload(self):
        eng = _bare_engine()
        fake = _FakeFacade()
        calls = {"n": 0}

        def find(context=None, force=False):
            calls["n"] += 1
            return fake if calls["n"] >= 2 else None

        with mock.patch.object(core_facade, "find_core_facade", find):
            self.assertIsNone(eng._core_facade())
            self.assertIs(eng._core_facade(), fake)
        self.assertEqual(calls["n"], 2, "门面还是一次性解析（_facade_tried 回来了？）")

    def test_no_permanent_tried_gate_in_source(self):
        src = inspect.getsource(SocialEngine._core_facade)
        self.assertNotIn("self._facade_tried", src)


class P209FacadeLivenessTest(unittest.TestCase):
    def test_dead_cached_facade_is_reset_and_replaced(self):
        eng = _bare_engine()
        old = _FakeFacade("old")
        new = _FakeFacade("new")
        eng._facade = old
        with mock.patch.object(core_facade, "probe", return_value=False):
            with mock.patch.object(core_facade, "reset") as reset:
                with mock.patch.object(
                    core_facade, "find_core_facade", return_value=new
                ):
                    self.assertIs(eng._core_facade(), new)
                    reset.assert_called_once()

    def test_alive_cached_facade_is_reused_without_lookup(self):
        eng = _bare_engine()
        old = _FakeFacade("old")
        eng._facade = old
        with mock.patch.object(core_facade, "probe", return_value=True):
            with mock.patch.object(core_facade, "find_core_facade") as find:
                self.assertIs(eng._core_facade(), old)
                find.assert_not_called()

    def test_status_line_mentions_reconnect_when_probe_fails(self):
        eng = _bare_engine()
        eng._facade = _FakeFacade("old")
        fake_facade = eng._facade
        status = {
            "connected": True,
            "api_version": 2,
            "capabilities": ["brief", "user"],
            "alive": False,
            "reason": "",
        }
        with mock.patch.object(core_facade, "probe", return_value=False):
            with mock.patch.object(core_facade, "reset"):
                with mock.patch.object(
                    core_facade, "find_core_facade", return_value=fake_facade
                ):
                    with mock.patch.object(core_facade, "status", return_value=status):
                        text = eng._facade_status_text()
        self.assertIn("已连接", text)
        self.assertIn("重连", text)

    def test_probe_is_throttled(self):
        clock = _Clock(NOW)
        eng = _bare_engine(clock)
        eng._facade = _FakeFacade("old")
        with mock.patch.object(core_facade, "probe", return_value=True) as probe:
            eng._facade_alive(eng._facade)
            eng._facade_alive(eng._facade)
            self.assertEqual(probe.call_count, 1, "存活探测没有 60 秒节流")
            clock.advance(61)
            eng._facade_alive(eng._facade)
            self.assertEqual(probe.call_count, 2)


# ═══════════════════════════════════════════════════════════════════════
# P2-12：use_core_facade=on 时主动消息不读 state.json
# ═══════════════════════════════════════════════════════════════════════


class P212FacadeOnModeTest(unittest.TestCase):
    def test_read_core_root_is_none_in_on_mode(self):
        root = {"roles": {"b1": {"self": {}, "users": {}}}}
        for mode, expected in (("on", None), ("auto", root), ("off", root)):
            eng = _bare_engine(use_core_facade=mode)
            eng.core = SimpleNamespace(read_root=lambda: root)
            self.assertEqual(eng._read_core_root(), expected, "mode=" + mode)

    def test_main_paths_use_the_single_entry(self):
        methods = (
            SocialEngine._speak_inner,
            SocialEngine._preview_run,
            SocialEngine.try_once,
            SocialEngine.trigger_once,
            SocialEngine.dry_run,
        )
        for method in methods:
            src = inspect.getsource(method)
            self.assertIn(
                "_read_core_root", src, method.__name__ + " 仍在直接读 state.json"
            )

    def test_core_status_text_says_on_mode_does_not_read_file(self):
        eng = _bare_engine(use_core_facade="on")
        eng._facade = _FakeFacade()
        eng.state = SimpleNamespace(data={"bots": {}})
        text = eng._core_status_text()
        self.assertIn("只走门面", text)
        self.assertNotIn("未找到 Core state.json", text)

    def test_facade_status_text_on_mode_is_accurate(self):
        eng = _bare_engine(use_core_facade="on")
        with mock.patch.object(core_facade, "find_core_facade", return_value=None):
            with mock.patch.object(
                core_facade,
                "status",
                return_value={"connected": False, "reason": "没装"},
            ):
                text = eng._facade_status_text()
        self.assertIn("不读 state.json", text)
        self.assertIn("不会静默退回", text)

    def test_facade_body_uses_brief_in_on_mode(self):
        eng = _bare_engine(use_core_facade="on")
        eng._facade = _FakeFacade()
        brief = {
            "v": 2,
            "self_state": {"energy": 42.0, "asleep": True, "energy_text": "有点累"},
            "wake": {"asleep": True, "inertia_minutes_left": 5},
        }
        with mock.patch.object(core_facade, "brief", return_value=brief):
            body = eng._facade_body("b1")
        self.assertEqual(body["energy"], 42.0)
        self.assertTrue(body["asleep"])


# ═══════════════════════════════════════════════════════════════════════
# P2-13：门面 brief 的 her/outfit/persona_framing 等真正进背景状态
# ═══════════════════════════════════════════════════════════════════════


BRIEF = {
    "v": 2,
    "task": "proactive",
    "her": {"char_name": "小娜", "persona_label": "温柔", "species": "猫娘"},
    "outfit": {"enabled": True, "text": "白色毛衣", "source": "manual"},
    "weather": {"env": "外面在下小雨"},
    "holiday": "中秋节",
    "routine": {"night_start_hour": 2.0, "night_end_hour": 10.0},
    "day": {"doing": "整理个案笔记", "done": ["吃了早饭"], "next": ["去书店"]},
    "persona_framing": "下面提到的「小娜」指你自己，「TA」指正在和你说话的那个人。",
}


class P213BriefContextTest(unittest.TestCase):
    def test_required_fields_reach_the_background_text(self):
        text = SocialEngine._brief_context_lines(BRIEF)
        self.assertIn("小娜", text)
        self.assertIn("白色毛衣", text)
        self.assertIn("指你自己", text)
        self.assertIn("中秋节", text)
        self.assertIn("2 点到 10 点", text)

    def test_compact_state_is_not_duplicated(self):
        text = SocialEngine._brief_context_lines(BRIEF)
        self.assertNotIn("整理个案笔记", text, "有文件快照时 day 应由 compact() 给，不重复")
        self.assertNotIn("外面在下小雨", text, "天气只能当由头，不摆进背景状态")

    def test_no_snapshot_falls_back_to_brief_day_and_weather(self):
        text = SocialEngine._brief_context_lines(BRIEF, include_day=True)
        self.assertIn("整理个案笔记", text)
        self.assertIn("外面在下小雨", text)

    def test_merge_keeps_file_context_and_appends_brief_lines(self):
        merged = SocialEngine._core_context_with_brief(
            "状态: 精神不错", BRIEF, include_day=False
        )
        self.assertTrue(merged.startswith("状态: 精神不错"))
        self.assertIn("小娜", merged)

    def test_empty_brief_keeps_the_old_placeholder(self):
        self.assertEqual(
            SocialEngine._core_context_with_brief("没有可用的 Humanoid Core 状态。", None),
            "没有可用的 Humanoid Core 状态。",
        )
        self.assertEqual(
            SocialEngine._core_context_with_brief("没有可用的 Humanoid Core 状态。", {}),
            "没有可用的 Humanoid Core 状态。",
        )

    def test_both_send_paths_consume_the_brief(self):
        for method in (SocialEngine._speak_inner, SocialEngine._preview_one):
            src = inspect.getsource(method)
            self.assertIn("_core_context_with_brief", src)
            self.assertIn("_apply_core_brief", src)


# ═══════════════════════════════════════════════════════════════════════
# P2-10 显示侧：契约过期/握手失败要能在状态页上说明白
# ═══════════════════════════════════════════════════════════════════════


class P210ContractNoteTest(unittest.TestCase):
    def test_stale_contract_is_reported_with_age(self):
        note = SocialEngine._contract_view_note(
            {"contract_stale": True, "contract_age_seconds": 900}
        )
        self.assertIn("过期", note)
        self.assertIn("15 分钟", note)

    def test_handshake_failure_is_reported(self):
        note = SocialEngine._contract_view_note(
            {"contract_stale": False, "contract_handshake": {"ok": False, "problems": ["a", "b"]}}
        )
        self.assertIn("握手校验未通过", note)
        self.assertIn("2 项", note)

    def test_fresh_contract_has_no_note(self):
        self.assertEqual(
            SocialEngine._contract_view_note({"contract_stale": False}), ""
        )
        self.assertEqual(SocialEngine._contract_view_note(None), "")


# ═══════════════════════════════════════════════════════════════════════
# P3-10：provider 预检（N/M 可用）接进状态页
# ═══════════════════════════════════════════════════════════════════════


class P310ProviderStatusTest(unittest.TestCase):
    def _eng(self, generator):
        eng = _bare_engine()
        eng.generator = generator
        return eng

    def test_health_detail_is_used(self):
        health = {
            "configured": True,
            "resolvable": 2,
            "total": 3,
            "ids": ["a", "b", "c"],
            "strict": False,
            "detail": "模型：主动消息专用 → a → b（2/3 可用）",
        }
        eng = self._eng(SimpleNamespace(proactive_provider_health=lambda: health))
        self.assertIn("2/3 可用", eng._provider_status_text())

    def test_zero_resolvable_is_reported(self):
        health = {
            "configured": True,
            "resolvable": 0,
            "total": 3,
            "ids": ["a", "b", "c"],
            "strict": True,
            "detail": "⚠️ 模型：专供模型 0/3 可解析，严格模式：本轮不主动开口",
        }
        eng = self._eng(SimpleNamespace(proactive_provider_health=lambda: health))
        text = eng._provider_status_text()
        self.assertIn("0/3", text)
        self.assertIn("严格模式", text)

    def test_missing_method_falls_back_to_old_echo(self):
        eng = self._eng(SimpleNamespace())
        eng.cfg.proactive_provider_ids = ["a", "b"]
        text = eng._provider_status_text()
        self.assertIn("主动消息专用", text)
        self.assertIn("a → b", text)

    def test_raising_health_falls_back(self):
        def boom():
            raise RuntimeError("boom")

        eng = self._eng(SimpleNamespace(proactive_provider_health=boom))
        eng.cfg.proactive_provider_ids = []
        self.assertIn("未指定专用模型", eng._provider_status_text())

    def test_default_config_means_follow_session_provider(self):
        eng = _engine()
        self.assertIn("未指定专用模型", eng._provider_status_text())


if __name__ == "__main__":
    unittest.main()
