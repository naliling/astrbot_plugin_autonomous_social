# -*- coding: utf-8 -*-
"""v2.2.49：Core 按人设排出来的卡要标 `generated`，卡面与规划约束都要提示
「她自己排的，未必真做过」——否则模型会把计划里的日程写成今天已经做过的事。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与其它测试同款，幂等） ───
_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v249_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.engine import SocialEngine  # noqa: E402
from social.generator import MessageGenerator, Plan  # noqa: E402

# 白名单里的来源都是「她排出来的生活」，不是真发生过的事。
GENERATED_SOURCES = (
    "日程", "想做的事", "未完事项", "梦境", "生活素材",
    "兴趣", "责任", "社交", "歇着", "偶发",
)
REAL_SOURCES = ("刚发生", "天气", "节日")


class GeneratedCardMarkTest(unittest.TestCase):
    """A2：`_normalize_cards` 按 source 白名单写 generated。"""

    def test_generated_sources_are_marked(self):
        cards = SocialEngine._normalize_cards(
            [{"title": f"事-{src}", "detail": "细节", "source": src}
             for src in GENERATED_SOURCES],
            at=1000.0,
        )
        self.assertEqual(len(cards), len(GENERATED_SOURCES))
        for card in cards:
            self.assertTrue(card["generated"], card)

    def test_real_sources_are_not_marked(self):
        cards = SocialEngine._normalize_cards(
            [{"title": f"事-{src}", "detail": "细节", "source": src}
             for src in REAL_SOURCES],
            at=1000.0,
        )
        self.assertEqual(len(cards), len(REAL_SOURCES))
        for card in cards:
            self.assertFalse(card["generated"], card)

    def test_missing_source_defaults_to_life_material_and_is_generated(self):
        cards = SocialEngine._normalize_cards(
            [{"title": "发呆", "detail": "看窗外"}], at=1000.0
        )
        self.assertEqual(cards[0]["source"], "生活素材")
        self.assertTrue(cards[0]["generated"])


class CardLinesGeneratedNoteTest(unittest.TestCase):
    """A3：卡面加注 + 规划约束提示「只能写成打算」。"""

    def test_generated_card_gets_note(self):
        lines = MessageGenerator._card_lines([
            {"title": "下午开会", "detail": "三点", "source": "日程", "generated": True},
            {"title": "下雨了", "detail": "带伞", "source": "天气", "generated": False},
        ])
        self.assertIn("（她自己排的，未必真做过）", lines[0])
        self.assertNotIn("未必真做过", lines[1])

    def test_card_without_generated_flag_gets_no_note(self):
        # 旧调用方/旧卡不带这个字段时不能凭空加注（兼容面）。
        lines = MessageGenerator._card_lines([
            {"title": "刚吃完饭", "detail": "", "source": "刚发生"},
        ])
        self.assertNotIn("未必真做过", lines[0])

    def test_plan_block_forbids_writing_generated_as_done(self):
        plan = Plan(
            decision="send",
            angle="日程",
            facts=["日程：下午开会（她自己排的，未必真做过）"],
        )
        block = MessageGenerator._plan_block(plan)
        self.assertIn("只能写成打算/可能", block)
        self.assertIn("不能写成做过", block)

    def test_plan_block_without_facts_keeps_old_forbid(self):
        block = MessageGenerator._plan_block(Plan(decision="send", angle="就说想她"))
        self.assertIn("不要编具体事件", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
