"""v1.28.8：状态面板「说实话」+ 把 Core 的亲密状态显示出来。

用户原话：「展示出来的东西可能被误导，以前留下的计数不会变，尤其是指令显示出来的」，
另一条是「core 的 ccb 场景没有在进行，也看不到性欲状态」。

这两句是同一个病：**面板上的数字没说清它是哪段时间的**。具体三处：

1. `blocked_reasons` 从 `state.json` 的 `_runtime` 接回、跨重启只增不减，
   注释却写「近 N 轮滚动」，面板也照着累计念 —— 重启十次之后
   「安静时段 900 次」还挂在那儿，看着像刚发生。
2. `sent_total` / `rounds` 是累计语义，面板上不标；重启后既没有起点也没有
   「这次到底发了几条」的对照，「最后心跳 3 分钟前」也可能来自上一个进程。
3. `/社交自检` 的「上次结算」只要 `settled` 非 0 就写死「刚刚」（自检自己会
   把 `last_settled` 改写成现在，所以它永远是刚刚），时间戳形同虚设。

另外，Core 早已把 ccb 场景（在不在场景里 / 阶段 / 今日次数 / 余韵）与
`mood.libido` 写进 state.json，桥接层却只读了 `ccb_done_count/ccb_last_at/
ccb_satisfy` 三个历史字段，于是「她这会儿在不在状态」在社交层面板里完全看不见。

## 本文件盯的

- 本次运行与历史累计算清楚：启动清零、持久化只接累计、面板分开念；
- 启动时刻（`boot_at`）存在且能渲染成「本次运行 X 分钟（HH:MM 启动）」；
- `_metrics_text` 在裸 `_m`（测试/重启中途）下不炸，且标注「本进程发送」；
- `core_bridge.view/compact`：ccb 场景/亲近欲/余韵读得到，缺字段/脏数据不炸、
  且 **ccb 数据不存在时一行都不多写**（与旧行为逐字等价）；
- `_intimacy_text`：有 uid 报那个人的状态、没 uid 只报能力、没有 ccb 数据时整段不出；
- `/社交自检` 的时间从时间戳算（`_selfcheck_settle_text`），源码里不再有写死的「刚刚」。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = (
    _HERE.parent
    if (_HERE.parent / "_conf_schema.json").is_file()
    else _HERE.parent / "astrbot_plugin_autonomous_social"
)


def _install_stub() -> None:
    """astrbot 不在测试环境里：按仓库既有做法打最小桩，只补被 import 到的名字。"""
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_status_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object

    star = types.ModuleType("astrbot.api.star")

    class _Context:
        pass

    class _Star:
        def __init__(self, *a, **k):
            pass

    star.Context = _Context
    star.Star = _Star

    class _EventMessageType:
        ALL = "ALL"

    filter_mod = types.ModuleType("astrbot.api.event.filter")

    def _pass_decorator(*_a, **_k):
        def _wrap(func):
            return func

        return _wrap

    filter_mod.EventMessageType = _EventMessageType
    filter_mod.command = _pass_decorator
    filter_mod.event_message_type = _pass_decorator
    event.filter = filter_mod
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})

    api.star = star
    api.event = event
    api.AstrBotConfig = dict
    astrbot.api = api
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.event.filter": filter_mod,
            "astrbot.api.star": star,
        }
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.config import SocialConfig  # noqa: E402
from social.core_bridge import CoreBridge  # noqa: E402
from social.engine import SocialEngine, _ago_text, _ccb_present  # noqa: E402


NOW = 1_700_000_000.0


def _engine(tmp: str, name: str = "s.json", now: float = NOW) -> SocialEngine:
    """造一个真引擎（不是壳）。时间源固定，面板输出才可断言。"""
    clock = [now]
    return SocialEngine(
        context=None,
        config=SocialConfig(),
        state_path=os.path.join(tmp, name),
        data_dir=tmp,
        time_source=lambda: clock[0],
    )


def _write_core(tmp: str, root: dict) -> str:
    """把一份 Humanoid Core state.json 摆到桥接层找得到的位置。"""
    d = os.path.join(tmp, "plugin_data", "humanoid_core")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(root, f, ensure_ascii=False)
    return path


def _core_root(**users) -> dict:
    return {"roles": {"b1": {"self": {}, "users": users}}}


class BlockedReasonsHonestyTest(unittest.TestCase):
    """「以前留下的计数不会变」：累计与本次运行必须分开，且都要能说明口径。"""

    def test_restart_restores_history_but_starts_run_at_zero(self):
        tmp = tempfile.mkdtemp()
        first = _engine(tmp)
        first._count_block("现在是安静时段")
        first._count_block("现在是安静时段")
        first._m["sent_total"] = 30
        first._m["rounds"] = 50
        first._m["sent_run"] = 7
        first._persist_metrics()

        second = _engine(tmp)
        # 累计接回来了：这是面板上「历史累计」那一份，语义没变。
        self.assertEqual(second._m["blocked_reasons"]["安静时段"], 2)
        self.assertEqual(second._m["sent_total"], 30)
        self.assertEqual(second._m["rounds"], 50)
        # 本次运行清零：不然重启一次「这次拦了 2 次」就是假的。
        self.assertEqual(second._m["blocked_reasons_run"], {},
                         "本次运行的拦截计数被上一次进程污染了")
        self.assertEqual(second._m["sent_run"], 0, "本进程发送数没有随启动清零")

    def test_a_new_block_updates_both_but_only_run_is_fresh(self):
        tmp = tempfile.mkdtemp()
        first = _engine(tmp)
        first._count_block("现在是安静时段")
        first._persist_metrics()

        second = _engine(tmp)
        second._count_block("现在是安静时段")
        self.assertEqual(second._m["blocked_reasons"]["安静时段"], 2)
        self.assertEqual(second._m["blocked_reasons_run"], {"安静时段": 1})

    def test_panel_reads_out_both_scopes(self):
        tmp = tempfile.mkdtemp()
        first = _engine(tmp)
        first._count_block("现在是安静时段")
        first._count_block("现在是安静时段")
        first._persist_metrics()

        second = _engine(tmp)
        second._count_block("现在是安静时段")
        second._m["last_heartbeat"] = NOW - 120
        text = second._metrics_text()
        self.assertIn("没发出去的去向", text)
        self.assertIn("本次运行 1 次", text, "本次运行的计数没显示")
        self.assertIn("历史累计 3 次", text, "历史累计被算成了本次运行")
        self.assertIn("不是「近 N 轮」", text, "口径说明被丢了，用户还会以为那是滚动窗口")

    def test_after_restart_with_no_run_blocks_only_history_is_shown(self):
        tmp = tempfile.mkdtemp()
        first = _engine(tmp)
        first._count_block("现在是安静时段")
        first._count_block("现在是安静时段")
        first._persist_metrics()

        second = _engine(tmp)
        second._m["last_heartbeat"] = NOW - 60
        text = second._metrics_text()
        self.assertIn("历史累计 2 次", text)
        self.assertNotIn("本次运行 0 次", text, "没发生过的事不该占一行")

    def test_bare_metrics_dict_does_not_crash(self):
        """重启中途/测试里手工拼的 `_m` 没有新字段，面板不能因此抛异常。"""
        tmp = tempfile.mkdtemp()
        eng = _engine(tmp)
        eng._m = {"last_heartbeat": NOW - 60}
        text = eng._metrics_text()
        self.assertIn("本进程发送", text)
        self.assertIn("历史累计", text)

    def test_run_only_counters_do_not_outlive_the_process(self):
        """「本进程发送」落盘的话会比进程活得久：下次启动前读到它的人是会信的。"""
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "s.json")
        eng = _engine(tmp)
        eng._count_block("现在是安静时段")
        eng._m["sent_run"] = 5
        eng._m["rounds_run"] = 2
        eng._persist_metrics()
        with open(path, encoding="utf-8") as f:
            runtime = json.load(f).get("_runtime", {})
        for key in ("boot_at", "blocked_reasons_run", "sent_run", "rounds_run"):
            self.assertNotIn(key, runtime, f"{key} 落盘了，重启前它会冒充「现在」")
        self.assertIn("blocked_reasons", runtime, "累计的那份必须继续落盘")


class BootTimeTest(unittest.TestCase):
    """「本次运行」要有起点：没有它，累计数字永远说不清是哪一段的。"""

    def test_boot_at_is_recorded(self):
        eng = _engine(tempfile.mkdtemp())
        self.assertAlmostEqual(eng._m["boot_at"], NOW, places=3)

    def test_boot_text_is_the_real_uptime(self):
        eng = _engine(tempfile.mkdtemp())
        self.assertIn("启动", eng._boot_text())
        eng._m["boot_at"] = NOW - 90 * 60
        text = eng._boot_text()
        self.assertIn("1.5 小时", text, f"运行时长算错了：{text}")

    def test_status_page_carries_the_boot_line(self):
        src = inspect.getsource(SocialEngine.status_text)
        self.assertIn("self._boot_text()", src, "状态页没把「本次运行」写进去")


class CoreBridgeIntimacyReadTest(unittest.TestCase):
    """桥接层要把「在不在场景里」读出来，且缺字段/脏数据一律当没有。"""

    def _bridge(self, tmp: str) -> CoreBridge:
        return CoreBridge("auto", data_dir=tmp)

    def test_scene_and_libido_are_read_into_the_snapshot(self):
        tmp = tempfile.mkdtemp()
        _write_core(tmp, _core_root(b1_user={
            "mood": {"libido": 41.0, "base_libido": 34.0, "affection": 78.0},
            "ccb_stage": 3,
            "ccb_turns": 2,
            "ccb_satisfy": 62.0,
            "ccb_last_at": NOW - 3600,
            "ccb_aftercare_until": NOW + 2400,
            "ccb_done_count": 12,
        }))
        snap = self._bridge(tmp).load_snapshot("b1", "b1_user")
        self.assertIsNotNone(snap)
        self.assertEqual(snap["ccb_stage"], 3.0)
        self.assertEqual(snap["ccb_turns"], 2.0)
        self.assertEqual(snap["ccb_satisfy"], 62.0)
        self.assertEqual(snap["ccb_aftercare_until"], NOW + 2400)
        self.assertEqual(snap["libido"], 41.0)
        self.assertEqual(snap["base_libido"], 34.0, "基线被 `or` 吃掉了？")

    def test_compact_has_an_intimacy_line_in_scene(self):
        tmp = tempfile.mkdtemp()
        _write_core(tmp, _core_root(b1_user={
            "mood": {"libido": 41.0, "base_libido": 34.0},
            "ccb_stage": 3,
            "ccb_turns": 2,
            "ccb_satisfy": 62.0,
            "ccb_done_count": 12,
            "ccb_aftercare_until": time.time() + 2400,
        }))
        bridge = self._bridge(tmp)
        snap = bridge.load_snapshot("b1", "b1_user")
        text = bridge.compact(snap, include_weather=False)
        self.assertIn("亲密:", text)
        self.assertIn("在场景里·阶段3", text)
        self.assertIn("亲近欲 41/50", text)
        self.assertIn("基线 34", text)
        self.assertIn("余韵还剩", text)

    def test_compact_has_no_intimacy_line_when_core_is_old(self):
        """老 Core / ccb 关闭：没有 ccb 键时这一行必须**一个字都不多**。"""
        tmp = tempfile.mkdtemp()
        _write_core(tmp, _core_root(b1_user={"mood": {"libido": 30.0}}))
        bridge = self._bridge(tmp)
        snap = bridge.load_snapshot("b1", "b1_user")
        text = bridge.compact(snap, include_weather=False)
        self.assertNotIn("亲密:", text)
        self.assertIn("情绪:", text, "原有的情绪行不该被这次改动挤掉")

    def test_dirty_ccb_fields_do_not_crash_and_are_treated_as_absent(self):
        tmp = tempfile.mkdtemp()
        _write_core(tmp, _core_root(b1_user={
            "ccb_stage": "坏", "ccb_turns": None, "mood": "坏",
        }))
        bridge = self._bridge(tmp)
        snap = bridge.load_snapshot("b1", "b1_user")  # 不该抛
        assert snap is not None
        self.assertIsNone(snap["ccb_stage"])
        self.assertIsNone(snap["libido"])
        self.assertNotIn("亲密:", bridge.compact(snap, include_weather=False))

    def test_aftercare_in_the_past_is_not_shown(self):
        tmp = tempfile.mkdtemp()
        _write_core(tmp, _core_root(b1_user={
            "mood": {"libido": 41.0, "base_libido": 34.0},
            "ccb_stage": 0.0,
            "ccb_aftercare_until": time.time() - 60,
        }))
        bridge = self._bridge(tmp)
        text = bridge.compact(bridge.load_snapshot("b1", "b1_user"))
        self.assertIn("未在场景", text)
        self.assertNotIn("余韵还剩", text, "过期的余韵不该再显示")


class EngineIntimacyPanelTest(unittest.TestCase):
    """面板【亲密】段：有 uid 报那个人的、没 uid 只报能力、没数据整段不出。"""

    def _engine_with_core(self, tmp: str) -> SocialEngine:
        _write_core(tmp, _core_root(
            b1_user={
                "mood": {"libido": 41.0, "base_libido": 34.0, "affection": 78.0},
                "ccb_stage": 3,
                "ccb_turns": 2,
                "ccb_satisfy": 62.0,
                "ccb_last_at": NOW - 3600,
                "ccb_aftercare_until": NOW + 2400,
                "ccb_done_count": 12,
            },
            other={"ccb_done_count": 1, "mood": {"libido": 30.0}},
        ))
        return _engine(tmp)

    def test_person_section_shows_scene_and_libido(self):
        eng = self._engine_with_core(tempfile.mkdtemp())
        text = eng._intimacy_text("b1", "b1_user")
        self.assertIn("【亲密】", text)
        self.assertIn("场景：进行中 · 阶段 3", text)
        self.assertIn("亲近欲：41/50", text)
        self.assertIn("基线 34", text)
        self.assertIn("上次 1 小时前", text)
        self.assertIn("一共 12 次", text)
        self.assertIn("余韵：还剩", text)
        self.assertIn("想要了通道", text)

    def test_without_a_uid_it_reports_capability_only(self):
        eng = self._engine_with_core(tempfile.mkdtemp())
        text = eng._intimacy_text("b1", "")
        self.assertIn("没有具体对话者", text)
        self.assertIn("能力：", text)
        self.assertNotIn("亲近欲：41", text, "拿不到具体人时不该出现任何人的数字")
        self.assertNotIn("阶段 3", text)

    def test_no_ccb_data_means_no_section_at_all(self):
        tmp = tempfile.mkdtemp()
        _write_core(tmp, _core_root(b1_user={"mood": {"libido": 30.0}}))
        eng = _engine(tmp)
        self.assertEqual(eng._intimacy_text("b1", "b1_user"), "",
                         "老 Core / ccb 关闭时面板多出了一个空段")
        self.assertEqual(eng._intimacy_text("b1", ""), "")

    def test_no_core_at_all_means_no_section(self):
        eng = _engine(tempfile.mkdtemp())
        self.assertEqual(eng._intimacy_text("b1", "b1_user"), "")
        self.assertEqual(eng._intimacy_text(), "")

    def test_status_page_only_appends_when_not_empty(self):
        src = inspect.getsource(SocialEngine.status_text)
        self.assertIn("if _intimacy:", src, "空段也会被塞进面板")
        self.assertIn("bid=bid, uid=uid", src, "状态页没把对话者传进【亲密】段")


class AgoTextTest(unittest.TestCase):
    def test_relative_time_is_computed_not_guessed(self):
        self.assertEqual(_ago_text(NOW - 5, NOW), "5 秒前")
        self.assertEqual(_ago_text(NOW - 300, NOW), "5 分钟前")
        self.assertEqual(_ago_text(NOW - 7300, NOW), "2 小时前")
        self.assertEqual(_ago_text(NOW - 90_000, NOW), "1 天前")
        self.assertEqual(_ago_text(0, NOW), "")


class SelfcheckTimeIsRealTest(unittest.IsolatedAsyncioTestCase):
    """`/社交自检` 的「上次结算」必须是时间戳算出来的，不是写死的「刚刚」。"""

    def _main(self):
        import importlib

        pkg = "as_plugin_for_status_test"
        if pkg not in sys.modules:
            module = types.ModuleType(pkg)
            module.__path__ = [str(PLUGIN_ROOT)]
            sys.modules[pkg] = module
        return importlib.import_module(pkg + ".main")

    def test_settle_line_uses_real_relative_time(self):
        main = self._main()
        text = main._selfcheck_settle_text(NOW, NOW - 7300, NOW)
        self.assertIn("2 小时前", text)
        self.assertIn("这次自检刚结算完", text)
        self.assertNotIn("刚刚", text, "写死的时间又回来了")

    def test_settle_line_without_history(self):
        main = self._main()
        self.assertEqual(main._selfcheck_settle_text(0, 0, NOW), "还没跑过")
        text = main._selfcheck_settle_text(NOW, 0, NOW)
        self.assertIn("这次启动还没跑过心跳结算", text)

    async def test_dry_run_keeps_the_previous_heartbeat_settle_time(self):
        eng = _engine(tempfile.mkdtemp())
        eng._m["last_settled"] = NOW - 3600
        eng._m["last_settled_users"] = 3
        await eng.dry_run()
        self.assertAlmostEqual(eng._m["prev_settled"], NOW - 3600, places=3,
                               msg="自检把上一次真实心跳的结算时间抹掉了")
        self.assertAlmostEqual(eng._m["last_settled"], NOW, places=3)
        eng._m["last_heartbeat"] = NOW - 60
        self.assertIn("自检前那次心跳结算", eng._metrics_text())

    def test_main_no_longer_hardcodes_jiji(self):
        src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("else '刚刚'", src, "「上次结算」又被写死成刚刚了")
        self.assertIn("_selfcheck_settle_text", src)


class CcbPresenceHelperTest(unittest.TestCase):
    """`_ccb_present` 是「要不要显示亲密」的唯一开关，边界必须钉住。"""

    def test_counts_only_real_ccb_keys(self):
        self.assertFalse(_ccb_present({"mood": {"libido": 40}}))
        self.assertFalse(_ccb_present(None))
        self.assertFalse(_ccb_present("坏"))
        self.assertTrue(_ccb_present({"ccb_stage": 0.0}))
        self.assertTrue(_ccb_present({"ccb_done_count": 1}))
        self.assertTrue(_ccb_present({"ccb_aftercare_until": NOW}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
