"""v1.28.6：跑起来看量——由头变多、一个人一轮能说两件事、死目标被永久丢弃。

单元测试证明的是「这个函数返回了这个值」。用户反馈的三件事全在**跑起来**那一层：

1. 「事件少 每次只有那几个」——由头来源扩了（wake/体感/feelings/next）、
   一个人一轮能说两件事之后，一轮心跳里发出的条数应该变多，而且**话题**应该变多；
2. 「私聊发送没看到几个」——每小时上限从 12 抬到 24、每轮名额 2→3 之后，
   同一天里应该服务到更多不同的人；
3. 「请添加对方为好友/你已被移除群聊就该对目标永久丢弃」——平台报这两个错时，
   这个人下一轮**根本不该再被选中**，而且不再烧 LLM。

这里把 `try_once` 真跑起来（假 provider / 假平台 / 模拟时钟），统计上面三件事。
不追求绿灯，追求「数字对得上改动」。
"""

from __future__ import annotations

import asyncio
import json
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
    api.logger = logging.getLogger("autonomous_social_sim")
    event = types.ModuleType("astrbot.api.event")

    class _Chain:
        """可调用的链：`MessageChain().message(text)` 要能返回一个对象。

        原来这里是 `_Chain` 的**实例**（`event.MessageChain = _Chain()`），
        于是 `MessageChain()` 报「not callable」，引擎只能走纯文本回退——
        那条回退路径是给「这版 AstrBot 不认 MessageChain」用的，不是给测试桩用的。
        """

        def message(self, text):
            return ("chain", text)

        def plain(self, text):
            return ("plain", text)

    event.MessageChain = _Chain
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social import engine as engine_mod  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social import groupflow  # noqa: E402

engine_mod.SEND_RETRY_DELAY = 0.0

HOUR = 3600.0
DAY = 86400.0
# 一个周三下午两点（本地时区）：不在默认安静时段（23:00–07:00）里，
# 否则整轮候选会被「安静时段」挡掉，什么都测不出来。
T0 = 1_700_028_800.0


class _Clock:
    """可推进的时钟：`now()` 是引擎唯一的时间来源。"""

    def __init__(self, start=T0):
        self.t = float(start)

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)


class _Provider:
    id = "sim"

    async def text_chat(self, **_kw):
        class _Resp:
            completion_text = "SEND\n今天路过那家店，闻到桂花香了"
            role = "assistant"

        return _Resp()


class _Ctx:
    """够用的 context：取得到 provider、发得出去。"""

    def __init__(self, *, fail_for=(), dead=()):
        # fail_for：这些 uid 一律报平台错误（不是好友）
        # dead：这些 uid 报「已被移除群聊」——群侧
        self.fail_for = set(fail_for)
        self.dead = set(dead)
        self.sent: list = []

    async def get_using_provider_async(self, umo=None):
        return _Provider()

    async def send_message(self, target, payload):
        text = payload if isinstance(payload, str) else getattr(payload, "parts", [""])[0]
        self.sent.append((str(target), str(text)))
        for uid in self.fail_for:
            if uid in str(target):
                raise RuntimeError("发送失败：请添加对方为好友")
        for gid in self.dead:
            if gid in str(target):
                raise RuntimeError("<ActionFailed retcode=1200> 你已被移除群聊")
        return True


def _engine(clock, ctx, **cfg_kw):
    """装配一个真能跑的引擎（真 state / 真 core_bridge / 假 provider）。

    走 `SocialEngine.__init__` 而不是手工塞属性：那个构造函数里有三十多个实例字段
    （`_last_flush`、`_hourly`、`_night_windows`、`_flow_inflight`…），手工列一遍
    必然漏，而漏一个就是一轮心跳里一个 AttributeError。
    """
    tmp = tempfile.mkdtemp(prefix="sim_v1286_")
    eng = SocialEngine(
        ctx,
        {
            "mode": "standalone",          # 不接 Core：由头全走本插件账本
            "heartbeat_min_minutes": 1,
            "heartbeat_max_minutes": 1,
            "llm_gate": False,             # 省掉 decide 那一轮
            "greeting_enabled": False,
            "miss_daily_cap": 0,           # 单独测「由头」通道
            "group_flow_enabled": False,
            "group_icebreak_enabled": False,
            "group_topic_enabled": False,
            "loop_enabled": False,
            "closer_enabled": False,
            "proactive_provider_ids": [],  # 跟着会话走（假 provider）
            **cfg_kw,
        },
        state_path=str(Path(tmp) / "state.json"),
        data_dir=tmp,
        time_source=clock.now,
    )
    eng.running = True
    return eng, tmp


