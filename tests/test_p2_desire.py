"""P2 批次的回归测试：desire.py 的 #32/#33/#34/#35 与 engine.py 的 #52/#53/#55。

每个用例都对应一个具体缺陷，注释里写清「原来会怎样」，并尽量用**修复前的口径**
再算一遍当反证（不然测试可能只是空跑）。按仓库惯例自带最小 astrbot 桩，
两种摆法（tests/ 在插件里 / 在工作区根）都能跑。
"""

from __future__ import annotations

import ast
import itertools
import logging
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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

from social import anchors, desire, engine as engine_mod, threads  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social.state import SocialState  # noqa: E402

NOW = 1_700_000_000.0
DAY = 86400.0
DT = datetime(2023, 11, 15, 14, 0)

_ENGINE_SRC = Path(engine_mod.__file__).read_text(encoding="utf-8")

# 每个用例拿一份**真实的**默认用户状态（不是手捏的 dict），再盖上要测的字段——
# 这样默认字段口径跟着 state._default_user() 走，不会因为那边加字段而假失败。
_DEFAULT_STATE = SocialState(
    os.path.join(tempfile.mkdtemp(), "defaults.json"), time_source=lambda: NOW
)
_SEQ = itertools.count()


def _user(**kw):
    uid = f"u{next(_SEQ)}"
    user = _DEFAULT_STATE.user("bot", uid)
    user.update(kw)
    return user


def _settle(user, now, **kw):
    args = dict(
        refill_hours=6.0,
        recent_talk_seconds=7200.0,
        mood_factor=1.0,
        rhythm=1.0,
        quiet=False,
        live_cue=False,
    )
    args.update(kw)
    return desire.settle(user, now, **args)


# ══════════════════════════════════════════════════════════════════════════
# #32 urge_at 缺失时用 last_seen 播种 → 老用户升级后同一次结算里集体过闸
# ══════════════════════════════════════════════════════════════════════════

def _old_first_settle_urge(user, now, refill_hours=6.0):
    """**修复前**的播种口径（照抄老代码的那两条）：urge_at 缺失就拿 last_seen 顶。

    只复现这一处接缝；用例里的 fixture 让 rhythm/mood/momentum 都是 1.0，
    所以老式子就是 hours*rate，足够反证「一次结算里全体老用户过闸」。
    """
    since = float(user.get("urge_at", 0.0) or 0.0)
    if since <= 0:
        since = float(user.get("last_seen", 0) or 0) or now   # ← 老口径
    hours = max(0.0, (now - since) / 3600.0)
    interest = float(user.get("interest", 0.35) or 0.35)
    rate = (desire.BASE_RATE + desire.INTEREST_RATE * interest) / max(refill_hours, 0.5)
    urge = float(user.get("urge", 0.0) or 0.0) + hours * rate
    return min(urge, desire.URGE_CEILING)


