"""社交引擎：念头积累、选谁、该不该说、说完之后。

v1.19.0：
- 不再「每几分钟掷一次骰子，掷中了就发」：每个用户身上有一个随时间积累的 urge，
  越在意的人、对方越可能醒着的时段，攒得越快；刚聊过直接清零
- 攒满也不等于就发：先过规则闸门（热聊/冷脸/作息/睡意），再让模型自己判断要不要说
- 发送后的体验被记下来：被接话会抬高在意度，没人理会压低并抬高下次门槛
- 冷却项降为护栏，不再是节奏的唯一来源
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from astrbot.api import logger

try:
    from astrbot.api.event import MessageChain
except ImportError:
    MessageChain = None

from . import __version__, anchors, anchors as anchor_mod, core_facade, desire
from . import history_ingest
from . import memory_bridge
from . import pacing
from .clock import city_now
from .config import SocialConfig
from .core_bridge import CoreBridge
from .input_state import InputStateNotifier
from .sanitize import sanitize_incoming
from .anchors import anchor_meta, anchor_sentence, drop_anchor, prepare_round_anchors
from .verify import (
    _bigrams,
    _check_cross_user,
    _check_cross_user_action,
    _jaccard,
    strip_actions,
    verify_message,
)
from .signals import FILE_NAME as SIGNALS_FILE_NAME, SignalsWriter
from .style_profile import emoji_habit
from .throttle import throttle
from .generator import MessageGenerator
from .persona import resolve_persona
from .reasoning import (
    extract_cue,
    generate_reason,
    greeting_due,
    greeting_window_kind,
    greet_meta,
    live_cue,
    sleep_signal,
    time_slot,
)
from .state import SocialState
from . import groupflow
from .threads import (
    live_loops,
    live_promises,
    promise_entries,
    loop_entries,
    promise_meta,
    closer_meta,
    recent_exchange,
    closer_reason,
    extract_open_loop,
    live_loop,
    loop_meta,
    loop_reason,
    thread_meta,
    thread_reason,
    loops_resolved_by,
    LOOP_LATE_HOURS,
    PROMISE_WAIT_SECONDS,
)

# ─── 常量定义 ───────────────────────────────────────

# 上一轮结束后到下一轮开始，随机等这么久（一律秒）。心跳是「想起来看一眼手机」的时机，
# 它的粒度决定了所有分钟级配置的实际精度：心跳 15 分钟时，把 probe_after_minutes
# 从 2 调到 3 不会有任何区别。真正的发送节奏由 urge_refill_hours 与各种冷却决定，
# 心跳变快只是让「到点了」被发现的时机更准，不会凭空多发。
MIN_HEARTBEAT_SECONDS = 60      # 心跳间隔下限，防止有人把配置填成 0 变成忙循环
FLUSH_INTERVAL_SECONDS = 30      # 状态刷盘间隔
HOT_CHAT_THRESHOLD = 900         # 十分钟内还在聊，不插话

# F1：本插件已注册的指令名（用于识别被框架剪掉 `/` 的指令消息）。
_COMMAND_NAMES = (
    "自主社交状态", "社交自检", "触发社交", "主动消息预览", "主动消息记录", "群社交状态",
)


def _strip_ts(conv: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去掉只为排序用的 ts，返回 prompt 侧要的 {dir,text} 列表。

    ts 是 `_generation_conversation` 合并两个来源时的排序键，不属于对话内容；
    留着它会让下游（`_format_conversation`、风格统计、`_recent_proactive` 去重）
    多一个无关字段，也让测试断言变噪。
    """
    return [{"dir": str(m.get("dir", "") or ""), "text": str(m.get("text", "") or "")}
            for m in conv if isinstance(m, dict)]


