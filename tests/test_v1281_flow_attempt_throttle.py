"""v1.28.1：群心流「模型选择不接」也要占一次尝试，否则活跃群里会刷屏。

## 线上实测（2026-10-23 00:12）

某个群在一分钟内出现 14 条：

    [social.engine:723] 群 1054414907 心流：模型选择不接或生成为空

间隔 70~700 毫秒一条。也就是说**每来一条群消息就真打了一次 LLM**，
而 LLM 当时是健康的（没有限流）。

## 根因

`note_flow_reply` 只在**成功发送**时调用，它更新 `flow_last_reply_at`。
而 `flow_should_consider` 的「距上一句插话太近」查的就是这个字段。
于是「模型说不接」这条路：

    过闸 -> 调 LLM -> 模型返回空 -> return（什么都不记）

下一条群消息到来时，闸门看到的状态与上一轮完全相同 -> 再打一次 LLM。
`flow_replies` / `flow_ignored` / `flow_hour_count` 三个配额同理都不增长，
因为它们也都在发送成功路径上。

## 修法

新增独立的 `flow_last_attempt_at`：

* 在调 LLM **之前**记一次（`note_flow_attempt`）；
* 闸门多一道「心流刚试过（N 秒前），先歇会儿」；
* **不动**原有三个配额的语义——它们仍然只数真正发出去的话。
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
    api.logger = logging.getLogger("autonomous_social_v1281_flow_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import groupflow  # noqa: E402


def group(**over):
    base = {
        "flow_open_until": 1e18,
        "flow_replies": 0,
        "flow_ignored": 0,
        "flow_last_reply_at": 0.0,
        "flow_last_attempt_at": 0.0,
        "flow_hour_bucket": 0,
        "flow_hour_count": 0,
    }
    base.update(over)
    return base


def consider(g, now, attempt_gap=90.0, **over):
    kw = dict(
        max_replies=3,
        min_gap_seconds=120.0,
        hourly_cap=6,
        hour_count=0,
        ignored_exit=3,
        attempt_gap_seconds=attempt_gap,
    )
    kw.update(over)
    return groupflow.flow_should_consider(g, now, **kw)


class AttemptThrottleTest(unittest.TestCase):
    """新闸门：两次尝试之间至少隔 attempt_gap_seconds。"""

    def test_second_attempt_is_blocked(self) -> None:
        now = 1000.0
        g = group(flow_last_attempt_at=now - 5.0)
        ok, why = consider(g, now, attempt_gap=90.0)
        self.assertFalse(ok, "刚试过 5 秒就又放行，等于没节流")
        self.assertIn("心流刚试过", why)

    def test_old_attempt_does_not_block(self) -> None:
        now = 1000.0
        g = group(flow_last_attempt_at=now - 200.0)
        ok, why = consider(g, now, attempt_gap=90.0)
        self.assertTrue(ok, f"试过 200 秒了还被挡：{why}")

    def test_never_attempted_does_not_block(self) -> None:
        ok, why = consider(group(), 1000.0, attempt_gap=90.0)
        self.assertTrue(ok, f"从没试过却被挡：{why}")

    def test_quota_gate_wins_over_attempt_gate(self) -> None:
        now = 1000.0
        g = group(flow_replies=3, flow_last_attempt_at=now - 5.0)
        ok, why = consider(g, now, attempt_gap=90.0)
        self.assertFalse(ok)
        self.assertIn("接够了", why)

    def test_reply_gap_still_uses_reply_timestamp(self) -> None:
        now = 1000.0
        g = group(flow_last_reply_at=now - 30.0, flow_last_attempt_at=now - 999.0)
        ok, why = consider(g, now, attempt_gap=90.0, min_gap_seconds=120.0)
        self.assertFalse(ok)
        self.assertIn("距上一句插话太近", why)

    def test_successful_send_also_counts_as_attempt(self) -> None:
        now = 1000.0
        g = group(flow_last_reply_at=now - 10.0, flow_last_attempt_at=now - 10.0)
        ok, _why = consider(g, now, attempt_gap=90.0, min_gap_seconds=120.0)
        self.assertFalse(ok)

    def test_zero_gap_disables_the_throttle(self) -> None:
        now = 1000.0
        g = group(flow_last_attempt_at=now - 1.0)
        ok, _why = consider(g, now, attempt_gap=0.0)
        self.assertTrue(ok)


class StateAttemptTest(unittest.TestCase):
    """note_flow_attempt 只记时间戳，不碰任何配额。"""

    def _state(self):
        import tempfile
        from pathlib import Path

        from social.state import SocialState

        return SocialState(str(Path(tempfile.mkdtemp()) / "state.json"))

    def test_attempt_updates_only_the_timestamp(self) -> None:
        st = self._state()
        st.note_flow_attempt("b1", "g1", 500.0)
        g = st.group("b1", "g1")
        self.assertEqual(g["flow_last_attempt_at"], 500.0)
        self.assertEqual(g["flow_replies"], 0)
        self.assertEqual(g["flow_ignored"], 0)
        self.assertEqual(g["flow_hour_count"], 0)
        self.assertEqual(g["flow_last_reply_at"], 0.0)

    def test_attempt_is_idempotent_for_quota(self) -> None:
        st = self._state()
        for t in (100.0, 200.0, 300.0):
            st.note_flow_attempt("b1", "g1", t)
        g = st.group("b1", "g1")
        self.assertEqual(g["flow_last_attempt_at"], 300.0)
        self.assertEqual(g["flow_replies"], 0)

    def test_old_state_without_the_key_is_treated_as_never(self) -> None:
        st = self._state()
        g = st.group("b1", "g1")
        g.pop("flow_last_attempt_at", None)
        # 关注窗口要开着，否则被测的是另一道闸
        g["flow_open_until"] = 1e18
        ok, why = groupflow.flow_should_consider(
            g, 1000.0,
            max_replies=3, min_gap_seconds=120.0, hourly_cap=6,
            hour_count=0, ignored_exit=3, attempt_gap_seconds=90.0,
        )
        self.assertTrue(ok, f"旧存档被这道闸挡死了：{why}")


if __name__ == "__main__":
    unittest.main()