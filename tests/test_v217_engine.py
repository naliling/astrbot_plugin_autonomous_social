"""v2.1.7：engine.py 审核修复的回归测试。

为什么单开一份：P1-03（会话补写插入位置）、P1-04（本地跳过被当成发送失败）、
P1-05（特别关心小时额度按角色算）都是「用户能直接感觉到」的行为缺陷，而且都藏在
`_speak_inner` / `_send_with_retry` 这种几百行的大函数里——只靠读代码回归不了，
必须有断言钉住。

构造方式沿用 tests/test_v12733_engine.py（真 Engine + 可控时钟）与
tests/test_v1273_review_fixes.py（最小桩）两套现成做法。
"""
from __future__ import annotations

import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

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
    api.logger = logging.getLogger("autonomous_social_engine_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import engine as engine_mod  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import (  # noqa: E402
    SocialEngine,
    _SendResult,
    _is_local_skip,
    _splice_ledger_into_session,
)

NOW = 1_800_000_000.0


class _Clock:
    """可控时间源；engine 的 `_time()` 会做单调钳制，所以回拨也测得了。"""

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


class SpliceLedgerTest(unittest.TestCase):
    """P1-03：缺条目按 ts 插回主干，未知时刻的主干条目沉到最后、不把缺条目顶到最前。"""

    @staticmethod
    def _texts(items):
        return [item["text"] for item in items]

    def test_unknown_slot_does_not_push_missing_entries_to_the_front(self):
        # 会话库第一条是导入的历史（没有 ts），账本补一条她刚主动说的话。
        session = [
            {"dir": "user", "text": "导入的历史"},
            {"dir": "user", "text": "她刚回的话"},
        ]
        ledger = [
            {"dir": "assistant", "text": "她刚主动说的", "ts": 1500.0},
            {"dir": "user", "text": "她刚回的话", "ts": 1000.0},
        ]
        got = _splice_ledger_into_session(session, ledger, 20)
        self.assertEqual(
            self._texts(got),
            ["她刚回的话", "她刚主动说的", "导入的历史"],
            "导入的历史没有时刻，应该沉到最后；缺条目不能被它顶到整段历史最前",
        )

    def test_missing_entry_older_than_every_anchor_goes_first(self):
        session = [{"dir": "user", "text": "她刚回的话"}]
        ledger = [
            {"dir": "assistant", "text": "她更早主动说的", "ts": 500.0},
            {"dir": "user", "text": "她刚回的话", "ts": 1000.0},
        ]
        got = _splice_ledger_into_session(session, ledger, 20)
        self.assertEqual(self._texts(got), ["她更早主动说的", "她刚回的话"])

    def test_same_ts_missing_entries_keep_ledger_order(self):
        session = [{"dir": "user", "text": "对方后来的回复"}]
        ledger = [
            {"dir": "assistant", "text": "连发第一句", "ts": 2000.0},
            {"dir": "assistant", "text": "连发第二句", "ts": 2000.0},
            {"dir": "user", "text": "对方后来的回复", "ts": 3000.0},
        ]
        got = _splice_ledger_into_session(session, ledger, 20)
        self.assertEqual(
            self._texts(got), ["连发第一句", "连发第二句", "对方后来的回复"]
        )

    def test_limit_keeps_the_newest_tail(self):
        session = [
            {"dir": "user", "text": "第一句"},
            {"dir": "assistant", "text": "第二句"},
            {"dir": "user", "text": "第三句"},
        ]
        got = _splice_ledger_into_session(session, [], 2)
        self.assertEqual(self._texts(got), ["第二句", "第三句"])


