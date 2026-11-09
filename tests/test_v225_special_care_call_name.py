"""v2.2.5：特别关心名单支持专属称谓（issue #5 的修正版实现）。

用户要的是：名单里除了 uid / 平台:uid，还能写「uid:称谓」「平台:uid:称谓」，
私聊主动消息里优先用这个称谓叫她。

和 issue 原补丁的差别（原补丁不能原样合入的原因）：

* 注入点不是 `_render_persona_block`（本仓库没有这个方法），而是
  `_apply_core_brief` + 生成侧 `_bond_line`；
* 不覆盖 `_nickname`（那个字段的契约是「Core 认定的称呼」），单独写 `_pet_name`；
* 群聊显式隔离，不依赖「这条链路目前只跑私聊」；
* `special_care_entries()` 保持旧签名和旧返回形态，称谓不参与「是不是特别关心」判定。
"""

from __future__ import annotations

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


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v225_test")
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

from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social.generator import _bond_line  # noqa: E402

BRIEF = {
    "v": 2,
    "user": {
        "nickname": "认定昵称",
        "display_name": "名片名",
        "call_name": "名片名",
        "name_source": "display_name",
    },
}


def _cfg(*entries) -> SocialConfig:
    return SocialConfig(special_care_uids=list(entries))


class SpecialCareParsedEntriesTest(unittest.TestCase):
    def test_four_forms(self):
        cfg = _cfg("10086", "aiocqhttp:10087", "10088:大当家", "aiocqhttp:10089:二当家")
        self.assertEqual(
            cfg.special_care_parsed_entries(),
            [
                ("", "10086", ""),
                ("aiocqhttp", "10087", ""),
                ("", "10088", "大当家"),
                ("aiocqhttp", "10089", "二当家"),
            ],
        )

    def test_fullwidth_colon_and_separators(self):
        cfg = _cfg("10086：大当家，aiocqhttp：10087：二当家; 10088")
        self.assertEqual(
            cfg.special_care_parsed_entries(),
            [
                ("", "10086", "大当家"),
                ("aiocqhttp", "10087", "二当家"),
                ("", "10088", ""),
            ],
        )

    def test_string_form(self):
        cfg = SocialConfig(special_care_uids="10086:大当家，10087; 10088")
        self.assertEqual(
            cfg.special_care_parsed_entries(),
            [("", "10086", "大当家"), ("", "10087", ""), ("", "10088", "")],
        )

    def test_bad_entries_are_skipped(self):
        cfg = SocialConfig(special_care_uids=["", "  ", ":", "aiocqhttp:", None])
        self.assertEqual(cfg.special_care_parsed_entries(), [])

    def test_non_numeric_uid_two_part_is_platform(self):
        # 文档约定：两段式只认纯数字 uid 当「uid:称谓」，非数字 uid 请写三段式。
        cfg = _cfg("openidABC:小明")
        self.assertEqual(cfg.special_care_parsed_entries(), [("openidABC", "小明", "")])

    def test_entries_unchanged_and_call_dropped(self):
        cfg = _cfg("10086", "aiocqhttp:10087", "10088:大当家", "aiocqhttp:10089:二当家")
        self.assertEqual(
            cfg.special_care_entries(), {"10086", "aiocqhttp:10087", "10088", "aiocqhttp:10089"}
        )

    def test_entries_deduped(self):
        cfg = _cfg("10086", "10086:大当家", "10086:二当家")
        self.assertEqual(cfg.special_care_entries(), {"10086"})


class SpecialCareCallNameTest(unittest.TestCase):
    def test_generic(self):
        cfg = _cfg("10086:大当家")
        self.assertEqual(cfg.special_care_call_name("10086", "aiocqhttp"), "大当家")
        self.assertEqual(cfg.special_care_call_name("10086", ""), "大当家")

    def test_platform_specific_wins(self):
        cfg = _cfg("10086:通用", "aiocqhttp:10086:专属")
        self.assertEqual(cfg.special_care_call_name("10086", "aiocqhttp"), "专属")
        self.assertEqual(cfg.special_care_call_name("10086", "telegram"), "通用")

    def test_platform_case_insensitive(self):
        cfg = _cfg("AIOCQHTTP:10086:老王")
        self.assertEqual(cfg.special_care_call_name("10086", "aiocqhttp"), "老王")

    def test_no_call_or_unknown_uid(self):
        cfg = _cfg("10086", "aiocqhttp:10087")
        self.assertEqual(cfg.special_care_call_name("10086", "aiocqhttp"), "")
        self.assertEqual(cfg.special_care_call_name("10087", "telegram"), "")
        self.assertEqual(cfg.special_care_call_name("", "aiocqhttp"), "")


