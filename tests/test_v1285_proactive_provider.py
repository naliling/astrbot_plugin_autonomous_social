"""v1.28.5：主动消息可以指定专用模型（v2.0.0 改默认 + 增加严格开关）。

## 为什么

以前这个插件每一次模型调用都跟着**当前会话的** provider 走
（`get_using_provider(umo)`）——群里换个模型，她主动找你说话的质量也跟着变。
而主动消息恰恰是最不该省的那一次：那是她**自己想起来**要说什么。

## 规矩（v2.0.0 起）

- 默认 **空**：不指定专供，跟着当前会话的模型走；
  （v1.28.5 的默认是作者自己的三个 provider ID——标准安装一个都解析不到，
  所有主动消息静默全停，审核 P1-2 发布阻断项。）
- 想用强模型：面板上显式填 provider ID 列表，按顺序试到第一个能用的即止；
- 配了却一个都取不到：默认**回退会话模型 + 一次性 warning**；
  打开 `strict_proactive_provider` 才保留「本轮不发、等下一轮」的旧行为。

## 本文件盯的

1. 列表解析：list / 逗号文本 / 分号 / 换行都认，去重保序，非字符串跳过；
2. 默认值：`{}` → 空链（跟随会话），schema 与 dataclass 一致；
3. 取得到就走它；取不到：默认回退会话模型，strict 才本轮不发；
4. schema / README 文档口径一致（面板说明、默认值、strict 开关）。
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE.parent


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    import logging
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_test")
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
    PROACTIVE_PROVIDER_MAX,
    SocialConfig,
    _provider_id_list,
)


class ParseTest(unittest.TestCase):
    def test_list(self):
        self.assertEqual(_provider_id_list(["a", "b"]), ["a", "b"])

    def test_tuple(self):
        self.assertEqual(_provider_id_list(("a", "b")), ["a", "b"])

    def test_comma_text(self):
        self.assertEqual(_provider_id_list("a, b"), ["a", "b"])

    def test_chinese_comma(self):
        self.assertEqual(_provider_id_list("a，b"), ["a", "b"])

    def test_semicolon_and_newline(self):
        self.assertEqual(_provider_id_list("a;b\nc"), ["a", "b", "c"])

    def test_dedup_keeps_order(self):
        self.assertEqual(_provider_id_list(["b", "a", "b", "a"]), ["b", "a"])

    def test_whitespace_stripped(self):
        self.assertEqual(_provider_id_list(["  a  ", "\tb\n"]), ["a", "b"])

    def test_empty_strings_dropped(self):
        self.assertEqual(_provider_id_list(["a", "", "   ", "b"]), ["a", "b"])

    def test_non_string_elements_skipped(self):
        """None 不能变成 "None"——那会每轮都去解析一个不存在的 provider。"""
        self.assertEqual(_provider_id_list(["a", None, 3, {"x": 1}, "b"]), ["a", "b"])

    def test_none_means_missing_follows_session(self):
        """v2.0.0：None = 键不存在（老配置）→ 空链 = 跟随会话默认。

        v1.28.5 这里兜到作者私有 ID 链，正是 P1-2 的根因。
        """
        self.assertEqual(_provider_id_list(None), [])
        self.assertEqual(_provider_id_list(None), list(DEFAULT_PROACTIVE_PROVIDERS))

    def test_explicit_empty_stays_empty(self):
        """`[""]` / `["  "]` / `""` / `[]` 都是「不指定专供」。"""
        self.assertEqual(_provider_id_list([""]), [])
        self.assertEqual(_provider_id_list(["   "]), [])
        self.assertEqual(_provider_id_list(""), [])
        self.assertEqual(_provider_id_list([]), [])

    def test_wrong_type_degrades_to_empty(self):
        """类型错（数字/字典）是「没配明白」→ 与缺省同口径。"""
        self.assertEqual(_provider_id_list(42), [])
        self.assertEqual(_provider_id_list({"a": 1}), [])

    def test_capped(self):
        got = _provider_id_list([f"p{i}" for i in range(50)])
        self.assertEqual(len(got), PROACTIVE_PROVIDER_MAX)

    def test_returns_list_not_tuple(self):
        """dataclass 默认值是 []，测试逐项比相等——tuple 会挂。"""
        self.assertIsInstance(_provider_id_list(["a"]), list)


class DefaultTest(unittest.TestCase):
    def test_default_chain_is_empty(self):
        """v2.0.0（P1-2）：默认**空** = 跟随当前会话模型。

        旧默认是作者自己的三个 provider ID；标准安装解析不到 → 主动消息全停。
        """
        self.assertEqual(tuple(DEFAULT_PROACTIVE_PROVIDERS), ())
        self.assertEqual(SocialConfig.from_astrbot({}).proactive_provider_ids, [])

    def test_schema_default_matches(self):
        import json
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["proactive_provider_ids"]["default"], [])
        self.assertEqual(schema["proactive_provider_ids"]["type"], "list")

    def test_schema_documents_follow_session_and_strict(self):
        """面板说明必须写清：默认跟随会话；取不到默认回退，strict 才本轮不发。"""
        import json
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        spec = schema["proactive_provider_ids"]
        text = spec["description"] + spec["hint"]
        self.assertIn("跟着当前会话", text)
        self.assertIn("回退跟随会话", text)
        self.assertIn("strict_proactive_provider", text)
        self.assertEqual(schema["strict_proactive_provider"]["type"], "bool")
        self.assertFalse(schema["strict_proactive_provider"]["default"])

    def test_from_astrbot_reads_it(self):
        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": ["a", "b"]})
        self.assertEqual(cfg.proactive_provider_ids, ["a", "b"])

    def test_from_astrbot_comma_text(self):
        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": "a,b"})
        self.assertEqual(cfg.proactive_provider_ids, ["a", "b"])

    def test_from_astrbot_missing_key_follows_session(self):
        """老配置里这一项是空的（装这个版本之前根本没有它）→ 跟随会话默认。"""
        cfg = SocialConfig.from_astrbot({})
        self.assertEqual(cfg.proactive_provider_ids, [])

    def test_from_astrbot_empty_list_follows_session(self):
        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": []})
        self.assertEqual(cfg.proactive_provider_ids, [])

    def test_from_astrbot_bad_value_degrades_to_empty(self):
        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": 42})
        self.assertEqual(cfg.proactive_provider_ids, [])

    def test_strict_default_off_and_reads_string(self):
        self.assertFalse(SocialConfig.from_astrbot({}).strict_proactive_provider)
        self.assertTrue(
            SocialConfig.from_astrbot({"strict_proactive_provider": "true"}).strict_proactive_provider
        )
        self.assertFalse(
            SocialConfig.from_astrbot({"strict_proactive_provider": "false"}).strict_proactive_provider
        )


class _Ctx:
    def __init__(self, ids, session_provider="session:default"):
        self._ids = set(ids)
        self.session_provider = session_provider
        self.session_calls = []

    def get_provider_by_id(self, pid):
        return f"provider:{pid}" if pid in self._ids else None

    async def get_using_provider_async(self, umo=None):
        self.session_calls.append(umo)
        return self.session_provider


class GetProviderTest(unittest.TestCase):
    """真调 `MessageGenerator._get_provider`，不是调私有解析。"""

    def _gen(self, ids, available, strict=None, ctx=None):
        from social.generator import MessageGenerator

        raw = {"proactive_provider_ids": ids}
        if strict is not None:
            raw["strict_proactive_provider"] = strict
        cfg = SocialConfig.from_astrbot(raw)
        context = ctx or _Ctx(available)
        return MessageGenerator(context=context, config=cfg), context

    def _run(self, ids, available, strict=None, ctx=None):
        gen, _ctx = self._gen(ids, available, strict=strict, ctx=ctx)
        return asyncio.run(gen._get_provider("umo:1"))

    def test_uses_first_available(self):
        self.assertEqual(self._run(["a", "b"], {"a"}), "provider:a")

    def test_falls_through_to_second(self):
        self.assertEqual(self._run(["a", "b"], {"b"}), "provider:b")

    def test_none_available_falls_back_to_session_by_default(self):
        """v2.0.0：配了却全取不到 → 回退会话模型（不再是静默全停）。"""
        self.assertEqual(self._run(["a", "b"], set()), "session:default")

    def test_none_available_strict_returns_none(self):
        """strict=True 保留 v1.28.5「本轮不发」。"""
        self.assertIsNone(self._run(["a", "b"], set(), strict=True))

    def test_empty_list_follows_session(self):
        """空链 = 不指定专供：直接跟会话走。"""
        gen, ctx = self._gen([], set())
        self.assertEqual(asyncio.run(gen._get_provider("umo:1")), "session:default")
        self.assertEqual(len(ctx.session_calls), 1)

    def test_partial_availability(self):
        self.assertEqual(self._run(["a", "b", "c"], {"c"}), "provider:c")

    def test_provider_manager_map_fallback(self):
        """没有 get_provider_by_id 时，试 provider_manager.inst_map。"""
        from social.generator import MessageGenerator

        class _Mgr:
            inst_map = {"a": "P:a", "b": "P:b"}

        class _Ctx2:
            provider_manager = _Mgr()

        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": ["b", "a"]})
        gen = MessageGenerator(context=_Ctx2(), config=cfg)
        self.assertEqual(asyncio.run(gen._get_provider("umo:1")), "P:b")


class SchemaAndReadmeTest(unittest.TestCase):
    def test_readme_documents_it(self):
        text = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("proactive_provider_ids", text)
        self.assertIn("主动消息专用模型", text)

    def test_readme_documents_follow_session_and_strict(self):
        """README 必须写清 v2.0.0 的新默认与新开关，不然用户以为还是旧行为。"""
        text = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("strict_proactive_provider", text)
        self.assertIn("跟随当前会话", text)


if __name__ == "__main__":
    unittest.main()
