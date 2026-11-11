# -*- coding: utf-8 -*-
"""v2.2.31：engine 传给 groupflow 的关键字，必须是函数真有的。

起因（线上真事故）：v2.2.29 把 `live_minutes` / `flow_on` 两个新参数加到了
`groupflow.group_topic_due`，但 engine 里写到了 `groupflow.icebreak_due(...)` 那一处
调用上 → 每次破冰都 `TypeError: icebreak_due() got an unexpected keyword argument
'live_minutes'`，被外层 `except` 吞成一行 WARN「本轮破冰出错」，**破冰整条链路静默失效**，
单元测试还全绿（因为只测了 groupflow 函数本身，没测 engine 怎么调用）。

所以这个测试用 AST 把 engine.py 里所有 `groupflow.xxx(...)` 调用抠出来，
逐个跟 `inspect.signature` 对，任何多出来的关键字都算失败。
"""
import ast
import importlib
import inspect
import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    filters = types.ModuleType("astrbot.api.event.filter")
    filters.event_message_type = lambda *a, **k: (lambda fn: fn)
    filters.EventMessageType = type("EventMessageType", (), {"ALL": "all"})
    filters.on_llm_request = lambda *a, **k: (lambda fn: fn)
    filters.on_decorating_result = lambda *a, **k: (lambda fn: fn)
    filters.after_message_sent = lambda *a, **k: (lambda fn: fn)
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = type("Plain", (), {"__init__": lambda self, text="": setattr(self, "text", text)})
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = object
    star.register = lambda *a, **k: (lambda cls: cls)
    api.event = event
    api.message_components = components
    api.star = star
    astrbot.api = api
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.event.filter": filters,
        "astrbot.api.message_components": components,
        "astrbot.api.star": star,
    })


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

ENGINE_SRC = (PLUGIN_ROOT / "social" / "engine.py").read_text(encoding="utf-8")


def _call_sites():
    """(行号, 函数名, [关键字名]) —— engine.py 里所有 groupflow.xxx(...) 调用。"""
    tree = ast.parse(ENGINE_SRC)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
            continue
        if fn.value.id != "groupflow":
            continue
        out.append((getattr(node, "lineno", 0), fn.attr, [k.arg for k in node.keywords if k.arg]))
    return out


class GroupflowKeywordTest(unittest.TestCase):
    def test_calls_exist(self):
        sites = _call_sites()
        self.assertGreaterEqual(len(sites), 2, "engine.py 里应该至少有破冰/起话头两处 groupflow 调用")
        names = {n for _, n, _ in sites}
        self.assertIn("icebreak_due", names)
        self.assertIn("group_topic_due", names)

    def test_every_keyword_is_supported(self):
        """任何 groupflow 调用都不能传函数签名里没有的关键字。"""
        gf = importlib.import_module("social.groupflow")
        bad = []
        for line, name, kws in _call_sites():
            fn = getattr(gf, name, None)
            if fn is None:
                bad.append((line, name, ["<groupflow 里没有这个函数>"]))
                continue
            allowed = set(inspect.signature(fn).parameters)
            extra = [k for k in kws if k not in allowed]
            if extra:
                bad.append((line, name, extra))
        self.assertEqual(bad, [], f"engine.py 传了 groupflow 不接受的关键字：{bad}")

    def test_live_guard_is_on_topic_not_icebreak(self):
        """v2.2.29 的守卫只能挂在起话头上：破冰不该有 live_minutes。"""
        for line, name, kws in _call_sites():
            if name == "icebreak_due":
                self.assertNotIn("live_minutes", kws, f"第 {line} 行：破冰不该传 live_minutes")
                self.assertNotIn("flow_on", kws, f"第 {line} 行：破冰不该传 flow_on")
            if name == "group_topic_due":
                self.assertIn("live_minutes", kws, f"第 {line} 行：起话头要传 live_minutes 才生效")


if __name__ == "__main__":
    unittest.main()
