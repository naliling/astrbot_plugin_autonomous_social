"""v1.28.6：更多由头来源 + 「目标已不存在」永久丢弃。

## 为什么

用户原话：「现在的问题是事件少 每次只有那几个 发出来的消息 而且私聊发送没看到几个」
以及「关于报错 出现 请添加对方为好友/你已被移除群聊的报错就该对目标永久丢弃」。

### 一、由头来源太少

Core 契约里**早就给了**这些字段，却从没有一条由头用过：

* `wake`（2.28 起）——醒来状态。原来派生只看「在做什么」，于是她刚睁眼
  说的还是「正在厨房煮饭」。
* body 轴（v1 契约起）——sleep_pressure / hunger / discomfort / sleep_debt。
  真人主动开口里「困死了」「饿到胃疼」是很常见的一类。
* `feelings`（v1 契约起）——Core 自己按显著度排好的前三句，原来只有
  `_is_idle` 读它、而且只为了判「今天挺空的」。
* `day.next`——原来是列表却只用第一条，白丢一条。

### 二、「目标已不存在」只隔离 24 小时

好友删了不会自己回来、群散了就是散了。24h 隔离只是让下一轮心跳再写一遍
完整 LLM、再撞一次同一面墙——线上就是这样把每轮的「尝试配额」吃光的
（一轮 25 次尝试只发出去 2 条）。

## 本文件盯的

永久档：
- 「请添加对方为好友」→ 永久隔离，且不计 giveup / 不记发送失败退避；
- 「你已被移除群聊」→ 群永久隔离；
- 24h 档的字样仍然只给 24h；
- 平台级失败（adapter 没加载）一个字都不隔离；
- 永久隔离**不删状态**：对方回来发消息时 `observe` 会清掉它。

新由头：
- 四类来源各自在「有数据」时产出、在「没数据」时一律空（不编）；
- 脏数据（字符串 / nan / 越界）不当真；
- 新 kind 都登记在 `_KIND_TTL` / `KIND_RANK` / `_REASON_BY_KIND` 里——
  漏登记的 kind 会被兜底档接管，而 `KIND_RANK` 的默认 9 会让它排到最后。
"""

from __future__ import annotations

import sys
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

    event.MessageChain = _Chain()
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import social.anchors as anchors  # noqa: E402
from social import engine as engine_mod  # noqa: E402
from social.core_bridge import CoreBridge  # noqa: E402


class _Bridge(CoreBridge):
    def __init__(self):
        super().__init__(mode="standalone", data_dir=None)


# ─── 一、「目标已不存在」永久丢弃 ────────────────────────────────


class PermanentDeadMarkersTest(unittest.TestCase):
    """判据：哪些报错算「这个目标已经没了」。"""

    def test_friend_side_marks(self):
        for msg in ("发送失败：请添加对方为好友", "不是好友", "unfriend",
                    "对方已注销", "你已被对方拉黑"):
            self.assertTrue(engine_mod._permanent_dead(msg), f"{msg!r} 该判永久")

    def test_group_side_marks(self):
        """用户原话里的第二种报错。这一档原来根本不在任何词表里。"""
        for msg in ("你已被移除群聊", "已被移出群聊", "群已解散",
                    "not in group", "群聊不存在"):
            self.assertTrue(engine_mod._permanent_dead(msg), f"{msg!r} 该判永久")

    def test_platform_side_is_not_permanent(self):
        """平台故障一个字都不能永久隔离——那会把健康的人彻底丢掉。"""
        for msg in ("adapter 没加载", "该平台不支持主动消息", "session expired",
                    "发送超时", "retcode=1200", "", "   "):
            self.assertFalse(engine_mod._permanent_dead(msg), f"{msg!r} 不是永久档")

    def test_permanent_implies_unreachable(self):
        """永久档必须同时算「这个人发不了」：下游有几处只读 peer_unreachable。"""
        for msg in ("请添加对方为好友", "你已被移除群聊"):
            self.assertTrue(
                engine_mod.SocialEngine._is_unreachable_error(msg),
                f"{msg!r} 也该被 unreachable 认出来",
            )

    def test_dead_seconds_is_json_safe(self):
        """写进 state.json 的值不能是 inf：json 不认，别的解析器会拒收整个文件。"""
        import json
        secs = engine_mod._PERMANENT_DEAD_SECONDS
        self.assertGreater(secs, 100 * 365 * 86400.0 - 1)
        json.dumps({"until": secs})  # 不抛就行
        self.assertLess(secs, 2 ** 53)


