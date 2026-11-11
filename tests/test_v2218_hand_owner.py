# -*- coding: utf-8 -*-
"""v2.2.18：她说的「手」到底是谁的手。

全历史回放（scripts/replay_full.py，2379 条她自己的话）发现：蹄类角色的物种闸
把**对方**的手也算成她的，于是把正常说法改坏：
  「尾巴尾梢绕上你手腕」→「你蹄腕」
  「蹭了蹭你的手心」    →「你的蹄心」
  「汝手凉」            →「汝蹄子凉」
  「找个有手指的干」    →「找个有蹄尖的干」
"""
import logging
import os
import sys
import types
import unittest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)


def _stub():
    if "astrbot" in sys.modules:
        return
    a = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("test_v2218")
    ev = types.ModuleType("astrbot.api.event")
    ev.MessageChain = object
    st = types.ModuleType("astrbot.api.star")

    class _Ctx(object):
        pass

    class _Star(object):
        def __init__(self, *a, **k):
            pass

    st.Context = _Ctx
    st.Star = _Star
    a.api = api
    api.event = ev
    api.star = st
    sys.modules.update({
        "astrbot": a, "astrbot.api": api,
        "astrbot.api.event": ev, "astrbot.api.star": st,
    })


_stub()
from social import verify as V  # noqa: E402

HOOF = "天马"


class OtherHandTest(unittest.TestCase):
    """对方的手：不改写、不拦。"""

    def test_partner_wrist_untouched(self):
        got = V.tidy_outgoing_text("尾巴尾梢绕上你手腕。", species=HOOF)
        self.assertIn("你手腕", got)
        self.assertNotIn("蹄子", got)

    def test_partner_palm_untouched(self):
        got = V.tidy_outgoing_text("我蹭了蹭你的手心", species=HOOF)
        self.assertIn("你的手心", got)

    def test_partner_arm_untouched(self):
        got = V.tidy_outgoing_text("触手顺着你的手臂攀爬", species=HOOF)
        self.assertIn("你的手臂", got)

    def test_classical_you_untouched(self):
        got = V.tidy_outgoing_text("汝手凉，本宫要睡了", species=HOOF)
        self.assertIn("汝手凉", got)

    def test_partner_hand_not_blocked(self):
        ok, why = V.verify_message("蹄子还搭在你手背上", species=HOOF)
        self.assertTrue(ok, why)

    def test_meta_fingers_not_blocked(self):
        ok, why = V.verify_message("这活儿是不是该找个有手指的干", species=HOOF)
        self.assertTrue(ok, why)


class SelfHandTest(unittest.TestCase):
    """她自己的手：照旧要改、要拦。"""

    def test_own_hand_rewritten(self):
        got = V.tidy_outgoing_text("我刚醒手还有点僵", species=HOOF)
        self.assertIn("蹄子", got)
        self.assertNotIn("手还", got)

    def test_own_hand_blocked(self):
        ok, why = V.verify_message("我刚醒手还有点僵", species=HOOF)
        self.assertFalse(ok)
        self.assertIn("身体不一致", why or "")

    def test_self_pronoun_counts(self):
        self.assertEqual(V._hand_owner("把我手弄疼了", 2), "self")

    def test_partner_pronoun_wins_when_closer(self):
        self.assertEqual(V._hand_owner("我蹭了蹭你的手心", 6), "other")


class MixedSentenceTest(unittest.TestCase):
    def test_own_double_hands_and_partner_wrist(self):
        got = V.tidy_outgoing_text("(抬起双手扶住你的手腕) 别乱动", species=HOOF)
        self.assertIn("两只蹄子", got)
        self.assertIn("你的手腕", got)

    def test_unknown_owner_treated_as_self(self):
        got = V.tidy_outgoing_text("手心划破了", species=HOOF)
        self.assertIn("蹄心", got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
