"""P2 遗留批次：`social/groupflow.py` 与 `social/pacing.py` 的四个修复点。

覆盖：
- #41 `flow_prefilter` 的预筛漏了「纯 emoji / 纯 [图片] 占位符」；
- #42 `icebreak_due` / `group_topic_due` 缺 `asleep` 闸门（心流有、这两处没有）；
- #43 `group_topic_due` 读 `icebreak_at` 做单向守卫、对称方向靠 `last_bot_spoke` 兜；
- #47 `split_segments` 的 `words_count_threshold` 用字符数判、与同文件 `_word_count` 两套口径；
- #51 `pacing` 文档声称「逐字对齐 AstrBot 源码」——本环境无 AstrBot 源码可核。

每条用例的注释都写清「原来会怎样」，并尽量内联复刻修复前的旧逻辑，证明用例不是空转。
按仓库惯例自带最小 astrbot 桩。
"""

from __future__ import annotations

import logging
import re
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


class _FakeMessageChain:
    def message(self, text):
        return ("chain", text)


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _FakeMessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import anchors, groupflow, pacing, textutil  # noqa: E402

NOW = 1_700_000_000.0

# 修复前 `_STICKER_ONLY` 的正则（只有中英标点与空白）。内联在这里是为了让
# 「旧口径判 A、新口径判 B」这类断言可以就地对照，不用去翻 git 历史。
_OLD_STICKER_ONLY = re.compile(r"^[\s\[\]（）()【】.。~!！?？…、,，]*$")
_OLD_AT_PREFIX = re.compile(r"^\s*@\S+\s*$")


def _old_prefilter(text: str, is_command: bool) -> bool:
    """复刻修复前的 `flow_prefilter`：emoji 与 [图片] 占位符都会漏过去。"""
    if is_command:
        return False
    body = str(text or "").strip()
    if len(body) < 3:
        return False
    if body.startswith("/"):
        return False
    if _OLD_STICKER_ONLY.match(body) or _OLD_AT_PREFIX.match(body):
        return False
    if body in groupflow._LOW_INFO:
        return False
    return True


class PrefilterStickerOnlyTest(unittest.TestCase):
    """#41：docstring 说「纯 @、纯表情/图片」先筛掉，实现却只挡标点。"""

    def test_pure_emoji_is_filtered(self):
        """原来会怎样：`😀😀😀` 三个码位 > 3 字符下限，正则一个都没匹配到 → 放行进 LLM。"""
        self.assertTrue(_old_prefilter("😀😀😀", False), "旧逻辑本就该漏，先证明用例非空转")
        self.assertFalse(groupflow.flow_prefilter("😀😀😀", False))

    def test_pure_emoji_frame_is_filtered(self):
        """单个表情加变体选择符（长度够 3）同样应该被挡掉。"""
        self.assertFalse(groupflow.flow_prefilter("❤️❤️", False))

    def test_picture_placeholder_is_filtered(self):
        """原来会怎样：`[图片]` 里是中文，旧正则的 `[]` 挡不住 → 放行进 LLM。"""
        self.assertTrue(_old_prefilter("[图片]", False), "旧逻辑本就该漏，先证明用例非空转")
        self.assertFalse(groupflow.flow_prefilter("[图片]", False))

    def test_common_qq_placeholders_are_filtered(self):
        """QQ 的一整套系统占位符都是「接不出话」的消息。"""
        for text in ("[表情]", "[动画表情]", "[语音]", "[视频]", "[文件]",
                     "[位置]", "[链接]", "[音乐]", "[红包]", "[转账]", "[合并转发]"):
            with self.subTest(text=text):
                self.assertFalse(groupflow.flow_prefilter(text, False), text)

    def test_placeholder_mix_and_fullwidth_brackets(self):
        """占位符加标点、全角括号、emoji 混排，剥完都不剩正文。"""
        for text in ("【图片】", "[图片][表情]", "[图片] [表情] 😀", "😀 [视频]"):
            with self.subTest(text=text):
                self.assertFalse(groupflow.flow_prefilter(text, False), text)

    def test_emoji_with_real_text_still_passes(self):
        """反向：表情只是语气，带正文的照样要进心流——别把整类消息误杀。"""
        for text in ("今天好累呀😮‍💨", "😀 你说得对", "在开会[图片]"):
            with self.subTest(text=text):
                self.assertTrue(groupflow.flow_prefilter(text, False), text)

    def test_normal_text_still_passes(self):
        """反向：普通中文/英文消息不受影响。"""
        for text in ("明天面试加油", "hello world", "@小明"):
            with self.subTest(text=text):
                self.assertEqual(
                    groupflow.flow_prefilter(text, False), _old_prefilter(text, False), text
                )
        self.assertFalse(groupflow.flow_prefilter("@小明", False), "@ 前缀仍然挡")
        self.assertTrue(groupflow.flow_prefilter("明天面试加油", False))
        self.assertTrue(groupflow.flow_prefilter("hello world", False))