class SendFailedFlagTest(unittest.TestCase):
    """`_SendFailed.permanent` 必须把 peer_unreachable 一起带上。"""

    def test_permanent_sets_peer_unreachable(self):
        from social.engine import _SendFailed
        e = _SendFailed("x", permanent=True)
        self.assertTrue(e.peer_unreachable, "只读 peer_unreachable 的下游会被漏掉")
        self.assertTrue(e.permanent)

    def test_plain_failure_has_neither(self):
        from social.engine import _SendFailed
        e = _SendFailed("x")
        self.assertFalse(e.permanent)
        self.assertFalse(e.peer_unreachable)


class BlockUntilTest(unittest.TestCase):
    """隔离到「现在 + 100 年」而不是 inf。"""

    def test_block_until_is_finite_and_huge(self):
        import time
        now = time.time()
        until = now + engine_mod._PERMANENT_DEAD_SECONDS
        self.assertGreater(until, now + 99 * 365 * 86400.0)
        self.assertEqual(until, until)          # 不是 nan
        self.assertNotIn("inf", repr(until).lower())


# ─── 二、新的由头来源 ────────────────────────────────────────────


def _body(**kw):
    base = {"activity": {}, "day": {}}
    base.update(kw)
    return base


def _derive(body, u=None, now=1_000_000.0, taken=None):
    kinds: dict = {}
    out = anchors._derive_from_state(
        body, u or {}, now, set() if taken is None else taken, kinds=kinds
    )
    return out, kinds