class MassFireOnMigrationTest(unittest.TestCase):
    """#32：老用户第一次结算的 dt 是「几天」，urge 一步到顶并全部过闸。"""

    def _old_user(self):
        # 审查报告里的那条 fixture：last_seen 五天前、没有 urge_at、在意程度很高
        return _user(last_seen=NOW - 5 * DAY, interest=0.8, message_count=300)

    def test_first_settle_only_starts_the_clock(self):
        """播种只记「从这一刻开始攒」，不补算插件没运行的那几天。"""
        user = self._old_user()
        self.assertEqual(user.get("urge_at", 0.0), 0.0, "fixture 必须先缺 urge_at")
        self.assertEqual(_settle(user, NOW), 0.0)
        self.assertEqual(user["urge_at"], NOW, "urge_at 应该被播种成 now")

        # 再过一整个攒满周期也只是正常涨，不会一步到天花板
        later = _settle(user, NOW + 6 * 3600)
        self.assertGreater(later, 0.0)
        self.assertLess(later, desire.URGE_CEILING)

    def test_old_seeding_really_reaches_the_ceiling(self):
        """反证：同样的输入在老口径下确实冲到 UE_CEILING 并过闸。"""
        user = self._old_user()
        old_urge = _old_first_settle_urge(user, NOW)
        self.assertEqual(old_urge, desire.URGE_CEILING)
        self.assertGreaterEqual(
            old_urge, desire.effective_gate(user, desire.URGE_CEILING)
        )

    def test_all_old_users_fire_in_one_settle_before_the_fix(self):
        """反证 + 可观测证明：N 个老用户在**同一次**结算里全部过闸。"""
        users = {f"old{i}": self._old_user() for i in range(5)}
        fired = [
            uid for uid, u in users.items()
            if _old_first_settle_urge(u, NOW) >= desire.effective_gate(u, desire.URGE_CEILING)
        ]
        self.assertEqual(len(fired), 5, "老口径下应该 5 个全过闸（这就是集体主动消息）")

    def test_all_old_users_stay_quiet_in_one_settle_after_the_fix(self):
        """修好后同一次结算：一个都不 ready（dt 只有一个时钟 tick，不是几天）。"""
        users = {
            f"old{i}": {
                "umo": f"aiocqhttp:FriendMessage:old{i}",
                "last_seen": NOW - 5 * DAY,
                "interest": 0.8,
                "message_count": 300,
            }
            for i in range(5)
        }
        eng = _SettleEngine(users)
        ready = eng.settle_minds("bot1", NOW, DT)
        self.assertEqual(ready, [], "迁移/升级后的第一次结算不该有人直接过闸")
        for uid in users:
            self.assertEqual(eng.state.user("bot1", uid)["urge_at"], NOW)


# ══════════════════════════════════════════════════════════════════════════
# #33 裸 int()/float() 读到脏持久化字段会抛 ValueError
# ══════════════════════════════════════════════════════════════════════════

def _old_int_reads(user):
    """**修复前** interest_level 里那几行裸转换（照抄），用来证明 fixture 真的会炸。"""
    msgs = int(user.get("message_count", 0) or 0)          # noqa: F841
    sent = int(user.get("proactive_sent", 0) or 0)          # noqa: F841
    replied = int(user.get("proactive_replied", 0) or 0)    # noqa: F841
    streak = int(user.get("no_reply_streak", 0) or 0)       # noqa: F841
    return msgs, sent, replied, streak


class TolerantFieldReadTest(unittest.TestCase):
    """#33：state.json / Core 快照里的字段读不出来时不能抛，要退回中性值。"""

    DIRTY = ("12.5", "x", None, "", {}, [], float("nan"), float("inf"), True)

    def test_old_bare_int_really_raises(self):
        """反证：脏值在老代码上确实抛 ValueError，不是因为测试太温和。

        注意老代码的 `or 0` 已经挡住了 None/""/{} /[] 这类**假值**，所以真正会炸的是
        **真值垃圾**（"x"、"12.5"、nan）——报告里点的也是这些。
        """
        for dirty in ("x", "12.5", float("nan")):
            with self.assertRaises((ValueError, TypeError)):
                _old_int_reads(
                    {
                        "message_count": dirty,
                        "proactive_sent": 0,
                        "proactive_replied": 0,
                        "no_reply_streak": 0,
                    }
                )

    def test_num_helpers(self):
        self.assertIsNone(desire._num(float("nan")))
        self.assertIsNone(desire._num(float("inf")))
        self.assertIsNone(desire._num("nan"))
        self.assertIsNone(desire._num("abc"))
        self.assertIsNone(desire._num(None))
        self.assertIsNone(desire._num(""))
        self.assertIsNone(desire._num([]))
        self.assertIsNone(desire._num({}))
        self.assertEqual(desire._num("12.5"), 12.5)
        self.assertEqual(desire._float("abc", 3.5), 3.5)
        self.assertEqual(desire._float(None, 3.5), 3.5)
        self.assertEqual(desire._int("12.5"), 12)
        self.assertEqual(desire._int("abc", 7), 7)
        self.assertEqual(desire._int(True), 1)

    def test_interest_level_survives_dirty_fields(self):
        for dirty in self.DIRTY:
            user = _user(
                message_count=dirty,
                proactive_sent=dirty,
                proactive_replied=dirty,
                no_reply_streak=dirty,
            )
            value = desire.interest_level(user, None)
            self.assertIsInstance(value, float)
            self.assertGreaterEqual(value, 0.05)
            self.assertLessEqual(value, 1.0)

    def test_other_readers_survive_dirty_fields(self):
        for dirty in self.DIRTY:
            user = _user(
                interest=dirty,
                urge=dirty,
                urge_at=dirty,
                urge_scale=dirty,
                fire_gate=dirty,
                rhythm_samples=dirty,
                last_ignored_at=dirty,
                last_seen=dirty,
            )
            self.assertIsInstance(desire.smooth_interest(user, 0.5, NOW), float)
            self.assertIsInstance(desire.rhythm_factor(user, 14), float)
            self.assertIsInstance(desire.settle(
                user, NOW, refill_hours=6.0, recent_talk_seconds=7200.0
            ), float)
            self.assertIsInstance(desire.effective_gate(user, desire.URGE_CEILING), float)
            desire.decay_streak(user, NOW)
            desire.after_ignored(user, NOW)
            desire.after_skip(user, NOW)

    def test_dirty_but_parseable_values_are_used(self):
        """容错不等于摆烂：能读出来的字符串照常用。"""
        user = _user(message_count="300", proactive_sent="2", proactive_replied="2")
        # 熟络度满格 + 回复率 3/4 → 明显高于「全读不出来」的中性值
        self.assertGreater(desire.interest_level(user, None), 0.6)


