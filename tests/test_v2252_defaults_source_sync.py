# -*- coding: utf-8 -*-
"""v2.2.52：升级维护门禁——默认值单一真源 + 版本号三处一致。

送检报告 v2.2.51 第七节「方向二 / 方向六」的轻量落地：不改运行时代码
（`from_astrbot` 本来就 schema 优先），而是把「多处默认值漂移」变成**构建期
硬失败**。作者记录过「两份差 16 项，调好的节奏对新装用户一次都没生效」，
根因就是默认值散落多处却没有人逐项对拍。本测试钉住三件事：

1. schema default == dataclass 裸默认值（145 项逐项比，浮点容差 1e-9）；
2. 常量区里 *_DEFAULT 常量 == schema 对应 default（按显式映射表）；
3. 版本号三处一致：metadata.yaml / social/__init__.py / CHANGELOG 最新标题。

任何一处漂移，unittest discover 立刻 FAIL，发版前就能发现。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("defaults_source_sync_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social.config import SocialConfig, _SCHEMA_DEFAULTS  # noqa: E402

SCHEMA = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
DEFAULTS = {
    k: v["default"]
    for k, v in SCHEMA.items()
    if isinstance(v, dict) and "default" in v and v["default"] is not None
}

#: 常量区里「名字 != schema key」的默认值常量映射（只列真实存在的）。
#: 没列进来的常量是 min/max 范围或纯内部值，不属于「默认值真源」。
CONST_TO_SCHEMA = {
    "ACTIVITY_DEFAULT": "activity_level",
    "HOURLY_SENDS_CAP_DEFAULT": "hourly_sends_cap",
    "PROBE_AFTER_DEFAULT": "probe_after_minutes",
    "MAX_MSG_LEN_DEFAULT": "max_message_length",
    "MAX_SENDS_PER_ROUND_DEFAULT": "max_sends_per_round",
    "GREETING_ENABLED_DEFAULT": "greeting_enabled",
    "GLOBAL_PROACTIVE_GAP_DEFAULT": "global_proactive_gap_seconds",
    "GROUP_CROSS_BOT_GAP_DEFAULT": "group_cross_bot_gap_seconds",
}


def _eq(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-9
    return a == b


class DefaultsSourceSyncTest(unittest.TestCase):
    def test_dataclass_bare_defaults_match_schema(self):
        cfg = SocialConfig()
        drift = []
        for key, expected in DEFAULTS.items():
            got = getattr(cfg, key, "<missing>")
            if not _eq(got, expected):
                drift.append((key, expected, got))
        self.assertEqual(drift, [], f"dataclass 裸默认值与 schema 不一致：{drift}")

    def test_constants_match_schema(self):
        import social.config as cfgmod

        drift = []
        for const, key in CONST_TO_SCHEMA.items():
            if key not in DEFAULTS or not hasattr(cfgmod, const):
                continue
            got = getattr(cfgmod, const)
            expected = DEFAULTS[key]
            if not _eq(got, expected):
                drift.append((const, key, expected, got))
        self.assertEqual(drift, [], f"默认值常量与 schema 不一致：{drift}")

    def test_schema_module_cache_matches_file(self):
        drift = [
            (k, DEFAULTS.get(k), v)
            for k, v in _SCHEMA_DEFAULTS.items()
            if k in DEFAULTS and not _eq(v, DEFAULTS[k])
        ]
        self.assertEqual(drift, [], f"config 模块缓存的 schema 与文件不一致：{drift}")


class VersionTripleSyncTest(unittest.TestCase):
    @staticmethod
    def _versions():
        meta = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        m = re.search(r'^version:\s*"([^"]+)"', meta, re.M)
        meta_v = m.group(1) if m else ""

        init = (PLUGIN_ROOT / "social" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
        code_v = m.group(1) if m else ""

        changelog = (PLUGIN_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        m = re.search(r"^## v(\d+\.\d+\.\d+)\b", changelog, re.M)
        log_v = m.group(1) if m else ""
        return meta_v, code_v, log_v

    def test_version_three_places_match(self):
        meta_v, code_v, log_v = self._versions()
        self.assertTrue(meta_v, "metadata.yaml 没有 version")
        self.assertEqual(code_v, meta_v, "social/__init__.py 版本与 metadata 不一致")
        self.assertEqual(log_v, meta_v, "CHANGELOG 最新版本与 metadata 不一致")


if __name__ == "__main__":
    unittest.main()