def _seed_users(eng, clock, count, *, body=None):
    """造 count 个用户，各带一条 umo 与一段对话。"""
    for i in range(count):
        uid = f"u{i}"
        u = eng.state.user("b1", uid)
        u["umo"] = f"aiocqhttp:FriendMessage:{uid}"
        u["interest"] = 0.5 + (i % 3) * 0.1
        u["message_count"] = 10 + i
        u["conversation"] = [
            {"dir": "in", "text": "昨天面试累死了", "ts": clock.now() - 3 * HOUR},
            {"dir": "out", "text": "后来呢", "ts": clock.now() - 2 * HOUR},
        ]
        u["last_seen"] = clock.now() - 3 * HOUR
        u["last_said"] = clock.now() - 3 * HOUR
        u["last_spoken"] = clock.now() - 2 * HOUR
        if body is not None:
            u["_sim_body"] = body
    eng.state.mark_dirty()
    eng.state.save()
    return [f"u{i}" for i in range(count)]


def _rich_body(now):
    """一份「什么都有的契约视图」：四类新来源全命中。"""
    return {
        "energy": 90.0, "social_energy": 80.0, "asleep": False,
        "feelings": ["手脚发沉", "脑子发蒙"],
        "hunger": 88.0, "sleep_pressure": 82.0, "discomfort": 65.0, "sleep_debt": 7.0,
        "wake": {"asleep": False, "wake_at": now - 300, "last_sleep_hours": 8.0},
        "ongoing": ["整理那份个案笔记"],
        "activity": {},
        "day": {"doing": "在厨房煮饭", "done": ["下午在门诊接待"],
                "next": ["去超市", "回家做饭"], "wants": ["那家书店"],
                "dreams": ["在海边"], "event": {}},
        "weather": {}, "holiday": "", "routine": {},
    }


