# -*- coding: utf-8 -*-
"""v2.2.23：动作骨架也回头看一眼。

## 为什么

用户说「动作描写模板化：六条里动作只有两种，重复六次」。前几版只修了**主动消息**
的动作标签复读（`_check_action_repeat`），门槛是 `_ACTION_TAG_MIN = 4`，而且
普通回复这条链路（1327/1839 条）根本没有动作轮换。

线上全量语料（去重后 1839 条）的骨架统计：

* 私聊 647/1327 = 48.8% 以括号动作开头；
* 823/1839 = 44.8% 以表情结尾，其中 ❤/💕 占 98%；
* 「动作 + 台词 + 爱心」三件套齐全 545/1839 = 29.6%；
* 动作复用前几名：`伸懒腰` 7 次、`伸个懒腰` 6 次、`红瞳微眯，散发草莓酒香` 4 次
  —— 全是 3 字以内的标签，4 字门槛一条都拦不到。

普通回复不能拦（拦了就是不回人话），所以照旧只给事实：`action_tics` 算出
「最近几条又用了同一个动作 / 又是先写动作再说话」，由 `reply_echo_hint`
写进开口前的临时块，她自己换。

## 本文件盯的

* `action_tics` 能抓到 3 字动作，而 `_check_action_repeat` 抓不到（证明这个缺口真实存在）；
* 只出现一次不啰嗦、窗口外的旧账不算；
* 括号动作密度 ≥3/5 才提，不滥报；
* 注入块里真的带上这句话，历史干净时不带，且**不改写正文**。
"""
import os
import sys
import tempfile
import types
import unittest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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

from social import verify as v_mod  # noqa: E402
import social.engine as engine_mod  # noqa: E402
from social.state import SocialState  # noqa: E402

NOW = 1800000000.0
BID = "bot1"
UID = "u1"


def _state(data=None):
    tmp = tempfile.mkdtemp(prefix="aso_v2223_")
    st = SocialState(os.path.join(tmp, "state.json"))
    if data is not None:
        st.data = data
    return st


def _conv(*rows):
    """rows: (text, ts, dir)"""
    return [{"text": t, "ts": ts, "dir": d, "src": "chat"} for (t, ts, d) in rows]


def _bot(conv):
    return {"groups": {}, "users": {UID: {"conversation": conv}}}


def _engine(state, **cfg):
    eng = object.__new__(engine_mod.SocialEngine)
    eng.cfg = types.SimpleNamespace(**cfg)
    eng.state = state
    eng._time = lambda: NOW
    return eng


# ─── 一、判据：抓得到 3 字动作，也不滥报 ────────────────────────


