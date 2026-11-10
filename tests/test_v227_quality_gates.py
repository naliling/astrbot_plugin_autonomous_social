"""v2.2.7：同一件事不说第二遍（回归用例）。

## 为什么

用户拿线上真实日志逐条点名，问题不是「不会说话」，是「只会说一种」：

* 群聊 27 条破冰里 15 条以「刚」开头；
* 「旧票据」在同一个群 15:22 说两遍、15:45 又说两遍（同群去重从来没生效过）；
* 17 秒内破冰 + 破冰 + 起话头 + 连发补充，四句全在说「我刚睡醒」；
* 27 条破冰 + 23 条起话头里，没有一条在回应群里正在聊的东西；
* 私聊六条里四条在纠正对方（「酥牧别刨根问底了」「酥牧别一惊一乍的」）；
* 动作描写只有两种，「(茉莉体香淡淡散开，纯白翅膀微微一拢)」重复三次；
* 「才睡0小时就醒了」——睡眠时长被 `int()` 截断成 0；
* 「我刚醒手还有点僵」——露娜是蹄子；
* 「(Model response will be rendered here)」被原样发出去。

用户明确反对用「限频 / 硬间隔」治这些（原话：加限制等于不拟人啊），所以这一版
只治「同一件事重复」和「把一句话拆成四条」，不动她说话的频率。

## 本文件盯的

* 睡眠时长分档，不再出现「睡 0 小时」；
* 占位符、物种错位、连续说教在**普通回复**出口被清掉（出口钩子 + 纯函数两层）；
* 开头轮换按统计拦，不靠禁词；
* 同一件事的判据（`_same_event`）能认出换个说法的复读，又不误伤两件不同的事；
* 群聊 prompt 里真的有群聊上下文，且要求「先接话」；
* 同群素材去重、同人由头去重真的会拦。
"""

from __future__ import annotations

import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v227_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import social.anchors as anchors_mod  # noqa: E402
import social.engine as engine_mod  # noqa: E402
import social.groupflow as groupflow  # noqa: E402
import social.verify as verify_mod  # noqa: E402
from social.generator import MessageGenerator  # noqa: E402
from social.state import SocialState  # noqa: E402

NOW = 1_800_000_000.0
HOOF = "半人马"
BID = "bot1"
GID = "group1"

GROUP_CTX = {
    "recent": [
        {"name": "阿澈", "text": "今天加班到十点，脑子已经木了", "self": False},
        {"name": "小满", "text": "我这儿下雨了，鞋全湿", "self": False},
        {"name": "露娜", "text": "（把翅膀收起来）本宫才不关心这些", "self": True},
    ],
    "latest": {"name": "小满", "text": "我这儿下雨了，鞋全湿"},
    "own_recent": ["（把翅膀收起来）本宫才不关心这些"],
    "topic_hint": "- 刚把桌上的旧票据收进抽屉\n- 外头阴天，毛潮",
    "style_ref": "阿澈：哈哈行\n小满：睡了睡了",
    "emoji": "emoji",
    "group_name": "测试群",
    "members": ["阿澈", "小满"],
    "addressed": None,
    "reason": "",
    "no_question": False,
    "openings": ["刚睡醒", "刚烧了壶水", "刚把窗户推开"],
}


def _state() -> SocialState:
    tmp = tempfile.mkdtemp(prefix="aso_v227_")
    return SocialState(str(Path(tmp) / "state.json"))


def _wake_body(hours: float) -> dict:
    return {"wake": {"asleep": False, "wake_at": NOW - 600.0, "last_sleep_hours": hours}}


# ─── 一、数据硬事实：睡 0 小时 ────────────────────────────────────


