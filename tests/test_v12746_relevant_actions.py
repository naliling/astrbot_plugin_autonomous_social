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


class HardBoundaryPromptTest(unittest.TestCase):
    def test_prompt_rules_keep_factuality_and_safety(self) -> None:
        joined = "".join(_SENTENCE_RULES)
        self.assertIn("Concrete claims require reliable context", joined)
        self.assertIn("Do not pressure the recipient", joined)


class NaturalActionTextTest(unittest.TestCase):
    def test_repeated_action_text_is_not_a_hard_veto(self) -> None:
        text = "(放下手中的活) 我刚看到你说的那个事。"
        ok, why = verify_message(text, recent=[text] * 20)
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main()
