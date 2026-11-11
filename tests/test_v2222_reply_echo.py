# -*- coding: utf-8 -*-
"""v2.2.22：普通回复也要知道自己刚说过什么。

## 为什么

用户 1019375937 那两句火大的回复（「用户说累 → 她讲自己用蹄子夹锅柄热娜莉灵
带来的东西 → 用户拒绝 → 她又邀请并评价泡面没营养」）走的是**普通对话回复**
（`src=chat`），和主动消息是两条链路。普通回复这条链路此前**没有任何重复检查**：

* `scripts/audit_reply_repeat.py` 扫线上 1327 条她自己的私聊正文，
  24 小时内跟同一个人重说同一件事的有 **51 条（3.8%）**：

  - `折腾半天，总算把晚饭备好了` → 1 秒后 `总算把晚饭备好了`；
  - `傍晚用蹄子夹着锅柄把妹妹带的东西热了一锅`
    → 4.4 分钟后 `把妹妹带来的东西热了一锅`；
  - 1–10 分钟那 23 条里就有投诉的那两句原文。

普通回复不能硬拦（拦了就是不回人话，比复读更糟），所以这一版只做两件确定的事：

1. **同一次发言内部**去重：一次拆成几段时，说了同一件事的第二段不发；
2. **开口之前**把「你刚跟这个人说过这些」作为临时内容块注入提示词
   （不落历史），让她自己避开——和群聊「这几个开头这次别用了」同一套做法。

## 本文件盯的

* `recent_own_out` 的窗口 / 条数 / 只取出方向；
* `reply_echo_hint` 的开关、空数据、超长截断、替身安全；
* `_dedupe_same_event_segments` 的合并与 waits 重建；
* 配置项与 schema 键真的落地了，且两个发送链路的接线没丢。
"""
import json
import os
import sys
import tempfile
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

from social import config as cfg_mod  # noqa: E402
import social.engine as engine_mod  # noqa: E402
from social.state import SocialState  # noqa: E402

NOW = 1800000000.0
HOUR = 3600.0
BID = "bot1"
UID = "u1"


def _state(data=None):
    tmp = tempfile.mkdtemp(prefix="aso_v2222_")
    st = SocialState(os.path.join(tmp, "state.json"))
    if data is not None:
        st.data = data
    return st


def _conv(*rows):
    """rows: (text, ts, dir)"""
    return [
        {"text": t, "ts": ts, "dir": d, "src": "chat"} for (t, ts, d) in rows
    ]


def _bot(conv):
    return {"groups": {}, "users": {UID: {"conversation": conv}}}


def _engine(state, **cfg):
    eng = object.__new__(engine_mod.SocialEngine)
    eng.cfg = types.SimpleNamespace(**cfg)
    eng.state = state
    eng._time = lambda: NOW
    return eng


# ─── 一、取数：只取她说出去的、只取窗口内的 ─────────────────────


class RecentOwnOutTest(unittest.TestCase):
    def test_only_out_direction(self):
        st = _state(
            {"bots": {BID: _bot(_conv(
                ("对方说的话", NOW - 60.0, "in"),
                ("我说的话", NOW - 30.0, "out"),
            ))}}
        )
        self.assertEqual(st.recent_own_out(BID, UID, NOW), ["我说的话"])

    def test_window_filters_old(self):
        st = _state(
            {"bots": {BID: _bot(_conv(
                ("三小时前说的", NOW - 3 * HOUR, "out"),
                ("一分钟前说的", NOW - 60.0, "out"),
            ))}}
        )
        self.assertEqual(st.recent_own_out(BID, UID, NOW, hours=2.0), ["一分钟前说的"])

    def test_newest_first_and_limit(self):
        st = _state(
            {"bots": {BID: _bot(_conv(
                ("第一句", NOW - 400.0, "out"),
                ("第二句", NOW - 300.0, "out"),
                ("第三句", NOW - 200.0, "out"),
            ))}}
        )
        self.assertEqual(st.recent_own_out(BID, UID, NOW, limit=2), ["第三句", "第二句"])

    def test_missing_user_is_empty(self):
        st = _state({"bots": {BID: {"groups": {}, "users": {}}}})
        self.assertEqual(st.recent_own_out(BID, "nobody", NOW), [])

    def test_duplicate_text_kept_once(self):
        st = _state(
            {"bots": {BID: _bot(_conv(
                ("同一句", NOW - 300.0, "out"),
                ("同一句", NOW - 200.0, "out"),
            ))}}
        )
        self.assertEqual(st.recent_own_out(BID, UID, NOW), ["同一句"])


# ─── 二、注入：把「刚说过的话」摆到模型眼前 ─────────────────────


