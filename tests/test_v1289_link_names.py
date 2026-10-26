"""v1.28.9：主动消息接上门面 `brief()` + 名字回退链（让主动消息真的会叫人）。

用户原话：

* 「两个插件联动主动消息从 core 拿到的消息不全，而且 core 可以给它的信息却很少，
  这是事实」
* 「而且主动消息从 core 那拿不到名称，以至于根本不会叫人」

根因两条：

1. 门面 `core_facade.brief()` 这个封装早就存在，却**只有 clock/status 在用**——
   主动消息仍然只读 Core 落盘的 state.json，「门面直给的结构化情境」这条路是死的。
2. `core_bridge` 只读 `nickname`（Core 的 `auto_nickname` 校验很严，含数字/emoji/
   装饰的名片一律不认），大多数用户 `nickname` 为空 → `generator._bond_line()` 整句
   不出 → 主动消息从来不叫名字。

本文件盯四件事（全部用自造的 dict fixture，不依赖 Humanoid Core 仓库）：

1. `core_bridge`：读得到 `display_name`、派生 `call_name` / `name_source`
   （nickname > display_name > local > ""），老数据不编名字；契约 `identity` /
   `outfit` 缺字段/脏数据不炸，且 `compact()` 输出不多一个字（名字是称呼不是状态）。
2. 引擎 `_core_brief()`：有门面才问、`task="proactive"` 透传；没门面 / 抛异常 /
   非 dict 一律 None——这是「可选上下文」，坏掉必须退回 v1.28.8 的旧行为。
3. `_apply_core_brief()` 的合并优先级：**门面 > state.json > 本地账本**；
   `_said` 要从门面的 `{"text"}` 形状转回生成侧认的 `{"said"}`（不转等于没接）。
4. `generator._bond_line` / 主动消息 prompt：三种名字来源各一种措辞、带「偶尔叫」
   的使用纪律、没名字时明说别编；prompt 里名字只出现**一处**（不再新增复读点）。
"""
from __future__ import annotations

import asyncio
import inspect
import json
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


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v1289_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

from social import core_facade  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.core_bridge import CoreBridge  # noqa: E402
from social.engine import SocialEngine  # noqa: E402
from social.generator import MessageGenerator, _bond_line  # noqa: E402

NOW = 1_700_000_000.0


# ─── 公共 fixture：自造的 Core state.json 根（不碰 HC 仓库）──────────────


def _core_root(user=None, contract=None, *, bot_id="b1", uid="u1"):
    selfs = {"energy": 80.0}
    if contract is not None:
        selfs["contract"] = contract
    return {"roles": {bot_id: {"self": selfs, "users": {uid: user or {}}}}}


