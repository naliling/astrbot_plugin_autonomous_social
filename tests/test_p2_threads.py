"""P2 批次：未完话题（`social/threads.py`）遗留条目的回归测试。

覆盖审查报告 P2 条目 #21–#27，每个用例注释里写清「原来会怎样」，钉住修好之后的行为。
按仓库惯例自带最小 astrbot 桩（照抄 `tests/test_v1273_review_fixes.py` 头部）。
"""

from __future__ import annotations

import logging
import random
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与 test_autonomous_social.py 同款，幂等） ───
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

from social import threads  # noqa: E402
from social.clock import city_epoch  # noqa: E402

NOW = 1_700_000_000.0


# ─── #21 「通过 / 过了 / 算了」是无词边界子串匹配 ────────────────────


class LoopClosedPhraseTest(unittest.TestCase):
    """#21：`_LOOP_CLOSED` 原来做的是 `word in body`，借词也算「人家给了结果」。

    原来会怎样：`any(word in body for word in _LOOP_CLOSED)` —— 句子里只要**出现**
    「通过 / 过了 / 算了」就判成收场。于是正常聊天一句「通过朋友打听」「我算了算时间」
    「路过了那家公司」就把整条线索关掉，而且 `loops_resolved_by` 也拿它当「已有结果」，
    对方明明还没说结果，那件事再也不会被回访。
    """

    def test_borrowed_words_keep_the_loop_open(self):
        """反例：词是借来的（不是在给这件事收场）→ 线索还得开着。"""
        for body in (
            "面试还没消息，昨天路过了那家公司",
            "体检还没出结果，我算了算时间应该快了",
            "考试还没消息，通过朋友打听也没用",
        ):
            with self.subTest(body=body):
                text, _due = threads.extract_open_loop(body, NOW)
                self.assertIsNotNone(text, f"借词把还开着的线索关掉了：{body}")

    def test_real_results_still_close_the_loop(self):
        """正例：整句就是在给这件事收场 → 仍然要关（别为了修误伤把判据放空）。"""
        for body in (
            "面试搞定了",
            "答辩结束了",
            "复查结果就是不太好",
            "面试通过了",
            "考试没通过，算了",
            "面试过了，就这样",
        ):
            with self.subTest(body=body):
                text, _due = threads.extract_open_loop(body, NOW)
                self.assertIsNone(text, f"该收的线索没收掉：{body}")

    def test_negation_keeps_the_loop_open(self):
        """「还没结束 / 还没搞定」是「还没有结果」，原来照样被当成结果关掉。"""
        for body in ("面试还没结束，明天还有一轮", "面试还没搞定，还在等消息"):
            with self.subTest(body=body):
                text, _due = threads.extract_open_loop(body, NOW)
                self.assertIsNotNone(text, f"否定式被当成结果了：{body}")

    def test_resolved_by_ignores_borrowed_words(self):
        """`loops_resolved_by` 一侧同理：借词不算「对方给了结果」。"""
        u = {"loops": [{"about": "上次面试那事", "due": NOW - 10}]}
        for body in (
            "面试的事我通过朋友打听了，还没消息",
            "面试还没消息，我路过了那家公司",
            "面试还没结束，还得再等",
        ):
            with self.subTest(body=body):
                self.assertEqual(threads.loops_resolved_by(u, body), [],
                                 f"借词被当成结果，线索被提前关掉：{body}")

    def test_resolved_by_still_closes_on_real_result(self):
        u = {"loops": [{"about": "上次面试那事", "due": NOW - 10}]}
        self.assertEqual(threads.loops_resolved_by(u, "面试通过了，还行"),
                         ["上次面试那事"])

    def test_both_sites_share_one_verdict(self):
        """两处判据必须同源：一边决定「还开不开」，另一边决定「关掉哪条」。"""
        u = {"loops": [{"about": "上次面试那事", "due": NOW - 10}]}
        for body in ("面试通过了", "面试还没消息，我路过了那家公司"):
            with self.subTest(body=body):
                closed = threads.extract_open_loop(body, NOW)[0] is None
                resolved = threads.loops_resolved_by(u, body) != []
                self.assertEqual(closed, resolved, f"两处口径不一致：{body}")


