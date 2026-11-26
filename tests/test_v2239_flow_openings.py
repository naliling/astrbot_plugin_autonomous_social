"""Regression: legacy opening history is context, not a rotation rule.

Recent conversation context and optional SEND/NO output remain available.
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v2239_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import social.generator as gen_mod  # noqa: E402

GEN = gen_mod.MessageGenerator(None, None)
PERSONA = "你叫露娜，是蹄类角色。"


def _ctx(openings=None):
    ctx = {
        "recent": [
            {"name": "阿澈", "text": "今天加班到十点，脑子已经木了", "self": False},
            {"name": "露娜", "text": "（把翅膀收起来）本宫才不关心这些", "self": True},
        ],
        "latest": {"name": "阿澈", "text": "今天加班到十点，脑子已经木了"},
        "own_recent": ["（把翅膀收起来）本宫才不关心这些"],
        "topic_hint": "- 刚把桌上的旧票据收进抽屉",
        "style_ref": "阿澈：哈哈行",
        "emoji": "",
        "group_name": "测试群",
        "members": ["阿澈"],
        "addressed": None,
        "reason": "",
        "no_question": False,
    }
    if openings is not None:
        ctx["openings"] = openings
    return ctx


def _prompt(mode, openings=None):
    return GEN._compose_group(mode, _ctx(openings), PERSONA, 120)[0]


class NeutralOpeningContextTest(unittest.TestCase):
    def test_recent_openings_do_not_change_flow_prompt(self):
        baseline = _prompt("flow")
        with_legacy_openings = _prompt(
            "flow", ["刚用魔法烧了壶水", "刚把窗户推开", "刚睡醒，睡0小时"]
        )
        self.assertEqual(with_legacy_openings, baseline)

    def test_recent_openings_do_not_change_icebreak_prompt(self):
        baseline = _prompt("icebreak")
        with_legacy_openings = _prompt(
            "icebreak", ["刚用魔法烧了壶水", "刚把窗户推开", "刚睡醒，睡0小时"]
        )
        self.assertEqual(with_legacy_openings, baseline)

    def test_group_context_and_optional_decision_remain(self):
        prompt = _prompt("flow", ["刚用魔法烧了壶水"])
        self.assertIn("群里最近在聊", prompt)
        self.assertIn("SEND", prompt)
        self.assertIn("NO", prompt)
        self.assertNotIn("这一条不要用「刚」开头", prompt)
        self.assertNotIn("你最近在群里开口用过的开头", prompt)


if __name__ == "__main__":
    unittest.main()
