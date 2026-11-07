"""v2.1.7 审核修复点的回归测试（state.py 名下）。

覆盖审核报告（审核报告_v2.1.6.md）里归到 state.py 的五条：

  P0-01  读盘「被占用」与「内容坏了」必须分开处理：前者不许把 state.json 改名，
         否则用户重启后看到的就是「她把我全忘了」；还要有一条能自己恢复的路径
  P1-01  关掉「保存消息正文」后，`pending_proactive_context` 不许留完整原文
  P2-20  异步写盘：序列化留在事件循环，写盘进工作线程；并发改动不能被当成已落盘
  P2-21  字段级类型损坏在加载期自愈，否则该用户此后每条消息都抛异常且不自愈
  P2-22  写盘前发现文件被外部改动要告警；跨进程锁文件尽力而为（拿不到只降级告警）

每个用例都钉住「原来会怎样」，改回去就会红。按仓库惯例自带最小 astrbot 桩，
tests/ 在插件里或在工作区根两种摆法都能跑。
"""

from __future__ import annotations

import asyncio
import builtins
import errno
import json
import logging
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

# ─── 路径与最小 astrbot 桩（与 test_autonomous_social.py 同款，幂等） ───
_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


class _FakeMessageChain:
    def message(self, text):
        return ("chain", text)


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _FakeMessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import state as state_mod  # noqa: E402
from social.state import (  # noqa: E402
    PENDING_PROACTIVE_KEEP,
    SocialState,
)
from social.throttle import throttle  # noqa: E402


def _dump_to_file(path: str, data, *, text: str = None) -> None:
    """往 path 写一份状态文件（data 走 json，text 直接写原文）。"""
    with open(path, "w", encoding="utf-8") as f:
        if text is not None:
            f.write(text)
        else:
            json.dump(data, f, ensure_ascii=False)


def _unreadable_backups(directory: str) -> list:
    return [n for n in os.listdir(directory) if ".unreadable." in n]


class _TempStateCase(unittest.TestCase):
    """公用：一个临时目录 + 一个已经写好状态文件的路径。"""

    def setUp(self):
        # 节流器是模块级单例：前一个用例刚打过同名日志，这里会被静默吞掉，
        # 于是「该告警」的断言随机变红。每个用例前清一次。
        throttle.reset()
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state.json")

    def _read_disk(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)


# ─── P0-01：读盘被占用 ≠ 文件坏了 ──────────────────────────────


