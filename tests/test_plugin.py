"""插件端到端自测：在仿真运行时上验证 driver / engine / runner / web api。

    python3 -m unittest discover -s tests -v
"""

import asyncio
import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import astrbot_stub  # noqa: E402

MGR = astrbot_stub.install()


def _load_plugin():
    """在桩注入之后导入插件模块。"""
    import importlib

    for name in list(sys.modules):
        if name.startswith("astrbot_plugin_fulltest"):
            del sys.modules[name]
    main = importlib.import_module("astrbot_plugin_fulltest.main")
    driver = importlib.import_module("astrbot_plugin_fulltest.driver")
    cases = importlib.import_module("astrbot_plugin_fulltest.cases")
    engine = importlib.import_module("astrbot_plugin_fulltest.engine")
    runner = importlib.import_module("astrbot_plugin_fulltest.runner")
    return main, driver, cases, engine, runner


MAIN, DRIVER, CASES, ENGINE, RUNNER = _load_plugin()


def _make_plugin(context):
    """构造插件实例，并把 llm_tool 注册进桩的工具管理器。"""
    plugin = MAIN.FullTestPlugin(context, config={})
    manager = context.get_llm_tool_manager()
    for tool_name, attr in astrbot_stub.collect_tool_names(MAIN.FullTestPlugin):
        manager.register(astrbot_stub._FakeTool(tool_name, getattr(plugin, attr)))
    return plugin


class PluginTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = astrbot_stub._FakeContext()
        self.plugin = _make_plugin(self.context)
        self.pipeline = astrbot_stub.FakePipeline(self.context, MGR, memory={})
        await self.pipeline.start()
        self.addAsyncCleanup(self.pipeline.stop)


class TestDriver(PluginTestBase):
    async def test_basic_chat_collects_text(self):
        driver = DRIVER.ChainDriver(self.context)
        turn = await driver.send("你好", timeout=5)
        self.assertTrue(turn.ok, turn.error)
        self.assertTrue(turn.text)
        self.assertEqual(turn.tool_calls, [])
        self.assertTrue(turn.session_id.startswith("conv_"))

    async def test_tool_call_parsing_openai_style(self):
        driver = DRIVER.ChainDriver(self.context)
        turn = await driver.send("请用 testkit_echo 工具回显：hello-toolkit", timeout=5)
        self.assertTrue(turn.ok, turn.error)
        self.assertEqual(turn.tool_names(), ["testkit_echo"])
        call = turn.find_call("testkit_echo")
        self.assertEqual(call["arguments"]["text"], "hello-toolkit")
        self.assertEqual(ENGINE.all_results_matched(turn), True)
        self.assertTrue(turn.reasoning)

    async def test_multistep_tool_calls(self):
        driver = DRIVER.ChainDriver(self.context)
        turn = await driver.send("请用 testkit_two_step 计算 3 和 4", timeout=5)
        self.assertGreaterEqual(len(turn.tool_calls), 2)
        self.assertEqual(ENGINE.all_results_matched(turn), True)

    async def test_same_conversation_keeps_memory(self):
        driver = DRIVER.ChainDriver(self.context)
        first = await driver.send("记住我的幸运数字是 42", timeout=5)
        second = await driver.send("我的幸运数字是多少？",
                                   conversation_id=first.session_id, timeout=5)
        self.assertIn("42", second.text)

    async def test_different_conversations_isolated(self):
        driver = DRIVER.ChainDriver(self.context)
        a = await driver.send("记住我的幸运数字是 777", timeout=5)
        b = await driver.send("我的幸运数字是多少？",
                              conversation_id=a.session_id, timeout=5)
        c = await driver.send("我的幸运数字是多少？", timeout=5)
        self.assertNotEqual(a.session_id, c.session_id)
        self.assertNotIn("777", c.text)
        self.assertIn("777", b.text)

    async def test_flags_are_propagated_to_event(self):
        driver = DRIVER.ChainDriver(self.context)
        seen = {}
        original = driver._build_event

        def spy(*a, **k):
            event = original(*a, **k)
            seen["flags"] = event.get_extra("flags")
            return event

        driver._build_event = spy
        await driver.send("你好", flags={"enable_streaming": False}, timeout=5)
        self.assertFalse(seen["flags"]["enable_streaming"])
        self.assertTrue(seen["flags"]["enable_reasoning"])

    async def test_back_queue_is_cleaned_up(self):
        driver = DRIVER.ChainDriver(self.context)
        turn = await driver.send("你好", timeout=5)
        self.assertNotIn(turn.run_id, MGR.back_queues)

    async def test_timeout_marks_turn(self):
        self.pipeline.task.cancel()
        await asyncio.sleep(0)
        driver = DRIVER.ChainDriver(self.context)
        turn = await driver.send("你好", timeout=0.3)
        self.assertTrue(turn.timed_out)
        self.assertFalse(turn.ok)


