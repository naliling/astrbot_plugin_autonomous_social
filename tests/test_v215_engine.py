# -*- coding: utf-8 -*-
"""v2.1.5 engine 接线测试：投递账本、平台熔断、连发收口、投递预算、工具循环、生成后自评。

为什么单独一个文件：这六件是**接线**，不是业务规则——模块本身（outbound / rhythm /
budget / generator）各有自己的测试，这里只验「engine 有没有真的把它们接上、接在
哪个位置、模块缺失/抛异常时是不是 fail-open」。老测试验的是发不发、发什么，混在
一起以后没人敢删。

所有依赖都是假对象：容器里没有 astrbot、没有 Core 包也能跑。
"""

import asyncio
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# 插件根：打包后 tests/ 在插件里；开发时 tests/ 与 social/ 同级。两种摆法都认。
_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub():
    """容器里没有 astrbot，而 social/ 每个模块都 from astrbot.api import logger。"""
    import logging

    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("social_v215_test")
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
from social.engine import SocialEngine  # noqa: E402

NOW = 1_700_000_000.0

# 平台级拒绝的文案：走 outbound 的真实词表，测试里不另造一份（词表改了这里跟着变）。
REFUSED = "retcode 1200 风控拦截"
TARGET_GONE = "不是好友，消息发送失败"
UNKNOWN_ERR = "发送超时，结果未知"
OTHER_ERR = "序列化之后才炸的怪错误"


# ─── 假对象 ──────────────────────────────────────────────────────
class _Recorder:
    def __init__(self):
        self.notes = []
        self.blocks = []

    def _blocked(self, code, text, **detail):
        self.blocks.append((code, text, detail))
        return False, text

    def _note_blocked(self, code, text, **detail):
        self.notes.append((code, text, detail))

    def log(self, message):
        pass

    def codes(self):
        return [code for code, _text, _detail in self.notes]


class _FakeState:
    """只带账本用得到的两个成员：data（dict）与 mark_dirty。"""

    def __init__(self, data=None, has_data=True):
        self.data = dict(data or {})
        self.dirty = 0
        self._has_data = has_data

    def mark_dirty(self):
        self.dirty += 1


class _NoDataState:
    """连 data 都没有的极简状态：账本必须能容忍。"""

    def mark_dirty(self):
        pass


class _FakeGenerator:
    def __init__(
        self,
        parts=None,
        tools=None,
        tools_error=None,
        review=None,
        review_error=None,
        provider="fake-provider",
    ):
        self.parts = parts
        self.tools = tools
        self.tools_error = tools_error
        self.review = review
        self.review_error = review_error
        self.provider = provider
        self.generate_calls = []
        self.tools_calls = []
        self.review_calls = []

    async def generate(self, *args, **kwargs):
        self.generate_calls.append((args, kwargs))
        return list(self.parts or [])

    async def generate_with_tools(self, *args, **kwargs):
        self.tools_calls.append((args, kwargs))
        if self.tools_error:
            raise self.tools_error
        return self.tools

    def review_draft(self, *args, **kwargs):
        self.review_calls.append((args, kwargs))
        if self.review_error:
            raise self.review_error
        return self.review


class _AsyncReviewGenerator(_FakeGenerator):
    async def review_draft(self, *args, **kwargs):
        self.review_calls.append((args, kwargs))
        if self.review_error:
            raise self.review_error
        return self.review


# engine 里这些方法内部都写成 SocialEngine.xxx(self, ...)，所以只要把它们绑到假引擎上
# 就能单独测；其余属性按需给。
_NEW_METHODS = (
    "_delivery_ledger",
    "_delivery_record",
    "_delivery_rows",
    "_platform_events",
    "_platform_event",
    "_platform_circuit",
    "_note_platform_failure",
    "_rhythm_gate",
    "_budget_gate",
    "_generate_parts",
    "_review_draft",
    "_note_send_ok",
    "_note_send_failure",
    "_breaker_slot",
)