class StateLoadDeferredTest(_TempStateCase):
    """旧实现把 OSError 与 JSONDecodeError 一起送进 `_fail_load` → `_backup_unreadable`
    → `os.rename`，然后拿空壳跑并整轮拒绝写盘。Windows 上杀软扫描、OneDrive 同步、
    另一个进程打开着文件，都会让 `open()` 抛 PermissionError（WinError 32/5）——
    一次瞬时占用 = 用户全部记忆被改名 + 此后所有新数据都写不进去。"""

    def setUp(self):
        super().setUp()
        _dump_to_file(
            self.path,
            {"version": 2, "bots": {"b": {"users": {"u1": {"last_message": "旧"}}}}},
        )

    def _load_with_read_error(self, exc):
        # 不真的等 0.4 秒退避：延迟表由模块常量决定，这里换成空表
        with mock.patch.object(state_mod, "_READ_RETRY_DELAYS", ()), mock.patch.object(
            SocialState, "_read_json_with_retry", side_effect=exc
        ):
            return SocialState(self.path, time_source=lambda: 1.0)

    def test_transient_permission_error_keeps_the_file(self):
        state = self._load_with_read_error(PermissionError(13, "另一个程序正在使用此文件"))
        self.assertTrue(state._load_failed, "读不出来时仍要挡住写盘，别拿空壳覆盖")
        self.assertTrue(state._load_deferred, "瞬时错误要走「稍后重试」，不是「永久失败」")
        self.assertTrue(
            os.path.exists(self.path),
            "读盘被占用就把 state.json 改名 = 用户重启后记忆全没（P0-01 的原始事故）",
        )
        self.assertEqual(_unreadable_backups(self.tmp), [], "瞬时错误不该产生备份文件")
        status = state.load_status()
        self.assertFalse(status["ok"])
        self.assertTrue(status["deferred"])
        self.assertIn("PermissionError", status["error"])

    def test_non_transient_oserror_is_deferred_without_retry(self):
        real_open = builtins.open
        attempts = []

        def fake_open(file, *args, **kwargs):
            if str(file) == self.path:
                attempts.append(1)
                raise OSError(errno.EIO, "磁盘 I/O 错误")
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", fake_open), mock.patch.object(
            state_mod.time, "sleep"
        ) as sleeper:
            state = SocialState(self.path, time_source=lambda: 1.0)
        self.assertEqual(len(attempts), 1, "非瞬时错误重试一万次也还是错，别空等")
        self.assertEqual(sleeper.call_count, 0)
        self.assertTrue(state._load_deferred)
        self.assertTrue(os.path.exists(self.path))

    def test_transient_read_error_is_retried_with_backoff(self):
        real_open = builtins.open
        attempts = []

        def fake_open(file, *args, **kwargs):
            if str(file) == self.path:
                attempts.append(1)
                if len(attempts) <= 2:
                    raise PermissionError(13, "文件被占用")
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", fake_open), mock.patch.object(
            state_mod.time, "sleep"
        ) as sleeper:
            state = SocialState(self.path, time_source=lambda: 1.0)

        self.assertEqual(len(attempts), 3, "占用放开后应当重试到成功，而不是直接放弃")
        self.assertEqual(
            [c.args[0] for c in sleeper.call_args_list],
            [0.1, 0.3],
            "退避间隔应当逐次变长（100ms / 300ms / 900ms 的前两档）",
        )
        self.assertFalse(state._load_failed)
        self.assertFalse(state._load_deferred)
        self.assertEqual(
            state.data["bots"]["b"]["users"]["u1"]["last_message"], "旧"
        )

    def test_corrupt_json_still_backs_up(self):
        _dump_to_file(self.path, None, text='{"bots": {"b": ')
        state = SocialState(self.path, time_source=lambda: 1.0)
        self.assertTrue(state._load_failed)
        self.assertFalse(
            state._load_deferred,
            "内容坏了不是「被占用」，重试没有意义，不该进重试路径",
        )
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(len(_unreadable_backups(self.tmp)), 1, "损坏的文件要留证据")

    def test_retry_load_recovers_after_the_lock_is_released(self):
        real_read = SocialState._read_json_with_retry
        calls = {"n": 0}

        def flaky_read(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "文件被占用")
            return real_read(self, **kwargs)

        with mock.patch.object(SocialState, "_read_json_with_retry", flaky_read):
            state = SocialState(self.path, time_source=lambda: 1.0)
            self.assertTrue(state._load_deferred)
            self.assertTrue(
                state.retry_load(),
                "占用解除后必须能自己加载回来，不能要求用户手动重载插件",
            )
            self.assertFalse(state._load_failed)
            self.assertEqual(state.load_status()["deferred"], False)
            self.assertEqual(
                state.data["bots"]["b"]["users"]["u1"]["last_message"], "旧"
            )

    def test_flush_is_the_recovery_point(self):
        real_read = SocialState._read_json_with_retry
        calls = {"n": 0}

        def flaky_read(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "文件被占用")
            return real_read(self, **kwargs)

        with mock.patch.object(SocialState, "_read_json_with_retry", flaky_read):
            state = SocialState(self.path, time_source=lambda: 1.0)
            self.assertTrue(state._load_failed)
            # 占用期间插件照常记账（往空壳里记），这些改动没法与盘上旧内容合并
            state.user("b", "u1")["last_message"] = "占用期间记的"
            state.mark_dirty()
            state.flush()
        self.assertFalse(state._load_failed, "心跳里的 flush() 是最稳定的重试点")
        self.assertEqual(
            state.data["bots"]["b"]["users"]["u1"]["last_message"],
            "旧",
            "重载后应当以磁盘内容为准（合并会让计数翻倍）",
        )

    def test_retry_that_still_fails_stays_deferred_and_keeps_the_file(self):
        with mock.patch.object(state_mod, "_READ_RETRY_DELAYS", ()), mock.patch.object(
            SocialState,
            "_read_json_with_retry",
            side_effect=PermissionError(13, "还被占着"),
        ):
            state = SocialState(self.path, time_source=lambda: 1.0)
            self.assertFalse(state.retry_load())
            self.assertFalse(state.retry_load())
            self.assertTrue(state._load_deferred)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(_unreadable_backups(self.tmp), [])

    def test_corrupt_file_is_not_retried_forever(self):
        _dump_to_file(self.path, None, text="不是 JSON")
        state = SocialState(self.path, time_source=lambda: 1.0)
        self.assertFalse(state.retry_load(), "损坏文件重试没有意义")
        self.assertEqual(
            len(_unreadable_backups(self.tmp)), 1, "重试不该再备份出第二份"
        )

    def test_blocked_write_warns_only_once(self):
        state = self._load_with_read_error(PermissionError(13, "还被占着"))
        with mock.patch.object(state_mod, "logger") as logger_mock:
            state.save()
            state.save()
        errors = [str(c.args[0]) for c in logger_mock.error.call_args_list]
        self.assertEqual(len(errors), 1, "写盘每 30 秒一次，不节流会把日志刷满")
        self.assertIn("读取被占用", errors[0], "要能分清「被占用」和「文件坏了」")


