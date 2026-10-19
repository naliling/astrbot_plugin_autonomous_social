"""v1.27.48：配置入口不许把实例静默换成默认值。

根因：`from_astrbot` 里每个字段都是 `c.get(key, 默认值)`，而 `SocialConfig` 实例
没有 `.get()`——于是整份配置被悄悄替换成默认值，面板改什么都不生效，还不报错。
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
    api.logger = logging.getLogger("autonomous_social_v12748_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.config import SocialConfig  # noqa: E402


class ConfigPassthroughTest(unittest.TestCase):
    def test_instance_is_returned_as_is(self) -> None:
        cfg = SocialConfig()
        cfg.context_inject_count = 4
        got = SocialConfig.from_astrbot(cfg)
        self.assertIs(got, cfg)
        self.assertEqual(got.context_inject_count, 4)

    def test_dict_is_still_parsed(self) -> None:
        got = SocialConfig.from_astrbot({"context_inject_count": 6})
        self.assertEqual(got.context_inject_count, 6)

    def test_none_falls_back_to_defaults(self) -> None:
        got = SocialConfig.from_astrbot(None)
        self.assertEqual(got.context_inject_count, SocialConfig().context_inject_count)

    def test_engine_does_not_lose_the_setting(self) -> None:
        # 引擎内部会再调一次 from_astrbot；这条钉住「调完之后设置还在」
        cfg = SocialConfig()
        cfg.context_inject_count = 4
        again = SocialConfig.from_astrbot(SocialConfig.from_astrbot(cfg))
        self.assertEqual(again.context_inject_count, 4)


if __name__ == "__main__":
    unittest.main()
