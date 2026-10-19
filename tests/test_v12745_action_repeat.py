"""v1.27.45：同一个动作标签不许连着用。

线上真实样本（改前全部放行，改后该拦的拦）：
`(停下进食的动作)` 用了两次、`(放下餐具)` 两次，正文措辞完全不同，
所以二元组复读抓不到。
"""
from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v12745_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.verify import _action_tags, verify_message  # noqa: E402

_RECENT = [
    "(停下进食的动作)早餐那页记录的后续，还没讲完吧。❤️",
    "(放下餐具) 普拉娜，外头阴沉7度，有点冷呢。❤️",
]


class ActionTagsTest(unittest.TestCase):
    def test_prefix_is_taken_not_the_whole_tag(self) -> None:
        self.assertEqual(_action_tags("(停下进食的动作) 嗯。"), ["停下进食"])

    def test_short_tags_are_ignored(self) -> None:
        # 两字招牌是人设（娜莉灵的人设就要求动作用括号），不能拦
        self.assertEqual(_action_tags("(轻笑) 嗯。"), [])

    def test_no_action_means_no_tags(self) -> None:
        self.assertEqual(_action_tags("今天有点冷。"), [])

    def test_fullwidth_and_bracket_forms_all_count(self) -> None:
        self.assertEqual(_action_tags("【揉了揉眼睛】好困。"), ["揉了揉眼"])
        self.assertEqual(_action_tags("[放下餐具] 吃完了。"), ["放下餐具"])


class ActionRepeatTest(unittest.TestCase):
    def test_same_tag_is_blocked(self) -> None:
        ok, why = verify_message("(放下餐具) 吃饱了，午后真困。❤️", recent=_RECENT)
        self.assertFalse(ok)
        self.assertIn("动作标签复读", why)

    def test_same_tag_with_extra_words_still_blocked(self) -> None:
        ok, why = verify_message(
            "(停下进食的动作，抬眼看你) 昨晚那句爱你，还让我心口发烫呢。❤️", recent=_RECENT
        )
        self.assertFalse(ok)
        self.assertIn("动作标签复读", why)

    def test_a_fresh_tag_passes(self) -> None:
        ok, why = verify_message("(揉了揉眼睛) 这天气，懒得动。", recent=_RECENT)
        self.assertTrue(ok, why)

    def test_short_persona_tag_passes_even_if_repeated(self) -> None:
        ok, why = verify_message("(轻笑) 你说这话的时候，我在想别的。", recent=["(轻笑) 嗯。"])
        self.assertTrue(ok, why)

    def test_window_covers_the_recent_twenty(self) -> None:
        # v1.27.46 把窗口从 8 提到 20：第 1 条用过的动作，第 12 条仍然算重复
        recent = ["(放下餐具) 先用了它"] + [f"第{i}条不一样的话" for i in range(1, 12)]
        ok, why = verify_message("(放下餐具) 吃饱了。", recent=recent)
        self.assertFalse(ok, why)
        far = ["(放下餐具) 先用了它"] + [f"第{i}条不一样的话" for i in range(1, 25)]
        ok, why = verify_message("(放下餐具) 吃饱了。", recent=far)
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main()