def _engine(**over):
    recorder = _Recorder()
    cfg = over.pop("cfg", SimpleNamespace())
    obj = SimpleNamespace(
        cfg=cfg,
        state=over.pop("state", _FakeState()),
        generator=over.pop("generator", _FakeGenerator()),
        _time=lambda: over.pop("now", NOW) if "now" in over else NOW,
        log=recorder.log,
        _blocked=recorder._blocked,
        _note_blocked=recorder._note_blocked,
        _recorder=recorder,
        _send_breaker={},
    )
    for key, value in over.items():
        setattr(obj, key, value)
    for name in _NEW_METHODS:
        setattr(obj, name, types.MethodType(getattr(SocialEngine, name), obj))
    return obj


def _run(coro):
    return asyncio.run(coro)


def _ledger_rows(*entries):
    """构造账本条目：entries 是 (uid, state, 相对 NOW 的秒数) 或 (uid, state, 秒数, bid)。"""
    rows = []
    for item in entries:
        uid, state, delta = item[0], item[1], item[2]
        bid = item[3] if len(item) > 3 else "bid1"
        rows.append({"at": NOW - float(delta), "state": state, "bid": bid, "uid": uid})
    return rows


# ─── 一、投递账本 ────────────────────────────────────────────────
class DeliveryLedgerTest(unittest.TestCase):
    def test_record_appends_row_and_marks_dirty(self):
        state = _FakeState()
        eng = _engine(state=state)
        eng._delivery_record("bid1", "u1", "delivered", NOW)
        rows = state.data[engine_mod.DELIVERY_LEDGER_KEY]
        self.assertEqual(1, len(rows))
        self.assertEqual({"at", "state", "bid", "uid"}, set(rows[0]))
        self.assertEqual("delivered", rows[0]["state"])
        self.assertEqual("bid1", rows[0]["bid"])
        self.assertEqual("u1", rows[0]["uid"])
        self.assertEqual(NOW, rows[0]["at"])
        self.assertEqual(1, state.dirty)

    def test_record_keeps_only_last_128(self):
        state = _FakeState()
        eng = _engine(state=state)
        for i in range(200):
            eng._delivery_record("bid1", "u1", "delivered", NOW + i)
        rows = state.data[engine_mod.DELIVERY_LEDGER_KEY]
        self.assertEqual(engine_mod.DELIVERY_LEDGER_MAX, len(rows))
        # 留下的是最近的 128 条（200-128=72 开始）
        self.assertEqual(NOW + 72, rows[0]["at"])
        self.assertEqual(NOW + 199, rows[-1]["at"])

    def test_record_tolerates_state_without_data(self):
        eng = _engine(state=_NoDataState())
        eng._delivery_record("bid1", "u1", "delivered")  # 不抛即通过

    def test_rows_filters_bid_uid_and_window(self):
        state = _FakeState(
            data={
                engine_mod.DELIVERY_LEDGER_KEY: _ledger_rows(
                    ("u1", "delivered", 10),
                    ("u2", "delivered", 10),
                    ("u1", "delivered", 10, "bid2"),
                    ("u1", "delivered", 7200),
                )
            }
        )
        eng = _engine(state=state)
        rows = eng._delivery_rows("bid1", "u1", now=NOW)
        self.assertEqual(1, len(rows))
        self.assertEqual("delivered", rows[0]["state"])

    def test_rows_skips_dirty_entries(self):
        state = _FakeState(
            data={
                engine_mod.DELIVERY_LEDGER_KEY: [
                    "not-a-dict",
                    None,
                    {"at": "bad", "state": "delivered", "bid": "bid1", "uid": "u1"},
                    {"at": float("nan"), "state": "delivered", "bid": "bid1", "uid": "u1"},
                    {"at": NOW - 5, "state": "delivered", "bid": "bid1", "uid": "u1"},
                ]
            }
        )
        eng = _engine(state=state)
        rows = eng._delivery_rows("bid1", "u1", now=NOW)
        self.assertEqual(1, len(rows))
        self.assertEqual(NOW - 5, rows[0]["at"])

    def test_rows_sorted_and_future_tolerated(self):
        state = _FakeState(
            data={
                engine_mod.DELIVERY_LEDGER_KEY: _ledger_rows(
                    ("u1", "delivered", 30),
                    ("u1", "delivered", 10),
                    ("u1", "delivered", -60),  # 未来 60 秒：容忍
                )
            }
        )
        eng = _engine(state=state)
        rows = eng._delivery_rows("bid1", "u1", now=NOW)
        self.assertEqual(3, len(rows))
        self.assertEqual(sorted(row["at"] for row in rows), [row["at"] for row in rows])

    def test_ledger_reads_only_dict_rows(self):
        state = _FakeState(data={engine_mod.DELIVERY_LEDGER_KEY: "坏账本"})
        eng = _engine(state=state)
        self.assertEqual([], eng._delivery_ledger())


