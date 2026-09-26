"""社交配置数据类。

v1.7.4：
- 新增「念头」相关配置：攒满一次念头要多久、刚聊过多久内不另起、发送前是否让模型把关
- 冷却项语义降为护栏（节奏由念头决定），值不再靠默认生效：旧配置里存的是旧默认时，
  首次以本版本运行会一次性提升（AstrBot 只补默认值，从不覆盖已有值）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

# 有效运行模式
# 只有这两种：auto 会自动探测有没有 Core，standalone 则完全不读它。
# 曾经还有个人为的 "humanoid（强制要求 Core）"，但代码里从来没有对应分支，
# 选它和 auto 行为完全一样——一个假装存在的档位比没有更误导人。
VALID_MODES = {"auto", "standalone"}

# 配置范围常量
ACTIVITY_MIN = 10
ACTIVITY_MAX = 90
# 65：接近一个「闲下来就想找人说话」的人。v1.8.x 的 55 被反馈为「主动消息不够频繁」，
# 整体节奏再往上顶一格（念头攒得快 × 冷却放得低，两边一起动才有感觉）
# v1.9.x 拉到 75：更像一个平时就话比较多、闲不住想找人聊两句的人
ACTIVITY_DEFAULT = 75

# 冷却项现在只是护栏（防刷屏），真正的节奏由 urge 决定，所以默认值可以放得很低
USER_COOLDOWN_MIN = 10
USER_COOLDOWN_MAX = 2880
USER_COOLDOWN_DEFAULT = 30

# 一个最在意的人多久能攒满一次「想说两句」的念头
URGE_REFILL_MIN = 1
URGE_REFILL_MAX = 96
# 8 小时是对 interest=1（最在意的人）而言的，普通人按比例更慢，实际要攒十几个小时
# 才发得出一条——那与「插件在跑但永远没动静」基本同义。降到 4；
# v1.9.0 直接降到下限 2：再配合更低的冷却，主动消息才算真的「频繁」
# v1.9.x 降到下限 2：更像话多的人，心里一有事就想找人说（URGE_REFILL_DEFAULT 实际为 2）
URGE_REFILL_DEFAULT = 2

# 刚聊完多久之内绝不另起一个话题（真人不会话刚说完又发一句无关的）
# 心跳间隔（分钟）。它决定所有分钟级配置的实际精度
HEARTBEAT_MIN_MINUTES_MIN = 1
HEARTBEAT_MINUTES_MAX = 120
HEARTBEAT_MIN_DEFAULT = 8
HEARTBEAT_MAX_DEFAULT = 15

RECENT_TALK_MIN = 5
RECENT_TALK_MAX = 720
RECENT_TALK_DEFAULT = 18

# 这一场话断了：多久之后接一句。追问那件事（有由头）比光问「在吗」可以更早。
FOLLOWUP_AFTER_MIN = 2
FOLLOWUP_AFTER_MAX = 240
FOLLOWUP_AFTER_DEFAULT = 4
PROBE_AFTER_MIN = 1
PROBE_AFTER_MAX = 120
PROBE_AFTER_DEFAULT = 2
# 超过这么久再问就不像接话了，像隔了半天重新打招呼
FOLLOWUP_MAX_MIN = 15
FOLLOWUP_MAX_MAX = 720
FOLLOWUP_MAX_DEFAULT = 70
# 同一段沉默只接一次；两次跟进之间的最短间隔
FOLLOWUP_COOLDOWN_MIN = 5
FOLLOWUP_COOLDOWN_MAX = 720
FOLLOWUP_COOLDOWN_DEFAULT = 30

# 隔一阵回访那件事：对方提了个没说完结果的事，过几小时问「后来呢」
LOOP_MIN_HOURS_FLOOR = 1.0
LOOP_MIN_HOURS_CEIL = 24.0
LOOP_MIN_HOURS_DEFAULT = 2.5
LOOP_MAX_HOURS_DEFAULT = 14.0
# 收场那句：她主动发的话没人回，隔多久自己冒一句
CLOSER_AFTER_MIN = 2
CLOSER_AFTER_MAX = 72
CLOSER_AFTER_DEFAULT = 8

# 模型否决（想过但决定不说）之后，多久之内不再就同一个人重新纠结
SKIP_COOLDOWN_MIN = 5
SKIP_COOLDOWN_MAX = 480
SKIP_COOLDOWN_DEFAULT = 10

HOUR_MIN = 0
HOUR_MAX = 23
QUIET_START_DEFAULT = 23
QUIET_END_DEFAULT = 7

MAX_MSG_LEN_MIN = 10
MAX_MSG_LEN_MAX = 500
# 120：够把一件事说完整，而不是被硬砍成一两句。长度本身交给人设与情境，
# 这里只做防超长的硬上限；配合连发能自然铺开。v1.13.0 从 60 上调（60 常被
# 反馈为「只能发一两句」）。
MAX_MSG_LEN_DEFAULT = 120

TOPIC_MEMORY_MIN = 0
TOPIC_MEMORY_MAX = 20
TOPIC_MEMORY_DEFAULT = 5

# 生成时注入多少条「最近的对话」当上下文（私聊合并会话库与插件账本，群聊读近期发言）。
# 含 bot 自己发过的话（看得到自己说过什么）。太少接不上上文、显得生硬；
# 太多浪费 token 也容易跑题。
CONTEXT_INJECT_MIN = 0
CONTEXT_INJECT_MAX = 50
CONTEXT_INJECT_DEFAULT = 10

ENERGY_THRESHOLD_MIN = 0
ENERGY_THRESHOLD_MAX = 100
ENERGY_THRESHOLD_DEFAULT = 15

SOCIAL_ENERGY_THRESHOLD_MIN = 0
SOCIAL_ENERGY_THRESHOLD_MAX = 100
SOCIAL_ENERGY_THRESHOLD_DEFAULT = 20

# 播种：没聊过的人（seed_users 名单）攒念头的速度倍率。这是「礼貌倍率」不是频率旋钮：
# 跟一个素未谋面的人开口，间隔本来就比熟人长，不能拿熟人节奏硬套
SEED_URGE_SCALE = 0.5

# 早晚问候（时间性触发）：早安/晚安窗口（小时，按她所在城市的钟走）
GREETING_ENABLED_DEFAULT = True
GREETING_MORNING_START_DEFAULT = 7
GREETING_MORNING_END_DEFAULT = 11
GREETING_NIGHT_START_DEFAULT = 21
GREETING_NIGHT_END_DEFAULT = 24   # 24 视作 0（午夜）

# 每轮心跳里一个角色最多主动发几条（对不同用户）：防刷屏上限。
# v1.10.2 之前所有人排成一条队（每轮只发一个人 + 全局冷却跨用户互卡），
# 表现出来就是「跟A聊完要过好久才轮到B」。现在每个用户独立算，
# 这条只是别在一轮里全发出去的护栏。
MAX_SENDS_PER_ROUND_MIN = 1
MAX_SENDS_PER_ROUND_MAX = 10
MAX_SENDS_PER_ROUND_DEFAULT = 3

# 连发：一次主动开口最多拆成几条消息（1-3）
MAX_BURST_PARTS_MIN = 1
MAX_BURST_PARTS_MAX = 3
MAX_BURST_PARTS_DEFAULT = 3

# ─── 群聊主动（v1.11.0）：心流主动回复 / 发言参考库 / 冷场破冰 ───
# 群聊与私聊两套逻辑各走各的开关：private_only 只管私聊侧的主动，
# 下面这些管群聊侧。默认在 bot 所在的所有群生效（不需要白名单）。

# 心流：bot 在群里说过话后，开一个「关注窗口」，窗口内群消息若值得接就无需被@接一句。
# 保守档——只有 bot 自己刚发过言才会开窗，不主动盯着整个群。
FLOW_WINDOW_MIN = 1
FLOW_WINDOW_MAX = 60
FLOW_WINDOW_DEFAULT = 8            # 分钟

# 两次心流插话之间的最短间隔（秒）：别人一句我一句地刷屏不像人
FLOW_MIN_GAP_MIN = 10
FLOW_MIN_GAP_MAX = 600
FLOW_MIN_GAP_DEFAULT = 45

# 一个关注窗口内最多主动接几条
FLOW_MAX_REPLIES_MIN = 1
FLOW_MAX_REPLIES_MAX = 10
FLOW_MAX_REPLIES_DEFAULT = 3

# 每群每小时心流插话总上限（防刷屏总闸）
FLOW_HOURLY_CAP_MIN = 1
FLOW_HOURLY_CAP_MAX = 60
FLOW_HOURLY_CAP_DEFAULT = 6

# 连续插话这么多条都没人接，就退出心流安静下来（别尬聊）
FLOW_IGNORED_EXIT_MIN = 1
FLOW_IGNORED_EXIT_MAX = 10
FLOW_IGNORED_EXIT_DEFAULT = 2

# 冷场破冰：群安静超过这么久（小时）才考虑主动抛个轻话题
GROUP_IDLE_HOURS_MIN = 1.0
GROUP_IDLE_HOURS_MAX = 240.0
GROUP_IDLE_HOURS_DEFAULT = 6.0

# 每群每天最多破冰几次
ICEBREAK_DAILY_CAP_MIN = 1
ICEBREAK_DAILY_CAP_MAX = 10
ICEBREAK_DAILY_CAP_DEFAULT = 2

# 超过这么多天没在某个群见到任何消息，就当已经不在这个群了（被踢/退群/群解散）：
# 不再对它破冰，并在清理时丢掉它的样本。心流本身只由实时群消息驱动，被踢自然就停。
GROUP_STALE_DAYS_MIN = 1
GROUP_STALE_DAYS_MAX = 90
GROUP_STALE_DAYS_DEFAULT = 3

# 发言参考库：每群保留多少条近期群友发言样本，注入生成时抽几条当风格参考
GROUP_REF_SAMPLE_MIN = 5
GROUP_REF_SAMPLE_MAX = 200
GROUP_REF_SAMPLE_DEFAULT = 30
GROUP_REF_PROMPT_MIN = 0
GROUP_REF_PROMPT_MAX = 30
GROUP_REF_PROMPT_DEFAULT = 8

REPLY_WINDOW_MIN = 1
REPLY_WINDOW_MAX = 48
# 把 6 小时后的回复也算作「回了我的主动消息」会高估回复率，进而错估关系熟细度；
# 真人聊天里「接得上」的窗口以分钟到小时计。
REPLY_WINDOW_DEFAULT = 2

# 是否持久化私聊消息文本（关闭后仍记录条数，但不落盘消息正文，降低隐私留存）
DEFAULT_STORE_MESSAGE_TEXT = True

# 长期不活跃用户的历史对话数据保留天数（0 = 永不清理）
USER_RETENTION_MIN = 0
USER_RETENTION_MAX = 3650
USER_RETENTION_DEFAULT = 30

# 历史上各版本的默认值。AstrBot 更新 schema 只会补缺失项，从不覆盖已有值，
# 所以发新版时老用户确实会继续跑在旧节奏上。但这份表只用来「提醒」，不再用来改写：
# 它无法区分「用户从没动过」与「用户主动就想要这个值」——把 max_message_length 设成 60
# 是完全合理的选择，升级后被强行改成 120 就是插件在背后改用户的设置。
LEGACY_DEFAULTS: Dict[str, Set[Any]] = {
    "user_cooldown_minutes": {180, 720, 120, 90, 45},
    "max_message_length": {200, 60},
    "reply_window_hours": {6},
    "urge_refill_hours": {8, 4, 3, 2},
    "recent_talk_minutes": {45, 30, 25},
    "skip_cooldown_minutes": {40, 25, 15},
    "activity_level": {45, 55, 65},
    "followup_after_minutes": {12, 6},
    "followup_cooldown_minutes": {60, 45},
    "probe_after_minutes": {3},
    "followup_max_minutes": {90},
}

# 提示标记文件：同一版本只提醒一次，不反复刷日志
_BASELINE_FILE = "config_baseline.json"

# ─── 默认值：唯一来源是 _conf_schema.json ──────────────────────
# 面板里显示的、新装用户拿到的都是 schema 里的值；配置类再自带一份必然漂移。
# 实测两份曾差 16 项，作者照着三天仿真数据调好的节奏对新装用户一次都没生效过，
# 而老用户又走迁移落到代码值——于是新老用户跑的是两套参数，谁都说不清当前生效的是什么。
# 现在默认值只有一个出处。MIN/MAX 那些是「范围」不是默认值，仍由本文件的 clamp 常量负责，
# 并已与 schema 的 min/max 逐项对齐。
_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_conf_schema.json"
)
_MISSING = object()


def _load_schema_defaults() -> Dict[str, Any]:
    try:
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: v.get("default")
        for k, v in raw.items()
        if isinstance(v, dict) and "default" in v
    }


_SCHEMA_DEFAULTS: Dict[str, Any] = _load_schema_defaults()


def _d(key: str, hard_fallback: Any = None) -> Any:
    """某配置项的默认值：schema 为准；schema 里缺失或为 null 时才用硬编码兜底。"""
    v = _SCHEMA_DEFAULTS.get(key, _MISSING)
    if v is _MISSING or v is None:
        return hard_fallback
    return v



@dataclass
class SocialConfig:
    """社交插件配置。"""

    # 基础开关
    enabled: bool = True
    mode: str = "auto"

    # 活跃度与护栏冷却（冷却只是防刷屏的下限，真正的节奏由念头攒得多快决定）
    activity_level: int = ACTIVITY_DEFAULT
    # 每个人自己的冷却才是节奏的来源：不同用户互不影响
    user_cooldown_minutes: int = USER_COOLDOWN_DEFAULT

    # 心跳间隔（分钟）：一轮跑完后随机等这么久再看一眼
    heartbeat_min_minutes: int = HEARTBEAT_MIN_DEFAULT
    heartbeat_max_minutes: int = HEARTBEAT_MAX_DEFAULT

    # 念头模型
    urge_refill_hours: int = URGE_REFILL_DEFAULT
    recent_talk_minutes: int = RECENT_TALK_DEFAULT
    skip_cooldown_minutes: int = SKIP_COOLDOWN_DEFAULT
    llm_gate: bool = True
    # v1.13.0 起默认关：本插件就是用来主动社交的，不该拿「对方现在多半没在线」去压
    # 主动开口的时机；深夜由 quiet_start/quiet_end 单独兜底，不靠这个。想恢复
    # 「只在对方常在线的点找 TA」再打开。
    respect_user_rhythm: bool = False
    cue_followup: bool = True

    # 这一场话断了就接一句（追问那件事 / 问一句在不在）。这不等念头攒满
    followup_enabled: bool = True
    followup_after_minutes: int = FOLLOWUP_AFTER_DEFAULT
    probe_after_minutes: int = PROBE_AFTER_DEFAULT
    followup_max_minutes: int = FOLLOWUP_MAX_DEFAULT
    followup_cooldown_minutes: int = FOLLOWUP_COOLDOWN_DEFAULT

    # 隔一阵回访对方提过的那件事（面试、复查、搬家…没说结果）
    loop_enabled: bool = True
    loop_min_hours: float = LOOP_MIN_HOURS_DEFAULT
    loop_max_hours: float = LOOP_MAX_HOURS_DEFAULT
    # 她主动发的话没人回时，过一阵自己冒一句把话收掉，而不是从此静音
    closer_enabled: bool = True
    closer_after_hours: int = CLOSER_AFTER_DEFAULT

    # 是否把 bot 在主链路里自己说的话也记进对话账本（追问判定的地基）
    track_own_replies: bool = True

    # 安静时段与作息画像按她所在城市的小时走（需要 Humanoid Core 提供时区偏移）
    use_core_clock: bool = True

    # 时段设置
    quiet_start: int = QUIET_START_DEFAULT
    quiet_end: int = QUIET_END_DEFAULT

    # 行为限制
    private_only: bool = True
    debug: bool = False

    # 性格与语气：直接取用 AstrBot 内置人格设定，按目标会话的 umo 解析（多角色天然隔离），
    # 插件不再保留一份性格配置
    max_message_length: int = MAX_MSG_LEN_DEFAULT
    topic_memory_count: int = TOPIC_MEMORY_DEFAULT
    # 生成时注入多少条最近对话当上下文（私聊+群聊都用，含 bot 自己的发言）
    context_inject_count: int = CONTEXT_INJECT_DEFAULT

    # 时段加成
    weekend_boost: bool = True

    # 能量阈值
    energy_threshold: int = ENERGY_THRESHOLD_DEFAULT
    social_energy_threshold: int = SOCIAL_ENERGY_THRESHOLD_DEFAULT

    # 自适应行为
    adaptive_reply_rate: bool = True

    # 回复检测窗口
    reply_window_hours: int = REPLY_WINDOW_DEFAULT

    # 手动触发白名单：管理面板里逐条添加的列表；留空则回退到 AstrBot 管理员（全局配置 admins_id）
    allowed_trigger_uids: Any = None

    # 播种：启动时从 AstrBot 会话库（data/data_v4.db 的 conversations 表）读回装插件
    # 之前就聊过的人，不用等插件装好后先聊一句才认识
    history_ingest: bool = True
    # 播种名单：没聊过、也读不到历史的人也能写进来，她攒够念头会主动找上门
    # （逐条 user_id，或 platform:user_id）；攒念头的速度按 SEED_URGE_SCALE 打折
    seed_users: Any = None
    # seed_users 只填 user_id 时，用来拼私聊目标的平台标识（与管理面板的平台名一致）
    seed_platform: str = "aiocqhttp"

    # 是否持久化私聊消息文本（关闭后仅统计、不落盘正文）
    store_message_text: bool = DEFAULT_STORE_MESSAGE_TEXT

    # 连续这么多天没再说过话的用户，丢掉历史对话正文/话题，只留统计与发送目标（0 = 永不清理）
    user_retention_days: int = USER_RETENTION_DEFAULT

    # 连发模式：主动消息可自然拆成多条短句逐条补发（间隔 1-3 分钟）
    allow_burst: bool = True
    # 连发最多拆几条（1-3）
    max_burst_parts: int = MAX_BURST_PARTS_DEFAULT

    # 每轮心跳每个角色最多主动发几条（对不同用户；同一用户仍走自己的冷却）
    max_sends_per_round: int = MAX_SENDS_PER_ROUND_DEFAULT

    # 早晚问候（时间性触发）：不靠念头攒，窗口到了、今天还没问候过就说一句
    greeting_enabled: bool = GREETING_ENABLED_DEFAULT
    greeting_morning_start: int = GREETING_MORNING_START_DEFAULT
    greeting_morning_end: int = GREETING_MORNING_END_DEFAULT
    greeting_night_start: int = GREETING_NIGHT_START_DEFAULT
    greeting_night_end: int = GREETING_NIGHT_END_DEFAULT

    # 群聊主动（v1.11.0）：心流主动回复 / 发言参考库 / 冷场破冰，各自独立开关
    group_flow_enabled: bool = True
    group_icebreak_enabled: bool = True
    group_ref_lib_enabled: bool = True
    # 参考库是否落盘群友发言正文（关掉则只统计、不存正文，参考库会空）
    group_store_message_text: bool = True
    flow_window_minutes: int = FLOW_WINDOW_DEFAULT
    flow_min_gap_seconds: int = FLOW_MIN_GAP_DEFAULT
    flow_max_replies_per_window: int = FLOW_MAX_REPLIES_DEFAULT
    flow_hourly_cap: int = FLOW_HOURLY_CAP_DEFAULT
    flow_ignored_exit: int = FLOW_IGNORED_EXIT_DEFAULT
    group_idle_hours: float = GROUP_IDLE_HOURS_DEFAULT
    icebreak_daily_cap: int = ICEBREAK_DAILY_CAP_DEFAULT
    group_stale_days: int = GROUP_STALE_DAYS_DEFAULT
    group_ref_sample_size: int = GROUP_REF_SAMPLE_DEFAULT
    group_ref_prompt_count: int = GROUP_REF_PROMPT_DEFAULT

    # 输出形状：是否保留 emoji、是否去掉括号动作/旁白
    allow_emoji: bool = False
    strip_roleplay_actions: bool = True

    # 两个管理指令是否仅机器人主人（全局 admins_id）可用
    owner_only_commands: bool = True

    @property
    def mood_scale(self) -> float:
        """整体想说活的程度：活跃度 45 为 1.0，90 约 1.6，10 约 0.4。"""
        return max(0.35, min(1.7, 0.55 + (self.activity_level / 100.0) * 1.25))

    def greeting_windows(self) -> tuple:
        """问候窗口（含跨午夜归一化）：(早安窗口, 晚安窗口)。"""
        morning = (self.greeting_morning_start % 24, self.greeting_morning_end % 24)
        night = (self.greeting_night_start % 24, self.greeting_night_end % 24)
        return morning, night

    def allowed_trigger_uid_set(self) -> set:
        """解析 allowed_trigger_uids 为去重后的集合。

        配置项已从「逗号分隔文本」改为 list，但 AstrBot 更新 Schema 时不会转换旧的同名值，
        所以两种形态都接受，字符串里的全角逗号/分号也能分。
        """
        raw = self.allowed_trigger_uids
        if raw is None:
            return set()
        if isinstance(raw, str):
            items = raw.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
        elif isinstance(raw, (list, tuple, set)):
            items = list(raw)
        else:
            items = [raw]
        out = set()
        for x in items:
            if x is None:  # 面板里新增但未填的行会以 None 进来，不能变成幽灵 UID "None"
                continue
            s = str(x).strip()
            if s:
                out.add(s)
        return out

    @classmethod
    def from_astrbot(cls, c: Any) -> "SocialConfig":
        """从 AstrBot 配置对象创建 SocialConfig 实例。

        Args:
            c: AstrBot 配置对象（支持 .get() 方法）

        Returns:
            SocialConfig 实例
        """

        def g(key: str, default: Any) -> Any:
            """安全获取配置值。"""
            try:
                return c.get(key, default)
            except Exception:
                return default

        # 模式
        mode = str(g("mode", _d("mode",  "auto"))).lower().strip()
        if mode not in VALID_MODES:
            mode = "auto"

        # 跟进窗口必须比「多久算没声」长，否则永远落在空区间里一条也发不出
        followup_after = max(
            FOLLOWUP_AFTER_MIN,
            min(FOLLOWUP_AFTER_MAX, int(g("followup_after_minutes", _d("followup_after_minutes",  FOLLOWUP_AFTER_DEFAULT)))),
        )
        probe_after = max(
            PROBE_AFTER_MIN,
            min(PROBE_AFTER_MAX, int(g("probe_after_minutes", _d("probe_after_minutes",  PROBE_AFTER_DEFAULT)))),
        )
        followup_max = max(
            followup_after + 5,
            max(FOLLOWUP_MAX_MIN, min(FOLLOWUP_MAX_MAX, int(g("followup_max_minutes", _d("followup_max_minutes",  FOLLOWUP_MAX_DEFAULT))))),
        )
        try:
            loop_min = max(LOOP_MIN_HOURS_FLOOR, min(LOOP_MIN_HOURS_CEIL, float(g("loop_min_hours", _d("loop_min_hours",  LOOP_MIN_HOURS_DEFAULT)))))
        except (TypeError, ValueError):
            loop_min = LOOP_MIN_HOURS_DEFAULT
        try:
            loop_max = max(loop_min + 1.0, float(g("loop_max_hours", _d("loop_max_hours",  LOOP_MAX_HOURS_DEFAULT))))
        except (TypeError, ValueError):
            loop_max = max(loop_min + 1.0, LOOP_MAX_HOURS_DEFAULT)

        return cls(
            enabled=bool(g("enabled", _d("enabled",  True))),
            mode=mode,
            activity_level=max(
                ACTIVITY_MIN,
                min(ACTIVITY_MAX, int(g("activity_level", _d("activity_level",  ACTIVITY_DEFAULT)))),
            ),
            user_cooldown_minutes=max(
                USER_COOLDOWN_MIN,
                min(USER_COOLDOWN_MAX, int(g("user_cooldown_minutes", _d("user_cooldown_minutes",  USER_COOLDOWN_DEFAULT)))),
            ),
            quiet_start=max(
                HOUR_MIN,
                min(HOUR_MAX, int(g("quiet_start", _d("quiet_start",  QUIET_START_DEFAULT)))),
            ),
            quiet_end=max(
                HOUR_MIN,
                min(HOUR_MAX, int(g("quiet_end", _d("quiet_end",  QUIET_END_DEFAULT)))),
            ),
            private_only=bool(g("private_only", _d("private_only",  True))),
            debug=bool(g("debug", _d("debug",  False))),
            max_message_length=max(
                MAX_MSG_LEN_MIN,
                min(MAX_MSG_LEN_MAX, int(g("max_message_length", _d("max_message_length",  MAX_MSG_LEN_DEFAULT)))),
            ),
            topic_memory_count=max(
                TOPIC_MEMORY_MIN,
                min(TOPIC_MEMORY_MAX, int(g("topic_memory_count", _d("topic_memory_count",  TOPIC_MEMORY_DEFAULT)))),
            ),
            context_inject_count=max(
                CONTEXT_INJECT_MIN,
                min(CONTEXT_INJECT_MAX, int(g("context_inject_count", _d("context_inject_count",  CONTEXT_INJECT_DEFAULT)))),
            ),
            weekend_boost=bool(g("weekend_boost", _d("weekend_boost",  True))),
            energy_threshold=max(
                ENERGY_THRESHOLD_MIN,
                min(ENERGY_THRESHOLD_MAX, int(g("energy_threshold", _d("energy_threshold",  ENERGY_THRESHOLD_DEFAULT)))),
            ),
            social_energy_threshold=max(
                SOCIAL_ENERGY_THRESHOLD_MIN,
                min(
                    SOCIAL_ENERGY_THRESHOLD_MAX,
                    int(g("social_energy_threshold", _d("social_energy_threshold",  SOCIAL_ENERGY_THRESHOLD_DEFAULT))),
                ),
            ),
            adaptive_reply_rate=bool(g("adaptive_reply_rate", _d("adaptive_reply_rate",  True))),
            reply_window_hours=max(
                REPLY_WINDOW_MIN,
                min(REPLY_WINDOW_MAX, int(g("reply_window_hours", _d("reply_window_hours",  REPLY_WINDOW_DEFAULT)))),
            ),
            allowed_trigger_uids=g("allowed_trigger_uids", _d("allowed_trigger_uids",  None)),
            history_ingest=bool(g("history_ingest", _d("history_ingest",  True))),
            seed_users=g("seed_users", _d("seed_users",  None)),
            seed_platform=str(g("seed_platform", _d("seed_platform",  "aiocqhttp")) or "aiocqhttp").strip() or "aiocqhttp",
            store_message_text=bool(g("store_message_text", _d("store_message_text",  DEFAULT_STORE_MESSAGE_TEXT))),
            user_retention_days=max(
                USER_RETENTION_MIN,
                min(USER_RETENTION_MAX, int(g("user_retention_days", _d("user_retention_days",  USER_RETENTION_DEFAULT)))),
            ),
            allow_burst=bool(g("allow_burst", _d("allow_burst",  True))),
            max_burst_parts=max(
                MAX_BURST_PARTS_MIN,
                min(MAX_BURST_PARTS_MAX, int(g("max_burst_parts", _d("max_burst_parts",  MAX_BURST_PARTS_DEFAULT)))),
            ),
            max_sends_per_round=max(
                MAX_SENDS_PER_ROUND_MIN,
                min(MAX_SENDS_PER_ROUND_MAX, int(g("max_sends_per_round", _d("max_sends_per_round",  MAX_SENDS_PER_ROUND_DEFAULT)))),
            ),
            greeting_enabled=bool(g("greeting_enabled", _d("greeting_enabled",  GREETING_ENABLED_DEFAULT))),
            greeting_morning_start=max(
                HOUR_MIN, min(HOUR_MAX + 1, int(g("greeting_morning_start", _d("greeting_morning_start",  GREETING_MORNING_START_DEFAULT))))
            ),
            greeting_morning_end=max(
                HOUR_MIN + 1, min(HOUR_MAX + 1, int(g("greeting_morning_end", _d("greeting_morning_end",  GREETING_MORNING_END_DEFAULT))))
            ),
            greeting_night_start=max(
                HOUR_MIN, min(HOUR_MAX + 1, int(g("greeting_night_start", _d("greeting_night_start",  GREETING_NIGHT_START_DEFAULT))))
            ),
            greeting_night_end=max(
                HOUR_MIN + 1, min(HOUR_MAX + 1, int(g("greeting_night_end", _d("greeting_night_end",  GREETING_NIGHT_END_DEFAULT))))
            ),
            group_flow_enabled=bool(g("group_flow_enabled", _d("group_flow_enabled",  True))),
            group_icebreak_enabled=bool(g("group_icebreak_enabled", _d("group_icebreak_enabled",  True))),
            group_ref_lib_enabled=bool(g("group_ref_lib_enabled", _d("group_ref_lib_enabled",  True))),
            group_store_message_text=bool(g("group_store_message_text", _d("group_store_message_text",  True))),
            flow_window_minutes=max(
                FLOW_WINDOW_MIN, min(FLOW_WINDOW_MAX, int(g("flow_window_minutes", _d("flow_window_minutes",  FLOW_WINDOW_DEFAULT))))
            ),
            flow_min_gap_seconds=max(
                FLOW_MIN_GAP_MIN, min(FLOW_MIN_GAP_MAX, int(g("flow_min_gap_seconds", _d("flow_min_gap_seconds",  FLOW_MIN_GAP_DEFAULT))))
            ),
            flow_max_replies_per_window=max(
                FLOW_MAX_REPLIES_MIN,
                min(FLOW_MAX_REPLIES_MAX, int(g("flow_max_replies_per_window", _d("flow_max_replies_per_window",  FLOW_MAX_REPLIES_DEFAULT)))),
            ),
            flow_hourly_cap=max(
                FLOW_HOURLY_CAP_MIN, min(FLOW_HOURLY_CAP_MAX, int(g("flow_hourly_cap", _d("flow_hourly_cap",  FLOW_HOURLY_CAP_DEFAULT))))
            ),
            flow_ignored_exit=max(
                FLOW_IGNORED_EXIT_MIN,
                min(FLOW_IGNORED_EXIT_MAX, int(g("flow_ignored_exit", _d("flow_ignored_exit",  FLOW_IGNORED_EXIT_DEFAULT)))),
            ),
            group_idle_hours=max(
                GROUP_IDLE_HOURS_MIN, min(GROUP_IDLE_HOURS_MAX, float(g("group_idle_hours", _d("group_idle_hours",  GROUP_IDLE_HOURS_DEFAULT))))
            ),
            icebreak_daily_cap=max(
                ICEBREAK_DAILY_CAP_MIN,
                min(ICEBREAK_DAILY_CAP_MAX, int(g("icebreak_daily_cap", _d("icebreak_daily_cap",  ICEBREAK_DAILY_CAP_DEFAULT)))),
            ),
            group_stale_days=max(
                GROUP_STALE_DAYS_MIN, min(GROUP_STALE_DAYS_MAX, int(g("group_stale_days", _d("group_stale_days",  GROUP_STALE_DAYS_DEFAULT))))
            ),
            group_ref_sample_size=max(
                GROUP_REF_SAMPLE_MIN,
                min(GROUP_REF_SAMPLE_MAX, int(g("group_ref_sample_size", _d("group_ref_sample_size",  GROUP_REF_SAMPLE_DEFAULT)))),
            ),
            group_ref_prompt_count=max(
                GROUP_REF_PROMPT_MIN,
                min(GROUP_REF_PROMPT_MAX, int(g("group_ref_prompt_count", _d("group_ref_prompt_count",  GROUP_REF_PROMPT_DEFAULT)))),
            ),
            heartbeat_min_minutes=max(
                HEARTBEAT_MIN_MINUTES_MIN,
                min(
                    HEARTBEAT_MINUTES_MAX,
                    int(g("heartbeat_min_minutes", _d("heartbeat_min_minutes",  HEARTBEAT_MIN_DEFAULT))),
                ),
            ),
            heartbeat_max_minutes=max(
                HEARTBEAT_MIN_MINUTES_MIN,
                min(
                    HEARTBEAT_MINUTES_MAX,
                    int(g("heartbeat_max_minutes", _d("heartbeat_max_minutes",  HEARTBEAT_MAX_DEFAULT))),
                ),
            ),
            urge_refill_hours=max(
                URGE_REFILL_MIN,
                min(URGE_REFILL_MAX, int(g("urge_refill_hours", _d("urge_refill_hours",  URGE_REFILL_DEFAULT)))),
            ),
            recent_talk_minutes=max(
                RECENT_TALK_MIN,
                min(RECENT_TALK_MAX, int(g("recent_talk_minutes", _d("recent_talk_minutes",  RECENT_TALK_DEFAULT)))),
            ),
            skip_cooldown_minutes=max(
                SKIP_COOLDOWN_MIN,
                min(SKIP_COOLDOWN_MAX, int(g("skip_cooldown_minutes", _d("skip_cooldown_minutes",  SKIP_COOLDOWN_DEFAULT)))),
            ),
            llm_gate=bool(g("llm_gate", _d("llm_gate",  True))),
            respect_user_rhythm=bool(g("respect_user_rhythm", _d("respect_user_rhythm",  False))),
            cue_followup=bool(g("cue_followup", _d("cue_followup",  True))),
            followup_enabled=bool(g("followup_enabled", _d("followup_enabled",  True))),
            followup_after_minutes=followup_after,
            probe_after_minutes=probe_after,
            followup_max_minutes=followup_max,
            followup_cooldown_minutes=max(
                FOLLOWUP_COOLDOWN_MIN,
                min(FOLLOWUP_COOLDOWN_MAX, int(g("followup_cooldown_minutes", _d("followup_cooldown_minutes",  FOLLOWUP_COOLDOWN_DEFAULT)))),
            ),
            loop_enabled=bool(g("loop_enabled", _d("loop_enabled",  True))),
            loop_min_hours=loop_min,
            loop_max_hours=loop_max,
            closer_enabled=bool(g("closer_enabled", _d("closer_enabled",  True))),
            closer_after_hours=max(
                CLOSER_AFTER_MIN,
                min(CLOSER_AFTER_MAX, int(g("closer_after_hours", _d("closer_after_hours",  CLOSER_AFTER_DEFAULT)))),
            ),
            track_own_replies=bool(g("track_own_replies", _d("track_own_replies",  True))),
            use_core_clock=bool(g("use_core_clock", _d("use_core_clock",  True))),
            allow_emoji=bool(g("allow_emoji", _d("allow_emoji",  False))),
            strip_roleplay_actions=bool(g("strip_roleplay_actions", _d("strip_roleplay_actions",  True))),
            owner_only_commands=bool(g("owner_only_commands", _d("owner_only_commands",  True))),
        )

    def in_quiet_hours(self, h: int) -> bool:
        """判断给定小时是否在安静时段内。

        Args:
            h: 小时（0-23）

        Returns:
            True 表示在安静时段
        """
        if self.quiet_start == self.quiet_end:
            return False
        if self.quiet_start < self.quiet_end:
            return self.quiet_start <= h < self.quiet_end
        else:  # 跨午夜
            return h >= self.quiet_start or h < self.quiet_end


# 旧默认值对应的新默认
_NEW_DEFAULTS: Dict[str, Any] = {
    "user_cooldown_minutes": USER_COOLDOWN_DEFAULT,
    "max_message_length": MAX_MSG_LEN_DEFAULT,
    "reply_window_hours": REPLY_WINDOW_DEFAULT,
    "urge_refill_hours": URGE_REFILL_DEFAULT,
    "recent_talk_minutes": RECENT_TALK_DEFAULT,
    "skip_cooldown_minutes": SKIP_COOLDOWN_DEFAULT,
    "activity_level": ACTIVITY_DEFAULT,
    "followup_after_minutes": FOLLOWUP_AFTER_DEFAULT,
    "followup_cooldown_minutes": FOLLOWUP_COOLDOWN_DEFAULT,
    # LEGACY_DEFAULTS 里有这两个键，_NEW_DEFAULTS 必须同样有，否则迁移循环取 new_value 时 KeyError
    "probe_after_minutes": PROBE_AFTER_DEFAULT,
    "followup_max_minutes": FOLLOWUP_MAX_DEFAULT,
}


def migrate_legacy_defaults(config: Any, baseline_dir: str, version: str) -> List[str]:
    """列出「值仍停在旧版默认上」的配置项，只提示，不改写。

    AstrBot 的 check_config_integrity 只在键缺失时插默认值，已有值一律保留，所以发新版
    时老用户确实会继续跑在旧节奏上。但这份表无法区分「用户从没动过」与「用户主动就
    想要这个值」：把 max_message_length 设成 60、把 activity_level 压到 55 都是合理的
    选择，升级后被强行改掉就是插件在背后改用户的设置。旧实现正是这么干的。

    现在只把差异摆到日志里，要不要跟着新版走由主人自己在面板决定。同一版本只提一次。

    Args:
        config: AstrBot 配置对象（dict 语义）
        baseline_dir: 插件数据目录（放标记文件）
        version: 当前插件版本

    Returns:
        提示描述列表，每条形如 "key（当前 60 → 本版默认 120）"
    """
    hints: List[str] = []
    try:
        marker = os.path.join(baseline_dir, _BASELINE_FILE)
        if os.path.exists(marker):
            with open(marker, encoding="utf-8") as f:
                done = json.load(f)
            if str(done.get("version", "")) == str(version):
                return []

        if config is None or not hasattr(config, "get"):
            return []

        for key, legacy_values in LEGACY_DEFAULTS.items():
            try:
                current = config.get(key)
            except Exception:
                continue
            if current is None or isinstance(current, bool):
                continue
            # 面板里可能存成字符串，比较前归一化成数字
            probe: Any = current
            try:
                probe = int(str(current).strip())
            except (TypeError, ValueError):
                pass
            if probe not in legacy_values and current not in legacy_values:
                continue
            new_value = _d(key, _NEW_DEFAULTS.get(key))
            if probe == new_value:
                continue
            hints.append(f"{key}（当前 {probe} → 本版默认 {new_value}）")

        try:
            os.makedirs(baseline_dir, exist_ok=True)
            with open(marker, "w", encoding="utf-8") as f:
                json.dump(
                    {"version": str(version), "hints": hints}, f, ensure_ascii=False
                )
        except OSError:
            pass
    except Exception:
        return hints
    return hints