# ─── #22 「到家」不是「有后续结果的事」 ──────────────────────────────


class LoopWordsHomeArrivalTest(unittest.TestCase):
    """#22：`_LOOP_WORDS` 里的「到家」会让「我到家了」挂成未完话题。

    原来会怎样：对方一句「我到家了」会被抽成一条 loop，过 2.5–5.5 小时她回来问
    「后来呢」——那件事根本没有下文。
    """

    def test_home_arrival_is_not_a_loop(self):
        self.assertIsNone(threads.extract_open_loop("我到家了", NOW)[0])
        self.assertIsNone(threads.extract_open_loop("到家了，今天挺累的", NOW)[0])

    def test_school_start_is_still_tracked(self):
        """「开学」保留：它背后确实有下文（分班/宿舍/交材料），不是一句到位的事。"""
        self.assertIsNotNone(threads.extract_open_loop("快开学了宿舍还没定", NOW)[0])


# ─── #23 max_hours 只是最后截一刀，样本堆在截断点上 ─────────────────


class LoopDueSpreadTest(unittest.TestCase):
    """#23：`high = max(low + 0.5, low * 2.2)` 只由 min_hours 决定。

    原来会怎样：`max_hours` 只在最后 `min()` 一刀切。min_hours=8 时抽样区间是
    [8h, 17.6h]，超出 14h 的那 37% 全被砍成**正好 14h**——回访时间在 14 小时那一刻
    堆成一个尖峰（min_hours=12 时是 86%），而不是「铺在 8–14 小时之间」。
    """

    @staticmethod
    def _old_loop_due(now: float, off: int, min_hours: float, max_hours: float) -> float:
        """修复前的原样复刻，用来证明用例不是空转。"""
        low = max(0.5, float(min_hours))
        high = max(low + 0.5, low * 2.2)
        stamp = now + random.uniform(low, min(high, 6.0 if low <= 6.0 else high)) * 3600.0
        local = threads.city_now(stamp, off)
        if local.hour < 9:
            moved = local.replace(hour=10, minute=random.randint(0, 50),
                                  second=0, microsecond=0)
            stamp = city_epoch(moved, off)
        return min(stamp, now + max_hours * 3600.0)

    def test_max_hours_bounds_the_draw_instead_of_spiking(self):
        off = 480
        # 当地 06:00：+8~14h 落在 14:00~20:00，不会触发「凌晨挪到上午」那条分支
        now = city_epoch(datetime(2024, 5, 1, 6, 0), off)
        cap = now + threads.LOOP_MAX_HOURS * 3600.0

        random.seed(20240501)
        old = [self._old_loop_due(now, off, 8, threads.LOOP_MAX_HOURS) for _ in range(200)]
        random.seed(20240501)
        new = [threads._loop_due(now, off, 8, threads.LOOP_MAX_HOURS) for _ in range(200)]

        self.assertGreaterEqual(
            old.count(cap), 1, "复刻的旧逻辑没造出堆在 14h 的样本，用例空转")
        self.assertEqual(new.count(cap), 0, "到期点仍然堆在 max_hours 那一刻")
        self.assertTrue(all(now + 8 * 3600 <= s < cap for s in new),
                        "有样本掉出 [min_hours, max_hours] 区间")
        self.assertGreater(max(new) - min(new), 4 * 3600, "样本没有铺开")

    def test_default_window_is_unchanged(self):
        """默认配置（2.5 / 14）本来就落在 [2.5h, 5.5h]：这次改动不动这个区间。"""
        off = 480
        now = city_epoch(datetime(2024, 5, 1, 10, 0), off)  # 当地 10:00 → 12:30~15:30
        random.seed(7)
        got = [threads._loop_due(now, off) for _ in range(200)]
        self.assertTrue(all(now + 2.5 * 3600 <= s <= now + 5.5 * 3600 for s in got))


# ─── #24 「对方说的话」两套口径 ──────────────────────────────────────