# ─── 二、平台熔断 ────────────────────────────────────────────────
class PlatformCircuitTest(unittest.TestCase):
    def test_events_stored_on_instance_and_capped(self):
        eng = _engine()
        for _ in range(40):
            eng._platform_event(False)
        rows = eng._platform_events()
        self.assertEqual(32, len(rows))
        self.assertEqual(False, rows[-1][1])
        # 存引擎实例上，不进 state.json
        self.assertNotIn("platform_send_events", eng.state.data)

    def test_single_refusal_does_not_open(self):
        eng = _engine()
        eng._platform_event(False)
        open_, text = eng._platform_circuit(NOW)
        self.assertFalse(open_)
        self.assertEqual("", text)

    def test_two_refusals_open_circuit(self):
        eng = _engine()
        eng._platform_event(False)
        eng._platform_event(False)
        open_, text = eng._platform_circuit(NOW)
        self.assertTrue(open_)
        self.assertIn("熔断", text)

    def test_success_clears_previous_refusals(self):
        eng = _engine()
        eng._platform_event(False)
        eng._platform_event(False)
        eng._platform_event(True)
        self.assertFalse(eng._platform_circuit(NOW)[0])

    def test_note_platform_failure_only_for_platform_refused(self):
        cases = (
            (REFUSED, 1),
            (TARGET_GONE, 0),
            (UNKNOWN_ERR, 0),
            (OTHER_ERR, 0),
            ("", 0),
        )
        for err, expected in cases:
            eng = _engine()
            eng._note_platform_failure("bid1", err)
            self.assertEqual(expected, len(eng._platform_events()), err)

    def test_circuit_fail_open_when_module_missing(self):
        eng = _engine()
        eng._platform_event(False)
        eng._platform_event(False)
        with mock.patch.object(engine_mod, "_opt_module", return_value=None):
            self.assertFalse(eng._platform_circuit(NOW)[0])

    def test_circuit_fail_open_when_module_raises(self):
        def boom(*args, **kwargs):
            raise RuntimeError("circuit down")

        broken = SimpleNamespace(circuit_state=boom, circuit_allows=boom)
        eng = _engine()
        eng._platform_event(False)
        eng._platform_event(False)
        with mock.patch.object(engine_mod, "_opt_module", return_value=broken):
            self.assertFalse(eng._platform_circuit(NOW)[0])

    def test_send_with_retry_blocks_when_circuit_open(self):
        eng = _engine()
        eng._platform_event(False)
        eng._platform_event(False)
        with mock.patch.object(
            SocialEngine, "_quiet_send_block", staticmethod(lambda self, bid, target: "")
        ), mock.patch.object(
            SocialEngine, "_resolve_send_user", staticmethod(lambda self, target, bid="": None)
        ), mock.patch.object(engine_mod, "_bad_umo", lambda target: ""):
            ok, text, unreachable = _run(
                SocialEngine._send_with_retry(eng, "umo", "正文", "bid1")
            )
        self.assertFalse(ok)
        self.assertFalse(unreachable)
        self.assertIn("熔断", text)
        self.assertIn("platform_circuit", eng._recorder.codes())


