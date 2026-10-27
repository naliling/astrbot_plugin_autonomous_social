"""v1.28.17：特别关心（配置项 + 从 Core 关系自动认定）。

用户提的需求是「配置项给一个特别关心」，而不是给某个人开后门：

* 名单里的人不该再被「关系档位 24 小时 / 她在睡 / 安静时段 / 对方没开口就永远
  不发」这些闸挡死——两个插件本该搭配，结果 Core 说关系很好、社交层却按生人
  档排节奏，就成了互相拆台。
* 也可以**自动**：Core 的关系数据够近（好感 + 对话轮数）就认定，认定结果记在
  用户状态里（`special_care`），下一轮起所有闸门读同一份标记，不是每轮重算。
* 放宽不等于无限：仍有单独的最小间隔 + 日上限。
"""

from __future__ import annotations

import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

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
    api.logger = logging.getLogger("autonomous_social_v12817_test")
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

from social import anchors  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social.state import SocialState  # noqa: E402

NOW = 1_800_000_000.0


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def set(self, now: float) -> None:
        self.now = float(now)


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


def _user(uid: str, **extra):
    u = SocialState._default_user()
    u.update({"umo": f"p:FriendMessage:{uid}", "__uid": uid})
    u.update(extra)
    return u


class SpecialCareConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = SocialConfig()
        self.assertTrue(cfg.special_care_enabled)
        self.assertTrue(cfg.special_care_auto)
        self.assertEqual(cfg.special_care_gap_minutes, 20)
        self.assertEqual(cfg.special_care_daily_cap, 8)
        self.assertTrue(cfg.special_care_ignore_quiet)

    def test_entries_from_list(self):
        cfg = SocialConfig(special_care_uids=["u1", " u2 ", None, "p:u3"])
        self.assertEqual(cfg.special_care_entries(), {"u1", "u2", "p:u3"})

    def test_entries_from_string(self):
        cfg = SocialConfig(special_care_uids="u1，u2; u3")
        self.assertEqual(cfg.special_care_entries(), {"u1", "u2", "u3"})

    def test_entries_empty(self):
        self.assertEqual(SocialConfig(special_care_uids=None).special_care_entries(), set())
        self.assertEqual(SocialConfig().special_care_entries(), set())

    def test_state_default_user_has_fields(self):
        u = SocialState._default_user()
        for key in (
            "special_care",
            "special_care_source",
            "special_care_at",
            "special_care_sent_day",
            "special_care_sent_count",
        ):
            self.assertIn(key, u)
        self.assertFalse(u["special_care"])


class SpecialCareManualTest(unittest.TestCase):
    def test_uid_match(self):
        eng = _engine(special_care_uids=["10086"])
        self.assertTrue(eng._special_care_manual("10086", None))
        self.assertFalse(eng._special_care_manual("10087", None))

    def test_platform_uid_match_requires_same_platform(self):
        eng = _engine(special_care_uids=["aiocqhttp:10086"])
        self.assertTrue(
            eng._special_care_manual("10086", {"umo": "aiocqhttp:FriendMessage:10086"})
        )
        self.assertFalse(
            eng._special_care_manual("10086", {"umo": "webchat:FriendMessage:10086"})
        )

    def test_disabled_switch_means_nobody(self):
        eng = _engine(special_care_uids=["10086"], special_care_enabled=False)
        self.assertFalse(eng._special_care_manual("10086", None))
        self.assertFalse(eng._special_care_now("10086", None))

    def test_manual_is_recorded_in_state(self):
        eng = _engine(special_care_uids=["10086"])
        u = _user("10086")
        self.assertFalse(u.get("special_care"))
        self.assertTrue(eng._special_care_effective("b1", "10086", u))
        self.assertTrue(u["special_care"])
        self.assertEqual(u["special_care_source"], "manual")
        self.assertGreater(u["special_care_at"], 0)


class SpecialCareAutoTest(unittest.TestCase):
    def test_auto_marks_close_relationship(self):
        eng = _engine()
        u = _user("10086")
        snap = {"affection": 96.0, "mood": {"turn_count": 300}}
        self.assertTrue(eng._special_care_effective("b1", "10086", u, snap=snap))
        self.assertTrue(u["special_care"])
        self.assertEqual(u["special_care_source"], "auto")

    def test_auto_requires_affection(self):
        eng = _engine()
        u = _user("10086")
        snap = {"affection": 50.0, "mood": {"turn_count": 300}}
        self.assertFalse(eng._special_care_effective("b1", "10086", u, snap=snap))
        self.assertFalse(u["special_care"])

    def test_auto_requires_enough_turns(self):
        eng = _engine()
        u = _user("10086", message_count=5)
        snap = {"affection": 96.0, "mood": {"turn_count": 5}}
        self.assertFalse(eng._special_care_effective("b1", "10086", u, snap=snap))

    def test_auto_falls_back_to_message_count(self):
        eng = _engine()
        u = _user("10086", message_count=120)
        snap = {"affection": 96.0, "mood": {}}
        self.assertTrue(eng._special_care_effective("b1", "10086", u, snap=snap))

    def test_auto_off(self):
        eng = _engine(special_care_auto=False)
        u = _user("10086")
        snap = {"affection": 96.0, "mood": {"turn_count": 300}}
        self.assertFalse(eng._special_care_effective("b1", "10086", u, snap=snap))

    def test_missing_snapshot_does_not_auto_mark(self):
        eng = _engine()
        u = _user("10086", message_count=999)
        self.assertFalse(eng._special_care_effective("b1", "10086", u, snap=None))
        self.assertFalse(u["special_care"])

    def test_flag_is_sticky_without_resnapshot(self):
        eng = _engine()
        u = _user("10086")
        snap = {"affection": 96.0, "mood": {"turn_count": 300}}
        self.assertTrue(eng._special_care_effective("b1", "10086", u, snap=snap))
        self.assertTrue(eng._special_care_effective("b1", "10086", u, snap=None))


