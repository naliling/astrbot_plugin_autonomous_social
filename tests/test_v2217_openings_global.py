"""ASO group-context isolation boundary.

Own proactive text from one group is not reused to shape another group's output.
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v2217_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from social.state import SocialState  # noqa: E402


class AudienceIsolationTest(unittest.TestCase):
    """群聊上下文不混入其他群的已发送正文。"""

    def test_cross_group_reader_is_not_wired(self):
        src = (ROOT / "social" / "engine.py").read_text(encoding="utf-8")
        self.assertNotIn("own_group_openings", src)
        self.assertNotIn("_x_openings", src)
        self.assertFalse(hasattr(SocialState, "own_group_openings"))


if __name__ == "__main__":
    unittest.main()