class WakeCueNoZeroHoursTest(unittest.TestCase):
    """线上原文「才睡0小时就醒了」：`int(0.4)` 截断成 0，说出人类不会说的话。"""

    def test_short_sleep_is_not_zero(self):
        for hours in (0.1, 0.4, 0.9):
            got = anchors_mod._wake_cue(_wake_body(hours), now=NOW)
            self.assertNotIn("0小时", got, f"{hours}h 说成了 {got!r}")

    def test_buckets(self):
        cases = {
            0.4: "没睡多久",
            1.2: "一个多小时",
            1.8: "不到2小时",
            5.0: "才睡5小时",
        }
        for hours, want in cases.items():
            got = anchors_mod._wake_cue(_wake_body(hours), now=NOW)
            self.assertIn(want, got, f"{hours}h → {got!r}")

    def test_long_sleep_still_says_long(self):
        got = anchors_mod._wake_cue(_wake_body(8.0), now=NOW)
        self.assertIn("睡了挺久", got)


# ─── 二、普通回复出口：占位符 / 物种 / 说教 ──────────────────────


class PlaceholderResidueTest(unittest.TestCase):
    """「(Model response will be rendered here)」被原样发出过一次。"""

    def test_embedded_placeholder_rejected(self):
        passed, why = verify_mod.verify_message(
            "今天好累 (Model response will be rendered here)"
        )
        self.assertFalse(passed, "嵌入正文的占位符被放行了")
        self.assertIn("占位符", why)

    def test_standalone_placeholder_rejected(self):
        passed, _why = verify_mod.verify_message("(Model response will be rendered here)")
        self.assertFalse(passed)

    def test_tidy_deletes_placeholder(self):
        out = verify_mod.tidy_outgoing_text(
            "今天好累 (Model response will be rendered here)", species=""
        )
        self.assertNotIn("rendered", out)
        self.assertIn("今天好累", out)

    def test_tidy_leaves_no_empty_bracket(self):
        # 删掉带括号的占位符后不许留下「今天好累 ()」这种空壳（v2.2.8 真机抽查发现）。
        out = verify_mod.tidy_outgoing_text(
            "今天好累 (Model response will be rendered here)", species=""
        )
        self.assertNotIn("(", out)
        self.assertNotIn(")", out)
        self.assertEqual(out, "今天好累")


class SpeciesConsistencyTest(unittest.TestCase):
    """露娜是蹄子，说「手还有点僵」就是穿帮。"""

    def test_hoof_rejects_hand(self):
        passed, why = verify_mod.verify_message(
            "我刚醒手还有点僵，快过来让我抱抱。", species=HOOF
        )
        self.assertFalse(passed, "蹄类角色写「手」被放行了")
        self.assertIn("身体不一致", why)

    def test_human_species_keeps_hand(self):
        passed, _why = verify_mod.verify_message("我刚醒手还有点僵。", species="人类")
        self.assertTrue(passed, "人类角色不该被这条误伤")

    def test_tidy_rewrites_hand_to_hoof(self):
        out = verify_mod.tidy_outgoing_text(
            "外面好冷，我刚醒手还有点僵，快过来让我抱抱。", species=HOOF
        )
        self.assertNotIn("手", out)
        self.assertIn("蹄子", out)


class PreachyTrimTest(unittest.TestCase):
    """娜莉灵六条里四条在纠正对方——不是禁「别」字，是连着纠正就不发。"""

    def test_repeated_preachy_sentence_dropped(self):
        recent = [
            "酥牧别刨根问底了。",
            "酥牧别再吵我了。",
        ]
        out = verify_mod.tidy_outgoing_text(
            "少自作多情了。我正忙着收桌上的旧票据呢。酥牧别胡思乱想。",
            species="",
            recent=recent,
        )
        self.assertNotIn("别胡思乱想", out)
        self.assertIn("旧票据", out, "该留的正文被一起删掉了")

    def test_single_preachy_allowed(self):
        out = verify_mod.tidy_outgoing_text("我正忙着呢。酥牧别胡思乱想。", species="")
        self.assertIn("别胡思乱想", out, "偶发一句逗弄不该被删")


# ─── 三、句式：开头轮换按统计，不靠禁词 ─────────────────────────


