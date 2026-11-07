"""v2.1.8：主动消息间隔的随机抖动（issue #9）。

issue 给的补丁是在 `_gap_left` 里直接 `random.uniform(0.75, 1.75)`。这里没有照抄：
`_gap_left` 一轮里会被问候判定、`_min_gap_left`、面板显示、投放决策各调一次。
每次现抽的话，面板上的「还剩多久」会来回跳；而且一轮里多次抽签等价于取最小值，
实际间隔会比配置区间更早、更不可控。

现在的做法是按 `(uid, 档位, 上次发送时刻, 是否特别关心)` 做稳定哈希：同一轮里所有
调用拿到同一个系数，发完一条 `last_sent` 变了，下一轮自动换一个。重启、多进程一致。
"""

import json
import logging
import math
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return

    class _FakeMessageChain:
        """与 tests/test_autonomous_social.py 的桩保持一致：谁先 import 都得能用。"""

        def message(self, text):
            return ("chain", text)

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v218_gap_jitter_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _FakeMessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()

from social import anchors  # noqa: E402
from social import config as config_mod  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402

NOW = 1_700_000_000.0


class _Engine(SocialEngine):
    """只借 `_gap_left`，绕开整个引擎的构造（它只用 self.cfg）。"""

    def __init__(self, **overrides) -> None:
        self.cfg = SocialConfig.from_astrbot(dict(overrides))


def _user(uid: str = "10086", last_sent: float = NOW, **extra):
    u = {"__uid": uid, "last_sent": last_sent}
    u.update(extra)
    return u


def _gap(
    uid: str = "10086",
    tier: str = anchors.TIER_MID,
    *,
    last: float = NOW,
    **cfg,
) -> float:
    engine = _Engine(**cfg)
    return engine._gap_left(_user(uid, last), tier, NOW)


