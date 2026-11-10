"""社交配置数据类。

v1.19.0：
- 新增「念头」相关配置：攒满一次念头要多久、刚聊过多久内不另起、发送前是否让模型把关
- 冷却项语义降为护栏（节奏由念头决定），值不再靠默认生效：旧配置里存的是旧默认时，
  首次以本版本运行会一次性提升（AstrBot 只补默认值，从不覆盖已有值）
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

# 上架硬规则：logger 必须且只能 `from astrbot.api import logger`，严禁内置 logging 模块。
# v1.28.1 被自动安全审核退回，唯一必改项就是这个——social/config.py 与 social/memory_bridge.py
# 各有一处 `except: import logging` 兜底。本插件只在 AstrBot 内运行，该分支永远走不到，
# 删掉它与 Core v2.27.29 删 generator.py/persona.py 同类兜底是同一个理由。
from astrbot.api import logger

# 安静时段的窗口语义只有一份实现（social/clock.py），配置层不再抄一遍区间比较。
from .clock import (
    QUIET_CLOCK_LOCAL,
    normalize_quiet_clock,
    normalize_timezone,
)

# 非法/越界的配置值只告警一次（按 key 去重），避免每轮心跳、每次构造刷屏
_CONFIG_WARNED: Set[str] = set()


def _warn_config_once(key: str, message: str) -> None:
    """同一个配置项只告警一次。告警本身失败也不能影响配置构造。"""
    if key in _CONFIG_WARNED:
        return
    _CONFIG_WARNED.add(key)
    try:
        logger.warning(f"[autonomous_social] {message}")
    except Exception:
        pass


def _finite_float(value: Any) -> Optional[float]:
    """读到**有限**浮点就返回，否则 None（坏字符串 / nan / ±inf / 大得转不动）。

    为什么必须在这里挡住 inf：`_safe_*` 的兜底是「退回默认值」，而 inf 不是
    「读不出来」——`float('inf')`、`float('1e999')` 都**成功**返回 inf，于是它一路
    走到下游的 `int(...)`：`int(float('inf'))` 抛的是 **OverflowError**，而这类异常
    既不是 TypeError 也不是 ValueError，会直接抛穿 `from_astrbot` →
    `SocialEngine.__init__` → 插件永久加载不了，用户只能在面板外手改 JSON 才救得回来。
    一个手滑填出来的 `inf` 不该有这个代价。
    """
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _safe_int(value: Any, default: Any, key: str = "") -> int:
    """安全取整数配置：非数字时告警一次并退回默认值，**绝不抛异常**。

    AstrBot 只补缺失项、从不覆盖已有值，所以一个被手改成 "abc" 的数字项会一直
    留在配置里。裸 `int()` 抛出会沿 `from_astrbot` → `SocialEngine.__init__` →
    `AutonomousSocial.__init__` 一路抛穿，插件永久无法加载，面板也修不回来
    （只能手改 JSON）。
    """
    out = _finite_float(value)
    if out is not None:
        return int(out)
    _warn_config_once(
        key or "<unknown>",
        f"配置项 {key} 的值 {value!r} 不是有效数字，已按默认值 {default!r} 处理；"
        "请在 AstrBot 面板里改正（插件不会替你改配置）",
    )
    fallback = _finite_float(default)
    return int(fallback) if fallback is not None else 0


def _safe_float(value: Any, default: Any, key: str = "") -> float:
    """安全取浮点配置：非数字时告警一次并退回默认值，**绝不抛异常**。"""
    out = _finite_float(value)
    if out is not None:
        return out
    _warn_config_once(
        key or "<unknown>",
        f"配置项 {key} 的值 {value!r} 不是有效数字，已按默认值 {default!r} 处理；"
        "请在 AstrBot 面板里改正（插件不会替你改配置）",
    )
    fallback = _finite_float(default)
    return fallback if fallback is not None else 0.0


# 手写 JSON / 旧面板把布尔项存成字符串时，「false」这个字符串本身是**真值**——
# `bool("false")` 得到 True。数字项有 _safe_int/_safe_float 防这一类，
# 布尔项以前没有，于是把 enabled 写成 "false" 反而等于打开（审核 P3-07）。
_BOOL_TRUE_TEXT = frozenset({"1", "true", "yes", "on", "y", "t"})
_BOOL_FALSE_TEXT = frozenset({"0", "false", "no", "off", "n", "f"})


def _as_bool(value: Any, default: bool = False, key: str = "") -> bool:
    """安全取布尔配置：字符串 "false"/"0"/"no"/"off" 认成 False，绝不反着来。

    规则：
    - 布尔、数字原样按真值处理（0/0.0 → False，其余数字 → True，与旧行为一致）；
    - 字符串先 strip + 小写，"true/1/yes/on/y/t" → True，"false/0/no/off/n/f" → False；
    - None → default（键缺失时就是 schema 默认值）；
    - 其它认不出的字符串/类型 → 告警一次并退回 default，**绝不抛异常**
      （配置构造抛穿会让插件永久加载不了，见 _safe_int 的同类教训）。

    Args:
        value: 原始配置值
        default: 认不出时的兜底值（通常是 schema 默认值）
        key: 配置项名，仅用于告警文案与去重

    Returns:
        bool
    """
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # 数字原样：0 → False，1 → True。旧行为 `bool(0)` 也是 False，不改变语义。
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return bool(default)
        if text in _BOOL_TRUE_TEXT:
            return True
        if text in _BOOL_FALSE_TEXT:
            return False
        # 数字形态的字符串（"2"、"0.0"）沿用「数字原样」的老语义：
        # 非 0 为真、0 为假，而不是当成看不懂的值退回默认。
        try:
            return bool(float(text))
        except (TypeError, ValueError):
            pass
        _warn_config_once(
            key or "<unknown>",
            f"配置项 {key} 的值 {value!r} 不是有效布尔值（true/false/1/0/yes/no/on/off），"
            f"已按默认值 {bool(default)!r} 处理；请在 AstrBot 面板里改正（插件不会替你改配置）",
        )
        return bool(default)
    _warn_config_once(
        key or "<unknown>",
        f"配置项 {key} 的值 {value!r} 不是有效布尔值，已按默认值 {bool(default)!r} 处理；"
        "请在 AstrBot 面板里改正（插件不会替你改配置）",
    )
    return bool(default)


def _fsync_dir(path: str) -> None:
    """fsync 目录项，保证 os.replace 的结果真的落盘。

    fsync 文件只保证内容落盘，`os.replace` 这个目录项变更没有 fsync 目录就仍在
    页缓存里；断电时可能出现「新数据在盘上但目录项还指向旧 inode」，甚至文件消失。
    Windows 上目录 `os.open(..., O_RDONLY)` 会失败（或不支持），必须容错。
    """
    try:
        dfd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        try:
            os.close(dfd)
        except OSError:
            pass


def _write_json_atomic(path: str, payload: Any) -> None:
    """原子写 JSON：同目录临时文件 + fsync 文件 + os.replace + fsync 父目录。

    `open(path, "w")` 会先截断再写，进程在截断后崩溃就留下 0 字节文件；与
    「读失败即静默」组合成一条自我锁死路径。
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".config_write.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# 有效运行模式
# 只有这两种：auto 会自动探测有没有 Core，standalone 则完全不读它。
# 曾经还有个人为的 "humanoid（强制要求 Core）"，但代码里从来没有对应分支，
# 选它和 auto 行为完全一样——一个假装存在的档位比没有更误导人。
VALID_MODES = {"auto", "standalone"}

# 门面（契约 v2 的同进程只读接口）开关。认不出的值按 auto。
#   auto —— 找得到就用，找不到退回读 state.json（默认，行为与旧版一致）
#   on   —— 只走门面（用来验证门面真的在工作）
#   off  —— 完全不碰门面（逃生门：与 1.27.38 逐字一致）
VALID_FACADE_MODES = {"auto", "on", "off"}

# 主动消息专用模型的 provider ID 上限。留一个上限是为了防手滑：
# 粘一大串进去时，每一轮心跳都要按顺序试到能用为止，那会把主动消息全堵死。
PROACTIVE_PROVIDER_MAX = 8

# 默认的主动消息模型链：**空**——不指定专供，跟随当前会话的模型走。
#
# v2.0.0 修正（审核 P1-2，发布阻断）：旧默认写死作者自己在用的三个 provider ID
# （openai/gpt-6.1-sol 等）。AstrBot 的 provider ID 是用户在面板里起的名字，
# 不是模型名——标准安装不可能出现这三个 ID，于是「装上就全静默」：所有主动消息
# （1:1 / 群心流 / 破冰 / 起话头）每一轮都按「配了专供却取不到 → 本轮不发」处理。
#
# 现在默认空 = 不指定专供，跟着会话默认模型走，装上即能说话；想用强模型请到面板
# 显式选（那本来就该是用户的决定，不该由默认值替他做）。
# 老配置里已经显式填了 ID 的行为完全不变：仍然按顺序试它。
DEFAULT_PROACTIVE_PROVIDERS: tuple = ()


