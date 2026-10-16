"""坏配置值不许把插件打死：`inf` / `1e999` / 超大整数。

为什么单独立一个文件：这类值不是「读不出来」，而是**读得出来但没意义**——
`float('inf')`、`float('1e999')` 都成功返回 inf，于是它躲过所有 `except (TypeError,
ValueError)`，一路走到下游的 `int(...)`，而 `int(float('inf'))` 抛的是
**OverflowError**。那个异常会抛穿 `from_astrbot` → `SocialEngine.__init__` →
插件永久加载不了，用户在面板里也改不回来。

`state.json` 是同一个病的另一个入口：里面的脏时间戳同样会走到比较与 `int()`。
"""

from __future__ import annotations

import logging
import math
import sys
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
    api.logger = logging.getLogger("autonomous_social_badconfig_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.config import SocialConfig, _safe_float, _safe_int  # noqa: E402
from social.state import _num, _safe_ts  # noqa: E402

#: 一律会被 `_finite_float` 判为「没意义」的写法。`inf` 与 `1e999` 是**成功解析**
#: 出来的无穷大，`10**400` 是转 float 就溢出的超大整数，`nan` 是 NaN。
POISON = ("inf", "-inf", "Infinity", "1e999", "-1e999", float("inf"),
          float("-inf"), float("nan"), 10 ** 400, float(10 ** 400) if False else None)
POISON = tuple(v for v in POISON if v is not None)


class SafeIntTest(unittest.TestCase):
    def test_poisoned_values_fall_back_instead_of_raising(self):
        for bad in POISON:
            with self.subTest(bad=bad):
                got = _safe_int(bad, 60, "min_gap_minutes")
                self.assertEqual(got, 60, f"{bad!r} 没有退回默认值")
                self.assertIsInstance(got, int)

    def test_a_poisoned_default_does_not_raise_either(self):
        self.assertEqual(_safe_int("inf", "inf"), 0)
        self.assertEqual(_safe_int("inf", None), 0)

    def test_normal_writing_still_works(self):
        self.assertEqual(_safe_int("2.5", 60), 2)
        self.assertEqual(_safe_int("1e3", 60), 1000)
        self.assertEqual(_safe_int(7, 60), 7)
        self.assertEqual(_safe_int(True, 60), 1)


class SafeFloatTest(unittest.TestCase):
    def test_infinity_is_not_a_valid_float(self):
        # 关键：`float('inf')` **不抛异常**，所以旧写法会把它原样放出去。
        for bad in POISON:
            with self.subTest(bad=bad):
                got = _safe_float(bad, 0.5, "some_ratio")
                self.assertTrue(math.isfinite(got), f"{bad!r} 变成了 {got!r}")
                self.assertEqual(got, 0.5)

    def test_normal_writing_still_works(self):
        self.assertEqual(_safe_float("1.5", 0.5), 1.5)
        self.assertEqual(_safe_float("1e-3", 0.5), 0.001)
        self.assertEqual(_safe_float(0, 0.5), 0.0)


class WholeConfigSurvivesPoisonTest(unittest.TestCase):
    """把每一个 int/float 配置项都填成 `inf`，配置构造必须照样成功。"""

    def test_from_astrbot_does_not_raise(self):
        import json

        schema_path = PLUGIN_ROOT / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        raw = {}
        for key, spec in schema.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("type") in ("int", "float"):
                raw[key] = "inf"
        self.assertTrue(raw, "一个数值配置项都没找到，测试本身失效了")
        cfg = SocialConfig.from_astrbot(raw)
        self.assertIsInstance(cfg, SocialConfig)
        # 抽查几个下游真的会 int() 的字段：必须是有限值。
        for field in ("min_gap_minutes", "max_message_length", "heartbeat_max_minutes"):
            value = getattr(cfg, field, None)
            if isinstance(value, (int, float)):
                self.assertTrue(math.isfinite(float(value)), f"{field} 被写成了非有限值")


class StateBadValueTest(unittest.TestCase):
    def test_num_rejects_non_finite(self):
        for bad in ("inf", "-inf", "nan", float("inf"), float("nan"), "abc", None, []):
            with self.subTest(bad=bad):
                got = _num(bad)
                self.assertEqual(got, 0.0)
                self.assertTrue(math.isfinite(got))

    def test_num_keeps_normal_values(self):
        self.assertEqual(_num("12.5"), 12.5)
        self.assertEqual(_num(0), 0.0)

    def test_safe_ts_rejects_non_finite(self):
        for bad in ("inf", "nan", float("inf"), float("nan"), "abc"):
            with self.subTest(bad=bad):
                got = _safe_ts(bad, 7.0)
                self.assertEqual(got, 7.0)
                self.assertTrue(math.isfinite(got))

    def test_safe_ts_keeps_normal_values(self):
        self.assertEqual(_safe_ts(1787382000.0), 1787382000.0)


if __name__ == "__main__":
    unittest.main()
