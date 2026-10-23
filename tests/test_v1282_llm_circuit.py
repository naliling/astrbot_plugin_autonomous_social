"""v1.28.2：LLM 熔断要**跨轮**生效，否则上游限流时每个心跳仍然白打一次请求。

## 线上实测（2026-10-23 12:12，v1.28.1）

`~/.astrbot/logs/backend.log`：

    429 Quota exceeded ... limit: 20 ... Please retry in 2h48m28s.
    [Gemini] Request failed with retryable error; retrying (2/5)
    ...
    No valid API keys remaining. Current key: sk-iZHTd7PvX...
    Switched from gemini/gemini-3.5-flash to fallback chat provider: ...

一天之内：

    rounds                    = 998
    sent_total                = 582
    Gemini 重试事件            = 404
    candidates is empty       = 45
    blocked_reasons: 发送熔断 66947 | 她在睡 77149 | 安静时段 25172

也就是说近千轮、四百多次重试的 Gemini 请求里，**绝大多数最终被闸门挡下**
（她在睡 / 发送熔断 / 安静时段）——配额烧在根本发不出去的候选上。

## 根因

`try_once` 里本来就有一个一轮内的熔断标志：

    infra_down: Dict[str, bool] = {"on": False}      # engine.py

它确实有效：同一轮里第一个候选撞见 provider 坏了，剩下的候选不再试
（`engine.py` 的 `speak()` 里两处 `if infra_down["on"]: return False`，
轮末还有一道 `backoff_left > 0 → 全部跳过`）。

但它是**每轮新建、每轮清零的局部变量**。上游按分钟级配额限流、心跳 8~15
分钟一轮，于是每个心跳都会：重新收集全部候选 → 逐个去撞一次墙 → 记一条
「本轮候选 594 个，尝试 0 次」。`infra_down` 只挡住了一轮之内的重复，
挡不住「下一轮照试」。

## 修法

生成器把「这一路整体是坏的」从局部状态里暴露出来：

* `_note_llm_failure` 首次失败时立 `_llm_down_since[scope]`（不刷新，
  否则「坏了多久」永远等于最近一次失败的时间）；
* `_note_llm_ok` 撤熔断——上游一好，下一个心跳就能重新说话；
* `llm_circuit_state()` 返回各 scope 的档位/坏了多久/连败次数；
* `llm_circuit_note_round()` 记空转轮数，供日志降频。

引擎在 `try_once` 开头读它，直接结束整轮：不收集候选、不进 settle。

## 顺带（B）

每轮 LLM 尝试次数 = `max_sends_per_round × ATTEMPT_PER_SEND(2)`。
默认值 6 → 2（一轮 12 次尝试 → 4 次），心跳上限 15 → 30 分钟。
发送成功率不变：998 轮只发出去 582 条，瓶颈从来不在尝试次数上。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = (
    _HERE.parent
    if (_HERE.parent / "_conf_schema.json").is_file()
    else _HERE.parent / "astrbot_plugin_autonomous_social"
)


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v1282_circuit_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.generator import MessageGenerator  # noqa: E402


class _Clock:
    """可推进的假时钟，与生成器共用一个时间源。"""

    def __init__(self, t: float = 1000.0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += float(secs)


def _gen(clock):
    return MessageGenerator(context=None, config=None, time_source=clock)


def _429(reason: str = "429 None. {'error': {'code': 429}}", *, fatal: bool = True):
    return reason, fatal


class CircuitStateTest(unittest.TestCase):
    """熔断状态本身：什么时候立、什么时候撤、坏了多久怎么算。"""

    def test_no_circuit_when_healthy(self) -> None:
        clock = _Clock()
        g = _gen(clock)
        self.assertEqual(g.llm_circuit_state(), {})

    def test_rate_limit_opens_circuit_immediately(self) -> None:
        """429 属于「换条路也没用」，一次失败就该立熔断。"""
        clock = _Clock()
        g = _gen(clock)
        reason, fatal = _429()
        g._note_llm_failure("gemini", reason, fatal=fatal)
        st = g.llm_circuit_state()
        self.assertIn("gemini", st, "429 之后没有立熔断，下一轮还会去撞墙")
        self.assertEqual(st["gemini"]["kind"], "rate")
        self.assertEqual(st["gemini"]["streak"], 1)

    def test_transient_needs_streak_before_circuit(self) -> None:
        """超时/5xx 按 transient 处理，要连败满阈值才熔断——一次抖动不该停摆。"""
        clock = _Clock()
        g = _gen(clock)
        g._note_llm_failure("gemini", "timeout", fatal=False)
        self.assertEqual(g.llm_circuit_state(), {}, "一次超时就熔断，上游抖一下插件就停摆")
        g._note_llm_failure("gemini", "timeout", fatal=False)
        g._note_llm_failure("gemini", "timeout", fatal=False)
        st = g.llm_circuit_state()
        self.assertIn("gemini", st)
        self.assertEqual(st["gemini"]["kind"], "transient")

    def test_since_is_not_refreshed_by_later_failures(self) -> None:
        """`since` 必须是首次失败的时刻，否则「坏了多久」没有意义。"""
        clock = _Clock()
        g = _gen(clock)
        g._note_llm_failure("gemini", "429 rate limit", fatal=True)
        first = g.llm_circuit_state()["gemini"]["since"]
        clock.advance(600.0)
        g._note_llm_failure("gemini", "429 rate limit", fatal=True)
        second = g.llm_circuit_state()["gemini"]["since"]
        self.assertEqual(first, second, "since 被后来的失败刷新了，「已连续 N 分钟」会永远归零")
        self.assertGreaterEqual(g.llm_circuit_state()["gemini"]["down_for"], 600.0)

    def test_success_clears_circuit(self) -> None:
        """上游一好必须立刻能恢复，否则一次 429 就把插件永久停摆。"""
        clock = _Clock()
        g = _gen(clock)
        g._note_llm_failure("gemini", "429 rate limit", fatal=True)
        self.assertIn("gemini", g.llm_circuit_state())
        g._note_llm_ok("gemini")
        self.assertEqual(g.llm_circuit_state(), {}, "成功后没有撤熔断，插件会一直停摆")

    def test_circuit_survives_backoff_expiry(self) -> None:
        """退避窗口过了但还没成功过一次：仍然算坏着。

        否则退避一过就又整轮重试，熔断只挡了一轮——正是本次要修的那个 bug。
        """
        clock = _Clock()
        g = _gen(clock)
        g._note_llm_failure("gemini", "429 rate limit", fatal=True)
        clock.advance(3600.0)  # 退避封顶 30 分钟，早过了
        self.assertEqual(g.llm_backoff_remaining("gemini"), 0.0)
        st = g.llm_circuit_state()
        self.assertIn("gemini", st, "退避过了就把熔断撤了，下一轮照样白打一次请求")

    def test_circuit_is_per_scope(self) -> None:
        """A 的 key 坏掉不能把 B 一起停掉。"""
        clock = _Clock()
        g = _gen(clock)
        g._note_llm_failure("gemini", "429 rate limit", fatal=True)
        g._note_llm_ok("other")
        self.assertEqual(set(g.llm_circuit_state()), {"gemini"})


class CircuitRoundCounterTest(unittest.TestCase):
    """空转轮计数：给日志降频用。"""

    def test_counts_consecutive_rounds(self) -> None:
        clock = _Clock()
        g = _gen(clock)
        g._note_llm_failure("gemini", "429 rate limit", fatal=True)
        self.assertEqual(g.llm_circuit_note_round("gemini"), 1)
        self.assertEqual(g.llm_circuit_note_round("gemini"), 2)
        self.assertEqual(g.llm_circuit_note_round("gemini"), 3)

    def test_counter_resets_on_success(self) -> None:
        clock = _Clock()
        g = _gen(clock)
        g._note_llm_failure("gemini", "429 rate limit", fatal=True)
        g.llm_circuit_note_round("gemini")
        g.llm_circuit_note_round("gemini")
        g._note_llm_ok("gemini")
        g._note_llm_failure("gemini", "429 rate limit", fatal=True)
        self.assertEqual(g.llm_circuit_note_round("gemini"), 1, "恢复后再坏，轮数没有从 1 重新数")


class EngineRoundSkipTest(unittest.TestCase):
    """引擎侧：熔断时 `try_once` 必须整轮收工，不收集任何候选。"""

    def _engine(self, clock):
        import tempfile
        from pathlib import Path

        from social.config import SocialConfig
        from social.engine import SocialEngine
        from social.state import SocialState

        state = SocialState(str(Path(tempfile.mkdtemp()) / "state.json"))
        cfg = SocialConfig.from_astrbot({})
        eng = SocialEngine(
            context=None, config=cfg, state_path=state.path, time_source=clock
        )
        return eng

    def test_try_once_returns_early_while_circuit_open(self) -> None:
        import asyncio

        clock = _Clock()
        eng = self._engine(clock)
        # 造一个「有用户」的 bot，否则 try_once 在更早的 bots 判定就返回了
        u = eng.state.user("b1", "u1")
        u["message_count"] = 3
        eng.state.mark_dirty()
        eng.state.save()

        eng.generator._note_llm_failure("gemini", "429 rate limit", fatal=True)

        import unittest.mock as mock

        with mock.patch.object(eng, "_refresh_clocks"), \
             mock.patch.object(eng, "settle_minds") as settle:
            asyncio.run(eng.try_once())
            settle.assert_not_called()
        self.assertIn("gemini", eng._m.get("llm_circuit_scopes", []))

    def test_try_once_runs_when_healthy(self) -> None:
        """没熔断时不能被这道闸挡住——否则插件彻底不发消息了。"""
        import asyncio

        clock = _Clock()
        eng = self._engine(clock)
        u = eng.state.user("b1", "u1")
        u["message_count"] = 3
        eng.state.mark_dirty()
        eng.state.save()

        import unittest.mock as mock

        with mock.patch.object(eng, "_refresh_clocks"), \
             mock.patch.object(eng, "settle_minds") as settle, \
             mock.patch.object(eng, "_seed_tick"):
            asyncio.run(eng.try_once())
            settle.assert_called_once()
        self.assertEqual(eng._m.get("llm_circuit_scopes"), [])


class ThrottleDefaultsTest(unittest.TestCase):
    """B：默认值必须真的降下来了（否则老用户升级后请求量一点没变）。"""

    def test_schema_defaults(self) -> None:
        import json

        schema = json.loads(
            (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["heartbeat_max_minutes"]["default"], 30)
        self.assertEqual(schema["max_sends_per_round"]["default"], 2)

    def test_config_fallback_defaults_match_schema(self) -> None:
        from social.config import (
            HEARTBEAT_MAX_DEFAULT,
            MAX_SENDS_PER_ROUND_DEFAULT,
        )

        self.assertEqual(HEARTBEAT_MAX_DEFAULT, 30)
        self.assertEqual(MAX_SENDS_PER_ROUND_DEFAULT, 2)

    def test_attempts_per_round_is_bounded(self) -> None:
        """一轮 LLM 尝试次数 = max_sends × ATTEMPT_PER_SEND，必须 ≤ 4。"""
        from social.config import SocialConfig
        from social.engine import ATTEMPT_PER_SEND

        cfg = SocialConfig.from_astrbot({})
        self.assertLessEqual(
            cfg.max_sends_per_round * ATTEMPT_PER_SEND,
            4,
            "一轮尝试次数又涨回 6 次以上了，限流下这些都是白烧的请求",
        )


if __name__ == "__main__":
    unittest.main()