class SpecialCarePacingTest(unittest.TestCase):
    def test_tier_is_high(self):
        eng = _engine()
        u = _user("10086", interest=0.1)
        self.assertEqual(eng._tier_of("b1", "10086", u, None, NOW)[0], anchors.TIER_LOW)
        u["special_care"] = True
        self.assertEqual(eng._tier_of("b1", "10086", u, None, NOW)[0], anchors.TIER_HIGH)

    def test_gap_uses_special_care_minutes(self):
        eng = _engine(special_care_gap_minutes=20)
        u = _user("10086", interest=0.1, last_sent=NOW - 600)
        normal = eng._gap_left(u, anchors.TIER_LOW, NOW)
        special = eng._gap_left(u, anchors.TIER_HIGH, NOW, special_care=True)
        self.assertAlmostEqual(special, 600.0, places=1)
        self.assertGreater(normal, 40000.0)

    def test_gap_zero_when_never_sent(self):
        eng = _engine()
        u = _user("10086")
        self.assertEqual(eng._gap_left(u, anchors.TIER_HIGH, NOW, special_care=True), 0.0)

    def test_gap_ignores_streak(self):
        """特别关心不吃「被冷落越久越拉远」：用户要的是「没动静去问一下」。"""
        eng = _engine(special_care_gap_minutes=20)
        u = _user("10086", last_sent=NOW - 30 * 60, no_reply_streak=3)
        self.assertEqual(eng._gap_left(u, anchors.TIER_LOW, NOW, special_care=True), 0.0)


class SpecialCareGateTest(unittest.TestCase):
    """闸门豁免：睡着了/安静时段/没被接住这些「普通关系」的护栏，对特别关心让路。"""

    def _eng(self):
        eng = _engine(special_care_uids=["10086"])
        return eng

    def test_gate_reason_skips_asleep(self):
        eng = self._eng()
        body = {"asleep": True}
        blocked = _user("n1")
        self.assertIn("睡", eng.gate_reason("b1", "n1", blocked, NOW, body))
        special = _user("10086")
        self.assertEqual(eng.gate_reason("b1", "10086", special, NOW, body), "")

    def test_greet_gate_skips_asleep(self):
        eng = self._eng()
        body = {"asleep": True}
        self.assertTrue(eng.greet_gate("b1", "n1", _user("n1"), NOW, body))
        self.assertEqual(eng.greet_gate("b1", "10086", _user("10086"), NOW, body), "")

    def test_loop_gate_skips_asleep(self):
        eng = self._eng()
        body = {"asleep": True}
        self.assertTrue(eng.loop_gate("b1", "n1", _user("n1"), NOW, body))
        self.assertEqual(eng.loop_gate("b1", "10086", _user("10086"), NOW, body), "")

    def test_thread_gate_skips_asleep(self):
        eng = self._eng()
        body = {"asleep": True}
        self.assertTrue(eng.thread_gate("b1", "n1", _user("n1"), NOW, body))
        self.assertEqual(eng.thread_gate("b1", "10086", _user("10086"), NOW, body), "")

    def test_thread_gate_still_prevents_double_ask(self):
        """豁免不等于重复追问：同一段沉默仍只接一次。"""
        eng = self._eng()
        u = _user("10086", last_seen=NOW - 5000, last_spoken=NOW - 5000)
        u["thread_for"] = u["last_spoken"]
        self.assertIn("接过一回", eng.thread_gate("b1", "10086", u, NOW, {}))

    def test_special_care_does_not_interrupt_an_active_chat(self):
        """放宽的是「对方没动静」，不是「人还在聊也插一句」——接不上话/瞎发就是这条。"""
        eng = self._eng()
        u = _user("10086", last_seen=NOW - 60)
        self.assertIn("刚聊上", eng.gate_reason("b1", "10086", u, NOW, {}))


class SpecialCareSettleTest(unittest.TestCase):
    def test_quiet_hours_do_not_freeze_special_care_urge(self):
        eng = _engine(_Clock(NOW), special_care_uids=["sc1"], quiet_start=23, quiet_end=7)
        sc = _user("sc1", urge=0.0, urge_at=NOW - 4 * 3600, interest=0.5,
                   last_seen=NOW - 100000)
        plain = _user("n1", urge=0.0, urge_at=NOW - 4 * 3600, interest=0.5,
                      last_seen=NOW - 100000)
        eng.state.bot("b1")["users"] = {"sc1": sc, "n1": plain}
        dt = datetime(2026, 5, 1, 23, 30)  # 落在 quiet 窗口里
        eng.settle_minds(
            "b1", NOW, dt, core_root=None,
            bot_state={"energy": 80.0, "social_energy": 80.0},
        )
        self.assertGreater(
            float(sc["urge"]), float(plain["urge"]) * 2,
            f"特别关心的念头该照常长：sc={sc['urge']} plain={plain['urge']}",
        )

    def test_quiet_hours_freeze_normal_users(self):
        eng = _engine(_Clock(NOW), quiet_start=23, quiet_end=7)
        plain = _user("n1", urge=0.0, urge_at=NOW - 4 * 3600, interest=0.5,
                      last_seen=NOW - 100000)
        eng.state.bot("b1")["users"] = {"n1": plain}
        dt = datetime(2026, 5, 1, 23, 30)
        eng.settle_minds(
            "b1", NOW, dt, core_root=None,
            bot_state={"energy": 80.0, "social_energy": 80.0},
        )
        self.assertLess(float(plain["urge"]), 0.6)


if __name__ == "__main__":
    unittest.main()