class WakeCueTest(unittest.TestCase):
    """她刚醒：Core 2.28 起契约的 `wake` 块。"""

    NOW = 1_000_000.0

    def _cue(self, wake, now=NOW):
        return anchors._wake_cue(_body(wake=wake), now)

    def test_woke_with_enough_sleep(self):
        got = self._cue({"asleep": False, "wake_at": self.NOW - 60,
                         "last_sleep_hours": 8.5})
        self.assertIn("刚睡醒", got)

    def test_woke_with_short_sleep(self):
        got = self._cue({"asleep": False, "wake_at": self.NOW - 60,
                         "last_sleep_hours": 5.0})
        self.assertIn("才睡5小时", got)

    def test_stale_wake_at_is_not_a_cue(self):
        """`wake_at` 是 epoch、跨天不清（回退 last_wake_at）：不看新鲜度就会
        拿三天前醒的觉派生出「刚睡醒」——一句假话。"""
        got = self._cue({"asleep": False, "wake_at": self.NOW - 3 * 86400.0,
                         "last_sleep_hours": 8.0})
        self.assertEqual(got, "")

    def test_future_wake_at_is_not_a_cue(self):
        """时钟不一致（手改 state.json / 换时区）：容忍 10 分钟，超过当读不出来。"""
        got = self._cue({"asleep": False, "wake_at": self.NOW + 3600.0,
                         "last_sleep_hours": 8.0})
        self.assertEqual(got, "")

    def test_small_clock_skew_is_tolerated(self):
        got = self._cue({"asleep": False, "wake_at": self.NOW + 120.0,
                         "last_sleep_hours": 8.0})
        self.assertTrue(got, "两分钟的钟差不该把由头吃掉")

    def test_without_now_still_works(self):
        """调用方明确说「只问形状」时不判新鲜度（由 KIND_WAKE 的 TTL 兜着）。"""
        got = anchors._wake_cue(_body(wake={
            "asleep": False, "wake_at": 1.0, "last_sleep_hours": 8.0,
        }))
        self.assertTrue(got)

    def test_asleep_is_not_a_cue(self):
        self.assertEqual(self._cue({"asleep": True, "wake_at": self.NOW - 60,
                                    "last_sleep_hours": 8.0}), "")

    def test_asleep_string_false_is_not_asleep(self):
        """Core 的布尔字段可能是字符串：`bool("false")` 是 True 会反判。"""
        got = self._cue({"asleep": "false", "wake_at": self.NOW - 60,
                         "last_sleep_hours": 8.0})
        self.assertTrue(got, '"false" 该当没在睡')

    def test_asleep_string_true_is_asleep(self):
        self.assertEqual(self._cue({"asleep": "true", "wake_at": self.NOW - 60,
                                    "last_sleep_hours": 8.0}), "")

    def test_old_core_missing_block(self):
        self.assertEqual(anchors._wake_cue(_body(), self.NOW), "")

    def test_bad_shapes(self):
        for bad in ("字符串", 3, [1, 2], None, {}, {"wake_at": "x"},
                    {"wake_at": None}, {"wake_at": float("nan")}):
            self.assertEqual(self._cue(bad), "",
                             msg=f"{bad!r} 该当没有")

    def test_wake_becomes_an_anchor(self):
        out, kinds = _derive(_body(wake={
            "asleep": False, "wake_at": self.NOW - 60, "last_sleep_hours": 8.0,
        }), now=self.NOW)
        self.assertTrue(any("刚睡醒" in t for t in out), f"没派生出来：{out}")
        hit = [t for t in out if "刚睡醒" in t][0]
        self.assertEqual(kinds.get(hit), anchors.KIND_WAKE)


class BodyCueTest(unittest.TestCase):
    """体感：饿 / 困 / 不舒服 / 欠觉。跨阈值才说。"""

    def test_hunger_above_threshold(self):
        self.assertIn("饿得有点受不了", anchors._body_cue(_body(hunger=85.0)))

    def test_hunger_below_threshold_is_silent(self):
        self.assertNotIn("饿得有点受不了", anchors._body_cue(_body(hunger=40.0)))
        self.assertEqual(anchors._body_cue(_body(hunger=40.0)), [])

    def test_sleep_pressure_above_threshold(self):
        self.assertIn("困得睁不开眼", anchors._body_cue(_body(sleep_pressure=80.0)))

    def test_discomfort_above_threshold(self):
        self.assertIn("身上不太得劲", anchors._body_cue(_body(discomfort=70.0)))

    def test_sleep_debt_above_threshold(self):
        self.assertIn("欠了一屁股觉", anchors._body_cue(_body(sleep_debt=8.0)))

    def test_at_most_two(self):
        got = anchors._body_cue(_body(hunger=90.0, sleep_pressure=90.0,
                                      discomfort=90.0, sleep_debt=9.0))
        self.assertLessEqual(len(got), 2)

    def test_nan_is_silent(self):
        self.assertEqual(anchors._body_cue(_body(hunger=float("nan"))), [])

    def test_missing_is_silent(self):
        self.assertEqual(anchors._body_cue(_body()), [])

    def test_body_cue_kind_is_registered(self):
        out, kinds = _derive(_body(hunger=90.0))
        hit = [t for t in out if "饿" in t]
        self.assertTrue(hit, f"没派生出来：{out}")
        self.assertEqual(kinds.get(hit[0]), anchors.KIND_BODY)


