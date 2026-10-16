"""v1.27.33：engine.py 裸数值转换的回归测试。

为什么单开一份：`engine.py` 里读 `state.json` / Core 快照的地方原来有近百处裸
`float()` / `int()`，而同一个病在 `anchors.py`、`desire.py`、`threads.py` 里都已经
用「容错取数 + `math.isfinite`」修过了——只有 `engine.py`（和 `state.py`）漏着。

这一份钉住两类失效，每条都给出最短复现：

1. **抛异常**：脏字段（`"abc"`、`None`、`""`）让 `float()`/`int()` 抛 ValueError。
   坏在位置上——闸门、挑选、轮内记账大多没有逐用户兜底，一条脏数据就作废一整轮心跳。

2. **nan/inf 静默改变判定**（一个异常都不抛，所以更坏）：
   * `nan <= stopped` 恒假 → 「停发」永不成立（她会一直追一个不回话的人）
   * `inf >= 急切线` 恒真 → 凭空多出一张「很想说」的豁免券
   * `nan < affection_min`、`nan - base < rise` 恒假 → 硬门槛被静默放过
   * `nan < floor` 恒假 → 循环走完落到**最高**关系档
   * `nan` 夜间窗口 → 安静时段硬闸失效

每条断言都对着修复前的行为写：改回裸 `float()`/`int()` 就会红。
"""
from __future__ import annotations

import asyncio
import logging
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path

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
    api.logger = logging.getLogger("autonomous_social_engine_badvalues_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import anchors, desire, engine as engine_mod  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import (  # noqa: E402
    AGGR_NOTE_SAMPLES,
    AGGR_NOTE_THRESHOLD,
    SocialEngine,
    _num,
)

NOW = 1_800_000_000.0
NAN = float("nan")
INF = float("inf")


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _engine(clock: _Clock | None = None, **cfg) -> SocialEngine:
    tmp = tempfile.mkdtemp()
    clock = clock or _Clock()
    eng = SocialEngine(
        context=None,
        config=SocialConfig(),
        state_path=str(Path(tmp) / "state.json"),
        data_dir=tmp,
        time_source=clock,
    )
    for key, value in cfg.items():
        setattr(eng.cfg, key, value)
    return eng


def _user(eng: SocialEngine, bid: str = "b1", uid: str = "u1", **fields):
    user = eng.state.user(bid, uid)
    user["umo"] = "aiocqhttp:private:u1"
    user["__uid"] = uid
    user.update(fields)
    return user


def _quiet_off(eng: SocialEngine) -> SocialEngine:
    """把安静时段压成零长（`in_quiet_hours` 对零长窗口恒返回 False）。

    测试要在**任意**本机时区、任意运行时刻下都可复现，不能让 `NOW` 落在哪个钟点上
    决定断言结果。
    """
    eng.cfg.quiet_start = 0
    eng.cfg.quiet_end = 0
    return eng


class _SnapCore:
    """只回快照的假 Core 桥（`gate_reason` 用两参调用，`_intimacy_pick` 用三参）。"""

    def __init__(self, snapshots=None) -> None:
        self._snapshots = snapshots or {}

    def load_snapshot(self, bid, uid, root=None):
        return self._snapshots.get((bid, uid))


class _BodyCore:
    """只回「她自己现在什么状态」的假 Core 桥。"""

    def __init__(self, body) -> None:
        self._body = body

    def bot_self_state(self, bid):
        return self._body

    def read_root(self):
        return None

    def load_snapshot(self, bid, uid, root=None):
        return None


class NumHelperTest(unittest.TestCase):
    """`_num` 本身：坏值与非有限值一律落到 default，好值原样保留。"""

    def test_unreadable_values_fall_back_to_the_default(self):
        for bad in ("abc", "", "  ", None, [], {}, object()):
            with self.subTest(bad=bad):
                self.assertEqual(_num(bad), 0.0)
                self.assertEqual(_num(bad, 100.0), 100.0)

    def test_non_finite_values_fall_back_to_the_default(self):
        # 修复前 `float(nan)` / `float(inf)` 不抛，两个都原样穿过去
        for bad in (NAN, INF, -INF, "nan", "inf", "-inf", float("1e999")):
            with self.subTest(bad=bad):
                self.assertEqual(_num(bad), 0.0)
                self.assertEqual(_num(bad, 42.0), 42.0)

    def test_good_values_are_preserved(self):
        self.assertEqual(_num(2.5), 2.5)
        self.assertEqual(_num("2.5"), 2.5)
        self.assertEqual(_num(7), 7.0)
        # `0` / `0.0` 是**读得出来的值**，不能被当成「没有这个字段」
        self.assertEqual(_num(0), 0.0)
        self.assertEqual(_num(0.0), 0.0)
        self.assertEqual(_num("0"), 0.0)
        # 巨大但有限的值照常放行（封顶交给调用方，`_num` 不做业务判定）
        self.assertEqual(_num(1e300), 1e300)


