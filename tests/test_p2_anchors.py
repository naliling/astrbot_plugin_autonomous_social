"""P2 批次 · 单元 anchors.py（审查报告 #4–#13）。

只有 `social/anchors.py` 是这一轮的修改面；本文件把每条都钉在一个**可观测**的行为上：
先写清「原来会怎样」（多数用例的断言正是旧行为会挂掉的那个点），再验「现在怎样」。

不依赖 AstrBot 运行时，直接 import 模块跑：

    python -m unittest tests.test_p2_anchors -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from social import anchors  # noqa: E402

NOW = 1_700_000_000.0

# 一个「什么事都有」的 body：do/next/天气/两件想做的/三件没做完的/很闲/节日。
# 用来验证池子容量、tier 分档、悬空记录这些**条数**上的性质。
RICH_BODY = {
    "day": {
        "doing": "整理个案笔记",
        "done": ["门诊接待"],
        "next": ["要睡了"],
        "wants": ["去那家旧书店", "看一场电影"],
    },
    "weather": "中雨，气温 9℃",
    "asleep": False,
    "feelings": [{"text": "今天很闲", "weight": 1.0}],
    "ongoing": ["练一首新曲子", "把阳台的花换盆", "整理相册"],
    "holiday": "中秋节",
}


def kind_of(item):
    return anchors._s(item.get("kind"))


class DerivedAnchorTemplateIntact(unittest.TestCase):
    """#4 + #5：截断发生在**片段**上，不在模板上。

    同一个根因：`offer()` 内部统一 `_short(text, 14)`，对所有派生由头做二次截断。
    表现之一是「你上次说的「明天面试」后来呢」被从第 14 个字剁掉，右引号和
    「后来呢」全没了，产出一个**未闭合的引号**。
    """

    def test_4_continued_topic_keeps_closing_quote_and_tail(self):
        seg = "明天下午去医院做复查"          # 10 字：6 + 10 > 14，旧路径必被切
        u = {"conversation": [{"text": seg, "dir": "in"}]}
        out = anchors.derive_relations(u, None, NOW, set(), last_talk=NOW - 3600.0)
        topic = [t for t in out if t.startswith("你上次说的")]
        self.assertEqual(len(topic), 1, f"续聊由头没派生出来：{out}")
        text = topic[0]
        self.assertTrue(text.endswith("」后来呢"), f"右引号/「后来呢」被截掉：{text!r}")
        self.assertEqual(text.count("「"), text.count("」"), f"引号未闭合：{text!r}")
        self.assertIn(seg, text)
        # 旧行为（二次截断到 14）在这里长这样——留着做前后对比的凭据。
        self.assertEqual(("你上次说的「" + seg + "」后来呢")[:14],
                         "你上次说的「明天下午去医院做")
        self.assertNotEqual(text, ("你上次说的「" + seg + "」后来呢")[:14])

    def test_4_continued_topic_survives_the_relational_channel(self):
        seg = "明天下午去医院做复查"
        u = {"conversation": [{"text": seg, "dir": "in"}]}
        picks = anchors.prepare_round_anchors(
            u, {}, NOW, limit=3, relational=True, affection=0.0, last_talk=NOW - 3600.0)
        abouts = [anchors._s(p.get("about")) for p in picks]
        self.assertTrue(any(a.endswith("」后来呢") for a in abouts), abouts)

    def test_5_ongoing_task_fragment_keeps_full_width(self):
        """#5：`还在{_short(task, 14)}` 曾经被 offer 再切一刀（16 → 14）。"""
        task = "整理二〇二四年度的个人档案和报销单据"
        expect = "还在" + task[:14]
        self.assertGreater(len(expect), 14, "这条用例要求片段比旧的 14 字上限长")
        picks = anchors.prepare_round_anchors(
            {}, {"ongoing": [task]}, NOW, limit=1)
        self.assertEqual(anchors._s(picks[0].get("about")), expect)
        self.assertNotEqual(expect, expect[:14], "旧行为：整句被再次截到 14 字")

    def test_5_offer_layer_cap_is_the_storage_cap(self):
        """`offer` 与 `add_anchor` 必须同一个上限——否则返回的文本和存进池子的
        文本不一样，`consume` / `mark_failed` 按文本查找就找不到对象。"""
        self.assertEqual(anchors._short("字" * 200), "字" * anchors.ANCHOR_TEXT_MAX)
        item = anchors.add_anchor({}, anchors.KIND_DERIVED, "字" * 200, now=NOW)
        self.assertEqual(len(anchors._s(item.get("about"))), anchors.ANCHOR_TEXT_MAX)

    def test_5_no_derived_anchor_is_cut_mid_template(self):
        """把各类 body 过一遍：凡是带后缀的模板，后缀必须完整。"""
        bodies = [
            RICH_BODY,
            {"day": {"doing": "整理个案笔记"}},
            {"ongoing": ["练一首新曲子"]},
            {"weather": {"env": "外面在下雨"}},
        ]
        seen = 0
        for body in bodies:
            for text in anchors._derive_from_state(body, {}, NOW, set()):
                seen += 1
                self.assertLessEqual(len(text), anchors.ANCHOR_TEXT_MAX, text)
                if text.startswith("你上次说的「"):
                    self.assertTrue(text.endswith("」后来呢"), text)
                if text.startswith("你上次说") and "后来好点没" in text:
                    self.assertTrue(text.endswith("，后来好点没"), text)
        self.assertGreater(seen, 3, "样本太少，这条用例没验到东西")