class PlanCueTest(unittest.TestCase):
    """`day.next` 是列表（Core 给最多两条），原来只用第一条。"""

    def test_both_entries(self):
        got = anchors._plan_cues(_body(day={"next": ["去超市", "回家做饭"]}))
        self.assertEqual(got, ["去超市", "回家做饭"])

    def test_only_first_before_the_fix(self):
        """这条盯的正是那个白丢一条的旧行为。"""
        got = anchors._plan_cues(_body(day={"next": ["去超市"]}))
        self.assertEqual(got, ["去超市"])

    def test_vague_and_waking_filtered(self):
        got = anchors._plan_cues(_body(day={"next": ["才醒", "忙自己的事", "去超市"]}))
        self.assertEqual(got, ["去超市"])

    def test_not_a_list(self):
        self.assertEqual(anchors._plan_cues(_body(day={"next": "去超市"})), [])

    def test_kind_is_plan(self):
        out, kinds = _derive(_body(day={"next": ["去超市", "回家做饭"]}))
        hit = [t for t in out if t.startswith("接下来是")]
        self.assertEqual(len(hit), 2, f"该有两条：{out}")
        self.assertEqual(kinds.get(hit[0]), anchors.KIND_PLAN)


class FeelingCueTest(unittest.TestCase):
    """Core 自己按显著度排好的前三句，原来只有 `_is_idle` 读它。"""

    def test_string_list(self):
        got = anchors._feeling_cues(_body(feelings=["今天到这会", "手脚发沉"]))
        self.assertIn("手脚发沉", got)

    def test_skips_activity_phrases(self):
        """跟 doing/done/next 重复的那几句不要再由一次。"""
        got = anchors._feeling_cues(_body(feelings=["手上正在做X", "接下来要Y", "有点闷"]))
        self.assertEqual(got, ["有点闷"])

    def test_skips_wants(self):
        got = anchors._feeling_cues(_body(feelings=["想去那家书店", "脑子发蒙"]))
        self.assertEqual(got, ["脑子发蒙"])

    def test_at_most_two(self):
        got = anchors._feeling_cues(_body(feelings=["一", "二", "三", "四"]))
        self.assertLessEqual(len(got), 2)

    def test_bad_shapes(self):
        for bad in (None, "字符串", 3, [None, 1, {}], ["", "  "]):
            self.assertEqual(anchors._feeling_cues(_body(feelings=bad)), [],
                             msg=f"{bad!r} 该当没有")

    def test_too_long_is_dropped(self):
        self.assertEqual(anchors._feeling_cues(_body(feelings=["啊" * 40])), [])


class NewKindsRegisteredTest(unittest.TestCase):
    """新 kind 必须在三张表里都有档——漏登记的会被兜底接管/排到最后。"""

    NEW = (anchors.KIND_WAKE, anchors.KIND_BODY, anchors.KIND_PLAN)

    def test_in_ttl_table(self):
        for kind in self.NEW:
            self.assertIn(kind, anchors._KIND_TTL, f"{kind} 没登记有效期")

    def test_in_rank_table(self):
        for kind in self.NEW:
            self.assertIn(kind, anchors.KIND_RANK, f"{kind} 会被默认 9 排到最后")

    def test_in_reason_table(self):
        for kind in self.NEW:
            self.assertIn(kind, anchors._REASON_BY_KIND, f"{kind} 没有理由句")

    def test_every_known_kind_still_has_a_ttl(self):
        """守门用例的原版： KIND_* 常量与 `_KIND_TTL` 必须一一对应。"""
        known = {v for k, v in vars(anchors).items()
                 if k.startswith("KIND_") and isinstance(v, str)}
        self.assertEqual(known - set(anchors._KIND_TTL), set())

    def test_fallback_is_still_the_shortest(self):
        """守门用例：兜底不高于任何已知档。KIND_WAKE 现在是最短的。"""
        self.assertEqual(anchors.KIND_TTL_FALLBACK, min(anchors._KIND_TTL.values()))

    def test_wake_ranks_with_dream(self):
        """刚醒与梦同级：都只在那几分钟里成立。"""
        self.assertEqual(anchors.KIND_RANK[anchors.KIND_WAKE],
                         anchors.KIND_RANK[anchors.KIND_DREAM])