class DefaultsTest(unittest.TestCase):
    def test_defaults_match_the_issue_range(self):
        cfg = SocialConfig()
        self.assertAlmostEqual(cfg.gap_jitter_low, 0.75)
        self.assertAlmostEqual(cfg.gap_jitter_high, 1.75)

    def test_schema_defaults_match_the_constants(self):
        schema = json.loads(
            (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertAlmostEqual(
            schema["gap_jitter_low"]["default"], config_mod.GAP_JITTER_LOW_DEFAULT
        )
        self.assertAlmostEqual(
            schema["gap_jitter_high"]["default"], config_mod.GAP_JITTER_HIGH_DEFAULT
        )

    def test_from_astrbot_reads_the_values(self):
        cfg = SocialConfig.from_astrbot({"gap_jitter_low": 0.5, "gap_jitter_high": 2.0})
        self.assertAlmostEqual(cfg.gap_jitter_low, 0.5)
        self.assertAlmostEqual(cfg.gap_jitter_high, 2.0)

    def test_from_astrbot_clamps_dirty_values(self):
        cfg = SocialConfig.from_astrbot({"gap_jitter_low": -5, "gap_jitter_high": 99})
        self.assertGreaterEqual(cfg.gap_jitter_low, config_mod.GAP_JITTER_MIN)
        self.assertLessEqual(cfg.gap_jitter_high, config_mod.GAP_JITTER_MAX)


class StableWithinCycleTest(unittest.TestCase):
    def test_same_cycle_is_stable(self):
        """一轮里被调用多次（问候/最小间隔/面板/投放），必须拿到同一个间隔。"""
        engine = _Engine()
        u = _user()
        first = engine._gap_left(u, anchors.TIER_MID, NOW)
        for _ in range(5):
            self.assertEqual(engine._gap_left(u, anchors.TIER_MID, NOW), first)

    def test_different_instances_agree(self):
        """重启/多进程：同样的 (uid, 档位, last_sent) 必须得到同样的间隔。"""
        a = _Engine()._gap_left(_user(), anchors.TIER_MID, NOW)
        b = _Engine()._gap_left(_user(), anchors.TIER_MID, NOW)
        self.assertEqual(a, b)

    def test_next_cycle_gets_a_new_draw(self):
        """发完一条之后 last_sent 变了，下一轮的间隔应该重新抽。"""
        values = {
            round(_gap(uid="u1", last=NOW - i), 6)
            for i in range(12)
        }
        self.assertGreater(len(values), 1, "每一轮都该是新抽的系数")

    def test_different_users_get_different_draws(self):
        values = {round(_gap(uid=f"u{i}"), 6) for i in range(12)}
        self.assertGreater(len(values), 1, "不同人的间隔不该完全一致")

    def test_remaining_time_counts_down_within_a_cycle(self):
        engine = _Engine()
        u = _user()
        early = engine._gap_left(u, anchors.TIER_MID, NOW)
        late = engine._gap_left(u, anchors.TIER_MID, NOW + 600)
        self.assertAlmostEqual(early - late, 600.0, places=3)


class RangeTest(unittest.TestCase):
    def test_normal_tier_stays_inside_the_range(self):
        """中档基准 30 分钟 × [0.75, 1.75] = 22.5~52.5 分钟。"""
        for i in range(40):
            gap = _gap(uid=f"u{i}")
            self.assertGreaterEqual(gap, 30 * 60 * 0.75 - 1e-6)
            self.assertLessEqual(gap, 30 * 60 * 1.75 + 1e-6)

    def test_special_care_stays_inside_the_range(self):
        """特别关心默认 20 分钟 × [0.75, 1.75] = 15~35 分钟。"""
        engine = _Engine()
        for i in range(40):
            gap = engine._gap_left(
                _user(f"u{i}"), anchors.TIER_HIGH, NOW, special_care=True
            )
            self.assertGreaterEqual(gap, 20 * 60 * 0.75 - 1e-6)
            self.assertLessEqual(gap, 20 * 60 * 1.75 + 1e-6)

    def test_cap_still_binds(self):
        """抖动放在夹取之前：`max_gap_minutes` 仍然是硬上限。"""
        gap = _gap(min_gap_minutes=720, tier=anchors.TIER_LOW)
        self.assertAlmostEqual(gap, config_mod.MAX_GAP_MINUTES_DEFAULT * 60.0)

    def test_jitter_never_returns_zero_or_negative(self):
        for i in range(40):
            self.assertGreater(_gap(uid=f"u{i}"), 0.0)


class DisabledTest(unittest.TestCase):
    def test_zero_means_off(self):
        self.assertAlmostEqual(_gap(gap_jitter_low=0, gap_jitter_high=0), 30 * 60.0)

    def test_low_equal_high_means_off(self):
        self.assertAlmostEqual(_gap(gap_jitter_low=1.5, gap_jitter_high=1.5), 30 * 60.0)

    def test_low_above_high_means_off(self):
        self.assertAlmostEqual(_gap(gap_jitter_low=2.0, gap_jitter_high=1.0), 30 * 60.0)

    def test_missing_attributes_fall_back_to_deterministic(self):
        """老配置对象（没有这两个字段）要按 1.0 走，不能炸。"""
        engine = _Engine()
        engine.cfg = SimpleNamespace(min_gap_minutes=60, max_gap_minutes=1440)
        u = _user()
        self.assertAlmostEqual(
            engine._gap_left(u, anchors.TIER_LOW, NOW), 720 * 60.0
        )

    def test_garbage_values_fall_back_to_deterministic(self):
        engine = _Engine()
        engine.cfg = SimpleNamespace(
            min_gap_minutes=60,
            max_gap_minutes=1440,
            gap_jitter_low="x",
            gap_jitter_high=None,
        )
        self.assertAlmostEqual(
            engine._gap_left(_user(), anchors.TIER_MID, NOW), 60 * 60.0
        )


class FactorTest(unittest.TestCase):
    def test_factor_is_inside_the_configured_band(self):
        engine = _Engine(gap_jitter_low=0.5, gap_jitter_high=2.0)
        for i in range(30):
            factor = engine._gap_jitter(_user(f"u{i}"), anchors.TIER_MID, False)
            self.assertGreaterEqual(factor, 0.5)
            self.assertLessEqual(factor, 2.0)
            self.assertTrue(math.isfinite(factor))

    def test_factor_differs_between_tiers(self):
        engine = _Engine()
        u = _user()
        factors = {
            engine._gap_jitter(u, anchors.TIER_HIGH, False),
            engine._gap_jitter(u, anchors.TIER_MID, False),
            engine._gap_jitter(u, anchors.TIER_LOW, False),
        }
        self.assertGreater(len(factors), 1)


if __name__ == "__main__":
    unittest.main()
