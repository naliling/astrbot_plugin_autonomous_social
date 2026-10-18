"""主动消息「说什么」的两处硬伤（v1.27.41）。

## 一、`# 事件名` 那行泄漏进正文

线上真实记录（`proactive_log`）：

    '想念\\n刚眯醒，脑子里全是你，有些想你了💕'
    '答应带回来的吃的\\n出门时许诺带给我的美味，现在到哪儿了呢？💕'

开头那个孤零零的名词就是提示词要求的「# 事件名」，本该在解析阶段被吃掉。
漏的原因是两个函数各做对了一半：`_strip_intent` 把「# 空格」当 markdown 保留，
`_clean_output` 又把 `#` 剥掉——合起来就变成正文第一行发出去。

## 二、天气成了默认台词

天气是唯一「永远有、永远具体」的素材：别的由头都可能空，它不会空。于是
「天亮了」「今早阴冷」「天还阴着」连着几条。现在它只在**变了**且**隔了 6 小时**
时才作为由头出现，并且**不再进素材块**当背景。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest

if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("test")
    _astrbot.api = _api
    _event = types.ModuleType("astrbot.api.event")
    _event.MessageChain = object
    _api.event = _event
    sys.modules.update({"astrbot": _astrbot, "astrbot.api": _api, "astrbot.api.event": _event})

from social import anchors  # noqa: E402
from social.generator import MessageGenerator  # noqa: E402

NOW = 1_700_000_000.0
MAX_LEN = 200


def parse(text: str):
    return MessageGenerator._parse_decision(text, MAX_LEN, None, allow_emoji=True)


class TitleLineLeakTest(unittest.TestCase):
    """协议行不能变成正文。"""

    def test_real_world_sample_title_is_stripped(self):
        """线上原文：'想念\\n刚眯醒，脑子里全是你，有些想你了💕'"""
        d = parse("SEND\n# 想念\n刚眯醒，脑子里全是你，有些想你了💕")
        self.assertTrue(d.send)
        self.assertEqual(d.parts, ["刚眯醒，脑子里全是你，有些想你了💕"])

    def test_title_without_space_is_stripped(self):
        d = parse("SEND\n#想念\n你在干嘛呀")
        self.assertTrue(d.send)
        self.assertEqual(d.parts, ["你在干嘛呀"])

    def test_second_real_world_sample(self):
        d = parse("SEND\n# 答应带回来的吃的\n出门时许诺带给我的美味，现在到哪儿了呢？💕")
        self.assertTrue(d.send)
        self.assertEqual(d.parts, ["出门时许诺带给我的美味，现在到哪儿了呢？💕"])

    def test_title_only_means_nothing_to_send(self):
        d = parse("SEND\n# 想念")
        self.assertFalse(d.send, "整条只有一个标题，没有正文，不该发")

    def test_a_long_heading_is_content_not_title(self):
        """超过 16 字的 `#` 行是模型自己写的 markdown，不是协议标题。"""
        body = "今天把那个拖了很久的方案终于改完了，心里一下子空了一块"
        d = parse(f"SEND\n# {body}")
        self.assertTrue(d.send)
        self.assertIn("今天把那个拖了很久的方案", d.parts[0])

    def test_sentence_like_heading_is_content(self):
        d = parse("SEND\n# 今天真冷啊。\n我裹了两层")
        self.assertTrue(d.send)
        self.assertIn("今天真冷啊。", d.parts[0])

    def test_normal_message_untouched(self):
        d = parse("SEND\n# 面试\n你那个面试后来怎么样了")
        self.assertEqual(d.parts, ["你那个面试后来怎么样了"])


class WeatherGateTest(unittest.TestCase):
    def setUp(self):
        self.user: dict = {}

    def test_first_time_is_told(self):
        got = anchors.weather_worth_telling(self.user, {"weather": {"env": "阴，12℃"}}, NOW)
        self.assertTrue(got)

    def test_same_weather_is_not_repeated(self):
        body = {"weather": {"env": "阴，12℃"}}
        anchors.mark_weather_told(self.user, body, NOW)
        self.assertEqual(
            anchors.weather_worth_telling(self.user, body, NOW + 10 * 3600), ""
        )

    def test_changed_weather_within_six_hours_is_held(self):
        body = {"weather": {"env": "阴，12℃"}}
        anchors.mark_weather_told(self.user, body, NOW)
        later = {"weather": {"env": "小雨，10℃"}}
        self.assertEqual(anchors.weather_worth_telling(self.user, later, NOW + 3600), "")

    def test_changed_weather_after_six_hours_is_told(self):
        body = {"weather": {"env": "阴，12℃"}}
        anchors.mark_weather_told(self.user, body, NOW)
        later = {"weather": {"env": "小雨，10℃"}}
        got = anchors.weather_worth_telling(self.user, later, NOW + 7 * 3600)
        self.assertIn("小雨", got)

    def test_missing_weather_is_silent(self):
        self.assertEqual(anchors.weather_worth_telling(self.user, {}, NOW), "")


class VagueDoingTest(unittest.TestCase):
    def test_vague_phrases(self):
        for text in ("忙手里的活", "忙自己的事", "手头的事", "发呆", "休息", "", "嗯"):
            self.assertTrue(anchors.is_vague_doing(text), text)

    def test_concrete_phrases(self):
        for text in ("写季度报告", "整理个案笔记", "改方案", "做晚饭"):
            self.assertFalse(anchors.is_vague_doing(text), text)

    def test_vague_doing_does_not_become_an_anchor(self):
        body = {"day": {"doing": "忙手里的活"}}
        picks = anchors.prepare_round_anchors({"umo": "u", "last_seen": NOW - 60}, body, NOW, limit=3)
        texts = [str(p.get("about") or "") for p in picks]
        self.assertNotIn("刚忙完忙手里的活", texts)


class WeatherNotInMaterialTest(unittest.TestCase):
    def test_material_block_has_no_weather_line(self):
        gen = MessageGenerator(context=None, config=None, time_source=lambda: NOW)
        block = gen._material_block(
            {"last_message": "在吗", "conversation": []},
            {"weather": {"env": "阴，12℃"}, "day": {"doing": "写报告"}},
            __import__("datetime").datetime(2026, 11, 15, 10, 0),
            NOW,
        )
        self.assertNotIn("阴", block, f"素材块不该再摆天气：{block!r}")
        self.assertIn("写报告", block)


if __name__ == "__main__":
    unittest.main()
