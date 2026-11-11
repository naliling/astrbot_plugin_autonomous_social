# -*- coding: utf-8 -*-
"""v2.2.20：跨渠道（群↔私聊）同一件事不二传。

改动面：
  · `social/state.py` 新增 `recent_own_group_texts` / `recent_proactive_all`
  · `social/config.py` 新增 `cross_channel_dedup_hours`（默认 6 小时，0 = 关）
  · `social/engine.py` 私聊主动消息与群破冰各接一道跨渠道闸

这组用例只跑离线逻辑：取数、窗口、以及「闸到底接进去了没有」。
"""
import io
import json
import os
import sys
import types
import unittest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 极简 astrbot 桩（与其它用例文件同款：只是为了能 import 插件模块）──
if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _log = types.ModuleType("astrbot.api.logger")
    _log.warning = lambda *a, **k: None
    _log.info = lambda *a, **k: None
    _log.error = lambda *a, **k: None
    _log.debug = lambda *a, **k: None
    _api.logger = _log
    _cmps = types.ModuleType("astrbot.api.message_components")
    for _n in ("Plain", "At", "Image", "Reply"):
        setattr(_cmps, _n, type(_n, (), {}))
    _api.message_components = _cmps
    _astrbot.api = _api
    for _n in ("astrbot.api.event", "astrbot.core", "astrbot.core.utils"):
        sys.modules.setdefault(_n, types.ModuleType(_n))
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api
    sys.modules["astrbot.api.logger"] = _log
    sys.modules["astrbot.api.message_components"] = _cmps

if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from social.state import SocialState  # noqa: E402
from social import config as cfg_mod  # noqa: E402

NOW = 1800000000.0
HOUR = 3600.0


def _state(tmpdir, data=None):
    st = SocialState(os.path.join(tmpdir, "state.json"))
    if data is not None:
        st.data = data
    return st


def _bot(groups=None, users=None):
    return {"groups": groups or {}, "users": users or {}}


def _grp(*samples):
    return {"samples": list(samples)}


def _self(text, ts):
    return {"ts": ts, "text": text, "self": True, "name": "她"}


def _other(text, ts):
    return {"ts": ts, "text": text, "self": False, "name": "群友"}


def _user(*log):
    return {"proactive_log": list(log)}


def _pro(text, ts, about=""):
    return {"ts": ts, "text": text, "about": about, "cat": "chat", "why": ""}


