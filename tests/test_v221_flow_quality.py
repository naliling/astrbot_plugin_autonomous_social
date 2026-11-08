# -*- coding: utf-8 -*-
"""v2.2.1 / v2.2.2：群心流插话的质量闸。

用户原话两轮：「那不应该加质量吗？」「插话里必须带一件她自己的具体事，要么不发」
——不砍量，改质量：插话是补充这件事，不是跟某一个人对话，不是整条只是附和，
而且必须带一件她自己的具体事。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


class _FakeMessageChain:
    def __init__(self, *a, **k):
        pass

    @classmethod
    def from_str(cls, text):
        return cls()

    def message(self, *a, **k):
        return self

    def file_image(self, *a, **k):
        return self

    def get_plain_text(self):
        return ""


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _FakeMessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.engine import _flow_personal_detail_issue, _flow_quality_issue  # noqa: E402


class SidewaysTalkTest(unittest.TestCase):
    """没人叫她的时候，插话不许对着某一个人说话。"""

    def test_talking_to_one_person_is_rejected(self):
        # 线上被投诉的原句：「那你先扛得住我四蹄踩」——没人叫她，她却在接某个人的话。
        for text in (
            "巨龟叔叔？行，那你先扛得住我四蹄踩❤️",
            "您先忙，我这边不急",
            "你们先聊，你那边不急，我这边刚好",  # 「你们」放行，但后面出现了单数「你」
        ):
            with self.subTest(text=text):
                self.assertTrue(_flow_quality_issue([text]))

    def test_plural_you_is_fine(self):
        # 「你们」是对全场说的，不是对某一个人。
        self.assertEqual(
            _flow_quality_issue(["你们聊的这个我也在琢磨，我刚把杯子放下"]), ""
        )


class PureEchoTest(unittest.TestCase):
    """整条只是附和的话，群里读起来就是「接了个不存在的话」。"""

    def test_bare_agreement_is_rejected(self):
        for text in ("确实", "哈哈哈哈哈", "绝了！", "是啊。"):
            with self.subTest(text=text):
                self.assertTrue(_flow_quality_issue([text]))

    def test_agreement_with_substance_is_fine(self):
        self.assertEqual(
            _flow_quality_issue(["确实，我刚把外套脱了，屋里还是闷"]), ""
        )

    def test_empty_is_fine(self):
        self.assertEqual(_flow_quality_issue([]), "")
        self.assertEqual(_flow_quality_issue([""]), "")


class PersonalDetailTest(unittest.TestCase):
    """v2.2.2：每条插话都必须带一件她自己的具体事，否则不发。"""

    def test_comment_only_is_rejected(self):
        # 线上原句：只在点评别人的事，没有一件她自己的事。
        for text in (
            "和弦转换卡住太正常了，多练几遍手指就有记忆了❤️",
            "今天降温降得挺突然，出门得多加一件",
            "点十个赞还嫌少，这胃口是真不小❤️",
        ):
            with self.subTest(text=text):
                self.assertTrue(_flow_quality_issue([text]))

    def test_first_person_without_concrete_thing_is_rejected(self):
        for text in ("我也觉得", "我觉得还行吧", "我也在琢磨这个"):
            with self.subTest(text=text):
                self.assertTrue(_flow_quality_issue([text]))

    def test_her_own_concrete_thing_is_fine(self):
        for text in (
            "我这边刚把咖啡放凉了都没顾上喝",
            "侧躺久了换个姿势，我尾巴压麻了",
            "我刚把水烧上，等会儿得泡点东西喝",
        ):
            with self.subTest(text=text):
                self.assertEqual(_flow_quality_issue([text]), "")

    def test_multi_segment_uses_the_whole_reply(self):
        self.assertEqual(
            _flow_quality_issue(["我这边水刚烧上，", "等会儿再聊"]), ""
        )

    def test_reason_is_readable(self):
        # 日志里要能看懂为什么没发。
        self.assertIn("没提她自己", _flow_personal_detail_issue("和弦转换卡住了"))
        self.assertIn(
            "只有评价", _flow_personal_detail_issue("我也觉得这事儿挺麻烦的")
        )


if __name__ == "__main__":
    unittest.main()
