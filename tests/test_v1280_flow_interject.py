"""v1.28.0 社交侧 S3：别人对聊时可以插话，由模型决定。

用户原话：「别人对话时就不会插嘴（不是必须），但是能不能插嘴看模型」。

旧行为是两道**硬拦**（`addressed_to` / `two_person_exchange`）：模型根本没有判断
机会。这一版把语境写进提示词交给模型，插件只保留频率/窗口等护栏；`group_interject_others`
关掉时退回旧语义（这种场合一律不插）。

本文件钉住：
* `flow_context_note` 的四种组合（叫别人 / 两人对聊 / 两者都有 / 都没有）；
* `flow_should_consider` 的新参数与「硬闸仍在」；
* flow prompt 里的【现在的群聊语境】块和「别叫名字」；
* engine 两处调用点都接上了配置、语境与「刚插过话先缓一缓」的间隔。
"""
from __future__ import annotations

import inspect
import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = (
    _HERE.parent
    if (_HERE.parent / "_conf_schema.json").is_file()
    else _HERE.parent / "astrbot_plugin_autonomous_social"
)


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v1280_flow_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import groupflow  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social.generator import MessageGenerator  # noqa: E402

NOW = 1_700_000_000.0


def _group(**kw):
    g = {
        "blocked_until": 0.0,
        "flow_open_until": NOW + 600,
        "flow_hold_until": 0.0,
        "flow_ignored": 0,
        "flow_replies": 0,
        "flow_last_reply_at": NOW - 600,
    }
    g.update(kw)
    return g


def _gate(g, **kw):
    args = dict(
        max_replies=3,
        min_gap_seconds=45,
        hourly_cap=6,
        hour_count=0,
        ignored_exit=2,
    )
    args.update(kw)
    return groupflow.flow_should_consider(g, NOW, **args)


class FlowContextNoteTest(unittest.TestCase):
    """把「群里现在谁在跟谁说话」写成一句给模型看的语境。"""

    def test_addressed_name_is_reported(self):
        note = groupflow.flow_context_note([], "小明")
        self.assertIn("小明", note)
        self.assertIn("不是跟你说", note)

    def test_two_person_exchange_is_reported_with_names(self):
        note = groupflow.flow_context_note(["阿哲", "小鱼", "阿哲"], "")
        self.assertIn("阿哲", note)
        self.assertIn("小鱼", note)
        self.assertIn("来回聊", note)

    def test_both_are_merged_into_one_sentence(self):
        note = groupflow.flow_context_note(["阿哲", "小鱼", "阿哲"], "小明")
        self.assertIn("来回聊", note)
        self.assertIn("小明", note)
        self.assertIn("不是跟你说", note)

    def test_no_context_is_empty(self):
        self.assertEqual(groupflow.flow_context_note([], ""), "")
        # 三个人在聊不算「对聊」——语境为空，照常放行
        self.assertEqual(
            groupflow.flow_context_note(["阿哲", "小鱼", "阿岚"], ""), ""
        )


class FlowInterjectGateTest(unittest.TestCase):
    """S3：语境不再硬拦；配置/节奏护栏保留；其余硬闸照旧生效。"""

    def test_two_person_exchange_is_now_allowed(self):
        note = groupflow.flow_context_note(["阿哲", "小鱼", "阿哲"], "")
        ok, why = _gate(_group(), context_note=note, interject_enabled=True)
        self.assertTrue(ok, why)

    def test_disabled_config_restores_the_old_hard_block(self):
        """`group_interject_others=False` 就是旧语义：两人对聊一律不插。"""
        note = groupflow.flow_context_note(["阿哲", "小鱼", "阿哲"], "")
        ok, why = _gate(_group(), context_note=note, interject_enabled=False)
        self.assertFalse(ok)
        self.assertIn("按配置不插话", why)

    def test_addressed_to_someone_else_is_now_allowed(self):
        note = groupflow.flow_context_note([], "小明")
        ok, why = _gate(_group(), context_note=note, interject_enabled=True)
        self.assertTrue(ok, why)

    def test_just_spoke_to_the_duet_must_wait(self):
        """刚插过话（几秒前）别人还在对聊：模型看到的语境已经过时，先缓一缓。"""
        note = groupflow.flow_context_note(["阿哲", "小鱼", "阿哲"], "")
        ok, why = _gate(
            _group(flow_last_reply_at=NOW - 60),
            context_note=note,
            interject_enabled=True,
            interject_gap_seconds=180,
        )
        self.assertFalse(ok)
        self.assertIn("先缓一缓", why)

    def test_no_context_note_skips_the_interject_brake(self):
        ok, why = _gate(
            _group(flow_last_reply_at=NOW - 60),
            context_note="",
            interject_enabled=True,
            interject_gap_seconds=180,
        )
        self.assertTrue(ok, why)

    def test_other_hard_gates_still_apply(self):
        """语境放开了，不代表刷屏/窗口/睡眠这些闸也放开。"""
        note = groupflow.flow_context_note(["阿哲", "小鱼", "阿哲"], "")
        cases = {
            "min_gap": (_gate(_group(flow_last_reply_at=NOW - 10),
                              context_note=note,
                              interject_gap_seconds=180), "距上一句插话太近"),
            "window": (_gate(_group(flow_open_until=NOW - 1),
                             context_note=note), "关注窗口"),
            "hourly": (_gate(_group(), context_note=note, hourly_cap=6,
                             hour_count=6), "上限"),
            "quiet": (_gate(_group(), context_note=note, is_quiet=True), "安静"),
            "asleep": (_gate(_group(), context_note=note, asleep=True), "睡"),
        }
        for label, (result, want) in cases.items():
            with self.subTest(gate=label):
                ok, why = result
                self.assertFalse(ok, f"{label} 这道闸没拦住")
                self.assertIn(want, why)


