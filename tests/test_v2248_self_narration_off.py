"""v2.2.48：主动开口的由头不再来自「她今天在干什么」。

用户原话（2026-11-14）：「你去看日志 发言很严重 这是谁造成的？要么删掉要么成背景
别影响到所有角色！」「发言看起来很傻！」

线上 5 天分布 `{derived: 839, miss: 250, greet: 109, weather: 1}`——839 条里绝大多数
是 `day.doing` / `day.done` / `day.next` / `day.wants` / `ongoing` 派生出来的
「我刚把碗洗好」「还在整理衣柜」。模型照着念，主动消息就成了日程播报，验收再反复
判「自说自话 / 没有指向对方」。

v2.2.48 起这五个来源默认不再产出由头（`anchors.SELF_NARRATION_ENABLED = False`）：
没有别的由头，这一轮就不开口——`engine.py:7613` 的 `if not picks: return []` 本来就是
这个语义（「没什么可说的就别说话」）。开关留在代码里，改回 True 即可恢复。
"""
import unittest

from social import anchors

NOW = 1_700_000_000.0

# 「她今天在干什么」的五个来源，全塞进一个 body。
SELF_LIFE_BODY = {
    "day": {
        "doing": "整理个案笔记",
        "done": ["门诊接待", "洗了碗"],
        "next": ["要睡了"],
        "wants": ["吃火锅"],
    },
    "ongoing": ["练一首新曲子"],
}


class SelfNarrationIsOffTest(unittest.TestCase):
    """默认：她「在做什么 / 刚做完 / 接下来 / 想要 / 没做完」都不再由头。"""

    def test_the_switch_is_off(self):
        self.assertFalse(anchors.SELF_NARRATION_ENABLED)

    def test_doing_done_next_wants_ongoing_produce_nothing(self):
        kinds = {}
        texts = anchors._derive_from_state(SELF_LIFE_BODY, {}, NOW, set(), kinds=kinds)
        self.assertEqual(texts, [], f"这些由头又回来了：{texts}")
        self.assertEqual(kinds, {})

    def test_only_self_life_means_no_round_anchors(self):
        """只剩「她自己的日子」时，这一轮不该进队——不开口比播报日程好。"""
        picks = anchors.prepare_round_anchors({}, SELF_LIFE_BODY, NOW, limit=3)
        self.assertEqual(picks, [])

    def test_vague_busy_is_gone_too(self):
        """「忙手里的活」这种空话本来会被 `is_vague_doing` 滤掉，现在连派生都不进。"""
        body = {"day": {"doing": "忙手里的活"}}
        self.assertEqual(anchors._derive_from_state(body, {}, NOW, set()), [])


class OtherSourcesSurviveTest(unittest.TestCase):
    """别的来源是「她真有话说」的凭据，一条都不能跟着掉。"""

    def test_dreams_still_produce(self):
        body = {"day": {"dreams": ["海边捡到一只玻璃瓶"]}}
        texts = anchors._derive_from_state(body, {}, NOW, set())
        self.assertTrue(any(t.startswith("梦到") for t in texts), texts)

    def test_event_still_produces(self):
        body = {"event": {"phrase": "被人夸了一句"}}
        self.assertIn("被人夸了一句", anchors._derive_from_state(body, {}, NOW, set()))

    def test_wake_still_produces(self):
        body = {"day": {"wake": {"asleep": False, "wake_at": NOW - 600,
                                 "last_sleep_hours": 7.5}}}
        texts = anchors._derive_from_state(body, {}, NOW, set())
        self.assertTrue(texts, "刚醒是一条正当的开口理由")

    def test_body_cue_still_produces(self):
        body = {"hunger": 88, "sleep_pressure": 90}
        kinds = {}
        texts = anchors._derive_from_state(body, {}, NOW, set(), kinds=kinds)
        self.assertTrue(texts, texts)
        self.assertTrue(any(k == anchors.KIND_BODY for k in kinds.values()), kinds)

    def test_weather_still_produces(self):
        """天气走 `derive_extras`（同一道闸），不在 `_derive_from_state` 里。"""
        body = {"weather": {"env": "外面下着大雨"}}
        extras = anchors.derive_extras({}, body, NOW, set())
        self.assertTrue(any(text == "外面下着大雨" for _kind, text in extras), extras)

    def test_holiday_still_produces(self):
        extras = anchors.derive_extras({}, {"holiday": "中秋"}, NOW, set())
        self.assertIn(("holiday", "今天是中秋"), extras)

    def test_silence_tier_still_produces(self):
        u = {"last_seen": NOW - 8 * 86400}
        texts = anchors._derive_from_state({}, u, NOW, set(), last_talk=NOW - 8 * 86400)
        self.assertIn("很久没见了", texts)

    def test_self_life_plus_a_dream_still_opens(self):
        """软由头没了，但只要有硬由头，这一轮照常开口。"""
        body = dict(SELF_LIFE_BODY)
        body["day"] = dict(SELF_LIFE_BODY["day"], dreams=["梦见下雪"])
        picks = anchors.prepare_round_anchors({}, body, NOW, limit=3)
        self.assertTrue(picks, "有梦就该开口")


class PurgeStaleSelfNarrationTest(unittest.TestCase):
    """存量池里已经派生好的，启动时清掉——不然开关要等两小时 TTL 才真的生效。"""

    def _bot_state(self):
        return {
            "users": {
                "u1": {"anchors": [
                    {"kind": "derived", "about": "正在整理衣柜"},
                    {"kind": "plan", "about": "接下来是要睡了"},
                    {"kind": "dream", "about": "梦到海边"},
                    {"kind": "miss", "about": "很久没见了"},
                ]},
                "u2": {"anchors": [{"kind": "derived", "about": "还在练一首新曲子"}]},
                "u3": {},
            }
        }

    def test_stale_self_narration_anchors_are_dropped_once(self):
        bot = self._bot_state()
        self.assertEqual(anchors.purge_self_narration_anchors(bot), 3)
        self.assertEqual(
            [a["about"] for a in bot["users"]["u1"]["anchors"]],
            ["梦到海边", "很久没见了"],
        )
        self.assertEqual(bot["users"]["u2"]["anchors"], [])
        # 标记已写：第二次一条都不动
        self.assertEqual(anchors.purge_self_narration_anchors(bot), 0)

    def test_bad_shapes_do_not_crash(self):
        self.assertEqual(anchors.purge_self_narration_anchors({}), 0)
        self.assertEqual(anchors.purge_self_narration_anchors({"users": {"x": "bad"}}), 0)
        self.assertEqual(anchors.purge_self_narration_anchors({"users": {"x": {"anchors": "no"}}}), 0)


class SwitchCanBeTurnedBackTest(unittest.TestCase):
    """开关是可逆的：改回 True，旧行为原样回来（别把代码删成不可恢复）。"""

    def setUp(self):
        self._was = anchors.SELF_NARRATION_ENABLED
        anchors.SELF_NARRATION_ENABLED = True

    def tearDown(self):
        anchors.SELF_NARRATION_ENABLED = self._was

    def test_doing_comes_back(self):
        body = {"day": {"doing": "整理个案笔记"}}
        texts = anchors._derive_from_state(body, {}, NOW, set())
        self.assertTrue(any("整理个案笔记" in t for t in texts), texts)


if __name__ == "__main__":
    unittest.main()
