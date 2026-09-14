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
VALID_MODES = {"auto", "humanoid", "standalone"}

# 配置范围常量
ACTIVITY_MIN = 10
ACTIVITY_MAX = 90
ACTIVITY_DEFAULT = 55

# 冷却项现在只是护栏（防刷屏），真正的节奏由 urge 决定，所以默认值可以放得很低
GLOBAL_COOLDOWN_MIN = 5
GLOBAL_COOLDOWN_MAX = 1440
GLOBAL_COOLDOWN_DEFAULT = 20

USER_COOLDOWN_MIN = 10
USER_COOLDOWN_MAX = 2880
USER_COOLDOWN_DEFAULT = 90

# 一个最在意的人多久能攒满一次「想说两句」的念头
URGE_REFILL_MIN = 2
URGE_REFILL_MAX = 96
# 8 小时是对 interest=1（最在意的人）而言的，普通人按比例更慢，实际要攒十几个小时
# 才发得出一条——那与「插件在跑但永远没动静」基本同义。降到 4。
URGE_REFILL_DEFAULT = 3

# 刚聊完多久之内绝不另起一个话题（真人不会话刚说完又发一句无关的）
RECENT_TALK_MIN = 5
RECENT_TALK_MAX = 720
RECENT_TALK_DEFAULT = 25

# 这一场话断了：多久之后接一句。追问那件事（有由头）比光问「在吗」可以更早。
FOLLOWUP_AFTER_MIN = 2
FOLLOWUP_AFTER_MAX = 240
FOLLOWUP_AFTER_DEFAULT = 6
PROBE_AFTER_MIN = 1
PROBE_AFTER_MAX = 120
PROBE_AFTER_DEFAULT = 3
# 超过这么久再问就不像接话了，像隔了半天重新打招呼
FOLLOWUP_MAX_MIN = 15
FOLLOWUP_MAX_MAX = 720
FOLLOWUP_MAX_DEFAULT = 90
# 同一段沉默只接一次；两次跟进之间的最短间隔
FOLLOWUP_COOLDOWN_MIN = 5
FOLLOWUP_COOLDOWN_MAX = 720
FOLLOWUP_COOLDOWN_DEFAULT = 45

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
SKIP_COOLDOWN_DEFAULT = 25

HOUR_MIN = 0
HOUR_MAX = 23
QUIET_START_DEFAULT = 23
QUIET_END_DEFAULT = 7

MAX_MSG_LEN_MIN = 10
MAX_MSG_LEN_MAX = 500
# 200 字是「发一段话」而不是「发条消息」；真人主动开一句大多十几个字。
MAX_MSG_LEN_DEFAULT = 60

TOPIC_MEMORY_MIN = 0
TOPIC_MEMORY_MAX = 20
TOPIC_MEMORY_DEFAULT = 5

ENERGY_THRESHOLD_MIN = 0
ENERGY_THRESHOLD_MAX = 100
ENERGY_THRESHOLD_DEFAULT = 15

SOCIAL_ENERGY_THRESHOLD_MIN = 0
SOCIAL_ENERGY_THRESHOLD_MAX = 100
SOCIAL_ENERGY_THRESHOLD_DEFAULT = 20

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
# 所以调默认值对老用户完全无效 —— 他们永远停在装插件那一版的行为上。
# 首次以本版本运行时，如果某项仍然等于某个旧默认值（用户没自己改过），就提升到现在的新默认。
LEGACY_DEFAULTS: Dict[str, Set[Any]] = {
    "global_cooldown_minutes": {45, 90, 30},
    "user_cooldown_minutes": {180, 720, 120},
    "max_message_length": {200},
    "reply_window_hours": {6},
    "urge_refill_hours": {8, 4},
    "recent_talk_minutes": {45, 30},
    "skip_cooldown_minutes": {40},
    "activity_level": {45},
    "followup_after_minutes": {12},
    "followup_cooldown_minutes": {60},
}

# 迁移标记文件：只跑一次，用户之后主动改回旧值不会被反复覆盖
_BASELINE_FILE = "config_baseline.json"