def _splice_ledger_into_session(
    session_history: List[Dict[str, Any]],
    ledger: List[Dict[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    """把账本里会话库没有的条目，按 ts 插回会话库主干的正确位置。

    AstrBot 的会话库只存 role/content，**没有时间戳**；而插件自己的账本每条都带 ts。
    直接「会话库 + 账本剩下的」会把主动消息排到用户所有回复之后（见调用处注释）。

    这里反过来：会话库是主干（它反映真实发生顺序，只是缺了主动消息那几条），
    账本里缺的条目各带 ts，按「落在哪两条之间」插进去。

    锚点：账本与会话库**同文同向**的条目就是同一句话，用它的 ts 当主干上那个位置的
    时刻。一条缺条目插在「第一个比它新的锚点」之前；比所有锚点都旧就插在开头，
    比所有锚点都新就插在末尾。

    同 ts 的缺条目（连发的后续几条）必须**保持账本里的相对顺序**：insert 的 pos
    用 `>` 严格大于来算，等于时不越过，后一条因此落在前一条之后。
    """
    anchor_ts: Dict[Tuple[str, str], float] = {}
    for item in ledger:
        key = (item["dir"], item["text"])
        # 账本可能因 store_text 关掉而过期，取不到 ts 时按 0（最旧）
        ts = _num(item.get("ts", 0.0) or 0.0)
        if key not in anchor_ts or ts > anchor_ts[key]:
            anchor_ts[key] = ts

    # 主干：会话库原样搬过来。**不去重**——用户连着说两句一样的「早」是真实发生的，
    # 原来（按来源拼接那版）也保留；这里不能顺手改掉这个既有行为。
    out: List[Dict[str, Any]] = []
    seen = set()
    for s in session_history:
        if not isinstance(s, dict):
            continue
        text = str(s.get("text", "") or "").strip()
        if not text:
            continue
        key = (str(s.get("dir", "") or ""), text)
        seen.add(key)
        out.append({"dir": key[0], "text": text})

    # 主干上每个位置的时刻。找不到锚点的位置（会话库有、账本没有：历史导入或手改过库）
    # 记 +inf —— 不知道它何时发生，就不拿它去挡任何缺条目，缺条目因此都排在它后面，
    # 而不会因为一个未知时刻被错误地顶到用户刚说的话前面。
    slot_ts = [anchor_ts.get((m["dir"], m["text"]), float("inf")) for m in out]

    missing = [
        item for item in ledger
        if (item["dir"], item["text"]) not in seen
    ]
    # 稳定排序：同 ts 保持账本顺序（连发的 p1、p2 不会颠倒）
    missing.sort(key=lambda m: _num(m.get("ts", 0.0) or 0.0))

    for item in missing:
        ts = _num(item.get("ts", 0.0) or 0.0)
        pos = len(out)
        for i, t in enumerate(slot_ts):
            if t > ts:
                pos = i
                break
        out.insert(pos, {"dir": item["dir"], "text": item["text"]})
        slot_ts.insert(pos, ts)

    if limit > 0:
        out = out[-limit:]
    return out


def _looks_like_command(event: Any, raw: str, msg: str) -> bool:
    """F1：这条消息是不是一次真的要执行的指令（含被框架剪掉 `/` 的情况）。

    框架会把唤醒前缀从 `message_str` 剪掉，所以只看 `msg.startswith('/')` 会漏判——
    别的插件的指令于是被当成聊天记进来，刷新 last_seen、清掉念头，主动消息再也不触发。
    三判据：原始消息链里带 `/`、当前文本带 `/`、或命中已注册指令名且是唤醒消息。
    """
    try:
        chain = getattr(getattr(event, "message_obj", None), "message", None)
        if chain:
            head = "".join(str(getattr(comp, "text", "")) for comp in chain).lstrip()
            if head.startswith("/"):
                return True
    except Exception:
        pass
    if str(raw or "").lstrip().startswith("/") or str(msg or "").lstrip().startswith("/"):
        return True
    try:
        wake = bool(getattr(event, "is_at_or_wake_command", True))
    except Exception:
        wake = True
    if wake:
        body = str(msg or "").strip()
        for name in _COMMAND_NAMES:
            if body == name or body.startswith(f"{name} "):
                return True
    return False


def _intimacy_reason() -> str:
    return "她这会儿有点想他，想问一句可不可以"


def _intimacy_meta() -> Dict[str, Any]:
    """「想要了」通道的元数据：只给写法要求，不给台词（台词交给人设与模型）。"""
    return {
        "msg_type": "intimacy_ask",
        "intent": "intimacy_ask",
        "msg_type_desc": "想问对方一句「今晚可不可以」这类的事",
        "style_hint": (
            "试探着、带点不好意思地问一句，别露骨、别像通知；"
            "对方要是不愿意就当没这回事，别追、别闹。"
        ),
    }
WEEKEND_MOOD_BOOST = 1.25      # 周末人更松，想说话多一点

# 发送前最终检查
FINAL_CHECK_GAP_SECONDS = 120

# 「没接话」的判定窗口至少给多长。回复率统计用 reply_window_hours（默认 2 小时），
# 但据此认为对方不想理你、从此不再主动找 TA，需要更长的观察期：晚上发的话早上才回，
# 完全正常。
PENDING_IGNORE_MIN_HOURS = 12

# 同一个由头试过一回但没发出去（模型否决/发送失败/对方刚好在说话）之后，往后推多久
# 再想。不推的话，到点的由头会每个心跳都把人拉回去重试，每半小时发一次同一件事。
CUE_RETRY_DELAY_SECONDS = 6 * 3600
# 重试次数上限：每次重试都要跑一遍完整 LLM，不设上限就是持续烧 token。
# 与 expire_at 双保险——有效期管「多久」，次数管「多少次」
CUE_MAX_TRIES = 3

# 错误重试
# 主动消息对时效性不敏感，重试三次意义不大；而 try_once 是串行的：每次失败的发送要
# 2×8 秒的等待，一轮里遇到几个失败的人就能把整个心跳占住几十秒，期间所有用户都停摆。
# 宁可少试、快点失败——下一轮心跳本来还会再试。
SEND_RETRY_COUNT = 1
SEND_RETRY_DELAY = 3.0

# 一轮里「试着说」的次数上限 = max_sends_per_round × 这个倍数。
#
# 为什么必须有：`budget` 只限制**发出去几条**，而发不出去时那条配额会被原样还回来
# （`serve()` 里 `budget[bid] += 1`）。LLM 挂着的时候每个候选都会失败、都会归还配额，
# 于是一轮心跳会把**全部候选**挨个试一遍——线上实测 550 个候选 = 550 次失败 =
# 550 行 `→ LLM 不可用` 日志，每条还要等满 90 秒超时才放弃。这就是用户看到的刷屏。
# 尝试次数不设上限，等于把「一轮最多发 3 条」这道护栏绕过去了。
# 取 2 倍而不是 1 倍：模型否决（NO）也是「没发出去」，但它是一次成功的调用，不该
# 因此让后面真有由头的人整轮没机会。
ATTEMPT_PER_SEND = 2
# 一轮里一次都没成功、且失败原因是基础设施（provider 取不到 / 超时 / 401…）时，
# 剩下的候选不必再试：同一个 provider，下一个请求只会得到同样的结果。
_INFRA_NOTES = ("LLM 不可用", "LLM 生成为空")

# 没接 Core 时好感度的合成权重：在意程度（近期的热度）与熟络度（相处时长）。
# 两个都留着，是因为只有在意程度时「最近回不回我」会把认识很久的人压成陌生人；
# 两个都给足，是因为「聊得多」本身也不该把一个长期无视你的人写得很亲。
AFFECTION_INTEREST_WEIGHT = 0.55
AFFECTION_FAMILIAR_WEIGHT = 0.45

# 发送侧熔断。发送是在**生成之后**才知道成不成的，而生成要花钱：平台掉线、风控、
# 适配器未就绪时，每个心跳都会先给每个候选跑一遍完整 LLM 把消息写好，再发现送不到。
# 这些消息一条也送不出去，调用却一次不落——这就是「发不出去还在拼命烧」的全部来源。
# 连续失败到阈值就先停止生成（一条 token 都不花），到期自动恢复。
SEND_BREAKER_TRIP = 3
SEND_BREAKER_BASE_SECONDS = 900.0    # 15 分钟起步
SEND_BREAKER_MAX_SECONDS = 7200.0    # 连续失败越多翻倍，2 小时封顶
# 翻倍指数的上限（见 `_note_send_failure`）。只为挡住 `2 ** 1000` 那种 OverflowError，
# 数值本身远大于 SEND_BREAKER_MAX_SECONDS，所以不影响任何实际退避时长。
_BREAKER_EXP_CAP = 16

# 念头超出门槛多少算「很想说」：很想说的时候不因为最小间隔再等——真人急着说某件事时
# 不会先看一眼上次是几点发的。
EAGER_URGE_RATIO = 1.45

# 单人发送失败后的冷却。平台报「不是好友」那种已经按人隔离了；这里管的是发送本身
# 失败（超时、平台抖动）。以前只有 cue/loop 两条路有重试节奏，收场/问候/追问发不出去
# 时下一轮心跳照旧各写一遍完整 LLM——收场那条能白烧 8 小时。
# 她会记仇的三道限（配合 generator._relationship_line）：
#   阈值——Core 的 aggression 常年 0~20，34 才叫「有点不舒服」
AGGR_NOTE_THRESHOLD = 34.0
#   持续——连着几次采样都在高位才起效
AGGR_NOTE_SAMPLES = 2
#   节制——用过一次之后隔这么久才允许再提
AGGR_NOTE_COOLDOWN = 3 * 86400.0

SEND_FAIL_COOLDOWN_BASE = 1800.0     # 30 分钟起步
SEND_FAIL_COOLDOWN_MAX = 28800.0     # 翻倍到 8 小时封顶

# 平台侧问题（适配器未就绪、**umo 已失效**、平台不支持主动消息）导致的发送失败：
# 不记在这个人的「关系账」上，但**按次数升档**，见 `_mark_send_failed`。
#
# 原来这是一档固定的 5 分钟。问题在「umo 已失效」这类失败**不会自愈**：被删好友、
# 换了号、群散了，那个 umo 永远解析不出平台。5 分钟等于下一轮心跳又把这个人选回来、
# 又花一次完整 LLM 生成、又失败一次。线上实测一轮 612 个候选里就有几十个这样的人，
# 它们把每轮的「尝试配额」吃光，健康的人反而轮不到（一轮 25 次尝试只发出去 2 条）。
# 现在 30 分钟起步、每次 ×4、12 小时封顶：同一个死人最多占掉几次尝试就自己退场，
# 而对方真回来发消息时 `observe` 会清掉隔离（见 `_mark_send_failed` 的说明）。
SEND_FAIL_PLATFORM_COOLDOWN = 1800.0
SEND_FAIL_PLATFORM_MAX = 43200.0     # 12 小时封顶

# 手动触发最多试几个候选：第一个发不出去就换下一个（回退）
TRIGGER_CANDIDATE_LIMIT = 3

# 两次手动触发之间的最短间隔。一次触发最多 3 个候选、每个要跑两轮 LLM，
# 连点会把这些调用成倍放大（而且连点看到的还是同一批候选人）。
TRIGGER_MIN_INTERVAL_SECONDS = 20.0

# 发送失败隔离这个人多久不再试
SEND_BLOCK_HOURS = 24
# 低档连着几次发不出去就停发（真人不会一直追一个不回话的人）
GIVEEUP_TRIES = 2
# 只有平台明确说「这个目标发不了」才隔离该用户。平台级的失败（不支持主动消息、
# 适配器未就绪、传输层报错）不在此列：那是平台与配置的问题，隔离用户只会让一个
# 健康的人凭空消失一整天，表现为「几百小时才发一条」。
# 匹配范围必须很窄：风控、限流、网络抖动的临时失败里也可能含这些字样。
_PEER_UNREACHABLE_MARKERS = (
    "请添加对方为好友", "添加对方为好友", "不是好友", "非好友", "对方不在你的好友列表",
    "好友关系不存在", "对方不是你的好友", "not friend", "add friend", "unfriend",
    "friendship", "已被删除", "已删除好友", "删除好友", "拉黑", "已拉黑",
    "对方已注销", "账号已注销",
)
# 平台级原因：只用来把错误说清楚，不用来隔离用户
_PLATFORM_UNAVAILABLE_MARKERS = (
    "未找到匹配", "不支持主动", "qq_official", "adapter", "平台未就绪", "平台未连接",
    "connection refused", "session not found", "session expired", "会话已失效",
)

# 「这条 umo 废了」的标记（相对于「整个平台/适配器坏了」）。见 `_umo_problem`。
_UMO_PROBLEM_MARKERS = (
    "未找到匹配", "会话已失效", "session not found", "session expired",
    "umo 可能已失效",
)


def _umo_problem(err: str) -> bool:
    """这次失败是不是**这个 umo 本身**的问题（会话失效 / 平台解析不出）。

    与 `_platform_problem` 的区别很重要：`_platform_problem` 还包含「adapter 没加载」
    这类**全局**故障，那种才该熔断整个角色；而「未找到匹配的会话平台」在线上
    99% 是这个人自己那条 umo 废了（被删好友、换号、群散了），拿它去熔断会让
    **所有人**陪着停 15 分钟。

    线上实测（`backend.log` 19:25~19:26）：三个这样的死人连着失败三次，
    `[default] 连续 3 次发不出去，暂停生成 15 分钟` —— 于是每一轮都在「熔断中」
    空转，健康的人一条也收不到。账本里 `发送熔断 66946` 就是这么来的。
    """
    low = str(err or "").lower()
    return any(m.lower() in low for m in _UMO_PROBLEM_MARKERS)


# 「平台收下了请求、但拒绝发给**这一个**目标」的标记：`ActionFailed` / `retcode=`。
# 这类失败（被禁言、群被风控、retcode 1200）和「适配器没加载」不是一回事：
# 同一次心跳里别的群/别人照常能发出去，拿它熔断整个角色纯属误伤。
_TARGET_PROBLEM_MARKERS = ("actionfailed", "retcode=")


def _target_problem(err: str) -> bool:
    """平台明确拒绝把这句发给**这一个**目标（区别于整条链路坏了）。"""
    low = str(err or "").lower()
    return any(m in low for m in _TARGET_PROBLEM_MARKERS)


# 「整条链路坏了」的标记：只有能**正面认出**这些，才允许熔断整个角色。
#
# 反过来默认（认不出就当全局故障）代价太大：线上 19:59 有一次 `ActionFailed` 的
# 异常文本是空的，`str(e) == ""`，任何按子串分类都认不出来，于是被当成全局故障
# 数进熔断，第三次就把整个角色停了 15 分钟 —— 而同一分钟别的群照常发得出去。
# 熔断的作用是止损，止损判错了就是**主动断掉所有人的消息**，所以宁可漏判。
_GLOBAL_PROBLEM_MARKERS = (
    "发送超时", "timeout", "timed out", "connection refused", "connection reset",
    "connection aborted", "broken pipe", "平台未就绪", "平台未连接", "adapter",
    "eof occurred", "cannot connect",
)


def _global_problem(err: str) -> bool:
    """这次失败是不是**整条链路**坏了（所有目标都会挂）。认不出来一律当不是。"""
    low = str(err or "").lower()
    return any(m in low for m in _GLOBAL_PROBLEM_MARKERS)


def _num(value: Any, default: float = 0.0) -> float:
    """容错取数：读不出来、或读出来不是有限值（nan/inf），一律当 default。

    两个来源都不是我们能控制的：`state.json` 是主人能手改的文件，Core 快照是另一个
    插件写的。它们出现 `"2 小时前"`、`""`、`null` 时，裸 `float()` 抛的 ValueError
    会冒泡到闸门/挑选/轮内记账，而那条链路上大多没有逐用户兜底——一条脏数据就作废
    一整轮心跳，事后还看不出是谁。

    更要紧的是 nan/inf 会**静默改变判定**，一个异常都不抛：
      * `now - nan < cooldown` 恒假 → 护栏冷却、最小间隔、48 小时冷静期全部失效
      * `nan <= stopped` 恒假 → 「停发」永不成立，她会一直追一个不回话的人
      * `nan - base < rise`、`nan < affection_min` 恒假 → 硬门槛被静默放过
      * 反过来 `inf >= 急切线` 恒真 → 凭空多出一张「很想说」的豁免券
      * `int(nan)` / `int(inf)` 则直接抛 ValueError / OverflowError
    归到 default 之后这些都变成**确定行为**，与「字段没写」同义，不再看她当天像谁。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _body_asleep(body: Any) -> bool:
    """Core 契约里「她现在在睡吗」。

    读它必须用**解析**而不是真值判断：Core 的布尔字段可能是字符串（配置面板、手改
    state.json、老版本迁移都能写出来），而 `bool("false")` 是 True —— 那会把她一整天
    当成「正在睡觉」，所有主动开口整体静默。口径统一在 `desire.truthy`（与 Core 的
    `_as_bool` 同一套词表），别在各处另判一套。

    `body` 不是 dict（Core 返回了字符串/None）时按「没在睡」处理：宁可让她说一句话，
    也不要因为一个读不出来的字段把人永久静音。
    """
    if not isinstance(body, dict):
        return False
    return desire.truthy(body.get("asleep"))


def _person_flow_state(engine: Any, bid: str, now: float) -> Tuple[bool, bool]:
    """现在是不是安静时段、她是不是在睡（人物侧那两道闸）。

    私聊侧五道闸都查了这两项，只有群心流漏了；而群心流是事件驱动的（群消息一到就走，
    不经过心跳），于是深夜群里有人说话她照样插一句。

    之所以是模块级函数而不是方法：调用它的地方（群心流、事件回调、测试桩）有的只装配了
    部分依赖，`engine` 上可能没有 `core`/`_remember_night`/`_moment_of`——缺哪样就按
    「不安静、没在睡」降级，绝不因为查不到而把整轮卡死。
    """
    asleep = False
    try:
        body = engine.core.bot_self_state(bid)
        engine._remember_night(bid, body)
        asleep = _body_asleep(body)
    except Exception:
        pass
    try:
        is_quiet = engine._quiet_now(bid, engine._moment_of(bid, now).hour)
    except Exception:
        is_quiet = False
    return is_quiet, asleep


class _SendFailed(Exception):
    """发送失败。

    unknown=True：投递结果未知（超时/连接中断），消息可能已经送达，
    再发一次就是给同一个人发两条一样的。
    peer_unreachable=True：平台明确说这个目标发不了（非好友/已注销），才值得隔离该用户。
    """

    def __init__(
        self,
        message: str,
        *,
        unknown: bool = False,
        peer_unreachable: bool = False,
    ) -> None:
        super().__init__(message)
        self.unknown = unknown
        self.peer_unreachable = peer_unreachable

# 回访那件事之前，这场话至少凉下来多久：还在你来我往时问「后来呢」太急
LOOP_QUIET_GAP_SECONDS = 1800.0

# 早晚问候只对最近有来往的人发：几天没说话的人每天收一句早安，那不是问候是群发
GREET_FRESH_SECONDS = 4 * 86400.0

# TA 一直没开口、她自己已经主动发了这么多条之后，「没有正事」的主动开口（分享/念想/
# 另起话题）就静下来，只留问候（早/晚安，本来就不指望回）与到点的由头，直到 TA 再次开口。
#
# 真人不会一直追一个不回话的人：发两三条没回应，就会等对方先开。
# 用 `silent_sent`（TA 沉默期内的主动条数）而不是 `no_reply_streak`：后者要等
# 回复窗口（12 小时）过了才 +1，实测「一直发、他一句不回」 4 天才涨到 2，太迟钝。
COLD_SILENCE_SENT = 3

# 连发补发间隔不再用插件自己的常量：拆段与间隔都跟 AstrBot 每个 Bot 的
# 「分段回复」设置走（见 social/pacing.py）。下面这两个旧常量已无引用。

# 读不到 Core 时，主人面板上必须写清楚「少了什么」，而不是只丢一句「未找到」
_DEGRADED_NOTE = (
    "\n   已停用：按她所在城市的时区计时（use_core_clock）、精力/社交能量阈值、"
    "睡意与作息硬闸、Core 给的句长约束。"
    "主动消息仍会发，只是少了这些依据。"
)

# 没拿到 Core 的时区偏移时的提示。静默降级是很坏的一类降级：所有时段判断（早安窗口、
# 安静时段、今天感觉怎么样）都跑在容器本地时区上，差几小时就是「下午收到早安、
# 清晨被叫醒收晚安」，而面板上看不出任何异常。
_NO_TZ_NOTE = (
    "\n   ⚠️ 时段判断跑在**本机时区**上（没拿到 Core 的时区偏移）："
    "所有「早安/晚安/安静时段」都会按你机器的钟点算。"
    "如果这台机器的时区和用户所在地差得多，收发时间会明显不对。"
)

# 过期用户数据清理的扫描间隔（秒）
PRUNE_INTERVAL_SECONDS = 3600


# 播种（读 AstrBot 会话库 + 应用名单）的扫描间隔（秒）
SEED_INTERVAL_SECONDS = 1800


class SocialEngine:
    """自主社交引擎。"""

    def __init__(
        self,
        context: Any,
        config: Any,
        state_path: Optional[str] = None,
        data_dir: Optional[str] = None,
        time_source=None,
    ):
        self.context = context
        self.cfg = SocialConfig.from_astrbot(config)
        # 引擎与状态层共用同一个时间源：判断「刚聊过」「超时没回」时两边取到不同的
        # 时钟会让结论互相矛盾
        self._time_source = time_source or time.time
        # 墙钟回拨保护：NTP 校正、容器从快照恢复、宿主改时间，都会让 now 小于已经写进
        # state 的时间戳。而 `now - last_sent < cooldown` 这类判断一旦拿到负数就会同时成立，
        # 于是所有冷却、热话窗口、被否决冷却**一起**把人永久挡住，直到墙钟追上偏差。
        # 集中在这里钳一下：回拨时时间相当于停住（所有“距今多久”都变成 0），宁可多等，
        # 也不会因为一个负数让谁永远静默。逐个改判断点容易漏，这里改一处就覆盖全部 28 处。
        self._time_high: float = 0.0
        # 每个 (bot, 用户) 一把锁。
        #
        # 为什么要：`settle_minds` 遍历所有用户、循环里有 await（读 Core 快照、算窗口），
        # 而 `observe` 可以在任意一个 await 点插进来改**同一个** user dict。不加锁的后果
        # 不是崩，是数据慢慢漂：urge 少加一点、last_seen 被覆盖、conversation 顺序错乱。
        # 锁是 per-user 而不是全局——全局锁会把 176 个用户的结算串成一条。
        #
        # 不像 Core 那样用 WeakValueDictionary：那里的锁存在服务对象上、只有调用栈
        # 持有强引用，所以必须担心回收。这里 `self._user_locks` 自己就持有强引用，
        # 锁的生命周期跟着引擎走，不存在「刚建好就被回收」那个坑。代价是长期运行会
        # 按用户数留锁，量级和 state 里的用户条目相同，可以接受。
        self._user_locks: Dict[Tuple[str, str], asyncio.Lock] = {}

        if state_path is None:
            state_path = os.path.abspath(
                "data/plugin_data/astrbot_plugin_autonomous_social/state.json"
            )
        self.state = SocialState(state_path, time_source=self._time)
        self.core = CoreBridge(self.cfg.mode, data_dir=data_dir)
        # 契约 v2 的同进程只读门面。懒解析：插件加载顺序不保证，构造时 Core 可能还没起来。
        # `None` 表示「还没查过」或「查过但没找到」，两者都由 `core_facade` 的缓存区分。
        self._facade: Any = None
        self._facade_tried = False
        # 写回给 Humanoid Core 的信号（它替她主动开过口、被谁冷落了几次）。写自己目录下的
        # 单独文件，两边不会并发写同一个文件。
        self.signals = SignalsWriter(
            os.path.join(os.path.dirname(state_path), SIGNALS_FILE_NAME), time_source=self._time
        )
        self.generator = MessageGenerator(context, self.cfg, time_source=self._time)
        # 「正在输入」：主动开口前先向 QQ 报一声（仅 NapCat 私聊，纯装饰，出错不影响发消息）
        self.input_state = InputStateNotifier(
            context, lambda: self.cfg, logger, time_source=lambda: time.monotonic()
        )
        # 把面板上配的间隔/超时真正吃到：不然永远按构造时的默认 0.5/120 跑。
        self.input_state.configure(
            self.cfg.input_state_interval_seconds, self.cfg.input_state_timeout_seconds
        )
        self._data_dir = data_dir
        self.running = False
        self._last_flush = 0.0
        self._last_prune = 0.0
        self._last_seed = 0.0
        # 发送侧熔断状态，**按 bid 分**。整份插件只有一个引擎实例，拆不开的话
        # A 的平台掉线会把 B 一起停掉；更糟的是「任何一次成功就清零」，B 每发成一条
        # 就把 A 的失败计数抹掉，A 的熔断永远攒不满阈值。
        self._send_breaker: Dict[str, Dict[str, Any]] = {}
        # 熔断只活在这个引擎实例的生命里。放模块级会让插件重载后新实例继承旧实例
        # 的熔断状态，看上去像刚启动就在停摆。
        # 每个角色最近一次 LLM 失败的样子，状态页逐角色显示
        self._last_llm_error: Dict[str, str] = {}
        # 本轮「有由头但没发」的原因计数（按分类聚合，一轮一行日志）。
        #
        # 为什么不在闸门处逐人打日志：一个 412 人的角色，每轮有一两百人因为
        # 「现在是安静时段」「她正在睡觉」被挡下，一天几十轮 —— 线上实测这两个原因
        # 一天各 5606 / 4917 行，比 LLM 那条刷得还狠。而这些行**没有一个信息量**：
        # 同一个原因在一轮里重复两百遍，结论仍然是「这轮大家都被安静时段挡了」。
        self._round_skips: Dict[str, int] = {}
        # 归不进已知分类的拦截原因原文 → 次数。轮末只打前三条，用于定位
        # 「其它未发 N 条」到底是什么（v1.27.50：曾把闸门日志降到 debug，
        # 于是 39 条失败一条原因都没有）。
        self._round_skip_samples: Dict[str, int] = {}
        # ── 运行指标（只记数字与短原因，不含任何正文）────────────────────
        # 为什么要有：状态页原来只写「心跳：每 8~15 分钟一次」这种**静态说明**，
        # 于是「没触发」时根本分不清是没到时间、provider 取不到、用户被隔离、
        # 模型否决、平台失败，还是后台循环压根没跑。容器侧更没法看——插件自己的
        # debug 日志默认关着，AstrBot 的文件日志也可能没开。
        # 每角色「这一小时已经发了几个」。安全闸，防 180 人同时攒满时一次涌出。
        # 计数是滚动的一小时窗口（记时间戳列表），不是自然小时——跨零点不会归零重来。
        self._hourly: Dict[str, List[float]] = {}
        # 清洗前的原始模型输出原来存在这里（实例级单字段），并发发送会互相污染；
        # 现在是 `_speak_inner` 的局部变量，验收失败时记下来，下次能分辨
        # 「模型自己漏写了括号」还是「清洗/截断弄坏的」。
        self._m: Dict[str, Any] = {
            "last_heartbeat": 0.0,      # 最后一次心跳进来的时刻
            "last_settled": 0.0,        # 最后一次真正结算了念头
            "last_settled_users": 0,    # 那次结算了多少个人
            "last_ready": 0,            # 多少人念头够了
            "last_candidate": 0,         # 多少个候选进了闸门
            "last_sent": 0,             # 最后一次真的发出去了
            "last_failed": 0.0,          # 最后一次失败
            "last_failure": "",         # 失败在哪一步（短标签）
            "rounds": 0,                # 累计心跳轮数
            "sent_total": 0,            # 累计主动消息
            "settle_user_errors": 0,    # 累计被跳过的坏用户数（见 settle_minds 的逐用户兜底）
            "blocked_reasons": {},      # 各闸门各拦下多少（近 N 轮滚动）
            "llm_circuit_scopes": [],  # v1.28.2：当前熔断中的 LLM scope（仅状态展示）
        }
        # 累计类指标从上次落盘的地方接上。`last_*` 那几个不接——它们本来就该是
        # 「最近一次」，接了会让人以为这一轮刚跑过。
        try:
            _prev = self.state.runtime_metrics()
        except Exception:
            _prev = {}
        for _key in ("rounds", "sent_total", "rejected_total", "rule_total",
                     "veto_total", "anchor_cross_user_total",
                     "settle_user_errors"):
            try:
                self._m[_key] = int(_prev.get(_key, 0) or 0)
            except (TypeError, ValueError):
                pass
        _prev_blocked = _prev.get("blocked_reasons")
        if isinstance(_prev_blocked, dict):
            self._m["blocked_reasons"] = {
                str(k): int(v or 0) for k, v in _prev_blocked.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }

        # 最近一次播种/导入的结果（仅用于状态展示，不落盘）
        self._last_import_note = ""
        # bid -> 本周期实际用到的人格名（仅用于状态展示，不落盘）
        self._last_persona: Dict[str, str] = {}
        # bid -> 她所在城市相对本机的分钟偏移（从 Core 契约里拿）。没有契约时不设，
        # 时段判断退回本机时钟：宁可用错一个时钟，也不能把她的城市当成 UTC。
        self._city_offset: Dict[str, int] = {}
        # bid -> (她实际的夜起始小时, 结束小时)，来自 Core 按今天日程算出的生物钟。
        # 安静时段优先用它：夜猫子人格凌晨四点睡，用配置里写死的 23 点去判是判错的。
        self._night_windows: Dict[str, Tuple[float, float]] = {}
        self._burst_tasks: set = set()
        # 群心流接话的后台任务（观察链路不阻塞）
        self._bg_tasks: set = set()
        # 正在生成心流接话的群 (bid, gid)：一个群同一时刻只允许一条接话在途。
        # 闸门计数要等接话发出后才更新（中间隔着几秒 LLM 生成），没有这道锁时
        # 活跃群里连来几条消息会各派一个任务、全部抢在计数更新前过闸 → 同时发好
        # 几句，把 min_gap/每窗上限/每小时上限一起击穿。
        self._flow_inflight: Set[Tuple[str, str]] = set()
        # 上一次手动触发（`/主动 触发`）的时刻；None = 本次进程还没触发过。
        # 不持久化：重启之后允许立刻再触发一次是合理的。
        self._last_trigger_at: Optional[float] = None

    # ─── 生命周期 ───────────────────────────────────────

    def _beat_signals(self) -> None:
        """刷新信号文件，并把我们**真实的保鲜期**告诉 Core。

        Core 那边的过期阈值原来是写死的 900 秒，而心跳间隔是可配的（最大 120 分钟）
        —— 只要配得比 15 分钟长，两次心跳之间整份信号就会被判过期、当成没有。
        这里声明「两个心跳间隔」，让对面按我们的节奏算（它自己会夹到上限）。
        """
        try:
            _lo, hi = self._heartbeat_range()
            self.signals.set_ttl_seconds(hi * 2.0)
        except Exception:
            pass
        self.signals.beat()

    def _open_items(self, bid: str, now: float) -> List[Dict[str, Any]]:
        """她手上**还开着**的事：对方提了没下文的（loop）+ 她自己许下还没兑现的（promise）。

        这是发给 Core 的那份「她此刻在惦记什么」。Core 把它渲染进 system_prompt 的稳定段，
        所以两点必须守住：
        1. `at` 取这件事**被记下的那一刻**（条目自己的 `at`），不是 now。每轮重盖成 now 会让
           「三天前答应的事」被说成「刚答应的」，而且那段文本每轮都变，把整段前缀缓存打掉。
        2. 整体重算、整体覆盖（见 `SignalsWriter.note_open_items`）。只增不减的话，对方早就
           给出结果的事会永远挂在她的提示词里。

        与 `live_loops` / `live_promises` 的区别：那两个回答的是「**现在该问 / 该兑现**了吗」，
        这里回答的是「**还开着**吗」。一件三小时后才到点回访的事现在照样是开着的——而对话
        历史一旦被压缩，它就只剩这条通道能留下。
        """
        out: List[Dict[str, Any]] = []
        for uid, u in (self.state.bot(bid).get("users") or {}).items():
            try:
                for item in loop_entries(u):
                    about = str(item.get("about", "") or "").strip()
                    due = _num(item.get("due"))
                    # due<=0 的条目永远不会被回访（live_loops 也跳过它），不该挂在她手上
                    if not about or due <= 0:
                        continue
                    expire_at = _num(item.get("expire_at"))
                    # 老数据没有 expire_at：与 live_loops 用同一个兜底口径，否则同一件事
                    # 会一边被当成「凉过头了」、一边还挂在提示词里
                    if now > (expire_at if expire_at > 0
                              else due + LOOP_LATE_HOURS * 3600.0):
                        continue
                    at = _num(item.get("at"))
                    if at <= 0:
                        # 不知道什么时候记下的：宁可不发，也不能编一个时间戳——Core 把它
                        # 渲染成「三天前」这种相对时间，编出来的比没有更糟
                        continue
                    out.append({"text": about, "uid": str(uid),
                                "at": at, "kind": "loop"})
                for item in promise_entries(u):
                    about = str(item.get("about", "") or "").strip()
                    if not about:
                        continue
                    at = _num(item.get("at"))
                    if at <= 0:
                        # 老数据只有 due：按写侧的公式反推许下的时刻
                        # （due = at + PROMISE_WAIT_SECONDS），而不是拿 0 当 1970 年发过去
                        at = _num(item.get("due")) - PROMISE_WAIT_SECONDS
                    if at <= 0:
                        continue
                    out.append({"text": about, "uid": str(uid),
                                "at": at, "kind": "promise"})
            except Exception as e:
                # 一条脏记录只该让这个用户这轮不上报，不该连累整轮心跳的信号写回
                self.log(f"整理 {bid}/{uid} 的未完事项失败: {e}")
        return out

    def start(self) -> None:
        """启动引擎。"""
        self.running = True
        # 先把信号文件放下去：Core 那边靠它判断「社交层活着」，不能等到第一次真的发消息才写。
        if self.cfg.mode != "standalone":
            try:
                self._beat_signals()
            except Exception as exc:  # 写不进去不影响启动
                logger.warning(f"[autonomous_social] 信号文件初始化失败: {exc}")
        logger.info(f"[autonomous_social] 引擎启动，模式: {self.cfg.mode}")
        # 启动即播种：读回会话库里的历史私聊对象 + 应用播种名单。放后台任务里跑，
        # 不阻塞插件加载（这段要扫 sqlite 全表）
        task = asyncio.create_task(self._seed_tick(force=True), name="social-seed")
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        # 启动后立刻空跑一轮：只结算不发送不调模型，几十毫秒就出结果。
        # 不做这个的话，部署后要先干等 8~15 分钟才知道链路通不通，而那期间
        # 「零发送」和「插件坏了」在状态页上长得一模一样。
        self._bg_tasks.add(
            asyncio.create_task(self._startup_dry_run(), name="social-dry-run")
        )

    async def _startup_dry_run(self) -> None:
        """等播种跑完再空跑：刚启动时 users 还是空的，跑早了只会得到「0 人」。"""
        try:
            await asyncio.sleep(3.0)
            await self.dry_run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[autonomous_social] 启动 dry-run 失败: {exc}")

    async def stop(self) -> None:
        """停止引擎：等后台任务真正结束后再落盘。

        原来这里是同步的：cancel 完任务就立刻 save。若某个连发任务正好卡在
        「消息已发出、record_outgoing 还没跑」的窗口里被 cancel，这次发送在状态里
        就完全没有记录——下一轮可能重复发，回复率统计也失真。取消后必须等它退出。
        """
        self.running = False
        pending = [t for t in (list(self._burst_tasks) + list(self._bg_tasks)) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._burst_tasks.clear()
        self._bg_tasks.clear()
        self._flow_inflight.clear()
        try:
            await self.input_state.stop_all()
        except Exception as exc:  # pragma: no cover - 纯装饰
            logger.debug(f"[autonomous_social] 停输入状态失败: {exc}")
        try:
            self.state.save()
            logger.info("[autonomous_social] 引擎已停止，状态已保存")
        except Exception as e:
            logger.error(f"[autonomous_social] 停止时保存状态失败: {e}")

    def _time(self) -> float:
        """当前时间（墙钟），带回拨钳制。全引擎只认这一个时间源。"""
        t = self._time_source()
        if t < self._time_high:
            return self._time_high
        self._time_high = t
        return t

    def log(self, message: str) -> None:
        """调试日志输出。"""
        if self.cfg.debug:
            logger.info(f"[autonomous_social] {message}")

    @staticmethod
    def _is_infra_note(result: str) -> bool:
        """这条「没发出去」是不是因为 LLM 基础设施坏了（而不是模型说不想说）。

        `_speak_inner` 在 provider 取不到 / 调用失败 / 生成结果为空时返回的说明只有
        `_INFRA_NOTES` 里那两种开头；模型否决与验收拦截的文案不在这里面。
        分辨它只为一件事：基础设施坏掉时，**同一轮剩下的候选不用再试**——同一个
        provider，下一个请求只会得到同样的结果。
        """
        text = str(result or "")
        return any(text.startswith(marker) or marker in text for marker in _INFRA_NOTES)

    # ─── 记忆联动（astrbot_plugin_memory_companion） ───────

    def _memory_bridge(self):
        """拿 memory_companion 的桥接对象；开关关掉 / 没装都返回 None。

        懒解析一次：插件加载顺序不保证，构造时对面可能还没就绪。
        """
        if not getattr(self.cfg, "memory_bridge_enabled", True):
            return None
        if not getattr(self, "_memory_bridge_resolved", False):
            self._memory_bridge_resolved = True
            try:
                self._memory_bridge_obj = memory_bridge.find_memory_bridge(
                    getattr(self, "context", None)
                )
            except Exception:
                self._memory_bridge_obj = None
        return getattr(self, "_memory_bridge_obj", None)

    async def _fetch_memory_note(self, umo: str, uid: str, query: str = "") -> str:
        """生成前拉这个人的长期记忆（拿不到返回空串，不影响发送）。

        `query` 给的是这次开口的**由头**，不是空串：空串等于「把这个人的记忆按时间
        倒出来」，而由头才是这次真正要谈的事——检索该跟着这件事走，记忆插件才判断得
        出哪些旧事相关（旧写法传空串，于是每次都倒出同一批最久远的条目）。
        """
        bridge = self._memory_bridge()
        if bridge is None:
            return ""
        try:
            return await memory_bridge.fetch_memory_text(bridge, umo, uid, query=query)
        except Exception:
            return ""

    def _remember_proactive(
        self, umo: str, uid: str, text: str, ts: float, about: str = ""
    ) -> None:
        """发出后把这条主动消息写回记忆库（后台任务，失败不影响主链路）。"""
        bridge = self._memory_bridge()
        if bridge is None:
            return
        try:
            task = asyncio.create_task(
                memory_bridge.write_proactive(
                    bridge, uid, text, umo=umo, ts=ts, about=about
                )
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except Exception:
            pass

    def _remember_open_closed(
        self, umo: str, uid: str, about: str, result: str = "", kind: str = "loop"
    ) -> None:
        """一件还开着的事有了结果 → 落一条长期记忆（后台任务，失败不影响主链路）。

        中间层（Core 的 `life_log`）到此就不再管它了：不再回访、也不再进注入。
        棒子在这里交给长期层——「对方终于答了」是这段关系里真实发生过的事，
        不该随账本一起消失。
        """
        bridge = self._memory_bridge()
        if bridge is None:
            return
        try:
            task = asyncio.create_task(
                memory_bridge.write_open_closed(
                    bridge, uid, about, result=result, kind=kind, umo=umo
                )
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except Exception:
            pass

    # ─── 消息观察 ───────────────────────────────────────

    async def observe(self, event: Any) -> None:
        """记录用户消息。只标记脏，不立即刷盘。

        Args:
            event: AstrBot 消息事件对象
        """
        if not self.cfg.enabled:
            return

        # 入群/退群这类 notice 不是聊天消息，不进观察账本。
        raw_obj = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw_obj, dict) and str(raw_obj.get("notice_type") or ""):
            return

        # 获取消息文本。**必须先清洗**：`message_str` 里可能夹着框架注入的内容
        # （Core 的「〔她的身体与生活 v10〕」事实块、<system_reminder>），
        # 原样存进 state 会被当成「对方说的话」喂回给模型，还会污染话题与时间锚点提取。
        raw = ""
        try:
            raw = str(getattr(event, "message_str", "") or "")
        except Exception:
            raw = ""
        msg = ""
        try:
            msg = sanitize_incoming(raw)
        except Exception:
            pass

        if not msg:
            return
        # F1/F2：指令（含被剪掉 `/` 的）与非聊天消息不入观察账本。
        if _looks_like_command(event, raw, msg):
            return

        # 群消息走群聊心流那条路（与私聊主动各自独立开关），不进 per-user 记录。
        # 注意：private_only 只管私聊侧；群聊侧由 group_* 开关控制。
        if self._detect_is_group(event):
            await self._observe_group(event, msg)
            return

        # 获取 bot_id
        bid = self._get_bot_id(event)
        if not bid:
            return
        # 本账号真实处理到了一条消息 → 它是个真实 bot（不只是 default 占位）。
        # 记下来：多 bot 同进程时，只混群的兄弟也能被 _sibling_bots 认出来。
        try:
            self.state.note_bot_id(bid)
        except Exception:
            pass

        # 获取用户 ID
        uid = self._get_sender_id(event)
        if not uid:
            return
        # 挡掉 bot 自己的消息。note_spoken 与群聊的 is_bot 都做了这个判断，只有私聊这条
        # 路漏了：适配器把自消息回灌进 EventMessageType.ALL 时（自消息回环、多端同步），
        # bot 会被建成一个「用户」，记下指向自己会话的 umo、攒念头、给自己私聊发「我好想你」
        if uid == bid:
            return
        # F2：兄弟 bot 的消息也不进私聊观察（多 bot 同进程时，别把另一台当成用户）。
        try:
            if uid in self.state.known_bot_ids():
                return
        except Exception:
            pass

        # 获取统一消息来源（用于发送）
        umo = ""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            pass

        # 迁移 default → 真实 bid（如果存在旧数据）
        if bid != "default":
            self.state.migrate_default_bot(bid)

        # 获取发送者名称
        sender_name = uid
        try:
            name = event.get_sender_name()
            if name:
                sender_name = str(name)
        except Exception:
            pass

        # 记录消息；顺带从话里找可跟进的时间锚点（「明天面试」）
        cue_text: Optional[str] = None
        cue_due = 0.0
        if self.cfg.cue_followup and self.cfg.store_message_text:
            try:
                cue_text, cue_due = extract_cue(
                    msg, self._time(), self._clock_offset_for(bid)
                )
            except Exception as e:
                self.log(f"提取时间锚点失败: {e}")
                cue_text = None
        # 没有锚点但没说完结果的事（「今天面试了」）走另一条路：过几小时回访
        loop_text: Optional[str] = None
        loop_due = 0.0
        if self.cfg.loop_enabled and self.cfg.store_message_text and not cue_text:
            try:
                loop_text, loop_due = extract_open_loop(
                    msg,
                    self._time(),
                    self._clock_offset_for(bid),
                    min_hours=self.cfg.loop_min_hours,
                    max_hours=self.cfg.loop_max_hours,
                )
            except Exception as e:
                self.log(f"提取未完话题失败: {e}")
                loop_text = None

        # 这一段全是「读改写同一个 user dict」，**必须在 per-(bot,uid) 锁里**。
        #
        # `_speak` 里有 LLM 调用（秒级），发送分支正是靠这把锁保证「observe 不会插进
        # 同一个 user dict」—— 但原来 `observe` **根本没取这把锁**：注释里承诺的保护
        # 是单向的（发送方之间互斥，observe 随时可以插进去）。不锁的后果不是崩，是
        # 数据慢慢漂：urge 少加一点、last_seen 被覆盖、conversation 顺序错乱。
        async with self._user_lock(bid, uid):
            self.state.record_incoming(
                bid,
                uid,
                msg,
                name=sender_name,
                reply_window_seconds=self.cfg.reply_window_hours * 3600,
                max_topics=max(3, self.cfg.topic_memory_count + 3),
                store_text=self.cfg.store_message_text,
                cue=cue_text,
                cue_due=cue_due,
                loop=loop_text,
                loop_due=loop_due,
                pending_window_seconds=self._pending_window(),
                hour_override=self._moment_of(bid, self._time()).hour,
            )

            # 记录/更新 umo（主动发送目标，始终跟随最近一次会话来源）
            u = self.state.user(bid, uid)
            # 时间锚点同时挂成一条由头：由头池负责挑选/消费/重试/过期，u["cue"] 只留作
            # 提示词里的「你们说好的」。少了这一步，cue 永远不会被消费——消费分支要求
            # category=="cue"，而自动心跳里的 category 永远是 "anchor"/"greet"/…，
            # 于是它会在每条主动消息的提示词里挂满 36 小时。
            if cue_text and cue_due:
                try:
                    item = anchors.add_anchor(
                        u, anchors.KIND_CUE, cue_text, due=cue_due, now=self._time()
                    )
                    # 由头池的 schema 是「每条都带 tries」：live_anchors / mark_failed
                    # 都读它。add_anchor 自己不写，别处都是在调用点补（prepare_round_anchors、
                    # derive_nag 都补了），只有这一条以前漏了——读侧虽然容错不崩，
                    # 但这条由头的重试计数与 ANCHOR_MAX_TRIES 淘汰判断就失去了意义。
                    # 这里拿到的是**已经存进 u["anchors"] 的那个 dict**，setdefault 即生效。
                    if isinstance(item, dict):
                        item.setdefault("tries", 0)
                except Exception as e:
                    self.log(f"挂时间锚点由头失败: {e}")
            # T2：对方这条里带了某件未完事的**结果** → 这件不必再回访了。
            # 中间层到此为止（不再回访、也不再进注入），但**棒子交给长期层**：
            # 「对方终于答了」是这段关系里真实发生过的事，不该随账本一起消失。
            try:
                for about in loops_resolved_by(u, msg):
                    self._drop_loop(u, about)
                    self.state.mark_dirty()
                    self._remember_open_closed(umo, uid, about, msg)
                    self.log(f"未完话题「{about[:20]}」对方已给出结果，不再回访")
            except Exception as e:
                self.log(f"关闭已了结的未完话题失败: {e}")
            if umo and u.get("umo") != umo:
                u["umo"] = umo
                self.state.mark_dirty()
            # 对方又发消息了：说明这个会话现在能通，清掉之前的发送隔离
            if _num(u.get("send_blocked_until", 0)) > 0:
                u["send_blocked_until"] = 0.0
                u["send_blocked_reason"] = ""
                self.state.mark_dirty()
        # 注意：这里不调用 save()，靠周期性 flush 落盘

    async def note_spoken(self, event: Any) -> None:
        """记下 bot 在主链路里自己说出去的那句话。

        插件原本只看得见对方说的话：「最后一句是谁说的」永远是对方的，她也永远不知道
        自己上一句问了什么——那正好是把未完话题接下去所需的全部信息。主动发出去的那句
        走 record_outgoing 记账，不经过这里（context.send_message 不进 respond 阶段）。
        """
        if not self.cfg.enabled or not self.cfg.track_own_replies:
            return
        text = self._spoken_text(event)
        if not text:
            return
        bid = self._get_bot_id(event)
        uid = self._get_sender_id(event)
        if not bid or not uid or uid == bid:
            return
        if self._detect_is_group(event):
            # bot 在群里说了话（主链路回复，多半是被 @ 后回的）：开心流关注窗口，
            # 之后窗口内群里继续聊就无需被 @ 也能接。不进 per-user 追问账本。
            if self.cfg.group_flow_enabled:
                gid = self._get_group_id(event)
                if gid:
                    now = self._time()
                    umo = ""
                    try:
                        umo = str(getattr(event, "unified_msg_origin", "") or "")
                    except Exception:
                        umo = ""
                    self.state.record_group_message(
                        bid, gid, umo, "", "", text, True, now, store_text=False,
                        is_self=True,
                    )
                    self.state.open_flow(bid, gid, now, self.cfg.flow_window_minutes * 60.0)
                    # 自己刚在主链路回了话：给心流一个「刚说完，先别接」的冷却。
                    # 开窗是为了「等群友继续聊」，不是「立刻找下一句接」——这两件事
                    # 混在一起时，观感就是「bot 前脚回完，后脚又插一句」，像主动代理。
                    self.state.hold_flow(
                        bid, gid, now, self.cfg.flow_hold_seconds
                    )
            return
        user = self.state.user(bid, uid)
        # 同一条消息可能被分几条发：跟上一条重复就不另记一次
        if str(user.get("last_spoken_text", "") or "") == text[:120]:
            return
        self.state.record_spoken(
            bid, uid, text, store_text=self.cfg.store_message_text
        )

    def consume_pending_proactive_context(self, bid: str, uid: str) -> str:
        """取出并清除待注入本轮 LLM 请求的主动消息文本。

        主动消息走 context.send_message，不经过 AstrBot 的 respond 阶段，
        因此不会自动进入会话历史。用户回复时由 on_llm_request 把它作为临时
        内容块注入；消费后立即清空，避免后续请求重复注入。
        """
        if not bid or not uid:
            return ""
        u = self.state.user(bid, uid)
        text = str(u.get("pending_proactive_context", "") or "")
        if not text:
            return ""
        u["pending_proactive_context"] = ""
        self.state.mark_dirty()
        return text

    def _generation_conversation(
        self, user: Dict[str, Any], session_history: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """合并用户/正常聊天历史与插件账本，供本次主动消息生成使用。

        AstrBot 会话库是只读来源；插件自己主动发出的消息不再写进会话库，
        但会留在 state 的 conversation 里。两者按文本去重后合并，避免会话库
        存在时把 bot 自己的主动消息覆盖掉。
        """
        limit = int(getattr(self.cfg, "context_inject_count", 10) or 0)
        if limit <= 0:
            return []

        # 账本里带 ts（`_splice_ledger_into_session` 按它插位）；会话库不带。
        ledger: List[Dict[str, Any]] = []
        for item in user.get("conversation") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "") or "").strip()
            if not text:
                continue
            ledger.append(
                {
                    "dir": "out" if item.get("dir") == "out" else "in",
                    "text": text[:300],
                    "ts": _num(item.get("ts", 0.0) or 0.0),
                }
            )
        if not session_history:
            return _strip_ts(ledger)[-limit:]

        # **按时间排序，不按来源拼接**（v1.28.1）。
        #
        # 原来是「会话库在前，账本里没有的追加到末尾」。两个来源的条目交替出现时
        # （她昨晚主动发了一句 → 用户今天回 → 她主链路回 → 用户再回），那句**主动消息
        # 会被排到用户所有回复之后**，模型看到的历史变成：
        #   对方：我也才忙完 / 我：那正好一起摸鱼 / 对方：哈哈行 / 我：刚下班今天累死了
        # 她以为「刚下班」是自己**刚说完**的话，于是接着这句往下接。用户看到的就是
        # 「她根本不知道自己在说哪一句，只会一个劲地问」。
        #
        # 去重本身没错，错的是去重之后没有恢复时间顺序。会话库的条目不带 ts
        # （AstrBot 的 history 只存 role/content），所以**不能**给它们编一个占位 ts
        # 再整体排序：整段会被一起推到账本最老一条之前或之后，顺序反而更乱。
        #
        # 做法：会话库保持原样当主干，只把账本里**它没有的**条目按 ts 插回正确位置。
        # 账本比会话库全（主动消息只在账本里），缺的正是这些；而主动消息全都带 ts，
        # 插得准。
        return _strip_ts(
            _splice_ledger_into_session(session_history, ledger, limit)
        )

    async def _load_session_history(self, umo: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """只读 AstrBot 会话库，取最近几轮用户与正常聊天记录。

        插件自己主动发出的消息不写入会话库；生成时由 _generation_conversation
        从 state 的 conversation 合并回来。条数由 context_inject_count 控制。
        """
        if limit is None:
            limit = int(getattr(self.cfg, "context_inject_count", 10) or 0)
        if limit <= 0:
            return []
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None or not umo:
            return []
        try:
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return []
            conv = await cm.get_conversation(umo, cid)
            if conv is None or not conv.history:
                return []
            history = json.loads(conv.history)
            if not isinstance(history, list):
                return []
            out: List[Dict[str, Any]] = []
            for m in history[-limit:]:
                if not isinstance(m, dict):
                    continue
                role = str(m.get("role") or "")
                content = m.get("content")
                if isinstance(content, list):
                    # 分段消息（如 qq_official 的多段 content）拼成一段纯文本
                    content = "".join(
                        str(seg.get("text", "") or "") if isinstance(seg, dict) else str(seg)
                        for seg in content
                    )
                # 会话库里存的是拼装后的消息，同样可能带框架注入块（Core 的事实块
                # 就是追加在 user 消息后面的，而 AstrBot 存的就是拼装后的那条）。
                # 不在这里洗，它会直接进生成上下文。
                text = sanitize_incoming(content)
                if not text or text.startswith("/"):
                    continue
                out.append({"dir": "out" if role == "assistant" else "in", "text": text[:300]})
            return out
        except Exception as e:
            logger.debug(f"[autonomous_social] 读取会话历史失败: {e}")
            return []

    @staticmethod
    def _spoken_text(event: Any) -> str:
        """从事件的发送结果里取出纯文本。命令回显（状态面板那种）不算她说的话。"""
        try:
            result = event.get_result()
        except Exception:
            return ""
        if result is None:
            return ""
        for name in ("is_model_result", "is_llm_result"):
            probe = getattr(result, name, None)
            if callable(probe):
                try:
                    if not bool(probe()):
                        return ""
                except Exception:
                    pass
                    continue
                break
        getter = getattr(result, "get_plain_text", None)
        if not callable(getter):
            return ""
        try:
            body = str(getter() or "").strip()
        except Exception:
            return ""
        if len(body) < 2 or body.startswith("/"):
            return ""
        return body[:400]

    @staticmethod
    def _get_bot_id(event: Any) -> str:
        """安全获取 bot ID。"""
        # 优先调用方法
        try:
            bid = event.get_self_id()
            if bid:
                return str(bid)
        except Exception:
            pass

        # 尝试属性
        for attr in ("self_id", "bot_id"):
            try:
                val = getattr(event, attr, "")
                if val:
                    return str(val)
            except Exception:
                pass

        # 尝试从 message_obj 获取
        try:
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj:
                val = getattr(msg_obj, "self_id", "")
                if val:
                    return str(val)
        except Exception:
            pass

        return "default"

    @staticmethod
    def _get_sender_id(event: Any) -> str:
        """安全获取发送者 ID。"""
        try:
            uid = event.get_sender_id()
            if uid:
                return str(uid)
        except Exception:
            pass

        # 尝试从 sender 对象获取
        try:
            sender = getattr(event, "sender", None)
            if sender:
                uid = getattr(sender, "user_id", "")
                if uid:
                    return str(uid)
        except Exception:
            pass

        # 尝试从 message_obj 获取
        try:
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj:
                sender = getattr(msg_obj, "sender", None)
                if sender:
                    uid = getattr(sender, "user_id", "")
                    if uid:
                        return str(uid)
        except Exception:
            pass

        return ""

    @staticmethod
    def _detect_is_group(event: Any) -> bool:
        """判定是否群消息。多路取证，任一权威信号说是群就按群处理。

        误判成私聊代价最大：会把一条群消息记成 per-user 记录、拿群 umo 当私聊
        目标，主动开口时把「我好想你」发进群里。所以宁可多查几处，也别漏判。
        """
        # 1. 最权威：AstrBot 的 MessageType 枚举（FriendMessage / GroupMessage）
        try:
            mt = event.get_message_type()
            token = str(
                getattr(mt, "name", "") or getattr(mt, "value", "") or mt
            ).upper()
            if "GROUP" in token or "GUILD" in token:
                return True
            if any(k in token for k in ("FRIEND", "PRIVATE", "DIRECT", "C2C")):
                return False
        except Exception:
            pass
        # 2. message_obj.group_id：群消息非空、私聊为空（官方文档明确）
        try:
            mo = getattr(event, "message_obj", None)
            if mo is not None:
                gid = getattr(mo, "group_id", None)
                if gid is None:
                    gid = getattr(mo, "group", None)
                if gid not in (None, "", 0):
                    return True
        except Exception:
            pass
        # 3. is_private_chat()：私聊 True
        try:
            ipc = event.is_private_chat()
            if isinstance(ipc, bool):
                return not ipc
        except Exception:
            pass
        # 4. is_group 布尔属性
        try:
            ig = getattr(event, "is_group", None)
            if isinstance(ig, bool):
                return ig
        except Exception:
            pass
        # 5. 最后看 unified_msg_origin 的 message_type 段
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            kind = history_ingest.umo_kind(umo)
            if kind == "group":
                return True
            if kind == "private":
                return False
        except Exception:
            pass
        return False

    @staticmethod
    def _get_group_id(event: Any) -> str:
        """安全获取群 ID（群会话的主键）。"""
        try:
            gid = event.get_group_id()
            if gid:
                return str(gid)
        except Exception:
            pass
        try:
            mo = getattr(event, "message_obj", None)
            if mo is not None:
                for attr in ("group_id", "group"):
                    val = getattr(mo, attr, None)
                    if val not in (None, "", 0):
                        return str(val)
        except Exception:
            pass
        return ""

    @staticmethod
    def _mentions_bot(event: Any, bid: str) -> bool:
        """这条群消息是不是 @ 了 bot。用作心流「有人接我插的话」的硬信号。"""
        if not bid:
            return False
        return str(bid) in SocialEngine._at_targets(event)

    @staticmethod
    def _at_targets(event: Any) -> List[str]:
        """这条消息 @ 了哪些 id（去重、原样字符串）。私聊或没有 @ 时返回空列表。

        多 bot 同群时这是唯一靠得住的「是不是在叫我」判据：
        一条消息 @ 了谁，在消息链里写得明明白白，不依赖模型猜。
        """
        at_all_cls, at_cls = SocialEngine._at_component_classes()
        out: List[str] = []
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
            for comp in chain:
                # 优先 isinstance（框架定义了类就用真类型，重命名/包装都不怕）；
                # 拿不到框架类时退到类名比对，保证只读场景也能工作。
                if at_all_cls is not None:
                    is_at_all = isinstance(comp, at_all_cls)
                    is_at = isinstance(comp, at_cls)
                else:
                    label = type(comp).__name__
                    is_at_all = label == "AtAll"
                    is_at = label == "At"
                if is_at_all:
                    # @全体成员也是「有目标」的消息：不当作没人点名的闲聊去接。
                    if "all" not in out:
                        out.append("all")
                    continue
                if not is_at:
                    continue
                val = getattr(comp, "qq", None)
                if val is None:
                    val = getattr(comp, "target", None)
                if val is None:
                    val = getattr(comp, "user_id", None)
                if val is None:
                    continue
                text = str(val)
                if text and text not in out:
                    out.append(text)
        except Exception:
            pass
        return out

    @staticmethod
    def _at_component_classes():
        """(AtAll, At) 两个框架类；导入失败时返回 (None, None) 退回类名比对。"""
        try:
            from astrbot.api.message_components import At, AtAll
        except Exception:
            try:
                from astrbot.api.message_components import At  # type: ignore
                AtAll = None  # type: ignore
            except Exception:
                return None, None
        return AtAll, At

    def _sibling_bots(self, bid: str) -> set:
        """本进程见过的其它 bot id（多 bot 同进程部署时的「兄弟」）。

        它们的消息在事件层看起来就是普通群成员，框架不会替我们区分。
        两个来源取并集：

        * 顶层 `bots` 键——有过私聊用户/被心跳结算的角色；
        * `_known_bot_ids`——本插件实例以某个 bid 真实处理过的账号（含只混群的）。

        只认确实见过的 id。各 bot 跑在不同进程/不同 state 文件时本函数恒为空——
        跨进程的兄弟认不出，README 里写清了边界。
        """
        out = set()
        try:
            for other in (self.state.data.get("bots", {}) or {}):
                other = str(other or "")
                if other and other != "default" and other != str(bid):
                    out.add(other)
        except Exception:
            pass
        try:
            for other in self.state.known_bot_ids():
                if other and other != str(bid):
                    out.add(other)
        except Exception:
            pass
        return out

    # ─── 群聊心流（v1.11.0） ───────────────────

    async def _observe_group(self, event: Any, msg: str) -> None:
        """观察一条群消息：进参考库、刷 last_seen；如果心流窗口开着且值得接，派一个后台任务去接。

        心流、破冰、参考库只要有一个开着就需要 last_seen/umo，所以只要不是全关就记录。
        """
        if not (self.cfg.group_flow_enabled or self.cfg.group_icebreak_enabled or self.cfg.group_ref_lib_enabled):
            return
        # 群消息同样可能带框架块（进了群参考库就会一直被当「这个群怎么说话」的样本）
        msg = sanitize_incoming(msg)
        bid = self._get_bot_id(event)
        gid = self._get_group_id(event)
        if not bid or not gid:
            return
        # 本账号真实处理到了群消息 → 它是个真实 bot。记下来供 _sibling_bots 识别
        # （只混群、从不私聊的兄弟也会出现在顶层 bots 之外，靠这个名单补上）。
        try:
            self.state.note_bot_id(bid)
        except Exception:
            pass
        uid = self._get_sender_id(event)
        # bot 自己的群发言不进参考库（不学自己）；主链路回复另走 note_spoken 开窗
        is_bot = bool(uid) and uid == bid
        # 兄弟 bot（同一账本里见过的其它 bot）：也不是真人。它们的消息不进风格库、
        # 不触发心流——不然多 bot 同群时，一个 bot 说话，另一个 bot 把它当群友接一句，
        # 串台就是从这儿开始的。
        is_sibling = bool(uid) and uid in self._sibling_bots(bid)
        umo = ""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            pass
        group_name = ""
        try:
            group_name = str(getattr(event, "get_group_name", lambda: "")() or "")
        except Exception:
            group_name = ""
        sender_name = ""
        try:
            sender_name = str(event.get_sender_name() or "")
        except Exception:
            sender_name = ""

        store_text = self.cfg.group_ref_lib_enabled and self.cfg.group_store_message_text
        now = self._time()
        g = self.state.record_group_message(
            bid, gid, umo, group_name, sender_name, msg, is_bot or is_sibling, now,
            store_text=store_text and not is_sibling, sample_cap=self.cfg.group_ref_sample_size,
            # 只有「我自己」不算别人开口；兄弟 bot 说话要刷 last_human_at。
            is_self=is_bot,
        )
        # 有人 @ 了 bot 且心流窗口开着：算「接住了我刚插的话」，把连着插没人理的计数清零，
        # 允许继续接。不是 @ 的普通群聊不算接话（避免一有人说话就当受欢迎继续无脑插）。
        if (
            not is_bot
            and not is_sibling
            and self._mentions_bot(event, bid)
            and _num(g.get("flow_open_until", 0) or 0) > now
        ):
            self.state.note_flow_pickup(bid, gid)
        # 心流：窗口开着、不是 bot（自己和兄弟都算）发的、**没有任何 @**、预筛过了，
        # 才去试着接一句。
        #
        # 「无艾特接话」的字面意思就是接没人点名的话：
        # - 消息 @ 了别人（哪怕不是本 bot）→ 那是对着某个具体的人说的，不掺和；
        # - 消息 @ 了本 bot → 主链路会正经回它，心流再插一句就是同一个人连说两遍；
        # - 多 bot 同群时，@ 还常常是「另一台 bot 在跟它说话」的信号，接了就串台。
        if not self.cfg.group_flow_enabled or is_bot or is_sibling:
            return
        if self._at_targets(event):
            return
        if _num(g.get("flow_open_until", 0) or 0) <= now:
            return
        if not groupflow.flow_prefilter(msg, is_command=False):
            return
        is_quiet, asleep = _person_flow_state(self, bid, now)
        # 上一波「插了没人理」的账要能随时间作废，否则这个群会被**永久**钉在退出阈值上：
        # 计数只由「新开窗」或「被 @」清零，而不再插话就永远不会新开窗。
        # 放在这里而不是闸门里 —— `flow_should_consider` 是纯函数，改状态得留在状态层。
        self.state.forgive_stale_flow_ignored(bid, gid, now)
        # 语境：她要看这个群**现在在干什么**，而不是只盯着这一条消息。
        # 近几条真人发言（去掉她自己）用来判断「是不是两个人在对聊」；本条消息开头
        # 如果叫了某个群友的名字，那它就是在叫那个人。S3（v1.28.0）：这两件事不再
        # 硬拦，而是写成 `context_note` 放进提示词交给模型判断——用户原话
        # 「不是必须，但是能不能插嘴看模型」。
        speakers = [
            str(s.get("name", "") or "").strip()
            for s in (g.get("samples") or [])
            if not s.get("self") and str(s.get("name", "") or "").strip()
        ]
        addressed = groupflow.addressed_to(msg, speakers)
        context_note = groupflow.flow_context_note(speakers, addressed)
        ok, why = groupflow.flow_should_consider(
            g, now,
            max_replies=self.cfg.flow_max_replies_per_window,
            min_gap_seconds=self.cfg.flow_min_gap_seconds,
            hourly_cap=self.cfg.flow_hourly_cap,
            hour_count=self.state.flow_hour_count(g, now),
            ignored_exit=self.cfg.flow_ignored_exit,
            is_quiet=is_quiet,
            asleep=asleep,
            interject_enabled=self.cfg.group_interject_others,
            context_note=context_note,
            # 别人对聊时，刚插过话就先缓一缓：至少是这个 Bot 自己的最小间隔，
            # 并且不短于 3 分钟——上一句刚落就再扎进两个人的对话里最招人烦。
            interject_gap_seconds=max(self.cfg.flow_min_gap_seconds, 180),
            # 同上：尝试也要节流，别让每条群消息都重打一次 LLM。
            attempt_gap_seconds=self._flow_attempt_gap(),
        )
        if not ok:
            self.log(f"群 {gid} 心流本轮不接：{why}")
            return
        if context_note:
            # 日志要能区分「两人对聊但交给模型判断」与「配置关闭不插话」——
            # 后者在 `why` 那条日志里已经写明了。
            self.log(f"群 {gid} 心流：{context_note}；交给模型判断要不要插")
        # 同一个群已有一条接话在生成中：先挣着。不然这条会抢在上一条落账前
        # 过闸（计数只在 note_flow_reply 里才 +1），两条同时发出就派了 min_gap。
        # 检查与标记都在同一个事件循环步里、create_task 前无 await → 原子。
        if (bid, gid) in self._flow_inflight:
            self.log(f"群 {gid} 心流本轮不接：上一条还在生成中")
            return
        self._flow_inflight.add((bid, gid))
        # 后台任务：不阻塞观察链路；生成前会再校一次闸
        task = asyncio.create_task(self._flow_reply(bid, gid, now))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _flow_attempt_gap(self) -> float:
        """心流两次**尝试**之间至少隔多久（秒）。

        与 `flow_min_gap_seconds`（两句真正发出去的话之间）分开：那个数的是
        「说出去的话别太密」，这个数的是「别把 LLM 当免费的打」。

        取 `flow_min_gap_seconds` 与一个下限的较大者：下限 60 秒——再短的话
        活跃群里两条消息就能打一次模型，限流期间尤其浪费。
        没有单独配置项：复用现有那个即可，主人要调节奏时改的还是同一个数。
        """
        try:
            base = float(getattr(self.cfg, "flow_min_gap_seconds", 120) or 120)
        except (TypeError, ValueError):
            base = 120.0
        return max(base, 60.0)
    async def _flow_reply(self, bid: str, gid: str, trigger_ts: float) -> None:
        """包一层 in-flight 释放：无论生成/发送成不成功、中途抛不抛，都把
        该群的在途标记拿掉，让后续消息能重新触发（此时计数/min_gap 已落账）。"""
        try:
            await self._flow_reply_inner(bid, gid, trigger_ts)
        finally:
            self._flow_inflight.discard((bid, gid))

    @staticmethod
    def _verify_all_segments(segments, *, recent, called, allow_short=False,
                             require_about_peer=False):
        """逐段过验收，返回 (不过的那段正文, 原因)；全过返回 ("", "")。

        `parts[0]` 只是模型输出的第一段；`plan_segments` 拆出来的后续段以前一律
        免检直接补发，动机泄漏/括号截断/复读都可能从那里进群（或单独发给对方）。
        验收要盖住**每一条真正发出去的段**。
        """
        for text in segments:
            body = str(text or "")
            passed, reason = verify_message(
                body, recent=recent, called=called, allow_short=allow_short,
                require_about_peer=require_about_peer,
            )
            if not passed:
                return body, reason
        return "", ""

    async def _flow_reply_inner(self, bid: str, gid: str, trigger_ts: float) -> None:
        """在关注窗口内无 @ 接一句。生成前重校闸（状态可能变了），模型可选择不接。"""
        if not self.running or not self.cfg.enabled or not self.cfg.group_flow_enabled:
            return
        breaker = self.send_breaker_left(bid)
        if breaker > 0:
            # 群消息比心跳密得多：熔断期间每条群消息都会走到这里，不早退就是一轮群消息
            # 一次 LLM。而现在发不出去，写了也白写。
            return
        now = self._time()
        g = self.state.group(bid, gid)
        umo = str(g.get("umo", "") or "")
        if not umo:
            return
        is_quiet, asleep = _person_flow_state(self, bid, now)
        # 上一波「插了没人理」的账要能随时间作废，否则这个群会被**永久**钉在退出阈值上：
        # 计数只由「新开窗」或「被 @」清零，而不再插话就永远不会新开窗。
        # 放在这里而不是闸门里 —— `flow_should_consider` 是纯函数，改状态得留在状态层。
        self.state.forgive_stale_flow_ignored(bid, gid, now)
        # 生成前重校也要看语境：生成要几秒，这几秒里群里可能已经变成两个人对聊、
        # 或者有人开始叫别人了。S3（v1.28.0）起这两件事不再直接否掉，而是把**最新**
        # 语境带进提示词——模型看到的是当下这个群，而不是几秒前的快照。
        # 触发消息的原文不在这个函数里，只能从 samples 里取最新一条真人发言来判
        # 「这句话是不是在叫谁」。
        speakers = [
            str(s.get("name", "") or "").strip()
            for s in (g.get("samples") or [])
            if not s.get("self") and str(s.get("name", "") or "").strip()
        ]
        latest_human = ""
        for sample in reversed(g.get("samples") or []):
            if sample.get("self"):
                continue
            latest_human = str(sample.get("text", "") or "")
            break
        context_note = groupflow.flow_context_note(
            speakers, groupflow.addressed_to(latest_human, speakers)
        )
        ok, why = groupflow.flow_should_consider(
            g, now,
            max_replies=self.cfg.flow_max_replies_per_window,
            min_gap_seconds=self.cfg.flow_min_gap_seconds,
            hourly_cap=self.cfg.flow_hourly_cap,
            hour_count=self.state.flow_hour_count(g, now),
            ignored_exit=self.cfg.flow_ignored_exit,
            is_quiet=is_quiet,
            asleep=asleep,
            interject_enabled=self.cfg.group_interject_others,
            context_note=context_note,
            interject_gap_seconds=max(self.cfg.flow_min_gap_seconds, 180),
            # 「模型选择不接」也要占一次尝试：不然活跃群里每条消息都重打一次 LLM。
            attempt_gap_seconds=self._flow_attempt_gap(),
        )
        if not ok:
            return
        group_ctx = self._build_group_ctx(bid, gid, g)
        group_ctx["now"] = now
        group_ctx["clock_offset"] = self._clock_offset_for(bid)
        group_ctx["context_note"] = context_note
        if context_note:
            self.log(f"群 {gid} 心流：{context_note}；交给模型判断要不要插")
        try:
            _persona_name, persona_prompt = await self._persona(umo, bid)
        except Exception as e:
            self.log(f"群 {gid} 解析人格失败: {e}")
            persona_prompt = ""
        # 记一次尝试：无论模型答不答应，这一轮都算试过。
        # 少了它，「模型选择不接」就不占任何配额，活跃群里会一条消息一次 LLM。
        self.state.note_flow_attempt(bid, gid, now)
        try:
            parts = await self.generator.group_message(
                umo, "flow", group_ctx, persona_prompt,
                at=now, clock_offset=self._clock_offset_for(bid),
            )
        except Exception as e:
            if throttle.allow(f"flow.generate.{bid}"):
                logger.warning(
                    f"[autonomous_social] 群心流生成失败: {e}" + throttle.summary(f"flow.generate.{bid}")
                )
            return
        if not parts:
            self.log(f"群 {gid} 心流：模型选择不接或生成为空")
            return
        # ── 验收层（与私聊侧同一套五道检查）────────────────────
        # 群心流以前没有任何验收：模型写什么就发什么，含动机泄漏、括号截断、
        # 复读（同一晚 23 条一个调子）的毛病都可能直接进群。与私聊侧对齐：
        # 判不过就不发（不重生成、不退回）——宁可不接，不可发一条废话。
        # 先拆段再逐段验：只验 parts[0] 的话，后续补发的段等于免检。
        # 近期发过的话用她在这个群自己说过的，防接话与自己的上一句重复。
        try:
            own_recent_texts = [
                str(t).strip()
                for t in (group_ctx.get("own_recent") or [])
                if str(t).strip()
            ]
        except Exception:
            own_recent_texts = []
        called = str(self._last_persona.get(bid, "") or "")
        # 拆不拆、隔多久：跟这个 Bot 的分段回复设置。模型已经分过的段（---）也一并
        # 交给它定：主人没开分段就合并回一条，开了才按框架的拆法与间隔发。
        segments, waits = pacing.plan_segments(
            parts, umo, self.context,
            allow_burst=bool(getattr(self.cfg, "allow_burst", True)),
        )
        if not segments:
            return
        bad_text, flow_why = self._verify_all_segments(
            segments, recent=own_recent_texts, called=called, allow_short=True,
        )
        if bad_text:
            self._m["rejected_total"] = int(self._m.get("rejected_total", 0)) + 1
            self._m["last_rejected"] = str(bad_text)[:200]
            if throttle.allow(f"flow.rejected.{bid}", window=1800.0):
                logger.info(
                    f"[autonomous_social][{bid}] 群 {gid} 心流验收不过，未发送：{flow_why}｜"
                    f"原文：{str(bad_text)[:120]!r}"
                    + throttle.summary(f"flow.rejected.{bid}")
                )
            self.log(f"群 {gid} 心流验收不过（{flow_why}），不发")
            return
        ok_send, err, peer_unreachable = await self._send_with_retry(umo, segments[0], bid)
        if not ok_send:
            if peer_unreachable:
                self.state.block_group(bid, gid, self._time() + SEND_BLOCK_HOURS * 3600.0, err)
                logger.warning(f"[autonomous_social] 群 {gid}（bid={bid}）发送失败，隔离 {SEND_BLOCK_HOURS}h：{err}")
            else:
                self.log(f"群 {gid} 心流发送失败：{err}")
            return
        sent_ts = self._time()
        self.state.note_flow_reply(
            bid, gid, sent_ts, segments[0],
            sample_cap=self.cfg.group_ref_sample_size,
            store_text=self.cfg.group_store_message_text,
        )
        self.state.save()
        self.log(f"群 {gid} 心流接话→ {segments[0]}")
        # 同一句拆成了多段：剩下的快速补发。算同一次插话（不再各自计额/过闸）。
        if len(segments) > 1:
            self._schedule_group_burst(bid, gid, umo, segments[1:], waits, sent_ts)

    def _build_group_ctx(self, bid: str, gid: str, g: Dict[str, Any]) -> Dict[str, Any]:
        """给生成侧凑群上下文：风格参考 + 最近几条群友发言 + 上一句心流接的话。"""
        samples = g.get("samples", []) or []
        n = max(1, int(getattr(self.cfg, "context_inject_count", 10) or 10))
        recent = [
            {
                "name": str(s.get("name", "") or ""),
                "text": str(s.get("text", "") or ""),
                "self": bool(s.get("self")),
            }
            for s in samples[-n:]
            if str(s.get("text", "") or "").strip()
        ]
        style_ref = ""
        if self.cfg.group_ref_lib_enabled:
            style_ref = groupflow.build_style_reference(
                self.state.group_samples(bid, gid, self.cfg.group_ref_prompt_count)
            )
        # 她自己最近在这个群里说过的话（最新在前）。以前只传最后一条
        # `last_flow_reply_text`，连发的后续句读不到——模型以为自己上一句还是第一条，
        # 于是接话要么重复、要么跟自己的最新句对不上。
        own_recent: List[str] = []
        for s in reversed(samples):
            if not s.get("self"):
                continue
            text = str(s.get("text", "") or "").strip()
            if not text:
                continue
            if text in own_recent:
                continue
            own_recent.append(text)
            if len(own_recent) >= 3:
                break
        # 最新一条群友发言（不是她自己的）：接话建议要指向它。
        latest: Dict[str, Any] = {}
        for s in reversed(samples):
            if s.get("self"):
                continue
            text = str(s.get("text", "") or "").strip()
            if not text:
                continue
            latest = {"name": str(s.get("name", "") or ""), "text": text}
            break
        return {
            "recent": recent,
            "style_ref": style_ref,
            # 她在这个群自己说过的话：用不用 emoji 由此判定，不是配置开关
            "emoji": emoji_habit(
                [str(s.get("text", "") or "") for s in samples if s.get("self")]
            ),
            "last_flow_text": str(g.get("last_flow_reply_text", "") or ""),
            "own_recent": own_recent,
            "latest": latest,
            "group_name": str(g.get("name", "") or ""),
            # G3：近期在群里说过话的人名（供「自然地点一下某个人」）。
            "members": groupflow.member_names(samples) if self.cfg.group_mention_member else [],
        }

    def _group_topic_hint(
        self, bid: str, gid: str, g: Dict[str, Any], now: float
    ) -> Tuple[str, str]:
        """K1/S2：破冰/起话头可用的现成素材。返回 (素材文本, 本次采用的天气)。

        素材以**她自己的生活**优先（`groupflow.life_lines`：刚做完/正在做/今天想去/
        还没做完/昨晚梦到），节日其次，天气最后。天气必须过
        `anchors.weather_worth_telling` 的「变了 + 6 小时未说过」闸——天气永远有，
        不过闸就成了唯一话头（用户原话：「只有天气，不会真的找话题」）。

        天气的去重存在**群状态** `g` 里（不是用户状态）：同一个群连着破冰不该反复
        说同一句天气；发送成功后由 `_icebreak` 调 `mark_weather_told` 落账。
        返回空天气表示这次没采用天气（或者压根没天气）。
        """
        try:
            body = self.core.bot_self_state(bid) or {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        life = groupflow.life_lines(body)
        holiday = str(body.get("holiday") or "").strip()
        raw_weather = body.get("weather")
        if isinstance(raw_weather, dict):
            weather = str(raw_weather.get("env") or raw_weather.get("weather") or "").strip()
        else:
            weather = str(raw_weather or "").strip()
        weather_used = ""
        if weather:
            # 闸的返回非空 = 这次可以说；用**完整原句**而不是截断版当素材，
            # 免得「外面下着很大的雷阵雨」被切成半句。
            worth = anchors.weather_worth_telling(
                g, {"weather": {"env": weather}}, now
            )
            if worth:
                weather_used = weather
        return (
            groupflow.topic_materials(
                life=life, holiday=holiday, weather=weather_used
            ),
            weather_used,
        )

    async def _run_icebreaks(self, bots: List[str], now: float) -> None:
        """每个角色一轮最多给一个冷得最久的群破冰（别一口气把好几个群都点一遍）。

        每个角色包在自己的 try 里（与 `try_once` 的 per-bot 隔离同一套写法）：
        一个坏群条目、一条脏群记录抛出来，只该让这个角色这一轮不破冰，
        不该掀翻整轮心跳——异常逃到 `run()` 会触发崩溃退避（30s→30min），
        而且每轮复发，表现就是**所有角色一起永久安静**。
        """
        for bid in bots:
            try:
                due = self._icebreak_pick(bid, now)
                if not due:
                    continue
                due.sort(key=lambda item: _num(item[1].get("last_seen", 0) or 0))
                gid, g = due[0]
                await self._icebreak(bid, gid, g, now)
            except Exception as e:
                if throttle.allow(f"icebreak.round.{bid}"):
                    logger.warning(
                        f"[autonomous_social][{bid}] 本轮破冰出错，这个角色这轮不破冰"
                        f"（其它角色照常）：{e}" + throttle.summary(f"icebreak.round.{bid}")
                    )

    def _icebreak_pick(self, bid: str, now: float) -> List[Tuple[str, Dict[str, Any]]]:
        """这个角色名下，哪些群冷场到该破冰了。返回 [(gid, g)]。"""
        if not self.cfg.group_icebreak_enabled:
            return []
        dt = self._moment_of(bid, now)
        # 安静时段与「她是不是在睡」都走人物侧那道闸（与私聊/心流同一个口径，见
        # `_flow_person_state`）。原来这里只查 quiet，漏了 asleep：深夜群里有人说话，
        # 她照样去破冰——而「别在人家睡着时把人叫醒闲聊」正是这道闸要挡的事。
        is_quiet, asleep = _person_flow_state(self, bid, now)
        today = dt.strftime("%Y-%m-%d")
        out: List[Tuple[str, Dict[str, Any]]] = []
        for gid, g in self.state.groups(bid).items():
            # 群条目可能是 null/非 dict（手改、旧版残留、外部工具写入）：
            # 直接 `g.get()` 会让整个角色这一轮破冰报废，跳过这一个就好。
            if not isinstance(g, dict) or not g.get("umo"):
                continue
            if groupflow.icebreak_due(
                g, now,
                idle_hours=self.cfg.group_idle_hours,
                daily_cap=self.cfg.icebreak_daily_cap,
                stale_days=self.cfg.group_stale_days,
                today=today,
                is_quiet=is_quiet,
                asleep=asleep,
            ):
                out.append((gid, g))
        return out

    async def _run_group_topics(self, bots: List[str], now: float) -> None:
        """G1：每个角色一轮最多在一个群里主动起个话头（不只在冷场时）。

        与 `_run_icebreaks` 同样按角色隔离：这条 await 在 `try_once` 末尾，
        异常逃出去就是整轮丢失 + 崩溃退避，而且每轮复发。
        """
        for bid in bots:
            try:
                due = self._group_topic_pick(bid, now)
                if not due:
                    continue
                # 静得最久的那个群优先
                due.sort(key=lambda item: _num(item[1].get("last_bot_spoke", 0) or 0))
                gid, g = due[0]
                await self._icebreak(bid, gid, g, now, mode="topic")
            except Exception as e:
                if throttle.allow(f"grouptopic.round.{bid}"):
                    logger.warning(
                        f"[autonomous_social][{bid}] 本轮起话头出错，这个角色这轮不开口"
                        f"（其它角色照常）：{e}" + throttle.summary(f"grouptopic.round.{bid}")
                    )

    def _group_topic_pick(self, bid: str, now: float) -> List[Tuple[str, Dict[str, Any]]]:
        """这个角色名下，哪些群该由她主动起个话头。返回 [(gid, g)]。"""
        if not self.cfg.group_topic_enabled:
            return []
        dt = self._moment_of(bid, now)
        # 同上：起话头也要看她是不是在睡，不然半夜群里冒一句「今天怎么样」。
        is_quiet, asleep = _person_flow_state(self, bid, now)
        today = dt.strftime("%Y-%m-%d")
        out: List[Tuple[str, Dict[str, Any]]] = []
        for gid, g in self.state.groups(bid).items():
            # 同上：坏群条目只跳过它自己，别让整个角色这一轮起不了话头。
            if not isinstance(g, dict) or not g.get("umo"):
                continue
            if groupflow.group_topic_due(
                g, now,
                gap_hours=self.cfg.group_topic_gap_hours,
                daily_cap=self.cfg.group_topic_daily_cap,
                stale_days=self.cfg.group_stale_days,
                today=today,
                is_quiet=is_quiet,
                asleep=asleep,
            ):
                out.append((gid, g))
        return out

    async def _icebreak(self, bid: str, gid: str, g: Dict[str, Any], now: float,
                        *, mode: str = "icebreak") -> bool:
        """向一个冷群抛一句破冰，或（G1，mode="topic"）主动起个话头。发出返回 True。

        两种走同一套生成/验收/发送/落账，差别只在提示词的框（群已经安静了一阵 vs
        她自己想开个新话头）。
        """
        umo = str(g.get("umo", "") or "")
        if not umo:
            return False
        if self.send_breaker_left(bid) > 0:
            return False
        group_ctx = self._build_group_ctx(bid, gid, g)
        group_ctx["now"] = now
        group_ctx["clock_offset"] = self._clock_offset_for(bid)
        if mode == "icebreak":
            group_ctx["reason"] = groupflow.icebreak_reason()
        # K1/S2：把「她自己的生活 + 今天现成的事」递进去（没有就空着，不硬编）。
        # 天气由 `_group_topic_hint` 过「变了 + 6 小时」的闸，并带回来这次采用的
        # 那一句：发送成功后才记账（见下面 mark_weather_told）。
        group_ctx["topic_hint"], weather_used = self._group_topic_hint(bid, gid, g, now)
        try:
            _persona_name, persona_prompt = await self._persona(umo, bid)
        except Exception as e:
            self.log(f"群 {gid} 解析人格失败: {e}")
            persona_prompt = ""
        try:
            parts = await self.generator.group_message(
                umo, mode, group_ctx, persona_prompt,
                at=now, clock_offset=self._clock_offset_for(bid),
            )
        except Exception as e:
            if throttle.allow(f"{mode}.generate.{bid}"):
                logger.warning(
                    f"[autonomous_social] 群{'破冰' if mode == 'icebreak' else '起话头'}生成失败: {e}"
                    + throttle.summary(f"{mode}.generate.{bid}")
                )
            return False
        if not parts:
            # 模型判断这次没有值得分享的事（S2：没话说就不发）。**不消耗今天的额度**
            # ——用户要的是「没有能说的就不发」，不是「把额度用掉也不发」；
            # 也不该生成一句占位内容。
            self.log(
                f"群 {gid} {'破冰' if mode == 'icebreak' else '起话头'}："
                "模型决定这次不说，不占今天的次数"
            )
            return False
        # ── 验收层（与私聊侧、心流同一套）──────────────────────
        # 破冰以前也是裸发：模型写出动机泄漏、括号截断、复读（连着几天同一句）
        # 都会直接进群。判不过就不发——宁可不破冰，不可抛一句废话头。
        # 先拆段再逐段验，后续补发的段不再免检。
        segments, waits = pacing.plan_segments(
            parts, umo, self.context,
            allow_burst=bool(getattr(self.cfg, "allow_burst", True)),
        )
        if not segments:
            return False
        try:
            bad_text, ice_why = self._verify_all_segments(
                segments,
                recent=[str(t).strip() for t in (group_ctx.get("own_recent") or []) if str(t).strip()],
                called=str(self._last_persona.get(bid, "") or ""),
            )
        except Exception:
            bad_text, ice_why = "", ""
        if bad_text:
            self._m["rejected_total"] = int(self._m.get("rejected_total", 0)) + 1
            self._m["last_rejected"] = str(bad_text)[:200]
            if throttle.allow(f"icebreak.rejected.{bid}", window=1800.0):
                logger.info(
                    f"[autonomous_social][{bid}] 群 {gid} 破冰验收不过，未发送：{ice_why}｜"
                    f"原文：{str(bad_text)[:120]!r}"
                    + throttle.summary(f"icebreak.rejected.{bid}")
                )
            return False
        ok_send, err, peer_unreachable = await self._send_with_retry(umo, segments[0], bid)
        if not ok_send:
            if peer_unreachable:
                self.state.block_group(bid, gid, self._time() + SEND_BLOCK_HOURS * 3600.0, err)
                logger.warning(f"[autonomous_social] 群 {gid}（bid={bid}）破冰发送失败，隔离 {SEND_BLOCK_HOURS}h：{err}")
            else:
                # 非「发不了」的失败（被禁言、群被风控、ActionFailed 1200）原来**一点
                # 退避都不记**，于是下一轮心跳又给这个群写一遍完整 LLM、又失败一次。
                # 线上实测 19:43 两个群连续 retcode=1200，就这样把角色的熔断撞开了。
                # 现在按群退避，逐次翻倍、封顶 8 小时；成功后清零。
                g = self.state.group(bid, gid)
                try:
                    n = int(g.get("fail_count", 0) or 0) + 1
                except (TypeError, ValueError):
                    n = 1
                g["fail_count"] = n
                delay = min(
                    SEND_FAIL_COOLDOWN_BASE * (2 ** min(n - 1, 6)),
                    SEND_FAIL_COOLDOWN_MAX,
                )
                self.state.block_group(
                    bid, gid, self._time() + delay, err or "发送失败"
                )
                self.log(
                    f"群 {gid} 破冰发送失败，{int(delay / 60)} 分钟后再试：{err}"
                )
            return False
        try:
            self.state.group(bid, gid)["fail_count"] = 0
        except Exception:
            pass
        sent_ts = self._time()
        day = self._moment_of(bid, sent_ts).strftime("%Y-%m-%d")
        if mode == "icebreak":
            self.state.note_icebreak(bid, gid, sent_ts, day)
        else:
            self.state.note_group_topic(bid, gid, sent_ts, day)
        if weather_used:
            # 与私聊由头同口径：真发出去了才算「说过天气」，6 小时内不再拿天气当
            # 话头。记在群状态上（`g` 随后会被 state.save() 落盘）。
            anchors.mark_weather_told(g, {"weather": {"env": weather_used}}, sent_ts)
        # 开口也是“bot 在群里说了话”：开心流关注窗口，后面有人搭话就能接
        self.state.open_flow(bid, gid, sent_ts, self.cfg.flow_window_minutes * 60.0)
        # 把内容记进她的近期发言：下一次的 prompt 靠 `own_recent`
        # 看到「前几回抛过什么话头」，否则会逐字重复。
        self.state.record_group_self_text(
            bid, gid, segments[0], sent_ts,
            sample_cap=self.cfg.group_ref_sample_size,
            store_text=self.cfg.group_store_message_text,
        )
        self.state.save()
        self.log(f"群 {gid} {'破冰' if mode == 'icebreak' else '起话头'}→ {segments[0]}")
        if len(segments) > 1:
            self._schedule_group_burst(bid, gid, umo, segments[1:], waits, sent_ts)
        return True

    # ─── 她的时钟 ─────────────────────────────────────

    def _core_facade(self) -> Any:
        """取 Core 的只读门面；拿不到返回 None（调用方退回读 state.json）。

        `use_core_facade`：
        * `auto`（默认）——找到就用，找不到静默退回，**行为与旧版一致**；
        * `on`——只走门面（验证用），找不到也不退回；
        * `off`——完全不碰门面（逃生门，与 1.27.38 逐字一致）。

        这里只在第一次真的去查注册表；之后走 `core_facade` 自己的缓存（含负缓存重试）。
        """
        mode = str(getattr(self.cfg, "use_core_facade", "auto") or "auto").lower()
        if mode == "off":
            return None
        if self._facade is not None:
            return self._facade
        if self._facade_tried and mode != "on":
            return None
        self._facade_tried = True
        try:
            self._facade = core_facade.find_core_facade(self.context)
        except Exception as exc:  # 门面这一路坏掉不该影响任何既有行为
            self.log(f"门面解析失败（退回读文件）: {exc}")
            self._facade = None
        return self._facade

    def _remember_clock(self, bid: str, body: Optional[Dict[str, Any]]) -> None:
        """从 Core 记下她所在城市的时区偏移。

        优先问门面（现算，不会旧）；门面不在时退回契约快照里的 `clock_offset_minutes`。
        两条路拿到的是同一个量，所以 `off` 模式下的行为与旧版逐字一致。
        """
        if not self.cfg.use_core_clock:
            return
        offset = None
        facade = self._core_facade()
        if facade is not None:
            clock = core_facade.clock(facade, bid)
            offset = clock.get("utc_offset_minutes")
        if offset is None:
            if not body:
                return
            offset = body.get("clock_offset_minutes")
        if offset is None:
            return
        try:
            value = int(offset)
        except (TypeError, ValueError):
            return
        if -24 * 60 <= value <= 24 * 60 and self._city_offset.get(bid) != value:
            self._city_offset[bid] = value

    def _moment_of(self, bid: str, now: float) -> datetime:
        """她那里现在是几点。接了 Core 就按她所在城市的 UTC 偏移算（不受容器时区影响），
        没接到时退回本机/内置时间。注意 offset 可能为 0（如伦敦/UTC+0 城市），
        不能拿 0 当「没 Core」——只有 None 才退本机。
        """
        offset = self._city_offset.get(bid) if self.cfg.use_core_clock else None
        return city_now(now, offset)

    def _clock_offset_for(self, bid: str) -> Optional[int]:
        """本角色用的时区偏移（分钟）；None 表示没有契约可参考，用本机时间。"""
        if not self.cfg.use_core_clock:
            return None
        return self._city_offset.get(bid)

    def _refresh_clocks(self) -> None:
        """状态展示前补一次时区：主循环还没跑过时 _city_offset 是空的。"""
        try:
            root = self.core.read_root()
        except Exception:
            return
        if not root:
            return
        for bid in self.state.data.get("bots", {}):
            try:
                self._remember_clock(bid, self.core.bot_self_state(bid, root=root))
            except Exception:
                continue

    # ─── 念头结算与选人 ───────────────────────────────

    @staticmethod
    def _sync_peer_said(u: Dict[str, Any], said: Any) -> None:
        """把 Core 记的「TA 说过的一句原话」同步进用户状态，供由头派生用。

        Core 契约里 `user.said` 是 `[{"said": 原话, "at": epoch}]`（短摘录、最新在前），
        正是「想起你之前说的X」这条由头要的素材；而 `anchors._said_recent` 读的是
        本插件用户状态里的 `[{"said": …, "ts": …}]`。两边一直没接上：插件从不写
        `u["said"]`（Core 的 said 只进生成侧的 `u_copy["_said"]`），于是这条由头来源
        从来没产出过。这里按 `_said_recent` 认的形状落一份，时间键从 `at` 映射到 `ts`。
        """
        if not isinstance(said, list):
            return
        fresh: List[Dict[str, Any]] = []
        for item in said:
            if not isinstance(item, dict):
                continue
            text = str(item.get("said") or "").strip()
            if not text:
                continue
            try:
                ts = _num(item.get("at") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            fresh.append({"said": text, "ts": ts})
        if fresh != u.get("said"):
            u["said"] = fresh

    def settle_minds(
        self,
        bid: str,
        now: float,
        dt: datetime,
        core_root: Optional[Dict] = None,
        bot_state: Optional[Dict] = None,
        min_urge: float = desire.FIRE_THRESHOLD,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """把该角色下所有用户的念头结算到现在，返回已经攒满的人（按 urge 降序）。

        这一层取代旧版的「掷骰子 + 加权随机」：不再每轮抽一个人看看能不能发，而是
        谁想说的念头真攒起来了就轮到谁。在意程度（interest）把好感度、聊过多少、对
        方接不接话、被冷落几次合到一个数里，所以不同人的节奏天然不同。
        """
        users = self.state.bot(bid).get("users", {})
        if not users:
            return []

        energy = bot_state.get("energy") if bot_state else None
        social_energy = bot_state.get("social_energy") if bot_state else None
        mood = desire.mood_multiplier(
            energy,
            social_energy,
            energy_threshold=self.cfg.energy_threshold,
            social_threshold=self.cfg.social_energy_threshold,
            body=bot_state,
        ) * self.cfg.mood_scale
        if self.cfg.weekend_boost and dt.weekday() >= 5:
            mood *= WEEKEND_MOOD_BOOST
        # 记下 Core 算出的生物钟夜：本函数的 bot_state 由 try_once 传进来，
        # 安静时段要用它判（见 _quiet_now）
        self._remember_night(bid, bot_state)
        quiet = self._quiet_now(bid, dt.hour)
        reply_window = self._pending_window()

        ready: List[Tuple[str, Dict[str, Any], float]] = []
        for uid, u in users.items():
            # 一个用户坏掉不能带走这一轮里其它用户。下面的每一步都在读/写这个人的
            # 持久化字段（settle_pending / decay_streak / interest_level / settle），
            # 而 state.json 里的字段可能被手改或跨版本迁移写坏；`u` 本身甚至可能是
            # null（非 dict）。以前这里没有 try：一次 ValueError/AttributeError 直接
            # 逃出 settle_minds，整个角色这一轮**所有人**都不结算（try_once 外面那道
            # per-bot 兜底只保证别的角色照常）。
            #
            # 但不静默吞掉：记一次数、打一条限流日志——「哪个人坏了、坏在哪」必须查得到，
            # 否则表现只是「有些人永远收不到消息」，没人能排。
            try:
                if not isinstance(u, dict):
                    # state.json 里这个人被写坏了（null/字符串/数字）：这不是「没配 umo」
                    # 那种正常跳过，扔进下面的异常分支记一笔，别让这个人无声无息地永不结算。
                    raise TypeError(f"用户状态不是 dict：{type(u).__name__}")
                if not u.get("umo"):
                    continue
                if self.state.is_send_blocked(bid, uid, now):
                    # 发送隔离期内（非好友/不支持主动消息等）：不攒念头、不选中，避免反复撞失败
                    continue

                # 上次主动联系有没有被接住（对方再也没回的情况在这里结算）
                self.state.settle_pending(bid, uid, now, reply_window)
                # 被冷落封顶后晾了很久：把冷落计数往回退，让她「算了再找一次」，
                # 而不是从此对这个人永久静默
                desire.decay_streak(u, now)

                affection = None
                if core_root is not None:
                    try:
                        snap = self.core.load_snapshot(bid, uid, root=core_root)
                        if snap:
                            affection = snap.get("affection")
                            self._sync_peer_said(u, snap.get("said"))
                    except Exception as e:
                        logger.debug(f"[autonomous_social] 加载用户 {uid} Core 快照失败: {e}")

                target = desire.interest_level(
                    u, affection, weigh_reply_rate=self.cfg.adaptive_reply_rate
                )
                desire.smooth_interest(u, target, now)

                rhythm = (
                    desire.rhythm_factor(u, dt.hour)
                    if self.cfg.respect_user_rhythm
                    else 1.0
                )
                cue = live_cue(u, now) if self.cfg.cue_followup else None
                urge = desire.settle(
                    u,
                    now,
                    refill_hours=self.cfg.urge_refill_hours,
                    recent_talk_seconds=self.cfg.recent_talk_minutes * 60,
                    mood_factor=mood,
                    rhythm=rhythm,
                    quiet=quiet,
                    live_cue=bool(cue),
                )
                # 念头天花板：只防长期不结算攒出离谱值；克制由 effective_gate 按 streak 抬门槛承担。
                cap = desire.URGE_CEILING
                if urge > cap:
                    # 被冷落够了，没正事就攒不过这道坎
                    u["urge"] = round(cap, 4)
                    urge = cap
                    self.state.mark_dirty()  # cap 改了内存 urge 就落盘，否则这轮无人 ready 时不写盘
                # 到点的由头本身就是一次念头落地：「想起一件具体的事」不需要再等时候攒满
                if cue:
                    urge = max(urge, desire.FIRE_THRESHOLD)
                    qualified = urge >= min_urge
                elif min_urge >= desire.FIRE_THRESHOLD:
                    # 自动循环：随机门槛只调节节奏快慢，由 effective_gate 压在天花板之内。
                    # 不能直接拿 fire_gate 比——cap 在下、gate 在上时念头永远够不着，
                    # 而 gate 只在发送成功后重抽，等于把这个人永久锁死。
                    qualified = urge >= desire.effective_gate(u, cap)
                else:
                    # 手动触发（min_urge 降到 0）：把所有人都列出来给主人看
                    qualified = urge >= min_urge
                if qualified:
                    ready.append((uid, u, urge))
            except Exception as e:
                metrics = getattr(self, "_m", None)
                if isinstance(metrics, dict):
                    metrics["settle_user_errors"] = (
                        int(_num(metrics.get("settle_user_errors"))) + 1
                    )
                if throttle.allow(f"settle.user.{bid}.{uid}"):
                    logger.warning(
                        f"[autonomous_social][{bid}] 结算用户 {uid} 的念头出错，本轮跳过"
                        f"这个人（其它用户照常）：{e}"
                        + throttle.summary(f"settle.user.{bid}.{uid}")
                    )
                continue

        if ready:
            self.state.mark_dirty()
        ready.sort(key=lambda x: -x[2])
        return ready

    def _heartbeat_range(self) -> Tuple[int, int]:
        """本轮心跳的等待区间（秒）。

        配置只给分钟，且下界不得大于上界——面板里两个值填反了不应该变成
        random.randint(抛 ValueError) 直接把后台循环打死。
        """
        lo = max(MIN_HEARTBEAT_SECONDS, int(self.cfg.heartbeat_min_minutes) * 60)
        hi = max(lo, int(self.cfg.heartbeat_max_minutes) * 60)
        return lo, hi

    def _flow_person_state(self, bid: str, now: float) -> Tuple[bool, bool]:
        """群心流缺的两道人物侧闸：现在是不是安静时段、她是不是在睡。

        私聊侧五道闸都查了这两项，只有群心流漏了；而群心流是事件驱动的（群消息一到
        就走，不经过心跳），于是深夜群里有人说话她照样插一句。

        实现统一在模块级的 `_person_flow_state`（那里对缺失的依赖降级）。
        """
        return _person_flow_state(self, bid, now)

    def _remember_night(self, bid: str, body: Optional[Dict[str, Any]]) -> None:
        """记下 Core 算出的生物钟夜。读不到就清掉，退回插件自己的配置。"""
        window = (body or {}).get("night_window") if isinstance(body, dict) else None
        key = str(bid or "")
        if not key:
            return
        try:
            if window and len(window) == 2:
                start, end = _num(window[0], math.nan), _num(window[1], math.nan)
                # nan 躲得过 `float()`（它不抛），却躲不过 `_quiet_now` 里的比较：
                # `nan < end`、`hour >= nan` 全假，于是这道「安静时段」硬闸静默失效、
                # 深夜照样开口。和「读不到」同等处理——清掉，退回插件自己的配置窗口。
                if math.isfinite(start) and math.isfinite(end):
                    self._night_windows[key] = (start % 24.0, end % 24.0)
                else:
                    self._night_windows.pop(key, None)
            else:
                self._night_windows.pop(key, None)
        except (TypeError, ValueError):
            self._night_windows.pop(key, None)

    def _quiet_now(self, bid: str, hour: int) -> bool:
        """这个角色现在该不该安静。

        优先用 Core 按她今天日程推出的生物钟夜——那才是她几点睡到几点；
        拿不到才退回本插件配置的 quiet_start/quiet_end。
        """
        window = self._night_windows.get(str(bid or ""))
        if not window:
            return self.cfg.in_quiet_hours(hour)
        start, end = window
        if abs(start - end) < 1e-9:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _pending_window(self) -> float:
        """多久没回算「没接话」：在回复统计窗口基础上至少给半天。"""
        return max(self.cfg.reply_window_hours, PENDING_IGNORE_MIN_HOURS) * 3600

    def _defer_anchor(self, user: Dict[str, Any], prefix: str) -> None:
        """由头/挂事这次没说成：把「最早能再试」往后推，次数用完就作废这件事。

        只写 <prefix>_retry_at 与 <prefix>_tries，锚点 <prefix>_due 和寿命
        <prefix>_expire_at 保持不动。推迟的应该是重试节奏，不是这件事能活多久——
        寿命由 expire_at 单向把守，推迟多少次都拦不住它凉掉。
        """
        try:
            tries = int(user.get(f"{prefix}_tries", 0) or 0) + 1
        except (TypeError, ValueError):
            tries = 1
        if prefix == "loop":
            self._defer_loop(user)
            return
        text = str(user.get(prefix, "") or "")
        if tries > CUE_MAX_TRIES:
            user[prefix] = ""
            user[f"{prefix}_due"] = 0.0
            user[f"{prefix}_expire_at"] = 0.0
            user[f"{prefix}_retry_at"] = 0.0
            user[f"{prefix}_tries"] = 0
            self.state.mark_dirty()
            logger.info(
                f"[autonomous_social] 「{text[:20]}」重试 {CUE_MAX_TRIES} 次仍没发成，作废不再试"
            )
            return
        user[f"{prefix}_retry_at"] = self._time() + CUE_RETRY_DELAY_SECONDS
        user[f"{prefix}_tries"] = tries
        self.state.mark_dirty()

    def _defer_loop(self, u: Dict[str, Any]) -> None:
        """回访没发成：只推**最紧要那一件**的重试节奏，别把整叠都推后。

        未完话题现在最多记 5 件（见 state._note_loop），一起推等于一次失手让五件事
        全部再等六小时。
        """
        hits = live_loops(u, self._time(), limit=1)
        if not hits:
            return
        _, _, item = hits[0]
        try:
            tries = int(_num(item.get("tries"))) + 1
        except (TypeError, ValueError):
            tries = 1
        if tries > CUE_MAX_TRIES:
            self._drop_loop(u, str(item.get("about", "")))
            logger.info(
                f"[autonomous_social] 「{str(item.get('about', ''))[:20]}」"
                f"重试 {CUE_MAX_TRIES} 次仍没问出去，这件不再问了"
            )
            return
        item["tries"] = tries
        item["retry_at"] = self._time() + CUE_RETRY_DELAY_SECONDS
        u["loops"] = loop_entries(u)
        # 平铺字段跟着最新那条走：盘上仍然是老结构的读者（以及诊断）看到的同一个值
        newest = u["loops"][0] if u["loops"] else None
        u["loop"] = str(newest.get("about", "")) if newest else ""
        u["loop_due"] = _num(newest.get("due")) if newest else 0.0
        u["loop_expire_at"] = _num(newest.get("expire_at")) if newest else 0.0
        u["loop_retry_at"] = _num(newest.get("retry_at")) if newest else 0.0
        u["loop_tries"] = int(_num(newest.get("tries"))) if newest else 0
        self.state.mark_dirty()

    @staticmethod
    def _drop_loop(u: Dict[str, Any], about: str) -> None:
        """把某一件事从未完话题里拿掉。"""
        kept = [
            i for i in loop_entries(u)
            if str(i.get("about", "")).strip() != str(about or "").strip()
        ]
        u["loops"] = kept
        newest = kept[0] if kept else None
        u["loop"] = str(newest.get("about", "")) if newest else ""
        u["loop_due"] = _num(newest.get("due")) if newest else 0.0
        u["loop_expire_at"] = _num(newest.get("expire_at")) if newest else 0.0
        u["loop_retry_at"] = 0.0
        u["loop_tries"] = 0

    def _closer_marker(self, u: Dict[str, Any], sent_ts: float) -> float:
        """记下「这条主动消息是冲谁去的」，供收场判断用。

        `threads.closer_reason` 拿这个值跟 `last_sent` **按等值**比较
        （`closer_for == sent` 就认为这条已经收过场了），所以两边必须是同一个数。
        `last_sent` 由 `state.record_outgoing` 自己取一次时钟写下，而 `sent_ts` 是
        发送前在 `_speak_inner` 里另取的一次——两次取时钟可能差一个 tick（Windows 上
        粗粒度时钟尤其容易），一差等值守卫就失效，同一条消息会被反复当成「该收场」
        的候选。所以统一以刚落盘的 `last_sent` 为准，取不到才退回 `sent_ts`。
        """
        return _num(u.get("last_sent")) or sent_ts

    @staticmethod
    def _is_unreachable_error(err: str) -> bool:
        """平台是不是明说「这个人发不了」（非好友 / 已注销）。

        只看平台抛出的错误文本，插件自己造的那句「未找到匹配的会话平台」不算——
        它对任何一种 send_message 失败都会说一遍，匹配它等于把平台故障全归给用户。
        """
        low = str(err or "").lower()
        return any(m.lower() in low for m in _PEER_UNREACHABLE_MARKERS)

    # 拦截理由 → 稳定分类。
    #
    # 为什么要这个：以前九道闸各返回一句中文，只逐人 log 一行。跑完一轮如果一条
    # 都没发出去，从指标里看不出**是哪道防线在起作用**——只知道「否决了 4 次」，
    # 不知道是「刚聊上」「冷却没到」还是「她说此刻不该说」。于是阈值调不动：
    # 凭感觉调，改完也不知道有没有用。
    #
    # 用**子串**匹配而不是给每道闸加个 code：闸有九道、散在三个方法里，逐个加 code
    # 意味着以后新增闸的人很容易忘；集中在这里，漏了新闸只会多出一个「其它」，
    # 不会静默丢掉。
    #
    # 刻意用「包含」而不是「开头匹配」：理由文本是人话，会变。写死前缀的话
    # 「现在是安静时段」改一个字就掉进「其它」——踩过一次。
    _BLOCK_CATEGORIES = (
        ("刚聊上", "刚聊上"),
        ("不久前才说过话", "刚聊过"),
        ("还没回", "没被接住"),
        ("护栏冷却", "冷却未过"),
        ("决定不说", "刚跳过"),
        ("要去睡", "TA要睡了"),
        ("作息", "作息时间"),
        ("安静时段", "安静时段"),
        ("太累", "她太累"),
        ("正在睡", "她在睡"),
        ("没有可用的会话来源", "投不出"),
        ("发送侧熔断", "发送熔断"),
        ("没有由头", "没由头"),
    )

    @classmethod
    def _block_category(cls, reason: str) -> str:
        text = str(reason or "").strip()
        if not text:
            return "未说明"
        # 长短语排前面：「刚想过一次决定不说」和「刚跳过」都要命中，
        # 顺序反了会被短的那个先吃掉。
        for needle, key in sorted(cls._BLOCK_CATEGORIES, key=lambda kv: -len(kv[0])):
            if needle in text:
                return key
        return "其它"

    # 关系档的分界。`interest` 是插件自己算的「这个人在我心里有多重要」
    # （好感 + 互动量 + 接不接话 + 冷落衰减），已经在 `settle_minds` 里写进
    # `u["interest"]`，所以拿它排序不额外花钱。
    #
    # 分 6 档而不是直接用连续值：直接用连续值等于「按好感精确排名」，
    # 高分那几个人会把低分的人彻底饿死——那正是要避免的。分档让**同档内**
    # 仍然是纯粹的轮转。
    _REL_BUCKETS = (0.20, 0.32, 0.45, 0.58, 0.72)

    @classmethod
    def _relation_bucket(cls, user: Dict[str, Any]) -> int:
        try:
            # 不能用 `or 0.35`：`0.0` 是个**读得出来的值**（`interest` 的正规下限是
            # 0.05，但手改/旧版迁移可能写成 0.0），而 `0.0 or 0.35` 会把它当成
            # 「没这个字段」抬到默认档——一个本该最边缘的人被排到中档，间隔反而更短。
            raw = user.get("interest")
            # 同理不能放 nan 进来：`iv < floor` 对 nan 恒假，循环走完直接落到
            # `return len(_REL_BUCKETS)`——一个读不出来的在意度被抬到**最高**档，
            # 比「缺失」还激进。nan 归到与缺失同一个默认值。
            iv = 0.35 if raw is None or raw == "" else _num(raw, 0.35)
        except (TypeError, ValueError):
            iv = 0.35
        for idx, floor in enumerate(cls._REL_BUCKETS):
            if iv < floor:
                return idx
        return len(cls._REL_BUCKETS)

    def _count_block(self, reason: str) -> None:
        """记一次「本来要发但没发」。同一轮里反复撞同一道闸会重复计——"""
        cat = self._block_category(reason)
        # 用 `blocked_reasons` 这个已有字段：它从 v1.x 就声明在初始 metrics 里、
        # 注释写着「各闸门各拦下多少」，但**从来没有代码写过它**——一个空壳。
        # 这次把它填上，不再另起一个名字。
        table = self._m.setdefault("blocked_reasons", {})
        table[cat] = int(table.get(cat, 0) or 0) + 1

    def _note_decision(self, uid: str, text: str) -> None:
        """C2：把一次决策（发了 / 没发）记进最近 24h 时间线，面板上看得到她都在想什么。"""
        try:
            now = self._time()
        except Exception:
            return
        rows = self._m.get("timeline")
        if not isinstance(rows, list):
            rows = []
        cutoff = now - 24 * 3600.0
        kept = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                if _num(r.get("t", 0) or 0) >= cutoff:
                    kept.append(r)
            except (TypeError, ValueError):
                continue
        kept.append({"t": round(now, 1), "uid": str(uid or ""), "text": str(text or "")[:80]})
        if len(kept) > 15:
            del kept[:-15]
        self._m["timeline"] = kept

    def timeline_text(self) -> str:
        """C2：最近 24h 的决策时间线（最新在后，只取最后 10 条）。"""
        rows = self._m.get("timeline")
        if not isinstance(rows, list) or not rows:
            return "  最近 24 小时还没有决策记录"
        now = self._time()
        lines = ["  最近 24 小时："]
        for r in rows[-10:]:
            if not isinstance(r, dict):
                continue
            try:
                ago = max(0, int((now - _num(r.get("t", now) or now)) / 60))
            except (TypeError, ValueError):
                ago = 0
            lines.append(f"    {ago} 分钟前 · {r.get('text', '')}")
        return "\n".join(lines)

    def _user_lock(self, bid: str, uid: str) -> asyncio.Lock:
        key = (str(bid or ""), str(uid or ""))
        lock = self._user_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[key] = lock
        return lock

    @staticmethod
    def _platform_problem(err: str) -> bool:
        """错误是不是平台/配置层面的（不支持主动消息、适配器未就绪等）。"""
        low = str(err or "").lower()
        return any(m.lower() in low for m in _PLATFORM_UNAVAILABLE_MARKERS)

    def gate_reason(
        self,
        bid: str,
        uid: str,
        u: Dict[str, Any],
        now: float,
        body: Optional[Dict[str, Any]] = None,
    ) -> str:
        """规则闸门：这些情况下就算念头攒满了也不该发。返回原因，空串表示可以发。

        只把「用常识就能看出来不对」的情况拦掉（便宜又快）；真正需要品味的「现在说
        这句到底合不合适」交给模型的 decide()。
        """
        # 身体在睡就不是「概率打折」的问题：把人从睡梦中叫醒闲聊不是拟人，是打扰。
        # 作息与安静时段同样的教训：这类东西做成权重会漏，必须是闸。
        if _body_asleep(body):
            return "她正在睡觉"
        # C5/D4：Core 那边的情绪也参与——她正在冷静期就不找这个人；
        # 对这个人的气还没消（攻击性远高于基线）也不找。
        try:
            snap = self.core.load_snapshot(bid, uid)
        except Exception:
            snap = None
        if isinstance(snap, dict):
            mood = snap.get("mood") if isinstance(snap.get("mood"), dict) else {}
            try:
                cool_until = _num(mood.get("cool_no_proactive_until", 0.0) or 0.0)
            except (TypeError, ValueError):
                cool_until = 0.0
            if cool_until > now:
                return "她还在为之前的事生这个人的气"
            try:
                aggr = _num(mood.get("aggression", 0.0) or 0.0)
                # 基线不能写成 `mood.get(...) or aggr`：基线**正常就是 0**（她平时没在生谁
                # 的气），而 `0.0 or aggr` 会把它当成「Core 没报基线」，于是 base 变成当前值、
                # 差值恒为 0——「气还没消」这道闸永远不会成立。同 `_relation_bucket` 的老账：
                # 只有「缺失」才回退，`0.0` 是个读得出来的值。
                raw_base = mood.get("base_aggression")
                base_aggr = aggr if raw_base is None or raw_base == "" else _num(raw_base, aggr)
            except (TypeError, ValueError):
                aggr = base_aggr = 0.0
            if aggr - base_aggr >= 10.0:
                return "她这会儿对这个人的气还没消"
        # 「累」的闸不在这里，而在 `_speak`：那里才是所有开口的唯一收口。
        # 放这儿只挡住了有由头的那一路（实测 61 条里过了 57 条）。
        seen = _num(u.get("last_seen", 0) or 0)
        if now - seen < HOT_CHAT_THRESHOLD:
            return "刚聊上，不插话"
        if now - seen < self.cfg.recent_talk_minutes * 60:
            return "不久前才说过话，再另起一句很突兀"
        if str(u.get("pending_result", "") or "") == "waiting":
            return "上一条主动发的还没回，现在再发就是追着说"
        if now - _num(u.get("last_sent", 0) or 0) < self.cfg.user_cooldown_minutes * 60:
            return "护栏冷却未过"
        if now - _num(u.get("last_skip_at", 0) or 0) < self.cfg.skip_cooldown_minutes * 60:
            return "刚想过一次决定不说，缓缓"
        if sleep_signal(u, now):
            return "TA 说过要去睡了，这时发过去不合适"
        # 念头攒满了也得看一眼表：这个点对方多半不在线，那就把话留着，等到合适的点再说
        hour = self._moment_of(bid, now).hour
        if self.cfg.respect_user_rhythm and desire.rhythm_factor(u, hour) == desire.RHYTHM_OFF:
            return "按TA 平时的作息，这个点 TA 不玩手机"
        if self._quiet_now(bid, hour):
            return "现在是安静时段"
        return ""

    def thread_gate(
        self,
        bid: str,
        uid: str,
        u: Dict[str, Any],
        now: float,
        body: Optional[Dict[str, Any]] = None,
    ) -> str:
        """接话的闸门。与 gate_reason 的区别只有一处：不管 recent_talk。

        真人追问恰恰发生在「刚聊完」之后，拿「不久前才说过话」去挡它，等于把
        这条路径永远堵死。同理不查对方作息：TA 几分钟前还在说话，这个点肯定醒着。
        """
        if _body_asleep(body):
            return "她正在睡觉"
        # 「刚聊过」也要拦。追问与「在吗」都是**接续型**——对方刚说完话就追一句
        # 是打断。对方一小时一条「在吗」，她每两小时（最小间隔一过）追问一次，
        # 7 天 44 次。
        if now - _num(u.get("last_seen", 0) or 0) < self.cfg.recent_talk_minutes * 60:
            return "刚聊过，不追"
        last_said = self._last_said(u)
        if last_said > 0 and _num(u.get("thread_for", 0) or 0) == last_said:
            return "这段沉默已经接过一回了，再问就是催"
        if now - _num(u.get("thread_at", 0) or 0) < self.cfg.followup_cooldown_minutes * 60:
            return "刚接过一次，给 TA 点时间"
        if self._quiet_now(bid, self._moment_of(bid, now).hour):
            return "现在是安静时段"
        return ""

    @staticmethod
    def _last_said(u: Dict[str, Any]) -> float:
        """这一段对话里最后一个人说话的时刻。沉默从这一刻算起。"""
        return max(_num(u.get("last_seen", 0) or 0), _num(u.get("last_spoken", 0) or 0))

    def _thread_windows(self) -> Tuple[float, float, float, float]:
        """四个时间窗：多久算能追问 / 多久算能问在吗 / 最长追到多久 / 之前多久内得有过话。

        「得先聊起来」这一条以前取 presence 的三倍（默认 18 分钟）：两个人你一句我一句
        但每句隔半小时，就被判成「没在聊」，追问永远不成立。它该跟「最长追到多久」同量级
        ——要的是「刚才确实在来往」，不是「聊得密」。
        """
        presence = self.cfg.followup_after_minutes * 60
        probe = min(presence, self.cfg.probe_after_minutes * 60)
        ceiling = self.cfg.followup_max_minutes * 60
        return probe, presence, ceiling, max(1800.0, ceiling)

    def _thread_pick(
        self,
        bid: str,
        now: float,
        body: Optional[Dict[str, Any]],
    ) -> Optional[Tuple[str, Dict[str, Any], str, str, float]]:
        """这个角色名下，谁的那场对话正说断了。返回 (uid, u, 种类, 动机, 沉默秒)。

        挑刚断的那一个：离得越近越像同一场话，隔了两小时再问「在吗」就不成立了。
        """
        if not self.cfg.followup_enabled:
            return None
        probe, presence, ceiling, context = self._thread_windows()
        best: Optional[Tuple[float, str, Dict[str, Any], str, str]] = None
        for uid, u in self.state.bot(bid).get("users", {}).items():
            if not u.get("umo"):
                continue
            if self.state.is_send_blocked(bid, uid, now):
                continue
            kind, reason = thread_reason(
                u,
                now,
                probe_after_seconds=probe,
                presence_after_seconds=presence,
                max_seconds=ceiling,
                context_seconds=context,
                presence_floor_seconds=presence,
            )
            if not kind:
                continue
            if self.thread_gate(bid, uid, u, now, body):
                continue
            gap = now - self._last_said(u)
            if best is None or gap < best[0]:
                best = (gap, uid, u, kind, reason)
        if best is None:
            return None
        return best[1], best[2], best[3], best[4], best[0]

    def greet_gate(
        self,
        bid: str,
        uid: str,
        u: Dict[str, Any],
        now: float,
        body: Optional[Dict[str, Any]] = None,
        kind: str = "morning",
    ) -> str:
        """问候的闸门。跟另起话题的区别：

        - 不查 recent_talk：早上第一句话往往就在昨晚那句之后没几小时，问候本来就是时间性触发；
        - 不查「上一条没人回」：真人说完晚安没人理，早上照样道早安，那不算追着说；
        - 不查对方作息：问候窗口本身就是「该说这句的时候」，画像里没早上样本的人也该收到早安。

        但会查「TA 说过要去睡」：以前这条只有另起话题查，于是 22:50 对方说「困了先睡了」，
        22:52 就收到一句「晚安，今天过得怎么样呀」。晚安本身豁免——对方要睡了，回一句
        晚安是合理的。
        """
        if _body_asleep(body):
            return "她正在睡觉"
        if kind != "night" and sleep_signal(u, now):
            return "TA 说过要去睡了，这时发过去不合适"
        if self._quiet_now(bid, self._moment_of(bid, now).hour):
            return "现在是安静时段"
        return ""

    @staticmethod
    def _miss_sent_today(u: Dict[str, Any], day: str) -> int:
        """今天已经发过几条「念想」。

        旧状态里只有 `miss_day`（日期字符串）——那等于「今天发过 1 条」；
        新状态用 `miss_day_count` 支持一天多条（配置上限调大时生效）。
        """
        if str(u.get("miss_day", "") or "") != day:
            return 0
        try:
            return max(0, int(u.get("miss_day_count", 1) or 0))
        except (TypeError, ValueError):
            return 1

    @classmethod
    def _miss_mark_today(cls, u: Dict[str, Any], day: str) -> int:
        """记下今天又多发了一条念想，返回今天累计条数。"""
        count = cls._miss_sent_today(u, day) + 1
        u["miss_day"] = day
        u["miss_day_count"] = count
        return count

    def _miss_floor(self, tier: str) -> float:
        """「念想」这一类按关系档位各要多少好感。

        统一门槛（配置项 `miss_affection_min`，默认 42）当**中档**用；
        热的关系门槛更低，压根不熟的关系要更高。
        """
        base = float(getattr(self.cfg, "miss_affection_min", 42) or 42)
        scale = {
            anchors.TIER_HIGH: 0.45,   # 基线被设高的那批人能进来
            anchors.TIER_MID: 1.0,
            anchors.TIER_LOW: 1.6,    # 说「想你了」给没怎么说过话的人，很怪
        }
        return max(0.0, min(100.0, base * scale.get(tier, 1.0)))

    def _tier_of(
        self,
        bid: str,
        uid: str,
        u: Dict[str, Any],
        core_root: Optional[Dict[str, Any]],
        now: float,
    ) -> Tuple[str, Optional[float], Optional[float]]:
        """这个人跟她的关系有多热。返回 (档位, 好感, 涨幅)。

        靠**涨幅**和互动量分档，不看好感度绝对值——有的运营会把初始好感度设成 40
        之类让人更好攻略，聊完直接到 80；只看绝对值的话，那种人和「一直停在 80 但
        从不回话」的人是同一档，而该被多找的恰恰是前者。
        """
        aff = base = first_met = None
        try:
            snap = self.core.load_snapshot(bid, uid, root=core_root) if core_root else None
        except Exception:
            snap = None
        if snap:
            aff = snap.get("affection")
            base = snap.get("base_affection")
            first_met = snap.get("first_met")
        try:
            aff = float(aff) if aff is not None else None
        except (TypeError, ValueError):
            aff = None
        try:
            base = float(base) if base is not None else None
        except (TypeError, ValueError):
            base = None
        try:
            first_met = float(first_met) if first_met is not None else None
        except (TypeError, ValueError):
            first_met = None
        if aff is None:
            try:
                # 同 `_relation_bucket`：`0.0 or 0.35` 会把「读出来的 0.0」当成
                # 「没有这个字段」。这里的后果更重——`aff = 35.0` 越过了
                # `anchors.TIER_AFFECTION_FLOOR = 25.0`，一个在意度为 0 的人不会
                # 被判成 TIER_LOW，于是 `_note_giveup` 永远不会让他静默下来。
                raw_interest = u.get("interest")
                if raw_interest is None or raw_interest == "":
                    raw_interest = 0.35
                aff = float(raw_interest) * 100.0
            except (TypeError, ValueError):
                aff = None
        tier = anchors.relation_tier(
            u, affection=aff, base_affection=base, first_met=first_met, now=now
        )
        # 手动覆盖：主人明确指定的档位优先于按涨幅/互动的推算。
        try:
            override = self.cfg.tier_override_for(uid)
        except Exception:
            override = ""
        if override:
            tier = override
        warmth = (aff - base) if (aff is not None and base is not None) else None
        return tier, aff, warmth

    def _gap_left(self, u: Dict[str, Any], tier: str, now: float) -> float:
        """按关系档位的最小间隔。档位越高近得越理所当然——她常聊那个人。

        `min_gap_minutes` 是**中档**的最小间隔（见配置说明）；高/低档按档位表的比例
        跟着它缩放，这样主人调这一项就能整体收紧或放松节奏（不调就与表一致）。
        """
        try:
            base = float(self.cfg.min_gap_minutes)
        except (TypeError, ValueError):
            base = 60.0
        table = anchors.TIER_MIN_GAP_MINUTES
        mid = float(table.get(anchors.TIER_MID, 60.0)) or 60.0
        minutes = float(table.get(tier, mid)) * (max(0.0, base) / mid)
        minutes = min(max(minutes, 1.0), 1440.0)
        try:
            streak = max(0, int(_num(u.get("no_reply_streak", 0) or 0)))
        except (TypeError, ValueError):
            streak = 0
        # 被冷落过就再拉远一点：真人不会一直追一个不回话的人
        minutes *= min(3.0, 1.0 + 0.4 * streak)
        last = _num(u.get("last_sent", 0) or 0)
        if last <= 0:
            return 0.0
        return max(0.0, minutes * 60.0 - (now - last))

    def _greet_pick(
        self,
        bid: str,
        now: float,
        body: Optional[Dict[str, Any]],
        core_root: Optional[Dict[str, Any]],
    ) -> List[Tuple[str, Dict[str, Any], str]]:
        """问候：到早/午/晚的窗口时道一句，看 TA 回不回。

        **总开关 `greeting_enabled`**：以前这个开关只在状态文本里出现过，
        `_greet_pick` 从不检查它——关掉早晚问候照样发（用户在面板上关了没用）。

        **窗口判定与「每天各一次」用现成的 `reasoning.greeting_window_kind` /
        `greeting_due`**：这两个函数早就写好、也早被 import，却从来没被调用过——
        引擎里另写了一份内联判断，于是「每人每天每窗口各一次」从未生效。

        「她有多长时间没回我」记在 ``reply_gap_hours`` 里，每轮刷新，写进运行指标。
        """
        if not self.cfg.greeting_enabled:
            return []
        out: List[Tuple[str, Dict[str, Any], str]] = []
        dt = self._moment_of(bid, now)
        hour = dt.hour
        today = dt.strftime("%Y-%m-%d")
        morning, midday, night = self.cfg.greeting_windows()
        for uid, u in list(self.state.bot(bid).get("users", {}).items()):
            if not u.get("umo") or self.state.is_send_blocked(bid, uid, now):
                continue
            if self._quiet_now(bid, hour):
                continue
            if _body_asleep(body):
                continue
            # 只问候最近有来往的人：对素未谋面或早就不聊的人道早安，像定时群发
            if now - self._last_said(u) > GREET_FRESH_SECONDS:
                continue
            kind = greeting_window_kind(hour, morning, night, midday)
            if not kind:
                continue  # 不落在任何问候窗口：这不是该问候的时候
            if not greeting_due(u, today, kind):
                continue  # 今天这个窗口已问候过（午间还额外受「先有早安」约束）
            tier, _aff, _warmth = self._tier_of(bid, uid, u, core_root, now)
            if self._gap_left(u, tier, now) > 0:
                continue
            if self.greet_gate(bid, uid, u, now, body, kind=kind):
                continue
            if self._quiesced(u):
                continue
            out.append((uid, u, kind))
        return out

    @staticmethod
    def _quiesced(u: Dict[str, Any]) -> bool:
        """停发：连着几次没发出去就算了，除非 TA 主动说话。

        真人不会一直追一个不回话的人。解除条件只有一个——对方先开口。
        """
        try:
            seen = _num(u.get("last_seen", 0) or 0)
            stopped = _num(u.get("quiesced_at", 0) or 0)
        except (TypeError, ValueError):
            return False
        return stopped > 0 and seen <= stopped

    def loop_gate(
        self,
        bid: str,
        uid: str,
        u: Dict[str, Any],
        now: float,
        body: Optional[Dict[str, Any]] = None,
    ) -> str:
        """回访那件事的闸门：不看 recent_talk（那件事本来就是聊出来的），但要等这场话凉下来。"""
        if _body_asleep(body):
            return "她正在睡觉"
        # 以前只有另起话题查「TA 说过要去睡」，于是 09:00 说完「我出门了」，
        # 09:20 就收到「你上次说的那个体检后来咋样」。
        if sleep_signal(u, now):
            return "TA 说过要去睡了，这时发过去不合适"
        if self._quiet_now(bid, self._moment_of(bid, now).hour):
            return "现在是安静时段"
        if now - self._last_said(u) < LOOP_QUIET_GAP_SECONDS:
            return "还在聊，这会儿问「后来呢」太急"
        if str(u.get("pending_result", "") or "") == "waiting":
            return "上一条主动发的还没回"
        return ""

    @staticmethod
    def _core_last_interaction(
        core_root: Optional[Dict[str, Any]], bid: str, uid: str
    ) -> Optional[float]:
        """Core 记的「这个人上次说话」的时刻（每条消息都记）。拿不到返回 None。

        社交层自己的 last_seen 只在它观察到消息时才更新，实测会落后 Core 几小时到
        几天；由头/问候里的「沉默多久」要用 Core 这个更准的。
        """
        try:
            user = (
                ((core_root or {}).get("roles", {}) or {}).get(str(bid), {}) or {}
            ).get("users", {}) or {}
            ts = _num((user.get(str(uid), {}) or {}).get("last_interaction") or 0)
        except (TypeError, ValueError, AttributeError):
            return None
        return ts if ts > 0 else None

    def _anchor_pick(
        self,
        u: Dict[str, Any],
        body: Optional[Dict[str, Any]],
        now: float,
        *,
        limit: int = 2,
        tier: str = "",
        affection: Optional[float] = None,
        warmth: Optional[float] = None,
        last_talk: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """这个人本轮可以用的由头。

        优先读她身上已经挂着的（cue/loop/thread/promise，以及从账本挖出来的）；
        存量不够时**当场从她的真实状态派生**一件，派生即消费——同一件事一辈子只出现
        一次。真实记录里某一个晚上 23 条全是「窝在沙发追剧…困了…晚安」，就是因为
        问候的由头是固定句池、每天抽一句、池子不消耗。
        """
        try:
            picks = prepare_round_anchors(
                u, body or {}, now, limit=limit,
                tier=tier, affection=affection, warmth=warmth, last_talk=last_talk,
            )
        except Exception as exc:
            self.log(f"准备由头失败（{u.get('umo','')}）: {exc}")
            return []
        if not picks:
            return []
        # 第一个记成「用掉了」，但要等真发出去才真正消费（见 _speak）
        return list(picks[:1])

    def _promise_pick(
        self,
        bid: str,
        now: float,
        body: Optional[Dict[str, Any]],
    ) -> List[Tuple[str, Dict[str, Any], str]]:
        """到点该兑现的诺。这个人一次最多问一件。

        和未完话题是**两件事**：那个是对方提了没结果（她要去问），这个是她自己许下的
        （她要去做）。后者情绪色彩完全不同——记着自己说过的话，是亲密感最强的一环。
        """
        out: List[Tuple[str, Dict[str, Any], str]] = []
        for uid, u in self.state.bot(bid).get("users", {}).items():
            if not u.get("umo"):
                continue
            if self.state.is_send_blocked(bid, uid, now):
                continue
            hits = live_promises(u, now, limit=1)
            if not hits:
                continue
            # 兑现的场合比随口搭话正经：她正在等对方 asleep 或安静时段时先不说
            if _body_asleep(body):
                continue
            if self._quiet_now(bid, self._moment_of(bid, now).hour):
                continue
            if sleep_signal(u, now):
                continue
            out.append((uid, u, hits[0][1]))
        return out

    def _miss_pick(
        self,
        bid: str,
        now: float,
        body: Optional[Dict[str, Any]],
        core_root: Optional[Dict[str, Any]],
    ) -> List[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
        """「念想」通道：没有正事，就是想 TA 说一句。

        ## 为什么要有这一条独立的通道
        `record_incoming` 每收到一条消息就把 `urge` 清零，而念头要攒四五个小时才够门槛。
        于是**对方聊得越勤，她越永远攒不起来**——仿真里好感度同样 0.75 的两个人：
        对方从不回复 → 7 天发 20 条；对方每小时发一条 → **7 天发 0 条**。
        插件于是只主动找那些不理她的人。你好感 90% 却一条没收到，就是这个。

        至于 `gate_reason` 里的「刚聊过不插话」——那对**接话**是对的（别打断），
        但对「想你了」是错的：过了一阵子想起你，不是在打断谁的对话。
        所以这一类**绕过 recent_talk / 刚聊过 / 上一条没回**这几条，只受这些约束：
        她在睡、安静时段、最小间隔、每天条数上限、好感门槛。

        于是好感高的人**从此是最容易被主动联系的人**——联动第一次真的参与了触发。
        """
        cap = int(getattr(self.cfg, "miss_daily_cap", 0) or 0)
        if cap <= 0 or self.cfg.miss_affection_min <= 0:
            return []
        out: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
        day = self._moment_of(bid, now).strftime("%Y-%m-%d")
        for uid, u in self.state.bot(bid).get("users", {}).items():
            if not u.get("umo") or self.state.is_send_blocked(bid, uid, now):
                continue
            if self._miss_sent_today(u, day) >= cap:
                continue
            if _body_asleep(body):
                continue
            if self._quiet_now(bid, self._moment_of(bid, now).hour):
                continue
            if self._min_gap_left(u, bid=bid, now=now) > 0:
                continue
            aff = self._affection_of(bid, uid, u, core_root)
            # 门槛按**关系档位**给，不是一个统一的绝对好感度。
            #
            # 实测 Core 里 5657 个用户，386 个有数值，中位 30.4、均值 37.3，只有
            # 5.4%（37 人）过 55——统一门槛的话，「念想」这条专程为「好感高的人也收得到」
            # 开的通道，对 94.6% 的人是永久关着的。
            #
            # 而且绝对值本身就不可靠：有运营为了让用户更好攻略，会把初始好感度设成 40
            # 之类；聊久了直接到 80。关系真的热但数值卡在中段的（大有人在），不该被挡。
            # 高档的门槛还比统一值低——她常聊的那个人，本来就该最容易收到「想你了」。
            tier, _aff, _warmth = self._tier_of(bid, uid, u, core_root, now)
            floor = self._miss_floor(tier)
            if aff is None or aff < floor:
                continue
            try:
                picks = anchors.prepare_round_anchors(
                    u, {}, now, limit=3, affection=aff, relational=True,
                    missing_floor=floor,
                )
            except Exception as exc:
                # 这里**不能只 log 就算完**：之前这一段把一个 NameError 静默吞了，
                # 表现为「念想通道一条都发不出去」，排查时完全看不到线索。
                if throttle.allow(f"miss.derive.{bid}"):
                    logger.warning(
                        f"[autonomous_social][{bid}] 派发念想由头失败: "
                        f"{type(exc).__name__}: {exc}"
                        + throttle.summary(f"miss.derive.{bid}")
                    )
                continue
            # 在「牽挂 / 续聊 / 想念 / 搭话 / 随口问一句」之间随机取一条：不是每次都
            # 按同一个优先级——真人有时接着上次的事说，有时就是随口搭一句。
            candidates = [
                a for a in picks
                if str(a.get("kind")) in (anchors.KIND_MISS, anchors.KIND_GREET)
            ]
            if candidates:
                out.append((uid, u, random.choice(candidates)))
        return out

    def _affection_of(
        self,
        bid: str,
        uid: str,
        u: Dict[str, Any],
        core_root: Optional[Dict[str, Any]],
    ) -> Optional[float]:
        """好感度：优先 Core 的，没有就用在意度折算。拿不到返回 None（这条通道就不开）。"""
        try:
            snap = self.core.load_snapshot(bid, uid, root=core_root) if core_root else None
        except Exception:
            snap = None
        aff = (snap or {}).get("affection")
        if aff is not None:
            try:
                out = float(aff)
            except (TypeError, ValueError):
                out = None
            # 只认有限值：nan 既不是「有值」也不该原样传出去——`aff is not None`
            # 会放它进门，接着所有 `aff < 门槛` 恒假，这条通道的硬门槛就白设了。
            # 读不出来就当没有，退回在意度折算（那才是这条通道的本意）。
            if out is not None and math.isfinite(out):
                return out
        try:
            return _num(u.get("interest", 0.35) or 0.35) * 100.0
        except (TypeError, ValueError):
            return None

    def _loop_pick(
        self,
        bid: str,
        now: float,
        body: Optional[Dict[str, Any]],
    ) -> Optional[Tuple[str, Dict[str, Any], str, float]]:
        """有没有哪件挂着的事今天到点了。挑最该问的那一个——在意程度高的优先。"""
        if not self.cfg.loop_enabled:
            return None
        best: Optional[Tuple[float, str, Dict[str, Any], str]] = None
        for uid, u in self.state.bot(bid).get("users", {}).items():
            if not u.get("umo"):
                continue
            if self.state.is_send_blocked(bid, uid, now):
                continue
            hits = live_loops(u, now)
            if not hits:
                continue
            due, about, _item = hits[0]
            if self.loop_gate(bid, uid, u, now, body):
                continue
            # 等得越久的越该先问；同一轮里多个候选时按到期时间排
            weight = -(now - due) - _num(u.get("interest", 0) or 0) * 3600.0
            if best is None or weight < best[0]:
                best = (weight, uid, u, about)
        if best is None:
            return None
        _, uid, u, about = best
        return uid, u, about, now - _num(u.get("loop_due", 0) or 0)

    def closer_gate(
        self,
        bid: str,
        uid: str,
        u: Dict[str, Any],
        now: float,
        body: Optional[Dict[str, Any]] = None,
    ) -> str:
        """收场那句的闸门：它存在的意义恰恰是绕过「上一条没人回」，所以不查那条。"""
        if _body_asleep(body):
            return "她正在睡觉"
        hour = self._moment_of(bid, now).hour
        if self._quiet_now(bid, hour):
            return "现在是安静时段"
        if self.cfg.respect_user_rhythm and desire.rhythm_factor(u, hour) == desire.RHYTHM_OFF:
            return "按TA 平时的作息，这个点 TA 不玩手机"
        return ""

    def _closer_pick(
        self,
        bid: str,
        now: float,
        body: Optional[Dict[str, Any]],
    ) -> Optional[Tuple[str, Dict[str, Any], str]]:
        """谁那儿有句话一直悬着，该自己收个尾了。"""
        if not self.cfg.closer_enabled:
            return None
        after = self.cfg.closer_after_hours * 3600.0
        best: Optional[Tuple[float, str, Dict[str, Any], str]] = None
        for uid, u in self.state.bot(bid).get("users", {}).items():
            if not u.get("umo"):
                continue
            if self.state.is_send_blocked(bid, uid, now):
                continue
            reason = closer_reason(u, now, after_seconds=after)
            if not reason:
                continue
            if self.closer_gate(bid, uid, u, now, body):
                continue
            waited = now - _num(u.get("last_sent", 0) or 0)
            if best is None or waited > best[0]:
                best = (waited, uid, u, reason)
        if best is None:
            return None
        return best[1], best[2], best[3]

    def _mind(self, bid: str, u: Dict[str, Any], now: float, urge: float) -> Dict[str, Any]:
        """把念头状态整理成给模型看的内心描述。"""
        return {
            "urge": urge,
            "interest": _num(u.get("interest", 0.35) or 0.35),
            "silence_hours": (now - _num(u.get("last_seen", 0) or 0)) / 3600.0,
            "pending_result": str(u.get("pending_result", "") or ""),
            "no_reply_streak": int(_num(u.get("no_reply_streak", 0) or 0)),
            "they_slept": sleep_signal(u, now),
            # 只在这个开关开着时才算：关着时（默认）引擎压根不用作息画像挑点，
            # 提示词里却写着「按对方作息这个点可能不在线」——和上一段
            # 「时机已经替你查过」直接打架，模型会被推着答 NO。
            "rhythm_off": (
                self.cfg.respect_user_rhythm
                and desire.rhythm_factor(u, self._moment_of(bid, now).hour) == desire.RHYTHM_OFF
            ),
        }

    async def _persona(self, umo: str, bid: str = "") -> Tuple[str, str]:
        """取目标会话当前生效的 AstrBot 人格，返回 (名字, 设定原文)。

        按 umo 解析意味着多角色天然隔离：每个角色的每个会话用的就是 AstrBot
        在该会话里本来就在用的人格，插件不再插手。
        """
        try:
            name, prompt = await resolve_persona(self.context, umo)
        except Exception as e:
            if throttle.allow(f"persona.read.{bid or '-'}"):
                logger.warning(
                    f"[autonomous_social] 人格设定读取失败，改用默认口吻: {e}"
                    + throttle.summary(f"persona.read.{bid or '-'}")
                )
            return "", ""
        if bid:
            self._last_persona[bid] = name
        return name, prompt

    # ─── 主动联系主循环 ─────────────────────────────────

    async def dry_run(self) -> Dict[str, Any]:
        """只结算、不发送、不花钱的一轮。给部署后立刻验证用。

        以前装上之后要先干等一个心跳间隔（8~15 分钟）才知道链路通不通——而这期间
        「零发送」是完全正常的，看不出是正常还是坏了。这一轮只做 `settle_minds`，
        不调 LLM、不发任何东西，结束时把「多少人念头够了、被哪道闸拦下」写进
        运行指标。`/自主社交状态` 一装上就能回答「链路是通的吗」。
        """
        now = self._time()
        self._m["dry_run_at"] = now
        if not self.cfg.enabled:
            self._m["last_failure"] = "dry-run：插件已停用"
            self._persist_metrics()
            return self._m
        core_root = None
        try:
            core_root = self.core.read_root()
        except Exception as exc:
            logger.warning(f"[autonomous_social] dry-run 读 Core 失败: {exc}")

        bots = [b for b, x in self.state.data.get("bots", {}).items()
                if ((x if isinstance(x, dict) else {}).get("users") or {})]
        total = ready = 0
        for bid in bots:
            bot_state = None
            if core_root:
                try:
                    bot_state = self.core.bot_self_state(bid, root=core_root)
                except Exception:
                    bot_state = None
            try:
                users = self.state.bot(bid).get("users", {})
                total += len(users)
                ranked = self.settle_minds(
                    bid, now, self._moment_of(bid, now), core_root, bot_state,
                    min_urge=0.0,
                )
                # min_urge=0 会把所有人列出来；这里只数「真攒够了」的
                for uid, u, urge in ranked:
                    if urge >= desire.FIRE_THRESHOLD and not self.gate_reason(
                        bid, uid, u, now, bot_state
                    ):
                        ready += 1
            except Exception as exc:
                logger.warning(f"[autonomous_social][{bid}] dry-run 结算出错: {exc}")
        self._m["last_settled"] = now
        self._m["last_settled_users"] = total
        self._m["last_ready"] = ready
        self._m["last_failure"] = "" if ready else "dry-run：暂时没有攒够念头的人（正常）"
        self._persist_metrics()
        logger.info(
            f"[autonomous_social] dry-run 完成：{total} 人，{ready} 人念头已够"
            f"（没有发送，也没有调用模型）"
        )
        return self._m

    def _note_cross_user_ratio(self, text: str, others: List[Tuple[str, str]]) -> None:
        """记下这条和「最近发给别人的」最高相似度是多少。

        阈值定 0.6 是猜的。开着 `cross_user_calibrate` 跑一天，`_runtime` 里就有
        真实分布，看完再把 `cross_user_repeat_ratio` 定到线上。
        """
        if not self.cfg.cross_user_calibrate or not others:
            return
        cur = _bigrams(strip_actions(text))
        if not cur:
            return
        best = 0.0
        for _who, old in others:
            ratio = _jaccard(cur, _bigrams(strip_actions(old)))
            best = max(best, ratio)
        self._m["cross_user_ratio_last"] = round(best, 3)
        samples = self._m.setdefault("cross_user_ratios", [])
        if isinstance(samples, list):
            samples.append(round(best, 3))
            del samples[:-200]

    def _persist_metrics(self) -> None:
        """运行指标落盘。容器侧靠它判断「跑没跑、卡在哪」，所以写完就 flush，
        不等那个 30 秒的周期——否则刚装上时去读文件会扑空。"""
        try:
            self.state.set_runtime_metrics(self._m)
            self.state.flush()
        except Exception as exc:
            self.log(f"写运行指标失败: {exc}")

    async def try_once(self) -> None:
        """一次心跳：每个用户各自结算、各自发送，互不排队。

        v1.10.2 之前所有人挤在一条队里：一轮心跳只有排在最前面的那件事能说出口
        （发完直接 return），发过之后还要等全局冷却——A 收到消息后，B 就算念头
        攒满、早安窗口正开着也得干等。表现出来就是「跟 A 说完要过好久才轮到 B」，
        每个人不是在被社交，是在等叫号。

        现在节奏长在每个人自己身上：每个用户有自己的念头、自己的冷却与闸门，
        一轮里可以先后找多个人。max_sends_per_round 只是别把攒下的话一口气
        全倒出去的护栏。心跳本身仍只是「想起来看一眼手机」的时机。
        """
        now = self._time()
        self._m["last_heartbeat"] = now
        self._m["rounds"] = int(self._m.get("rounds", 0)) + 1
        # 每轮重置「有由头但没发」的分类计数（见 __init__ 里的说明）
        # 每轮先把「她那座城市现在几点」问一次 Core。原来只有状态展示前才刷，
        # 主循环里 _city_offset 一直是空的 → _moment_of 退回本机时区。
        # 线上表现：她在泽列诺格拉茨克（UTC+2），北京 23:22 她那边才 17:22，
        # 却被判成「安静时段」，一轮 156 个候选全挡住，生成的句子也会带错时段词。
        try:
            self._refresh_clocks()
        except Exception:
            pass
        self._round_skips = {}
        self._round_skip_samples = {}
        if not self.cfg.enabled:
            return

        # 周期性刷盘
        if now - self._last_flush > FLUSH_INTERVAL_SECONDS:
            self.state.flush()
            self._last_flush = now
        # provider 可用性：按「要发的这个人」取，不再拿任意一个用户代表整个 bot。
        # 原来是从 users 里取**第一个**有 umo 的用户试一次，取不到就整个 bot 这轮跳过
        # ——而「第一个」是历史导入顺序决定的，与它现在能不能发无关：一个陈旧/已失效的
        # UMO 就能让这个 bot 下**所有**用户停止攒念头（180 人规模实测就是这么零发送的）。
        # 现在没有这层探测：provider 真正取不到时由 _compose 报（已按 bid 节流），
        # 状态页的「LLM 生成」行也看得见。
        # 这里在所有 try 之外：state.json 里任一 `bots[bid]` 是 null/非 dict
        # （手改、旧版残留、外部工具写入）时 `x.get` 会抛 AttributeError，
        # 每轮心跳都抛、退避 30 分钟且永久复发 —— 引擎彻底不工作，面板还显示「启用」。
        bots = [
            b for b, x in self.state.data.get("bots", {}).items()
            if ((x if isinstance(x, dict) else {}).get("users") or {})
        ]
        if not bots:
            return

        # ── 跨轮熔断（v1.28.2）────────────────────────────────
        # 上面那段 `infra_down` 是**一轮内**的：同一轮里第一个候选撞见 provider 坏了，
        # 剩下的候选就不再试。但它管不到下一轮——`infra_down` 每轮新建、每轮清零，
        # 于是上游持续限流时，每个心跳仍然会收集全部候选、再逐个去撞一次墙。
        #
        # 真实代价（本机日志，v1.28.1）：一天 998 轮、404 次 Gemini 重试，
        # 只发出去 582 条，绝大部分请求最终被「她在睡」「发送熔断」挡下——
        # 也就是配额烧在了根本发不出去的候选上，而 429 的恢复窗口是 2h48m。
        #
        # 现在从生成器把「这一路整体是坏的」读出来，直接结束整轮：
        # 不收集候选、不进 settle、不刷日志洪水。恢复由 `_note_llm_ok` 撤熔断，
        # 所以上游一好，下一个心跳（8~15 分钟）就能重新说话。
        try:
            circuit = self.generator.llm_circuit_state()
        except Exception:
            circuit = {}
        if circuit:
            self._m["last_failure"] = (
                f"LLM 熔断中（{'; '.join(sorted(circuit))}），本轮未尝试"
            )
            self._m["llm_circuit_scopes"] = sorted(circuit)
            for scope, info in circuit.items():
                n = self.generator.llm_circuit_note_round(scope)
                kind = info.get("kind", "transient")
                mins = int(info.get("down_for", 0.0) // 60)
                # 降频：第 1、2 轮各一条，之后按 2/4/8/16… 轮一条，封顶 128 轮。
                # 不降的话熔断只是把「每轮 550 行 provider 报错」换成「每轮 1 行熔断」。
                if n <= 2 or (n & (n - 1)) == 0 or n % 128 == 0:
                    logger.warning(
                        f"[autonomous_social][{scope}] LLM 仍处于熔断中"
                        f"（已连续 {mins} 分钟，连续失败 {info.get('streak', 0)} 次，"
                        f"档位={kind}），本轮 {len(bots)} 个角色的候选全部不尝试。"
                        f"这是连续第 {n} 个空转轮。"
                    )
            return

        # 每个角色只读一次 Core 状态
        core_root = None
        try:
            core_root = self.core.read_root()
        except Exception as e:
            if throttle.allow("core.read_root"):
                logger.warning(
                    f"[autonomous_social] 读取 Core 状态失败: {e}"
                    + throttle.summary("core.read_root")
                )

        # 各类候选按用户收集，不挑唯一：同一类里可以同时有多个人等着
        # 早晚问候（时间性触发，窗口过了就没了）
        greets: List[Tuple[str, str, Dict[str, Any], str, str]] = []
        # 由头驱动：手上真有一件具体的事才进队。
        # 原来的 contenders（念头攒满就发）是个**计时器**而不是理由——197 条真实记录里
        # `why=念头攒到 2.80` 的那 82 条几乎全是「想表达…」「饿了…撒娇」，
        # 而 `why=刚热上饭，顺口问下人还在没` 的那几条完全像人。念头从此只调节节奏。
        anchored: List[Tuple[str, str, Dict[str, Any], float, Dict[str, Any]]] = []
        # 话说到一半断掉的人：这比「另起一个话题」紧迫，排在念头前面处理
        threads: List[Tuple[str, str, Dict[str, Any], str, str, float]] = []
        # 挂着没回访的那件事，以及该自己收场的那些
        loops: List[Tuple[str, str, Dict[str, Any], str, float]] = []
        closers: List[Tuple[str, str, Dict[str, Any], str]] = []
        promises: List[Tuple[str, str, Dict[str, Any], str]] = []
        # 念想：没有正事，就是想 TA 说一句。这一类绕过「刚聊过」阻断（见 _miss_pick）
        misses: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = []
        # 「想要了」：只对真的做过色色、且此刻欲望涨上来的人主动问一句（见 _intimacy_pick）
        intimacies: List[Tuple[str, str, Dict[str, Any]]] = []
        # 按角色存住身体快照：后面要用它过闸门，拿循环残留的变量会把 A 的身体套到 B 头上
        bodies: Dict[str, Dict[str, Any]] = {}
        settled_users = 0
        for bid in bots:
            bot_state = None
            if core_root:
                try:
                    bot_state = self.core.bot_self_state(bid, root=core_root)
                except Exception as e:
                    self.log(f"读取 {bid} 自身状态失败: {e}")
            if bot_state:
                bodies[bid] = bot_state
                self._remember_clock(bid, bot_state)
            # 冷落计数与 Core 无关，只看本插件的账本。放在 bot_state 判断里的话，
            # Core 装着但该角色还没进它 state.json 时会恒为 0，然后把这个 0 写回
            # 信号文件——Core 读到的就是「她最近没被冷落过」。
            # 整个角色包在 try 里：一条脏记录（结算、挑人里任何一个抛）只该让这个角色
            # 这一轮缺席，不该掀翻整轮 try_once——那样其它角色那一轮整个丢失，
            # 而 run() 的退避会把惩罚放大到最长半小时。单个角色被脏数据打挂，
            # 结果是所有角色一起安静半小时。
            try:
                settled_users += len(self.state.bot(bid).get("users", {}))
                for uid, u, about in self._promise_pick(bid, now, bot_state):
                    promises.append((bid, uid, u, about))
                for uid, u, a in self._miss_pick(bid, now, bodies.get(bid), core_root):
                    misses.append((bid, uid, u, a))
                for uid, u in self._intimacy_pick(bid, now, bodies.get(bid), core_root):
                    intimacies.append((bid, uid, u))
                ready = self.settle_minds(bid, now, self._moment_of(bid, now), core_root, bot_state)
                # 只留下**手上真有由头**的。念头够不够由 settle_minds 管，
                # 但没有由头的人不进队——这就是「没什么可说的就别说话」。
                for uid, u, urge in ready:
                    picks = self._anchor_pick(
                        u, bodies.get(bid), now,
                        last_talk=self._core_last_interaction(core_root, bid, uid),
                    )
                    if not picks:
                        # 逐人一行的话，一轮里有几十上百个人都走这条（念头够了但手上
                        # 没有一件具体的事）。归到本轮汇总的「没由头」里。
                        self._round_skips["没由头"] = self._round_skips.get("没由头", 0) + 1
                        logger.debug(f"[autonomous_social] {uid} 念头到了但没由头，不找话说")
                        continue
                    for a in picks[:1]:
                        anchored.append((bid, uid, u, urge, a))
                picked = self._thread_pick(bid, now, bot_state)
                if picked:
                    threads.append((bid, *picked))
                looped = self._loop_pick(bid, now, bot_state)
                if looped:
                    loops.append((bid, *looped))
                closed = self._closer_pick(bid, now, bot_state)
                if closed:
                    closers.append((bid, *closed))
                for uid, u, kind in self._greet_pick(bid, now, bodies.get(bid), core_root):
                    greets.append((bid, uid, u, kind, ""))
            except Exception as e:
                if throttle.allow(f"bot.round.{bid}"):
                    logger.warning(
                        f"[autonomous_social][{bid}] 本轮结算出错，这个角色这轮不参与"
                        f"（其它角色照常）：{e}" + throttle.summary(f"bot.round.{bid}")
                    )

        # 把近况写回给 Core：她被冷落了几个、刚替谁主动开过口。
        # 写失败不影响发送，但要在状态页看得见（原来静默 return，表现是两边像没装）。
        # 冷落计数改为**按角色**各记各的（顶层那个是跨角色取 max，单文件单字段会让
        # 所有人共享最糟的那一个）。
        try:
            # 先定角色数：多 Bot 时顶层 ignored_streak 不写（防串台）。
            # 这一步必须在写回循环**之前**——晚于 set_ignored_streak 的话，
            # 第一轮心跳时 _roles 还是默认的 1，顶层会被写一次并从此冻结，
            # 所有角色长期看到一个不属于自己的冷落计数。
            self.signals.set_role_count(len(bots))
            for bid in bots:
                self.signals.set_ignored_streak(
                    self.state.max_no_reply_streak(bid), bid
                )
                # **按人**也记一份。角色级的 streak 只说得清「我最近有点冷清」，
                # 说不清是谁——而好感是用户级的，Core 那边要把被冷落换算成
                # 「对 TA 的好感下降」，必须知道是谁没理她。
                for uid, u in (self.state.bot(bid).get("users") or {}).items():
                    try:
                        streak = int(_num(u.get("no_reply_streak", 0) or 0))
                    except (TypeError, ValueError):
                        continue
                    if streak > 0:
                        self.signals.note_ignored(uid, streak, bid)
                # 她手上还开着的事（对方提了没下文的 + 她自己许下还没兑现的）：
                # **每轮重算并整体覆盖**。Core 靠它判断「这件事结了没有」，只增不减会让
                # 早就了结的事永远挂在她的提示词里（见 SignalsWriter.note_open_items）。
                self.signals.note_open_items(self._open_items(bid, now), bid)
            # 无条件重写：Core 那边靠 mtime 判信号是否过期，而心跳间隔远大于它的
            # 默认阈值，靠「内容没变就跳过」是撑不过去的。顺便把真实保鲜期写进
            # `ttl_seconds`（见 `_beat_signals`）。
            self._beat_signals()
        except Exception as e:
            self.log(f"写回社交信号失败: {e}")

        self._m["last_settled"] = now
        self._m["last_settled_users"] = settled_users
        self._m["last_ready"] = len(anchored) + len(greets)
        self._m["last_candidate"] = (len(anchored) + len(greets) + len(threads)
                                     + len(loops) + len(promises) + len(closers)
                                     + len(misses) + len(intimacies))

        # ⚠️ 早退条件必须**逐个列出**所有候选桶。漏掉 misses 时，念想通道虽然被收集了
        # 却永远走不到发送段（它在最后面），表现是「好感 90% 的人一条都收不到」。
        if not greets and not anchored and not threads and not loops and not closers \
                and not promises and not misses and not intimacies:
            self.log("没有人攒够念头，本轮只是把时间补算上")
            # 没人可找时仍可能有冷群该破冰（破冰走群数据，不依赖 per-user 念头）
            if self.cfg.group_icebreak_enabled:
                try:
                    await self._run_icebreaks(bots, now)
                except Exception as e:
                    self.log(f"群破冰这一轮出错（不影响其它流程）：{e}")
            # G1：没冷场也可能该由她主动起个话头。
            if self.cfg.group_topic_enabled:
                try:
                    await self._run_group_topics(bots, now)
                except Exception as e:
                    self.log(f"群起话头这一轮出错（不影响其它流程）：{e}")
            # 早退也要落指标。`last_heartbeat` 是在 try_once 开头写进内存的，而落盘只
            # 发生在下面的发送段——于是**安静的时候仪表盘永远是死的**（last_heartbeat=0、
            # sent_total=0），而那恰恰是最需要它说话的时候：主人正要确认「她到底跑没跑」。
            self._persist_metrics()
            return

        # 每个角色这一轮的发送配额：只是防刷屏的护栏。节奏本身长在各自的
        # user_cooldown 与念头上，跟「上一个被找的人是谁」无关
        budget: Dict[str, int] = {
            bid: max(1, self.cfg.max_sends_per_round) for bid in bots
        }
        # 同一个人一轮里只服务一次：既到问候窗口、念头又攒满时，说一句早安就够
        served: Set[Tuple[str, str]] = set()

        # 本轮「尝试」配额与基础设施熔断（见 ATTEMPT_PER_SEND）。
        # 这两样与 `budget` 的区别：`budget` 管「发出去几条」，它们管「能试几次」。
        # 混成一个数的话，模型否决（NO，一次成功的调用）会把发送配额吃掉；而完全不
        # 管尝试次数时，一个坏掉的 provider 会让整轮 550 个候选全都跑一遍。
        attempt_left: Dict[str, int] = {
            bid: max(2, int(self.cfg.max_sends_per_round) * ATTEMPT_PER_SEND)
            for bid in bots
        }
        skipped_no_attempt: Dict[str, int] = {}
        infra_down: Dict[str, bool] = {"on": False}
        round_stats: Dict[str, int] = {
            "jobs": 0, "attempts": 0, "sent": 0, "vetoed": 0, "infra": 0, "other": 0,
        }

        # 逐人生成、批量调度：下面各通道只**收集**候选成 job（收集阶段不 await 发送），
        # 全部攒齐后再统一交给 speak() 并发处理。job 带齐发送所需的一切，外加可选的
        # 「发送成功后的 post 动作」（念想写 miss_day、想要了写 intimacy_asked_at）。
        def job_of(
            bid: str,
            uid: str,
            u: Dict[str, Any],
            urge: float,
            preset: Optional[Tuple[str, Dict[str, Any]]],
            note: str,
            require_about_peer: bool = False,
            post: Optional[Any] = None,
        ) -> Dict[str, Any]:
            # 不再把 `u` 塞进 job：`speak` 拿到锁之后会自己重新取一次
            # （`self.state.user(bid, uid)`），job 里那个引用是等锁前的旧快照。
            return {
                "bid": bid,
                "uid": uid,
                "urge": urge,
                "preset": preset,
                "note": note,
                "require_about_peer": require_about_peer,
                "post": post,
            }

        jobs: List[Dict[str, Any]] = []

        async def speak(job: Dict[str, Any]) -> bool:
            """发一条、记账、记账后稍隔几秒。发不出（被否决/失败）不耗配额。

            「检查 + 占用」在拿到 per-(bot,uid) 锁之后、下一个 await 之前一次做完：
            `served.add` 占住这个人，`budget[bid] -= 1` 占住这个角色的名额（失败再还
            回去）。两件事都必须在锁内、中间不夹 await——同一角色下不同人的 job 并发
            时各自的锁互不阻塞，若把占用留到 `await self._speak` 之后再做，它们会一起
            读到同一个 budget 值，把 max_sends_per_round 冲破。
            """
            bid = job["bid"]
            uid = job["uid"]
            preset = job["preset"]
            note = job["note"]
            if budget.get(bid, 0) <= 0:
                return False
            # 基础设施已经明确坏了：同一个 provider 的下一个请求只会得到同样的结果，
            # 剩下的候选一个都不用试。（`infra_down` 是一轮内的共享标志，并发下先
            # 失败的那条把它立起来，后面排队的在进锁前就能直接退出。）
            if infra_down["on"]:
                return False
            # 尝试配额：只花在「还有机会发出去」的候选上。见 ATTEMPT_PER_SEND。
            if attempt_left.get(bid, 0) <= 0:
                skipped_no_attempt[bid] = skipped_no_attempt.get(bid, 0) + 1
                return False
            # 短临界区：只做「检查 + 占位 + 取 u」。`_speak` 里有两次 LLM 调用
            # （秒级~数十秒），锁跨过它们就会把 `observe` 堵在锁上（它是
            # EventMessageType.ALL 的入口，同一个人连发两条消息时第二条要等第一条
            # 生成+发送完），而且第一条的 `record_outgoing` 会把 out 记在 in 前面，
            # conversation 顺序倒置 —— 正是这段注释声称要防的事，实际由锁的粒度造成。
            # 所以生成/发送放锁外，写回（退还/记账）时再取一次锁（读改写分离）。
            # 锁是 per-(bot,uid) 的，所以发 A 的时候不会把发 B 一起堵住。
            async with self._user_lock(bid, uid):
                if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                    return False
                if infra_down["on"] or attempt_left.get(bid, 0) <= 0:
                    return False
                # 检查与占用之间不能有 await，否则并发进来的同角色 job 会挤进这个窗口。
                served.add((bid, uid))
                budget[bid] -= 1
                attempt_left[bid] -= 1
                # 拿到锁之后重新取一次 u：等锁期间 observe 可能已经改过它，
                # 继续用等锁前那个引用就是在写一份过期的快照。
                u = self.state.user(bid, uid)
                if preset is not None:
                    _sent_txt, preset_meta = preset
                    # per-user 差异化的第二条通道。与 `_tier_note` 一起构成
                    # 「同一个人设、同一件事，对不同的人说出不同的话」所需的全部差异。
                    # 放在这里而不是各个 meta 构造点：那样六条通道要各写一遍，漏一条
                    # 那条就退回共享字幕。
                    preset_meta.setdefault("_interest", u.get("interest"))
                    preset_meta.setdefault("_message_count", u.get("message_count", 0))
                    preset_meta.setdefault("_avg_reply_seconds", u.get("avg_reply_seconds", 0.0))
            try:
                sent, result = await self._speak(
                    bid, uid, u, job["urge"], now, preset=preset,
                    require_about_peer=job["require_about_peer"],
                )
            except BaseException:
                # 生成/发送抛异常时把占的位和名额还回去：调用方只记一行
                # 「发送任务异常」，没人归还的话这一轮这个人和这个角色都被占死，
                # 后面的候选跟着少发。退还之后再抛，别把异常吞掉。
                async with self._user_lock(bid, uid):
                    served.discard((bid, uid))
                    budget[bid] += 1
                raise
            round_stats["attempts"] += 1
            if not sent:
                # 写回（重新取锁）：没发成就把占的位和名额都还回去，让后面的候选还有机会。
                async with self._user_lock(bid, uid):
                    served.discard((bid, uid))
                    budget[bid] += 1
                if self._is_infra_note(result):
                    # provider 已经坏了：剩下的人这一轮一个都不用试。这里不 return 之前
                    # 已经把 pos/配额还清了，其它并发中的候选拿到结果后也会看到这个标志。
                    infra_down["on"] = True
                    round_stats["infra"] += 1
                elif "不该说" in result or "不想说" in result:
                    round_stats["vetoed"] += 1
                else:
                    round_stats["other"] += 1
                try:
                    _cat = self._block_category(result)
                    self._round_skips[_cat] = self._round_skips.get(_cat, 0) + 1
                    if _cat == "其它":
                        _k = " ".join(str(result).split())[:46]
                        self._round_skip_samples[_k] = (
                            self._round_skip_samples.get(_k, 0) + 1
                        )
                except Exception:
                    pass
                # **逐人的结果只进 debug**。这行原来是 `self.log()`，而 `self.log()`
                # 在 cfg.debug=true 时走 INFO——线上就是它把日志刷成 550 行一轮。
                # 详细状态仍可从 /自主社交状态 的「最近决定」里看到，不必靠日志。
                logger.debug(f"[autonomous_social] {note}{uid} → {result}")
            if not sent:
                # 模型自己否决的另记在 veto_total；这里记的是「这一条最终没发出去」。
                # 两者会重叠，但方向不同：一个说「她不想说」，一个说「没发成」。
                self._count_block(result)
                self._note_decision(uid, f"没发：{result}")
                return False
            round_stats["sent"] += 1
            self.log(f"{note}{uid} → {result}")
            self._note_decision(uid, f"发了：{note}")
            post = job.get("post")
            if post is not None:
                # post 直接改 user dict（念想写 miss_day、想要了写 intimacy_asked_at），
                # 与 observe 的记账共用一把锁。
                async with self._user_lock(bid, uid):
                    post()
            if budget.get(bid, 0) > 0:
                # 这一轮还会找下一个人：稍微隔开几秒，别一口气把所有话说完
                await asyncio.sleep(random.uniform(2.0, 5.0))
            return True

        # 优先级：问候（有时间窗，错过就没了）> 追问（话正热着）> 回访（到点的事）
        # > 另起话题（念头攒满）> 收场。收场排最后：前三条都是「有正事说」，
        # 收场是「没正事也别冷着」。
        #
        # **这个优先级管的是「谁拿到这一轮的名额」，不是「谁先到线上」**：job 按这个
        # 顺序创建，而 `served`/`budget` 是在 per-user 锁内按创建顺序扣的，所以名额
        # 确实按优先级分配。但真正**发出**的先后取决于各自 LLM 返回的快慢
        # （`batch_send_concurrency` 默认 3，几条是并发跑的）—— 原来这里读起来像
        # 「高优先级先发出去」，那在并发下不成立，别按它去理解线上顺序。
        #
        # **每轮必须给「另起话题」留一个名额**。原来每轮只有 max_sends_per_round
        # （默认 3）条，前三条通道各有一个名额就吃光了——于是「她想找某个人说话」
        # 这个主通道整轮一条都发不出。那条通道恰恰是**关系最亲的人**唯一会走的
        # 通路（问候只有早/晚两个时间窗），所以表现是「好感最高的那个人一天收不到
        # 一条」。前三条有窗口期、错过还有下一次，另起话题没有——它攒的是念头。
        #
        # 做法：有 anchored 候选的角色，前三条通道合起来最多用 budget-1 条，把最后
        # 一条留给它（只在还有富余时留，不会把前三条压到 0）。以前是把整轮配额统一
        # 减 1——那既没给 anchored 留出名额，又让 max_sends_per_round=3 实际只发 2 条
        # （与面板说明对不上）。
        _anchored_bids = {row[0] for row in anchored}
        _pre_cap = {
            b: (max(0, budget.get(b, 0) - 1) if budget.get(b, 0) >= 2 else budget.get(b, 0))
            for b in _anchored_bids
        }
        _pre_used: Dict[str, int] = {}

        def pre_take(bid: str) -> bool:
            """前三条通道还能不能再占一条（给 anchored 留的那条不被它们吃掉）。"""
            cap = _pre_cap.get(bid)
            if cap is None:
                return True
            if _pre_used.get(bid, 0) >= cap:
                return False
            _pre_used[bid] = _pre_used.get(bid, 0) + 1
            return True

        # 问候：没有正事，随口说一句，看 TA 回不回。
        #
        # 触发不再是「到点了 + 今天还没问候过」——那是日历，真人打招呼看的是
        # **距上次说上话多久了**。所以这里纯静音时长驱动，隔了够久就能说，一天说几次
        # 由关系档和这句话当下的分量决定。
        for bid, uid, u, kind, _reason in greets:
            if budget.get(bid, 0) <= 0 or (bid, uid) in served or not pre_take(bid):
                continue
            tier, aff, warmth = self._tier_of(bid, uid, u, core_root, now)
            picks = self._anchor_pick(
                u, bodies.get(bid), now, limit=1, tier=tier, affection=aff, warmth=warmth,
                last_talk=self._core_last_interaction(core_root, bid, uid),
            )
            if not picks:
                # 问候本来就不要求「有事」：手上真没有具体的事时，也别把这条问候整条丢掉——
                # 直接给一句纯问候（不带由头）。避重由发送侧的复读检查兜着。
                meta = greet_meta(kind)
                meta["tier_note"] = anchors.tier_note(tier, aff, warmth)
                label = {"morning": "早安", "night": "晚安"}.get(kind, "打个招呼")
                jobs.append(job_of(bid, uid, u, 0.0, ("", meta), f"问候（{label}） "))
                continue
            a = picks[0]
            meta = greet_meta(kind)
            meta["anchor"] = dict(a)
            meta["anchor_fact"] = anchor_sentence(a)
            meta["about"] = str(a.get("about", ""))
            meta["tier_note"] = anchors.tier_note(tier, aff, warmth)
            label = {"morning": "早安", "night": "晚安"}.get(kind, "打个招呼")
            # 这里**不**开 require_about_peer。
            #
            # 那一道是给「念想」通道的：没有正事、只是想起一个人，整条却不提对方，
            # 那就是在讲自己的日记（197 条真实记录里最扎手的问题）。
            #
            # 但问候/分享恰恰相反：由头本身就是「她这边刚发生的一件具体的事」，
            # 「楼下便利店的关东煮今天有蟹棒诶」**正是真人会发的话**——真人分享自己
            # 看到的东西时不会句句带「你」。按「必须出现第二人称」去卡这一类，
            # 实测一天 155 个候选里 84 个被拦掉，量直接归零。
            jobs.append(job_of(
                bid, uid, u, 0.0, (anchor_sentence(a), meta), f"问候（{label}） "
            ))

        for bid, uid, u, kind, reason, gap in sorted(threads, key=lambda item: item[5]):
            if budget.get(bid, 0) <= 0 or not pre_take(bid):
                continue
            meta = thread_meta(
                kind,
                about=str(u.get("last_message", "") or ""),
                asked=str(u.get("last_spoken_text", "") or ""),
            )
            jobs.append(job_of(bid, uid, u, 0.0, (reason, meta), f"{kind}（沉默 {gap / 60:.0f} 分钟）"))

        for bid, uid, u, about, overdue in sorted(loops, key=lambda item: -item[4]):
            if budget.get(bid, 0) <= 0 or not pre_take(bid):
                continue
            jobs.append(job_of(
                bid, uid, u, 0.0,
                (loop_reason(about), loop_meta(about)),
                f"回访「{about}」（到期 {overdue / 60:.0f} 分钟后）",
            ))

        # 轮转：先按「最久没联系的优先」，念头只做同分次序。
        #
        # 原来是按念头降序。真实记录里 129 条有 100 多条落在同样 6 个人身上
        # （2047181070 收了 3 次、2163874801 收 3 次、3231002996 收 3 次…），
        # 另外 166 个人一条没收到。原因：**不回复的人的念头会一路涨到封顶 2.80，
        # 每轮都排第一**；会回复的人发完就清零，永远排在后面。插件等于只在跟
        # 从不回复的人说话。按 last_sent 升序就能把它摊开。
        # 排序分两层：**先关系档，同档内再按「最久没联系」。
        #
        # 原来只有后一层，而每轮只有 3 条、队列里有 176 个人——于是好感 100 的那位
        # 和陌生人在同一个队列里排队，她一天可能一条都收不到。这是「她不知道自己在
        # 对谁上心」的直接后果：**她算得出好感，算完却不用在选人上。**
        #
        # 为什么不是直接按好感排：那样就回到「6 个人霸占 100 多条、166 个人一条没有」
        # 那个更早的问题（念头涨到封顶的人每轮都排第一）。**同档内保持 last_sent
        # 轮转**，于是同一档里没人会被饿死，各档之间又按亲疏分出先后。
        anchored.sort(key=lambda x: (
            -self._relation_bucket(x[2]),
            _num(x[2].get("last_sent", 0) or 0),
            -x[3],
        ))
        # 走「另起话题」之前，前三条通道已经按 pre_cap 让出了名额（见上）。
        for bid, uid, u, urge, a in anchored:
            if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                continue
            why = self.gate_reason(bid, uid, u, now, bodies.get(bid))
            if why:
                self._count_block(why)
                # 逐人一行会把日志刷成「两百行同一个原因」（见 __init__ 里
                # `_round_skips` 的注释）。按分类聚到本轮汇总里。
                _cat = self._block_category(why)
                self._round_skips[_cat] = self._round_skips.get(_cat, 0) + 1
                logger.debug(f"[autonomous_social] 有由头但没说（{uid}）：{why}")
                continue
            jobs.append(job_of(
                bid, uid, u, urge,
                (anchor_sentence(a), anchor_meta(a)),
                f"因为{a.get('about')} urge={urge:.2f} ",
            ))

        # 兑现承诺排在「另起话题」之后：她是自己答应过这件事的，理由比随口找个话题足。
        for bid, uid, u, about in promises:
            if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                continue
            jobs.append(job_of(
                bid, uid, u, 0.0, (f"该兑现「{about}」了", promise_meta(about)),
                f"兑现承诺「{about}」 ",
            ))

        # 念想型：没有正事可说时不占「有正事」的优先级，但也别排到最后——
        # 它本来就是「闲下来想起一个人」的状态。
        def _miss_post(_bid: str, _uid: str) -> None:
            # 记上今天已发几条。上限不再是一天一条（那让「没有正事就是想找你」
            # 变成稀有事件，用户反馈里「每次发言都有事」的一半原因就在这）。
            st_u = self.state.user(_bid, _uid)
            st_u["miss_day"] = self._miss_mark_today(
                st_u, self._moment_of(_bid, self._time()).strftime("%Y-%m-%d")
            )
            self.state.mark_dirty()

        for bid, uid, u, a in misses:
            if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                continue
            jobs.append(job_of(
                bid, uid, u, 0.0,
                (anchor_sentence(a), anchor_meta(a)),
                f"念想「{a.get('about')}」 ",
                require_about_peer=True,
                post=lambda _b=bid, _u=uid: _miss_post(_b, _u),
            ))

        # 「想要了」：只对真的做过色色、且此刻欲望涨上来的人主动问一句（可不可以）。
        def _intimacy_post(_bid: str, _uid: str) -> None:
            self.state.user(_bid, _uid)["intimacy_asked_at"] = now
            self.state.mark_dirty()

        for bid, uid, u in intimacies:
            if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                continue
            jobs.append(job_of(
                bid, uid, u, 0.0,
                (_intimacy_reason(), _intimacy_meta()),
                "想要了（主动问一句可不可以）",
                post=lambda _b=bid, _u=uid: _intimacy_post(_b, _u),
            ))

        for bid, uid, u, reason in closers:
            if budget.get(bid, 0) <= 0:
                continue
            hung = (now - _num(u.get("last_sent", 0) or 0)) / 3600.0
            # 已读续接：对方回过（那句回话走的是正常聊天链路，不是插件发的）之后又
            # 没声了，这时候补的一句该**接着那几句说**。没有素材时退回老做法。
            jobs.append(job_of(
                bid, uid, u, 0.0,
                (
                    reason,
                    closer_meta(
                        str(u.get("last_spoken_text", "") or ""),
                        recent_exchange(u, 3),
                    ),
                ),
                f"收场（那句悬了 {hung:.1f} 小时）",
            ))

        # 有界并发：收集完成后统一调度。`batch_send_concurrency == 1` 时逐条 await，
        # 与改造前的串行行为一致（连发送之间那几秒间隔也一样）。
        #
        # 调度前先看 provider 是不是正处在失败退避里：在的话，这一轮一条都不用试。
        # 原来没有这道闸：每个候选都进 `_speak` → `_compose` 拿到 provider 再查退避
        # → 查到了返回 None → 引擎记「LLM 不可用」。550 个候选就是 550 行日志，而
        # 它们本来一秒就能判定出来「这个 provider 现在不能用」。
        round_stats["jobs"] = len(jobs)
        backoff_left = 0.0
        try:
            backoff_left = float(self.generator.llm_backoff_remaining())
        except Exception:
            backoff_left = 0.0
        if backoff_left > 0:
            infra_down["on"] = True
            if throttle.allow("round.llm_backoff", window=600.0):
                logger.warning(
                    f"[autonomous_social] LLM 处于失败退避中（还剩 {int(backoff_left)} 秒），"
                    f"本轮 {len(jobs)} 个候选全部跳过，不逐个重试"
                    + throttle.summary("round.llm_backoff")
                )

        limit = max(1, int(self.cfg.batch_send_concurrency))
        if limit == 1:
            # 串行分支也要隔离单个候选的异常：并发分支早就用 try/except 挡住了，
            # 而这里没有 —— 一个人炸了会带走整轮（后面的候选不发了、
            # `_persist_metrics` 不执行、还记一次 crash_streak 退避）。
            for job in jobs:
                try:
                    await speak(job)
                except Exception as exc:
                    self.log(f"发送任务异常（{job['uid']}）：{exc}")
        else:
            sem = asyncio.Semaphore(limit)

            async def _dispatch(job: Dict[str, Any]) -> None:
                async with sem:
                    try:
                        await speak(job)
                    except Exception as exc:  # 单个候选炸了不该带走这一轮其它人的发送
                        self.log(f"发送任务异常（{job['uid']}）：{exc}")

            await asyncio.gather(*(_dispatch(job) for job in jobs))

        # 一轮一行汇总。这是本轮「到底发生了什么」的唯一 INFO：候选几个、真的试了几次、
        # 发了几条、被否决几条、被闸门挡下几条（按原因）。原来这些信息分散在几百行
        # 逐人日志里，既看不出结论，又淹掉了真正该看的东西。
        self._m["last_round"] = dict(round_stats)
        self._m["last_round_skips"] = dict(self._round_skips)
        _skip_text = ""
        if self._round_skips:
            _skip_text = "；闸门挡下 " + "、".join(
                f"{name} {count}"
                for name, count in sorted(
                    self._round_skips.items(), key=lambda kv: -kv[1]
                )[:4]
            )
        skipped = sum(skipped_no_attempt.values())
        if round_stats["jobs"] or skipped or _skip_text:
            logger.info(
                f"[autonomous_social] 本轮候选 {round_stats['jobs']} 个，"
                f"尝试 {round_stats['attempts']} 次，发出 {round_stats['sent']} 条，"
                f"模型否决 {round_stats['vetoed']} 条，"
                f"其它未发 {round_stats['other']} 条"
                + (f"，尝试配额用完跳过 {skipped} 个" if skipped else "")
                + _skip_text
                + (
                    "；这些被归进「其它」的原因："
                    + "、".join(
                        f"{k}（{v}）"
                        for k, v in sorted(
                            self._round_skip_samples.items(), key=lambda kv: -kv[1]
                        )[:3]
                    )
                    if self._round_skip_samples
                    else ""
                )
            )

        # 运行指标刷盘：容器侧不用开日志就能从 state.json 的 _runtime 段判断
        # 「她到底跑起来没、卡在哪一步」。只存数字与短标签。
        self._persist_metrics()

        # 群冷场破冰：每个角色一轮最多给一个冷群破冰，别一口气把好几个群都点一遍。
        # 破冰与私聊发送各走各的配额，不与 per-user 互抢。
        # 这两条 await 在整轮的最末尾：异常逃出去 = 这一轮白跑 + `run()` 崩溃退避
        # （30s→30min），而且每轮复发。runner 内部已经按角色隔离，这里再兜一层。
        if self.cfg.group_icebreak_enabled:
            try:
                await self._run_icebreaks(bots, now)
            except Exception as e:
                self.log(f"群破冰这一轮出错（不影响其它流程）：{e}")
        # G1：不只在冷场时——她也可以在群里主动起个新话头。
        if self.cfg.group_topic_enabled:
            try:
                await self._run_group_topics(bots, now)
            except Exception as e:
                self.log(f"群起话头这一轮出错（不影响其它流程）：{e}")

    async def _speak(
        self,
        bid: str,
        uid: str,
        u: Dict[str, Any],
        urge: float,
        now: float,
        *,
        allow_veto: bool = True,
        preset: Optional[Tuple[str, Dict[str, Any]]] = None,
        require_about_peer: bool = False,
    ) -> Tuple[bool, str]:
        """把念头变成一条消息。返回 (是否发出, 说明文本)。

        只做一件事：把「正在输入」的收尾包住——开始生成时点亮，不管是从哪条路
        退出（发出、模型否决、验收拦下、发送失败、抛异常）都停掉。
        真正的逻辑在 `_speak_inner`。
        """
        umo = str(u.get("umo", "") or "")
        try:
            return await self._speak_inner(
                bid, uid, u, urge, now,
                allow_veto=allow_veto, preset=preset, require_about_peer=require_about_peer,
            )
        finally:
            if umo:
                try:
                    await self.input_state.stop(umo)
                except Exception as exc:  # pragma: no cover - 纯装饰
                    logger.debug(f"[autonomous_social] 停输入状态失败: {exc}")

    async def _speak_inner(
        self,
        bid: str,
        uid: str,
        u: Dict[str, Any],
        urge: float,
        now: float,
        *,
        allow_veto: bool = True,
        preset: Optional[Tuple[str, Dict[str, Any]]] = None,
        require_about_peer: bool = False,
    ) -> Tuple[bool, str]:
        """（实现）把念头变成一条消息。返回 (是否发出, 说明文本)。

        allow_veto=True 时模型可以回答「现在不该说」，这是自动循环的默认；手动触发
        传 False —— 主人明确要看效果，就别再替他否决了。

        preset 用于「跟进断掉的话」：那条的动机不是从念头里长出来的，不能交给
        generate_reason 重新找一个开口的理由。
        """
        # `now` 是这一**轮**心跳开始时刻，而一轮里前面的人各跑了两次 LLM，
        # 轮到这个人时它可能已经是几十秒到几分钟前。下面的闸门与记账
        # （`_track_aggression` 写 `aggr_note_at`、`_note_giveup` 写 `quiesced_at`）
        # 都当它是「现在」用，与同一函数里其它 `self._time()` 的口径分裂。
        # 这里重取一次，全函数统一；轮次指标仍用 `try_once` 那个 now。
        now = self._time()
        umo = str(u.get("umo", "") or "")
        if not umo:
            return False, "没有可用的会话来源（umo），发不出去"

        # 发送侧熔断：平台发不出去的时候，先把消息写好再失败是纯浪费。
        # 这道闸必须开在调 LLM 之前，否则它根本挡不住任何开销。
        breaker = self.send_breaker_left(bid)
        if breaker > 0:
            return False, f"发送侧熔断中（还剩 {int(breaker / 60)} 分钟），本轮不生成任何内容"

        # 累到没力气说话：这里才是**唯一**收口。
        # 原来把这条闸加在 `gate_reason` 里，只挡住了有由头的那一路（实测 61 条里
        # 过了 57 条）——问候/追问/回访/收场各自走自己的检查，根本不经过它。
        # 判据得落在所有开口都要经过的地方。
        try:
            body = self.core.bot_self_state(bid)
        except Exception:
            body = None
        # 读不出来时按 100.0（满精力）而不是 0：Core 没接上、或契约里没这个字段是
        # 常态，把它当「累到不能说话」会让整个插件在没接 Core 的机器上永久静默。
        # nan/inf 也走这条：一个坏字段不该让她一整天不开口。
        energy = _num((body or {}).get("energy"), 100.0)
        if energy < self.cfg.gate_energy_floor:
            logger.debug(f"[autonomous_social] 没发（{uid}）：她太累了（精力 {energy:.0f}）")
            return False, f"她太累了（精力 {energy:.0f}，坐不住也不想说话）"

        # 最小间隔：不管哪一种主动开口（问候/追问/回访/另起话题/收场），两条之间至少
        # 隔这么久。以前只有「另起话题」查 30 分钟护栏，其余四条全都绕过了它，
        # 早安 08:00、追问 08:12 这种扎堆从来没被挡过。
        #
        # 两条豁免：到点的由头（她想起一件具体的事，不该被间隔压住），以及「很想说」
        # （念头远超门槛——真人急着说某件事时不会先看上次几点发的）。
        # 关系档位决定这次离上一条得多近才合适。
        #
        # 以前是所有人一个 min_gap_minutes（默认 2 小时）一刀切，而她常聊的那个人本来
        # 就该被多找一点——结果是他一周只收到 2 条。现在按档位：高档 30 分钟，低档
        # 隔一两天试试就好。**没有日上限**——一天能发几次，取决于她当天有几件事发生。
        core_root = None
        try:
            core_root = self.core.read_root()
        except Exception as e:
            self.log(f"读 Core 状态目录失败: {e}")
        _tier, _aff, _warmth = self._tier_of(bid, uid, u, core_root, self._time())
        if self._quiesced(u):
            return False, "已经决定不再主动找 TA 了（等 TA 先开口）"

        # 上一条主动消息还悬着（发出去了、还没回）：别追着说。
        #
        # 这道闸以前只在 `gate_reason`（另起话题）与 `loop_gate`（回访）里查，
        # **问候、念想、分享全绕过了它**——用户完全不回时，她会每 30 分钟换一种
        # 由头接着发（实测 2 天 7 条，其中 2 条「想你」）。判据得落在所有开口的
        # 唯一收口上。
        #
        # 两类豁免：问候（早/晚安本来不指望回）、到点的由头（她想起一件具体的事）。
        # 「很想说」不豁免——连着两三次都没回应时，再“想说”也该等对方先开口。
        category = str((preset[1] or {}).get("category") or "") if preset else ""
        is_greet = category == "greet"
        has_cue = bool(live_cue(u, self._time()) if self.cfg.cue_followup else False)
        if not (is_greet or has_cue):
            try:
                silent_sent = int(_num(u.get("silent_sent", 0) or 0))
            except (TypeError, ValueError):
                silent_sent = 0
            if silent_sent >= COLD_SILENCE_SENT:
                return False, f"TA 一直没回、她已经主动发了 {silent_sent} 条，先等 TA 开口"
            if str(u.get("pending_result", "") or "") == "waiting":
                return False, "上一条主动发的还没回，现在再发就是追着说"

        gap_left = self._gap_left(u, _tier, self._time())
        if gap_left > 0 and not (self._urge_is_eager(u) or has_cue):
            return False, f"离上一条才过 {int(gap_left / 60)} 分钟，这次先不开口"

        # 每小时总预算（安全闸）。**念头不清零**——挡住这一轮，下一轮它还在，
        # 所以这只是「别一次涌出」，不是「今天到此为止」。
        if self.cfg.hourly_sends_cap > 0 and self._hourly_left(bid, self._time()) <= 0:
            return False, (
                f"这个角色这一小时已经发过 {self.cfg.hourly_sends_cap} 条，先歇着"
                "（念头保留，下一轮继续）"
            )

        # 这个人的上一次发送失败过：这会儿写好了也送不到。
        # 以前只有 cue/loop 两条路有重试节奏，收场/问候/追问发不出去时下一轮心跳
        # 照旧各写一遍完整 LLM——收场那条能空烧 8 小时。
        fail_left = _num(u.get("send_fail_until", 0) or 0) - self._time()
        if fail_left > 0:
            return False, f"上次发给 TA 没发出去，{int(fail_left / 60)} 分钟内不再试"

        # 私聊主动内容（可能很亲密，如「我好想你」）绝不能发进群：群会话只走群聊心
        # 流，那边用群感知的口吻生成。一个群 umo 出现在 1:1 用户池里必是幽灵（观察
        # 链路从不给群建 per-user 记录，只有旧版导入/群判定失误才会混进来），顺手清
        # 掉，别每个心跳都撞。
        if history_ingest.umo_kind(umo) == "group":
            self.state.bot(bid).get("users", {}).pop(uid, None)
            self.state.mark_dirty()
            return False, f"目标 {umo} 是群会话，不在这里发 1:1 主动消息（已清掉这条群幽灵）"
        user_core = None
        core_context = "没有可用的 Humanoid Core 状态。"
        try:
            if core_root:
                user_core = self.core.load_snapshot(bid, uid, root=core_root)
                if user_core:
                    core_context = self.core.compact(user_core, include_weather=False)
        except Exception as e:
            logger.debug(f"[autonomous_social] 加载用户 {uid} Core 快照失败: {e}")

        u_copy = dict(u)
        affection = user_core.get("affection") if user_core else None
        if affection is None:
            # 没接 Core 时好感度是空的，关系档位会永远停在「未知」；用在意程度估一个，
            # 它本来就由熟悉度与回复情况合成，比一律按陌生人写要准。
            #
            # 再加一道熟络度地板：在意程度里「TA 回不回我」占了一半，而回不回是**近期**
            # 状态。所以连着几次没回，一个认识两年的人会被整段拉回「刚认识不久」——
            # 落差大到不像人。这里保证相处时长不会被近期热度抹掉：聊得越多，
            # 地板越高。接了 Core 时好感度由 Core 自己管，这里不插手。
            #
            # 但在意程度本身还带着刻度错位：它实际落在 0.05~0.8 之间，硬乘 100 之后
            # 一个挺熟的人永远到不了「朋友」档（60），而连着几次没回又会被整段拉回
            # 「刚认识不久」（<20）——那种落差大到不像人。所以按熟络度加权混合成
            # 连续的滑动，而不是加一道会把它钉死的地板。
            try:
                msgs = int(_num(u.get("message_count", 0) or 0))
            except (TypeError, ValueError):
                msgs = 0
            blended = (
                AFFECTION_INTEREST_WEIGHT * _num(u.get("interest", 0.35) or 0.35)
                + AFFECTION_FAMILIAR_WEIGHT * desire.familiarity(msgs)
            )
            affection = round(blended * 100, 1)
        u_copy["_affection"] = affection
        # 关系档位（高/中/低）：模型得知道现在这个人跟她什么关系，否则它没法判断
        # 该不该开口。给的是一句人话，不是一堆数字。
        u_copy["_tier"] = _tier
        u_copy["_tier_note"] = anchors.tier_note(_tier, _aff, _warmth)
        u_copy["_energy"] = user_core.get("energy") if user_core else None
        u_copy["_social_energy"] = user_core.get("social_energy") if user_core else None
        # Core 记的「这个人上次说话」时刻。主动消息里的「距离上次说话」用它，
        # 不用社交层自己的 last_seen（那个只在它观察到消息时才更新，会落后几小时到几天）。
        u_copy["_last_interaction"] = user_core.get("last_interaction") if user_core else None
        # 接了 Core v2.14 的契约时这里有完整的身体：困不困、饿不饿、想说话的程度，
        # 以及她此刻正手上的事。拿不到契约时为空，生成器会退回旧的两个标量。
        u_copy["_body"] = user_core if (user_core or {}).get("contract_v") else None
        # 「记仇」的三道限在这里记账，生成器只读 target（它是 u 的副本，改不回去）
        self._track_aggression(u, user_core, now)
        u_copy["_aggr_high_n"] = int(_num(u.get("aggr_high_n", 0) or 0))
        u_copy["_aggr_recently_acted"] = bool(
            now - _num(u.get("aggr_note_at", 0) or 0) < AGGR_NOTE_COOLDOWN
        )
        # Core 替她记着的对方信息：称呼、对方说过什么、对方此刻的状态。
        # 以前这些只躺在 Core 的状态文件里，主动消息一条也用不上。
        if user_core:
            u_copy["_nickname"] = str(user_core.get("nickname") or "")
            u_copy["_said"] = list(user_core.get("said") or [])
            u_copy["_mood_tag"] = str(user_core.get("mood_tag") or "")
            u_copy["_attention"] = user_core.get("attention")

        # 会话库只提供用户与正常聊天历史；主动消息留在插件账本里，合并后
        # 一起作为本次主动消息生成的上下文，避免 bot 自己的话被覆盖掉。
        session_history = await self._load_session_history(umo)
        u_copy["conversation"] = self._generation_conversation(u, session_history)

        # 7 天主动消息日志（按 bid,uid 隔离）喂给生成侧防重复：比从会话史里扫 out 更准，
        # 不会把对方的话或别人的话混进来
        recent_pro = self.state.recent_proactive(bid, uid, now)
        u_copy["_recent_proactive"] = [
            str(e.get("text", "") or "").strip()
            for e in recent_pro
            if str(e.get("text", "") or "").strip()
        ]

        # **上一条主动消息有没有被接住**（v1.28.1）。
        #
        # 用户原话：「模型在下一次主动消息会把上一次的主动消息参考用户最晚的消息
        # 直接跨天问 结果就是没有上下文回复的很假！不知道说过啥就只会一个劲的问」。
        #
        # 根因有两半，这一半是「没有告诉模型对方压根没回」：
        # `last_spoken_text` 一直是她自己那句主动消息，而 `last_message` 是空的
        # （对方从那天起就没再开口）。生成侧只看到「你上一句说的是 X」，看不到
        # 「这句话没人接」，于是它照常写下一句——用户看到的就是「她明明问过了，
        # 又当没问过一样再问一遍」。
        #
        # `last_proactive_replied` 是 state 里早有的字段（回复窗口内对方开口就置 True），
        # 只是从来没有任何一处把它读进 prompt。这里补上，并同时给出「悬了多久」，
        # 因为「昨晚问的没人回」和「三分钟前问的还没回」是两件事。
        u_copy["_proactive_unanswered"] = bool(
            _num(u.get("last_proactive_sent", 0) or 0) > 0
            and not u.get("last_proactive_replied", True)
        )
        u_copy["_proactive_unanswered_hours"] = (
            round(max(0.0, now - _num(u.get("last_proactive_sent", 0) or 0)) / 3600.0, 1)
            if u_copy["_proactive_unanswered"]
            else 0.0
        )

        if preset is not None:
            reason, reason_meta = preset
        else:
            reason, reason_meta = generate_reason(u_copy, now, self._moment_of(bid, now).hour)

        # 人格按该用户的会话来源解析，不同角色/会话各用各自的人设
        try:
            _persona_name, persona_prompt = await self._persona(umo, bid)
        except Exception as e:
            self.log(f"解析人格失败: {e}")
            persona_prompt = ""

        mind = self._mind(bid, u, now, urge)


        def defer_cue() -> None:
            """这次因由头或挂着的事开口但没说成：往后推重试，别每个心跳都试同一件事。

            推的是 retry_at 而不是 due：due 是锚点、expire_at 是寿命，推它们等于
            每次都把「这件事已经凉了多久」清零，于是同一个由头可以每 6 小时重试一次、
            永不作废，而且每次重试都要跑一遍完整 LLM。
            """
            self._defer_cue_only(u, reason_meta)

        def fail_anchor() -> None:
            """这条由头这次没能说成（后续段验收不过 / 后续段撞车）：记一次失败。

            `defer_cue()` 只推 cue/loop 的重试节奏，对普通由头
            （`category=="anchor"` 且 `kind != KIND_CUE`）是**空操作**：少了这一步，
            同一条由头下一轮心跳会被重新挑中、再跑一遍完整 LLM，模型持续产出坏尾段
            时每轮复发，直到 14 天 TTL —— 一条都发不出去却持续烧 token。
            """
            _ra = (reason_meta or {}).get("anchor")
            if isinstance(_ra, dict) and _ra.get("about"):
                anchor_mod.mark_failed(u, str(_ra.get("about")), self._time())

        thread_anchor = self._last_said(u)
        # 从现在起她开始写话了：报「正在输入」。放到这一行而不是函数开头——前面
        # 全是本地的闸门与准备，没过闸门就返回的话不该点灯。
        try:
            await self.input_state.start(umo, uid)
        except Exception as exc:  # pragma: no cover - 纯装饰
            logger.debug(f"[autonomous_social] 启动输入状态失败: {exc}")
        parts: Optional[List[str]] = None
        veto_note = ""
        # 承诺只从 decide 的结构化输出里取（generate 路径没有那行 >），所以两条分支
        # 都得有这个变量——原来只在 llm_gate 分支里定义，关掉闸门时下面读它会 NameError。
        decision = None
        # 清洗前的原始模型输出。**必须是局部变量**：原来存在 `self._raw_generated`
        # 这个实例级单字段上，`batch_send_concurrency=3` 时 A 的验收失败日志与
        # `last_rejected` 可能是 B 的原文，跨用户内容串台、还会经状态页显示给主人。
        raw_generated = ""
        # 记忆联动：装了记忆插件就先把这个人的长期记忆拉出来，揉进这次生成。
        # 检索跟着**这次的由头**走，不是空串。
        memory_note = await self._fetch_memory_note(umo, uid, query=reason)
        if allow_veto and self.cfg.llm_gate:
            try:
                decision = await self.generator.decide(
                    umo, u_copy, core_context, reason, reason_meta, persona_prompt, mind,
                    at=now, clock_offset=self._clock_offset_for(bid), memory_note=memory_note,
                )
            except Exception as e:
                if throttle.allow(f"speak.decide.{bid}"):
                    logger.warning(
                        f"[autonomous_social] LLM 调用失败（provider 取到了但请求挂了）: {type(e).__name__}: {e}"
                        + throttle.summary(f"speak.decide.{bid}")
                    )
            raw_generated = str(getattr(decision, "raw_text", "") or "")
            if decision is None:
                # LLM 不可用/退避中/超时：这是基础设施故障，不是「模型觉得不该说」。
                # 两者走同一条 defer_cue 会把一个真实的由头（如「明天面试」）静默作废：
                # 一次持续十几小时的 key 失效就够把 3 次重试预算全烧光。
                note = "LLM 不可用（provider 取不到），本轮未发送"
                self._last_llm_error[bid] = note
                return False, note
            if not decision.send:
                self._m["last_failure"] = f"[{bid}] 模型说现在不该说：{decision.why_not[:40]}"
                self._m["veto_total"] = int(self._m.get("veto_total", 0)) + 1
                # **否决不消耗由头。**
                #
                # 模型说「现在不该说」，说的是**这个人此刻不该被找**，不是「这件由头她
                # 驾驭不了」。所以由头留着，下一次状态变了还可以再说。
                #
                # 但也不能完全当无事发生：念头轻退避（降一点、门槛抬一点）免得每轮都
                # 同一个念头再来一次。真正消耗由头的是下面那道验收——那是「她驾驭不了
                # 这句话」，再给机会也只是浪费。两者必须分开算。
                desire.after_skip(u, now)
                u["last_skip_reason"] = decision.why_not
                # 轻退避：`desire.after_skip` 已经把「多久之后可以再试」落在
                # last_skip_at / last_skip_reason 上（由头不消耗，只是别每轮都问同一句）。
                # 这里不再另写一个没人读的 retry_at——那是 defer_cue 时代的残留。
                self.state.mark_dirty()
                self.state.save()
                return False, f"想过，但觉得现在不该说：{decision.why_not}"
            parts = decision.parts
        else:
            try:
                parts = await self.generator.generate(
                    umo, u_copy, core_context, reason, reason_meta, persona_prompt, mind,
                    at=now, clock_offset=self._clock_offset_for(bid), memory_note=memory_note,
                )
            except Exception as e:
                if throttle.allow(f"speak.generate.{bid}"):
                    logger.warning(
                        f"[autonomous_social] 生成消息失败: {e}"
                        + throttle.summary(f"speak.generate.{bid}")
                    )
            if not parts:
                note = "LLM 生成为空（provider 不可用或调用失败）"
                self._last_llm_error[bid] = note
                return False, note

        # 生成期间对方可能发了新消息
        latest = self.state.user(bid, uid)
        if self._time() - _num(latest.get("last_seen", 0)) < FINAL_CHECK_GAP_SECONDS:
            defer_cue()
            return False, "对方刚发了新消息，不插话"

        # ── 验收层 ──────────────────────────────────────────────
        # SEND/NO 协议只保证**格式**（首行是不是 SEND、有没有正文），完全不保证内容。
        # 真实记录里这些直接进了用户手机：约 40 条整条是「想表达…」这种描述说话的话、
        # 约 10 条括号不配平、9 条只剩括号动作没有正文、某一晚 23 条同一件事复读。
        # 判不过就**不发**（不重生成、不退回）——宁可少发一条，不可发一条废话。
        ok, why = verify_message(
            parts[0],
            recent=[e.get("text", "") for e in self.state.recent_proactive(bid, uid, self._time())],
            called=str(self._last_persona.get(bid, "") or ""),
            require_about_peer=require_about_peer,
        )
        # ── 跨用户撞车 ─────────────────────────────────────────
        #
        # `_check_repeat` 只跟**这个人**最近几条比。素材却是共用的一份：Core 的日程
        # 挂在角色级（`mood` 在用户级，`daily_schedule` 在角色级），所以「刚忙完个案
        # 笔记」这句话对同一 bot 下所有人**逐字相同**。32 条真实记录里 16:10~16:51
        # 这 1 小时 41 分里有 12 个不同的人分别收到它——那不是复读，是 12 个人读了
        # 同一行字幕，在用户眼里就是群发。这道结构上不可能被那道复读检查抓到。
        # ── 事件级跨用户护栏 ────────────────────────────────────
        #
        # 下面那道是**措辞级**的（比文本相似度），而撞车真正发生在「哪件事」这一层：
        # 同一个「刚忙完个案笔记」可以用完全不同的说法派给 12 个人，文本相似度只有
        # 0.02~0.06，措辞级那条永远抓不到（实测 33 分钟里「刚忙完歇着/刚忙完窝着」
        # 派了 3 次给 3 个人，cross_user_ratios 全是 0.02~0.06）。
        #
        # 所以这里比的是**由头原文**：同一件事在窗口内派给别人过了就不派。
        # 不依赖任何阈值，也不会因为换个说法就漏掉。
        if ok and self.cfg.cross_user_repeat_hours > 0:
            _about = str((reason_meta or {}).get("about") or "")
            _a2 = (reason_meta or {}).get("anchor")
            if not _about and isinstance(_a2, dict):
                _about = str(_a2.get("about") or "")
            if _about:
                _used_by = self.state.anchor_recent_users(
                    bid, self._time(), hours=self.cfg.cross_user_repeat_hours
                ).get(_about, [])
                if _used_by and uid not in _used_by:
                    self._m["anchor_cross_user_total"] = int(
                        self._m.get("anchor_cross_user_total", 0)) + 1
                    ok = False
                    why = f"「{_about[:16]}」刚发给过别人了，换一个"
                    # 这件事本身该换一件：当场作废，让下一轮心跳派别的由头。
                    # （不是在这一轮里重试——本轮候选已经收完，重试要等下一轮。）
                    drop_anchor(u, _about, self._time())
        if ok and self.cfg.cross_user_repeat_hours > 0:
            others = self.state.recent_proactive_others(
                bid, uid, self._time(), hours=self.cfg.cross_user_repeat_hours
            )
            hit = _check_cross_user(parts[0], others, threshold=self.cfg.cross_user_repeat_ratio)
            # 校准：把「这条和最近别人那条差多少」记进运行指标。
            # 阈值不拍脑袋——跑一天看真实分布再定线（见 CHANGELOG）。
            self._note_cross_user_ratio(parts[0], others)
            if hit:
                ok, why = False, hit
            else:
                # 上面那条比的是**正文**。动作标签是另一回事：正文可以完全不同，
                # 括号里挂的却是同一套动作。线上 524 条里 15 个动作标签被用在了
                # 多个人身上（「蜷在沙发」10 人、「伸个懒腰」9 人）——按人比对的
                # `_check_action_repeat` 永远查不到，用户看到的就是「她跟谁都这样」。
                # 窗口沿用同一个「跨人重复小时数」，不再新增一个开关。
                hit2 = _check_cross_user_action(parts[0], others)
                if hit2:
                    ok, why = False, hit2
        if not ok:
            self._m["last_failure"] = f"[{bid}] 验收不过：{why}"
            self._m["last_rejected"] = (raw_generated or parts[0])[:200]
            self._m["rejected_total"] = int(self._m.get("rejected_total", 0)) + 1
            self._m["rule_total"] = int(self._m.get("rule_total", 0)) + 1
            if throttle.allow("verify.rejected", window=1800.0):
                logger.info(
                    f"[autonomous_social][{bid}] 验收不过，未发送：{why}｜"
                    f"原文：{(raw_generated or parts[0])[:120]!r}"
                    + throttle.summary("verify.rejected")
                )
            # 记下来，主人能在状态页/主动消息记录里看到「本来要发什么、被哪条拦下」
            u["last_rejected"] = why
            _ra = (reason_meta or {}).get("anchor")
            if isinstance(_ra, dict) and _ra.get("about"):
                anchor_mod.mark_failed(u, str(_ra.get("about")), self._time())
            self.state.mark_dirty()
            defer_cue()
            return False, f"验收不过：{why}"

        # 由头在**真的发出去**这一刻才消费。前面任何一步失败都不算——
        # 一次模型抽风不该让这个话题冷掉 7 天。
        #
        # 拆段与间隔不在这里猜：交给该 Bot 在 AstrBot 里的「分段回复」设置。
        # 没开分段的 Bot：多段合并为整条发；开了的：按它的拆分法与间隔逐段发。
        segments, waits = pacing.plan_segments(
            parts, umo, self.context,
            allow_burst=bool(getattr(self.cfg, "allow_burst", True)),
        )
        if not segments:
            return False, "拆段规划为空"
        # 逐段验收：`parts[0]` 过了**不代表后续段也过**——模型常在结尾再补一句纯括号
        # 的内心（「（记住了 宝宝16岁…）」），拆段后那一段会**单独**发出去，看起来就是
        # 「内心独白被当成发言发了一条」。旧版只验 parts[0]，这一路完全免检。
        # 群心流早改成逐段验了，私聊侧补上（含念想通道的 require_about_peer）。
        _recent = [
            e.get("text", "")
            for e in self.state.recent_proactive(bid, uid, self._time())
        ]
        bad_seg, seg_why = self._verify_all_segments(
            segments, recent=_recent,
            called=str(self._last_persona.get(bid, "") or ""),
            require_about_peer=require_about_peer,
        )
        if bad_seg:
            self._m["last_failure"] = f"[{bid}] 后续段验收不过：{seg_why}"
            self._m["last_rejected"] = str(bad_seg)[:200]
            self._m["rejected_total"] = int(self._m.get("rejected_total", 0)) + 1
            self._m["rule_total"] = int(self._m.get("rule_total", 0)) + 1
            if throttle.allow("verify.rejected", window=1800.0):
                logger.info(
                    f"[autonomous_social][{bid}] 后续段验收不过，未发送：{seg_why}｜"
                    f"原文：{str(bad_seg)[:120]!r}" + throttle.summary("verify.rejected")
                )
            u["last_rejected"] = seg_why
            self.state.mark_dirty()
            # 这条由头这次是「她驾驭不了这句话」，得记一次失败——否则 `defer_cue()`
            # 对普通由头是空操作，下一轮会被重新挑中，每轮复发。
            fail_anchor()
            defer_cue()
            return False, f"后续段验收不过：{seg_why}"
        # 后续段的跨用户撞车也补一道：旧版只拿 parts[0] 比，补发的段会绕过去。
        if self.cfg.cross_user_repeat_hours > 0:
            others = self.state.recent_proactive_others(
                bid, uid, self._time(), hours=self.cfg.cross_user_repeat_hours
            )
            for seg in segments:
                hit = _check_cross_user(seg, others, threshold=self.cfg.cross_user_repeat_ratio)
                if hit:
                    self._m["last_failure"] = f"[{bid}] 后续段和发给别人的太像：{hit}"
                    u["last_rejected"] = hit
                    self.state.mark_dirty()
                    # 同上：这件事该换一件，记一次失败，别每轮重试同一条由头。
                    fail_anchor()
                    defer_cue()
                    return False, hit
        # 配额在**发送之前**占掉，且与发送之间不留 await（见 `_hourly_reserve`）。
        # 上面那道早退只是省一次 LLM，真正的闸门在这里。
        if not self._hourly_reserve(bid):
            return False, "这个角色这一小时已经发够了，先歇着（念头保留，下一轮继续）"
        sent_ok, err, peer_unreachable = await self._send_with_retry(umo, segments[0], bid)
        if not sent_ok:
            self._hourly_refund(bid)
            # 发送失败是可以重试的（换一轮心跳、换个时间再来），但不该拿掉这件事
            # 本身——所以推的是 retry_at，寿命 expire_at 不动。
            self._defer_cue_only(u, reason_meta)
            platform_side = self._platform_problem(err or "")
            self._mark_send_failed(u, platform_side=platform_side, reason=err or "")
            # 平台侧的问题（适配器没就绪/不支持主动消息/抖动）与「他」无关：
            # 无条件计 giveup 时，两次这样的失败就把**低档**用户写 `quiesced_at`，
            # 而它只由「对方先开口」解除 —— 平台恢复后她再也不主动找这个人。
            if not platform_side:
                self._note_giveup(u, bid, self._time())
            self._m["last_failed"] = self._time()
            self._m["last_failure"] = f"[{bid}] 发送失败：{(err or '原因未知')[:70]}"
            # 只在平台明确说「这个人发不了」（非好友/已注销）时才隔离。平台没就绪、
            # 不支持主动消息、结果未知这些都不是他的错，隔离只会让一个健康的人
            # 凭空消失 24 小时，而且被当成「TA 不想理我」写进关系账本
            if peer_unreachable:
                self.state.block_send(
                    bid, uid, self._time() + SEND_BLOCK_HOURS * 3600.0, err
                )
                if throttle.allow(f"send.blocked.{bid}.{uid}"):
                    logger.warning(
                        f"[autonomous_social] {uid}（bid={bid}, umo={umo}）平台报告发不到这个人，"
                        f"隔离 {SEND_BLOCK_HOURS}h：{err}" + throttle.summary(f"send.blocked.{bid}.{uid}")
                    )
            elif self._platform_problem(err) and throttle.allow(f"send.platform.{bid}"):
                logger.warning(
                    f"[autonomous_social] {uid} 发送失败但原因在平台侧（不隔离该用户）：{err}"
                    + throttle.summary(f"send.platform.{bid}")
                )
            return False, f"消息写好了但发送失败：{err}"

        sent_ts = self._time()
        # 「你有多长时间没回我」——记下来，写进运行指标，主人一眼能看到
        try:
            u["reply_gap_hours"] = round(
                max(0.0, sent_ts - _num(u.get("last_seen", 0) or 0)) / 3600.0, 1
            )
        except (TypeError, ValueError):
            pass
        _a = (reason_meta or {}).get("anchor")
        if isinstance(_a, dict) and _a.get("about"):
            anchor_mod.consume(u, str(_a.get("about")), sent_ts)
        # 配额已经在发送前占掉了（见上面的 `_hourly_reserve`），这里不再记一次。
        self._m["last_sent"] = sent_ts
        self._m["sent_total"] = int(self._m.get("sent_total", 0)) + 1
        msg_type = reason_meta.get("msg_type") if reason_meta else None
        category = str((reason_meta or {}).get("category") or "")
        # 她在这条里又许了新诺？记下来，到点她会自己兑现
        promise = str(getattr(decision, "promise", "") or "")
        if promise:
            self.state.note_promise(u, promise, sent_ts)
        if category == "promise":
            # 这件已经做了，别再提醒自己
            _done_about = str((reason_meta or {}).get("about") or "").strip()
            kept = [
                i for i in promise_entries(u)
                if str(i.get("about", "")).strip() != _done_about
            ]
            u["promises"] = kept
            # 兑现了 → 落一条长期记忆。中间层不再管它，但这件事真的发生过。
            if _done_about:
                self._remember_open_closed(
                    umo, uid, _done_about, segments[0], kind="promise"
                )
        # 问候与收场本来就不要求对方回：它们不进「等一句回话」的计时器，
        # 否则「从来没回过早安」会被当成「连着被冷落」，念头天花板永久压死，
        # 而衰减的锚点（last_sent）又每天被问候刷新，永远等不到。
        _sent_about = str((reason_meta or {}).get("about") or "")
        if not _sent_about:
            _a = (reason_meta or {}).get("anchor")
            if isinstance(_a, dict):
                _sent_about = str(_a.get("about") or "")
        self.state.record_outgoing(
            bid, uid, segments[0], msg_type,
            about=_sent_about,
            expect_reply=category not in ("greet", "closer"),
            why=self._why_now(bid, u, category, reason, reason_meta, sent_ts),
            store_text=self.cfg.store_message_text,
        )
        # 记忆联动：把这条主动消息写回记忆库（后台任务，失败不影响主链路）。
        # 带上由头：将来按由头检索时，它就是「这件事她开过口」的证据。
        self._remember_proactive(umo, uid, segments[0], sent_ts, about=_sent_about)
        # 发成功了，之前那次失败就不再约束他
        u["send_fail_count"] = 0
        u["send_fail_until"] = 0.0
        u["platform_fail_count"] = 0
        # 失败原因也跟着清：留着它会让状态页一直显示一条早就过去的故障
        u["send_blocked_reason"] = ""
        # 发成了 → 不再是「连着发不出去」
        u["giveup_count"] = 0
        u["quiesced_at"] = 0.0
        if category in ("probe", "presence"):
            # 记的是「这次追问发出去的时刻」。thread_reason 用它判断：
            # **追过一次之后对方有没有说过新话**——说过就是新断点，可以说；
            # 没说就还是那个断点，不再追第二遍。
            #
            # 以前记的是断点时间戳并拿它跟 last_said 比相等，而 last_said 每次都
            # 会被她自己这条消息推进，等式第二次就必然不成立，守卫等于没有。
            # 真实表现：一个每小时回一句的用户，7 天被追问 44 次。
            u["thread_asked_at"] = sent_ts
            u["thread_for"] = thread_anchor
            u["thread_at"] = sent_ts
        elif category == "loop":
            # 问过这一件就不再挂着，否则每次心跳都会把它捡回来问一遍；
            # 其余几件留着——一次只问一件，不是把整叠清空
            self._drop_loop(u, str((reason_meta or {}).get("about") or ""))
            u["loop_at"] = sent_ts
        elif (category == "cue"
              or str((_a or {}).get("kind") or "") == anchors.KIND_CUE):
            # 时间锚点被当成由头说出口了：消费掉它，别让「你们说好的：…」在提示词里
            # 挂到 36 小时寿命结束。问候/收场不会走到这里（它们的 meta 没有这条由头），
            # 所以「一句早安把『TA 说明天面试』吃掉」那条老问题不会回来。
            u["cue"] = ""
            u["cue_due"] = 0.0
            u["cue_expire_at"] = 0.0
            u["cue_retry_at"] = 0.0
            u["cue_tries"] = 0
        elif category == "closer":
            u["closer_for"] = self._closer_marker(u, sent_ts)
            u["closer_at"] = sent_ts
        elif category == "greet":
            # 今天这个窗口已经问候过：记下日期与种类，同一窗口不再发第二遍
            u["greet_kind"] = str((reason_meta or {}).get("kind") or "")
            u["greet_day"] = self._moment_of(bid, sent_ts).strftime("%Y-%m-%d")
            u["greet_at"] = sent_ts
            # 问候本来就是「不用回」的话（晚安没人理很正常）：预先记成已收场，
            # 别让收场路径 8 小时后给一句没被回的早安补「没事 我就是随口一说」。
            # 与 closer 分支走同一个标记助手，免得写成 sent_ts 与 last_sent 不等值，
            # 让 closer_reason 的等值守卫失效（等于预告收场没生效）。
            u["closer_for"] = self._closer_marker(u, sent_ts)
        bot = self.state.bot(bid)
        bot["last_global_send"] = sent_ts
        self.state.save()
        self.signals.note_proactive(uid, sent_ts, bid)
        if len(segments) > 1:
            self._schedule_burst(bid, uid, segments[1:], waits, sent_ts)
            veto_note = f"\n（按这个 Bot 的分段设置，稍后还会补 {len(segments) - 1} 条）"
        self._last_llm_error.pop(bid, None)
        return True, (
            f"已主动联系 {uid}（类型={msg_type}，念头={urge:.2f}）：\n{segments[0]}{veto_note}"
        )

    def _track_aggression(
        self,
        u: Dict[str, Any],
        user_core: Optional[Dict[str, Any]],
        now: float,
    ) -> None:
        """「她心里记着这件事」的两道限：持续高位才算、而且用完有节制。

        Core 的 per-user mood.aggression 是**她对 TA 的不满**，由 TA 的话引起。以前
        只在 28 以上就翻成「心里有点不满」塞进提示词——而这个值常年 0~20，等于她
        天天有气。现在要连着几次采样都在高位，而且用过一次之后三天内不再提。
        记账在 state 上，生成器只读（target 是 u 的副本，在那里改是改不掉的）。
        """
        mood = (user_core or {}).get("mood")
        raw = mood.get("aggression") if isinstance(mood, dict) else None
        try:
            value = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            value = 0.0
        try:
            prev = int(_num(u.get("aggr_high_n", 0) or 0))
        except (TypeError, ValueError):
            prev = 0
        n = prev + 1 if value >= AGGR_NOTE_THRESHOLD else 0
        if n != prev:
            u["aggr_high_n"] = n
            self.state.mark_dirty()
        if n >= AGGR_NOTE_SAMPLES and now - _num(u.get("aggr_note_at", 0) or 0) >= AGGR_NOTE_COOLDOWN:
            u["aggr_note_at"] = now
            self.state.mark_dirty()

    def _why_now(
        self,
        bid: str,
        u: Dict[str, Any],
        category: str,
        reason: str,
        reason_meta: Optional[Dict[str, Any]],
        sent_ts: float,
    ) -> str:
        """这一条为什么这时候发（给主人看的依据）。

        以前日志里只有正文，主人看到的是一句没头没尾的话。这里把「依据」显式记下来：
        是到点的由头、还是念头攒了多久、还是对方提过什么。判断不了就给空串，
        绝不编——它是给人看的，不是给她自己看的。
        """
        bits: List[str] = []
        try:
            urge = _num(u.get("urge", 0.0) or 0.0)
        except (TypeError, ValueError):
            urge = 0.0
        if urge > 0:
            try:
                bits.append(f"念头攒到 {urge:.2f}")
            except (TypeError, ValueError):
                pass
        about = str((reason_meta or {}).get("about") or "").strip()
        if category == "cue" and u.get("cue"):
            bits.append(f"到点的由头：{str(u.get('cue'))[:20]}")
        elif category == "loop" and about:
            bits.append(f"回访：{about[:20]}")
        elif category == "closer":
            bits.append("她上次的主动消息没人接")
        elif category == "promise" and about:
            bits.append(f"兑现她答应过的事：{about[:20]}")
        elif category == "greet":
            mode = str((reason_meta or {}).get("mode") or "")
            kind = {"greet_morning": "早安", "greet_night": "晚安", "greet_midday": "午间招呼"}
            bits.append(f"{kind.get(mode, '问候')}窗口")
        elif category in ("probe", "presence"):
            bits.append("话说到一半断了")
        elif u.get("last_ignored_at"):
            bits.append("之前被冷落过，这次主动一点")
        if not bits and str(reason or "").strip():
            bits.append(str(reason)[:40])
        return "；".join(bits)[:60]

    def _hourly_left(self, bid: str, now: float) -> int:
        """这个角色这一小时还能发几条。返回 0 表示已经用完（配置为 0 则永远不限）。

        每小时总预算是**防失控的安全闸**，不是节奏——节奏由关系档的最小间隔管。
        档位高的人一天能发好几次，但也不该出现「一小时内连着七条」。
        """
        cap = int(getattr(self.cfg, "hourly_sends_cap", 0) or 0)
        # 裁剪必须**先做**：原来 cap<=0 时提前 return 999999，跳过了唯一的裁剪行，
        # 而 `_hourly_take` 是无条件 append 的 —— 于是把每小时上限关掉（cap=0）
        # 之后这个列表只增不减，长跑进程里就是一条稳定泄漏。
        window = [ts for ts in self._hourly.get(bid, []) if now - ts < 3600.0]
        self._hourly[bid] = window
        if cap <= 0:
            return 999999
        return max(0, cap - len(window))

    def _hourly_take(self, bid: str, now: float) -> None:
        # 上限关掉时不记时间戳：记了也没人会去裁剪它（见 `_hourly_left`）。
        if int(getattr(self.cfg, "hourly_sends_cap", 0) or 0) <= 0:
            return
        try:
            self._hourly.setdefault(bid, []).append(now)
        except Exception:
            pass

    def _hourly_reserve(self, bid: str) -> bool:
        """发送**之前**占掉这一小时的一个名额。返回 False 表示已用满、没有占。

        检查与占位之间没有 await，所以同一角色下的并发发送不会一起挤过这道闸 ——
        原来「生成（秒级 await）→ 发送 → 记账」的顺序下，几个用户会同时通过
        「这一小时还剩几条」的检查，`hourly_sends_cap=12` 实际能发出 14 条。
        cap<=0（不限）时不占位，直接放行。
        """
        if int(getattr(self.cfg, "hourly_sends_cap", 0) or 0) <= 0:
            return True
        now = self._time()
        if self._hourly_left(bid, now) <= 0:
            return False
        self._hourly_take(bid, now)
        return True

    def _hourly_refund(self, bid: str) -> None:
        """把刚占掉的名额退回去（消息没发出去时）。按后进先出退最近占的那个。"""
        window = self._hourly.get(bid)
        if not window:
            return
        window.pop()
        if not window:
            self._hourly.pop(bid, None)

    def _min_gap_left(
        self, u: Dict[str, Any], tier: str = "", bid: str = "", now: float = 0.0
    ) -> float:
        """距上一条主动消息多久了。**按关系档位**算，档位越高近得越理所当然。

        以前是所有人一个 `min_gap_minutes`（默认 2 小时），而她常聊的那个人本来就该
        被多找一点——一刀切的结果是：他 7 天只收到 2 条。
        """
        if not tier and bid:
            try:
                tier = self._tier_of(bid, str(u.get("__uid", "")), u, None, now or self._time())[0]
            except Exception:
                tier = anchors.TIER_MID
        if not tier:
            tier = anchors.TIER_MID
        return self._gap_left(u, tier, now or self._time())

    @staticmethod
    def _urge_is_eager(u: Dict[str, Any]) -> bool:
        """念头是否远超门槛（她现在确实很想说这句话）。"""
        try:
            urge = _num(u.get("urge", 0.0) or 0.0)
            gate = _num(u.get("fire_gate") or desire.FIRE_THRESHOLD)
        except (TypeError, ValueError):
            return False
        # 念头天花板是 URGE_CEILING（3.0），而门槛 ~2.1 乘 1.45 已经越过它——
        # 直接比会恒假（settle 把 urge 夹到 3.0），「很想说」这个特性从未生效。
        # 把阈值夹到天花板：攒到顶就是「想说到不能再想」。
        eager_line = min(
            desire.URGE_CEILING, max(gate, desire.FIRE_THRESHOLD) * EAGER_URGE_RATIO
        )
        return urge >= eager_line

    def _defer_cue_only(
        self, u: Dict[str, Any], reason_meta: Optional[Dict[str, Any]]
    ) -> None:
        """由头/挂着的这件事这次没说成：只推重试节奏，不动寿命。"""
        category = str((reason_meta or {}).get("category") or "")
        kind = str(((reason_meta or {}).get("anchor") or {}).get("kind") or "")
        if category == "cue" or kind == anchors.KIND_CUE:
            self._defer_anchor(u, "cue")
        elif category == "loop":
            self._defer_anchor(u, "loop")

    def _representative_umo(self, bid: str) -> str:
        """这个角色最近有来往的一个会话来源。用来解析它实际会用的那个 provider。"""
        users = self.state.bot(bid).get("users", {}) or {}
        best, best_ts = "", 0.0
        for u in users.values():
            umo = str(u.get("umo", "") or "")
            if not umo:
                continue
            ts = max(
                _num(u.get("last_seen", 0) or 0),
                _num(u.get("last_sent", 0) or 0),
            )
            if ts > best_ts:
                best, best_ts = umo, ts
        return best

    async def _trigger_precheck(self, bid: str = "") -> Tuple[bool, str]:
        """手动触发的第一道闸：引擎到底在不在正常工作。

        `enabled` 只说明「允许发」，不等于「后台循环活着」：插件刚重载、正在停机、
        或主循环已抛异常退出时，enabled 仍然是 true。原来的触发路径完全没查这些，
        于是「插件已经不在正常工作」的时候，手动触发照样把一整轮状态推着往前走——
        念头结算掉、冷却记上、结果还是发不出去。

        Returns:
            (是否可以触发, 不能触发时给主人看的说明)
        """
        if not self.running:
            return False, (
                "引擎没有在运行（插件刚重载、正在停机，或后台循环已退出），不触发。"
                "在面板里「重载」一次插件即可。"
            )
        if not self.cfg.enabled:
            return False, "插件已停用（enabled=false），不触发。"
        backoff = 0.0
        scope = ""
        try:
            # 退避是 per-provider 的，所以要按这个角色实际会用的那一格来问。
            # 不问的话就是「所有人里最长的那个」，A 的 key 坏掉时 B 也触发不了。
            umo = self._representative_umo(bid)
            provider = await self._get_provider(umo) if umo else None
            if provider is not None:
                scope = self.generator._scope_of(provider)
                backoff = float(self.generator.llm_backoff_remaining(scope))
            else:
                # 拿不到 provider 就退一步问「所有人里最长的那个」：宁可多挡一次手动
                # 触发，也不要在明显有模型故障时让人以为触发了却什么都没发出来。
                backoff = float(self.generator.llm_backoff_remaining())
        except Exception:
            backoff = 0.0
        if backoff > 0:
            return False, (
                f"模型正在失败退避中（{scope or '该模型'}，还剩 {int(backoff // 60)} 分钟），"
                "现在触发也生成不出内容。退避结束后会自动恢复；"
                "急用可以先在面板里检查模型配置与 key。"
            )
        return True, ""

    async def preview_once(self, bid: str, uid_filter: str = "") -> str:
        """只生成、不发送：看一眼她此刻想对谁开口、打算说出一句什么。

        全程不开「正在输入」、不消费由头、不记账、不动冷却——就是一次 dry run。
        可选带一个人 ID「主动消息预览 12345」只看那个人。

        `settle_minds` 会**就地**结算念头与冷落（写 urge/interest，settle_pending
        还可能把「被冷落」提前定案），所以预览前把该角色的用户子树深拷一份，
        不管从哪条路退出都原样还原。也不在这里 flush（本来就没有真改动要落盘）。
        """
        ok, why = await self._trigger_precheck(bid)
        if not ok:
            return why
        now = self._time()
        users = self.state.bot(bid).get("users", {})
        if not users:
            return "还没有可预览的用户（都没人私聊过 bot，且历史导入与播种名单都是空的）。"

        # dry run：保存用户子树，结束后还原——预览不该改变任何人的念头/兴趣/冷落。
        #
        # 还原必须**逐人就地写回**，不能把整棵 `users` 换掉：`_preview_run` 里有 LLM
        # 调用（秒级 await），这期间真实心跳与 `observe` 完全可能给**别人**记了账
        # （last_sent、proactive_log、conversation、last_seen）。整树替换会把这些
        # 真实改动一起抹掉 —— 而它们已经记进内存、还没落盘，抹掉就永久丢了。
        users_snapshot = copy.deepcopy(users)
        try:
            return await self._preview_run(bid, now, uid_filter)
        finally:
            live = self.state.bot(bid).get("users")
            if isinstance(live, dict):
                for uid, snap in users_snapshot.items():
                    cur = live.get(uid)
                    # 预览没动过这个人（dict 完全相等）就一个字都不碰：`_preview_run`
                    # 里的 `settle_minds` 会遍历**所有**用户，逐人还原会把并发进来
                    # 的真实记账一起抹掉。只有真被改过的那几个人才需要还原。
                    if not isinstance(cur, dict) or cur == snap:
                        continue
                    # **逐字段就地还原，不换 dict**：`live[uid] = snap` 会把 live 树里
                    # 那个 dict 换成快照副本，而并发 `_speak_inner` 手里还攥着旧引用
                    # （它在锁外跑两次 LLM），后续所有 per-user 记账会写进一个已经不在
                    # 树里的 dict，然后被 GC —— 用户收到了消息，一半记账在盘上、一半蒸发。
                    cur.clear()
                    cur.update(snap)

    async def _preview_run(self, bid: str, now: float, uid_filter: str) -> str:
        """（预览实现，无副作用）拼上下文、调模型拿一句，不发送不改状态。"""
        core_root = None
        bot_state = None
        try:
            core_root = self.core.read_root()
            if core_root:
                bot_state = self.core.bot_self_state(bid, root=core_root)
                self._remember_clock(bid, bot_state)
                self._remember_night(bid, bot_state)
        except Exception as e:
            logger.warning(f"[autonomous_social] 预览时读 Core 失败: {e}")

        ranked = self.settle_minds(
            bid, now, self._moment_of(bid, now), core_root, bot_state, min_urge=0.0
        )
        if not ranked:
            return "没有可预览的用户（都没有会话来源，或都还在刚聊完的窗口里）。"
        ordered = sorted(ranked, key=lambda x: x[2], reverse=True)
        target = str(uid_filter or "").strip()
        if target:
            filtered = [row for row in ordered if str(row[0]) == target]
            if not filtered:
                # C1：说清「现在为什么发不出去」——把这个人单独过一遍闸门。
                try:
                    why = self._why_not_sending(bid, target, now)
                except Exception:
                    why = ""
                if why:
                    return f"{target} 此刻不会收到主动消息：{why}"
                return f"名单里没有 {target}，或 TA 还没有可用的会话来源。"
            ordered = filtered

        tried: List[str] = []
        for uid, u, urge in ordered[:TRIGGER_CANDIDATE_LIMIT]:
            if history_ingest.umo_kind(str(u.get("umo", "") or "")) == "group":
                continue
            # 与 trigger_once 同一道「正在热聊」过滤：刚聊过的人真实不会开口，
            # 预览也不该拿它当样例。
            seen_gap = now - _num(u.get("last_seen", 0) or 0)
            if seen_gap < HOT_CHAT_THRESHOLD:
                tried.append(f"{uid}：刚聊过，现在不会开口")
                continue
            try:
                text = await self._preview_one(bid, uid, u, urge, now, core_root)
            except Exception as e:
                tried.append(f"{uid} 出错：{e}")
                continue
            if text:
                head = f"[预览] {uid}（念头 {urge:.2f}）——只生成、没有发送："
                tail = f"\n\n（前面 {len(tried)} 位没生成出来，才轮到 TA）" if tried else ""
                return head + "\n" + text + tail
            tried.append(f"{uid}：这次没生成出内容")
        return "候选都试过了，没生成出内容：" + "；".join(tried)

    def _why_not_sending(self, bid: str, uid: str, now: float) -> str:
        """C1：这个人此刻为什么发不出去——把各道闸门的理由串起来。

        预览时用来回答「我明明设了，为什么不发」。取不到就返回空串。
        """
        try:
            u = self.state.user(bid, uid)
        except Exception:
            return ""
        if not u.get("umo"):
            return "还没有可用的会话来源（TA 没私聊过 bot）"
        try:
            if self.state.is_send_blocked(bid, uid, now):
                return "发送隔离中（之前发给 TA 失败过）"
        except Exception:
            pass
        try:
            why = self.gate_reason(bid, uid, u, now)
        except Exception:
            why = ""
        if why:
            return why
        try:
            urge = _num(u.get("urge", 0.0) or 0.0)
        except (TypeError, ValueError):
            urge = 0.0
        return f"闸门都过了，只是念头还没攒够（现在 {urge:.2f}，要到 {desire.FIRE_THRESHOLD:.2f} 左右）"

    def _intimacy_pick(
        self,
        bid: str,
        now: float,
        body: Optional[Dict[str, Any]],
        core_root: Optional[Dict[str, Any]],
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """「想要了」通道：只对**真的做过色色**、且此刻**欲望涨上来**的人，主动问一句可不可以。

        「做过色色」是硬门槛（ccb_done_count>0）——填 ID 不够，好感高也不够；
        「欲望涨上来」是当下状态——有过亲密史也不免检，否则一旦有过就变成一直触发。
        """
        out: List[Tuple[str, Dict[str, Any]]] = []
        for uid, u in self.state.bot(bid).get("users", {}).items():
            if not u.get("umo") or self.state.is_send_blocked(bid, uid, now):
                continue
            if _body_asleep(body):
                continue
            if self._quiet_now(bid, self._moment_of(bid, now).hour):
                continue
            try:
                if sleep_signal(u, now):
                    continue
            except Exception:
                pass
            try:
                if self.gate_reason(bid, uid, u, now, body):
                    continue
            except Exception as e:
                # 闸门自己出错时**绝不能当「过了」**：原来这里 `except: pass`，
                # 等于一个坏字段就让「想要了」这条通道整体绕过闸门。
                # 宁可这个人这一轮不选，也不能绕过。
                if throttle.allow(f"intimacy.gate.{bid}"):
                    logger.warning(
                        f"[autonomous_social][{bid}] {uid} 的闸门判断出错，"
                        f"跳过这个人（不绕过闸门）：{e}"
                        + throttle.summary(f"intimacy.gate.{bid}")
                    )
                continue
            snap = None
            try:
                snap = self.core.load_snapshot(bid, uid, root=core_root) if core_root else None
            except Exception:
                snap = None
            if not isinstance(snap, dict):
                continue
            try:
                aff = _num(snap.get("affection") or 0.0)
            except (TypeError, ValueError):
                aff = 0.0
            if aff < float(getattr(self.cfg, "intimacy_affection_min", 70) or 70):
                continue
            mood = snap.get("mood") if isinstance(snap.get("mood"), dict) else {}
            try:
                lib = _num(mood.get("libido", 0.0) or 0.0)
                # 同 gate_reason 的 `base_aggression`：基线常常就是 0（她从没什么欲望），
                # `base_libido or lib` 会把 0 当缺失、base 变成当前值，「欲望涨上来」这道
                # 硬门槛于是永不成立——整条「想要了」通道形同不存在。
                raw_base = mood.get("base_libido")
                base_lib = lib if raw_base is None or raw_base == "" else _num(raw_base, lib)
            except (TypeError, ValueError):
                lib = base_lib = 0.0
            try:
                done = _num(snap.get("ccb_done_count") or 0.0)
            except (TypeError, ValueError):
                done = 0.0
            if done <= 0:
                continue
            rise = float(getattr(self.cfg, "intimacy_libido_rise", 6.0) or 6.0)
            if (lib - base_lib) < rise:
                continue
            try:
                last = _num(u.get("intimacy_asked_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                last = 0.0
            cool = float(getattr(self.cfg, "intimacy_cooldown_hours", 48) or 48) * 3600.0
            if last > 0 and now - last < cool:
                continue
            out.append((uid, u))
        return out

    async def _preview_one(
        self, bid: str, uid: str, u: Dict[str, Any], urge: float, now: float,
        core_root: Optional[Dict],
    ) -> str:
        """（预览实现）拼出与真实发送同一份上下文与由头，只调模型拿文本，不发送。

        u 传的是 settle_minds 返回的副本，但拼上下文要读最新那份，所以进锁后重新取。
        """
        async with self._user_lock(bid, uid):
            u = self.state.user(bid, uid)
            umo = str(u.get("umo", "") or "")
            if not umo:
                return ""
            tier, aff, warmth = self._tier_of(bid, uid, u, core_root, now)
            user_core = None
            core_context = "没有可用的 Humanoid Core 状态。"
            try:
                if core_root:
                    user_core = self.core.load_snapshot(bid, uid, root=core_root)
                    if user_core:
                        core_context = self.core.compact(user_core, include_weather=False)
            except Exception as e:
                self.log(f"预览加载用户 {uid} Core 快照失败: {e}")
            u_copy = dict(u)
            affection = user_core.get("affection") if user_core else None
            if affection is None:
                try:
                    msgs = int(_num(u.get("message_count", 0) or 0))
                except (TypeError, ValueError):
                    msgs = 0
                blended = (
                    AFFECTION_INTEREST_WEIGHT * _num(u.get("interest", 0.35) or 0.35)
                    + AFFECTION_FAMILIAR_WEIGHT * desire.familiarity(msgs)
                )
                affection = round(blended * 100, 1)
            u_copy["_affection"] = affection
            u_copy["_tier"] = tier
            u_copy["_tier_note"] = anchors.tier_note(tier, aff, warmth)
            u_copy["_energy"] = user_core.get("energy") if user_core else None
            u_copy["_social_energy"] = user_core.get("social_energy") if user_core else None
            u_copy["_last_interaction"] = user_core.get("last_interaction") if user_core else None
            u_copy["_body"] = user_core if (user_core or {}).get("contract_v") else None
            if user_core:
                u_copy["_nickname"] = str(user_core.get("nickname") or "")
                u_copy["_said"] = list(user_core.get("said") or [])
                u_copy["_mood_tag"] = str(user_core.get("mood_tag") or "")
                u_copy["_attention"] = user_core.get("attention")
            session_history = await self._load_session_history(umo)
            u_copy["conversation"] = self._generation_conversation(u, session_history)
            recent_pro = self.state.recent_proactive(bid, uid, now)
            u_copy["_recent_proactive"] = [
                str(e.get("text", "") or "").strip()
                for e in recent_pro
                if str(e.get("text", "") or "").strip()
            ]
            # 预览路径同样要告诉模型「上一条主动消息没人接」（见 _speak_inner 处长注释）。
            u_copy["_proactive_unanswered"] = bool(
                _num(u.get("last_proactive_sent", 0) or 0) > 0
                and not u.get("last_proactive_replied", True)
            )
            u_copy["_proactive_unanswered_hours"] = (
                round(max(0.0, now - _num(u.get("last_proactive_sent", 0) or 0)) / 3600.0, 1)
                if u_copy["_proactive_unanswered"]
                else 0.0
            )
            reason, reason_meta = generate_reason(
                u_copy, now, self._moment_of(bid, now).hour
            )
            try:
                _persona_name, persona_prompt = await self._persona(umo, bid)
            except Exception as e:
                self.log(f"预览解析人格失败: {e}")
                persona_prompt = ""
            mind = self._mind(bid, u, now, urge)
            memory_note = await self._fetch_memory_note(umo, uid, query=reason)
            decision = None
            if self.cfg.llm_gate:
                try:
                    decision = await self.generator.decide(
                        umo, u_copy, core_context, reason, reason_meta, persona_prompt, mind,
                        at=now, clock_offset=self._clock_offset_for(bid), memory_note=memory_note,
                    )
                except Exception as e:
                    self.log(f"预览判断该不该说时出错: {e}")
                if decision is not None and not decision.send:
                    return f"（她想了想，觉得现在不该开口：{decision.why_not}）"
            if decision is not None and decision.parts:
                return str(decision.parts[0])
            try:
                parts = await self.generator.generate(
                    umo, u_copy, core_context, reason, reason_meta, persona_prompt, mind,
                    at=now, clock_offset=self._clock_offset_for(bid), memory_note=memory_note,
                )
            except Exception as e:
                self.log(f"预览生成消息出错: {e}")
                parts = None
            if not parts:
                return ""
            return str(parts[0])

    async def trigger_once(self, bid: str) -> str:
        """手动触发一次主动联系：绕过念头是否攒满，直接挑最想说的人说一句。

        仍然避开「对方正在热聊」的情况 —— 主人要看效果，不代表可以去打扰正在聊天的人。

        Args:
            bid: 发起命令的 bot ID

        Returns:
            结果描述文本（供命令回显）
        """
        ok, why = await self._trigger_precheck(bid)
        if not ok:
            return why

        now = self._time()
        # 连点限频：一次触发最多 3 个候选、每个要跑两轮 LLM，连点会把这些调用成倍放大，
        # 而且看到的还是**同一批候选人**（念头不会因为你多点几下就攒满）。
        # 用 getattr 容错：这是节流而不是正确性要求，对象上没有这个属性时就不限频。
        last_trigger = getattr(self, "_last_trigger_at", None)
        if last_trigger is not None:
            since = now - last_trigger
            if 0 <= since < TRIGGER_MIN_INTERVAL_SECONDS:
                return (
                    f"刚触发过，{int(TRIGGER_MIN_INTERVAL_SECONDS - since)} 秒后再试"
                    "（连点只会把同一批候选人的生成重复一遍）"
                )
        self._last_trigger_at = now

        if now - self._last_flush > FLUSH_INTERVAL_SECONDS:
            self.state.flush()
            self._last_flush = now

        users = self.state.bot(bid).get("users", {})
        if not users:
            return (
                "还没有可联系的用户（都没人私聊过 bot，且历史导入与播种名单都是空的）。"
                "可先和 bot 说句话，或在配置里打开 history_ingest / 填 seed_users。"
            )

        core_root = None
        bot_state = None
        try:
            core_root = self.core.read_root()
            if core_root:
                bot_state = self.core.bot_self_state(bid, root=core_root)
                self._remember_clock(bid, bot_state)
                self._remember_night(bid, bot_state)
        except Exception as e:
            if throttle.allow("core.read"):
                logger.warning(
                    f"[autonomous_social] 触发时读 Core 失败: {e}" + throttle.summary("core.read")
                )

        # min_urge=0：不管攒没攒满都列出来，手动触发挑念头最高的那个
        ranked = self.settle_minds(
            bid, now, self._moment_of(bid, now), core_root, bot_state, min_urge=0.0
        )
        if not ranked:
            return "没有可联系的用户（都没有会话来源，或都还在刚聊完的窗口里）。"

        # 回退：按念头从高到低挨个试，第一个发不出去就换下一个。
        # 手动触发的目的是「看一眼效果」，模型挂一次、平台抖一下就直接把失败
        # 原样甩回给主人，等于白点一次；换个人往往就成了。
        ordered = sorted(ranked, key=lambda x: x[2], reverse=True)
        tried: List[str] = []
        for uid, u, urge in ordered[:TRIGGER_CANDIDATE_LIMIT]:
            seen_gap = now - _num(u.get("last_seen", 0) or 0)
            if seen_gap < HOT_CHAT_THRESHOLD:
                tried.append(
                    f"{uid} 正在热聊（{int(seen_gap / 60)} 分钟前还在说话），跳过"
                )
                continue
            try:
                # 取 per-(bot,uid) 锁：手动触发与并发心跳可能同时挑中同一个人，
                # 两边都过 gap/pending 检查就会各发一条（也绕过 observe 的互斥）。
                # 手动触发本身有 60 秒限频，锁内跑一次 LLM 可以接受。
                async with self._user_lock(bid, uid):
                    u = self.state.user(bid, uid)
                    sent, note = await self._speak(
                        bid, uid, u, urge, now, allow_veto=False
                    )
            except Exception as e:
                # 单个人失败不该让整次触发报废：记一笔，继续试下一个
                self.log(f"手动触发 {uid} 异常：{e}")
                tried.append(f"{uid} 出错：{e}")
                continue
            if sent:
                self.log(f"手动触发 {uid}：{note}")
                if tried:
                    return note + f"（前面 {len(tried)} 位没轮到，才轮到 TA）"
                return note
            tried.append(f"{uid}：{note}")
        return "候选都试过了，没发出去：" + "；".join(tried)

    async def _send_message(self, target: str, text: str) -> None:
        """发送消息，兼容多种 API。

        Args:
            target: 目标（统一消息来源）
            text: 消息文本

        Raises:
            _SendFailed: 发送失败。unknown=True 表示投递结果未知，不能再发一遍
        """
        if not target:
            raise _SendFailed("unified_msg_origin 为空，无法发送主动消息")

        # 方式1: AstrBot 官方主动消息 API —— 优先使用 MessageChain
        if hasattr(self.context, "send_message"):
            chain = None
            if MessageChain is not None:
                try:
                    chain = MessageChain().message(text)
                except Exception as e:
                    # 构造是**纯本地**操作，失败就确定没发出去，回退纯文本是安全的。
                    logger.warning(
                        f"[autonomous_social] MessageChain 构造失败，回退纯文本: {e}"
                    )
                    chain = None
            if chain is not None:
                try:
                    ok = await self.context.send_message(target, chain)
                    if ok is False:
                        # 框架只说「没发出去」，没说是谁的問題：可能 umo 失效，也可能平台
                        # 压根不支持主动消息。这两种都不该当成「这个人发不了」
                        raise _SendFailed(
                            "send_message 返回 False：未找到匹配的会话平台"
                            "（umo 可能已失效，或该平台不支持主动消息）"
                        )
                    return
                except _SendFailed:
                    raise
                except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                    # 投递结果未知：请求可能已经到了平台并被转发，只是回执没回来。
                    # 这里回退到纯文本再发一次，就是给用户发两条一样的消息
                    raise _SendFailed(
                        f"发送超时或连接中断，结果未知（不重发）: {type(e).__name__}: {e}",
                        unknown=True,
                    ) from e
                except (TypeError, ValueError, AttributeError, KeyError) as e:
                    # 本地/类型层面的问题（多半是这版 AstrBot 不认 MessageChain 这个参数），
                    # 消息不可能已经发出去 —— **只有这几类**才回退纯文本。
                    logger.warning(
                        f"[autonomous_social] MessageChain 不被接受，回退纯文本: {e}"
                    )
                except Exception as e:
                    _err_text = str(e)
                    if self._is_unreachable_error(_err_text):
                        # 平台明确说「这个人发不了」（不是好友 / 会话已失效）：
                        # 消息**确定没送达**，可以据此隔离该用户，也不存在重复投递。
                        raise _SendFailed(
                            f"context.send_message 调用失败: {_err_text}",
                            peer_unreachable=True,
                        ) from e
                    # 其余异常来自发送本身（平台侧风控/限流/序列化之后才炸）：
                    # **结果未知，绝不能回退重发** —— 那时消息可能已经送达，
                    # 用户会收到两条一样的。与 docstring 里 unknown=True 的契约一致。
                    raise _SendFailed(
                        f"context.send_message 调用失败（结果未知，不重发）: {_err_text}",
                        unknown=True,
                    ) from e
            # 部分兼容版本可能接受纯文本
            try:
                ok = await self.context.send_message(target, text)
                if ok is False:
                    raise _SendFailed(
                        "send_message 返回 False：未找到匹配的会话平台"
                        "（umo 可能已失效，或该平台不支持主动消息）"
                    )
                return
            except _SendFailed:
                raise
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                raise _SendFailed(
                    f"发送超时或连接中断，结果未知（不重发）: {type(e).__name__}: {e}",
                    unknown=True,
                ) from e
            except Exception as e:
                # 与 MessageChain 路径同构：这里异常发生在**调用之后**（回执处理/
                # 序列化阶段才炸），消息可能已经送达 —— 标 `unknown=True`，
                # `_send_with_retry` 就不会再发一遍（否则用户收到两条一样的话）。
                raise _SendFailed(
                    f"context.send_message 调用失败（结果未知，不重发）: {e}",
                    peer_unreachable=self._is_unreachable_error(str(e)),
                    unknown=True,
                ) from e

        # 方式2: 通过 platform 客户端发送（旧版兼容）
        if hasattr(self.context, "platform") and hasattr(self.context.platform, "send_message"):
            # 与方式1 同构的异常分类。原来这里是**裸 await**：旧版部署走这条路时，
            # 「非好友/已注销」变成通用异常 → `_send_with_retry` 视为可重试且
            # `peer_unreachable=False` → 该用户永不被隔离（每轮重试、每轮一次 LLM），
            # 同时把 bid 级熔断累到让整个角色停发。
            try:
                await self.context.platform.send_message(target, text)
                return
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                # 投递结果未知：消息可能已经送达，不能再发一遍。
                raise _SendFailed(
                    f"platform.send_message 发送超时或连接中断，结果未知（不重发）: "
                    f"{type(e).__name__}: {e}",
                    unknown=True,
                ) from e
            except Exception as e:
                _err_text = str(e)
                if self._is_unreachable_error(_err_text):
                    # 平台明确说「这个人发不了」：消息**确定没送达**，据此隔离该用户。
                    raise _SendFailed(
                        f"platform.send_message 调用失败: {_err_text}",
                        peer_unreachable=True,
                    ) from e
                # 其余异常来自发送本身：结果未知，不重发（否则用户收到两条一样的）。
                raise _SendFailed(
                    f"platform.send_message 调用失败（结果未知，不重发）: {_err_text}",
                    unknown=True,
                ) from e

        # 方式3: 兜底
        raise _SendFailed("无法找到可用的发送消息 API")

    async def _send_with_retry(
        self, target: str, text: str, bid: str = ""
    ) -> Tuple[bool, str, bool]:
        """发送一条消息，失败自动重试。返回 (是否成功, 最后错误, 是否「对这个人发不了」)。

        第三个值才决定要不要隔离该用户：它只在平台明确说「不是好友/会话失效」时为真。
        平台没就绪、adapter 未加载、结果未知这些一概不算——那不是这个人的错。

        「结果未知」的失败不重试：可能已经送到了，再发一次就是给同一个人发两条一样的。

        无论成败都会更新熔断计数：连续发不出去时，下一轮就不再先写一遍消息再失败了。
        """
        last_err = ""
        peer_unreachable = False
        for attempt in range(SEND_RETRY_COUNT + 1):
            try:
                await self._send_message(target, text)
                self._note_send_ok(bid)
                return True, "", False
            except _SendFailed as e:
                last_err = str(e)
                peer_unreachable = peer_unreachable or e.peer_unreachable
                if e.unknown:
                    # 异常消息可能是空的（框架把异常 repr 吞了的时候），那时只打
                    # 「结果未知: 」后面什么都没有，排查时等于没有信息。补上异常类型，
                    # 至少能分清是 ApiNotAvailable（没连上）还是别的东西。
                    _detail = str(e).strip()
                    if not _detail:
                        _detail = f"{type(e).__name__}（无消息，通常是适配器未就绪或连接抖动）"
                    if throttle.allow(f"send.unknown.{bid or '-'}"):
                        logger.warning(
                            f"[autonomous_social] 发送结果未知（不重试，避免重复投递）: {_detail}"
                            + throttle.summary(f"send.unknown.{bid or '-'}")
                        )
                    self._note_send_failure(bid, last_err, peer_unreachable)
                    # 第三个值必须原样带出去：`_send_message` 的纯文本回退路径会同时
                    # 置 unknown 与 peer_unreachable（「调用失败，结果未知」+ 平台说
                    # 这个人不是好友）。这里写死 False 的话，平台明说的「不是好友」
                    # 永远传不到 `_speak_inner`，那个人就不会被隔离 24 小时——每轮
                    # 心跳仍然花一次 LLM 生成再失败，退避也走通用档而不是隔离档。
                    return False, last_err, peer_unreachable
            except Exception as e:
                last_err = str(e)
            if attempt < SEND_RETRY_COUNT:
                # 中间的重试不再单独打日志：一次发送失败只留最后那一条，
                # 免得一次失败就刷好几行（手机端一行警告就占满屏）。
                await asyncio.sleep(SEND_RETRY_DELAY)
            elif throttle.allow(f"send.failed.{bid or '-'}"):
                _retried = f"（已重试{SEND_RETRY_COUNT}次）" if SEND_RETRY_COUNT else ""
                logger.warning(
                    f"[autonomous_social] 发送失败{_retried}: {last_err}"
                    + throttle.summary(f"send.failed.{bid or '-'}")
                )
        self._note_send_failure(bid, last_err, peer_unreachable)
        return False, last_err, peer_unreachable

    def _breaker_slot(self, bid: str) -> Dict[str, Any]:
        """取该角色的熔断格。bid 为空时归到一个公共格（拿不到角色的调用点用）。"""
        return self._send_breaker.setdefault(
            bid or "", {"streak": 0, "until": 0.0, "err": ""}
        )

    def send_breaker_left(self, bid: str = "") -> float:
        """这个角色还剩多少秒熔断。0 = 没在熔断。"""
        return max(0.0, float(self._breaker_slot(bid).get("until", 0.0)) - self._time())

    def _note_send_failure(self, bid: str, err: str, peer_unreachable: bool) -> None:
        """连续发不出去就把生成也停下来：写好的消息送不出去，那次调用就是纯浪费。

        但**只在故障是全局的时候**才停：一个失效的 umo 失败三次，不该让整个角色
        停 15 分钟（线上就是这样，每轮都在熔断里空转，健康的人一条也收不到）。
        """
        if peer_unreachable:
            # 平台明说「这个人发不了」：是这一个用户的事，不该把所有人一起停掉
            return
        if _umo_problem(err):
            # 这条 umo 自己废了：冷却已经按人记在 `_mark_send_failed` 里，
            # 不该再拿它去熔断整个角色。
            return
        if _target_problem(err):
            # 平台拒发给**这一个**目标（禁言/风控/retcode 1200）：同一次心跳里
            # 别的目标照常能发出，按目标退避就够了，不熔断整个角色。
            return
        if not _global_problem(err):
            # 认不出来是什么原因（线上真出现过异常文本为空：`str(e) == ""`）：
            # 按目标级处理。熔断判错的代价是**所有人**收不到消息，宁可不熔断。
            return
        slot = self._breaker_slot(bid)
        slot["streak"] = int(slot.get("streak", 0)) + 1
        slot["err"] = str(err or "")
        if slot["streak"] < SEND_BREAKER_TRIP:
            return
        # 指数也要封顶。`streak` 本身不封顶是对的（它记录真实失败次数），但
        # `2 ** (streak - TRIP)` 在 streak 上千时会抛 `OverflowError: int too large
        # to convert to float`——而抛点就在这里，由 `_send_with_retry` 的 except 分支
        # 调用，异常会冒泡出 `_speak_inner`，把这次失败的记账（streak / until / err）
        # 连同 `_mark_send_failed`、`_note_giveup` 一起丢掉，且此后每次失败都复发。
        # 2**16 已经远超 SEND_BREAKER_MAX_SECONDS，再大没有意义。
        delay = min(
            SEND_BREAKER_BASE_SECONDS
            * (2 ** min(slot["streak"] - SEND_BREAKER_TRIP, _BREAKER_EXP_CAP)),
            SEND_BREAKER_MAX_SECONDS,
        )
        slot["until"] = self._time() + delay
        # 节流 key 带 bid：A 的故障不该把 B 的熔断日志一起静默掉
        if throttle.allow(f"send.breaker.{bid or '-'}"):
            logger.error(
                f"[autonomous_social][{bid or '默认'}] 连续 {slot['streak']} 次发不出去，"
                f"暂停生成 {int(delay / 60)} 分钟（到期自动恢复，期间一条 token 都不花）："
                f"{err or '原因未知'}" + throttle.summary(f"send.breaker.{bid or '-'}")
            )

    def _note_send_ok(self, bid: str) -> None:
        slot = self._breaker_slot(bid)
        if slot.get("streak"):
            logger.info(
                f"[autonomous_social][{bid or '默认'}] 发送已恢复"
                f"（此前连续失败 {slot['streak']} 次），主动消息继续"
            )
        # 只清自己那一格：以前是「任何一次成功就全局清零」，于是 B 每发成一条就把 A
        # 的失败计数抹掉，A 的熔断永远攒不满阈值。
        slot["streak"] = 0
        slot["until"] = 0.0
        slot["err"] = ""
        throttle.reset(f"send.breaker.{bid or '-'}")

    def _note_giveup(self, u: Dict[str, Any], bid: str, now: float) -> None:
        """连着 GIVEEUP_TRIES 次发不出去就算了——真人不会一直追一个不回话的人。

        只有**低档**才停。中高档停掉没道理：她常聊的那个人某天没回，不代表明天也不回。
        停发状态由 `_quiesced` 判定，解除条件只有一个——对方先说话。
        """
        tier, _aff, _warmth = self._tier_of(bid, str(u.get("__uid", "")), u, None, now)
        if tier != anchors.TIER_LOW:
            return
        try:
            n = int(_num(u.get("giveup_count", 0) or 0)) + 1
        except (TypeError, ValueError):
            n = 1
        u["giveup_count"] = n
        if n >= GIVEEUP_TRIES:
            u["quiesced_at"] = now
            self.log(f"{u.get('__uid', '')} 连着 {n} 次发不出去，先不主动找 TA 了（等 TA 先开口）")

    def _mark_send_failed(
        self, u: Dict[str, Any], platform_side: bool = False, reason: str = ""
    ) -> None:
        """这个人的发送刚失败过：往后推一段，别下一轮心跳又给他写一遍。

        **平台侧的问题不记在这个人头上**：适配器没就绪、平台不支持主动消息这类失败
        与「他」无关，而原来一律走同一套指数退避（1800s 起、封顶 28800s）—— 一次平台
        抖动就让这个人在接下来最多 8 小时里彻底收不到消息，而且退避计数还挂在他身上
        继续累加（下一轮再失败就再翻倍）。平台侧只给一个短而固定的冷却。

        `send_blocked_reason` 以前**从来没有被写过**（只有清理时置空），于是状态页那一
        栏永远是空的：用户看到「这个人不发消息」却查不到为什么。现在把失败原文记在
        这个人身上，下一轮成功时由 `_note_send_ok` 那条路清掉。
        """
        if platform_side:
            # 升档而不是固定冷却：见 `SEND_FAIL_PLATFORM_COOLDOWN` 上面那段。
            try:
                n = int(_num(u.get("platform_fail_count", 0) or 0)) + 1
            except (TypeError, ValueError):
                n = 1
            delay = min(
                SEND_FAIL_PLATFORM_COOLDOWN * (4 ** min(n - 1, 6)),
                SEND_FAIL_PLATFORM_MAX,
            )
            u["platform_fail_count"] = n
            u["send_fail_until"] = self._time() + delay
            u["send_blocked_reason"] = f"平台侧（与TA无关）：{(reason or '原因未知')[:80]}"
            self.state.mark_dirty()
            return
        try:
            n = int(_num(u.get("send_fail_count", 0) or 0)) + 1
        except (TypeError, ValueError):
            n = 1
        delay = min(
            SEND_FAIL_COOLDOWN_BASE * (2 ** (n - 1)), SEND_FAIL_COOLDOWN_MAX
        )
        u["send_fail_count"] = n
        u["send_fail_until"] = self._time() + delay
        if reason:
            u["send_blocked_reason"] = str(reason)[:80]
        self.state.mark_dirty()

    def _schedule_burst(self, bid: str, uid: str, rest: List[str],
                        waits: List[float], sent_ts: float) -> None:
        """把连发的后续几条挂成后台任务，不阻塞主循环与命令回显。"""
        task = asyncio.create_task(self._send_burst_followup(bid, uid, rest, waits, sent_ts))
        self._burst_tasks.add(task)
        task.add_done_callback(self._burst_tasks.discard)

    async def _send_burst_followup(
        self, bid: str, uid: str, rest: List[str],
        waits: List[float], sent_ts: float,
    ) -> None:
        """按该 Bot 分段设置的间隔逐条补发剩余段；对方回话或插件停止则停下。

        隔多久由 `pacing.part_interval`（框架同款算法）算好；对方回了话就停——TA 已经
        接上了，再把剩下的旧话发过去就是答非所问。
        """
        try:
            for idx, text in enumerate(rest):
                wait = waits[idx] if idx < len(waits) else random.uniform(1.5, 3.5)
                await asyncio.sleep(max(0.0, wait))
                # 与群版（`_send_group_burst_followup`）一致：插件停了**或已停用**
                # 都不该再补发。只查 `self.running` 时，配置里关掉 enabled
                # 也照样把剩下的旧话一条条发出去。
                if not self.running or not self.cfg.enabled:
                    return
                if self.send_breaker_left(bid) > 0:
                    return
                u = self.state.user(bid, uid)
                if _num(u.get("last_seen", 0)) > sent_ts:
                    self.log(f"用户 {uid} 已有新消息，取消连发补充")
                    return
                umo = str(u.get("umo", "") or "")
                if not umo:
                    return
                ok, _, _ = await self._send_with_retry(umo, text, bid)
                if not ok:
                    return
                self.state.record_outgoing(
                    bid, uid, text, count_proactive=False,
                    store_text=self.cfg.store_message_text,
                )
                self.state.save()
                self.log(f"已连发补充给 {uid}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[autonomous_social] 连发补充失败: {e}")

    def _schedule_group_burst(
        self, bid: str, gid: str, umo: str, rest: List[str],
        waits: List[float], sent_ts: float,
    ) -> None:
        """把群聊心流连发的后续几句挂成后台任务，不阻塞观察链路。"""
        task = asyncio.create_task(
            self._send_group_burst_followup(bid, gid, umo, rest, waits, sent_ts)
        )
        self._burst_tasks.add(task)
        task.add_done_callback(self._burst_tasks.discard)

    async def _send_group_burst_followup(
        self, bid: str, gid: str, umo: str, rest: List[str],
        waits: List[float], sent_ts: float,
    ) -> None:
        """按该 Bot 的分段间隔逐条补发心流连发的剩余句子。

        这几句不再各自过心流闸/计额（算同一次插话），只写进自己的近期发言供下一句参考。
        插件停了、群被隔离就不再补。

        **群里有人新说话就停**：连发是同一个念头的拆分，群里别人插话后继续补旧句，
        观感就是“她不管别人说什么，自己一口气发一堆”——私聊侧早就有这道闸，群侧漏了。
        """
        try:
            for idx, text in enumerate(rest):
                wait = waits[idx] if idx < len(waits) else random.uniform(1.5, 3.5)
                await asyncio.sleep(max(0.0, wait))
                if not self.running or not self.cfg.enabled:
                    return
                if self.send_breaker_left(bid) > 0:
                    return
                now = self._time()
                if self.state.is_group_blocked(bid, gid, now):
                    return
                # 群里有人（非 bot）在本次连发首句之后说过话：取消剩下的补发
                g = self.state.group(bid, gid)
                if _num(g.get("last_human_at", 0) or 0) > sent_ts:
                    self.log(f"群 {gid} 有新群友发言，取消连发补充")
                    return
                ok, _, _ = await self._send_with_retry(umo, text, bid)
                if not ok:
                    return
                self.state.record_group_self_text(
                    bid, gid, text, now,
                    sample_cap=self.cfg.group_ref_sample_size,
                    store_text=self.cfg.group_store_message_text,
                )
                self.state.save()
                self.log(f"群 {gid} 心流连发补充→ {text}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[autonomous_social] 群心流连发补充失败: {e}")

    # ─── 后台循环 ───────────────────────────────────────

    async def _seed_tick(self, force: bool = False) -> None:
        """播种用户：把还没进插件状态的人补进来（历史导入 + 播种名单）。

        历史导入是启动一次 + 每半小时一次：数据库在长，插件状态里的「认识的人」
        要跟上。已经在 state.json 里的用户一律不动，实时数据永远比旧快照新。

        会话库扫描（sqlite 全表 + 逐段 json.loads）搬到线程里跑：这段原本是同步的，
        而它既跑在启动路径上（卡住插件加载）也跑在心跳里（300 个会话就是几百毫秒到
        数秒，期间 AstrBot 对所有消息都不响应）。写 state 仍然留在主循环。
        """
        if not (force or self._time() - self._last_seed >= SEED_INTERVAL_SECONDS):
            return
        self._last_seed = self._time()
        if not self.cfg.history_ingest and not self.cfg.seed_users:
            return
        added = 0
        import_diag = ""
        try:
            if self.cfg.history_ingest:
                # 找不到会话库是「装了不认人」最常见的原因，必须大声说出来，
                # 不能静默 return 0 让面板上看不出识别到底跑没跑
                db_path = history_ingest.find_astrbot_db(self._data_dir)
                if not db_path:
                    import_diag = (
                        f"历史导入：会话库未找到（data_dir={self._data_dir or '未解析'}），"
                        "装之前的聊天对象导不进来！"
                    )
                    if throttle.allow("seed.no_db"):
                        logger.warning(
                            f"[autonomous_social] {import_diag}"
                            + throttle.summary("seed.no_db")
                        )
                rows = await asyncio.to_thread(
                    history_ingest.collect_from_history,
                    self._data_dir,
                    private_only=self.cfg.private_only,
                    store_text=self.cfg.store_message_text,
                    existing_uids=history_ingest.existing_uids(self.state),
                    target_bid=history_ingest.seed_target_bid(self.state),
                    now=self._time(),
                )
                added += history_ingest.apply_history_rows(self.state, rows)
            if self.cfg.seed_users:
                added += history_ingest.apply_seed_list(
                    self.state,
                    self.cfg.seed_users,
                    self.cfg.seed_platform,
                    now=self._time(),
                )
            if added:
                self.state.save()
                n_list = len(
                    history_ingest.normalize_seed_entries(
                        self.cfg.seed_users, self.cfg.seed_platform
                    )
                )
                logger.info(
                    f"[autonomous_social] 播种用户：新导入/新增 {added} 人"
                    f"（历史导入{'开' if self.cfg.history_ingest else '关'}，"
                    f"名单 {n_list} 人）"
                )
                self._last_import_note = f"最近一次导入：新增 {added} 人"
            elif import_diag:
                self._last_import_note = import_diag
        except Exception as e:
            self._last_import_note = f"最近一次导入：失败 {e}"
            logger.warning(f"[autonomous_social] 播种用户失败: {e}")

    def _prune_expired(self) -> None:
        """每小时最多扫一次，把长期不活跃用户的历史正文从状态文件里清掉。

        启动后首次调用会立即执行一次，升级后能把已有的膨胀文件缩回去。
        """
        now = self._time()
        if now - self._last_prune < PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        # 永久删除：很久不说话、她发过去也没人回的会话，连记录一起丢掉（私聊 + 群）。
        # 放在历史清理**之前**：清完历史就分不清它当初到底有没有人回过。
        try:
            _days = float(getattr(self.cfg, "purge_inactive_user_days", 0) or 0)
            if _days > 0:
                _gone = 0
                for _bid, _bot in (self.state.data.get("bots") or {}).items():
                    _users = (_bot or {}).get("users") or {}
                    for _uid in list(_users):
                        if isinstance(_users.get(_uid), dict) and _silent_too_long(_users[_uid], now, _days):
                            _users.pop(_uid, None)
                            _gone += 1
                    _groups = (_bot or {}).get("groups") or {}
                    for _gid in list(_groups):
                        if isinstance(_groups.get(_gid), dict) and _silent_too_long(_groups[_gid], now, _days):
                            _groups.pop(_gid, None)
                            _gone += 1
                if _gone:
                    self.state.mark_dirty()
                    self._m["purged_sessions_total"] = int(self._m.get("purged_sessions_total", 0)) + _gone
                    logger.info(
                        f"[autonomous_social] 已永久删除 {_gone} 个长期不回（超过 {int(_days)} 天且无回复）的会话"
                    )
        except Exception as e:
            logger.warning(f"[autonomous_social] 永久删除长期不回的会话失败: {e}")

        try:
            pruned = self.state.prune_expired(self.cfg.user_retention_days, now)
        except Exception as e:
            logger.warning(f"[autonomous_social] 过期数据清理失败: {e}")
            return
        if pruned:
            logger.info(
                f"[autonomous_social] 已清理 {pruned} 个长期不活跃用户的历史对话数据"
            )
        # 长期没见消息的群（被踢/退群/解散）连样本一起丢，也确保不再对它破冰
        try:
            gone = self.state.prune_stale_groups(self.cfg.group_stale_days, now)
            if gone:
                logger.info(f"[autonomous_social] 已清理 {gone} 个长期不活跃的群（很可能已不在群）")
        except Exception as e:
            logger.warning(f"[autonomous_social] 群数据清理失败: {e}")
        self._prune_user_locks()

    def _prune_user_locks(self) -> int:
        """丢掉已经没有对应用户条目的 per-user 锁。

        `_user_locks` 原来只增不减，原注释说「量级和 state 里的用户条目相同，可以接受」
        —— 但 state 里的用户条目会被过期清理与幽灵清扫删掉，锁不会：于是它的量级是
        「**曾经见过**的用户数」，在人多的大群里只增不减。
        """
        if not self._user_locks:
            return 0
        try:
            live: Set[Tuple[str, str]] = set()
            for bid, bot in (self.state.data.get("bots", {}) or {}).items():
                users = (bot or {}).get("users")
                if isinstance(users, dict):
                    live.update((str(bid), str(uid)) for uid in users)
        except Exception:
            return 0
        # **正在被持有的锁一律不丢**：丢掉之后另一个协程会拿到一把新锁，
        # 两把锁同时放行，互斥就没了。
        stale = [
            key for key, lock in self._user_locks.items()
            if key not in live and not lock.locked()
        ]
        for key in stale:
            self._user_locks.pop(key, None)
        return len(stale)

    async def run(self) -> None:
        """后台主循环。"""
        logger.info("[autonomous_social] 后台循环已启动")
        self._prune_expired()
        lo, hi = self._heartbeat_range()
        # 连续异常时的退避：原来不管三七二十一 sleep(30) 就再来一轮，
        # try_once 一直抛就是每 30 秒跑一次完整心跳（还刷一条日志），
        # 心跳被拖成 30 秒一轮，压力和日志都是原子的好几倍。
        crash_streak = 0
        while self.running:
            try:
                await asyncio.sleep(random.randint(lo, hi))
                if not self.running:
                    break
                self._prune_expired()
                await self._seed_tick()
                await self.try_once()
                crash_streak = 0
            except asyncio.CancelledError:
                logger.info("[autonomous_social] 后台循环被取消")
                raise
            except Exception as e:
                crash_streak += 1
                if throttle.allow("loop.crash"):
                    logger.warning(
                        f"[autonomous_social] 后台循环异常（连续 {crash_streak} 次）: {e}"
                        + throttle.summary("loop.crash")
                    )
                # 30s → 2min → 8min → 封顶 30min
                await asyncio.sleep(min(1800.0, 30.0 * (4 ** min(crash_streak - 1, 6))))
        logger.info("[autonomous_social] 后台循环已退出")

    # ─── 状态展示 ───────────────────────────────────────

    def _core_status_text(self) -> str:
        """检查 Humanoid Core 联动状态，返回描述文本。

        降级必须写清楚「哪些能力停了」。只说「未找到 Core」而不说少了什么，主人
        看到的就是一个「装了却好像没装」的插件，既不知道少了什么也不知道该做什么。
        """
        degraded = _DEGRADED_NOTE
        if self.cfg.mode == "standalone":
            return "独立模式（不联动 Core）" + degraded

        try:
            root = self.core.read_root()
        except Exception as e:
            return f"❌ Core 读取失败: {e}" + degraded

        if not root:
            return "❌ 未找到 Core state.json" + degraded

        roles = list(root.get("roles", {}).keys())
        our_bots = set(self.state.data.get("bots", {}).keys())
        matched = our_bots & set(roles)

        if not matched:
            our_bot_list = list(our_bots)[:3]
            return (
                f"⚠️ Core 已找到但角色不匹配（Core: {roles[:3]}..., 本插件: {our_bot_list}）"
                + degraded
            )

        # 读到契约 v1 才能用身体轴；否则只能退回两个标量，主动消息的把关会差一大截。
        contract_v = None
        for bid in matched:
            try:
                view = self.core.bot_self_state(bid, root=root)
            except Exception:
                view = None
            if view and view.get("contract_v"):
                contract_v = view["contract_v"]
                break
        if contract_v:
            # 契约里的 persona 是 Core 排日程时用的那个人格，社交层用的是按会话
            # 解析出来的 AstrBot 人格。两者不是同一条链，对不上就说明两个插件
            # 在扮演不同的人——原来这里一律报「联动正常」。
            tz_note = "" if any(
                self._city_offset.get(b) is not None for b in matched
            ) else _NO_TZ_NOTE
            mismatch = self._persona_mismatch_note(root, matched)
            if mismatch:
                return (
                    f"⚠️ 联动中但人设对不上：Core 角色 {len(roles)} 个，匹配 {len(matched)} 个，"
                    f"契约 v{contract_v}；{mismatch}{tz_note}"
                )
            return (
                f"✅ 联动正常（Core 角色 {len(roles)} 个，匹配 {len(matched)} 个；"
                f"契约 v{contract_v}，身体轴参与决策）{tz_note}"
            )
        return (
            f"⚠️ 联动中但降级：Core 角色 {len(roles)} 个，匹配 {len(matched)} 个，"
            "但没读到契约快照（旧版 Core），只剩精力与社交能量两个标量。"
            "升级 Core 到 v2.14+ 才能用上睡意/饥饿/时区/句长约束。"
        )

    def _facade_status_text(self) -> str:
        """门面（契约 v2 同进程只读接口）这一路的状态。

        单独一行是刻意的：读文件和走门面是**两条路**，一条坏了另一条可能还在，
        混成一句「联动正常」等于把「正在用哪条路」这件事藏起来——而排查时最需要
        知道的恰恰是这个。
        """
        mode = str(getattr(self.cfg, "use_core_facade", "auto") or "auto")
        if mode == "off":
            return "门面：已关闭（use_core_facade=off，与 1.27.38 行为一致）"
        info = core_facade.status()
        if info.get("connected"):
            caps = "、".join(info.get("capabilities") or []) or "（无）"
            return (
                f"✅ 门面：已连接（Core API v{info.get('api_version')}；能力：{caps}）"
                "；主动消息的上下文改由 Core 直接给"
            )
        reason = str(info.get("reason") or "还没查过")
        if mode == "on":
            return f"❌ 门面：未连接（{reason}）；use_core_facade=on，所以不读 state.json"
        return f"⚠️ 门面：未连接（{reason}）；已退回读 state.json"

    def _per_bot_status_text(self) -> str:
        """按角色分行：念头、冷却、发送、熔断、退避各自一格。

        状态页原来是一锅粥——多 Bot 装了等于看不出任何一个角色的真实情况，
        而「哪个角色坏了」恰恰是排查时要的第一眼。
        """
        bots = self.state.data.get("bots", {}) or {}
        if not bots:
            return "（还没有角色记录）"
        now = self._time()
        self._refresh_clocks()
        lines: List[str] = []
        for bid, bot in list(bots.items())[:8]:
            users = (bot or {}).get("users", {}) or {}
            groups = (bot or {}).get("groups", {}) or {}
            hot = 0
            last_send = 0.0
            streak = 0
            for u in users.values():
                if not isinstance(u, dict):
                    continue
                try:
                    hot += 1 if _num(u.get("urge", 0.0) or 0.0) >= desire.FIRE_THRESHOLD else 0
                    last_send = max(last_send, _num(u.get("last_sent", 0) or 0))
                    streak = max(streak, int(_num(u.get("no_reply_streak", 0) or 0)))
                except (TypeError, ValueError):
                    continue
            when = (
                datetime.fromtimestamp(last_send).strftime("%m-%d %H:%M") if last_send else "还没发过"
            )
            gap = self.cfg.min_gap_minutes
            left = "—"
            if last_send:
                gap_left = max(0.0, last_send + gap * 60.0 - now)
                left = f"{int(gap_left / 60)} 分钟后" if gap_left > 0 else "可以开口了"
            label = self._bot_label(bid)
            persona = self._last_persona.get(bid, "") or "（没读到人设）"
            slot = self._breaker_slot(bid)
            breaker = (
                f"熔断{int(max(0.0, float(slot.get('until', 0.0)) - now) / 60)}分"
                if float(slot.get("until", 0.0) or 0.0) > now else "正常"
            )
            lines.append(
                f"  {label}：{len(users)} 人 / {len(groups)} 群｜人设 {persona}｜"
                f"念头够了 {hot} 人｜冷落最多 {streak} 次｜上次发送 {when}"
                f"（{left}）｜发送 {breaker}"
            )
        return "\n".join(lines)

    def _send_status_text(self) -> str:
        """各角色发送通道健康度。熔断是 per-bot 的，混成一句会看不出是哪一路坏了。"""
        lines: List[str] = []
        for bid, slot in sorted(self._send_breaker.items()):
            left = max(0.0, float(slot.get("until", 0.0)) - self._time())
            streak = int(slot.get("streak", 0) or 0)
            if left > 0:
                lines.append(
                    f"{self._bot_label(bid)} ⚠️ 熔断中（还剩 {int(left / 60)} 分钟，"
                    f"连着 {streak} 次发不出去，已停止生成以免白烧 token；"
                    f"最后一次错误：{slot.get('err') or '未知'}）"
                )
            elif streak:
                lines.append(f"{self._bot_label(bid)} ⚠️ 上次发送失败过（连续 {streak} 次）")
        return "；".join(lines) or "✅ 发送正常"

    def _metrics_text(self) -> str:
        """运行指标：回答「她到底跑起来没、卡在哪」。

        状态页原来只有「心跳：每 8~15 分钟一次」这种静态说明，于是「没触发」时
        分不清是没到时间、provider 取不到、用户被隔离、模型否决、平台失败，
        还是后台循环压根没跑。这里把上一次心跳的时间与每一步的人数写出来，
        一眼就能定位到是哪一步之后就不再往下走。
        """
        m = self._m
        now = self._time()
        # dry-run 也算「跑过了」：启动后 3 秒那次就是给部署验证用的，
        # 那时候主循环还在睡第一个心跳间隔，不该显示成「还没跑过」
        last_run = m.get("last_heartbeat") or m.get("dry_run_at") or 0.0
        if not last_run:
            return "⏳ 后台循环还没跑过第一轮（启动后会先等一个心跳间隔）"
        since_hb = now - _num(last_run)
        stale = since_hb > (self.cfg.heartbeat_max_minutes * 60 * 2)
        hb = f"{int(since_hb / 60)} 分钟前" if since_hb < 3600 else f"{since_hb / 3600:.1f} 小时前"
        settled = m.get("last_settled") or 0.0
        since_set = now - settled if settled else -1
        settle_txt = "还没结算过" if settled <= 0 else (
            f"{int(since_set / 60)} 分钟前，{m.get('last_settled_users', 0)} 人"
        )
        last_sent = m.get("last_sent") or 0.0
        sent_txt = "还没发出去过" if last_sent <= 0 else (
            datetime.fromtimestamp(last_sent).strftime("%m-%d %H:%M")
        )
        nxt = int(max(0, (self.cfg.heartbeat_min_minutes * 60) - since_hb) / 60)
        lines = [
            f"最后心跳：{hb}{'（⚠️ 超过两个心跳间隔还没跑，后台循环可能卡住了）' if stale else ''}",
            f"最后结算：{settle_txt}｜念头够了 {m.get('last_ready', 0)} 人"
            f"｜候选 {m.get('last_candidate', 0)} 个",
            f"最后发送：{sent_txt}（累计 {m.get('sent_total', 0)} 条）"
            f"｜累计跑了 {m.get('rounds', 0)} 轮心跳",
        ]
        fail = str(m.get("last_failure") or "")
        if fail:
            lines.append(f"最后卡住：{fail}")
        # 「没发出去」要分清三种，混成一个数就看不出是哪道防线在起作用
        vetoed = int(_num(m.get("veto_total", 0) or 0))
        if vetoed:
            lines.append(
                f"模型否决：累计 {vetoed} 次（她说此刻不该找这个人——由头留着，"
                f"下轮状态变了还可以说）"
            )
        rejected = int(_num(m.get("rejected_total", 0) or 0))
        if rejected:
            lines.append(
                f"验收拦下：累计 {rejected} 条（这由头她驾驭不了，已消耗；"
                f"念头保留，下轮还能再试）"
            )
            last_rej = str(m.get("last_rejected") or "")
            if last_rej:
                lines.append(f"  最近一条被拦的是：{last_rej[:60]}")
        # 没发出去的都去了哪：按理由分组。这是唯一能回答「这一轮为什么一条都没发」
        # 的地方——以前只知道总数，调阈值全凭感觉。
        blocked = m.get("blocked_reasons") or {}
        if isinstance(blocked, dict) and blocked:
            total_blocked = sum(int(_num(v or 0)) for v in blocked.values())
            lines.append(f"没发出去的去向（累计 {total_blocked} 次）：")
            for cat, cnt in sorted(blocked.items(), key=lambda kv: -int(_num(kv[1] or 0)))[:8]:
                lines.append(f"  {cat}：{int(_num(cnt))} 次")
        # 关系档位与「多久没回」
        tier_rows = []
        for bid in sorted(self.state.data.get("bots", {})):
            for uid, u in (self.state.bot(bid).get("users") or {}).items():
                gap_h = u.get("reply_gap_hours")
                if gap_h is None:
                    continue
                tier_rows.append(f"  {uid}：上次联系时 TA 已 {gap_h} 小时没回")
        if tier_rows:
            lines.append("对方多久没回（记在主动发送那一刻）：")
            lines.extend(tier_rows[:8])
            if len(tier_rows) > 8:
                lines.append(f"  ……另有 {len(tier_rows) - 8} 人")
        cleaned = int(_num(self.state.data.get("_sanitized_users", 0) or 0))
        if cleaned:
            lines.append(
                f"正文清洗：已洗过 {cleaned} 个用户（之前录进过框架注入内容，"
                "升级后不会再有）"
            )
        if nxt > 0:
            lines.append(f"预计下一轮：约 {nxt} 分钟后")
        return "\n".join(lines)

    def _signals_status_text(self) -> str:
        """写回 Core 的信号。写不进去要说出来——原来静默，表现是两边像没装。"""
        err = ""
        try:
            err = str(self.signals.last_error() or "")
        except Exception as exc:
            err = str(exc)
        if err:
            return f"❌ 写入失败：{err}（Core 那边会一直报「没对接」）"
        return "正常（被冷落计数与刚主动联系过谁）"

    def _persona_mismatch_note(self, root: Dict[str, Any], matched: set) -> str:
        """Core 排日程用的 persona 与会话里实际生效的 persona 对不上时给一句说明。

        契约里 persona 的用途写得很明白：让社交层确认两边用的是同一个人。以前读进来
        就丢，于是角色配错了也照样报「联动正常」——两个插件各演各的，还看不出来。
        """
        problems: List[str] = []
        for bid in sorted(matched)[:5]:
            try:
                view = self.core.bot_self_state(bid, root=root)
            except Exception:
                view = None
            core_persona = str((view or {}).get("persona") or "").strip()
            if not core_persona:
                continue
            local = str(self._last_persona.get(bid, "") or "").strip()
            if local and local != core_persona:
                problems.append(f"{bid}：Core 侧「{core_persona}」vs 会话侧「{local}」")
        return "；".join(problems)

    def _llm_status_text(self) -> str:
        """LLM 调用健康度。退避中要一眼看见，而不是只翻日志。"""
        gen = self.generator
        notes: List[str] = []
        for scope, until in sorted(getattr(gen, "_llm_skip_until", {}).items()):
            try:
                left = float(gen.llm_backoff_remaining(scope))
            except Exception:
                left = 0.0
            if left > 0:
                notes.append(f"{scope} 暂停中（还剩 {int(left // 60)} 分钟）")
        if notes:
            return "⚠️ LLM 连续失败，暂停调用中：" + "；".join(notes)
        worst = 0
        for scope, n in sorted(getattr(gen, "_llm_fail_streak", {}).items()):
            if int(n or 0) > worst:
                worst = int(n or 0)
        if worst:
            return f"⚠️ LLM 上次失败过（连续 {worst} 次）"
        bad = [f"{self._bot_label(b)} {v}" for b, v in sorted(self._last_llm_error.items()) if v]
        if bad:
            return "⚠️ 上次 LLM 异常：" + "；".join(bad)
        return "✅ LLM 调用正常"

    async def _persona_by_role_text(self) -> str:
        """按角色列出主动消息实际会用到的 AstrBot 人格。

        人格是按目标会话的 umo 解析的，所以这里取每个角色最近互动过的那个会话作代表；
        同一角色的不同会话如果绑了不同人设，这里会跟着变。
        """
        bots = self.state.data.get("bots", {})
        if not bots:
            return "  暂无角色记录（还没人私聊过 bot）"
        lines: List[str] = []
        for bid, bot in list(bots.items())[:8]:
            users = (bot or {}).get("users", {}) or {}
            rep_umo = ""
            rep_ts = -1.0
            for u in users.values():
                umo = str(u.get("umo", "") or "")
                if not umo:
                    continue
                try:
                    ts = _num(u.get("last_seen", 0) or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts > rep_ts:
                    rep_ts, rep_umo = ts, umo
            if not rep_umo:
                lines.append(f"  · {bid}：没有可用的会话来源，无法判定")
                continue
            name, _ = await self._persona(rep_umo, bid)
            lines.append(f"  · {bid}：{name or '未取到人设（用插件默认口吻）'}")
        if len(bots) > 8:
            lines.append(f"  …另有 {len(bots) - 8} 个角色未列出")
        return "\n".join(lines)

    def _bot_label(self, bid: str) -> str:
        """把 bot 的账号配上它此刻的人设名，一眼看出是哪个角色在社交。

        人设名取自最近一次用到的缓存（发过主动消息或看过状态后就有）；拿不到就只回账号。
        """
        name = str(self._last_persona.get(bid, "") or "").strip()
        if name and name != bid:
            return f"{name}·{bid}"
        return bid

    def urge_panel_text(self, limit: int = 6) -> str:
        """列出此刻念头最重的几个人（状态面板用）。"""
        now = self._time()
        rows: List[Tuple[str, str, float, float, str, float]] = []
        for bid, bot in self.state.data.get("bots", {}).items():
            for uid, u in (bot.get("users") or {}).items():
                try:
                    urge = _num(u.get("urge", 0) or 0)
                    interest = _num(u.get("interest", 0) or 0)
                except (TypeError, ValueError):
                    continue
                rows.append((
                    bid, uid, urge, interest,
                    str(u.get("pending_result", "") or ""),
                    _num(u.get("last_seen", 0) or 0),
                ))
        if not rows:
            return "  还没有用户记录（没人说过话，也没播种进来任何人）"
        rows.sort(key=lambda x: -x[2])
        lines: List[str] = []
        for bid, uid, urge, interest, pend, seen in rows[:limit]:
            hours = (now - seen) / 3600.0 if seen else 0.0
            state = {"waiting": "等回复中", "replied": "接了话", "ignored": "没接话"}.get(pend, "")
            bar = "★想说" if urge >= desire.FIRE_THRESHOLD else ""
            lines.append(
                f"  · {uid}（{self._bot_label(bid)}）念头 {urge:.2f}/{desire.FIRE_THRESHOLD:.0f}"
                f" 在意 {interest:.2f} 多久没说话 {hours:.1f}h {state}{bar}"
                f"{self._thread_mark(bid, uid, now)}"
            )
        if len(rows) > limit:
            lines.append(f"  …另有 {len(rows) - limit} 人未列出")
        return "\n".join(lines)

    def _thread_mark(self, bid: str, uid: str, now: float) -> str:
        """状态面板上标出「话正断着 / 还挂着哪件没回访的事」。

        沉默与几个窗口一律用同一套来源算：以前这里自己算一份、选人那里算另一份，
        两边单位还不一样（分钟 vs 秒），面板那列会静默永远空白。
        """
        u = self.state.user(bid, uid)
        bits: List[str] = []
        last_said = self._last_said(u)
        if self.cfg.followup_enabled and last_said > 0:
            probe, presence, ceiling, context = self._thread_windows()
            silence = now - last_said          # 秒
            if min(probe, presence) <= silence <= ceiling:
                kind, _ = thread_reason(
                    u,
                    now,
                    probe_after_seconds=probe,
                    presence_after_seconds=presence,
                    max_seconds=ceiling,
                    context_seconds=context,
                    presence_floor_seconds=presence,
                )
                if _num(u.get("thread_for", 0) or 0) == last_said:
                    bits.append(f"｜话断了 {silence / 60:.0f} 分钟（已接过一回）")
                elif kind:
                    why = self.thread_gate(bid, uid, u, now)
                    label = "追问" if kind == "probe" else "问在不在"
                    bits.append(
                        f"｜话断了 {silence / 60:.0f} 分钟（{label if not why else f'不接：{why}'}）"
                    )
        about = live_loop(u, now)
        if about:
            bits.append(f"｜记着「{about[:14]}」该回访")
        if self.cfg.closer_enabled and str(u.get("pending_result", "") or "") == "waiting":
            sent = _num(u.get("last_sent", 0) or 0)
            waited = (now - sent) / 3600.0 if sent else 0.0
            if waited >= self.cfg.closer_after_hours:
                bits.append(f"｜那句没人回已 {waited:.0f} 小时（该收场）")
        return "".join(bits)

    def last_contact_text(self) -> str:
        """最近一次主动联系的结果与当时否决的理由。"""
        now = self._time()
        best: Optional[Tuple[float, str, str, str]] = None
        skip: Optional[Tuple[float, str, str, str]] = None
        for bid, bot in self.state.data.get("bots", {}).items():
            for uid, u in (bot.get("users") or {}).items():
                sent = _num(u.get("last_sent", 0) or 0)
                if sent and (best is None or sent > best[0]):
                    text = ""
                    for m in reversed(u.get("conversation") or []):
                        if m.get("dir") == "out":
                            text = str(m.get("text", ""))[:40]
                            break
                    best = (sent, bid, uid, text)
                skipped = _num(u.get("last_skip_at", 0) or 0)
                if skipped and (skip is None or skipped > skip[0]):
                    skip = (skipped, bid, uid, str(u.get("last_skip_reason", ""))[:60])
        lines: List[str] = []
        if best:
            lines.append(
                f"  上次主动找：{best[2]}（{self._bot_label(best[1])} → {int((now - best[0]) / 60)} 分钟前）「{best[3]}」"
            )
        if skip and (best is None or skip[0] > best[0]):
            lines.append(
                f"  上次想过没说：{skip[2]}（{self._bot_label(skip[1])} → {int((now - skip[0]) / 60)} 分钟前）{skip[3]}"
            )
        base = "\n".join(lines) if lines else "  还没有主动联系过任何人"
        return base + "\n" + self.timeline_text()

    def proactive_log_text(self, uid_filter: str = "", limit_users: int = 6, limit_each: int = 15) -> str:
        """按用户列出最近 7 天发过的主动消息（按 bid,uid 隔离不串台），供主人审计。

        uid_filter 非空时只看那个用户；为空时列最近发过主动消息的几个人。
        """
        now = self._time()
        want = str(uid_filter or "").strip()
        rows: List[Tuple[float, str, str, List[Dict[str, Any]]]] = []
        for bid, bot in self.state.data.get("bots", {}).items():
            for uid, u in (bot.get("users") or {}).items():
                if want and str(uid) != want:
                    continue
                log = self.state.recent_proactive(bid, uid, now)
                if not log:
                    continue
                last_ts = max((_num(e.get("ts", 0) or 0) for e in log), default=0.0)
                rows.append((last_ts, bid, uid, log))
        if not rows:
            if want:
                return f"  {want}：最近 7 天没给 TA 发过主动消息"
            return "  最近 7 天还没给任何人发过主动消息"
        rows.sort(key=lambda x: -x[0])
        lines: List[str] = ["【最近 7 天主动消息】按用户隔离"]
        for _last, bid, uid, log in rows[:limit_users]:
            name = str(self.state.user(bid, uid).get("name", "") or uid)
            lines.append("")
            lines.append(f"◆ {name}（{uid}·{self._bot_label(bid)}）共 {len(log)} 条")
            for e in log[-limit_each:]:
                try:
                    ts = _num(e.get("ts", 0) or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                when = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "??"
                text = str(e.get("text", "") or "").strip()
                why = str(e.get("why", "") or "").strip()
                lines.append(f"   {when}　{text}")
                if why:
                    lines.append(f"           └ {why}")
        if len(rows) > limit_users:
            lines.append("")
            lines.append(f"…另有 {len(rows) - limit_users} 人未列出（用「主动消息记录 <用户ID>」看单个人）")
        return "\n".join(lines)

    def group_status_text(self, limit: int = 20) -> str:
        """列出各角色在管的群：最近活跃、心流窗口、是否被隔离，供主人核对“精准识别”与踢群是否真停。"""
        now = self._time()
        self._refresh_clocks()
        on, off = "开", "关"
        lines = [
            "【群聊主动】",
            f"- 心流接话：{on if self.cfg.group_flow_enabled else off}"
            f"　破冰：{on if self.cfg.group_icebreak_enabled else off}"
            f"　发言参考库：{on if self.cfg.group_ref_lib_enabled else off}",
        ]
        rows: List[Tuple[float, str]] = []
        for bid, bot in self.state.data.get("bots", {}).items():
            for gid, g in (bot.get("groups") or {}).items():
                seen = _num(g.get("last_seen", 0) or 0)
                idle_h = (now - seen) / 3600.0 if seen > 0 else -1
                flow_open = _num(g.get("flow_open_until", 0) or 0) > now
                blocked = _num(g.get("blocked_until", 0) or 0) > now
                name = str(g.get("name", "") or gid)
                mark = "⛔" if blocked else ("🟢" if flow_open else "○")
                idle_txt = f"{idle_h:.1f} 小时前" if idle_h >= 0 else "未知"
                extra = "　隔离中（" + str(g.get('blocked_reason', ''))[:20] + "）" if blocked else ""
                rows.append((
                    seen,
                    f"  {mark} {name}（{gid}·{self._bot_label(bid)}）\n"
                    f"      最近说话 {idle_txt}　样本 {len(g.get('samples') or [])} 条{extra}",
                ))
        if not rows:
            lines.append("- 还没在任何群里观察到消息（群里有人说话后才会出现在这里）。")
            return "\n".join(lines)
        rows.sort(key=lambda x: -x[0])
        lines.append(f"【在管的群】共 {len(rows)} 个")
        lines.append("（🟢 关注窗口开着　○ 静着　⛔ 被隔离）")
        lines.extend(r for _s, r in rows[:limit])
        if len(rows) > limit:
            lines.append(f"  …另有 {len(rows) - limit} 个群未列出")
        return "\n".join(lines)

    def _recognition_text(self) -> str:
        """状态面板的「识别」一行：历史导入到底认没认出人、卡在什么地方。

        这是「装上就该认出聊过的人」能不能兑现的可见证据：会话库找不到、
        没有可导入的私聊会话、还能再导几个，都能直接看到，不用再猜。
        """
        if not self.cfg.history_ingest:
            return "识别：历史导入关着（装了也不认旧人，要开 history_ingest）"
        try:
            snap = history_ingest.seed_diag_snapshot(
                self.state, self._data_dir, now=self._time()
            )
        except Exception as e:
            return f"识别：会话库读取失败（{e}）"
        if not snap["db_path"]:
            return f"识别：{snap['reason']}"
        parts = ["会话库 OK"]
        parts.append(f"私聊会话 {snap['private_conversations']} 个")
        note = self._last_import_note
        parts.append(f"可再导 {snap['importable']} 人" if snap["importable"] else "没有可再导入的")
        if note:
            parts.append(note)
        return "识别：" + "，".join(parts)

    async def status_text(self) -> str:
        """获取插件状态文本。"""
        bots = self.state.data.get("bots", {})
        # 面板是排障第一入口：一个 null/非 dict 的 bot 条目不该让它整体报错。
        users = sum(
            len((x if isinstance(x, dict) else {}).get("users", {}))
            for x in bots.values()
        )
        total_sent = 0
        total_replied = 0
        for bot_data in bots.values():
            for u in bot_data.get("users", {}).values():
                total_sent += int(_num(u.get("proactive_sent", 0)))
                total_replied += int(_num(u.get("proactive_replied", 0)))
        reply_rate = f"{total_replied}/{total_sent}" if total_sent > 0 else "—"

        ref_bid = next(iter(bots), "")
        self._refresh_clocks()
        dt = self._moment_of(ref_bid, self._time())
        slot = time_slot(dt.hour)
        slot_names = {
            "early_morning": "清晨", "morning": "上午", "lunch": "午休",
            "afternoon": "下午", "evening": "傍晚", "late_night": "深夜",
            "deep_night": "凌晨",
        }

        bot_keys = list(bots.keys())
        if len(bot_keys) > 3:
            bot_list = "、".join(bot_keys[:3]) + "..."
        else:
            bot_list = "、".join(bot_keys) if bot_keys else "无"

        persona_text = await self._persona_by_role_text()

        # 分段拼排：每段一个主题，行首统一符号，行内用全角空格断句。
        # 此前是一把 30+ 行的流水，排查时要逐行读；重排后按「你想知道什么」找段就行。
        sections: List[str] = []
        sections.append(
            f"自主拟人社交 v{__version__}\n"
            f"- 状态：{'🟢 启用' if self.cfg.enabled else '⏸ 停用'}"
            f"　模式：{self.cfg.mode}"
            f"　活跃度：{self.cfg.activity_level}（×{self.cfg.mood_scale:.2f}）\n"
            f"- 当前时段：{slot_names.get(slot, '未知')} {dt.strftime('%H:%M')}"
            f"{'（安静时段，念头长得极慢）' if self._quiet_now(ref_bid, dt.hour) else ''}"
            f"　{'按她所在城市' if self._city_offset.get(ref_bid) is not None else '按本机时钟'}"
        )
        sections.append(
            "【人格】按会话解析，各角色互不影响\n" + persona_text
        )
        sections.append(
            "【联动】\n"
            f"- Core：{self._core_status_text()}\n"
            f"- {self._facade_status_text()}\n"
            f"- 写回：{self._signals_status_text()}\n"
            f"- 模型：{self._llm_status_text()}\n"
            f"- 发送：{self._send_status_text()}"
        )
        sections.append("【运行指标】\n" + self._metrics_text())
        sections.append(
            "【触发与节奏】\n"
            f"- 心跳：每 {self.cfg.heartbeat_min_minutes}~{self.cfg.heartbeat_max_minutes} 分钟一次"
            f"（分钟级配置的实际精度就是它）\n"
            f"- 节奏：念头驱动（最在意的人约 {self.cfg.urge_refill_hours} 小时攒满一次；"
            f"刚聊完 {self.cfg.recent_talk_minutes} 分钟内不另起）\n"
            f"- 把关：{'模型判断该不该说' if self.cfg.llm_gate else '仅规则闸门'}"
            f"　时机：{'按对方作息挑点' if self.cfg.respect_user_rhythm else '不看作息'}"
            f"、{'记得对方说的约定' if self.cfg.cue_followup else '不跟由头'}\n"
            f"- 护栏：同一人 {self.cfg.user_cooldown_minutes} 分钟 / "
            f"每轮每角色最多 {self.cfg.max_sends_per_round} 条（不同人互不排队）\n"
            f"- 未完话题："
            + (
                f"开（对方答得敷衍 {self.cfg.probe_after_minutes} 分钟就追问那件事；"
                f"聊到一半断了 {self.cfg.followup_after_minutes} 分钟问一句；一段沉默只接一次）"
                if self.cfg.followup_enabled
                else "关"
            )
            + "\n"
            + (
                f"- 回访挂事：开（提过没说结果的事，隔 {self.cfg.loop_min_hours:g}~"
                f"{self.cfg.loop_max_hours:g} 小时问后来）"
                if self.cfg.loop_enabled
                else "- 回访挂事：关"
            )
            + "\n"
            + (
                f"- 没人回收场：开（她主动发的话 {self.cfg.closer_after_hours} 小时没人回，"
                "自己冒一句揭过去）"
                if self.cfg.closer_enabled
                else "- 没人回收场：关"
            )
            + "\n"
            + (
                "- 看得见她说过的话：开"
                if self.cfg.track_own_replies
                else "- 看得见她说过的话：关（未完话题的判定会明显变弱）"
            )
            + "\n"
            + (
                f"- 早晚问候：开（早安 {self.cfg.greeting_morning_start}~{self.cfg.greeting_morning_end} 点、"
                f"晚安 {self.cfg.greeting_night_start}~{self.cfg.greeting_night_end % 24} 点，"
                "每人每天每窗口一次）"
                if self.cfg.greeting_enabled
                else "- 早晚问候：关"
            )
        )
        sections.append(
            "【群聊主动】\n"
            + (
                f"- 心流：开（bot 在群里说过话后 {self.cfg.flow_window_minutes} 分钟内无需@接话；"
                f"自己说完静 {self.cfg.flow_hold_seconds} 秒；接话间隔 {self.cfg.flow_min_gap_seconds} 秒；"
                f"同群同小时最多 {self.cfg.flow_hourly_cap} 条）"
                if self.cfg.group_flow_enabled
                else "- 心流：关"
            )
            + f"\n- 破冰：{'开' if self.cfg.group_icebreak_enabled else '关'}"
            + f"　参考库：{'开' if self.cfg.group_ref_lib_enabled else '关'}"
            + f"　在管群 {sum(len(b.get('groups') or {}) for b in bots.values())} 个（详见「群社交状态」）"
        )
        sections.append(
            "【识别与投递】\n"
            f"- {self._recognition_text()}\n"
            f"- 播种：历史导入{'开' if self.cfg.history_ingest else '关'}"
            f"　名单 {len(history_ingest.normalize_seed_entries(self.cfg.seed_users, self.cfg.seed_platform))} 人（没聊过也能找）\n"
            f"- 记录 bot：{len(bots)}（{bot_list}）　候选用户：{users}\n"
            f"- 发送/回复：{reply_rate}　回复窗口：{self.cfg.reply_window_hours} 小时\n"
            f"- 连发：{'开' if self.cfg.allow_burst else '关'}（拆法跟 AstrBot 的分段回复设置）"
            f"　括号动作/emoji：看她自己的说话习惯\n"
            f"- 输入状态：{'开' if self.cfg.input_state_enabled else '关'}（仅 NapCat 私聊）"
        )
        sections.append("【各角色】\n" + self._per_bot_status_text())
        sections.append("【念头】\n" + self.urge_panel_text())
        sections.append("【最近联系】\n" + self.last_contact_text())
        return "\n\n".join(s for s in sections if s.strip())


def _silent_too_long(record: Dict[str, Any], now: float, days: float) -> bool:
    """这个人/这个群是不是「很久没说话、她发过去也没人回」。

    只认两种证据，缺一不删：
    * `last_seen` 超过 N 天（对方从没说过话时它可能是 0，靠 `last_said` 兜底）；
    * 这段时间里没有任何回复记录（`replied_at`/`reply_gap_hours` 都指向很久以前）。
    """
    if days <= 0:
        return False
    span = days * 86400.0
    seen = _num(record.get("last_seen", 0) or 0)
    said = _num(record.get("last_said", 0) or 0)
    anchor = max(seen, said)
    if anchor <= 0 or now - anchor < span:
        return False
    replied = _num(record.get("replied_at", 0) or record.get("last_replied", 0) or 0)
    return replied <= 0 or now - replied >= span
