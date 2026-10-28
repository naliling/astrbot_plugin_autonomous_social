"""v2.0.0：provider/config 域回归（P1-2 / P2-05 / P3-07 / P3-11 + 状态页体检接口）。

本文件钉住四件事：

1. **P1-2（发布阻断）**：默认 provider 不再指向作者私有 ID。
   ``{}`` → ``[]``（跟随会话默认）；老配置显式填了 ID 的仍按顺序尝试；
   配了却全部取不到时——默认回退会话模型并打**一次性** warning，
   打开 ``strict_proactive_provider`` 才「本轮不发」。
2. **P3-07**：布尔配置字符串归一。手改 JSON 写 ``"false"`` 不能再被 ``bool()`` 当成 True。
3. **P3-11**：4 个 list 配置默认 null → []，与 schema 一致，不再让 AstrBot 每次加载
   判定配置有变更并回写。
4. **P3-10 接口**：``MessageGenerator.proactive_provider_health()`` 给状态页报
   「专供 N/M 可用」，且取不到/异常时也绝不抛。

另外顺带钉住 docs-release 需要的两个声明值（P2-05 / P3-03 / P3-05 的口径来源）：
metadata 的 astrbot_version 下限、support_platforms 不含不存在的 wechat、心跳默认 8/30（上限 120）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if (_HERE.parent / "_conf_schema.json").is_file():
    PLUGIN_ROOT = _HERE.parent
else:
    PLUGIN_ROOT = _HERE.parent / "astrbot_plugin_autonomous_social"


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v200_config_test")
    event = types.ModuleType("astrbot.api.event")

    class _Chain:
        def message(self, text):
            return ("chain", text)

    event.MessageChain = _Chain()
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social.config import (  # noqa: E402
    DEFAULT_PROACTIVE_PROVIDERS,
    HEARTBEAT_MAX_MINUTES_MAX,
    SocialConfig,
    _as_bool,
    _provider_id_list,
)
from social.generator import MessageGenerator  # noqa: E402
from social.throttle import throttle  # noqa: E402

SCHEMA = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
BOOL_KEYS = [
    key
    for key, spec in SCHEMA.items()
    if isinstance(spec, dict) and spec.get("type") == "bool"
]
LIST_KEYS = [
    key
    for key, spec in SCHEMA.items()
    if isinstance(spec, dict) and spec.get("type") == "list"
]

#: 旧默认里的作者私有 provider ID（P1-2 的根因），任何形式的默认值都不许再出现。
AUTHOR_PRIVATE_IDS = (
    "openai/gpt-6.1-sol",
    "openai/gpt-6-sol",
    "gemini/gemini-3.5-flash",
)


# ── 假上下文 ──────────────────────────────────────────

class _Ctx:
    """最小 provider 上下文：按 ID 取 + 会话默认取。"""

    def __init__(self, designated=(), session_provider="session:default", raise_on_get=False):
        self.designated = set(designated)
        self.session_provider = session_provider
        self.session_calls = []
        self.raise_on_get = raise_on_get

    def get_provider_by_id(self, pid):
        if self.raise_on_get:
            raise RuntimeError("provider manager exploded")
        return f"designated:{pid}" if pid in self.designated else None

    async def get_using_provider_async(self, umo=None):
        self.session_calls.append(umo)
        return self.session_provider


class _CtxManagerOnly:
    """没有 get_provider_by_id、只有 provider_manager.inst_map 的旧/新混合形态。"""

    def __init__(self, mapping):
        class _Mgr:
            inst_map = dict(mapping)

        self.provider_manager = _Mgr()

    async def get_using_provider_async(self, umo=None):
        return "session:default"


class _CtxNoResolution:
    """连按 ID 取都做不到的 AstrBot 版本：必须放行到旧路径，不能全停。"""

    def __init__(self):
        self.session_calls = []

    async def get_using_provider_async(self, umo=None):
        self.session_calls.append(umo)
        return "session:default"


def _cfg(ids=None, strict=None):
    raw = {}
    if ids is not None:
        raw["proactive_provider_ids"] = ids
    if strict is not None:
        raw["strict_proactive_provider"] = strict
    return SocialConfig.from_astrbot(raw)


def _provider(gen):
    return asyncio.run(gen._get_provider("umo:1"))


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


# ── P1-2：默认值 / 解析 ───────────────────────────────

class DefaultProviderTest(unittest.TestCase):
    def test_default_constant_is_empty(self):
        self.assertEqual(tuple(DEFAULT_PROACTIVE_PROVIDERS), ())
        self.assertEqual(list(DEFAULT_PROACTIVE_PROVIDERS), [])

    def test_instance_default_is_empty_list(self):
        self.assertEqual(SocialConfig().proactive_provider_ids, [])

    def test_missing_key_means_follow_session(self):
        """`{}`（新装用户）→ 空链 = 跟随会话默认。"""
        self.assertEqual(SocialConfig.from_astrbot({}).proactive_provider_ids, [])

    def test_none_raw_means_follow_session(self):
        self.assertEqual(_provider_id_list(None), [])

    def test_schema_default_is_empty_list(self):
        self.assertEqual(SCHEMA["proactive_provider_ids"]["default"], [])
        self.assertEqual(SCHEMA["proactive_provider_ids"]["type"], "list")

    def test_no_author_private_id_in_defaults(self):
        """P1-2 回归：任何默认值里都不许再出现作者私有 provider ID。"""
        defaults = list(DEFAULT_PROACTIVE_PROVIDERS)
        self.assertEqual(defaults, [])
        for pid in AUTHOR_PRIVATE_IDS:
            self.assertNotIn(pid, defaults)
            self.assertNotIn(pid, SCHEMA["proactive_provider_ids"]["default"])

    def test_explicit_ids_are_kept(self):
        """老配置里用户显式配了 ID 的行为保留。"""
        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": ["openai/gpt-6.1-sol"]})
        self.assertEqual(cfg.proactive_provider_ids, ["openai/gpt-6.1-sol"])

    def test_explicit_empty_is_the_same_as_missing(self):
        self.assertEqual(_provider_id_list([]), [])
        self.assertEqual(_provider_id_list([""]), [])
        self.assertEqual(SocialConfig.from_astrbot({"proactive_provider_ids": []}).proactive_provider_ids, [])
        self.assertEqual(
            SocialConfig.from_astrbot({"proactive_provider_ids": None}).proactive_provider_ids,
            [],
        )

    def test_wrong_type_degrades_to_empty(self):
        self.assertEqual(_provider_id_list(42), [])
        self.assertEqual(_provider_id_list({"a": 1}), [])
        self.assertEqual(SocialConfig.from_astrbot({"proactive_provider_ids": 42}).proactive_provider_ids, [])

    def test_comma_text_still_parsed(self):
        self.assertEqual(_provider_id_list("a, b;c"), ["a", "b", "c"])

    def test_strict_default_is_false(self):
        self.assertFalse(SocialConfig().strict_proactive_provider)
        self.assertFalse(SCHEMA["strict_proactive_provider"]["default"])
        self.assertEqual(SCHEMA["strict_proactive_provider"]["type"], "bool")


# ── P1-2：生成器取 provider 的行为 ─────────────────────

class GetProviderBehaviorTest(unittest.TestCase):
    def setUp(self):
        throttle.reset("provider.designated.fallback")
        throttle.reset("provider.designated.unsupported")

    def test_explicit_ids_are_still_tried_in_order(self):
        ctx = _Ctx(designated={"b"})
        gen = MessageGenerator(context=ctx, config=_cfg(ids=["a", "b"]))
        self.assertEqual(_provider(gen), "designated:b")
        self.assertEqual(ctx.session_calls, [], "配了专供且取到了，不该动会话默认")

    def test_all_missing_falls_back_to_session_by_default(self):
        """P1-2 核心：默认回退会话模型，而不是本轮不发。"""
        ctx = _Ctx(designated=set())
        gen = MessageGenerator(context=ctx, config=_cfg(ids=["a", "b"]))
        self.assertEqual(_provider(gen), "session:default")
        self.assertEqual(len(ctx.session_calls), 1)

    def test_all_missing_strict_keeps_not_sending(self):
        """strict=True 保留 v1.28.5「本轮不发」的行为。"""
        ctx = _Ctx(designated=set())
        gen = MessageGenerator(context=ctx, config=_cfg(ids=["a"], strict=True))
        self.assertIsNone(_provider(gen))
        self.assertEqual(ctx.session_calls, [])

    def test_strict_string_true_is_parsed_as_bool(self):
        """手改 JSON 写成 "true" 也必须认成开启（P3-07 同一套归一）。"""
        ctx = _Ctx(designated=set())
        gen = MessageGenerator(context=ctx, config=_cfg(ids=["a"], strict="true"))
        self.assertTrue(gen.cfg.strict_proactive_provider)
        self.assertIsNone(_provider(gen))

    def test_fallback_warning_is_logged_only_once(self):
        """一次性 warning：同一恢复窗口内连打两轮也只记一条。"""
        ctx = _Ctx(designated=set())
        gen = MessageGenerator(context=ctx, config=_cfg(ids=["a", "b"]))
        # 直接挂在生成器模块自己的 logger 上：不同测试文件会各自替换 astrbot 桩
        # （logger 名不同），而 test_v1241 导入 _sim_support 时把那个 logger
        # 的 propagate 设成了 False——挂 root 会漏捕，挂模块 logger 才稳。
        from social import generator as generator_module

        capture = _LogCapture()
        generator_module.logger.addHandler(capture)
        try:
            _provider(gen)
            _provider(gen)
        finally:
            generator_module.logger.removeHandler(capture)
        hits = [r for r in capture.records if "已回退跟随当前会话的模型" in r.getMessage()]
        self.assertEqual(len(hits), 1, f"warning 没有去重：{[r.getMessage() for r in capture.records]}")
        self.assertIn("strict_proactive_provider", hits[0].getMessage())

    def test_unsupported_version_falls_through_even_in_strict(self):
        """版本根本不支持按 ID 取时，严格模式也不该把主动消息全停。"""
        ctx = _CtxNoResolution()
        gen = MessageGenerator(context=ctx, config=_cfg(ids=["a"], strict=True))
        self.assertEqual(_provider(gen), "session:default")
        self.assertEqual(len(ctx.session_calls), 1)

    def test_legacy_provider_manager_map_still_works(self):
        ctx = _CtxManagerOnly({"b": "designated:b"})
        gen = MessageGenerator(context=ctx, config=_cfg(ids=["a", "b"]))
        self.assertEqual(_provider(gen), "designated:b")


# ── P3-10：状态页体检接口 ─────────────────────────────

class ProviderHealthTest(unittest.TestCase):
    def test_unconfigured(self):
        ctx = _Ctx()
        health = MessageGenerator(context=ctx, config=_cfg(ids=[])).proactive_provider_health()
        self.assertFalse(health["configured"])
        self.assertEqual(health["total"], 0)
        self.assertEqual(health["resolvable"], 0)
        self.assertFalse(health["strict"])
        self.assertIn("跟着当前会话", health["detail"])
        self.assertNotIn("\n", health["detail"])

    def test_partially_resolvable_reports_n_of_m(self):
        ctx = _Ctx(designated={"b"})
        health = MessageGenerator(context=ctx, config=_cfg(ids=["a", "b", "c"])).proactive_provider_health()
        self.assertTrue(health["configured"])
        self.assertEqual(health["total"], 3)
        self.assertEqual(health["resolvable"], 1)
        self.assertEqual(health["ids"], ["a", "b", "c"])
        self.assertIn("1/3", health["detail"])

    def test_none_resolvable_default_mode_says_fallback(self):
        ctx = _Ctx(designated=set())
        health = MessageGenerator(context=ctx, config=_cfg(ids=["a", "b"])).proactive_provider_health()
        self.assertEqual(health["resolvable"], 0)
        self.assertIn("0/2", health["detail"])
        self.assertIn("回退", health["detail"])
        self.assertIn("strict_proactive_provider", health["detail"])

    def test_none_resolvable_strict_mode_says_strict(self):
        ctx = _Ctx(designated=set())
        health = MessageGenerator(
            context=ctx, config=_cfg(ids=["a"], strict=True)
        ).proactive_provider_health()
        self.assertTrue(health["strict"])
        self.assertIn("严格模式", health["detail"])

    def test_provider_manager_map_counts_as_resolvable(self):
        ctx = _CtxManagerOnly({"a": "designated:a"})
        health = MessageGenerator(context=ctx, config=_cfg(ids=["a", "b"])).proactive_provider_health()
        self.assertEqual(health["resolvable"], 1)
        self.assertEqual(health["total"], 2)

    def test_never_raises_even_if_provider_lookup_throws(self):
        ctx = _Ctx(raise_on_get=True)
        health = MessageGenerator(context=ctx, config=_cfg(ids=["a"])).proactive_provider_health()
        self.assertEqual(health["resolvable"], 0)
        self.assertIsInstance(health["detail"], str)
        for key in ("configured", "resolvable", "total", "ids", "strict", "detail"):
            self.assertIn(key, health)


# ── P3-07：布尔归一 ──────────────────────────────────

class BoolNormalizationTest(unittest.TestCase):
    def test_false_writings(self):
        for value in ("false", "False", " FALSE ", "0", "no", "off", "n", "f"):
            with self.subTest(value=value):
                self.assertIs(_as_bool(value, True), False)

    def test_true_writings(self):
        for value in ("true", "True", " TRUE ", "1", "yes", "on", "y", "t"):
            with self.subTest(value=value):
                self.assertIs(_as_bool(value, False), True)

    def test_numbers_and_bools_pass_through(self):
        self.assertIs(_as_bool(True, False), True)
        self.assertIs(_as_bool(False, True), False)
        self.assertIs(_as_bool(0, True), False)
        self.assertIs(_as_bool(1, False), True)
        self.assertIs(_as_bool(0.0, True), False)
        self.assertIs(_as_bool(2, False), True)
        # 数字形态的字符串也按数字语义处理（手改 JSON 写成 "2"/"0.0"）
        self.assertIs(_as_bool("0.0", True), False)
        self.assertIs(_as_bool("2", False), True)

    def test_none_uses_default(self):
        self.assertIs(_as_bool(None, True), True)
        self.assertIs(_as_bool(None, False), False)

    def test_unrecognized_degrades_to_default_without_raising(self):
        self.assertIs(_as_bool("maybe", True, "debug"), True)
        self.assertIs(_as_bool("maybe", False, "debug"), False)
        self.assertIs(_as_bool(["x"], True, "debug"), True)
        self.assertIs(_as_bool({"x": 1}, False, "debug"), False)

    def test_from_astrbot_false_strings(self):
        """审核 P3-07 的原始复现：`{"enabled":"false","debug":"0"}` 以前是 True。"""
        cfg = SocialConfig.from_astrbot({"enabled": "false", "debug": "0"})
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.debug)

    def test_every_schema_bool_accepts_false_string(self):
        """全文件布尔项统一走 _as_bool：每个 bool 配置写 "false" 读出来都得是 False。"""
        self.assertTrue(BOOL_KEYS, "schema 里一个 bool 配置都没有，测试本身失效了")
        cfg = SocialConfig.from_astrbot({key: "false" for key in BOOL_KEYS})
        wrong = [key for key in BOOL_KEYS if getattr(cfg, key, None) is not False]
        self.assertEqual(wrong, [], f"这些布尔项把 'false' 读成了真：{wrong}")

    def test_every_schema_bool_accepts_true_string(self):
        cfg = SocialConfig.from_astrbot({key: "true" for key in BOOL_KEYS})
        wrong = [key for key in BOOL_KEYS if getattr(cfg, key, None) is not True]
        self.assertEqual(wrong, [], f"这些布尔项把 'true' 读成了假：{wrong}")

    def test_number_values_still_work(self):
        cfg = SocialConfig.from_astrbot({"enabled": 0, "debug": 1})
        self.assertFalse(cfg.enabled)
        self.assertTrue(cfg.debug)


# ── P3-11：list 默认 null → [] ────────────────────────

FOUR_LIST_KEYS = (
    "allowed_trigger_uids",
    "tier_override",
    "special_care_uids",
    "seed_users",
)


class ListDefaultsTest(unittest.TestCase):
    def test_schema_defaults_are_empty_lists(self):
        for key in FOUR_LIST_KEYS:
            with self.subTest(key=key):
                self.assertEqual(SCHEMA[key]["type"], "list")
                self.assertEqual(SCHEMA[key]["default"], [])

    def test_dataclass_defaults_are_empty_lists(self):
        cfg = SocialConfig()
        for key in FOUR_LIST_KEYS:
            with self.subTest(key=key):
                self.assertEqual(getattr(cfg, key), [])

    def test_from_astrbot_defaults_are_empty_lists(self):
        cfg = SocialConfig.from_astrbot({})
        for key in FOUR_LIST_KEYS:
            with self.subTest(key=key):
                self.assertEqual(getattr(cfg, key), [])

    def test_explicit_null_is_normalized_to_empty_list(self):
        """老配置 JSON 里显式写的 null 也要归一成 []，否则 AstrBot 仍会判配置有变更。"""
        cfg = SocialConfig.from_astrbot({key: None for key in FOUR_LIST_KEYS})
        for key in FOUR_LIST_KEYS:
            with self.subTest(key=key):
                self.assertEqual(getattr(cfg, key), [])

    def test_no_null_defaults_left_in_schema(self):
        bad = [
            key
            for key, spec in SCHEMA.items()
            if isinstance(spec, dict) and "default" in spec and spec["default"] is None
        ]
        self.assertEqual(bad, [], f"这些配置项默认值还是 null：{bad}")

    def test_empty_lists_are_tolerated_by_parsers(self):
        cfg = SocialConfig()
        self.assertEqual(cfg.allowed_trigger_uid_set(), set())
        self.assertEqual(cfg.special_care_entries(), set())
        self.assertEqual(cfg.tier_override_for("12345"), "")

    def test_legacy_comma_text_still_works(self):
        """gl() 不能把旧的逗号文本形态改成 []——老配置仍要能解析。"""
        cfg = SocialConfig.from_astrbot({"allowed_trigger_uids": "1, 2"})
        self.assertEqual(cfg.allowed_trigger_uid_set(), {"1", "2"})
        cfg2 = SocialConfig.from_astrbot({"special_care_uids": "aiocqhttp:1,2"})
        self.assertIn("aiocqhttp:1", cfg2.special_care_entries())


# ── 声明值 / 文档口径（P2-05 / P3-03 / P3-05）────────

class MetadataDeclarationTest(unittest.TestCase):
    @staticmethod
    def _metadata_text() -> str:
        return (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")

    def test_astrbot_version_floor_is_4_24_2(self):
        """P2-05：mark_as_temp 等 API 的实际下限；附录 B 实测。"""
        text = self._metadata_text()
        self.assertRegex(text, r"astrbot_version:\s*[\"']?\s*>=\s*4\.24\.2")

    def test_support_platforms_use_real_adapter_ids(self):
        """P3-03：AstrBot 没有 `wechat` 这个平台 id（4.29.3 实测）。"""
        lines = self._metadata_text().splitlines()
        platforms = []
        in_block = False
        for line in lines:
            if line.strip().startswith("support_platforms:"):
                in_block = True
                continue
            if in_block:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("- "):
                    platforms.append(stripped[2:].strip())
                else:
                    break
        self.assertIn("aiocqhttp", platforms)
        self.assertNotIn("wechat", platforms, f"不存在的平台 id：{platforms}")
        # 只允许 AstrBot 4.29.3 core/platform/sources 里真实存在的 adapter id。
        real_ids = {
            "aiocqhttp", "dingtalk", "discord", "kook", "lark", "line",
            "mattermost", "misskey", "qq_official", "qq_official_webhook",
            "satori", "slack", "telegram", "webchat", "wecom", "wecom_ai_bot",
            "weixin_oc", "weixin_official_account",
        }
        unknown = [p for p in platforms if p not in real_ids]
        self.assertEqual(unknown, [], f"metadata 声明的平台 id 不存在：{unknown}")


class HeartbeatValuesTest(unittest.TestCase):
    """P3-05：给文档/排障用的心跳口径 = 默认 8–30，可配上限 120。"""

    def test_defaults(self):
        cfg = SocialConfig()
        self.assertEqual(cfg.heartbeat_min_minutes, 8)
        self.assertEqual(cfg.heartbeat_max_minutes, 30)

    def test_schema_matches(self):
        self.assertEqual(SCHEMA["heartbeat_min_minutes"]["default"], 8)
        self.assertEqual(SCHEMA["heartbeat_max_minutes"]["default"], 30)
        self.assertEqual(SCHEMA["heartbeat_min_minutes"]["max"], 60)
        self.assertEqual(SCHEMA["heartbeat_max_minutes"]["max"], 120)
        self.assertEqual(HEARTBEAT_MAX_MINUTES_MAX, 120)

    def test_out_of_range_values_are_clamped(self):
        cfg = SocialConfig.from_astrbot(
            {"heartbeat_min_minutes": 999, "heartbeat_max_minutes": 10000}
        )
        self.assertEqual(cfg.heartbeat_min_minutes, 60)
        self.assertEqual(cfg.heartbeat_max_minutes, 120)


if __name__ == "__main__":
    unittest.main()