class LocalSkipTest(unittest.IsolatedAsyncioTestCase):
    """P1-04：本地主动跳过必须打标，调用方不能把它记成「这个人发不出去」。"""

    async def _call(self, eng, patch_obj, attr, value, target="aiocqhttp:FriendMessage:u1"):
        with mock.patch.object(patch_obj, attr, value):
            return await eng._send_with_retry(target, "hi", "b1")

    async def test_quiet_hours_is_local_skip(self):
        eng = _engine()
        res = await self._call(
            eng,
            SocialEngine,
            "_quiet_send_block",
            lambda self, bid, target="": "现在是安静时段",
        )
        self.assertFalse(res[0])
        self.assertFalse(res[2], "安静时段不是「对这个人发不了」，不能触发隔离")
        self.assertTrue(_is_local_skip(res))
        self.assertEqual(eng.send_breaker_left("b1"), 0.0)

    async def test_per_user_cooldown_is_local_skip(self):
        eng = _engine()
        with mock.patch.object(
            SocialEngine,
            "_resolve_send_user",
            lambda self, target, bid="": {"__uid": "u1", "send_fail_until": eng._time() + 600},
        ), mock.patch.object(
            SocialEngine, "_send_fail_left", lambda self, u: 600.0
        ):
            res = await eng._send_with_retry("aiocqhttp:FriendMessage:u1", "hi", "b1")
        self.assertFalse(res[0])
        self.assertTrue(_is_local_skip(res))
        self.assertEqual(eng.send_breaker_left("b1"), 0.0)

    async def test_bad_umo_is_local_skip(self):
        eng = _engine()
        res = await self._call(
            eng, engine_mod, "_bad_umo", lambda target: "缺少会话类型"
        )
        self.assertFalse(res[0])
        self.assertTrue(_is_local_skip(res))

    async def test_platform_circuit_is_local_skip(self):
        eng = _engine()
        res = await self._call(
            eng,
            SocialEngine,
            "_platform_circuit",
            lambda self, now=None: (True, "平台熔断中"),
        )
        self.assertFalse(res[0])
        self.assertTrue(_is_local_skip(res))
        self.assertEqual(eng.send_breaker_left("b1"), 0.0)

    def test_plain_tuple_from_old_stub_is_not_local_skip(self):
        # 旧测试的桩直接返回普通三元组：getattr 兜底必须当成「不是本地跳过」。
        self.assertFalse(_is_local_skip((False, "网络错误", False)))
        self.assertFalse(_is_local_skip((True, "", False)))

    def test_send_result_unpacks_like_a_tuple(self):
        # 既有测试与调用方都按三元组解包，子类不能破坏这个契约。
        ok, err, peer = _SendResult(False, "安静时段", False, local_skip=True)
        self.assertFalse(ok)
        self.assertEqual(err, "安静时段")
        self.assertFalse(peer)


class SpeakInnerLocalSkipTest(unittest.IsolatedAsyncioTestCase):
    """P1-04 的调用侧：本地跳过不许写投递账本、失败计数、放弃记录，但额度要退。"""

    async def _speak(self, eng, res):
        u = eng.state.user("b1", "u1")
        u["umo"] = "aiocqhttp:FriendMessage:u1"
        u["__uid"] = "u1"
        u["tier"] = "mid"
        # 真引擎没有 LLM provider：让「模型决定说」这一步直接放行，把测试钉在
        # 发送失败后的记账分支上（这正是 P1-04/P2-13 要守的地方）。
        decision = types.SimpleNamespace(
            send=True, parts=["由头句"], why_not="", raw_text="", plan=None
        )
        with mock.patch.object(
            SocialEngine, "_send_with_retry", mock.AsyncMock(return_value=res)
        ), mock.patch.object(
            SocialEngine, "_decide_proactive", mock.AsyncMock(return_value=(decision, []))
        ), mock.patch.object(SocialEngine, "_hourly_reserve", lambda self, *a, **k: True):
            return await eng._speak_inner(
                "b1", "u1", u, 50.0, eng._time(),
                preset=("由头句", {"category": "greet"}),
            )

    async def test_local_skip_keeps_no_failure_accounts(self):
        eng = _engine()
        ok, why = await self._speak(
            eng, _SendResult(False, "现在是安静时段", False, local_skip=True)
        )
        self.assertFalse(ok)
        self.assertIn("没发", why)
        u = eng.state.user("b1", "u1")
        self.assertEqual(u.get("send_fail_count", 0), 0)
        self.assertEqual(u.get("giveup_count", 0), 0)
        self.assertEqual(u.get("send_fail_until", 0), 0)
        self.assertFalse(
            eng._delivery_rows("b1", "u1", now=eng._time()),
            "没尝试发出去，不该写投递账本",
        )

    async def test_real_failure_still_books_the_delivery_ledger(self):
        eng = _engine()
        await self._speak(eng, _SendResult(False, "网络错误", False))
        self.assertTrue(
            eng._delivery_rows("b1", "u1", now=eng._time()), "真实失败要留一条投递记录"
        )


