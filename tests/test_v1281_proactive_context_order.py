"""v1.28.1：主动消息之后，她「不知道自己说过什么」。

用户原话：「主动消息后 用户回复了 但是模型在下一次主动消息会把上一次的主动消息
参考用户最晚的消息直接跨天问 结果就是没有上下文回复的很假！不知道说过啥就只会一个劲的问」。

## 症状

昨晚她主动发了一句「刚下班，今天累死了」，用户没回。今天用户回「我也才忙完」，
她走主链路回「那正好，一起摸鱼」，用户再回「哈哈行」。

下一次主动消息生成时，模型看到的「最近的对话」是：

    对方：我也才忙完 / 我：那正好一起摸鱼 / 对方：哈哈行 / 我：刚下班今天累死了

那句主动消息被排到了用户所有回复**之后**。模型据此认为「刚下班」是自己**刚说完**的话，
于是接着这句往下接——用户看到的就是「她根本不知道自己在说哪一句，只会一个劲地问」。

## 根因

`_generation_conversation` 把两个来源合并时用的是「会话库在前，账本里没有的追加到末尾」，
按**来源**拼接，而不是按**时间**排序。主动消息只存在于账本里，于是永远落在最后。

AstrBot 的会话库（`conversations.content`）只存 `role`/`content`，**没有时间戳**，
所以不能给两边的条目各编一个 ts 再一起排——整段会话库会被一起推到账本最老一条之前
或之后。正确做法是拿账本当锚：同文同向的条目就是同一句话，用它的 ts 定位主干上
那个位置，再把缺条目插进去。

## 守门测试

本文件每条用例都钉在「顺序」上：旧代码（按来源拼接）下 `test_*_order_*` 会红。
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
    api.logger = logging.getLogger("autonomous_social_v1281_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.engine import SocialEngine  # noqa: E402
from social.state import SocialState  # noqa: E402

BASE = 1_700_000_000.0


class _Cfg:
    """只要 context_inject_count 一个字段（`_generation_conversation` 只读它）。"""

    def __init__(self, count: int = 10) -> None:
        self.context_inject_count = count


class _Engine(SocialEngine):
    """只借 `_generation_conversation`，绕开整个引擎的构造。"""

    def __init__(self, count: int = 10) -> None:
        self.cfg = _Cfg(count)


class _Clock:
    """可控时钟：每次调用往前走一步，让账本里的 ts 真的不一样。"""

    def __init__(self, start: float = BASE, step: float = 3600.0) -> None:
        self.t = start
        self.step = step

    def __call__(self) -> float:
        value = self.t
        self.t += self.step
        return value


def _ledger(*pairs):
    """[(dir, text, ts), ...] -> conversation 账本条目。"""
    return [
        {"dir": d, "text": t, "ts": ts} for d, t, ts in pairs
    ]


def _render(conv) -> str:
    """把合并结果压成一行，方便断言顺序。"""
    return " | ".join(
        ("我:" if m["dir"] == "out" else "对方:") + m["text"] for m in conv
    )


class ProactiveContextOrderTest(unittest.TestCase):
    """主动消息必须在它**真实发生的时间点**上，不是被推到用户所有回复之后。"""

    def test_reported_case_proactive_is_not_last(self) -> None:
        """用户报的那一条：昨晚主动 -> 今天回 -> 主链路回 -> 再回。"""
        clock = _Clock()
        engine = _Engine()
        ledger = _ledger(
            ("out", "刚下班，今天累死了", clock()),   # 昨晚主动
            ("in", "我也才忙完", clock()),            # 今天回
            ("out", "那正好，一起摸鱼", clock()),     # 主链路回
            ("in", "哈哈行", clock()),                # 再回
        )
        session = [
            {"dir": "in", "text": "我也才忙完"},
            {"dir": "out", "text": "那正好，一起摸鱼"},
            {"dir": "in", "text": "哈哈行"},
        ]
        out = engine._generation_conversation({"conversation": ledger}, session)
        # 主动消息排第一：它确实发生在所有那些回复之前
        self.assertEqual(
            _render(out),
            "我:刚下班，今天累死了 | 对方:我也才忙完 | 我:那正好，一起摸鱼 | 对方:哈哈行",
        )

    def test_proactive_in_the_middle_of_a_long_day(self) -> None:
        """主动消息夹在中间：不能因为「只在账本里」就被扔到末尾。"""
        clock = _Clock(BASE, 100.0)
        engine = _Engine()
        ledger = _ledger(
            ("in", "早", clock()),
            ("out", "早安", clock()),
            ("in", "早", clock()),
            ("out", "刚睡醒，想你了", clock()),   # 主动
            ("in", "哈哈", clock()),
            ("out", "嗯呐", clock()),
        )
        session = [
            {"dir": "in", "text": "早"},
            {"dir": "out", "text": "早安"},
            {"dir": "in", "text": "早"},
            {"dir": "in", "text": "哈哈"},
            {"dir": "out", "text": "嗯呐"},
        ]
        out = engine._generation_conversation({"conversation": ledger}, session)
        self.assertEqual(
            _render(out),
            "对方:早 | 我:早安 | 对方:早 | 我:刚睡醒，想你了 | 对方:哈哈 | 我:嗯呐",
        )

    def test_burst_keeps_ledger_order(self) -> None:
        """连发的几条同 ts 附近：p1 必须在 p2 前面（不能倒过来）。"""
        clock = _Clock(BASE, 10.0)
        engine = _Engine()
        ledger = _ledger(
            ("in", "在吗", clock()),
            ("out", "在呢", clock()),
            ("out", "刚忙完", clock()),      # 连发后续条
            ("in", "哦哦", clock()),
        )
        session = [
            {"dir": "in", "text": "在吗"},
            {"dir": "out", "text": "在呢"},
            {"dir": "in", "text": "哦哦"},
        ]
        out = engine._generation_conversation({"conversation": ledger}, session)
        self.assertEqual(
            _render(out),
            "对方:在吗 | 我:在呢 | 我:刚忙完 | 对方:哦哦",
        )

    def test_no_session_history_returns_ledger_verbatim(self) -> None:
        """会话库读不到时（新会话/被清）：账本就是全部历史，别再动它。"""
        engine = _Engine()
        ledger = _ledger(
            ("out", "A", 10.0),
            ("in", "B", 20.0),
            ("out", "C", 30.0),
        )
        out = engine._generation_conversation({"conversation": ledger}, [])
        self.assertEqual(_render(out), "我:A | 对方:B | 我:C")

    def test_session_and_ledger_identical_keeps_order(self) -> None:
        """两个来源完全一致（没有主动消息要补）：顺序不变。"""
        engine = _Engine()
        ledger = _ledger(
            ("in", "B", 20.0),
            ("out", "C", 30.0),
        )
        session = [
            {"dir": "in", "text": "B"},
            {"dir": "out", "text": "C"},
        ]
        out = engine._generation_conversation({"conversation": ledger}, session)
        self.assertEqual(_render(out), "对方:B | 我:C")

    def test_limit_keeps_the_newest(self) -> None:
        """条数上限仍然保留最新的那几条，且不会把主动消息提到最前面。"""
        engine = _Engine(count=3)
        ledger = _ledger(
            ("out", "m1", 100.0),
            ("out", "m2", 200.0),
            ("out", "m3", 300.0),
            ("out", "m4", 400.0),
        )
        out = engine._generation_conversation({"conversation": ledger}, [])
        self.assertEqual(_render(out), "我:m2 | 我:m3 | 我:m4")

    def test_unknown_ts_session_entry_does_not_block(self) -> None:
        """会话库里有、账本里没有的条目（导入的历史）：不知道时刻就别挡路。"""
        engine = _Engine()
        ledger = _ledger(
            ("in", "a", 100.0),
            ("out", "主动的那句", 150.0),
        )
        session = [{"dir": "in", "text": "导入的历史"}]
        out = engine._generation_conversation({"conversation": ledger}, session)
        self.assertEqual(_render(out), "对方:a | 我:主动的那句 | 对方:导入的历史")

    def test_empty_inputs(self) -> None:
        engine = _Engine()
        self.assertEqual(engine._generation_conversation({"conversation": []}, []), [])
        self.assertEqual(engine._generation_conversation({}, []), [])

    def test_limit_zero_means_off(self) -> None:
        engine = _Engine(count=0)
        ledger = _ledger(("in", "x", 1.0), ("out", "y", 2.0))
        self.assertEqual(engine._generation_conversation({"conversation": ledger}, []), [])

    def test_dirty_ledger_entries_are_skipped(self) -> None:
        """账本里的脏元素（旧版本写进的字符串）不能让合并抛异常。"""
        engine = _Engine()
        ledger = [
            "我不是 dict",
            {"dir": "in", "text": "真话", "ts": 5.0},
            {"dir": "in", "text": "   ", "ts": 6.0},
        ]
        out = engine._generation_conversation({"conversation": ledger}, [])
        self.assertEqual(_render(out), "对方:真话")


class RealStateOrderTest(unittest.TestCase):
    """走一遍真实的 SocialState，确认 ts 真的被记下来了（不只是测试里手写）。"""

    def test_state_records_ts_and_merges_in_order(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            clock = _Clock(BASE, 3600.0)
            state = SocialState(str(Path(tmp) / "state.json"), time_source=clock)
            state.record_outgoing(
                "b1", "u1", "刚下班，今天累死了", about="下班",
                expect_reply=True, why="她想他了",
            )
            # on_llm_request 消费掉待注入的主动消息（发出去→用户回→注入一轮）
            state.user("b1", "u1")["pending_proactive_context"] = ""
            state.record_incoming("b1", "u1", "我也才忙完", name="小明")
            state.record_spoken("b1", "u1", "那正好，一起摸鱼")
            state.record_incoming("b1", "u1", "哈哈行", name="小明")

            conv = state.user("b1", "u1")["conversation"]
            self.assertEqual(len(conv), 4)
            self.assertEqual(conv[0]["dir"], "out")
            self.assertEqual(conv[0]["text"], "刚下班，今天累死了")
            # 每条都带 ts，且严格递增
            stamps = [float(c["ts"]) for c in conv]
            self.assertEqual(stamps, sorted(stamps))

            engine = _Engine()
            session = [
                {"dir": "in", "text": "我也才忙完"},
                {"dir": "out", "text": "那正好，一起摸鱼"},
                {"dir": "in", "text": "哈哈行"},
            ]
            out = engine._generation_conversation(
                {"conversation": conv}, session
            )
            self.assertEqual(
                _render(out),
                "我:刚下班，今天累死了 | 对方:我也才忙完 | 我:那正好，一起摸鱼 | 对方:哈哈行",
            )


class UnansweredProactiveHintTest(unittest.TestCase):
    """生成侧必须知道「上一条主动消息没人接」，否则它会当没问过一样再问一遍。

    用户原话：「不知道说过啥就只会一个劲的问」。只写「你上一句说的是 X」而不写
    「X 没人回」，模型看到的是一段没有结局的对话——它照常往下写。

    旧代码里 `last_proactive_replied` 这个字段**从来没有任何一处读进 prompt**，
    所以这条用例在旧代码下必然红（提示词里找不到那句话）。
    """

    def _generator(self):
        from social.config import SocialConfig
        from social.generator import MessageGenerator

        return MessageGenerator(_FakeCtx(), SocialConfig())

    def _prompt(self, target):
        import asyncio

        gen = self._generator()
        prepared = asyncio.run(gen._compose(
            "aiocqhttp:FriendMessage:u1",
            target,
            "能量: 62 社交能量: 71",
            "",
            None,
            "你是小娜",
            None,
            decide=True,
            at=BASE,
        ))
        self.assertIsNotNone(prepared, "compose 返回 None")
        return prepared[1]

    def _target(self, **over):
        base = {
            "umo": "aiocqhttp:FriendMessage:u1",
            "conversation": [
                {"dir": "out", "text": "刚下班，今天累死了", "ts": BASE - 13 * 3600},
            ],
            "last_message": "",
            "last_spoken_text": "刚下班，今天累死了",
            "last_seen": BASE - 13 * 3600,
            "topics": [],
            "_affection": 62,
            "_recent_proactive": ["刚下班，今天累死了"],
            "_proactive_unanswered": True,
            "_proactive_unanswered_hours": 13.0,
        }
        base.update(over)
        return base

    def test_unanswered_proactive_is_stated_as_a_fact(self) -> None:
        text = self._prompt(self._target())
        self.assertIn("对方没有接话", text)
        self.assertIn("13 小时", text)

    def test_recent_unanswered_uses_a_softer_wording(self) -> None:
        text = self._prompt(self._target(_proactive_unanswered_hours=0.2))
        self.assertIn("对方还没有接话", text)
        self.assertNotIn("13 小时", text)

    def test_answered_proactive_gets_no_such_line(self) -> None:
        """对方接话了就别再提「没人回」——那句话会变成翻旧账。"""
        text = self._prompt(self._target(_proactive_unanswered=False))
        self.assertNotIn("没有接话", text)
        self.assertNotIn("还没有接话", text)

    def test_no_instruction_is_given(self) -> None:
        """只给事实，不给指令：不说「别重复问」（会压出敷衍收场），也不说「再问一次」。"""
        text = self._prompt(self._target())
        for phrase in ("不要重复", "别再问", "不许再问", "必须再问", "应该再问", "重新问"):
            self.assertNotIn(phrase, text, f"提示词里出现了指令性措辞：{phrase}")


class _FakeProvider:
    """最小 provider 替身：只要能过 `_get_provider` 与退避判断。"""

    provider_config = {"id": "fake"}

    def get_model(self):
        return "fake-model"

    async def text_chat(self, **kw):
        return None


class _FakeCtx:
    """只实现 `_get_provider` 用到的那一个方法（它必须是协程）。"""

    async def get_using_provider_async(self, umo=None):
        return _FakeProvider()


class UnansweredSurvivesSheddingTest(unittest.TestCase):
    """「那句话没人接」是上下文，不能被预算裁掉。

    装到线上跑真实数据时发现两个漏点，都在这一版补上：

    1. 它原来挂在 `out_block`（「别重复句式」，纯装饰）那个 shed 组里。
       超预算时装饰先被丢，于是**最需要这句话的用户反而看不到它**。
       现在它独占 `unanswered` 组，排在 `history` 之后、`mind`/`persona` 之前。
    2. 它原来只写在 `assemble()` 的 else 分支里，而 `intimate`（亲密氛围）分支
       整段替换了那个 else——**所有走亲密路的用户都拿不到这个事实**。
       真实数据里 67 个待回复用户有 7 个落在这条路上，提示词里一个字的上下文都没有。
    """

    def _prompt(self, target):
        import asyncio

        gen = self._gen()
        prepared = asyncio.run(gen._compose(
            "aiocqhttp:FriendMessage:u1",
            target,
            "能量: 62 社交能量: 71",
            "",
            None,
            "你是小娜",
            None,
            decide=True,
            at=BASE,
        ))
        self.assertIsNotNone(prepared)
        return prepared[1]

    def _gen(self):
        from social.config import SocialConfig
        from social.generator import MessageGenerator

        return MessageGenerator(_FakeCtx(), SocialConfig())

    def _target(self, **over):
        base = {
            "umo": "aiocqhttp:FriendMessage:u1",
            "conversation": [{"dir": "in", "text": "在吗", "ts": BASE - 7200}],
            "last_message": "在吗",
            "last_spoken_text": "刚下班，今天累死了",
            "last_seen": BASE - 7200,
            "topics": [],
            "_affection": 62,
            "_recent_proactive": ["刚下班，今天累死了"],
            "_proactive_unanswered": True,
            "_proactive_unanswered_hours": 12.0,
        }
        base.update(over)
        return base

    def test_unanswered_has_its_own_shed_group(self) -> None:
        """它不能再挂在 out_block 那个装饰组里。"""
        from social import generator as G

        self.assertIn("unanswered", G.SHED_ORDER)
        self.assertGreater(
            G.SHED_ORDER.index("unanswered"),
            G.SHED_ORDER.index("out_block"),
            "unanswered 必须排在装饰性的 out_block 之后才被丢",
        )
        self.assertLess(
            G.SHED_ORDER.index("unanswered"),
            G.SHED_ORDER.index("persona"),
            "unanswered 不该比人设更晚被丢",
        )

    def test_intimate_branch_also_states_it(self) -> None:
        """亲密氛围分支也不能吞掉这个事实。

        真实数据里 7/67 个待回复用户走这条路（露骨内容对方常常不知道怎么接），
        旧代码在她们身上一个字的上下文都不给。
        """
        # 触发 intimate：最近几条足够亲密
        text = self._prompt(self._target(
            conversation=[
                {"dir": "in", "text": "想操你", "ts": BASE - 600},
                {"dir": "out", "text": "（拉你入怀）这么急？", "ts": BASE - 300},
            ],
            last_message="想操你",
            last_spoken_text="（拉你入怀）这么急？",
        ))
        self.assertIn("对方没有接话", text)

    def test_normal_branch_still_states_it(self) -> None:
        text = self._prompt(self._target())
        self.assertIn("对方没有接话", text)


if __name__ == "__main__":
    unittest.main()
