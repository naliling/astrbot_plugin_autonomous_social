# 运行测试

这个目录是随插件发布的测试套件，用来确认这一版在你的环境里能跑通。测试全部使用内置的 AstrBot 替身与临时目录，**不联网、不改你的插件配置和数据**。

## 准备

- Python 3.10 及以上；
- 已安装 pytest：`pip install pytest`；
- 在插件根目录（和 `main.py` 同一层）执行下面的命令。

## 跑全量

```bash
python -m pytest tests -q
```

预期结果：全部通过（0 failed、0 error）。个别用例会打印提示，不影响结果。

## 只跑一部分

```bash
# 单个文件
python -m pytest tests/test_v1282_llm_circuit.py -q

# 按名字筛单条
python -m pytest tests/test_autonomous_social.py -q -k "括号"
```

## 常见问题

- **提示 `No module named 'astrbot'`**：不用装 AstrBot，测试自带最小替身；请确认是在插件根目录执行。
- **Windows 控制台中文乱码**：先设 `set PYTHONIOENCODING=utf-8`（PowerShell 用 `$env:PYTHONIOENCODING='utf-8'`）再跑。
- **测试产生的临时目录**：跑完可以直接删；`tmp*/` 已加入 `.gitignore`，不会进发行包。
