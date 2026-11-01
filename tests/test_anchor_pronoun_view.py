"""Validate source roles in the rendered prompt without changing stored anchor keys."""

import unittest

from social import anchors
from tests import test_autonomous_social as fixtures

NOW = 1_700_000_000.0
RAW = "我明天面试你帮我看看"
VIEW = "其中“我”指你自己、“你”指TA"


class AnchorPronounView(unittest.TestCase):
    def test_relational_anchor_explains_both_injection_paths(self):
        u = {"conversation": [{"dir": "in", "text": RAW}], "last_seen": NOW - 3600}
        a = anchors.prepare_round_anchors(u, {}, NOW, relational=True, affection=0.0)[0]
        reason = anchors.anchor_sentence(a)
        target = dict(u, umo="aiocqhttp:FriendMessage:verify", last_message=RAW)
        for meta in (anchors.anchor_meta(a), {}):
            with self.subTest(metadata=bool(meta)):
                prompt = fixtures.TestNoScriptedContent()._compose(reason, meta, target)
                self.assertIn(VIEW, prompt)
                self.assertIn("转述TA原话的部分仍按TA视角理解", prompt)
                self.assertIn(f"你上次说的「{RAW}」后来呢", prompt)
                self.assertNotIn("你最近一直在想TA：你上次说", prompt)

    def test_cue_keeps_the_peer_as_original_speaker(self):
        a = {"kind": anchors.KIND_CUE, "about": "我明天面试"}
        text = anchors.anchor_sentence(a)
        self.assertIn("TA之前的原话", text)
        self.assertIn("我明天面试", text)
        self.assertNotIn(VIEW, text)

    def test_rendering_keeps_existing_anchor_keys_and_consumption(self):
        for kind, about in (
            (anchors.KIND_DERIVED, "想起你之前说的我明天面试"),
            (anchors.KIND_MISS, "有点想你了"),
            (anchors.KIND_GREET, "你好久没理我了"),
        ):
            with self.subTest(kind=kind):
                u = {}
                a = anchors.add_anchor(u, kind, about, now=NOW)
                meta = anchors.anchor_meta(a)
                self.assertIn(VIEW, meta["anchor_fact"])
                self.assertEqual(a["about"], about)
                self.assertEqual(meta["about"], about)
                anchors.consume(u, about, NOW + 1)
                self.assertIn(about, anchors._remembered(u, NOW + 2))


if __name__ == "__main__":
    unittest.main()
