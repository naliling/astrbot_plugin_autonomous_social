"""v1.29.2：进提示词的引文/由头片段不许带未闭合括号。

## 背景（线上实测）

主动消息的提示词会引用户/她说过的话：

    TA最后说的是：「{about}」。
    你上次说的「{seg}」后来呢
    你上次说{clause}，后来好点没
    · 你们说好的：{cue}

这些片段都是**截断**出来的（_peer_text 截 80、_short 截 12、
extract_cue 取 12 字上下文），截断落在一个成对括号中间，就只剩单边。
线上 state.json / prompt 里抓到的真实样本：

    你上次说的「(慢慢躺在你的身边抱着你」后来呢
    你上次说那不奇怪了……(温柔地揉，后来好点没
    你上次说哈哈(完成一个后，后来好点没
    · 你们说好的：时钟发现很晚了)嘛…不过明天照样美好…

模型把这种残句当成「对方就是这么说的」，照着写就是断句/怪句。

## 本文件钉住

1. textutil.balance_brackets：只删单边，成对（含语C 动作括号）原样保留；
2. reasoning.extract_cue：先去掉成对动作括号段再取上下文，返回的 cue 括号成对；
3. anchors.derive_relations / derive_topic_relations：两条引文模板整句成对；
4. generator._peer_text / _mode_note：about/asked 渲染出的引文成对；
5. generator._material_block：cue 直塞提示词的那一行成对；
6. generator 里另外几处把 about/exchange 拼进提示词的地方（anchor_fact /
   msg_type_desc）也成对。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path

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
    api.logger = logging.getLogger("autonomous_social_v1292_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import anchors, generator, reasoning  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.textutil import balance_brackets  # noqa: E402

NOW = 1_700_000_000.0
# 线上那个被截断的片段：原文 (慢慢躺在你的身边抱着你)，截到 12 字只剩左括号。
HALF_PAREN = "(慢慢躺在你的身边抱着你"


def _balanced(text: str) -> bool:
    """整串的括号/引号是否都成对。

    balance_brackets 对成对的串是幂等的（原样返回），所以「洗一遍没变化」
    就是「没有孤立括号」。
    """
    return balance_brackets(text) == text


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class BalanceBracketsTest(unittest.TestCase):
    """balance_brackets 本身：只删单边，内容与成对括号一个不动。"""

    def test_unpaired_chars_are_dropped_and_content_is_kept(self):
        cases = (
            # 只有左半边：截断留下的那种
            ("（慢慢躺在你的身边抱着你", "慢慢躺在你的身边抱着你"),
            (HALF_PAREN, "慢慢躺在你的身边抱着你"),
            ("「明天下午去医院做复查", "明天下午去医院做复查"),
            ("『book", "book"),
            ("【她的身体与生活 v10", "她的身体与生活 v10"),
            # 只有右半边：取上下文时从句子中间开始的那种（线上 cue 的原样）
            ("慢慢躺在你的身边抱着你）", "慢慢躺在你的身边抱着你"),
            ("慢慢躺在你的身边抱着你)", "慢慢躺在你的身边抱着你"),
            ("时钟发现很晚了)嘛…不过明天照样美好…", "时钟发现很晚了嘛…不过明天照样美好…"),
            ("今天真累」", "今天真累"),
            # 对称双引号：落单就删，成对保留
            ('他说"你好', "他说你好"),
            ("“你好", "你好"),
        )
        for raw, expect in cases:
            with self.subTest(raw=raw):
                self.assertEqual(balance_brackets(raw), expect)

    def test_paired_brackets_are_untouched(self):
        # 用户/角色用括号写动作是正常风格：成对的动作括号**不许**删。
        for text in (
            "（轻轻抱你）今天风大",
            "（他说的「好」）",
            "（（x））",
            "（a「b」c）",
            "（(x)）",
            "「明天面试」后来呢",
            "【她】『book』[x]{y}",
            "“你好”",
            '"你好"',
            "(慢慢躺在你的身边抱着你)",
        ):
            with self.subTest(text=text):
                self.assertEqual(balance_brackets(text), text)

    def test_nested_pairs_survive(self):
        self.assertEqual(balance_brackets("（（x）"), "（x）")
        self.assertEqual(balance_brackets("（他说「好」）"), "（他说「好」）")
        self.assertEqual(balance_brackets("「（x）」"), "「（x）」")

    def test_empty_values(self):
        self.assertEqual(balance_brackets(""), "")
        self.assertEqual(balance_brackets(None), "")
        # 非 str 先 str()，但 None 不该变成提示词里的 "None"
        self.assertEqual(balance_brackets(123), "123")


class ExtractCueSanitizeTest(unittest.TestCase):
    """时间锚点 cue 进提示词的片段：先去掉成对动作括号，取完再配一次对。"""

    def _cue(self, text: str) -> str:
        cue, due = reasoning.extract_cue(text, NOW)
        self.assertTrue(cue, f"没提到 cue：{text!r}")
        self.assertGreater(due, NOW)
        return cue

    def test_online_cue_does_not_keep_the_half_bracket(self):
        # 线上真实 cue 的形状：原文 (看时钟发现很晚了)嘛…不过明天照样美好… 被从
        # 「时」字开始取上下文，左括号没了、右括号留下来。
        cue = self._cue("(看时钟发现很晚了)嘛…不过明天照样美好…")
        self.assertFalse(cue.startswith(")"), cue)
        self.assertNotIn(")", cue, cue)
        self.assertNotIn("）", cue, cue)
        self.assertTrue(_balanced(cue), cue)

    def test_context_never_starts_inside_an_action_bracket(self):
        cue = self._cue("(看时钟发现很晚了)嘛…明天十点面试")
        self.assertEqual(cue, "嘛…明天十点面试")
        self.assertFalse(cue.startswith((")", "）")), cue)
        self.assertTrue(_balanced(cue), cue)

    def test_lone_bracket_in_the_original_is_dropped_after_slicing(self):
        cue = self._cue("时钟发现很晚了)嘛…不过明天照样美好…")
        self.assertNotIn(")", cue, cue)
        self.assertTrue(_balanced(cue), cue)

    def test_ordinary_message_is_unchanged(self):
        self.assertEqual(self._cue("明天十点面试"), "明天十点面试")


class DeriveRelationsQuoteTest(unittest.TestCase):
    """关系向由头的两条引文模板：「你上次说的「…」后来呢」/「你上次说…，后来好点没」。"""

    @staticmethod
    def _topic_lines(out):
        return [t for t in out if str(t).startswith("你上次说的")]

    def test_truncated_action_paren_in_quote_is_dropped(self):
        # 原文是成对的语C 动作，被 _short(…, 12) 切掉右括号 —— 线上真实样本：
        # 「你上次说的「(慢慢躺在你的身边抱着你」后来呢」
        u = {"conversation": [{"text": "(慢慢躺在你的身边抱着你)", "dir": "in"}]}
        lines = self._topic_lines(anchors.derive_relations(u, 85.0, NOW, set()))
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertTrue(line.endswith("」后来呢"), line)
        self.assertEqual(line.count("「"), 1, line)
        self.assertEqual(line.count("」"), 1, line)
        self.assertTrue(_balanced(line), f"引文里还有孤立括号：{line!r}")
        self.assertEqual(line, "你上次说的「慢慢躺在你的身边抱着你」后来呢")

    def test_unpaired_paren_from_user_message_is_dropped(self):
        u = {"conversation": [{"text": HALF_PAREN, "dir": "in"}]}
        lines = self._topic_lines(anchors.derive_relations(u, 85.0, NOW, set()))
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0], "你上次说的「慢慢躺在你的身边抱着你」后来呢")
        self.assertTrue(_balanced(lines[0]), lines[0])

    def test_care_quote_is_balanced(self):
        # 牵挂那条模板：「你上次说{clause}，后来好点没」，clause 是 _clause 截出来的。
        u = {
            "conversation": [{"text": "那不奇怪了……(温柔地揉着你的头)，好累", "dir": "in"}],
            "last_seen": NOW - 3600.0,
        }
        out = anchors.derive_relations(u, 85.0, NOW, set())
        care = [t for t in out if str(t).endswith("，后来好点没")]
        self.assertEqual(len(care), 1, out)
        self.assertTrue(_balanced(care[0]), f"牵挂由头里有孤立括号：{care[0]!r}")
        self.assertNotIn("(", care[0], care[0])

    def test_topic_quote_template_is_balanced(self):
        topic = "(看塞尔达攻略"
        u = {
            "last_seen": NOW - 3 * 3600.0,
            "topics": [topic],
            "topic_seen_at": {topic: NOW - 3 * 3600.0},
        }
        out = anchors.derive_topic_relations(u, NOW, set())
        self.assertEqual(len(out), 1, out)
        self.assertEqual(out[0], "你上次说的「看塞尔达攻略」后来呢")
        self.assertTrue(_balanced(out[0]), out[0])


class PeerTextSanitizeTest(unittest.TestCase):
    """「对方说的话」这道清洗：截断之后必须再配一次对。"""

    def test_truncated_action_paren_is_dropped(self):
        self.assertEqual(
            generator._peer_text(HALF_PAREN, 60),
            "慢慢躺在你的身边抱着你",
        )

    def test_limit_truncation_does_not_leave_a_half_bracket(self):
        out = generator._peer_text("(慢慢躺在你的身边抱着你)晚安", 12)
        self.assertEqual(out, "慢慢躺在你的身边抱着你")
        self.assertTrue(_balanced(out), out)

    def test_paired_action_is_kept(self):
        self.assertEqual(
            generator._peer_text("（轻轻抱你）今天风大", 60),
            "（轻轻抱你）今天风大",
        )


class ModeNoteSanitizeTest(unittest.TestCase):
    """_mode_note 渲染出的引文句（about/asked 会被「」捧进提示词）。"""

    def test_probe_about_has_no_isolated_bracket(self):
        note = generator._mode_note("probe", about=HALF_PAREN)
        self.assertIn("慢慢躺在你的身边抱着你", note)
        self.assertTrue(_balanced(note), f"提示词里还有孤立括号：{note!r}")

    def test_presence_asked_has_no_isolated_bracket(self):
        note = generator._mode_note("presence", asked=HALF_PAREN)
        self.assertIn("慢慢躺在你的身边抱着你", note)
        self.assertTrue(_balanced(note), f"提示词里还有孤立括号：{note!r}")


class MaterialBlockCueTest(unittest.TestCase):
    """「你们说好的：{cue}」这一行：cue 直塞提示词，同样要成对。"""

    def test_cue_fragment_in_material_block_is_balanced(self):
        gen = object.__new__(generator.MessageGenerator)
        block = gen._material_block(
            {"cue": "时钟发现很晚了)嘛…不过明天照样美好…"}, {}, NOW, NOW)
        self.assertIn("你们说好的", block)
        self.assertNotIn("很晚了)嘛", block, block)
        self.assertIn("时钟发现很晚了嘛…不过明天照样美好…", block)
        self.assertTrue(_balanced(block), f"素材块里还有孤立括号：{block!r}")


class ComposeAboutSanitizeTest(unittest.TestCase):
    """generator 里另外几处把 about 拼进提示词的地方（自查发现）。"""

    class _P:
        id = "p1"

        async def text_chat(self, **kw):
            return None

    class _Ctx:
        async def get_using_provider_async(self, umo=None):
            return ComposeAboutSanitizeTest._P()

    def _prompt(self, meta, target=None):
        gen = generator.MessageGenerator(self._Ctx(), SocialConfig())
        out = _run(gen._compose(
            "umo", {"conversation": []} if target is None else target, "ctx", "", meta,
            "人设", None, decide=True, at=NOW,
        ))
        self.assertIsNotNone(out, "provider 不可用时跳过是正常的")
        return out[1]

    def test_anchor_fact_and_msg_type_desc_are_balanced(self):
        text = self._prompt({
            "category": "anchor",
            "mode": "share",
            "msg_type_desc": "就那件事说句话：" + HALF_PAREN,
            "anchor_fact": "刚忙完：" + HALF_PAREN,
        })
        # 内容一个字不少，只少了那个被截断切出来的左括号
        self.assertIn("就那件事说句话：慢慢躺在你的身边抱着你", text)
        self.assertIn("刚忙完：慢慢躺在你的身边抱着你", text)
        self.assertNotIn(HALF_PAREN, text)
        self.assertTrue(_balanced(text), "提示词里还有孤立括号")

    def test_material_quotes_in_the_full_prompt_are_balanced(self):
        # 一份线上真出现过的 target：旧 state 里存着被截断的 cue / last_message。
        text = self._prompt(
            {"category": "anchor", "mode": "share", "msg_type_desc": "说点什么",
             "anchor_fact": "刚忙完：" + HALF_PAREN},
            target={
                "conversation": [],
                "last_message": HALF_PAREN,
                "cue": "时钟发现很晚了)嘛…不过明天照样美好…",
            },
        )
        self.assertIn("慢慢躺在你的身边抱着你", text)
        self.assertNotIn("很晚了)嘛", text, "素材块里还留着被切断的右括号")
        self.assertNotIn(HALF_PAREN + "」", text, "引文里还留着被切断的左括号")
        self.assertTrue(_balanced(text), "提示词里还有孤立括号")


class ClauseSanitizeTest(unittest.TestCase):
    """_clause 的返回值会被拼进牵挂/天气模板，截断后同样要配一次对。"""

    def test_truncated_clause_drops_the_half_bracket(self):
        out = anchors._clause("那不奇怪了……(温柔地揉着你的头)，后来", 12)
        self.assertTrue(_balanced(out), out)
        self.assertNotIn("(", out, out)
        self.assertEqual(out, "那不奇怪了……温柔地揉")

    def test_full_clause_is_untouched(self):
        self.assertEqual(anchors._clause("今天加班到现在，好累", 12), "今天加班到现在，好累")


if __name__ == "__main__":
    unittest.main()