@dataclass
class SocialConfig:
    """社交插件配置。"""

    # 基础开关
    enabled: bool = True
    mode: str = "auto"

    # 活跃度与护栏冷却（冷却只是防刷屏的下限，真正的节奏由念头攒得多快决定）
    activity_level: int = ACTIVITY_DEFAULT
    global_cooldown_minutes: int = GLOBAL_COOLDOWN_DEFAULT
    user_cooldown_minutes: int = USER_COOLDOWN_DEFAULT

    # 念头模型
    urge_refill_hours: int = URGE_REFILL_DEFAULT
    recent_talk_minutes: int = RECENT_TALK_DEFAULT
    skip_cooldown_minutes: int = SKIP_COOLDOWN_DEFAULT
    llm_gate: bool = True
    respect_user_rhythm: bool = True
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

    # 是否持久化私聊消息文本（关闭后仅统计、不落盘正文）
    store_message_text: bool = DEFAULT_STORE_MESSAGE_TEXT

    # 连续这么多天没再说过话的用户，丢掉历史对话正文/话题，只留统计与发送目标（0 = 永不清理）
    user_retention_days: int = USER_RETENTION_DEFAULT

    # 连发模式：约 30% 的主动消息可拆成两条短句，间隔 1-3 分钟补发第二条
    allow_burst: bool = True

    # 输出形状：是否保留 emoji、是否去掉括号动作/旁白
    allow_emoji: bool = False
    strip_roleplay_actions: bool = True

    # 两个管理指令是否仅机器人主人（全局 admins_id）可用
    owner_only_commands: bool = True

    @property
    def mood_scale(self) -> float:
        """整体想说活的程度：活跃度 45 为 1.0，90 约 1.6，10 约 0.4。"""
        return max(0.35, min(1.7, 0.55 + (self.activity_level / 100.0) * 1.25))

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
        mode = str(g("mode", "auto")).lower().strip()
        if mode not in VALID_MODES:
            mode = "auto"

        # 跟进窗口必须比「多久算没声」长，否则永远落在空区间里一条也发不出
        followup_after = max(
            FOLLOWUP_AFTER_MIN,
            min(FOLLOWUP_AFTER_MAX, int(g("followup_after_minutes", FOLLOWUP_AFTER_DEFAULT))),
        )
        probe_after = max(
            PROBE_AFTER_MIN,
            min(PROBE_AFTER_MAX, int(g("probe_after_minutes", PROBE_AFTER_DEFAULT))),
        )
        followup_max = max(
            followup_after + 5,
            max(FOLLOWUP_MAX_MIN, min(FOLLOWUP_MAX_MAX, int(g("followup_max_minutes", FOLLOWUP_MAX_DEFAULT)))),
        )
        try:
            loop_min = max(LOOP_MIN_HOURS_FLOOR, min(LOOP_MIN_HOURS_CEIL, float(g("loop_min_hours", LOOP_MIN_HOURS_DEFAULT))))
        except (TypeError, ValueError):
            loop_min = LOOP_MIN_HOURS_DEFAULT
        try:
            loop_max = max(loop_min + 1.0, float(g("loop_max_hours", LOOP_MAX_HOURS_DEFAULT)))
        except (TypeError, ValueError):
            loop_max = max(loop_min + 1.0, LOOP_MAX_HOURS_DEFAULT)

        return cls(
            enabled=bool(g("enabled", True)),
            mode=mode,
            activity_level=max(
                ACTIVITY_MIN,
                min(ACTIVITY_MAX, int(g("activity_level", ACTIVITY_DEFAULT))),
            ),
            global_cooldown_minutes=max(
                GLOBAL_COOLDOWN_MIN,
                min(GLOBAL_COOLDOWN_MAX, int(g("global_cooldown_minutes", GLOBAL_COOLDOWN_DEFAULT))),
            ),
            user_cooldown_minutes=max(
                USER_COOLDOWN_MIN,
                min(USER_COOLDOWN_MAX, int(g("user_cooldown_minutes", USER_COOLDOWN_DEFAULT))),
            ),
            quiet_start=max(
                HOUR_MIN,
                min(HOUR_MAX, int(g("quiet_start", QUIET_START_DEFAULT))),
            ),
            quiet_end=max(
                HOUR_MIN,
                min(HOUR_MAX, int(g("quiet_end", QUIET_END_DEFAULT))),
            ),
            private_only=bool(g("private_only", True)),
            debug=bool(g("debug", False)),
            max_message_length=max(
                MAX_MSG_LEN_MIN,
                min(MAX_MSG_LEN_MAX, int(g("max_message_length", MAX_MSG_LEN_DEFAULT))),
            ),
            topic_memory_count=max(
                TOPIC_MEMORY_MIN,
                min(TOPIC_MEMORY_MAX, int(g("topic_memory_count", TOPIC_MEMORY_DEFAULT))),
            ),
            weekend_boost=bool(g("weekend_boost", True)),
            energy_threshold=max(
                ENERGY_THRESHOLD_MIN,
                min(ENERGY_THRESHOLD_MAX, int(g("energy_threshold", ENERGY_THRESHOLD_DEFAULT))),
            ),
            social_energy_threshold=max(
                SOCIAL_ENERGY_THRESHOLD_MIN,
                min(
                    SOCIAL_ENERGY_THRESHOLD_MAX,
                    int(g("social_energy_threshold", SOCIAL_ENERGY_THRESHOLD_DEFAULT)),
                ),
            ),
            adaptive_reply_rate=bool(g("adaptive_reply_rate", True)),
            reply_window_hours=max(
                REPLY_WINDOW_MIN,
                min(REPLY_WINDOW_MAX, int(g("reply_window_hours", REPLY_WINDOW_DEFAULT))),
            ),
            allowed_trigger_uids=g("allowed_trigger_uids", None),
            store_message_text=bool(g("store_message_text", DEFAULT_STORE_MESSAGE_TEXT)),
            user_retention_days=max(
                USER_RETENTION_MIN,
                min(USER_RETENTION_MAX, int(g("user_retention_days", USER_RETENTION_DEFAULT))),
            ),
            allow_burst=bool(g("allow_burst", True)),
            urge_refill_hours=max(
                URGE_REFILL_MIN,
                min(URGE_REFILL_MAX, int(g("urge_refill_hours", URGE_REFILL_DEFAULT))),
            ),
            recent_talk_minutes=max(
                RECENT_TALK_MIN,
                min(RECENT_TALK_MAX, int(g("recent_talk_minutes", RECENT_TALK_DEFAULT))),
            ),
            skip_cooldown_minutes=max(
                SKIP_COOLDOWN_MIN,
                min(SKIP_COOLDOWN_MAX, int(g("skip_cooldown_minutes", SKIP_COOLDOWN_DEFAULT))),
            ),
            llm_gate=bool(g("llm_gate", True)),
            respect_user_rhythm=bool(g("respect_user_rhythm", True)),
            cue_followup=bool(g("cue_followup", True)),
            followup_enabled=bool(g("followup_enabled", True)),
            followup_after_minutes=followup_after,
            probe_after_minutes=probe_after,
            followup_max_minutes=followup_max,
            followup_cooldown_minutes=max(
                FOLLOWUP_COOLDOWN_MIN,
                min(FOLLOWUP_COOLDOWN_MAX, int(g("followup_cooldown_minutes", FOLLOWUP_COOLDOWN_DEFAULT))),
            ),
            loop_enabled=bool(g("loop_enabled", True)),
            loop_min_hours=loop_min,
            loop_max_hours=loop_max,
            closer_enabled=bool(g("closer_enabled", True)),
            closer_after_hours=max(
                CLOSER_AFTER_MIN,
                min(CLOSER_AFTER_MAX, int(g("closer_after_hours", CLOSER_AFTER_DEFAULT))),
            ),
            track_own_replies=bool(g("track_own_replies", True)),
            use_core_clock=bool(g("use_core_clock", True)),
            allow_emoji=bool(g("allow_emoji", False)),
            strip_roleplay_actions=bool(g("strip_roleplay_actions", True)),
            owner_only_commands=bool(g("owner_only_commands", True)),
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
    "global_cooldown_minutes": GLOBAL_COOLDOWN_DEFAULT,
    "user_cooldown_minutes": USER_COOLDOWN_DEFAULT,
    "max_message_length": MAX_MSG_LEN_DEFAULT,
    "reply_window_hours": REPLY_WINDOW_DEFAULT,
    "urge_refill_hours": URGE_REFILL_DEFAULT,
    "recent_talk_minutes": RECENT_TALK_DEFAULT,
    "skip_cooldown_minutes": SKIP_COOLDOWN_DEFAULT,
    "activity_level": ACTIVITY_DEFAULT,
    "followup_after_minutes": FOLLOWUP_AFTER_DEFAULT,
    "followup_cooldown_minutes": FOLLOWUP_COOLDOWN_DEFAULT,
}


