"""v1.27.31：把「她手上还开着的事」发布给 Core（signals schema 1 → 2）。

对话历史一旦被压缩，角色就只剩这条通道记得自己还惦记着什么：`open_items` 里的
loop（对方提了没下文的）和 promise（她自己许下没兑现的）。

这一层最容易坏在两件事上，所以每个都单独钉住：

1. **`at` 必须是「这件事被记下的那一刻」**，不能每轮盖成 now。Core 把它渲染成
   「三天前答应的事」这种相对时间，而这段文本进 system_prompt 的**稳定段**；
   每轮重盖既让语义漂移，又让文本每轮都变，把整段前缀缓存打掉——比不发布更贵。
2. **必须整体覆盖，空列表也要写**。只增不减的话，对方早就给出结果的事会永远挂在
   她提示词里，正是「过程系统锁死」那个老毛病的另一个入口。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_open_items_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.anchors import _derive_from_state  # noqa: E402
from social.core_bridge import CoreBridge  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social.signals import (  # noqa: E402
    MAX_OPEN_ITEMS,
    OPEN_ITEM_TEXT_MAX,
    SignalsWriter,
)
from social.state import SocialState  # noqa: E402
from social.threads import LOOP_LATE_HOURS, PROMISE_WAIT_SECONDS  # noqa: E402
from social.threads import loop_entries as _real_loop_entries  # noqa: E402

NOW = 1_700_000_000.0


# ── signals 侧：规范化与发布 ────────────────────────────────────────
class OpenItemsPublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "humanoid_signals.json")
        self.writer = SignalsWriter(self.path, time_source=lambda: 100.0)

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_schema_bumped_and_existing_fields_untouched(self):
        """只加不改：老版 Core 还在跑，既有字段一个都不能动。"""
        self.writer.note_open_items(
            [{"text": "问对方面试结果", "uid": "u1", "at": 900.0, "kind": "loop"}])
        self.writer.note_open_items([])
        payload = self._read()
        self.assertEqual(payload["schema"], 2)
        # 空列表是**有意义**的：Core 靠它判断「这件事结了没有」
        self.assertEqual(payload["open_items"], [])
        for key in ("last_proactive_at", "last_target_uid", "ignored_streak", "by_bid"):
            self.assertIn(key, payload, "既有字段被这次改动挤掉了")

    def test_single_bot_writes_top_level(self):
        self.writer.note_open_items(
            [{"text": "问对方面试结果", "uid": "u1", "at": 900.0, "kind": "loop"}])
        self.assertEqual([i["text"] for i in self._read()["open_items"]],
                         ["问对方面试结果"])

    def test_multi_bot_keeps_top_level_empty(self):
        """角色数从 1 变 2 之后，顶层那格不会自己消失；留着就会被 B 认成自己的事。"""
        self.writer.set_role_count(2)
        self.writer.note_open_items(
            [{"text": "问对方面试结果", "uid": "u1", "at": 900.0, "kind": "loop"}],
            bid="bot1")
        payload = self._read()
        self.assertEqual(payload["open_items"], [])
        self.assertEqual(
            [i["text"] for i in payload["by_bid"]["bot1"]["open_items"]],
            ["问对方面试结果"])

    def test_text_is_collapsed_and_truncated(self):
        long_text = "问" + "面" * 60
        self.writer.note_open_items(
            [{"text": f" 问\n对方\t面试 {long_text} ", "uid": "u1", "at": 1.0,
              "kind": "loop"}])
        text = self._read()["open_items"][0]["text"]
        self.assertNotIn("\n", text, "换行会把 Core 那边的格式挤坏")
        self.assertNotIn("\t", text)
        self.assertLessEqual(len(text), OPEN_ITEM_TEXT_MAX)

    def test_bad_kind_is_dropped_not_passed_through(self):
        """白名单而不是透传：拼错的 kind 会让 Core 静默丢条，不如在这里就丢掉。"""
        self.writer.note_open_items([
            {"text": "A", "uid": "u1", "at": 3.0, "kind": "loop"},
            {"text": "B", "uid": "u1", "at": 2.0, "kind": "commitment"},
            {"text": "C", "uid": "u1", "at": 1.0, "kind": ""},
        ])
        self.assertEqual([i["text"] for i in self._read()["open_items"]], ["A"])

    def test_malformed_shapes_are_ignored(self):
        self.writer.note_open_items(
            [None, 123, "x", {}, {"text": "", "kind": "loop"},
             {"text": "留下的", "uid": "u1", "at": "坏", "kind": "promise"}])
        items = self._read()["open_items"]
        self.assertEqual([i["text"] for i in items], ["留下的"])
        self.assertEqual(items[0]["at"], 0.0, "坏时间戳该归 0 而不是抛异常")

    def test_non_list_is_treated_as_empty(self):
        self.writer.note_open_items(
            [{"text": "问对方面试结果", "uid": "u1", "at": 900.0, "kind": "loop"}])
        self.writer.note_open_items({"text": "不是列表", "kind": "loop"})
        self.assertEqual(self._read()["open_items"], [])

    def test_capped_at_max(self):
        self.writer.note_open_items([
            {"text": f"第{i}件事", "uid": "u1", "at": float(i), "kind": "loop"}
            for i in range(MAX_OPEN_ITEMS + 4)])
        items = self._read()["open_items"]
        self.assertEqual(len(items), MAX_OPEN_ITEMS)
        # 新的在前，砍掉的是最旧的那几条
        self.assertEqual(items[0]["text"], f"第{MAX_OPEN_ITEMS + 3}件事")

    def test_order_is_deterministic_regardless_of_input_order(self):
        """同一批内容每轮排出同样的顺序，否则文本每轮都变，前缀缓存整段失效。"""
        items = [
            {"text": "B", "uid": "u2", "at": 200.0, "kind": "loop"},
            {"text": "A", "uid": "u1", "at": 200.0, "kind": "loop"},
            {"text": "C", "uid": "u1", "at": 100.0, "kind": "promise"},
        ]
        first = SignalsWriter._normalize_open_items(items)
        shuffled = SignalsWriter._normalize_open_items([items[2], items[0], items[1]])
        self.assertEqual(first, shuffled)

    def test_duplicate_keeps_the_newest(self):
        """同一件事可能从两个来源各来一次（她许的诺也可能被抽成未完话题）。"""
        got = SignalsWriter._normalize_open_items([
            {"text": "问面试结果", "uid": "u1", "at": 100.0, "kind": "loop"},
            {"text": "问面试结果", "uid": "u1", "at": 900.0, "kind": "loop"},
        ])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["at"], 900.0)

    def test_unchanged_content_does_not_rewrite(self):
        """每轮心跳都会调它，内容没变就别白重写整份文件（flush 的 120 秒闸门就是为这个）。"""
        items = [{"text": "问对方面试结果", "uid": "u1", "at": 900.0, "kind": "loop"}]
        calls = []
        self.writer.flush = lambda *a, **k: calls.append(1) or True
        self.writer.note_open_items(items)
        self.writer.note_open_items(list(items))
        self.assertEqual(len(calls), 1, "内容没变却重写了")

    def test_changed_content_rewrites(self):
        calls = []
        self.writer.flush = lambda *a, **k: calls.append(1) or True
        self.writer.note_open_items(
            [{"text": "A", "uid": "u1", "at": 1.0, "kind": "loop"}])
        self.writer.note_open_items(
            [{"text": "B", "uid": "u1", "at": 2.0, "kind": "loop"}])
        self.assertEqual(len(calls), 2)


# ── engine 侧：哪些事算「还开着」 ───────────────────────────────────
class _OpenItemsEngine:
    """只借 `_open_items` 与它真正用到的依赖，不启动引擎。"""

    _open_items = SocialEngine._open_items

    def __init__(self):
        self.state = SocialState(os.path.join(tempfile.mkdtemp(), "state.json"))
        self.logs: list = []

    def log(self, msg):
        self.logs.append(msg)

    def user(self, uid, bid="bot1"):
        bot = self.state.bot(bid)
        return bot.setdefault("users", {}).setdefault(uid, {})


class OpenItemsSourceTest(unittest.TestCase):
    def setUp(self):
        self.engine = _OpenItemsEngine()

    def test_at_is_creation_time_not_now(self):
        """这条是整个契约里最贵的一条：盖成 now 会打掉 Core 的前缀缓存。"""
        u = self.engine.user("u1")
        SocialState._note_loop(u, "问对方面试结果", NOW + 3600.0, NOW)
        first = self.engine._open_items("bot1", NOW + 60.0)
        later = self.engine._open_items("bot1", NOW + 7200.0)
        self.assertEqual(first[0]["at"], NOW)
        self.assertEqual(later[0]["at"], NOW, "`at` 被重新盖成了 now")

    def test_loop_not_yet_due_is_still_open(self):
        """三小时后才到点回访的事，现在照样是开着的——历史被压缩后只剩这条通道记得。"""
        u = self.engine.user("u1")
        SocialState._note_loop(u, "问对方面试结果", NOW + 3 * 3600.0, NOW)
        got = self.engine._open_items("bot1", NOW)
        self.assertEqual([(i["kind"], i["text"]) for i in got],
                         [("loop", "问对方面试结果")])

    def test_expired_loop_is_dropped(self):
        """凉过头的事不该挂在她手上（与 live_loops 同一个兜底口径）。"""
        u = self.engine.user("u1")
        due = NOW - (LOOP_LATE_HOURS + 10.0) * 3600.0
        SocialState._note_loop(u, "问对方面试结果", due, NOW - 200 * 3600.0)
        self.assertEqual(self.engine._open_items("bot1", NOW), [])

    def test_loop_without_created_at_is_skipped(self):
        """不知道什么时候记下的：宁可不发，也不能编一个时间戳。"""
        u = self.engine.user("u1")
        u["loops"] = [{"about": "问对方面试结果", "due": NOW + 3600.0, "at": 0}]
        self.assertEqual(self.engine._open_items("bot1", NOW), [])

    def test_promise_published_until_fulfilled(self):
        u = self.engine.user("u1")
        SocialState.note_promise(u, "帮你查那本书", NOW)
        got = self.engine._open_items("bot1", NOW + 600.0)
        self.assertEqual([(i["kind"], i["text"], i["at"]) for i in got],
                         [("promise", "帮你查那本书", NOW)])
        # 兑现路径（engine 发送段）就是把那条从 promises 里删掉
        u["promises"] = [i for i in u["promises"] if i.get("about") != "帮你查那本书"]
        self.assertEqual(self.engine._open_items("bot1", NOW + 900.0), [])

    def test_legacy_promise_without_at_derives_from_due(self):
        """老数据只有 due：按写侧公式反推，而不是拿 0 当 1970 年发过去。"""
        u = self.engine.user("u1")
        u["promises"] = [{"about": "旧承诺", "due": NOW + PROMISE_WAIT_SECONDS}]
        got = self.engine._open_items("bot1", NOW)
        self.assertEqual(got[0]["at"], NOW)

    def test_uid_is_a_string(self):
        u = self.engine.user("u1")
        SocialState._note_loop(u, "问对方面试结果", NOW + 3600.0, NOW)
        self.assertIsInstance(self.engine._open_items("bot1", NOW)[0]["uid"], str)

    def test_one_dirty_record_does_not_kill_the_round(self):
        """一条脏记录只该让这个用户这轮不上报，不该连累整轮心跳的信号写回。"""
        good = self.engine.user("u2")
        SocialState._note_loop(good, "问对方面试结果", NOW + 3600.0, NOW)
        bad = self.engine.user("u1")  # 存在，但读它会炸

        def boom(u):
            if u is bad:
                raise RuntimeError("坏记录")
            return _real_loop_entries(u)

        with patch("social.engine.loop_entries", boom), \
                patch("social.engine.promise_entries", lambda u: []):
            got = self.engine._open_items("bot1", NOW)
        self.assertEqual([i["uid"] for i in got], ["u2"])
        self.assertTrue(self.engine.logs, "吞掉了异常却没留日志")

    def test_end_to_end_into_signals(self):
        """engine 算出什么，Core 那边就能原样读到什么。"""
        u = self.engine.user("u1")
        SocialState._note_loop(u, "问对方面试结果", NOW + 3600.0, NOW)
        SocialState.note_promise(u, "帮你查那本书", NOW - 60.0)
        path = os.path.join(tempfile.mkdtemp(), "humanoid_signals.json")
        writer = SignalsWriter(path, time_source=lambda: 100.0)
        writer.note_open_items(self.engine._open_items("bot1", NOW))
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual([i["text"] for i in payload["open_items"]],
                         ["问对方面试结果", "帮你查那本书"])
        self.assertEqual([i["at"] for i in payload["open_items"]],
                         [NOW, NOW - 60.0])


# ── core_bridge 侧：这条通道已撤（审计发现它从来没通过，见 CHANGELOG v1.27.31） ──
class LifeLogChannelRemovedTest(unittest.TestCase):
    """原来这里有一组 `CoreBridge._life_log_items([...])` 用例，手工喂 `kind == "did"`。

    它们能过，只因为测试自己造了 Core **永远写不出来**的数据：Core 侧 `life_log.py` 只存
    open 条目，而且文件里明确写着「社交层带的 `kind` 不保留」。也就是说那条通道在任何
    已发布版本里都恒为空表，这组用例给的是一份假的信心。现在连读取器一起删掉，
    并在这里钉住「它真的没了」。
    """

    def test_the_dead_reader_is_gone(self):
        self.assertFalse(hasattr(CoreBridge, "_life_log_items"))

    def test_the_cap_constant_is_gone(self):
        import social.core_bridge as cb

        self.assertFalse(hasattr(cb, "CORE_LIFE_LOG_MAX"))

    def test_view_no_longer_carries_a_life_log(self):
        bridge = CoreBridge("bridge", None)
        view = bridge._build_self_view(
            {"life_log": [{"kind": "did", "text": "把那份笔记整理完了", "at": 100.0}]},
            "bot1")
        self.assertNotIn("life_log", view)

    def test_view_still_builds_without_a_contract(self):
        """删的是那一段，不是「没有契约也要给出自身视图」这件事本身。"""
        bridge = CoreBridge("bridge", None)
        view = bridge._build_self_view({}, "bot1")
        self.assertIn("ongoing", view)
        self.assertIn("holiday", view)

    def test_anchors_never_see_it_through_a_real_view(self):
        """端到端：即使 Core 那边真有 `did` 条目，也到不了由头池了。"""
        bridge = CoreBridge("bridge", None)
        view = bridge._build_self_view(
            {"life_log": [{"kind": "did", "text": "把那份笔记整理完了",
                           "at": NOW - 60.0}]},
            "bot1")
        got = _derive_from_state(view, {"said": []}, NOW, set())
        self.assertFalse(any("刚做完" in x for x in got), got)


# ── anchors 侧：原来的 `LifeLogBecomesAnchorTest` 也一并删了 ──────────
#
# 那组用例直接给 `_derive_from_state` 喂 `body(life_log=[{"kind": "did", ...}])`，
# 于是绕过 core_bridge、断言「刚做完X」能派生出来。但真实链路上 `body` 就是
# `CoreBridge._build_self_view()` 的产物，而它已经不（也从来没有）带 `life_log`
# 这个键——所以那组用例测的是一条端到端不可能出现的路径。
# 端到端的真实结论在 `LifeLogChannelRemovedTest.test_anchors_never_see_it_through_a_real_view`。
#
# `anchors._life_log_done` 本身还在（`social/anchors.py:736`），但它现在恒返回空表：
# 唯一的调用点 `anchors.py:596` 拿不到 `life_log`。anchors.py 不属本次审计范围，
# 只在报告里列出，由该文件的负责人决定是否删掉这个死分支。


if __name__ == "__main__":
    unittest.main()
