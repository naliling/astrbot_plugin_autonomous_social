"""v1.28.5：主动消息可以指定专用模型。

## 为什么

以前这个插件每一次模型调用都跟着**当前会话的** provider 走
（`get_using_provider(umo)`）——群里换个模型，她主动找你说话的质量也跟着变。
而主动消息恰恰是最不该省的那一次：那是她**自己想起来**要说什么。

## 规矩（用户原话）

- 「只要是这个插件就用专模型」——1:1 主动、群聊心流、破冰、起话头全走它；
- 「指定的就要管它」——配了却拿不到就**本轮不发**，不退默认、不重试；
- provider ID 列表，按顺序试到第一个能用的即止。

## 本文件盯的

1. 列表解析：list / 逗号文本 / 分号 / 换行都认，去重保序，非字符串跳过；
2. 取得到就走它，取不到就本轮不发（不偷用会话默认）；
3. **默认就带值**（「只要是这个插件就用专模型」），老配置读进来也兜到默认链；
4. schema / dataclass / README 三处默认值一致，且面板描述写着「脑子/思考好的」。
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

    def test_none_means_missing_falls_back_to_default(self):
        """None = 键不存在（老配置）→ 兜到默认链，不是空。"""
        self.assertEqual(_provider_id_list(None), list(DEFAULT_PROACTIVE_PROVIDERS))

    def test_explicit_empty_stays_empty(self):
        """`[""]` / `["  "]` / `""` 是用户真的清空了——按空处理，调用方据此走旧行为。"""
        self.assertEqual(_provider_id_list([""]), [])
        self.assertEqual(_provider_id_list(["   "]), [])
        self.assertEqual(_provider_id_list(""), [])
        self.assertEqual(_provider_id_list([]), [])

    def test_wrong_type_falls_back_to_default(self):
        """类型错（数字/字典）是意外，不是「用户清空了」——兜回默认链。"""
        self.assertEqual(_provider_id_list(42), list(DEFAULT_PROACTIVE_PROVIDERS))
        self.assertEqual(_provider_id_list({"a": 1}), list(DEFAULT_PROACTIVE_PROVIDERS))

    def test_explicit_empty_stays_empty(self):
        """`[""]` / `["  "]` 是用户真的清空了——按空处理，调用方据此走旧行为。"""
        self.assertEqual(_provider_id_list([""]), [])
        self.assertEqual(_provider_id_list(["   "]), [])
        self.assertEqual(_provider_id_list(""), [])

    def test_capped(self):
        got = _provider_id_list([f"p{i}" for i in range(50)])
        self.assertEqual(len(got), PROACTIVE_PROVIDER_MAX)

    def test_returns_list_not_tuple(self):
        """dataclass 默认值是 []，测试逐项比相等——tuple 会挂。"""
        self.assertIsInstance(_provider_id_list(["a"]), list)


class DefaultTest(unittest.TestCase):
    def test_default_is_the_thinking_chain(self):
        """默认值在**实例**上：`field(default_factory=...)` 是类级 sentinel，
        dataclass 装饰后不会在类上留同名属性——断言类属性会假失败。

        默认**不是空**：「只要是这个插件就用专模型」。写成空等于这个功能
        装上是关的，那句话白说。
        """
        self.assertEqual(
            SocialConfig.from_astrbot({}).proactive_provider_ids,
            list(DEFAULT_PROACTIVE_PROVIDERS),
        )

    def test_schema_default_matches(self):
        import json
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["proactive_provider_ids"]["default"],
                         list(DEFAULT_PROACTIVE_PROVIDERS))
        self.assertEqual(schema["proactive_provider_ids"]["type"], "list")

    def test_schema_says_thinking_model(self):
        """面板那一栏要写着「脑子/思考好的」——那是用户的原话。"""
        import json
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("脑子/思考好的", schema["proactive_provider_ids"]["description"])
        self.assertIn("脑子/思考好的", schema["proactive_provider_ids"]["hint"])

    def test_from_astrbot_reads_it(self):
        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": ["a", "b"]})
        self.assertEqual(cfg.proactive_provider_ids, ["a", "b"])

    def test_from_astrbot_comma_text(self):
        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": "a,b"})
        self.assertEqual(cfg.proactive_provider_ids, ["a", "b"])

    def test_from_astrbot_missing_key_falls_back_to_default(self):
        """老配置里这一项是空的（装这个版本之前根本没有它）。

        AstrBot 只补缺、不覆盖已存在的键，所以不在这里兜一手，
        老用户升上来还是空的——那句话又白说。
        """
        cfg = SocialConfig.from_astrbot({})
        self.assertEqual(cfg.proactive_provider_ids, list(DEFAULT_PROACTIVE_PROVIDERS))

    def test_from_astrbot_empty_list_disables_the_feature(self):
        """`[]` 是用户在面板上清空了多选下拉 → 关掉功能，回旧行为。

        与「键不存在」区分：那个才是缺省，兜到默认链。空列表是明确的选择。
        """
        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": []})
        self.assertEqual(cfg.proactive_provider_ids, [])

    def test_from_astrbot_bad_value_falls_back_to_default(self):
        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": 42})
        self.assertEqual(cfg.proactive_provider_ids, list(DEFAULT_PROACTIVE_PROVIDERS))

    def test_blank_strings_disable_the_feature(self):
        """`[""]` / `["  "]` 是「真的清空了」→ 空，调用方据此走旧行为。"""
        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": [""]})
        self.assertEqual(cfg.proactive_provider_ids, [])


class _Ctx:
    def __init__(self, ids):
        self._ids = ids

    def get_provider_by_id(self, pid):
        return f"provider:{pid}" if pid in self._ids else None


class GetProviderTest(unittest.TestCase):
    """真调 `MessageGenerator._get_provider`，不是调私有解析。"""

    def _run(self, ids, available):
        import asyncio

        from social.generator import MessageGenerator

        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": ids})
        gen = MessageGenerator(context=_Ctx(available), config=cfg)
        return asyncio.run(gen._get_provider("umo:1"))

    def test_uses_first_available(self):
        self.assertEqual(self._run(["a", "b"], {"a": "P:a", "b": "P:b"}), "provider:a")

    def test_falls_through_to_second(self):
        self.assertEqual(self._run(["a", "b"], {"b": "P:b"}), "provider:b")

    def test_none_available_returns_none(self):
        """配了却拿不到 → None（调用方本轮不发）。绝不能用会话默认。"""
        self.assertIsNone(self._run(["a", "b"], set()))

    def test_empty_list_falls_back_to_context(self):
        """显式清空 = 关掉这个功能，回旧行为跟着会话走。

        注意：`from_astrbot({})` **不会**清空（它会兜到默认链）；
        这里传 `[""]` 才是「用户真的清空了」。
        """
        import asyncio

        from social.generator import MessageGenerator

        cfg = SocialConfig.from_astrbot({"proactive_provider_ids": [""]})
        gen = MessageGenerator(context=_Ctx(set()), config=cfg)
        # context 什么都不给 → None（旧行为：取不到就不发）
        self.assertIsNone(asyncio.run(gen._get_provider("umo:1")))

    def test_partial_availability(self):
        self.assertEqual(self._run(["a", "b", "c"], {"c": "P:c"}), "provider:c")

    def test_provider_manager_map_fallback(self):
        """没有 get_provider_by_id 时，试 provider_manager.inst_map。"""
        import asyncio

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

    def test_readme_states_no_fallback(self):
        """README 必须写清「不退默认、不重试」——那是这个功能的核心取舍。"""
        text = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("本轮不主动开口", text)


if __name__ == "__main__":
    unittest.main()
