"""P2 批次：`social/style_profile.py` 的修复点回归测试。

每个用例的注释都写清「原来会怎样」。这个模块的产物**整段进 prompt**
（`generator.py:1680-1681` 把 `style.text` 挂进消息里），所以这里判错不是
内部记账问题，是直接把错的结论告诉模型。

按仓库惯例自带最小 astrbot 桩，两种摆法（tests/ 在插件里 / 在工作区根）都能跑。
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与 test_v1273_review_fixes.py 同款，幂等） ───
_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    import logging

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import style_profile  # noqa: E402
from social.style_profile import (  # noqa: E402
    _habit_line,
    _length_stats,
    _stat_line,
    _tone_line,
    build_style_profile,
)


def _text_of(conversation, own_recent=()):
    return build_style_profile(conversation, list(own_recent)).text


def _conv(texts, dir_="in"):
    return [{"dir": dir_, "text": t} for t in texts]


class QuestionTallyMatchesReasoningTest(unittest.TestCase):
    """#54：问句判据与 `reasoning._ASKING` 同口径（句尾「呢/吧/呗」不是提问）。

    原来是 `_QUESTION_TAIL = re.compile(r"[吗呢吧呗嘛？?]+\\s*$")`：把「呢/吧/呗」
    也算成句尾疑问语气词，于是「我忙着呢」「走吧」「好吧」被判成问句，
    `_stat_line` 产出「TA很爱问句，习惯把话头递出去」——这段字面文字整段进 prompt，
    模型于是把话头一直递回去，而对方其实只是在陈述。

    `reasoning.py:477` 已把那边修成 `[?？]|[吗]$`（只认问号与句尾「吗」），
    本用例钉住 style_profile 侧同样的口径。
    """

    def test_statement_particles_are_not_questions(self):
        for texts in (
            ["走吧", "我忙着呢", "好吧"],
            ["走吧", "我忙着呢", "好吧", "走呗"],
            ["开会呢", "买了吧", "吃饭了么"],
        ):
            line = _stat_line("TA", texts)
            self.assertTrue(line, f"样本够了却没有产出特征：{texts}")
            self.assertIn("基本不问句", line, f"陈述句被算成问句：{line}")

    def test_real_questions_still_count(self):
        # 「吗」是稳定的疑问语气词；「嘛」是口语里的「吗」，两个都留。
        for texts in (
            ["在吗", "吃了吗", "你几点回来？"],
            ["今天忙嘛", "有空吗？", "在不在?"],
        ):
            line = _stat_line("TA", texts)
            self.assertIn("爱问句", line, f"真问句没被认出来：{line}")

    def test_question_marks_anywhere_still_count(self):
        # `_ASKING` 对问号是**任意位置**匹配：句中的问号也要留下证据。
        # 注意 `_SENTENCE_SPLIT` 会把问号当边界吃掉，所以判据必须跑在整条消息上，
        # 否则「你几点回来？我等你」拆成两段后问号消失，整条一个问句都数不出来。
        line = _stat_line("TA", ["在吗？吃了吗？在不在", "嗯嗯", "好"])
        self.assertIn("问句不少", line, f"句中的问号没被认出来：{line}")
        self.assertNotIn("基本不问句", line, f"问号被漏掉了：{line}")
        # 只有一处提问时不能被放大（1/3 = 0.33）
        line = _stat_line("TA", ["你几点回来？我等你", "吃了吗，今天", "在不在啊"])
        self.assertIn("偶尔才问一句", line, f"单处问号被放大了：{line}")

    def test_mixed_corpus_lands_in_the_middle_band(self):
        # 2/6 是问句 = 0.33 → 落在 0.15 档，不吹成「很爱问句」
        line = _stat_line("TA", ["在吗", "吃了吗", "我忙着呢", "走吧", "好吧", "嗯嗯"])
        self.assertIn("偶尔才问一句", line, f"问句占比算错了：{line}")


class AddressTokenBoundaryTest(unittest.TestCase):
    """#59：称呼词表用子串匹配，「宝」命中「宝贵」/「宝宝」。

    于是任何说了「宝贵 / 珍贵 / 宝贝计划」的人都被统计成「开口习惯带称呼」，
    而那句结论会以文字形式写进 prompt，模型就真的每条都开始叫「宝」。
    按中文词边界处理：称呼必须落在句首，且后面不能紧跟汉字。
    """

    def test_precious_is_not_an_address(self):
        line = _habit_line("TA", ["时间很宝贵", "宝贵的经验", "这很珍贵"])
        self.assertNotIn("偶尔带称呼", line, f"「宝贵」被当成称呼：{line}")
        self.assertNotIn("开口习惯带称呼", line, f"「宝贵」被当成称呼：{line}")
        self.assertIn("说话不带称呼", line, f"没得出「不带称呼」的结论：{line}")

    def test_short_prefix_tokens_can_still_match(self):
        # 「宝贵」开头咬住的正是最短的 token「宝」，边界判据必须仍然认得真称呼。
        line = _habit_line("TA", ["宝，在吗", "宝儿想你", "宝宝睡了吗"])
        self.assertIn("开口习惯带称呼", line, f"真称呼没被认出来：{line}")

    def test_address_inside_a_sentence_is_not_an_opening_address(self):
        # 「亲爱的」在句子里出现，但不是开口（旧行为只看开头，这里保持一致）
        line = _habit_line("TA", ["喂那个亲爱的", "我跟你讲亲爱的", "在吗亲爱的"])
        self.assertNotIn("开口习惯带称呼", line, f"句中称呼被当成开口称呼：{line}")
        self.assertIn("说话不带称呼", line, f"错了档：{line}")


class SentenceSplitPeriodTest(unittest.TestCase):
    """#56：句长切分只认全角句号，半角 `.` 不算句尾 —— 与 `_PERIOD` 口径打架。

    `_PERIOD = re.compile(r"[。\\.]")` 把半角点算成「句号收尾」，`_SENTENCE_SPLIT`
    却不把它当句尾，于是同一份文本「用了句号收尾」但「平均 11 个字」：
    半角句号写法的人（手机键盘上很常见）句长被整条算成一句，最长字数是假的。
    """

    def test_half_width_period_is_a_sentence_boundary(self):
        parts = [p for p in style_profile._SENTENCE_SPLIT.split("你好.今天天气不错") if p]
        self.assertEqual(
            parts, ["你好", "今天天气不错"], f"半角句号没被当句尾：{parts}"
        )

    def test_corpus_with_half_width_periods_gives_shorter_sentences(self):
        # 半角写法与全角写法是同一批话，统计结果必须一致
        half = ["你好.今天天气不错", "吃饭.我们走吧", "睡觉.明天见"]
        full = ["你好。今天天气不错", "吃饭。我们走吧", "睡觉。明天见"]
        self.assertEqual(
            _length_stats(half), _length_stats(full),
            "半角句号与全角句号统计出的句长不一致",
        )
        self.assertEqual(_stat_line("TA", half), _stat_line("TA", full))

    def test_semicolon_variants_match(self):
        # 消息数要过 `MIN_SAMPLES`（=3）才出数
        half = ["你好啊;今天不错", "吃饭了;我们走吧", "睡觉吧;明天见"]
        full = ["你好啊；今天不错", "吃饭了；我们走吧", "睡觉吧；明天见"]
        self.assertEqual(_length_stats(half), _length_stats(full))
        self.assertEqual(_length_stats(half), (3, 4), f"半角分号没切开：{half}")

    def test_full_width_behaviour_is_unchanged(self):
        self.assertEqual(
            _length_stats(["你好。今天天气不错", "吃饭。我们走吧", "睡觉。明天见"]),
            (3, 6),
        )


class SeparatorTest(unittest.TestCase):
    """#57：三处拼接缺分隔符，产出会粘在一起。

    `_stat_line` 的两段是 `["TA一条平均 3 个字，最长 4 字", "TA基本不问句…"]` 用
    `；` 拼的（那处有分隔），但第一段里「最长 4 字」和 `_habit_line` 的
    `f"{label}" + "、".join(found)` 都在粘：「TA爱用感叹号」这种串读起来是两个 token。
    """

    def test_label_and_count_are_separated(self):
        line = _stat_line("TA", ["走吧", "我忙着呢", "好吧"])
        self.assertIn("最长 4 字", line, f"最长的字数没带单位：{line}")
        self.assertIn("一条平均 3 个字，最长 4 字", line, f"统计串粘在一起了：{line}")

    def test_habit_label_is_separated_from_the_list(self):
        line = _habit_line("TA", ["喂!", "诶!", "哎!", "嗯!", "哦!"])
        self.assertTrue(line.startswith("TA "), f"「TA」和习惯串粘在一起：{line!r}")
        self.assertIn("TA 爱用感叹号", line, f"没分隔符：{line!r}")

    def test_self_label_is_separated_too(self):
        # 主语换成「你」时同样要有分隔符，否则「你爱用感叹号」也读不出来
        line = _habit_line("你", ["喂!", "诶!", "哎!", "嗯!", "哦!"])
        self.assertTrue(line.startswith("你 "), f"「你」和习惯串粘在一起：{line!r}")

    def test_prompt_lines_do_not_glue(self):
        conv = _conv(["喂!", "诶!", "哎!", "嗯!", "哦!"])
        out = _text_of(conv)
        self.assertIn("- TA 爱用感叹号", out, f"进 prompt 的这行还是粘的：{out!r}")


class ReservedLengthFieldTest(unittest.TestCase):
    """#55：`StyleProfile.avg_chars / max_chars` 是死字段。

    全仓库 grep：`avg_chars` 只在定义处出现，`.max_chars` 除定义外没有任何读取点
    （`memory_bridge.py:212` 的 `max_chars` 是无关的截断参数）。于是
    `_length_stats` 每次 `build_style_profile` 都白算一遍。
    本用例钉住两件事：字段仍然存在且算得对（对外契约不动），以及它确实没有读取点。
    """

    _HALF_PERIOD = ["你好.今天天气不错", "吃饭.我们走吧", "睡觉.明天见"]
    # 半角句号切分后的六段（也是 #56 修好之后的口径）
    _PARTS = ["你好", "今天天气不错", "吃饭", "我们走吧", "睡觉", "明天见"]

    def test_length_fields_are_still_computed(self):
        profile = build_style_profile(_conv(self._HALF_PERIOD, dir_="out"))
        expected_avg, expected_max = _length_stats(self._PARTS)
        self.assertEqual((profile.avg_chars, profile.max_chars),
                         (expected_avg, expected_max))
        self.assertEqual(expected_max, 6, f"最长句长算错了：{expected_max}")
        self.assertGreater(profile.avg_chars, 0)

    def test_sample_shortage_still_yields_zero(self):
        profile = build_style_profile(_conv(["就一句"], dir_="out"))
        self.assertEqual((profile.avg_chars, profile.max_chars), (0, 0))

    def test_nothing_in_social_reads_the_length_fields(self):
        """字段被标成「保留字段」而不是接线：钉住「现在没有读取点」这个事实。

        原来的证据：`avg_chars` 全仓库只出现在定义处，`.max_chars` 的读取点只有
        `generator.py` 里的 `form.max_chars` / `cap`（Core 契约，与本字段无关）。
        这里不数「谁 grep 到了」——那会把注释里的 `style.max_chars` 也算进来——
        而是钉住「除了 style_profile 自己，没有模块 import 这个类」：
        哪天有人把 `style.avg_chars` 接进 `generator` 的长度约束（报告 #55 的建议），
        这条会失败，提醒同时删掉「保留字段」的注释。
        """
        social_dir = Path(style_profile.__file__).resolve().parent
        hits = []
        for path in sorted(social_dir.glob("*.py")):
            if path.name == "style_profile.py":
                continue
            for i, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "StyleProfile" in ln:
                    hits.append(f"{path.name}:{i}")
        self.assertEqual(hits, [], f"已有模块开始读 StyleProfile：{hits}")


class EmojiRegexIsSharedTest(unittest.TestCase):
    """#58（已去重）：emoji 字符类只有一份，在 `social/emoji.py`。

    原来 `style_profile` / `generator` / `groupflow` 各抄了一份**逐字节等价**的正则，
    只在注释里互相点名。三份里 `groupflow` 那份没人守——它漏改就会把「😀😀😀」
    重新当成有正文的群消息送进模型，而其余测试全绿。现在三处 import 同一个对象。
    """

    def test_三个模块共用同一个_pattern_对象(self):
        from social import emoji as emoji_module
        from social import generator, groupflow

        for module in (style_profile, generator, groupflow):
            with self.subTest(module=module.__name__):
                self.assertIs(
                    module._EMOJI,
                    emoji_module.EMOJI,
                    f"{module.__name__} 的 _EMOJI 不是 social/emoji.py 里那一个",
                )

    def test_没有模块再自己抄一份_emoji_字符类(self):
        """源码级：三处都不许再出现 `_EMOJI = re.compile(`。"""
        root = Path(style_profile.__file__).resolve().parent
        offenders = [
            name
            for name in ("style_profile.py", "generator.py", "groupflow.py")
            if "_EMOJI = re.compile(" in (root / name).read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [], f"这些模块又抄了一份 emoji 字符类：{offenders}")

    def test_bands_still_work(self):
        heavy = ["今天好开心呀 😂", "在吗 ✨", "晚安啦 🌙"]
        none = ["今天好开心呀", "在吗", "晚安啦"]
        self.assertEqual(style_profile.emoji_habit(heavy), style_profile.EMOJI_HEAVY)
        self.assertEqual(style_profile.emoji_habit(none), style_profile.EMOJI_NONE)


class ToneLineTest(unittest.TestCase):
    """顺带钉住 `_tone_line`（同族函数）：改 `_stat_line`/`_habit_line` 别伤到它。"""

    def test_tone_line_shapes(self):
        self.assertIn(
            "语气词很多",
            _tone_line("TA", ["喂", "诶", "哎", "嗯", "哦"]),
        )
        self.assertIn(
            "几乎不用语气词",
            _tone_line("你", ["今天上班", "晚上吃面", "还行吧", "好的", "明天见"]),
        )


class LegacyPipelineStillWorksTest(unittest.TestCase):
    """整条 `build_style_profile` 的产物形态没变（prompt 标题行 + 若干 `- ` 行）。"""

    def test_prompt_block_shape(self):
        conv = _conv(["在吗", "吃了吗", "你干嘛呢", "哦哦", "困了"])
        conv += _conv(["你好.今天天气不错", "吃饭.我们走吧", "睡觉.明天见"], dir_="out")
        out = _text_of(conv)
        self.assertTrue(out.startswith("【你们平时怎么说话的"), out)
        for line in out.splitlines()[1:]:
            self.assertTrue(line.startswith("- "), f"特征行格式变了：{line!r}")
        for raw in ("在吗", "吃了吗", "你干嘛呢", "困了", "你好"):
            self.assertNotIn(raw, out, f"风格特征里泄漏了原句：{raw}")

    def test_no_samples_no_text(self):
        self.assertEqual(_text_of(_conv(["就一句"])), "")


if __name__ == "__main__":
    unittest.main()
