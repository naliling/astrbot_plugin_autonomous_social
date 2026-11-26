from __future__ import annotations

import inspect
import unittest

from social.engine import SocialEngine
from social.generator import MessageGenerator


class ProactiveGrowthWiringTest(unittest.TestCase):
    def test_real_private_send_and_preview_set_user_scoped_marker(self):
        for method in (SocialEngine._speak_inner, SocialEngine._preview_one):
            source = inspect.getsource(method)
            self.assertIn('_proactive_growth_context"] = True', source)

    def test_short_return_context_is_excluded_for_intimate_generation(self):
        source = inspect.getsource(MessageGenerator._compose)
        self.assertIn('if not intimate:', source)
        self.assertIn('_short_return_context_line(target, ref)', source)