def _provider_id_list(raw: Any) -> list:
    """把 provider ID 列表归一成 list。

    面板里这一项是 ``list``，但有人会粘一段文本（逗号/分号/换行分隔）——
    三种写法都认。非字符串元素（数字、None、字典）一律跳过：provider ID
    只可能是字符串，硬 ``str()`` 会把 ``None`` 变成 ``"None"`` 然后每轮都
    试着去解析一个不存在的 provider。

    去重且保序：同一个 ID 写三遍没有意义，还会让「按顺序回退」的日志难读。

    返回 `list` 而不是 `tuple`：dataclass 默认值是 `[]`，而测试
    `test_every_documented_default_is_the_schema_default` 逐项比相等。
    """
    if raw is None:
        # 键不存在（老配置里本来就没有这一项）→ 空 = 跟随会话默认。
        # 旧版这里兜到作者私有 ID 链，正是 P1-2 的根因：标准安装一个都解析不到，
        # 主动消息全停。缺省必须等于「不指定」，才可能装上就说话。
        return list(DEFAULT_PROACTIVE_PROVIDERS)
    if isinstance(raw, str):
        parts = re.split(r"[,，;；\n\r]+", raw)
    elif isinstance(raw, (list, tuple)):
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            # 列表里混进来的非字符串直接丢，不 str()——见上面
    elif raw:
        # 非 None、非字符串、非列表：类型就不对（数字、字典、布尔）。
        # 那是「没配明白」，不是「用户点名要这几个」——与缺省同口径（空 = 跟随会话）。
        return list(DEFAULT_PROACTIVE_PROVIDERS)
    else:
        # 显式的空值（`[]` / `""` / `0` / `False`）：同样是「不指定专供」，
        # 跟会话默认走。v2.0.0 起「缺省」与「显式清空」行为一致，不再分成两套。
        return []

    out: list = []
    for part in parts:
        text = str(part).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= PROACTIVE_PROVIDER_MAX:
            break
    # 给了列表但一条都没解析出来（`[""]`、`["   "]`、`"  "`）：
    # 那也是「清空」，不是「缺省」。与上面的显式空值同一口径。
    return out

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
# v1.9.x 一度降到下限 2：更像话多的人，心里一有事就想找人说
# （注释原来写「实际为 2」，与代码和 schema 都不符：URGE_REFILL_DEFAULT 实际为 4）
#
# 2.0.8：4 → 1.5。这个名字一直骗人——它进的是**分母**：
#   rate = (0.25 + 1.10*interest) / max(refill_hours, 0.5)
# 而门槛是 2.10 起（不是 1.0）。refill_hours=4、interest 0.65 时 rate 只有 0.24/h，
# 攒到门槛要 8.7~11.3 小时（实测报告 P0-2），与「4 小时攒满」的字面意思差了三倍。
# 1.5 之后同样的 interest 是 0.64/h，约 3.3~4.3 小时够一次——这才是「一晚上想找人
# 说句话」的量级。要更快就继续往下调，别指望改名字。
URGE_REFILL_DEFAULT = 1.5
# 跨用户撞车：同一 bot 近 N 小时发给别人的消息，和这条重合度到这个线就拦。
#
# 2.0.8：0.6 → 0.88、2.0 → 0.5 小时。0.6 太松——同一句「刚忙完个案笔记」换个说法
# 就能过（实测跨用户撞车一次没拦住，报告 P1-3），而拦截总量 57 次里绝大多数是
# 一天里不同时段的正常重复。收紧到 0.88 才拦得住「几乎逐字相同」，窗口收到 0.5
# 小时才只拦「这一小时内给别人发过同样的话」——真正会让人看出「群发」的那个窗口。
CROSS_USER_REPEAT_RATIO_DEFAULT = 0.88
CROSS_USER_REPEAT_HOURS_DEFAULT = 0.5
HOURLY_SENDS_CAP_DEFAULT = 24
HOURLY_SENDS_CAP_MAX = 200
GREETING_MIDDAY_START_DEFAULT = 12
GREETING_MIDDAY_END_DEFAULT = 14
# 中档好感门槛。「念想」这道门的绝对值本来就不可靠（基线设 40 的人聊久了到 80），
# 真正的档位换算在 _miss_floor：高档 ×0.45、中档 ×1.0、低档 ×1.6。
# 55 → 42：反馈「每句话都得有件事、不像想找他聊天」——门槛压在 55 时，
# 大多数真实用户（Core 实测中位 30、均值 37）永远收不到一句没有正事的「想你」。
MISS_AFFECTION_MIN_DEFAULT = 42
# 「念想」（没有正事、就是想找你）每人每天的条数上限。
# 1 → 2：反馈是「每一次发言都有事情，感觉不像想跟他聊天」——一天一条且门槛高，
# 「就是想你了」几乎从来没发生过；两句日常聊天里的黏糊话该是正常频率。
# 2 → 3（v1.28.6）：用户反馈「私聊发送没看到几个」。好感高的人本该是最容易
# 被主动联系的那批，而这条专程为他们开的通道一天只给两句。
MISS_DAILY_CAP_DEFAULT = 3
# 两条主动消息的最小间隔（分钟）默认值与范围；
MIN_GAP_MINUTES_DEFAULT = 30
MIN_GAP_MAX = 720
# v2.2.6：所有主动开口共用的**硬**最小间隔（分钟）。`min_gap_minutes` 会被
# 「到点由头 / 很想说」整条豁免（实测同一人 7 秒内收到两条），这一条不豁免。
PROACTIVE_MIN_GAP_DEFAULT = 10
PROACTIVE_MIN_GAP_MAX = 1440
# 档位间隔按 min_gap 等比缩放后的硬上限（分钟）。LOW 档本身就是 720，
# 缩放后很容易顶到上限；以前写死 1440（一天），LOW 几乎永远被它卡死。
MAX_GAP_MINUTES_MIN = 60
MAX_GAP_MINUTES_MAX = 10080
MAX_GAP_MINUTES_DEFAULT = 1440

# 间隔抖动（v2.1.8，issue #9）：每「一轮」按 (uid, 档位, 上次发送时刻) 稳定抽一个
# [low, high] 的系数乘在间隔上。默认 0.75~1.75 是 issue 作者实测区间：下限不至于
# 连发（基准 60 分钟时最短 45），上限不至于大半天没动静（最长 105）。
# 两个值任一 <= 0、或 low >= high 就关闭，回到确定性间隔。
GAP_JITTER_LOW_DEFAULT = 0.75
GAP_JITTER_HIGH_DEFAULT = 1.75
GAP_JITTER_MIN = 0.0
GAP_JITTER_MAX = 3.0

# 刚聊完多久之内绝不另起一个话题（真人不会话刚说完又发一句无关的）
# 心跳间隔（分钟）。它决定所有分钟级配置的实际精度
HEARTBEAT_MIN_MINUTES_MIN = 1
# 心跳下限的上限（与 schema 的 heartbeat_min_minutes.max=60 对齐）
HEARTBEAT_MINUTES_MAX = 60
# 心跳上限的上限（与 schema 的 heartbeat_max_minutes.max=120 对齐）
HEARTBEAT_MAX_MINUTES_MAX = 120
HEARTBEAT_MIN_DEFAULT = 8
# v1.28.2：15 → 30。原值下一天约 96 轮，而每轮最多 `max_sends_per_round × 2`
# 次 LLM 尝试——上游一限流（429 恢复窗口按小时计），这些请求全部白烧。
# （2.0.8 起倍数是 ATTEMPT_PER_SEND=4，上面那句「×2」按当时的取值理解即可。）
# 30 分钟仍在她「想起来看一眼手机」的尺度内，轮次与请求量直接减半。
HEARTBEAT_MAX_DEFAULT = 30

# 2.0.8：下限 5 → 3，与默认值 3 对齐（默认值不能低于自己的下限）
RECENT_TALK_MIN = 3
RECENT_TALK_MAX = 720
# 2.0.8：18 → 3。这个值是「刚聊过多久之内念头清零、绝不主动开口」。18 分钟意味着
# 对方回一句之后她半小时内都不会再说话，可真人接话的间隔常常是几分钟——结果是
# 「她好像没在听」。3 分钟只挡住「对方还在打字就插一句」这种真正的抢话。
RECENT_TALK_DEFAULT = 3

# 这一场话断了：多久之后接一句。追问那件事（有由头）比光问「在吗」可以更早。
FOLLOWUP_AFTER_MIN = 3
FOLLOWUP_AFTER_MAX = 240
FOLLOWUP_AFTER_DEFAULT = 30
PROBE_AFTER_MIN = 1
PROBE_AFTER_MAX = 120
PROBE_AFTER_DEFAULT = 12
# 超过这么久再问就不像接话了，像隔了半天重新打招呼
FOLLOWUP_MAX_MIN = 15
FOLLOWUP_MAX_MAX = 720
FOLLOWUP_MAX_DEFAULT = 70
# 同一段沉默只接一次；两次跟进之间的最短间隔
FOLLOWUP_COOLDOWN_MIN = 5
FOLLOWUP_COOLDOWN_MAX = 720
FOLLOWUP_COOLDOWN_DEFAULT = 30

# ─── 接话（continue）：她回完、对方就没声了 ──────────────────────
#
# 上面那条 followup 的判据是「**对方**那句还悬着」，而主链路一回复，最后一条永远是
# 她的——于是「她说完之后对方消失」永远落不进任何一条未完话题：`thread_reason` 在
# 「spoken >= seen 且沉默超过 presence_after」处直接 return ""，再往上又被
# followup_max（默认 70 分钟）截一刀。用户原话是「这种情况应当判断要不要接话/问
# 对方干啥去了，而不是被回避逻辑挡住」。所以单开一条：起点比追问晚（她刚回完就
# 追问像催），窗口比追问长（隔几小时问一句「你人呢」才自然）。
CONTINUE_AFTER_MIN = 10
CONTINUE_AFTER_MAX = 720
# v2.1.8：90 → 25。用户原话「私聊时模型在聊完一句不会选择继续发言」——90 分钟是
# 按「隔很久再追一句」定的，可热聊里对方停一下，她要等一个半小时才接，观感就是
# 「聊完一句就消失了」。热聊（15 分钟内来回 3 轮以上）另有更短的 6 分钟覆盖，
# 见 engine.HOT_CONTINUE_AFTER_MINUTES；这里下调的是所有场景的底噪。
CONTINUE_AFTER_DEFAULT = 25
CONTINUE_WINDOW_HOURS_MIN = 1
CONTINUE_WINDOW_HOURS_MAX = 24
CONTINUE_WINDOW_HOURS_DEFAULT = 8

# ─── 真回避（silent giveup）────────────────────────────────────
#
# 对方连续这么多天没开口、而这期间她一直在主动发 → 全面停发（问候也算），等对方
# 先开口才恢复。0 = 关闭。
#
# 为什么需要它：问候走 `expect_reply=False`，不进 pending / `no_reply_streak`，
# 而 `COLD_SILENCE_SENT` 只挡非 greet 类——于是「一句不回的人每天照样收早安」。
# 线上真实记录：某人最后一次开口是 10-18 15:33，之后 10-19/21/22/23/25 各收到一条
# cat=greeting 的主动消息（silent_sent=5、proactive_sent=0、no_reply_streak=0，
# 现有九道闸没有任何一道看得见这件事）。
SILENT_GIVEUP_DAYS_MIN = 0
SILENT_GIVEUP_DAYS_MAX = 30
# 2.0.8：3 → 0（默认关闭）。这条闸是「对方连续 N 天不开口就全面停发，等 TA 先开口」，
# 出发点是好的（上面那段线上记录），但它默认开着，而 3 天沉默在真实用户里太常见——
# 出差、考试、生病、单纯不想聊天。实测报告 P1-1 里它把一整个人的主动消息全停了，
# 而且恢复条件只有「对方先开口」，插件自己没有任何出口。默认改成关闭，需要的人自己开。
SILENT_GIVEUP_DAYS_DEFAULT = 0

# 隔一阵回访那件事：对方提了个没说完结果的事，过几小时问「后来呢」
LOOP_MIN_HOURS_FLOOR = 1.0
LOOP_MIN_HOURS_CEIL = 24.0
LOOP_MIN_HOURS_DEFAULT = 2.5
LOOP_MAX_HOURS_DEFAULT = 14.0
# 回访最长间隔的上限（与 _conf_schema.json 的 max 对齐；以前完全没有 clamp，
# 从旧配置迁移来的异常值会一路进到时间窗计算里）
LOOP_MAX_HOURS_CEIL = 48.0
# 收场那句：她主动发的话没人回，隔多久自己冒一句
CLOSER_AFTER_MIN = 2
CLOSER_AFTER_MAX = 72
CLOSER_AFTER_DEFAULT = 8

# 模型否决（想过但决定不说）之后，多久之内不再就同一个人重新纠结
# 10 → 30（v2.0.5）：10 分钟太短，一次真心的「不想说」几乎立刻被重新提问，等于没有否决。
SKIP_COOLDOWN_MIN = 5
SKIP_COOLDOWN_MAX = 480
SKIP_COOLDOWN_DEFAULT = 30

HOUR_MIN = 0
HOUR_MAX = 23
QUIET_START_DEFAULT = 23
QUIET_END_DEFAULT = 7
# 面板上这两个小时按哪个时钟读：local（默认）= 插件所在机器；core = 她所在城市。
QUIET_CLOCK_DEFAULT = QUIET_CLOCK_LOCAL

# 插件自己的时区（v2.1.1）：面板最上面那一项，默认 UTC+8。
# 填了它，主动消息的所有时间判定（安静时段、问候、每日额度、作息画像）
# 都按这个钟走，不再看 AstrBot 主机时区；auto 才退回「有 Core 用 Core、
# 没有用本机」的老行为。
TIMEZONE_DEFAULT = "UTC+8"

MAX_MSG_LEN_MIN = 10
MAX_MSG_LEN_MAX = 500
# 60：没人用一条消息发小作文，分段发的时候每段更短才对。容器实测一条 110 字
# 4 句的主动消息里，模型自己数了三遍「一二三」当分隔符，读起来是清单不是人话。
# v1.13.0 从 60 上调到 120 是因为「只能发一两句」，但那时的 60 其实是把整条
# 压扁了，不是分段更短——v1.24.1 改回 60，同时把连发的每段上限留着。
MAX_MSG_LEN_DEFAULT = 60