class ReadErrorClassificationTest(unittest.TestCase):
    """`_is_transient_read_error` 是 P0-01 的分流闸门：判错一次就是改名事故。"""

    def test_permission_error_is_transient(self):
        self.assertTrue(
            SocialState._is_transient_read_error(PermissionError(13, "被占用"))
        )

    def test_windows_sharing_violation_is_transient(self):
        class _SharingViolation(OSError):
            """模拟 Windows WinError 32（另一个进程正在使用此文件）。"""

            winerror = 32

        self.assertTrue(
            SocialState._is_transient_read_error(_SharingViolation(errno.EIO, "x"))
        )

    def test_plain_io_error_is_not_transient(self):
        self.assertFalse(
            SocialState._is_transient_read_error(OSError(errno.EIO, "磁盘错误"))
        )

    def test_missing_file_is_not_transient(self):
        self.assertFalse(
            SocialState._is_transient_read_error(FileNotFoundError(2, "没有这个文件"))
        )


# ─── P2-21：字段级类型损坏在加载期自愈 ─────────────────────────


DIRTY_STATE = {
    "version": 2,
    "bots": {
        "b": {
            "users": {
                "u1": {
                    "message_count": "很多",
                    "last_seen": "昨天",
                    "conversation": {"不是": "列表"},
                    "topics": "话题",
                    "topic_seen_at": ["不是", "字典"],
                    "active_hours": "0",
                    "special_care": "yes",
                    "interest": "很高",
                    # bool 是 int 的子类：`true` 落在计数字段上是脏值，不是 1
                    "proactive_sent": True,
                }
            },
            "groups": {"g1": {"msg_count": "很多", "flow_replies": [1]}},
        }
    },
}


