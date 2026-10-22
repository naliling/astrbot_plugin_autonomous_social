"""v1.28.1：不许打破第四面墙——她活在对话里，不活在设备里。

用户原话：「现实→网友关键是这个会突然拿个手机出来 或者看着屏幕上的消息，
这类词 我自己也遇到过！」

## 真实数据

714 条角色会话里逐条扫 assistant 发言：

    「屏幕」50 处、「手机」19 处、「打字」3 处、「电脑」1 处

典型样本：

    (放下杯子…起身去打开界面。)
    我把手机按在胸口，深深吸了一口气，仿佛还能透过屏幕闻到你身上那股清冽的气息。
    可惜我现在只能打字给你…
    (独角轻蹭) 茉莉会用魔法看电脑……但蹄子打字很慢……

## 不是人设教的

五个人设（娜莉灵/阿莉娜/茉心/露娜/chat）里都没有这些词。阿莉娜那条
「日常使用手机联系」是在描述姐姐茉心的背景设定，不是让她自己用手机。
所以这是**模型的通病**：聊天气泡里的对话，它很自然地借用现实中的通讯隐喻。

## 为什么插件要拦

她是角色，不是客户端。她一「看屏幕」，用户那边瞬间出戏——而且这类句子几乎总是
长句、带着环境描写，正好是最容易暴露「这是 AI 在演」的那一类。

## 实现要点

* 查**原文**，不查 `strip_actions()` 之后的正文字——破墙点大量在括号里的动作描写中，
  而 `strip_actions` 恰恰把括号整段剥掉（线上那条「起身去打开界面」剥完只剩
  「真是的，这么着急呀。」，干净得像个乖宝宝）。
* 「消息」**不进**词表：真实数据里单独出现「消息」的 40 条中只有 2 条真破墙，
  误伤率 95%。它只在同句已有设备词时被连带覆盖。
"""

from __future__ import annotations

import logging
import sys
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
    api.logger = logging.getLogger("autonomous_social_v1281_wall_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.verify import verify_message  # noqa: E402


def check(text: str, recent=()):
    return verify_message(text, recent=list(recent), called="")


class WallRejectedTest(unittest.TestCase):
    """破墙的必须被拦。样本全部来自线上真实记录或它们的直系变体。"""

    def test_phone_against_chest(self) -> None:
        ok, why = check("我把手机按在胸口，深深吸了一口气，仿佛还能透过屏幕闻到你身上的气息。")
        self.assertFalse(ok, "「手机/屏幕」放进来了")
        self.assertIn("第四面墙", why)

    def test_open_ui_inside_action_paren(self) -> None:
        """破墙点在括号里——所以必须查原文，不能查 strip_actions 之后的正文字。"""
        ok, why = check(
            "(放下杯子，无奈地叹了口气，起身去打开界面。)  真是的，这么着急呀。"
        )
        self.assertFalse(ok, "括号里的「界面」被 strip_actions 剥掉了就没拦住")
        self.assertIn("第四面墙", why)

    def test_typing_is_a_device_act(self) -> None:
        ok, why = check("可惜我现在只能打字给你… 但心里默念给你听好不好？")
        self.assertFalse(ok)
        self.assertIn("打字", why)

    def test_magic_computer(self) -> None:
        ok, why = check("(独角轻蹭) 茉莉会用魔法看电脑……但蹄子打字很慢……")
        self.assertFalse(ok)

    def test_chat_box(self) -> None:
        ok, why = check("打开对话框看到你还在，我就放心了。")
        self.assertFalse(ok)

    def test_put_phone_down(self) -> None:
        ok, why = check("(把手机扣在桌上) 你刚才那句我没听清。")
        self.assertFalse(ok, "「把手机扣在桌上」也是设备动作")

    def test_stop_staring_at_screen(self) -> None:
        ok, why = check("别盯着屏幕了，眼睛会疼。")
        self.assertFalse(ok)

    def test_ai_self_reference(self) -> None:
        ok, why = check("作为一个人工智能助手，我理解您的要求。")
        self.assertFalse(ok, "AI 自指放进来了")

    def test_app_crash(self) -> None:
        ok, why = check("你这个APP又崩了吧，哈哈。")
        self.assertFalse(ok)


class WallNotOverreachingTest(unittest.TestCase):
    """不能误伤正常发言——这一半同样重要。"""

    def test_plain_daily_lines_pass(self) -> None:
        for text in (
            "(轻轻睁开眼，有些慵懒地看着你) 又怎么了，孩子？",
            "早上好呀，今天外面阴冷，记得多穿点。",
            "嗯嗯，我在听，你继续说。",
            "(蹭蹭你的手心) 好久不见，想你了。",
            "肚子好饿，有没有吃的。",
            "晚安，做个好梦。",
            "今天群里好安静呀，大家都去忙什么啦？",
            "别闹，再让我睡会儿嘛。",
            "困得眼皮都睁不开了。",
            "去厨房看看还有什么。",
        ):
            ok, why = check(text)
            self.assertTrue(ok, f"误伤了正常发言 {text!r}: {why}")

    def test_markup_id_is_not_a_wall_break(self) -> None:
        """`<ignore id="3802…">` 这类框架标记不是她提账号——要求 ID 前有汉字。"""
        ok, why = check("嘘，睡吧，小可爱。<ignore id=\"3802643441\" duration=\"480\" />")
        self.assertTrue(ok, f"框架标记里的 id= 被误伤: {why}")

    def test_cronjob_echo_is_not_a_wall_break(self) -> None:
        ok, why = check("[CronJob] 日常问候 triggered at 2026-08-23T07:00:00")
        self.assertTrue(ok, f"CronJob 回显被误伤: {why}")

    def test_account_with_chinese_prefix_is_caught(self) -> None:
        """真提账号时要拦——她不会说「我给你我的账号」。"""
        ok, why = check("我没法给你我的账号哦，想去AI角色平台找她")
        self.assertFalse(ok)
        self.assertIn("账号", why)

    def test_bare_message_word_is_not_a_wall_break(self) -> None:
        """「消息」单独出现不拦：真实数据里 40 条只有 2 条真破墙，误伤率 95%。"""
        for text in (
            "你还没回我，是不是在忙？",
            "刚才说到哪了？我们继续。",
            "有话就直说，别憋着。",
        ):
            ok, why = check(text)
            self.assertTrue(ok, f"「消息」误伤 {text!r}: {why}")

    def test_other_checks_still_fire(self) -> None:
        """新闸门不能把别的要求挤掉。"""
        # 协议残留仍然拦得住
        ok, why = check("SEND\n早上好呀")
        self.assertFalse(ok)
        self.assertIn("协议残留", why)


if __name__ == "__main__":
    unittest.main()