class TestEngine(PluginTestBase):
    async def test_assertion_levels_split_status(self):
        """require 不满足判 FAIL，expect 不满足只判 WARN。"""
        driver = DRIVER.ChainDriver(self.context)
        selected = [c for c in CASES.CASES if c.id == "tool-echo"]
        report = ENGINE.Report("run_lvl")
        await ENGINE.Suite(driver, {"timeout": 5, "concurrency": 1}).run(
            selected, report)
        result = report.results[0]
        levels = {c.level for c in result.checks}
        self.assertTrue(levels.issubset({"require", "expect", "note"}))
        for c in result.checks:
            if c.level == "require":
                self.assertTrue(c.ok, f"require 断言不应失败: {c.message}")

    async def test_suite_runs_selected_cases(self):
        driver = DRIVER.ChainDriver(self.context)
        selected = [c for c in CASES.CASES if c.group == "tool_core"]
        report = ENGINE.Report("run_x")
        suite = ENGINE.Suite(driver, {"timeout": 5, "concurrency": 1})
        await suite.run(selected, report)
        self.assertTrue(report.finished)
        self.assertEqual(len(report.results), len(selected))
        for r in report.results:
            self.assertNotEqual(r.status, "FAIL",
                                f"{r.name}: {r.error or r.failed_checks}")

    async def test_concurrency_path(self):
        driver = DRIVER.ChainDriver(self.context)
        selected = [c for c in CASES.CASES if c.id in ("tool-noop", "tool-number")]
        report = ENGINE.Report("run_c")
        suite = ENGINE.Suite(driver, {"timeout": 5, "concurrency": 4})
        await suite.run(selected, report)
        self.assertEqual(len(report.results), 2)
        self.assertTrue(report.finished)

    async def test_broken_case_becomes_fail_without_stopping_run(self):
        async def boom(ctx):
            raise RuntimeError("用例内部炸了")

        async def fine(ctx):
            ctx.require(True, "正常用例断言")

        broken = ENGINE.Case("x", "故意失败", "test", boom)
        good = ENGINE.Case("y", "正常用例", "test", fine)
        driver = DRIVER.ChainDriver(self.context)
        report = ENGINE.Report("run_e")
        await ENGINE.Suite(driver, {"timeout": 5, "concurrency": 1}).run(
            [broken, good], report)
        self.assertEqual(len(report.results), 2)
        failed = [r for r in report.results if r.status == "FAIL"]
        self.assertEqual(len(failed), 1, "仅故意失败的用例应为 FAIL")
        self.assertIn("用例内部炸了", failed[0].error)
        self.assertEqual([r.status for r in report.results if r.name == "正常用例"],
                         ["PASS"])

    async def test_skipped_case(self):
        skipped = ENGINE.Case("s", "跳过项", "test", lambda ctx: None,
                              skip_reason="环境不满足")
        driver = DRIVER.ChainDriver(self.context)
        report = ENGINE.Report("run_s")
        await ENGINE.Suite(driver, {"timeout": 5, "concurrency": 1}).run(
            [skipped], report)
        self.assertEqual(report.results[0].status, "SKIP")

    async def test_report_serialisation(self):
        driver = DRIVER.ChainDriver(self.context)
        selected = [c for c in CASES.CASES if c.id == "tool-echo"]
        report = ENGINE.Report("run_j")
        await ENGINE.Suite(driver, {"timeout": 5, "concurrency": 1}).run(
            selected, report)
        data = report.to_dict()
        self.assertIn("counts", data)
        self.assertIn("summary", data)
        text = report.to_text()
        self.assertIn("函数调用-字符串参数", text)