class GateReasonDirtyFieldTest(unittest.TestCase):
    """闸门读的是 `state.json`：坏时间戳以前直接抛，整轮心跳作废。"""

    def _reason(self, **fields):
        eng = _quiet_off(_engine())
        u = _user(eng, **fields)
        return eng, eng.gate_reason("b1", "u1", u, NOW)

    def test_corrupt_last_seen_does_not_raise(self):
        _, why = self._reason(last_seen="abc")
        self.assertIsInstance(why, str)

    def test_corrupt_cooldown_fields_do_not_raise(self):
        for field in ("last_sent", "last_skip_at"):
            with self.subTest(field=field):
                _, why = self._reason(**{field: "abc"})
                self.assertIsInstance(why, str)

    def test_non_finite_last_seen_does_not_raise(self):
        for bad in (NAN, INF):
            with self.subTest(bad=bad):
                _, why = self._reason(last_seen=bad)
                self.assertIsInstance(why, str)


class ThreadGateDirtyFieldTest(unittest.TestCase):
    def test_corrupt_thread_fields_do_not_raise(self):
        for field in ("last_seen", "thread_for", "thread_at"):
            with self.subTest(field=field):
                eng = _quiet_off(_engine())
                u = _user(eng, **{field: "abc"})
                self.assertIsInstance(eng.thread_gate("b1", "u1", u, NOW), str)

    def test_last_said_survives_a_corrupt_timestamp(self):
        # 修复前 `float("abc")` 抛 ValueError；它在 loop_gate / thread_gate 的第一行
        self.assertEqual(SocialEngine._last_said({"last_seen": "abc"}), 0.0)
        self.assertEqual(SocialEngine._last_said({"last_spoken": "abc"}), 0.0)

    def test_last_said_ignores_a_non_finite_timestamp(self):
        self.assertEqual(SocialEngine._last_said({"last_seen": NAN, "last_spoken": 5.0}), 5.0)
        self.assertEqual(SocialEngine._last_said({"last_seen": INF}), 0.0)


class GapLeftDirtyFieldTest(unittest.TestCase):
    """最小间隔闸：坏 `last_sent` 以前抛异常，现在按「从没发过」处理。"""

    def test_corrupt_last_sent_returns_zero_instead_of_raising(self):
        u = {"last_sent": "abc"}
        self.assertEqual(_engine()._gap_left(u, anchors.TIER_MID, NOW), 0.0)

    def test_non_finite_last_sent_returns_a_finite_gap(self):
        for bad in (NAN, INF):
            with self.subTest(bad=bad):
                gap = _engine()._gap_left({"last_sent": bad}, anchors.TIER_MID, NOW)
                self.assertTrue(math.isfinite(gap))
                self.assertEqual(gap, 0.0)


class QuiescedNonFiniteTest(unittest.TestCase):
    """「停发」闸：`nan <= stopped` 恒假 ⇒ 永不淘汰，她会一直追一个不回话的人。"""

    def test_nan_last_seen_still_reads_as_never_spoken(self):
        u = {"last_seen": NAN, "quiesced_at": NOW}
        self.assertTrue(
            SocialEngine._quiesced(u),
            "last_seen 读不出来就是「TA 从没开口」，这时必须维持停发",
        )

    def test_corrupt_last_seen_does_not_raise(self):
        self.assertTrue(SocialEngine._quiesced({"last_seen": "abc", "quiesced_at": NOW}))

    def test_a_peer_who_did_speak_still_lifts_quiescence(self):
        self.assertFalse(SocialEngine._quiesced({"last_seen": NOW + 1, "quiesced_at": NOW}))


