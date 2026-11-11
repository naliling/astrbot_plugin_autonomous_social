"""v2.2.4：把「soc 一直不通过」的根因钉住。

## 线上实测（2026-11-09 20:30 → 11-10 01:56，soc 2.2.3）

窗口内被挡下的原因（按次数）：

    上次发给 TA 没发出去，N 分钟内不再试      57
    这条引用了证据卡里没有的事（编的）         27
    整条只有一行标题、没有正文                 22
    消息写好了但发送失败（结果未知，不重发）   20
    TA 一直没回、她已经主动发了 3 条           14
    复读（和最近发过的一条重合度 72% 以上）     8
    空泛（正文只有 '笨凝'）                     8
    LLM 不可用（3 条 provider 都调不通，退避中，还剩 43 秒）    6

## 四条根因

1. **`_dead_providers` 是永久集合**：三个专供模型只要都进过黑名单，
   `_get_provider` 在严格模式下永远返回 None，整轮只打一句「LLM 不可用
   （3 条 provider 都调不通，退避中）」，直到重载插件才恢复。→ 改成带 TTL 的跳过表。
2. **`_call_llm` 收到 None**：`_review_draft` 传的是
   `getattr(self.generator, "provider", None)`，而 `MessageGenerator` 上没有
   `provider` 属性，于是每次自评都拿 None 去调，`_candidates` 必然为空——
   日志里所有「无可用调用方法」都是同一个 `obj<id(None)>`，「生成后自评」
   这道闸从来没真正跑过。→ 入口先解析真 provider，且不把 None 记死。
3. **`require_about_peer` 被逐段套用**：一条消息分两段，只要有一段是纯自述，
   整条按「没有指向对方」作废。→ 改成整条级判定。
4. **退化的后续段让整条作废**：模型在结尾补一句昵称（`笨凝`）或只写一行标题，
   整条消息就不发了。→ 丢掉坏段、剩下的照发。
"""
from __future__ import annotations