# ══════════════════════════════════════════════════════════════════════════
# #34 `if body.get("asleep")` 真值判断 → 字符串 "false" 被当成「她正在睡觉」
# ══════════════════════════════════════════════════════════════════════════

# Humanoid Core `humanoid/config.py` 的 `_as_bool` 词表（跨插件不能 import，钉住同一套）
_CORE_TRUE = {"1", "true", "yes", "y", "on", "是", "开"}
_CORE_FALSE = {"0", "false", "no", "n", "off", "否", "关"}


class AsleepTruthinessTest(unittest.TestCase):
    """#34：Core 的布尔字段经常是字符串，真值判断会把「没睡」判成「正在睡觉」。"""

    def test_vocabulary_matches_humanoid_core(self):
        self.assertEqual(set(desire._TRUE_STRINGS), _CORE_TRUE)
        self.assertEqual(set(desire._FALSE_STRINGS), _CORE_FALSE)

    def test_false_forms_are_not_asleep(self):
        for value in (False, 0, 0.0, None, "", "false", "FALSE", " False ",
                      "0", "no", "off", "n", "否", "关"):
            self.assertAlmostEqual(
                desire.mood_multiplier(None, None, body={"asleep": value}), 1.0,
                msg=f"asleep={value!r} 不该被当成在睡觉",
            )

    def test_true_forms_are_asleep(self):
        for value in (True, 1, 2.5, "true", "1", "yes", "on", "是", "开"):
            self.assertAlmostEqual(
                desire.mood_multiplier(None, None, body={"asleep": value}), 0.06,
                msg=f"asleep={value!r} 应该按在睡觉算",
            )

    def test_old_truthiness_really_cut_the_factor(self):
        """反证：老写法 `if body.get("asleep")` 对 "false" 是真值，factor 被砍到 1/16。"""
        body = {"asleep": "false"}
        old_factor = 0.06 if body.get("asleep") else 1.0
        self.assertAlmostEqual(old_factor, 0.06)
        self.assertAlmostEqual(desire.mood_multiplier(None, None, body=body), 1.0)

    def test_unreadable_body_is_not_asleep(self):
        """body 不是 dict（Core 返回字符串/None）时宁可开口，也别永久静音。"""
        for body in (None, "", "false", 0, []):
            self.assertFalse(engine_mod._body_asleep(body))

    def test_engine_uses_the_same_helper(self):
        """engine 侧不许再另判一套：只剩 _body_asleep 里那一处读 asleep。"""
        self.assertEqual(_ENGINE_SRC.count('body.get("asleep")'), 1)
        self.assertIn('return desire.truthy(body.get("asleep"))', _ENGINE_SRC)
        self.assertIn("asleep = _body_asleep(body)", _ENGINE_SRC)