class AsleepGateTest(unittest.TestCase):
    """#42：她正在睡觉时不该破冰、不该起话头（原来只有安静时段一道闸）。"""

    def _g(self, **kw):
        base = {
            "umo": "g", "msg_count": 20, "blocked_until": 0.0,
            "last_seen": NOW - 12 * 3600,
            "last_bot_spoke": NOW - 20 * 3600,
            "icebreak_at": NOW - 20 * 3600,
            "icebreak_day": "2026-10-06", "icebreak_count_day": 0,
            "topic_day": "", "topic_count_day": 0,
        }
        base.update(kw)
        return base

    def _icebreak(self, g, **kw):
        args = dict(idle_hours=6.0, daily_cap=1, stale_days=3,
                    today="2026-10-06", is_quiet=False)
        args.update(kw)
        return groupflow.icebreak_due(g, NOW, **args)

    def _topic(self, g, **kw):
        args = dict(gap_hours=8.0, daily_cap=1, stale_days=3,
                    today="2026-10-06", is_quiet=False)
        args.update(kw)
        return groupflow.group_topic_due(g, NOW, **args)

    def test_icebreak_suppressed_while_asleep(self):
        """原来会怎样：她凌晨睡着、群里正好不在静音时段，照样抛一句破冰。"""
        self.assertTrue(self._icebreak(self._g()), "醒着时本就该破冰，先证明前置条件成立")
        self.assertFalse(self._icebreak(self._g(), asleep=True), "睡着时不该破冰")

    def test_topic_suppressed_while_asleep(self):
        self.assertTrue(self._topic(self._g()), "醒着时本就该起话头，先证明前置条件成立")
        self.assertFalse(self._topic(self._g(), asleep=True), "睡着时不该起话头")

    def test_awake_behaves_exactly_as_before(self):
        """双向：`asleep=False` 与不传 asleep 的结论必须逐项一致（不能顺手改别的闸）。"""
        for g in (
            self._g(),
            self._g(last_seen=NOW - 60),           # 群还热，不该破冰
            self._g(msg_count=1),                  # 刚进的空群
            self._g(blocked_until=NOW + 3600),     # 发送隔离中
            self._g(icebreak_day="2026-10-06", icebreak_count_day=1),  # 今天破够了
        ):
            with self.subTest(g=g):
                self.assertEqual(
                    self._icebreak(g, asleep=False), self._icebreak(g),
                )
                self.assertEqual(self._topic(g, asleep=False), self._topic(g))

    def test_asleep_does_not_override_quiet(self):
        """两道闸互相独立：哪一个先命中都返回 False，不能因为睡着了就绕过安静时段的判断。"""
        self.assertFalse(self._icebreak(self._g(), asleep=True, is_quiet=True))
        self.assertFalse(self._topic(self._g(), asleep=True, is_quiet=True))


