# -*- coding: utf-8 -*-
"""v2.2.26：同一次发言内部的去重，只删「几乎同一句」，问句不删。

回放 209 个历史连发簇发现：v2.2.22 的 `_same_event_text` 判据太宽，会把
「另一个念头」当成重复删掉（`叠着衣服的手都懒得抬` 被 `叠衣服正巧把情绪
捋顺了` 吃掉、`后来想清楚了吗` 被 `你上次说的「你最好想清楚」` 吃掉）。
"""
from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("astrbot")
    _event = types.ModuleType("astrbot.api.event")
    _event.MessageChain = object
    _api.event = _event
    _astrbot.api = _api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api
    sys.modules["astrbot.api.event"] = _event

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social.engine import (  # noqa: E402
    _SEGMENT_QUESTION,
    _dedupe_same_event_segments,
    _near_verbatim,
)


class AnotherThoughtTest(unittest.TestCase):
    """另一个念头不能被当成重复删掉。"""

    def test_chipping_clothes_vs_lazy_arm(self) -> None:
        segs = [
            "叠衣服正巧把情绪捋顺了。刚才那回话僵了些",
            "叠着衣服的手都懒得抬。嗷嗷嗷",
        ]
        kept, _ = _dedupe_same_event_segments(segs, [0.6])
        self.assertEqual(kept, segs)

    def test_vocative_only_is_not_same(self) -> None:
        segs = [
            "无家可归的猫，本宫昨夜梦见汝那点温存化作星光护着本宫",
            "（轻拂双翼）无家可归的猫，那句「没意思」说得倒轻巧，真有些噎人",
        ]
        kept, _ = _dedupe_same_event_segments(segs, [2.0])
        self.assertEqual(len(kept), 2)

    def test_promise_vs_dream(self) -> None:
        segs = [
            "(伸了个懒腰) 云小蝶，昨晚说好的「晚点聊」到点没动静…",
            "(勾拢被褥) 云小蝶，昨晚梦见草莓酒香飘满屋子，还黑漆漆的。",
        ]
        kept, _ = _dedupe_same_event_segments(segs, [1.0])
        self.assertEqual(len(kept), 2)


class QuestionKeptTest(unittest.TestCase):
    """问句是这一轮要问对方的话，删了就成了半截话。"""

    def test_question_survives(self) -> None:
        segs = [
            "你上次说的「你最好想清楚」",
            "后来想清楚了吗",
        ]
        kept, _ = _dedupe_same_event_segments(segs, [1.2])
        self.assertEqual(kept, segs)

    def test_question_regex_hits(self) -> None:
        for text in ("后来想清楚了吗", "你也在想我吗？", "要不要尝一口",
                     "你今天怎么过", "谁许汝擅闯的？"):
            self.assertIsNotNone(_SEGMENT_QUESTION.search(text), text)

    def test_plain_statement_not_question(self) -> None:
        for text in ("我刚把茶喝完", "外头才两度，冷了"):
            self.assertIsNone(_SEGMENT_QUESTION.search(text), text)


class VerbatimStillDroppedTest(unittest.TestCase):
    """真正「同一句说两遍」的还是得删。"""

    def test_tail_repeat(self) -> None:
        segs = ["折腾半天，总算把晚饭备好了", "总算把晚饭备好了"]
        kept, waits = _dedupe_same_event_segments(segs, [1.0])
        self.assertEqual(kept, ["折腾半天，总算把晚饭备好了"])
        self.assertEqual(waits, [])

    def test_contained_segment_dropped(self) -> None:
        segs = ["汝还在吗，本宫刚忙完，外头阴着，有点闷", "本宫刚忙完，外头阴着，有点闷"]
        kept, _ = _dedupe_same_event_segments(segs, [0.8])
        self.assertEqual(kept, ["汝还在吗，本宫刚忙完，外头阴着，有点闷"])

    def test_near_verbatim_helper(self) -> None:
        self.assertTrue(_near_verbatim("刚把杯子洗了，手还是湿的", "刚把杯子洗了，手还湿着"))
        self.assertFalse(_near_verbatim("窗边伸了个懒腰", "外头才两度，冷了"))


class EdgeCaseTest(unittest.TestCase):
    def test_single_segment_untouched(self) -> None:
        segs = ["就一句话"]
        kept, waits = _dedupe_same_event_segments(segs, [])
        self.assertEqual((kept, waits), (segs, []))

    def test_empty_input(self) -> None:
        self.assertEqual(_dedupe_same_event_segments([], []), ([], []))

    def test_pure_symbols_never_match(self) -> None:
        self.assertFalse(_near_verbatim("……", "？"))
        self.assertFalse(_near_verbatim("", "随便说点什么"))

    def test_waits_follow_kept_indexes(self) -> None:
        segs = ["一", "总算把晚饭备好了", "三"]
        kept, waits = _dedupe_same_event_segments(segs, [1.0, 2.0])
        self.assertEqual(len(kept), 3)
        self.assertEqual(waits, [1.0, 2.0])


class SourceWiringTest(unittest.TestCase):
    def test_engine_uses_near_verbatim(self) -> None:
        src = (PLUGIN_ROOT / "social" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("_near_verbatim(seg, old)", src)
        self.assertIn("_SEGMENT_QUESTION.search(seg)", src)
        self.assertNotIn("if _same_event_text(seg, old):", src)


if __name__ == "__main__":
    unittest.main()