class CareWordsNeedWordSense(unittest.TestCase):
    """#6：`_CARE_WORDS` 是无词边界的子串匹配，「忙/累/困/赶/烦」这些单字会把
    「帮忙 / 积累 / 困难 / 赶紧 / 麻烦」判成「对方状态不好」。"""

    FALSE_FRIENDS = ("帮忙", "积累", "困难", "赶紧", "麻烦")

    def test_6_false_friends_do_not_count_as_care(self):
        for phrase in self.FALSE_FRIENDS:
            self.assertEqual(anchors._care_hit(f"我最近在{phrase}呢"), "",
                             f"「{phrase}」被读成了对方状态不好")

    def test_6_real_state_still_counts(self):
        cases = ("我今天好累", "最近有点忙", "有点困", "赶进度赶疯了", "好烦")
        for phrase in cases:
            self.assertTrue(anchors._care_hit(phrase), f"「{phrase}」没被认出来")

    def test_6_care_anchor_not_derived_from_false_friend(self):
        u = {"conversation": [{"text": "我最近在帮忙搬家", "dir": "in"}]}
        out = anchors.derive_relations(u, None, NOW, set(), last_talk=NOW - 3600.0)
        self.assertNotIn("后来好点没", " ".join(out),
                         f"对方只是说了「帮忙」，却派出一句牵挂：{out}")

    def test_6_care_anchor_still_derived_from_real_state(self):
        u = {"conversation": [{"text": "我今天好累", "dir": "in"}]}
        out = anchors.derive_relations(u, None, NOW, set(), last_talk=NOW - 3600.0)
        care = [t for t in out if t.endswith("，后来好点没")]
        self.assertEqual(len(care), 1, out)

    def test_6_care_anchor_quotes_the_original_words(self):
        """#6 的后半截：旧行为输出裸词「你上次说忙，后来好点没」——对方到底说了什么，
        读这句话的模型并不知道，只能顺着那个字现编。现在带上对方原句片段。"""
        u = {"conversation": [{"text": "今天加班到十一点，好累啊", "dir": "in"}]}
        out = anchors.derive_relations(u, None, NOW, set(), last_talk=NOW - 3600.0)
        care = [t for t in out if t.endswith("，后来好点没")]
        self.assertEqual(len(care), 1, out)
        self.assertIn("加班", care[0], f"没带上对方原句片段：{care[0]!r}")
        self.assertNotEqual(care[0], "你上次说累，后来好点没")

    def test_6_care_anchor_uses_the_most_recent_mention(self):
        u = {"conversation": [
            {"text": "前几天好累", "dir": "in"},
            {"text": "今天在帮忙搬东西", "dir": "in"},
        ]}
        out = anchors.derive_relations(u, None, NOW, set(), last_talk=NOW - 3600.0)
        care = [t for t in out if t.endswith("，后来好点没")]
        self.assertEqual(len(care), 1, out)
        self.assertIn("前几天好累", care[0], out)

    def test_6_care_anchor_skips_a_false_friend_message(self):
        """最近一条是假朋友（帮忙），但窗口里更早那条是真的——现在能跳过它。"""
        u = {"conversation": [
            {"text": "我好累", "dir": "in"},
            {"text": "今天在帮忙搬东西", "dir": "in"},
        ]}
        out = anchors.derive_relations(u, None, NOW, set(), last_talk=NOW - 3600.0)
        care = [t for t in out if t.endswith("，后来好点没")]
        self.assertEqual(len(care), 1, out)
        self.assertIn("我好累", care[0], out)