# ─── 三、连发收口 ────────────────────────────────────────────────
class RhythmGateTest(unittest.TestCase):
    def _engine_with(self, *entries):
        state = _FakeState(
            data={engine_mod.DELIVERY_LEDGER_KEY: _ledger_rows(*entries)}
        )
        return _engine(state=state)

    def test_missing_module_allows(self):
        eng = self._engine_with(("u1", "delivered", 10))
        with mock.patch.object(engine_mod, "_opt_module", return_value=None):
            self.assertEqual(("", ""), eng._rhythm_gate("bid1", "u1", now=NOW))

    def test_closing_hold_after_three_delivered(self):
        eng = self._engine_with(
            ("u1", "delivered", 300),
            ("u1", "delivered", 200),
            ("u1", "delivered", 100),
        )
        code, text = eng._rhythm_gate("bid1", "u1", now=NOW)
        self.assertEqual("closing_hold", code)
        self.assertIn("收口", text)

    def test_burst_active_when_hold_expired(self):
        # 三条都送达，但最后一条在 1000 秒前：收口 grace（900s）已过，
        # 连发冷却（1800s）还没走完 → 拦的是 burst 这条判据。
        eng = self._engine_with(
            ("u1", "delivered", 1900),
            ("u1", "delivered", 1800),
            ("u1", "delivered", 1000),
        )
        code, text = eng._rhythm_gate("bid1", "u1", now=NOW)
        self.assertEqual("burst_active", code)
        self.assertIn("连发", text)

    def test_burst_cooldown_passed_allows(self):
        eng = self._engine_with(
            ("u1", "delivered", 3000),
            ("u1", "delivered", 2800),
            ("u1", "delivered", 2600),
        )
        self.assertEqual(("", ""), eng._rhythm_gate("bid1", "u1", now=NOW))

    def test_undelivered_breaks_streak(self):
        eng = self._engine_with(
            ("u1", "delivered", 400),
            ("u1", "delivered", 300),
            ("u1", "unknown", 200),
            ("u1", "delivered", 100),
        )
        self.assertEqual(("", ""), eng._rhythm_gate("bid1", "u1", now=NOW))

    def test_other_user_rows_ignored(self):
        eng = self._engine_with(
            ("u2", "delivered", 300),
            ("u2", "delivered", 200),
            ("u2", "delivered", 100),
        )
        self.assertEqual(("", ""), eng._rhythm_gate("bid1", "u1", now=NOW))

    def test_rhythm_exception_fail_open(self):
        def boom(*args, **kwargs):
            raise RuntimeError("rhythm down")

        broken = SimpleNamespace(closing_hold=boom, burst_state=boom, round_allows=boom)
        eng = self._engine_with(("u1", "delivered", 10))
        with mock.patch.object(engine_mod, "_opt_module", return_value=broken):
            self.assertEqual(("", ""), eng._rhythm_gate("bid1", "u1", now=NOW))


# ─── 四、投递预算 ────────────────────────────────────────────────
class BudgetGateTest(unittest.TestCase):
    def _engine_with(self, *entries):
        state = _FakeState(
            data={engine_mod.DELIVERY_LEDGER_KEY: _ledger_rows(*entries)}
        )
        return _engine(state=state)

    def test_budget_full_with_six_pending(self):
        eng = self._engine_with(*[("u1", "unknown", 10 * i) for i in range(6)])
        full, text = eng._budget_gate("bid1", "u1", now=NOW)
        self.assertTrue(full)
        self.assertIn("预算", text)

    def test_not_sent_releases(self):
        entries = [("u1", "unknown", 10 * i) for i in range(5)]
        entries.append(("u1", "not_sent", 60))
        eng = self._engine_with(*entries)
        self.assertFalse(eng._budget_gate("bid1", "u1", now=NOW)[0])

    def test_delivered_outside_window_released(self):
        entries = [("u1", "delivered", 3700 + 10 * i) for i in range(6)]
        eng = self._engine_with(*entries)
        # 账本自己的 1 小时窗口先把它们滤掉了，预算看到的是空账本
        self.assertFalse(eng._budget_gate("bid1", "u1", now=NOW)[0])

    def test_missing_module_allows(self):
        eng = self._engine_with(*[("u1", "unknown", 10 * i) for i in range(6)])
        with mock.patch.object(engine_mod, "_opt_module", return_value=None):
            self.assertFalse(eng._budget_gate("bid1", "u1", now=NOW)[0])

    def test_budget_exception_fail_open(self):
        def boom(*args, **kwargs):
            raise RuntimeError("budget down")

        broken = SimpleNamespace(occupancy=boom, budget_allows=boom)
        eng = self._engine_with(*[("u1", "unknown", 10 * i) for i in range(6)])
        with mock.patch.object(engine_mod, "_opt_module", return_value=broken):
            self.assertFalse(eng._budget_gate("bid1", "u1", now=NOW)[0])


