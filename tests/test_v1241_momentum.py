"""主动性也有惯性：聊开了会连着说，冷了会慢下来。

以前只有「被冷落」这一个方向——连着被无视之后会退。缺的是另一边：真人一旦热起来
会连着说一阵子，而她以前是发完一条、下次照样等满一个周期，于是
「连发三条然后消失三天」这种最不像人的节奏照样出得来。
"""
import unittest

from social.desire import MOMENTUM_MAX, MOMENTUM_WINDOW_HOURS, momentum_factor, settle

NOW = 1_700_000_000.0


class MomentumCurve(unittest.TestCase):
    def test_strongest_right_after_speaking(self):
        self.assertAlmostEqual(
            momentum_factor({"last_spoken": NOW}, NOW), MOMENTUM_MAX, places=3)

    def test_decays_within_the_window(self):
        a = momentum_factor({"last_spoken": NOW - 2 * 3600}, NOW)
        b = momentum_factor({"last_spoken": NOW - 10 * 3600}, NOW)
        self.assertGreater(a, b, "势头该随时间淡掉")

    def test_gone_after_the_window(self):
        for h in (MOMENTUM_WINDOW_HOURS + 1, 36, 72):
            self.assertEqual(
                momentum_factor({"last_spoken": NOW - h * 3600}, NOW), 1.0,
                f"{h} 小时后不该还有势头")

    def test_never_above_the_cap(self):
        for h in (0, 1, 2, 5):
            self.assertLessEqual(
                momentum_factor({"last_spoken": NOW - h * 3600}, NOW), MOMENTUM_MAX)

    def test_missing_or_broken_input_is_neutral(self):
        for u in ({}, {"last_spoken": 0}, {"last_spoken": None},
                  {"last_spoken": "x"}, {"last_spoken": NOW + 99999}):
            self.assertEqual(momentum_factor(u, NOW), 1.0, u)


class MomentumInSettle(unittest.TestCase):
    def _urge_after(self, hours_since_spoke, hours_of_urge, **kw):
        u = {"urge": 0.0, "urge_at": NOW - hours_of_urge * 3600,
             "last_seen": 0.0, "interest": 0.6, "last_spoken": NOW - hours_since_spoke * 3600}
        u.update(kw)
        return settle(u, NOW, refill_hours=4.0, recent_talk_seconds=0.0)

    def test_faster_when_just_talked(self):
        hot = self._urge_after(0.5, 6.0)
        cold = self._urge_after(40.0, 6.0)
        self.assertGreater(hot, cold, "刚聊开应该攒得更快")

    def test_never_hurts(self):
        """惯性只能加快，不能拖慢——它补的是缺的那一维。"""
        for h in (0, 3, 8, 20):
            self.assertGreaterEqual(self._urge_after(h, 6.0), self._urge_after(60.0, 6.0))

    def test_exactly_equal_when_no_history(self):
        a = self._urge_after(0.5, 6.0, last_spoken=0.0)
        b = self._urge_after(60.0, 6.0, last_spoken=0.0)
        self.assertAlmostEqual(a, b, places=6)


if __name__ == "__main__":
    unittest.main()