class WeatherLeadIsNotDoubled(unittest.TestCase):
    """#7：Core 的 `weather.env` 本身就是「外面在下雨」这类整句，硬加「外头」
    得到的是「外头外面在下雨」。"""

    def test_7_no_double_position_prefix(self):
        self.assertEqual(anchors._notable_weather({"weather": "外面在下雨"}), "外面在下雨")
        self.assertEqual(
            anchors._notable_weather({"weather": {"env": "外面在下雨"}}), "外面在下雨")

    def test_7_prefix_still_added_when_missing(self):
        self.assertEqual(anchors._notable_weather({"weather": "下雨了"}), "外头下雨了")

    def test_7_prepare_round_never_yields_doubled_prefix(self):
        picks = anchors.prepare_round_anchors(
            {}, {"weather": {"env": "外面在下雨"}}, NOW, limit=5)
        abouts = [anchors._s(p.get("about")) for p in picks]
        self.assertTrue(abouts)
        for a in abouts:
            self.assertNotIn("外头外面", a, abouts)
            self.assertNotIn("外头外头", a, abouts)


class GreetIsRanked(unittest.TestCase):
    """#8：`KIND_RANK` 漏了 `KIND_GREET` → `KIND_RANK.get(kind, 9)`，greet 落到默认值
    9（最低），而 `derive_nag` 造的正是一堆 greet；`_REASON_BY_KIND` 却收录了它。"""

    def test_8_greet_has_a_real_rank(self):
        self.assertIn(anchors.KIND_GREET, anchors.KIND_RANK, "greet 又落回默认值 9 了")
        self.assertEqual(anchors.KIND_RANK[anchors.KIND_GREET], 5)
        self.assertLess(anchors.KIND_RANK[anchors.KIND_GREET],
                        anchors.KIND_RANK[anchors.KIND_DERIVED])
        self.assertLess(anchors.KIND_RANK[anchors.KIND_GREET],
                        anchors.KIND_RANK[anchors.KIND_WEATHER])

    def test_8_rank_table_covers_every_kind(self):
        known = {v for k, v in vars(anchors).items()
                 if k.startswith("KIND_") and isinstance(v, str)}
        self.assertTrue(known)
        self.assertEqual(known - set(anchors.KIND_RANK), set(),
                         "有 KIND_* 没登记档位，会静默落到默认值 9")

    def test_8_greet_wins_the_cut_before_derived_and_weather(self):
        """可观测的前后对比：`limit=2` 时旧行为把 greet 切掉，现在它排第一。"""
        u = {}
        anchors.add_anchor(u, anchors.KIND_WEATHER, "外头下雨了", now=NOW)
        anchors.add_anchor(u, anchors.KIND_DERIVED, "刚忙完方案", now=NOW)
        anchors.add_anchor(u, anchors.KIND_GREET, "你好久没理我了", now=NOW)
        order = [kind_of(a) for a in anchors.live_anchors(u, NOW, limit=5)]
        self.assertLess(order.index(anchors.KIND_GREET), order.index(anchors.KIND_DERIVED))
        self.assertLess(order.index(anchors.KIND_DERIVED), order.index(anchors.KIND_WEATHER))
        top2 = [kind_of(a) for a in anchors.live_anchors(u, NOW, limit=2)]
        self.assertIn(anchors.KIND_GREET, top2,
                      f"旧行为：greet 排在默认值 9，被切掉（top2={top2}）")