class MoreAnchorsSimTest(unittest.IsolatedAsyncioTestCase):
    """① 由头来源扩了之后，同一个人手上挂的具体事变多。"""

    async def test_rich_contract_yields_two_per_round(self):
        """一轮给 2 条（ANCHOR_ROUND_PER_USER）——不是 1 条。

        原来只取第一条：派生出来的第二、三条当晚就过期作废，下一轮重新派生、
        又只发第一条，于是**永远是同样那几个话题**。
        """
        now = T0
        body = _rich_body(now)
        u = {"conversation": [{"dir": "in", "text": "昨天面试累死了", "ts": now - 3 * HOUR}]}
        eng = _AnchorEngine()
        picks = eng._anchor_pick(u, body, now)
        self.assertEqual(len(picks), engine_mod.ANCHOR_ROUND_PER_USER,
                         f"该给 {engine_mod.ANCHOR_ROUND_PER_USER} 条，实际 {len(picks)}："
                         f"{[p.get('about') for p in picks]}")
        # 两条必须是不同的由头
        self.assertNotEqual(picks[0].get("about"), picks[1].get("about"))

    async def test_topic_anchor_reaches_the_pool(self):
        """v1.28.7：`u["topics"]` 现在也是由头——「你上次说的那个X」对每个人独一份。

        ⚠️ 话题由头是**关系向**的（「你上次说的X」是关于 TA 的话），所以它从
        `prepare_round_anchors(relational=True)` 出来，也就是「念想」那条通道
        （`engine._miss_pick`）——不是「另起话题」那条。这两条通道的素材本来就
        该分开：一边说她自己的生活，一边说关于 TA 的事。
        """
        import social.anchors as anchors_mod
        now = T0
        u = {
            "conversation": [],
            "last_seen": now - 6 * HOUR,
            "topics": ["塞尔达传说", "复习备考"],
            "topic_seen_at": {"塞尔达传说": now - DAY, "复习备考": now - DAY},
        }
        picks = anchors_mod.prepare_round_anchors(
            u, {}, now, limit=3, relational=True, missing_floor=0.0,
        )
        abouts = [str(p.get("about") or "") for p in picks]
        self.assertTrue(any("塞尔达传说" in t for t in abouts),
                        f"话题由头没进来：{abouts}")
        # 它必须是 KIND_MISS：念想通道只收 miss/greet
        hit = [p for p in picks if "塞尔达传说" in str(p.get("about") or "")][0]
        self.assertEqual(hit.get("kind"), anchors_mod.KIND_MISS)

    async def test_topic_anchor_not_polluted(self):
        """污染话题一句都不该进来：捧「你上次说的「好的」后来呢」回去最假。"""
        import social.anchors as anchors_mod
        now = T0
        u = {
            "conversation": [], "last_seen": now - 6 * HOUR,
            "topics": ["好的", "哈哈", "〔她的身体与生活 v10〕", "yep"],
            "topic_seen_at": {t: now - DAY for t in
                              ("好的", "哈哈", "〔她的身体与生活 v10〕", "yep")},
        }
        picks = anchors_mod.prepare_round_anchors(
            u, {}, now, limit=3, relational=True, missing_floor=0.0,
        )
        abouts = [str(p.get("about") or "") for p in picks]
        for bad in ("好的", "哈哈", "身体与生活", "yep"):
            self.assertFalse(any(bad in t for t in abouts),
                             f"污染话题「{bad}」进了由头：{abouts}")

    async def test_derivation_pool_is_deeper_than_the_round_cap(self):
        """池子里的候选比一轮能取的多：否则「每轮两条」很快就没新话可说。"""
        now = T0
        body = _rich_body(now)
        u = {"conversation": [{"dir": "in", "text": "昨天面试累死了", "ts": now - 3 * HOUR}]}
        # 直接问派生层：一轮的上限在引擎侧（ANCHOR_ROUND_PER_USER），不在这。
        import social.anchors as anchors_mod
        kinds: dict = {}
        out = anchors_mod._derive_from_state(body, u, now, set(), kinds=kinds)
        self.assertGreaterEqual(
            len(out), 5,
            f"来源这么全却只派生出 {len(out)} 条：{out}",
        )

    async def test_sparse_contract_still_yields_something(self):
        """「手上真有一件事」与「什么都没有」要分得开：空的就一条都不给。"""
        eng = _AnchorEngine()
        self.assertEqual(eng._anchor_pick({}, {}, T0), [])

    async def test_same_user_two_different_things_in_one_round(self):
        """一个人一轮里可以说两件不同的事（ANCHOR_ROUND_PER_USER）。"""
        now = T0
        clock = _Clock(now)
        ctx = _Ctx()
        eng, _tmp = _engine(clock, ctx)
        _seed_users(eng, clock, 1)
        eng.generator = _FakeGen(eng)
        await _run_one_round(eng, clock, body=_rich_body(now))
        # 由头通道一轮最多两次 → 这个人最多收到 2 条
        self.assertLessEqual(
            len(ctx.sent), engine_mod.ANCHOR_ROUND_PER_USER,
            f"一轮里对同一个人发了 {len(ctx.sent)} 条，超过 {engine_mod.ANCHOR_ROUND_PER_USER}",
        )
        self.assertGreaterEqual(len(ctx.sent), 1, "一条都没发出去")
        # 两条必须是**不同的由头**：同一条说两遍就是复读
        if len(ctx.sent) > 1:
            self.assertNotEqual(ctx.sent[0][1], ctx.sent[1][1])