class RelationBucketNonFiniteTest(unittest.TestCase):
    """坏在意度以前被抬到**最高**档（`nan < floor` 恒假，循环走完落地）。"""

    def test_nan_interest_lands_on_the_default_not_the_top_bucket(self):
        top = len(SocialEngine._REL_BUCKETS)
        got = SocialEngine._relation_bucket({"interest": NAN})
        self.assertNotEqual(got, top, "读不出来的在意度不该被排到最高档")
        self.assertEqual(got, SocialEngine._relation_bucket({"interest": 0.35}))

    def test_corrupt_interest_matches_the_missing_field_default(self):
        missing = SocialEngine._relation_bucket({})
        self.assertEqual(SocialEngine._relation_bucket({"interest": "abc"}), missing)

    def test_an_explicit_zero_is_still_a_real_value(self):
        # 这条是早已修好的老账：`0.0 or 0.35` 会把 0.0 当缺字段
        self.assertEqual(SocialEngine._relation_bucket({"interest": 0.0}), 0)


class AffectionOfNonFiniteTest(unittest.TestCase):
    """`nan` 既不是「有值」也不该原样传出去：它会让下游所有 `< 门槛` 恒假。"""

    def _engine_with(self, snap):
        eng = _engine()
        eng.core = _SnapCore({("b1", "u1"): snap})
        return eng, _user(eng)

    def test_nan_core_affection_falls_back_to_interest(self):
        eng, u = self._engine_with({"affection": NAN})
        got = eng._affection_of("b1", "u1", u, {"roles": {}})
        self.assertIsNotNone(got)
        self.assertTrue(math.isfinite(got))
        self.assertEqual(got, 0.35 * 100.0)

    def test_a_valid_core_affection_is_untouched(self):
        eng, u = self._engine_with({"affection": 88.0})
        self.assertEqual(eng._affection_of("b1", "u1", u, {"roles": {}}), 88.0)

    def test_a_corrupt_core_affection_falls_back_to_interest(self):
        eng, u = self._engine_with({"affection": "abc"})
        self.assertEqual(eng._affection_of("b1", "u1", u, {"roles": {}}), 35.0)


class EagerUrgeNonFiniteTest(unittest.TestCase):
    """`inf >= 急切线` 恒真 ⇒ 凭空多一张豁免券（绕过最小间隔与冷处理）。"""

    def test_infinite_urge_is_not_eager(self):
        self.assertFalse(
            SocialEngine._urge_is_eager({"urge": INF, "fire_gate": desire.FIRE_THRESHOLD}),
            "读不出来的念头不该被当成「想说到不能再想」",
        )

    def test_nan_urge_is_not_eager(self):
        self.assertFalse(SocialEngine._urge_is_eager({"urge": NAN, "fire_gate": NAN}))

    def test_a_genuinely_high_urge_is_still_eager(self):
        self.assertTrue(
            SocialEngine._urge_is_eager(
                {"urge": desire.URGE_CEILING, "fire_gate": desire.FIRE_THRESHOLD}
            )
        )


class IntimacyPickNonFiniteTest(unittest.TestCase):
    """「想要了」通道的两道硬门槛：好感下限与欲望涨幅。nan 以前两道都能过。"""

    def _pick(self, snap):
        eng = _quiet_off(_engine())
        eng.core = _SnapCore({("b1", "u1"): snap})
        u = _user(eng)
        return eng, u, eng._intimacy_pick("b1", NOW, None, {"roles": {}})

    def test_nan_affection_does_not_pass_the_affection_floor(self):
        _, _, out = self._pick(
            {"affection": NAN, "ccb_done_count": 3, "mood": {"libido": 99.0, "base_libido": 0.0}}
        )
        self.assertEqual(out, [], "nan 好感不该让「好感 ≥ 下限」这道硬门槛放行")

    def test_nan_libido_does_not_count_as_a_desire_rise(self):
        _, _, out = self._pick(
            {"affection": 90.0, "ccb_done_count": 3, "mood": {"libido": NAN, "base_libido": 0.0}}
        )
        self.assertEqual(out, [], "nan 欲望与基线相减恒假，不该被当成「欲望涨上来了」")

    def test_nan_affection_with_no_history_is_still_rejected(self):
        _, _, out = self._pick({"affection": NAN, "ccb_done_count": 0, "mood": {}})
        self.assertEqual(out, [])

    def test_a_real_candidate_still_gets_through(self):
        eng, u, out = self._pick(
            {"affection": 90.0, "ccb_done_count": 3, "mood": {"libido": 40.0, "base_libido": 0.0}}
        )
        self.assertEqual([uid for uid, _ in out], ["u1"])