class GroupTopicSymmetricGuardTest(unittest.TestCase):
    """#43：`topic_at` 全仓库无人读；对称方向（刚起过话头就别又破冰）只靠
    `last_bot_spoke` 间接兜住。这里钉住「topic_at 被真的读到了」这件事。"""

    def _g(self, **kw):
        base = {
            "umo": "g", "msg_count": 20, "blocked_until": 0.0,
            "last_seen": NOW - 12 * 3600,
            "last_bot_spoke": NOW - 20 * 3600,   # 20h 前说过话：大于 idle_hours 的 6h
            "icebreak_at": NOW - 20 * 3600,
            "topic_at": NOW - 20 * 3600,
            "icebreak_day": "2026-10-06", "icebreak_count_day": 0,
        }
        base.update(kw)
        return base

    def _icebreak(self, g, **kw):
        args = dict(idle_hours=6.0, daily_cap=1, stale_days=3,
                    today="2026-10-06", is_quiet=False)
        args.update(kw)
        return groupflow.icebreak_due(g, NOW, **args)

    def test_topic_at_is_actually_read(self):
        """原来会怎样：`note_group_topic` 写的 `topic_at` 没人读，对称守卫不存在。
        这个前置条件刻意把 `last_bot_spoke` / `icebreak_at` 都放到 20h 前（20h > 6h
        的 idle 窗口），只留下 `topic_at`（1h 前）能挡住重复开口。
        旧实现这一格读的是 `max` 里的 `icebreak_at`，看不到 topic 的账 → 破冰照发。"""
        g_topic_1h_ago = self._g(topic_at=NOW - 3600)
        # 旧实现（只读 icebreak_at）：20h > 6h → 放行。这就是那个 bug。
        self.assertGreater(NOW - float(g_topic_1h_ago["icebreak_at"]), 6 * 3600.0)
        self.assertLess(NOW - float(g_topic_1h_ago["topic_at"]), 6 * 3600.0)
        self.assertFalse(
            self._icebreak(g_topic_1h_ago, idle_hours=6.0),
            "刚起过话头（1h 前）就不该再破冰 —— 对称方向也被 topic_at 挡住了",
        )

    def test_topic_guard_does_not_block_a_fresh_group(self):
        """反向：没起过话头的群（topic_at=0）不受影响，照样能破冰。"""
        self.assertTrue(self._icebreak(self._g(topic_at=0.0), idle_hours=6.0))


class WordCountThresholdTest(unittest.TestCase):
    """#47：`words_count_threshold` 该用同文件 `_word_count` 的口径（字母数字字符数）。"""

    def _cfg(self, threshold, *, words=("。",)):
        """word 模式 + 显式分隔符，把「拆分」这一步钉死，单测阈值闸门本身。"""
        return {"enable": True, "split_mode": "words", "split_words": list(words),
                "regex": r".*?[。？！~…]+|.+$", "words_count_threshold": threshold}

    def test_threshold_counts_alnum_not_characters(self):
        """临界输入：`你好呀？？？` 字符数 6、字母数字数 3。

        旧口径 `len(body) > threshold`（t=5）：6 > 5 → 判「超阈值」整条直发，
        `？？？` 原样留在消息里；
        新口径 `_word_count(body) > threshold`：3 <= 5 → 放行去拆，分隔符被框架
        那套拆法吃掉，于是拿到的是剥掉 `？` 的 `你好呀`。两条路径的首段因此不同：
        `['你好呀？？？']` vs `['你好呀']`。
        """
        body = "你好呀？？？"
        self.assertEqual(len(body), 6, "字符数")
        self.assertEqual(pacing._word_count(body), 3, "字母数字数")
        self.assertGreater(len(body), 5, "旧口径会判「超阈值，整条直发」")
        self.assertLessEqual(pacing._word_count(body), 5, "新口径判「没超，可以拆」")
        segs = pacing.split_segments(body, self._cfg(5, words=("？",)))
        self.assertEqual(segs, ["你好呀"], "旧口径给的是整条 ['你好呀？？？']")

    def test_long_but_few_alnum_still_splits(self):
        """反向：一堆标点凑出来的长串不再占满阈值。

        `好。！？。！？。！？` 字符 10、字母数字只有 1；旧口径 10 > 5 会整条直发。
        """
        body = "好。！？。！？。！？"
        self.assertEqual(len(body), 10, "字符数")
        self.assertGreater(len(body), 5, "旧口径会判「超阈值，整条直发」")
        self.assertEqual(pacing._word_count(body), 1)
        self.assertGreaterEqual(len(pacing.split_segments(body, self._cfg(5))), 2)

    def test_genuinely_long_text_still_goes_out_whole(self):
        """反向：正文真的超阈值时仍然整条直发（不要为了对齐口径把长消息放去拆）。

        两条拆法分支都要守住：words 与 regex（默认配置）。
        """
        body = "第一句很长的话。" * 10
        self.assertEqual(len(body), 80)
        self.assertGreater(len(body), 5, "旧口径也是超阈值")
        self.assertEqual(pacing._word_count(body), 70)
        self.assertGreater(pacing._word_count(body), 5, "新口径同样超阈值")
        self.assertEqual(pacing.split_segments(body, self._cfg(5)), [body])
        regex_cfg = {"enable": True, "split_mode": "regex",
                     "regex": r".*?[。？！~…]+|.+$", "words_count_threshold": 5}
        self.assertEqual(pacing.split_segments(body, regex_cfg), [body])

    def test_ascii_branch_unchanged(self):
        """ASCII 走空格分词那一支：`hello world` 字符 11、词数 2。

        阈值 1：旧口径 11 > 1 → 整条；新口径 2 > 1 → 也整条（这一支不变）。
        阈值 10：旧口径 11 > 10 → 整条；新口径 2 <= 10 → 拆成两段。
        """
        self.assertEqual(len("hello world"), 11)
        self.assertEqual(pacing._word_count("hello world"), 2)
        cfg = self._cfg(1, words=(" ",))
        self.assertEqual(pacing.split_segments("hello world", cfg), ["hello world"])
        self.assertGreater(len("hello world"), 1, "旧口径也整条直发")
        cfg = self._cfg(10, words=(" ",))
        self.assertEqual(pacing.split_segments("hello world", cfg), ["hello", "world"])


