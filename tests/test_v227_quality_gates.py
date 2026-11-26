"""Regression tests for group-flow compatibility without soft style rejection."""

from __future__ import annotations

import logging
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
    api.logger = logging.getLogger("autonomous_social_v227_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import social.anchors as anchors_mod  # noqa: E402
import social.engine as engine_mod  # noqa: E402
import social.verify as verify_mod  # noqa: E402

NOW = 1_800_000_000.0
HOOF = "半人马"
def _wake_body(hours: float) -> dict:
    return {"wake": {"asleep": False, "wake_at": NOW - 600.0, "last_sleep_hours": hours}}


# ─── 一、数据硬事实：睡 0 小时 ────────────────────────────────────


class WakeCueNoZeroHoursTest(unittest.TestCase):
    """线上原文「才睡0小时就醒了」：`int(0.4)` 截断成 0，说出人类不会说的话。"""

    def test_short_sleep_is_not_zero(self):
        for hours in (0.1, 0.4, 0.9):
            got = anchors_mod._wake_cue(_wake_body(hours), now=NOW)
            self.assertNotIn("0小时", got, f"{hours}h 说成了 {got!r}")

    def test_buckets(self):
        cases = {
            0.4: "没睡多久",
            1.2: "一个多小时",
            1.8: "不到2小时",
            5.0: "才睡5小时",
        }
        for hours, want in cases.items():
            got = anchors_mod._wake_cue(_wake_body(hours), now=NOW)
            self.assertIn(want, got, f"{hours}h → {got!r}")

    def test_long_sleep_still_says_long(self):
        got = anchors_mod._wake_cue(_wake_body(8.0), now=NOW)
        self.assertIn("睡了挺久", got)


# ─── 二、普通回复出口：占位符 / 物种 / 说教 ──────────────────────


class PlaceholderResidueTest(unittest.TestCase):
    """「(Model response will be rendered here)」被原样发出过一次。"""

    def test_embedded_placeholder_rejected(self):
        passed, why = verify_mod.verify_message(
            "今天好累 (Model response will be rendered here)"
        )
        self.assertFalse(passed, "嵌入正文的占位符被放行了")
        self.assertIn("占位符", why)

    def test_standalone_placeholder_rejected(self):
        passed, _why = verify_mod.verify_message("(Model response will be rendered here)")
        self.assertFalse(passed)

    def test_tidy_preserves_placeholder_verbatim(self):
        text = "今天好累 (Model response will be rendered here)"
        self.assertEqual(verify_mod.tidy_outgoing_text(text), text)



class SpeciesConsistencyTest(unittest.TestCase):
    """露娜是蹄子，说「手还有点僵」就是穿帮。"""

    def test_hoof_rejects_hand(self):
        passed, why = verify_mod.verify_message(
            "我刚醒手还有点僵，快过来让我抱抱。", species=HOOF
        )
        self.assertFalse(passed, "蹄类角色写「手」被放行了")
        self.assertIn("身体不一致", why)

    def test_human_species_keeps_hand(self):
        passed, _why = verify_mod.verify_message("我刚醒手还有点僵。", species="人类")
        self.assertTrue(passed, "人类角色不该被这条误伤")

    def test_tidy_preserves_species_conflict_for_hard_rejection(self):
        text = "外面好冷，我刚醒手还有点僵，快过来让我抱抱。"
        self.assertEqual(verify_mod.tidy_outgoing_text(text, species=HOOF), text)
        passed, reason = verify_mod.verify_hard_safety(text, species=HOOF)
        self.assertFalse(passed)
        self.assertIn("身体不一致", reason)



class HardSafetyBoundaryTest(unittest.TestCase):
    def test_tidy_outgoing_text_is_exact_passthrough(self):
        text = "唔……今天好累 (Model response will be rendered here)"
        self.assertEqual(
            verify_mod.tidy_outgoing_text(
                text, species=HOOF, recent=("之前的句子",), context=("对方的话",)
            ),
            text,
        )

    def test_standalone_protocol_marker_is_rejected(self):
        passed, reason = verify_mod.verify_hard_safety("正常正文\nSEND\n后续")
        self.assertFalse(passed)
        self.assertIn("协议残留", reason)

    def test_pressure_requires_and_rejects_reply_demand_with_silence_blame(self):
        passed, reason = verify_mod.verify_hard_safety("快回我一句，你整晚都不回我。")
        self.assertFalse(passed)
        self.assertIn("催", reason)

    def test_ultimatum_is_rejected(self):
        passed, reason = verify_mod.verify_hard_safety("你到底要怎样，给我个说法。")
        self.assertFalse(passed)
        self.assertIn("逼人表态", reason)

    def test_configured_maximum_length_is_enforced(self):
        passed, reason = verify_mod.verify_hard_safety("这句话超过限制", max_length=4)
        self.assertFalse(passed)
        self.assertIn("长度上限", reason)

    def test_fragment_compatibility_helper_preserves_text_and_timing(self):
        segments = ["窗边伸了个懒腰", "眼睛闭了会儿。你刚才那句来抱抱"]
        waits = [1.2]
        self.assertEqual(engine_mod._merge_fragments(segments, waits), (segments, waits))


class ChannelSwitchTest(unittest.TestCase):
    """v2.2.10：私聊 / 群聊总开关（`allow_private_chat` / `allow_group_chat`）。

    用户要的是「只允许私聊 / 只允许群聊 / 全开」三种组合，两个默认值都为开，
    两个开关放在同一处（配置面板相邻两项）。原来群侧没有任何总开关，私聊也没有；
    这次两边补齐，且**每个发送入口都要真的早退**——只加配置项不加闸，面板上关掉
    也照样发，等于假开关。
    """

    def _engine(self, **cfg):
        eng = object.__new__(engine_mod.SocialEngine)
        eng.cfg = types.SimpleNamespace(**cfg)
        return eng

    def test_defaults_are_on(self):
        eng = self._engine(allow_private_chat=True, allow_group_chat=True)
        self.assertTrue(eng._private_allowed())
        self.assertTrue(eng._group_allowed())

    def test_missing_keys_default_to_on(self):
        eng = self._engine()
        self.assertTrue(eng._private_allowed(), "旧配置没这个键时必须按开处理")
        self.assertTrue(eng._group_allowed(), "旧配置没这个键时必须按开处理")

    def test_private_off_does_not_touch_group(self):
        eng = self._engine(allow_private_chat=False, allow_group_chat=True)
        self.assertFalse(eng._private_allowed())
        self.assertTrue(eng._group_allowed(), "关私聊不该顺手把群聊也关掉")

    def test_group_off_does_not_touch_private(self):
        eng = self._engine(allow_private_chat=True, allow_group_chat=False)
        self.assertFalse(eng._group_allowed())
        self.assertTrue(eng._private_allowed(), "关群聊不该顺手把私聊也关掉")

    def test_schema_has_both_switches_default_true(self):
        import json

        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in ("allow_private_chat", "allow_group_chat"):
            self.assertIn(key, schema, f"schema 缺 {key}")
            self.assertIs(schema[key]["default"], True, key)
            self.assertEqual(schema[key]["type"], "bool", key)

    def test_every_group_entry_gated(self):
        import inspect

        for name in (
            "_flow_reply",
            "_run_icebreaks",
            "_run_group_topics",
            "_icebreak",
            "_send_group_burst_followup",
        ):
            src = inspect.getsource(getattr(engine_mod.SocialEngine, name))
            self.assertIn("_group_gate(self)", src, f"{name} 没有查群聊总开关")

    def test_every_private_entry_gated(self):
        import inspect

        for name in ("_speak", "_send_burst_followup"):
            src = inspect.getsource(getattr(engine_mod.SocialEngine, name))
            self.assertIn("_private_gate(self)", src, f"{name} 没有查私聊总开关")


class QuietRoundReasonTest(unittest.TestCase):
    """v2.2.15：早退那条日志必须带上「为什么这一轮没人可发」。

    线上真的踩到了：她一整个窗口一条没发，日志只写「没有人攒够念头」，
    看起来像念头不够，其实是安静时段整轮跳过。沉默必须能自我解释。
    """

    def _src(self):
        import social.engine as engine_mod
        return Path(engine_mod.__file__).read_text(encoding="utf-8")

    def test_early_return_logs_skip_reasons(self):
        src = self._src()
        i = src.index("没有人攒够念头")
        j = src.index("_persist_metrics", i)
        seg = src[max(0, i - 800):j]
        self.assertIn("这一轮被跳过的原因", seg, "早退日志没写跳过原因")
        self.assertIn("_round_skips", seg, "早退日志没用本轮跳过计数")

    def test_skips_are_reset_each_round(self):
        src = self._src()
        self.assertIn("self._round_skips = {}", src, "跳过计数没有每轮清零")


if __name__ == "__main__":
    unittest.main()