class FlowPromptContextTest(unittest.TestCase):
    """S3：语境进 flow prompt，并明确「插不插由你」。"""

    def _prompt(self, note=""):
        gen = MessageGenerator(None, None)
        return gen._compose_group(
            "flow", {"context_note": note, "latest": {"name": "甲", "text": "在吗"}},
            "人设", 40,
        )[0]

    def test_context_block_is_present(self):
        prompt = self._prompt(groupflow.flow_context_note(["阿哲", "小鱼", "阿哲"], ""))
        self.assertIn("【现在的群聊语境】", prompt)
        self.assertIn("来回聊", prompt)

    def test_model_is_told_it_may_skip(self):
        prompt = self._prompt(groupflow.flow_context_note([], "小明"))
        self.assertIn("你可以不插", prompt)
        self.assertIn("否则 NO", prompt)
        self.assertIn("别叫正在聊的那几个人的名字", prompt)

    def test_no_context_adds_no_block(self):
        prompt = self._prompt("")
        # 指令句里本来就会提「先看【现在的群聊语境】」，所以按语境块**正文**判。
        self.assertNotIn("他们会看到你在插话", prompt)

    def test_two_step_judgement_points_at_the_context_block(self):
        prompt = self._prompt("群里现在是 阿哲 和 小鱼 在来回聊")
        self.assertIn("先看【现在的群聊语境】", prompt)
        # 旧的硬性口径（「大家在对着某个具体的人说事就别接」）改成了「接不上就 NO」
        self.assertNotIn("大家在对着某个具体的人说事", prompt)


class FlowMentionPromptTest(unittest.TestCase):
    """S1 + S3 交界：flow 里允许点名时也只允许叫正在接的那个人。"""

    def _gen(self, mention: bool):
        return MessageGenerator(None, SocialConfig(group_mention_member=mention))

    def test_mention_on_names_only_the_latest_speaker(self):
        prompt, _ = self._gen(True)._compose_group(
            "flow",
            {"members": ["小明", "小红"], "latest": {"name": "小明", "text": "在吗"}},
            "人设",
            40,
        )
        self.assertIn("小明", prompt)
        self.assertNotIn("小红", prompt, "把整个群名单递给模型了")
        self.assertIn("正在接的那个人", prompt)

    def test_mention_off_says_do_not_name_anyone(self):
        prompt, _ = self._gen(False)._compose_group(
            "flow", {"members": ["小明"]}, "人设", 40
        )
        self.assertNotIn("小明", prompt)
        self.assertIn("不要点名任何群友", prompt)


class InterjectConfigTest(unittest.TestCase):
    """新配置项：默认开、能在面板/代码两侧生效。"""

    def test_default_is_on(self):
        self.assertTrue(SocialConfig().group_interject_others)
        self.assertTrue(
            SocialConfig.from_astrbot({}).group_interject_others,
            "from_astrbot 没读这一项（面板上的开关不会生效）",
        )

    def test_can_be_turned_off(self):
        self.assertFalse(
            SocialConfig.from_astrbot({"group_interject_others": False})
            .group_interject_others
        )

    def test_schema_agrees(self):
        import json

        schema = json.loads(
            (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        spec = schema["group_interject_others"]
        self.assertEqual(spec["type"], "bool")
        self.assertIs(spec["default"], True)
        self.assertIn("模型判断", spec["hint"])


class EngineInterjectWiringTest(unittest.TestCase):
    """engine 的两条心流路径都要把语境/配置/间隔接上（结构性钉子）。"""

    @classmethod
    def setUpClass(cls):
        # 设计文档里叫 `_on_group_message`，代码里的实际方法名是 `_observe_group`。
        cls.on_msg = inspect.getsource(SocialEngine._observe_group)
        cls.inner = inspect.getsource(SocialEngine._flow_reply_inner)

    def test_both_paths_compute_the_context_note(self):
        for label, src in (("_observe_group", self.on_msg),
                           ("_flow_reply_inner", self.inner)):
            with self.subTest(path=label):
                self.assertIn("flow_context_note", src, f"{label} 没算语境")

    def test_both_paths_pass_the_config_and_the_note(self):
        for label, src in (("_observe_group", self.on_msg),
                           ("_flow_reply_inner", self.inner)):
            with self.subTest(path=label):
                self.assertIn("interject_enabled=self.cfg.group_interject_others", src)
                self.assertIn("context_note=context_note", src)
                self.assertIn("interject_gap_seconds=max(", src)

    def test_generation_path_puts_the_note_into_group_ctx(self):
        self.assertIn('group_ctx["context_note"] = context_note', self.inner)

    def test_old_hard_block_parameters_are_gone(self):
        for label, src in (("_observe_group", self.on_msg),
                           ("_flow_reply_inner", self.inner)):
            with self.subTest(path=label):
                self.assertNotIn("addressed_to_name=", src)
                self.assertNotIn("recent_speakers=", src)


if __name__ == "__main__":
    unittest.main()