import asyncio
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
    api.logger = logging.getLogger("autonomous_social_v224_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.engine import SocialEngine  # noqa: E402
from social.generator import DEAD_PROVIDER_TTL, MessageGenerator  # noqa: E402
from social.verify import verify_message  # noqa: E402


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += float(secs)


def _gen(clock):
    return MessageGenerator(context=None, config=None, time_source=clock)


class DeadProviderTtlTest(unittest.TestCase):
    """结构性不可用的 provider 只跳过 TTL 这么久，不能永久记死。"""

    def test_no_methods_marks_dead_with_expiry(self) -> None:
        clock = _Clock()
        g = _gen(clock)
        provider = types.SimpleNamespace()  # 什么方法都没有
        asyncio.run(g._call_llm(provider, "hi"))
        scope = g._scope_of(provider)
        self.assertIn(scope, g._dead_until, "没有方法集的 provider 应该被记成暂时不可用")
        self.assertAlmostEqual(g._dead_until[scope], clock.t + DEAD_PROVIDER_TTL, places=3)
        self.assertEqual({}, g.llm_circuit_state(), "结构性不可用不该立熔断")

    def test_dead_provider_comes_back_after_ttl(self) -> None:
        clock = _Clock()
        g = _gen(clock)
        dead = types.SimpleNamespace()
        g._dead_until[g._scope_of(dead)] = clock.t + 60.0
        g._designated_configured = lambda: True  # type: ignore[assignment]
        g.cfg = types.SimpleNamespace(proactive_provider_ids=("dead",))
        g.context = types.SimpleNamespace(get_provider_by_id=lambda pid: dead)
        self.assertIsNone(
            asyncio.run(g._get_designated_provider()), "TTL 内应该跳过这个 provider"
        )
        clock.advance(61.0)
        self.assertIs(
            asyncio.run(g._get_designated_provider()),
            dead,
            "TTL 过了必须再试一次，否则整轮静默到重载插件",
        )

    def test_none_provider_is_not_marked_dead(self) -> None:
        """自评那条路以前传 None：不能把它记死，也不能刷同一条告警。"""
        clock = _Clock()
        g = _gen(clock)
        self.assertIsNone(asyncio.run(g._call_llm(None, "hi")))
        self.assertEqual({}, g._dead_until)


class DegenerateSegmentTest(unittest.TestCase):
    """纯称呼 / 只有标题 的段是可丢的废话，不该让整条消息作废。"""

    def test_degenerate_reasons(self) -> None:
        f = SocialEngine._is_degenerate_segment
        self.assertTrue(f("空泛（正文只有 '笨凝'）"))
        self.assertTrue(f("空泛（正文只有 '银星'）"))
        self.assertTrue(f("整条只有一行标题、没有正文"))

    def test_real_reasons_are_not_degenerate(self) -> None:
        f = SocialEngine._is_degenerate_segment
        self.assertFalse(f("没有指向对方（这一类要说给 TA 听，不是讲她自己）"))
        self.assertFalse(f("复读（和最近发过的一条重合度 72% 以上）"))
        self.assertFalse(f("打破第四面墙（提了设备「手机」）"))
        self.assertFalse(f(""))


class PeerRuleIsMessageLevelTest(unittest.TestCase):
    """`require_about_peer` 判的是整条消息，不是每一段。"""

    def test_self_talk_segment_does_not_sink_the_message(self) -> None:
        segments = ["我刚把窗关严实了，风从缝里钻进来", "你那边冷不冷"]
        joined = "\n".join(segments)
        ok, why = verify_message(joined, require_about_peer=True)
        self.assertTrue(ok, f"整条里有指向对方的话就该放行，实际：{why}")
        ok_first, _ = verify_message(segments[0], require_about_peer=True)
        self.assertFalse(ok_first, "单看第一段确实没有指向对方——这正是旧实现误杀的原因")

    def test_message_without_peer_still_rejected(self) -> None:
        ok, why = verify_message("我刚把窗关严实了", require_about_peer=True)
        self.assertFalse(ok)
        self.assertIn("没有指向对方", why)



class StrictFallsBackWhenTemporarilyDeadTest(unittest.TestCase):
    """严格模式是防「ID 配错」，不是防「模型暂时抽风」。

    11-10 凌晨实测：三个专供模型进 TTL 黑名单后，每轮只打一句
    「LLM 不可用（3 条 provider 都调不通，退避中，还剩 43 秒）」，静默到重载插件。
    """

    def _wire(self, clock, *, strict=True):
        g = _gen(clock)
        alive = types.SimpleNamespace()
        dead1 = types.SimpleNamespace()
        dead2 = types.SimpleNamespace()
        fallback = types.SimpleNamespace()

        async def _using(umo=""):
            return fallback

        g.cfg = types.SimpleNamespace(
            proactive_provider_ids=("a", "b"),
            strict_proactive_provider=strict,
        )
        g.context = types.SimpleNamespace(
            get_provider_by_id=lambda pid: {"a": dead1, "b": dead2}.get(pid),
            get_using_provider_async=_using,
        )
        g._dead_until[g._scope_of(dead1)] = clock.t + 300.0
        g._dead_until[g._scope_of(dead2)] = clock.t + 300.0
        return g, alive, dead1, dead2, fallback

    def test_strict_falls_back_when_all_designated_are_in_ttl(self) -> None:
        clock = _Clock()
        g, _alive, _d1, _d2, fallback = self._wire(clock)
        self.assertTrue(g._designated_temporarily_dead())
        self.assertIs(
            asyncio.run(g._get_provider("u")),
            fallback,
            "专供只是暂时不可用时，严格模式也该回退会话模型，而不是整轮静默",
        )

    def test_strict_still_silent_when_one_designated_is_healthy(self) -> None:
        clock = _Clock()
        g, _alive, _d1, _d2, _fb = self._wire(clock)
        healthy = types.SimpleNamespace()
        g.context.get_provider_by_id = lambda pid: {"a": healthy}.get(pid)

        async def _boom(umo=""):  # pragma: no cover - 不该被调到
            raise AssertionError("还有能用的专供时不该回退会话模型")

        g.context.get_using_provider_async = _boom
        self.assertFalse(g._designated_temporarily_dead())
        self.assertIs(
            asyncio.run(g._get_provider("u")),
            healthy,
            "有能用的专供时就用它，不回退也不静默",
        )

    def test_ttl_expiry_releases_the_provider(self) -> None:
        clock = _Clock()
        g, _alive, _d1, _d2, _fb = self._wire(clock)
        clock.advance(301.0)
        self.assertFalse(g._designated_temporarily_dead())


if __name__ == "__main__":
    unittest.main()

if __name__ == "__main__":
    unittest.main()