# ══════════════════════════════════════════════════════════════════════════
# #35 `-0.03 * streak` 无上限 → interest 被钉死在下限
# ══════════════════════════════════════════════════════════════════════════

def _old_interest_with_streak(streak, *, a=0.5, familiar=1.0, rate=0.5):
    """**修复前**的 interest_level 算式（惩罚项不封顶）。"""
    raw = 0.12 + 0.34 * a + 0.30 * familiar + 0.24 * rate - 0.03 * streak
    return min(1.0, max(0.05, raw))


class StreakPenaltyCapTest(unittest.TestCase):
    """#35：冷落惩罚要有上限，而不是把 interest 永久钉在下限。"""

    def _user_with_streak(self, streak):
        # message_count=300 → 熟络度满格；affection 缺省 → a=0.5；没发过 → rate=0.5
        return _user(message_count=300, no_reply_streak=streak)

    def test_cap_value_and_derivation(self):
        self.assertEqual(desire.STREAK_INTEREST_PENALTY_CAP, 8)
        # 上限的依据：0.03*8 = 0.24，正好等于回复率那一项的权重
        self.assertAlmostEqual(
            0.03 * desire.STREAK_INTEREST_PENALTY_CAP, 0.24
        )
        self.assertLessEqual(
            desire.STREAK_INTEREST_PENALTY_CAP, desire.STREAK_GATE_MAX_STREAK * 2
        )

    def test_penalty_stops_growing_at_the_cap(self):
        at_cap = desire.interest_level(self._user_with_streak(8), None)
        beyond = desire.interest_level(self._user_with_streak(40), None)
        self.assertAlmostEqual(at_cap, beyond, msg="超过上限后不该再往下掉")
        self.assertGreater(at_cap, 0.05, "封顶后不能还被钉在下限")

    def test_penalty_is_still_monotonic_below_the_cap(self):
        zero = desire.interest_level(self._user_with_streak(0), None)
        at_cap = desire.interest_level(self._user_with_streak(8), None)
        self.assertLess(at_cap, zero)

    def test_old_unbounded_penalty_pinned_to_the_floor(self):
        """反证：老口径 streak=40 时惩罚 1.2，interest 直接落到下限 0.05。"""
        self.assertAlmostEqual(_old_interest_with_streak(40), 0.05)
        self.assertGreater(
            desire.interest_level(self._user_with_streak(40), None), 0.05
        )


# ══════════════════════════════════════════════════════════════════════════
# #52 greet 分支用 sent_ts 写 closer_for，与 last_sent 不等值 → 等值守卫失效
# ══════════════════════════════════════════════════════════════════════════