class TestRunner(PluginTestBase):
    async def test_start_and_complete(self):
        result = await self.plugin.runs.start(
            groups=["tool_core"], concurrency=1, timeout=5)
        self.assertIn("run_id", result)
        run = self.plugin.runs.get(result["run_id"])
        for _ in range(200):
            if run.report.finished:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(run.report.finished)
        self.assertEqual(run.state, "done")
        self.assertEqual(run.done, result["total"])
        self.assertEqual(run.report.counts()["FAIL"], 0,
                         f"桩环境下 tool_core 应全部通过: "
                         f"{[(r.name, r.error or [c.message for c in r.failed_checks]) for r in run.report.results if r.status == 'FAIL']}")

    async def test_events_published_for_subscribers(self):
        import json as _json

        result = await self.plugin.runs.start(groups=["system"], timeout=5)
        run = self.plugin.runs.get(result["run_id"])
        queue = asyncio.Queue(maxsize=200)
        run.subscribers.add(queue)
        for _ in range(200):
            if run.report.finished:
                break
            await asyncio.sleep(0.05)
        raw = []
        while not queue.empty():
            raw.append(queue.get_nowait())

        # 订阅队列收到的是 SSE 负载（JSON 字符串）或 EOF 标记
        kinds = set()
        saw_eof = False
        for item in raw:
            if item == RUNNER.EOF_MARKER:
                saw_eof = True
                continue
            kinds.add(_json.loads(item)["kind"])
        self.assertIn("case_start", kinds)
        self.assertIn("run_state", kinds)
        self.assertIn("run_done", kinds)
        self.assertTrue(saw_eof, "运行结束应向订阅者投递 EOF 标记")

        # 事件存档则是结构化 dict，供刷新页面后回放
        archived = [e["kind"] for e in self.plugin.runs.recent_events(run.run_id)]
        self.assertIn("case_done", archived)

    async def test_rejects_concurrent_run(self):
        first = await self.plugin.runs.start(groups=["endurance"], timeout=5)
        second = await self.plugin.runs.start(groups=["system"], timeout=5)
        self.assertIn("error", second)
        run = self.plugin.runs.get(first["run_id"])
        self.plugin.runs.cancel(run.run_id)
        for _ in range(100):
            if run.report.finished:
                break
            await asyncio.sleep(0.05)

    async def test_filter_by_keyword(self):
        result = await self.plugin.runs.start(only="tool-number", timeout=5)
        self.assertEqual(result["total"], 1)

    async def test_no_match_returns_error(self):
        result = await self.plugin.runs.start(only="不存在的用例xyz", timeout=5)
        self.assertIn("error", result)

    async def test_catalog_shape(self):
        catalog = self.plugin.runs.catalog()
        self.assertEqual(catalog["total"], len(CASES.CASES))
        self.assertTrue(catalog["groups"])
        for item in catalog["cases"]:
            self.assertIn("id", item)
            self.assertIn("group", item)

    async def test_latest_and_current(self):
        result = await self.plugin.runs.start(groups=["system"], timeout=5)
        run = self.plugin.runs.get(result["run_id"])
        for _ in range(200):
            if run.report.finished:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.plugin.runs.latest().run_id, run.run_id)
        self.assertIsNone(self.plugin.runs.current())


