"""由头多三个来源：还没做完的事 / 想起 TA 说的话 / 今天特别空。

之前只有四个来源（手上在做的事 / 接下来那件 / 天气 / 沉默时长），
于是「她今天没事做」= 彻底安静。而真人闲下来恰恰会冒一句——
**没事可说是真实的开口理由**，不是没话说。
"""
import time
import unittest

from social.anchors import _derive_from_state

NOW = 1_700_000_000.0


def body(**kw):
    base = {"day": {"doing": "整理个案笔记", "done": ["门诊接待"], "next": ["要睡了"]},
            "weather": "外面在下雨"}
    base.update(kw)
    return base


class OngoingBecomesAnAnchor(unittest.TestCase):
    """Core 那边的跨天任务是她能说的最好的一件事。"""

    def test_used_as_anchor(self):
        got = _derive_from_state(
            body(ongoing=["整理那份个案笔记（还差 2 份）"]), {"said": []}, NOW, set())
        self.assertTrue(any("整理那份个案笔记" in x for x in got), got)

    def test_works_with_alternate_key(self):
        got = _derive_from_state(
            body(ongoing_tasks=["写完那封信"]), {"said": []}, NOW, set())
        self.assertTrue(any("写完那封信" in x for x in got), got)

    def test_absent_when_core_not_installed(self):
        """接不到就不能编一个出来。"""
        got = _derive_from_state(body(), {"said": []}, NOW, set())
        self.assertFalse(any("还没做完" in x or "还在" in x for x in got), got)

    def test_malformed_is_ignored(self):
        got = _derive_from_state(
            body(ongoing=[None, 123, {"nope": 1}, ""]), {"said": []}, NOW, set())
        self.assertFalse(any("还在" in x for x in got), got)


class RememberingWhatTaSaid(unittest.TestCase):
    """「诶你上次说的那个」——真人主动开口最常见的一种。"""

    def test_recent_said_becomes_anchor(self):
        u = {"said": [{"said": "想去那家店看看", "ts": NOW - 3 * 86400}]}
        got = _derive_from_state(body(), u, NOW, set())
        self.assertTrue(any("想去那家店看看" in x for x in got), got)

    def test_too_old_is_skipped(self):
        """太久了说出来不像「刚想起」。"""
        u = {"said": [{"said": "想去那家店看看", "ts": NOW - 60 * 86400}]}
        got = _derive_from_state(body(), u, NOW, set())
        self.assertFalse(any("想去那家店看看" in x for x in got), got)

    def test_malformed_entries_ignored(self):
        u = {"said": [None, "字符串", {"said": ""}, {"said": "x" * 200}]}
        got = _derive_from_state(body(), u, NOW, set())
        self.assertFalse(any("想起你之前说的" in x for x in got), got)


class SpareTimeIsAnAnchor(unittest.TestCase):
    def test_idle_counts_as_a_reason(self):
        got = _derive_from_state(body(feelings={"spare": 0.8}), {"said": []}, NOW, set())
        self.assertTrue(any("今天挺空的" in x for x in got), got)

    def test_busy_does_not(self):
        got = _derive_from_state(body(feelings={"spare": 0.2}), {"said": []}, NOW, set())
        self.assertFalse(any("今天挺空的" in x for x in got), got)

    def test_missing_or_bad_is_safe(self):
        for feelings in (None, {}, {"spare": "x"}, {"spare": None}):
            got = _derive_from_state(body(feelings=feelings), {"said": []}, NOW, set())
            self.assertFalse(any("今天挺空的" in x for x in got))


class WantsIsAnAnchor(unittest.TestCase):
    """她今天**自己想做**的事——最好的一类由头。"""

    def test_used(self):
        got = _derive_from_state(
            body(), {"said": []}, NOW, set())
        self.assertTrue(got)
        # 换一种：带 wants 的 day
        b = body()
        b["day"]["wants"] = ["拐去那家书店"]
        got = _derive_from_state(b, {"said": []}, NOW, set())
        self.assertIn("她今天拐去那家书店", got)

    def test_no_double_prefix(self):
        b = body()
        b["day"]["wants"] = ["想去看那个展", "看那部老电影", "喝杯东西"]
        got = _derive_from_state(b, {"said": []}, NOW, set())
        for line in got:
            self.assertNotIn("想去去", line, f"前缀拼重了：{line}")
            self.assertNotIn("想去想去", line, f"前缀拼重了：{line}")

    def test_absent_when_none(self):
        got = _derive_from_state(body(), {"said": []}, NOW, set())
        self.assertFalse(any("她今天想" in x for x in got))

    def test_malformed_is_ignored(self):
        b = body()
        b["day"]["wants"] = "不是列表"
        got = _derive_from_state(b, {"said": []}, NOW, set())
        self.assertFalse(any("她今天想" in x for x in got))


class RealContractShapes(unittest.TestCase):
    """必须按**契约真实给的形状**写，而不是按自己以为的形状。

    这一条是被真 bug 逼出来的：`feelings` 在契约里一直是**列表**
    （`[{"text":…, "weight":…}]`），我按字典写，于是那一行一执行就抛
    AttributeError，而调用方的 except 把它吞成空列表——
    **整条由头派生路径静默死掉**（实测 222 次调用 0 产出，7 天送达从 127 掉到 8）。
    单测全绿、仿真也没报错，只有跑量才看得出来。
    """

    def test_feelings_as_list(self):
        b = body()
        b["feelings"] = [{"text": "她现在很闲", "weight": 0.8}]
        got = _derive_from_state(b, {"said": []}, NOW, set())
        self.assertIn("今天挺空的", got, "列表形态的 feelings 没认出来")

    def test_feelings_as_dict(self):
        b = body()
        b["feelings"] = {"spare": 0.8}
        self.assertIn("今天挺空的", _derive_from_state(b, {"said": []}, NOW, set()))

    def test_day_has_no_wants_key(self):
        """`day` 里没有 wants 键是常态（今天没有想做的事），不能因此崩。"""
        self.assertNotIn("wants", body()["day"])
        _derive_from_state(body(), {"said": []}, NOW, set())

    def test_body_none_and_empty(self):
        for b in (None, {}, {"day": None}):
            _derive_from_state(b, {}, NOW, set())

    def test_every_source_survives_a_minimal_body(self):
        """最小契约（有 day 但什么都没发生）不能把整条路打死。"""
        got = _derive_from_state({"day": {"doing": "", "done": [], "next": []}},
                                 {}, NOW, set())
        self.assertIsInstance(got, list)

    def test_never_raises_on_odd_shapes(self):
        for body_ in ({"feelings": "字符串"}, {"feelings": [None, 1]},
                      {"day": {"wants": "不是列表"}}, {"ongoing": {"不是": "列表"}},
                      {"say": [{"said": 123}]}):
            _derive_from_state(body_, {"say": [{"said": 123}]}, NOW, set())


class SourceCountWentUp(unittest.TestCase):
    """加上三个来源之后，一轮能派生的条数应该变多。"""

    def test_more_than_before(self):
        plain = _derive_from_state(body(), {"said": []}, NOW, set())
        rich = _derive_from_state(
            body(ongoing=["整理那份个案笔记"], feelings={"spare": 0.8}),
            {"said": [{"said": "想去那家店", "ts": NOW - 86400}]}, NOW, set())
        self.assertGreater(len(rich), len(plain), f"{len(plain)} → {len(rich)}")
        self.assertGreaterEqual(len(rich), 5)


if __name__ == "__main__":
    unittest.main()
