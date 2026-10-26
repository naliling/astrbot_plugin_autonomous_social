"""v1.28.9：接话（`continue`）——她说完之后对方就没声了。

用户原话：「用户发完、bot 回完、用户不说话后……这种情况应当判断要不要接话/问对方
干啥去了，而不是被回避逻辑挡住」。

原来的死路有两条，任何一条单独存在都会让这条路径永不成立：

1. `threads.thread_reason` 在「`spoken >= seen` 且沉默超过 presence_after」处直接
   `return ""` —— 而主链路一回复，最后一条永远是她的，于是「她回完、对方消失」
   连一次都进不来；
2. 就算进得来，`followup_max`（默认 70 分钟）也会把它截掉，而真人隔两三个小时
   问一句「你人呢」才是最自然的时机。

本文件盯五件事：

1. `thread_reason` 的 continue 分支与七类边界（2 小时→接话 / 10 分钟太早 /
   9 小时太晚 / 已接过一次 / 对方说睡了 / 还悬着一条主动消息 / 活跃聊天）；
2. **默认参数 0 = 关闭**：老调用（老测试、老行为）逐字不变——这是不破坏
   `tests/test_p2_threads.py` 那批直接调 `thread_reason` 的用例的地基；
3. `_thread_windows` 的六元组与 `_thread_pick` 的端到端候选（bot 回完 + 用户沉默 2 小时）；
4. `thread_meta` / 生成侧文案（`_MODE_NOTES` / `_MODE_ASK` / `_DECIDE_NOTES` / is_followup）；
5. 记账：一段沉默只接一次（`thread_asked_at`）、`_why_now` 与 `_thread_mark` 认得它。

按仓库惯例自带最小 astrbot 桩（照抄 `tests/test_v12733_engine_badvalues.py` 头部）。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与其它测试同款，幂等） ───
_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v1289_continue_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import generator, threads  # noqa: E402
from social.config import (  # noqa: E402
    CONTINUE_AFTER_DEFAULT,
    CONTINUE_AFTER_MAX,
    CONTINUE_AFTER_MIN,
    CONTINUE_WINDOW_HOURS_DEFAULT,
    CONTINUE_WINDOW_HOURS_MAX,
    CONTINUE_WINDOW_HOURS_MIN,
    SocialConfig,
)
from social.engine import SocialEngine  # noqa: E402

NOW = 1_800_000_000.0

# 与引擎默认配置同源：追问 12 分钟 / 问在不在 30 分钟 / 最长追 70 分钟 / 上文 70 分钟，
# 接话 90 分钟 ~ 8 小时。
BASE_KW = {
    "probe_after_seconds": 12 * 60.0,
    "presence_after_seconds": 30 * 60.0,
    "max_seconds": 70 * 60.0,
    "context_seconds": 70 * 60.0,
    "presence_floor_seconds": 30 * 60.0,
}
CONTINUE_KW = {
    "continue_after_seconds": 90 * 60.0,
    "continue_max_seconds": 8 * 3600.0,
}


def _kw(**over):
    kw = dict(BASE_KW)
    kw.update(over)
    return kw


def _ckw(**over):
    kw = dict(BASE_KW)
    kw.update(CONTINUE_KW)
    kw.update(over)
    return kw


def _u(**over) -> dict:
    """「她回完、对方就没声了」的基准状态。

    对方 2 小时 1 分钟前说完，她 2 小时前回完。文案刻意避开 `is_leaving` /
    `sleep_signal` / `busy_signal` 三张词表（「加班」「先睡了」之类会让这条路径
    本来就不该成立，那些边界另有专门的用例）。
    """
    u = {
        "last_seen": NOW - 2 * 3600.0 - 60,
        "last_spoken": NOW - 2 * 3600.0,
        "last_message": "今天路上好堵",
        "last_spoken_text": "那你早点休息吧",
        "conversation": [
            {"dir": "in", "text": "今天路上好堵", "ts": NOW - 2 * 3600.0 - 600},
            {"dir": "out", "text": "那你早点休息吧", "ts": NOW - 2 * 3600.0},
        ],
        "pending_result": "replied",
    }
    u.update(over)
    return u


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


def _engine(**cfg) -> SocialEngine:
    tmp = tempfile.mkdtemp()
    eng = SocialEngine(
        context=None,
        config=SocialConfig(),
        state_path=str(Path(tmp) / "state.json"),
        data_dir=tmp,
        time_source=_Clock(),
    )
    for key, value in cfg.items():
        setattr(eng.cfg, key, value)
    # 安静时段压成零长：断言不能取决于跑测试时本机几点
    eng.cfg.quiet_start = 0
    eng.cfg.quiet_end = 0
    return eng


def _user(eng: SocialEngine, bid: str = "b1", uid: str = "u1", **fields):
    user = eng.state.user(bid, uid)
    user["umo"] = "aiocqhttp:private:u1"
    user["__uid"] = uid
    user.update(fields)
    return user


def _silent_user(eng: SocialEngine, seen_ago: float, spoken_ago: float, **fields):
    """「bot 回完 + 用户沉默」的真实状态：最后一句是她说的，之后谁都没说话。"""
    user = _user(
        eng,
        last_seen=NOW - seen_ago,
        last_spoken=NOW - spoken_ago,
        last_message="今天路上好堵",
        last_spoken_text="那你早点休息吧",
        conversation=[
            {"dir": "in", "text": "今天路上好堵", "ts": NOW - spoken_ago - 600},
            {"dir": "out", "text": "那你早点休息吧", "ts": NOW - spoken_ago},
        ],
        pending_result="replied",
        **fields,
    )
    return user


# ─── 1. thread_reason 的 continue 分支 ───────────────────────────


class ThreadReasonContinueTest(unittest.TestCase):
    """`continue` 是第四种「话没说完」，它的判据是**她说完之后**对方消失多久。"""

    def test_two_hours_after_she_replied_is_a_continue(self):
        self.assertEqual(threads.thread_reason(_u(), NOW, **_ckw()), ("continue", ""))

    def test_ten_minutes_is_too_soon(self):
        """刚回完十分钟就追一句，像盯着人看。"""
        u = _u(last_seen=NOW - 660, last_spoken=NOW - 600)
        self.assertEqual(threads.thread_reason(u, NOW, **_ckw())[0], "")

    def test_nine_hours_is_too_late(self):
        """超过 8 小时这场话已经过去了，再接就不像同一场话。"""
        u = _u(last_seen=NOW - 9 * 3600 - 60, last_spoken=NOW - 9 * 3600)
        self.assertEqual(threads.thread_reason(u, NOW, **_ckw())[0], "")

    def test_only_once_per_silence(self):
        """接过一次之后对方没再开口 → 还是同一段沉默，不接第二遍。"""
        u = _u(thread_asked_at=NOW - 3600)
        self.assertEqual(threads.thread_reason(u, NOW, **_ckw())[0], "")

    def test_a_new_silence_after_the_peer_speaks_again_is_fine(self):
        """对方后来又说过话（seen 晚于上次接话）→ 这是新的一段，可以再接。"""
        u = _u(thread_asked_at=NOW - 4 * 3600)
        self.assertEqual(threads.thread_reason(u, NOW, **_ckw()), ("continue", ""))

    def test_peer_said_they_are_going_to_sleep(self):
        """对方最后一句是「我先睡了」→ 这时候接话就是把人吵醒。"""
        self.assertEqual(threads.thread_reason(_u(last_message="我先睡了"), NOW, **_ckw())[0], "")

    def test_pending_proactive_is_still_waiting(self):
        """她上一条主动消息还悬着：再补一句就是追着人要回复。"""
        self.assertEqual(
            threads.thread_reason(_u(pending_result="waiting"), NOW, **_ckw())[0], ""
        )

    def test_active_chat_is_not_continued(self):
        """对方还在连着说话 → 不接话。

        `continue` 要求 `spoken >= seen`（最后一句是她说的），而「对方最近还在连着
        说话」必然让 `seen > spoken`——两个条件互斥，所以这条路径天然不会在活跃
        聊天里触发；这里同时钉住下面那道「活跃聊天」闸也返回空。
        """
        u = _u(
            last_seen=NOW - 60,
            last_spoken=NOW - 1800,
            conversation=[
                {"dir": "in", "text": "在吗", "ts": NOW - 120},
                {"dir": "in", "text": "刚才那个人", "ts": NOW - 60},
                {"dir": "out", "text": "那你早点休息吧", "ts": NOW - 1800},
            ],
        )
        self.assertEqual(threads.thread_reason(u, NOW, **_ckw())[0], "")

    def test_missing_timestamps_do_not_raise(self):
        for bad in ({"last_seen": 0}, {"last_spoken": "abc"}, {"last_seen": "nan"}):
            with self.subTest(bad=bad):
                self.assertEqual(threads.thread_reason(_u(**bad), NOW, **_ckw())[0], "")

    def test_disabled_returns_empty(self):
        """参数为 0（默认）= 关闭：这条路不成立，回旧行为。"""
        self.assertEqual(threads.thread_reason(_u(), NOW, **_kw())[0], "")


# ─── 2. 默认 0 = 关闭：老行为逐字不变（回归） ─────────────────────


class ThreadReasonRegressionTest(unittest.TestCase):
    """`continue_*` 两个关键字参数默认 0。

    `tests/test_p2_threads.py` 等一批用例直接按老签名调 `thread_reason`，它们必须
    **逐字**通过；本类把「不传新参数」与「显式传 0」两种调用对齐，并钉住三种老结论。
    """

    def _probe_case(self) -> dict:
        """对方答得敷衍（「还行」）→ probe。"""
        return {
            "last_seen": NOW - 2400,
            "last_spoken": NOW - 800,
            "last_message": "还行",
            "conversation": [
                {"dir": "in", "text": "在干嘛", "ts": NOW - 3000},
                {"dir": "in", "text": "还行", "ts": NOW - 2400},
                {"dir": "out", "text": "那你今天忙完了吗", "ts": NOW - 800},
            ],
            "pending_result": "replied",
        }

    def _presence_case(self) -> dict:
        """对方 33 分钟没声了（最后一句不是敷衍）→ presence。"""
        return {
            "last_seen": NOW - 2000,
            "last_spoken": NOW - 3000,
            "last_message": "今天路上好堵",
            "conversation": [
                {"dir": "out", "text": "那你今天忙完了吗", "ts": NOW - 3000},
                {"dir": "in", "text": "今天路上好堵", "ts": NOW - 2000},
            ],
            "pending_result": "replied",
        }

    def test_defaults_match_explicit_zero(self):
        cases = [
            ("probe", self._probe_case(), "probe"),
            ("presence", self._presence_case(), "presence"),
            ("她说完对方消失", _u(), ""),
            ("活跃聊天", _u(last_seen=NOW - 60, last_spoken=NOW - 1800), ""),
        ]
        for label, u, want in cases:
            with self.subTest(label=label):
                old = threads.thread_reason(dict(u), NOW, **_kw())
                zero = threads.thread_reason(
                    dict(u), NOW, **_kw(continue_after_seconds=0.0, continue_max_seconds=0.0)
                )
                self.assertEqual(old, zero, "默认值必须等价于显式传 0")
                self.assertEqual(old[0], want, f"{label} 的老结论被改掉了")

    def test_old_signature_still_works(self):
        """按老签名（不传新参数）调用不报错——老测试就是这么调的。"""
        kind, reason = threads.thread_reason(
            _u(),
            NOW,
            probe_after_seconds=120,
            presence_after_seconds=240,
            max_seconds=4200,
            context_seconds=1800,
        )
        self.assertEqual((kind, reason), ("", ""))


# ─── 3. 引擎：六元组窗口与端到端候选 ──────────────────────────────


class ThreadWindowsTest(unittest.TestCase):
    """`_thread_windows` 从四元组变六元组，默认配置的值必须能对上 schema 默认。"""

    def test_default_windows(self):
        windows = _engine()._thread_windows()
        self.assertEqual(len(windows), 6)
        probe, presence, ceiling, context, cont_after, cont_max = windows
        self.assertEqual(probe, 12 * 60.0)
        self.assertEqual(presence, 30 * 60.0)
        self.assertEqual(ceiling, 70 * 60.0)
        self.assertEqual(context, 70 * 60.0)
        self.assertEqual(cont_after, 90 * 60.0)
        self.assertEqual(cont_max, 8 * 3600.0)

    def test_disabled_gives_zero(self):
        _p, _s, _c, _x, cont_after, cont_max = _engine(continue_enabled=False)._thread_windows()
        self.assertEqual((cont_after, cont_max), (0.0, 0.0))

    def test_custom_windows(self):
        _p, _s, _c, _x, cont_after, cont_max = _engine(
            continue_after_minutes=120, continue_window_hours=3
        )._thread_windows()
        self.assertEqual((cont_after, cont_max), (120 * 60.0, 3 * 3600.0))


class ThreadPickContinueTest(unittest.TestCase):
    """端到端：bot 回完 + 用户沉默 2 小时 → 产出一个 continue 候选。"""

    def test_pick_produces_a_continue_candidate(self):
        eng = _engine()
        _silent_user(eng, seen_ago=2 * 3600 + 60, spoken_ago=2 * 3600)
        picked = eng._thread_pick("b1", NOW, None)
        self.assertIsNotNone(picked, "「她回完、对方消失 2 小时」应当成为候选")
        uid, _u_dict, kind, reason, gap = picked
        self.assertEqual(uid, "u1")
        self.assertEqual(kind, "continue")
        self.assertEqual(reason, "")
        self.assertAlmostEqual(gap, 2 * 3600.0, places=3)

    def test_disabled_pick_returns_nothing(self):
        """关掉接话 = 回旧行为：同一份状态一个候选都产不出来。"""
        eng = _engine(continue_enabled=False)
        _silent_user(eng, seen_ago=2 * 3600 + 60, spoken_ago=2 * 3600)
        self.assertIsNone(eng._thread_pick("b1", NOW, None))

    def test_followup_off_does_not_kill_continue(self):
        """followup 与 continue 是两个开关：关掉追问不该顺手关掉接话。"""
        eng = _engine(followup_enabled=False)
        _silent_user(eng, seen_ago=2 * 3600 + 60, spoken_ago=2 * 3600)
        picked = eng._thread_pick("b1", NOW, None)
        self.assertIsNotNone(picked)
        self.assertEqual(picked[2], "continue")

    def test_followup_off_does_not_revive_probe(self):
        """反过来也不能顺手把追问打开：followup 关掉时 probe 候选必须被丢掉。"""
        eng = _engine(followup_enabled=False)
        _user(
            eng,
            last_seen=NOW - 2400,
            last_spoken=NOW - 800,
            last_message="还行",
            conversation=[
                {"dir": "in", "text": "在干嘛", "ts": NOW - 3000},
                {"dir": "in", "text": "还行", "ts": NOW - 2400},
                {"dir": "out", "text": "那你今天忙完了吗", "ts": NOW - 800},
            ],
            pending_result="replied",
        )
        self.assertIsNone(eng._thread_pick("b1", NOW, None))


class ThreadGateContinueTest(unittest.TestCase):
    """`thread_gate` 不得拿 recent_talk 挡住 continue。"""

    def test_recent_talk_does_not_block_continue(self):
        """recent_talk 能配到 12 小时（上限 720 分钟）：拿它挡接话等于这条路永不成立。"""
        eng = _engine(recent_talk_minutes=720)
        u = _silent_user(eng, seen_ago=2 * 3600 + 60, spoken_ago=2 * 3600)
        self.assertEqual(eng.thread_gate("b1", "u1", u, NOW, None, kind="continue"), "")
        # 同一个人、同一时刻，追问/问在不在仍然被「刚聊过」挡住（口径没被放宽）
        self.assertEqual(eng.thread_gate("b1", "u1", u, NOW, None), "刚聊过，不追")

    def test_quiet_hours_still_block_continue(self):
        """接话不是免死金牌：安静时段照样不发。"""
        # `_engine()` 默认把安静时段压成零长，这里要**显式**把它打开
        eng = _engine()
        eng.cfg.quiet_start = 0
        eng.cfg.quiet_end = 24          # 0~24 覆盖一整天，断言与跑测试的时刻无关
        u = _silent_user(eng, seen_ago=2 * 3600 + 60, spoken_ago=2 * 3600)
        self.assertIn("安静时段", eng.thread_gate("b1", "u1", u, NOW, None, kind="continue"))


class ThreadMarkContinueTest(unittest.TestCase):
    def test_mark_labels_it_as_continue(self):
        eng = _engine()
        _silent_user(eng, seen_ago=2 * 3600 + 60, spoken_ago=2 * 3600)
        mark = eng._thread_mark("b1", "u1", NOW)
        self.assertIn("接话", mark, f"面板没把接话标出来：{mark}")


# ─── 4. thread_meta / 生成侧文案 ─────────────────────────────────


class ThreadMetaContinueTest(unittest.TestCase):
    def test_category_and_mode(self):
        meta = threads.thread_meta(
            "continue", about="今天路上好堵", asked="那你早点休息吧", silence_minutes=120
        )
        self.assertEqual(meta["category"], "continue")
        self.assertEqual(meta["mode"], "continue")
        self.assertEqual(meta["silence_minutes"], 120)
        self.assertIn("没声", meta["msg_type_desc"])

    def test_old_signature_still_works(self):
        """老调用（两个位置参数）逐字不变。"""
        meta = threads.thread_meta("probe", "还行", "在干嘛")
        self.assertEqual(meta["category"], "probe")
        self.assertNotIn("silence_minutes", meta)


class ContinueGeneratorNotesTest(unittest.TestCase):
    """生成侧必须知道「接话不是另起话题，也不是查岗」。"""

    def test_mode_note_quotes_her_own_last_sentence(self):
        note = generator._mode_note("continue", about="今天路上好堵", asked="那你早点休息吧")
        self.assertIn("那你早点休息吧", note, f"没引她自己上一句：{note}")
        self.assertIn("不许查岗", note)

    def test_decide_note_says_it_is_not_small_talk(self):
        self.assertIn("不是没话找话", generator._decide_note("continue"))

    def test_is_followup_includes_continue(self):
        """`is_followup` 决定提示词走「就那件事接一句」那套，continue 必须算进去。"""
        src = inspect.getsource(generator.MessageGenerator._compose)
        self.assertIn('"continue"', src)
        self.assertIn(
            'is_followup = mode in ("probe", "presence", "loop", "continue")',
            src,
            "is_followup 没把 continue 算进去，提示词会退回「另起话题」那套",
        )


class _Resp:
    def __init__(self, text: str = "", role: str = "assistant") -> None:
        self.completion_text = text
        self.role = role


class _ProviderCtx:
    """能取到 provider 的 context（只为拼提示词，不真调模型）。"""

    class _P:
        id = "p1"

        async def text_chat(self, **kw):
            return _Resp("NO")

    async def get_using_provider_async(self, umo=None):
        return self._P()


class ContinuePromptTest(unittest.TestCase):
    """提示词层面：接话要引**她自己**上一句，并且明说这不是查岗。"""

    def _prompt(self, meta) -> str:
        gen = generator.MessageGenerator(_ProviderCtx(), SocialConfig())
        target = {
            "umo": "aiocqhttp:FriendMessage:u1",
            "conversation": [
                {"dir": "in", "text": "今天路上好堵"},
                {"dir": "out", "text": "那你早点休息吧"},
            ],
            "last_message": "今天路上好堵",
            "last_spoken_text": "那你早点休息吧",
            "last_seen": NOW - 2 * 3600.0 - 60,
            "_affection": 62,
        }
        prepared = asyncio.run(
            gen._compose(
                target["umo"], target, "能量: 62", "", meta, "你是小娜", None,
                decide=True, at=NOW,
            )
        )
        self.assertIsNotNone(prepared, "compose 返回 None（provider 不可用）")
        return prepared[1]

    def test_continue_prompt_carries_the_silence_and_her_last_sentence(self):
        meta = threads.thread_meta(
            "continue", about="今天路上好堵", asked="那你早点休息吧", silence_minutes=120
        )
        text = self._prompt(meta)
        self.assertIn("那你早点休息吧", text, "没引她自己上一句，接话会接错话头")
        self.assertIn("没声了", text, "没告诉模型对方已经消失多久")
        self.assertIn("不许查岗", text, "没写清这是接话不是查岗")


# ─── 5. 记账：一段沉默只接一次 + 面板 + why ───────────────────────


class ContinueSpeakEndToEndTest(unittest.IsolatedAsyncioTestCase):
    """整条路真跑一遍：continue 候选 → `_speak_inner` → 记账。

    只把「调模型 / 发平台 / 读会话库」这三样换掉，其余全走真实代码——接话发出的
    消息必须按**普通主动消息**记账（`expect_reply=True`、进 pending、记 why），
    并且这一段沉默之后不能再接第二次。
    """

    def _engine_and_user(self):
        eng = _engine()
        u = _silent_user(eng, seen_ago=2 * 3600 + 60, spoken_ago=2 * 3600)
        return eng, u

    def _wire(self, eng) -> None:
        """把外部依赖换成桩：模型、平台、会话库、人格、记忆。"""
        decision = types.SimpleNamespace(
            raw_text="SEND\n那你早点休息吧", send=True,
            parts=["那你早点休息吧"], promise="",
        )

        async def _decide(*a, **k):
            return decision

        async def _history(_umo):
            return []

        async def _memory(_umo, _uid, query=""):
            return ""

        async def _persona(_umo, _bid):
            return ("p", "")

        async def _noop(*a, **k):
            return None

        async def _send(_target, _text, bid=""):
            return True, "", False

        eng.generator = types.SimpleNamespace(decide=_decide)
        eng._load_session_history = _history
        eng._fetch_memory_note = _memory
        eng._persona = _persona
        eng.input_state = types.SimpleNamespace(start=_noop, stop=_noop)
        eng._send_with_retry = _send

    async def test_a_continue_is_sent_and_accounted_as_a_normal_proactive(self):
        eng, u = self._engine_and_user()
        self._wire(eng)
        breakpoint_ts = max(u["last_seen"], u["last_spoken"])
        meta = threads.thread_meta(
            "continue", about="今天路上好堵", asked="那你早点休息吧", silence_minutes=120
        )
        ok, why = await eng._speak_inner("b1", "u1", u, 0.0, NOW, preset=("", meta))
        self.assertTrue(ok, f"接话没发出去：{why}")
        self.assertIn("continue_topic", why, "消息类型没跟着 continue 走")

        # 一段沉默只接一次：记下接话时刻与当时那个断点
        self.assertEqual(u.get("thread_asked_at"), NOW)
        self.assertEqual(u.get("thread_for"), breakpoint_ts)
        # 接完就进「等回话」——上一条主动消息还悬着，不会追着说
        self.assertEqual(u.get("pending_result"), "waiting")

        log = u.get("proactive_log") or []
        self.assertTrue(log, "没进主动消息日志")
        self.assertEqual(log[-1]["cat"], "continue_topic")
        self.assertIn("她说完之后", log[-1]["why"])
        self.assertEqual(log[-1]["about"], "今天路上好堵")

    async def test_the_same_silence_is_not_continued_twice(self):
        eng, u = self._engine_and_user()
        self._wire(eng)
        meta = threads.thread_meta(
            "continue", about="今天路上好堵", asked="那你早点休息吧", silence_minutes=120
        )
        ok, _why = await eng._speak_inner("b1", "u1", u, 0.0, NOW, preset=("", meta))
        self.assertTrue(ok)
        # 两小时后再看：窗口还开着，但这段沉默已经接过一次（且进了等回话），不再接
        self.assertIsNone(eng._thread_pick("b1", NOW + 2 * 3600.0, None))


class ContinueAccountingTest(unittest.TestCase):
    """发出去之后要记 `thread_asked_at`，否则同一段沉默每轮心跳都会再接一次。"""

    def test_speak_inner_records_continue_like_probe(self):
        src = inspect.getsource(SocialEngine._speak_inner)
        self.assertIn('("probe", "presence", "continue")', src)

    def test_why_now_explains_continue(self):
        eng = _engine()
        u = _user(eng)
        why = eng._why_now(
            "b1", u, "continue", "", {"category": "continue", "silence_minutes": 120}, NOW
        )
        self.assertIn("TA", why)
        self.assertIn("120", why)

    def test_continue_is_recorded_as_expecting_a_reply(self):
        """接话是在问人，按普通主动消息记账（`category not in ("greet", "closer")`）。"""
        meta = threads.thread_meta("continue", asked="那你早点休息吧", silence_minutes=120)
        self.assertNotIn(meta["category"], ("greet", "closer"))


# ─── 6. 配置：默认值 / 夹取 / 窗口必须长于起点 ────────────────────


class ContinueConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = SocialConfig()
        self.assertTrue(cfg.continue_enabled)
        self.assertEqual(cfg.continue_after_minutes, 90)
        self.assertEqual(cfg.continue_window_hours, 8)
        self.assertEqual(CONTINUE_AFTER_DEFAULT, 90)
        self.assertEqual(CONTINUE_WINDOW_HOURS_DEFAULT, 8)
        from_empty = SocialConfig.from_astrbot({})
        self.assertTrue(from_empty.continue_enabled)
        self.assertEqual(from_empty.continue_after_minutes, 90)
        self.assertEqual(from_empty.continue_window_hours, 8)

    def test_range_constants_match_the_schema(self):
        self.assertEqual((CONTINUE_AFTER_MIN, CONTINUE_AFTER_MAX), (10, 720))
        self.assertEqual(
            (CONTINUE_WINDOW_HOURS_MIN, CONTINUE_WINDOW_HOURS_MAX), (1, 24)
        )

    def test_values_are_clamped(self):
        cfg = SocialConfig.from_astrbot(
            {"continue_after_minutes": 1, "continue_window_hours": 99}
        )
        self.assertEqual(cfg.continue_after_minutes, CONTINUE_AFTER_MIN)
        self.assertEqual(cfg.continue_window_hours, CONTINUE_WINDOW_HOURS_MAX)
        cfg = SocialConfig.from_astrbot({"continue_after_minutes": 9999})
        self.assertEqual(cfg.continue_after_minutes, CONTINUE_AFTER_MAX)

    def test_window_is_always_longer_than_the_start(self):
        """窗口 = 起点时区间是空的：接话一次都不会发生，而面板上两个数看着都正常。"""
        for after, window in ((720, 1), (600, 10), (120, 2), (90, 8), (10, 1)):
            with self.subTest(after=after, window=window):
                cfg = SocialConfig.from_astrbot(
                    {"continue_after_minutes": after, "continue_window_hours": window}
                )
                self.assertGreater(
                    cfg.continue_window_hours * 3600,
                    cfg.continue_after_minutes * 60,
                    "窗口没有比起点长，接话永远落不进区间",
                )

    def test_switch_off(self):
        self.assertFalse(SocialConfig.from_astrbot({"continue_enabled": False}).continue_enabled)


class ContinueStatusTextTest(unittest.TestCase):
    def test_status_line_is_on(self):
        text = _engine()._continue_status_text()
        self.assertIn("接话", text)
        self.assertIn("90", text)
        self.assertIn("8", text)

    def test_status_line_is_off(self):
        text = _engine(continue_enabled=False)._continue_status_text()
        self.assertIn("接话", text)
        self.assertIn("关", text)

    def test_status_page_actually_prints_the_line(self):
        src = inspect.getsource(SocialEngine.status_text)
        self.assertIn("self._continue_status_text()", src, "面板没把「接话」那一行拼进去")

    def test_full_status_page_carries_the_line(self):
        text = asyncio.run(_engine().status_text())
        self.assertIn("- 接话：开", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