class StateFieldRepairTest(_TempStateCase):
    """旧实现只归一「容器」（bots/users 不是 dict），字段类型不管。于是
    `record_incoming` 里的 `int(u["message_count"])`、`_extract_topics` 里的
    `topics.insert`、`reply_rate` 里的 `int(...)` 都会抛——而且异常发生在
    `mark_dirty()` 之前，坏值永远写不回盘，这个用户此后**每条消息都抛异常**。"""

    def setUp(self):
        super().setUp()
        _dump_to_file(self.path, DIRTY_STATE)

    def test_dirty_field_types_are_repaired_at_load(self):
        state = SocialState(self.path, time_source=lambda: 1.0)
        u = state.data["bots"]["b"]["users"]["u1"]
        self.assertEqual(u["message_count"], 0)
        self.assertEqual(u["last_seen"], 0.0)
        self.assertEqual(u["conversation"], [])
        self.assertEqual(u["topics"], [])
        self.assertEqual(u["topic_seen_at"], {})
        self.assertEqual(len(u["active_hours"]), 24)
        self.assertIs(u["special_care"], False)
        self.assertIsNone(u["interest"])
        self.assertEqual(
            u["proactive_sent"], 0, "JSON 的 true 不是数字，按「没发过」重置"
        )
        group = state.data["bots"]["b"]["groups"]["g1"]
        self.assertEqual(group["msg_count"], 0)
        self.assertEqual(group["flow_replies"], 0)

    def test_repaired_user_no_longer_raises_on_every_message(self):
        state = SocialState(self.path, time_source=lambda: 1.0)
        u = state.record_incoming("b", "u1", "在吗")
        self.assertEqual(u["message_count"], 1, "旧实现这里 int(\"很多\") 直接抛")
        self.assertEqual(state.reply_rate("b", "u1"), 0.5)
        state.record_outgoing("b", "u1", "在的")
        self.assertTrue(state._dirty)

    def test_repair_is_persisted_so_it_does_not_come_back(self):
        state = SocialState(self.path, time_source=lambda: 1.0)
        self.assertTrue(state._dirty, "修了就要落盘，否则下次启动又是坏的")
        self.assertTrue(state.flush())
        again = SocialState(self.path, time_source=lambda: 1.0)
        self.assertEqual(again.data["bots"]["b"]["users"]["u1"]["message_count"], 0)
        self.assertFalse(again._dirty, "修好之后再加载不该又标脏（会全量重写一遍）")

    def test_dirty_topic_list_elements_do_not_break_incoming(self):
        # 列表本身是 list、元素被手改成数字：`_extract_topics` 里 `t.lower()` 会抛，
        # 而这个函数每条用户消息都跑——一个人坏掉 = 这个用户此后每条消息都炸
        _dump_to_file(
            self.path,
            {
                "version": 2,
                "bots": {
                    "b": {"users": {"u1": {"topics": ["塞尔达", 42, None]}}}
                },
            },
        )
        state = SocialState(self.path, time_source=lambda: 1.0)
        u = state.record_incoming("b", "u1", "我最近还在玩塞尔达")
        self.assertNotIn(42, u["topics"])
        self.assertNotIn(None, u["topics"])
        self.assertTrue(all(isinstance(t, str) for t in u["topics"]))

    def test_valid_values_are_left_alone(self):
        _dump_to_file(
            self.path,
            {
                "version": 2,
                "bots": {
                    "b": {
                        "users": {
                            "u1": {
                                "message_count": 5.0,
                                "last_message": "旧消息",
                                "interest": 0.4,
                                "special_care": True,
                                "active_hours": [0.0] * 24,
                            }
                        }
                    }
                },
            },
        )
        state = SocialState(self.path, time_source=lambda: 1.0)
        u = state.data["bots"]["b"]["users"]["u1"]
        self.assertEqual(u["message_count"], 5, "整数值的浮点按整数收下")
        self.assertEqual(u["last_message"], "旧消息")
        self.assertEqual(u["interest"], 0.4)
        self.assertIs(u["special_care"], True)
        self.assertEqual(len(u["active_hours"]), 24)


# ─── P1-01：关掉正文保存后，pending 里也不许有正文 ─────────────