def _write_core(tmp: str, root) -> str:
    path = os.path.join(tmp, "plugin_data", "humanoid_core", "state.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(root, f, ensure_ascii=False)
    return path


def _snapshot(user=None, contract=None):
    tmp = tempfile.mkdtemp()
    _write_core(tmp, _core_root(user, contract))
    bridge = CoreBridge("auto", data_dir=tmp)
    snap = bridge.load_snapshot("b1", "u1")
    assert snap is not None
    return bridge, snap


# ─── 1. core_bridge：名字回退链 ─────────────────────────────────────


class CoreBridgeNameFallbackTest(unittest.TestCase):
    """`call_name` 三级回退，且老数据一个名字都不编。"""

    def test_core_recognized_nickname_wins(self):
        _bridge, snap = _snapshot({
            "nickname": " 小明 ",
            "display_name": "爱筝挽起·冲击波冠军",
            "name": "本地名片",
        })
        self.assertEqual(snap["nickname"], "小明")
        self.assertEqual(snap["display_name"], "爱筝挽起·冲击波冠军")
        self.assertEqual(snap["call_name"], "小明")
        self.assertEqual(snap["name_source"], "nickname")

    def test_display_name_is_the_second_choice(self):
        _bridge, snap = _snapshot({"nickname": "", "display_name": " 阿澈 "})
        self.assertEqual(snap["display_name"], "阿澈")
        self.assertEqual(snap["call_name"], "阿澈")
        self.assertEqual(snap["name_source"], "display_name")

    def test_local_name_is_the_last_choice(self):
        _bridge, snap = _snapshot({"name": "游鱼"})
        self.assertEqual(snap["display_name"], "")
        self.assertEqual(snap["call_name"], "游鱼")
        self.assertEqual(snap["name_source"], "local")

    def test_old_core_invents_no_name(self):
        """老 Core 没有 display_name / name：明确空串，不猜、不编。"""
        _bridge, snap = _snapshot({"mood": {"affection": 50.0}})
        self.assertEqual(snap["nickname"], "")
        self.assertEqual(snap["display_name"], "")
        self.assertEqual(snap["call_name"], "")
        self.assertEqual(snap["name_source"], "")

    def test_dirty_name_fields_do_not_leak_containers_as_names(self):
        """手改坏/跨版本脏数据：容器不能变成「名字」被念出去。"""
        _bridge, snap = _snapshot({
            "nickname": {"坏": 1},
            "display_name": ["坏"],
            "name": 123,
        })
        self.assertEqual(snap["nickname"], "")
        self.assertEqual(snap["display_name"], "")
        # 数字名（「123」这种）是真实存在的，保留；容器一律当没有。
        self.assertEqual(snap["call_name"], "123")
        self.assertEqual(snap["name_source"], "local")

    def test_display_name_is_not_written_into_the_state_summary(self):
        """名字是**称呼**，不是状态：compact() 里不能出现它。"""
        bridge, snap = _snapshot({
            "nickname": "",
            "display_name": "爱筝挽起·冲击波冠军",
        })
        text = bridge.compact(snap, include_weather=False)
        self.assertNotIn("爱筝挽起", text)
        self.assertNotIn("昵称", text)


# ─── 2. core_bridge：identity / outfit 契约块 ───────────────────────


class CoreBridgeIdentityOutfitTest(unittest.TestCase):
    def _view(self, contract):
        return CoreBridge("bridge", None)._build_self_view({"contract": contract}, "b1")

    def test_identity_and_outfit_are_read_from_the_contract(self):
        view = self._view({
            "v": 1,
            "identity": {"char_name": "小娜", "persona_label": "温柔"},
            "outfit": {"enabled": True, "text": "白色毛衣", "source": "manual"},
        })
        self.assertEqual(
            view["identity"], {"char_name": "小娜", "persona_label": "温柔"}
        )
        self.assertEqual(
            view["outfit"],
            {"enabled": True, "text": "白色毛衣", "source": "manual"},
        )

    def test_old_core_without_the_blocks_is_empty_and_unchanged(self):
        """旧版 Core 没有这两个键：空 dict，且原有字段一个不少。"""
        view = self._view({"v": 1, "body": {"energy": 61.0}})
        self.assertEqual(view["identity"], {})
        self.assertEqual(view["outfit"], {})
        self.assertEqual(view["energy"], 61.0)
        self.assertEqual(view["contract_v"], 1)

    def test_dirty_blocks_do_not_crash(self):
        for contract in (
            {"v": 1, "identity": "坏", "outfit": ["坏"]},
            {"v": 1, "identity": {"char_name": {"x": 1}, "persona_label": None}},
            {"v": 1, "outfit": {"enabled": "yes", "text": ["坏"]}},
        ):
            with self.subTest(contract=contract):
                view = self._view(contract)
                self.assertIsInstance(view["identity"], dict)
                self.assertIsInstance(view["outfit"], dict)

    def test_compact_output_is_identical_with_or_without_the_new_blocks(self):
        """`compact()` 不消费 identity/outfit：两边的状态摘要素面必须完全一致。"""
        bridge = CoreBridge("bridge", None)
        old = bridge._build_self_view(
            {"contract": {"v": 1, "persona": "小娜", "holiday": "中秋"}}, "b1"
        )
        new = bridge._build_self_view(
            {
                "contract": {
                    "v": 1,
                    "persona": "小娜",
                    "holiday": "中秋",
                    "identity": {"char_name": "小娜", "persona_label": "温柔"},
                    "outfit": {"enabled": True, "text": "白色毛衣", "source": "manual"},
                }
            },
            "b1",
        )
        self.assertEqual(
            bridge.compact(old, include_weather=False),
            bridge.compact(new, include_weather=False),
        )

    def test_identity_reaches_the_user_snapshot(self):
        _bridge, snap = _snapshot(
            {"mood": {"affection": 40.0}},
            {"v": 1, "identity": {"char_name": "小娜", "persona_label": "温柔"}},
        )
        self.assertEqual(snap["identity"]["char_name"], "小娜")


# ─── 3. 引擎：门面 brief 取数与合并 ─────────────────────────────────


class _FakeFacade:
    """最小门面替身：只实现 `_core_brief` 走到的那些方法。"""

    def __init__(self, out=None, *, raise_exc=None, caps=None):
        self.out = out
        self.raise_exc = raise_exc
        self.caps = list(caps if caps is not None else ["brief", "clock", "fingerprint"])
        self.calls = []

    def api_version(self):
        return 2

    def capabilities(self):
        return list(self.caps)

    def clock(self, role_id):
        return {"utc_offset_minutes": 300}

    def state_fingerprint(self, role_id):
        return "fp"

    def brief(self, role_id, user_id="", **kwargs):
        self.calls.append((role_id, user_id, kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.out


def _engine(facade=None, *, cfg=None) -> SocialEngine:
    """绕开完整构造：`_core_brief` / `_apply_core_brief` 只用到这几个属性。"""
    eng = object.__new__(SocialEngine)
    eng.cfg = cfg or SocialConfig(use_core_facade="auto")
    eng.context = None
    eng._facade = facade
    eng._facade_tried = True
    eng._city_offset = {}
    eng.log = lambda *a, **k: None
    return eng


#: 一份「HC 2.29.0 门面 brief」的自造 fixture（字段清单照任务书抄，不 import HC）。
BRIEF = {
    "v": 2,
    "task": "proactive",
    "her": {"char_name": "小娜", "persona_label": "温柔"},
    "outfit": {"enabled": True, "text": "白色毛衣", "source": "manual"},
    "weather": {"env": "外面在下小雨"},
    "holiday": "无",
    "routine": {"night_start_hour": 2.0, "night_end_hour": 10.0},
    "user": {
        "user_id": "u1",
        "nickname": "认定昵称",
        "nickname_source": "auto",
        "display_name": "名片名·装饰",
        "call_name": "名片名",
        "name_source": "display_name",
        "affection": 88.0,
        "base_affection": 40.0,
        "affection_delta": 48.0,
        "label": "有点黏人",
        "mood_tag": "心情很好",
        "aggression": 2.0,
        "libido": 31.0,
        "attention": {"care": 72.0, "focus": 0.4, "spare": 0.1},
        "first_met": NOW - 86400 * 300,
        "known_days": 300.0,
        "last_interaction": NOW - 600.0,
        "said": [{"text": "明天要面试", "at": NOW - 3600.0}],
        "intimacy": {
            "in_scene": False,
            "stage_name": "",
            "turns_today": 0,
            "can_enter": True,
            "want_line": "",
        },
    },
}


class CoreBriefFetchTest(unittest.TestCase):
    """`_core_brief` 只是取数：有门面才问，任何异常都返回 None。"""

    def test_facade_brief_is_called_with_the_proactive_task(self):
        facade = _FakeFacade(out=BRIEF)
        eng = _engine(facade)
        out = eng._core_brief("b1", "u1", char_name="小娜", user_name="小明")
        self.assertIs(out, BRIEF)
        self.assertEqual(len(facade.calls), 1)
        role_id, user_id, kwargs = facade.calls[0]
        self.assertEqual(role_id, "b1")
        self.assertEqual(user_id, "u1")
        self.assertEqual(kwargs.get("task"), "proactive")
        self.assertEqual(kwargs.get("char_name"), "小娜")
        self.assertEqual(kwargs.get("user_name"), "小明")

    def test_no_facade_returns_none(self):
        self.assertIsNone(_engine(None)._core_brief("b1", "u1"))

    def test_facade_off_returns_none(self):
        eng = _engine(_FakeFacade(out=BRIEF), cfg=SocialConfig(use_core_facade="off"))
        self.assertIsNone(eng._core_brief("b1", "u1"))

    def test_facade_raising_returns_none(self):
        eng = _engine(_FakeFacade(raise_exc=RuntimeError("boom")))
        self.assertIsNone(eng._core_brief("b1", "u1"))

    def test_non_dict_result_returns_none(self):
        eng = _engine(_FakeFacade(out="不是 dict"))
        self.assertIsNone(eng._core_brief("b1", "u1"))

    def test_lightweight_engine_stub_without_the_facade_does_not_crash(self):
        """`_speak_inner` / `_preview_one` 会被只挂了少数依赖的引擎桩调用（仓库里就是这么
        测的）；桩上没有 `_core_facade`，类调用必须自己吞掉并返回 None，不能抛。"""
        stub = types.SimpleNamespace()
        self.assertIsNone(SocialEngine._core_brief(stub, "b1", "u1"))


#: 状态文件（旧）那一份快照：故意每条都跟 BRIEF 不一样，用来验证「门面优先」。
SNAP = {
    "nickname": "旧认定昵称",
    "nickname_src": "auto",
    "display_name": "旧名片",
    "call_name": "旧名片",
    "name_source": "display_name",
    "said": [{"said": "旧话说", "at": NOW - 7200.0}],
    "mood_tag": "旧状态",
    "attention": 10.0,
    "affection": 12.0,
    "base_affection": 5.0,
    "libido": 9.0,
    "last_interaction": NOW - 9999.0,
    "identity": {"char_name": "旧名字", "persona_label": ""},
}

LOCAL_USER = {"name": "本地名片", "message_count": 5}


class ApplyCoreBriefTest(unittest.TestCase):
    """合并优先级：门面 > state.json > 本地账本；坏字段只跳过那一个。"""

    def test_facade_values_override_the_state_file(self):
        u_copy: dict = {}
        SocialEngine._apply_core_brief(u_copy, BRIEF, SNAP, LOCAL_USER)
        self.assertEqual(u_copy["_nickname"], "认定昵称")
        self.assertEqual(u_copy["_call_name"], "名片名")
        self.assertEqual(u_copy["_name_source"], "display_name")
        self.assertEqual(u_copy["_display_name"], "名片名·装饰")
        self.assertEqual(u_copy["_affection"], 88.0)
        self.assertEqual(u_copy["_affection_base"], 40.0)
        self.assertEqual(u_copy["_label"], "有点黏人")
        self.assertEqual(u_copy["_mood_tag"], "心情很好")
        self.assertEqual(u_copy["_attention"], 72.0)
        self.assertEqual(u_copy["_attention_block"]["focus"], 0.4)
        self.assertEqual(u_copy["_her_name"], "小娜")
        self.assertEqual(u_copy["_known_days"], 300.0)
        self.assertEqual(u_copy["_last_interaction"], NOW - 600.0)
        self.assertEqual(u_copy["_libido"], 31.0)
        self.assertIs(u_copy["_brief"], BRIEF)

    def test_said_is_converted_to_the_generator_shape(self):
        """门面给 `{"text"}`，生成侧读 `{"said"}`——不转等于没接。"""
        u_copy: dict = {}
        SocialEngine._apply_core_brief(u_copy, BRIEF, None, {})
        self.assertEqual(
            u_copy["_said"], [{"said": "明天要面试", "at": NOW - 3600.0}]
        )

    def test_without_facade_it_falls_back_to_the_state_file(self):
        """没门面（或老 Core）：退回 v1.28.8 的取数，不炸、不丢旧逻辑。"""
        u_copy: dict = {"_affection": 12.0}
        SocialEngine._apply_core_brief(u_copy, None, SNAP, LOCAL_USER)
        self.assertEqual(u_copy["_nickname"], "旧认定昵称")
        self.assertEqual(u_copy["_call_name"], "旧名片")
        self.assertEqual(u_copy["_name_source"], "display_name")
        self.assertEqual(u_copy["_display_name"], "旧名片")
        self.assertEqual(u_copy["_said"], [{"said": "旧话说", "at": NOW - 7200.0}])
        self.assertEqual(u_copy["_mood_tag"], "旧状态")
        self.assertEqual(u_copy["_affection"], 12.0, "没门面时不该改写旧好感")
        self.assertNotIn("_affection_base", u_copy)
        self.assertEqual(u_copy["_her_name"], "旧名字")  # 旧 Core 走契约 identity 兜底
        self.assertNotIn("_brief", u_copy)

    def test_local_name_is_the_last_resort(self):
        """没 Core / 老 Core 时，本地账本记的名片名也能让她叫得出口。"""
        u_copy: dict = {}
        SocialEngine._apply_core_brief(u_copy, None, None, LOCAL_USER)
        self.assertEqual(u_copy["_call_name"], "本地名片")
        self.assertEqual(u_copy["_name_source"], "local")

    def test_nothing_anywhere_sets_no_name_and_does_not_crash(self):
        u_copy: dict = {}
        SocialEngine._apply_core_brief(u_copy, None, None, {})
        self.assertNotIn("_call_name", u_copy)
        self.assertNotIn("_name_source", u_copy)
        self.assertNotIn("_nickname", u_copy)

    def test_dirty_facade_values_are_skipped_one_by_one(self):
        brief = {
            "v": 2,
            "user": {
                "call_name": "小明",
                "name_source": "nickname",
                "affection": "坏",
                "said": "坏",
                "attention": {"care": "坏"},
            },
        }
        u_copy: dict = {"_affection": 55.0}
        SocialEngine._apply_core_brief(u_copy, brief, None, {})
        self.assertEqual(u_copy["_call_name"], "小明")
        self.assertEqual(u_copy["_name_source"], "nickname")
        self.assertEqual(u_copy["_affection"], 55.0, "读不出的好感不该被清成 0")
        self.assertEqual(u_copy["_said"], [])
        self.assertNotIn("_attention", u_copy)

    def test_facade_without_user_block_still_gives_her_name(self):
        u_copy: dict = {}
        SocialEngine._apply_core_brief(
            u_copy, {"v": 2, "her": {"char_name": "小娜"}}, None, {}
        )
        self.assertEqual(u_copy["_her_name"], "小娜")
        self.assertNotIn("_call_name", u_copy)


class ProactivePathsUseBriefTest(unittest.TestCase):
    """两条主动消息路径（真实发送 / 预览）都必须走同一条门面 brief 合并。"""

    def test_speak_inner_calls_it(self):
        src = inspect.getsource(SocialEngine._speak_inner)
        self.assertIn("_core_brief(", src)
        self.assertIn("_apply_core_brief(", src)

    def test_preview_one_calls_it(self):
        src = inspect.getsource(SocialEngine._preview_one)
        self.assertIn("_core_brief(", src)
        self.assertIn("_apply_core_brief(", src)


class FacadeStatusUsesCapabilitiesTest(unittest.TestCase):
    def setUp(self) -> None:
        core_facade.reset()

    def tearDown(self) -> None:
        core_facade.reset()

    def test_user_capability_is_reported(self):
        core_facade._CACHE["facade"] = _FakeFacade(
            caps=["brief", "clock", "fingerprint", "user"]
        )
        core_facade._CACHE["resolved"] = True
        text = _engine()._facade_status_text()
        self.assertIn("已连接", text)
        self.assertIn("称呼", text)

    def test_old_facade_without_user_capability_is_plain(self):
        core_facade._CACHE["facade"] = _FakeFacade(
            caps=["brief", "clock", "fingerprint"]
        )
        core_facade._CACHE["resolved"] = True
        text = _engine()._facade_status_text()
        self.assertIn("已连接", text)
        self.assertNotIn("称呼/好感", text)


# ─── 4. `_bond_line`：名字真的用起来了 ─────────────────────────────


class BondLineNameTest(unittest.TestCase):
    def test_core_nickname_wording(self):
        text = _bond_line({
            "_nickname": "小明",
            "_call_name": "小明",
            "_name_source": "nickname",
        })
        self.assertIn("你一直叫TA「小明」", text)
        self.assertNotIn("名片名", text)

    def test_display_name_wording_uses_the_short_call_name(self):
        text = _bond_line({
            "_display_name": "爱筝挽起·冲击波冠军",
            "_call_name": "爱筝挽起·冲击波冠军",
            "_name_source": "display_name",
        })
        self.assertIn("TA 的名片名是「爱筝挽起」", text)
        self.assertNotIn("冲击波冠军", text)
        self.assertIn("你可以偶尔这么叫", text)

    def test_local_name_uses_the_same_card_name_wording(self):
        text = _bond_line({"_call_name": "游鱼", "_name_source": "local"})
        self.assertIn("TA 的名片名是「游鱼」", text)
        self.assertIn("偶尔", text)

    def test_every_name_source_carries_the_usage_discipline(self):
        for target in (
            {"_nickname": "小明", "_name_source": "nickname"},
            {"_call_name": "小明", "_name_source": "display_name"},
            {"_call_name": "小明", "_name_source": "local"},
        ):
            with self.subTest(target=target):
                text = _bond_line(target)
                # v1.28.13：主动消息要开口叫一声（否则一条条发过去都不认人），
                # 但位置要自然，不许孤零零挂在句尾。
                self.assertIn("开口要叫一声TA的称呼", text)
                self.assertIn("别单独挂在句尾", text)
                self.assertIn("也别每句话都叫", text)

    def test_no_name_says_do_not_invent_one(self):
        text = _bond_line({})
        self.assertIn("你还不知道TA叫什么", text)
        self.assertIn("别自己给TA编名字", text)

    def test_mood_attention_and_label_are_kept(self):
        text = _bond_line({
            "_nickname": "小明",
            "_name_source": "nickname",
            "_mood_tag": "心情很好",
            "_attention": 80,
            "_label": "有点黏人",
        })
        self.assertIn("TA这会儿的状态是心情很好", text)
        self.assertIn("你挺在意这个人", text)
        self.assertIn("有点黏人", text)

    def test_label_alone_does_not_crash_and_no_name_is_still_said(self):
        text = _bond_line({"_label": "有点黏人"})
        self.assertIn("有点黏人", text)
        self.assertIn("你还不知道TA叫什么", text)

    def test_the_name_appears_only_once_in_the_bond_line(self):
        text = _bond_line({
            "_nickname": "小明",
            "_call_name": "小明",
            "_name_source": "nickname",
            "_mood_tag": "心情很好",
        })
        self.assertEqual(text.count("小明"), 1)


# ─── 5. 主动消息 prompt：称呼句出现且不重复 ─────────────────────────


class _FakeProvider:
    provider_config = {"id": "fake"}

    def get_model(self):
        return "fake-model"

    async def text_chat(self, **kw):
        return None


class _FakeCtx:
    async def get_using_provider_async(self, umo=None):
        return _FakeProvider()


class ProactivePromptNameTest(unittest.TestCase):
    def _prompt(self, target) -> str:
        # 显式清空专用模型链：本用例只测 prompt 拼装，不想让它去解析不存在的 provider。
        gen = MessageGenerator(
            _FakeCtx(), SocialConfig(proactive_provider_ids=[])
        )
        prepared = asyncio.run(gen._compose(
            "aiocqhttp:FriendMessage:u1",
            target,
            "能量: 62 社交能量: 71",
            "想到 TA 了",
            {"msg_type_desc": "随便说点什么"},
            "你是小娜",
            None,
            decide=True,
            at=NOW,
        ))
        self.assertIsNotNone(prepared, "_compose 返回 None（provider 桩没接上？）")
        return prepared[1]

    def _target(self, **over) -> dict:
        base = {
            "umo": "aiocqhttp:FriendMessage:u1",
            "conversation": [],
            "last_message": "",
            "last_spoken_text": "",
            "last_seen": NOW - 3600.0,
            "topics": [],
            "_affection": 60.0,
        }
        base.update(over)
        return base

    def test_prompt_carries_the_call_name_exactly_once(self):
        text = self._prompt(self._target(_call_name="小明", _name_source="nickname"))
        self.assertIn("你一直叫TA「小明」", text)
        self.assertIn("开口要叫一声TA的称呼", text)
        # 名字只能出现在 bond_line 这一处：core_context 的 compact() 与
        # 关系/素材块都不该再提一次（否则模型会把它当复读模板）。
        self.assertEqual(text.count("小明"), 1, "名字在 prompt 里出现了不止一处")

    def test_prompt_carries_the_display_name_wording(self):
        text = self._prompt(self._target(
            _call_name="爱筝挽起·冲击波冠军",
            _name_source="display_name",
        ))
        self.assertIn("TA 的名片名是「爱筝挽起」", text)
        self.assertNotIn("冲击波冠军", text)
        self.assertEqual(text.count("爱筝挽起"), 1)

    def test_prompt_without_a_name_tells_her_not_to_invent(self):
        text = self._prompt(self._target())
        self.assertIn("你还不知道TA叫什么", text)
        self.assertIn("别自己给TA编名字", text)

    def test_prompt_has_no_name_when_only_unusable_dirty_data_exists(self):
        text = self._prompt(self._target(_call_name={"坏": 1}, _name_source="nickname"))
        self.assertIn("你还不知道TA叫什么", text)


# ─── 6. core_facade 封装：task 透传 + capabilities ─────────────────


class FacadeBriefWrapperTest(unittest.TestCase):
    def test_task_and_names_are_forwarded(self):
        class F:
            def __init__(self):
                self.kw = None

            def brief(self, role_id, user_id="", **kwargs):
                self.kw = kwargs
                return {"v": 2, "task": kwargs.get("task")}

        facade = F()
        out = core_facade.brief(
            facade, "b1", "u1", task="proactive",
            char_name="小娜", user_name="小明",
        )
        self.assertEqual(out["task"], "proactive")
        self.assertEqual(facade.kw.get("char_name"), "小娜")
        self.assertEqual(facade.kw.get("user_name"), "小明")

    def test_old_signature_is_supported_by_stepping_down(self):
        """旧门面不认某个关键字：逐级降级也要拿得到 brief（不能把门面路径判死）。"""

        class Picky:
            def __init__(self):
                self.calls: list = []

            def brief(self, role_id, user_id="", **kwargs):
                self.calls.append(set(kwargs))
                if "char_name" in kwargs:
                    raise TypeError("unexpected keyword argument 'char_name'")
                return {"v": 2, "ok": True}

        picky = Picky()
        out = core_facade.brief(
            picky, "b1", "u1", task="proactive",
            char_name="小娜", user_name="小明",
        )
        self.assertEqual(out, {"v": 2, "ok": True})
        self.assertEqual(len(picky.calls), 2, "应当在第一次 TypeError 后立刻降级")
        self.assertIn("char_name", picky.calls[0])

    def test_minimal_signature_still_works(self):
        """最旧的门面只认 (role_id, user_id)：最终退到无关键字调用也成功。"""

        class Old:
            def __init__(self):
                self.calls = 0

            def brief(self, role_id, user_id=""):
                self.calls += 1
                return {"v": 2, "ok": True}

        old = Old()
        self.assertEqual(
            core_facade.brief(old, "b1", "u1", task="proactive"),
            {"v": 2, "ok": True},
        )
        # TypeError 在进函数体之前就抛了，所以只有最后那次无关键字调用被记到。
        self.assertEqual(old.calls, 1)

    def test_raising_or_non_dict_returns_none(self):
        class Boom:
            def brief(self, *a, **k):
                raise RuntimeError("boom")

        class Bad:
            def brief(self, *a, **k):
                return "不是 dict"

        self.assertIsNone(core_facade.brief(Boom(), "b1", "u1"))
        self.assertIsNone(core_facade.brief(Bad(), "b1", "u1"))
        self.assertIsNone(core_facade.brief(None, "b1", "u1"))

    def test_capabilities_helper_and_user_probe(self):
        class F:
            def capabilities(self):
                return ["brief", "user", "user", 3, ""]

        facade = F()
        self.assertEqual(core_facade.capabilities(facade), ["brief", "user", "3"])
        self.assertTrue(core_facade.has_capability(facade, "user"))
        self.assertFalse(core_facade.has_capability(facade, "identity"))
        self.assertEqual(core_facade.capabilities(None), [])
        self.assertFalse(core_facade.has_capability(None, "user"))

    def test_broken_capabilities_do_not_raise(self):
        class Boom:
            def capabilities(self):
                raise RuntimeError("boom")

        self.assertEqual(core_facade.capabilities(Boom()), [])
        self.assertFalse(core_facade.has_capability(Boom(), "user"))


if __name__ == "__main__":
    unittest.main()