class RecentOwnGroupTextsTest(unittest.TestCase):
    """她最近在**所有群**说过的话——私聊侧要靠它看得见群发言。"""

    def test_collects_across_groups_newest_first(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            data = {"bots": {"b1": _bot(groups={
                "g1": _grp(_self("群一的话", NOW - 100.0), _other("别人的话", NOW - 90.0)),
                "g2": _grp(_self("群二的话", NOW - 50.0)),
            })}}
            st = _state(d, data)
            rows = st.recent_own_group_texts("b1", NOW, hours=6.0)
            self.assertEqual([r[1] for r in rows], ["群二的话", "群一的话"])
            self.assertEqual([r[0] for r in rows], ["g2", "g1"])

    def test_ignores_others_and_old(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            data = {"bots": {"b1": _bot(groups={
                "g1": _grp(
                    _other("群友的话", NOW - 60.0),
                    _self("七小时前的话", NOW - 7 * HOUR),
                    _self("刚刚的话", NOW - 60.0),
                ),
            })}}
            st = _state(d, data)
            rows = st.recent_own_group_texts("b1", NOW, hours=6.0)
            self.assertEqual([r[1] for r in rows], ["刚刚的话"])

    def test_limit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            data = {"bots": {"b1": _bot(groups={
                "g1": _grp(*[_self("第%d句" % i, NOW - (20 - i) * 60.0) for i in range(20)]),
            })}}
            st = _state(d, data)
            rows = st.recent_own_group_texts("b1", NOW, hours=6.0, limit=3)
            self.assertEqual([r[1] for r in rows], ["第19句", "第18句", "第17句"])

    def test_empty_state(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            st = _state(d, {"bots": {}})
            self.assertEqual(st.recent_own_group_texts("b1", NOW), [])


class RecentProactiveAllTest(unittest.TestCase):
    """她最近发给**任何人**的主动消息——群侧开口前要靠它看得见私聊。"""

    def test_collects_across_users_newest_first(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            data = {"bots": {"b1": _bot(users={
                "u1": _user(_pro("给甲的话", NOW - 500.0)),
                "u2": _user(_pro("给乙的话", NOW - 100.0)),
            })}}
            st = _state(d, data)
            rows = st.recent_proactive_all("b1", NOW, hours=6.0)
            self.assertEqual([r[1] for r in rows], ["给乙的话", "给甲的话"])
            self.assertEqual([r[0] for r in rows], ["u2", "u1"])

    def test_window_and_limit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            data = {"bots": {"b1": _bot(users={
                "u1": _user(
                    _pro("九小时前", NOW - 9 * HOUR),
                    _pro("三小时前", NOW - 3 * HOUR),
                    _pro("一小时前", NOW - 1 * HOUR),
                ),
            })}}
            st = _state(d, data)
            rows = st.recent_proactive_all("b1", NOW, hours=6.0, limit=1)
            self.assertEqual([r[1] for r in rows], ["一小时前"])

    def test_empty_state(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            st = _state(d, {"bots": {}})
            self.assertEqual(st.recent_proactive_all("b1", NOW), [])


class ConfigWiringTest(unittest.TestCase):
    """窗口有默认值、能夹到 0~72、schema 与 README 计数跟着走。"""

    def test_default_is_six_hours(self):
        self.assertEqual(cfg_mod.CROSS_CHANNEL_DEDUP_HOURS_DEFAULT, 6.0)
        self.assertEqual(
            cfg_mod.SocialConfig().cross_channel_dedup_hours, 6.0
        )

    def test_schema_key_has_slider_and_default(self):
        with io.open(os.path.join(PLUGIN_ROOT, "_conf_schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
        item = schema["cross_channel_dedup_hours"]
        self.assertEqual(item["type"], "float")
        self.assertEqual(item["default"], 6.0)
        self.assertEqual(item["min"], 0.0)
        self.assertEqual(item["max"], 72.0)
        self.assertEqual(item["slider"], {"min": 0.0, "max": 72.0, "step": 1.0})

    def test_readme_count_matches_schema(self):
        with io.open(os.path.join(PLUGIN_ROOT, "_conf_schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
        with io.open(os.path.join(PLUGIN_ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("共 %d 项" % len(schema), readme)
        self.assertIn("`cross_channel_dedup_hours`", readme)


class EngineGateTest(unittest.TestCase):
    """两道闸真的接在发送前：私聊主动消息、群破冰各一处。"""

    def test_private_gate_uses_group_texts(self):
        with io.open(
            os.path.join(PLUGIN_ROOT, "social", "engine.py"), encoding="utf-8"
        ) as f:
            src = f.read()
        self.assertIn("recent_own_group_texts", src)
        self.assertIn("这件事刚在群里讲过，私聊再说就成了二传", src)
        # 窗口必须来自配置，不能是硬编码的 6 小时
        self.assertIn('getattr(self.cfg, "cross_channel_dedup_hours"', src)

    def test_group_gate_uses_proactive_texts(self):
        with io.open(
            os.path.join(PLUGIN_ROOT, "social", "engine.py"), encoding="utf-8"
        ) as f:
            src = f.read()
        self.assertIn("recent_proactive_all", src)
        self.assertIn("这件事刚跟人私聊讲过，未发送", src)


class CrossChannelHitTest(unittest.TestCase):
    """闸的判据：同一件事换个说法也要拦住，不同的两件事不许误拦。"""

    def test_verbatim_duplicate_blocked(self):
        from social import verify as V

        olds = [("g1", "刚用蹄子夹着碗勺洗完")]
        self.assertIsNotNone(
            V._check_cross_user("刚用蹄子夹着碗勺洗完", olds, threshold=0.6)
        )

    def test_reworded_same_event_blocked(self):
        from social import verify as V

        olds = [("u1", "娜莉灵下午来了一趟")]
        self.assertIsNotNone(
            V._check_cross_user("刚用蹄子夹着碗勺洗完", [("g1", "碗勺用蹄子夹着洗完了")],
                                threshold=0.6)
        )

    def test_different_events_pass(self):
        from social import verify as V

        olds = [("g1", "刚用蹄子夹着碗勺洗完")]
        self.assertIsNone(
            V._check_cross_user("外头晴得干净才想起来茶还在灶上温着", olds, threshold=0.6)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