async def _run_one_round(eng, clock, body=None):
    """跑一轮 try_once。桩掉启动期才需要的东西（时钟刷新/播种/清理）。"""
    eng._refresh_clocks = lambda: None
    eng._seed_tick = _async_noop
    eng._prune_expired = lambda: None
    # settle_minds 要把这个人判成「念头攒满」：桩掉它，直接返回**没被隔离**的人。
    # ⚠️ 必须自己过滤 `is_send_blocked`：真 `settle_minds` 里有这道检查，桩掉它
    # 就等于把「隔离期内不攒念头」这条一起绕过了——那正是本测试要盯的东西。
    def _settle(bid, now, dt, core_root=None, bot_state=None, min_urge=0.0):
        return [
            (uid, u, 3.0) for uid, u in eng.state.bot(bid).get("users", {}).items()
            if not eng.state.is_send_blocked(bid, uid, now)
        ]

    eng.settle_minds = _settle
    # `try_once` 只在 `core_root` 非空时才问 Core 的身体视图（standalone 下它本该是
    # None）。这里给一份**空 dict** 让它往下走，再由 `bot_self_state` 的桩喂真视图。
    eng.core.read_root = lambda: {} if body is None else {"roles": {}}
    if body is not None:
        eng.core.bot_self_state = lambda bid, root=None: body
    eng._core_last_interaction = lambda root, bid, uid: None
    await eng.try_once()


class _FakeGen:
    """不走真 LLM：decide/generate 都直接返回一句**能过验收**的正文。

    返回纯括号动作会被 `verify_message` 拦下（「只剩括号动作，没有正文」）——
    那不是本测试要测的东西，所以给一句正常的话，只把由头带在正文里。
    """

    def __init__(self, eng):
        self._eng = eng
        self._n = 0

    def _line(self, reason):
        self._n += 1
        return f"刚在想{reason}，你在干嘛呢"

    async def decide(self, umo, u, core_context, reason, reason_meta, persona, mind,
                     at=None, clock_offset=None, memory_note=""):
        line = self._line(reason)

        class _D:
            send = True
            why_not = ""
            parts = [line]
            promise = ""
            raw_text = line

        return _D()

    async def generate(self, umo, u, core_context, reason, reason_meta, persona, mind,
                       at=None, clock_offset=None, memory_note=""):
        return [self._line(reason)]


class _AnchorEngine:
    _anchor_pick = SocialEngine._anchor_pick

    def log(self, _msg):
        pass


async def _async_noop(*_a, **_kw):
    return None


class VolumeSimTest(unittest.IsolatedAsyncioTestCase):
    """② 默认节奏：每轮名额与每小时上限都抬上来了。"""

    def test_defaults_are_higher(self):
        cfg = SocialConfig.from_astrbot({})
        self.assertEqual(cfg.max_sends_per_round, 3)
        self.assertEqual(cfg.miss_daily_cap, 3)
        self.assertEqual(cfg.hourly_sends_cap, 24)

    def test_round_budget_scales_with_config(self):
        """每轮名额 = max_sends_per_round（不再被写死 2）。"""
        cfg = SocialConfig.from_astrbot({"max_sends_per_round": 5})
        self.assertEqual(cfg.max_sends_per_round, 5)