class SpecialCareManualStillWorksTest(unittest.TestCase):
    def test_call_name_does_not_break_manual_match(self):
        cfg = _cfg("10086:大当家")
        self.assertEqual(cfg.special_care_entries(), {"10086"})
        self.assertTrue(SocialEngine._special_care_manual(
            types.SimpleNamespace(cfg=cfg), "10086", {"umo": "aiocqhttp:FriendMessage:10086"}
        ))

    def test_platform_entry_with_call(self):
        eng = types.SimpleNamespace(cfg=_cfg("aiocqhttp:10086:大当家"))
        self.assertTrue(
            SocialEngine._special_care_manual(eng, "10086", {"umo": "aiocqhttp:FriendMessage:10086"})
        )
        self.assertFalse(
            SocialEngine._special_care_manual(eng, "10086", {"umo": "webchat:FriendMessage:10086"})
        )


class SpecialCareEngineInjectTest(unittest.TestCase):
    def _apply(self, cfg, umo="aiocqhttp:FriendMessage:10086", uid="10086"):
        u_copy = {}
        SocialEngine._apply_core_brief(
            u_copy, BRIEF, None, {"umo": umo, "__uid": uid}, cfg=cfg, uid=uid
        )
        return u_copy

    def test_pet_name_written_without_touching_nickname(self):
        u_copy = self._apply(_cfg("10086:大当家"))
        self.assertEqual(u_copy.get("_pet_name"), "大当家")
        self.assertEqual(u_copy.get("_nickname"), "认定昵称")
        self.assertEqual(u_copy.get("_call_name"), "名片名")

    def test_four_arg_call_still_works(self):
        u_copy = {}
        SocialEngine._apply_core_brief(u_copy, BRIEF, None, {})
        self.assertNotIn("_pet_name", u_copy)
        self.assertEqual(u_copy.get("_nickname"), "认定昵称")

    def test_group_message_isolated(self):
        u_copy = self._apply(
            _cfg("10086:大当家"), umo="aiocqhttp:GroupMessage:10086"
        )
        self.assertNotIn("_pet_name", u_copy)

    def test_platform_mismatch(self):
        u_copy = self._apply(
            _cfg("telegram:10086:大当家"), umo="aiocqhttp:FriendMessage:10086"
        )
        self.assertNotIn("_pet_name", u_copy)

    def test_disabled_switch(self):
        cfg = _cfg("10086:大当家")
        cfg.special_care_enabled = False
        self.assertNotIn("_pet_name", self._apply(cfg))

    def test_cfg_none_or_broken_is_safe(self):
        self.assertNotIn("_pet_name", self._apply(None))
        broken = types.SimpleNamespace(special_care_enabled=True)
        self.assertNotIn("_pet_name", self._apply(broken))


class SpecialCareBondLineTest(unittest.TestCase):
    def test_pet_name_has_highest_priority(self):
        line = _bond_line(
            {
                "_pet_name": "大当家",
                "_nickname": "认定昵称",
                "_call_name": "名片名",
                "_name_source": "display_name",
            }
        )
        self.assertIn("你一直叫TA「大当家」", line)
        self.assertNotIn("认定昵称", line)
        self.assertNotIn("名片名", line)
        self.assertIn("开口要叫一声TA的称呼", line)

    def test_no_pet_name_keeps_old_wording(self):
        line = _bond_line({"_nickname": "认定昵称", "_call_name": "名片名", "_name_source": "display_name"})
        self.assertIn("你一直叫TA「认定昵称」", line)

    def test_no_pet_name_display_name_wording(self):
        line = _bond_line({"_call_name": "名片名", "_name_source": "display_name"})
        self.assertIn("TA 的名片名是「名片名」", line)


class SpecialCareWiringTest(unittest.TestCase):
    def test_both_private_proactive_paths_pass_cfg_and_uid(self):
        src = (PLUGIN_ROOT / "social" / "engine.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count('cfg=getattr(self, "cfg", None), uid=uid'), 2)

    def test_generator_reads_pet_name(self):
        src = (PLUGIN_ROOT / "social" / "generator.py").read_text(encoding="utf-8")
        self.assertIn('target.get("_pet_name")', src)


if __name__ == "__main__":
    unittest.main()