class TestWebApi(PluginTestBase):
    async def test_routes_registered(self):
        routes = self.context.web_apis
        expected = [
            f"/{MAIN.PLUGIN_NAME}/catalog",
            f"/{MAIN.PLUGIN_NAME}/runs",
            f"/{MAIN.PLUGIN_NAME}/start",
            f"/{MAIN.PLUGIN_NAME}/cancel",
            f"/{MAIN.PLUGIN_NAME}/report/<run_id>",
            f"/{MAIN.PLUGIN_NAME}/events/<run_id>",
            f"/{MAIN.PLUGIN_NAME}/selfcheck",
        ]
        for route in expected:
            self.assertIn(route, routes, f"未注册路由 {route}")

    async def test_catalog_endpoint(self):
        res = await self.plugin.api_catalog()
        self.assertEqual(res["__json__"]["total"], len(CASES.CASES))

    async def test_selfcheck_endpoint(self):
        res = await self.plugin.api_selfcheck()
        data = res["__json__"]
        self.assertTrue(data["ready"], data)
        self.assertEqual(data["tools_missing"], [])
        self.assertEqual(data["tools_disabled"], [])

    async def test_start_endpoint_and_report(self):
        res = await self.plugin.api_start()
        data = res["__json__"]
        self.assertIn("run_id", data)
        run = self.plugin.runs.get(data["run_id"])
        self.plugin.runs.cancel(run.run_id)
        for _ in range(200):
            if run.report.finished:
                break
            await asyncio.sleep(0.05)
        report = await self.plugin.api_report(run.run_id)
        self.assertIn("report", report["__json__"])

    async def test_report_missing_run(self):
        res = await self.plugin.api_report("run_not_exist")
        self.assertIn("__error__", res)
        self.assertEqual(res["status"], 404)

    async def test_runs_endpoint(self):
        res = await self.plugin.api_runs()
        self.assertIn("runs", res["__json__"])


class TestPluginTools(PluginTestBase):
    async def test_all_tools_registered(self):
        manager = self.context.get_llm_tool_manager()
        registered = {t.name for t in manager.func_list}
        tools_mod = importlib.import_module("astrbot_plugin_fulltest.tools")
        for name in tools_mod.TOOL_NAMES:
            self.assertIn(name, registered, f"工具未注册: {name}")
        for name in ("fulltest_run", "fulltest_status", "fulltest_report"):
            self.assertIn(name, registered, f"测试工具未注册: {name}")

    async def test_tool_returns_typed_params(self):
        tools_mod = importlib.import_module("astrbot_plugin_fulltest.tools")
        out = await self.plugin.testkit_add(self._event(), 17, 25)
        self.assertIn("42", out)
        self.assertIn("testkit_add", out)

    async def test_fulltest_run_tool(self):
        event = self._event()
        out = await self.plugin.fulltest_run(event, group="system")
        self.assertIn("已启动", out)
        run = self.plugin.runs.current()
        self.assertIsNotNone(run)
        self.plugin.runs.cancel(run.run_id)
        for _ in range(200):
            if run.report.finished:
                break
            await asyncio.sleep(0.05)

    async def test_fulltest_status_tool(self):
        out = await self.plugin.fulltest_status(self._event())
        self.assertIn("尚未运行过测试", out)

    async def test_fulltest_report_tool_empty(self):
        out = await self.plugin.fulltest_report(self._event())
        self.assertIn("尚未运行过测试", out)

    def _event(self):
        import types as _t

        return _t.SimpleNamespace(plain_result=lambda x: x)


class TestCommands(PluginTestBase):
    async def test_fulltest_command_rejects_unknown_group(self):
        out = []

        async def collect():
            async for item in self.plugin.fulltest_cmd(self._event(), "nope"):
                out.append(item)

        await collect()
        self.assertTrue(out)
        self.assertIn("未知分组", out[0])

    async def test_fulltest_command_status_without_run(self):
        out = []

        async def collect():
            async for item in self.plugin.fulltest_cmd(self._event(), "status"):
                out.append(item)

        await collect()
        self.assertIn("尚未运行过测试", out[0])

    async def test_testkit_ping_command(self):
        out = []

        async def collect():
            async for item in self.plugin.testkit_ping(self._event()):
                out.append(item)

        await collect()
        self.assertIn("testkit_pong", out[0])

    def _event(self):
        import types as _t

        return _t.SimpleNamespace(plain_result=lambda x: x)


if __name__ == "__main__":
    unittest.main()
