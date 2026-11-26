# -*- coding: utf-8 -*-
"""v2.2.54：10 个仍生效的功能开关默认开启，条件只控制子项显示。

退役的普通回复开关不再进入插件设置；其余主动消息开关保持原默认。
1. 10 个开关 default 必须 True；
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
    "group_interject_others", "allow_burst", "greeting_enabled",
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