class ReplyEchoHintTest(unittest.TestCase):
    def _hint(self, conv=None, **cfg):
        conv = conv if conv is not None else _conv(("总算把晚饭备好了", NOW - 60.0, "out"))
        st = _state({"bots": {BID: _bot(conv)}})
        base = dict(reply_echo_guard=True, reply_echo_hours=2.0, reply_echo_limit=4)
        base.update(cfg)
        return engine_mod.SocialEngine.reply_echo_hint(_engine(st, **base), BID, UID)

    def test_contains_tag_and_numbering(self):
        hint = self._hint()
        self.assertIn("<autonomous_social_echo_guard>", hint)
        self.assertIn("</autonomous_social_echo_guard>", hint)
        self.assertIn("1. 总算把晚饭备好了", hint)
        self.assertIn("别再讲第二遍", hint)

    def test_switch_off(self):
        self.assertEqual(self._hint(reply_echo_guard=False), "")

    def test_zero_hours_disables(self):
        self.assertEqual(self._hint(reply_echo_hours=0.0), "")

    def test_no_rows_is_empty(self):
        self.assertEqual(self._hint(conv=_conv(("对方说的话", NOW - 60.0, "in"))), "")

    def test_limit_respected(self):
        conv = _conv(
            ("第一句", NOW - 400.0, "out"),
            ("第二句", NOW - 300.0, "out"),
            ("第三句", NOW - 200.0, "out"),
        )
        hint = self._hint(conv=conv, reply_echo_limit=1)
        self.assertIn("1. 第三句", hint)
        self.assertNotIn("第一句", hint)

    def test_long_line_truncated(self):
        long_text = "傍晚用蹄子夹着锅柄把妹妹带的东西热了一锅" * 3
        hint = self._hint(conv=_conv((long_text, NOW - 60.0, "out")))
        self.assertIn("…", hint)
        self.assertNotIn(long_text, hint)

    def test_stand_in_without_new_method_is_safe(self):
        eng = object.__new__(engine_mod.SocialEngine)
        eng.cfg = types.SimpleNamespace(
            reply_echo_guard=True, reply_echo_hours=2.0, reply_echo_limit=4
        )
        eng.state = types.SimpleNamespace()
        eng._time = lambda: NOW
        self.assertEqual(engine_mod.SocialEngine.reply_echo_hint(eng, BID, UID), "")

    def test_blank_ids_are_safe(self):
        st = _state({"bots": {BID: _bot(_conv(("随便一句", NOW - 60.0, "out")))}})
        eng = _engine(st, reply_echo_guard=True, reply_echo_hours=2.0, reply_echo_limit=4)
        self.assertEqual(
            engine_mod.SocialEngine.reply_echo_hint(eng, "", UID), "", "没 bid 还拼提示词"
        )
        self.assertEqual(
            engine_mod.SocialEngine.reply_echo_hint(eng, BID, ""), "", "没 uid 还拼提示词"
        )


# ─── 三、同一次发言内部去重 ─────────────────────────────────────


class DedupeSegmentsTest(unittest.TestCase):
    """一次拆段里说了同一件事的第二段不发。"""

    def test_same_event_collapsed(self):
        segs = ["折腾半天，总算把晚饭备好了", "总算把晚饭备好了"]
        kept, waits = engine_mod._dedupe_same_event_segments(segs, [1.0])
        self.assertEqual(len(kept), 1, kept)
        self.assertEqual(kept[0], segs[0], "该留前面那段")
        self.assertEqual(waits, [], "只剩一段就不该还有段间隔")

    def test_different_events_kept(self):
        segs = ["刚把茶喝完", "刚趴窗边看外头"]
        kept, waits = engine_mod._dedupe_same_event_segments(segs, [2.0])
        self.assertEqual(len(kept), 2, kept)
        self.assertEqual(waits, [2.0])

    def test_waits_rebuilt_on_middle_drop(self):
        segs = [
            "傍晚用蹄子夹着锅柄把妹妹带的东西热了一锅",
            "把妹妹带来的东西热了一锅",
            "你要不要也来尝尝",
        ]
        kept, waits = engine_mod._dedupe_same_event_segments(segs, [1.0, 2.0])
        self.assertEqual(kept, [segs[0], segs[2]], kept)
        self.assertEqual(len(waits), 1, waits)

    def test_single_segment_untouched(self):
        kept, waits = engine_mod._dedupe_same_event_segments(["只有一句"], [])
        self.assertEqual(kept, ["只有一句"])
        self.assertEqual(waits, [])


# ─── 四、配置项与 schema ────────────────────────────────────────


class ConfigAndSchemaTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(cfg_mod.REPLY_ECHO_HOURS_DEFAULT, 2.0)
        self.assertEqual(cfg_mod.REPLY_ECHO_HOURS_MAX, 72.0)
        self.assertEqual(cfg_mod.REPLY_ECHO_LIMIT_DEFAULT, 4)
        self.assertEqual(cfg_mod.REPLY_ECHO_LIMIT_MAX, 12)

    def test_schema_keys(self):
        with open(
            os.path.join(PLUGIN_ROOT, "_conf_schema.json"), encoding="utf-8"
        ) as fh:
            schema = json.load(fh)
        expect = {
            "reply_echo_guard": ("bool", True),
            "reply_echo_hours": ("float", 2.0),
            "reply_echo_limit": ("int", 4),
        }
        for key, (typ, default) in expect.items():
            self.assertIn(key, schema, f"schema 缺 {key}")
            self.assertEqual(schema[key]["type"], typ, key)
            self.assertEqual(schema[key]["default"], default, key)

    def test_schema_sliders(self):
        # 没有 slider 块的数值项会被 test_v1273_review_fixes 的 ConfigSliderKeyTest 判红。
        with open(
            os.path.join(PLUGIN_ROOT, "_conf_schema.json"), encoding="utf-8"
        ) as fh:
            schema = json.load(fh)
        for key in ("reply_echo_hours", "reply_echo_limit"):
            self.assertIn("slider", schema[key], f"{key} 缺 slider")


# ─── 五、接线：两条链路都得真的调────────────────────────────────


class WiringTest(unittest.TestCase):
    def test_engine_source_calls_dedupe(self):
        with open(
            os.path.join(PLUGIN_ROOT, "social", "engine.py"), encoding="utf-8"
        ) as fh:
            src = fh.read()
        self.assertIn("_dedupe_same_event_segments(segments, waits)", src)
        self.assertGreaterEqual(
            src.count("_dedupe_same_event_segments("), 2, "只定义没调用"
        )

    def test_main_injects_echo_hint(self):
        with open(os.path.join(PLUGIN_ROOT, "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("reply_echo_hint", src, "on_llm_request 没把刚说过的话给她")
        self.assertIn("mark_as_temp", src, "不该落进历史")


if __name__ == "__main__":
    unittest.main()