class StatePendingTextPrivacyTest(_TempStateCase):
    """`record_outgoing` 里 `conversation` / `proactive_log` / `last_spoken_text`
    都按 `store_text` 写空串了，只有 `pending_proactive_context` 还塞
    `str(text or "")`（连 200 字截断都没有）。关掉「保存消息正文」的用户以为
    「她说过的话不落盘」，实际整段原文还躺在 state.json 里。"""

    def setUp(self):
        super().setUp()
        self.state = SocialState(self.path, time_source=lambda: 1.0)

    def test_store_text_off_queues_no_proactive_text(self):
        text = "今天面试完我特别想跟你说说话"
        u = self.state.record_outgoing("b", "u1", text, store_text=False)
        self.assertEqual(
            u["pending_proactive_context"],
            [],
            "关掉正文保存后，原文仍会整段写进 state.json",
        )
        self.assertNotIn(
            text,
            json.dumps(self.state.data, ensure_ascii=False),
            "状态里任何位置都不该留下完整原文",
        )

    def test_store_text_off_does_not_grow_the_queue(self):
        for i in range(3):
            u = self.state.record_outgoing("b", "u1", f"第{i}条原文", store_text=False)
        self.assertEqual(u["pending_proactive_context"], [])

    def test_store_text_on_still_queues_the_text(self):
        text = "我想你了，今天过得怎么样"
        u = self.state.record_outgoing("b", "u1", text, store_text=True)
        self.assertEqual(u["pending_proactive_context"][-1]["text"], text)

    def test_queue_stays_bounded(self):
        for i in range(PENDING_PROACTIVE_KEEP + 4):
            u = self.state.record_outgoing("b", "u1", f"第{i}条", store_text=True)
        self.assertEqual(len(u["pending_proactive_context"]), PENDING_PROACTIVE_KEEP)


# ─── P2-20：异步写盘 ──────────────────────────────────────────


class StateAsyncWriteTest(_TempStateCase):
    """`save()`/`flush()` 把「全量序列化 + fsync」整套同步跑在事件循环上：用户上千时
    一次就是上百毫秒，整轮心跳里所有人的消息处理都被卡住。新 API 把序列化留在
    事件循环（它要读 self.data），只把写盘交给工作线程。"""

    def setUp(self):
        super().setUp()
        self.state = SocialState(self.path, time_source=lambda: 1.0)

    def test_snapshot_then_write_lands_on_disk(self):
        self.state.user("b", "u1")["last_message"] = "新"
        self.state.mark_dirty()
        snap = self.state.snapshot()
        self.assertIsNotNone(snap)
        self.assertIsInstance(snap[0], str, "序列化必须在调用方线程先做完")
        self.assertTrue(self.state.write_snapshot(snap))
        self.assertFalse(self.state._dirty)
        self.assertEqual(
            self._read_disk()["bots"]["b"]["users"]["u1"]["last_message"], "新"
        )

    def test_stale_snapshot_does_not_clear_a_newer_dirty_flag(self):
        self.state.user("b", "u1")["last_message"] = "一"
        self.state.mark_dirty()
        snap = self.state.snapshot()
        # 快照与落盘之间主线程又改了：不能把这几十毫秒里的改动当成已落盘
        self.state.user("b", "u1")["last_message"] = "二"
        self.state.mark_dirty()
        self.assertTrue(self.state.write_snapshot(snap))
        self.assertTrue(
            self.state._dirty, "过期快照落盘后仍清脏 → 最新改动永久丢失"
        )

    def test_flush_async_writes_through_a_worker_thread(self):
        self.state.user("b", "u1")["last_message"] = "异步"
        self.state.mark_dirty()
        self.assertTrue(asyncio.run(self.state.flush_async()))
        self.assertFalse(self.state._dirty)
        self.assertEqual(
            self._read_disk()["bots"]["b"]["users"]["u1"]["last_message"], "异步"
        )

    def test_flush_async_without_changes_does_not_write(self):
        self.assertFalse(asyncio.run(self.state.flush_async()))
        self.assertFalse(os.path.exists(self.path))

    def test_save_async_writes_even_when_not_marked_dirty(self):
        self.state.user("b", "u1")["last_message"] = "强制"
        self.assertTrue(asyncio.run(self.state.save_async()))
        self.assertEqual(
            self._read_disk()["bots"]["b"]["users"]["u1"]["last_message"], "强制"
        )

    def test_write_snapshot_is_safe_from_another_thread(self):
        self.state.user("b", "u1")["last_message"] = "线程"
        self.state.mark_dirty()
        snap = self.state.snapshot()
        worker = threading.Thread(target=self.state.write_snapshot, args=(snap,))
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(
            self._read_disk()["bots"]["b"]["users"]["u1"]["last_message"], "线程"
        )

    def test_snapshot_is_none_while_the_gate_is_closed(self):
        _dump_to_file(self.path, {"version": 2, "bots": {}})
        state = SocialState(self.path, time_source=lambda: 1.0)
        self.assertIsNotNone(state.snapshot(), "正常状态应当能取快照")
        with mock.patch.object(state_mod, "_READ_RETRY_DELAYS", ()), mock.patch.object(
            SocialState, "_read_json_with_retry", side_effect=PermissionError(13, "占用")
        ):
            blocked = SocialState(self.path, time_source=lambda: 1.0)
        self.assertIsNone(blocked.snapshot(), "闸门关着时不许把空壳序列化下去")