class InboundDirConsistencyTest(unittest.TestCase):
    """#24：`recent_in_count` 把缺 dir 的记录算作对方说的，`last_in_text` 把它跳过。

    原来会怎样：同一个「对方说过什么」的问题，两个函数给出相反答案。老数据
    （只有对方说过的话、还没写 dir 的年代）进 `last_in_text` / `recent_exchange`
    会被整条丢掉——「回得敷衍」判不出来、已读续接退化成空。
    """

    def test_is_inbound_truth_table(self):
        self.assertTrue(threads._is_inbound({"text": "在吗"}))
        self.assertTrue(threads._is_inbound({"dir": "in", "text": "在吗"}))
        self.assertTrue(threads._is_inbound({"dir": None, "text": "在吗"}))
        self.assertFalse(threads._is_inbound({"dir": "out", "text": "在的"}))
        self.assertFalse(threads._is_inbound("在吗"))

    def test_last_in_text_reads_dir_less_old_data(self):
        """老数据缺 dir → 原来被整条跳过，返回空串，于是回退去读 last_message。"""
        u = {"conversation": [{"text": "还行", "ts": NOW - 60}]}
        self.assertEqual(threads.last_in_text(u, NOW, 600), "还行")

    def test_recent_exchange_reads_dir_less_old_data(self):
        u = {"conversation": [{"text": "面试定了", "ts": NOW - 120}]}
        self.assertEqual(threads.recent_exchange(u, 3), ["面试定了"])

    def test_count_side_agrees_dir_less_is_inbound(self):
        """`recent_in_count` 一侧本来就是「缺 dir 当对方说的」，钉住统一后的口径。"""
        base = dict(last_seen=NOW - 600, last_spoken=NOW - 300,
                    last_message="刚才说到哪了")
        dir_less = dict(base, conversation=[
            {"text": "在吗", "ts": NOW - 120},
            {"text": "刚才那个人", "ts": NOW - 60},
        ])
        hers = dict(base, conversation=[
            {"dir": "out", "text": "在吗", "ts": NOW - 600},
            {"dir": "out", "text": "刚才那个人", "ts": NOW - 480},
        ])
        kw = dict(probe_after_seconds=60, presence_after_seconds=300,
                  max_seconds=3600, context_seconds=3600)
        self.assertEqual(threads.thread_reason(dir_less, NOW, **kw)[0], "",
                         "缺 dir 的两条应算「对方还在连着说」，把追问按住")
        self.assertNotEqual(threads.thread_reason(hers, NOW, **kw)[0], "",
                            "她自己连发两条不该算成对方在说话")


# ─── #25 收场次数用的是「被冷落次数」 ────────────────────────────────


class CloserBudgetTest(unittest.TestCase):
    """#25：`MAX_CLOSERS` 读的是 `no_reply_streak`（被冷落次数）。

    原来会怎样：streak 只在 `after_ignored`（12 小时没回）时 +1，跟收场门槛 5 小时
    不同源——每个 12 小时里她可以收一次场，streak 才走一格，于是 docstring 写的
    「一共允许收三次场」实际管的是「被冷落三次」。反过来，收过 4 次场而没被冷落过的人
    照样能继续收。
    """

    def _u(self, **kw) -> dict:
        u = {
            "pending_result": "ignored",
            "last_sent": NOW - 9 * 3600,
            "conversation": [
                {"dir": "in", "text": "在吗", "ts": NOW - 700},
                {"dir": "out", "text": "早", "ts": NOW - 650},
            ],
        }
        u.update(kw)
        return u

    def test_sent_closers_are_counted(self):
        """已经收过 4 次场、但没被冷落过（streak=0）→ 原来照样还能再收一次。"""
        self.assertEqual(
            threads.closer_reason(self._u(closer_count=4), NOW, after_seconds=8 * 3600),
            "")

    def test_old_data_falls_back_to_streak(self):
        """老数据没有 closer_count → 退回旧口径，行为不比现在差。"""
        self.assertNotEqual(
            threads.closer_reason(self._u(no_reply_streak=1), NOW, after_seconds=8 * 3600),
            "")
        self.assertEqual(
            threads.closer_reason(self._u(no_reply_streak=3), NOW, after_seconds=8 * 3600),
            "")

    def test_closer_count_wins_once_written(self):
        """写侧一落地就以 closer_count 为准（docstring 的「一共收三次场」）。"""
        self.assertNotEqual(
            threads.closer_reason(self._u(closer_count=1, no_reply_streak=9), NOW,
                                  after_seconds=8 * 3600),
            "")


