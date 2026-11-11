"""v2.2.39：心流接话也要知道自己最近怎么开口的。

开头轮换这道统计闸从 v2.2.7 起就挂在破冰/起话头分支上，但心流接话那条分支一直
没有——而线上「刚…」开头的群消息里，有相当一部分就是心流接话（11-10 那天 52 条
群消息里 13 条以「刚」开头）。这一版把同一份事实也递给心流：你最近用过这些开头、
这个字用滥了别再用；只给事实，不硬拦（心流是「有话说才说」，拦掉等于让她闭嘴）。
"""

from __future__ import annotations

import inspect
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
TAIL = "你最近在群里开口用过的开头"


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


class FlowOpeningsTest(unittest.TestCase):
    def test_flow_prompt_lists_recent_openings(self):
        prompt = _prompt("flow", ["刚用魔法烧了壶水", "外头阴天，毛潮"])
        self.assertIn(TAIL, prompt)
        self.assertIn("刚用魔法烧了壶水", prompt)

    def test_flow_prompt_bans_overused_head(self):
        prompt = _prompt("flow", ["刚用魔法烧了壶水", "刚把窗户推开", "刚睡醒，睡0小时"])
        self.assertIn("这一条不要用「刚」开头", prompt)

    def test_flow_prompt_no_ban_when_sparse(self):
        prompt = _prompt("flow", ["刚用魔法烧了壶水", "外头阴天，毛潮", "本宫才不关心这些"])
        self.assertNotIn("这一条不要用", prompt)

    def test_flow_works_without_openings(self):
        prompt = _prompt("flow", None)
        self.assertNotIn(TAIL, prompt)
        self.assertIn("群里最近在聊", prompt)

    def test_icebreak_still_has_openings(self):
        prompt = _prompt("icebreak", ["刚用魔法烧了壶水", "刚把窗户推开", "刚睡醒，睡0小时"])
        self.assertIn(TAIL, prompt)
        self.assertIn("这一条不要用「刚」开头", prompt)


class WiringTest(unittest.TestCase):
    def test_both_branches_ship_the_note(self):
        src = inspect.getsource(gen_mod.MessageGenerator._compose_group)
        self.assertEqual(src.count(TAIL), 2, "破冰/心流两条分支都要给开头事实")


if __name__ == "__main__":
    unittest.main()