class TierPoolCapIsReal(unittest.TestCase):
    """#10：`TIER_POOL_MAX`（12/8/4）与 `ANCHOR_MAX = 5` 冲突，`limit=12` 永远取不满，
    分档在上限这一维上是假的。"""

    def test_10_caps_never_exceed_the_pool(self):
        self.assertLessEqual(max(anchors.TIER_POOL_MAX.values()), anchors.ANCHOR_MAX)
        self.assertEqual(anchors.TIER_POOL_MAX[anchors.TIER_HIGH], anchors.ANCHOR_MAX)

    def test_10_tiers_are_monotonic(self):
        self.assertGreater(anchors.TIER_POOL_MAX[anchors.TIER_HIGH],
                           anchors.TIER_POOL_MAX[anchors.TIER_MID])
        self.assertGreater(anchors.TIER_POOL_MAX[anchors.TIER_MID],
                           anchors.TIER_POOL_MAX[anchors.TIER_LOW])

    def test_10_tier_really_bounds_the_pick(self):
        """前后对比：旧值 high/mid/low = 12/8/4（而池子只装 5 条），v1.28.7 起 8/6/4。"""
        want = {
            anchors.TIER_HIGH: 8,
            anchors.TIER_MID: 6,
            anchors.TIER_LOW: 4,
        }
        for tier, n in want.items():
            picks = anchors.prepare_round_anchors({}, dict(RICH_BODY), NOW,
                                                 limit=12, tier=tier)
            self.assertEqual(len(picks), n, f"{tier} 档取了 {len(picks)} 条")

    def test_10_no_dangling_pick_outside_the_pool(self):
        """池满后被挤出去的条目**不能**留在 `picked` 里——`consume` / `mark_failed`
        按文本找不到它，同一句每轮重新派生。"""
        u = {}
        picks = anchors.prepare_round_anchors(u, dict(RICH_BODY), NOW,
                                             limit=12, tier=anchors.TIER_HIGH)
        pool = {anchors._s(a.get("about")) for a in anchors.load_anchors(u)}
        self.assertTrue(picks)
        for p in picks:
            self.assertIn(anchors._s(p.get("about")), pool,
                          f"picked 里有池子里已经没有的悬空记录：{anchors._s(p.get('about'))}")

    def test_10_picked_items_are_actionable(self):
        """端到端：`picked` 里每一条都能被 `consume` 找到（没有悬空）。"""
        u = {}
        picks = anchors.prepare_round_anchors(u, dict(RICH_BODY), NOW, limit=3)
        for p in picks:
            about = anchors._s(p.get("about"))
            anchors.consume(u, about, NOW)
            rec = [a for a in anchors.load_anchors(u)
                   if anchors._s(a.get("about")) == about]
            self.assertEqual(len(rec), 1, f"consume 找不到 {about!r}（悬空）")
            self.assertGreater(anchors._f(rec[0].get("used_at")), 0.0)


class TtlVocabularyIsComplete(unittest.TestCase):
    """#11：`ANCHOR_TTL = 14 天` 实际是死值——9 种已知 kind 全在 `_KIND_TTL` 里，
    只有未知 kind 才会落到它。"""

    def test_11_dead_value_is_gone(self):
        self.assertFalse(hasattr(anchors, "ANCHOR_TTL"),
                         "死值 ANCHOR_TTL 又回来了：一个永不生效的数字比没有更误导人")

    def test_11_every_known_kind_has_a_ttl(self):
        known = {v for k, v in vars(anchors).items()
                 if k.startswith("KIND_") and isinstance(v, str)}
        self.assertTrue(known)
        self.assertEqual(known - set(anchors._KIND_TTL), set(),
                         "有 KIND_* 没登记有效期，只能落到兜底值")

    def test_11_fallback_is_the_shortest_bucket(self):
        self.assertEqual(anchors.KIND_TTL_FALLBACK, min(anchors._KIND_TTL.values()))

    def test_11_unknown_kind_gets_hours_not_two_weeks(self):
        """可观测：表外 kind 现在是 2 小时（最短），旧行为是 14 天。"""
        item = anchors.add_anchor({}, "brand_new_kind", "某件新事", now=NOW)
        self.assertEqual(anchors._f(item.get("expire_at")),
                         NOW + anchors.KIND_TTL_FALLBACK)
        self.assertLess(anchors._f(item.get("expire_at")), NOW + 86400.0)