# 精力低于这个值就不主动开口。做得成闸是因为「累」是硬事实：累到坐不住的人
# 不会先去想「我该不该找 TA 说话」。设高了会把「有点倦」也当成累，于是白天
# 大量本该说的话被挡掉。
GATE_ENERGY_FLOOR_DEFAULT = 20

# 【特别关心】自动认定的好感门槛。85 只有被手动塞满好感的号够得到：普通人从
# 中性起点起跑、每轮最多涨 2 分，85 等于「永远不可能自动认定」，自动认定形同关闭。
# 55 与 Core 的中性起点对齐（mood_initial_affection 也是 55）：聊得好就能自己走到。
SPECIAL_CARE_MIN_AFFECTION_DEFAULT = 55

TOPIC_MEMORY_MIN = 0
TOPIC_MEMORY_MAX = 20
TOPIC_MEMORY_DEFAULT = 5

# 生成时注入多少条「最近的对话」当上下文（私聊合并会话库与插件账本，群聊读近期发言）。
# 含 bot 自己发过的话（看得到自己说过什么）。太少接不上上文、显得生硬；
# 太多浪费 token 也容易跑题。
CONTEXT_INJECT_MIN = 0
CONTEXT_INJECT_MAX = 50
CONTEXT_INJECT_DEFAULT = 5

ENERGY_THRESHOLD_MIN = 0
ENERGY_THRESHOLD_MAX = 100
ENERGY_THRESHOLD_DEFAULT = 15

SOCIAL_ENERGY_THRESHOLD_MIN = 0
SOCIAL_ENERGY_THRESHOLD_MAX = 100
SOCIAL_ENERGY_THRESHOLD_DEFAULT = 20

# 播种：没聊过的人（seed_users 名单）攒念头的速度倍率。这是「礼貌倍率」不是频率旋钮：
# 跟一个素未谋面的人开口，间隔本来就比熟人长，不能拿熟人节奏硬套
SEED_URGE_SCALE = 0.5

# 早晚问候（时间性触发）：早安/晚安窗口（小时）。v2.1.1 起按插件时区
# （timezone_offset，默认 UTC+8）的钟走；填 auto 才退回她所在城市/本机时钟
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
# v1.28.2：3 → 2。每轮 LLM 尝试次数 = 这个数 × ATTEMPT_PER_SEND（当时是 2），所以它就是
# 「一轮最多打几次模型」。降到 2 之后一轮最多 4 次尝试（原 6 次）。
# 发送成功率并没有变差：线上 998 轮只发出去 582 条，瓶颈从来不在尝试次数上。
#
# v1.28.6：2 → 3。用户反馈「事件少 每次只有那几个」「私聊发送没看到几个」。
# 那一轮 4 次尝试的账是拿「LLM 挂着」算的，而真实瓶颈是**候选根本进不了发送段**：
# 由头来源扩了（wake/体感/feelings/next）、一个人一轮能说两件事之后，
# 3 条名额 + 每小时 24 条的上限才配得上「她一天发生几次变化就有几次机会」。
# 3 条 × 2 = 6 次尝试，与 v1.28.2 之前同级，不是新量级。
#
# 2.0.8：3 → 8。实测报告 P1-2 那一轮「候选 893 个，尝试 14 次，发出 1 条，尝试配额
# 用完跳过 860 个」——名额和尝试次数都比候选少一个数量级，等于让排序决定一切、
# 后面的人永远轮不到。8 条配合 ATTEMPT_PER_SEND=4 = 32 次尝试：心跳最短 8 分钟、
# 批量并发 3，按每次 10~30 秒算是 110~320 秒，仍在一轮之内；每小时上限
# （HOURLY_SENDS_CAP_DEFAULT=24）仍是最终的天花板，不会被这条冲破。
MAX_SENDS_PER_ROUND_DEFAULT = 8

# ─── 群聊主动（v1.11.0）：心流主动回复 / 发言参考库 / 冷场破冰 ───
# 群聊与私聊两套逻辑各走各的开关：private_only 只管私聊侧的主动，
# 下面这些管群聊侧。默认在 bot 所在的所有群生效（不需要白名单）。
#
# `private_only` **只管「要不要在群里主动说话」这一件事**：群会话会不会被当成
# 「人」导入是另一码事（历史导入只收私聊会话、seed 名单只收私聊类型），
# 与这一项无关。以前两者共用这一个开关，于是把它关掉时群会话会被建成用户条目，
# 而条目的 umo 是群地址 —— 发送路径照着它发，一句私聊口吻的主动消息就进了群。

# 心流：bot 在群里说过话后，开一个「关注窗口」，窗口内群消息若值得接就无需被@接一句。
# 保守档——只有 bot 自己刚发过言才会开窗，不主动盯着整个群。
FLOW_WINDOW_MIN = 1
FLOW_WINDOW_MAX = 60
FLOW_WINDOW_DEFAULT = 8            # 分钟

# 两次心流插话之间的最短间隔（秒）：别人一句我一句地刷屏不像人
FLOW_MIN_GAP_MIN = 10
FLOW_MIN_GAP_MAX = 600
# 两条心流接话之间的最小间隔（秒）。45 → 120：45 秒在群里几乎等于没有间隔，
# 同一个人前一秒接一句、后一分钟再次插一句，群友看到的就是「bot 一直在说话」。
FLOW_MIN_GAP_DEFAULT = 90
# 主链路回复后的冷却期（秒）。拉长的理由：45 秒的 min_gap 配上 8 分钟的窗口，
# 「被@回完→群里有人接话→马上再插一句」几乎必然发生，观感变成她在连续说话。
FLOW_HOLD_MIN = 0
FLOW_HOLD_MAX = 1800
FLOW_HOLD_DEFAULT = 120

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
GROUP_IDLE_HOURS_DEFAULT = 1.0

# 每群每天最多破冰几次
ICEBREAK_DAILY_CAP_MIN = 1
ICEBREAK_DAILY_CAP_MAX = 10
ICEBREAK_DAILY_CAP_DEFAULT = 2

# G1：群内主动起话头——不只在冷场时才开口。她在群里多久没说话之后可以起一个新话头，
# 以及每群每天最多几次。比破冰宽松（不用等群彻底冷掉），但仍旧保守。
GROUP_TOPIC_GAP_HOURS_MIN = 1.0
GROUP_TOPIC_GAP_HOURS_MAX = 72.0
GROUP_TOPIC_GAP_HOURS_DEFAULT = 2.5
GROUP_TOPIC_DAILY_CAP_MIN = 1
GROUP_TOPIC_DAILY_CAP_MAX = 5
GROUP_TOPIC_DAILY_CAP_DEFAULT = 1
# v2.1.0：跨群去重窗口（小时）。她只有一份生活，群有好几个，而 _run_group_topics
# 每轮只挑一个群——于是同一件事会在一小时里挨个群播一遍。这个窗口内的素材和措辞
# 不会在第二个群再说一遍。0 = 关掉（退回 2.0.7 的行为）。
GROUP_TOPIC_DEDUP_HOURS_MIN = 0.0
GROUP_TOPIC_DEDUP_HOURS_MAX = 48.0
GROUP_TOPIC_DEDUP_HOURS_DEFAULT = 6.0

# v2.1.2：每个角色**在所有群里**一天最多主动说几条（破冰 + 起话头 + 心流都算）。
# 每群各自的额度只管单群，59 个群 × 2 就是一天 118 条——线上 11-06 群发 68 条、
# 私聊 13 条，群聊把私聊挤没了，而主人以为「她不太找我」。
GROUP_DAILY_TOTAL_CAP_DEFAULT = 20
GROUP_DAILY_TOTAL_CAP_MAX = 100

# v2.1.2：特别关心的**独立**小时配额。特别关心已经绕过「TA 没开口」「没回就停」，
# 但还得跟所有人一起抢每小时总预算（hourly_sends_cap），于是名单里的人常常排在
# 队尾轮不到。给它一份自己的小时额度：0 = 跟随 hourly_sends_cap。
SPECIAL_CARE_HOURLY_CAP_DEFAULT = 6
SPECIAL_CARE_HOURLY_CAP_MAX = 60
# v2.1.0：一次起话头最多发几段（0 = 不限）。线上有单群一分钟 4 条的记录，碎句连发
# 不是「想到什么分两条说」，是弹幕。超出的段并进最后一段，不再单独发。
GROUP_TOPIC_MAX_SEGMENTS_MIN = 0
GROUP_TOPIC_MAX_SEGMENTS_MAX = 4
GROUP_TOPIC_MAX_SEGMENTS_DEFAULT = 2
# v2.1.0：群聊跨群撞车判定用的相似度阈值。私聊侧的 cross_user_repeat_ratio 在
# 2.1.0 松到了 0.88（「同一个人别再听第二遍」本来就该宽），但群里要抓的是
# 「同一句话换个群又说一遍」——0.88 只认近乎逐字，实测抓不到 11-06 那批
# 「刚睡醒」/「你们今天都干嘛呢」的换群复读。所以群侧取两者更严的那个：
# 用户把私聊阈值调得更严就跟着更严，但不会比 0.6 更宽。
GROUP_CROSS_GROUP_RATIO_MAX = 0.6

# v2.2.7（issue #3）：同一条连发里，两段**说的是同一件事**就合并成一段。
# 判据是字符二元组重合度：0.45 能抓住「刚醒，睡0小时，趴床上赖着」与
# 「（趴在床上把翅膀摊开）才睡0小时就醒了」这种换了说法的重复，又不会把
# 「先说了水壶，又说了窗外」这种两件不同的事误并。
GROUP_SEGMENT_DUP_RATIO = 0.45

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

# v2.1.3：收尾沉默守卫。她在私聊里已经用「晚安」收过尾，用户又只回了句「晚安」，
# 她再补一条同义道别就是死循环（线上复现过一整屏「晚安」）。守卫按「本轮消息是不是
# 纯收尾词 + 窗口内她是否已强收尾」判断，命中就静默。窗口取 1800 秒：道别之后半小时内
# 的「晚安」都还算同一场收尾。
CLOSING_GUARD_WINDOW_MIN = 60
CLOSING_GUARD_WINDOW_MAX = 7200
CLOSING_GUARD_WINDOW_DEFAULT = 1800

# v2.1.3：群硬收口。短时间内被反复唤醒（每轮都是 @ 她 / 引用她 / 叫到名字）说明群里
# 已经在她身上叠加注意力，这时除了点名类理由，问题、兴趣词、冷场破冰、复读、行为猜测
# 一律静音。阈值/窗口/冷却都从已送达轮次的时间戳重算，不落可变冷却行。
GROUP_CLOSEOUT_WINDOW_MIN = 60
GROUP_CLOSEOUT_WINDOW_MAX = 3600
GROUP_CLOSEOUT_WINDOW_DEFAULT = 300
GROUP_CLOSEOUT_THRESHOLD_MIN = 2
GROUP_CLOSEOUT_THRESHOLD_MAX = 20
GROUP_CLOSEOUT_THRESHOLD_DEFAULT = 3
GROUP_CLOSEOUT_COOLDOWN_MIN = 0
GROUP_CLOSEOUT_COOLDOWN_MAX = 86400
GROUP_CLOSEOUT_COOLDOWN_DEFAULT = 900

# v2.1.4：主动消息的三项新机制，各自一个独立开关，默认全开。
#
# 三项都不是「多一个功能」，而是给一条已经出过事故的路径加一道**能关掉的闸**：
# - 规划/写作分离：以前「要不要说」和「怎么说」挤在同一次调用里，模型为了凑一句
#   正文会自己编一件可指认的事；拆成两次之后，写作阶段只能引用证据卡里的事实。
# - 按任务类型给 max_tokens：全库此前**从不传**这个参数，长度只在事后
#   _truncate_at 里切，切在词中间的风险全靠边界逻辑兜着。
# - 时间投影：模型会自己按日期换算星期、算「多久没联系」，算错就冒出
#   「好久没见」这种它其实并不知道的话。
#
# 三项都 fail-open：关掉任一项就退回 2.1.3 的行为，老路径一行代码都不变。
PLANNING_ENABLED_DEFAULT = True
MAX_OUTPUT_TOKENS_ENABLED_DEFAULT = True
TIME_PROJECTION_ENABLED_DEFAULT = True

