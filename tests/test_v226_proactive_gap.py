"""v2.2.6：同一个人不再秒级连发、补发间隔不再拖几分钟、主段不再只有称呼。

三件事都来自 2026-11-10 的真实日志：

* 03:36 同一 uid 7.3 秒 / 10.7 秒内收到两条「已主动联系」——`_gap_left` 的结果
  被 `has_cue`（到点由头）与 `_urge_is_eager`（很想说）整条豁免；
* 02:54–03:03 同一 uid 8 分钟 5 条，其中补发间隔 84 秒 / 221 秒——AstrBot 对
  `log_base` ≤ 1 是整条换回 2.6，插件却夹到 1.01，`ln(wc+1)/ln(1.01)` 把同一条
  消息的后续段拖成上百秒；
* 00:44 发出去的主段是「又嫩又圆润那句」——主段没有「能不能独立成句」这一道检查。
"""

from __future__ import annotations

import json
import logging
import random
import sys
import types
import unittest
from pathlib import Path

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
    api.logger = logging.getLogger("autonomous_social_v226_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    star = types.ModuleType("astrbot.api.star")

    class _Context:
        pass

    class _Star:
        def __init__(self, *a, **k):
            pass

    star.Context = _Context
    star.Star = _Star

    class _EventMessageType:
        ALL = "ALL"

    filter_mod = types.ModuleType("astrbot.api.event.filter")

    def _pass_decorator(*_a, **_k):
        def _wrap(func):
            return func

        return _wrap

    filter_mod.EventMessageType = _EventMessageType
    filter_mod.command = _pass_decorator
    filter_mod.event_message_type = _pass_decorator
    event.filter = filter_mod
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    api.star = star
    api.event = event
    api.AstrBotConfig = dict
    astrbot.api = api
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.event.filter": filter_mod,
            "astrbot.api.star": star,
        }
    )


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social import pacing  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402

NOW = 1_800_000_000.0


def _engine(**cfg) -> SocialEngine:
    eng = SocialEngine.__new__(SocialEngine)
    eng.cfg = types.SimpleNamespace(**cfg)
    return eng