class OpeningRotationTest(unittest.TestCase):
    """27 条破冰 15 条以「刚」开头：提示词压不住，只能按统计换。"""

    def test_opening_head_strips_actions_and_punctuation(self):
        self.assertEqual(verify_mod.opening_head("（把翅膀收起来）刚睡醒"), "刚")
        self.assertEqual(verify_mod.opening_head("嗯…外头好冷"), "嗯")

    def test_repeat_issue_blocks_fourth(self):
        openings = ["刚睡醒", "刚烧了壶水", "刚把窗户推开"]
        issue = verify_mod._opening_repeat_issue("刚把茶喝完", openings)
        self.assertTrue(issue, "最近 6 条里「刚」已 3 次，这条还该拦")

    def test_varied_opening_passes(self):
        openings = ["刚睡醒", "刚烧了壶水", "刚把窗户推开"]
        self.assertFalse(verify_mod._opening_repeat_issue("窗台上落了层灰", openings))

    def test_group_prompt_tells_her_to_switch_opening(self):
        prompt, _ = MessageGenerator(None, None)._compose_group(
            "icebreak", dict(GROUP_CTX), "你叫露娜。", 120
        )
        self.assertIn("不要用", prompt)
        self.assertIn("开头", prompt)


# ─── 四、同一件事：判据 + 连发合并 + 素材闸 ─────────────────────


class SameEventTest(unittest.TestCase):
    """「旧票据」说四遍、「外头才5度」85 秒说两遍，都该被认成同一件事。"""

    def test_same_event_detected_across_wordings(self):
        pairs = [
            ("刚用蹄子夹着杯子泡茶，洒了半桌", "刚泡上茶，正捧着杯子在沙发上慢慢喝"),
            ("（趴在床上把翅膀摊开）才睡0小时就醒了", "刚醒，睡0小时，趴床上赖着"),
            ("刚把桌上的旧票据收进抽屉", "桌上的旧票据还没收拾完呢"),
            ("外头才5度，翅膀摊在冰凉的地板上硬邦邦的", "外头才5度，像块硬邦邦的石头一样躺着"),
        ]
        for a, b in pairs:
            self.assertTrue(engine_mod._same_event(a, b), f"没认出同一件事：{a!r} / {b!r}")

    def test_different_events_not_merged(self):
        pairs = [
            ("刚把茶喝完", "刚趴窗边看外头"),
            ("刚睡醒", "刚把窗户推开"),
            ("刚用蹄子夹抹布擦窗台", "刚叼着布袋去街角买回一条黑麦面包"),
        ]
        for a, b in pairs:
            self.assertFalse(engine_mod._same_event(a, b), f"两件事被误并：{a!r} / {b!r}")

    def test_cap_segments_collapses_same_event(self):
        segs = [
            "刚用蹄子夹着杯子泡茶，洒了半桌",
            "刚泡上茶，正捧着杯子在沙发上慢慢喝",
            "泡茶的时候手一抖，洒了半桌",
        ]
        merged, _waits = engine_mod._cap_group_segments(segs, [3.0, 3.0], 3, 0)
        self.assertEqual(len(merged), 1, f"同一件事没收敛：{merged}")

    def test_proactive_issue_blocks_repeated_material(self):
        issue = engine_mod._group_proactive_issue(
            ["刚把桌上的旧票据收进抽屉"],
            used_lines=["桌上的旧票据还没收拾完呢"],
        )
        self.assertTrue(issue, "本群刚讲过的素材又被放行")
        self.assertIn("讲过", issue)

    def test_proactive_issue_passes_fresh_material(self):
        issue = engine_mod._group_proactive_issue(
            ["窗台上落了层灰，擦了半天"],
            used_lines=["桌上的旧票据还没收拾完呢"],
        )
        self.assertEqual(issue, "")


# ─── 五、群聊要先看群里在聊什么 ─────────────────────────────────


