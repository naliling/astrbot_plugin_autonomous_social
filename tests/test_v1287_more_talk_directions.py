"""v1.28.7：由头来源再扩四条 + 漏斗放宽。

用户原话：「在发言的方向我觉得得扩展1个 有啥方案」「A组可以全做 保证质量（最关键）」。

## 这次做的四件事

**A1 `u["topics"]` 当由头**（`anchors.derive_topic_relations`）

`topics` 是每条消息都在攒的关键词表（每人 8 个，`TOPIC_MAX_CHARS=6`），而它此前
**只喂 prompt**（`generator.py` 一行「之前聊到的话题」），从没当过由头。这是由头里
唯一一个「加法」来源——别的都在做减法（跨用户太像就不发），而它每人一套，
天然不撞车：Core 的日程是角色级的，同一句「刚忙完个案笔记」发给 12 个人要靠护栏拦；
「你上次说的那个塞尔达」对每个人都是独一份。

三道闸（缺一不可）：`is_quotable`（`topics` 里混过「好的」「哈哈」和框架注入块）、
够新（`topic_seen_at` 记的是最后一次出现，21 天内）、刚聊过不提。
另有两条：没有来往记录不派（播种名单那个形状）、话题类有自己的额度（`TOPIC_QUOTA`）
不把关系向挤掉。

**A2 `day.done` 2→4 条**：Core 的 `day_phrases(past_limit=3)` 本来就给三件，
我们原来只取前两条，第三件白丢。

**A3 `ongoing` 3→5 条**：Core 契约自己截到 3，放宽在当前 Core 下是空转，
只为对面的升级留空间。

**A4 nag 三档 → 七档 + 按关系档措辞**：三档用完这个人当天/这周就没有
「没事干」的开口理由了。加档不等于多说话——门槛由 `_min_gap_left` 与
`miss_daily_cap` 管着，这里只是把「已经等了那么久」变成一句可说出口的话。
三张表各自独立、互不共用句子，否则「算不出档位」和「算出是熟人」会说同一句。

**配套 `ANCHOR_MAX` 5→8、`TIER_POOL_MAX` 8/6/4**：不抬高读取侧上限，
A1~A4 全被漏斗吃掉（派生侧产能 15 类以上，原来只看得见 5 条）。

## 本文件盯的

- A1：污染话题不进来、刚聊过不提、用过的不重提、没来往不派、旧账过期、
  `topic_seen_at` 跟着 topics 收、话题额度不越界；
- A2/A3：上限真的放宽了，且脏数据/空值不抛；
- A4：七档都够得着、三档措辞不串、低档没有短档、tier 传空串时退回通用档；
- 配套：池子容量与分档一致（不出现悬空记录）、`ANCHOR_ROUND_PER_USER` 仍是 2
  （漏斗放宽 ≠ 一轮倒八条）。
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE.parent


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    import logging
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_test")
    event = types.ModuleType("astrbot.api.event")

    class _Chain:
        def message(self, text):
            return ("chain", text)

    event.MessageChain = _Chain
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import social.anchors as anchors  # noqa: E402
from social import engine as engine_mod  # noqa: E402
from social.state import SocialState  # noqa: E402

HOUR = 3600.0
DAY = 86400.0
NOW = 1_700_028_800.0


def _u(**kw):
    base = {"conversation": [], "said": []}
    base.update(kw)
    return base


def _fresh(topics, **kw):
    """带「这些话题都才聊过」的时间戳的用户。"""
    out = {"topics": list(topics),
           "topic_seen_at": {t: NOW - DAY for t in topics}}
    out.update(kw)
    return out


# ─── A1：topics 当由头 ───────────────────────────────────────────


class TopicRelationTest(unittest.TestCase):
    """「你上次说的那个X，后来呢」——素材是 `u["topics"]`。

    ⚠️ 两个口径，写样本时容易踩：

    1. **话题必须 ≥3 个汉字**：`is_quotable` 把两字词一律挡掉（「时间」「怎么」
       这种常常只是被切碎的一个词，见 `textutil.is_quotable` 的注释）。
    2. **整段 ≤6 字**（`TOPIC_MAX_CHARS`）：超长的整段不提取，避免
       「尔达传说」这种切尾。所以「我在玩塞尔达传说」这种整句提不出话题，
       要写成「塞尔达传说，好难」。
    """

    def test_basic(self):
        u = _u(**_fresh(["塞尔达传说", "复习备考", "加班加点"],
                        last_seen=NOW - 5 * HOUR))
        out = anchors.derive_topic_relations(u, NOW, set(), limit=2)
        self.assertEqual(len(out), 2, f"该给 2 条：{out}")
        self.assertIn("你上次说的「塞尔达传说」后来呢", out)

    def test_polluted_topics_are_dropped(self):
        """`topics` 里混过「好的」「哈哈」和框架注入块。

        捧一句「你上次说的「好的」后来呢」回去，是整条消息里最假的地方。
        """
        bad = ["好的", "哈哈", "〔她的身体与生活 v10〕", "yep", "怎么"]
        u = _u(**_fresh(bad, last_seen=NOW - 5 * HOUR))
        out = anchors.derive_topic_relations(u, NOW, set(), limit=2)
        self.assertEqual(out, [], f"污染话题一条都不该进来：{out}")

    def test_too_short_is_dropped(self):
        u = _u(**_fresh(["时间", "面试", "复习备考"], last_seen=NOW - 5 * HOUR))
        out = anchors.derive_topic_relations(u, NOW, set(), limit=2)
        self.assertNotIn("你上次说的「时间」后来呢", out)
        self.assertTrue(out, "该留下真话题")

    def test_no_history_is_skipped(self):
        """从没聊过的人不翻旧账：`_talk_ts` 读不到时是 0，而 `now - 0` 看着像
        「好久没理你了」，其实是「这个人压根没聊过」。播种名单正是这个形状。"""
        u = _fresh(["塞尔达传说"])          # 没有 last_seen
        self.assertEqual(anchors.derive_topic_relations(u, NOW, set(), limit=2), [])

    def test_just_talked_is_skipped(self):
        """刚聊过就回头问「后来呢」是打断不是惦记。"""
        u = _u(**_fresh(["塞尔达传说"], last_seen=NOW - 10 * 60))
        self.assertEqual(anchors.derive_topic_relations(u, NOW, set(), limit=2), [])

    def test_two_hours_ago_is_fine(self):
        u = _u(**_fresh(["塞尔达传说"], last_seen=NOW - 3 * HOUR))
        self.assertTrue(anchors.derive_topic_relations(u, NOW, set(), limit=2))

    def test_taken_is_respected(self):
        u = _u(**_fresh(["塞尔达传说", "复习备考"], last_seen=NOW - 5 * HOUR))
        out = anchors.derive_topic_relations(
            u, NOW, {"你上次说的「塞尔达传说」后来呢"}, limit=2)
        self.assertNotIn("你上次说的「塞尔达传说」后来呢", out)

    def test_newest_first(self):
        """同一批话题里，最近聊过的先问。"""
        u = _u(
            topics=["旧事一桩", "新事一桩"],
            topic_seen_at={"旧事一桩": NOW - 20 * DAY, "新事一桩": NOW - 2 * HOUR},
            last_seen=NOW - 5 * HOUR,
        )
        out = anchors.derive_topic_relations(u, NOW, set(), limit=1)
        self.assertEqual(out, ["你上次说的「新事一桩」后来呢"])

    def test_undated_topics_are_treated_as_stale(self):
        """旧状态没有 `topic_seen_at`：一律当过期。

        升级后头几天话题由头会少一些，下一条消息带过时间戳就有了。
        宁可少几天，不要把陈年话题当新事问（那会是「你三个月前说的那个」）。
        """
        u = _u(topics=["塞尔达传说"], last_seen=NOW - 5 * HOUR)
        self.assertNotIn("topic_seen_at", u)
        self.assertEqual(anchors.derive_topic_relations(u, NOW, set(), limit=2), [])

    def test_stale_dated_topics_dropped(self):
        u = _u(topics=["塞尔达传说"],
               topic_seen_at={"塞尔达传说": NOW - 30 * DAY},
               last_seen=NOW - 5 * HOUR)
        self.assertEqual(anchors.derive_topic_relations(u, NOW, set(), limit=2), [])

    def test_fresh_window_boundary(self):
        """21 天内算新，超过不要。"""
        fresh = _u(topics=["塞尔达传说"],
                   topic_seen_at={"塞尔达传说": NOW - 20 * DAY},
                   last_seen=NOW - 5 * HOUR)
        self.assertTrue(anchors.derive_topic_relations(fresh, NOW, set(), limit=1))
        stale = _u(topics=["塞尔达传说"],
                   topic_seen_at={"塞尔达传说": NOW - 22 * DAY},
                   last_seen=NOW - 5 * HOUR)
        self.assertEqual(anchors.derive_topic_relations(stale, NOW, set(), limit=1), [])

    def test_bad_shapes(self):
        for bad in (None, "字符串", 3, [None, 1, {}], ["", "  "], [["嵌套"]]):
            u = _u(topics=bad)
            self.assertEqual(
                anchors.derive_topic_relations(u, NOW, set(), limit=2), [],
                msg=f"{bad!r} 该当没有",
            )

    def test_limit_is_honoured(self):
        u = _u(**_fresh(["甲事一桩", "乙事一桩", "丙事一桩", "丁事一桩"],
                        last_seen=NOW - 5 * HOUR))
        self.assertEqual(len(anchors.derive_topic_relations(u, NOW, set(), limit=1)), 1)
        self.assertEqual(len(anchors.derive_topic_relations(u, NOW, set(), limit=0)), 0)

    def test_wired_into_relational_branch(self):
        """端到端：`prepare_round_anchors(relational=True)` 该把它带出来。"""
        u = _u(**_fresh(["塞尔达传说", "复习备考"], last_seen=NOW - 5 * HOUR))
        picks = anchors.prepare_round_anchors(
            u, {}, NOW, limit=3, relational=True, missing_floor=0.0,
        )
        abouts = [str(a.get("about") or "") for a in picks]
        self.assertTrue(
            any("塞尔达传说" in t for t in abouts),
            f"话题由头没进池子：{abouts}",
        )
        # 它必须是 KIND_MISS（关系向）：念想通道只收 miss/greet
        hit = [a for a in picks if "塞尔达传说" in str(a.get("about") or "")][0]
        self.assertEqual(hit.get("kind"), anchors.KIND_MISS)

    def test_does_not_crowd_out_relations(self):
        """`derive_relations` 的产出优先：话题由头只占自己的额度，不把它挤掉。"""
        u = _u(
            topics=["甲事一桩", "乙事一桩", "丙事一桩"],
            topic_seen_at={t: NOW - DAY for t in ("甲事一桩", "乙事一桩", "丙事一桩")},
            conversation=[
                {"dir": "in", "text": "昨天加班到十一点整个人都散了", "ts": NOW - 5 * HOUR},
            ],
            last_seen=NOW - 5 * HOUR,
        )
        picks = anchors.prepare_round_anchors(
            u, {}, NOW, limit=3, relational=True, missing_floor=0.0,
        )
        abouts = [str(a.get("about") or "") for a in picks]
        self.assertTrue(
            any("后来好点没" in t for t in abouts),
            f"牵挂由头被话题挤掉了：{abouts}",
        )

    def test_topic_quota_is_capped(self):
        """话题由头最多 `TOPIC_QUOTA` 条，不能把关系向全占了。"""
        many = [f"甲事{i}一桩" for i in range(6)]
        u = _u(**_fresh(many, last_seen=NOW - 5 * HOUR))
        picks = anchors.prepare_round_anchors(
            u, {}, NOW, limit=8, relational=True, missing_floor=0.0,
        )
        topic_hits = [a for a in picks if "后来呢" in str(a.get("about") or "")]
        self.assertLessEqual(len(topic_hits), anchors.TOPIC_QUOTA)


class TopicSeenAtTest(unittest.TestCase):
    """`topic_seen_at`：话题由头的时效依据。

    ⚠️ `record_incoming` 不接受 `ts`：它用 `SocialState` 自己的时钟
    （`time_source`，测试里可注入）。所以这里不传 ts，改用注入时钟断言。
    """

    def _state(self, start):
        return SocialState(path=str(Path(tempfile.mkdtemp()) / "s.json"),
                           time_source=lambda: start)

    def test_records_last_seen(self):
        st = self._state(NOW)
        u = st.user("b", "u")
        # 「塞尔达传说」是 5 字（≤ TOPIC_MAX_CHARS=6），整句 8 字提不出来
        st.record_incoming("b", "u", "塞尔达传说，好难")
        self.assertIn("塞尔达传说", u.get("topics", []))
        self.assertAlmostEqual(
            float(u["topic_seen_at"].get("塞尔达传说", 0)), NOW, delta=1.0
        )

    def test_refreshed_when_retalked(self):
        """话题被重新提起就是它又新鲜了——记的是**最后一次**出现。"""
        st = self._state(NOW - 10 * DAY)
        st.record_incoming("b", "u", "塞尔达传说，开不开")
        first = float(st.user("b", "u")["topic_seen_at"]["塞尔达传说"])
        st._now = lambda: NOW          # 换时钟：同一个人十天后又提起
        st.record_incoming("b", "u", "塞尔达传说，通关了")
        second = float(st.user("b", "u")["topic_seen_at"]["塞尔达传说"])
        self.assertGreater(second, first)
        self.assertAlmostEqual(second, NOW, delta=1.0)

    def test_pruned_with_topics(self):
        """`topics` 被挤出上限时，时间戳一起走——不然按「见过的所有话题」增长。"""
        st = self._state(NOW)
        u = st.user("b", "u")
        u["topic_seen_at"] = {f"话题{i}": float(i) for i in range(50)}
        st._extract_topics(u, "面试加班", max_topics=8, now=NOW)
        self.assertLessEqual(len(u["topic_seen_at"]), 16,
                             f"孤儿时间戳没被收：{len(u['topic_seen_at'])}")

    def test_no_store_text_no_record(self):
        """`store_message_text=false` 时不落派生关键词（时间戳留空壳）。"""
        st = self._state(NOW)
        st.record_incoming("b", "u", "塞尔达传说，好难", store_text=False)
        u = st.user("b", "u")
        self.assertEqual(u.get("topics"), [])
        self.assertEqual(u.get("topic_seen_at"), {})


class TopicMergeTest(unittest.TestCase):
    """同一人被拆成两条记录时，`topic_seen_at` 合并取**较晚**的那个。"""

    def test_merge_takes_latest(self):
        st = SocialState(path=str(Path(tempfile.mkdtemp()) / "s.json"))
        target = {"topics": ["塞尔达传说"],
                  "topic_seen_at": {"塞尔达传说": NOW - 10 * DAY}}
        st._merge_user(
            target,
            {"topics": ["塞尔达传说", "新事一桩"],
             "topic_seen_at": {"塞尔达传说": NOW, "新事一桩": NOW}},
        )
        self.assertAlmostEqual(float(target["topic_seen_at"]["塞尔达传说"]), NOW,
                               delta=1.0,
                               msg="合并取了较早的时间戳，重新聊过的话题被判成旧的")
        self.assertIn("新事一桩", target["topic_seen_at"])

    def test_merge_handles_missing_dict(self):
        """一边没有 `topic_seen_at`（旧状态）时不抛，也不凭空造。"""
        st = SocialState(path=str(Path(tempfile.mkdtemp()) / "s.json"))
        target = {"topics": ["塞尔达传说"]}
        st._merge_user(target, {"topics": ["新事一桩"]})
        self.assertFalse(target.get("topic_seen_at"),
                         "两边都没有时间戳时不该凭空造一个空 dict")


class TopicSanitizeTest(unittest.TestCase):
    """洗掉污染话题时，孤儿时间戳一起走。

    ⚠️ `sanitize_user_record` 只洗**框架注入块**（Core 身体事实块、
    `<system-reminder>`），不洗「好的」「哈哈」那类语气词——那是
    `_STOP_WORDS` 在**提取时**挡的，不是清洗时挡的。由头那三道闸在
    `anchors.is_quotable`，也不在这里。三层各管一层，别混。
    """

    def test_orphan_timestamps_dropped(self):
        from social import sanitize
        u = {
            "last_message": "",
            "topics": ["〔她的身体与生活 v10〕", "面试"],
            "topic_seen_at": {
                "〔她的身体与生活 v10〕": 2.0,
                "面试": 3.0,
                "早就没人聊的": 4.0,
            },
        }
        self.assertTrue(sanitize.sanitize_user_record(u))
        self.assertNotIn("〔她的身体与生活 v10〕", u["topics"])
        self.assertEqual(set(u["topic_seen_at"]), {"面试"},
                         "被洗掉的话题留着孤儿时间戳")

    def test_clean_record_only_reports_real_change(self):
        """已经干净的话题不该让时间戳被改（迁移每启动跑一遍，不能谎报）。"""
        from social import sanitize
        u = {
            "last_message": "",
            "topics": ["面试"],
            "topic_seen_at": {"面试": 3.0},
        }
        before = dict(u["topic_seen_at"])
        sanitize.sanitize_user_record(u)
        self.assertEqual(u["topic_seen_at"], before, "干净记录的时间戳被改了")


# ─── A2 / A3：done 与 ongoing 的上限 ─────────────────────────────


class DoneOngoingCapTest(unittest.TestCase):
    def _derive(self, body):
        kinds: dict = {}
        return anchors._derive_from_state(body, {}, NOW, set(), kinds=kinds)

    def test_done_takes_four(self):
        body = {"day": {"done": ["一早去了门诊", "中午吃了面", "下午开了个会", "傍晚散了步"]}}
        out = self._derive(body)
        self.assertEqual(len([t for t in out if t in
                              ("一早去了门诊", "中午吃了面", "下午开了个会", "傍晚散了步")]), 4)

    def test_done_still_filters_vague_and_waking(self):
        body = {"day": {"done": ["才醒", "忙自己的事", "下午开了个会"]}}
        self.assertEqual(self._derive(body), ["下午开了个会"])

    def test_done_bad_items_skipped(self):
        # ⚠️ 有效项必须落在 `DONE_MAX`（4）以内——切片先切，脏数据在后面就够不着。
        body = {"day": {"done": [None, 3, {"x": 1}, ["嵌套"], "下午开了个会"]}}
        self.assertEqual(self._derive(body), [],
                         "第 5 条被 DONE_MAX 切掉，且前 4 条全是脏数据")

    def test_done_numbers_do_not_become_anchors(self):
        """`3` / `True` 这种非字符串会被 `_s` 翻成 "3" / "True"，
        变成一条由头——而「她刚做完 3」不是人话。"""
        body = {"day": {"done": [3, True, 3.5, "下午开了个会"]}}
        self.assertEqual(self._derive(body), ["下午开了个会"])

    def test_done_not_a_list(self):
        self.assertEqual(self._derive({"day": {"done": "下午开了个会"}}), [])

    def test_ongoing_takes_five(self):
        body = {"ongoing": [f"事{i}" for i in range(7)]}
        self.assertEqual(len(anchors._ongoing_tasks(body)), anchors.ONGOING_MAX)
        self.assertEqual(anchors._ongoing_tasks(body)[0], "事0")

    def test_ongoing_alternate_key(self):
        body = {"ongoing_tasks": [{"what": "整理个案笔记"}]}
        self.assertEqual(anchors._ongoing_tasks(body), ["整理个案笔记"])

    def test_constants_are_sane(self):
        self.assertEqual(anchors.DONE_MAX, 4)
        self.assertEqual(anchors.ONGOING_MAX, 5)


# ─── A4：nag 分层 + 按关系档措辞 ─────────────────────────────────


class NagTiersTest(unittest.TestCase):
    def _nag(self, gap, tier="", taken=None):
        u = _u(last_seen=NOW - gap)
        out = anchors.derive_nag(u, NOW, set() if taken is None else taken, tier=tier)
        return [str(a.get("about") or "") for a in out]

    def test_every_gap_tier_has_a_line(self):
        """七档都够得着：原来只有三档，用完这个人就没话可说了。"""
        for gap in (8 * DAY, 2 * DAY, 13 * HOUR, 7 * HOUR, 4 * HOUR, 90 * 60, 30 * 60):
            got = self._nag(gap)
            self.assertTrue(got, f"等了 {gap / HOUR:.0f} 小时却无话可说")

    def test_tier_wording_differs(self):
        """同一段沉默，三种关系说三句不同的话。

        ⚠️ 用 30 小时：低档那张表最短是 24 小时，5 小时它一条都没有。
        """
        lines = {
            tier: self._nag(30 * HOUR, tier=tier)[0]
            for tier in (anchors.TIER_HIGH, anchors.TIER_MID, anchors.TIER_LOW)
        }
        self.assertEqual(len(set(lines.values())), 3,
                         f"三档措辞串了：{lines}")

    def test_high_tier_can_say_miss_you(self):
        """对熟人可以说「想你了」——这是关系近才自然的一句。

        ⚠️ 高档那张表里带「想你」的是 6 小时那一档，5 小时落到 3 小时档
        （「忽然想找你」）。所以这里用 7 小时。
        """
        self.assertIn("想你", self._nag(7 * HOUR, tier=anchors.TIER_HIGH)[0])

    def test_low_tier_never_says_miss_you(self):
        """对半生不熟的人说「想你了」很怪。"""
        for gap in (30 * HOUR, 8 * DAY):
            got = self._nag(gap, tier=anchors.TIER_LOW)
            self.assertTrue(got)
            self.assertNotIn("想你", got[0], f"{gap / HOUR:.0f}h 时低档说出了亲昵话")

    def test_empty_tier_falls_back_to_generic(self):
        """算不出档位时用最中性的一句——那是「不知道多熟」，不是「最熟」。"""
        got = self._nag(5 * HOUR, tier="")
        self.assertTrue(got)
        self.assertIn(got[0], [t for _n, t in anchors._NAG_BY_GAP])

    def test_unknown_tier_falls_back(self):
        self.assertTrue(self._nag(5 * HOUR, tier="wat"))

    def test_low_tier_has_no_short_gaps(self):
        """低档最小间隔 12 小时：它压根等不到 6 小时以下，表里就不该有那些档。"""
        needs = [n for n, _t in anchors._NAG_BY_TIER[anchors.TIER_LOW]]
        self.assertEqual(min(needs), 24 * 3600.0)

    def test_used_line_not_repeated(self):
        taken = {t for _n, t in anchors._NAG_BY_GAP}
        self.assertEqual(self._nag(5 * HOUR, taken=taken), [])

    def test_no_history_no_line(self):
        u = _u(last_seen=0.0)
        self.assertEqual(anchors.derive_nag(u, NOW, set()), [])

    def test_tier_tables_are_ordered(self):
        """每张表都要按「等得越久措辞越重」排，且阈值单调不升。"""
        tables = {"generic": anchors._NAG_BY_GAP}
        tables.update(anchors._NAG_BY_TIER)
        for name, rows in tables.items():
            needs = [n for n, _t in rows]
            self.assertEqual(needs, sorted(needs, reverse=True),
                             f"{name} 表的阈值不是降序")

    def test_all_tier_lines_are_unique(self):
        """中短档（24h 以下）的措辞不能互相重复：重复就等于分组白做。

        长档（7 天 / 24 小时）**故意共用**——那两句对三种关系都成立，
        分组是为了拆开「想你了」这类亲昵话，不是为了把每句都改写一遍
        （硬改写只会得到「好久没你的音讯了」这种不像人话的句子）。
        共用的清单在 `_NAG_SHARED_LONG_TIERS`，加新的共用句要同步登记。
        """
        shared = set(anchors._NAG_SHARED_LONG_TIERS)
        seen = [t for _n, t in anchors._NAG_BY_GAP]
        for tier, rows in anchors._NAG_BY_TIER.items():
            for need, t in rows:
                if need >= 24 * 3600.0:
                    continue                     # 长档（含 24h）：允许共用
                self.assertNotIn(t, seen, f"{tier} 档的「{t}」在别处也有")
                seen.append(t)
        # 共用清单必须真的在用：登记了却没用上，说明表被改过而清单没跟上
        generic = [t for _n, t in anchors._NAG_BY_GAP]
        for t in shared:
            self.assertIn(t, generic, f"共用的「{t}」不在通用档里了")


# ─── 配套：漏斗放宽 ──────────────────────────────────────────────


class FunnelWidenedTest(unittest.TestCase):
    """`ANCHOR_MAX` / `TIER_POOL_MAX`：派生侧产能早就超过 5 条了。"""

    def test_pool_is_bigger(self):
        self.assertEqual(anchors.ANCHOR_MAX, 8)

    def test_tier_caps_stay_within_pool(self):
        self.assertLessEqual(max(anchors.TIER_POOL_MAX.values()), anchors.ANCHOR_MAX)
        self.assertEqual(anchors.TIER_POOL_MAX[anchors.TIER_HIGH], anchors.ANCHOR_MAX)

    def test_tier_caps_are_monotonic(self):
        self.assertGreater(anchors.TIER_POOL_MAX[anchors.TIER_HIGH],
                           anchors.TIER_POOL_MAX[anchors.TIER_MID])
        self.assertGreater(anchors.TIER_POOL_MAX[anchors.TIER_MID],
                           anchors.TIER_POOL_MAX[anchors.TIER_LOW])

    def test_no_dangling_pick_outside_the_pool(self):
        """池满后被挤出去的条目不能留在 `picked` 里（consume/mark_failed 找不到它）。"""
        u: dict = {}
        picks = anchors.prepare_round_anchors(u, dict(RICH_BODY), NOW,
                                             limit=12, tier=anchors.TIER_HIGH)
        pool = {anchors._s(a.get("about")) for a in anchors.load_anchors(u)}
        self.assertTrue(picks)
        for a in picks:
            self.assertIn(anchors._s(a.get("about")), pool,
                          f"「{a.get('about')}」是悬空记录：池子里没有它")

    def test_pool_never_exceeds_cap(self):
        u: dict = {}
        anchors.prepare_round_anchors(u, dict(RICH_BODY), NOW,
                                     limit=12, tier=anchors.TIER_HIGH)
        free = [a for a in anchors.load_anchors(u) if not anchors._f(a.get("used_at"))]
        self.assertLessEqual(len(free), anchors.ANCHOR_MAX)

    def test_round_per_user_unchanged(self):
        """漏斗放宽 ≠ 一轮倒八条：同一个人一轮仍是两件。"""
        self.assertEqual(engine_mod.ANCHOR_ROUND_PER_USER, 2)

    def test_rich_body_yields_more_than_before(self):
        """一份「什么都有的契约」现在该派生出 6 条以上（原来最多 5，且常只有 2）。"""
        u = _u(
            topics=["塞尔达传说", "复习备考"],
            topic_seen_at={"塞尔达传说": NOW - DAY, "复习备考": NOW - DAY},
            last_seen=NOW - 6 * HOUR,
            conversation=[{"dir": "in", "text": "昨天加班到十一点", "ts": NOW - 6 * HOUR}],
        )
        kinds: dict = {}
        out = anchors._derive_from_state(dict(RICH_BODY), u, NOW, set(), kinds=kinds)
        self.assertGreaterEqual(len(out), 6, f"只派生出 {len(out)} 条：{out}")
        # 每条都认得出自己的 kind
        for text in out:
            self.assertIn(text, kinds)


RICH_BODY = {
    "energy": 90.0, "social_energy": 80.0, "asleep": False,
    "feelings": ["手脚发沉", "脑子发蒙"],
    "hunger": 88.0, "sleep_pressure": 82.0, "discomfort": 65.0, "sleep_debt": 7.0,
    "wake": {"asleep": False, "wake_at": NOW - 300, "last_sleep_hours": 8.0},
    "ongoing": ["整理个案笔记", "回一封邮件", "约好的复诊", "修台灯", "浇花"],
    "activity": {},
    "day": {
        "doing": "在厨房煮饭",
        "done": ["一早去了门诊", "中午吃了面", "下午开了个会", "傍晚散了步"],
        "next": ["去超市", "回家做饭"],
        "wants": ["那家书店", "新上映的那部片"],
        "dreams": ["在海边"],
    },
    "weather": {}, "holiday": "", "routine": {},
}


if __name__ == "__main__":
    unittest.main()
