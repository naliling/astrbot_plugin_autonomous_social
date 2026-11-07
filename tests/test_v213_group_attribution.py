"""v2.1.3：群提及严格归因（`social.groupflow.attribution`）。

`addressed_to` 只回答「开头是不是叫了某个名字」。拿它当「在叫我吗」用，三种情况
会被误判成点名：同名群名片、第三人称谈论（「小助手刚才说…」）、句中随口提到。
归因的口径反过来——**只有 At 才算点名**，文本证据只用来解释「为什么不算」。

用例分组：
- A 组 At 判定：@本 bot / @全体 / @全体别名 / @别人；
- B 组 文本证据：同名名片 / 当面叫 / 第三人称转述 / 代词 / 只是提到 / 闲聊；
- C 组 边界：空文本 / 纯 @ / 脏参数 / 旧函数不被改动 / 与 BYPASS_REASONS 对齐。

每条用例都写清「旧口径会怎么判」，证明断言不是空转。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

# ─── 路径与最小 astrbot 桩（与 test_p2_groupflow_pacing.py 同款，幂等） ───
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
    api.logger = logging.getLogger("autonomous_social_v213_attribution_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _FakeMessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import groupflow, throttle  # noqa: E402

BOT_ID = "10086"
BOT_NAMES = ("小助手", "助手酱")
NOW = 1_700_000_000.0


class AtAttributionTest(unittest.TestCase):
    """A 组：只有 At 才算点名。"""

    def test_at_bot_id_is_the_only_addressed(self):
        """@ 到本 bot = 唯一确定的点名；正文再普通也不影响。"""
        got = groupflow.attribution(
            "在吗", at_ids=[BOT_ID], bot_id=BOT_ID, bot_names=BOT_NAMES
        )
        self.assertTrue(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_MENTION)
        self.assertEqual(got["targets"], (BOT_ID,))

    def test_at_all_counts_as_addressed(self):
        """@全体也算——她也在「全体」里；旧口径只看正文，这条会漏成 none。"""
        got = groupflow.attribution("开会了", at_ids=["all"], bot_id=BOT_ID)
        self.assertTrue(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_MENTION)
        self.assertEqual(got["targets"], ("all",))

    def test_at_all_aliases_are_normalised(self):
        """适配器给 @全体 的写法不统一，归一化后一律是 "all"。"""
        for raw in ("@all", "atall", "at_all", "everyone", "ALL"):
            with self.subTest(raw=raw):
                got = groupflow.attribution("集合", at_ids=[raw], bot_id=BOT_ID)
                self.assertTrue(got["addressed"])
                self.assertEqual(got["targets"], ("all",))

    def test_at_someone_else_is_not_addressed(self):
        """@ 的是别人：不是叫她，正文也没证据 → none。"""
        got = groupflow.attribution(
            "今天天气不错", at_ids=["99999"], bot_id=BOT_ID, bot_names=BOT_NAMES
        )
        self.assertFalse(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_NONE)
        self.assertEqual(got["targets"], ())

    def test_at_other_person_with_bot_name_in_text_is_name_only(self):
        """@ 别人、正文提了 bot 的名字：只是 name_only，不是点名。"""
        got = groupflow.attribution(
            "小助手你觉得呢", at_ids=["99999"], bot_id=BOT_ID, bot_names=BOT_NAMES
        )
        self.assertFalse(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_NAME_ONLY)


class TextEvidenceTest(unittest.TestCase):
    """B 组：文本证据一律不构成点名，只解释「为什么不算」。"""

    def test_same_name_card_is_not_addressed(self):
        """同名群名片：这句「小助手你…」在叫那个人，不是叫她。

        旧口径 `addressed_to` 会返回「小助手」并被当成在叫她。
        """
        got = groupflow.attribution(
            "小助手你昨天那个面试怎么样了", bot_names=BOT_NAMES
        )
        self.assertFalse(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_NAME_ONLY)
        self.assertEqual(got["targets"], ("小助手",))

    def test_direct_call_with_you_is_name_only(self):
        """「小助手，说说你的看法」是当面叫，但没 At → name_only，不是第三人称。"""
        got = groupflow.attribution("小助手，说说你的看法", bot_names=BOT_NAMES)
        self.assertFalse(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_NAME_ONLY)

    def test_third_person_about_bot(self):
        """「小助手刚才说的那个」是在说她，不是叫她 → third_person。"""
        got = groupflow.attribution("小助手刚才说的那个功能挺好玩", bot_names=BOT_NAMES)
        self.assertFalse(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_THIRD_PERSON)
        self.assertEqual(got["targets"], ("小助手",))

    def test_third_person_pronoun(self):
        """纯代词转述：「她刚才说…」 → third_person。"""
        got = groupflow.attribution("她刚才说今天不来了", bot_names=BOT_NAMES)
        self.assertFalse(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_THIRD_PERSON)
        self.assertTrue(got["targets"][0].startswith("她"))

    def test_third_person_about_another_member(self):
        """转述的是别的群友：「小明刚才说…」——旧口径会当成在叫小明。"""
        got = groupflow.attribution(
            "小明刚才说他要请假", bot_names=BOT_NAMES, known_names=["小明", "小红"]
        )
        self.assertFalse(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_THIRD_PERSON)
        self.assertEqual(got["targets"], ("小明",))

    def test_plain_mention_in_comment_is_name_only(self):
        """评论里随口提到名字（没有转述动词）→ name_only。"""
        got = groupflow.attribution("我觉得小助手挺好的", bot_names=BOT_NAMES)
        self.assertFalse(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_NAME_ONLY)

    def test_third_person_beats_plain_name_mention(self):
        """同一句里既有名字又有转述动词：reason 必须是 third_person。

        「只算 third_person」是需求原话——名字在这里是转述的对象，不是称呼。
        """
        got = groupflow.attribution("小助手说得对", bot_names=BOT_NAMES)
        self.assertEqual(got["reason"], groupflow.ATTR_THIRD_PERSON)

    def test_chatter_is_none(self):
        """普通闲聊：既没 At 也没名字 → none。"""
        got = groupflow.attribution("今天天气不错", bot_names=BOT_NAMES)
        self.assertFalse(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_NONE)
        self.assertEqual(got["targets"], ())

    def test_talking_to_another_member_is_not_addressed(self):
        """对着别人说话（「小明你觉得呢」）不该被算成在叫她。"""
        got = groupflow.attribution("小明你觉得呢", bot_names=BOT_NAMES, known_names=["小明"])
        self.assertFalse(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_NONE)


class BoundaryTest(unittest.TestCase):
    """C 组：边界与兼容。"""

    def test_empty_text_is_empty(self):
        """没正文、没 At → empty。"""
        got = groupflow.attribution("", bot_names=BOT_NAMES)
        self.assertFalse(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_EMPTY)
        self.assertEqual(got["targets"], ())

    def test_at_bot_with_empty_text_is_still_mention(self):
        """纯 @（正文为空）仍然是点名：那是明着叫她，只是没打字。

        这是与「空文本 empty」字面顺序不同的**有意**取舍——把 @ 判成 empty
        等于让她无视一次点名。
        """
        got = groupflow.attribution("   ", at_ids=[BOT_ID], bot_id=BOT_ID)
        self.assertTrue(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_MENTION)

    def test_mention_beats_text_evidence(self):
        """@ 了她，同时正文在第三人称谈论她：At 更强，仍是 mention。"""
        got = groupflow.attribution(
            "小助手刚才说…", at_ids=[BOT_ID], bot_id=BOT_ID, bot_names=BOT_NAMES
        )
        self.assertTrue(got["addressed"])
        self.assertEqual(got["reason"], groupflow.ATTR_MENTION)

    def test_junk_inputs_do_not_raise(self):
        """跨适配器的脏参数一律兜住：群消息链路上抛一次就是整条消息丢掉。"""
        got = groupflow.attribution(
            None, at_ids=None, bot_id=None, bot_names=None, known_names=None
        )
        self.assertEqual(got["reason"], groupflow.ATTR_EMPTY)
        self.assertEqual(got, {"addressed": False, "reason": "empty", "targets": ()})
        # 单个字符串当列表传也认（调用方手滑）
        self.assertEqual(
            groupflow.attribution("小助手你好", bot_names="小助手")["reason"],
            groupflow.ATTR_NAME_ONLY,
        )
        # 非字符串正文：str() 兜底，不抛
        self.assertEqual(groupflow.attribution(12345)["reason"], groupflow.ATTR_NONE)

    def test_single_char_names_are_ignored(self):
        """单字名字做子串匹配等于匹配一切，与 addressed_to 的 len>=2 口径一致。"""
        got = groupflow.attribution("小你好", bot_names=["小"])
        self.assertEqual(got["reason"], groupflow.ATTR_NONE)

    def test_targets_is_always_a_tuple(self):
        """契约：targets 恒为 tuple（调用方会直接拿去做日志/比较）。"""
        cases = [
            ("", {}),
            ("今天天气不错", {"bot_names": BOT_NAMES}),
            ("小助手你昨天那个面试怎么样了", {"bot_names": BOT_NAMES}),
            ("她刚才说今天不来了", {"bot_names": BOT_NAMES}),
            ("在吗", {"at_ids": [BOT_ID], "bot_id": BOT_ID}),
        ]
        for text, kwargs in cases:
            with self.subTest(text=text):
                got = groupflow.attribution(text, **kwargs)
                self.assertIsInstance(got["targets"], tuple)
                self.assertEqual(
                    set(got), {"addressed", "reason", "targets"}
                )
                self.assertIsInstance(got["addressed"], bool)

    def test_addressed_to_is_unchanged(self):
        """旧函数签名与语义不动：别的调用方（心流语境）还在用它。"""
        self.assertEqual(
            groupflow.addressed_to("小明你那个面试怎么样了", ["小明", "小红"]), "小明"
        )
        # 旧口径对句中的名字本来就不判——这里只是确认没被顺手改掉
        self.assertEqual(groupflow.addressed_to("我觉得小明说得对", ["小明"]), "")
        self.assertEqual(groupflow.addressed_to("", ["小明"]), "")

    def test_reason_codes_align_with_closeout_bypass(self):
        """归因的 mention 与群硬收口的放行白名单是同一个字面量。

        两处各写一份映射迟早会漂：这边改成 "at"，收口就会把她真正的点名也拦掉。
        """
        self.assertEqual(groupflow.ATTR_MENTION, "mention")
        self.assertIn(groupflow.ATTR_MENTION, throttle.BYPASS_REASONS)
        self.assertEqual(
            {groupflow.ATTR_MENTION, groupflow.ATTR_NAME_ONLY,
             groupflow.ATTR_THIRD_PERSON, groupflow.ATTR_NONE, groupflow.ATTR_EMPTY},
            {"mention", "name_only", "third_person", "none", "empty"},
        )


if __name__ == "__main__":
    unittest.main()
