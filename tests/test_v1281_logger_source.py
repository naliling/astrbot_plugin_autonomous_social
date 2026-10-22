"""上架硬规则：`logger` 只能来自 `astrbot.api`，插件代码里不许出现标准库 `logging`。

v1.28.1 被上架自动安全审核退回，唯一必改项就是这个。审核原文点名的两处：
`social/config.py:21-23` 与 `social/memory_bridge.py:35-37` —— 它们在
`try: from astrbot.api import logger` 失败时回退 `import logging;
logging.getLogger("autonomous_social")`。规则明确要求 logger 必须且只能从
`astrbot.api` 导入、严禁使用内置 logging 模块。

审核同时说明：插件本就只在 AstrBot 内运行，该回退分支实际永远走不到；本仓库
CHANGELOG v1.9.1 也以同样理由删掉过 generator.py/persona.py 的同类回退。

这条守卫因此**按整棵生产树**扫，不按那两个文件名扫——只要生产代码里再出现
`import logging` / `from logging import ...` 就红，免得下次换个写法又漏过去。
写法与 `humanoid_core/tests/test_v22729_logger_source.py` 同一套（AST 级检查）。
"""

from __future__ import annotations

import ast
import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = (
    _HERE.parent
    if (_HERE.parent / "_conf_schema.json").is_file()
    else _HERE.parent / "astrbot_plugin_autonomous_social"
)
LOG_MODULE = "logging"

#: 生产源码：业务包 + 插件入口（`tests/` 自己要 import logging 做断言，不在此列）。
PRODUCTION_FILES = sorted((PLUGIN_ROOT / "social").rglob("*.py")) + [
    PLUGIN_ROOT / "main.py"
]


def _install_stub():
    """装一个最小 astrbot stub。

    这里**不能**因为 `"astrbot" in sys.modules` 就早退：同一次 unittest 进程里，
    先跑的测试文件可能只造了一个空壳 `astrbot`（没有 `.api`），直接拿它 import
    下游模块会炸。所以每次都补齐 `astrbot.api.logger`。

    不替换已有的 logger 对象——先跑的那个测试文件可能已经把它设成自己的了，
    而下游模块此刻已经 import 完、拿着旧对象。替换它只会让两个 stub 不一致。
    """
    astrbot = sys.modules.get("astrbot")
    if astrbot is None:
        astrbot = types.ModuleType("astrbot")
        sys.modules["astrbot"] = astrbot
    api = getattr(astrbot, "api", None)
    if not isinstance(api, types.ModuleType):
        api = types.ModuleType("astrbot.api")
        astrbot.api = api
        sys.modules["astrbot.api"] = api
    if not hasattr(api, "logger"):
        api.logger = logging.getLogger("astrbot")


_install_stub()


def _logging_imports(path: Path) -> list[str]:
    """AST 级检查：只看真正的 import 语句。

    不用文本正则，是因为「logging」会合法地出现在注释、docstring、变量名里
    （本仓库就有若干处解释这条规则的注释），正则会把它们误判成违规。
    """
    found: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == LOG_MODULE or alias.name.startswith(LOG_MODULE + "."):
                    found.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == LOG_MODULE or module.startswith(LOG_MODULE + "."):
                names = ", ".join(a.name for a in node.names)
                found.append(f"line {node.lineno}: from {module} import {names}")
    return found


class NoStdlibLoggingTest(unittest.TestCase):
    """审核规则本身：生产代码里一个标准库 logging 都不许有。"""

    def test_production_tree_scanned(self) -> None:
        """确保下面那条扫描真的扫到了东西（否则它可能空转变绿）。"""
        relative = {p.relative_to(PLUGIN_ROOT).as_posix() for p in PRODUCTION_FILES}
        self.assertIn("main.py", relative)
        self.assertIn("social/config.py", relative)
        self.assertIn("social/memory_bridge.py", relative)
        self.assertIn("social/engine.py", relative)
        self.assertGreaterEqual(len(relative), 20, f"扫到的生产文件太少：{len(relative)}")

    def test_no_production_module_imports_stdlib_logging(self) -> None:
        offenders = {
            path.relative_to(PLUGIN_ROOT).as_posix(): _logging_imports(path)
            for path in PRODUCTION_FILES
        }
        offenders = {name: hits for name, hits in offenders.items() if hits}
        self.assertEqual(
            offenders,
            {},
            "上架规则：logger 必须且只能 `from astrbot.api import logger`，"
            f"严禁内置 logging 模块。违规文件：{offenders}",
        )


# 为什么不写「模块拿到的必须是 astrbot.api.logger 本体」那类运行时断言：
# 随包发布的其它测试文件各自 `api.logger = logging.getLogger(<自己的名字>)`，而一次
# unittest 进程里 stub 与模块都只初始化一次，先跑的那个文件决定了 logger 长什么样。
# 那种断言在本仓库的测试组织下必然误报（Core 那边能这么写，是因为它的 stub 用的是
# 与真 AstrBot 同形的代理对象）。规则真正要拦的「用回了标准库 logging」由上面的
# AST 扫描完整覆盖，且它不依赖导入顺序。

if __name__ == "__main__":
    unittest.main()
