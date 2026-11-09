"""v2.2.2 补丁：Core 素材里的设备类片段不许进生成提示词。

背景（2026-11-09 实测）：Core 的生活素材写着「躺着刷手机」「屏幕的光照在脸上」，
社交层原样喂给模型 → 模型照着写 → 验收层 `verify.py` 的 `_WALL_DEVICE` 判成
打破第四面墙 → 整条丢掉。2026-11-09 那一轮 18 次尝试只发出 1 条，
`last_rejected` 就是「（窝在沙发上，屏幕的光照在脸上，蹄子夹着手机慢慢划）…」。

这组用例钉住：素材侧不再把设备类片段交给模型。
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
    api.logger = logging.getLogger("autonomous_social_v222_device_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.core_bridge import CoreBridge, _DEVICE_WORDS, _scrub_device_material  # noqa: E402


class ScrubUnitTest(unittest.TestCase):
    def test_list_items_with_devices_are_dropped(self):
        view = {
            "day": {
                "doing": "在沙发上躺着刷手机",
                "done": ["把手机里没用的截图清了一遍", "把碗洗了", "翻了会儿手机"],
                "next": ["屏幕的光照在脸上", "去厨房烧水"],
                "wants": ["把相册往前翻"],
                "dreams": ["梦见自己在打字"],
            },
            "ongoing": ["在客厅坐着，翻了会儿手机", "把那份笔记整理完"],
        }
        got = _scrub_device_material(view)
        self.assertEqual(got["day"]["doing"], "")
        self.assertEqual(got["day"]["done"], ["把碗洗了"])
        self.assertEqual(got["day"]["next"], ["去厨房烧水"])
        self.assertEqual(got["day"]["wants"], ["把相册往前翻"])
        self.assertEqual(got["day"]["dreams"], [])
        self.assertEqual(got["ongoing"], ["把那份笔记整理完"])

    def test_clean_material_is_untouched(self):
        view = {
            "day": {"doing": "在厨房烧水", "done": ["把碗洗了"], "next": [], "wants": []},
            "ongoing": ["把那份笔记整理完"],
        }
        got = _scrub_device_material(view)
        self.assertEqual(got["day"]["doing"], "在厨房烧水")
        self.assertEqual(got["day"]["done"], ["把碗洗了"])
        self.assertEqual(got["ongoing"], ["把那份笔记整理完"])

    def test_event_block_with_device_is_dropped(self):
        view = {"event": {"phrase": "刚在屏幕上看到那句话"}, "day": {"event": {"phrase": "刚在屏幕上看到那句话"}}}
        got = _scrub_device_material(view)
        self.assertEqual(got["event"], {})
        self.assertEqual(got["day"]["event"], {})

    def test_bad_shapes_do_not_raise(self):
        for bad in ({}, {"day": None}, {"day": {"done": "手机"}}, {"ongoing": "手机"}, None):
            _scrub_device_material(bad)  # 不抛就算过

    def test_message_semantics_count_as_devices(self):
        # v2.2.3：日程里「给姐姐编辑消息」「回消息」会让模型补出手机与打字。
        for text in ("翅膀托着手边的东西给姐姐编辑消息", "把没回的消息翻出来", "戴着耳机听了一会儿"):
            self.assertIsNotNone(_DEVICE_WORDS.search(text), text)
    def test_device_words_cover_the_verifier_list(self):
        for word in ("手机", "屏幕", "电脑", "平板", "键盘", "鼠标", "充电器", "打字"):
            self.assertTrue(_DEVICE_WORDS.search(word), word)


class SelfViewNeverCarriesDevicesTest(unittest.TestCase):
    def test_view_drops_device_flavoured_day_material(self):
        bridge = CoreBridge("bridge", None)
        contract = {
            "v": 3,
            "body": {"energy": 50.0},
            "day": {
                "doing": "在沙发上躺着刷手机",
                "done": ["把手机里没用的截图清了一遍", "把碗洗了"],
                "next": ["去厨房烧水"],
                "dreams": ["屏幕的光照在脸上"],
            },
            "wants": ["翻了会儿手机"],
            "ongoing": ["把手机充上电"],
        }
        view = bridge._build_self_view({"contract": contract}, "bot1")
        flat = repr(view)
        for word in ("手机", "屏幕"):
            self.assertNotIn(word, flat, f"素材里不该再出现「{word}」：{flat[:400]}")
        # 干净的那条必须还在，不能一刀切清空
        self.assertIn("把碗洗了", repr(view))


if __name__ == "__main__":
    unittest.main()