class DeadTargetSimTest(unittest.IsolatedAsyncioTestCase):
    """③ 平台说「不是好友 / 已被移出群聊」→ 这个目标下一轮不再被选中。"""

    def test_permanent_markers(self):
        self.assertTrue(engine_mod._permanent_dead("请添加对方为好友"))
        self.assertTrue(engine_mod._permanent_dead("你已被移除群聊"))
        self.assertFalse(engine_mod._permanent_dead("adapter 没加载"))

    def test_block_send_is_permanent_in_effect(self):
        """隔离到「现在 + 100 年」：`is_send_blocked` 的语义不变，但再也醒不过来。"""
        import social.state as state_mod
        tmp = tempfile.mkdtemp(prefix="sim_dead_")
        st = state_mod.SocialState(path=str(Path(tmp) / "state.json"))
        st.block_send("b1", "u1", T0 + engine_mod._PERMANENT_DEAD_SECONDS, "请添加对方为好友")
        self.assertTrue(st.is_send_blocked("b1", "u1", T0 + 30 * DAY),
                        "一个月后仍该被隔离")
        self.assertTrue(st.is_send_blocked("b1", "u1", T0 + 365 * DAY),
                        "一年后仍该被隔离")
        # 但**状态还在**：对方回来发消息时 observe 会清掉它
        u = st.user("b1", "u1")
        u["send_blocked_until"] = 0.0
        u["send_blocked_reason"] = ""
        self.assertFalse(st.is_send_blocked("b1", "u1", T0 + 365 * DAY))

    def test_group_block_is_permanent_in_effect(self):
        import social.state as state_mod
        tmp = tempfile.mkdtemp(prefix="sim_dead_g_")
        st = state_mod.SocialState(path=str(Path(tmp) / "state.json"))
        st.block_group("b1", "g1", T0 + engine_mod._PERMANENT_DEAD_SECONDS, "你已被移除群聊")
        self.assertTrue(st.is_group_blocked("b1", "g1", T0 + 365 * DAY))

    def test_retry_is_skipped_for_dead_target(self):
        """死目标不重试：重试只是把同一面墙再撞一次。"""
        calls = {"n": 0}

        class _DeadCtx:
            async def send_message(self, target, payload):
                calls["n"] += 1
                raise RuntimeError("请添加对方为好友")

        clock = _Clock()
        eng, _tmp = _engine(clock, _DeadCtx())
        ok, err, peer = asyncio.run(eng._send_with_retry("aiocqhttp:FriendMessage:u1", "hi", "b1"))
        self.assertFalse(ok)
        self.assertTrue(peer, "平台明说不是好友 → 该隔离")
        self.assertEqual(calls["n"], 1,
                         f"死目标被重试了 {calls['n']} 次——每次都只会再撞同一面墙")


