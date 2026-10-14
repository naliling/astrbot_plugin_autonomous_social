"""v1.27.22：主动消息「逐人生成、批量调度」（方案 B）。

把 `try_once` 的发送段从「一个接一个 await」改成「先收集候选、再按并发上限调度」，
风险全在并发那一层：`budget`/`served` 的「检查 + 占用」如果不是原子的，同一角色下
不同人的 job 并发时会一起读到同一个 `budget`，把 `max_sends_per_round` 冲破；或者
同一个人被发两条。这里把 `try_once` 直接挂到一个最小替身上跑，钉住这几条不变量，
并确认 `batch_send_concurrency == 1` 退回串行。
"""
from __future__ import annotations

import asyncio
import datetime
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
    api.logger = logging.getLogger("autonomous_social_batch_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social.state import SocialState  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _NullCore:
    def read_root(self):
        return None

    def bot_self_state(self, bid, root=None):
        return None


class _NullSignals:
    def set_ignored_streak(self, *a, **k):
        pass

    def note_ignored(self, *a, **k):
        pass

    def set_role_count(self, *a, **k):
        pass

    def beat(self):
        pass


class _BatchEngine:
    """只保留 `try_once` 发送段用到的那几个依赖，收集层只走「念想」通道。"""

    try_once = SocialEngine.try_once
    _user_lock = SocialEngine._user_lock
    _persist_metrics = SocialEngine._persist_metrics

    def __init__(self, uids, miss_dupes=(), budget=2, concurrency=4):
        self.cfg = SocialConfig.from_astrbot({})
        self.cfg.enabled = True
        self.cfg.group_icebreak_enabled = False
        self.cfg.group_topic_enabled = False
        self.cfg.max_sends_per_round = budget
        self.cfg.batch_send_concurrency = concurrency
        self.state = SocialState(os.path.join(tempfile.mkdtemp(), "state.json"))
        self.core = _NullCore()
        self.signals = _NullSignals()
        self._user_locks = {}
        self._last_flush = 1.0
        self._m = {}
        self.sent = []
        self.max_inflight = 0
        self._inflight = 0

        bot = self.state.bot("bot1")
        bot.setdefault("users", {})
        for uid in list(uids) + list(miss_dupes):
            bot["users"].setdefault(uid, {"umo": f"aiocqhttp:FriendMessage:{uid}"})
        self._misses = [(uid, bot["users"][uid], {"about": "x"}) for uid in uids]
        self._misses += [(uid, bot["users"][uid], {"about": "y"}) for uid in miss_dupes]
        self.state.mark_dirty()

    # ── 收集层：除「念想」外一律空 ─────────────────────────────
    def _promise_pick(self, *a, **k):
        return []

    def _miss_pick(self, *a, **k):
        return list(self._misses)

    def _intimacy_pick(self, *a, **k):
        return []

    def settle_minds(self, *a, **k):
        return []

    def _anchor_pick(self, *a, **k):
        return []

    def _thread_pick(self, *a, **k):
        return None

    def _loop_pick(self, *a, **k):
        return None

    def _closer_pick(self, *a, **k):
        return None

    def _greet_pick(self, *a, **k):
        return []

    # ── 记账/时钟 ──────────────────────────────────────────────
    def _miss_mark_today(self, u, day):
        return 1

    def _count_block(self, *a, **k):
        pass

    def _note_decision(self, *a, **k):
        pass

    def log(self, *a, **k):
        pass

    def _time(self):
        return 1_700_000_000.0

    def _moment_of(self, bid, now):
        return datetime.datetime(2023, 11, 15, 14, 0)

    async def _speak(self, bid, uid, u, urge, now, *, allow_veto=True, preset=None,
                     require_about_peer=False):
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        # 主动让出事件循环两次：串行时其它 job 没有机会挤进来（inflight 恒为 1），
        # 并发时它们会同时停在 await 上（inflight 涨上去）。
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.sent.append((bid, uid))
        self._inflight -= 1
        return True, "ok"


class BatchDispatch(unittest.TestCase):
    def _run_round(self, engine):
        # 成功后那条 `random.uniform(2, 5)` 的间隔对本测试没意义，压成 0 省时间。
        with patch("social.engine.random.uniform", return_value=0.0):
            _run(engine.try_once())

    def test_budget_one_sends_exactly_one(self):
        # 配额 1 时那句「给另起话题留一条」不生效（要求 >1），所以这一轮严格只发 1 条。
        # 若「检查 + 占用」不是原子的，多个 job 并发会一起读到 budget=1 而各发一条。
        e = _BatchEngine(["u%d" % i for i in range(1, 9)], budget=1, concurrency=5)
        self._run_round(e)
        self.assertEqual(len(e.sent), 1, e.sent)

    def test_budget_not_exceeded_under_concurrency(self):
        # 候选远多于配额：无论并发多少，发出的条数都不能超过 max_sends_per_round。
        e = _BatchEngine(["u%d" % i for i in range(1, 11)], budget=4, concurrency=6)
        self._run_round(e)
        self.assertLessEqual(len(e.sent), 4, e.sent)
        self.assertGreater(len(e.sent), 0, e.sent)
        self.assertEqual(len({uid for _b, uid in e.sent}), len(e.sent), e.sent)

    def test_same_user_never_sent_twice(self):
        e = _BatchEngine(["u1"], miss_dupes=["u1", "u1"], budget=10, concurrency=4)
        self._run_round(e)
        self.assertEqual(len(e.sent), 1, e.sent)

    def test_concurrency_really_overlaps(self):
        # 配额给足，5 个候选全发；并发上限 3 → 应能看到 3 个 job 同时在途。
        e = _BatchEngine(["u1", "u2", "u3", "u4", "u5"], budget=10, concurrency=3)
        self._run_round(e)
        self.assertEqual(len(e.sent), 5, e.sent)
        self.assertEqual(e.max_inflight, 3, e.max_inflight)

    def test_concurrency_one_is_serial(self):
        e = _BatchEngine(["u1", "u2", "u3", "u4", "u5"], budget=10, concurrency=1)
        self._run_round(e)
        self.assertEqual(len(e.sent), 5, e.sent)
        self.assertEqual(e.max_inflight, 1, "并发数 1 时必须退回串行")


if __name__ == "__main__":
    unittest.main()