# v2.1.5：这两项在 engine.py 里早就被 getattr(cfg, ..., False) 读着，但配置层一直
# 没这两个字段——getattr 的兜底是 False，于是「生成后自评一遍再发」和「先查一次
# 记忆再开口」两条路永远是死代码。补进配置层等于给已有的实现接上电源。
# - draft_review_enabled：自评只能收紧不能放宽（改完照样过验收层），风险低，默认开。
# - memory_tools_enabled：每轮多一次模型调用、多一份延迟，默认关，想要的人自己开。
DRAFT_REVIEW_ENABLED_DEFAULT = True
MEMORY_TOOLS_ENABLED_DEFAULT = False

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
    "min_gap_minutes": {60, 45, 30},
    # 2.1.0 把「松闸」那一批默认值一起改了：老用户盘上还是旧值，AstrBot 不会覆盖，
    # 所以只能在这里提醒一句。注意 30 现在是新默认，写在集合里也不会重复提示
    # （下面 probe == new_value 就跳过）。
    "gate_energy_floor": {35},
    "max_sends_per_round": {3, 2},
    "cross_user_repeat_ratio": {0.6},
    "cross_user_repeat_hours": {2.0},
    "silent_giveup_days": {3},
    "special_care_min_affection": {85},
    # v2.2.1：群主动的三个节奏默认值收紧了。原来的 6 小时冷场 / 8 小时间隔 / 120 秒
    # 插话间隔是按「一个人几个群」调的；线上 71 个群轮一圈要六天，群侧永远轮不到。
    "group_idle_hours": {6.0},
    "group_topic_gap_hours": {8.0},
    "flow_min_gap_seconds": {120},
    "max_message_length": {200, 60},
    "reply_window_hours": {6},
    "urge_refill_hours": {8, 4, 3, 2},
    "recent_talk_minutes": {45, 30, 25, 18},
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
# 并与 schema 的 min/max 对齐（v2.24.0 前有四处对不上：心跳上限 120 vs 60、追问下限 2 vs 3、
# 问候开始小时放到 24 vs 23、回访上限完全没夹）。
_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_conf_schema.json"
)
_MISSING = object()


def _load_schema_raw() -> Dict[str, Any]:
    """读取 _conf_schema.json 原文；失败时**记一条告警**（原来完全静默返回 {}）。"""
    try:
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        _warn_config_once(
            "<schema>",
            f"读取配置 schema 失败（{_SCHEMA_PATH}）：{e}；默认值退回代码里的硬编码兜底",
        )
        return {}
    if not isinstance(raw, dict):
        _warn_config_once(
            "<schema>",
            f"配置 schema 顶层不是对象（{_SCHEMA_PATH}）；默认值退回代码里的硬编码兜底",
        )
        return {}
    return raw


_SCHEMA_RAW: Dict[str, Any] = _load_schema_raw()


def _load_schema_defaults() -> Dict[str, Any]:
    return {
        k: v.get("default")
        for k, v in _SCHEMA_RAW.items()
        if isinstance(v, dict) and "default" in v
    }


_SCHEMA_DEFAULTS: Dict[str, Any] = _load_schema_defaults()

# schema 的 min/max 与代码里的 clamp 常量逐项一致（审查报告附录 A：54 组 100% 一致），
# 所以「取值越界」等价于「clamp 会静默改写」。在这里统一判断并告警，
# 不必在 50 多处 max/min 上各加一份（那样改动面大得多，还容易漏）。
_SCHEMA_BOUNDS: Dict[str, Tuple[Any, Any]] = {}
for _key, _meta in _SCHEMA_RAW.items():
    if not isinstance(_meta, dict):
        continue
    _lo, _hi = _meta.get("min"), _meta.get("max")
    if isinstance(_lo, (int, float)) or isinstance(_hi, (int, float)):
        _SCHEMA_BOUNDS[_key] = (_lo, _hi)
