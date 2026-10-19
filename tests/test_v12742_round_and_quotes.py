"""v1.27.42：一轮心跳的「尝试次数」与基础设施短路。

## 为什么要有这一版

线上真实日志（一天之内同一条重复了 9763 次）：

    念想「有点想你了」 2114128887 → LLM 不可用（provider 取不到或调用失败），本轮未发送
    念想「在干嘛呢」     3574784101 → LLM 不可用（provider 取不到或调用失败），本轮未发送
    ……（一分钟内 550 行）

根因不是「日志写得太多」，是**一轮里把 550 个候选全试了一遍**：

* `budget` 只限制「发出去几条」，而没发出去时那条配额会被原样还回来；
* provider 坏了的时候每个候选都失败、都归还配额，于是全都试一遍；
* 每个候选还会先等满 90 秒超时（或直接返回不可用），再记一行日志。

所以这一版给「尝试」本身上了闸，并且一旦确认是 provider 坏了，本轮剩下的候选
一个都不再试。测试钉的就是这两条不变量——它们都不能靠「读代码看起来对」保证。
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
    api.logger = logging.getLogger("autonomous_social_v12742_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.config import SocialConfig  # noqa: E402
from social.engine import ATTEMPT_PER_SEND, SocialEngine  # noqa: E402
from social.state import SocialState  # noqa: E402
from social.textutil import is_quotable  # noqa: E402
from social.verify import merge_lead_line  # noqa: E402


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

    def note_open_items(self, *a, **k):
        pass

    def beat(self):
        pass


class _FakeGenerator:
    def __init__(self, backoff=0.0):
        self.backoff = backoff

    def llm_backoff_remaining(self, scope=""):
        return self.backoff


class _RoundEngine:
    """只保留 `try_once` 发送段需要的依赖；候选全走「念想」通道。"""

    try_once = SocialEngine.try_once
    _user_lock = SocialEngine._user_lock
    _persist_metrics = SocialEngine._persist_metrics
    _is_infra_note = staticmethod(SocialEngine._is_infra_note)

    def __init__(self, n_users=30, budget=3, result="ok", backoff=0.0):
        self.cfg = SocialConfig.from_astrbot({})
        self.cfg.enabled = True
        self.cfg.group_icebreak_enabled = False
        self.cfg.group_topic_enabled = False
        self.cfg.max_sends_per_round = budget
        self.cfg.batch_send_concurrency = 4
        self.state = SocialState(os.path.join(tempfile.mkdtemp(), "state.json"))
        self.core = _NullCore()
        self.signals = _NullSignals()
        self._user_locks = {}
        self._last_flush = 1.0
        self._m = {}
        self.generator = _FakeGenerator(backoff=backoff)
        self.attempts = 0
        self.result = result

        bot = self.state.bot("bot1")
        bot.setdefault("users", {})
        self._misses = []
        for i in range(n_users):
            uid = f"u{i}"
            bot["users"][uid] = {"umo": f"aiocqhttp:FriendMessage:{uid}"}
            self._misses.append((uid, bot["users"][uid], {"about": f"上次说的第{i}件事"}))
        self.state.mark_dirty()

    # 收集层
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

    # 记账
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

    def _open_items(self, bid, now):
        return []

    def _beat_signals(self):
        pass

    def _core_last_interaction(self, root, bid, uid):
        return 0.0

    def _relation_bucket(self, u):
        return 0

    async def _speak(self, bid, uid, u, urge, now, *, allow_veto=True, preset=None,
                     require_about_peer=False):
        self.attempts += 1
        if self.result == "ok":
            return True, "ok"
        return False, self.result


INFRA = "LLM 不可用（provider 取不到或调用失败），本轮未发送"


class AttemptGate(unittest.TestCase):
    """一轮心跳不能把全部候选都试一遍。"""

    def test_infra_failure_stops_the_rest_of_the_round(self):
        """provider 坏了 → 只试一次，剩下的不再挨个失败。"""
        engine = _RoundEngine(n_users=30, result=INFRA)
        _run(engine.try_once())
        self.assertEqual(engine.attempts, 1, "provider 已经坏了，不该再试第二个人")

    def test_model_veto_still_allows_the_rest(self):
        """模型说「现在不该说」是**一次成功的调用**，不该把整轮关掉。"""
        engine = _RoundEngine(n_users=30, result="想过，但觉得现在不该说：太晚了")
        _run(engine.try_once())
        self.assertGreater(engine.attempts, 1, "否决不是基础设施故障，后面的候选还得有机会")
        self.assertLessEqual(
            engine.attempts, engine.cfg.max_sends_per_round * ATTEMPT_PER_SEND
        )

    def test_attempts_are_capped_even_when_nothing_sends(self):
        """一直发不出去也不能无限试：尝试次数按 max_sends_per_round 的倍数封顶。"""
        engine = _RoundEngine(n_users=40, budget=2, result="发送失败：平台侧错误")
        _run(engine.try_once())
        self.assertEqual(engine.attempts, 2 * ATTEMPT_PER_SEND)

    def test_backoff_skips_the_whole_round_without_trying(self):
        """provider 在退避里时，连候选都不用试。"""
        engine = _RoundEngine(n_users=30, backoff=600.0)
        _run(engine.try_once())
        self.assertEqual(engine.attempts, 0)


class InfraNote(unittest.TestCase):
    def test_only_llm_notes_are_infra(self):
        self.assertTrue(SocialEngine._is_infra_note(INFRA))
        self.assertTrue(SocialEngine._is_infra_note("LLM 生成为空（provider 不可用或调用失败）"))
        self.assertFalse(SocialEngine._is_infra_note("想过，但觉得现在不该说：太晚了"))
        self.assertFalse(SocialEngine._is_infra_note("发送失败：未找到匹配的会话平台"))


class Quotable(unittest.TestCase):
    """「你上次说的「X」后来呢」里的 X 必须真的是一件事。"""

    def test_rejects_real_leaks(self):
        for bad in ("yep", "怎么", "不是", "打你", "时间", "[图片][Image", "666", "??"):
            self.assertFalse(is_quotable(bad), f"这个片段不该被当成话题：{bad!r}")

    def test_accepts_a_real_thing(self):
        for good in ("明天要体检", "那个面试过了没", "我妈生病住院了"):
            self.assertTrue(is_quotable(good), f"这是一件真事，不该被丢：{good!r}")


class LeadLine(unittest.TestCase):
    def test_bracket_lead_is_merged(self):
        out = merge_lead_line("有点想你了\n(伸个懒腰) 今天风大")
        self.assertNotIn("\n", out)
        self.assertIn("有点想你了", out)

    def test_plain_line_break_is_kept(self):
        """「早\\n起了」是同一句话被换行切开，并起来会把停顿也拼掉。"""
        self.assertEqual(merge_lead_line("早\n起了"), "早\n起了")

    def test_sentence_ending_head_is_kept(self):
        self.assertEqual(
            merge_lead_line("今天真累。\n(躺下) 睡了"), "今天真累。\n(躺下) 睡了"
        )


class _Recorder:
    """替掉模块级 logger，只收消息。

    不用 `assertLogs`：这个测试套件里每个文件都装一份自己的 astrbot 替身，
    `astrbot.api.logger` 到底叫哪个名字取决于**哪个文件先被导入**——在单文件里跑
    能过、全量跑就抓不到（实测）。直接换 logger 对象才不依赖导入顺序。
    """

    def __init__(self):
        self.messages = []

    def _add(self, level, msg, *args, **kwargs):
        try:
            self.messages.append((level, str(msg) % args if args else str(msg)))
        except Exception:
            self.messages.append((level, str(msg)))

    def debug(self, msg, *a, **k):
        self._add("DEBUG", msg, *a, **k)

    def info(self, msg, *a, **k):
        self._add("INFO", msg, *a, **k)

    def warning(self, msg, *a, **k):
        self._add("WARNING", msg, *a, **k)

    def error(self, msg, *a, **k):
        self._add("ERROR", msg, *a, **k)

    def exception(self, msg, *a, **k):
        self._add("ERROR", msg, *a, **k)


class GateSkipAggregationTest(unittest.TestCase):
    """闸门挡下的人按原因聚合成一行，不逐人打日志。

    线上实测：一天里「有由头但没说（uid）：现在是安静时段」5606 行、
    「她正在睡觉」4917 行 —— 比 LLM 那条刷得还狠，而一条信息量都没有：
    同一个原因在一轮里重复两百遍，结论仍然是「这轮大家都被安静时段挡了」。
    """

    class _GatedEngine(_RoundEngine):
        def __init__(self, n_users=20, why="现在是安静时段"):
            super().__init__(n_users=n_users)
            self._why = why
            self._anchor_rows = []
            for uid in [f"u{i}" for i in range(n_users)]:
                self._anchor_rows.append(
                    ("bot1", uid, self.state.bot("bot1")["users"][uid], 2.0,
                     {"about": f"第{uid}件事"})
                )
            self._misses = []

        def _anchor_pick(self, *a, **k):
            return [{"about": "某件事", "kind": "derived", "due": 0.0}]

        def settle_minds(self, *a, **k):
            return [(row[1], row[2], row[3]) for row in self._anchor_rows]

        def gate_reason(self, bid, uid, u, now, body):
            return self._why

        def _block_category(self, reason):
            return str(reason or "其它")

    def _run(self, engine):
        import social.engine as engine_mod

        recorder = _Recorder()
        original = engine_mod.logger
        engine_mod.logger = recorder
        try:
            _run(engine.try_once())
        finally:
            engine_mod.logger = original
        return recorder.messages

    def test_skips_are_counted_by_category(self):
        engine = self._GatedEngine()
        self._run(engine)
        self.assertEqual(engine.attempts, 0)
        self.assertEqual(engine._round_skips.get("现在是安静时段"), 20)

    def test_one_line_per_round_not_one_per_person(self):
        engine = self._GatedEngine(n_users=20)
        messages = self._run(engine)
        info = [m for lvl, m in messages if "本轮候选" in m]
        self.assertEqual(len(info), 1, f"一轮只该有一行汇总，实际：{info}")
        self.assertIn("现在是安静时段 20", info[0])
        detail = [m for _lvl, m in messages if "有由头但没说" in m]
        self.assertEqual(len(detail), 20, "逐人明细降到 debug，仍然查得到")


if __name__ == "__main__":
    unittest.main()
