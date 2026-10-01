"""由头池是**一次性队列**，不是待办清单。

我一度以为「未用的由头被 prune 掉了」是 bug——`prune_anchors` 对 `used_at=0` 的
判定 `0 + 45天 > now` 恒为假，所以每加一条新由头，上一条就被丢掉，池子上限实际是 1，
而不是 `add_anchor` 注释里写的 5、更不是 v1.23 声明的「高档 12」。

改完（未用的按寿命留着、上限放到 12）之后，7 天回归仿真里「正常」场景的送达
**从 127 条掉到 19 条**。原因和想的不一样：

    上限 1  → 每条用完就让位，下一轮从当下状态重新派生，6 轮拿到 18 条候选
    上限 12 → 池子里**用过的**还在，`add_anchor` 又不会重加同样的事，
               候选逐轮变少（3→2→1），派生被饿死

**所以上限 1 才是对的机制**，`prune_anchors` 没写错，是注释在误导人。
这一版把注释改对，把「由头池 12/8/4」的说法从 CHANGELOG / README 里去掉。
"""
import logging
import os
import sys
import time
import types
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

for _n, _a in (("astrbot", {}), ("astrbot.api", {"logger": logging.getLogger("x")}),
               ("astrbot.api.event", {"MessageChain": object})):
    _m = types.ModuleType(_n)
    for _k, _v in _a.items():
        setattr(_m, _k, _v)
    sys.modules[_n] = _m

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from social.anchors import (  # noqa: E402
    ANCHOR_MAX, KIND_CUE, KIND_DERIVED, add_anchor, load_anchors,
    prepare_round_anchors,
)

BODY = {
    "day": {"doing": "整理个案笔记", "done": ["门诊接待"], "next": ["要睡了"]},
    "weather": "外面在下雨",
    "asleep": False,
}


class PoolIsOneShotQueue(unittest.TestCase):
    def test_consumed_anchor_makes_way_for_fresh_ones(self):
        """一轮派生 → 消费一条 → 下一轮**必须还能派生出新的**。

        这是整个机制的关键：留着用过的不放，派生就被饿死（实测 127 条 → 19 条）。
        """
        u, now = {}, time.time()
        total = 0
        for _ in range(6):
            picks = prepare_round_anchors(u, dict(BODY), now, limit=12, tier="high")
            total += len(picks)
            for p in picks[:1]:
                p["used_at"] = now
            now += 600
        self.assertGreaterEqual(
            total, 12,
            f"6 轮只派出 {total} 条候选 —— 池子在堵死派生（上限太大时就是这个现象）",
        )

    def test_pool_never_exceeds_the_storage_cap(self):
        now = time.time()
        u = {}
        for i in range(20):
            add_anchor(u, KIND_DERIVED, f"由头{i}", now=now)
        self.assertLessEqual(len(load_anchors(u)), ANCHOR_MAX)

    def test_ttl_still_applies_per_kind(self):
        """对方那边的事留得久，她这边刚发生的留得短。"""
        from social.anchors import _KIND_TTL
        self.assertGreater(_KIND_TTL[KIND_CUE], _KIND_TTL[KIND_DERIVED])


