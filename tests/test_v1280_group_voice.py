"""v1.28.0 社交侧 S1 + S2：群主动不点名、素材回到她自己的生活、没话说就不发。

对应设计文档 `DESIGN-v1.28.0.md` §2：

* S1 —— 用户原话「主动消息发群时会叫别人」：群主动默认不点名；起话头/破冰
  **一个名字都不给**；心流接话最多只给「正在接的那个人」一个名字。
* S2 —— 用户原话「只有天气，不会真的找话题」「如果没有能说的就不发」：
  破冰/起话头的素材改成「她自己的生活」优先（刚做完/正在做/想做的/没做完/梦到的），
  天气只在「变了 + 6 小时未说过」时当备选；破冰/起话头加 SEND/NO 决策，
  模型答 NO 时这次不发、也不消耗当天的额度。

另外覆盖与 Core 2.28 新字段 `day.dreams` 的兼容：旧版 Core 没有这个字段时必须
容错成空，不允许 AttributeError/KeyError。
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
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
    api.logger = logging.getLogger("autonomous_social_v1280_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import anchors, generator, groupflow  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.core_bridge import CoreBridge  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social.generator import MessageGenerator  # noqa: E402
from social.state import SocialState  # noqa: E402

NOW = 1_700_000_000.0


class _StubProvider:
    """`_get_provider` 的最小替身：只需要能算出一个退避 scope。"""

    id = "p1280"


def _fake_llm(text: str):
    """把 generator 的两层调用直接换掉（与 test_v1273 的写法一致）。"""

    async def _provider(_umo):
        return _StubProvider()

    async def _call(_provider, _prompt):
        return text

    return _provider, _call


# ═══════════════════════════════════════════════════════════════════════
# S1：群主动不点名
# ═══════════════════════════════════════════════════════════════════════


class GroupBoundaryNoNameTest(unittest.TestCase):
    """S1：主动开口是对着整个群说的，默认不该叫任何群友的名字。"""

    def test_icebreak_gives_no_name_and_says_so(self):
        text = generator._group_boundary(
            ["小明", "小红"], mention=True, mode="icebreak"
        )
        self.assertNotIn("小明", text, "破冰提示词里还带着群友名")
        self.assertNotIn("小红", text)
        self.assertIn("别点名", text)
        self.assertIn("所有人", text)

    def test_topic_gives_no_name_and_says_so(self):
        text = generator._group_boundary(["小明"], mention=True, mode="topic")
        self.assertNotIn("小明", text, "起话头提示词里还带着群友名")
        self.assertIn("别点名", text)

    def test_flow_mention_gives_only_the_one_being_answered(self):
        """心流 + 允许点名：只给最新说话的那个人（members 最新在前）。"""
        text = generator._group_boundary(
            ["小明", "小红", "阿岚"], mention=True, mode="flow"
        )
        self.assertIn("小明", text)
        self.assertNotIn("小红", text, "把整个群名单都递给模型了")
        self.assertNotIn("阿岚", text)
        self.assertIn("正在接的那个人", text)
        self.assertIn("提问", text, "没有说明「别用名字去提问」")

    def test_flow_without_mention_names_nobody(self):
        text = generator._group_boundary(["小明"], mention=False, mode="flow")
        self.assertNotIn("小明", text)
        self.assertIn("不要点名", text)

    def test_empty_names_do_not_add_a_name_line(self):
        text = generator._group_boundary(["", "   "], mention=True, mode="flow")
        self.assertIn("不要点名", text)

    def test_config_default_is_off_and_schema_agrees(self):
        self.assertFalse(
            SocialConfig().group_mention_member, "group_mention_member 默认又打开了"
        )
        import json

        schema = json.loads(
            (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertIs(schema["group_mention_member"]["default"], False)

    def test_compose_group_icebreak_never_shows_member_names(self):
        """端到端：group_ctx 里带着 members，破冰/起话头 prompt 里也不许出现。"""
        gen = MessageGenerator(None, None)
        prompt, _ = gen._compose_group(
            "icebreak", {"members": ["小明"], "topic_hint": "- 下午去了银行"}, "人设", 40
        )
        self.assertNotIn("小明", prompt)
        self.assertIn("别点名", prompt)
        prompt_topic, _ = gen._compose_group(
            "topic", {"members": ["小红"]}, "人设", 40
        )
        self.assertNotIn("小红", prompt_topic)
        self.assertIn("别点名", prompt_topic)


# ═══════════════════════════════════════════════════════════════════════
# S2：群主动素材 = 她自己的生活
# ═══════════════════════════════════════════════════════════════════════


class TopicMaterialsTest(unittest.TestCase):
    """S2：生活 > 节日 > 天气；全空返回空串。"""

    def test_order_is_life_then_holiday_then_weather(self):
        text = groupflow.topic_materials(
            life=["刚做完手头的活"], holiday="中秋节", weather="中雨，气温 9℃"
        )
        lines = text.splitlines()
        self.assertEqual(len(lines), 3, text)
        self.assertIn("刚做完手头的活", lines[0])
        self.assertIn("中秋", lines[1])
        self.assertIn("中雨", lines[2])
        # 天气那一行照旧走 outside_lead（原句没交代方位时补「外头」）
        self.assertTrue(lines[2].startswith("- 外头"))

    def test_no_life_still_has_holiday_and_weather(self):
        text = groupflow.topic_materials(holiday="春节", weather="外面在下雪")
        lines = text.splitlines()
        self.assertEqual(len(lines), 2, text)
        self.assertIn("春节", lines[0])
        self.assertIn("外面在下雪", lines[1])

    def test_all_empty_is_empty(self):
        self.assertEqual(groupflow.topic_materials(), "")
        self.assertEqual(groupflow.topic_materials(life=["", "  "]), "")

    def test_life_capped_at_four(self):
        text = groupflow.topic_materials(life=["一", "二", "三", "四", "五"])
        self.assertEqual(len(text.splitlines()), 4)

    def test_icebreak_topics_keeps_the_old_wrapper(self):
        """旧入口不删：破冰素材的名字与「节日/天气」语义保持兼容。"""
        self.assertEqual(groupflow.icebreak_topics("", ""), "")
        text = groupflow.icebreak_topics("中秋节", "中雨，气温 9℃")
        self.assertIn("中秋", text)
        self.assertIn("中雨", text)


class LifeLinesTest(unittest.TestCase):
    """S2：从契约 body 里抽「她自己的生活」，每个来源都要认。"""

    BODY = {
        "day": {
            "done": ["下午去了一趟银行", "傍晚才醒"],
            "doing": "整理个案笔记",
            "wants": ["去那家旧书店", "看一场电影"],
            "dreams": ["自己在天上飞"],
        },
        "ongoing": ["练一首新曲子", "把阳台的花换盆"],
    }

    def test_high_priority_sources_are_extracted(self):
        """总条数上限 4：done(1) + doing + wants(2) 之后轮不到 ongoing/梦。"""
        lines = groupflow.life_lines(self.BODY)
        self.assertIn("下午去了一趟银行", lines)
        self.assertIn("正在整理个案笔记", lines)
        self.assertIn("今天去那家旧书店", lines)
        self.assertEqual(len(lines), groupflow.LIFE_MAX_LINES)

    def test_every_source_is_extracted(self):
        """每个来源单独喂都要认（含 ongoing / 梦——它们排在后面）。"""
        cases = (
            ({"day": {"done": ["下午去了一趟银行"]}}, "下午去了一趟银行"),
            ({"day": {"doing": "整理个案笔记"}}, "正在整理个案笔记"),
            ({"day": {"wants": ["去那家旧书店"]}}, "今天去那家旧书店"),
            ({"ongoing": ["练一首新曲子"]}, "还在练一首新曲子"),
            ({"day": {"dreams": ["自己在天上飞"]}}, "梦到自己在天上飞"),
        )
        for body, want in cases:
            with self.subTest(want=want):
                self.assertIn(want, groupflow.life_lines(body))

    def test_awake_verb_is_filtered_out_of_done(self):
        lines = groupflow.life_lines(self.BODY)
        self.assertNotIn("傍晚才醒", lines, "「才醒」不是做了件事")

    def test_vague_done_is_filtered(self):
        body = {"day": {"done": ["忙自己的事", "发呆"]}}
        self.assertEqual(groupflow.life_lines(body), [])

    def test_dream_is_written_as_dreamt(self):
        lines = groupflow.life_lines({"day": {"dreams": ["自己在天上飞"]}})
        self.assertEqual(lines, ["梦到自己在天上飞"])

    def test_wants_prefix_does_not_double(self):
        lines = groupflow.life_lines(
            {"day": {"wants": ["想去看那个展", "拐去那家书店"]}}
        )
        self.assertIn("今天想去看那个展", lines)
        self.assertIn("今天拐去那家书店", lines)
        for line in lines:
            self.assertNotIn("想去想", line)

    def test_dedupe_and_caps(self):
        body = {
            "day": {
                "done": ["同一件事", "同一件事"],
                "doing": "同一件事",
                "wants": ["一", "二"],
                "dreams": ["三"],
            },
            "ongoing": ["四", "五"],
        }
        lines = groupflow.life_lines(body)
        self.assertLessEqual(len(lines), groupflow.LIFE_MAX_LINES)
        self.assertEqual(len(lines), len(set(lines)))
        for line in lines:
            self.assertLessEqual(len(line), groupflow.LIFE_LINE_MAX_CHARS)

    def test_dirty_shapes_are_tolerated(self):
        """旧版 Core 没有 wants/dreams；手改坏时会是字符串/字典。一律当没有。"""
        for body in (
            {},
            {"day": None},
            {"day": "坏数据"},
            {"day": {"done": "不是列表", "doing": {"不是": "字符串"},
                     "wants": 3, "dreams": {"x": 1}}},
            {"ongoing": "不是列表"},
            None,
        ):
            with self.subTest(body=body):
                self.assertIsInstance(groupflow.life_lines(body), list)

    def test_doing_starting_with_zai_does_not_double(self):
        """Core 的 event 名有时本身就是「在厨房煮饭」，别再补一个「正在」。"""
        self.assertEqual(
            groupflow.life_lines({"day": {"doing": "在厨房煮饭"}}),
            ["在厨房煮饭"],
        )


class DreamsBridgeTest(unittest.TestCase):
    """S2 / C4：Core 2.28 的 `day.dreams` 要进 self view；旧版 Core 容错成 []。"""

    def _view(self, contract_day):
        bridge = CoreBridge("bridge", None)
        contract = {"v": 2, "day": contract_day, "body": {}}
        return bridge._build_self_view({"contract": contract}, "bot1")

    def test_dreams_are_mapped(self):
        view = self._view({"dreams": ["梦到下雨", "梦到考试"]})
        self.assertEqual(view["day"]["dreams"], ["梦到下雨", "梦到考试"])

    def test_missing_field_is_empty_list(self):
        view = self._view({})
        self.assertEqual(view["day"]["dreams"], [])

    def test_bad_shapes_are_empty_list(self):
        for raw in ("不是列表", 3, {"x": 1}, [None, {"x": 1}, "", "  "]):
            with self.subTest(raw=raw):
                view = self._view({"dreams": raw})
                self.assertEqual(view["day"]["dreams"], [])

    def test_mixed_list_keeps_only_usable_strings(self):
        view = self._view({"dreams": [None, {"x": 1}, "梦到下雨", "  "]})
        self.assertEqual(view["day"]["dreams"], ["梦到下雨"])

    def test_no_contract_still_builds_a_view(self):
        """旧版 Core（没有契约）走的是 view 为空 dict 的那条路，不能崩。"""
        bridge = CoreBridge("bridge", None)
        view = bridge._build_self_view({}, "bot1")
        self.assertIsInstance(view["day"], dict)
        self.assertEqual(groupflow.life_lines(view), [])


# ═══════════════════════════════════════════════════════════════════════
# S2：私聊由头（anchors）
# ═══════════════════════════════════════════════════════════════════════


class AnchorLifeSourcesTest(unittest.TestCase):
    """S2：刚做完 / 正在做 / 梦到都要能成为由头；天气不许绕过闸。"""

    def test_doing_is_ongoing_not_just_finished(self):
        """用户反馈的语义错误：「刚忙完在厨房煮饭」——doing 是此刻正在做。"""
        got = anchors._derive_from_state(
            {"day": {"doing": "整理个案笔记"}}, {}, NOW, set()
        )
        self.assertIn("正在整理个案笔记", got)
        self.assertFalse(any("刚忙完" in x for x in got), got)

    def test_done_becomes_an_anchor(self):
        got = anchors._derive_from_state(
            {"day": {"done": ["下午去了一趟银行", "傍晚才醒"]}}, {}, NOW, set()
        )
        self.assertIn("下午去了一趟银行", got)
        self.assertNotIn("傍晚才醒", got)

    def test_done_vague_is_filtered(self):
        got = anchors._derive_from_state(
            {"day": {"done": ["忙自己的事"]}}, {}, NOW, set()
        )
        self.assertFalse(any("忙自己的事" in x for x in got), got)

    def test_dream_becomes_an_anchor(self):
        got = anchors._derive_from_state(
            {"day": {"dreams": ["自己在天上飞"]}}, {}, NOW, set()
        )
        self.assertIn("梦到自己在天上飞", got)

    def test_dream_wrong_type_is_silent(self):
        for dreams in ("自己在天上飞", 3, {"x": 1}, [None, {"x": 1}]):
            with self.subTest(dreams=dreams):
                got = anchors._derive_from_state(
                    {"day": {"dreams": dreams}}, {}, NOW, set()
                )
                self.assertFalse(any("梦到" in x for x in got), got)

    def test_dream_kind_is_registered(self):
        self.assertEqual(anchors.KIND_RANK[anchors.KIND_DREAM], 2)
        self.assertEqual(anchors._KIND_TTL[anchors.KIND_DREAM], anchors.TTL_DAY_PART)
        self.assertIn("梦", anchors.anchor_sentence(
            {"kind": anchors.KIND_DREAM, "about": "梦到自己在天上飞"}
        ))
        self.assertEqual(
            anchors.anchor_meta({"kind": anchors.KIND_DREAM})["mode"], "share"
        )

    def test_dream_is_a_dream_kind_anchor(self):
        picks = anchors.prepare_round_anchors(
            {}, {"day": {"dreams": ["自己在天上飞"]}}, NOW, limit=5
        )
        kinds = [p["kind"] for p in picks]
        self.assertIn(anchors.KIND_DREAM, kinds, picks)
        self.assertTrue(any("梦到自己在天上飞" in p["about"] for p in picks), picks)

    def test_weather_is_single_entry_with_the_six_hour_gate(self):
        """天气只能从 extras 进池子，而且第二次不许再说同一句。"""
        body = {"weather": {"env": "中雨，气温 9℃"}}
        u: dict = {}
        first = anchors.derive_extras(u, body, NOW, set())
        self.assertIn(anchors.KIND_WEATHER, [k for k, _ in first], first)
        second = anchors.derive_extras(u, body, NOW + 60, set())
        self.assertNotIn(
            anchors.KIND_WEATHER,
            [k for k, _ in second],
            "同一句天气被重复派生（6 小时闸没生效）",
        )

    def test_changed_weather_within_six_hours_is_held(self):
        u: dict = {}
        anchors.derive_extras(u, {"weather": {"env": "中雨，气温 9℃"}}, NOW, set())
        later = {"weather": {"env": "小雨，10℃"}}
        held = anchors.derive_extras(u, later, NOW + 3600, set())
        self.assertNotIn(anchors.KIND_WEATHER, [k for k, _ in held])
        after = anchors.derive_extras(u, later, NOW + 7 * 3600, set())
        self.assertIn(anchors.KIND_WEATHER, [k for k, _ in after])

    def test_plain_weather_is_not_an_anchor(self):
        """多云/20℃ 这种不算话头——extras 仍保留「变了样」的第一道筛选。"""
        picks = anchors.prepare_round_anchors(
            {}, {"weather": "多云，气温 20℃"}, NOW, limit=5
        )
        self.assertFalse(
            any(p.get("kind") == anchors.KIND_WEATHER for p in picks), picks
        )

    def test_life_anchor_comes_before_weather(self):
        picks = anchors.prepare_round_anchors(
            {},
            {"day": {"done": ["下午去了一趟银行"]},
             "weather": {"env": "中雨，气温 9℃"}},
            NOW,
            limit=5,
        )
        abouts = [str(p.get("about") or "") for p in picks]
        life = next(i for i, a in enumerate(abouts) if "银行" in a)
        weather = next(i for i, a in enumerate(abouts) if "中雨" in a)
        self.assertLess(life, weather, f"生活由头排在天气后面：{abouts}")


# ═══════════════════════════════════════════════════════════════════════
# S2：破冰/起话头 prompt + SEND/NO 决策
# ═══════════════════════════════════════════════════════════════════════


class IcebreakPromptTest(unittest.TestCase):
    """S2：prompt 要教她「优先分享自己的生活」「没话说就不发」。"""

    def _prompt(self, mode: str = "icebreak", ctx=None) -> str:
        gen = MessageGenerator(None, None)
        prompt, _ = gen._compose_group(
            mode, ctx or {"topic_hint": "- 下午去了一趟银行"}, "人设", 60
        )
        return prompt

    def test_material_block_is_her_own_life(self):
        prompt = self._prompt()
        self.assertIn("她自己的生活", prompt)
        self.assertIn("没有就别硬说", prompt)
        self.assertIn("下午去了一趟银行", prompt)

    def test_weather_is_only_a_fallback(self):
        prompt = self._prompt()
        self.assertIn("节日、天气只是备选", prompt)
        self.assertIn("别把它当唯一话头", prompt)

    def test_send_no_decision_is_offered(self):
        prompt = self._prompt()
        self.assertIn("SEND", prompt)
        self.assertIn("只写一行 NO", prompt)
        self.assertIn("这次不发", prompt)

    def test_topic_mode_also_has_the_decision(self):
        self.assertIn("只写一行 NO", self._prompt("topic"))

    def test_original_requirements_are_kept(self):
        prompt = self._prompt()
        self.assertIn("别像客服", prompt)
        self.assertIn("长度不超过", prompt)
        self.assertIn("别点名", prompt)


class IcebreakDecisionParseTest(unittest.IsolatedAsyncioTestCase):
    """S2：`group_message(..., "icebreak")` 的 SEND/NO 解析。"""

    async def _parts(self, text: str, mode: str = "icebreak"):
        gen = MessageGenerator(None, None)
        provider, call = _fake_llm(text)
        gen._get_provider = provider
        gen._call_llm = call
        return await gen.group_message("u", mode, {}, "人设")

    async def test_no_means_do_not_send(self):
        for text in ("NO", "NO。", "  NO！  ", "NO，今天没什么好说的"):
            with self.subTest(text=text):
                self.assertIsNone(await self._parts(text), f"{text!r} 被当成正文发了")

    async def test_send_prefix_is_stripped(self):
        parts = await self._parts("SEND\n下午去银行回来路上买了花")
        self.assertEqual(parts, ["下午去银行回来路上买了花"])

    async def test_send_with_no_body_means_do_not_send(self):
        self.assertIsNone(await self._parts("SEND"))

    async def test_plain_body_is_still_accepted(self):
        """没写协议的宽容路径不能丢：模型直接写正文也收。"""
        parts = await self._parts("下午去银行回来路上买了花")
        self.assertEqual(parts, ["下午去银行回来路上买了花"])

    async def test_intent_line_is_still_stripped(self):
        parts = await self._parts("#想开个话头\n今天真冷啊")
        self.assertEqual(parts, ["今天真冷啊"])

    async def test_topic_mode_shares_the_decision(self):
        self.assertIsNone(await self._parts("NO", mode="topic"))
        parts = await self._parts("SEND\n刚看完那部老电影", mode="topic")
        self.assertEqual(parts, ["刚看完那部老电影"])


# ═══════════════════════════════════════════════════════════════════════
# S2：engine 的素材组装（天气闸）与「不发不占额度」
# ═══════════════════════════════════════════════════════════════════════


class _HintEngine:
    """只使用 `_group_topic_hint` 的最小替身。"""

    _group_topic_hint = SocialEngine._group_topic_hint

    def __init__(self, body):
        self.core = types.SimpleNamespace(bot_self_state=lambda _bid: body)


class GroupTopicHintTest(unittest.TestCase):
    def test_life_first_then_weather(self):
        body = {
            "day": {"done": ["下午去了一趟银行"], "doing": "整理个案笔记"},
            "holiday": "中秋节",
            "weather": {"env": "中雨，气温 9℃"},
        }
        g: dict = {}
        text, weather = _HintEngine(body)._group_topic_hint("b", "g", g, NOW)
        lines = text.splitlines()
        self.assertIn("下午去了一趟银行", lines[0])
        self.assertIn("中秋", text)
        self.assertTrue(weather, "天气过了闸却没被采用")
        self.assertIn(weather, text)

    def test_weather_is_gated_for_six_hours(self):
        body = {"weather": {"env": "中雨，气温 9℃"}}
        g: dict = {}
        engine = _HintEngine(body)
        _text, weather = engine._group_topic_hint("b", "g", g, NOW)
        self.assertTrue(weather)
        # 发送成功的路径会记账；这里直接替它记上，模拟「刚在群里说过天气」。
        anchors.mark_weather_told(g, {"weather": {"env": weather}}, NOW)
        text, again = engine._group_topic_hint("b", "g", g, NOW + 3600)
        self.assertEqual(again, "", "6 小时闸没生效，天气又被当成话头")
        self.assertNotIn("中雨", text)
        # 一字不差永远不再说；变了样的天气要过了 6 小时才轮到它。
        same, unchanged = engine._group_topic_hint("b", "g", g, NOW + 7 * 3600)
        self.assertEqual(unchanged, "")
        self.assertNotIn("中雨", same)
        changed = {"weather": {"env": "小雨，10℃"}}
        changed_engine = _HintEngine(changed)
        held_text, held = changed_engine._group_topic_hint("b", "g", g, NOW + 3600)
        self.assertEqual(held, "", "变了样的天气在一小时内又说了一遍")
        self.assertNotIn("小雨", held_text)
        text2, after = changed_engine._group_topic_hint("b", "g", g, NOW + 7 * 3600)
        self.assertTrue(after)
        self.assertIn("小雨", text2)

    def test_old_core_without_day_fields_is_tolerated(self):
        for body in ({}, None, {"day": None}, {"holiday": "", "weather": {}}):
            with self.subTest(body=body):
                text, weather = _HintEngine(body)._group_topic_hint("b", "g", {}, NOW)
                self.assertEqual(text, "")
                self.assertEqual(weather, "")

    def test_body_raising_is_tolerated(self):
        class _Boom:
            def bot_self_state(self, _bid):
                raise RuntimeError("core 挂了")

        engine = _HintEngine({})
        engine.core = _Boom()
        self.assertEqual(engine._group_topic_hint("b", "g", {}, NOW), ("", ""))


class _FakeGenerator:
    def __init__(self, parts):
        self.parts = parts

    async def group_message(self, *_args, **_kwargs):
        return self.parts


class _DateTimeStub:
    def strftime(self, _fmt):
        return "2026-08-22"


class _IcebreakEngine:
    """只实现 `_icebreak` 走到「发 / 不发」所需的最小表面。"""

    _icebreak = SocialEngine._icebreak
    _group_topic_hint = SocialEngine._group_topic_hint

    def __init__(self, body, parts, state):
        self.core = types.SimpleNamespace(bot_self_state=lambda _bid: body)
        self.generator = _FakeGenerator(parts)
        self.state = state
        self.cfg = SocialConfig()
        self.context = None
        self._last_persona: dict = {}
        self.logs: list = []
        self.sent: list = []

    def _time(self):
        return NOW

    def log(self, message):
        self.logs.append(str(message))

    def send_breaker_left(self, _bid):
        return 0.0

    def _build_group_ctx(self, _bid, _gid, _g):
        return {"own_recent": []}

    def _clock_offset_for(self, _bid):
        return None

    async def _persona(self, _umo, _bid):
        return "", ""

    def _moment_of(self, _bid, _now):
        return _DateTimeStub()

    def _verify_all_segments(self, _segments, **_kw):
        return "", ""

    async def _send_with_retry(self, _umo, text, _bid):
        self.sent.append(text)
        return True, "", False

    def _schedule_group_burst(self, *_args, **_kwargs):
        pass


class IcebreakNoQuotaTest(unittest.TestCase):
    """S2：模型答 NO 时这次不发，也不消耗今天的破冰/起话头额度。"""

    def _state(self):
        st = SocialState(
            os.path.join(tempfile.mkdtemp(), "s.json"), time_source=lambda: NOW
        )
        g = st.group("bot", "g1")
        g["umo"] = "aiocqhttp:GroupMessage:g1"
        g["msg_count"] = 10
        g["last_seen"] = NOW - 6 * 3600
        return st, g

    def _run(self, parts, body=None):
        st, g = self._state()
        engine = _IcebreakEngine(body or {}, parts, st)
        import asyncio

        out = asyncio.run(engine._icebreak("bot", "g1", g, NOW))
        return out, engine, g, st

    def test_no_does_not_send_or_count(self):
        out, engine, g, _st = self._run(None)
        self.assertFalse(out)
        self.assertEqual(engine.sent, [], "模型答 NO 还是发出去了")
        self.assertEqual(int(g.get("icebreak_count_day", 0) or 0), 0, "NO 也占了额度")
        self.assertTrue(
            any("不占今天的次数" in x for x in engine.logs), engine.logs
        )

    def test_send_counts_and_marks_the_weather(self):
        body = {"weather": {"env": "中雨，气温 9℃"}}
        out, engine, g, _st = self._run(["下午去银行回来路上买了花"], body)
        self.assertTrue(out)
        self.assertEqual(engine.sent, ["下午去银行回来路上买了花"])
        self.assertEqual(int(g.get("icebreak_count_day", 0) or 0), 1)
        self.assertEqual(
            g.get("weather_seen"), "中雨，气温 9℃", "发出去的天气没记账，下次还会说"
        )


if __name__ == "__main__":
    unittest.main()
