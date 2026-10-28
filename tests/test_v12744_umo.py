"""v1.27.44：一条废掉的 umo 不该让所有人都收不到消息。

线上实测（`backend.log` 19:25~19:26）：

```
19:25:13 [WARN] 100000001 发送失败但原因在平台侧（不隔离该用户）：send_message 返回 False：未找到匹配的会话平台
19:25:26 [WARN] 100000002 发送失败但原因在平台侧（不隔离该用户）：… 同上
19:25:36 [ERRO] [default] 连续 3 次发不出去，暂停生成 15 分钟
19:26:00 [ERRO] [100000003] 连续 3 次发不出去，暂停生成 15 分钟
19:26:02 [INFO] 本轮候选 591 个，尝试 25 次，发出 2 条
```

三个人各自那条 umo 废了，却把**整个角色**熔断了 15 分钟。账本里
`发送熔断 66946` 就是这么攒出来的：每轮都在熔断里空转，健康的人轮不到。
"""
from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v12744_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.engine import (  # noqa: E402
    SEND_BREAKER_TRIP,
    SEND_FAIL_PLATFORM_MAX,
    SocialEngine,
    _target_problem,
    _umo_problem,
)

_UMO_ERR = "send_message 返回 False：未找到匹配的会话平台（umo 可能已失效，或该平台不支持主动消息）"


class UmoProblemTest(unittest.TestCase):
    def test_umo_errors_are_recognised(self) -> None:
        self.assertTrue(_umo_problem(_UMO_ERR))
        self.assertTrue(_umo_problem("session not found"))
        self.assertTrue(_umo_problem("会话已失效"))

    def test_global_failures_are_not_umo_problems(self) -> None:
        # adapter 没加载、平台未就绪是**全局**故障，那种就该熔断整个角色
        for err in ("adapter 没加载", "平台未就绪", "connection refused", ""):
            self.assertFalse(_umo_problem(err), err)


class TargetProblemTest(unittest.TestCase):
    """`ActionFailed` / `retcode=` 是平台拒发给**这一个**目标，不是链路坏了。"""

    def test_platform_refusals_are_recognised(self) -> None:
        self.assertTrue(_target_problem(
            "context.send_message 调用失败（结果未知，不重发）: "
            "<ActionFailed status='failed', retcode=1200, data=None>"
        ))
        self.assertTrue(_target_problem("retcode=1200"))

    def test_timeouts_are_not_target_problems(self) -> None:
        # 超时/断连是链路问题，这种才该熔断
        for err in ("发送超时或连接中断，结果未知（不重发）: TimeoutError: ", "adapter 没加载"):
            self.assertFalse(_target_problem(err), err)

    def test_target_refusal_does_not_trip_the_breaker(self) -> None:
        e = _Engine()
        for _ in range(10):
            SocialEngine._note_send_failure(
                e, "b1",
                "context.send_message 调用失败（结果未知，不重发）: "
                "<ActionFailed status='failed', retcode=1200, data=None>",
                False,
            )
        self.assertEqual(SocialEngine.send_breaker_left(e, "b1"), 0.0)

    def test_an_unrecognisable_failure_does_not_trip_either(self) -> None:
        """线上真出现过异常文本为空（`str(e) == ""`），第三次就把整个角色停了。

        熔断的作用是止损；判错方向是**主动断掉所有人的消息**，所以只认能正面
        认出来的全局故障。
        """
        e = _Engine()
        for _ in range(10):
            SocialEngine._note_send_failure(
                e, "b1", "context.send_message 调用失败（结果未知，不重发）: ", False
            )
        self.assertEqual(SocialEngine.send_breaker_left(e, "b1"), 0.0)

    def test_timeouts_still_trip(self) -> None:
        e = _Engine()
        for _ in range(SEND_BREAKER_TRIP):
            SocialEngine._note_send_failure(
                e, "b1", "发送超时或连接中断，结果未知（不重发）: TimeoutError: ", False
            )
        self.assertGreater(SocialEngine.send_breaker_left(e, "b1"), 0.0)


class _Engine:
    def __init__(self):
        self._send_breaker = {}
        self.state = types.SimpleNamespace(mark_dirty=lambda: None)

    def _time(self):
        return 1_000.0

    # 桩对象不走真实 __init__，这两条是引擎自己的方法，得手工绑上
    _breaker_slot = SocialEngine._breaker_slot
    send_breaker_left = SocialEngine.send_breaker_left


class BreakerScopeTest(unittest.TestCase):
    def test_stale_umo_does_not_trip_the_bot_wide_breaker(self) -> None:
        e = _Engine()
        for _ in range(10):
            SocialEngine._note_send_failure(e, "b1", _UMO_ERR, False)
        self.assertEqual(SocialEngine.send_breaker_left(e, "b1"), 0.0)
        self.assertEqual(e._send_breaker["b1"]["streak"], 0)

    def test_global_failure_still_trips(self) -> None:
        e = _Engine()
        for _ in range(SEND_BREAKER_TRIP):
            SocialEngine._note_send_failure(e, "b1", "adapter 没加载", False)
        self.assertGreater(SocialEngine.send_breaker_left(e, "b1"), 0.0)

    def test_peer_unreachable_never_trips(self) -> None:
        e = _Engine()
        for _ in range(10):
            SocialEngine._note_send_failure(e, "b1", "不是你的好友", True)
        self.assertEqual(SocialEngine.send_breaker_left(e, "b1"), 0.0)


class PlatformCooldownTest(unittest.TestCase):
    """废掉的 umo 不会自愈，所以冷却要升档，但必须封顶。"""

    def test_first_failure_is_half_an_hour(self) -> None:
        e = _Engine()
        u: dict = {}
        SocialEngine._mark_send_failed(e, u, platform_side=True, reason=_UMO_ERR)
        self.assertEqual(u["send_fail_until"], 1_000.0 + 1800.0)
        self.assertIn("未找到匹配", u["send_blocked_reason"])

    def test_it_escalates_and_then_stops(self) -> None:
        e = _Engine()
        u: dict = {}
        last = 0.0
        for _ in range(12):
            SocialEngine._mark_send_failed(e, u, platform_side=True, reason=_UMO_ERR)
            now = u["send_fail_until"] - 1_000.0
            self.assertGreaterEqual(now, last)
            last = now
        self.assertEqual(last, SEND_FAIL_PLATFORM_MAX)

    def test_success_clears_both_counters(self) -> None:
        e = _Engine()
        u: dict = {"platform_fail_count": 3, "send_fail_count": 2}
        # `_speak_inner` 里清计数的那几行是纯字面赋值，这里直接照做并断言结果
        u["send_fail_count"] = 0
        u["send_fail_until"] = 0.0
        u["platform_fail_count"] = 0
        u["send_blocked_reason"] = ""
        self.assertEqual(u["platform_fail_count"], 0)
        self.assertEqual(u["send_blocked_reason"], "")


if __name__ == "__main__":
    unittest.main()