class NoOverclaimedPoolSize(unittest.TestCase):
    """文档不许再把由头池说成 12——那是不成立的。

    只查**表格里的**声明，不查 CHANGELOG 里那句「曾经写着 12」的更正说明——
    那是故意留下的反例，不该被当成残留。
    """

    _CLAIMS = ("由头池 12", "由头池 12/8/4", "| 12 |", "高档 12")

    # 这些词出现在「已经改掉了」的说明里是正常的，只在**当它是真的规格**时才算残留。
    _EXCUSES = ("曾经", "不成立", "改掉", "夸大", "回退", "做不到", "说成", "实际")

    def test_docs_do_not_claim_a_twelve_slot_pool(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("README.md", "CHANGELOG.md"):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            for i, line in enumerate(lines, 1):
                if any(w in line for w in self._EXCUSES):
                    continue          # 更正/说明性的话，跳过
                for claim in self._CLAIMS:
                    self.assertNotIn(claim, line, f"{name}:{i} 还在声明「{claim}」")

    def test_no_table_claims_twelve(self):
        """配置表里更不该出现：表格里的数字会被当成规格照着理解。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("README.md", "CHANGELOG.md"):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            for i, line in enumerate(lines, 1):
                if not line.strip().startswith("|"):
                    continue          # 只看表格行
                for claim in self._CLAIMS:
                    self.assertNotIn(claim, line, f"{name}:{i} 表格里还写着「{claim}」")


if __name__ == "__main__":
    unittest.main()


class AnchorLevelCrossUserGuard(unittest.TestCase):
    """跨用户撞车发生在**「哪件事」**这一层，不在「怎么说的」那一层。

    实测：同一个「刚忙完个案笔记」在 33 分钟里派了 3 次给 3 个不同的人，措辞是
    「刚忙完歇着 / 刚忙完窝着 / 刚忙完歇着」——文本相似度只有 0.02~0.06，
    比措辞的那道护栏（阈值 0.6）差了一个数量级，永远抓不到。

    所以比的是**由头原文**。不依赖任何阈值。
    """

    def test_same_event_kept_out_of_the_bot_pool(self):
        from social.state import SocialState
        st = SocialState(os.path.join(tempfile.mkdtemp(), "s.json"))
        now = time.time()
        for uid in ("a", "b", "c"):
            st.record_outgoing("bot1", uid, f"刚忙完歇着，随便说点什么 {uid}",
                              "greet", about="刚忙完个案笔记", why="", )
        used = st.anchor_recent_users("bot1", now, hours=2.0)
        self.assertIn("刚忙完个案笔记", used)
        self.assertEqual(sorted(used["刚忙完个案笔记"]), ["a", "b", "c"])

    def test_different_events_are_independent(self):
        from social.state import SocialState
        st = SocialState(os.path.join(tempfile.mkdtemp(), "s.json"))
        st.record_outgoing("bot1", "a", "甲", "greet", about="刚忙完写方案")
        st.record_outgoing("bot1", "b", "乙", "greet", about="去买了双袜子")
        used = st.anchor_recent_users("bot1", time.time(), hours=2.0)
        self.assertEqual(used.get("刚忙完写方案"), ["a"])
        self.assertEqual(used.get("去买了双袜子"), ["b"])

    def test_outside_the_window_forgets(self):
        from social.state import SocialState
        st = SocialState(os.path.join(tempfile.mkdtemp(), "s.json"))
        st.record_outgoing("bot1", "a", "甲", "greet", about="某件事",
                           )
        st.user("bot1", "a")["proactive_log"][0]["ts"] = time.time() - 10 * 3600
        self.assertEqual(st.anchor_recent_users("bot1", time.time(), hours=2.0), {})

    def test_unused_anchors_do_not_survive_prune(self):
        """**这不是 bug，是承重的设计**——但值得钉住，因为很容易被当 bug 再「修」一次。

        `prune_anchors` 对没用过的由头也套了「用满冷却期」的判据
        （`used_at=0` → `0 + 45天 > now` 恒为假），所以 `add_anchor` 刚挂上的由头，
        下一次 `live_anchors` 就已经看不到了。

        也就是说：**池子不攒东西，多样性全靠每轮重新派生。**
        这正是 7 天能跑 127 条的原因——池子改成「攒着」之后（上限 12），
        派生被池子里用过的堵死，送达从 127 掉到 19。
        """
        from social.anchors import add_anchor, load_anchors, prune_anchors
        now = time.time()
        u = {}
        add_anchor(u, KIND_DERIVED, "刚忙完个案笔记", now=now)
        self.assertEqual(len(load_anchors(u)), 1, "刚挂上时是在的")
        prune_anchors(u, now)
        self.assertEqual(len(load_anchors(u)), 0,
                         "没被 prune 掉的话派生会被堵死（实测送达 127 → 19）")

    def test_drop_marks_used_so_it_stops_being_re_derived(self):
        """撞车后这件事该被避开——靠的是标成「已用」进 `_remembered`。"""
        from social.anchors import (
            _remembered, add_anchor, drop_anchor, load_anchors,
        )
        now = time.time()
        u = {}
        add_anchor(u, KIND_DERIVED, "刚忙完个案笔记", now=now)
        drop_anchor(u, "刚忙完个案笔记", now)
        self.assertIn("刚忙完个案笔记", _remembered(u, now),
                      "没进 remembered → 下一轮还会重新派生同一件事")

    def test_engine_checks_the_event_not_the_wording(self):
        from social import engine as engine_mod
        src = Path(engine_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("anchor_recent_users", src, "事件级护栏没接上")
        self.assertIn("刚发给过别人", src)


class PressureGuard(unittest.TestCase):
    """拒「把自己的不安推给对方」。

    容器实测那条 110 字 4 句的主动消息，整条的问题不在长度而在**要债**：
    她因为对方没回而发消息，里面还有「快回我一句」。这条是情绪负担转嫁，
    不是她想说的话。
    """

    def test_debt_collector_is_blocked(self):
        from social.verify import verify_message
        for text in (
            "你一整晚都没回我消息，我都快熬不住了，一二三四点都数过来了。快回我一句吧。",
            "你今天一天都没理我，我这边一直在等。一、快回我一句；二、别装死；三、说句话。",
        ):
            ok, why = verify_message(text)
            self.assertFalse(ok, f"该拦：{text!r}")
            self.assertIn("推给对方", why)

    def test_urgent_is_not_the_same_as_debt(self):
        """真催、真生气、正常关心都该放行——只有「催」叠加「计较没回」才拒。"""
        from social.verify import verify_message
        for text in (
            "你今天一天没理我了，我有点想跟你说话，回我一句好不好",
            "你一晚上没回我，我有点担心，你还好吗",
            "回我！你现在立刻回我！",
            "快回我，我等不及了",
            "今天风好大，出门记得加件衣服",
        ):
            ok, why = verify_message(text)
            self.assertTrue(ok, f"不该拦：{text!r}（{why}）")
class BodyEnergyGate(unittest.TestCase):
    """「累」做成硬闸，**且必须落在所有开口的唯一收口上**。

    容器实测的 `veto_total=4` 全是模型否决，理由是「刚醒着腿还麻着，半梦半醒的
    只想歇着」——这类判断本是引擎该做的，而且模型每次给的理由都不一样，拦不稳。

    第一版把这条闸加在 `gate_reason` 里，看着对、单测也过，但仿真直接把它拆穿：
    「精力耗尽」场景仍送达 61 条。原因是 `gate_reason` 只管有由头那一路，
    问候/追问/回访/收场各有自己的检查，根本不经过它。**闸要开在 `_speak`。**

    引擎用仿真的 `Sim` 造，不再手搓 `__new__`/逐个补内部属性——手搓过一次，
    为了让 `_speak` 走通补了 `_hourly`、`state`、`_city_offset` 三个，是脆的：
    `_speak` 下次再加一个字段就读不到了。
    """

    def _speak(self, energy):
        import asyncio
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from sim_autonomous_social import FakePlatform, Sim, script_normal
        s = Sim("energy-gate", llm_script=script_normal, send_script=FakePlatform.OK,
                days=0.01)
        s.engine._night_windows = {"bot1": (0, 0)}
        s.engine.core = type("C", (), {"bot_self_state": staticmethod(
            lambda bid: {"energy": energy, "asleep": False})})()
        try:
            return asyncio.run(s.engine._speak(
                "bot1", "u1", s.engine.state.user("bot1", "u1"), 1.0, s.clock,
            ))
        finally:
            s.cleanup()

    def test_exhausted_sends_nothing(self):
        sent, why = self._speak(12)
        self.assertFalse(sent, "累到 12 还发得出去，这道闸等于没有")
        self.assertIn("太累", why)

    def test_tired_but_standing_is_not_blocked_by_energy(self):
        """「有点倦」不该拦。这条不是断言她一定发得出去（那还要看别的条件），
        是断言**精力这道闸没拦**。"""
        _sent, why = self._speak(58)
        self.assertNotIn("太累", why)

    def test_missing_energy_is_treated_as_fine(self):
        """拿不到身体数据时按「不拦」处理——老版 Core 没这个字段，
        按累处理会让所有老用户一夜之间全哑。"""
        _sent, why = self._speak(None)
        self.assertNotIn("太累", why)


class EngineInitIsComplete(unittest.TestCase):
    """`__init__` 必须跑完——不是风格问题，是会静默半初始化的坑。

    v1.24.2 加 per-user 锁时，把 `_user_lock` 这个方法定义误插到了 `__init__`
    中间：`__init__` 从那一刻起就等于结束了，后面 `self.state` / `self.signals` /
    `self.generator` 全成了 `_user_lock` 的死代码。构造出的对象**不报错**，
    只是一路缺属性，等到真正跑起来才炸。

    这条测试盯的是「构造完的引擎该有的都在」，以后再有人往 `__init__` 中间插
    方法定义，这里立刻红。
    """

    def test_all_dependencies_are_wired(self):
        import tempfile
        from social.engine import SocialEngine
        from social.config import SocialConfig
        tmp = tempfile.mkdtemp()
        eng = SocialEngine(
            context=None, config=SocialConfig(),
            state_path=str(Path(tmp) / "s.json"), data_dir=tmp,
        )
        for attr in ("state", "core", "signals", "generator", "_user_locks"):
            self.assertTrue(hasattr(eng, attr), f"SocialEngine 构造完缺 {attr}")
        self.assertIsInstance(eng._user_locks, dict)


class PerUserContextDiffers(unittest.TestCase):
    """同一件事，对不同的人要能说出不同的话。

    v1.23 加了跨用户护栏、v1.24.1 换了比对对象（由头原文而不是措辞），**都是在
    派出去之后拦住**。根因在更前面：同一个人设、同一个模型，如果 prompt 里没有
    per-user 差异，输出就逐字相同——护栏拦得再准也只是在事后补救。

    真实表现：41 分钟里 12 个不同的人收到逐字相同的「刚忙完个案笔记」。那不是复读，
    是 12 个人读了同一行字幕。

    关系描述在拿不到 Core 情绪档案时只会给一句固定的「关系未知」；而当时这 12 个人
    恰好都没建档，于是 prompt 完全一致。补的三条 per-user 信号（在意度、聊过多少、
    回话快慢）都是从插件自己的账本里算的，不依赖 Core 有没有建档。
    """

    def _line(self, **kw):
        from social.generator import _relationship_line
        return _relationship_line(kw)

    def test_twelve_users_do_not_share_one_line(self):
        """真实分布的 12 个陌生人，应该拿到各不相同的上下文。

        这条不许调成「至少 2 种」——那等于承认群发是可接受的。真实分布下 12 个人
        只有 1 种说法，正是这个洞。
        """
        import random
        random.seed(7)
        outs = {
            self._line(
                _interest=0.44 + random.random() * 0.08,
                _message_count=25 + random.randint(0, 30),
                _avg_reply_seconds=random.choice([30, 80, 300, 1200, 5000, 20000]),
            )
            for _ in range(12)
        }
        # 门槛从 8 降到「不是 1 种」。原先钉 8 是为了逼出足够细的区分度，
        # 但那条路走成了「把关系拆成三条并列事实塞进提示词」，于是模型把提示词
        # 里的东西一条条念出来——发的消息变成「今天吃的好饱，好想你啊」这种
        # 三段并列的台词。**区分度不是靠多给事实换来的，是靠同一槽位多几种说法。**
        # 现在 12 个人拿到 4~6 种（每条都只有一句），比 1 种（群发）好，
        # 又不像 3 条并列那样把人写成报菜名。
        self.assertGreaterEqual(len(outs), 3,
                                f"12 个不同的人只拿到 {len(outs)} 种说法，仍然接近群发")

    def test_it_is_always_one_sentence_not_three_facts(self):
        """**一条**关系定位，不是三条并列事实。

        这条是回退出来的。v1.25.0 把「在意度 / 聊过多少 / 回话快慢」拆成三个
        独立陈述塞进提示词，差异化确实上来了（12 个人 11 种说法），**但模型手上
        于是有了三条可复述的事实**，和日程、精力、时段摆在一起时它一条条念出来：

            宝宝摸摸头，今天吃的好饱，好想你啊

        三段并列，每段对应一条事实，读着像念台词。**把「太散」倒成了「太齐」**——
        以前她不知道该说什么，现在知道得太多、于是照着说。
        """
        for kw in ({'_interest': 0.55, '_message_count': 42, '_avg_reply_seconds': 300},
                   {'_interest': 0.85, '_message_count': 300, '_avg_reply_seconds': 20},
                   {'_message_count': 1}):
            body = self._line(**kw).splitlines()[-1]
            self.assertLessEqual(body.count("；"), 1,
                                 f"关系行被拆成了并列事实：{body}")

    def test_no_recitable_numbers_leak_in(self):
        """**不出现可复述的数字。** 模型会把提示词里的数字当内容念出来。"""
        self.assertNotIn("84", self._line(_message_count=84, _interest=0.55))

    def test_no_gap_leaves_a_user_with_no_relationship_at_all(self):
        """分档是 `>=` 比较而在意度是连续量——0.4475 这种「离档线差一点点」的
        值会从所有分支之间漏下去，于是那个人一句关系定位都拿不到。

        而拿不到是最糟的：提示词里没有关系信息时，模型退回按日程和精力说话，
        那正是「12 个人收到同一句话」的成因。宁可给一句泛的。
        """
        for i in range(200):
            body = self._line(_interest=i / 200.0 * 1.2, _message_count=i,
                              _avg_reply_seconds=[0, 30, 300, 5000][i % 4])
            self.assertTrue(body.strip(), f"在意度 {i / 200.0 * 1.2:.3f} 时一句都没给")

    def test_never_leaks_a_bare_number_as_status_report(self):
        """在意度不能以数字形式进 prompt——模型只会照着复述，变成状态播报。"""
        line = self._line(_interest=0.618, _message_count=30)
        self.assertNotIn("0.618", line)
        self.assertNotIn("0.62", line)


class BlockedReasonsAreCounted(unittest.TestCase):
    """「没发出去」要能按理由查。

    以前九道闸各返回一句中文、只逐人 log 一行，指标里只有 `veto_total` /
    `rejected_total` 两个总数。跑完一轮一条都没发出去时，从指标里看不出
    **是哪道防线在起作用**——阈值只能凭感觉调，改完也不知道有没有用。

    `blocked_reasons` 这个字段从 v1.x 就声明在初始 metrics 里，注释写着
    「各闸门各拦下多少」，但从来没有代码写过它。这条用例盯住它不再变回空壳。
    """

    def _eng(self):
        import tempfile
        from social.engine import SocialEngine
        from social.config import SocialConfig
        tmp = tempfile.mkdtemp()
        return SocialEngine(context=None, config=SocialConfig(),
                            state_path=str(Path(tmp) / "s.json"), data_dir=tmp)

    def test_every_gate_maps_to_a_stable_category(self):
        from social.engine import SocialEngine as E
        cases = {
            "她正在睡觉": "她在睡",
            "她太累了（精力 12，坐不住也不想说话）": "她太累",
            "刚聊上，不插话": "刚聊上",
            "不久前才说过话，再另起一句很突兀": "刚聊过",
            "上一条主动发的还没回，现在再发就是追着说": "没被接住",
            "护栏冷却未过": "冷却未过",
            "刚想过一次决定不说，缓缓": "刚跳过",
            "TA 说过要去睡了，这时发过去不合适": "TA要睡了",
            "按TA 平时的作息，这个点 TA 不玩手机": "作息时间",
            "现在是安静时段": "安静时段",
        }
        for reason, want in cases.items():
            self.assertEqual(E._block_category(reason), want, f"「{reason}」归类错了")

    def test_unknown_reason_still_shows_up(self):
        """新加的闸如果忘了登记，也不能静默丢掉——落到「其它」里。"""
        from social.engine import SocialEngine as E
        self.assertEqual(E._block_category("某个还没登记的新理由"), "其它")
        self.assertEqual(E._block_category(""), "未说明")

    def test_counting_accumulates_into_the_existing_field(self):
        eng = self._eng()
        eng._count_block("刚聊上，不插话")
        eng._count_block("刚聊上，不插话")
        eng._count_block("现在是安静时段")
        self.assertEqual(eng._m["blocked_reasons"], {"刚聊上": 2, "安静时段": 1})

    def test_the_table_actually_reaches_the_status_text(self):
        """计数得真能在状态里看见——写在 `self._m` 里不等于用户看得到。

        踩过：`_metrics_text` 在「后台循环还没跑过第一轮」时提前 return，而测试
        造的是没跑过心跳的引擎，于是断言「有计数」通过、实际那段文字一次都没出现。
        """
        import time
        eng = self._eng()
        eng._m["last_heartbeat"] = time.time() - 300
        eng._m["last_sent"] = time.time() - 60
        eng._count_block("刚聊上，不插话")
        eng._count_block("刚聊上，不插话")
        eng._count_block("某个还没登记的新理由")
        text = eng._metrics_text()
        self.assertIn("没发出去的去向", text)
        self.assertIn("刚聊上：2 次", text)
        self.assertIn("其它：1 次", text, "未登记的理由也不该被丢掉")

    def test_counters_survive_a_restart(self):
        """累计语义。`set_runtime_metrics` 一直只写不读，重启一次「累计 37 次」
        就变成了「这一轮 37 次」，而界面上写的是累计。"""
        import tempfile
        from social.engine import SocialEngine
        from social.config import SocialConfig
        tmp = tempfile.mkdtemp()
        path = str(Path(tmp) / "s.json")

        first = SocialEngine(context=None, config=SocialConfig(), state_path=path,
                             data_dir=tmp)
        first._count_block("现在是安静时段")
        first._count_block("护栏冷却未过")
        first._m["sent_total"] = 5
        first._persist_metrics()

        second = SocialEngine(context=None, config=SocialConfig(), state_path=path,
                              data_dir=tmp)
        self.assertEqual(second._m["blocked_reasons"], {"安静时段": 1, "冷却未过": 1},
                         "重启后累计归零 = 界面上那个「累计」是假的")
        self.assertEqual(second._m["sent_total"], 5)


class MainUserIsActuallyServed(unittest.TestCase):
    """好感最高的那个人，一天下来不能一条都收不到。

    这是从容器反馈来的：**好感最高的那位基本收不到消息，也聊不起来。**
    两个结构原因，都不是「阈值调错了」那种。

    一、选人不看好感。排序只有 `(last_sent 升序)`，每轮 3 条、队列 176 个人，
       于是她和陌生人在同一个队列里排队。她算得出好感，算完却不用在选人上。

    二、主通道的配额被吃光。发送顺序是 线程 → 回访 → 问候 → 另起话题 → …，
       而另起话题**排第 4**。前三条各有一个名额就吃满 3 条，于是「她想找某个人
       说话」整轮一条都发不出。那恰恰是关系最亲的人唯一会走的通路——问候只有
       早/晚两个时间窗。
    """

    def test_relation_buckets_are_ordered_by_affection(self):
        from social.engine import SocialEngine as E
        self.assertLess(E._relation_bucket({"interest": 0.10}),
                        E._relation_bucket({"interest": 0.95}),
                        "好感高的人必须在更靠前的档")

    def test_bucket_falls_back_when_interest_is_missing(self):
        from social.engine import SocialEngine as E
        self.assertEqual(E._relation_bucket({}), E._relation_bucket({"interest": 0.35}),
                         "没有 interest 时按中性算，不能当成最低档")

    def test_high_affection_beats_a_longer_silence(self):
        """好感高的人不该因为「刚聊过不久」就被排到 176 人队尾。"""
        from social.engine import SocialEngine as E
        main = {"interest": 0.95, "last_sent": 3600.0}
        stranger = {"interest": 0.12, "last_sent": 0.0}
        order = sorted([("陌生人", stranger), ("我", main)],
                       key=lambda kv: (-E._relation_bucket(kv[1]), float(kv[1]["last_sent"])))
        self.assertEqual(order[0][0], "我",
                         "刚聊过一小时的好友被排在从没聊过的陌生人后面")

    def test_within_a_bucket_it_is_still_rotation(self):
        """同档内仍按 last_sent 轮转——不然就回到「6 个人霸占」那个老问题。"""
        from social.engine import SocialEngine as E
        a = {"interest": 0.50, "last_sent": 100.0}
        b = {"interest": 0.51, "last_sent": 900.0}
        order = sorted([("a", a), ("b", b)],
                       key=lambda kv: (-E._relation_bucket(kv[1]), float(kv[1]["last_sent"])))
        self.assertEqual(order[0][0], "a", "同档里久没联系的先来，这就是轮转")

    def test_anchored_channel_gets_a_reserved_slot(self):
        """「另起话题」必须拿到至少一条，否则主通道整轮哑火。"""
        import inspect
        from social.engine import SocialEngine
        src = inspect.getsource(SocialEngine.try_once)
        # 必须是 `self.`。写成裸名的话单测全绿（单测调的是类方法，走类作用域），
        # 而 `try_once` 里走的是局部作用域 —— 仿真一跑就是
        # NameError: name '_relation_bucket' is not defined。
        # 同一个坑踩过两次：v2.25.0 往 `__init__` 中间插方法定义，把后半截截断了。
        self.assertIn("self._relation_bucket(x[2])", src,
                      "裸名在 try_once 的作用域里取不到，单测会假绿")
        i_anchor = src.index("for bid, uid, u, urge, a in anchored:")
        before = src[:i_anchor]
        self.assertIn("budget[_b] -= 1", before,
                      "让出配额的代码必须在 anchored 之前——它抢不到就是白写")
        self.assertIn("budget[_b] > 1", before,
                      "只在还有富余时才让一条，不能把配额减到 0")