class ActionTicsTest(unittest.TestCase):
    def test_head_is_first_three_chars(self):
        self.assertEqual(v_mod._action_head("（伸懒腰）好困"), "伸懒腰")
        self.assertEqual(v_mod._action_head("好困，没有动作"), "")

    def test_catches_three_char_action(self):
        notes = v_mod.action_tics(
            ["（伸懒腰）好困", "（伸懒腰）再睡会儿", "（伸个懒腰）啊"]
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("伸懒腰", notes[0])
        self.assertIn("2 次", notes[0])

    def test_action_repeat_gate_misses_the_same_case(self):
        """同一个例子，老闸一条都不拦——这就是这个洞的证据。"""
        self.assertIsNone(
            v_mod._check_action_repeat(
                "（伸懒腰）好困", ["（伸懒腰）再睡会儿", "（伸个懒腰）啊"]
            )
        )

    def test_single_occurrence_is_silent(self):
        self.assertEqual(v_mod.action_tics(["（伸懒腰）好困", "好饿"]), [])

    def test_window_ignores_old_habits(self):
        old = ["（伸懒腰）一", "（伸懒腰）二", "（伸懒腰）三", "新", "新", "新"]
        self.assertEqual(v_mod.action_tics(old, window=3), [])

    def test_bracket_opening_density(self):
        three = ["（摸摸头）嗯", "（歪头）啊", "（叹气）唉", "好", "哦"]
        self.assertTrue(any("先写括号动作" in n for n in v_mod.action_tics(three)))
        two = ["（摸摸头）嗯", "（歪头）啊", "好", "哦", "行"]
        self.assertFalse(any("先写括号动作" in n for n in v_mod.action_tics(two)))

    def test_empty_input_is_safe(self):
        self.assertEqual(v_mod.action_tics([]), [])
        self.assertEqual(v_mod.action_tics([""]), [])


# ─── 二、注入：这句话要真的送到她眼前，且不改正文 ──────────────


class ReplyEchoHintTicsTest(unittest.TestCase):
    def _hint(self, conv, **cfg):
        base = dict(reply_echo_guard=True, reply_echo_hours=2.0, reply_echo_limit=4)
        base.update(cfg)
        st = _state({"bots": {BID: _bot(conv)}})
        return engine_mod.SocialEngine.reply_echo_hint(_engine(st, **base), BID, UID)

    def test_hint_carries_the_note(self):
        conv = _conv(
            ("（伸懒腰）好困", NOW - 600.0, "out"),
            ("（伸懒腰）再睡会儿", NOW - 500.0, "out"),
            ("（伸个懒腰）几点了", NOW - 400.0, "out"),
            ("唔 才六点", NOW - 300.0, "out"),
        )
        hint = self._hint(conv)
        self.assertIn("这一轮注意", hint)
        self.assertIn("伸懒腰", hint)
        self.assertIn("直接说话", hint)

    def test_clean_history_has_no_note(self):
        conv = _conv(
            ("今天好累", NOW - 300.0, "out"),
            ("早点睡吧", NOW - 200.0, "out"),
            ("我也想你", NOW - 100.0, "out"),
        )
        self.assertNotIn("这一轮注意", self._hint(conv))

    def test_hint_does_not_rewrite_text(self):
        conv = _conv(
            ("（伸懒腰）好困", NOW - 600.0, "out"),
            ("（伸懒腰）再睡会儿", NOW - 500.0, "out"),
        )
        hint = self._hint(conv)
        # 原文原样留着（只提示不改写），且仍然只有一种提示块
        self.assertIn("（伸懒腰）好困", hint)
        self.assertEqual(hint.count("这一轮注意"), 1)

    def test_switch_off_still_wins(self):
        conv = _conv(
            ("（伸懒腰）好困", NOW - 600.0, "out"),
            ("（伸懒腰）再睡会儿", NOW - 500.0, "out"),
        )
        self.assertEqual(self._hint(conv, reply_echo_guard=False), "")

    def test_stub_without_state_method_is_safe(self):
        eng = _engine(_state(), reply_echo_guard=True, reply_echo_hours=2.0,
                      reply_echo_limit=4)
        eng.state = types.SimpleNamespace()
        self.assertEqual(
            engine_mod.SocialEngine.reply_echo_hint(eng, BID, UID), ""
        )


if __name__ == "__main__":
    unittest.main()


# ─── 三、动作名本身：不能是截断词（v2.2.24）──────────────────


class ActionNameQualityTest(unittest.TestCase):
    """v2.2.23 的第一版把动作名截成前 3 个字，线上 942 个动作片段里 879 个
    （93.3%）超过 3 个字，于是提醒会说出「放下茶」「刚把取」「迷迷糊」这种半截词，
    甚至「180」「Mod」这种量词/英文——提醒是给模型看的，给半截词等于给胡话。
    v2.2.24 改成「完整短语 + 共同前缀」。
    """

    def test_measurement_prefix_is_not_the_name(self):
        text = "（180cm纯白翅膀在被窝里将你裹得严严实实，浅蓝瞳孔带着困意）嗯"
        self.assertEqual(v_mod._action_head(text), "纯白翅")
        self.assertEqual(v_mod._action_span(text).startswith("纯白翅膀"), True)

    def test_no_digits_or_ascii_in_name(self):
        for text in (
            "（180cm纯白翅膀裹着）嗯",
            "（3℃的阴冷天里贴着）好",
            "（Mod 开关切换了）啊",
        ):
            head = v_mod._action_head(text)
            self.assertTrue(head, text)
            self.assertRegex(head, r"^[\u4e00-\u9fff]+$", text)

    def test_full_span_is_kept(self):
        self.assertEqual(
            v_mod._action_span("（放下茶杯捏了捏眉心）嗯"), "放下茶杯捏了捏眉心"
        )
        self.assertEqual(v_mod._action_span("（伸懒腰）好困"), "伸懒腰")

    def test_note_uses_common_prefix(self):
        texts = [
            "（180cm纯白翅膀在被窝里将你裹得严严实实）嗯",
            "（180cm纯白翅膀懒懒散散裹着身子）如今",
            "（180cm纯白翅膀在 3℃ 的阴冷天里将你裹得密不透风）好",
        ]
        notes = v_mod.action_tics(texts)
        self.assertTrue(notes)
        self.assertIn("「纯白翅膀」", notes[0])
        self.assertNotIn("180", notes[0])

    def test_grouping_still_catches_three_char_actions(self):
        notes = v_mod.action_tics(
            ["（迷糊地蹭着床单）嗯", "（迷糊地哼了一声）啊"]
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("「迷糊地」", notes[0])

    def test_display_length_capped(self):
        long = "（用尾巴绕住手腕再轻轻收紧把人拉近怀里）嗯"
        notes = v_mod.action_tics([long, long])
        if notes:
            name = notes[0].split("「")[1].split("」")[0]
            self.assertLessEqual(len(name), v_mod._ACTION_TIC_DISPLAY)