# ─── 五、工具循环 ────────────────────────────────────────────────
class ToolLoopTest(unittest.TestCase):
    def test_normalize_dict_parts(self):
        parts, code = SocialEngine._normalize_tool_output({"parts": ["一", "二"]})
        self.assertEqual(["一", "二"], parts)
        self.assertEqual("", code)

    def test_normalize_text_and_list(self):
        self.assertEqual((["你好"], ""), SocialEngine._normalize_tool_output("你好"))
        self.assertEqual((["一", "二"], ""), SocialEngine._normalize_tool_output(["一", " 二 "]))

    def test_normalize_empty(self):
        self.assertEqual((None, ""), SocialEngine._normalize_tool_output(""))
        self.assertEqual((None, ""), SocialEngine._normalize_tool_output([]))
        self.assertEqual((None, ""), SocialEngine._normalize_tool_output(None))

    def test_normalize_capped_flag(self):
        parts, code = SocialEngine._normalize_tool_output(
            {"parts": ["收口"], "rounds_capped": True}
        )
        self.assertEqual(["收口"], parts)
        self.assertEqual("tool_round_cap", code)

    def test_flag_off_uses_legacy_generate(self):
        gen = _FakeGenerator(parts=["老路"], tools=["新路"])
        eng = _engine(generator=gen, cfg=SimpleNamespace(memory_tools_enabled=False))
        parts = _run(eng._generate_parts(
            umo="umo", target={}, core_context="ctx", reason="r", reason_meta={},
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0, memory_note="",
        ))
        self.assertEqual(["老路"], parts)
        self.assertEqual([], gen.tools_calls)
        self.assertEqual(1, len(gen.generate_calls))

    def test_flag_on_uses_tool_loop(self):
        gen = _FakeGenerator(parts=["老路"], tools=["新路"])
        eng = _engine(generator=gen, cfg=SimpleNamespace(memory_tools_enabled=True))
        parts = _run(eng._generate_parts(
            umo="umo", target={}, core_context="ctx", reason="r", reason_meta={},
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0, memory_note="",
        ))
        self.assertEqual(["新路"], parts)
        self.assertEqual(1, len(gen.tools_calls))
        self.assertEqual([], gen.generate_calls)

    def test_tool_loop_empty_falls_back(self):
        gen = _FakeGenerator(parts=["老路"], tools=[])
        eng = _engine(generator=gen, cfg=SimpleNamespace(memory_tools_enabled=True))
        parts = _run(eng._generate_parts(
            umo="umo", target={}, core_context="ctx", reason="r", reason_meta={},
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0, memory_note="",
        ))
        self.assertEqual(["老路"], parts)
        self.assertEqual(1, len(gen.generate_calls))

    def test_tool_loop_error_falls_back(self):
        gen = _FakeGenerator(parts=["老路"], tools_error=RuntimeError("tool down"))
        eng = _engine(generator=gen, cfg=SimpleNamespace(memory_tools_enabled=True))
        parts = _run(eng._generate_parts(
            umo="umo", target={}, core_context="ctx", reason="r", reason_meta={},
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0, memory_note="",
        ))
        self.assertEqual(["老路"], parts)
        self.assertEqual(1, len(gen.generate_calls))

    def test_tool_loop_capped_notes_reason(self):
        gen = _FakeGenerator(tools={"parts": ["收口"], "rounds_capped": True})
        eng = _engine(generator=gen, cfg=SimpleNamespace(memory_tools_enabled=True))
        parts = _run(eng._generate_parts(
            umo="umo", target={}, core_context="ctx", reason="r", reason_meta={},
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0, memory_note="",
        ))
        self.assertEqual(["收口"], parts)
        self.assertIn("tool_round_cap", eng._recorder.codes())

    def test_tool_loop_awaitable_result(self):
        class _AsyncTools(_FakeGenerator):
            async def generate_with_tools(self, *args, **kwargs):
                self.tools_calls.append((args, kwargs))
                return {"text": "异步结果"}

        gen = _AsyncTools()
        eng = _engine(generator=gen, cfg=SimpleNamespace(memory_tools_enabled=True))
        parts = _run(eng._generate_parts(
            umo="umo", target={}, core_context="ctx", reason="r", reason_meta={},
            persona_prompt="p", mind=None, at=NOW, clock_offset=0.0, memory_note="",
        ))
        self.assertEqual(["异步结果"], parts)


