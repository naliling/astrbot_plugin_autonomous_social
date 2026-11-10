"""v2.1.5：draft_review_enabled / memory_tools_enabled 落进配置层与面板。

为什么单独钉这两个开关：它们和 v2.1.4 那三项坏得不一样。那三项是「默认开、坏了
就退回老路」；这两项原本**根本没进配置**——engine.py 早就用
`getattr(self.cfg, "draft_review_enabled", False)` 读着，但 config.py 里没有这个
字段、schema 里没有这个键，于是 getattr 每次都拿到兜底 False，「生成后自评」和
「先查一次记忆再开口」两条路永远走不到，属于静默死代码。

所以这里钉四件事：

* 默认值与 engine.py 的 getattr 兜底**故意不同**（draft 默认开、memory 默认关），
  以 schema 为准——engine 那边的 False 只是「配置层没写」时的兜底，不是产品默认；
* 面板里填的 `"false"/"0"/"off"` 真的能关掉（gb 归一，老坑 P3-07）；
* schema 键数、README 行、README 计数三处与 dataclass 一致；
* engine.py 读的属性名与 dataclass 字段名对得上（这条是防死代码复发的唯一断言）。

按仓库惯例自带最小 astrbot 桩（照抄 tests/test_v214_config.py 头部）。
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
NEW_KEYS = ("draft_review_enabled", "memory_tools_enabled")
EXPECTED_DEFAULTS = {"draft_review_enabled": True, "memory_tools_enabled": False}
# 从 engine.py 里读的属性名 → 期望的 dataclass 字段。两者必须一致，否则又是死代码。
ENGINE_ATTRS = {
    "draft_review_enabled": "engine.py:8650 的 bool(getattr(self.cfg, \"draft_review_enabled\", False))",
    "memory_tools_enabled": "engine.py:8595 的 bool(getattr(self.cfg, \"memory_tools_enabled\", False))",
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
    """出厂状态：自评开、记忆工具关——与 engine.py 的 getattr 兜底故意不同。"""

    def test_constants(self):
        self.assertIs(config_mod.DRAFT_REVIEW_ENABLED_DEFAULT, True)
        self.assertIs(config_mod.MEMORY_TOOLS_ENABLED_DEFAULT, False)

    def test_dataclass_defaults(self):
        c = SocialConfig()
        self.assertIs(c.draft_review_enabled, True)
        self.assertIs(c.memory_tools_enabled, False)

    def test_from_astrbot_empty_dict_uses_defaults(self):
        c = _cfg()
        self.assertIs(c.draft_review_enabled, True)
        self.assertIs(c.memory_tools_enabled, False)

    def test_fields_are_bool_typed(self):
        # 类型标注写成 bool：面板与 dataclass 都靠它，写成 str 会让 gb 的返回值
        # 在别处被当成字符串真值（"false" 也是 True）。
        for key in NEW_KEYS:
            ann = SocialConfig.__dataclass_fields__[key].type
            self.assertIn(ann, (bool, "bool"), f"{key} 的标注不是 bool：{ann!r}")


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
            self.assertIs(node.get("obvious_hint"), True, key)

    def test_schema_default_matches_dataclass(self):
        for key in NEW_KEYS:
            self.assertIs(SCHEMA[key]["default"], getattr(SocialConfig(), key), key)

    def test_schema_key_count_is_132(self):
        # v2.2.7：新增 decorate_outgoing（出口改写），131 → 132。
        self.assertEqual(len(SCHEMA), 132)

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

    def test_false_text_turns_draft_review_off(self):
        for raw in self.FALSE_TEXT:
            with self.subTest(raw=raw):
                self.assertIs(_cfg(draft_review_enabled=raw).draft_review_enabled, False)

    def test_true_text_turns_memory_tools_on(self):
        for raw in self.TRUE_TEXT:
            with self.subTest(raw=raw):
                self.assertIs(_cfg(memory_tools_enabled=raw).memory_tools_enabled, True)

    def test_true_text_keeps_draft_review_on(self):
        for raw in self.TRUE_TEXT:
            with self.subTest(raw=raw):
                self.assertIs(_cfg(draft_review_enabled=raw).draft_review_enabled, True)

    def test_false_text_keeps_memory_tools_off(self):
        for raw in self.FALSE_TEXT:
            with self.subTest(raw=raw):
                self.assertIs(_cfg(memory_tools_enabled=raw).memory_tools_enabled, False)

    def test_missing_and_null_fall_back_to_schema_default(self):
        for key in NEW_KEYS:
            with self.subTest(key=key):
                self.assertIs(getattr(_cfg(), key), EXPECTED_DEFAULTS[key])
                self.assertIs(
                    getattr(_cfg(**{key: None}), key), EXPECTED_DEFAULTS[key]
                )

    def test_empty_string_falls_back_to_default(self):
        self.assertIs(_cfg(draft_review_enabled="").draft_review_enabled, True)
        self.assertIs(_cfg(memory_tools_enabled="").memory_tools_enabled, False)

    def test_unrecognized_value_never_raises(self):
        for raw in ("随便", "maybe", object(), [1], {"a": 1}):
            with self.subTest(raw=raw):
                c = _cfg(draft_review_enabled=raw, memory_tools_enabled=raw)
                self.assertIs(c.draft_review_enabled, True)
                self.assertIs(c.memory_tools_enabled, False)

    def test_native_bools_and_numbers(self):
        self.assertIs(_cfg(draft_review_enabled=False).draft_review_enabled, False)
        self.assertIs(_cfg(memory_tools_enabled=True).memory_tools_enabled, True)
        self.assertIs(_cfg(draft_review_enabled=0).draft_review_enabled, False)
        self.assertIs(_cfg(memory_tools_enabled=1).memory_tools_enabled, True)

    def test_two_switches_are_independent(self):
        c = _cfg(draft_review_enabled=False, memory_tools_enabled=True)
        self.assertIs(c.draft_review_enabled, False)
        self.assertIs(c.memory_tools_enabled, True)
        # 其余 v2.1.4 三项不受影响
        self.assertIs(c.planning_enabled, True)
        self.assertIs(c.time_projection_enabled, True)


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
        # v2.2.7：新增 decorate_outgoing，README 的「共 N 项」跟着 131 → 132。
        self.assertEqual(_readme_count(), 132)


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
