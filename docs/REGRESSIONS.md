# 回归守则（社交插件）

这份表记的是**反复犯过的毛病**，不是功能清单。每一条后面都挂着一个（或一组）
会在旧行为下变红的测试——以后要改同一块代码，先跑它。

用法：

```powershell
$env:PYTHONIOENCODING='utf-8'
..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

定位单条：

```powershell
..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider `
  tests/test_v1280_group_voice.py -k "NoName"
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
| 群名单/群消息里的换行伪造出 prompt 结构（假块头、假指令） | 昵称/正文是群友自己能改的，直接拼进提示词 | `tests/test_v1273_review_fixes.py::GroupMemberNameTest`、`…::GroupRecentSanitizingTest` | 任何要进 prompt 的群友文本都必须过 `_safe_member_name` / 压成一行；破冰/起话头现在没有名单块，但 `own_recent`、`style_ref`、`latest` 仍在，同样适用。 |

## 两条通用纪律

1. **改行为必须留一条会在旧行为下变红的测试。** 先写「原来会怎样」（多数用例的
   docstring/断言就钉在那个点上），再写「现在怎样」；
2. **不要删既有测试。** 语义被用户明确改变时，改断言并在 `CHANGELOG.md` 里写清原因
   （v1.28.0 的「旧测试的断言改动」一节是例子：两人对聊硬拦改用
   `interject_enabled=False` 保留旧语义，而不是删掉那条用例）。
