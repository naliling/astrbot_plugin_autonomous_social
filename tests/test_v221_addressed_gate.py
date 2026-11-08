# -*- coding: utf-8 -*-
"""v2.2.1：群里这句话如果是在叫别人，就不是在叫她。

用户原话：「别人没叫他搞得好像别人叫他了一样」「要搭话不是这样搭话的」。

v1.28.0 起，`addressed_to` 判出来的「这句话在叫 X」只写成提示词里的一句
「最后一句像是说给 X 的」，要不要接交给模型判断。线上结果是：模型照样把它
当提问接过去，于是群里出现「没人叫她，她却像被叫了一样」的插话。

这里钉死新的硬闸：`speakers` 是从 samples 里剔掉她自己之后取的名字，所以
`addressed` 非空只可能是**别的群友**。这种情况下不接话，也不打模型。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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
    api.logger = logging.getLogger("autonomous_social_v221_addressed_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.clock import QUIET_CLOCK_LOCAL  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402

NOW = 1_800_000_000.0


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


def _run(coro):
    return asyncio.run(coro)


def _engine(now: float = NOW) -> SocialEngine:
    tmp = str(Path(tempfile.mkdtemp()) / "state.json")
    eng = SocialEngine(
        context=None,
        config=SocialConfig(),
        state_path=tmp,
        data_dir=str(Path(tmp).parent),
        time_source=_Clock(now),
    )
    eng.cfg.quiet_start = 0
    eng.cfg.quiet_end = 0
    eng.cfg.quiet_clock = QUIET_CLOCK_LOCAL
    eng.cfg.quiet_use_core_night = True
    eng.cfg.use_core_clock = True
    eng.cfg.special_care_ignore_quiet = False
    eng.cfg.group_flow_enabled = True
    eng._night_windows = {}
    eng._city_offset = {}
    return eng


def _group(eng: SocialEngine, samples):
    eng.running = True
    g = eng.state.group("bot1", "g1")
    g["umo"] = "aiocqhttp:GroupMessage:g1"
    g["samples"] = samples
    return g


class AddressedToSomeoneElseTest(unittest.TestCase):
    def test_calling_another_member_never_reaches_the_gate(self):
        """最后一句开头就叫了「小满」——那是在叫小满，不接，也不打模型。"""
        eng = _engine()
        _group(eng, [
            {"ts": NOW - 90.0, "name": "阿明", "text": "今天加班到九点"},
            {"ts": NOW - 40.0, "name": "小满", "text": "小满你先回去吧，我收拾完就走"},
        ])
        with patch(
            "social.engine.groupflow.flow_should_consider", return_value=(True, "")
        ) as gate:
            _run(eng._flow_reply_inner("bot1", "g1", NOW))
        gate.assert_not_called()

    def test_a_plain_message_still_reaches_the_gate(self):
        """没人被点名时照旧走闸门——这道硬闸不能顺手把正常接话也砍掉。"""
        eng = _engine()
        _group(eng, [
            {"ts": NOW - 90.0, "name": "阿明", "text": "今天加班到九点"},
            {"ts": NOW - 40.0, "name": "小满", "text": "那你也早点回去"},
        ])
        with patch(
            "social.engine.groupflow.flow_should_consider",
            return_value=(False, "群里刚安静下来，不硬接"),
        ) as gate:
            _run(eng._flow_reply_inner("bot1", "g1", NOW))
        self.assertTrue(gate.called)


if __name__ == "__main__":
    unittest.main()
