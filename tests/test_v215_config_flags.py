"""ASO configuration contracts for active settings.

Memory tools remain configurable; output review and cross-recipient filters are retired.
The same-user anchor cooldown supports legacy-key migration.






* Memory settings retain their schema/dataclass/default synchronization checks.

* String-valued boolean settings normalize without silently enabling features.
* The schema property count and README count match the current dataclass.



"""

from __future__ import annotations

import json
import logging
import re
import sys
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与 test_v214_config.py 同款，幂等） ───
_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


class _FakeMessageChain:
    def message(self, text):
        return ("chain", text)


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _FakeMessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import config as config_mod  # noqa: E402
from social.config import SocialConfig  # noqa: E402

SCHEMA = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
README = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

# v2.1.5 的两项：名字是外部契约（engine.py 的 getattr、面板、README 都按这个名字读）。
NEW_KEYS = ("memory_tools_enabled",)
EXPECTED_DEFAULTS = {"memory_tools_enabled": False}
# 从 engine.py 里读的属性名 → 期望的 dataclass 字段。两者必须一致，否则又是死代码。
ENGINE_ATTRS = {
    "memory_tools_enabled": "engine.py 的 memory-tools 配置读取",
}


def _cfg(**overrides) -> SocialConfig:
    return SocialConfig.from_astrbot(dict(overrides))


def _readme_table() -> dict:
    """按 README「## 全部配置项」小节解析表格，键 → 默认值文本。"""
    start = README.find("## 全部配置项")
    if start < 0:
        return {}
    section = README[start:]
    nxt = section.find("\n## ", 3)
    if nxt >= 0:
        section = section[:nxt]
    rows = {}
    for line in section.splitlines():
        m = re.match(r"^\|\s*`([a-z0-9_]+)`\s*\|\s*`?([^|`]*)`?\s*\|", line)
        if m:
            rows[m.group(1)] = m.group(2).strip()
    return rows


def _readme_count() -> int:
    m = re.search(r"共\s*(\d+)\s*项", README)
    return int(m.group(1)) if m else -1


class DefaultsTest(unittest.TestCase):
    def test_memory_tools_default_off(self):
        self.assertIs(config_mod.MEMORY_TOOLS_ENABLED_DEFAULT, False)
        self.assertIs(SocialConfig().memory_tools_enabled, False)
        self.assertIs(_cfg().memory_tools_enabled, False)

    def test_memory_tools_field_is_bool_typed(self):
        ann = SocialConfig.__dataclass_fields__["memory_tools_enabled"].type
        self.assertIn(ann, (bool, "bool"), f"memory_tools_enabled 的标注不是 bool：{ann!r}")


