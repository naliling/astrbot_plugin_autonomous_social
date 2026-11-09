"""v2.2.4：主动消息只对「真人平台」开口。

两件事：

1. **WebChat 禁掉**。它是 AstrBot 面板里的调试会话，umo 形如
   `webchat!naliling!<会话UUID>`：用户本来就在面板里敲字，插件主动去敲他
   没有意义；更糟的是每开一个新会话 UUID 就变，以前会被当成一个新「用户」
   反复建档（线上 state 里真的长出了一个 `webchat` 的 bot 键）。
2. **微信不再声明支持**。metadata 的 `support_platforms` 里删掉 `weixin_oc`：
   这个插件只做 QQ / NapCat 与 AstrBot 平台。
"""
from __future__ import annotations

import asyncio
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


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v224_platform_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

import social.engine as engine_mod  # noqa: E402
from social.engine import (  # noqa: E402
    PROACTIVE_SKIP_PLATFORMS,
    SocialEngine,
    _platform_skipped,
    _umo_platform,
)


class UmoPlatformTest(unittest.TestCase):
    def test_aiocqhttp_umo(self) -> None:
        self.assertEqual(_umo_platform("aiocqhttp:private:12345"), "aiocqhttp")
        self.assertEqual(_umo_platform("aiocqhttp:group:67890"), "aiocqhttp")

    def test_webchat_umo_uses_bang_separator(self) -> None:
        """WebChat 的 umo 不是 `platform:type:id`，而是 `webchat!<用户名>!<uuid>`。"""
        self.assertEqual(_umo_platform("webchat!naliling!0f8c2e10-aaaa"), "webchat")
        self.assertEqual(_umo_platform("WebChat!naliling!x"), "webchat")

    def test_empty_and_garbage(self) -> None:
        self.assertEqual(_umo_platform(""), "")
        self.assertEqual(_umo_platform(None), "")
        self.assertFalse(_platform_skipped(""))


class PlatformSkipTest(unittest.TestCase):
    def test_webchat_is_skipped(self) -> None:
        self.assertIn("webchat", PROACTIVE_SKIP_PLATFORMS)
        self.assertTrue(_platform_skipped("webchat!naliling!uuid"))
        self.assertTrue(_platform_skipped("webchat:private:1"))

    def test_real_platforms_are_not_skipped(self) -> None:
        self.assertFalse(_platform_skipped("aiocqhttp:private:12345"))
        self.assertFalse(_platform_skipped("qq_official:private:abc"))


class ObserveIgnoresWebchatTest(unittest.TestCase):
    """`observe` 必须在碰任何账本之前就返回——否则又会给 WebChat 建档。"""

    class _BoomEngine:
        """除 cfg 外任何属性访问都炸：能证明 observe 早退了。"""

        def __init__(self) -> None:
            self.cfg = types.SimpleNamespace(enabled=True)

        def __getattr__(self, name):  # pragma: no cover - 只有早退失败才会走到
            raise AssertionError(f"WebChat 的消息不该走到 self.{name}")

    def test_observe_returns_before_touching_state(self) -> None:
        original = engine_mod._looks_like_command
        engine_mod._looks_like_command = lambda *a, **k: False
        try:
            event = types.SimpleNamespace(
                message_str="在吗",
                unified_msg_origin="webchat!naliling!uuid-1234",
            )
            asyncio.run(SocialEngine.observe(self._BoomEngine(), event))
        finally:
            engine_mod._looks_like_command = original

    def test_observe_still_processes_qq(self) -> None:
        """反向确认：QQ 的私聊不会被这道闸误伤（会继续往下走、炸在别处）。"""
        original = engine_mod._looks_like_command
        engine_mod._looks_like_command = lambda *a, **k: False
        try:
            event = types.SimpleNamespace(
                message_str="在吗",
                unified_msg_origin="aiocqhttp:private:12345",
            )
            with self.assertRaises(AssertionError):
                asyncio.run(SocialEngine.observe(self._BoomEngine(), event))
        finally:
            engine_mod._looks_like_command = original


class MetadataPlatformsTest(unittest.TestCase):
    """微信支持按用户要求删掉：只留 QQ 与 AstrBot 平台。"""

    def _platforms(self):
        text = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        out, in_block = [], False
        for line in text.splitlines():
            if line.strip().startswith("support_platforms:"):
                in_block = True
                continue
            if in_block:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("- "):
                    out.append(stripped[2:].strip())
                else:
                    break
        return out

    def test_weixin_is_not_declared(self) -> None:
        platforms = self._platforms()
        self.assertNotIn("weixin_oc", platforms, f"微信支持已按要求删除：{platforms}")
        self.assertNotIn("weixin_official_account", platforms)
        self.assertNotIn("wechat", platforms)

    def test_qq_and_astrbot_remain(self) -> None:
        platforms = self._platforms()
        self.assertIn("aiocqhttp", platforms)
        self.assertIn("qq_official", platforms)


if __name__ == "__main__":
    unittest.main()
