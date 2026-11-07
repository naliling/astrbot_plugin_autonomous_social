"""v2.1.4：social 三项新开关的配置层（config.py / _conf_schema.json / README）。

为什么单独测这一层：这三项全是**默认开、关掉即退回 2.1.3** 的闸。它们坏掉的方式
不会抛异常，而是安静地失效——面板里填了 `false` 却被 `bool("false")` 读成 True，
或者 schema 里少了键、README 里少了一行，用户以为关掉了其实没关。所以这里不测
行为（行为归 tests/test_v214_plan.py 等），只钉死三件事：

* 缺字段 / null / 认不出的值一律按 **True**（fail-open，不能因为配置残缺把新机制
  悄悄关掉，也不能因为配置残缺让插件起不来）；
* 面板里写下的 `"false"/"0"/"off"/"no"` 真的能关掉（字符串归一，老坑 P3-07）；
* schema、dataclass、README 三处默认值必须一致——它们由三份文件各写一遍，
  任何一处漏改都会让「面板显示的默认值」和「实际跑的默认值」对不上。

按仓库惯例自带最小 astrbot 桩（照抄 tests/test_v213_followup_review.py 头部）。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与 test_p2_threads.py 同款，幂等） ───
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

# v2.1.4 的三项：名字是外部契约（面板、README、调用方都按这个名字读）。
NEW_KEYS = (
    "planning_enabled",
    "max_output_tokens_enabled",
    "time_projection_enabled",
)


def _cfg(**overrides) -> SocialConfig:
    return SocialConfig.from_astrbot(dict(overrides))


def _readme_table() -> dict:
    """按 README「## 全部配置项」小节解析表格，键 → 默认值文本。

    正则与 tests/test_v12733_options.py:740 的同名函数一致，避免两份解析器
    对同一行给出不同结论。
    """
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


class DefaultsTest(unittest.TestCase):
    """默认全开：这是三项机制的唯一「出厂状态」。"""

    def test_constants_are_true(self):
        self.assertIs(config_mod.PLANNING_ENABLED_DEFAULT, True)
        self.assertIs(config_mod.MAX_OUTPUT_TOKENS_ENABLED_DEFAULT, True)
        self.assertIs(config_mod.TIME_PROJECTION_ENABLED_DEFAULT, True)

    def test_dataclass_defaults_are_true(self):
        # 直接构造（不经 from_astrbot）：字段默认值就是 dataclass 声明的那一份。
        cfg = SocialConfig()
        for key in NEW_KEYS:
            self.assertIs(getattr(cfg, key), True, key)

    def test_dataclass_fields_exist(self):
        fields = SocialConfig.__dataclass_fields__
        for key in NEW_KEYS:
            self.assertIn(key, fields, key)

    def test_fields_are_declared_as_bool(self):
        for key in NEW_KEYS:
            self.assertEqual(SocialConfig.__dataclass_fields__[key].type, "bool", key)


class MissingAndNullTest(unittest.TestCase):
    """配置里没有这一项（老用户的 config 就是没有）→ 必须按 True 起。"""

    def test_empty_config_yields_true(self):
        cfg = _cfg()
        for key in NEW_KEYS:
            self.assertIs(getattr(cfg, key), True, key)

    def test_missing_single_key_keeps_the_others_true(self):
        cfg = _cfg(planning_enabled=False)
        self.assertIs(cfg.planning_enabled, False)
        self.assertIs(cfg.max_output_tokens_enabled, True)
        self.assertIs(cfg.time_projection_enabled, True)

    def test_explicit_none_falls_back_to_true(self):
        cfg = _cfg(**{key: None for key in NEW_KEYS})
        for key in NEW_KEYS:
            self.assertIs(getattr(cfg, key), True, key)

    def test_empty_string_falls_back_to_true(self):
        cfg = _cfg(**{key: "" for key in NEW_KEYS})
        for key in NEW_KEYS:
            self.assertIs(getattr(cfg, key), True, key)

    def test_value_is_always_a_real_bool(self):
        # 下游写的是 `if cfg.planning_enabled:`，拿到 0/""/"false" 这类真值对象
        # 也不会崩，但会让人误判——这里钉死类型。
        cfg = _cfg(planning_enabled=0, max_output_tokens_enabled="0", time_projection_enabled="no")
        for key in NEW_KEYS:
            self.assertIsInstance(getattr(cfg, key), bool, key)


class StringNormalizationTest(unittest.TestCase):
    """面板/手写 config 里这些值真实存在，且必须是「关」。"""

    def test_false_words_turn_off(self):
        for raw in ("false", "FALSE", " false ", "0", "0.0", "no", "off", "n", "f"):
            for key in NEW_KEYS:
                with self.subTest(raw=raw, key=key):
                    self.assertIs(getattr(_cfg(**{key: raw}), key), False)

    def test_true_words_turn_on(self):
        for raw in ("true", "TRUE", " true ", "1", "1.0", "yes", "on", "y", "t"):
            for key in NEW_KEYS:
                with self.subTest(raw=raw, key=key):
                    self.assertIs(getattr(_cfg(**{key: raw}), key), True)

    def test_python_bools_and_numbers(self):
        self.assertIs(_cfg(planning_enabled=True).planning_enabled, True)
        self.assertIs(_cfg(planning_enabled=False).planning_enabled, False)
        self.assertIs(_cfg(planning_enabled=1).planning_enabled, True)
        self.assertIs(_cfg(planning_enabled=0).planning_enabled, False)


class FailOpenTest(unittest.TestCase):
    """认不出的值不许抛异常（配置构造抛穿 = 插件永久加载不了）。"""

    def test_garbage_string_does_not_raise(self):
        cfg = _cfg(**{key: "也许吧" for key in NEW_KEYS})
        for key in NEW_KEYS:
            self.assertIs(getattr(cfg, key), True, key)

    def test_garbage_object_does_not_raise(self):
        cfg = _cfg(**{key: object() for key in NEW_KEYS})
        for key in NEW_KEYS:
            self.assertIs(getattr(cfg, key), True, key)

    def test_list_value_does_not_raise(self):
        cfg = _cfg(**{key: [1, 2] for key in NEW_KEYS})
        for key in NEW_KEYS:
            self.assertIs(getattr(cfg, key), True, key)


class SchemaTest(unittest.TestCase):
    """schema 是面板与 _d() 的唯一真源，三项都要齐、类型要 bool、默认要 true。"""

    def test_keys_present(self):
        for key in NEW_KEYS:
            self.assertIn(key, SCHEMA, key)

    def test_type_is_bool_and_default_true(self):
        for key in NEW_KEYS:
            entry = SCHEMA[key]
            self.assertEqual(entry.get("type"), "bool", key)
            self.assertIs(entry.get("default"), True, key)

    def test_every_key_has_description_and_hint(self):
        for key in NEW_KEYS:
            entry = SCHEMA[key]
            self.assertTrue(str(entry.get("description", "")).strip(), key)
            self.assertTrue(str(entry.get("hint", "")).strip(), key)
            self.assertIn("obvious_hint", entry, key)

    def test_schema_default_drives_config_default(self):
        # _d() 以 schema 为准：schema 里写 true，硬编码兜底即使是 False 也读 true。
        for key in NEW_KEYS:
            self.assertIs(config_mod._d(key, False), True, key)

    def test_schema_defaults_match_dataclass(self):
        cfg = SocialConfig()
        for key in NEW_KEYS:
            self.assertEqual(getattr(cfg, key), SCHEMA[key]["default"], key)


class ReadmeTest(unittest.TestCase):
    """README 是用户唯一会读的那份；漏一行等于「这项不存在」。"""

    def test_rows_present_with_true(self):
        rows = _readme_table()
        for key in NEW_KEYS:
            self.assertIn(key, rows, key)
            self.assertEqual(rows[key], "true", key)

    def test_declared_count_matches_schema(self):
        m = re.search(r"共\s*(\d+)\s*项", README)
        self.assertIsNotNone(m, "README 里应有「共 N 项」")
        self.assertEqual(int(m.group(1)), len(SCHEMA))

    def test_table_lists_every_schema_key(self):
        rows = _readme_table()
        missing = [key for key in SCHEMA if key not in rows]
        self.assertEqual(missing, [])

    def test_table_has_no_unknown_key(self):
        rows = _readme_table()
        extra = [key for key in rows if key not in SCHEMA]
        self.assertEqual(extra, [])


class IndependenceTest(unittest.TestCase):
    """三项互相独立：关一项不能连带关掉另一项（否则排障时找不到是谁关的）。"""

    def test_each_can_be_disabled_alone(self):
        for key in NEW_KEYS:
            with self.subTest(key=key):
                cfg = _cfg(**{key: False})
                for other in NEW_KEYS:
                    expected = False if other == key else True
                    self.assertIs(getattr(cfg, other), expected, other)

    def test_all_can_be_disabled_together(self):
        cfg = _cfg(**{key: False for key in NEW_KEYS})
        for key in NEW_KEYS:
            self.assertIs(getattr(cfg, key), False, key)


if __name__ == "__main__":
    unittest.main()
