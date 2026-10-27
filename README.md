<p align="center">
  <img src="logo.png" alt="主动消息 logo" width="140" />
</p>

# 主动消息

> **她攒够了念头，会先开口。**
> 没说完的话，她记得回来问。
> 不是定时器：什么时候开口、对谁开口、说什么，由念头和一件具体的事决定。

![version](https://img.shields.io/badge/version-v1.28.16-7c3aed?style=flat-square)
![platform](https://img.shields.io/badge/platform-QQ%20%7C%20%E4%BB%BB%E6%84%8F%20AstrBot%20%E5%B9%B3%E5%8F%B0-2ea44f?style=flat-square)
![python](https://img.shields.io/badge/python-3.10%2B-3776ab?style=flat-square)
![license](https://img.shields.io/badge/license-%E7%A7%81%E7%94%A8-blue?style=flat-square)

<sub>作者 **娜莉灵** · 插件 ID `娜莉灵/astrbot_plugin_autonomous_social` · 显示名 **主动消息**</sub>

---

## 它做什么

让她在**没有收到消息的时候**也会主动找你，而且说得像个有自己生活的人，不像一个定时器。

- **先有件具体的事，才开口**：她刚做完的、你提过没下文的、到点该问的；
- **念头只管等多久，不管发不发**：攒够了也要有由头，派不出就不说；
- **不合适就憋着**：模型可以先否决，验收不过也会拦下；
- **三条不发**：派不出由头、模型否决、验收不过——都不发，念头留着下一轮再试。

一次主动消息大概这么走完：看状态 → 选人 → 找由头 → 过闸（安静时段、冷却、频率、关系档）→ 问模型该不该说 → 写 → 发 → 记账。

---

## 特性

![主动开口](https://img.shields.io/badge/-%E4%B8%BB%E5%8A%A8%E5%BC%80%E5%8F%A3-7c3aed?style=flat-square) ![未完话题](https://img.shields.io/badge/-%E6%9C%AA%E5%AE%8C%E8%AF%9D%E9%A2%98-7c3aed?style=flat-square) ![群聊心流](https://img.shields.io/badge/-%E7%BE%A4%E8%81%8A%E5%BF%83%E6%B5%81-7c3aed?style=flat-square) ![破冰](https://img.shields.io/badge/-%E7%A0%B4%E5%86%B0-7c3aed?style=flat-square) ![念头系统](https://img.shields.io/badge/-%E5%BF%B5%E5%A4%B4%E7%B3%BB%E7%BB%9F-7c3aed?style=flat-square) ![关系记忆](https://img.shields.io/badge/-%E5%85%B3%E7%B3%BB%E8%AE%B0%E5%BF%86-7c3aed?style=flat-square) ![长期记忆](https://img.shields.io/badge/-%E9%95%BF%E6%9C%9F%E8%AE%B0%E5%BF%86-7c3aed?style=flat-square) ![主动消息](https://img.shields.io/badge/-%E4%B8%BB%E5%8A%A8%E6%B6%88%E6%81%AF-7c3aed?style=flat-square) ![时机判断](https://img.shields.io/badge/-%E6%97%B6%E6%9C%BA%E5%88%A4%E6%96%AD-7c3aed?style=flat-square) ![冷却限频](https://img.shields.io/badge/-%E5%86%B7%E5%8D%B4%E9%99%90%E9%A2%91-7c3aed?style=flat-square) ![管理员指令](https://img.shields.io/badge/-%E7%AE%A1%E7%90%86%E5%91%98%E6%8C%87%E4%BB%A4-7c3aed?style=flat-square)

### 什么时候开口

- **主动开口** —— 攒够念头才开口；
- **未完话题** —— 没说完的会接上；
- **追问** —— 你敷衍时问那件事；
- **回访** —— 没结果的事回头问；
- **主动收场** —— 没人接就收尾；
- **问候** —— 隔久了问候一句；
- **静音结束** —— 到点先醒一次，不等下一轮心跳。

### 关系与分寸

- **关系分档** —— 越熟联系越自然，越生越克制；连着发不出去就停发，等对方先开口；
- **安静时段** —— 深夜不打扰；
- **同平台 bot** —— 也能聊；主动找它隔 6 小时、一天最多一条；
- **跨用户去重** —— 不群发同一句。

### 群聊

- **群聊心流** —— 群里自然接话；群里刚安静下来不硬接；
- **破冰** —— 冷场时开个话头；
- **不点名** —— 主动开口不叫人；
- **群聊另算一套** —— 被叫到、接得上才接话；冷场时破冰；没话说就不发，不硬凑。

### 记得住

- **历史导入** —— 认识装插件前就聊过的人；
- **播种名单** —— 指定的人也能找；
- **长期记忆** —— 按由头读记忆。

### 说话的样子

- **正在输入** —— 写的时候显示输入中；
- **连发** —— 拆成几条短句发。

---

## 怎么用（三十秒）

**1. 装上**

AstrBot 插件页 → 本地安装，上传 zip；或者把整个文件夹放进 `AstrBot/data/plugins/`，然后重载插件。

**2. 面板里改配置**

不用手改文件。第一次只动四项就够：`activity_level`（整体更主动 / 更安静）、`urge_refill_hours`（两条消息之间隔多久）、`recent_talk_minutes`（刚聊完多久内不另起话）、`quiet_start` / `quiet_end`（晚上几点到几点不说话）。其余项默认值已经调好。

**3. 看一眼通没通**

重载后发 `/自主社交状态`：权限判定依据、LLM 状态、Core 联动会一次列清。

> **升级注意**：卸载时**不要勾**「删除配置」「删除数据」，或者直接覆盖插件目录后重载；状态与配置都不会丢。

---

## 指令

全部指令默认只有机器人主人（AstrBot 的 `admins_id`）与 AstrBot 管理员能用。要放给别人：先关掉 `owner_only_commands`，再把对方 ID 填进 `allowed_trigger_uids`（关闭后白名单才生效）；非主人在群里发这些指令时，插件完全静默。

| 指令（仅管理员） | 作用 |
| --- | --- |
| `/自主社交状态` | 看运行状态、念头面板、Core 联动、发送通道 |
| `/社交自检` | 不调模型不发消息，几毫秒查链路通不通 |
| `/触发社交` | 立刻挑一个最想说的人开口 |
| `/主动消息预览 [用户ID]` | 只生成不发送，看她此刻想说什么 |
| `/主动消息记录 [用户ID]` | 最近 7 天发过什么，附一句为什么 |
| `/群社交状态` | 群聊心流在管哪些群、是否被隔离 |

> 忘了哪条能看什么：先发 `/自主社交状态`，它会把权限判定依据、LLM 状态、Core 联动一次列清。

---

## 配置

### 关键配置项

其余项大多有合理默认值，第一次用先看这四个：

| 想达到的效果 | 调这个 |
| --- | --- |
| 整体更主动 / 更安静 | `activity_level`（默认 75） |
| 两条消息之间至少隔多久 | `urge_refill_hours`（默认 4 小时） |
| 刚聊完多久内不另起话 | `recent_talk_minutes`（默认 18 分钟） |
| 晚上几点到几点不说话 | `quiet_start` / `quiet_end`（默认 23–7） |

> 想让她用会思考的模型说话：`proactive_provider_ids` 填主动消息专用模型，按顺序试第一个能用的；配了却一个都取不到时**本轮不主动开口**，不退默认模型、不重试——弱模型写出来的话，比不发更糟。

> 插件是按心跳醒来的（默认 8–15 分钟一次），所以分钟级配置最快也要等一个心跳才生效。想让分钟级配置真按分钟走，把 `heartbeat_min_minutes` 调到 1。

### 全部配置项

> 下表与 `_conf_schema.json` 同源，共 97 项；面板里看到的默认值与这里一致。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `mode` | `auto` | `auto` 自动探测 Core；`standalone` 完全不读 Core |
| `activity_level` | `75` | 整体想说说话的程度，缩放念头积累速度（不是「每隔几分钟发一条」）；不影响未完话题那几条 |
| `urge_refill_hours` | `4` | 【节奏】最在意的人攒满一次念头大约要多少小时；一般熟的人按比例更慢。只管另起话题 |
| `recent_talk_minutes` | `18` | 【节奏】刚聊完多久之内绝不另起新话题（不影响未完话题那几条） |
| `heartbeat_min_minutes` | `8` | 心跳间隔下限（分钟）。**决定所有分钟级配置的实际精度** |
| `heartbeat_max_minutes` | `30` | 心跳间隔上限（分钟），实际间隔在上下界之间随机取 |
| `llm_gate` | `true` | 发送前让模型判断该不该说，它可以说 NO |
| `memory_bridge_enabled` | `true` | 【记忆】装了「我会牢牢记住你」时，主动消息按由头读这个人的长期记忆、发完再写回，事有结果还会交棒；没装自动跳过 |
| `skip_cooldown_minutes` | `10` | 模型否决后，多久之内不再就同一个人重新纠结 |
| `respect_user_rhythm` | `false` | 按对方平时的活跃时段挑时机。**默认关**：本插件就是用来主动社交的，不该拿「对方现在多半没在线」压住开口的时机；深夜由 `quiet_start`/`quiet_end` 单独兜底 |
| `cue_followup` | `true` | 记住对方说的时间锚点，到点当成开口的由头 |
| `followup_enabled` | `true` | 【未完话题】这场话断了就接一句（自动分「追问那件事」与「问在不在」两种） |
| `probe_after_minutes` | `12` | 【未完话题】对方回得敷衍时，隔多久追问那件事 |
| `followup_after_minutes` | `30` | 【未完话题】双方都不说话了，隔多久问一句在不在 |
| `followup_max_minutes` | `70` | 【未完话题】超过这么久就别接了，像隔半天重新打招呼 |
| `followup_cooldown_minutes` | `30` | 【未完话题】两次接话之间的最短间隔（同一段沉默只会接一次，这是跨段护栏） |
| `continue_enabled` | `true` | 【接话】她说完、对方就没声了：过一阵接一句，或问一句「你干啥去了」。这不是另起话题，也不是追问那件事——是「话说完了，人走了」 |
| `continue_after_minutes` | `90` | 【接话】她上一条说完之后至少隔这么久才接话。太短像盯着人看，太长这场话就凉了 |
| `continue_window_hours` | `8` | 【接话】接话窗口最长多少小时，超过就当这场话已经过去了，不再接 |
| `silent_giveup_days` | `3` | 【回避】对方连续这么多天一句话没说、她这期间主动发过、也一直没人回，就全面停发（早安晚安也算），等对方先开口才恢复。0=关闭。真人不会一直追一个不回话的人 |
| `loop_enabled` | `true` | 【回访】记住对方提过、没说结果的事，过几小时问后来 |
| `loop_min_hours` | `2.5` | 【回访】回访窗口的下沿 |
| `loop_max_hours` | `14.0` | 【回访】回访窗口的上沿 |
| `closer_enabled` | `true` | 【收场】她主动发的话一直没人接时，自己冒一句收场 |
| `closer_after_hours` | `8` | 【收场】隔多久没人回才适合自己收尾（一共最多收三次） |
| `track_own_replies` | `true` | 【拟人地基】把 bot 自己说过的话也记进对话账本，追问才接得上话头 |
| `use_core_clock` | `true` | 【时区】安静时段与作息画像按她所在城市的小时算（需 Core 提供时区） |
| `use_core_facade` | `auto` | 【联动】走 Core 的同进程只读门面（契约 v2）：`auto` 找得到就用、找不到退回读状态文件；`on` 只走门面（验证用）；`off` 完全不碰（出问题时的逃生门，行为与旧版一致） |
| `proactive_provider_ids` | `['openai/gpt-6.1-sol', 'openai/gpt-6-sol', 'gemini/gemini-3.5-flash']` | 【模型】主动消息专用模型（脑子/思考好的）：主动消息是她自己想起来要说什么，要用会思考的模型生成，不是能跑就行。本插件所有模型调用（1:1 主动、群聊心流、破冰、起话头）按顺序试，第一个能用的即止。配了却一个都取不到时**本轮不主动开口**，等下一轮（心跳 15~120 分钟）——不退默认模型、不重试：弱模型写出来的主动消息比不发更糟。清空 = 关掉这个功能，回旧行为跟着会话走 |
| `user_cooldown_minutes` | `30` | 【护栏】同一个人两次被主动联系的最短间隔（每人独立，互不排队） |
| `max_sends_per_round` | `3` | 【护栏】一轮心跳里一个角色最多主动联系几个不同的人（防刷屏总闸）。每轮 LLM 尝试次数 = 这个数 × 2。一个人一轮里最多说 2 件不同的事，所以实际发出的条数会比这个数多一点 |
| `batch_send_concurrency` | `3` | 【进阶】一轮里对多个人「逐人生成、并发发送」的并发上限。只把「等生成」叠起来、更快发完，不改任何人的冷却/配额与内容。设 1 退回串行 |
| `quiet_start` | `23` | 安静时段开始（小时） |
| `quiet_end` | `7` | 安静时段结束（小时）；结束后会先补一次评估 |
| `private_only` | `true` | 仅私聊。只控制**要不要在群里主动说话**；群会话任何时候都不会被当成「人」导入（那与这一项无关） |
| `debug` | `false` | 决策日志（谁攒满了念头、为什么没说） |
| `max_message_length` | `60` | 单条最大长度（字） |
| `cross_user_repeat_ratio` | `0.6` | 同角色**发给别人的**消息和这条重合度超线就不发。跟防复读是两回事：Core 的日程是角色级的，同一句话对所有人逐字相同，真实记录里 1 小时 41 分内有 12 个不同的人分别收到它 |
| `cross_user_repeat_hours` | `2.0` | 跨用户撞车回看多久（小时）。设 0 关闭 |
| `cross_user_calibrate` | `false` | 记录相似度分布。阈值 0.6 是估的，开一天看真实分布再定线 |
| `hourly_sends_cap` | `24` | 每小时最多主动联系几人（按角色）。**安全闸不是节奏**：180 人同时攒满时防一次涌出，撞上限念头保留不清零。设 0 关闭 |
| `miss_affection_min` | `42` | 「念想」通道的好感门槛。对陌生人说「想你了」很怪，所以门要高 |
| `miss_daily_cap` | `3` | 每天最多几条「念想」。这一类不受「刚聊过」阻断，所以单独限次 |
| `min_gap_minutes` | `60` | **中等关系的人**两条主动消息的最小间隔（分钟）。关系很熟的 30 分钟、关系一般的隔 1~2 天。防扎堆另有 `hourly_sends_cap` 那道安全闸 |
| `greeting_midday_start` | `12` | 午间问候开始（小时） |
| `greeting_midday_end` | `14` | 午间问候结束（小时）。这是**早安错过时的兜底**，今天已经问候过就不会再补 |
| `gate_energy_floor` | `35` | 【硬闸】精力低于这个值就不主动开口（0=不按精力拦）。「累到坐不住」不交给模型判断 |
| `allow_burst` | `true` | 连发总开关：拆不拆与隔多久跟随该 Bot 的「分段回复」设置，关掉则只发一整条 |
| `input_state_enabled` | `true` | 【输入状态】她写主动消息的那几秒，QQ 会给对方显示她正在输入（仅 NapCat 私聊） |
| `input_state_interval_seconds` | `0.5` | 【输入状态】上报间隔（秒）。QQ 的状态约 8~10 秒自己会消失，隔太久会闪 |
| `input_state_timeout_seconds` | `120` | 【输入状态】超时兜底（秒）：生成/发送卡住时最多显示这么久 |
| `greeting_enabled` | `true` | 【问候】按「距上次说上话多久」触发，不绑日历。手上没新事时安静本身就是理由——「你好久没理我了」也算由头 |
| `greeting_morning_start` | `7` | 【早晚问候】早安窗口开始（小时） |
| `greeting_morning_end` | `11` | 【早晚问候】早安窗口结束（小时） |
| `greeting_night_start` | `21` | 【早晚问候】晚安窗口开始（小时） |
| `greeting_night_end` | `24` | 【早晚问候】晚安窗口结束（24 视作午夜 0） |
| `group_flow_enabled` | `true` | 【群聊心流】bot 在群里说过话后的关注窗口内，无 @ 主动接话（模型可拒答） |
| `group_icebreak_enabled` | `true` | 【群聊心流】群冷场时主动抛个轻话题 |
| `group_ref_lib_enabled` | `true` | 【群聊心流】采集群友发言风格并注入生成 |
| `group_topic_enabled` | `true` | 【群聊心流】她在群里静了一阵、又有现成话头（她自己的生活/节日/天气）时主动起个话题，而不只是接话；没话说可以不发 |
| `group_topic_gap_hours` | `8.0` | 【群聊心流】在同一个群里静了这么久之后，才考虑主动起话头 |
| `group_topic_daily_cap` | `1` | 【群聊心流】每群每天最多主动起几次话头 |
| `group_mention_member` | `false` | 【群聊心流】接话时允许叫正在接的那个人一句（起话头/破冰不会点名）。默认关：主动开口是对着整个群说的 |
| `group_interject_others` | `true` | 【群聊心流】别人对聊/在叫别人时可以插话，由模型判断接得上才插；关掉则这种场合一律不插 |
| `group_store_message_text` | `true` | 【群聊心流】是否落盘群友发言正文作为参考库（关掉则参考库为空） |
| `flow_hold_seconds` | `120` | 【群聊心流】Bot 被回完一句后，先静多久再接话（防自己跟自己连成一串） |
| `flow_window_minutes` | `8` | 【群聊心流】bot 说过话后关注窗口持续多少分钟 |
| `flow_min_gap_seconds` | `120` | 【群聊心流】两次插话最短间隔（秒） |
| `flow_max_replies_per_window` | `3` | 【群聊心流】一个窗口内最多接几条 |
| `flow_hourly_cap` | `6` | 【群聊心流】每群每小时插话总上限（防刷屏总闸） |
| `flow_ignored_exit` | `2` | 【群聊心流】连着插几条都没人接就退出心流安静下来 |
| `group_idle_hours` | `6.0` | 【群聊心流】群安静超过多少小时才考虑破冰 |
| `icebreak_daily_cap` | `2` | 【群聊心流】每群每天最多破冰几次 |
| `group_stale_days` | `3` | 【群聊心流】超过多少天没见消息就当已不在这个群（不再破冰并清样本） |
| `group_ref_sample_size` | `30` | 【群聊心流】参考库每群保留多少条样本 |
| `group_ref_prompt_count` | `8` | 【群聊心流】生成时抽多少条当风格参考（设 0 不注入） |
| `topic_memory_count` | `5` | 话题记忆数量 |
| `context_inject_count` | `10` | 生成时注入多少条最近对话当上下文（含 bot 自己的发言） |
| `weekend_boost` | `true` | 周末念头涨得快些 |
| `energy_threshold` | `15` | 精力低于此值念头几乎涨不动（需 Core） |
| `social_energy_threshold` | `20` | 社交能量低于此值念头涨得极慢（需 Core） |
| `adaptive_reply_rate` | `true` | 把「对方接不接话」算进在意度 |
| `reply_window_hours` | `2` | 多久内回话算「接住了」（回复率统计口径） |
| `intimacy_affection_min` | `70` | 【想要了】好感要到这里、且确实有过亲密史，才会被主动问 |
| `intimacy_libido_rise` | `6.0` | 【想要了】亲近欲比基线高多少才算「想要了」。有过亲密史也不免检——不然一旦有过就会一直问 |
| `intimacy_cooldown_hours` | `48` | 【想要了】问过一次隔多久才会再问（小时） |
| `owner_only_commands` | `true` | 开启：只有主人与 AstrBot 管理员可用，白名单不生效；关闭后白名单才生效 |
| `trigger_group_requires_at` | `true` | 【多 bot】群里「触发社交」是否必须 @ 本 bot。多台 bot 同群时保持开，哪台被 @ 就只触发哪台；单台部署想免 @ 可关 |
| `allowed_trigger_uids` | `[]` | 额外白名单，仅在 `owner_only_commands` 关闭时生效 |
| `tier_override` | `[]` | 关系档手动覆盖：逐条填 `uid:high`、`uid:mid` 或 `uid:low`。写了的人不再按好感涨幅推算，最小间隔/由头池/念想门槛都按指定档走 |
| `store_message_text` | `true` | 是否把私聊正文存入 state.json |
| `user_retention_days` | `30` | 长期不活跃用户的历史正文清理天数，0 = 永不清理 |
| `purge_inactive_user_days` | `0` | 【清理】超过这么多天没互动、她发过去也没人回的会话，连整条记录一起永久删除（私聊与群）；0 = 永不清理。比 `user_retention_days` 更彻底：那个只清正文 |
| `history_ingest` | `true` | 【播种】启动与每半小时读 AstrBot 会话库，把装插件前就聊过的人导进来 |
| `seed_users` | `[]` | 【播种】没聊过的人也能写进来，她攒够念头会主动找上门（user_id 或 platform:user_id） |
| `seed_platform` | `aiocqhttp` | 【播种】seed_users 只填 user_id 时拼私聊目标用的平台标识 |

---

## 联动

### 和人形化伴侣一起用（可选）

- 装了：能读到她累不累、今天在忙什么、她那边几点——精力低就少开口；
- 没装：插件用自己的互动记录、念头和冷却判断，功能不受影响；
- 分工：人形化伴侣管她的生活，本插件管她什么时候开口。

### 和长期记忆一起用（可选）

- 装了「我会牢牢记住你」：开口前按这次的由头读这个人的长期记忆，发完再写回；
- 没装：主动消息照常发，只是少这一份记忆。

---

## 数据与边界

- **状态文件**：`data/plugin_data/astrbot_plugin_autonomous_social/state.json`
- **存什么**：互动记录、话题关键词、时间锚点、念头与节奏、发送记录
- **正文存不存**：`store_message_text`（默认开），关掉只记结构不记原文
- **保留多久**：`user_retention_days`（默认 30 天，0 = 永不清理）
- **整条记录清理**：`purge_inactive_user_days`（默认 0 = 关；开了会永久删除整条会话记录，不只是正文）
- **读不出来时**：先备份成 `state.json.unreadable.<时间戳>` 再报错，不会拿空状态盖掉
- **平台边界**：主动消息要平台支持主动发送（如 qq_official 这类可能发不出去）；发失败不算一次成功交流，也不会误记冷却

---

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md) · 仓库 [naliling/astrbot_plugin_autonomous_social](https://github.com/naliling/astrbot_plugin_autonomous_social)
