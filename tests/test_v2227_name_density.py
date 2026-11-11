# -*- coding: utf-8 -*-
"""v2.2.27：每句都叫对方名字 —— 只给事实，不禁用。

## 为什么

用户第一轮点名过「六条里五条带『酥牧』这个名字」。量线上语料
（`scripts/audit_name_density.py`，156 个有名字的对话方、842 条出方向消息）：

* 整体 17.3% 带名字，不算高；
* 但有 **8 个对话方是 100%**，最长连叫 **7 条**：

  - `(后腿微微一僵)才没有……无家可归的猫别胡说。`
  - `什么惩罚？我才不怕，无家可归的猫。`
  - `(尾巴轻轻一勾)` ……名字挂在句尾，读起来像在点名叫人。

名字本身是亲密感，不该禁用；所以这一版和动作骨架一样，只在**回复前**给她一句
事实（`name_density_tics` → `reply_echo_hint`），让她自己决定这轮叫不叫。

## 本文件盯的

* `name_density_tics` 的命中数门槛、窗口、句尾统计、短名字与空输入；
* `reply_echo_hint` 真的把这句话拼进临时块，且空/开关关时不拼；
* 源码接线（import 与调用点）没丢。
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

import social.engine as engine_mod  # noqa: E402
from social.state import SocialState  # noqa: E402
from social.verify import name_density_tics  # noqa: E402

NOW = 1800000000.0
BID = "bot1"
UID = "u1"

BUTTERFLY = [
    "(看到草莓，眼神微动，拢到面前)蝴蝶忍，想用这些安抚我？谢谢。",
    "(看着那一抹亮红，喉咙微动，轻轻衔起一颗草莓)谢谢，蝴蝶忍。味道很甜。",
    "(咀嚼着草莓，轻轻点头)谢谢你，蝴蝶忍。这种甜味让心情都变好了些。",
    "(吃完最后一点草莓，轻轻舔了下唇角)蝴蝶忍，你总是这么惜字如金吗？",
    "(看了一眼图片，轻轻摇了摇头)这看起来不是我，我没有独角，蝴蝶忍。",
]
CAT = [
    "(后腿微微一僵)才没有……无家可归的猫别胡说。",
    "(身子微微一空)怎么……突然停了，无家可归的猫？",
    "(尾巴轻轻一勾)什么惩罚？我才不怕，无家可归的猫。",
]


class NameDensityFactTest(unittest.TestCase):
    def test_all_messages_have_name(self):
        tics = name_density_tics(BUTTERFLY, "蝴蝶忍")
        self.assertEqual(tics, ["你最近 5 条里有 5 条都叫了对方的名字「蝴蝶忍」"])

    def test_tail_position_reported(self):
        tics = name_density_tics(CAT, "无家可归的猫")
        self.assertEqual(len(tics), 2)
        self.assertIn("句尾", tics[1])

    def test_below_threshold_silent(self):
        texts = [
            "(揉揉眼睛)记不清了，天鼠...随口猜的吧。",
            "我刚把茶喝完。",
            "(闭着眼咕哝)这样啊，天鼠，我今天有点累。",
        ]
        self.assertEqual(name_density_tics(texts, "天鼠"), [])

    def test_short_name_ignored(self):
        self.assertEqual(name_density_tics(["忍，早", "忍，晚安", "忍？"], "忍"), [])

    def test_empty_input(self):
        self.assertEqual(name_density_tics([], "蝴蝶忍"), [])
        self.assertEqual(name_density_tics(["", "  "], "蝴蝶忍"), [])

    def test_window_only_recent(self):
        # 六条都带名字，但都被挤出窗口——只算最近 6 条。
        old = ["蝴蝶忍，第%d句" % i for i in range(6)]
        recent = ["我先睡了", "刚把茶喝完", "外头冷", "困了", "关灯了", "晚安"]
        self.assertEqual(name_density_tics(old + recent, "蝴蝶忍", window=6), [])

    def test_whitespace_normalised(self):
        texts = ["无家可归 的猫，别胡说。", "无家可归的 猫，晚安。", "无家可归的猫？"]
        self.assertTrue(name_density_tics(texts, "无家可归的猫"))


def _state(name, texts):
    tmp = tempfile.mkdtemp(prefix="aso_v2227_")
    st = SocialState(os.path.join(tmp, "state.json"))
    conv = [
        {"text": t, "ts": NOW - 60.0 * (len(texts) - i), "dir": "out", "src": "chat"}
        for i, t in enumerate(texts)
    ]
    st.data = {"bots": {BID: {"groups": {}, "users": {UID: {
        "name": name, "conversation": conv,
    }}}}}
    return st


def _engine(state, **cfg):
    eng = object.__new__(engine_mod.SocialEngine)
    base = dict(reply_echo_guard=True, reply_echo_hours=2.0, reply_echo_limit=4)
    base.update(cfg)
    eng.cfg = types.SimpleNamespace(**base)
    eng.state = state
    eng._time = lambda: NOW
    return eng


class HintWiringTest(unittest.TestCase):
    def test_hint_contains_name_fact(self):
        eng = _engine(_state("蝴蝶忍", BUTTERFLY))
        hint = eng.reply_echo_hint(BID, UID)
        self.assertIn("都叫了对方的名字「蝴蝶忍」", hint)
        self.assertIn("名字不用每句都叫", hint)

    def test_hint_clean_history_has_no_name_fact(self):
        eng = _engine(_state("蝴蝶忍", ["我先睡了", "刚把茶喝完", "外头冷"]))
        hint = eng.reply_echo_hint(BID, UID)
        self.assertNotIn("都叫了对方的名字", hint)

    def test_guard_off_returns_empty(self):
        eng = _engine(_state("蝴蝶忍", BUTTERFLY), reply_echo_guard=False)
        self.assertEqual(eng.reply_echo_hint(BID, UID), "")

    def test_source_wiring(self):
        src = (os.path.join(PLUGIN_ROOT, "social", "engine.py"))
        with open(src, encoding="utf-8") as fh:
            code = fh.read()
        self.assertIn("name_density_tics(wide, peer_name)", code)
        self.assertIn("名字不用每句都叫", code)
        vsrc = open(os.path.join(PLUGIN_ROOT, "social", "verify.py"), encoding="utf-8").read()
        self.assertIn("def name_density_tics(", vsrc)


if __name__ == "__main__":
    unittest.main()