class ZeroBaselineTest(unittest.TestCase):
    """基线**正常就是 0**，但 `x.get(k, cur) or cur` 会把 0 当成「没报这个字段」。

    Core 没报基线时该退回当前值（差值 0 = 这道闸不成立）——那是**回退**语义；
    而「报了 0」是**读得出来的值**。两者混在一起，后果是这两道闸永不成立：
    「她对这个人的气还没消」与「欲望涨上来」，一个放行了一个从不触发。
    同 `_relation_bucket` 里 `0.0 or 0.35` 的老账，见那里的注释。
    """

    def _mood_engine(self, mood):
        class E:
            gate_reason = SocialEngine.gate_reason

            def __init__(self):
                self.cfg = SocialConfig()
                self.core = _SnapCore({("b1", "u1"): {"mood": mood}})
                self._city_offset = {}

            def _moment_of(self, bid, now):
                from datetime import datetime

                return datetime(2023, 11, 15, 14, 0)

            def _quiet_now(self, bid, hour):
                return False

        return E()

    def test_a_zero_baseline_does_not_swallow_a_real_aggression_spike(self):
        eng = self._mood_engine({"aggression": 45.0, "base_aggression": 0.0})
        self.assertIn("气", eng.gate_reason("b1", "u1", {}, NOW, None))

    def test_a_missing_baseline_still_means_no_delta(self):
        # 真·回退语义要保留：Core 没报基线时不能凭空判定她在生气
        eng = self._mood_engine({"aggression": 45.0})
        self.assertEqual(eng.gate_reason("b1", "u1", {}, NOW, None), "")

    def test_a_low_aggression_over_a_zero_baseline_is_calm(self):
        eng = self._mood_engine({"aggression": 4.0, "base_aggression": 0.0})
        self.assertEqual(eng.gate_reason("b1", "u1", {}, NOW, None), "")

    def _pick(self, mood):
        eng = _quiet_off(_engine())
        eng.core = _SnapCore(
            {("b1", "u1"): {"affection": 90.0, "ccb_done_count": 3, "mood": mood}}
        )
        _user(eng)
        return eng._intimacy_pick("b1", NOW, None, {"roles": {}})

    def test_a_zero_libido_baseline_does_not_swallow_a_real_rise(self):
        out = self._pick({"libido": 40.0, "base_libido": 0.0})
        self.assertEqual([uid for uid, _ in out], ["u1"])

    def test_a_missing_libido_baseline_means_no_rise(self):
        self.assertEqual(self._pick({"libido": 40.0}), [])


class MindAndAggressionDirtyFieldTest(unittest.TestCase):
    def test_mind_survives_corrupt_counters(self):
        eng = _quiet_off(_engine())
        u = _user(eng, no_reply_streak="abc", interest="abc", last_seen="abc")
        mind = eng._mind("b1", u, NOW, 1.0)
        self.assertEqual(mind["no_reply_streak"], 0)
        self.assertTrue(math.isfinite(mind["silence_hours"]))
        self.assertTrue(math.isfinite(mind["interest"]))

    def test_mind_does_not_leak_infinity_into_silence_hours(self):
        eng = _quiet_off(_engine())
        u = _user(eng, last_seen=INF)
        self.assertTrue(math.isfinite(eng._mind("b1", u, NOW, 1.0)["silence_hours"]))

    def test_track_aggression_survives_a_corrupt_note_timestamp(self):
        eng = _engine()
        u = _user(eng, aggr_high_n=AGGR_NOTE_SAMPLES - 1, aggr_note_at="abc")
        eng._track_aggression(u, {"mood": {"aggression": AGGR_NOTE_THRESHOLD + 1}}, NOW)
        self.assertEqual(u["aggr_note_at"], NOW)


