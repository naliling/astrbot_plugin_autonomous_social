# tests/ 为什么有两份

这个目录是**随包发布**的（和 Core 一样）——容器里的审查工具据此判断这个插件
「有一个测试套件」，出问题时也能直接定位到版本。

工作区根的 `~/workspace/tests/` 也有一份同样的文件。**两份必须一致**，
否则会出现「工作区全绿、发出去的包跑不过」。

- 开发时改 `~/workspace/tests/test_autonomous_social.py`
- 打完包前同步：`cp astrabot_plugin_autonomous_social/tests/*.py tests/`
- 校验一致性：`diff -q tests/test_autonomous_social.py astrabot_plugin_autonomous_social/tests/test_autonomous_social.py`

`test_autonomous_social.py` 里的 `PLUGIN_ROOT` 同时认两种摆法
（tests 在插件里 / tests 在工作区根），所以两份都能直接跑。
