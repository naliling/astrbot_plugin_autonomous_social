# 回归守则（社交插件）

这份表记的是**反复犯过的毛病**，不是功能清单。每一条后面都挂着一个（或一组）
会在旧行为下变红的测试——以后要改同一块代码，先跑它。

用法（在插件根目录，和 `main.py` 同一层）：

```bash
python -m pytest tests -q
```

定位单条：

```bash
python -m pytest tests/test_v1280_group_voice.py -q -k "NoName"
```

## 一张表

| 症状（用户看到的） | 根因 | 守门测试 | 以后改这里要注意什么 |
|---|---|---|---|
| 天气成了默认台词：主动消息一条接一条全在说天气/「天亮了」 | 天气是唯一「永远有、永远具体」的素材；私聊 `_material_block` 摆过它、破冰 `topic_hint` 只给节日+天气，`derive_extras` 还绕过了去重闸 | `tests/test_v12741_proactive_content.py::WeatherGateTest`、`tests/test_v1280_group_voice.py::AnchorLifeSourcesTest::test_weather_is_single_entry_with_the_six_hour_gate`、`…::test_changed_weather_within_six_hours_is_held`、`…::GroupTopicHintTest::test_weather_is_gated_for_six_hours` | 天气只能有**一个**入口（`anchors.derive_extras` / `engine._group_topic_hint`），必须走 `weather_worth_telling` + `mark_weather_told`；任何「顺便把天气递进 prompt」的改动都会把它变回默认台词。群侧记账在**群状态**上，私聊在**用户状态**上，别混。 |
| 「刚忙完在厨房煮饭」这种自相矛盾的话 | 把 `day.doing`（此刻正在做）当成「刚做完」拼模板 | `tests/test_v1280_group_voice.py::AnchorLifeSourcesTest::test_doing_is_ongoing_not_just_finished`、`tests/test_v12741_proactive_content.py::VagueDoingTest` | `day.doing` 永远只作「正在」；「刚做完」用 `day.done`（Core 已经在文本里带了「下午/傍晚」时间词，不要再套模板）。doing 本身以「在」开头时别补「正在」。 |
| 主动消息发到群里会叫别人的名字、拿别人的名字提问 | `group_mention_member` 默认开，且所有群模式都把「群里的人」名单递给模型 | `tests/test_v1280_group_voice.py::GroupBoundaryNoNameTest::*`、`tests/test_v1280_flow_interject.py::FlowMentionPromptTest::*` | 破冰/起话头**永远不给名字**；心流也只给「最新的那一个」。不要把名单块加回来——模型看到名字就会用，这不是提示词措辞能压住的。 |
| 群里两个人正聊着，bot 硬要插一句；或者该插的时候永远不插 | `flow_should_consider` 把 `addressed_to` / `two_person_exchange` 设成硬拦，模型没有判断机会 | `tests/test_v1280_flow_interject.py::FlowInterjectGateTest::*`、`tests/test_p2_groupflow_pacing.py::FlowContextTest`（`interject_enabled=False` 保留旧语义） | 这两件事只能作为**语境**交给模型；要恢复旧行为就关 `group_interject_others`，不要在闸门里重新加 return False。频率/窗口/睡眠/安静时段这些闸不许跟着放松。 |
| 破冰/起话头每次都硬发一句，哪怕没什么可说 | 破冰没有「不发」的出口，模型只能硬写 | `tests/test_v1280_group_voice.py::IcebreakDecisionParseTest::test_no_means_do_not_send`、`…::IcebreakNoQuotaTest::test_no_does_not_send_or_count` | NO 路径要**早退**：不发、不占当天额度、不生成占位内容。宽容路径（只写正文、不写 SEND）也要保留，否则模型不听话时整条链路全哑。 |
| 「主动消息只有天气，不会真的找话题」 | 破冰/起话头素材只有节日+天气，她自己的日程/梦/没做完的事完全没进群素材 | `tests/test_v1280_group_voice.py::LifeLinesTest::*`、`…::TopicMaterialsTest::*`、`…::GroupTopicHintTest::test_life_first_then_weather`、`…::IcebreakPromptTest::*` | 素材顺序是「生活 > 节日 > 天气」；新增生活素材要进 `groupflow.life_lines`（有去重/每行 40 字/最多 4 条的硬约束）。`day.dreams`/`day.wants` 来自 Core 2.28，旧版 Core 没有——必须容错成空，不允许 `AttributeError`/`KeyError`。 |
| 天气在同一个群里连着两次破冰说同一句 | 群侧没有「说过天气」的账，或账记在用户状态上 | `tests/test_v1280_group_voice.py::IcebreakNoQuotaTest::test_send_counts_and_marks_the_weather`、`…::GroupTopicHintTest::test_weather_is_gated_for_six_hours` | 群侧天气账记在**群状态**（`g["weather_seen"]/["weather_seen_at"]`），且只在**发送成功**后记；失败路径不许提前消费。 |
| 面板上关了「点名/插嘴」，运行时却没变化 | 新配置项只加进 dataclass/schema，没在 `from_astrbot` 里读，或调用点没把这个值传下去 | `tests/test_v1280_flow_interject.py::InterjectConfigTest::*`、`…::EngineInterjectWiringTest::*`、`tests/test_v12733_options.py::ReadmeMatchesSchemaTest` | 配置三件套缺一不可：`config.py` 默认值 + `_conf_schema.json` + `from_astrbot` 读取；README 配置表也要同步（默认值要对得上）。调用点用 `inspect.getsource` 钉住，别只改 dataclass。 |
| 主动消息之后她「不知道自己说过什么」：下次主动消息把上次那句排到用户所有回复之后，于是接着一句早就说过的话往下接，或者当没问过一样再问一遍 | 会话库没有时间戳，`_generation_conversation` 却按**来源**拼接（会话库在前、账本追加到末尾），主动消息只存在于账本里，于是永远落在最后；且 `last_proactive_replied` 从来没被读进 prompt | `tests/test_v1281_proactive_context_order.py`（15 条，含连发不倒序、条数上限、脏条目、真实 `SocialState`）、`tests/test_v1281_logger_source.py` | 合并必须按**时间**：会话库当主干，账本缺条目按 `ts` 插位（`_splice_ledger_into_session`）。别给会话库条目编占位 ts 再整体排序——整段会被一起推走。会话库的重复条目要保留。「那句话有没有人接」只给事实（「已经 N 小时没人接话」），不给「别重复问」这类指令 |
| 活跃群里「模型选择不接或生成为空」刷屏：一分钟 14 条，每条都真打了一次 LLM | `note_flow_reply` 只在**成功发送**时更新 `flow_last_reply_at`，而闸门的「距上一句插话太近」查的就是它。于是「模型说不接」这条路什么都不记，下一条群消息看到的状态与上轮完全相同 → 再打一次。`flow_replies`/`flow_ignored`/`flow_hour_count` 同理都只长在发送成功路径上 | `tests/test_v1281_flow_attempt_throttle.py`（10 条：刚试过被挡、配额闸优先、三个配额不被尝试污染、旧存档按「从没试过」处理、设 0 关掉） | 加**独立的** `flow_last_attempt_at`，在调 LLM 之前记（`note_flow_attempt`），闸门查它。别去改 `flow_last_reply_at` 的更新时机——那会弄脏「两句真正发出去的话之间隔多久」的语义。间隔复用 `max(flow_min_gap_seconds, 60)`，不新增配置项 |
| 她突然「拿个手机出来」「看着屏幕上的消息」「只能打字给你」——用户瞬间出戏 | 不是人设教的（五个人设都没这些词），是模型的通病：聊天气泡里的对话，它自然借用现实通讯隐喻。真实数据：屏幕 50 处、手机 19 处、打字 3 处 | `tests/test_v1281_no_fourth_wall.py`（12 条：拦 9 + 不误伤 3） | 查**原文**，不要查 `strip_actions()` 之后的正文字——破墙点大量在括号里的动作描写中，剥完就没了。「消息」**不要**进词表（40 条里只 2 条真破墙，误伤率 95%） |
| 群名单/群消息里的换行伪造出 prompt 结构（假块头、假指令） | 昵称/正文是群友自己能改的，直接拼进提示词 | `tests/test_v1273_review_fixes.py::GroupMemberNameTest`、`…::GroupRecentSanitizingTest` | 任何要进 prompt 的群友文本都必须过 `_safe_member_name` / 压成一行；破冰/起话头现在没有名单块，但 `own_recent`、`style_ref`、`latest` 仍在，同样适用。 |
| 上架审核退回：`social/config.py` / `social/memory_bridge.py` 的 `except: import logging` 兜底 | 图省事给 logger 加了标准库兜底，而规则要求 logger 必须且只能从 `astrbot.api` 导入 | `tests/test_v1281_logger_source.py`（AST 扫整棵生产树） | 新增模块一律 `from astrbot.api import logger`，不要 try/except 兜底；本插件只在 AstrBot 内运行，兜底分支永远走不到 |

