# tests/ 为什么有两份

这个目录是**随包发布**的（和 Core 一样）——容器里的审查工具据此判断这个插件
「有一个测试套件」，出问题时也能直接定位到版本。

工作区根的 `~/workspace/tests/` 也有一份同样的文件。**两份必须一致**，
否则会出现「工作区全绿、发出去的包跑不过」。

- 开发时改 `~/workspace/tests/test_autonomous_social.py`
- 打完包前同步：`cp astrabot_plugin_autonomous_social/tests/test_*.py tests/` 之外的**具名**复制，
  例如 `cp ~/workspace/tests/test_autonomous_social.py astrabot_plugin_autonomous_social/tests/`
- 校验一致性：`diff -q tests/test_autonomous_social.py astrabot_plugin_autonomous_social/tests/test_autonomous_social.py`

> **不要用 `cp .../tests/*.py`。** 工作区根的 `tests/` 里除了本插件的测试，还有
> 工作区级的共享工具（`test_orbit.py` 测的是另一个插件 Orbit Cache、`test_selfcheck.py`
> 依赖 `astrbot_testkit`、`test_plugin.py` 依赖 `astrbot_stub`）。通配符复制会把它们
> 一起塞进包里，于是**装好插件跑测试会看到 20 个 error**，而且 `test_orbit.py` 测的是
> 完全无关的代码。随包发布的测试必须只有本插件自己的。

`test_autonomous_social.py` 里的 `PLUGIN_ROOT` 同时认两种摆法
（tests 在插件里 / tests 在工作区根），所以两份都能直接跑。
