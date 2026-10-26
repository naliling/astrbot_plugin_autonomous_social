"""v1.28.11：`purge_inactive_user_days` 进面板。

背景：它有字段（social/config.py）、有读取（from_astrbot）、引擎
`_prune_expired` 里也真的会永久删除整条会话记录（0=关），但
`_conf_schema.json` 里没有它 —— 面板上根本看不到、改不了，只能手改 JSON。
「开箱即用」= 面板上的每一项都真的能用、真的能配。

它和 `user_retention_days` 是两件事：后者只清聊天正文，前者删整条记录，
所以默认必须关（0），hint 里写明后果。
"""

from __future__ import annotations

import inspect
import json
import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = (
    _HERE.parent
    if (_HERE.parent / "_conf_schema.json").is_file()
    else _HERE.parent / "astrbot_plugin_autonomous_social"
)
README_PATH = PLUGIN_ROOT / "README.md"
SCHEMA = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_purge_config_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402


class PurgeSchemaTest(unittest.TestCase):
    """面板上必须真有这一项。"""

    def test_key_is_in_the_panel(self):
        self.assertIn("purge_inactive_user_days", SCHEMA)

    def test_default_is_off(self):
        self.assertEqual(SCHEMA["purge_inactive_user_days"]["default"], 0)

    def test_bounds_cover_ten_years(self):
        meta = SCHEMA["purge_inactive_user_days"]
        self.assertEqual(meta["min"], 0)
        self.assertEqual(meta["max"], 3650)

    def test_hint_warns_it_deletes_whole_records(self):
        hint = SCHEMA["purge_inactive_user_days"]["hint"]
        self.assertIn("整条记录", hint)
        self.assertIn("0", hint)

    def test_readme_lists_it_with_zero_default(self):
        text = README_PATH.read_text(encoding="utf-8")
        self.assertIn("| `purge_inactive_user_days` | `0` |", text)


class PurgeConfigTest(unittest.TestCase):
    """配置读取与钳制。"""

    def test_default_is_zero(self):
        self.assertEqual(SocialConfig.from_astrbot({}).purge_inactive_user_days, 0)

    def test_negative_falls_back_to_zero(self):
        cfg = SocialConfig.from_astrbot({"purge_inactive_user_days": -5})
        self.assertEqual(cfg.purge_inactive_user_days, 0)

    def test_huge_is_clamped(self):
        cfg = SocialConfig.from_astrbot({"purge_inactive_user_days": 99999})
        self.assertEqual(cfg.purge_inactive_user_days, 3650)

    def test_custom_value_is_kept(self):
        cfg = SocialConfig.from_astrbot({"purge_inactive_user_days": 30})
        self.assertEqual(cfg.purge_inactive_user_days, 30)


class PurgeEngineWiringTest(unittest.TestCase):
    """引擎那侧真的读这个选项（不是只读进配置对象就完了）。"""

    def test_prune_reads_the_option(self):
        src = inspect.getsource(SocialEngine._prune_expired)
        self.assertIn("purge_inactive_user_days", src)


if __name__ == "__main__":
    unittest.main()