## 「现实→网友」/「变成手机发消息」是什么（v1.28.1 / v2.28.1 调查结论）

**不是插件的问题，也无需修。** 已按一位用户的反馈逐条核对过日志（其 QQ 号与昵称已隐去）。

### 数据

该用户 240 条 event_bus 记录：

| 类型 | 条数 | 昵称显示 |
|---|---|---|
| 真正的文字消息 | 39 | 正常显示昵称 |
| 戳一戳 / 图片 / 表情 / 语音 / 空 | 201 | 显示成 QQ 号 |

### 官方代码路径

`astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:211-214`（AstrBot 4.29.1 官方原文）：

    abm.sender = MessageMember(
        str(event.sender["user_id"]),
        event.sender.get("card") or event.sender.get("nickname", "N/A"),
    )

昵称只从 OneBot v11 事件的 `sender.card` / `sender.nickname` 取。而 **OneBot v11 规范里，
`notice` 类事件（戳一戳）和纯图片/表情/语音消息的 `sender` 字段本来就是空的或不完整**，
于是 AstrBot 只能回退显示 QQ 号。

695 个发件人里昵称含「关系/分组词」的只有 1 个，是对方自己起的昵称（其 QQ 号已隐去）。
没有任何插件调用过 `set_friend_category`（ccb_plus 只读 `get_stranger_info`；
relationship 的 `delete_friend`/`set_group_leave` 是删好友/退群，与分组无关且需命令触发；
user_tag 的「网友」是它自己关系库的 DEPRECATED 项，完全不碰 OneBot API）。

