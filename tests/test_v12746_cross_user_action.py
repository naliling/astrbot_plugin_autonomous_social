"""跨用户动作标签：同一个动作被套在好几个人身上就该拦。

用户的原话：「放下手中的活这些话很影响聊天…和用户聊天时一点也不相关，还会重复出现。」

线上统计（524 条主动消息）：15 个动作标签被套在多个聊天对象身上——
「蜷在沙发」10 人、「伸个懒腰」9 人、「窝在沙发」6 人。正文各不相同，所以
`_check_cross_user`（比正文）不响；按人比对的 `_check_action_repeat` 也永远不响。
"""
from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v12746_cross_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.verify import _check_cross_user_action  # noqa: E402

_OTHERS_2 = [
    ("u1", "(蜷在沙发) 今天什么都不想干。"),
    ("u2", "(蜷在沙发) 外面下雨了，出门算了。"),
]
_OTHERS_1 = [("u1", "(蜷在沙发) 今天什么都不想干。")]


class CrossUserActionTest(unittest.TestCase):
    def test_tag_shared_by_two_people_is_blocked(self) -> None:
        why = _check_cross_user_action("(蜷在沙发) 你说的那事我记着呢。", _OTHERS_2)
        self.assertIsNotNone(why)
        self.assertIn("蜷在沙发", why)
        self.assertIn("2 个人", why)

    def test_one_other_is_enough(self) -> None:
        # v1.27.54：一轮里同一条动作发给两个不同的人就是「怪」，门槛从 2 人降到 1 人
        why = _check_cross_user_action("(蜷在沙发) 突然想起你。", _OTHERS_1)
        self.assertIsNotNone(why)

    def test_a_different_action_passes(self) -> None:
        self.assertIsNone(_check_cross_user_action("(把手机扣在桌上) 你说什么？", _OTHERS_2))

    def test_message_without_action_passes(self) -> None:
        self.assertIsNone(_check_cross_user_action("你说的那事我记着呢。", _OTHERS_2))

    def test_short_persona_tag_is_never_counted(self) -> None:
        others = [("u1", "(轻笑) 嗯。"), ("u2", "(轻笑) 好。")]
        self.assertIsNone(_check_cross_user_action("(轻笑) 我知道。", others))

    def test_empty_others(self) -> None:
        self.assertIsNone(_check_cross_user_action("(蜷在沙发) 嗯。", []))


if __name__ == "__main__":
    unittest.main()
