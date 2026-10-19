"""v1.27.50：长期不回就永久删除（私聊 + 群）。"""
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
    api.logger = logging.getLogger("autonomous_social_purge_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.engine import _silent_too_long  # noqa: E402

DAY = 86400.0
NOW = 1_800_000_000.0


class SilentTooLongTest(unittest.TestCase):
    def test_long_silence_without_reply_is_purged(self) -> None:
        rec = {"last_seen": NOW - 40 * DAY, "last_said": NOW - 39 * DAY}
        self.assertTrue(_silent_too_long(rec, NOW, 30))

    def test_recent_contact_is_kept(self) -> None:
        rec = {"last_seen": NOW - 2 * DAY, "last_said": NOW - 1 * DAY}
        self.assertFalse(_silent_too_long(rec, NOW, 30))

    def test_they_replied_recently_is_kept(self) -> None:
        rec = {"last_seen": NOW - 60 * DAY, "last_said": NOW - 59 * DAY, "replied_at": NOW - 1 * DAY}
        self.assertFalse(_silent_too_long(rec, NOW, 30))

    def test_zero_days_means_off(self) -> None:
        rec = {"last_seen": NOW - 999 * DAY, "last_said": NOW - 999 * DAY}
        self.assertFalse(_silent_too_long(rec, NOW, 0))

    def test_never_talked_is_not_purged(self) -> None:
        # 从没说过话的新条目没有时间锚点，不该被当成「很久不回」
        self.assertFalse(_silent_too_long({}, NOW, 30))

    def test_boundary_just_under_days_is_kept(self) -> None:
        rec = {"last_seen": NOW - 29.9 * DAY}
        self.assertFalse(_silent_too_long(rec, NOW, 30))


if __name__ == "__main__":
    unittest.main()
