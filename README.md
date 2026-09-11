# 自主拟人社交

## 功能

### 核心机制
- 随机时间检查，不使用固定发送间隔。
- 加权随机选择联系人，不固定找最高好感度用户。
- 全局冷却、用户冷却、近期活跃保护、安静时段。
- 没有回复不会自动连续追发。
- LLM 只负责生成自然语言，调度逻辑由插件负责。

### v1.4.0 拟人化增强

**时段感知**：清晨、上午、午休、下午、傍晚、深夜、凌晨各有独立的概率曲线和语气指导。傍晚最活跃，凌晨几乎不联系。周末可额外提升概率。

**能量影响**：读取 Humanoid Core 的精力和社交能量值。AI 累了就不太想说话（概率降低、消息更简短），社交能量低了就不太想社交。

**回复检测**：追踪每次主动消息后用户是否回复。回复率高的用户会被更频繁地联系，从不回复的用户会被逐渐"冷落"。

**对话记忆**：保留每用户最近 10 条对话记录，生成消息时提供上下文。从用户消息中提取话题关键词，用于自然地延续对话。检测未回答的问题作为优先联系理由。

**关系分级**：根据好感度分为 5 个等级，不同等级有不同的语气：熟人礼貌克制，亲密关系自然表达在意和想念。

**Prompt 工程**：完全重写的 LLM prompt，融入时段、精力、情绪、关系、历史对话、话题和性格提示。消息长度可配置。

### 兼容性
- 支持 `auto`、`humanoid`、`standalone` 三种模式。
- 与 Humanoid Core 只读衔接，不修改 Core 状态。
- 社交状态按 bot 独立保存。
- 状态文件自动从 v1 迁移至 v2。

## 文件
插件根目录直接包含 `main.py`、`metadata.yaml`、`_conf_schema.json`。
```
social/
  __init__.py
  config.py        # 配置数据类
  state.py         # 状态持久化 + 对话历史 + 回复追踪
  core_bridge.py   # Humanoid Core 只读桥接
  reasoning.py     # 时段感知 + 关系分级 + 理由生成
  generator.py     # LLM prompt 工程
  engine.py        # 主引擎：概率 + 选人 + 调度
```

## 数据
`data/plugin_data/astrbot_plugin_autonomous_social/state.json`

## 指令
`/自主社交状态`

## 配置项

| 配置 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否启用自主社交 |
| `mode` | `auto` | 运行模式 |
| `activity_level` | `45` | 活跃度（10-90） |
| `global_cooldown_minutes` | `45` | 全局冷却（分钟） |
| `user_cooldown_minutes` | `180` | 用户冷却（分钟） |
| `quiet_start` | `23` | 安静时段开始 |
| `quiet_end` | `7` | 安静时段结束 |
| `private_only` | `true` | 仅私聊 |
| `debug` | `false` | 决策日志 |
| `personality_hint` | `""` | 性格提示词 |
| `max_message_length` | `200` | 消息最大长度 |
| `topic_memory_count` | `5` | 话题记忆数量 |
| `weekend_boost` | `true` | 周末概率提升 |
| `energy_threshold` | `15` | 精力阈值 |
| `social_energy_threshold` | `20` | 社交能量阈值 |
| `adaptive_reply_rate` | `true` | 自适应回复率 |
| `min_initiate_interval_minutes` | `120` | 最短联系间隔 |