class GroupPromptHasContextTest(unittest.TestCase):
    """「27 条破冰 + 23 条起话头，没有一条在回应群聊」的直接原因是 prompt 里没有群聊。"""

    def test_icebreak_prompt_contains_peer_words(self):
        prompt, _ = MessageGenerator(None, None)._compose_group(
            "icebreak", dict(GROUP_CTX), "你叫露娜。", 120
        )
        self.assertIn("加班到十点", prompt, "破冰 prompt 里没有群友原话")
        self.assertIn("群里最近在聊", prompt)

    def test_icebreak_prompt_asks_to_answer_first(self):
        prompt, _ = MessageGenerator(None, None)._compose_group(
            "icebreak", dict(GROUP_CTX), "你叫露娜。", 120
        )
        self.assertTrue(("先接" in prompt) or ("接住" in prompt), "没有「先接话」的要求")

    def test_topic_prompt_contains_peer_words(self):
        prompt, _ = MessageGenerator(None, None)._compose_group(
            "topic", dict(GROUP_CTX), "你叫露娜。", 120
        )
        self.assertIn("加班到十点", prompt)

    def test_no_longer_pushes_just_finished(self):
        prompt, _ = MessageGenerator(None, None)._compose_group(
            "icebreak", dict(GROUP_CTX), "你叫露娜。", 120
        )
        self.assertNotIn("优先说她刚做完", prompt)


# ─── 六、素材与由头去重真的会拦 ─────────────────────────────────


class DedupBooksTest(unittest.TestCase):
    """`life_used_elsewhere` 旧判据是「别的群用过才算」，同群永远放行。"""

    def test_same_group_counts_as_used(self):
        st = _state()
        st.note_life_used(BID, "旧票据还没收拾", GID, NOW)
        self.assertFalse(st.life_used_elsewhere(BID, "旧票据还没收拾", GID, NOW + 60))
        self.assertTrue(
            st.life_used_elsewhere(BID, "旧票据还没收拾", GID, NOW + 60, same_group=True)
        )

    def test_other_group_still_counts(self):
        st = _state()
        st.note_life_used(BID, "旧票据还没收拾", GID, NOW)
        self.assertTrue(st.life_used_elsewhere(BID, "旧票据还没收拾", "group2", NOW + 60))

    def test_lines_used_in_group_listed(self):
        st = _state()
        st.note_life_used(BID, "旧票据还没收拾", GID, NOW)
        used = st.life_lines_used_in_group(BID, GID, NOW + 60)
        self.assertEqual(len(used), 1)

    def test_anchor_used_for_same_person(self):
        st = _state()
        bot = st.bot(BID)
        bot.setdefault("users", {})["u1"] = {
            "proactive_log": [{"about": "外头才5度", "ts": NOW - 30.0, "why": "sent"}]
        }
        self.assertTrue(st.anchor_used_for(BID, "u1", "外头才5度", NOW, hours=12.0))
        self.assertFalse(st.anchor_used_for(BID, "u1", "外头才5度", NOW, hours=0.0001))


class LocalMaterialsTest(unittest.TestCase):
    """兜底素材硬编码了「刚醒，眼睛还没完全睁开」「手还是湿的」。"""

    def test_no_gang_opening(self):
        lines = groupflow.local_materials(NOW, count=4)
        for line in lines:
            self.assertFalse(line.startswith("刚"), f"素材还在硬推「刚」：{line!r}")

    def test_hoof_has_no_hand(self):
        lines = groupflow.local_materials(NOW, count=4, species=HOOF)
        for line in lines:
            self.assertNotIn("手", line, f"蹄类素材里还有「手」：{line!r}")