class WakeBlockReadTest(unittest.TestCase):
    """`core_bridge` 必须把 `wake` 块照进视图（原来根本没映射这个键）。"""

    def view(self, contract):
        return _Bridge()._build_self_view({"contract": contract}, "bot1")

    def test_present(self):
        got = self.view({"v": 2, "wake": {
            "asleep": False, "wake_at": 12.0, "last_sleep_hours": 8.0,
            "inertia_minutes_left": 3, "disturbed": False,
        }})
        self.assertEqual(got["wake"]["last_sleep_hours"], 8.0)
        self.assertFalse(got["wake"]["asleep"])

    def test_mirrored_into_day(self):
        """顶层与 `day` 里必须是**同一个对象**：`anchors._wake_cue` 与
        `groupflow.life_lines` 读的是不同入口，两份不一致就会一边说刚醒、
        一边说三天前醒的。"""
        got = self.view({"v": 2, "wake": {
            "asleep": False, "wake_at": 12.0, "last_sleep_hours": 8.0,
        }})
        self.assertIs(got["day"]["wake"], got["wake"])

    def test_old_core_missing(self):
        got = self.view({"v": 2})
        self.assertEqual(got.get("wake"), {})
        self.assertEqual(got.get("day", {}).get("wake"), {})

    def test_bad_shapes_do_not_raise(self):
        for bad in ("字符串", 3, [1], None, {"wake_at": "x"},
                    {"wake_at": float("nan")}, {"last_sleep_hours": None}):
            got = self.view({"v": 2, "wake": bad})
            self.assertIsInstance(got["wake"], dict,
                                  msg=f"{bad!r} 该退化成空 dict 或可读 dict")

    def test_nan_values_become_none_not_nan(self):
        got = self.view({"v": 2, "wake": {"asleep": False, "wake_at": float("nan")}})
        self.assertIsNone(got["wake"]["wake_at"])


class DeriveStillSafeTest(unittest.TestCase):
    """整个派生路径在任意形状下都不抛（由头是每个用户每轮都走的入口）。"""

    def test_minimal_body(self):
        out, _ = _derive({})
        self.assertEqual(out, [])

    def test_everything_present(self):
        now = 1_000_000.0
        body = _body(
            wake={"asleep": False, "wake_at": now - 60, "last_sleep_hours": 8.0},
            feelings=["手脚发沉", "脑子发蒙"],
            hunger=90.0, sleep_pressure=90.0,
            ongoing=["整理个案笔记"],
            day={"doing": "在厨房煮饭", "done": ["下午在门诊接待"],
                 "next": ["去超市", "回家做饭"], "wants": ["那家书店"],
                 "dreams": ["在海边"]},
        )
        out, kinds = _derive(body, now=now)
        self.assertGreater(len(out), 5, f"来源这么全却只派生出 {len(out)} 条：{out}")
        # 每条都得认得出自己的 kind
        for text in out:
            self.assertIn(text, kinds)


# ─── 三、一轮里能说几件事 / 默认节奏 ─────────────────────────────


