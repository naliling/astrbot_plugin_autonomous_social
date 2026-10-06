"""v1.27.15：社交侧这一批（A1/L2 由头、T1 忙/敷衍、T2 回访结果、K1 破冰素材、G1 起话头、G3 点名）。

都是纯逻辑，不依赖 AstrBot 运行时，直接 import 对应模块跑。
"""
from __future__ import annotations

import unittest

from social import anchors, groupflow, reasoning, threads

NOW = 1_700_000_000.0


class NewAnchorKinds(unittest.TestCase):
    """A1/L2：节日与天气两类由头。"""

    def test_holiday_becomes_an_anchor(self):
        picks = anchors.prepare_round_anchors({}, {"holiday": "中秋节"}, NOW, limit=5)
        self.assertTrue(any(a.get("kind") == anchors.KIND_HOLIDAY for a in picks), picks)
        self.assertTrue(any("中秋" in str(a.get("about")) for a in picks), picks)

    def test_notable_weather_becomes_an_anchor(self):
        picks = anchors.prepare_round_anchors(
            {}, {"weather": "中雨，气温 9℃"}, NOW, limit=5)
        self.assertTrue(any(a.get("kind") == anchors.KIND_WEATHER for a in picks), picks)

    def test_plain_weather_is_not_an_anchor(self):
        """多云、气温 20℃ 这种太平常，拿它当由头等于没话找话。"""
        picks = anchors.prepare_round_anchors(
            {}, {"weather": "多云，气温 20℃"}, NOW, limit=5)
        self.assertFalse(any(a.get("kind") == anchors.KIND_WEATHER for a in picks), picks)

    def test_cold_snap_counts(self):
        self.assertTrue(anchors._notable_weather({"weather": "晴，气温 -3℃"}))

    def test_relational_channel_gets_no_extras(self):
        """念想通道说的必须是关于 TA 的，插一句「今天是中秋」就跑题。"""
        picks = anchors.prepare_round_anchors(
            {}, {"holiday": "中秋节"}, NOW, limit=5, relational=True, affection=80.0)
        self.assertFalse(any(a.get("kind") == anchors.KIND_HOLIDAY for a in picks), picks)


class BusyVsDismissive(unittest.TestCase):
    """T1：真忙与敷衍要分开——「在忙」之后回一个「嗯」不是敷衍。"""

    def _u(self, texts):
        return {"conversation": [
            {"dir": "in", "text": t, "ts": NOW - 60 * (len(texts) - i)}
            for i, t in enumerate(texts)
        ]}

    def test_recent_busy_is_detected(self):
        self.assertTrue(reasoning.busy_signal(self._u(["我在开会，回头说"]), NOW))

    def test_terse_after_busy_still_counts_as_busy(self):
        self.assertTrue(reasoning.busy_signal(self._u(["在忙", "嗯"]), NOW))

    def test_not_busy_is_not_busy(self):
        self.assertFalse(reasoning.busy_signal(self._u(["我不忙", "怎么了"]), NOW))

    def test_old_busy_expires(self):
        u = {"conversation": [{"dir": "in", "text": "在忙", "ts": NOW - 5 * 3600}]}
        self.assertFalse(reasoning.busy_signal(u, NOW))

    def test_thread_reason_suppressed_when_busy(self):
        """真忙时不再追问。"""
        u = {
            "last_seen": NOW - 300,
            "last_spoken": NOW - 200,
            "last_message": "嗯",
            "conversation": [
                {"dir": "in", "text": "在忙，回头说", "ts": NOW - 240},
                {"dir": "in", "text": "嗯", "ts": NOW - 300},
            ],
        }
        kind, _ = threads.thread_reason(
            u, NOW, probe_after_seconds=60, presence_after_seconds=300,
            max_seconds=3600, context_seconds=3600)
        self.assertEqual(kind, "", "对方说了在忙，不该再追问")


class LoopResolved(unittest.TestCase):
    """T2：对方给了结果，那件就不必再回访。"""

    def test_result_closes_the_loop(self):
        u = {"loops": [{"about": "上次面试那事", "due": NOW - 10}]}
        got = threads.loops_resolved_by(u, "面试过了，还行")
        self.assertEqual(got, ["上次面试那事"])

    def test_unrelated_message_keeps_it(self):
        u = {"loops": [{"about": "上次面试那事", "due": NOW - 10}]}
        self.assertEqual(threads.loops_resolved_by(u, "今天天气不错"), [])

    def test_no_loop_noop(self):
        self.assertEqual(threads.loops_resolved_by({}, "面试过了"), [])


class IcebreakTopics(unittest.TestCase):
    """K1：破冰可用的现成素材。"""

    def test_holiday_and_weather(self):
        text = groupflow.icebreak_topics("中秋节", "中雨，气温 9℃")
        self.assertIn("中秋", text)
        self.assertIn("中雨", text)

    def test_empty_is_empty(self):
        self.assertEqual(groupflow.icebreak_topics("", ""), "")


class MemberNames(unittest.TestCase):
    """G3：可点名的群友（去重、最新在前、跳过自己）。"""

    def test_collects_recent_unique(self):
        samples = [
            {"name": "小明", "text": "a"},
            {"name": "小红", "text": "b"},
            {"name": "小明", "text": "c"},
            {"self": True, "name": "她", "text": "d"},
        ]
        self.assertEqual(groupflow.member_names(samples), ["小明", "小红"])


class GroupTopicGate(unittest.TestCase):
    """G1：主动起话头的闸门。"""

    def _g(self, **kw):
        base = {
            "umo": "g", "msg_count": 20, "last_seen": NOW - 60,
            "last_bot_spoke": NOW - 9 * 3600, "icebreak_at": NOW - 9 * 3600,
            "topic_day": "", "topic_count_day": 0, "blocked_until": 0.0,
        }
        base.update(kw)
        return base

    def _due(self, g, **kw):
        args = dict(gap_hours=8.0, daily_cap=1, stale_days=3,
                    today="2026-10-06", is_quiet=False)
        args.update(kw)
        return groupflow.group_topic_due(g, NOW, **args)

    def test_due_after_quiet_gap(self):
        self.assertTrue(self._due(self._g()))

    def test_not_due_when_she_spoke_recently(self):
        self.assertFalse(self._due(self._g(last_bot_spoke=NOW - 3600)))

    def test_not_due_when_quiet_hours(self):
        self.assertFalse(self._due(self._g(), is_quiet=True))

    def test_not_due_when_daily_cap_reached(self):
        self.assertFalse(self._due(self._g(topic_day="2026-10-06", topic_count_day=1)))

    def test_not_due_when_no_history(self):
        self.assertFalse(self._due(self._g(msg_count=1)))


if __name__ == "__main__":
    unittest.main()
