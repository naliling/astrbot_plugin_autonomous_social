# -*- coding: utf-8 -*-
"""v2.1.4 engine 接线测试：证据卡、四道发前闸、先规划后写作（task-14）。

只测 engine 这一层：generator / core / 状态全用假对象，所以容器里（没有 astrbot、
没有 Core 包）也能跑。为什么值得单独建文件而不是塞进 test_autonomous_social.py：
这组用例验的是「接线是否真的接上了」——包括方法顺序、CRLF、退回老路的路径——
和老测试验的业务规则不是一类东西，混在一起以后没人敢删。
"""

import asyncio
import sys
import time
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
    api.logger = logging.getLogger("social_v214_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import anchors as anchors_mod  # noqa: E402
from social import engine as engine_mod  # noqa: E402
from social import guards  # noqa: E402
from social.engine import SocialEngine  # noqa: E402

NOW = 1_700_000_000.0


# ─── 假对象 ──────────────────────────────────────────────────────
class _Recorder:
    """替身引擎的记账：_blocked 返回 (False, text)，与真实实现同签名。"""

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
    def __init__(self, user=None, proactive=None):
        self.user_row = user
        self.proactive = list(proactive or [])
        self.user_calls = 0

    def user(self, bid, uid):
        self.user_calls += 1
        return self.user_row

    def recent_proactive(self, bid, uid, now=None, days=7):
        return list(self.proactive)


class _FakeCore:
    def __init__(self, snapshot=None, boom=False):
        self.snapshot = snapshot
        self.boom = boom
        self.calls = 0

    def read_root(self):
        # engine._read_core_root(self) 内部走的是 self.core.read_root()（engine.py:3491），
        # 所以假引擎要在这里给根，而不是在假引擎上挂一个同名方法——类内是显式调用。
        if self.boom:
            raise RuntimeError("core down")
        return {"root": "/tmp/core"}

    def load_snapshot(self, bid, uid, root=None):
        self.calls += 1
        if self.boom:
            raise RuntimeError("core down")
        return self.snapshot


class _FakeFacade:
    def __init__(self, opportunities=None, materials=None, boom=False):
        self._opps = opportunities
        self._mats = materials
        self._boom = boom

    def opportunities(self, role_id):
        if self._boom:
            raise RuntimeError("facade down")
        return list(self._opps or [])

    def materials(self, role_id):
        if self._boom:
            raise RuntimeError("facade down")
        return list(self._mats or [])


class _FakeGenerator:
    def __init__(self, plan_result=None, plan_error=None, decide_result=None, decide_error=None):
        self.plan_calls = []
        self.decide_calls = []
        self.plan_result = plan_result
        self.plan_error = plan_error
        self.decide_result = decide_result
        self.decide_error = decide_error

    async def decide_with_plan(self, *args, **kwargs):
        self.plan_calls.append((args, kwargs))
        if self.plan_error:
            raise self.plan_error
        return self.plan_result

    async def decide(self, *args, **kwargs):
        self.decide_calls.append((args, kwargs))
        if self.decide_error:
            raise self.decide_error
        return self.decide_result


class _LegacyGenerator:
    """2.1.3 的 generator：没有 decide_with_plan，engine 必须原样退回 decide()。"""

    def __init__(self, result):
        self.decide_calls = []
        self.result = result

    async def decide(self, *args, **kwargs):
        self.decide_calls.append((args, kwargs))
        return self.result


def _engine(**over):
    """拼一个只带「接线用得到」的那些属性的假引擎。"""
    recorder = _Recorder()
    cfg = over.pop("cfg", SimpleNamespace(planning_enabled=True))
    obj = SimpleNamespace(
        cfg=cfg,
        state=over.pop("state", _FakeState()),
        core=over.pop("core", _FakeCore()),
        generator=over.pop("generator", _FakeGenerator()),
        _facade=over.pop("facade", None),
        _time=lambda: NOW,
        _clock_offset_for=lambda bid: 0.0,
        log=recorder.log,
        _blocked=recorder._blocked,
        _note_blocked=recorder._note_blocked,
        _recorder=recorder,
    )
    for key, value in over.items():
        setattr(obj, key, value)
    obj._core_facade = lambda: obj._facade
    # 只绑入口方法：其余方法内部都是 SocialEngine.xxx(self, ...) 显式调用，
    # 与 engine.py 里「老测试可能缺方法」的既有注释同一种考虑。
    for name in ("evidence_cards", "_evidence_cards", "_pre_send_gate", "_decide_proactive"):
        setattr(obj, name, types.MethodType(getattr(SocialEngine, name), obj))
    return obj


def _card(**over):
    base = {
        "id": "c1",
        "kind": "life",
        "purpose": "life",
        "title": "楼下新开的面包店",
        "detail": "她昨天说想去看看",
        "source": "生活素材",
        "captured_at": NOW - 600.0,
        "expires_at": NOW + 600.0,
        "anchor_key": "k1",
    }
    base.update(over)
    return base


def _row(role, text, ts):
    return {"role": role, "text": text, "ts": ts}


def _run(coro):
    return asyncio.run(coro)


CARD_KEYS = {
    "id",
    "kind",
    "purpose",
    "title",
    "detail",
    "source",
    "captured_at",
    "expires_at",
    "anchor_key",
    "generated",
}


# ─── 一、证据卡 ──────────────────────────────────────────────────
class EvidenceCardsTest(unittest.TestCase):
    def test_normalizes_to_ten_keys(self):
        eng = _engine(facade=_FakeFacade(opportunities=[{"id": "o1", "title": "面试", "detail": "她明天下午面试"}]))
        cards = eng.evidence_cards("bid1", at=NOW)
        self.assertEqual(1, len(cards))
        self.assertEqual(CARD_KEYS, set(cards[0]))
        self.assertEqual("面试", cards[0]["title"])
        # core 没给 captured_at/expires_at 时按「此刻 + 6 小时」补齐。
        self.assertEqual(NOW, cards[0]["captured_at"])
        self.assertEqual(NOW + 6 * 3600.0, cards[0]["expires_at"])

    def test_expired_card_is_dropped(self):
        eng = _engine(
            facade=_FakeFacade(opportunities=[{"id": "old", "title": "旧事", "detail": "过期了", "expires_at": NOW - 1}]),
            state=_FakeState(user=None),
        )
        self.assertEqual([], eng.evidence_cards("bid1", at=NOW))

    def test_materials_used_when_opportunities_empty(self):
        eng = _engine(facade=_FakeFacade(opportunities=[], materials=[{"id": "m1", "title": "雨伞", "detail": "她忘带伞"}]))
        cards = eng.evidence_cards("bid1", at=NOW)
        self.assertEqual(1, len(cards))
        self.assertEqual("雨伞", cards[0]["title"])

    def test_contract_blocks_fallback(self):
        blocks = {"opportunities": [], "materials": [{"id": "ct", "title": "体检", "detail": "她周五体检"}]}
        eng = _engine(facade=None, core=_FakeCore(snapshot={"contract": {"x": 1}}), state=_FakeState(user=None))
        with mock.patch.object(anchors_mod, "contract_blocks", lambda core, contract, now=None: blocks, create=True):
            cards = eng.evidence_cards("bid1", at=NOW)
        self.assertEqual(["体检"], [c["title"] for c in cards])

    def test_local_anchor_fallback_marks_source(self):
        user = {
            "anchors": [
                {"kind": "derived", "about": "她今天加班到很晚", "due": NOW - 60.0, "used_at": 0.0, "expire_at": NOW + 3600.0}
            ]
        }
        eng = _engine(facade=None, core=_FakeCore(snapshot=None), state=_FakeState(user=user))
        cards = eng.evidence_cards("bid1", at=NOW)
        self.assertEqual(1, len(cards))
        self.assertEqual("由头", cards[0]["source"])
        # captured_at 取由头自己的 due（不是「此刻」），过期时间随之 = due + 6h。
        self.assertEqual(NOW - 60.0 + 6 * 3600.0, cards[0]["expires_at"])

    def test_no_sources_returns_empty(self):
        eng = _engine(facade=None, core=_FakeCore(snapshot=None), state=_FakeState(user=None))
        self.assertEqual([], eng.evidence_cards("bid1", at=NOW))

    def test_limit_truncates(self):
        opps = [{"id": "o%d" % i, "title": "事%d" % i, "detail": "细节%d" % i} for i in range(4)]
        eng = _engine(facade=_FakeFacade(opportunities=opps))
        self.assertEqual(2, len(eng.evidence_cards("bid1", at=NOW, limit=2)))

    def test_card_identity_variants(self):
        self.assertEqual(("bid1", ""), SocialEngine._card_identity("bid1"))
        self.assertEqual(("bid1", "u1"), SocialEngine._card_identity({"bid": "bid1", "uid": "u1"}))
        self.assertEqual(("bid1", "u2"), SocialEngine._card_identity({"bot_id": "bid1", "user_id": "u2"}))
        self.assertEqual(("", ""), SocialEngine._card_identity(object()))

    def test_never_raises_when_everything_breaks(self):
        eng = _engine(facade=_FakeFacade(boom=True), core=_FakeCore(boom=True), state=_FakeState(user=None))
        self.assertEqual([], eng.evidence_cards("bid1", at=NOW))

    def test_evidence_cards_cached_per_turn(self):
        calls = []

        def fake_collect(self, bid, uid, *, at, limit=5):
            calls.append((bid, uid, at))
            return [_card()]

        eng = _engine()
        with mock.patch.object(SocialEngine, "_collect_cards", fake_collect):
            first = _run(eng._evidence_cards("bid1", uid="u1", at=NOW))
            second = _run(eng._evidence_cards("bid1", uid="u1", at=NOW))
            third = _run(eng._evidence_cards("bid1", uid="u1", at=NOW + 1))
        self.assertEqual(1, len(first))
        self.assertEqual(1, len(second))
        self.assertEqual(2, len(calls), "同一轮只该取一次卡；换时间才重取")
        self.assertEqual(1, len(third))

    def test_async_returns_empty_on_exception(self):
        eng = _engine()
        with mock.patch.object(SocialEngine, "_collect_cards", side_effect=RuntimeError("boom")):
            self.assertEqual([], _run(eng._evidence_cards("bid1", uid="u1", at=NOW)))


# ─── 二、对话账本转换 ────────────────────────────────────────────
class GuardDialogueTest(unittest.TestCase):
    def test_maps_dir_to_role(self):
        rows = engine_mod._guard_dialogue(
            [{"dir": "in", "text": "在吗", "ts": NOW - 120}, {"dir": "out", "text": "在", "ts": NOW - 60}]
        )
        self.assertEqual([("user", NOW - 120.0), ("assistant", NOW - 60.0)], [(r["role"], r["ts"]) for r in rows])

    def test_skips_unknown_dir_and_junk(self):
        rows = engine_mod._guard_dialogue(
            [{"dir": "sys", "text": "x", "ts": NOW}, "junk", {"dir": "out", "text": "嗯", "ts": NOW}]
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("assistant", rows[0]["role"])

    def test_none_is_empty(self):
        self.assertEqual([], engine_mod._guard_dialogue(None))


# ─── 三、四道发前闸 ──────────────────────────────────────────────
class PreSendGateTest(unittest.TestCase):
    def setUp(self):
        self.eng = _engine()

    def test_refusal_detected(self):
        code, detail = self.eng._pre_send_gate("很抱歉，我无法继续这个话题", at=NOW)
        self.assertEqual("refusal_detected", code)
        self.assertIn("我无法", detail)

    def test_self_continuation(self):
        code, _ = self.eng._pre_send_gate(
            "那我先睡了", dialogue=[_row("assistant", "嗯。", NOW - 60.0)], at=NOW
        )
        self.assertEqual("self_continuation", code)

    def test_no_self_continuation_when_user_spoke_last(self):
        code, _ = self.eng._pre_send_gate(
            "那我先睡了", dialogue=[_row("user", "你睡吧", NOW - 60.0)], at=NOW
        )
        self.assertEqual("", code)

    def test_duplicate_inflight(self):
        text = "今天降温了，记得加衣服"
        inflight = [{"state": "accepted", "text": text, "ts": time.time() - 60.0}]
        code, _ = self.eng._pre_send_gate(text, inflight=inflight, at=NOW)
        self.assertEqual("duplicate_inflight", code)

    def test_no_duplicate_for_different_text(self):
        inflight = [{"state": "accepted", "text": "今天降温了，记得加衣服", "ts": time.time() - 60.0}]
        code, _ = self.eng._pre_send_gate("刚才看到一只很胖的猫", inflight=inflight, at=NOW)
        self.assertEqual("", code)

    def test_evidence_missing(self):
        plan = SimpleNamespace(facts=["她今天去拔牙了"])
        cards = [_card(title="面包店", detail="楼下新开的面包店")]
        code, detail = self.eng._pre_send_gate(
            "她今天去拔牙了，说有点怕", plan=plan, cards=cards, at=NOW
        )
        self.assertEqual("evidence_missing", code)
        self.assertIn("1/1", detail)

    def test_evidence_fact_not_written_is_not_blocked(self):
        # v2.2.3：提纲里列了卡外的事，正文压根没提 → 不算「这条引用了卡外的事」。
        plan = SimpleNamespace(facts=["她今天去拔牙了"])
        cards = [_card(title="面包店", detail="楼下新开的面包店")]
        self.assertEqual(
            ("", ""),
            self.eng._pre_send_gate("楼下新开了一家面包店", plan=plan, cards=cards, at=NOW),
        )

    def test_evidence_present_passes(self):
        plan = SimpleNamespace(facts=["她今天去拔牙了"])
        cards = [_card(title="拔牙", detail="她今天去拔牙了，说有点怕")]
        self.assertEqual(("", ""), self.eng._pre_send_gate("拔牙疼不疼", plan=plan, cards=cards, at=NOW))

    def test_empty_facts_passes(self):
        plan = SimpleNamespace(facts=[])
        self.assertEqual(("", ""), self.eng._pre_send_gate("随口一句", plan=plan, cards=[], at=NOW))

    def test_normal_text_passes(self):
        self.assertEqual(("", ""), self.eng._pre_send_gate("今天下雨了，记得带伞", at=NOW))

    def test_fail_open_when_guard_raises(self):
        with mock.patch.object(guards, "looks_like_refusal", side_effect=RuntimeError("boom")):
            self.assertEqual(("", ""), self.eng._pre_send_gate("今天下雨了", at=NOW))

    def test_fail_open_when_dangling_raises(self):
        with mock.patch.object(guards, "dangling_after_own_reply", side_effect=RuntimeError("boom")):
            self.assertEqual(("", ""), self.eng._pre_send_gate("今天下雨了", at=NOW))


# ─── 四、先规划后写作 ────────────────────────────────────────────
class DecideProactiveTest(unittest.TestCase):
    def _call(self, eng):
        return _run(
            eng._decide_proactive(
                umo="umo1",
                u_copy={"conversation": []},
                core_context="ctx",
                reason="r",
                reason_meta={},
                persona_prompt="p",
                mind=None,
                now=NOW,
                bid="bid1",
                uid="u1",
                memory_note="",
            )
        )

    def test_uses_decide_with_plan(self):
        planned = SimpleNamespace(send=True, parts=["好"], why_not="", raw_text="x", plan=None)
        gen = _FakeGenerator(plan_result=planned)
        eng = _engine(generator=gen, facade=_FakeFacade(opportunities=[_card()]))
        decision, cards = self._call(eng)
        self.assertIs(planned, decision)
        self.assertEqual(1, len(gen.plan_calls))
        self.assertEqual([], gen.decide_calls)
        self.assertEqual(1, len(cards))
        self.assertEqual(cards, gen.plan_calls[0][1]["cards"], "规划与证据闸必须看同一份卡")

    def test_falls_back_when_plan_returns_none(self):
        legacy = SimpleNamespace(send=True, parts=["嗯"], why_not="", raw_text="y", plan=None)
        gen = _FakeGenerator(plan_result=None, decide_result=legacy)
        eng = _engine(generator=gen)
        decision, _cards = self._call(eng)
        self.assertIs(legacy, decision)
        self.assertEqual(1, len(gen.decide_calls))
        self.assertIn("plan_unavailable", eng._recorder.codes())

    def test_planning_disabled_skips_plan(self):
        legacy = SimpleNamespace(send=True, parts=["嗯"], why_not="", raw_text="y", plan=None)
        gen = _FakeGenerator(plan_result=SimpleNamespace(send=True), decide_result=legacy)
        eng = _engine(generator=gen, cfg=SimpleNamespace(planning_enabled=False))
        self._call(eng)
        self.assertEqual([], gen.plan_calls)
        self.assertEqual(1, len(gen.decide_calls))
        self.assertNotIn("plan_unavailable", eng._recorder.codes())

    def test_legacy_generator_without_plan_method(self):
        legacy = SimpleNamespace(send=True, parts=["嗯"], why_not="", raw_text="y", plan=None)
        gen = _LegacyGenerator(legacy)
        eng = _engine(generator=gen)
        self._call(eng)
        self.assertEqual(1, len(gen.decide_calls))
        self.assertNotIn("plan_unavailable", eng._recorder.codes())

    def test_plan_raises_falls_back(self):
        legacy = SimpleNamespace(send=True, parts=["嗯"], why_not="", raw_text="y", plan=None)
        gen = _FakeGenerator(plan_error=RuntimeError("plan boom"), decide_result=legacy)
        eng = _engine(generator=gen)
        decision, _cards = self._call(eng)
        self.assertIs(legacy, decision)
        self.assertIn("plan_unavailable", eng._recorder.codes())

    def test_cards_empty_is_noted(self):
        gen = _FakeGenerator(plan_result=SimpleNamespace(send=True, plan=None, raw_text=""))
        eng = _engine(generator=gen, facade=None, core=_FakeCore(snapshot=None), state=_FakeState(user=None))
        self._call(eng)
        self.assertIn("cards_empty", eng._recorder.codes())

    def test_decide_raises_returns_none(self):
        gen = _FakeGenerator(plan_result=None, decide_error=RuntimeError("decide boom"))
        eng = _engine(generator=gen)
        decision, _cards = self._call(eng)
        self.assertIsNone(decision)


# ─── 五、接线本身（顺序 / 文件格式） ────────────────────────────
class WiringSourceTest(unittest.TestCase):
    def setUp(self):
        self.source = (PLUGIN_ROOT / "social" / "engine.py").read_text(encoding="utf-8")

    def test_imports_guards(self):
        self.assertIn("core_facade, desire, guards", self.source)

    def test_decide_goes_through_planner(self):
        self.assertIn("SocialEngine._decide_proactive(", self.source)

    def test_gates_run_before_hourly_reserve(self):
        gate_at = self.source.index("SocialEngine._pre_send_gate(")
        # v2.1.7：占配额改成「先取返回值、再判空」（名单用户还要同时占按人窗口），
        # 这里锚定调用本身而不是某种 if 写法，钉的仍然是「闸在占配额之前」。
        reserve_at = self.source.index("self._hourly_reserve(bid")
        self.assertLess(gate_at, reserve_at, "四道闸必须在占配额、发送之前")

    def test_gates_run_after_segment_verification(self):
        verify_at = self.source.index("_verify_all_segments(")
        gate_at = self.source.index("SocialEngine._pre_send_gate(")
        self.assertLess(verify_at, gate_at, "闸要判最终发出的那版正文")

    def test_engine_stays_crlf(self):
        data = (PLUGIN_ROOT / "social" / "engine.py").read_bytes()
        crlf = data.count(b"\r\n")
        lone = data.count(b"\n") - crlf
        self.assertGreater(crlf, 10000)
        self.assertEqual(0, lone, "engine.py 必须保持全 CRLF（仓库里唯一一个 CRLF 文件）")


if __name__ == "__main__":
    unittest.main()
