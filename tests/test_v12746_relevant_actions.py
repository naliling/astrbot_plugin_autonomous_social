"""v1.27.46：动作要跟这句话有关；同一个动作隔十几条也不许再出现。

用户的反馈（原话）："放下手中的活这些话很影响聊天，这些虽然一样是动作词但是和用户
聊天时一点也不相关，还会重复出现。"

两件事分开处理：

1. **语义**：只在提示词里说「只写跟这句话有关的那一下，想不出就不写」。
   不举例——举了例子模型就把例子当模板（`_SENTENCE_RULES` 上面那段注释记着这个坑）。
2. **重复**：验收窗口 8 → 20。8 条只盖住一次连发，隔十几条换个措辞挂同一个动作
   照样「重复出现」。
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
    api.logger = logging.getLogger("autonomous_social_v12746_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.generator import _SENTENCE_RULES  # noqa: E402
from social.verify import verify_message  # noqa: E402


class PromptTellsTheModelToDropIrrelevantActionsTest(unittest.TestCase):
    def test_rule_exists(self) -> None:
        joined = "".join(_SENTENCE_RULES)
        self.assertIn("跟这句话本身有关", joined)

    def test_rule_names_no_concrete_action(self) -> None:
        """举例会被模型当模板照抄——所以只许说「有关/无关」，不许出现具体动作词。"""
        rule = [r for r in _SENTENCE_RULES if "跟这句话本身有关" in r][0]
        for word in ("放下", "抬头", "手里的活", "手中的活", "揉", "笑", "叹气"):
            self.assertNotIn(word, rule, f"规则里出现了具体动作「{word}」，会被照抄")

    def test_rule_allows_no_action_at_all(self) -> None:
        rule = [r for r in _SENTENCE_RULES if "跟这句话本身有关" in r][0]
        self.assertIn("宁可不写", rule)


class WiderRepeatWindowTest(unittest.TestCase):
    def _recent(self, n: int, tag: str) -> list:
        # 第 1 条用掉这个动作，中间 n-1 条都是别的话
        return [f"({tag}) 这一条用了它"] + [f"第{i}条完全不同的话" for i in range(1, n)]

    def test_repeat_twelve_messages_ago_is_caught(self) -> None:
        ok, why = verify_message(
            "(放下手中的活) 我刚看到你说的那个事。", recent=self._recent(12, "放下手中的活")
        )
        self.assertFalse(ok, why)
        self.assertIn("动作标签复读", why)

    def test_repeat_nineteen_messages_ago_is_still_caught(self) -> None:
        ok, why = verify_message(
            "(放下手中的活) 我刚看到你说的那个事。", recent=self._recent(19, "放下手中的活")
        )
        self.assertFalse(ok, why)

    def test_repeat_beyond_the_window_is_allowed(self) -> None:
        ok, why = verify_message(
            "(放下手中的活) 我刚看到你说的那个事。", recent=self._recent(25, "放下手中的活")
        )
        self.assertTrue(ok, why)

    def test_a_different_action_is_fine(self) -> None:
        ok, why = verify_message(
            "(把手机扣在桌上) 你刚才那句我没听清。", recent=self._recent(12, "放下手中的活")
        )
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main()