class CloserMarkerTest(unittest.TestCase):
    """#52：closer_for 与 last_sent 必须取同一个数，收场守卫才成立。"""

    def _closer_user(self, closer_for):
        return _user(
            last_sent=NOW + 0.5,          # 落盘的 last_sent 比发送前的 sent_ts 晚一个 tick
            closer_for=closer_for,
            closer_at=NOW + 0.5,
            pending_result="waiting",
            conversation=[{"dir": "out", "text": "早安"}],
        )

    def test_marker_reads_the_persisted_last_sent(self):
        user = self._closer_user(0.0)
        marker = SocialEngine._closer_marker(SimpleNamespace(), user, NOW)
        self.assertEqual(marker, NOW + 0.5)

    def test_marker_falls_back_to_sent_ts(self):
        user = _user(last_sent=0.0)
        self.assertEqual(
            SocialEngine._closer_marker(SimpleNamespace(), user, NOW), NOW
        )

    def test_old_marker_defeats_the_guard(self):
        """反证 + 后果：老写法（closer_for=sent_ts）下 closer_reason 认为还没收场。"""
        after = 8 * 3600.0
        now = NOW + 9 * 3600.0

        old = self._closer_user(closer_for=NOW)           # 老 greet 分支的写法
        self.assertNotEqual(
            threads.closer_reason(old, now, after_seconds=after), "",
            "closer_for 与 last_sent 不等值时守卫失效，会被重复当成收场候选",
        )

        fixed = self._closer_user(
            closer_for=SocialEngine._closer_marker(SimpleNamespace(), old, NOW)
        )
        self.assertEqual(
            threads.closer_reason(fixed, now, after_seconds=after), "",
            "同一个数 → 守卫认出「这条已经收过场了」",
        )

    def test_both_branches_go_through_the_marker(self):
        """两条写 closer_for 的分支都得走同一处，别再手写 sent_ts。"""
        self.assertEqual(_ENGINE_SRC.count('u["closer_for"] = '), 2)
        self.assertEqual(
            _ENGINE_SRC.count('u["closer_for"] = self._closer_marker(u, sent_ts)'), 2
        )


# ══════════════════════════════════════════════════════════════════════════
# #53 engine 挂时间锚点这条路径没补 schema 要求的 tries
# ══════════════════════════════════════════════════════════════════════════

class CueAnchorTriesTest(unittest.TestCase):
    """#53：由头池的每条都要带 tries，engine 那条 add_anchor 路径漏了。"""

    def test_setdefault_on_the_returned_item_reaches_the_stored_anchor(self):
        user = _user()
        item = anchors.add_anchor(
            user, anchors.KIND_CUE, "明天面试", due=NOW + 3600, now=NOW
        )
        self.assertIsInstance(item, dict)
        # add_anchor 自己不写 tries（anchors.py 不是本单元的文件，只在调用点补）
        self.assertNotIn("tries", item)
        item.setdefault("tries", 0)
        stored = [a for a in user["anchors"] if a.get("about") == "明天面试"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["tries"], 0, "取到的是存进 u['anchors'] 的同一个 dict")

    def test_engine_call_site_does_setdefault(self):
        """结构断言：observe 里的 add_anchor 调用后面必须跟一句 setdefault("tries", 0)。"""
        tree = ast.parse(_ENGINE_SRC)
        parents: dict = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_anchor"
        ]
        self.assertTrue(calls, "engine 里应该有 add_anchor 调用点")
        for call in calls:
            # 往上找最近的 try（调用点就在里面），再看这段 try 有没有补 tries
            guard = call
            while guard in parents and not isinstance(guard, ast.Try):
                guard = parents[guard]
            body_src = ast.dump(guard) if isinstance(guard, ast.Try) else ""
            self.assertIn(
                "attr='setdefault'", body_src,
                "engine 的 add_anchor 调用点必须补 setdefault('tries', 0)",
            )
            self.assertIn("value='tries'", body_src)


# ══════════════════════════════════════════════════════════════════════════
# #55 settle_minds 的逐用户循环没有异常隔离 → 一个人坏掉整轮心跳
# ══════════════════════════════════════════════════════════════════════════

class _NullCore:
    def load_snapshot(self, bid, uid, root=None):
        return None


class _SettleEngine:
    """跑真的 settle_minds 需要的那几个依赖（与 test_autonomous_social 的 _MetricsEngine 同款）。"""

    settle_minds = SocialEngine.settle_minds
    _remember_night = SocialEngine._remember_night
    _quiet_now = SocialEngine._quiet_now
    _pending_window = SocialEngine._pending_window
    _sync_peer_said = staticmethod(SocialEngine._sync_peer_said)

    def __init__(self, users):
        self.cfg = SocialConfig()
        self.state = SocialState(
            os.path.join(tempfile.mkdtemp(), "s.json"), time_source=lambda: NOW
        )
        self.core = _NullCore()
        self._night_windows = {}
        self._m = {}
        self.logs = []
        for uid, fields in users.items():
            self.state.user("bot1", uid).update(fields)

    def _time(self):
        return NOW

    def _moment_of(self, bid, now):
        return DT

    def log(self, message):
        self.logs.append(message)


