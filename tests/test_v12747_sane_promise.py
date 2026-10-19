"""v1.27.47：脏「承诺」不许进账本——它会原样变成下一轮的事实。

线上真实样本（从 prompt 里抓到的原文）：

```
【你现在知道的】
  · 你们说好的：了强烈的红光，你猛的惊喜回头望去发现这红光的指向点正是…
  · ？：…
```

第一条是从一段带括注的正文里切出来的碎片（括号都没闭合），第二条是纯符号。
这个字段会原样喂回模型，它就会顺着演一段根本不存在的剧情。
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
    api.logger = logging.getLogger("autonomous_social_v12747_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.state import SocialState  # noqa: E402
from social.threads import _promise_looks_sane, promise_entries  # noqa: E402


class SanePromiseTest(unittest.TestCase):
    def test_real_promises_pass(self) -> None:
        for text in ("明天给你看那个", "我回头查一下机票", "晚点把照片找出来"):
            self.assertTrue(_promise_looks_sane(text), text)

    def test_body_fragment_is_rejected(self) -> None:
        self.assertFalse(
            _promise_looks_sane(
                "了强烈的红光，你猛的惊喜回头望去发现这红光的指向点正是那装神弄鬼的医生)"
            )
        )

    def test_symbols_only_is_rejected(self) -> None:
        for text in ("？：…", "……", "）】"):
            self.assertFalse(_promise_looks_sane(text), text)

    def test_too_long_is_rejected(self) -> None:
        # prompt 让它写八个字以内，16 是宽容上限；再长一定是从正文里切出来的
        self.assertFalse(_promise_looks_sane("明天" * 12))

    def test_empty_is_rejected(self) -> None:
        for text in ("", "   ", None):
            self.assertFalse(_promise_looks_sane(text), repr(text))


class NotePromiseGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.user: dict = {}

    def test_bad_promise_is_not_recorded(self) -> None:
        SocialState.note_promise(self.user, "？：…", 1000.0)
        self.assertEqual(promise_entries(self.user), [])

    def test_fragment_is_not_recorded(self) -> None:
        SocialState.note_promise(self.user, "了强烈的红光，你猛的惊喜回头望去…)", 1000.0)
        self.assertEqual(promise_entries(self.user), [])

    def test_good_promise_is_recorded_once(self) -> None:
        SocialState.note_promise(self.user, "明天给你看那个", 1000.0)
        SocialState.note_promise(self.user, "明天给你看那个", 2000.0)
        self.assertEqual(len(promise_entries(self.user)), 1)


if __name__ == "__main__":
    unittest.main()
