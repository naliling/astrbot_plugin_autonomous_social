"""v2.2.33：`LLM 不可用` 要说清是「没配」还是「调不通」。

## 为什么要改

线上 2026-11-11 13:00 那一轮，`engine.py` 打的是：

    [social.engine:8569] 本轮候选 81 个，尝试 3 次，发出 0 条……
    LLM 不可用（provider 取不到），本轮未发送（3）

我照着这行先去查了三遍 `proactive_provider_ids`——三个专供 provider 在
`cmd_config.json` 里都存在、都是 `enable=True`。真实原因是三条都在 90 秒超时
之后进了退避。同一句文案覆盖了三种完全不同的情况：

- 真的没配 provider（要改配置）
- 正在退避（等着就好，什么都不用做）
- 调用失败（超时/连接/5xx，同上，但要知道是它）

`_is_infra_note()` 靠前缀 `LLM 不可用` 分类（一轮里剩下的候选不必再试），所以
新文案必须保留这个前缀。本文件钉住两件事：三种原因写得出来、前缀没被改掉。
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    import logging

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("aso_v2233")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from social import engine as engine_mod  # noqa: E402

BID = "bot1"
PREFIX = "LLM 不可用"


class _FakeGen:
    def __init__(self, left: float = 0.0) -> None:
        self._left = left

    def llm_backoff_remaining(self, scope: str = "") -> float:
        return self._left


class _FakeGenBroken:
    def llm_backoff_remaining(self, scope: str = "") -> float:
        raise RuntimeError("boom")


def _engine(ids, left=0.0, gen=None):
    eng = engine_mod.SocialEngine.__new__(engine_mod.SocialEngine)
    eng.cfg = types.SimpleNamespace(proactive_provider_ids=list(ids))
    eng.generator = _FakeGen(left) if gen is None else gen
    return eng


class _NameNote:
    def note(self, ids, left=0.0, gen=None) -> str:
        return engine_mod.SocialEngine._llm_unavailable_note(_engine(ids, left, gen), BID)


class NoProviderTest(unittest.TestCase):
    """真的没配 provider，要能一眼看出来。"""

    def test_empty_list_says_not_configured(self):
        text = _NameNote().note([])
        self.assertIn("没配 provider", text)

    def test_missing_field_is_treated_as_not_configured(self):
        eng = _engine([])
        eng.cfg = types.SimpleNamespace()
        text = engine_mod.SocialEngine._llm_unavailable_note(eng, BID)
        self.assertIn("没配 provider", text)


class BackoffTest(unittest.TestCase):
    """正在退避，要能看出「等就行」。"""

    def test_backoff_reports_remaining_seconds(self):
        text = _NameNote().note(["p1", "p2", "p3"], left=42.2)
        self.assertIn("退避中", text)
        self.assertIn("43 秒", text, "向上取整，别写「还剩 42 秒」然后提前 1 秒恢复")
        self.assertIn("3 条 provider", text)

    def test_tiny_backoff_is_not_treated_as_backoff(self):
        text = _NameNote().note(["p1"], left=0.2)
        self.assertNotIn("退避中", text)
        self.assertIn("调用失败", text)

    def test_broken_generator_does_not_raise(self):
        text = _NameNote().note(["p1"], gen=_FakeGenBroken())
        self.assertIn("调用失败", text, "取不到退避时长也不许抛，按普通失败写")


class OrdinaryFailureTest(unittest.TestCase):
    def test_failure_names_the_common_causes(self):
        text = _NameNote().note(["p1"], left=0.0)
        self.assertIn("调用失败", text)
        self.assertIn("超时", text)


class PrefixContractTest(unittest.TestCase):
    """`_INFRA_NOTES` 靠前缀分类，三种文案都不能丢前缀。"""

    def test_all_three_keep_the_prefix_and_stay_infra(self):
        eng = _engine(["p1"], left=0.0)
        notes = [
            engine_mod.SocialEngine._llm_unavailable_note(eng, BID),
            engine_mod.SocialEngine._llm_unavailable_note(_engine(["p1"], 30.0), BID),
            engine_mod.SocialEngine._llm_unavailable_note(_engine([]), BID),
        ]
        for text in notes:
            self.assertTrue(text.startswith(PREFIX), text)
            self.assertTrue(
                engine_mod.SocialEngine._is_infra_note(text),
                f"「{text}」不再被当成基础设施故障，一轮短路会失效",
            )

    def test_model_refusal_is_still_not_infra(self):
        self.assertFalse(engine_mod.SocialEngine._is_infra_note("模型这次说不想说"))


class SourceWiringTest(unittest.TestCase):
    def test_engine_uses_the_helper_and_drops_the_old_wording(self):
        src = (PLUGIN_ROOT / "social" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("note = self._llm_unavailable_note(bid)", src)
        self.assertNotIn("provider 取不到），本轮未发送", src, "旧文案不该再出现在发送路径上")
        self.assertIn('_INFRA_NOTES = ("LLM 不可用"', src)


if __name__ == "__main__":
    unittest.main()
