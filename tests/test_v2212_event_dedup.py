"""v2.2.12：跨消息的「同一件事」判据（回归用例）。

## 为什么

第三轮真机巡检的实时窗口里她一条都没发（群聊日额度在 23:59 就用完了），于是回头
把 2026-11-10 一整天她**真发出去**的 52 条群消息捞出来审。抓到的是老问题的新形态：

* 08:03 群 691114631「本宫把茶壶茶盏都洗了」；
* 08:04 群 611456288「本宫方才把茶壶茶盏一并洗了」；
* 08:27 群 1097575806「本宫才把茶壶茶盏洗了」；
* 15:22 群 602543123「正把抽屉里的旧票据一张张用蹄子夹出来摊开」；
* 15:45 群 224992692「抽屉里攒的旧票据全叼出来摊开了」。

这几对是同一件事换了说法。跨群闸（`_check_cross_user`）只比 bigram，重合度
0.10~0.35，阈值 0.6 全部放行——所以「同一件事反复说」在 v2.2.11 上仍然活着。

## 这一版加的判据

去掉模板词（本宫/方才/刚/用蹄子夹/趴着…）之后，比内容字的**最长连续公共片段**：

| 组 | 应判同事件 | 内容字连续公共片段 |
|---|---|---|
| 洗茶壶 ×3 | 是 | 5 字「茶壶茶盏洗」 |
| 旧票据 ×2 | 是 | 3 字「旧票据」 |
| 睡 0 小时 ×2 | 是 | 4 字 |
| 煮茶 ×2 | 是 | 4 字「煮壶热茶」 |
| 茶壶 vs 巡梦境 | 否 | 0 |
| 泡茶 vs 旧票据 | 否 | 0 |
| 泡茶 vs 擦窗台 | 否 | 0 |

实测：应判同事件的 7 组是 3~11 字，不应判的 7 组最多 1 字，这组数据能干净分开。
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
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
    api.logger = logging.getLogger("autonomous_social_v2212_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import social.verify as V  # noqa: E402
from social.state import SocialState  # noqa: E402

NOW = 1800000000.0

# 线上 2026-11-10 的原话（去掉无障碍差异后逐字保留）
TEA_A = "本宫把茶壶茶盏都洗了"
TEA_B = "本宫方才把茶壶茶盏一并洗了"
TEA_C = "本宫才把茶壶茶盏洗了"
TICKET_A = "正把抽屉里的旧票据一张张用蹄子夹出来摊开"
TICKET_B = "抽屉里攒的旧票据全叼出来摊开了"
SLEEP_A = "（趴在床上把翅膀摊开）才睡0小时就醒了，现在只想把脸埋回枕头里"
SLEEP_B = "刚醒，睡0小时，趴床上赖着，翅膀都懒得收"
BOIL_A = "本宫刚煮了一壶热茶"
BOIL_B = "方才在厨房煮了一壶热茶"
DREAM = "本宫方才巡过一遍梦境"
SPILL = "刚用蹄子夹着杯子泡茶，洒了半桌，倒是一屋子茉莉香"
WINDOW = "刚用蹄子夹抹布擦窗台"
MOON = "刚把月亮挂回原位"


class EventContentTest(unittest.TestCase):
    """内容字：去掉括号动作与模板词。"""

    def test_action_span_and_boiler_removed(self):
        got = V._event_content(SLEEP_A)
        self.assertIn("睡", got)
        self.assertIn("0", got)
        self.assertNotIn("翅膀", got)   # 括号动作被去掉
        self.assertNotIn("把", got)     # 模板字被去掉

    def test_empty_text_is_empty(self):
        self.assertEqual(V._event_content(""), "")
        self.assertEqual(V._event_content("（只是动作）"), "")

    def test_common_run_is_contiguous(self):
        self.assertEqual(V._content_run("茶壶茶盏洗", "茶壶茶盏"), "茶壶茶盏")


class SameEventTrueTest(unittest.TestCase):
    """应判成同一件事的（线上真实对子）。"""

    def test_tea_cups(self):
        self.assertTrue(V._same_event_text(TEA_A, TEA_B))
        self.assertTrue(V._same_event_text(TEA_A, TEA_C))

    def test_old_tickets(self):
        self.assertTrue(V._same_event_text(TICKET_A, TICKET_B))

    def test_zero_hours_sleep(self):
        self.assertTrue(V._same_event_text(SLEEP_A, SLEEP_B))

    def test_boiling_tea(self):
        self.assertTrue(V._same_event_text(BOIL_A, BOIL_B))


class SameEventFalseTest(unittest.TestCase):
    """不该判成同一件事的（模板巧合，全是线上同一天的不同事）。"""

    def test_tea_cups_vs_dream_patrol(self):
        self.assertFalse(V._same_event_text(TEA_B, DREAM))

    def test_spill_vs_tickets(self):
        self.assertFalse(V._same_event_text(SPILL, TICKET_A))

    def test_spill_vs_wiping_window(self):
        self.assertFalse(V._same_event_text(SPILL, WINDOW))

    def test_moon_vs_dream_patrol(self):
        self.assertFalse(V._same_event_text(MOON, DREAM))

    def test_empty_is_never_same(self):
        self.assertFalse(V._same_event_text("", TEA_A))
        self.assertFalse(V._same_event_text("（只是动作）", TEA_A))


class CheckSameEventTest(unittest.TestCase):
    def test_hit_names_the_old_one(self):
        got = V._check_same_event(TEA_B, [TICKET_A, TEA_A])
        self.assertIsNotNone(got)
        self.assertIn("这件事", got)

    def test_miss_returns_none(self):
        self.assertIsNone(V._check_same_event(SPILL, [TICKET_A, MOON]))

    def test_empty_old_list(self):
        self.assertIsNone(V._check_same_event(TEA_B, []))


class CrossUserSameEventTest(unittest.TestCase):
    """跨群闸：bigram 抓不住的换说法，同事件判据要抓住。"""

    def test_bigram_below_threshold_but_same_event(self):
        from social.verify import _bigrams, _jaccard, strip_actions

        ratio = _jaccard(_bigrams(strip_actions(TEA_A)), _bigrams(strip_actions(TEA_B)))
        self.assertLess(ratio, 0.6, f"这对 bigram 竟然到 {ratio:.2f}，用例前提不成立")
        hit = V._check_cross_user(TEA_B, [("群691114631", TEA_A)], threshold=0.6)
        self.assertIsNotNone(hit)
        self.assertIn("同一件事", hit)

    def test_unrelated_still_passes(self):
        self.assertIsNone(V._check_cross_user(DREAM, [("群602543123", TICKET_A)], threshold=0.6))

    def test_other_group_text_in_message(self):
        hit = V._check_cross_user(TICKET_B, [("群602543123", TICKET_A)], threshold=0.6)
        self.assertIsNotNone(hit)
        self.assertIn("群602543123", hit)


class GroupSelfTextsTest(unittest.TestCase):
    """state.group_self_texts：本群她说过的正文。"""

    def _state(self) -> SocialState:
        tmp = tempfile.mkdtemp(prefix="aso_v2212_")
        return SocialState(str(Path(tmp) / "state.json"))

    def test_only_self_and_window(self):
        st = self._state()
        st.record_group_self_text("b1", "g1", TEA_A, NOW - 3600.0)
        st.record_group_self_text("b1", "g1", TICKET_A, NOW - 600.0)
        st.record_group_self_text("b1", "g1", MOON, NOW - 40 * 3600.0)  # 窗口外
        st.record_group_message(
            "b1", "g1", "umo://g1", "g1", "群友", "别人说的话", False, NOW - 600.0
        )
        got = st.group_self_texts("b1", "g1", NOW, hours=12.0)
        self.assertEqual(got, [TEA_A, TICKET_A])

    def test_limit_keeps_newest(self):
        st = self._state()
        for i in range(6):
            st.record_group_self_text("b1", "g1", f"第{i}件事", NOW - (6 - i) * 60.0)
        got = st.group_self_texts("b1", "g1", NOW, hours=12.0, limit=3)
        self.assertEqual(got, ["第3件事", "第4件事", "第5件事"])

    def test_unknown_group_is_empty(self):
        st = self._state()
        self.assertEqual(st.group_self_texts("b1", "g404", NOW), [])

    def test_dedup_same_text(self):
        st = self._state()
        st.record_group_self_text("b1", "g1", TEA_A, NOW - 300.0)
        st.record_group_self_text("b1", "g1", TEA_A, NOW - 100.0)
        self.assertEqual(st.group_self_texts("b1", "g1", NOW), [TEA_A])


class GroupIssueSpokenTest(unittest.TestCase):
    """`_group_proactive_issue` 的 spoken 闸。"""

    def _issue(self, **kw):
        from social.engine import _group_proactive_issue

        return _group_proactive_issue([TEA_B], **kw)

    def test_same_event_in_same_group_blocked(self):
        got = self._issue(spoken=[TEA_A])
        self.assertTrue(got)
        self.assertIn("这件事", got)

    def test_unrelated_spoken_passes(self):
        self.assertEqual(self._issue(spoken=[TICKET_A, MOON]), "")

    def test_no_spoken_keeps_old_behaviour(self):
        self.assertEqual(self._issue(), "")

    def test_openings_still_blocks(self):
        got = self._issue(openings=["本宫闲得很", "本宫想问问", "本宫才起"])
        self.assertTrue(got)


class ConfigAndWiringTest(unittest.TestCase):
    def test_config_default(self):
        from social.config import GROUP_EVENT_DEDUP_HOURS_DEFAULT, SocialConfig

        self.assertEqual(GROUP_EVENT_DEDUP_HOURS_DEFAULT, 12.0)
        self.assertEqual(SocialConfig().group_event_dedup_hours, 12.0)

    def test_schema_entry(self):
        sch = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        key = sch["group_event_dedup_hours"]
        self.assertEqual(key["type"], "float")
        self.assertEqual(key["default"], 12.0)
        self.assertEqual(key["min"], 0.0)
        self.assertEqual(key["max"], 72.0)

    def test_engine_checks_spoken_text(self):
        src = (PLUGIN_ROOT / "social" / "engine.py").read_text(encoding="utf-8", errors="replace")
        self.assertIn("_check_same_event(joined, spoken)", src)
        self.assertIn("group_self_texts(", src)
        self.assertIn("group_event_dedup_hours", src)

    def test_verify_exports_judgement(self):
        src = (PLUGIN_ROOT / "social" / "verify.py").read_text(encoding="utf-8", errors="replace")
        self.assertIn("def _same_event_text(", src)
        self.assertIn("def _check_same_event(", src)
        self.assertIn("_same_event_text(text, old_text)", src)   # 跨群闸里真的接了


class GenericPhraseTest(unittest.TestCase):
    """v2.2.13：客套话不许被当成「同一件事」。

    真实风险：跨用户闸拿同一份素材比，而她会对不同的人说同样的客套话。
    旧版（片段≥3 或 覆盖率≥0.6）会把「晚安呀宝宝 / 宝宝晚安」判成同一件事，
    于是她跟第二个人道晚安直接被拦——宁可不说话也不许被拦掉正常表达。
    """

    def test_goodnight_to_two_people(self):
        self.assertFalse(V._same_event_text("晚安呀宝宝", "宝宝晚安"))
        self.assertFalse(V._same_event_text("晚安，做个好梦", "晚安好梦呀"))

    def test_early_sleep(self):
        self.assertFalse(V._same_event_text("早点睡吧", "你也早点睡"))

    def test_tired(self):
        self.assertFalse(V._same_event_text("今天好累", "今天真累"))

    def test_here_with_you(self):
        self.assertFalse(V._same_event_text("嗯嗯我在呢", "我在呢"))

    def test_just_woke(self):
        self.assertFalse(V._same_event_text("刚睡醒", "刚睡醒呢"))

    def test_drink_water(self):
        self.assertFalse(V._same_event_text("喝口热水", "记得喝热水"))

    def test_laugh(self):
        self.assertFalse(V._same_event_text("哈哈哈好呀", "好呀好呀"))

    def test_short_content_never_counts(self):
        """内容字不足 4 个的一律不算事件（哪怕覆盖率 100%）。"""
        self.assertFalse(V._same_event_text("在的", "在的在的"))
        self.assertLess(len(V._event_content("晚安呀")), V._EVENT_CONTENT_MIN)

    def test_structural_particles_are_not_content(self):
        """「得/地」这类结构助词不算内容字。

        线上真实误判：11-10 07:28「外头晴得干净才想起来茶还在灶上温着」和
        08:04「台面擦干连点心屑也收得干净」只共享「得干净」三个字，被当成同一件事。
        """
        self.assertFalse(
            V._same_event_text(
                "外头晴得干净才想起来茶还在灶上温着",
                "台面擦干连点心屑也收得干净",
            )
        )
        self.assertNotIn("得", V._event_content("台面擦干连点心屑也收得干净"))

    def test_real_event_still_wins(self):
        """收紧之后，真正的同一件事一个都不能漏。"""
        self.assertTrue(V._same_event_text("本宫把茶壶茶盏都洗了", "本宫方才把茶壶茶盏一并洗了"))
        self.assertTrue(
            V._same_event_text(
                "正把抽屉里的旧票据一张张用蹄子夹出来摊开",
                "抽屉里攒的旧票据全叼出来摊开了",
            )
        )
        self.assertTrue(V._same_event_text("方才在厨房煮了一壶热茶", "本宫刚煮了一壶热茶"))


class TimeFrameTest(unittest.TestCase):
    """v2.2.21：共享的只是「昨晚梦」这种框架，不算同一件事。

    线上真实误判（跨渠道回放 40 条命中里占 4 条）：她跟不同的人梦到的是
    完全不同的事（合影 / 街灯 / 预约名单），却因为都用了「昨晚梦到…」被判成
    「同一件事」，于是第二条会被当成二传拦掉。
    """

    def test_different_dreams_are_not_same_event(self):
        self.assertFalse(
            V._same_event_text("醒啦…宝宝昨晚梦到那张合影", "昨晚梦见街灯一盏一盏亮成串")
        )
        self.assertFalse(
            V._same_event_text("昨晚梦到姐姐搬走那天的合影了", "昨晚梦见街灯一盏一盏亮成串")
        )
        self.assertFalse(
            V._same_event_text(
                "宝宝，昨晚梦到预约名单还剩好几条没回", "昨晚梦见街灯一盏一盏亮成串"
            )
        )

    def test_same_dream_still_wins(self):
        self.assertTrue(
            V._same_event_text("梦见预约名单还剩好几条没回", "昨晚梦见预约名单还剩几条没回")
        )

    def test_real_visit_not_lost(self):
        """第一版把时间字当模板词剔掉，连这条真二传也放过了——现在必须拦住。"""
        for text in (
            "娜莉灵下午来了一趟",
            "娜莉灵下午拎着袋子来敲门",
            "娜莉灵带来的那锅我热着吃完了",
        ):
            self.assertTrue(
                V._same_event_text(text, "中午用魔法托着手边的东西听娜莉灵说话"), text
            )

    def test_frame_set_exists(self):
        self.assertTrue(all(ch in V._EVENT_FRAME for ch in "昨晚梦到"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