class HardGapTest(unittest.TestCase):
    """硬最小间隔：不看由头、不看「多想说」。"""

    def test_default_is_ten_minutes(self):
        self.assertEqual(SocialConfig().proactive_min_gap_minutes, 10)

    def test_no_last_sent_never_blocks(self):
        eng = _engine(proactive_min_gap_minutes=10)
        self.assertEqual(eng._hard_gap_left({}, NOW), 0.0)

    def test_within_gap_blocks(self):
        eng = _engine(proactive_min_gap_minutes=10)
        u = {"last_sent": NOW - 60}
        self.assertAlmostEqual(eng._hard_gap_left(u, NOW), 540.0, places=3)

    def test_after_gap_passes(self):
        eng = _engine(proactive_min_gap_minutes=10)
        u = {"last_sent": NOW - 700}
        self.assertEqual(eng._hard_gap_left(u, NOW), 0.0)

    def test_bad_value_falls_back_to_ten(self):
        eng = _engine(proactive_min_gap_minutes="坏值")
        u = {"last_sent": NOW - 60}
        self.assertAlmostEqual(eng._hard_gap_left(u, NOW), 540.0, places=3)

    def test_gap_is_clamped(self):
        eng = _engine(proactive_min_gap_minutes=99999)
        u = {"last_sent": NOW - 60}
        # 上限 1440 分钟，不会因为填了个大数就永久挡住
        self.assertAlmostEqual(eng._hard_gap_left(u, NOW), 1440 * 60 - 60, places=3)

    def test_gate_wired_without_cue_exemption(self):
        src = (PLUGIN_ROOT / "social" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("_hard_gap_left(self, u, self._time())", src)
        self.assertIn('"proactive_min_gap"', src)
        # 硬闸必须在「cooldown」那条可豁免的闸之后，且不带 has_cue 条件
        gate = src.split("_hard_left = SocialEngine._hard_gap_left", 1)[1]
        self.assertIn("proactive_min_gap", gate)
        self.assertNotIn("has_cue", gate.split("proactive_min_gap")[0])


class IncompleteLeadTest(unittest.TestCase):
    """主段不能是碎片：只有称呼、半句、或指着「那句」的指代。"""

    def test_bare_call_name_is_incomplete(self):
        self.assertTrue(SocialEngine._incomplete_lead("小阔耐", ("小阔耐",)))

    def test_name_with_particle_is_incomplete(self):
        self.assertTrue(SocialEngine._incomplete_lead("小阔耐呀～", ("小阔耐",)))

    def test_same_text_without_names_can_stand(self):
        # 不按字数一刀切：不知道 TA 叫什么时，「小阔耐」可能只是句昵称
        self.assertFalse(SocialEngine._incomplete_lead("小阔耐"))

    def test_short_greeting_can_stand_alone(self):
        self.assertFalse(SocialEngine._incomplete_lead("晚安", ("小阔耐",)))

    def test_dangling_reference_is_incomplete(self):
        self.assertTrue(SocialEngine._incomplete_lead("又嫩又圆润那句"))

    def test_connector_tail_is_incomplete(self):
        self.assertTrue(SocialEngine._incomplete_lead("我今天在楼下看见一只猫，因为"))

    def test_actions_and_punct_are_stripped(self):
        self.assertTrue(SocialEngine._incomplete_lead("（笑）小阔耐～", ("小阔耐",)))

    def test_complete_sentence_passes(self):
        self.assertFalse(SocialEngine._incomplete_lead("你今天有没有好好吃饭"))

    def test_merge_two_segments(self):
        segs, waits, why = SocialEngine._merge_incomplete_lead(
            ["小阔耐", "你今天吃饭没"], [12.0, 20.0], ("小阔耐",)
        )
        self.assertEqual(segs, ["小阔耐你今天吃饭没"])
        self.assertEqual(waits, [20.0])
        self.assertEqual(why, "")

    def test_merge_keeps_rest(self):
        segs, waits, why = SocialEngine._merge_incomplete_lead(
            ["小阔耐", "第一句", "第二句"], [1.0, 2.0], ("小阔耐",)
        )
        self.assertEqual(segs, ["小阔耐第一句", "第二句"])
        self.assertEqual(waits, [2.0])
        self.assertEqual(why, "")

    def test_single_short_segment_returns_reason(self):
        segs, waits, why = SocialEngine._merge_incomplete_lead(
            ["小阔耐"], [], ("小阔耐",)
        )
        self.assertEqual(segs, ["小阔耐"])
        self.assertEqual(waits, [])
        self.assertIn("不能单独开口", why)

    def test_complete_single_segment_untouched(self):
        segs, waits, why = SocialEngine._merge_incomplete_lead(["你今天有没有好好吃饭"], [])
        self.assertEqual(segs, ["你今天有没有好好吃饭"])
        self.assertEqual(why, "")

    def test_wired_after_plan_segments(self):
        src = (PLUGIN_ROOT / "social" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("_merge_incomplete_lead(\n            segments, waits, _lead_names\n        )", src)
        self.assertIn('"short_main"', src)
        self.assertIn('u.get("_call_name")', src)


class PartIntervalCapTest(unittest.TestCase):
    """非法 log_base 按 AstrBot 的规则回退 2.6，不再夹到 1.01 炸出上百秒。"""

    def test_bad_log_base_falls_back_to_2_6(self):
        text = "傍晚用蹄子夹着锅柄把妹妹带的东西热了一锅"
        bad = pacing.part_interval(
            text, {"interval_method": "log", "log_base": 0.9}, rng=random.Random(7)
        )
        good = pacing.part_interval(
            text, {"interval_method": "log", "log_base": 2.6}, rng=random.Random(7)
        )
        self.assertAlmostEqual(bad, good, places=6)

    def test_non_finite_log_base_falls_back(self):
        bad = pacing.part_interval(
            "晚安", {"interval_method": "log", "log_base": "nan"}, rng=random.Random(7)
        )
        good = pacing.part_interval(
            "晚安", {"interval_method": "log", "log_base": 2.6}, rng=random.Random(7)
        )
        self.assertAlmostEqual(bad, good, places=6)

    def test_interval_stays_in_seconds(self):
        cfg = {"interval_method": "log", "log_base": 0.9}
        for text in ("晚安", "傍晚用蹄子夹着锅柄把妹妹带的东西热了一锅"):
            wait = pacing.part_interval(text, cfg)
            self.assertGreater(wait, 1.0)
            self.assertLess(wait, 5.0)

    def test_normal_log_base_still_small(self):
        cfg = {"interval_method": "log", "log_base": 2.6}
        wait = pacing.part_interval("晚安", cfg)
        self.assertLess(wait, 5.0)

    def test_random_branch_untouched(self):
        cfg = {"interval_method": "random", "interval": "1.5,1.9"}
        for _ in range(20):
            wait = pacing.part_interval("随便一句", cfg)
            self.assertGreaterEqual(wait, 1.5)
            self.assertLessEqual(wait, 1.9)

    def test_cap_constant_documented(self):
        src = (PLUGIN_ROOT / "social" / "pacing.py").read_text(encoding="utf-8")
        self.assertIn("MAX_PART_INTERVAL = 30.0", src)
        self.assertIn("min(MAX_PART_INTERVAL", src)
        self.assertIn("base = 2.6", src)


class SchemaTest(unittest.TestCase):
    def test_schema_has_field(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        block = schema["proactive_min_gap_minutes"]
        self.assertEqual(block["type"], "int")
        self.assertEqual(block["default"], 10)
        self.assertEqual(block["min"], 1)


if __name__ == "__main__":
    unittest.main()