class SpecialCareHourlyTest(unittest.TestCase):
    """P1-05：角色总闸（24）与按人小闸（6）分两层：互不挤占，但都要过。"""

    def test_sc_user_can_still_send_after_role_sent_to_others(self):
        eng = _engine(hourly_sends_cap=24, special_care_hourly_cap=6)
        for _ in range(7):
            self.assertTrue(eng._hourly_reserve("b1", "u_other", special_care=False))
        # 旧实现：SC 额度直接把角色总闸覆盖成 6，角色已发 7 条 → TA 被挡死。
        self.assertTrue(
            eng._hourly_reserve("b1", "u_sc", special_care=True),
            "角色给别人发过 7 条之后，名单用户仍该有自己的 6 条",
        )

    def test_sc_user_is_blocked_by_their_own_six(self):
        eng = _engine(hourly_sends_cap=24, special_care_hourly_cap=6)
        for _ in range(6):
            self.assertTrue(eng._hourly_reserve("b1", "u_sc", special_care=True))
        self.assertFalse(eng._hourly_reserve("b1", "u_sc", special_care=True))
        self.assertEqual(len(eng._hourly_user[("b1", "u_sc")]), 6)
        self.assertEqual(len(eng._hourly["b1"]), 6)

    def test_role_wide_cap_still_limits_sc_user(self):
        eng = _engine(hourly_sends_cap=3, special_care_hourly_cap=6)
        self.assertTrue(eng._hourly_reserve("b1", "u_sc", special_care=True))
        self.assertTrue(eng._hourly_reserve("b1", "u_sc", special_care=True))
        self.assertTrue(eng._hourly_reserve("b1", "u_sc", special_care=True))
        self.assertFalse(eng._hourly_reserve("b1", "u_sc", special_care=True))

    def test_refund_returns_both_windows(self):
        eng = _engine(hourly_sends_cap=24, special_care_hourly_cap=1)
        self.assertTrue(eng._hourly_reserve("b1", "u_sc", special_care=True))
        self.assertFalse(eng._hourly_reserve("b1", "u_sc", special_care=True))
        eng._hourly_refund("b1", "u_sc", special_care=True)
        self.assertTrue(
            eng._hourly_reserve("b1", "u_sc", special_care=True),
            "两层都要退，只退一边会让角色总闸或按人窗口继续被占着",
        )

    def test_old_single_arg_calls_keep_working(self):
        eng = _engine(hourly_sends_cap=2)
        self.assertEqual(
            [eng._hourly_reserve("b1") for _ in range(3)], [True, True, False]
        )
        eng._hourly_refund("b1")
        self.assertTrue(eng._hourly_reserve("b1"))
        self.assertEqual(eng._hourly_left("b1", eng._time()), 0)
        self.assertEqual(eng._hourly_user, {})

    def test_cap_off_still_keeps_no_ledger(self):
        eng = _engine(hourly_sends_cap=0)
        for _ in range(50):
            self.assertTrue(eng._hourly_reserve("b1"))
        self.assertFalse(eng._hourly.get("b1"))
        self.assertEqual(eng._hourly_left("b1", eng._time()), 999999)

    def test_refund_does_not_pop_a_stale_record(self):
        # P3-13：退之前先按一小时窗口裁剪，别把一小时前的老记录当成刚占的退掉。
        clock = _Clock()
        eng = _engine(clock=clock, hourly_sends_cap=2)
        self.assertTrue(eng._hourly_reserve("b1"))
        self.assertTrue(eng._hourly_reserve("b1"))
        clock.advance(3601)
        self.assertTrue(eng._hourly_reserve("b1"))
        self.assertEqual(len(eng._hourly["b1"]), 1)
        eng._hourly_refund("b1")
        self.assertFalse(eng._hourly.get("b1"), "退掉的应该是刚占的那条")