def _ready_user(uid):
    """念头已经攒够、只等结算的人（过闸条件：urge 2.5 >= effective_gate 1.0）。"""
    return {
        "umo": f"aiocqhttp:FriendMessage:{uid}",
        "urge": 2.5,
        "urge_at": NOW - 3600.0,
        "interest": 0.7,
        "fire_gate": 1.0,
    }


class PerUserIsolationTest(unittest.TestCase):
    """#55：一个用户炸掉不能带走同一个角色这一轮里的其它用户。"""

    def _engine(self):
        return _SettleEngine({
            "good1": _ready_user("good1"),
            "bad": _ready_user("bad"),
            "good2": _ready_user("good2"),
        })

    def test_one_bad_user_does_not_stop_the_others(self):
        eng = self._engine()
        real = desire.interest_level

        def flaky(user, affection=None, *, weigh_reply_rate=True):
            if str(user.get("umo", "")).endswith("bad"):
                raise ValueError("state.json 里的字段被写坏了")
            return real(user, affection, weigh_reply_rate=weigh_reply_rate)

        with mock.patch.object(engine_mod.desire, "interest_level", side_effect=flaky), \
                mock.patch.object(engine_mod, "logger") as log:
            ready = eng.settle_minds("bot1", NOW, DT)

        uids = [uid for uid, _u, _urge in ready]
        self.assertIn("good1", uids)
        self.assertIn("good2", uids)
        self.assertNotIn("bad", uids)
        self.assertEqual(eng._m.get("settle_user_errors"), 1, "坏用户要计数，不能静默")
        self.assertTrue(log.warning.called, "坏用户要留一条日志，别让人查不到")

    def test_old_loop_without_try_really_raises(self):
        """反证：把同一个人放进「没有 try」的老循环形态，异常会直接逃出整轮。"""
        users = self._engine().state.bot("bot1")["users"]

        def boom(user):
            raise ValueError("state.json 里的字段被写坏了")

        with self.assertRaises(ValueError):
            for _uid, user in users.items():     # ← 老 settle_minds 的循环体就是这种直筒式
                boom(user)

    def test_non_dict_user_entry_is_counted_not_swallowed(self):
        eng = self._engine()
        eng.state.bot("bot1")["users"]["u_null"] = None
        with mock.patch.object(engine_mod, "logger") as log:
            ready = eng.settle_minds("bot1", NOW, DT)
        uids = [uid for uid, _u, _urge in ready]
        self.assertIn("good1", uids)
        self.assertIn("good2", uids)
        self.assertEqual(eng._m.get("settle_user_errors"), 1)
        self.assertTrue(log.warning.called)

    def test_dirty_but_parseable_user_still_settles(self):
        """#33 + #55 合起来的效果：脏字段只是被容错读掉，不该触发隔离也不该计数。"""
        eng = self._engine()
        eng.state.bot("bot1")["users"]["dirty"] = {
            "umo": "aiocqhttp:FriendMessage:dirty",
            "urge": "2.5",
            "urge_at": NOW - 3600.0,
            "interest": "0.9",
            "message_count": "12.5",
            "no_reply_streak": "x",
            "fire_gate": "?",
        }
        with mock.patch.object(engine_mod, "logger") as log:
            ready = eng.settle_minds("bot1", NOW, DT)
        uids = [uid for uid, _u, _urge in ready]
        self.assertIn("dirty", uids)
        self.assertNotIn("settle_user_errors", eng._m)
        self.assertFalse(log.warning.called)

    def test_counter_is_a_declared_and_persisted_metric(self):
        self.assertIn('"settle_user_errors": 0', _ENGINE_SRC)
        tail = _ENGINE_SRC.split("for _key in (", 1)[1][:400]
        self.assertIn('"settle_user_errors"', tail, "累计计数要跨重启接上")


if __name__ == "__main__":
    unittest.main()
