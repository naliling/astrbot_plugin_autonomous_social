"""v2.2.37：两段之间至少隔 1.2 秒（唯一一处覆盖框架分段参数的地方）。

线上 Bot 级 `segmented_reply.interval` 有 `0.5,1.1` / `0.8,1.1` 这种配法，插件照着算，
于是 3 秒能连发 7 条（11-10 18:28:17 那簇：n=7、跨 3.61 秒）。低于 1.2 秒不像人在打字。
"""

from __future__ import annotations

import logging
import random
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE

if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("aso_v2237_stub")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import social.pacing as pacing  # noqa: E402


class FloorTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(pacing.MIN_PART_INTERVAL, 1.2)
        self.assertGreater(pacing.MAX_PART_INTERVAL, pacing.MIN_PART_INTERVAL)

    def test_random_method_is_floored(self):
        rnd = random.Random(20261111)
        cfg = {"interval_method": "random", "interval": "0.5,1.1"}
        for _ in range(50):
            self.assertGreaterEqual(pacing.part_interval("嗯", cfg, rng=rnd), 1.2)

    def test_log_method_is_floored(self):
        rnd = random.Random(20261111)
        cfg = {"interval_method": "log", "log_base": 2.6}
        for text in ("嗯", "好", "刚睡醒呢", "x" * 200):
            self.assertGreaterEqual(pacing.part_interval(text, cfg, rng=rnd), 1.2)

    def test_normal_interval_untouched(self):
        rnd = random.Random(7)
        cfg = {"interval_method": "random", "interval": "1.5,3.5"}
        values = [pacing.part_interval("嗯嗯", cfg, rng=rnd) for _ in range(30)]
        self.assertTrue(all(1.5 <= v <= 3.5 for v in values))

    def test_reversed_window_still_floored(self):
        rnd = random.Random(3)
        cfg = {"interval_method": "random", "interval": "1.1,0.5"}
        for _ in range(20):
            self.assertGreaterEqual(pacing.part_interval("嗯", cfg, rng=rnd), 1.2)

    def test_cap_still_wins_for_long_text(self):
        rnd = random.Random(11)
        cfg = {"interval_method": "log", "log_base": 2.6}
        value = pacing.part_interval("字" * 5000, cfg, rng=rnd)
        self.assertLessEqual(value, pacing.MAX_PART_INTERVAL)
        self.assertGreaterEqual(value, pacing.MIN_PART_INTERVAL)


class PlanPartsTest(unittest.TestCase):
    def test_waits_are_floored(self):
        ctx = types.SimpleNamespace()
        parts, waits = pacing.plan_parts(
            "刚睡醒呢，端了杯热水揣着蹄子在沙发上发呆。外面阴冷，你要不要过来一起？",
            "umo://g1",
            ctx,
            allow_burst=True,
        )
        if len(parts) >= 2:  # 框架没开分段时不拆，跳过
            self.assertTrue(all(w >= 1.2 for w in waits), waits)
            self.assertEqual(len(waits), len(parts) - 1)


if __name__ == "__main__":
    unittest.main()