# ─── P2-22：外部改动告警 + 跨进程锁 ───────────────────────────


class StateExternalChangeTest(_TempStateCase):
    """全量重写是静默覆盖：另一个实例刚写进去的用户、用户手改的 state.json，
    都会被下一次心跳整份抹掉，日志里一个字节都不会有。"""

    def setUp(self):
        super().setUp()
        self.state = SocialState(self.path, time_source=lambda: 1.0)
        self.state.user("b", "u1")["last_message"] = "一"
        self.state.mark_dirty()
        self.assertTrue(self.state.flush())

    def test_external_change_warns_but_still_writes(self):
        # 模拟另一个实例 / 手改：整份换掉，mtime 与 size 都变了
        _dump_to_file(self.path, {"version": 2, "bots": {"other": {"users": {}}}})
        self.state.user("b", "u1")["last_message"] = "二"
        self.state.mark_dirty()
        with mock.patch.object(state_mod, "logger") as logger_mock:
            self.assertTrue(self.state.flush())
        warnings = [str(c.args[0]) for c in logger_mock.warning.call_args_list]
        self.assertTrue(
            any("外部改动" in w for w in warnings),
            f"外部改动必须告警，否则双实例互相丢更新永远查不出来：{warnings}",
        )
        self.assertEqual(
            self._read_disk()["bots"]["b"]["users"]["u1"]["last_message"],
            "二",
            "只告警不阻止写：主链路（发送/结算）不能被一个锁卡死",
        )

    def test_our_own_write_does_not_look_like_an_external_change(self):
        self.state.user("b", "u1")["last_message"] = "二"
        self.state.mark_dirty()
        with mock.patch.object(state_mod, "logger") as logger_mock:
            self.assertTrue(self.state.flush())
        warnings = [str(c.args[0]) for c in logger_mock.warning.call_args_list]
        self.assertFalse(
            any("外部改动" in w for w in warnings),
            "自己的写入被当成外部改动 → 每次写都报假警",
        )

    def test_lock_file_is_cleaned_up(self):
        self.assertFalse(
            os.path.exists(self.path + ".lock"), "正常写完不该留下锁文件"
        )

    def test_busy_lock_degrades_to_a_warning_instead_of_blocking(self):
        lock_path = self.path + ".lock"
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write("99999")
        self.state.user("b", "u1")["last_message"] = "三"
        self.state.mark_dirty()
        with mock.patch.object(state_mod, "logger") as logger_mock:
            self.assertTrue(self.state.flush())
        warnings = [str(c.args[0]) for c in logger_mock.warning.call_args_list]
        self.assertTrue(any("写锁被占用" in w for w in warnings), warnings)
        self.assertEqual(
            self._read_disk()["bots"]["b"]["users"]["u1"]["last_message"], "三"
        )
        self.assertTrue(
            os.path.exists(lock_path), "别人（或另一个实例）的锁不能替人家删掉"
        )

    def test_stale_lock_is_stolen_after_a_crash(self):
        lock_path = self.path + ".lock"
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write("99999")
        old = os.path.getmtime(lock_path) - state_mod._LOCK_STALE_SECONDS - 5
        os.utime(lock_path, (old, old))
        self.state.user("b", "u1")["last_message"] = "四"
        self.state.mark_dirty()
        self.assertTrue(self.state.flush())
        self.assertFalse(
            os.path.exists(lock_path), "崩溃残留的锁必须能被抢占并清掉，否则永远写不进去"
        )
        self.assertEqual(
            self._read_disk()["bots"]["b"]["users"]["u1"]["last_message"], "四"
        )


if __name__ == "__main__":
    unittest.main()
