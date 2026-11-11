# -*- coding: utf-8 -*-
"""v2.2.25：私聊主动消息的开头轮换，别再瞎掉四分之一的样本。

线上 951 条主动消息里 231 条（24.3%）以括号动作起头（`（翅膀收拢）唔…`）。
旧 `_opening_neurons` 直接取 `body[:1]`，首字是「（」、判成「没有起头」→ 跳过。
结果 196 个有主动消息的对话方里，54 个（27.6%）一句开头提示都拿不到。
"""

import logging
import sys
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
    api.logger = logging.getLogger("aso_test_2225")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    event.AstrMessageEvent = object
    api.event = event
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social import generator as gen  # noqa: E402
from social import verify as V  # noqa: E402


class ActionStartIsNotBlankTest(unittest.TestCase):
    def test_action_start_contributes_its_first_word(self):
        notes = gen._opening_neurons(
            ["（翅膀收拢）唔 好", "（翅膀收拢）唔 再睡会儿", "（翅膀张开）嗯"]
        )
        self.assertTrue(notes)
        self.assertNotIn("（", notes)
        # 出现两次的排前面
        self.assertEqual(notes[0], "唔")

    def test_only_bracket_starts_are_no_longer_blank(self):
        notes = gen._opening_neurons(
            ["（伸手摸了摸你的头）乖", "（把被子往上拉了拉）睡吧"]
        )
        self.assertEqual(len(set(notes)), 2)

    def test_pure_punctuation_still_skipped(self):
        self.assertEqual(gen._opening_neurons(["……", "？？？", "。。。"]), [])

    def test_action_start_text_is_normalised(self):
        # 与群聊侧同一个规整器：开头轮换不该按「括号」分类
        for text in ("（翅膀收拢）唔 好", "（翅膀收拢，羽尖还带着凉意）唔 好"):
            self.assertEqual(V.opening_head(text), "唔")


class ProactiveActionTicTest(unittest.TestCase):
    def test_repeated_action_skeleton_is_reported(self):
        # 同一个动作重复 ≥2 次：报动作名
        rep = V.action_tics(["（翅膀收拢）唔 好", "（翅膀收拢羽尖抵着枕头）嗯"])
        self.assertTrue(any("翅膀收拢" in n for n in rep), rep)

        # 5 条里 3 条以括号动作起头：报骨架密度（阈值是 5 条起算）
        texts = [
            "（翅膀收拢）唔 好",
            "（把脸埋进枕头）困",
            "（翻了个身）嗯",
            "早",
            "在",
        ]
        notes = V.action_tics(texts)
        self.assertTrue(any("先写括号动作" in n for n in notes), notes)

    def test_prompt_has_the_two_blocks_wired(self):
        src = Path(PLUGIN_ROOT, "social", "generator.py").read_text(encoding="utf-8")
        self.assertIn("opening_head(body)", src)
        self.assertIn("action_tics(", src)
        self.assertIn("这次换个起手，或者干脆不写动作", src)


if __name__ == "__main__":
    unittest.main()