class PacingDocstringTest(unittest.TestCase):
    """#51：声称「逐字对齐 AstrBot 源码」而本环境没有 AstrBot 源码可核。"""

    def test_claim_is_not_presented_as_verbatim_verified(self):
        doc = pacing.__doc__ or ""
        self.assertNotIn("逐字对齐", doc, "本环境核不了 AstrBot 源码，不该再声称逐字对齐")
        self.assertIn("没有逐字比对过", doc, "应当如实说明「没在本环境逐字比对过」")
        self.assertIn("本环境", doc, "要写明是「本环境」核不了，而不是含糊带过")

    def test_alignment_claim_still_recorded(self):
        """不要删掉「照着那个 stage 的行为做」这件事本身，只改成如实表述。"""
        doc = pacing.__doc__ or ""
        self.assertIn("result_decorate", doc)
        self.assertIn("respond/stage.py", doc)


class WeatherPrefixTest(unittest.TestCase):
    """#7 的同型 bug 在破冰链路上还有一份：`icebreak_topics` 硬写 `f"外头{weather}"`。

    Core 的 `weather.env`（`engine._group_topic_hint` → `core.bot_self_state`）给的
    往往就是「外面在下雨」这种**整句**，于是破冰素材里出现过「外头外面在下雨」。
    `anchors._notable_weather` 那边 A1 已经修了，`groupflow` 这边漏了。
    判据现在只有一份（`social/textutil.py`），这条用例钉住两边共用同一份。
    """

    def _topics(self, weather):
        return groupflow.icebreak_topics("", weather)

    def test_line_does_not_double_the_prefix(self):
        """原来会怎样：`f"外头{weather}"` 直接拼 → 「外头外面在下雨」。"""
        out = self._topics("外面在下雨")
        self.assertIn("外面在下雨", out)
        self.assertNotIn("外头外面", out)
        self.assertNotIn("外头外头", out)
        # 内联复刻旧写法，证明这一格不是空转：
        self.assertIn("外头外面在下雨", f"外头{'外面在下雨'}")

    def test_prefix_is_still_added_when_the_sentence_lacks_it(self):
        """反向：原句没交代方位时照旧补「外头」，别把原来的能力改没了。"""
        out = self._topics("中雨，气温 9℃")
        self.assertIn("外头中雨", out)

    def test_every_declared_word_is_honoured(self):
        """词表里每一个词都要真的被认出来（不然守卫只对「外面」有效）。"""
        for word in textutil.OUTSIDE_WORDS:
            with self.subTest(word=word):
                body = f"{word}在下雪"
                self.assertEqual(textutil.outside_lead(body), body)
                # icebreak_topics 渲染成「- <原句>」：原句照旧，没有多出来的「外头」
                self.assertEqual(self._topics(body), f"- {body}")

    def test_both_layers_share_one_helper(self):
        """守卫：`groupflow` 不许再自己拼一遍「外头」。"""
        self.assertIs(groupflow.outside_lead, textutil.outside_lead)
        self.assertIs(anchors._outside_lead, textutil.outside_lead)
        self.assertIs(anchors._OUTSIDE_WORDS, textutil.OUTSIDE_WORDS)
        src = (PLUGIN_ROOT / "social" / "groupflow.py").read_text(encoding="utf-8")
        self.assertNotIn('f"外头{', src, "groupflow 又自己拼了一遍前缀")
        self.assertIn("from .textutil import outside_lead", src)


if __name__ == "__main__":
    unittest.main()