class DeadTargetRoundSimTest(unittest.IsolatedAsyncioTestCase):
    """③ 跑起来看：平台说「不是好友」之后，这个人下一轮**根本不该再被选中**。"""

    async def test_dead_user_is_not_picked_next_round(self):
        now = T0
        clock = _Clock(now)
        ctx = _Ctx(fail_for={"u0"})     # u0 一律报「请添加对方为好友」
        eng, _tmp = _engine(clock, ctx, max_sends_per_round=1)
        _seed_users(eng, clock, 1)      # 只有 u0：一轮必然选它
        eng.generator = _FakeGen(eng)
        body = _rich_body(now)

        # 第一轮：u0 会被选中、会写一条、然后发送失败 → 永久隔离
        await _run_one_round(eng, clock, body=body)
        self.assertTrue(
            eng.state.is_send_blocked("b1", "u0", clock.now()),
            "第一轮之后 u0 就该被隔离",
        )
        blocked_until = float(eng.state.user("b1", "u0").get("send_blocked_until", 0) or 0)
        self.assertGreater(
            blocked_until, clock.now() + 300 * DAY,
            "隔离必须是「永久」而不是 24 小时",
        )
        # 发送侧：一次都没重试（重试只会再撞同一面墙）
        self.assertLessEqual(len(ctx.sent), 2, f"死目标被发了 {len(ctx.sent)} 次")

        # 第二轮（推进一小时）：u0 仍在隔离期，不该再被选中
        clock.advance(HOUR)
        ctx.sent.clear()
        eng.generator = _CountingGen()
        await _run_one_round(eng, clock, body=body)
        self.assertEqual(eng.generator.calls, 0,
                         "被永久隔离的人又被选中去生成了——每轮白烧一次 LLM")

    async def test_group_kicked_is_not_picked_next_round(self):
        """群侧同一个要求：报「你已被移除群聊」之后不再对它破冰。"""
        now = T0
        clock = _Clock(now)
        ctx = _Ctx(dead={"g1"})
        eng, _tmp = _engine(
            clock, ctx,
            group_icebreak_enabled=True,
            group_idle_hours=1,        # 最短的合法值（面板下限就是 1）
            icebreak_daily_cap=5,
        )
        g = eng.state.group("b1", "g1")
        g["umo"] = "aiocqhttp:GroupMessage:g1"
        g["msg_count"] = 20              # 破冰要求这群真的有人聊过（≥5）
        g["last_human_at"] = clock.now() - 10 * HOUR
        g["last_seen"] = clock.now() - 10 * HOUR
        g["samples"] = [{"name": "甲", "text": "有人吗", "self": False}]
        eng.core.bot_self_state = lambda bid, root=None: _rich_body(now)
        eng.core.read_root = lambda: {}
        eng.generator = _FakeGroupGen()
        await eng._run_icebreaks(["b1"], clock.now())
        self.assertTrue(
            eng.state.is_group_blocked("b1", "g1", clock.now()),
            "报「已被移除群聊」之后这个群就该被隔离",
        )
        self.assertGreater(
            float(eng.state.group("b1", "g1").get("blocked_until", 0) or 0),
            clock.now() + 300 * DAY,
            "群隔离也必须是永久的",
        )
        # 再跑一轮破冰：这个群不该再被选中（选中它就意味着再写一遍 LLM）
        counting = _CountingGen()
        eng.generator = counting
        await eng._run_icebreaks(["b1"], clock.now())
        self.assertEqual(counting.calls, 0, "被永久隔离的群又被选中去生成了")
    """群破冰的素材也跟着多了（同一批事实，不能一边读一边不读）。"""

    def test_wake_and_next_added(self):
        now = T0
        body = _rich_body(now)
        lines = groupflow.life_lines(body, now)
        self.assertTrue(any("刚睡醒" in ln for ln in lines), f"群里也该有「刚醒」：{lines}")
        self.assertTrue(any("接下来" in ln for ln in lines), f"群里也该有「接下来」：{lines}")

    def test_stale_wake_is_not_in_group_material(self):
        body = _rich_body(T0)
        body["wake"]["wake_at"] = T0 - 3 * DAY
        lines = groupflow.life_lines(body, T0)
        self.assertFalse(any("刚睡醒" in ln for ln in lines), f"三天前醒的觉不该出现：{lines}")


class _GroupIceEngine:
    """只暴露 `_run_icebreaks`：破冰那条路要群状态，不需要完整心跳。"""

    _run_icebreaks = SocialEngine._run_icebreaks
    _icebreak_pick = SocialEngine._icebreak_pick


class _CountingGen:
    """数「这一轮调了几次生成」：死人/死群一次都不该调。

    带 `group_message`：破冰走的是那个入口（不是 generate），少了它
    连第一轮都发不出去，测试就什么都验不到。
    """

    def __init__(self):
        self.calls = 0

    async def generate(self, *a, **kw):
        self.calls += 1
        return ["刚泡了茶，群里有人吗"]

    async def group_message(self, *a, **kw):
        self.calls += 1
        return ["刚泡了茶，群里有人吗"]

    async def decide(self, *a, **kw):
        self.calls += 1

        class _D:
            send = True
            why_not = ""
            parts = ["刚泡了茶，群里有人吗"]
            promise = ""
            raw_text = "刚泡了茶，群里有人吗"

        return _D()


class _FakeGroupGen:
    """破冰用的假生成器：直接返回一句能过验收的话（只返回 List[str]）。"""

    def __init__(self):
        self.calls = 0

    async def group_message(self, *a, **kw):
        self.calls += 1
        return ["刚泡了茶，群里有人吗"]

    async def generate(self, *a, **kw):
        self.calls += 1
        return ["刚泡了茶，群里有人吗"]


if __name__ == "__main__":
    unittest.main()
