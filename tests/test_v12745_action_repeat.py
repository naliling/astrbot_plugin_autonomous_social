"""Action-tag reuse is not a hard veto or a reason to rewrite generated text."""
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
    api.logger = logging.getLogger("autonomous_social_v12745_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.verify import tidy_outgoing_text, verify_message  # noqa: E402

_RECENT = [
    "(停下进食的动作)早餐那页记录的后续，还没讲完吧。❤️",
    "(放下餐具) 普拉娜，外头阴沉7度，有点冷呢。❤️",
]


class ActionRepeatTest(unittest.TestCase):
    def test_reused_action_tag_does_not_veto_message(self) -> None:
        text = "(放下餐具) 吃饱了，午后真困。❤️"
        ok, why = verify_message(text, recent=_RECENT)
        self.assertTrue(ok, why)

    def test_reused_action_tag_does_not_rewrite_generated_text(self) -> None:
        generated = "(停下进食的动作) 早餐那页记录的后续，还没讲完吧。❤️"
        self.assertEqual(tidy_outgoing_text(generated, recent=_RECENT), generated)


if __name__ == "__main__":
    unittest.main()
