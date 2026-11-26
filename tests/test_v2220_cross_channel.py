# -*- coding: utf-8 -*-
"""Ensure group and private-message content stays audience-isolated."""
import io
import json
import os
import unittest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AudienceIsolationTest(unittest.TestCase):
    def test_runtime_does_not_compare_group_and_private_message_text(self):
        with io.open(os.path.join(PLUGIN_ROOT, "social", "engine.py"), encoding="utf-8") as f:
            engine = f.read()
        with io.open(os.path.join(PLUGIN_ROOT, "social", "state.py"), encoding="utf-8") as f:
            state = f.read()
        with io.open(os.path.join(PLUGIN_ROOT, "_conf_schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
        with io.open(os.path.join(PLUGIN_ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertNotIn("recent_own_group_texts", engine + state)
        self.assertNotIn("recent_proactive_all", engine + state)
        self.assertNotIn("cross_channel_dedup_hours", schema)
        self.assertNotIn("cross_channel_dedup_hours", readme)
        self.assertIn("群消息与私聊消息不会互相读取或比较", readme)


if __name__ == "__main__":
    unittest.main(verbosity=2)
