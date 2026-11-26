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

from social.engine import _flow_quality_issue


class FlowQualityCompatibilityTest(unittest.TestCase):
    def test_soft_style_signals_do_not_block_generated_text(self):
        examples = (
            "你那边呢",
            "是啊",
            "今天降温降得挺突然，出门得多加一件",
            "我觉得还行吧",
            "三个也够了，别折腾了，我再眯会儿……",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(_flow_quality_issue([text]), "")

    def test_opening_history_is_not_a_quality_gate(self):
        self.assertEqual(
            _flow_quality_issue(["刚把水烧上"], openings=["刚睡醒", "刚喝完茶"]),
            "",
        )


if __name__ == "__main__":
    unittest.main()