class ZeroMeansOffTest(unittest.TestCase):
    """P2-09 / P2-10：0 是「关掉」，不是「没配」——私聊与群聊必须同一口径。"""

    def test_group_context_inject_zero_injects_nothing(self):
        eng = _engine(context_inject_count=0)
        g = {"samples": [{"name": "A", "text": "hi"}, {"name": "B", "text": "yo"}]}
        self.assertEqual(eng._build_group_ctx("b1", "g1", g)["recent"], [])

    def test_group_context_inject_one_keeps_only_the_last(self):
        eng = _engine(context_inject_count=1)
        g = {"samples": [{"name": "A", "text": "hi"}, {"name": "B", "text": "yo"}]}
        self.assertEqual(
            [r["text"] for r in eng._build_group_ctx("b1", "g1", g)["recent"]],
            ["yo"],
        )

    def test_miss_affection_min_zero_disables_the_floor(self):
        # 旧写法 `or 42` 把 0 变成 42：面板上填 0 的人以为关了，实际还在按 42 卡。
        self.assertEqual(_engine(miss_affection_min=0)._miss_floor("high"), 0.0)


class QuietWindowIntersectionTest(unittest.TestCase):
    """P3-01：两个安静窗口部分重叠时，续聊顺延不能把重叠那段算两遍。"""

    DAY = 86400
    START = 20000 * DAY
    END = START + DAY

    def test_partial_overlap_is_subtracted_once(self):
        # 22–6 与 23–7 的交集是 23–6 = 7 小时；偏移 0 = 按 UTC 墙上时间切。
        got = SocialEngine._quiet_intersection_seconds(
            self.START, self.END, (22.0, 6.0, 0), (23.0, 7.0, 0)
        )
        self.assertAlmostEqual(got, 7 * 3600.0, delta=1.0)

    def test_disjoint_windows_have_no_intersection(self):
        got = SocialEngine._quiet_intersection_seconds(
            self.START, self.END, (1.0, 3.0, 0), (10.0, 12.0, 0)
        )
        self.assertEqual(got, 0.0)


class SendRetryCountTest(unittest.IsolatedAsyncioTestCase):
    """P3-09：SEND_RETRY_COUNT 必须真的控制尝试次数（旧代码写死只发一次）。"""

    @staticmethod
    def _fail_factory(calls):
        async def _fail(_self, target, text):
            calls.append(text)
            raise RuntimeError("boom")

        return _fail

    async def test_retry_count_is_honoured(self):
        eng = _engine()
        calls = []
        with mock.patch.object(
            engine_mod.SocialEngine, "_send_message", self._fail_factory(calls)
        ), mock.patch.object(engine_mod, "SEND_RETRY_COUNT", 2), mock.patch.object(
            engine_mod, "SEND_RETRY_DELAY", 0.0
        ):
            ok, err, peer = await eng._send_with_retry(
                "aiocqhttp:FriendMessage:u1", "hi", "b1"
            )
        self.assertFalse(ok)
        self.assertIn("boom", err)
        self.assertFalse(peer)
        self.assertEqual(len(calls), 3, "配置重试 2 次 = 总共尝试 3 次")

    async def test_default_retry_count_sends_once(self):
        # 默认 0 必须还是只发一次：修好常量不能顺手让线上突然多发。
        eng = _engine()
        calls = []
        with mock.patch.object(
            engine_mod.SocialEngine, "_send_message", self._fail_factory(calls)
        ):
            await eng._send_with_retry("aiocqhttp:FriendMessage:u1", "hi", "b1")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
