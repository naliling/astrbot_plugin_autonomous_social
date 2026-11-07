"""v2.0.5：档位间隔的 1440 分钟硬上限改成可配置（max_gap_minutes）。

v2.0.4 的 `_gap_left` 把按关系档位缩放后的分钟数写死夹在 1440（一天）。LOW 档本身
就是 720 分钟，`min_gap_minutes` 一调大就被 1440 卡住——用户根本调不动「关系淡的人
隔多久」，也不知道这个上限是有意的还是写死的。

这里钉住三件事：

1. 默认配置（1440）下行为与原来完全一致：LOW 档 720 分钟，缩放后仍按 1440 夹断；
2. 把 `max_gap_minutes` 调到 2880 后，超过 1440 的间隔不再被夹断；
3. 配置读取对越界/非法值有兜底（60 ~ 10080 分钟，0/负数/乱填回默认 1440）。

注意 `special_care_gap_minutes` 那条分支的 1440 上限**没有**改：它是「特别关心也
别一分钟问一次」的语义，与档位缩放无关。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = (
    _HERE.parent
    if (_HERE.parent / "_conf_schema.json").is_file()
    else _HERE.parent / "astrbot_plugin_autonomous_social"
)


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v205_gap_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import anchors  # noqa: E402
from social.config import (  # noqa: E402
    MAX_GAP_MINUTES_DEFAULT,
    MAX_GAP_MINUTES_MAX,
    MAX_GAP_MINUTES_MIN,
    SocialConfig,
)
from social.engine import SocialEngine  # noqa: E402

BASE = 1_700_000_000.0


class _Engine(SocialEngine):
    """只借 `_gap_left`，绕开整个引擎的构造（它只用 self.cfg）。"""

    def __init__(self, **overrides) -> None:
        # 这些用例测的是「档位缩放 + 上限夹取」本身的精确秒数；间隔抖动是另一层
        # （v2.1.8，见 tests/test_v218_gap_jitter.py），这里显式关掉。
        overrides.setdefault("gap_jitter_low", 0)
        overrides.setdefault("gap_jitter_high", 0)
        self.cfg = SocialConfig.from_astrbot(dict(overrides))


def _gap(overrides: dict, tier: str, *, streak: int = 0) -> float:
    engine = _Engine(**overrides)
    user = {"last_sent": BASE, "no_reply_streak": streak}
    return engine._gap_left(user, tier, BASE)


class DefaultCapTest(unittest.TestCase):
    """上限默认值就是 1440；档位间隔按 min_gap_minutes 缩放（v2.1.0 基准 60→30）。

    这一层盯的是「上限夹取」本身：夹子在不在、先夹后乘的顺序对不对、脏值会不会炸。
    """

    def test_default_constant_and_config(self) -> None:
        self.assertEqual(MAX_GAP_MINUTES_DEFAULT, 1440)
        self.assertEqual(SocialConfig().max_gap_minutes, MAX_GAP_MINUTES_DEFAULT)

    def test_low_tier_scales_from_its_720_baseline(self) -> None:
        # v2.1.0：min_gap_minutes 默认 60→30。档位表（LOW 720 / MID 60 / HIGH 20）
        # 只是基准，实际按 min_gap_minutes/60 缩放，默认下 LOW = 720×(30/60)=360 分钟。
        # 旧断言写死 720 分钟——那是 min_gap_minutes 还是 60 时的结果。
        self.assertAlmostEqual(_gap({}, anchors.TIER_LOW), 360 * 60.0)

    def test_mid_and_high_tiers_follow_the_same_base(self) -> None:
        # 同上：MID 60×(30/60)=30 分钟，HIGH 20×(30/60)=10 分钟。
        self.assertAlmostEqual(_gap({}, anchors.TIER_MID), 30 * 60.0)
        self.assertAlmostEqual(_gap({}, anchors.TIER_HIGH), 10 * 60.0)

    def test_scaled_gap_still_clipped_at_default(self) -> None:
        # min_gap=720（中档上限）→ LOW 缩放成 720*(720/60)=8640 分钟，默认仍夹到 1440。
        self.assertAlmostEqual(
            _gap({"min_gap_minutes": 720}, anchors.TIER_LOW), 1440 * 60.0
        )

    def test_missing_attribute_falls_back_to_1440(self) -> None:
        """老配置对象（没有 max_gap_minutes 字段）也要按 1440 走，不能炸。"""
        engine = _Engine()
        engine.cfg = SimpleNamespace(min_gap_minutes=60)
        user = {"last_sent": BASE, "no_reply_streak": 0}
        # LOW 档 720 分钟 < 1440，本来就不该被夹——缺字段只影响上限，不改变档位。
        self.assertAlmostEqual(
            engine._gap_left(user, anchors.TIER_LOW, BASE), 720 * 60.0
        )


class ConfigurableCapTest(unittest.TestCase):
    """调大之后，超过 1440 的间隔真的不再被夹断。"""

    def test_2880_lets_low_scaled_gap_through(self) -> None:
        # min_gap=720 → 8640 分钟，上限 2880 → 夹到 2880（而不是 1440）。
        self.assertAlmostEqual(
            _gap(
                {"min_gap_minutes": 720, "max_gap_minutes": 2880},
                anchors.TIER_LOW,
            ),
            2880 * 60.0,
        )

    def test_configured_cap_still_applies(self) -> None:
        # 上限仍然是上限：2880 的缩放结果遇到用户设的 1000，要夹到 1000。
        self.assertAlmostEqual(
            _gap(
                {"min_gap_minutes": 720, "max_gap_minutes": 1000},
                anchors.TIER_LOW,
            ),
            1000 * 60.0,
        )

    def test_streak_multiplier_applies_after_cap(self) -> None:
        # 上限在乘「被冷落」倍数**之前**生效（v2.0.4 就是这个顺序，这次没改）：
        # min_gap=720 → 8640 先夹到 1440，再乘 2.2 = 3168 分钟。
        self.assertAlmostEqual(
            _gap({"min_gap_minutes": 720}, anchors.TIER_LOW, streak=3),
            1440 * 2.2 * 60.0,
        )
        # 上限调到 2880 后同样先夹再乘：2880 * 2.2 = 6336 分钟。
        self.assertAlmostEqual(
            _gap(
                {"min_gap_minutes": 720, "max_gap_minutes": 2880},
                anchors.TIER_LOW,
                streak=3,
            ),
            2880 * 2.2 * 60.0,
        )

    def test_invalid_runtime_cap_falls_back(self) -> None:
        engine = _Engine()
        user = {"last_sent": BASE, "no_reply_streak": 0}
        for bad in (float("inf"), float("nan"), 0, -10, "oops"):
            engine.cfg = SimpleNamespace(min_gap_minutes=720, max_gap_minutes=bad)
            self.assertAlmostEqual(
                engine._gap_left(user, anchors.TIER_LOW, BASE),
                1440 * 60.0,
                msg=f"max_gap_minutes={bad!r} 应回退到 1440",
            )

    def test_special_care_branch_untouched(self) -> None:
        """特别关心的 1440 上限是另一套语义，这次没有改。"""
        engine = _Engine()
        engine.cfg = SimpleNamespace(
            special_care_gap_minutes=5000, max_gap_minutes=2880
        )
        user = {"last_sent": BASE}
        self.assertAlmostEqual(
            engine._gap_left(user, anchors.TIER_LOW, BASE, special_care=True),
            1440 * 60.0,
        )


class ConfigParseTest(unittest.TestCase):
    """面板/JSON 写进来的值要按范围夹住，0 和非法值回默认。"""

    def test_value_round_trip(self) -> None:
        self.assertEqual(
            SocialConfig.from_astrbot({"max_gap_minutes": 2880}).max_gap_minutes, 2880
        )

    def test_zero_falls_back_to_default(self) -> None:
        self.assertEqual(
            SocialConfig.from_astrbot({"max_gap_minutes": 0}).max_gap_minutes,
            MAX_GAP_MINUTES_DEFAULT,
        )

    def test_negative_falls_back_to_default(self) -> None:
        self.assertEqual(
            SocialConfig.from_astrbot({"max_gap_minutes": -5}).max_gap_minutes,
            MAX_GAP_MINUTES_DEFAULT,
        )

    def test_below_min_clamped(self) -> None:
        self.assertEqual(
            SocialConfig.from_astrbot({"max_gap_minutes": 20}).max_gap_minutes,
            MAX_GAP_MINUTES_MIN,
        )

    def test_above_max_clamped(self) -> None:
        self.assertEqual(
            SocialConfig.from_astrbot({"max_gap_minutes": 99999}).max_gap_minutes,
            MAX_GAP_MINUTES_MAX,
        )

    def test_garbage_falls_back_to_default(self) -> None:
        self.assertEqual(
            SocialConfig.from_astrbot({"max_gap_minutes": "天天"}).max_gap_minutes,
            MAX_GAP_MINUTES_DEFAULT,
        )


if __name__ == "__main__":
    unittest.main()
