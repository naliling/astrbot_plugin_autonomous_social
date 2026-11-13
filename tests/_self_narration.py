"""v2.2.48：`SELF_NARRATION_ENABLED = False` 之后，测「她今天在干什么」这条派生
机制的用例必须显式把它打开。

这些用例验的是**机制**——空话 doing 被滤掉、done 取四条、模板不被截断、来源计数、
计划由头的 kind——不是「默认开关」。默认开关本身的行为由
`test_v2248_self_narration_off.py` 钉住（那一条才是产品契约）。

用法：

    class XxxTest(unittest.TestCase):
        def setUp(self):
            self._sn = _self_narration.enable()

        def tearDown(self):
            _self_narration.restore(self._sn)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from social import anchors  # noqa: E402


def enable() -> bool:
    """打开开关，返回原值（交给 `restore` 还原）。"""
    was = anchors.SELF_NARRATION_ENABLED
    anchors.SELF_NARRATION_ENABLED = True
    return was


def restore(was: bool) -> None:
    anchors.SELF_NARRATION_ENABLED = was