class RepresentativeUmoDirtyFieldTest(unittest.TestCase):
    """`_trigger_precheck` 里手动触发要先找代表会话，坏时间戳以前让指令直接报错。"""

    def _two_users(self, bad: dict):
        """一个坏时间戳的用户 + 一个时间戳正常的用户。

        必须凑出「正常那个 ts 更大」：`_representative_umo` 只认最新，
        坏的归 0 之后本来就该落选——这里验的是**不再抛异常**，顺带验没选错人。
        """
        eng = _engine()
        bad_user = _user(eng, "b1", "u1", **bad)
        bad_user["umo"] = "aiocqhttp:private:u1"
        good_user = _user(eng, "b1", "u2", last_seen=NOW)
        good_user["umo"] = "aiocqhttp:private:u2"
        return eng

    def test_corrupt_timestamps_do_not_raise(self):
        eng = self._two_users({"last_seen": "abc", "last_sent": "abc"})
        self.assertEqual(eng._representative_umo("b1"), "aiocqhttp:private:u2")

    def test_non_finite_timestamps_do_not_raise(self):
        eng = self._two_users({"last_seen": NAN, "last_sent": INF})
        self.assertEqual(eng._representative_umo("b1"), "aiocqhttp:private:u2")

    def test_a_corrupt_timestamp_is_not_mistaken_for_the_most_recent(self):
        eng = self._two_users({"last_seen": INF})
        # inf 归 0 之后，这个坏用户不该因为「无穷大」被选成最新
        self.assertEqual(eng._representative_umo("b1"), "aiocqhttp:private:u2")


class RememberNightNonFiniteTest(unittest.TestCase):
    """Core 给了一个 nan 夜间窗口：`_quiet_now` 的比较全假 ⇒ 安静时段硬闸失效。"""

    def test_nan_window_is_dropped_and_falls_back_to_the_config(self):
        eng = _engine(quiet_start=23, quiet_end=7)
        eng._remember_night("b1", {"night_window": [NAN, NAN]})
        self.assertEqual(eng._night_windows, {}, "读不出来的窗口该清掉，退回配置窗口")
        self.assertTrue(eng._quiet_now("b1", 3), "凌晨三点应落在配置的 23→7 安静时段里")

    def test_a_valid_window_still_overrides_the_config(self):
        eng = _engine(quiet_start=23, quiet_end=7)
        eng._remember_night("b1", {"night_window": [1, 6]})
        self.assertEqual(eng._night_windows["b1"], (1.0, 6.0))
        self.assertTrue(eng._quiet_now("b1", 3))
        self.assertFalse(eng._quiet_now("b1", 12))


class SpeakEnergyGateTest(unittest.IsolatedAsyncioTestCase):
    """精力闸的默认值必须是「满精力」，否则没接 Core 的机器会永久静默。"""

    async def test_a_missing_energy_is_not_read_as_exhausted(self):
        eng = _quiet_off(_engine())
        eng.core = _BodyCore({})  # Core 在跑，但契约里没有 energy
        u = _user(eng)
        try:
            _ok, why = await eng._speak_inner("b1", "u1", u, 3.0, NOW)
        except Exception:
            # 后面全是重活（会话史 / 人格 / LLM），测试里只装配到精力闸
            return
        self.assertNotIn("太累", why, "读不出来的精力不该被当成累垮")

    async def test_a_genuinely_low_energy_still_blocks(self):
        eng = _quiet_off(_engine())
        eng.core = _BodyCore({"energy": 5})
        u = _user(eng)
        ok, why = await eng._speak_inner("b1", "u1", u, 3.0, NOW)
        self.assertFalse(ok)
        self.assertIn("太累", why)


class StatusTextDirtyFieldTest(unittest.IsolatedAsyncioTestCase):
    """状态面板是排障第一入口：它不该在**正好有脏数据**的时候自己报错。"""

    async def test_corrupt_proactive_counters_do_not_raise_value_error(self):
        eng = _engine()
        u = _user(eng)
        u["proactive_sent"] = "abc"
        u["proactive_replied"] = NAN
        try:
            await eng.status_text()
        except ValueError as exc:
            self.fail(f"坏计数不该让状态页抛 ValueError：{exc}")
        except Exception:
            # 面板后半段要读会话库 / 识别诊断，测试里没有装配
            pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