class EchoAlsoTest(unittest.TestCase):
    """「也」是回声词：没有由头就不许用（v2.2.8）。"""

    def test_echo_without_context_is_flagged(self):
        reason = verify_mod._check_echo_also("我也爱你呀。", ())
        self.assertTrue(reason, "没有由头的「我也爱你」应该被拦")

    def test_echo_with_context_passes(self):
        reason = verify_mod._check_echo_also("我也爱你呀。", ("我爱你",))
        self.assertIsNone(reason, "对方说了「我爱你」，回「我也爱你」是正常的")

    def test_plain_also_is_not_echo(self):
        # 「我也想去」是「同样」，不是回声——不能被当成问题。
        self.assertIsNone(verify_mod._check_echo_also("我也想去看看。", ()))

    def test_tidy_drops_also_without_context(self):
        got = verify_mod.tidy_outgoing_text("嗯 我也爱你 亲爱的。", context=())
        self.assertNotIn("我也爱", got, f"没有由头还留着「也」：{got!r}")
        self.assertIn("我爱你", got)

    def test_tidy_keeps_also_with_context(self):
        got = verify_mod.tidy_outgoing_text("我也爱你呀。", context=("我爱你",))
        self.assertIn("我也爱你", got, f"有由头却把「也」删了：{got!r}")

    def test_verify_rejects_echo_without_context(self):
        ok, reason = verify_mod.verify_message("我也爱你呀，今天想你了。")
        self.assertFalse(ok, "主动消息没有上下文，不该出现「我也爱你」")
        self.assertIn("也", reason)

    def test_proactive_prompt_forbids_echo(self):
        # `_compose` 是 async 且要 provider，这里用源码断言（与其他结构性验收一致）：
        # 主动消息提示词里必须有一条「别用『也』接」。
        src = (PLUGIN_ROOT / "social" / "generator.py").read_text(encoding="utf-8")
        self.assertIn("别用「也」接", src, "主动消息提示词没有禁止「也」")


class FragmentMergeTest(unittest.TestCase):
    """没说完就被拆出来的碎片段要并回相邻段（v2.2.10）。

    真机样本（22:18:52–22:18:57）四条连发：
      「窗边伸了个懒腰」「眼睛闭了会儿」「骨头都松了。你刚才那句来抱抱」「还暖着呢。…」
    前两条是同一句话被切成两半。
    """

    def test_short_fragment_merged_into_next(self):
        segs, waits = engine_mod._merge_fragments(
            ["窗边伸了个懒腰", "眼睛闭了会儿。你刚才那句来抱抱"], [1.2]
        )
        self.assertEqual(len(segs), 1, f"碎片没并进去：{segs!r}")
        self.assertIn("窗边伸了个懒腰", segs[0])
        self.assertIn("眼睛闭了会儿", segs[0])
        self.assertEqual(waits, [], "并成一段后不该还留着段间隔")

    def test_complete_sentence_not_merged(self):
        segs, _ = engine_mod._merge_fragments(
            ["我下午在阳台晒了会儿太阳。", "你要不要也来"], [1.0]
        )
        self.assertEqual(len(segs), 2, f"完整句子不该被并：{segs!r}")

    def test_pure_action_segment_kept(self):
        # 纯括号动作段是刻意的动作描写，本来就能独立发，不算碎片。
        segs, _ = engine_mod._merge_fragments(
            ["（尾巴甩了甩）", "我才没有想你。"], [1.0]
        )
        self.assertEqual(len(segs), 2, f"纯动作段被误并：{segs!r}")

    def test_last_fragment_merged_into_previous(self):
        segs, _ = engine_mod._merge_fragments(
            ["骨头都松了。", "还暖着呢"], [1.0]
        )
        self.assertEqual(len(segs), 1, f"末段碎片没并进上一段：{segs!r}")
        self.assertIn("还暖着呢", segs[0])