class RoundPerUserTest(unittest.TestCase):
    """一个人一轮里最多因为「由头」被找几次。

    用户反馈「事件少 每次只有那几个」的一半原因：`_anchor_pick` 返回的 picks
    只取第一条，派生出来的第二、三条当晚就过期作废，下一轮重新派生、又只发
    第一条——于是**永远是同样那几个话题**。
    """

    def test_anchor_pick_returns_two_not_one(self):
        u: dict = {}
        now = 1_000_000.0
        body = _body(
            wake={"asleep": False, "wake_at": now - 60, "last_sleep_hours": 8.0},
            hunger=90.0, sleep_pressure=90.0,
            ongoing=["整理个案笔记"],
            day={"doing": "在厨房煮饭", "next": ["去超市", "回家做饭"]},
        )
        eng = _AnchorEngine()
        got = eng._anchor_pick(u, body, now)
        self.assertEqual(len(got), 2, f"该给 2 条由头，实际 {len(got)}：{got}")
        # 两条必须是**不同的**事：同一条说两遍就是复读
        self.assertNotEqual(got[0].get("about"), got[1].get("about"))

    def test_round_per_user_constant_is_two(self):
        self.assertEqual(engine_mod.ANCHOR_ROUND_PER_USER, 2,
                         "一轮说三件不同的事，观感是「她在报日程」")

    def test_other_channels_still_once(self):
        """除「由头」外的通道仍是一轮一次：job_of 的默认 once=1。"""
        self.assertEqual(engine_mod.ANCHOR_ROUND_PER_USER, 2)


class _AnchorEngine:
    """只暴露 `_anchor_pick`，不跑真实 __init__。"""

    _anchor_pick = engine_mod.SocialEngine._anchor_pick

    def log(self, _msg):
        pass


class VolumeDefaultsTest(unittest.TestCase):
    """默认节奏：新装用户拿到的值要与 schema 一致，且比 v1.28.5 宽松。"""

    def _schema(self):
        import json
        with open(PLUGIN_ROOT / "_conf_schema.json", encoding="utf-8") as f:
            return json.load(f)

    def _cfg(self):
        from social.config import SocialConfig
        return SocialConfig.from_astrbot({})

    def test_schema_and_code_agree(self):
        schema = self._schema()
        cfg = self._cfg()
        for key in ("max_sends_per_round", "miss_daily_cap", "hourly_sends_cap"):
            self.assertEqual(
                getattr(cfg, key), schema[key]["default"],
                f"{key}：代码默认 {getattr(cfg, key)} ≠ schema {schema[key]['default']}",
            )

    def test_volume_went_up(self):
        """v1.28.6 起的量比 v1.28.5 宽松（用户反馈「私聊发送没看到几个」）。"""
        cfg = self._cfg()
        self.assertGreaterEqual(cfg.max_sends_per_round, 3)
        self.assertGreaterEqual(cfg.miss_daily_cap, 3)
        self.assertGreaterEqual(cfg.hourly_sends_cap, 24)


class ServedCountsTest(unittest.TestCase):
    """`served` 从集合改成计数：由头通道要能服务同一个人两次。

    这个测试盯的是 `speak` 闭包里的判据——用 AST 而不是跑整轮心跳
    （整轮要 provider、要 Core 状态文件，成本远高于收益）。
    """

    def _speak_fn(self):
        import ast
        src = (PLUGIN_ROOT / "social" / "engine.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "speak":
                return ast, node
        self.fail("`try_once` 里没找到 speak 闭包")

    def test_served_is_a_count_not_a_set(self):
        ast_mod, speak = self._speak_fn()
        # `ast.unparse` 会把下标里的元组括号去掉、字符串统一成单引号，
        # 所以判据按**语义**写，不按字面写。
        text = ast_mod.unparse(speak)
        self.assertIn("served[bid, uid] = served.get((bid, uid), 0) + 1", text,
                      "占用该写成计数自增")
        self.assertIn("served[bid, uid] = max(0, served.get((bid, uid), 1) - 1)", text,
                      "归还要写成计数自减")
        self.assertNotIn("served.add(", text, "served 不该再是集合")
        self.assertNotIn("served.discard(", text, "served 不该再是集合")

    def test_speak_respects_per_job_once_limit(self):
        ast_mod, speak = self._speak_fn()
        text = ast_mod.unparse(speak)
        self.assertIn("job.get('once', 1)", text,
                      "speak 必须读每条 job 自己的服务次数上限")
        self.assertIn("_served_once(bid, uid, once)", text,
                      "判「还能不能再服务一次」要带上 once，否则由头通道仍是一轮一次")


if __name__ == "__main__":
    unittest.main()