# ─── #26 她连发时 recent_exchange 取不到对方的话 ─────────────────────


class RecentExchangeBurstTest(unittest.TestCase):
    """#26：`recent_exchange` 只看最后 limit*2 条，她连发时窗口里全是她自己的话。

    原来会怎样：对方说过三句、她接着连发六句，`limit=3` 的窗口（最后 6 条）里
    一条对方的话都没有 → 返回 `[]`，「已读续接」退化成凭空找话。
    """

    def test_peer_words_survive_her_burst(self):
        conv = [{"dir": "in", "text": t, "ts": NOW + i}
                for i, t in enumerate(["面试定了", "后天", "在城东"])]
        conv += [{"dir": "out", "text": f"她发的第 {i} 条", "ts": NOW + 10 + i}
                 for i in range(6)]
        self.assertEqual(threads.recent_exchange({"conversation": conv}, 3),
                         ["面试定了", "后天", "在城东"])

    def test_order_and_limit_unchanged(self):
        """顺序仍是「由旧到新」，条数仍受 limit 约束。"""
        u = {"conversation": [
            {"dir": "in", "text": "面试定了", "ts": NOW},
            {"dir": "out", "text": "那挺好", "ts": NOW + 1},
            {"dir": "in", "text": "后天", "ts": NOW + 2},
            {"dir": "out", "text": "加油", "ts": NOW + 3},
        ]}
        self.assertEqual(threads.recent_exchange(u, 2), ["面试定了", "后天"])


# ─── #27 老数据里没有 due 的承诺被永久静默 ──────────────────────────


class PromiseDueFallbackTest(unittest.TestCase):
    """#27：`live_promises` 要求 `due > 0`，只有 `at` 的老条目永远不会到点。

    原来会怎样：老数据（或迁移前的条目）只写了 `at`，`due` 缺省为 0 → `due <= 0`
    直接 continue，那件诺永远兑现不了，还一直占着 `PROMISE_SLOTS_MAX = 3` 的名额。
    """

    def test_due_falls_back_to_promise_time(self):
        u = {"promises": [{"about": "答应给她带蛋糕", "at": NOW}]}
        self.assertEqual(threads.live_promises(u, NOW), [], "还没到点就兑现了")
        hits = threads.live_promises(u, NOW + threads.PROMISE_WAIT_SECONDS + 1)
        self.assertEqual([h[1] for h in hits], ["答应给她带蛋糕"])

    def test_due_still_wins_when_present(self):
        """有 due 就按 due —— 回落只在缺 due 时发生，不改新数据的口径。

        due 故意取在**补偿口径之后**（at + 30h > at + PROMISE_WAIT_SECONDS 的 20h）：
        这样「按 due」与「按补偿 due」会给出相反答案，这条断言才有鉴别力。
        """
        u = {"promises": [{"about": "答应给她带蛋糕", "at": NOW,
                           "due": NOW + 30 * 3600}]}
        self.assertEqual(threads.live_promises(u, NOW + threads.PROMISE_WAIT_SECONDS + 1), [],
                         "补偿口径不该覆盖明确的 due")
        self.assertEqual(len(threads.live_promises(u, NOW + 30 * 3600 + 1)), 1)

    def test_timestampless_entry_is_dropped_not_silent(self):
        """连 at 都没有的条目没有任何基准可补：留着既兑现不了又占名额。"""
        u = {"promises": [
            {"about": "没有时间基准的诺"},
            {"about": "答应给她带蛋糕", "at": NOW},
        ]}
        hits = threads.live_promises(u, NOW + threads.PROMISE_WAIT_SECONDS + 1)
        self.assertEqual([h[1] for h in hits], ["答应给她带蛋糕"])
        self.assertEqual([i.get("about") for i in u["promises"]], ["答应给她带蛋糕"])


if __name__ == "__main__":
    unittest.main()