class SchemaContractTest(unittest.TestCase):
    """schema 是面板与 _d() 的唯一事实来源。"""

    def test_keys_exist(self):
        for key in NEW_KEYS:
            self.assertIn(key, SCHEMA)

    def test_keys_are_bool_with_hint_and_default(self):
        for key in NEW_KEYS:
            node = SCHEMA[key]
            self.assertEqual(node.get("type"), "bool", key)
            self.assertIs(node.get("default"), EXPECTED_DEFAULTS[key], key)
            for field in ("description", "hint"):
                self.assertTrue(str(node.get(field) or "").strip(), f"{key}.{field} 为空")
            self.assertNotIn("obvious_hint", node, key)

    def test_schema_default_matches_dataclass(self):
        for key in NEW_KEYS:
            self.assertIs(SCHEMA[key]["default"], getattr(SocialConfig(), key), key)

    def test_schema_key_count_is_136(self):
        # v2.2.54 also retires the unused cross-channel dedup option.
        self.assertEqual(len(SCHEMA), 136)

    def test_schema_json_has_no_duplicate_keys(self):
        # json.loads 会静默保留后一个同名键；重复键会让面板只显示一个、_d() 读到另一个。
        text = (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        seen = re.findall(r'^\s{4}"([a-z0-9_]+)"\s*:\s*\{', text, re.MULTILINE)
        dupes = sorted({k for k in seen if seen.count(k) > 1})
        self.assertEqual(dupes, [], f"schema 里有重复键：{dupes}")


class StringNormalizationTest(unittest.TestCase):
    """面板/手写 JSON 里 "false"/"0"/"off" 必须真能关掉（P3-07 老坑）。"""

    FALSE_TEXT = ("false", "0", "off", "no", "n", "f", "FALSE", " False ", "Off")
    TRUE_TEXT = ("true", "1", "on", "yes", "y", "t", "TRUE", " True ")

    def test_true_text_turns_memory_tools_on(self):
        for raw in self.TRUE_TEXT:
            with self.subTest(raw=raw):
                self.assertIs(_cfg(memory_tools_enabled=raw).memory_tools_enabled, True)

    def test_false_text_keeps_memory_tools_off(self):
        for raw in self.FALSE_TEXT:
            with self.subTest(raw=raw):
                self.assertIs(_cfg(memory_tools_enabled=raw).memory_tools_enabled, False)

    def test_missing_null_and_empty_fall_back_to_default(self):
        for value in (None, ""):
            self.assertIs(_cfg(memory_tools_enabled=value).memory_tools_enabled, False)
        self.assertIs(_cfg().memory_tools_enabled, False)

    def test_unrecognized_value_never_raises(self):
        for raw in ("随便", "maybe", object(), [1], {"a": 1}):
            with self.subTest(raw=raw):
                self.assertIs(_cfg(memory_tools_enabled=raw).memory_tools_enabled, False)

    def test_native_bools_and_numbers(self):
        self.assertIs(_cfg(memory_tools_enabled=True).memory_tools_enabled, True)
        self.assertIs(_cfg(memory_tools_enabled=1).memory_tools_enabled, True)
        self.assertIs(_cfg(memory_tools_enabled=0).memory_tools_enabled, False)

    def test_memory_setting_does_not_change_other_defaults(self):
        c = _cfg(memory_tools_enabled=True)
        self.assertIs(c.memory_tools_enabled, True)
        self.assertIs(c.planning_enabled, True)
        self.assertIs(c.time_projection_enabled, True)


class AnchorCooldownConfigTest(unittest.TestCase):
    def test_schema_readme_and_dataclass_default_match(self):
        self.assertEqual(SCHEMA["anchor_repeat_cooldown_hours"]["default"], 0.5)
        self.assertEqual(SocialConfig().anchor_repeat_cooldown_hours, 0.5)
        self.assertEqual(_readme_table()["anchor_repeat_cooldown_hours"], "0.5")

    def test_legacy_key_migrates_when_new_key_is_missing_or_default(self):
        self.assertEqual(_cfg(cross_user_repeat_hours=2).anchor_repeat_cooldown_hours, 2.0)
        self.assertEqual(_cfg(cross_user_repeat_hours=2, anchor_repeat_cooldown_hours=0.5).anchor_repeat_cooldown_hours, 2.0)

    def test_explicit_new_value_and_zero_are_respected(self):
        self.assertEqual(_cfg(cross_user_repeat_hours=2, anchor_repeat_cooldown_hours=3).anchor_repeat_cooldown_hours, 3.0)
        self.assertEqual(_cfg(anchor_repeat_cooldown_hours=0).anchor_repeat_cooldown_hours, 0.0)

    def test_retired_settings_are_absent(self):
        for key in ("draft_review_enabled", "cross_user_repeat_ratio", "cross_user_calibrate"):
            self.assertNotIn(key, SCHEMA)


class ReadmeTest(unittest.TestCase):
    """README 是用户唯一会读的那份默认值说明。"""

    def test_both_rows_present(self):
        rows = _readme_table()
        for key in NEW_KEYS:
            self.assertIn(key, rows, f"README 配置表缺 {key}")

    def test_row_defaults_match_schema(self):
        rows = _readme_table()
        for key in NEW_KEYS:
            expected = "true" if EXPECTED_DEFAULTS[key] else "false"
            self.assertEqual(rows.get(key), expected, key)

    def test_count_line_matches_schema(self):
        self.assertEqual(_readme_count(), len(SCHEMA))
        # The README schema count stays synchronized with the 136 active properties.
        self.assertEqual(_readme_count(), 136)


class EngineWiringTest(unittest.TestCase):
    """死代码复发的唯一断言：engine.py 读的属性名必须真的是 dataclass 字段。

    只读 engine.py 源码（不改它）：属性名一旦被重命名或从 config 里被删掉，
    getattr 又会静默回落到 False，功能重新变成死代码。
    """

    ENGINE = PLUGIN_ROOT / "social" / "engine.py"

    def test_engine_reads_these_attrs(self):
        if not self.ENGINE.is_file():
            self.skipTest("engine.py 不存在")
        src = self.ENGINE.read_text(encoding="utf-8", errors="replace")
        for key, where in ENGINE_ATTRS.items():
            with self.subTest(key=key):
                self.assertIn(f'getattr(self.cfg, "{key}"', src, f"{key} 不在 {where}")

    def test_engine_attrs_are_dataclass_fields(self):
        fields = SocialConfig.__dataclass_fields__
        for key in ENGINE_ATTRS:
            with self.subTest(key=key):
                self.assertIn(key, fields, f"{key} 是 engine 读的，但 config 里没这个字段")


if __name__ == "__main__":
    unittest.main(verbosity=2)
