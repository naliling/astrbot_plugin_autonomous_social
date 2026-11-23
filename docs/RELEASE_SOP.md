# 发版 SOP checklist（autonomous_social）

送检报告 v2.2.51 第七节「方向七」的落地：把发版从靠记忆变成逐条勾选。
每次发版按顺序做完，缺一项都不许打包。

## 发版前

1. **版本号三处同步**：`metadata.yaml` 的 `version`、`social/__init__.py` 的
   `__version__`、`CHANGELOG.md` 最新的 `## vX.Y.Z ` 标题。每次升级自动 +0.0.1，
   不用请示。三处不一致时 `pack_as.py` 会直接拒绝打包。
2. **CHANGELOG 写新条目**：只写「加的 / 改的 / 修的 / 测试」，功能项不超过 20 字；
   涉及默认值改动的，补 `### 默认值变更` 与 `### 上限变更` 表格。
3. **默认值四处同步**：`_conf_schema.json` 的 default、`social/config.py` 常量区、
   `SocialConfig` dataclass 字段、README「全部配置项」表。
4. **一致性门禁**：跑 `python -m unittest tests.test_v2252_defaults_source_sync`，
   确认 145 项默认值对拍与版本号三处一致全过；任一漂移先修再发。
5. **全量测试**：跑 `python -m unittest discover -s tests -p "test_*.py"`，
   记录 RAN / FAIL / ERR 数字，FAIL 或 ERR 非 0 不发版。
6. **打包**：`python misc/_dev/pack_as.py <版本号>`，确认逐文件 sha256 全部一致，
   生成 `.zip` / `.zip.sha256` / `.manifest.json`。
7. **发行清理**：包里不得有 `__pycache__` / `.pyc` / `.log` / `state.json`；
   打包脚本已排除，打包后回读 zip 顶层目录再确认一次。

## 发版后

8. 把 zip、sha256、manifest 放进 `C:\astrbotc插件\`。
9. 真机升级：先卸载旧版（不勾「删除配置」「删除数据」）再装新 zip，或直接覆盖
   `data/plugins/` 目录后重载插件；状态与配置都不丢。
10. 装完发 `/自主社交状态` 与 `/社交自检` 看链路，再观察一轮心跳的实际发言。
