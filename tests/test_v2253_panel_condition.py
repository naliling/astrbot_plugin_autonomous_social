# -*- coding: utf-8 -*-
"""v2.2.53：开关门禁——11 个功能开关默认值必须为开，面板条件必须为 True。

坏开关的由来：v2.2.51 基线把这 11 个开关默认改成关，用户装上新版本后发现
追问、接话、回访、问候、群破冰等功能默认不工作；同时把它们子项的条件写成
False（关着才显示参数）。本门禁钉住两件事：
1. 11 个开关 default 必须 True；
2. 所有 condition 的值必须 True（AstrBot 前端: config[键] !== 期望值 → 隐藏）。
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE
SCHEMA = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

SWITCHES = (
    "followup_enabled", "continue_enabled", "loop_enabled", "closer_enabled",
    "special_care_auto", "group_icebreak_enabled", "group_topic_enabled",
    "group_interject_others", "allow_burst", "decorate_outgoing", "greeting_enabled",
)


def _placeholder_astrbot():
    if "astrbot" in sys.modules:
        return
    a = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    import logging
    api.logger = logging.getLogger("switch_gate_test")
    ev = types.ModuleType("astrbot.api.event")
    ev.MessageChain = object
    api.event = ev
    a.api = api
    sys.modules.update({"astrbot": a, "astrbot.api": api, "astrbot.api.event": ev})


class SwitchGateTest(unittest.TestCase):
    def test_switch_defaults_are_on(self):
        bad = [(k, SCHEMA[k].get("default")) for k in SWITCHES
               if SCHEMA[k].get("default") is not True]
        self.assertEqual(bad, [], f"功能开关默认关着（装上就不工作）：{bad}")

    def test_all_conditions_mean_parent_on(self):
        bad = []
        for k, spec in SCHEMA.items():
            if not isinstance(spec, dict):
                continue
            cond = spec.get("condition")
            if isinstance(cond, dict):
                for parent, expected in cond.items():
                    if expected is not True:
                        bad.append((k, parent, expected))
        self.assertEqual(bad, [], f"面板条件反转（关着才显示）：{bad}")

    def test_runtime_defaults_are_on(self):
        _placeholder_astrbot()
        if str(PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(PLUGIN_ROOT))
        from social.config import SocialConfig
        cfg = SocialConfig()
        empty = SocialConfig.from_astrbot({})
        bad = [k for k in SWITCHES
               if getattr(cfg, k, None) is not True or getattr(empty, k, None) is not True]
        self.assertEqual(bad, [], f"运行时开关默认关着：{bad}")


if __name__ == "__main__":
    unittest.main()