### 「突然变成手机发消息」

QQ 的「手机/Pad/PC」由**当前在线会话**决定。NapCat 用平板协议登录，掉线重连时若手机 QQ
恰好在线抢到主会话，对方那边显示就切回手机端。与上下文压缩无关（压缩只影响模型 prompt，
不影响 QQ 设备显示），是时间上的巧合。日志时间线：

    00:15:13  [ERRO] Exception on websocket /ws
    00:15:14  [INFO] aiocqhttp(OneBot v11) 适配器已连接
    00:20:26  [INFO] [连接已中断]
    00:21:48  [WARN] [阿莉娜] OneBot HTTP 3002 不通 (第1/15次)
    00:22:52  [RECOVER] [阿莉娜] 已恢复正常

### 处置

无需改插件。要减少观感只能从 NapCat 侧降低掉线频率（网络稳定性/心跳/避免多端抢占）。

## 两条通用纪律

1. **改行为必须留一条会在旧行为下变红的测试。** 先写「原来会怎样」（多数用例的
   docstring/断言就钉在那个点上），再写「现在怎样」；
2. **不要删既有测试。** 语义被用户明确改变时，改断言并在 `CHANGELOG.md` 里写清原因
   （v1.28.0 的「旧测试的断言改动」一节是例子：两人对聊硬拦改用
   `interject_enabled=False` 保留旧语义，而不是删掉那条用例）。
