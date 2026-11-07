"""v2.1.5 §1：分段边界保护（social.pacing._protected_spans / _merge_protected）。

为什么值得一堆用例：拆段只看「这里有没有标点」，不看这个位置在什么里面，
于是「她说『好。我知道了』」被切成两条，第二条单独看像自言自语；23:00、
https://…、安装包.zip 被从中间切开，用户收到一串碎片。保护区间就是
「这些位置不许切」的清单。

用例分组：
- A 组 保护区间的识别（引号/括号/未闭合/时间/URL/文件名/缩写/域名/嵌套/空文本）；
- B 组 接入 split_segments（regex 两个分支 + words 分支）；
- C 组 _merge_protected 的合并语义（连续合并、无保护原样、空输入）；
- D 组 _auto_split 跳过保护断点；
- E 组 回归（正常长句仍能切、阈值超长整条直发）。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与 test_v213_group_closeout.py 同款，幂等） ───
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
    api.logger = logging.getLogger("autonomous_social_v215_segments_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _FakeMessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import pacing  # noqa: E402

# 拆点用的三条自定义正则：分别只在冒号/斜杠/点上切，专门用来验证 token 级保护。
_COLON_RE = {"regex": r".*?[:：]+|.+$"}
_SLASH_RE = {"regex": r".*?[/]+|.+$"}
_DOT_RE = {"regex": r".*?[.]+|.+$"}


class ProtectedSpansTest(unittest.TestCase):
    """A 组：_protected_spans 识别得对不对。"""

    def test_quote_pair(self):
        self.assertEqual(pacing._protected_spans("她说「好」"), [(2, 5)])

    def test_quote_unclosed_protects_to_end(self):
        # 未闭合（模型被截断）时整条都不可信，保护到文末
        self.assertEqual(pacing._protected_spans("她说「好"), [(2, 4)])

    def test_bracket_pair(self):
        self.assertEqual(pacing._protected_spans("他（笑）"), [(1, 4)])

    def test_time_and_ratio(self):
        self.assertEqual(pacing._protected_spans("23:00"), [(0, 5)])
        self.assertEqual(pacing._protected_spans("16:9"), [(0, 4)])

    def test_url(self):
        self.assertEqual(pacing._protected_spans("看 https://x.y/z 吧"), [(2, 15)])

    def test_filename(self):
        self.assertEqual(pacing._protected_spans("安装包.zip"), [(0, 7)])

    def test_abbreviation_dots(self):
        self.assertEqual(pacing._protected_spans("U.S."), [(0, 4)])
        self.assertEqual(pacing._protected_spans("e.g."), [(0, 4)])

    def test_domain_dot(self):
        self.assertEqual(pacing._protected_spans("上 example.com"), [(2, 13)])

    def test_nested_spans_merge(self):
        # （他说『好』）三层嵌套合成一个区间
        self.assertEqual(pacing._protected_spans("（他说『好』）"), [(0, 7)])

    def test_empty_text(self):
        self.assertEqual(pacing._protected_spans(""), [])
        self.assertEqual(pacing._protected_spans(None), [])

    def test_plain_text_has_no_spans(self):
        self.assertEqual(pacing._protected_spans("今天天气真好。我们出去玩吧。"), [])


class SplitSegmentsProtectionTest(unittest.TestCase):
    """B 组：保护接进 split_segments 之后，这些 token 不再被切开。"""

    def test_quote_not_split(self):
        self.assertEqual(
            pacing.split_segments("她说「今天好累。想早点睡」", {}),
            ["她说「今天好累。想早点睡」"],
        )

    def test_bracket_not_split(self):
        self.assertEqual(
            pacing.split_segments("他（笑了一下。又接着说）", {}),
            ["他（笑了一下。又接着说）"],
        )

    def test_time_not_split(self):
        self.assertEqual(
            pacing.split_segments("明天23:00见", _COLON_RE), ["明天23:00见"]
        )

    def test_url_not_split(self):
        self.assertEqual(
            pacing.split_segments("看 https://x.com/a 吧", _SLASH_RE),
            ["看 https://x.com/a 吧"],
        )

    def test_filename_not_split(self):
        self.assertEqual(
            pacing.split_segments("安装包.zip", _DOT_RE), ["安装包.zip"]
        )

    def test_abbreviation_not_split(self):
        # 「U.S.」整个留在第一段；它后面的断点是合法断点，允许切
        self.assertEqual(pacing.split_segments("U.S. 那边", _DOT_RE), ["U.S.", "那边"])

    def test_words_branch_protects_quotes(self):
        parts = pacing.split_segments(
            "她说「今天好累。想早点睡」", {"split_mode": "words", "split_words": ["。"]}
        )
        self.assertEqual(len(parts), 1)
        self.assertIn("今天好累", parts[0])
        self.assertIn("想早点睡", parts[0])

    def test_unsplittable_returns_original(self):
        self.assertEqual(pacing.split_segments("「好」", {}), ["「好」"])


class MergeProtectedTest(unittest.TestCase):
    """C 组：_merge_protected 的合并语义。"""

    def test_merges_consecutive_boundaries(self):
        parts = pacing._merge_protected(["她说「", "今天好累。", "想早点睡」"])
        self.assertEqual(parts, ["她说「今天好累。想早点睡」"])

    def test_without_protection_returns_same(self):
        parts = ["今天天气真好。", "我们出去玩吧。"]
        self.assertEqual(pacing._merge_protected(parts), parts)

    def test_empty_and_single(self):
        self.assertEqual(pacing._merge_protected([]), [])
        self.assertEqual(pacing._merge_protected(["只有一段"]), ["只有一段"])

    def test_keeps_legit_boundary(self):
        # 引号在第二段内部，第一段的边界不在保护区间里 → 不合并
        parts = pacing._merge_protected(["好。", "她说「行」"])
        self.assertEqual(parts, ["好。", "她说「行」"])


class AutoSplitProtectionTest(unittest.TestCase):
    """D 组：_auto_split 选断点时跳过保护区间。"""

    def test_skips_break_inside_quote(self):
        # 长度够、唯一的候选断点在引号里 → 宁可不拆
        text = "她说「今天真的太累了，想早点睡。你也早点休息吧」然后我们就聊到这里"
        self.assertEqual(pacing._auto_split(text), [])

    def test_falls_back_to_earlier_legal_break(self):
        text = "她今天真的好累啊，想早点睡。她说「我也该睡了。明天再聊吧」晚安"
        parts = pacing._auto_split(text)
        self.assertEqual(len(parts), 2)
        # 引号必须完整落在某一段里，不能被拆到两段
        quoted = "「我也该睡了。明天再聊吧」"
        self.assertTrue(any(quoted in part for part in parts), parts)

    def test_short_text_still_returns_empty(self):
        self.assertEqual(pacing._auto_split("太短了。"), [])


class RegressionTest(unittest.TestCase):
    """E 组：保护不能把正常拆分也一并废掉。"""

    def test_normal_long_text_still_splits(self):
        parts = pacing.split_segments("今天天气真好。我们出去玩吧。顺便买点水果", {})
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(part.strip() for part in parts))

    def test_consecutive_punctuation_no_empty_part(self):
        parts = pacing.split_segments("真的吗？！。然后呢", {})
        self.assertTrue(parts)
        self.assertTrue(all(part.strip() for part in parts))

    def test_over_threshold_returns_whole(self):
        long_text = "字" * 40 + "。"
        parts = pacing.split_segments(long_text, {"words_count_threshold": 5})
        self.assertEqual(parts, [long_text])

    def test_empty_text_returns_empty(self):
        self.assertEqual(pacing.split_segments("   ", {}), [])


if __name__ == "__main__":
    unittest.main()