class PreachyClauseTrimTest(unittest.TestCase):
    """说教只剪小句，不整句删、更不能把整条删空（v2.2.10）。

    线上 22:39:53 的清理日志：「主链路回复清理后为空，这条不发：
    '（嘟囔着拱了拱）没睡透，别闹。上次冷落我可还记着仇呢，不给我补偿可别想我理你。'」
    ——她那一轮一个字没说，比说教更糟。
    """

    _RECENT = ["酥牧别刨根问底了。", "酥牧别再吵我了。"]

    def test_clause_trimmed_sentence_kept(self):
        got = verify_mod.tidy_outgoing_text(
            "（嘟囔着拱了拱）没睡透，别闹。", recent=self._RECENT
        )
        self.assertNotIn("别闹", got, f"祈使小句没剪掉：{got!r}")
        self.assertIn("没睡透", got, f"把正经内容一起删了：{got!r}")
        self.assertTrue(got.strip(), "整条被删空了")

    def test_whole_message_never_emptied(self):
        got = verify_mod.tidy_outgoing_text(
            "少自作多情了。酥牧别胡思乱想。", recent=self._RECENT
        )
        self.assertTrue(
            got.strip(), "整条全被删空——宁可带点脾气也不能让她这轮不吭声"
        )

    def test_name_only_residue_dropped(self):
        got = verify_mod.tidy_outgoing_text(
            "酥牧别胡思乱想。我正忙着收桌上的旧票据呢。", recent=self._RECENT
        )
        self.assertNotIn("别胡思乱想", got, f"祈使小句没剪掉：{got!r}")
        self.assertIn("旧票据", got, f"同一条里别的内容被误删：{got!r}")


class ChannelSwitchTest(unittest.TestCase):
    """v2.2.10：私聊 / 群聊总开关（`allow_private_chat` / `allow_group_chat`）。

    用户要的是「只允许私聊 / 只允许群聊 / 全开」三种组合，两个默认值都为开，
    两个开关放在同一处（配置面板相邻两项）。原来群侧没有任何总开关，私聊也没有；
    这次两边补齐，且**每个发送入口都要真的早退**——只加配置项不加闸，面板上关掉
    也照样发，等于假开关。
    """

    def _engine(self, **cfg):
        eng = object.__new__(engine_mod.SocialEngine)
        eng.cfg = types.SimpleNamespace(**cfg)
        return eng

    def test_defaults_are_on(self):
        eng = self._engine(allow_private_chat=True, allow_group_chat=True)
        self.assertTrue(eng._private_allowed())
        self.assertTrue(eng._group_allowed())

    def test_missing_keys_default_to_on(self):
        eng = self._engine()
        self.assertTrue(eng._private_allowed(), "旧配置没这个键时必须按开处理")
        self.assertTrue(eng._group_allowed(), "旧配置没这个键时必须按开处理")

    def test_private_off_does_not_touch_group(self):
        eng = self._engine(allow_private_chat=False, allow_group_chat=True)
        self.assertFalse(eng._private_allowed())
        self.assertTrue(eng._group_allowed(), "关私聊不该顺手把群聊也关掉")

    def test_group_off_does_not_touch_private(self):
        eng = self._engine(allow_private_chat=True, allow_group_chat=False)
        self.assertFalse(eng._group_allowed())
        self.assertTrue(eng._private_allowed(), "关群聊不该顺手把私聊也关掉")

    def test_schema_has_both_switches_default_true(self):
        import json

        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in ("allow_private_chat", "allow_group_chat"):
            self.assertIn(key, schema, f"schema 缺 {key}")
            self.assertIs(schema[key]["default"], True, key)
            self.assertEqual(schema[key]["type"], "bool", key)

    def test_every_group_entry_gated(self):
        import inspect

        for name in (
            "_flow_reply",
            "_run_icebreaks",
            "_run_group_topics",
            "_icebreak",
            "_send_group_burst_followup",
        ):
            src = inspect.getsource(getattr(engine_mod.SocialEngine, name))
            self.assertIn("_group_gate(self)", src, f"{name} 没有查群聊总开关")

    def test_every_private_entry_gated(self):
        import inspect

        for name in ("_speak", "_send_burst_followup"):
            src = inspect.getsource(getattr(engine_mod.SocialEngine, name))
            self.assertIn("_private_gate(self)", src, f"{name} 没有查私聊总开关")


if __name__ == "__main__":
    unittest.main()