class RememberedSurvivesThePool(unittest.TestCase):
    """#12：`_remembered` 只从由头池里取「最近用过的 about」，池子一被清理/挤出，
    避重就整体失效。"""

    def test_12_remembered_survives_pool_wipe(self):
        u = {}
        anchors.add_anchor(u, anchors.KIND_DERIVED, "刚忙完个案笔记", now=NOW)
        anchors.consume(u, "刚忙完个案笔记", NOW)
        u["anchors"] = []                      # 状态保留 / 被挤出池
        self.assertEqual(anchors.load_anchors(u), [])
        self.assertIn("刚忙完个案笔记", anchors._remembered(u, NOW + 60.0),
                      "池子一清，避重就失效了")

    def test_12_remembered_survives_prune_of_legacy_records(self):
        """老状态里的记录带着当初那条短 TTL（派生类 2h），prune 一过就没了记录。"""
        u = {"anchors": [{
            "kind": anchors.KIND_DERIVED,
            "about": "刚忙完个案笔记",
            "used_at": NOW,
            "expire_at": NOW + anchors.TTL_SELF_EVENT,
            "use_count": 1,
        }]}
        later = NOW + anchors.TTL_SELF_EVENT + 60.0
        anchors.prune_anchors(u, later)
        self.assertEqual(anchors.load_anchors(u), [], "短 TTL 之后记录被丢掉了")
        self.assertIn("刚忙完个案笔记", anchors._remembered(u, later),
                      "记录一丢，避重依据跟着丢")

    def test_12_legacy_pool_only_state_still_works(self):
        """没有独立台账的老状态：池内记录照旧算数。"""
        u = {"anchors": [{
            "kind": anchors.KIND_DERIVED,
            "about": "旧事",
            "used_at": NOW,
            "expire_at": NOW + anchors.ANCHOR_COOLDOWN,
            "use_count": 1,
        }]}
        self.assertIn("旧事", anchors._remembered(u, NOW + 60.0))

    def test_12_same_event_is_not_re_derived_after_pool_wipe(self):
        """端到端：池子清空后，同一件事不会被重新派生出来。"""
        u = {}
        anchors.add_anchor(u, anchors.KIND_DERIVED, "刚忙完个案笔记", now=NOW)
        anchors.consume(u, "刚忙完个案笔记", NOW)
        u["anchors"] = []
        picks = anchors.prepare_round_anchors(
            u, {"day": {"doing": "个案笔记"}}, NOW + 600.0, limit=2)
        abouts = [anchors._s(p.get("about")) for p in picks]
        self.assertNotIn("刚忙完个案笔记", abouts, f"同一件事又被派出来了：{abouts}")


class RetiredOutcomeIsDistinguishable(unittest.TestCase):
    """#13：「真发出去过」与「试满作废」在记录上不可区分，运行指标与排障都分不出来。"""

    def _one(self, u):
        pool = anchors.load_anchors(u)
        self.assertEqual(len(pool), 1, pool)
        return pool[0]

    def test_13_three_endings_are_three_values(self):
        now = NOW
        u_sent, u_failed, u_cross = {}, {}, {}

        anchors.add_anchor(u_sent, anchors.KIND_DERIVED, "甲", now=now)
        anchors.consume(u_sent, "甲", now)

        anchors.add_anchor(u_failed, anchors.KIND_DERIVED, "乙", now=now)
        for _ in range(anchors.ANCHOR_MAX_TRIES):
            anchors.mark_failed(u_failed, "乙", now)

        anchors.add_anchor(u_cross, anchors.KIND_DERIVED, "丙", now=now)
        anchors.drop_anchor(u_cross, "丙", now)

        sent = anchors.anchor_meta(self._one(u_sent))
        failed = anchors.anchor_meta(self._one(u_failed))
        cross = anchors.anchor_meta(self._one(u_cross))

        self.assertEqual(sent["retired"], anchors.RETIRED_SENT)
        self.assertEqual(failed["retired"], anchors.RETIRED_FAILED)
        self.assertEqual(cross["retired"], anchors.RETIRED_CROSS)
        self.assertEqual(len({sent["retired"], failed["retired"], cross["retired"]}), 3)
        # 排障用的人话也不一样，且老面板看的 `reason` 仍有内容
        for meta in (sent, failed, cross):
            self.assertTrue(meta["retired_note"], meta)
            self.assertTrue(meta["reason"], meta)

    def test_13_kind_and_about_survive_for_triage(self):
        """`consume` 之后 `kind` / `about` 不能丢——指标要按 kind 归类。"""
        u = {}
        anchors.add_anchor(u, anchors.KIND_GREET, "你好久没理我了", now=NOW)
        anchors.consume(u, "你好久没理我了", NOW)
        meta = anchors.anchor_meta(self._one(u))
        self.assertEqual(meta["kind"], anchors.KIND_GREET)
        self.assertEqual(meta["about"], "你好久没理我了")
        self.assertEqual(meta["retired"], anchors.RETIRED_SENT)

    def test_13_independent_log_also_records_why(self):
        """记录已经不在池子里时，排障仍能从独立台账看出它是怎么结束的。"""
        u = {}
        anchors.add_anchor(u, anchors.KIND_DERIVED, "甲", now=NOW)
        anchors.consume(u, "甲", NOW)
        u["anchors"] = []
        log = u[anchors._ANCHOR_USED_KEY]
        self.assertEqual([r["why"] for r in log], [anchors.RETIRED_SENT])
        self.assertEqual(log[0]["about"], "甲")