def migrate_legacy_defaults(config: Any, baseline_dir: str, version: str) -> List[str]:
    """把「用户从未改过、但值停在旧默认上」的配置项提升到本版本的新默认。

    AstrBot 的 check_config_integrity 只在键缺失时插默认值，已有值一律保留，所以
    插件调默认值对老用户是一点作用的：他们永远停在装插件那一版的行为上。上一版
    把冷却从 45/180 调到 90/720、长度从 200 调到 60，老用户实际收到的仍是 45/180/200，
    表现出来就是「同一个朋友两小时被找一次、每次一长段」。

    只在首次以本版本运行时跑一次：跑完在数据目录留一个标记，用户之后自己改回
    旧值不会再被覆盖。

    Args:
        config: AstrBot 配置对象（dict 语义）
        baseline_dir: 插件数据目录（放标记文件）
        version: 当前插件版本

    Returns:
        变更描述列表，供日志输出
    """
    changes: List[str] = []
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
            new_value = _NEW_DEFAULTS[key]
            if probe == new_value:
                continue
            try:
                config[key] = new_value
                changes.append(f"{key}: {probe} → {new_value}")
            except Exception:
                continue

        try:
            os.makedirs(baseline_dir, exist_ok=True)
            with open(marker, "w", encoding="utf-8") as f:
                json.dump({"version": str(version), "changes": changes}, f, ensure_ascii=False)
        except OSError:
            pass

        if changes and hasattr(config, "save_config"):
            # 写回配置文件，让管理面板里看到的就是实际生效的值
            try:
                config.save_config()
            except Exception:
                pass
    except Exception:
        return changes
    return changes
