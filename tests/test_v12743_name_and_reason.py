"""v1.27.43：主动消息里别再念全名；发送失败要留得下原因。

两条都来自线上实测：

* `(休息) 忙着吃饭，爱筝挽起·冲击波冠军，想你❤️` —— 真人不会把群名片一个字不落地喊出来；
* 账本里 44 个用户 `send_fail_count > 0`，而 `send_blocked_reason` 永远是空的 ——
  那个字段**从来没有被写过**（只有清理时置空），状态页因此查不到「他为什么不发消息」。
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v12743_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.engine import SocialEngine  # noqa: E402
from social.generator import _bond_line  # noqa: E402
from social.state import SocialState  # noqa: E402
from social.textutil import short_call_name  # noqa: E402


class ShortCallNameTest(unittest.TestCase):
    def test_short_names_untouched(self) -> None:
        for name in ("小明", "阿澈", "游鱼", "无名又无言"):
            self.assertEqual(short_call_name(name), name)

    def test_long_chinese_uses_first_segment(self) -> None:
        self.assertEqual(short_call_name("爱筝挽起·冲击波冠军"), "爱筝挽起")

    def test_long_chinese_without_separator_is_cut(self) -> None:
        self.assertEqual(short_call_name("超级无敌大风车"), "超级无敌")

    def test_latin_name_is_never_cut(self) -> None:
        for name in ("Regnator", "hanhandouya", "Luture"):
            self.assertEqual(short_call_name(name), name)

    def test_empty(self) -> None:
        self.assertEqual(short_call_name(None), "")
        self.assertEqual(short_call_name(""), "")


class BondLineUsesShortNameTest(unittest.TestCase):
    """prompt 里那句「你一直叫TA「…」」是模型照抄的来源。"""

    def test_full_name_is_not_handed_to_the_model(self) -> None:
        text = _bond_line({"_nickname": "爱筝挽起·冲击波冠军"})
        self.assertIn("爱筝挽起", text)
        self.assertNotIn("冲击波冠军", text)

    def test_short_name_passes_through(self) -> None:
        self.assertIn("「小明」", _bond_line({"_nickname": "小明"}))


class _Stub:
    def __init__(self):
        self.state = SocialState(os.path.join(tempfile.mkdtemp(), "state.json"))
        self._now = 1_000.0

    def _time(self):
        return self._now


class SendBlockedReasonTest(unittest.TestCase):
    def test_failure_reason_is_recorded(self) -> None:
        stub = _Stub()
        u: dict = {}
        SocialEngine._mark_send_failed(
            stub, u, platform_side=False, reason="未找到匹配的会话平台"
        )
        self.assertIn("未找到匹配的会话平台", u["send_blocked_reason"])

    def test_platform_side_failure_is_marked_as_such(self) -> None:
        stub = _Stub()
        u: dict = {}
        SocialEngine._mark_send_failed(stub, u, platform_side=True, reason="adapter 没加载")
        self.assertTrue(u["send_blocked_reason"].startswith("平台侧"))
        self.assertNotIn("send_fail_count", u)

    def test_empty_reason_does_not_wipe_an_old_one(self) -> None:
        stub = _Stub()
        u: dict = {"send_blocked_reason": "上次的原因"}
        SocialEngine._mark_send_failed(stub, u, platform_side=False, reason="")
        self.assertEqual(u["send_blocked_reason"], "上次的原因")


if __name__ == "__main__":
    unittest.main()