class DeadKindsWereRemoved(unittest.TestCase):
    """#9（用户定夺：**选 A，删掉死词汇**）：`KIND_LOOP` / `KIND_PROMISE` /
    `KIND_THREAD` 三种由头从来没有生产者，TTL、档位、理由句、meta 分支全是死词汇。
    常量与四张表里的条目已一并删除。

    这里是**防回潮**，不只是快照：词表里不许再出现没有生产者的 kind。将来若要重新
    引入「承诺」「话说到一半」，生产者该落在 `threads.py` 那条链路上
    （category=promise / presence 已由它发出），不要在 anchors 里再加一个空壳常量。
    """

    DEAD = ("loop", "promise", "thread")

    def test_9_constants_are_gone(self):
        """常量本身不该再存在——留着就还会有人去用它。"""
        for name in ("KIND_LOOP", "KIND_PROMISE", "KIND_THREAD"):
            self.assertFalse(hasattr(anchors, name), f"{name} 又回来了")

    def test_9_no_dead_word_in_any_table(self):
        """四张表与 `anchor_meta` 的 mode 分支里都不许再出现这三个词。"""
        for kind in self.DEAD:
            self.assertNotIn(kind, anchors._KIND_TTL)
            self.assertNotIn(kind, anchors.KIND_RANK)
            self.assertNotIn(kind, anchors._REASON_BY_KIND)
        modes = {
            anchors.anchor_meta({"kind": kind})["mode"] for kind in anchors.KIND_RANK
        }
        self.assertNotIn("promise", modes)
        self.assertNotIn("presence", modes)

    def test_9_old_state_still_degrades_gracefully(self):
        """老状态 / 老落盘记录里若存过这三种 kind，必须安全降级而不是抛错：
        TTL 走兜底、档位走默认 9、理由句退回 `KIND_DERIVED` 的措辞、mode 走 share。"""
        for kind in self.DEAD:
            a = {"kind": kind, "about": "某件事"}
            self.assertTrue(anchors.anchor_sentence(a))
            self.assertEqual(anchors.anchor_meta(a)["mode"], "share")
            self.assertEqual(anchors.KIND_RANK.get(kind, 9), 9)
            # 兜底 TTL 必须存在，否则老记录会在 trim 时被当成无寿命
            self.assertGreater(anchors.KIND_TTL_FALLBACK, 0)

    def test_9_no_producer_on_any_known_path(self):
        """把当前能走的派生路径全跑一遍，确认没有谁造出这三种 kind。"""
        recorded = []
        original = anchors.add_anchor

        def spy(u, kind, about, **kw):
            recorded.append(kind)
            return original(u, kind, about, **kw)

        anchors.add_anchor = spy
        try:
            for body in (RICH_BODY, {"day": {"doing": "整理个案笔记"}}, {}):
                for tier in ("", anchors.TIER_HIGH, anchors.TIER_LOW):
                    anchors.prepare_round_anchors(
                        {}, dict(body), NOW, limit=5, tier=tier, last_talk=NOW - 6 * 3600.0)
                    anchors.prepare_round_anchors(
                        {}, dict(body), NOW, limit=3, tier=tier, relational=True,
                        affection=80.0, last_talk=NOW - 6 * 3600.0)
            # 安静久了随口问候——`derive_nag` 是 greet 的生产者，必须落进样本里
            anchors.derive_nag({}, NOW, set(), last_talk=NOW - 7 * 3600.0)
        finally:
            anchors.add_anchor = original

        self.assertTrue(recorded, "一条由头都没派生出来，这条用例没验到东西")
        self.assertEqual(set(recorded) & set(self.DEAD), set(),
                         "有生产者出现了：那就该把常量加回来并更新本条用例")
        self.assertIn(anchors.KIND_GREET, set(recorded),
                      "样本里连 greet 都没有，覆盖不够")


if __name__ == "__main__":
    unittest.main()
