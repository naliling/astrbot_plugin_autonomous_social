"""六条管理指令的端到端回归 + v1.29.0 审计修掉的两处缺陷。

审计背景：/自主社交状态、/社交自检、/触发社交、/主动消息预览、/主动消息记录、
/群社交状态 六条指令此前**没有任何用例**——注册名写错、参数解析坏掉、或某条分支
抛异常，全量 1553 条测试一条都不会红。这个文件补上：

1. 六条指令都真的注册了、处理函数都在（名字是用户唯一能打的东西，错一个字就等于没有）；
2. 六条在「引擎未初始化」「非主人」这两种异常/权限路径下都给得出回执，不抛；
3. 六条在引擎真跑起来时都能产出文本（trigger/preview 没模型时给的是
   「没发出去 + 原因」，不是异常）；
4. 带参数的 /主动消息预览 <ID>、/主动消息记录 <ID> 参数真的被解析进去；
5. 群内 /触发社交 默认要求 @ 本 bot：没 @ 静默、@ 了别人静默、@ 自己才执行。

另外钉住本轮修掉的两处：

* **/主动消息预览 <不存在的ID> 会往 state.json 里写一条幽灵用户**。根因是
  _why_not_sending 走 state.user(bid, uid)，而那个访问器「取不到就新建」。
  预览是只读动作，查一个打错的 ID 不该永久多出一个候选用户。
* **/主动消息记录 的标题重复两遍**：指令回显给了一行
  「最近 7 天主动消息记录（按用户隔离）：」，引擎返回值里又自带
  「【最近 7 天主动消息】按用户隔离」。

按仓库惯例自带最小 astrbot 桩；为了能抓到 @command 的注册名，这里给 main.py
装一个**会记账**的 filter 桩，并用独立的合成包名加载它（不复用 pluginpkg，
避免与 test_v1273_review_fixes.py 的缓存互相干扰）。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


# ─── 最小 astrbot 桩（幂等，与 test_autonomous_social.py 同款）─────────
def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_commands_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = type(
        "_FakeMessageChain", (), {"message": lambda self, text: ("chain", text)}
    )
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social.engine import SocialEngine  # noqa: E402

#: main.py 里六条指令的 (注册名, 处理函数名)。名字是唯一对外契约。
COMMANDS = (
    ("自主社交状态", "social_status"),
    ("社交自检", "social_selfcheck"),
    ("触发社交", "trigger_social"),
    ("主动消息预览", "preview_proactive"),
    ("主动消息记录", "proactive_log"),
    ("群社交状态", "group_social_status"),
)

_PKG = "social_audit_pkg"
#: 这个模块里 filter.command 抓到的注册名（按定义顺序）
_REGISTERED: list = []


def _install_main_stub() -> None:
    """给 main.py 补齐 star / filter / AstrBotConfig，并让 command 记账。

    **每次调用都重装 filter 模块**：别的测试文件可能已经把
    astrbot.api.event.filter.command 换成了纯透传的桩，那样这里就抓不到注册名。
    重装只影响之后才 import 的模块，而 main.py 在下面用独立包名重新 exec。
    """
    api = sys.modules["astrbot.api"]
    if "astrbot.api.star" not in sys.modules:
        star = types.ModuleType("astrbot.api.star")

        class Context:
            pass

        class Star:
            def __init__(self, context=None):
                self.context = context

        star.Context = Context
        star.Star = Star
        sys.modules["astrbot.api.star"] = star
        api.star = star
    api.AstrBotConfig = dict

    filters = types.ModuleType("astrbot.api.event.filter")

    class EventMessageType:
        ALL = "all"

    def _command(name=None, **_kwargs):
        def _decorator(func):
            _REGISTERED.append(name)
            return func

        return _decorator

    def _passthrough(*_a, **_k):
        def _decorator(func=None):
            return func

        return _decorator

    filters.EventMessageType = EventMessageType
    filters.command = _command
    filters.event_message_type = _passthrough
    filters.on_llm_request = _passthrough
    filters.after_message_sent = _passthrough
    sys.modules["astrbot.api.event.filter"] = filters
    event = sys.modules["astrbot.api.event"]
    event.filter = filters
    event.AstrMessageEvent = object


def _load_main() -> types.ModuleType:
    """用独立包名加载 main.py，返回模块（模块级缓存）。"""
    cached = sys.modules.get(_PKG + ".main")
    if cached is not None:
        return cached
    _install_main_stub()

    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[_PKG] = pkg
    social_pkg = importlib.import_module("social")
    sys.modules[_PKG + ".social"] = social_pkg
    for name in ("config", "engine"):
        sys.modules[f"{_PKG}.social.{name}"] = importlib.import_module(
            f"social.{name}"
        )

    spec = importlib.util.spec_from_file_location(
        _PKG + ".main", os.path.join(str(PLUGIN_ROOT), "main.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PKG + ".main"] = module
    spec.loader.exec_module(module)
    return module


# ─── 测试替身 ────────────────────────────────────────────────────


class At:
    """@ 组件替身。

    _at_targets 在拿不到框架类（测试环境没有 astrbot.api.message_components）时
    **按类名比对**，所以这两个替身的类名必须与框架的 At / AtAll 逐字一致。
    """

    def __init__(self, qq: str) -> None:
        self.qq = qq


class AtAll:
    """@全体成员替身（同上：类名参与判定）。"""


class _Event:
    """够用的 AstrMessageEvent 替身。"""

    def __init__(
        self,
        text: str = "",
        *,
        private: bool = True,
        sender: str = "100",
        admin: bool = False,
        at: tuple = (),
        self_id: str = "bot1",
        group_id: str = "g1",
    ) -> None:
        self.message_str = text
        self._private = private
        self._sender = sender
        self._admin = admin
        self._at = list(at)
        self._self_id = self_id
        self._group_id = group_id
        kind = "FriendMessage" if private else "GroupMessage"
        target = sender if private else group_id
        self.unified_msg_origin = f"aiocqhttp:{kind}:{target}"
        self.message_obj = SimpleNamespace(
            message=[AtAll() if str(x) == "all" else At(str(x)) for x in self._at],
            group_id=None if private else group_id,
            self_id=self_id,
        )
        self.results: list = []

    def get_sender_id(self) -> str:
        return self._sender

    def get_self_id(self) -> str:
        return self._self_id

    def get_group_id(self) -> str:
        return self._group_id

    def is_private_chat(self) -> bool:
        return self._private

    def is_admin(self) -> bool:
        return self._admin

    def get_message_type(self):
        return "friend" if self._private else "group"

    def plain_result(self, text):
        self.results.append(text)
        return ("plain", text)


async def _drain(generator) -> list:
    out = []
    async for item in generator:
        out.append(item)
    return out


def _text_of(results: list) -> str:
    return "\n".join(str(r[1]) for r in results if isinstance(r, tuple) and len(r) == 2)


def _engine(**overrides) -> SocialEngine:
    """真引擎 + 临时目录。config 必须是原始 dict（见 test_v12733_options 的说明）。"""
    tmp = tempfile.mkdtemp()
    return SocialEngine(
        context=None,
        config=dict(overrides),
        state_path=str(Path(tmp) / "state.json"),
        data_dir=tmp,
    )


def _plugin(engine, *, admins=("100",)):
    """造一个只挂了必要依赖的 AutonomousSocial（不跑 __init__）。"""
    main_mod = _load_main()
    obj = object.__new__(main_mod.AutonomousSocial)
    obj._engine = engine
    obj.context = SimpleNamespace(get_config=lambda: {"admins_id": list(admins)})
    return obj


def _seed_user(engine, uid="u1", bid="bot1", **fields) -> dict:
    u = engine.state.user(bid, uid)
    u.update(
        {
            "umo": f"aiocqhttp:FriendMessage:{bid}-{uid}",
            "last_seen": engine._time() - 3600,
            "interest": 0.9,
            "urge": 5.0,
            "urge_at": engine._time() - 7200,
        }
    )
    u.update(fields)
    return u


# ─── 1. 注册与存在性 ────────────────────────────────────────────


class CommandRegistrationTest(unittest.TestCase):
    """六条指令必须真的挂在 @command 上，且处理函数都存在。"""

    def setUp(self) -> None:
        self.main_mod = _load_main()

    def test_all_six_registered_with_exact_names(self):
        self.assertEqual(
            _REGISTERED,
            [name for name, _fn in COMMANDS],
            "指令注册名与文档不一致（用户按 README 打的那六个字必须能命中）",
        )

    def test_no_extra_or_missing_command(self):
        self.assertEqual(len(_REGISTERED), 6, f"指令数量不对：{_REGISTERED}")

    def test_handlers_exist_and_are_async_generators(self):
        """六条都是 async generator（框架靠 async for 取回执），不能是普通函数。"""
        for _name, fn_name in COMMANDS:
            fn = getattr(self.main_mod.AutonomousSocial, fn_name, None)
            self.assertIsNotNone(fn, f"缺少处理函数 {fn_name}")
            self.assertTrue(
                inspect.isasyncgenfunction(fn) or asyncio.iscoroutinefunction(fn),
                f"{fn_name} 不是 async（框架调不起来）",
            )


# ─── 2. 异常 / 权限路径 ─────────────────────────────────────────


class CommandDegradedPathTest(unittest.IsolatedAsyncioTestCase):
    """引擎没起来、或者调用者不是主人时，六条都得给回执而不是抛异常。"""

    async def test_engine_none_returns_message_for_all_six(self):
        obj = _plugin(None)
        for name, fn_name in COMMANDS:
            ev = _Event("/" + name)
            results = await _drain(getattr(obj, fn_name)(ev))
            self.assertEqual(
                _text_of(results),
                "自主社交插件未正常初始化。",
                f"{name} 在引擎未初始化时没给回执",
            )

    async def test_non_owner_in_private_is_told(self):
        engine = _engine()
        obj = _plugin(engine, admins=("100",))
        for name, fn_name in COMMANDS:
            ev = _Event("/" + name, sender="999")
            results = await _drain(getattr(obj, fn_name)(ev))
            self.assertEqual(
                _text_of(results),
                "这个指令只有机器人的主人能用。",
                f"{name} 对非主人没给拒绝回执",
            )

    async def test_non_owner_in_group_is_silent(self):
        """群里拒绝本身会告诉所有群成员「这里有个能用的指令」，所以必须静默。"""
        engine = _engine()
        obj = _plugin(engine, admins=("100",))
        for name, fn_name in COMMANDS:
            ev = _Event("/" + name, private=False, sender="999")
            results = await _drain(getattr(obj, fn_name)(ev))
            self.assertEqual(results, [], f"{name} 在群里对非主人回了话")

    async def test_admin_flag_grants_access(self):
        engine = _engine()
        obj = _plugin(engine, admins=("100",))
        ev = _Event("/群社交状态", sender="999", admin=True)
        results = await _drain(obj.group_social_status(ev))
        self.assertIn("群聊主动", _text_of(results), "被 AstrBot 标为 admin 的人用不了指令")


# ─── 3. 正常运行路径 ────────────────────────────────────────────


class CommandHappyPathTest(unittest.IsolatedAsyncioTestCase):
    """引擎真跑起来之后，六条都要产出文本；没有模型时给的是原因，不是异常。"""

    async def asyncSetUp(self) -> None:
        self.engine = _engine()
        self.engine.start()
        _seed_user(self.engine)
        self.obj = _plugin(self.engine)

    async def asyncTearDown(self) -> None:
        try:
            await self.engine.stop()
        except Exception:
            pass

    async def test_status(self):
        results = await _drain(self.obj.social_status(_Event("/自主社交状态")))
        text = _text_of(results)
        self.assertIn("自主拟人社交", text)
        self.assertIn("权限：", text, "状态输出末尾没有权限判定依据")

    async def test_selfcheck(self):
        results = await _drain(self.obj.social_selfcheck(_Event("/社交自检")))
        text = _text_of(results)
        self.assertIn("自检完成", text)
        self.assertIn("已认识的人", text)
        self.assertNotIn("自检失败", text)

    async def test_trigger_returns_reason_not_traceback(self):
        results = await _drain(self.obj.trigger_social(_Event("/触发社交")))
        text = _text_of(results)
        self.assertTrue(text, "触发社交没有任何回执")
        self.assertNotIn("触发失败", text, f"触发社交抛异常了：{text[:200]}")

    async def test_preview_returns_text(self):
        results = await _drain(
            self.obj.preview_proactive(_Event("/主动消息预览 u1"))
        )
        text = _text_of(results)
        self.assertTrue(text, "主动消息预览没有任何回执")
        self.assertNotIn("预览失败", text, f"预览抛异常了：{text[:200]}")

    async def test_group_status(self):
        g = self.engine.state.group("bot1", "g1")
        g["last_seen"] = self.engine._time() - 60
        results = await _drain(self.obj.group_social_status(_Event("/群社交状态")))
        text = _text_of(results)
        self.assertIn("群聊主动", text)
        self.assertIn("g1", text, "在管的群没有出现在列表里")


# ─── 4. 参数解析 ────────────────────────────────────────────────


class CommandArgumentTest(unittest.IsolatedAsyncioTestCase):
    """/主动消息预览 [用户ID]、/主动消息记录 [用户ID] 的 ID 必须真的被用上。"""

    async def asyncSetUp(self) -> None:
        self.engine = _engine()
        self.engine.start()
        _seed_user(self.engine, uid="u1", name="小明")
        _seed_user(self.engine, uid="u2", name="小红")
        now = self.engine._time()
        self.engine.state.user("bot1", "u1")["proactive_log"] = [
            {"ts": now - 600, "text": "给 u1 的话", "why": "念想"}
        ]
        self.engine.state.user("bot1", "u2")["proactive_log"] = [
            {"ts": now - 300, "text": "给 u2 的话", "why": "挂事"}
        ]
        self.obj = _plugin(self.engine)

    async def asyncTearDown(self) -> None:
        try:
            await self.engine.stop()
        except Exception:
            pass

    async def test_log_without_arg_lists_everyone(self):
        text = _text_of(await _drain(self.obj.proactive_log(_Event("/主动消息记录"))))
        self.assertIn("给 u1 的话", text)
        self.assertIn("给 u2 的话", text)

    async def test_log_with_arg_filters_to_that_user(self):
        text = _text_of(
            await _drain(self.obj.proactive_log(_Event("/主动消息记录 u2")))
        )
        self.assertIn("给 u2 的话", text)
        self.assertNotIn("给 u1 的话", text, "带了用户 ID 却把别人也列出来了")

    async def test_preview_with_arg_targets_that_user(self):
        """带上 u1 时不该再「轮到别人」——过滤没生效就会从念头更高的人开始试。"""
        text = _text_of(
            await _drain(self.obj.preview_proactive(_Event("/主动消息预览 u1")))
        )
        self.assertNotIn("u2", text, "预览带了 u1 却试到了 u2")

    async def test_preview_unknown_id_is_explained(self):
        text = _text_of(
            await _drain(self.obj.preview_proactive(_Event("/主动消息预览 404404")))
        )
        self.assertIn("404404", text)


# ─── 5. 群内触发的 @ 门 ─────────────────────────────────────────


class GroupTriggerAtGateTest(unittest.IsolatedAsyncioTestCase):
    """多 bot 同群时，群里一条 /触发社交 不能把每台 bot 都叫醒。"""

    async def asyncSetUp(self) -> None:
        self.engine = _engine()
        self.engine.start()
        _seed_user(self.engine)
        self.obj = _plugin(self.engine)

    async def asyncTearDown(self) -> None:
        try:
            await self.engine.stop()
        except Exception:
            pass

    async def test_no_at_is_silent(self):
        ev = _Event("/触发社交", private=False, at=())
        self.assertEqual(await _drain(self.obj.trigger_social(ev)), [])

    async def test_at_other_bot_is_silent(self):
        ev = _Event("/触发社交", private=False, at=("bot2",))
        self.assertEqual(await _drain(self.obj.trigger_social(ev)), [])

    async def test_at_self_runs(self):
        ev = _Event("/触发社交", private=False, at=("bot1",))
        text = _text_of(await _drain(self.obj.trigger_social(ev)))
        self.assertTrue(text, "@ 了本 bot 却没有执行")

    async def test_at_all_runs(self):
        """@全体成员本来就该叫醒群里所有 bot。"""
        ev = _Event("/触发社交", private=False, at=("all",))
        self.assertTrue(_text_of(await _drain(self.obj.trigger_social(ev))))

    async def test_gate_can_be_turned_off(self):
        engine = _engine(trigger_group_requires_at=False)
        engine.start()
        _seed_user(engine)
        obj = _plugin(engine)
        try:
            ev = _Event("/触发社交", private=False, at=())
            self.assertTrue(
                _text_of(await _drain(obj.trigger_social(ev))),
                "关掉「群内触发须@本bot」后仍被静默跳过",
            )
        finally:
            await engine.stop()


# ─── 6. 本轮修掉的两处 ─────────────────────────────────────────


class PreviewIsReadOnlyTest(unittest.IsolatedAsyncioTestCase):
    """/主动消息预览 <不存在的ID> 不许往 state.json 里写幽灵用户。

    原来 _why_not_sending 走 state.user(bid, uid)，那个访问器取不到就新建：
    主人手滑打错一个 ID，state.json 里就永久多出一条空壳用户，状态页的
    「候选用户」跟着多一个，而它永远发不出任何东西。
    """

    async def asyncSetUp(self) -> None:
        self.engine = _engine()
        self.engine.start()
        _seed_user(self.engine, uid="u1")
        self.obj = _plugin(self.engine)

    async def asyncTearDown(self) -> None:
        try:
            await self.engine.stop()
        except Exception:
            pass

    def _uids(self) -> set:
        return set(self.engine.state.bot("bot1").get("users", {}))

    async def test_unknown_uid_creates_no_record(self):
        before = self._uids()
        await _drain(self.obj.preview_proactive(_Event("/主动消息预览 999999")))
        self.assertEqual(self._uids(), before, "预览一个不存在的 ID 造出了幽灵用户")

    async def test_unknown_uid_message_says_not_in_list(self):
        text = _text_of(
            await _drain(self.obj.preview_proactive(_Event("/主动消息预览 999999")))
        )
        self.assertIn("名单里没有", text, f"没有说清「根本没这个人」：{text[:120]}")

    async def test_known_uid_still_explains_gate(self):
        """反向对照：真人（在名单里、但没有 umo）仍要走原来的解释路径。"""
        self.engine.state.user("bot1", "u2")  # 有记录、没有 umo
        text = _text_of(
            await _drain(self.obj.preview_proactive(_Event("/主动消息预览 u2")))
        )
        self.assertIn("会话来源", text)


class ProactiveLogTitleTest(unittest.IsolatedAsyncioTestCase):
    """/主动消息记录 的标题只能出现一次（原来指令回显和引擎各写一行）。"""

    async def asyncSetUp(self) -> None:
        self.engine = _engine()
        self.engine.start()
        self.obj = _plugin(self.engine)

    async def asyncTearDown(self) -> None:
        try:
            await self.engine.stop()
        except Exception:
            pass

    async def test_empty_log_has_one_title(self):
        text = _text_of(await _drain(self.obj.proactive_log(_Event("/主动消息记录"))))
        self.assertIn("最近 7 天主动消息记录", text)
        self.assertNotIn("【最近 7 天主动消息】", text, f"标题重复了：{text}")

    async def test_with_rows_has_one_title(self):
        _seed_user(self.engine, uid="u1", name="小明")
        self.engine.state.user("bot1", "u1")["proactive_log"] = [
            {"ts": self.engine._time() - 60, "text": "在吗"}
        ]
        text = _text_of(await _drain(self.obj.proactive_log(_Event("/主动消息记录"))))
        self.assertIn("最近 7 天主动消息记录", text)
        self.assertNotIn("【最近 7 天主动消息】", text, f"标题重复了：{text}")
        self.assertIn("在吗", text)


# ─── 7. state.json 里的脏 bot 条目 ─────────────────────────────


class NullBotEntryTest(unittest.IsolatedAsyncioTestCase):
    """一条 null 的 bot 条目不该让三条管理指令一起失效。

    state.json 可能被手改、被旧版本或外部工具写坏，bots[bid] = null 就是其中
    最常见的一种残留。引擎自己在 try_once 里为此写过守卫，但面板/日志这几条
    路径仍然裸调 bot.get(...)：/自主社交状态 变成
    「获取状态失败: 'NoneType' object has no attribute 'get'」，
    /主动消息记录、/群社交状态 同样打不开——偏偏那正是需要看面板的时候。
    """

    async def asyncSetUp(self) -> None:
        self.engine = _engine()
        self.engine.start()
        _seed_user(self.engine, uid="u1", name="小明")
        self.engine.state.user("bot1", "u1")["proactive_log"] = [
            {"ts": self.engine._time() - 60, "text": "在吗"}
        ]
        # 脏条目：null / 字符串 / 列表各来一条
        self.engine.state.data["bots"]["ghost_null"] = None
        self.engine.state.data["bots"]["ghost_str"] = "broken"
        self.engine.state.data["bots"]["ghost_list"] = []
        self.obj = _plugin(self.engine)

    async def asyncTearDown(self) -> None:
        try:
            await self.engine.stop()
        except Exception:
            pass

    async def test_status_still_works(self):
        text = _text_of(await _drain(self.obj.social_status(_Event("/自主社交状态"))))
        self.assertIn("自主拟人社交", text, f"脏 bot 条目把面板打崩了：{text[:120]}")
        self.assertNotIn("获取状态失败", text)

    async def test_log_still_works(self):
        text = _text_of(await _drain(self.obj.proactive_log(_Event("/主动消息记录"))))
        self.assertIn("在吗", text, f"脏 bot 条目把记录打崩了：{text[:120]}")
        self.assertNotIn("获取失败", text)

    async def test_group_status_still_works(self):
        text = _text_of(await _drain(self.obj.group_social_status(_Event("/群社交状态"))))
        self.assertIn("群聊主动", text, f"脏 bot 条目把群状态打崩了：{text[:120]}")
        self.assertNotIn("获取失败", text)

    async def test_selfcheck_still_works(self):
        text = _text_of(await _drain(self.obj.social_selfcheck(_Event("/社交自检"))))
        self.assertIn("自检完成", text)


if __name__ == "__main__":
    unittest.main()