del _key, _meta


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
    # 两条主动消息的最小间隔。与 user_cooldown_minutes 的区别：那个是「另起话题」
    # 的护栏，问候/追问/回访/收场全都绕过了它，于是早安 08:00、追问 08:12 这种
    # 扎堆从来没被挡过。这个覆盖全部五条路径。
    min_gap_minutes: int = MIN_GAP_MINUTES_DEFAULT
    # 档位间隔缩放后的硬上限（分钟）。LOW 档 720 分钟起步，缩放后需要能超过 1440。
    max_gap_minutes: int = MAX_GAP_MINUTES_DEFAULT
    # v2.2.6：不管由头多硬、多想说，两条主动主发之间至少隔这么久。
    proactive_min_gap_minutes: int = PROACTIVE_MIN_GAP_DEFAULT
    # v2.1.8：间隔抖动系数（issue #9）。每个「这一轮」按 (uid, 档位, 上次发送时刻)
    # 稳定抽一个 [low, high] 的系数乘上去；发下一条时重新抽。两个值填 0 就是关掉。
    gap_jitter_low: float = GAP_JITTER_LOW_DEFAULT
    gap_jitter_high: float = GAP_JITTER_HIGH_DEFAULT
    # 「念想」通道：没有正事、只是想 TA 说一句。这一类**不受「刚聊过」阻断**——
    # 「刚聊完还发一条」是打断，「过了一阵子想你了」不是。
    miss_affection_min: int = MISS_AFFECTION_MIN_DEFAULT
    miss_daily_cap: int = MISS_DAILY_CAP_DEFAULT
    # 每小时最多主动联系几人（按角色算）。安全闸，不是节奏——见 schema 的说明。
    hourly_sends_cap: int = HOURLY_SENDS_CAP_DEFAULT
    cross_user_repeat_ratio: float = CROSS_USER_REPEAT_RATIO_DEFAULT
    cross_user_repeat_hours: float = CROSS_USER_REPEAT_HOURS_DEFAULT
    cross_user_calibrate: bool = False

    # 心跳间隔（分钟）：一轮跑完后随机等这么久再看一眼
    heartbeat_min_minutes: int = HEARTBEAT_MIN_DEFAULT
    heartbeat_max_minutes: int = HEARTBEAT_MAX_DEFAULT

    # 念头模型
    # 2.0.8 起是 float：默认 1.5，整数类型会把 1.5 截成 1（见 gi/_safe_int）
    urge_refill_hours: float = URGE_REFILL_DEFAULT
    recent_talk_minutes: int = RECENT_TALK_DEFAULT
    skip_cooldown_minutes: int = SKIP_COOLDOWN_DEFAULT
    gate_energy_floor: int = GATE_ENERGY_FLOOR_DEFAULT
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

    # 接话：她回完、对方就没声了 → 隔一阵接一句/问一句人去哪了。
    # 关掉（或窗口算成 0）= 回旧行为：这条路永远不成立，她说完之后对方消失就是消失。
    continue_enabled: bool = True
    continue_after_minutes: int = CONTINUE_AFTER_DEFAULT
    continue_window_hours: int = CONTINUE_WINDOW_HOURS_DEFAULT

    # 对方连续这么多天没开口就全面停发主动消息（问候也算），等 TA 先开口才恢复。
    # 0 = 关闭（回旧行为：只剩 COLD_SILENCE_SENT 挡非 greet 类）。
    silent_giveup_days: int = SILENT_GIVEUP_DAYS_DEFAULT

    # 隔一阵回访对方提过的那件事（面试、复查、搬家…没说结果）
    loop_enabled: bool = True
    loop_min_hours: float = LOOP_MIN_HOURS_DEFAULT
    loop_max_hours: float = LOOP_MAX_HOURS_DEFAULT
    # 她主动发的话没人回时，过一阵自己冒一句把话收掉，而不是从此静音
    closer_enabled: bool = True
    closer_after_hours: int = CLOSER_AFTER_DEFAULT

    # 是否把 bot 在主链路里自己说的话也记进对话账本（追问判定的地基）
    track_own_replies: bool = True

    # v2.2.7：普通对话回复发出前的确定性清理（占位符/物种用词/连着的说教句）。
    # 默认开——线上被投诉的那几句全部来自普通回复，而那条链本来没有任何闸。
    decorate_outgoing: bool = True

    # 主动发出的消息要不要追加进 AstrBot 的会话历史（WebUI 可见、别的插件读得到）
    persist_proactive_history: bool = True

    # 主动消息的时区（v2.1.1）：插件自己的钟，默认 UTC+8，与宿主时区无关。
    #
    # 填 UTC±N（也认「+8」「8」「UTC+5:30」）就固定按它判；填 auto 才是旧行为：
    # 有 Core 用 Core 给的偏移，没有 Core 就用本机时间。
    # 之前没有这一项，所有时间判定最终都落到「本机时间」——宿主在 UTC 容器里、
    # 或者主人自己不在 UTC+8 时，安静时段/问候/每日额度会集体错几个小时。
    timezone_offset: str = TIMEZONE_DEFAULT

    # 安静时段与作息画像按她所在城市的小时走（需要 Humanoid Core 提供时区偏移）。
    # timezone_offset 不是 auto 时，这一项不再参与计时（Core 只提供作息夜窗）。
    use_core_clock: bool = True

    # 要不要走 Humanoid Core 的只读门面（同进程直连，契约 v2）。
    #   auto（默认）——找得到就用，找不到静默退回读 state.json，行为与旧版一致；
    #   on        ——只走门面，找不到就不读 Core（用来验证门面确实在工作）；
    #   off       ——完全不碰门面，与 1.27.38 逐字一致（出问题时的逃生门）。
    use_core_facade: str = "auto"

    # ── 主动消息专用模型（v1.28.5；v2.0.0 改默认）─────────────────
    #
    # 以前这个插件的每一次模型调用都跟着**当前会话的** provider 走
    # （`get_using_provider(umo)`）。这里给一个 provider ID 列表，本插件所有模型
    # 调用（1:1 主动、群聊心流、破冰、起话头）都按顺序试，第一个能用的即止。
    #
    # **默认空 = 不指定专供，跟着当前会话的模型走**（v2.0.0，审核 P1-2）：
    # 旧默认写死了作者自己在用的三个 provider ID，而 AstrBot 的 provider ID 是
    # 用户在面板里起的名字、不是模型名——标准安装一个都解析不到，主动消息全停。
    # 想用强模型请在面板显式选；老配置里显式填了 ID 的照旧生效。
    #
    # **配了却取不到时**：
    #   - strict_proactive_provider=False（默认）：回退跟随会话默认模型，并打一次性
    #     warning。不这么做的话，一旦面板选的 ID 在当前环境不存在，表现就是
    #     「插件装了、状态正常、就是永远不说话」。
    #   - strict_proactive_provider=True：本轮不发、等下一轮。弱模型写出来的主动
    #     消息比不发更糟，这个取舍留给坚持不降级的用户。
    #
    # 默认值必须是 `list` 而不是 `tuple`：schema 里写的是 `[]`，而测试
    # `test_every_documented_default_is_the_schema_default` 逐项比
    # `getattr(cfg, key) != spec["default"]`——两者必须字面相等。
    proactive_provider_ids: list = field(default_factory=list)
    # 「配了专供但取不到」时：False（默认）回退会话默认 + 一次性 warning；
    # True 坚持本轮不发（v1.28.5 的旧行为）。
    strict_proactive_provider: bool = False

    # 时段设置
    quiet_start: int = QUIET_START_DEFAULT
    quiet_end: int = QUIET_END_DEFAULT
    # quiet_start/quiet_end 按哪个时钟读（v2.0.6 新增）。
    #
    # local（默认）—— 插件所在机器的墙上时间。面板上那个 23→7 是操作者按自己的
    #   作息写的，就该按本机判；
    # core —— 她所在城市（Core 契约的 utc_offset_minutes）。
    #
    # v2.0.5 之前没有这个选项，实际行为固定是 core：她设定在 UTC+2、宿主在 UTC+8
    # 时，本机凌晨 4 点在她那边是 22 点，「现在是安静时段」不成立，凌晨 4:06 那条
    # continue_topic 就是这么发出去的（issue #3）。
    quiet_clock: str = QUIET_CLOCK_DEFAULT
    # Core 的生物钟夜（night_window）要不要也当安静时段。
    #
    # v2.0.5 的写法是「有 Core 的夜就**顶替**面板窗口」（见旧 _quiet_now）：面板写
    # 23→7、Core 给出 00:15–05:30，那 23:00–00:15 与 05:30–07:00 就又成了可发时段；
    # Core 给出一个午睡（16:45–18:15）时 23–7 干脆不存在。现在两条来源取并集，
    # 这条开关只决定 Core 那条参不参与。
    quiet_use_core_night: bool = True

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
    # 长期不回就永久删除这个会话（0 = 关）。私聊与群都适用。
    purge_inactive_user_days: int = 0

    weekend_boost: bool = True

    # 能量阈值
    energy_threshold: int = ENERGY_THRESHOLD_DEFAULT
    social_energy_threshold: int = SOCIAL_ENERGY_THRESHOLD_DEFAULT

    # 自适应行为
    adaptive_reply_rate: bool = True

    # 回复检测窗口
    reply_window_hours: int = REPLY_WINDOW_DEFAULT

    # 记忆联动：装了 astrbot_plugin_memory_companion 时，主动消息生成前拉这个人的
    # 长期记忆揉进提示词，发出后把这条写回记忆库。没装就自动跳过（照常发，只是没记忆）。
    memory_bridge_enabled: bool = True

    # 「想要了」通道（v1.27.13）：只对**真的做过色色**、且此刻欲望涨上来的人主动问一句。
    # 做过色色是硬门槛（填 ID 不够、好感高也不够）；欲望涨上来是当下状态（有过亲密史也不免检）。
    intimacy_affection_min: int = 70
    intimacy_libido_rise: float = 6.0
    intimacy_cooldown_hours: int = 48

    # 手动触发白名单：管理面板里逐条添加的列表；留空则回退到 AstrBot 管理员（全局配置 admins_id）
    # 默认 []（不是 None）：schema 的 default 也是 []，否则 AstrBot 每次加载都判定
    # 「配置有变更」并回写 config（审核 P3-11）。
    allowed_trigger_uids: Any = field(default_factory=list)

    # 关系档手动覆盖：逐条 "uid:high/mid/low"。设了的人不再按好感涨幅推算档位，
    # 直接按指定的档走（最小间隔、由头池大小、念想门槛都以它为准）。
    tier_override: Any = field(default_factory=list)

    # ── 特别关心（v1.28.17）─────────────────────────────────────────
    # 名单里的人：不受关系档位的最小间隔限制、不被「她在睡 / 安静时段 / 太累」挡，
    # 对方不回也允许再问一句。自动认定读 Core 的关系数据（好感 + 对话轮数），
    # 认定结果落在用户状态（`special_care` / `special_care_source`），不是每次重算。
    # 手动名单优先于自动；仍受「单独的最小间隔 + 日上限」约束，防刷屏。
    special_care_enabled: bool = True
    # 逐条 "uid"，或 "平台:uid"（平台就是 umo 的第一段，如 aiocqhttp / 娜莉灵）
    special_care_uids: Any = field(default_factory=list)
    special_care_auto: bool = True
    special_care_min_affection: int = SPECIAL_CARE_MIN_AFFECTION_DEFAULT
    special_care_min_turns: int = 100
    # 自动认定次要依据：Core 老版本没有对话轮数时，退回社交层自己数的消息条数
    special_care_min_messages: int = 60
    special_care_gap_minutes: int = 20
    special_care_daily_cap: int = 8
    # v2.1.2：特别关心的小时配额（独立于 hourly_sends_cap）。0 = 跟随总预算。
    special_care_hourly_cap: int = SPECIAL_CARE_HOURLY_CAP_DEFAULT
    # 静音/睡眠豁免：关掉后「特别关心」只改间隔与追问，不碰安静时段
    special_care_ignore_quiet: bool = False

    # 播种：启动时从 AstrBot 会话库（data/data_v4.db 的 conversations 表）读回装插件
    # 之前就聊过的人，不用等插件装好后先聊一句才认识
    history_ingest: bool = True
    # 播种名单：没聊过、也读不到历史的人也能写进来，她攒够念头会主动找上门
    # （逐条 user_id，或 platform:user_id）；攒念头的速度按 SEED_URGE_SCALE 打折
    seed_users: Any = field(default_factory=list)
    # seed_users 只填 user_id 时，用来拼私聊目标的平台标识（与管理面板的平台名一致）
    seed_platform: str = "aiocqhttp"

    # 是否持久化私聊消息文本（关闭后仅统计、不落盘正文）
    store_message_text: bool = DEFAULT_STORE_MESSAGE_TEXT

    # 连续这么多天没再说过话的用户，丢掉历史对话正文/话题，只留统计与发送目标（0 = 永不清理）
    user_retention_days: int = USER_RETENTION_DEFAULT

    # 连发模式：主动消息可拆成多条短句逐条补发。拆法与间隔跟这个 Bot 在 AstrBot
    # 里的「分段回复」设置走（见 pacing.py）；这里只是总开关——关掉后无论框架怎么配
    # 都只发一整条。
    allow_burst: bool = True

    # 输入状态（正在输入）：她写主动消息的那几秒，QQ 会给对方显示她在输入。
    # 只在 NapCat（aiocqhttp）私聊里生效；interval/timeout 与 Core 那边同一套口径。
    input_state_enabled: bool = True
    input_state_interval_seconds: float = 0.5
    input_state_timeout_seconds: float = 120.0

    # 每轮心跳每个角色最多主动发几条（对不同用户；同一用户仍走自己的冷却）
    max_sends_per_round: int = MAX_SENDS_PER_ROUND_DEFAULT
    # 批量调度：一轮里对多个人「逐人生成、并发发送」的上限（1 = 退回旧的串行）。
    # 只影响同一轮内多个人之间的并行度，不改任何人的冷却/配额与内容。
    batch_send_concurrency: int = 3

    # 早晚问候（时间性触发）：不靠念头攒，窗口到了、今天还没问候过就说一句
    greeting_enabled: bool = GREETING_ENABLED_DEFAULT
    greeting_midday_start: int = GREETING_MIDDAY_START_DEFAULT
    greeting_midday_end: int = GREETING_MIDDAY_END_DEFAULT
    greeting_morning_start: int = GREETING_MORNING_START_DEFAULT
    # 对方沉默多久才问候。对方还在正常聊还每天收早安，那是刷屏。
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
    # 主链路回复后的心流冷却（秒）：自己刚说完话，先缓一下再接别人的话。
    # 没开分段的 Bot 只发一整条时，这段冷却就是「别跟自己的上一句连成一串」的全部保障。
    flow_hold_seconds: int = FLOW_HOLD_DEFAULT
    flow_max_replies_per_window: int = FLOW_MAX_REPLIES_DEFAULT
    flow_hourly_cap: int = FLOW_HOURLY_CAP_DEFAULT
    flow_ignored_exit: int = FLOW_IGNORED_EXIT_DEFAULT
    group_idle_hours: float = GROUP_IDLE_HOURS_DEFAULT
    icebreak_daily_cap: int = ICEBREAK_DAILY_CAP_DEFAULT
    # G1：群内主动起话头（不只在冷场时）。她在群里静了这么久之后可以起一个新话头。
    group_topic_enabled: bool = True
    group_topic_gap_hours: float = GROUP_TOPIC_GAP_HOURS_DEFAULT
    group_topic_daily_cap: int = GROUP_TOPIC_DAILY_CAP_DEFAULT
    # v2.1.0：跨群去重窗口（小时）。她只有一份生活、群有好几个，而每轮只挑一个群，
    # 于是同一件事一小时里挨个群播一遍（线上 11-06 16:40 五个群收到同一套）。这个
    # 窗口内用过的素材/措辞不在第二个群再说。0 = 关。
    group_topic_dedup_hours: float = GROUP_TOPIC_DEDUP_HOURS_DEFAULT
    # v2.1.2：每个角色在所有群里一天最多主动说几条（破冰 + 起话头 + 心流都算）。
    # 0 = 不设总上限（只按每群各自的额度）。
    group_daily_total_cap: int = GROUP_DAILY_TOTAL_CAP_DEFAULT
    # v2.1.0：一次起话头最多发几段（0 = 不限）。多出来的段并进最后一段。
    group_topic_max_segments: int = GROUP_TOPIC_MAX_SEGMENTS_DEFAULT
    # G3 → v1.28.0：群主动**默认不点名**。用户原话是「主动消息发群时会叫别人」
    # ——模型只要拿到名字，就会拿别人的名字去提问/点评/招呼，而打招呼本来该对着
    # 整个群说。打开后也只剩一种用法：心流接话时，可以叫**正在接的那一个人**一句；
    # 起话头/破冰永远不点名（见 generator._group_boundary 的 mode 分叉）。
    group_mention_member: bool = False
    # v1.28.0：别人在群里对聊、或者这句话是在叫别人时，心流不再硬拦——用户原话
    # 「不是必须，但是能不能插嘴看模型」。关掉退回旧语义：这两种场合一律不插。
    # 频率/窗口/配额等护栏不受它影响，仍然全部生效。
    group_interject_others: bool = True
    group_stale_days: int = GROUP_STALE_DAYS_DEFAULT
    group_ref_sample_size: int = GROUP_REF_SAMPLE_DEFAULT
    group_ref_prompt_count: int = GROUP_REF_PROMPT_DEFAULT

    # 输出形状。用不用 emoji、要不要括号动作，都不在配置里定：那两个是人格说话
    # 习惯的一部分，该由她自己发出去的消息说话（见 generator._emoji_policy）。
    # 括号动作原来是一个开关，但无差别删除会吃掉正常中文：「他去（上海）出差了」
    # 会变成「他去出差了」，「这个（很重要）的事」变成「这个的事」。

    # 两个管理指令是否仅机器人主人（全局 admins_id）可用
    owner_only_commands: bool = True
    # 多台 bot 同群时，群内「触发社交」默认要求消息 @ 了本 bot——哪台被 @ 就只触发哪台。
    # 关掉则退回到旧行为：群里一发指令，每台 bot 都会各自触发（只适合单台部署）。
    trigger_group_requires_at: bool = True

    # v2.1.3：收尾沉默守卫（见 social/closing.py）。关掉退回旧行为：用户道晚安，她照答。
    closing_guard_enabled: bool = True
    closing_guard_window_seconds: int = CLOSING_GUARD_WINDOW_DEFAULT
    # 每轮给模型的短提示（「上一轮你已经道过晚安」）。关掉只留静默判定。
    closing_guard_prompt_enabled: bool = True

    # v2.1.3：群硬收口（见 social/throttle.py）。被反复唤醒时只放行点名类理由。
    group_closeout_enabled: bool = True
    group_closeout_window_seconds: int = GROUP_CLOSEOUT_WINDOW_DEFAULT
    group_closeout_threshold: int = GROUP_CLOSEOUT_THRESHOLD_DEFAULT
    group_closeout_cooldown_seconds: int = GROUP_CLOSEOUT_COOLDOWN_DEFAULT

    # v2.1.4：主动消息先规划后写作（plan → decide_with_plan，见 social/generator.py）。
    # 关掉退回 2.1.3 的单次 decide：同一段 prompt 里既要判断要不要说、又要写出正文。
    planning_enabled: bool = PLANNING_ENABLED_DEFAULT
    # v2.1.4：按任务类型给 max_tokens（plan / proactive / group / chat / structured）。
    # 关掉则完全不传这个参数，沿用 provider 自己的默认值（与 2.1.3 完全一致）。
    max_output_tokens_enabled: bool = MAX_OUTPUT_TOKENS_ENABLED_DEFAULT
    # v2.1.4：每次生成注入「此刻」投影块（本地时间 / 星期 / 距上次发消息多久），
    # 防她按日期自己换算、说「好久没见」。关掉不注入。
    time_projection_enabled: bool = TIME_PROJECTION_ENABLED_DEFAULT
    # v2.1.5：生成后自评一次（generator.review_draft），不过就换个时机重来。
    # 关掉则生成完直接进验收层，省一次模型调用。
    draft_review_enabled: bool = DRAFT_REVIEW_ENABLED_DEFAULT
    # v2.1.5：允许她先查一次记忆再开口（generator.generate_with_tools）。
    # 会多一次模型调用，所以默认关；生成器不支持工具时自动退回普通生成。
    memory_tools_enabled: bool = MEMORY_TOOLS_ENABLED_DEFAULT

    @property
    def mood_scale(self) -> float:
        """整体想说活的程度。公式 0.55 + 活跃度/100×1.25，夹在 0.35~1.7。

        实际取值：10→0.68、45→1.11、75（默认）→1.49、90→1.68。
        （旧注释写的「45 为 1.0、90 约 1.6、10 约 0.4」三处都对不上，害得调参的人
        按错的数去反推该填多少。）
        """
        return max(0.35, min(1.7, 0.55 + (self.activity_level / 100.0) * 1.25))

    def greeting_windows(self) -> tuple:
        """问候窗口（含跨午夜归一化）：(早安, 午间, 晚安)。

        午间是早安窗口错过时的兜底。开始与结束相同就等于关掉。
        """
        morning = (self.greeting_morning_start % 24, self.greeting_morning_end % 24)
        midday = None
        if self.greeting_midday_start % 24 != self.greeting_midday_end % 24:
            midday = (self.greeting_midday_start % 24, self.greeting_midday_end % 24)
        night = (self.greeting_night_start % 24, self.greeting_night_end % 24)
        return morning, midday, night

    def tier_override_for(self, uid: Any) -> str:
        """这个人的关系档手动覆盖值（"high"/"mid"/"low"），没设返回空。

        接受 "uid:high"、"high:uid" 两种写法：UID 纯数字，档位就是那三个词，
        哪边是 UID 一看便知，不用要求固定顺序。
        """
        target = str(uid or "").strip()
        if not target:
            return ""
        raw = self.tier_override
        if isinstance(raw, str):
            items = raw.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
        elif isinstance(raw, (list, tuple, set)):
            # 列表元素本身也可能是「a:high,b:low」这种带分隔符的写法（面板列表里手填），
            # 一样按逗号/分号再拆一次，不然整个元素会被当成一项丢掉。
            items = []
            for item in raw:
                if isinstance(item, str):
                    items.extend(
                        item.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
                    )
                else:
                    items.append(item)
        else:
            items = [raw] if raw is not None else []
        valid = {"high", "mid", "low"}
        for item in items:
            if item is None:
                continue
            parts = [p.strip() for p in str(item).replace("：", ":").split(":")]
            if len(parts) != 2:
                continue
            left, right = parts
            level = left.lower() if left.lower() in valid else (right.lower() if right.lower() in valid else "")
            who = right if left.lower() in valid else left
            if level and who and who == target:
                return level
        return ""

    def special_care_parsed_entries(self) -> List[Tuple[str, str, str]]:
        """特别关心名单解析成 (平台, uid, 专属称谓)。

        支持四种写法（中英文冒号都认，逗号/分号/中文逗号分隔，和面板上其它名单一个写法）：

        * ``uid``            —— 通用，任何平台都认；
        * ``平台:uid``       —— 只认这个平台（会话地址的第一段，如 aiocqhttp）；
        * ``uid:称谓``       —— 通用 + 专属称谓；
        * ``平台:uid:称谓``  —— 平台专属 + 专属称谓。

        两段式靠「左边是不是纯数字」区分 ``uid:称谓`` 和 ``平台:uid``：QQ 号这类 uid 是
        纯数字，而平台名不会是纯数字。非数字 uid（微信 / QQ 官方 openid）想配称谓请写
        三段式，否则会被当成 ``平台:uid``。坏条目跳过、不抛——这是热路径上的配置，
        不能因为一行手误把主动消息全停。
        """
        raw = self.special_care_uids
        if isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, (list, tuple, set)):
            items = [str(x) for x in raw if x is not None]
        else:
            items = [str(raw)] if raw is not None else []
        out: List[Tuple[str, str, str]] = []
        for item in items:
            for chunk in (
                item.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
            ):
                chunk = chunk.replace("：", ":").strip()
                if not chunk:
                    continue
                parts = [p.strip() for p in chunk.split(":")]
                if len(parts) == 1:
                    plat, who, call = "", parts[0], ""
                elif len(parts) == 2:
                    left, right = parts
                    if left.isdigit() and right and not right.isdigit():
                        plat, who, call = "", left, right
                    else:
                        plat, who, call = left, right, ""
                else:
                    plat, who, call = parts[0], parts[1], ":".join(parts[2:]).strip()
                if not who:
                    continue
                out.append((plat, who, call))
        return out

    def special_care_entries(self) -> set:
        """特别关心名单归一成集合（"uid" 或 "平台:uid"），称谓不参与判定。

        保持旧签名与旧返回形态：``_special_care_manual`` 等闸门只关心是谁。
        """
        entries = set()
        for plat, uid, _call in self.special_care_parsed_entries():
            entries.add(f"{plat}:{uid}" if plat else uid)
        return entries

    def special_care_call_name(self, uid: Any, platform: Any = "") -> str:
        """这个人在这个平台上的专属称谓；平台专属优先于通用，没配返回 ""。"""
        target = str(uid or "").strip()
        if not target:
            return ""
        plat = str(platform or "").strip().lower()
        generic = ""
        for entry_plat, entry_uid, call in self.special_care_parsed_entries():
            if entry_uid != target or not call:
                continue
            if not entry_plat:
                if not generic:
                    generic = call
                continue
            if plat and entry_plat.lower() == plat:
                return call
        return generic

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

        已经是 `SocialConfig` 就**原样返回**。以前不管传什么都会走一遍 `c.get(...)`,
        而 `SocialConfig` 没有 `.get()`，于是每个字段都取不到、悄悄退回默认值——
        表现就是「面板上改了，运行时不生效」，而且不报错，最难查的那一类。
        """
        if isinstance(c, cls):
            return c

        def g(key: str, default: Any) -> Any:
            """安全获取配置值。"""
            try:
                return c.get(key, default)
            except Exception:
                return default

        def _warn_clamped(key: str, value: Any, lo: Any, hi: Any) -> None:
            _warn_config_once(
                key,
                f"配置项 {key} 的值 {value!r} 超出允许范围（"
                f"{lo if lo is not None else '-∞'} ~ {hi if hi is not None else '+∞'}），"
                "已按边界值处理；请在面板里改正",
            )

        def gi(key: str, hard_default: Any) -> int:
            """安全取整数：非数字→告警+默认值；越界→告警（外层 clamp 仍会夹住）。"""
            default = _d(key, hard_default)
            value = _safe_int(g(key, default), default, key)
            lo, hi = _SCHEMA_BOUNDS.get(key, (None, None))
            if (isinstance(lo, (int, float)) and value < lo) or (
                isinstance(hi, (int, float)) and value > hi
            ):
                _warn_clamped(key, value, lo, hi)
            return value

        def gf(key: str, hard_default: Any) -> float:
            """安全取浮点：非数字→告警+默认值；越界→告警（外层 clamp 仍会夹住）。"""
            default = _d(key, hard_default)
            value = _safe_float(g(key, default), default, key)
            lo, hi = _SCHEMA_BOUNDS.get(key, (None, None))
            if (isinstance(lo, (int, float)) and value < lo) or (
                isinstance(hi, (int, float)) and value > hi
            ):
                _warn_clamped(key, value, lo, hi)
            return value

        def gb(key: str, hard_default: bool) -> bool:
            """安全取布尔：手写 JSON 的 "false"/"0" 归一成 False（审核 P3-07）。

            default 走 _d()：缺键时与 schema 默认一致；认不出的值告警一次后回退它。
            """
            default = _d(key, hard_default)
            return _as_bool(g(key, default), default, key)

        def gl(key: str, hard_default: Any) -> Any:
            """安全取 list：缺键与显式 null 都归一成 []（审核 P3-11）。

            旧配置里这几项可能是 null，也可能是逗号文本/列表——非 None 原样返回，
            由下游的 *_entries / *_set / normalize_* 解析（它们本来就两种形态都认）。
            """
            default = _d(key, hard_default)
            value = g(key, default)
            return [] if value is None else value

        # 模式
        mode = str(g("mode", _d("mode",  "auto"))).lower().strip()
        if mode not in VALID_MODES:
            mode = "auto"

        # 门面开关：认不出的值一律按 auto（最保守——能用就用，用不了退回读文件）
        facade_mode = str(g("use_core_facade", _d("use_core_facade", "auto"))).lower().strip()
        if facade_mode not in VALID_FACADE_MODES:
            facade_mode = "auto"

        # 主动消息专用模型：provider ID 列表。三种写法都认（面板里是 list，
        # 有人会粘一段逗号/分号/换行分隔的文本）。
        # v2.0.0（P1-2）：默认空 = 跟随会话默认模型。传 None 给 _provider_id_list
        # 与显式清空同一个结果（[]），老配置里显式填的 ID 依旧逐条保留。
        proactive_providers = _provider_id_list(g("proactive_provider_ids", None))

        # 跟进窗口必须比「多久算没声」长，否则永远落在空区间里一条也发不出
        followup_after = max(
            FOLLOWUP_AFTER_MIN,
            min(FOLLOWUP_AFTER_MAX, gi("followup_after_minutes", FOLLOWUP_AFTER_DEFAULT)),
        )
        probe_after = max(
            PROBE_AFTER_MIN,
            min(PROBE_AFTER_MAX, gi("probe_after_minutes", PROBE_AFTER_DEFAULT)),
        )
        followup_max = max(
            followup_after + 5,
            max(FOLLOWUP_MAX_MIN, min(FOLLOWUP_MAX_MAX, gi("followup_max_minutes", FOLLOWUP_MAX_DEFAULT))),
        )
        # 接话窗口：起点必须在范围内，窗口必须**严格长于**起点。窗口 = 起点时区间是空的，
        # 接话一次都不会发生，而面板上两个数看着都正常——与 followup_max 必须大于
        # followup_after 同一条教训（那边是 +5 分钟，这边按整点小时抬）。
        continue_after = max(
            CONTINUE_AFTER_MIN,
            min(CONTINUE_AFTER_MAX, gi("continue_after_minutes", CONTINUE_AFTER_DEFAULT)),
        )
        continue_window_hours = max(
            CONTINUE_WINDOW_HOURS_MIN,
            min(
                CONTINUE_WINDOW_HOURS_MAX,
                gi("continue_window_hours", CONTINUE_WINDOW_HOURS_DEFAULT),
            ),
        )
        continue_window_hours = max(continue_window_hours, continue_after // 60 + 1)
        try:
            loop_min = max(LOOP_MIN_HOURS_FLOOR, min(LOOP_MIN_HOURS_CEIL, gf("loop_min_hours", LOOP_MIN_HOURS_DEFAULT)))
        except (TypeError, ValueError):
            loop_min = LOOP_MIN_HOURS_DEFAULT
        try:
            loop_max = min(
                LOOP_MAX_HOURS_CEIL,
                max(loop_min + 1.0, gf("loop_max_hours", LOOP_MAX_HOURS_DEFAULT)),
            )
        except (TypeError, ValueError):
            loop_max = max(loop_min + 1.0, LOOP_MAX_HOURS_DEFAULT)

        _max_gap = gi("max_gap_minutes", MAX_GAP_MINUTES_DEFAULT)
        if _max_gap <= 0:
            _max_gap = MAX_GAP_MINUTES_DEFAULT
        return cls(
            enabled=gb("enabled", True),
            mode=mode,
            activity_level=max(
                ACTIVITY_MIN,
                min(ACTIVITY_MAX, gi("activity_level", ACTIVITY_DEFAULT)),
            ),
            min_gap_minutes=max(
                10, min(MIN_GAP_MAX, gi("min_gap_minutes", MIN_GAP_MINUTES_DEFAULT)),
            ),
            max_gap_minutes=max(
                MAX_GAP_MINUTES_MIN, min(MAX_GAP_MINUTES_MAX, _max_gap)
            ),
            proactive_min_gap_minutes=max(
                1,
                min(
                    PROACTIVE_MIN_GAP_MAX,
                    gi("proactive_min_gap_minutes", PROACTIVE_MIN_GAP_DEFAULT),
                ),
            ),
            # v2.1.8（issue #9）：间隔抖动。0/0 或 low>=high = 关闭。
            gap_jitter_low=max(
                GAP_JITTER_MIN,
                min(GAP_JITTER_MAX, gf("gap_jitter_low", GAP_JITTER_LOW_DEFAULT)),
            ),
            gap_jitter_high=max(
                GAP_JITTER_MIN,
                min(GAP_JITTER_MAX, gf("gap_jitter_high", GAP_JITTER_HIGH_DEFAULT)),
            ),
            miss_affection_min=max(
                0, min(100, gi("miss_affection_min", MISS_AFFECTION_MIN_DEFAULT)),
            ),
            miss_daily_cap=max(
                0, min(10, gi("miss_daily_cap", MISS_DAILY_CAP_DEFAULT)),
            ),
            cross_user_repeat_ratio=max(
                0.1, min(1.0, gf("cross_user_repeat_ratio", CROSS_USER_REPEAT_RATIO_DEFAULT))
            ),
            # 0 = 整道检查关掉（面板上就写着这么用，所以这里得真的认 0，
            # 钳到 0.5 的话「设 0 关闭」就是一句骗人的话）
            cross_user_repeat_hours=max(
                0.0, min(48.0, gf("cross_user_repeat_hours", CROSS_USER_REPEAT_HOURS_DEFAULT))
            ),
            cross_user_calibrate=gb("cross_user_calibrate", False),
            hourly_sends_cap=max(
                0, min(HOURLY_SENDS_CAP_MAX, gi("hourly_sends_cap", HOURLY_SENDS_CAP_DEFAULT)),
            ),
            user_cooldown_minutes=max(
                USER_COOLDOWN_MIN,
                min(USER_COOLDOWN_MAX, gi("user_cooldown_minutes", USER_COOLDOWN_DEFAULT)),
            ),
            quiet_start=max(
                HOUR_MIN,
                min(HOUR_MAX, gi("quiet_start", QUIET_START_DEFAULT)),
            ),
            quiet_end=max(
                HOUR_MIN,
                min(HOUR_MAX, gi("quiet_end", QUIET_END_DEFAULT)),
            ),
            # 认不出的值一律按 local：面板上这两个小时是操作者按自己的作息填的，
            # 退回本机时钟至少是「他写的那个意思」，退回她所在城市则可能是任意偏移。
            quiet_clock=normalize_quiet_clock(
                g("quiet_clock", _d("quiet_clock", QUIET_CLOCK_DEFAULT))
            ),
            # 看不懂的时区回 auto（旧行为），不拿 UTC+0 顶：填错一个字符就让
            # 所有人半夜收到消息，比退回本机时间糟糕得多。
            timezone_offset=normalize_timezone(
                g("timezone_offset", _d("timezone_offset", TIMEZONE_DEFAULT))
            ),
            quiet_use_core_night=gb("quiet_use_core_night", True),
            private_only=gb("private_only", True),
            debug=gb("debug", False),
            input_state_enabled=gb("input_state_enabled", True),
            input_state_interval_seconds=max(
                0.1, min(10.0, gf("input_state_interval_seconds", 0.5) )
            ),
            input_state_timeout_seconds=max(
                10.0, min(600.0, gf("input_state_timeout_seconds", 120.0) )
            ),
            max_message_length=max(
                MAX_MSG_LEN_MIN,
                min(MAX_MSG_LEN_MAX, gi("max_message_length", MAX_MSG_LEN_DEFAULT)),
            ),
            topic_memory_count=max(
                TOPIC_MEMORY_MIN,
                min(TOPIC_MEMORY_MAX, gi("topic_memory_count", TOPIC_MEMORY_DEFAULT)),
            ),
            context_inject_count=max(
                CONTEXT_INJECT_MIN,
                min(CONTEXT_INJECT_MAX, gi("context_inject_count", CONTEXT_INJECT_DEFAULT)),
            ),
            purge_inactive_user_days=max(
                0, min(3650, gi("purge_inactive_user_days", 0))
            ),
            weekend_boost=gb("weekend_boost", True),
            energy_threshold=max(
                ENERGY_THRESHOLD_MIN,
                min(ENERGY_THRESHOLD_MAX, gi("energy_threshold", ENERGY_THRESHOLD_DEFAULT)),
            ),
            social_energy_threshold=max(
                SOCIAL_ENERGY_THRESHOLD_MIN,
                min(
                    SOCIAL_ENERGY_THRESHOLD_MAX,
                    gi("social_energy_threshold", SOCIAL_ENERGY_THRESHOLD_DEFAULT),
                ),
            ),
            adaptive_reply_rate=gb("adaptive_reply_rate", True),
            reply_window_hours=max(
                REPLY_WINDOW_MIN,
                min(REPLY_WINDOW_MAX, gi("reply_window_hours", REPLY_WINDOW_DEFAULT)),
            ),
            memory_bridge_enabled=gb("memory_bridge_enabled", True),
            intimacy_affection_min=max(
                0, min(100, gi("intimacy_affection_min", 70)),
            ),
            intimacy_libido_rise=max(
                0.0, min(50.0, gf("intimacy_libido_rise", 6.0)),
            ),
            intimacy_cooldown_hours=max(
                1, min(720, gi("intimacy_cooldown_hours", 48)),
            ),
            allowed_trigger_uids=gl("allowed_trigger_uids", None),
            tier_override=gl("tier_override", None),
            special_care_enabled=gb("special_care_enabled", True),
            special_care_uids=gl("special_care_uids", None),
            special_care_auto=gb("special_care_auto", True),
            special_care_min_affection=max(
                0, min(100, gi("special_care_min_affection", SPECIAL_CARE_MIN_AFFECTION_DEFAULT))
            ),
            special_care_min_turns=max(0, min(1_000_000, gi("special_care_min_turns", 100))),
            special_care_min_messages=max(0, min(1_000_000, gi("special_care_min_messages", 60))),
            special_care_gap_minutes=max(
                1, min(1440, gi("special_care_gap_minutes", 20))
            ),
            special_care_daily_cap=max(0, min(100, gi("special_care_daily_cap", 8))),
            special_care_hourly_cap=max(
                0,
                min(
                    SPECIAL_CARE_HOURLY_CAP_MAX,
                    gi("special_care_hourly_cap", SPECIAL_CARE_HOURLY_CAP_DEFAULT),
                ),
            ),
            special_care_ignore_quiet=gb("special_care_ignore_quiet", False),
            history_ingest=gb("history_ingest", True),
            seed_users=gl("seed_users", None),
            seed_platform=str(g("seed_platform", _d("seed_platform",  "aiocqhttp")) or "aiocqhttp").strip() or "aiocqhttp",
            store_message_text=gb("store_message_text", DEFAULT_STORE_MESSAGE_TEXT),
            user_retention_days=max(
                USER_RETENTION_MIN,
                min(USER_RETENTION_MAX, gi("user_retention_days", USER_RETENTION_DEFAULT)),
            ),
            allow_burst=gb("allow_burst", True),
            max_sends_per_round=max(
                MAX_SENDS_PER_ROUND_MIN,
                min(MAX_SENDS_PER_ROUND_MAX, gi("max_sends_per_round", MAX_SENDS_PER_ROUND_DEFAULT)),
            ),
            batch_send_concurrency=max(1, min(10, gi("batch_send_concurrency", 3))),
            greeting_enabled=gb("greeting_enabled", GREETING_ENABLED_DEFAULT),
            greeting_morning_start=max(
                HOUR_MIN, min(HOUR_MAX, gi("greeting_morning_start", GREETING_MORNING_START_DEFAULT))
            ),
            greeting_morning_end=max(
                HOUR_MIN + 1, min(HOUR_MAX + 1, gi("greeting_morning_end", GREETING_MORNING_END_DEFAULT))
            ),
            greeting_midday_start=max(
                HOUR_MIN, min(HOUR_MAX, gi("greeting_midday_start", GREETING_MIDDAY_START_DEFAULT))
            ),
            greeting_midday_end=max(
                HOUR_MIN + 1, min(HOUR_MAX + 1, gi("greeting_midday_end", GREETING_MIDDAY_END_DEFAULT))
            ),
            greeting_night_start=max(
                HOUR_MIN, min(HOUR_MAX, gi("greeting_night_start", GREETING_NIGHT_START_DEFAULT))
            ),
            greeting_night_end=max(
                HOUR_MIN + 1, min(HOUR_MAX + 1, gi("greeting_night_end", GREETING_NIGHT_END_DEFAULT))
            ),
            group_flow_enabled=gb("group_flow_enabled", True),
            group_icebreak_enabled=gb("group_icebreak_enabled", True),
            group_ref_lib_enabled=gb("group_ref_lib_enabled", True),
            group_store_message_text=gb("group_store_message_text", True),
            flow_window_minutes=max(
                FLOW_WINDOW_MIN, min(FLOW_WINDOW_MAX, gi("flow_window_minutes", FLOW_WINDOW_DEFAULT))
            ),
            flow_min_gap_seconds=max(
                FLOW_MIN_GAP_MIN, min(FLOW_MIN_GAP_MAX, gi("flow_min_gap_seconds", FLOW_MIN_GAP_DEFAULT))
            ),
            flow_hold_seconds=max(
                FLOW_HOLD_MIN, min(FLOW_HOLD_MAX, gi("flow_hold_seconds", FLOW_HOLD_DEFAULT))
            ),
            flow_max_replies_per_window=max(
                FLOW_MAX_REPLIES_MIN,
                min(FLOW_MAX_REPLIES_MAX, gi("flow_max_replies_per_window", FLOW_MAX_REPLIES_DEFAULT)),
            ),
            flow_hourly_cap=max(
                FLOW_HOURLY_CAP_MIN, min(FLOW_HOURLY_CAP_MAX, gi("flow_hourly_cap", FLOW_HOURLY_CAP_DEFAULT))
            ),
            flow_ignored_exit=max(
                FLOW_IGNORED_EXIT_MIN,
                min(FLOW_IGNORED_EXIT_MAX, gi("flow_ignored_exit", FLOW_IGNORED_EXIT_DEFAULT)),
            ),
            group_idle_hours=max(
                GROUP_IDLE_HOURS_MIN, min(GROUP_IDLE_HOURS_MAX, gf("group_idle_hours", GROUP_IDLE_HOURS_DEFAULT))
            ),
            icebreak_daily_cap=max(
                ICEBREAK_DAILY_CAP_MIN,
                min(ICEBREAK_DAILY_CAP_MAX, gi("icebreak_daily_cap", ICEBREAK_DAILY_CAP_DEFAULT)),
            ),
            group_topic_enabled=gb("group_topic_enabled", True),
            group_topic_gap_hours=max(
                GROUP_TOPIC_GAP_HOURS_MIN,
                min(GROUP_TOPIC_GAP_HOURS_MAX, gf("group_topic_gap_hours", GROUP_TOPIC_GAP_HOURS_DEFAULT)),
            ),
            group_topic_daily_cap=max(
                GROUP_TOPIC_DAILY_CAP_MIN,
                min(GROUP_TOPIC_DAILY_CAP_MAX, gi("group_topic_daily_cap", GROUP_TOPIC_DAILY_CAP_DEFAULT)),
            ),
            group_topic_dedup_hours=max(
                GROUP_TOPIC_DEDUP_HOURS_MIN,
                min(
                    GROUP_TOPIC_DEDUP_HOURS_MAX,
                    gf("group_topic_dedup_hours", GROUP_TOPIC_DEDUP_HOURS_DEFAULT),
                ),
            ),
            group_daily_total_cap=max(
                0,
                min(
                    GROUP_DAILY_TOTAL_CAP_MAX,
                    gi("group_daily_total_cap", GROUP_DAILY_TOTAL_CAP_DEFAULT),
                ),
            ),
            group_topic_max_segments=max(
                GROUP_TOPIC_MAX_SEGMENTS_MIN,
                min(
                    GROUP_TOPIC_MAX_SEGMENTS_MAX,
                    gi("group_topic_max_segments", GROUP_TOPIC_MAX_SEGMENTS_DEFAULT),
                ),
            ),
            group_mention_member=gb("group_mention_member", False),
            group_interject_others=gb("group_interject_others", True),
            group_stale_days=max(
                GROUP_STALE_DAYS_MIN, min(GROUP_STALE_DAYS_MAX, gi("group_stale_days", GROUP_STALE_DAYS_DEFAULT))
            ),
            group_ref_sample_size=max(
                GROUP_REF_SAMPLE_MIN,
                min(GROUP_REF_SAMPLE_MAX, gi("group_ref_sample_size", GROUP_REF_SAMPLE_DEFAULT)),
            ),
            group_ref_prompt_count=max(
                GROUP_REF_PROMPT_MIN,
                min(GROUP_REF_PROMPT_MAX, gi("group_ref_prompt_count", GROUP_REF_PROMPT_DEFAULT)),
            ),
            heartbeat_min_minutes=max(
                HEARTBEAT_MIN_MINUTES_MIN,
                min(
                    HEARTBEAT_MINUTES_MAX,
                    gi("heartbeat_min_minutes", HEARTBEAT_MIN_DEFAULT),
                ),
            ),
            heartbeat_max_minutes=max(
                HEARTBEAT_MIN_MINUTES_MIN,
                min(
                    HEARTBEAT_MAX_MINUTES_MAX,
                    gi("heartbeat_max_minutes", HEARTBEAT_MAX_DEFAULT),
                ),
            ),
            # 2.0.8：gi → gf。默认值改成 1.5 之后 gi 会把它截成 1（int(1.5)），
            # 于是「配置里写 1.5、实际按 1 跑」——正是本文件开头警告的那种漂移。
            urge_refill_hours=max(
                URGE_REFILL_MIN,
                min(URGE_REFILL_MAX, gf("urge_refill_hours", URGE_REFILL_DEFAULT)),
            ),
            recent_talk_minutes=max(
                RECENT_TALK_MIN,
                min(RECENT_TALK_MAX, gi("recent_talk_minutes", RECENT_TALK_DEFAULT)),
            ),
            skip_cooldown_minutes=max(
                SKIP_COOLDOWN_MIN,
                min(SKIP_COOLDOWN_MAX, gi("skip_cooldown_minutes", SKIP_COOLDOWN_DEFAULT)),
            ),
            llm_gate=gb("llm_gate", True),
            gate_energy_floor=max(
                0, min(100, gi("gate_energy_floor", GATE_ENERGY_FLOOR_DEFAULT))
            ),
            respect_user_rhythm=gb("respect_user_rhythm", False),
            cue_followup=gb("cue_followup", True),
            followup_enabled=gb("followup_enabled", True),
            followup_after_minutes=followup_after,
            probe_after_minutes=probe_after,
            followup_max_minutes=followup_max,
            followup_cooldown_minutes=max(
                FOLLOWUP_COOLDOWN_MIN,
                min(FOLLOWUP_COOLDOWN_MAX, gi("followup_cooldown_minutes", FOLLOWUP_COOLDOWN_DEFAULT)),
            ),
            continue_enabled=gb("continue_enabled", True),
            continue_after_minutes=continue_after,
            continue_window_hours=continue_window_hours,
            silent_giveup_days=max(
                SILENT_GIVEUP_DAYS_MIN,
                min(
                    SILENT_GIVEUP_DAYS_MAX,
                    gi("silent_giveup_days", SILENT_GIVEUP_DAYS_DEFAULT),
                ),
            ),
            loop_enabled=gb("loop_enabled", True),
            loop_min_hours=loop_min,
            loop_max_hours=loop_max,
            closer_enabled=gb("closer_enabled", True),
            closer_after_hours=max(
                CLOSER_AFTER_MIN,
                min(CLOSER_AFTER_MAX, gi("closer_after_hours", CLOSER_AFTER_DEFAULT)),
            ),
            track_own_replies=gb("track_own_replies", True),
            decorate_outgoing=gb("decorate_outgoing", True),
            persist_proactive_history=gb("persist_proactive_history", True),
            use_core_clock=gb("use_core_clock", True),
            use_core_facade=facade_mode,
            proactive_provider_ids=proactive_providers,
            strict_proactive_provider=gb("strict_proactive_provider", False),
            owner_only_commands=gb("owner_only_commands", True),
            trigger_group_requires_at=gb("trigger_group_requires_at", True),
            closing_guard_enabled=gb("closing_guard_enabled", True),
            closing_guard_window_seconds=max(
                CLOSING_GUARD_WINDOW_MIN,
                min(
                    CLOSING_GUARD_WINDOW_MAX,
                    gi("closing_guard_window_seconds", CLOSING_GUARD_WINDOW_DEFAULT),
                ),
            ),
            closing_guard_prompt_enabled=gb("closing_guard_prompt_enabled", True),
            group_closeout_enabled=gb("group_closeout_enabled", True),
            group_closeout_window_seconds=max(
                GROUP_CLOSEOUT_WINDOW_MIN,
                min(
                    GROUP_CLOSEOUT_WINDOW_MAX,
                    gi("group_closeout_window_seconds", GROUP_CLOSEOUT_WINDOW_DEFAULT),
                ),
            ),
            group_closeout_threshold=max(
                GROUP_CLOSEOUT_THRESHOLD_MIN,
                min(
                    GROUP_CLOSEOUT_THRESHOLD_MAX,
                    gi("group_closeout_threshold", GROUP_CLOSEOUT_THRESHOLD_DEFAULT),
                ),
            ),
            group_closeout_cooldown_seconds=max(
                GROUP_CLOSEOUT_COOLDOWN_MIN,
                min(
                    GROUP_CLOSEOUT_COOLDOWN_MAX,
                    gi("group_closeout_cooldown_seconds", GROUP_CLOSEOUT_COOLDOWN_DEFAULT),
                ),
            ),
            # v2.1.4 三项：缺字段 / null / 认不出的值一律按 schema 默认（True）处理。
            # 走 gb() 而不是手写 bool()，是为了让面板里填的 "false"/"0"/"off"
            # 也能关掉——配置文件里这些值现实中就是字符串，bool("false") 会是 True。
            planning_enabled=gb("planning_enabled", PLANNING_ENABLED_DEFAULT),
            max_output_tokens_enabled=gb(
                "max_output_tokens_enabled", MAX_OUTPUT_TOKENS_ENABLED_DEFAULT
            ),
            time_projection_enabled=gb(
                "time_projection_enabled", TIME_PROJECTION_ENABLED_DEFAULT
            ),
            # v2.1.5 两项：同样走 gb()，面板里填的 "false"/"0"/"off" 也认。
            # 注意默认值与 engine.py 的 getattr 兜底不同（那边是 False），以 schema 为准。
            draft_review_enabled=gb(
                "draft_review_enabled", DRAFT_REVIEW_ENABLED_DEFAULT
            ),
            memory_tools_enabled=gb(
                "memory_tools_enabled", MEMORY_TOOLS_ENABLED_DEFAULT
            ),
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
    "min_gap_minutes": MIN_GAP_MINUTES_DEFAULT,
    "proactive_min_gap_minutes": PROACTIVE_MIN_GAP_DEFAULT,
    "miss_affection_min": MISS_AFFECTION_MIN_DEFAULT,
    "miss_daily_cap": MISS_DAILY_CAP_DEFAULT,
    "hourly_sends_cap": HOURLY_SENDS_CAP_DEFAULT,
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
    # 2.1.0 新进 LEGACY_DEFAULTS 的这几项
    "gate_energy_floor": GATE_ENERGY_FLOOR_DEFAULT,
    "max_sends_per_round": MAX_SENDS_PER_ROUND_DEFAULT,
    "cross_user_repeat_ratio": CROSS_USER_REPEAT_RATIO_DEFAULT,
    "cross_user_repeat_hours": CROSS_USER_REPEAT_HOURS_DEFAULT,
    "silent_giveup_days": SILENT_GIVEUP_DAYS_DEFAULT,
    "special_care_min_affection": SPECIAL_CARE_MIN_AFFECTION_DEFAULT,
    "group_daily_total_cap": GROUP_DAILY_TOTAL_CAP_DEFAULT,
    "special_care_hourly_cap": SPECIAL_CARE_HOURLY_CAP_DEFAULT,
    "group_idle_hours": GROUP_IDLE_HOURS_DEFAULT,
    "group_topic_gap_hours": GROUP_TOPIC_GAP_HOURS_DEFAULT,
    "flow_min_gap_seconds": FLOW_MIN_GAP_DEFAULT,
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
            try:
                with open(marker, encoding="utf-8") as f:
                    done = json.load(f)
            except Exception as e:
                # 原来这里被外层宽 except 吞掉并直接 return []：标记文件一旦损坏，
                # 每次启动都读一次坏文件、而且**永远不再写标记** → 提示永久静默失效。
                # 现在告警一次并当作「没写过」，继续走到下面的重写。
                done = None
                _warn_config_once(
                    "<baseline>",
                    f"配置基线标记文件损坏（{marker}）：{e}；已忽略并重新生成",
                )
            if isinstance(done, dict) and str(done.get("version", "")) == str(version):
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
                # 浮点默认值（cross_user_repeat_ratio 这类）用 int() 永远转不过去，
                # 面板把值存成字符串 "0.6" 时就会被判成「不是旧默认」而漏掉提示。
                try:
                    probe = float(str(current).strip())
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
            # 原子写：原来 `open(marker, "w")` 先截断再写，进程在截断后崩溃就留下
            # 0 字节文件，与「读失败即静默」正好构成一条自我锁死路径。
            _write_json_atomic(
                marker, {"version": str(version), "hints": hints}
            )
        except Exception as e:
            _warn_config_once(
                "<baseline>", f"写入配置基线标记失败（{marker}）：{e}"
            )
    except Exception:
        return hints
    return hints
