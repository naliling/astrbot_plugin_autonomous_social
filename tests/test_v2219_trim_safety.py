# -*- coding: utf-8 -*-
"""验证出站正文保持原样，硬安全不靠改写来修复不安全文本。

背景：v2.2.10 把「删整句」改成「剪小句」以后，全历史 2379 条回放发现 6 条被
剪坏的真事故（都是模型用空格断句、或说教词命中了动作描写）：

  1. `（翅膀无力地扇动，眼角渗出生理性泪水）…别发这些了，我真的一宿没睡…`
     → `（翅膀无力地扇动，我真的一宿没睡…`（括号没闭合）
  2. `（刚醒没多久就听他在胡思乱想，有些气恼地咬了咬下唇。）\n\n不许这么说自己……`
     → `，有些气恼地咬了咬下唇。`（整条只剩动作，还以逗号开头）
  3. `唔 既如此 那就别再扰本宫了 夜里清静…` → `唔 既如此`（只剩一句前言）
  4. `嗯 好 晚安 凝宝宝 别闹了 贴紧些` → `嗯 好 晚安 凝宝宝`（把好话一起吃掉了）
  5. `…再气我的话 那些大梦就都别做了。💕` → `…再气我的话💕`（停在半截条件上）
  6. 蹄类把自己的手改成了「蹄子臂」「蹄子心」这类不存在的词

这里逐条钉住。
"""

import os
import sys
import unittest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

try:  # astrbot 在测试环境里通常不存在，用最小桩顶上
    import astrbot  # noqa: F401
except Exception:  # pragma: no cover
    import types as _types

    _astrbot = _types.ModuleType("astrbot")
    _api = _types.ModuleType("astrbot.api")
    _logger = _types.ModuleType("astrbot.api.logger")

    class _L:
        def __getattr__(self, name):
            return lambda *a, **k: None

    _logger.logger = _L()
    _api.logger = _logger
    _astrbot.api = _api
    sys.modules.setdefault("astrbot", _astrbot)
    sys.modules.setdefault("astrbot.api", _api)
    sys.modules.setdefault("astrbot.api.logger", _logger)

from social import verify as V  # noqa: E402

HOOF = "天马"


class TrimSafetyTest(unittest.TestCase):
    """剪完的句子必须还是人话。"""

    def test_brackets_stay_closed(self):
        # 事故 1：小句跨过了「）」
        src = "（翅膀无力地扇动，眼角渗出生理性泪水）…别发这些了，我真的一宿没睡…"
        got = V.tidy_outgoing_text(src, species=HOOF)
        self.assertEqual(got, src)

    def test_action_inside_brackets_is_not_trimmed(self):
        # 事故 2：动作描写里的「胡思乱想」曾被当说教，整条剪成「，…咬了咬下唇。」
        src = "（刚醒没多久就听他在胡思乱想，有些气恼地咬了咬下唇。）\n\n不许这么说自己……❤️"
        got = V.tidy_outgoing_text(src, species=HOOF)
        self.assertEqual(got, src)

    def test_no_dangling_condition_with_emoji(self):
        # 事故 5：尾巴上挂了 emoji，`的话` 半截话判定曾失效
        src = "大清早就算计这个 心肠真坏 过来先把吹僵的翅尖捂暖。再气我的话 那些大梦就都别做了。💕"
        got = V.tidy_outgoing_text(src, species=HOOF)
        self.assertEqual(got, src)
        ok, why = V.verify_hard_safety(src, species=HOOF)
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_conditional_retaliation_is_rejected(self):
        text = "再气我的话，那些大梦就都别做了。"
        ok, reason = V.verify_hard_safety(text, species=HOOF)
        self.assertFalse(ok)
        self.assertIn("把话说死", reason)

    def test_ordinary_condition_is_allowed(self):
        for text in ("如果今晚下雨，就在家看电影。", "再努力的话，就能把事情做好。"):
            self.assertTrue(V.verify_hard_safety(text, species=HOOF)[0], text)

    def test_keeps_the_good_half(self):
        # 事故 4：别闹了被剪掉，但「贴紧些」必须留着
        src = "嗯 好 晚安 凝宝宝 别闹了 贴紧些"
        got = V.tidy_outgoing_text(
            src, species=HOOF, recent=["别闹了", "别问了"]
        )
        self.assertEqual(got, src)

    def test_prefix_only_result_is_rejected(self):
        # 事故 3：只剩一句前言（「唔 既如此」）时，剪枝必须整体放弃
        src = "唔 既如此 那就别再扰本宫了 夜里清静 汝也该自去安歇 别再用响动吵本宫补个安稳觉"
        got = V.tidy_outgoing_text(src, species=HOOF)
        self.assertEqual(got, src)

    def test_trim_safe_rejects_bad_candidates(self):
        self.assertFalse(V._preachy_trim_safe("（动作）正文内容还有", "，正文内容"))
        self.assertFalse(V._preachy_trim_safe("（动作）正文内容还有", "正文（动作"))
        self.assertFalse(V._preachy_trim_safe("正文内容还有很多", "再气我的话"))
        self.assertFalse(V._preachy_trim_safe("正文内容还有很多", "短"))
        self.assertTrue(V._preachy_trim_safe("正文内容还有很多", "正文内容还有"))

    def test_action_word_is_not_preachy(self):
        # 「听他在胡思乱想」是描述对方，不是说教；祈使才算
        self.assertEqual(V._preachy_hits("（听他在胡思乱想，咬了咬下唇。）"), 0)
        self.assertGreaterEqual(V._preachy_hits("别胡思乱想的"), 1)


class NaturalBodyWordTest(unittest.TestCase):
    """物种检查拒绝冲突事实，但不会把角色身体词替换成别的词。"""

    def test_arm_becomes_front_leg(self):
        got = V.tidy_outgoing_text("(抱着手臂靠在墙边)", species=HOOF)
        self.assertEqual(got, "(抱着手臂靠在墙边)")
        self.assertFalse(V.verify_hard_safety(got, species=HOOF)[0])

    def test_palm_becomes_hoof_center(self):
        for body in ("（蹭了蹭手心）", "掰着手指算"):
            self.assertEqual(V.tidy_outgoing_text(body, species=HOOF), body)

    def test_plain_hand_still_falls_back(self):
        got = V.tidy_outgoing_text("我刚醒手还有点僵", species=HOOF)
        self.assertEqual(got, "我刚醒手还有点僵")
        self.assertFalse(V.verify_hard_safety(got, species=HOOF)[0])

    def test_other_persons_hand_untouched(self):
        got = V.tidy_outgoing_text("（蹭了蹭你的手心）", species=HOOF)
        self.assertEqual(got, "（蹭了蹭你的手心）")


if __name__ == "__main__":
    unittest.main()