# ─── 六、生成后自评 ──────────────────────────────────────────────
class DraftReviewTest(unittest.TestCase):
    def _review(self, gen, parts, cfg=None, decision=None, cards=None):
        eng = _engine(generator=gen, cfg=cfg or SimpleNamespace(draft_review_enabled=True))
        return _run(eng._review_draft(parts, decision, cards))

    def test_flag_off_returns_parts(self):
        gen = _FakeGenerator(review={"verdict": "drop"})
        out = self._review(gen, ["正文"], cfg=SimpleNamespace(draft_review_enabled=False))
        self.assertEqual(["正文"], out)
        self.assertEqual([], gen.review_calls)

    def test_drop_returns_none(self):
        gen = _FakeGenerator(review={"verdict": "drop"})
        self.assertIsNone(self._review(gen, ["正文"]))

    def test_rewrite_replaces_text(self):
        gen = _FakeGenerator(review={"verdict": "rewrite", "text": "改过的"})
        self.assertEqual(["改过的"], self._review(gen, ["正文"]))

    def test_ok_returns_parts(self):
        gen = _FakeGenerator(review={"verdict": "ok"})
        self.assertEqual(["正文"], self._review(gen, ["正文"]))

    def test_error_returns_parts(self):
        gen = _FakeGenerator(review_error=RuntimeError("review down"))
        self.assertEqual(["正文"], self._review(gen, ["正文"]))

    def test_awaitable_result_awaited(self):
        gen = _AsyncReviewGenerator(review={"verdict": "drop"})
        self.assertIsNone(self._review(gen, ["正文"]))

    def test_non_dict_result_returns_parts(self):
        gen = _FakeGenerator(review="随便什么")
        self.assertEqual(["正文"], self._review(gen, ["正文"]))

    def test_rewrite_empty_text_returns_parts(self):
        gen = _FakeGenerator(review={"verdict": "rewrite", "text": "   "})
        self.assertEqual(["正文"], self._review(gen, ["正文"]))


# ─── 七、发送记账 ────────────────────────────────────────────────
class SendAccountingTest(unittest.TestCase):
    def test_note_send_ok_records_success_event(self):
        eng = _engine()
        eng._note_send_ok("bid1")
        rows = eng._platform_events()
        self.assertEqual(1, len(rows))
        self.assertEqual(True, rows[-1][1])

    def test_note_send_failure_records_platform_failure_before_early_return(self):
        eng = _engine()
        # peer_unreachable=True 会在后面直接 return；平台拒发必须在它之前记上，
        # 否则「平台拒发」同时带 peer_unreachable 时永远进不了熔断。
        eng._note_send_failure("bid1", REFUSED, True)
        self.assertEqual(1, len(eng._platform_events()))

    def test_note_send_failure_ignores_target_gone(self):
        eng = _engine()
        eng._note_send_failure("bid1", TARGET_GONE, True)
        self.assertEqual([], eng._platform_events())


if __name__ == "__main__":
    unittest.main()
