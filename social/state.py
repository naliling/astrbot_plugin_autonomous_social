"""Social 状态持久化。

v1.7.4：
- 用户条目新增「念头」相关字段：urge / interest / 作息画像 / 冷落计数 / 待回复状态 / 由头
- 记录消息时顺带结算上一次主动联系的结局（被接话 or 没人理）
- 作息画像按小时统计并对过老的样本做半衰衰减，让「TA 一般几点玩手机」跟着实际变化
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import desire
from .reasoning import CUE_LATE_LIMIT
from .threads import (
    LOOP_LATE_HOURS,
    LOOP_SLOTS_MAX,
    PROMISE_SLOTS_MAX,
    PROMISE_MAX_CHARS,
    PROMISE_WAIT_SECONDS,
    _promise_looks_sane,
    loop_entries,
    promise_entries,
)
from .sanitize import sanitize_state
from .throttle import throttle

from astrbot.api import logger


def _num(value: Any) -> float:
    """时间戳/计数的容错取值。state.json 里的脏数据不该让正常流程抛异常。

    `inf` 也算脏：它能让「冷却还没到吗」这类比较全假、让 `int()` 抛 OverflowError。
    读不出来与非有限一律归 0——0 在这里的语义就是「这个字段没写」。
    """
    try:
        out = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _safe_ts(value: Any, default: float = 0.0) -> float:
    """时间戳容错转换。state.json 里的脏数据不该让正常流程抛异常。"""
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _is_number(value: Any) -> bool:
    """是不是一个能参与数值运算的有限数。

    `bool` 不算：JSON 里的 `true` 落到 message_count 上是被写坏的脏值，不是 1；
    按「数字」收下它会让计数悄悄多一，比直接重置更糟。
    超大整数（float 装不下）仍算数：它数学上有限，只是转换溢出，不是脏数据。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return True


# ─── v2.1.0：跨群去重 / 反问频控用的小工具 ─────────────

# 生活素材的归一化：Core 给的素材行前面常挂「正在/接下来/今天想去/还在/梦到」这些
# 拼装前缀（见 groupflow.life_lines），同一条事换个前缀就是另一个字符串。去重比的是
# **这件事本身**，所以前缀先剥掉。
_LIFE_PREFIXES = ("正在", "接下来", "今天想去", "今天想", "还在", "梦到", "刚")


def _norm_life_key(line: Any) -> str:
    """把一条生活素材压成去重用的键（剥前缀、去空白、限长）。"""
    text = str(line or "").strip()
    if not text:
        return ""
    text = "".join(text.split())
    changed = True
    while changed:
        changed = False
        for pre in _LIFE_PREFIXES:
            if text.startswith(pre) and len(text) > len(pre):
                text = text[len(pre):]
                changed = True
                break
    return text[:40]


# 「这句是不是在反问」。只看**结尾**：中间带问号的多半是引用/语气，不算把问题抛给对方。
_QUESTION_TAIL = re.compile(r"[?？]\s*$|[吗呢啥]\s*[。！!…~～\s]*$")


def _is_question_text(text: Any) -> bool:
    """这句话是不是以问句收尾（反问/提问）。"""
    body = str(text or "").strip()
    if not body:
        return False
    return bool(_QUESTION_TAIL.search(body))

# ─── 常量定义 ───────────────────────────────────────

# umo 的 message_type 段里，哪些表示「这是一个群/频道会话」。
# 必须与 `history_ingest._GROUP_TYPES` 保持同一套写法：`guildmessage` 曾经只在前者里，
# 于是 QQ 频道的幽灵永远清不掉（前者拿它判「是群、别导入」，后者拿它判「是不是幽灵」）。
_GROUP_UMO_TYPES = frozenset(
    {"group", "g", "groupmessage", "guild", "guildmessage"}
)

# 多久没在这个群插过话，就把「连着插没人理」的账一笔勾销。
#
# 这个计数说的是**同一波活跃里**连插了几句没人接。隔了几个小时还记着，它就变成了
# 「这个群永久不许她开口」—— 见 `forgive_stale_flow_ignored`。
FLOW_IGNORED_RESET_SECONDS = 3 * 3600.0

# 她插完一句之后，群里**一个人都没再说**，过多久就认定「这波散了」而不是「没人理我」。
#
# 原来的账只按「有没有 @ 她」记：她插一句、群里就此安静，也照样 +1 被冷落，
# 两次就把这个群的心流关掉。线上 59 个关注窗口只接了 13 次话、其中 15 次记成被冷落，
# 里面相当一部分其实是「话题自然结束」，不是「她说话没人搭理」。
# 3 分钟：够群里有人接着聊，又不至于把真正冷场的那次漏记成被接住。
FLOW_QUIET_AFTER_REPLY_SECONDS = 3 * 60.0

STATE_VERSION = 2
DEFAULT_BOT_KEY = "default"

# 对话历史保留条数
MAX_CONVERSATION_HISTORY = 10
# 消息文本截断长度
MESSAGE_TRUNCATE_LENGTH = 200
# 对话历史单条截断长度
CONVERSATION_TRUNCATE_LENGTH = 200
# pending_proactive_context 最多保留几条：每次主动发送追加一条，用户下一次
# LLM 请求时整段消费掉；不设上限的话长时间没人回会一直攒。
PENDING_PROACTIVE_KEEP = 8
# 最近消息类型追踪数量
RECENT_MSG_TYPES_COUNT = 6
# 每用户主动消息日志保留天数：只记自己主动发出的那几条，按（bid,uid）天然隔离不串台，
# 既用于生成时防重复，也供主人指令审计。超过保留期的条目写入时自动清。
PROACTIVE_LOG_DAYS = 7
# 主动消息日志最多留多少条（防止单个用户日志无限涨）
PROACTIVE_LOG_MAX = 60
# 话题提取允许的最大单段长度（超过此长度不提取）
TOPIC_MAX_CHARS = 6
# 「为什么发这条」一行最多记多少字
WHY_MAX_CHARS = 60

# ─── 状态文件「读盘被占用」的退避节奏（秒）─────────────────
#
# 为什么需要：Windows 上杀软扫描、资源管理器索引、OneDrive 同步都会在几毫秒到几百
# 毫秒内独占 state.json，报出来是 PermissionError / EBUSY。旧实现把它和「JSON 坏了」
# 走同一条路——把原文件 rename 成 .unreadable.xxx，然后插件拿着空壳跑、还拒绝写盘。
# 线上表现就是「重启一次，她把我全忘了」，而原文件其实一个字节都没坏。
# 100/300/900ms 三次足够熬过这类瞬时占用，加起来不到 1.5 秒，只在启动或重试时付一次。
_READ_RETRY_DELAYS = (0.1, 0.3, 0.9)
# 这些 errno 的含义都是「文件现在被占着/暂时不让读」，过一会儿就好
_TRANSIENT_READ_ERRNOS = frozenset(
    (
        errno.EACCES,  # Windows 权限/共享冲突、Linux 权限瞬时变更
        errno.EPERM,   # 某些安全软件用 EPERM 报拒绝
        errno.EBUSY,   # 另一个进程正打开着
    )
)
# Windows 上 Python 把 ERROR_SHARING_VIOLATION(32) / ERROR_LOCK_VIOLATION(33) /
# ERROR_ACCESS_DENIED(5) 都包成 PermissionError，但 errno 偶尔对不上，补一层 winerror。
_TRANSIENT_READ_WINERRORS = frozenset((5, 32, 33))

# 跨进程写锁文件：拿不到锁时降级成告警而不是阻塞写盘（见 _cross_process_lock）。
# 崩溃会留下一个锁文件，超过这个年龄就认定是上一个进程的残骸，直接抢占——
# 否则一次崩溃会让这个状态文件从此再也写不进去。
_LOCK_STALE_SECONDS = 30.0

# 长期不活跃用户会被重置的历史字段：这些是状态文件里真正占体积的部分。
# 保留 name/umo/message_count/last_seen 与各回复统计：umo 是主动发送的唯一目标地址，
# 回复统计驱动自适应权重，丢了会直接影响选择谁、多久发一次。
RETENTION_HEAVY_LIST_FIELDS = ("conversation", "topics", "recent_msg_types", "proactive_log")
RETENTION_HEAVY_TEXT_FIELDS = ("last_message", "cue", "loop", "last_spoken_text")
# 作息画像也是逐条攒出来的正文派生物，人长期不说话了它就没意义了
RETENTION_RHYTHM_HOURS = 24

# 作息直方图的总计数超过这个数就整体折半：只增不衰减的话，一年前的作息会永远压着
# 最近三个月的变化，对方换了工作/改了作息就再也跟不上了
RHYTHM_DECAY_TOTAL = 120

# 停用词表（用于话题提取过滤）
_STOP_WORDS = {
    "什么", "怎么", "为什么", "可以", "一下", "觉得", "知道", "时候", "现在",
    "我们", "你们", "他们", "就是", "这样", "那个", "这个", "其实", "好像",
    "或者", "但是", "因为", "所以", "如果", "虽然", "然后", "不过", "一直",
    "已经", "还是", "只是", "那样", "今天", "昨天", "明天",
    "也是", "这是", "那是", "都不", "我就", "你是", "我是", "有没有", "是不是",
    "真的", "刚才", "刚刚", "突然", "然后", "所以", "可是", "但是", "不过",
    "还是", "或者", "也许", "可能", "应该", "大概", "差不多", "反正",
    "自己", "别人", "大家", "人家",
    "吃饭", "睡觉", "上班", "下班", "上课", "下课", "出门", "回家",
    "东西", "事情", "问题", "地方", "样子",
    # 附和与语气词。它们不是话题：以前「好的」「哈哈」会被当成两件事记进 topics，
    # 于是接下来好几条主动消息的提示词里都挂着「之前聊到的话题：好的、哈哈」，
    # 模型照着这个"话题"说话，冒出莫名其妙的呼应。
    "好的", "好哒", "好呀", "好吧", "行吧", "可以", "哈哈", "哈哈哈", "嘻嘻",
    "真的", "确实", "没错", "是啊", "对", "对的", "嗯", "嗯嗯", "唔", "哦",
    "噢", "哦哦", "知道了", "收到", "谢了", "谢谢", "客气", "没事", "没关系",
    "在吗", "怎么", "怎么办", "为啥", "咋了", "什么情况",
}

# 中文标点分隔符
_CHINESE_PUNCT_PATTERN = r"[，。！？、,.!?;；\s]+"
# 英文单词（3+字母）
_ENGLISH_WORD_PATTERN = r"[a-zA-Z]{3,}"
# 非中文字符
_NON_CHINESE_PATTERN = r"[a-zA-Z0-9]+"


class SocialState:
    """社交状态持久化管理。

    特性：
    - v2 状态格式，自动从 v1 迁移
    - 脏标记 + flush：observe 高频写入时不立即刷盘
    - 回复检测带时间窗口
    - 话题记忆数量可配置
    - 原子写入（tmp + replace）
    - 念头（urge）与作息画像随消息记录自动演化
    """

    VERSION = STATE_VERSION

    def __init__(self, path: str, time_source=None):
        self.path = path
        self.data: Dict[str, Any] = {"version": STATE_VERSION, "bots": {}}
        self._dirty = False
        # 读不出原来的记忆时上闸门：闸门未解除前拒绝写盘。不上闸门的话，
        # 内存里已经是空壳，第一条新消息一标脏就会把空状态盖回磁盘。
        self._load_failed = False
        self._write_blocked_warned = False
        # 「读盘被占用」与「文件内容坏了」必须分开：前者原文件是好的，只是这一瞬间
        # 打不开，绝不能改名；后者才该备份留证。这个标记决定闸门要不要留一个
        # 自动重试的口子（retry_load / flush），而不是等到重载插件。
        self._load_deferred = False
        self._load_error = ""
        # 上次「我们读到/写下」的那份文件的 (mtime, size)。写盘前比对它，是为了
        # 发现另一个进程（或人）也在改这个文件：全量重写会把对方刚写进去的更新
        # 整份盖掉，而且不留任何痕迹，事后只能靠猜。
        self._disk_stamp: Optional[Tuple[float, int]] = None
        # 变更代数：mark_dirty() 每次 +1。异步写盘时快照与真正落盘之间可能隔了
        # 几十毫秒，中间主线程又改了状态——落盘完成后只能清掉「快照那一刻」的脏标记，
        # 否则那几十毫秒里的改动会被当成已落盘，永久丢失。
        self._gen = 0
        # 同进程里多个写者（心跳、发送成功、指标落盘、后台线程）串行化：
        # tmp+replace 本身是原子的，但「谁后写谁赢」必须确定，否则旧快照会盖掉新快照。
        self._write_lock = threading.Lock()
        # 时间源可注入：所有落盘时间戳与调用方的判断必须同源，否则「刚聊过」「超时没回」
        # 这类判断会因为两处各取一次时钟而错位；测试与仿真也需要能推进它。
        self._now = time_source or time.time
        self.load()

    # ─── 基础访问 ───────────────────────────────────────

    def bot(self, bid: str) -> Dict[str, Any]:
        """获取或创建 bot 状态。"""
        bots = self.data.get("bots")
        if not isinstance(bots, dict):
            # 盘上被手改/写坏成非对象时不能直接 setdefault：脏值会在这里（以及
            # 后面几乎所有状态 API 入口）抛，而不是退化成一个空记录。
            bots = self.data["bots"] = {}
        bot = bots.get(bid)
        if not isinstance(bot, dict):
            bot = bots[bid] = {"users": {}, "last_global_send": 0.0}
        return bot

    def user(self, bid: str, uid: str) -> Dict[str, Any]:
        """获取或创建用户状态。"""
        users = self.bot(bid).get("users")
        if not isinstance(users, dict):
            users = self.bot(bid)["users"] = {}
        u = users.get(uid)
        if not isinstance(u, dict):
            u = users[uid] = self._default_user()
        # 读路径（_min_gap_left / _note_giveup / 日志）要拿得到这个用户的 uid：
        # 记录里存一份，省得每处调用都显式传 uid。每次取都归一化重写一遍。
        u["__uid"] = str(uid)
        return u

    def known_bot_ids(self) -> set:
        """本进程见过「自己也在发言」的 bot id 集合（多 bot 同进程部署时的兄弟）。

        与顶层 `bots` 键不同：后者只在某 bot 有过私聊用户/被心跳结算时才建。
        只混群、从不私聊的兄弟 bot 永远不在 `bots` 里——用这个独立名单才能把它认出来。
        """
        return {
            str(x) for x in (self.data.get("_known_bot_ids") or []) if x and str(x) != "default"
        }

    def note_bot_id(self, other: str) -> None:
        """记下一个「确认是 bot 自己」的 id（它以自己的身份发过消息）。"""
        other = str(other or "")
        if not other or other == "default":
            return
        ids = self.data.setdefault("_known_bot_ids", [])
        if other not in ids:
            ids.append(other)
            if len(ids) > 64:
                del ids[:-64]
            self.mark_dirty()

    @staticmethod
    def _default_user() -> Dict[str, Any]:
        """返回默认用户状态结构。"""
        return {
            "name": "",
            "umo": "",
            "__uid": "",
            "message_count": 0,
            "last_seen": 0.0,
            "last_message": "",
            "last_sent": 0.0,
            "last_targeted": 0.0,
            # v1.7.4 念头模型：urge 是「现在有多想找 TA 说话」，interest 是在意程度基线
            "urge": 0.0,
            "urge_at": 0.0,
            "fire_gate": 1.0,
            "interest": None,
            "interest_at": 0.0,
            # 攒念头的速度倍率：播种名单（从没聊过的人）为 0.5，正常用户 1.0
            "urge_scale": 1.0,
            # v1.7.4 作息画像：对方在 0-23 点各自的活跃计数
            "active_hours": [0.0] * RETENTION_RHYTHM_HOURS,
            "rhythm_samples": 0,
            # v1.7.4 主动联系后的心理状态：waiting / replied / ignored + 连续被冷落次数
            "pending_since": 0.0,
            "pending_result": "",
            "no_reply_streak": 0,
            "last_replied_at": 0.0,
            "last_skip_at": 0.0,
            "last_skip_reason": "",
            # v1.28.17 特别关心：手动名单 / Core 关系自动认定后落在用户状态里。
            # 主动层据此放宽档位间隔、静音与「没回就停」的闸；发送成功时按日计数。
            "special_care": False,
            "special_care_source": "",
            "special_care_at": 0.0,
            "special_care_sent_day": "",
            "special_care_sent_count": 0,
            # v1.7.4 由头：对方话里的时间锚点（「明天面试」）与它的到期时间。
            # cue_due 是锚点本身（一旦记下就不再改动），cue_expire_at 是它的绝对寿命，
            # cue_retry_at 只是「最早能再试一次」的重试节奏。三者分开，推迟重试不会
            # 顺带把寿命也续上——否则同一件事可以每 6 小时重试一次、永不作废。
            "cue": "",
            "cue_due": 0.0,
            "cue_at": 0.0,
            "cue_expire_at": 0.0,
            "cue_retry_at": 0.0,
            "cue_tries": 0,
            # v1.8.0 她在主链路里自己说的话（after_message_sent 记账）：
            # 看不到这些时，插件以为她每说完一句对方都会回，未完话题也就无从判断。
            "last_spoken": 0.0,
            "last_spoken_text": "",
            # 由头池：每一次主动开口都要从这里取一件具体的事（anchors.py）
            "anchors": [],
            # 验收层上一次判不合格的内容（含清洗前的原始输出），供排障
            "last_rejected": "",
            # 未完话题：thread_for 记下「为哪一次断点追过」，一段沉默只追一次
            "thread_for": 0.0,
            "thread_at": 0.0,
            # 隔一阵回访那件事：loop 是没说完的事，loop_due 是大概什么时候再问。
            # 与 cue 同一套三字段分工
            "loop": "",
            "loop_due": 0.0,
            "loop_at": 0.0,
            "loop_expire_at": 0.0,
            "loop_retry_at": 0.0,
            "loop_tries": 0,
            # 对方没回也不空着：closer_for 记下「为哪一次没人回收过场」
            "closer_for": 0.0,
            "closer_at": 0.0,
            # v1.10.2 早晚问候：greet_day 记「哪天问候过」（按她所在城市的日期），
            # greet_kind 记那次是早安还是晚安——每个用户每天每窗口各一次，互不共用
            "greet_day": "",
            "greet_kind": "",
            "greet_at": 0.0,
            # v2: 对话历史
            "conversation": [],
            # v2: 主动消息回复追踪
            "proactive_sent": 0,
            "proactive_replied": 0,
            "last_proactive_sent": 0.0,
            "last_proactive_replied": True,
            # 尚未被用户下一次 LLM 请求消费的主动消息原文。每项
            # {text, ts, in_history}：in_history=True 表示这条已经补写进 AstrBot
            # 会话历史，on_llm_request 不能再把它当临时内容块注入一遍。
            "pending_proactive_context": [],
            # v2: 回复时间统计
            "avg_reply_seconds": 0.0,
            "reply_samples": 0,
            # v2: 提取的话题
            "topics": [],
            # v1.28.7: 每个话题最后一次被提到的时刻。`topics` 从这一版起还是
            # **由头来源**（`anchors.derive_topic_relations` 拿它问「你上次说的
            # 那个X，后来呢」），由头有时效而关键词表本身不带时间戳——
            # 没有这个字段就分不清「上周聊的」和「去年聊的」。
            # 旧状态没有这个键：由头派生按「不知道多久前」处理，不丢由头。
            "topic_seen_at": {},
            # v2: 最近消息类型（反重复）
            "recent_msg_types": [],
            # 主动消息 7 天日志：只记自己主动发出的那几条（{ts, text, cat}），
            # 按（bid,uid）隔离不串台，写入时自动修剪过期条目
            "proactive_log": [],
            # 发送失败（非好友/不支持主动消息等）隔离：隔离到什么时候、为什么
            "send_blocked_until": 0.0,
            "send_blocked_reason": "",
            # v1.28.16 兄弟 bot（同一进程里的另一台 bot）的防刷屏账：
            # 私聊里它们现在也是「能聊的人」，但主动找它们必须极克制——
            # 最近一次找它的时刻、那天是哪天、当天已经找过几次。
            # 只有真的发成功了才写（见 engine `_speak_inner` 成功分支）。
            "bot_peer_last_sent": 0.0,
            "bot_peer_sent_day": "",
            "bot_peer_sent_count": 0,
        }

    # ─── 群会话（v1.11.0 群聊心流） ─────────────────────

    def groups(self, bid: str) -> Dict[str, Any]:
        """这个角色名下的群会话表（gid -> 群状态）。与 users 平行，按 bid 隔离不串群。"""
        return self.bot(bid).setdefault("groups", {})

    def group(self, bid: str, gid: str) -> Dict[str, Any]:
        """获取或创建群状态。"""
        g = self.groups(bid).setdefault(gid, self._default_group())
        # 老记录补齐新键
        for k, v in self._default_group().items():
            g.setdefault(k, v)
        return g

    @staticmethod
    def _default_group() -> Dict[str, Any]:
        """群会话默认结构。umo 原样存 event.unified_msg_origin，用于精准发送。"""
        return {
            "umo": "",
            "group_id": "",
            "name": "",
            "last_seen": 0.0,        # 最近一条群消息（任何人）
            "msg_count": 0,
            "last_bot_spoke": 0.0,   # bot 最近在这个群说话（主链路回复/破冰/心流）
            "flow_open_until": 0.0,  # 心流关注窗口到期
            "flow_replies": 0,       # 当前窗口已主动接几条
            "flow_last_reply_at": 0.0,
            # 最近一次**发起过**心流生成的时刻（不论发没发出去）。
            # 与 flow_last_reply_at 分开：后者只在成功发送时更新，
            # 于是「模型选择不接」完全不消耗任何配额——活跃群里每条消息
            # 都会重新走一遍闸门 + 一次 LLM（实测 1 分钟 14 次）。
            "flow_last_attempt_at": 0.0,
            "flow_ignored": 0,       # 连续插话没人接的计数
            "flow_hour_bucket": 0,   # 小时级插话计数的时间桶（epoch 小时）
            "flow_hour_count": 0,
            "last_flow_reply_text": "",  # bot 上一句心流接的话，给生成侧防重复
            "icebreak_day": "",      # 最近破冰的日期（按角色时钟）
            "icebreak_count_day": 0,
            "icebreak_at": 0.0,
            "topic_day": "",         # G1：最近主动起话头的日期
            "topic_count_day": 0,
            "topic_at": 0.0,
            # 最近一次**尝试过**破冰 / 起话头（不论发没发出去）。与 icebreak_at/topic_at
            # 分开：那两个只在成功发送时更新，于是「模型决定不说」「三次仍是问句」
            # 这类失败完全不碰节流——一个冷群一旦 due 就永久 due，每轮心跳都重新
            # 生成，`due[0]` 还永久占着不让别的冷群轮上（v2.2.0 修）。
            "icebreak_try_at": 0.0,
            "topic_try_at": 0.0,
            "samples": [],           # 参考库：近期群友发言 [{ts, name, text}]（去重、限长）
            "blocked_until": 0.0,    # 发送失败（被踢/会话失效）隔离
            "blocked_reason": "",
        }

    def record_group_message(
        self,
        bid: str,
        gid: str,
        umo: str,
        name: str,
        sender_name: str,
        text: str,
        is_bot: bool,
        now: float,
        store_text: bool = True,
        sample_cap: int = 30,
        is_self: bool = False,
    ) -> Dict[str, Any]:
        """记下一条群消息：刷新 last_seen/umo，并（需要时）把群友发言存进参考库。

        is_bot=True 时不进参考库（不学自己），但会刷 last_seen。参考库只存真人发言。

        `is_self` 与 `is_bot` 要分开：多 bot 同群时，兄弟 bot 的发言**也是 bot**，
        但它不是「我自己」。`last_human_at` 的语义是「最近一次**别人**开口」，
        所以只有 `is_self=True` 才不更新它 —— 原来按 `is_bot` 判，于是兄弟 bot
        说话时这个时间戳不动，连发补发任务以为群里没人接话、继续补旧句，
        看起来就是两台 bot 在互相抢话。
        """
        g = self.group(bid, gid)
        if umo and g.get("umo") != umo:
            g["umo"] = umo
        if not g.get("group_id"):
            g["group_id"] = str(gid)
        if name:
            g["name"] = str(name)[:60]
        g["last_seen"] = now
        g["msg_count"] = int(g.get("msg_count", 0) or 0) + 1
        if not is_self:
            # **别人**（真人或兄弟 bot）最近一次发言的时刻。连发补发任务用它判断
            # 「我话还没发完，群里已经有人接着说上了」——那种情况下继续补旧句
            # 就是自说自话。
            g["last_human_at"] = now
        # 收到群消息说明这个群现在能通：清掉之前的发送隔离
        if float(g.get("blocked_until", 0) or 0) > 0:
            g["blocked_until"] = 0.0
            g["blocked_reason"] = ""
        if is_bot:
            # bot 自己在群里说的话也存进 samples（标 self）：这样生成上下文里能看到自己
            # 刚说了什么，接话不至于自说自话/重复。风格参考库会滤掉 self，不学自己。
            # 但它同样要受 store_text 约束：这个开关在面板上承诺的是「更隐私」，
            # 原来只关了一半——群友的话不存了，bot 自己说的每句照样逐条落盘。
            if store_text:
                self._append_group_sample(g, text, now, sample_cap, self_flag=True)
        elif store_text:
            self._append_group_sample(
                g, text, now, sample_cap, self_flag=False, name=sender_name
            )
        self.mark_dirty()
        return g

    @staticmethod
    def _append_group_sample(
        g: Dict[str, Any],
        text: str,
        now: float,
        sample_cap: int,
        *,
        self_flag: bool = False,
        name: str = "",
    ) -> None:
        """往群的近期发言缓冲追加一条（真人或 bot 自己）。self 条只用于上下文、不进风格库。"""
        body = str(text or "").strip()
        if not body or body.startswith("/"):
            return
        samples = g.setdefault("samples", [])
        body = body[:CONVERSATION_TRUNCATE_LENGTH]
        # 跟上一条一模一样就不重复存（复读/刷屏预防）
        if samples and str(samples[-1].get("text", "")) == body:
            return
        entry: Dict[str, Any] = {"ts": now, "name": str(name or "")[:24], "text": body}
        if self_flag:
            entry["self"] = True
        samples.append(entry)
        # bot 自己的话也占额度，缓冲放宽一点，别让几句自己的话把群友样本挤没了。
        # 上限必须是配置给的固定值：用「当前长度」当上限等于永不裁剪
        cap = max(1, int(sample_cap)) + 10
        if len(samples) > cap:
            del samples[: len(samples) - cap]

    def open_flow(self, bid: str, gid: str, now: float, window_seconds: float) -> None:
        """bot 在这个群说了话（走主链路回复/破冰）：开/续关注窗口。

        bot 亲口说过一句算一次“真正参与”（多半是被 @ 后回的），所以把“连着插话没人
        理”的计数 flow_ignored 归零；本窗接话计数只在新开窗时归零。心流插话本身不延长
        窗口（走 note_flow_reply），避免自己把窗口无限推下去。
        """
        g = self.group(bid, gid)
        was_open = float(g.get("flow_open_until", 0) or 0) > now
        g["last_bot_spoke"] = now
        g["flow_open_until"] = now + max(0.0, window_seconds)
        if not was_open:
            # 新一轮活跃开窗：上一波「插了没人理」的账一笔勾销，重新开始数。
            # 但同一波持续活跃里被 @ 回一句不清零 flow_ignored——被叫去回话不代表
            # 「我随口插的那几句有人接」，那笔账只由真正的接话（note_flow_pickup）勾销。
            g["flow_replies"] = 0
            g["flow_ignored"] = 0
        self.mark_dirty()

    def hold_flow(self, bid: str, gid: str, now: float, hold_seconds: float) -> None:
        """给自己刚说完话后的心流设一段「先别接」冷却。

        与 open_flow 分开：开窗是「等群友继续聊」，冷却才是「我自己先缓一口气」。
        没有这一层时，bot 被 @ 回完一句、群里紧接着有人说话，45 秒间隔一到它就
        又接一句——在群里看就是两个人格里的同一个她连着说话，像主动代理在刷存在。
        """
        try:
            hold = max(0.0, float(hold_seconds))
        except (TypeError, ValueError):
            return
        if hold <= 0:
            return
        g = self.group(bid, gid)
        until = now + hold
        if float(g.get("flow_hold_until", 0) or 0) < until:
            g["flow_hold_until"] = until
            self.mark_dirty()

    def note_flow_attempt(self, bid: str, gid: str, now: float) -> None:
        """记一次心流**尝试**（不论成没成）。

        为什么需要它：`note_flow_reply` 只在**成功发送**时调用，于是「模型选择不接」
        这条路完全不碰任何配额——活跃群里每来一条群消息就重新走一遍闸门 + 一次 LLM。
        线上实测：某个群 1 分钟内 14 次「模型选择不接或生成为空」，每次都真打了模型。

        这个时间戳只用于**节流**（别把 LLM 当免费的打），不计入：
        · `flow_replies`（窗口配额——那个数的是「真正接了几句话」）
        · `flow_ignored`（连续插话没人理——那个数的是「发出去没人接」）
        · `flow_hour_count`（每小时插话上限）
        三者都保持「只算真正发出去的」，语义不混。
        """
        g = self.group(bid, gid)
        g["flow_last_attempt_at"] = now
        self.mark_dirty()

    def note_proactive_attempt(
        self, bid: str, gid: str, now: float, mode: str = "icebreak"
    ) -> None:
        """记一次破冰 / 起话头**尝试**（不论成没成）。

        为什么需要它：`note_icebreak` / `note_group_topic` 只在**成功发送**时调用，
        于是「模型决定不说」「连续三次仍是问句」「跨群撞车」「验收打回」这四条失败
        路径完全不碰任何时间戳——一个冷群一旦 due 就永久 due，每轮心跳都重新生成
        （每轮最多 3 次 LLM），`due[0]` 也永远被它占着，其它冷群永远轮不到。

        这个时间戳**只用于节流**（`groupflow.icebreak_due` / `group_topic_due` 的
        冷却闸 + `_run_icebreaks` / `_run_group_topics` 的轮转排序），不计入：
        · `icebreak_count_day` / `topic_count_day`（那是「今天真发出去几次」）
        · `icebreak_at` / `topic_at`（那是「上次真开口是什么时候」）
        三者都保持「只算真正发出去的」，语义不混（与 `note_flow_attempt` 同一套理由）。
        """
        g = self.group(bid, gid)
        if str(mode or "") == "topic":
            g["topic_try_at"] = now
        else:
            g["icebreak_try_at"] = now
        self.mark_dirty()

    def note_flow_reply(        self,
        bid: str,
        gid: str,
        now: float,
        text: str,
        sample_cap: int = 30,
        store_text: bool = True,
    ) -> None:
        """记一次心流接话：窗口计数 +1、“连续自说自话”计数 +1、刷小时桶、记下接的话防重复。

        flow_ignored 在这里递增：统计“自上次真正被搞话（被@回）以来连着插了几句”。
        插够 flow_ignored_exit 次都没人正式搞你，就安静下来，别自说自话。不延长关注窗口。

        sample_cap 必须是配置里的上限。这里原先传的是 `len(samples) or 30`——也就是
        “当前长度”，于是每次心流插话的实际上限变成「当前长度+10」，追加一条后永远
        不触发裁剪，样本只增不减。活跃群里跑几天就能堆到几百条，每次写盘都要全量重写。
        """
        g = self.group(bid, gid)
        g["flow_replies"] = int(g.get("flow_replies", 0) or 0) + 1
        g["flow_ignored"] = int(g.get("flow_ignored", 0) or 0) + 1
        g["flow_last_reply_at"] = now
        g["last_bot_spoke"] = now
        g["last_flow_reply_text"] = str(text or "")[:120]
        # 自己插的话也进近期发言缓冲，生成下一句时看得到（同样受隐私开关约束）
        if store_text:
            self._append_group_sample(g, text, now, sample_cap, self_flag=True)
        bucket = int(now // 3600)
        if int(g.get("flow_hour_bucket", 0) or 0) == bucket:
            g["flow_hour_count"] = int(g.get("flow_hour_count", 0) or 0) + 1
        else:
            g["flow_hour_bucket"] = bucket
            g["flow_hour_count"] = 1
        self.mark_dirty()

    def note_flow_pickup(self, bid: str, gid: str) -> None:
        """有人接了 bot 的心流插话（@ 了 bot）：把「连着插没人理」的计数清零，可以继续接。

        这是心流「知道对方接没接话」的关键：没这一步时 flow_ignored 只增不减（除非新
        开窗），bot 要么插到上限就闷、要么被 @ 回话就误当成「插话受欢迎」继续无脑插。
        """
        g = self.group(bid, gid)
        if int(g.get("flow_ignored", 0) or 0):
            g["flow_ignored"] = 0
            self.mark_dirty()

    def forgive_stale_flow_ignored(self, bid: str, gid: str, now: float) -> None:
        """把「连着插话没人理」的账作废。两种情形都算：

        ① **时间到**：距上一次插话超过 `FLOW_IGNORED_RESET_SECONDS`，上一波早结束了。
        ② **这波散了**：她插完话之后群里**一个人都没再说**（`last_human_at` 没往前走），
           且已过 `FLOW_QUIET_AFTER_REPLY_SECONDS`。那不是「没人理我」，是话题自然结束——
           原来只有 ①，于是「她插一句、群里安静了」也照样记成被冷落，两次就把这个群的
           心流关掉。

        反过来，如果**有人接着说**（只是没 @ 她），那才是真的被冷落，账留着。

        原来这个计数只有两处会清零：新开关注窗口（`open_flow` 里 `not was_open` 那一支）
        与被 @（`note_flow_pickup`）。而它一旦到过退出阈值，她就不再插话
        → 永远不会新开窗 → 计数**永远停在阈值上**：这个群从此不许她开口，
        除非有人恰好 @ 她一次。加一道按时间的作废，让新的一波能重新开始数。
        """
        g = self.group(bid, gid)
        if int(g.get("flow_ignored", 0) or 0) <= 0:
            return
        last = _num(g.get("flow_last_reply_at"))
        if last <= 0:
            return
        if (now - last) >= FLOW_IGNORED_RESET_SECONDS:
            g["flow_ignored"] = 0
            self.mark_dirty()
            return
        if (now - last) < FLOW_QUIET_AFTER_REPLY_SECONDS:
            return
        if _num(g.get("last_human_at")) > last:
            # 有人接着说了话（只是没 @ 她）→ 这才是真的「插了没人理」，账留着。
            return
        g["flow_ignored"] = 0
        self.mark_dirty()

    def record_group_self_text(
        self,
        bid: str,
        gid: str,
        text: str,
        now: float,
        sample_cap: int = 30,
        store_text: bool = True,
    ) -> None:
        """记下 bot 自己在群里补发的一句（连发续句）：只进近期发言缓冲供下一句参考，
        不动 last_seen/msg_count、不过心流闸、不计额。"""
        g = self.group(bid, gid)
        g["last_bot_spoke"] = now
        if store_text:
            self._append_group_sample(g, text, now, sample_cap, self_flag=True)
        self.mark_dirty()

    def flow_hour_count(self, g: Dict[str, Any], now: float) -> int:
        """当前自然小时里已经心流插了几次（跨桶自动归零）。"""
        if int(g.get("flow_hour_bucket", 0) or 0) != int(now // 3600):
            return 0
        return int(g.get("flow_hour_count", 0) or 0)

    def note_icebreak(self, bid: str, gid: str, now: float, day: str) -> None:
        """记一次破冰：同一天计数 +1，跨天归零。"""
        g = self.group(bid, gid)
        if str(g.get("icebreak_day", "") or "") == day:
            g["icebreak_count_day"] = int(g.get("icebreak_count_day", 0) or 0) + 1
        else:
            g["icebreak_day"] = day
            g["icebreak_count_day"] = 1
        g["icebreak_at"] = now
        self.mark_dirty()

    def note_group_topic(self, bid: str, gid: str, now: float, day: str) -> None:
        """G1：记一次「主动起话头」：同一天计数 +1，跨天归零。"""
        g = self.group(bid, gid)
        if str(g.get("topic_day", "") or "") == day:
            g["topic_count_day"] = int(g.get("topic_count_day", 0) or 0) + 1
        else:
            g["topic_day"] = day
            g["topic_count_day"] = 1
        g["topic_at"] = now
        self.mark_dirty()

    def note_group_send(self, bid: str, now: float, day: str) -> None:
        """v2.1.2：记一次群里的主动发言（破冰/起话头/心流都算），给全局日上限用。

        原来只有「每群每天几条」的账（`note_icebreak` / `note_group_topic`），
        群一多就没人管总数：线上 11-06 群发 68 条、私聊只有 13 条。这本账记在
        角色上（`bot`），因为上限是「她今天在群里说了几句」，不是某个群的。
        """
        b = self.bot(bid)
        if str(b.get("group_send_day", "") or "") == day:
            try:
                b["group_send_count_day"] = int(b.get("group_send_count_day", 0) or 0) + 1
            except (TypeError, ValueError):
                b["group_send_count_day"] = 1
        else:
            b["group_send_day"] = day
            b["group_send_count_day"] = 1
        b["group_send_at"] = now
        self.mark_dirty()

    def group_send_count(self, bid: str, day: str) -> int:
        """v2.1.2：这个角色今天在群里已经主动说了几条（跨天自动归零）。"""
        b = self.bot(bid)
        if str(b.get("group_send_day", "") or "") != day:
            return 0
        try:
            return max(0, int(b.get("group_send_count_day", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def is_group_blocked(self, bid: str, gid: str, now: float) -> bool:
        return float(self.group(bid, gid).get("blocked_until", 0) or 0) > now

    def block_group(self, bid: str, gid: str, until: float, reason: str) -> None:
        g = self.group(bid, gid)
        g["blocked_until"] = until
        g["blocked_reason"] = str(reason or "")[:120]
        self.mark_dirty()

    def group_samples(self, bid: str, gid: str, count: int) -> List[str]:
        """抽最近 count 条群友发言正文（去 @/命令），供风格参考。"""
        if count <= 0:
            return []
        samples = self.group(bid, gid).get("samples", []) or []
        out: List[str] = []
        # 只拿真人发言当风格参考：bot 自己说的（self 标记）不学，别把自己的腔调越练越浓
        real = [s for s in samples if not s.get("self")]
        for s in real[-count:]:
            t = str(s.get("text", "") or "").strip()
            if t:
                out.append(t)
        return out

    # ─── 加载与迁移 ──────────────────────────────────────────────

    def load(self, *, allow_sleep: bool = True) -> None:
        """从磁盘加载状态。

        两种情况必须分开处理，**不能都当「记忆读不出来」**：

        1. 内容真的坏了（JSON 语法错、不是 UTF-8、顶层结构不是预期形状）：
           原文件已经是废的，挪到一边留证、报 error、上写盘闸门，等人工处理。
        2. 文件读不出来但内容没坏（Windows 杀软扫描、OneDrive 同步、另一个进程
           刚好打开着——表现为 PermissionError / EBUSY）：原文件是**好的**，
           只是这一瞬间打不开。旧实现把它和坏文件走同一条路，`os.rename` 把
           state.json 改成了 .unreadable.xxx：全部记忆从原路径消失，插件拿着
           空壳继续跑还拒绝写盘，重启之后依然如此。线上表现就是「升级/重启一次，
           她把我全忘了」。现在这里只记「本次没加载上」，原文件一动不动，
           并允许后续心跳重试（`retry_load`，`flush()` 每轮也会顺手试一次）。

        allow_sleep=False 是给周期重试用的：那条路径每 30 秒就会走一次，
        在里面做 100/300/900ms 退避会把事件循环卡住；文件锁放开后一次就能读出来。
        """
        self._load_deferred = False
        self._load_error = ""
        try:
            if not os.path.exists(self.path):
                # 没有文件 = 第一次跑。闸门也要清掉：上一轮可能因为「被占用」关着
                # 闸门，而这期间文件被人删了（手动清理/换机器），再关着就永远
                # 写不出第一份状态。
                self._load_failed = False
                self._disk_stamp = None
                return
            x = self._read_json_with_retry(allow_sleep=allow_sleep)
        except FileNotFoundError:
            # exists() 与 open() 之间被删掉（或正在被 os.replace 换掉）：
            # 这不是「记忆坏了」，按「现在没有状态文件」处理，闸门放开。
            self._load_failed = False
            self._disk_stamp = None
            return
        except json.JSONDecodeError as e:
            self._fail_load(f"状态文件 JSON 损坏（{e}）")
            return
        except UnicodeDecodeError as e:
            # UnicodeDecodeError 是 ValueError 的子类，不会被 json.JSONDecodeError 捕获，
            # 但性质一样：原文件里的记忆读不出来，绝不能当「没有记忆」处理
            self._fail_load(f"状态文件不是 UTF-8 文本（{e}）")
            return
        except OSError as e:
            # 读不出来 ≠ 内容坏了。文件保持原样，只关闸门等重试。
            self._defer_load(e)
            return
        except Exception as e:
            self._fail_load(f"状态文件读取时发生意外错误（{e!r}）")
            return

        if not (isinstance(x, dict) and isinstance(x.get("bots"), dict)):
            top = sorted(x)[:8] if isinstance(x, dict) else type(x).__name__
            self._fail_load(
                f"状态文件结构不是预期形状（顶层应为含 bots 的对象，实际顶层：{top}）"
            )
            return

        self.data = x
        # 先置不脏再迁移：_migrate 只在真的改了数据时才标脏。顺序反了的话，
        # 迁移的结果会被当成「已落盘」而丢弃，下次启动又重跑一遍
        self._dirty = False
        # 这次是真的读到了：解除闸门（可能是上一轮 deferred 之后终于读上来的）
        self._load_failed = False
        self._write_blocked_warned = False
        self._disk_stamp = self._stat_stamp()
        # 盘上非对象的 bot/user/group 条目先归一化一次（只在这一处落盘修复）
        if self._normalize_containers():
            self.mark_dirty()
        self._merge_split_identities_once()
        self._migrate()

    def _read_json_with_retry(self, *, allow_sleep: bool = True) -> Any:
        """读一次状态文件；只有「被占用」这类瞬时错误才退避重试。

        内容坏了（JSON/编码）重试一万次也还是坏的，那属于要备份留证的另一条路；
        非瞬时的 OSError（磁盘错误之类）重试同样没有意义。所以这里只对
        `_is_transient_read_error` 判真的错误重试，其余原样抛给 load() 分类。
        """
        delays = _READ_RETRY_DELAYS if allow_sleep else ()
        attempt = 0
        while True:
            try:
                with open(self.path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError, FileNotFoundError):
                raise
            except OSError as exc:
                if attempt >= len(delays) or not self._is_transient_read_error(exc):
                    raise
                time.sleep(delays[attempt])
                attempt += 1

    @staticmethod
    def _is_transient_read_error(exc: BaseException) -> bool:
        """这个读盘错误是「过一会儿再来就好了」吗。

        判断只针对「文件现在被占着」这一类。真坏了（内容、权限永久拒绝）不在此列：
        对永久错误反复退避，等于每次启动白等 1.3 秒还读不出来。
        """
        if isinstance(exc, PermissionError):
            return True
        if not isinstance(exc, OSError):
            return False
        if getattr(exc, "errno", None) in _TRANSIENT_READ_ERRNOS:
            return True
        return getattr(exc, "winerror", None) in _TRANSIENT_READ_WINERRORS

    def _stat_stamp(self) -> Optional[Tuple[float, int]]:
        """盘上状态文件的 (mtime, size)；拿不到就 None（比对时按「不知道」跳过）。"""
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime, st.st_size)

    def _defer_load(self, exc: BaseException) -> None:
        """读盘被占用：原文件不动、不备份，只记「本次没加载上」，等心跳重试。

        与 `_fail_load` 的分界见 `load()` 的注释。写盘闸门**同样要关**：
        内存里现在是空壳，放开写等于拿空状态覆盖那份完好的记忆。
        """
        self.data = {"version": STATE_VERSION, "bots": {}}
        self._dirty = False
        self._load_failed = True
        self._load_deferred = True
        self._load_error = f"{type(exc).__name__}: {exc}"
        # 每次新的占用事件都该让人看见一次「本次没加载上」
        self._write_blocked_warned = False
        # 状态页/排障要知道「为什么这次没加载上」，所以错误原文要留着
        if throttle.allow("state.load_deferred", window=600.0):
            logger.error(
                "[autonomous_social] 状态文件读取被占用，本次未加载"
                f"（{self._load_error}）；原文件保持不动，稍后自动重试。"
                f"路径：{self.path}"
                + throttle.summary("state.load_deferred")
            )

    def retry_load(self, *, allow_sleep: bool = False) -> bool:
        """重试一次「上次因为读盘被占用而没加载上」的状态文件。返回是否加载成功。

        只在 deferred 时才真的重读：
        - 已经加载好的一律不碰——重读会把内存里还没落盘的改动丢掉；
        - 已经判定为内容损坏（备份过）的也不碰——备份只该做一次，重试只会
          一遍遍撞同一个坏文件，并把 .unreadable.* 越堆越多。

        allow_sleep 默认 False：周期心跳每 30 秒就会调到这里，退避等待会把事件
        循环卡住；文件锁放开之后单次尝试就能读出来。
        """
        if not self._load_deferred:
            return False
        # 占用期间插件仍会照常记账（往空壳里记），这些改动无法与盘上的旧内容合并
        # （同一个人的计数相加会翻倍）。重载会丢掉它们，所以要留一句实话。
        discarded = self._dirty
        self.load(allow_sleep=allow_sleep)
        if self._load_deferred or self._load_failed:
            return False
        if discarded:
            logger.warning(
                "[autonomous_social] 状态文件已恢复读取，但占用期间在内存里记下的"
                f"改动（还没落盘）已被磁盘上的旧内容替换，无法合并。路径：{self.path}"
            )
        logger.info(
            "[autonomous_social] 状态文件已恢复读取（之前被占用），"
            f"本次重试加载成功：{self.path}"
        )
        return True

    def load_status(self) -> Dict[str, Any]:
        """加载状态摘要，供状态页/排障面板显示「记忆到底加载上没有」。

        为什么不让人去翻日志：这两种失败的表现都是「她好像什么都忘了」，
        而处理方式完全不同——被占用等一会儿自己就好了，内容损坏必须人工去
        处理 .unreadable.* 文件。状态页上必须能一眼分开。
        """
        return {
            "ok": not self._load_failed,
            "deferred": bool(self._load_deferred),
            "error": self._load_error,
            "path": self.path,
        }

    def _normalize_containers(self) -> bool:
        """把盘上非对象的 bot / user / group 条目归一化成对象。返回是否真的改动过。

        手改配置或写入损坏会把这些位置留成字符串/列表；它们处在**几乎所有状态 API
        的入口**（`bot()` / `user()`），脏值会让每一条消息的处理都抛异常。
        """
        changed = False
        bots = self.data.get("bots")
        if not isinstance(bots, dict):
            self.data["bots"] = {}
            return True
        for bid, bot in list(bots.items()):
            if not isinstance(bot, dict):
                bots[bid] = {"users": {}, "last_global_send": 0.0}
                changed = True
                continue
            users = bot.get("users")
            if not isinstance(users, dict):
                bot["users"] = {}
                changed = True
                users = bot["users"]
            for uid, u in list(users.items()):
                if not isinstance(u, dict):
                    users[uid] = self._default_user()
                    changed = True
            groups = bot.get("groups")
            if groups is not None and not isinstance(groups, dict):
                bot["groups"] = {}
                changed = True
            elif isinstance(groups, dict):
                for gid, g in list(groups.items()):
                    if not isinstance(g, dict):
                        groups[gid] = self._default_group()
                        changed = True
        return changed

    def _merge_split_identities_once(self) -> None:
        """把「同一个人被拆成多个 uid」的记录合并回一条。

        历史导入按 umo 取 uid，而 WebChat 的 umo 带会话 UUID（`webchat!naliling!<uuid>`），
        于是每次开新会话都建成一个「新人」：各自攒念头、各自收问候、关系与冷落记录被
        拆散。修法（history_ingest.stable_uid）只对**新导入**生效，盘上已经拆开的
        需要在这里合一次。

        合并规则：uid 去掉末段会话标识后相同、且都带 umo 的，归到最短的那个 uid 上；
        计数类字段求和、最近类字段取新、文本类保留较长的那份。
        """
        if self.data.get("_merged_identities_v1"):
            return
        try:
            moved = 0
            for bot in self.data.get("bots", {}).values():
                if not isinstance(bot, dict):
                    continue
                users = bot.get("users")
                if not isinstance(users, dict):
                    continue
                groups: Dict[str, List[str]] = {}
                for uid in list(users.keys()):
                    if not isinstance(uid, str) or "!" not in uid:
                        continue
                    head = "!".join(uid.split("!")[:2])
                    groups.setdefault(head, []).append(uid)
                for _head, members in groups.items():
                    if len(members) < 2:
                        continue
                    # 最短的那个当主记录（最接近稳定身份）
                    members.sort(key=lambda x: (len(x), x))
                    main = members[0]
                    tm = users.get(main) if isinstance(users.get(main), dict) else {}
                    for uid in members[1:]:
                        other = users.get(uid)
                        if not isinstance(other, dict) or not tm:
                            continue
                        self._merge_user(tm, other)
                        users.pop(uid, None)
                        moved += 1
                    users[main] = tm
            self.data["_merged_identities_v1"] = True
            self.mark_dirty()
            if moved:
                logger.info(
                    f"[autonomous_social] 已把 {moved} 条被拆开的同账号记录合并回去"
                    "（WebChat 每个会话 UUID 曾被当成一个新人）"
                )
        except Exception as exc:
            logger.warning(f"[autonomous_social] 合并同账号身份失败（不影响运行）: {exc}")

    @staticmethod
    def _merge_user(target: Dict[str, Any], other: Dict[str, Any]) -> None:
        """把 other 并进 target。计数相加、最近取大、文本取长。"""
        sum_fields = ("proactive_sent", "proactive_replied", "message_count", "reply_samples")
        max_fields = ("last_seen", "last_sent", "last_spoken", "last_proactive_sent",
                      "last_replied_at", "last_ignored_at", "aggr_high_n", "interest_at")
        long_fields = ("last_message", "last_spoken_text", "name")
        for f in sum_fields:
            try:
                target[f] = int(target.get(f, 0) or 0) + int(other.get(f, 0) or 0)
            except (TypeError, ValueError):
                pass
        for f in max_fields:
            try:
                target[f] = max(float(target.get(f, 0) or 0), float(other.get(f, 0) or 0))
            except (TypeError, ValueError):
                pass
        for f in long_fields:
            if len(str(other.get(f, "") or "")) > len(str(target.get(f, "") or "")):
                target[f] = other.get(f)
        for f in ("topics", "recent_msg_types", "proactive_log", "conversation"):
            a, b = target.get(f), other.get(f)
            if isinstance(a, list) and isinstance(b, list):
                for item in b:
                    if item not in a:
                        a.append(item)
                target[f] = a
        # `topic_seen_at`（话题 → 最后一次出现）：合并取**较晚**的那个。
        # 取并集的话，同一个话题会留两份时间戳，而它回答的是「最后一次」——
        # 取早的那个等于把重新聊过的话题判成旧的。与 `no_reply_streak` 取小、
        # `urge` 取高是同一条原则：合并要按这个字段**自己的语义**取边。
        seen_a, seen_b = target.get("topic_seen_at"), other.get("topic_seen_at")
        if isinstance(seen_a, dict) or isinstance(seen_b, dict):
            merged = dict(seen_a) if isinstance(seen_a, dict) else {}
            if isinstance(seen_b, dict):
                for key, val in seen_b.items():
                    try:
                        if float(val or 0) > float(merged.get(key, 0) or 0):
                            merged[key] = val
                    except (TypeError, ValueError):
                        merged[key] = val
            target["topic_seen_at"] = merged
        # conversation / proactive_log 合并后要按时间重排：`threads` / `recent_exchange`
        # 靠 `reversed(conversation)` 取「最近一句」，不重排会取到更早的那条。
        for f in ("conversation", "proactive_log"):
            rows = target.get(f)
            if isinstance(rows, list) and rows:
                target[f] = sorted(
                    (i for i in rows if isinstance(i, dict)),
                    key=lambda i: _safe_ts(i.get("ts")),
                )
        # 上面只做了并集，**没有重新夹长度**：两份各自合规（各 10 条）的记录并起来就是
        # 20 条，而写路径要等到**下一次** `record_incoming` / `record_outgoing` 才会
        # `del conv[:-MAX_CONVERSATION_HISTORY]`。中间这段时间 `state.json` 是超容的，
        # `reversed(conversation)` 取「最近一句」也会取到本该被砍掉的那批。
        # 口径与写路径完全一致：留最新的那几条（列表是时间升序，所以切尾巴）。
        conv = target.get("conversation")
        if isinstance(conv, list) and len(conv) > MAX_CONVERSATION_HISTORY:
            target["conversation"] = conv[-MAX_CONVERSATION_HISTORY:]
        log = target.get("proactive_log")
        if isinstance(log, list) and len(log) > PROACTIVE_LOG_MAX:
            target["proactive_log"] = log[-PROACTIVE_LOG_MAX:]
        types = target.get("recent_msg_types")
        if isinstance(types, list) and len(types) > RECENT_MSG_TYPES_COUNT:
            target["recent_msg_types"] = types[-RECENT_MSG_TYPES_COUNT:]
        # 冷落计数取小的：合并的是同一个人，保留较宽松的那个判断更合理
        try:
            target["no_reply_streak"] = min(int(target.get("no_reply_streak", 0) or 0),
                                            int(other.get("no_reply_streak", 0) or 0))
        except (TypeError, ValueError):
            pass
        # 念头取高的：别因为合并把「她很想说话」这件事抹掉
        try:
            target["urge"] = max(float(target.get("urge", 0.0) or 0.0),
                                  float(other.get("urge", 0.0) or 0.0))
        except (TypeError, ValueError):
            pass
        # 发送地址取有 umo 的那个
        if not target.get("umo") and other.get("umo"):
            target["umo"] = other["umo"]

    def _sanitize_once(self) -> None:
        """一次性洗掉正文里混进去的框架注入块。

        清洗规则上线之前录进去的 `last_message`/`conversation`/`proactive_log`/
        `topics` 里带着 Core 的「〔她的身体与生活 v10〕」和 <system_reminder>。
        它们会被当成「对方说的话」喂回给模型，也会污染话题与时间锚点提取。

        只跑一次（认 `_sanitized_v1` 标记）：这是全量遍历所有用户，180 人规模下
        每次启动都跑一遍不划算；而清洗是幂等的，跑一次就够。
        """
        if self.data.get("_sanitized_v1"):
            return
        try:
            touched = sanitize_state(self.data)
        except Exception as exc:
            logger.warning(
                f"[autonomous_social] 清洗历史正文失败（不影响运行）: {exc}"
            )
            return
        self.data["_sanitized_v1"] = True
        if touched:
            # 记下清洗规模，状态页能看：这个数字不为 0 就说明之前确实录进过框架内容
            self.data["_sanitized_users"] = int(self.data.get("_sanitized_users", 0)) + touched
            self.mark_dirty()
            logger.info(
                f"[autonomous_social] 已清洗 {touched} 个用户的正文："
                "剥掉了混进聊天记录里的框架注入内容（Humanoid Core 身体事实块、"
                "<system_reminder>）"
            )

    def _fail_load(self, reason: str) -> None:
        """加载失败：备份原文件、报错、上闸门；内存留空壳且不标脏。"""
        logger.error(f"[autonomous_social] {reason}，路径：{self.path}")
        self._backup_unreadable()
        self.data = {"version": STATE_VERSION, "bots": {}}
        self._dirty = False
        self._load_failed = True

    def _backup_unreadable(self) -> None:
        """把读不出来的原文件挪到一边，别让它被空状态覆盖。"""
        try:
            backup_path = f"{self.path}.unreadable.{int(time.time())}"
            if os.path.exists(self.path):
                os.rename(self.path, backup_path)
                logger.error(
                    f"[autonomous_social] 原状态文件已备份到 {backup_path}，"
                    "确认后可自行删除；在修好它并重载插件之前，插件不会写回新状态。"
                )
        except OSError:
            pass

    # v1.7.7 的「悬空跟进」在 v1.8.0 换成了不分谁说话的 thread_*，旧键留着只会让人
    # 以为还有人在读它。
    _RETIRED_USER_KEYS = (
        "last_spoken_question", "followup_for", "followup_at",
    )

    @staticmethod
    def _repair_fields(record: Dict[str, Any], defaults: Dict[str, Any]) -> int:
        """按默认值的类型修一条记录里的字段，返回修了几个。就地改，不抛。

        为什么必须在**加载期**做：单个字段类型坏掉（手改 state.json、旧版本写错、
        磁盘位翻转）之后，读它的每一处都得自己挡一次——`record_incoming` 的
        `int(u["message_count"])`、`_extract_topics` 的 `topics.insert`、
        `reply_rate` 的 `int(...)` 都是。漏掉一处，这个用户从此**每条消息都抛
        异常**，而且不会自愈：异常发生在 `mark_dirty()` 之前，坏值永远写不回盘。
        与其在每个读点补 isinstance，不如在唯一入口把它修成正常值，读点照旧写。

        只修「类型不对」的，不修「值不好看」的：`message_count=999999` 是数据，
        `message_count="abc"` 才是损坏。JSON 里 `5.0` 这种整数值也按 5 收下。
        """
        repaired = 0
        for key, default in defaults.items():
            if key not in record:
                continue
            value = record[key]
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    record[key] = default
                    repaired += 1
            elif isinstance(default, (int, float)):
                # bool 是 int 的子类：`true` 落在 message_count 上是脏值，不是 1
                if not _is_number(value):
                    record[key] = default
                    repaired += 1
                elif isinstance(default, int):
                    # 整数字段收下整数值的浮点（5.0），避免下次 `int()` 之外的地方
                    # 出现 5.0 与 5 的比较/哈希差异
                    if isinstance(value, float) and value.is_integer():
                        record[key] = int(value)
                        repaired += 1
            elif isinstance(default, str):
                if not isinstance(value, str):
                    record[key] = default
                    repaired += 1
            elif isinstance(default, list):
                if not isinstance(value, list):
                    record[key] = list(default)
                    repaired += 1
            elif isinstance(default, dict):
                if not isinstance(value, dict):
                    record[key] = dict(default)
                    repaired += 1
            elif default is None:
                # 目前只有 interest：None 或有限数都合法，其余按「没算过」重置
                if value is not None and not _is_number(value):
                    record[key] = None
                    repaired += 1
        return repaired

    def _migrate(self) -> None:
        """确保所有已存在的用户都有当前版本字段，清掉旧键，并洗掉被污染的正文。"""
        self._sanitize_once()
        repaired_users = 0
        repaired_groups = 0
        try:
            for bot in self.data.get("bots", {}).values():
                if not isinstance(bot, dict):
                    continue
                user_defaults = self._default_user()
                for uid, u in bot.get("users", {}).items():
                    if not isinstance(u, dict):
                        continue
                    for k, v in user_defaults.items():
                        u.setdefault(k, v)
                    repaired_users += self._repair_fields(u, user_defaults)
                    for k in self._RETIRED_USER_KEYS:
                        if k in u:
                            u.pop(k, None)
                            self.mark_dirty()
                # v1.11.0 群会话：补齐新键（加法式，不动已有 users）
                groups = bot.get("groups")
                if isinstance(groups, dict):
                    group_defaults = self._default_group()
                    for g in groups.values():
                        if not isinstance(g, dict):
                            continue
                        for k, v in group_defaults.items():
                            g.setdefault(k, v)
                        repaired_groups += self._repair_fields(g, group_defaults)
                # v1.12.0：清掉旧版历史导入误进 users 的群会话幽灵。旧版按 updated_at
                # 取前 300 个会话再滤群，`GroupMessage` 类型不在识别表里被判 unknown
                # 放行，群会话被当「人」导入、拿群 umo 去发主动消息。群消息在观察链路
                # 里从不建 per-user 记录，所以 users 里 umo 是群会话的一定是幽灵。
                #
                # **改成每次启动都跑**：两个入口现在已经不会再产出群 umo 用户了
                # （群会话导入无条件跳过、seed 名单拒绝群类型），所以留在 users 里的
                # 群 umo 一定是幽灵。原来只跑一次，而那个标记在更早的版本里「只写不读」
                # —— 升级上来时它已经是 true，于是这次清扫根本不执行，幽灵永远清不掉，
                # 而幽灵带着群 umo，发送路径会照着它把消息发进群。
                # 只有 seed 来的条目需要豁免：老配置里可能真写了 qq:group:12345。
                users = bot.get("users")
                if isinstance(users, dict):
                    for uid, u in list(users.items()):
                        if not isinstance(u, dict):
                            continue
                        segs = str(u.get("umo", "") or "").lower().split(":")
                        mtype = segs[1] if len(segs) >= 3 else ""
                        if mtype in _GROUP_UMO_TYPES:
                            if str(u.get("source") or "") == "seed":
                                continue
                            users.pop(uid)
                            self.mark_dirty()
        except Exception as exc:
            # 这里以前是 `except Exception: pass`：一条坏记录会废掉整条迁移链
            # （后面所有 bot 的字段补齐、退休键清理、幽灵清扫全都不跑），
            # 而状态停在「半迁移」且没有任何人看得见。不写日志等于静默损坏。
            logger.error(
                f"[autonomous_social] 状态迁移中断（{type(exc).__name__}: {exc}），"
                "部分字段可能没补齐，路径：" + str(self.path)
            )
        if repaired_users or repaired_groups:
            # 只报数字不报正文：这些字段可能含消息内容，日志不该多一份副本
            self.mark_dirty()
            logger.warning(
                f"[autonomous_social] 状态文件里有 {repaired_users} 个用户字段、"
                f"{repaired_groups} 个群字段类型不对，已按默认值重置"
                "（不修的话这些用户此后每条消息都会抛异常，且不会自愈）。路径："
                + str(self.path)
            )

    def migrate_default_bot(self, real_bid: str) -> None:
        """将 "default" bot 下的数据迁移到真实 bid 下。

        这是为了修复 v1.5.0 及更早版本中 bot_id 恒为 "default" 的 bug。
        迁移成功后删除 "default" key，标记已迁移避免重复执行。
        """
        bots = self.data.get("bots", {})
        if DEFAULT_BOT_KEY not in bots:
            return  # 没有旧数据，无需迁移
        if real_bid == DEFAULT_BOT_KEY:
            return  # 还是 default，没法迁
        if self.data.get("_default_migrated"):
            return  # 已经迁移过了

        # 多 bot 串台保护：default 里可能混了多个角色的播种/历史用户（_seed_target_bid 在
        # 2+ 个真实 bot 时统一归 default）。若除了 real_bid 还存在其它真实 bot，把 default
        # 整批搬给 real_bid 会把 B 的人误搬到 A 名下——发送时走 A 的适配器，而 A 不是这个人的
        # 好友，就报“请添加对方为好友”。这种时候宁可不搬：留在 default 照样结算，
        # 人格按 umo 解析仍正确，发送按 umo 平台路由，不主动把别人的人污染过去。
        other_real_bots = [
            b for b in bots
            if b not in (DEFAULT_BOT_KEY, real_bid) and (bots.get(b) or {}).get("users")
        ]
        if other_real_bots:
            return

        default_bot = bots[DEFAULT_BOT_KEY]
        if real_bid not in bots or not (bots[real_bid].get("users") or bots[real_bid].get("groups")):
            # 目标 bid 什么都没有，直接搬过去
            bots[real_bid] = default_bot
            del bots[DEFAULT_BOT_KEY]
            self.data["_default_migrated"] = True
            self.mark_dirty()
        else:
            # 目标 bid 已有数据，只合并用户（不覆盖已有用户）
            real_bot = bots[real_bid]
            real_users = real_bot.setdefault("users", {})
            default_users = default_bot.get("users", {})
            for uid, udata in default_users.items():
                if uid not in real_users:
                    real_users[uid] = udata
            # 合并全局发送时间（取较大的）
            default_last_send = default_bot.get("last_global_send", 0)
            real_last_send = real_bot.get("last_global_send", 0)
            if default_last_send > real_last_send:
                real_bot["last_global_send"] = default_last_send
            del bots[DEFAULT_BOT_KEY]
            self.data["_default_migrated"] = True
            self.mark_dirty()

    # ─── 过期数据清理 ───────────────────────────────────

    def settle_pending(self, bid: str, uid: str, now: float, window_seconds: float) -> str:
        """结算上一次主动联系的结局：窗口内没等到回复就算没人理。

        对方后来发了消息的情况在 record_incoming 里已经处理掉了，这里负责的是
        「发出去之后 TA 再也没说话」这种永远不会触发记录回调的情况。

        Returns:
            结算后的状态：waiting / ignored，已结算过的返回当前值
        """
        u = self.user(bid, uid)
        pending = _num(u.get("pending_since", 0))
        if pending <= 0 or u.get("pending_result") != "waiting":
            return str(u.get("pending_result", "") or "")
        if now - pending <= window_seconds:
            return "waiting"
        desire.after_ignored(u, now)
        self.mark_dirty()
        return "ignored"

    def max_no_reply_streak(self, bid: str) -> int:
        """该角色下最严一次「连发几条没人接」，写回给 Core 当一句压着的挂念。"""
        users = self.bot(bid).get("users", {})
        worst = 0
        for user in users.values():
            if not isinstance(user, dict):
                continue
            try:
                worst = max(worst, int(user.get("no_reply_streak", 0) or 0))
            except (TypeError, ValueError):
                continue
        return worst

    def prune_expired(self, days: int, now: Optional[float] = None) -> int:
        """把连续 days 天没说过话的用户的历史正文/话题清掉，返回被清理的用户数。

        用户条目只会新增、不会删除，而整个状态文件每次保存都要全量序列化；
        不限量地攒下去，落盘开销会随曾经聊过天的人数线性增长。days <= 0 表示永不清理。
        从未记过 last_seen（为 0）的用户一律跳过，不误清无法判定的记录。
        """
        try:
            days = int(days)
        except (TypeError, ValueError):
            return 0
        if days <= 0:
            return 0

        now = self._now() if now is None else float(now)
        cutoff = now - days * 86400.0
        pruned = 0

        for bot in self.data.get("bots", {}).values():
            if not isinstance(bot, dict):
                continue
            for u in bot.get("users", {}).values():
                if not isinstance(u, dict):
                    continue
                has_heavy = bool(
                    any(u.get(k) for k in RETENTION_HEAVY_LIST_FIELDS)
                    or u.get("last_message")
                    or u.get("cue")
                    or any(u.get("active_hours") or ())
                    or u.get("loops") or u.get("promises") or u.get("said") or u.get("anchors")
                )
                if not has_heavy:
                    continue
                try:
                    seen = float(u.get("last_seen", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if seen <= 0 or seen > cutoff:
                    continue
                for key in RETENTION_HEAVY_LIST_FIELDS:
                    u[key] = []
                # 后加的「按用户挂在盘上的结构」也要一起清，否则长期不活跃的用户会
                # 一直占着 loops/promises/said/anchors（旧实现只清 cue/loop 平铺字段）。
                u["loops"] = []
                u["promises"] = []
                u["said"] = []
                u["anchors"] = []
                for key in RETENTION_HEAVY_TEXT_FIELDS:
                    u[key] = ""
                u["active_hours"] = [0.0] * RETENTION_RHYTHM_HOURS
                u["rhythm_samples"] = 0
                u["cue"] = ""
                u["cue_due"] = 0.0
                u["cue_expire_at"] = 0.0
                u["cue_retry_at"] = 0.0
                u["cue_tries"] = 0
                u["loop"] = ""
                u["loop_due"] = 0.0
                u["loop_expire_at"] = 0.0
                u["loop_retry_at"] = 0.0
                u["loop_tries"] = 0
                pruned += 1

        if pruned:
            self.mark_dirty()
        return pruned

    # ─── 持久化 ─────────────────────────────────────────

    def prune_stale_groups(self, stale_days: int, now: Optional[float] = None) -> int:
        """丢掉超过 stale_days 天没见过任何消息的群（被踢/退群/解散），返回被清群数。

        心流只由实时群消息驱动，被踢自然收不到消息、不再触发；这里把长期不活跃
        的群连样本一起删，既防状态文件膨胀，也确保不再对它破冰。从未记过 last_seen
        （为 0）的群跳过。stale_days <= 0 表示不清理。
        """
        try:
            stale_days = int(stale_days)
        except (TypeError, ValueError):
            return 0
        if stale_days <= 0:
            return 0
        now = self._now() if now is None else float(now)
        cutoff = now - stale_days * 86400.0
        removed = 0
        for bot in self.data.get("bots", {}).values():
            if not isinstance(bot, dict):
                continue
            groups = bot.get("groups")
            if not isinstance(groups, dict):
                continue
            for gid in list(groups.keys()):
                g = groups.get(gid)
                if not isinstance(g, dict):
                    continue
                try:
                    seen = float(g.get("last_seen", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if seen <= 0 or seen > cutoff:
                    continue
                del groups[gid]
                removed += 1
        if removed:
            self.mark_dirty()
        return removed

    def mark_dirty(self) -> None:
        """标记状态已变更但暂不写入磁盘。

        顺带推进变更代数：异步写盘时「快照」与「落盘完成」之间隔了几十毫秒，
        这中间主线程可能又改了状态。落盘后只能清掉快照那一刻的脏标记，
        否则这几十毫秒里的改动会被当成已落盘而永久丢失。
        """
        self._dirty = True
        self._gen += 1

    def _warn_write_blocked(self) -> None:
        """闸门挡住写盘时只报一次。

        这条日志会被每 30 秒一次的心跳反复撞到，不节流会把日志刷满；
        但也不能不报——用户看到的现象是「她什么都忘了」，必须有人告诉他为什么。
        """
        if self._write_blocked_warned:
            return
        self._write_blocked_warned = True
        if self._load_deferred:
            # 「被占用」和「文件坏了」的处理方式完全不同，日志必须能分开
            logger.error(
                "[autonomous_social] 状态写入暂停：状态文件读取被占用"
                f"（{self._load_error}），本次没加载上，继续写会拿空状态覆盖原文件。"
                f"原文件保持不动，稍后自动重试。路径：{self.path}"
            )
            return
        logger.error(
            f"[autonomous_social] 状态写入已暂停：原文件读不出来，"
            f"继续写会拿空状态覆盖掉它。处理完请重载插件。路径：{self.path}"
        )

    def save(self) -> None:
        """立即写入磁盘（原子操作）。

        读盘失败时**不抛**而是记一条日志就返回：发送成功的路上会调到这里，
        抛出去会让人看到「以为没发其实发了」——消息平台已经收到了，状态却因为
        一个存档问题没记上，整轮心跳还被异常打断。写盘闸门本身已经由 flush()
        在前面挡着，这里报一声就够了。

        这个方法把「序列化 + 写盘 + fsync」整套同步跑完。事件循环上要避免
        整段阻塞时用 `save_async()`（序列化仍在当前线程，只有写盘进工作线程）。
        """
        if self._load_failed:
            self._warn_write_blocked()
            return
        try:
            self._write()
            self._dirty = False
        except Exception as e:
            if throttle.allow("state.save_failed"):
                logger.error(
                    f"[autonomous_social] 保存状态失败: {e}"
                    + throttle.summary("state.save_failed")
                )

    def flush(self) -> bool:
        """如果有变更则写入磁盘。适合周期性调用。

        Returns:
            True 表示执行了写入，False 表示没有变更、写入被闸门拦下、或写入失败
        """
        # 上次因为「文件被占用」没加载上：这里是心跳里最稳定的重试点。
        # 必须放在闸门判断**之前**——否则闸门一关就再也没有第二次机会，
        # 只能等用户手动重载插件，而那正是这次要修掉的事。
        if self._load_deferred:
            self.retry_load()
        if self._load_failed:
            self._warn_write_blocked()
            return False
        if self._dirty:
            try:
                self._write()
                self._dirty = False
                return True
            except Exception as e:
                # 磁盘满、权限变化之类：_dirty 保持 True 会重试，但必须让人知道
                # 数据正在丢，不能只默默返回 False。写盘每 30 秒一次，不节流的话
                # 磁盘满会在几分钟内把日志刷满，反而把「磁盘满」这条关键信息埋掉
                if throttle.allow("state.write_failed"):
                    logger.error(
                        f"[autonomous_social] 状态写盘失败（稍后重试）: {e}"
                        + throttle.summary("state.write_failed")
                    )
                return False
        return False

    # ─── 异步写盘 ────────────────────────────────────────
    #
    # 为什么要有这一套：`save()`/`flush()` 把「全量序列化 + 写 tmp + fsync +
    # replace」整套同步跑完。用户数上千时一次 dumps 就是上百毫秒，机械盘/网络盘上
    # fsync 还能再拖几十毫秒，而这些都跑在事件循环线程上——整轮心跳里所有人的消息
    # 处理都被这一下卡住，表现是回复变慢、心跳周期被拉长。
    #
    # 拆法：序列化留在调用方线程（它要读 self.data，挪进工作线程反而会和主线程的
    # 修改打架），只把「写 tmp + fsync + replace」交给工作线程。同步的 save()/flush()
    # 原样保留——调用点很多，行为不能变。

    def snapshot(self) -> Optional[Tuple[str, int]]:
        """取一份「状态快照」：(序列化好的文本, 变更代数)。被闸门拦下返回 None。"""
        if self._load_failed:
            self._warn_write_blocked()
            return None
        try:
            return (self._dump(), self._gen)
        except Exception as e:
            if throttle.allow("state.snapshot_failed"):
                logger.error(
                    f"[autonomous_social] 状态快照序列化失败: {e}"
                    + throttle.summary("state.snapshot_failed")
                )
            return None

    def write_snapshot(self, snapshot: Tuple[str, int]) -> bool:
        """把 snapshot() 的结果写盘，返回是否成功。**可以在工作线程里调用。**"""
        payload, gen = snapshot
        with self._write_lock:
            try:
                self._write_payload(payload)
            except Exception as e:
                if throttle.allow("state.write_failed"):
                    logger.error(
                        f"[autonomous_social] 状态写盘失败（稍后重试）: {e}"
                        + throttle.summary("state.write_failed")
                    )
                return False
            # 只有「从快照到现在没人再改」才清脏标记：否则这几十毫秒里的改动
            # 会被当成已落盘，永久丢失
            if self._gen == gen:
                self._dirty = False
            return True

    async def flush_async(self) -> bool:
        """flush() 的异步版：序列化在事件循环，写盘丢给工作线程。

        返回值语义与 flush() 一致（是否执行了写入）。
        """
        if self._load_deferred:
            self.retry_load()
        if self._load_failed:
            self._warn_write_blocked()
            return False
        if not self._dirty:
            return False
        snap = self.snapshot()
        if snap is None:
            return False
        return await asyncio.to_thread(self.write_snapshot, snap)

    async def save_async(self) -> bool:
        """save() 的异步版（不看脏标记，调用方明确要求落盘时用）。"""
        if self._load_failed:
            self._warn_write_blocked()
            return False
        snap = self.snapshot()
        if snap is None:
            return False
        return await asyncio.to_thread(self.write_snapshot, snap)

    def _dump(self) -> str:
        """把状态序列化成落盘用的文本。

        紧凑格式：这份文件每次保存都要全量序列化，indent=2 在用户数上千时
        会把单次 dumps 拖到上百毫秒，而它跑在事件循环上。不加缩进后体积约降四分
        之一、序列化快 3 倍。格式变更不影响读取（json.load 不关心缩进）。
        """
        return json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))

    def _write(self) -> None:
        """同步写盘：序列化 + 写盘。行为与以前完全一致。"""
        payload = self._dump()
        with self._write_lock:
            self._write_payload(payload)

    @contextlib.contextmanager
    def _cross_process_lock(self):
        """尽力而为的跨进程写锁（`<state>.lock`，O_EXCL 创建）。

        为什么需要：两个 AstrBot 实例（或一个实例 + 手工脚本）同时写这份文件时，
        全量重写会让后写的那个把前一个刚写进去的更新整份盖掉，双方都不报错。
        锁只保证「同一时刻只有一个人在写」。拿不到锁**不阻塞**——发送与结算链路
        都建立在「状态能写下去」上，为了一个锁把整轮心跳卡死更糟，所以降级成
        告警：让「你是不是开了两个实例」这件事至少能被看见，而不是静默覆盖。

        锁文件在进程崩溃后会残留，超过 `_LOCK_STALE_SECONDS` 就认定是残骸并抢占，
        否则一次崩溃会让这个状态文件从此再也写不进去。
        """
        lock_path = self.path + ".lock"
        held = False
        try:
            for attempt in (0, 1):
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    if attempt == 0 and self._lock_is_stale(lock_path):
                        with contextlib.suppress(OSError):
                            os.remove(lock_path)
                        continue
                    break
                except OSError:
                    # 目录不可写之类：锁本身拿不到，但写盘大概率也会失败，
                    # 让真正的写盘错误去报错，这里不抢戏
                    break
                try:
                    os.write(fd, str(os.getpid()).encode("ascii"))
                except OSError:
                    pass
                finally:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                held = True
                break
            if not held and throttle.allow("state.lock_busy", window=600.0):
                logger.warning(
                    "[autonomous_social] 状态文件写锁被占用（可能是另一个实例在写），"
                    "本次仍会写入，但可能覆盖对方的更新；请确认只有一个实例在跑。"
                    f"路径：{lock_path}"
                    + throttle.summary("state.lock_busy")
                )
            yield
        finally:
            if held:
                with contextlib.suppress(OSError):
                    os.remove(lock_path)

    @staticmethod
    def _lock_is_stale(lock_path: str) -> bool:
        """锁文件是不是上一个进程崩溃留下的残骸（按 mtime 判断）。"""
        try:
            return time.time() - os.stat(lock_path).st_mtime > _LOCK_STALE_SECONDS
        except OSError:
            return False

    def _check_external_change(self) -> None:
        """写盘前比对盘上文件的 (mtime, size)：不是我们上次写的那份就告警。

        为什么只告警、不阻止写：这个插件的主链路（发送、结算、心跳）都建立在
        「状态写下去」上，锁住写盘会让整轮心跳空转；而另一个写者是谁我们并不知道
        （手改、备份还原、第二个实例）。所以先让人看见，由人决定怎么处理。

        为什么不告警就无法排障：全量重写是静默覆盖——用户改了 state.json 里的
        配置、或者第二个实例刚写入新用户，都会被下一次心跳整份抹掉，日志里
        一个字节都不会有。
        """
        stamp = self._stat_stamp()
        if stamp is None or self._disk_stamp is None or stamp == self._disk_stamp:
            return
        if throttle.allow("state.external_change", window=600.0):
            logger.warning(
                "[autonomous_social] 状态文件在本次加载后被外部改动过"
                f"（上次 {self._disk_stamp} → 现在 {stamp}），"
                "本次写盘会以内存里的版本为准覆盖它。"
                "如果同时开了两个实例，请只保留一个，否则会互相丢更新。"
                + throttle.summary("state.external_change")
            )

    def _write_payload(self, payload: str) -> None:
        """把已序列化好的文本原子写到磁盘（调用方负责 `_write_lock`）。"""
        dir_path = os.path.dirname(self.path) or "."
        os.makedirs(dir_path, exist_ok=True)

        with self._cross_process_lock():
            self._check_external_change()
            fd, tmp_path = tempfile.mkstemp(
                prefix="social_", suffix=".tmp", dir=dir_path
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.path)
                # fsync 文件只保证**内容**落盘；os.replace 这个目录项变更没有 fsync 目录
                # 就仍在页缓存里，断电时可能出现「新数据在盘上但目录项还指向旧 inode」，
                # 甚至整个 state.json 消失（只留 social_*.tmp）。对一个全量重写、丢了就丢
                # 全部用户历史的状态文件，这是唯一一处能造成整文件损失的窗口。
                # Windows 上目录 fsync 不可用（os.open 直接失败），必须容错。
                try:
                    dfd = os.open(dir_path, os.O_RDONLY)
                except OSError:
                    dfd = -1
                if dfd >= 0:
                    try:
                        os.fsync(dfd)
                    except OSError:
                        pass
                    finally:
                        try:
                            os.close(dfd)
                        except OSError:
                            pass
            finally:
                # 清理临时文件
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        # 这份文件现在是「我们写的」：记下指纹，下次写盘前比对才不会把自己的写入
        # 当成外部改动（否则每次写都会报一次假的「被别人改了」）
        self._disk_stamp = self._stat_stamp()

    # ─── 消息记录 ───────────────────────────────────────

    def _fallback_user(self, uid: str) -> Optional[Dict[str, Any]]:
        """兜底桶（`default`）里同一个人的记录；没有就 None。**只读，不建条目。**"""
        bots = self.data.get("bots", {})
        bot = bots.get(DEFAULT_BOT_KEY) if isinstance(bots, dict) else None
        if not isinstance(bot, dict):
            return None
        users = bot.get("users")
        if not isinstance(users, dict):
            return None
        rec = users.get(str(uid or ""))
        return rec if isinstance(rec, dict) else None

    def _settle_pending_reply(
        self,
        u: Dict[str, Any],
        ts: float,
        reply_window_seconds: int,
        pending_window_seconds: Optional[float],
    ) -> None:
        """结算「上一次主动联系有没有被接住」，并给回复率记账。

        抽成独立方法是因为它要对**两条**记录各跑一次：当前 bot 下的那条，以及兜底桶
        （`default`）里同一个人的那条。历史导入时（2+ 个真实 bot）用户被统一归到
        `default`，主动消息也从那里发出去；但他们的回复从**真实 bid** 进来，落到那边
        新建的记录上。只结算 bid 那条的话，兜底桶里的 `proactive_replied` 永远是 0、
        `no_reply_streak` 永远停在 1——「发了一堆没人理」对这批人永远不成立，
        退避也就永远不触发。线上 `default` 里 41 条主动消息、0 条回复就是这么来的。
        """
        pending_window = pending_window_seconds or reply_window_seconds
        pending = _num(u.get("pending_since", 0))
        if pending > 0 and u.get("pending_result") == "waiting":
            if ts - pending <= pending_window:
                desire.after_reply(u, ts)
            else:
                desire.after_ignored(u, ts)
        elif u.get("pending_result") == "ignored":
            # 12 小时没回就已经被判过 ignored、pending_since 也被清零了。等到对方
            # 隔夜回一句，那条记账压根不在了——after_reply 永远不会被调用，人就一直
            # 冷着。人回来了就是回来了，这里把他叫回来；不进回复率分母（那是 2 小时
            # 的另一个口径）。
            desire.forgive_late(u, ts)

        # 回复检测：只有在回复窗口内才算
        last_pro = _num(u.get("last_proactive_sent", 0))
        if last_pro > 0 and not u.get("last_proactive_replied", True):
            if ts - last_pro <= reply_window_seconds:
                u["last_proactive_replied"] = True
                u["proactive_replied"] = int(u.get("proactive_replied", 0)) + 1
                # 滚动平均回复时间
                reply_time = ts - last_pro
                samples = int(u.get("reply_samples", 0))
                old_avg = _num(u.get("avg_reply_seconds", 0))
                if samples == 0:
                    u["avg_reply_seconds"] = reply_time
                else:
                    u["avg_reply_seconds"] = (old_avg * samples + reply_time) / (samples + 1)
                u["reply_samples"] = samples + 1
            # else: 超过窗口了，不算回复，但 last_proactive_replied 保持 False
            # （下一次主动消息会重置它）

    def record_incoming(
        self,
        bid: str,
        uid: str,
        text: str,
        name: Optional[str] = None,
        reply_window_seconds: int = 21600,
        max_topics: int = 8,
        store_text: bool = True,
        cue: Optional[str] = None,
        cue_due: float = 0.0,
        loop: Optional[str] = None,
        loop_due: float = 0.0,
        pending_window_seconds: Optional[float] = None,
        hour_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        """记录用户发来的消息，并结算上一次主动联系的结局。

        Args:
            bid: bot ID
            uid: 用户 ID
            text: 消息文本
            name: 用户昵称
            reply_window_seconds: 回复窗口（秒），超过这个时间的消息不算回复
            max_topics: 最多保留多少个话题
            store_text: 是否持久化消息正文（关闭后仅统计、不存正文/话题，降低隐私留存）
            cue: 从这条消息里提取到的时间锚点（「明天面试」），None 表示没有
            cue_due: 这个锚点大概什么时候可以顺嘴问一句
            loop: 没有锚点但结果还没说出来的那件事（「今天面试了」）
            loop_due: 隔多久回访比较像人（几小时到一夜）
            pending_window_seconds: 多久没回才算「没接话」；比回复统计窗口宽，默认同值
            hour_override: 作息直方图用哪个小时记账（她所在城市的小时）。None 用本机小时

        Returns:
            用户状态 dict
        """
        u = self.user(bid, uid)
        ts = self._now()

        # 对话历史（保留最近 N 条）；store_text 关闭时不存正文
        conv = u.setdefault("conversation", [])
        conv.append(
            {
                "text": text[:CONVERSATION_TRUNCATE_LENGTH] if store_text else "",
                "ts": ts,
                "dir": "in",
            }
        )
        del conv[:-MAX_CONVERSATION_HISTORY]

        # 上一次主动联系有没有被接住。这里的窗口比回复率统计宽：晚上发的话对方早上回，
        # 算不算「回了我的主动消息」可以商量，但因此判定 TA 不想理我就太武断了。
        self._settle_pending_reply(u, ts, reply_window_seconds, pending_window_seconds)
        if bid != DEFAULT_BOT_KEY:
            # 兜底桶里的那个人：主动消息从 `default` 发出去，回复却从真实 bid 进来。
            # 不补这一步，那批人的回复率永远是 0、退避永远不触发（见方法注释）。
            fallback = self._fallback_user(uid)
            if fallback is not None and fallback is not u:
                self._settle_pending_reply(
                    fallback, ts, reply_window_seconds, pending_window_seconds
                )

        # 提取话题（store_text 关闭时不提取，避免落盘派生关键词）
        if store_text:
            self._extract_topics(u, text, max_topics, now=ts)
            self._note_cue(u, cue, cue_due, ts)
            self._note_loop(u, loop, loop_due, ts)

        # 作息画像：TA 一般几点说话，决定下次念头想不想往这个人上靠。
        # 记账与判断必须用同一个小时，否则画像与闸门差几个钟头，永远判成「点不对」。
        self._note_rhythm(u, ts, hour_override)

        # 更新基础统计
        u["message_count"] = int(u.get("message_count", 0)) + 1
        u["last_seen"] = ts
        # TA 开口了：“TA 沉默期的主动条数”归零。
        # 这个计数比 `no_reply_streak` 灵敏——后者要等 12 小时回复窗口过了才 +1，
        # 于是「连发好几条没人回」要等好多小时才看得出来（实测 4 天才到 2）。
        u["silent_sent"] = 0
        # 头部而不是尾部：生成侧引用对方最后一句时取的是**前**若干字
        # （`TA最后说的是：「{last[:60]}」`），而这里存的是尾部 500 —— 于是对方发一段
        # 900 字长文时，她引用的是第 400 字起的一段，开头是断的。同一件事在盘上
        # 有两个互相对不上的版本（conversation 存头 200，这里存尾 500），口径统一到头。
        u["last_message"] = text[:MESSAGE_TRUNCATE_LENGTH] if store_text else ""
        if name:
            u["name"] = name
        # 刚说过话：念头**软清零**（v1.27.8，F3）。
        # 原来是直接归零：对方每发一条消息念头就清零，而念头要攒四五个小时才够门槛
        # ——于是「对方每小时发一句」的人永远攒不起来，主动消息一次都不触发。
        # 现在保留三成，让人勤发消息也能被主动找。
        u["urge"] = _num(u.get("urge", 0.0)) * 0.3
        u["urge_at"] = ts

        self.mark_dirty()
        return u

    @staticmethod
    def _note_rhythm(u: Dict[str, Any], ts: float, hour: Optional[int] = None) -> None:
        """把这条消息落在哪个小时计入作息直方图，并对过老的样本折半衰减。"""
        hours = u.get("active_hours")
        if not isinstance(hours, list) or len(hours) != RETENTION_RHYTHM_HOURS:
            hours = [0.0] * RETENTION_RHYTHM_HOURS
        if hour is None:
            try:
                hour = datetime.fromtimestamp(ts).hour
            except (OSError, OverflowError, ValueError, TypeError):
                return
        try:
            hour = int(hour) % RETENTION_RHYTHM_HOURS
        except (TypeError, ValueError):
            return
        try:
            hours[hour] = _num(hours[hour]) + 1.0
        except (TypeError, ValueError):
            hours[hour] = 1.0
        # 直方图元素也可能是脏数据（手改/损坏）：`sum(float(x) ...)` 会抛 ValueError，
        # 而那会一路冒到心跳的宽 except 里 —— 这一条消息的 conversation /
        # message_count / last_seen / 念头结算**全部丢失**，且 mark_dirty 不执行。
        total = sum(_num(x) for x in hours)
        if total > RHYTHM_DECAY_TOTAL:
            # 折半而不是清零：保留大致轮廓，但让最近的样本占主导
            hours = [_num(x) * 0.5 for x in hours]
            total *= 0.5
        u["active_hours"] = [round(_num(x), 3) for x in hours]
        u["rhythm_samples"] = round(total, 2)

    @staticmethod
    def _note_cue(u: Dict[str, Any], cue: Optional[str], cue_due: float, ts: float) -> None:
        """记下对方话里的时间锚点。新锚点覆盖旧锚点（更近的事更值得问）。"""
        if not cue:
            return
        try:
            due = float(cue_due or 0)
        except (TypeError, ValueError):
            due = 0.0
        if due <= 0:
            return
        old_due = _num(u.get("cue_due", 0))
        if old_due > ts and due < old_due:
            # 已经有一个更晚到点的约定在等，不被更早的顶掉
            return
        u["cue"] = str(cue)[:CONVERSATION_TRUNCATE_LENGTH]
        u["cue_due"] = due
        u["cue_at"] = ts
        # 寿命从锚点当场算死，之后无论推迟多少次重试都不动
        u["cue_expire_at"] = due + CUE_LATE_LIMIT
        u["cue_retry_at"] = 0.0
        u["cue_tries"] = 0

    def runtime_metrics(self) -> Dict[str, Any]:
        """读回上次的运行指标。

        `set_runtime_metrics` 一直只写不读，于是**每次重启累计值都从零开始**。
        别的指标无所谓（`last_*` 本来就该是「最后一次」），但 `blocked_reasons`、
        `sent_total`、`rejected_total` 这几个是**累计**语义——重启一次归零，
        「累计拦下 37 次」就变成了「这一轮 37 次」，而界面上写的是累计。
        """
        cur = self.data.get("_runtime")
        return dict(cur) if isinstance(cur, dict) else {}

    def set_runtime_metrics(self, metrics: Dict[str, Any]) -> None:
        """把运行指标写进 state.json 的顶层。

        放盘上而不是只在内存里，是为了**容器侧不用开日志就能判断它跑没跑**：
        直接读 state.json 的 `_runtime` 段就知道最后一次心跳是什么时候、
        结算了多少人、卡在哪一步。这里只存数字与短标签，不含任何正文。
        """
        if not isinstance(metrics, dict):
            return
        current = self.data.get("_runtime")
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update(metrics)
        self.data["_runtime"] = merged
        self.mark_dirty()

    @staticmethod
    def note_promise(u: Dict[str, Any], promise: Optional[str], ts: float) -> None:
        """记下**她自己**在这条里许下的诺（「明天给你看那个」）。

        和未完话题不是一回事：那个是**对方**提了没下文的事，这个是**她自己**
        说过要做的事。记着自己说过的话，是亲密感最强的一环。

        同一个诺不重复记；最多留 PROMISE_SLOTS_MAX 件，超了挤掉最老的一件。
        到点的兑现逻辑在 engine._promise_pick。

        写进来之前先过一道**像不像一件事**的检查（`_promise_looks_sane`）：
        这个字段会原样进下一轮的 prompt（`【你现在知道的】· 你们说好的：…`），
        脏数据比多几条规则更伤——线上真实样本里出现过

            · 你们说好的：了强烈的红光，你猛的惊喜回头望去发现这红光的指向点正是…
            · ？：…

        一条是从半句开始、括号都没闭合的正文碎片，一条是纯符号。它们会让模型
        接着往下演一段根本不存在的剧情。
        """
        text = str(promise or "").strip()
        if not text or not _promise_looks_sane(text):
            return
        text = text[:PROMISE_MAX_CHARS]
        items = promise_entries(u)
        for item in items:
            if str(item.get("about", "")).strip() == text:
                return
        items.append({
            "about": text,
            "at": ts,
            "due": ts + PROMISE_WAIT_SECONDS,
        })
        items.sort(key=lambda i: _num(i.get("at")), reverse=True)
        u["promises"] = items[:PROMISE_SLOTS_MAX]

    def record_spoken(
        self,
        bid: str,
        uid: str,
        text: str,
        store_text: bool = True,
    ) -> Dict[str, Any]:
        """记下她在聊天里自己说出去的那句话（不是插件主动发的）。

        没这一手时插件只看得见对方说的话：「最后一句是谁说的」永远是对方的，
        她也永远不知道自己上一句问了什么——那正好是把未完话题接下去所需的全部信息。
        不改动 last_seen：那是「对方最后一次说话」的时刻。
        """
        u = self.user(bid, uid)
        ts = self._now()
        body = str(text or "").strip()
        conv = u.setdefault("conversation", [])
        conv.append(
            {
                "text": body[:CONVERSATION_TRUNCATE_LENGTH] if store_text else "",
                "ts": ts,
                "dir": "out",
                "src": "chat",
            }
        )
        del conv[:-MAX_CONVERSATION_HISTORY]
        u["last_spoken"] = ts
        u["last_spoken_text"] = body[:120] if store_text else ""
        self.mark_dirty()
        return u

    @staticmethod
    def _note_loop(u: Dict[str, Any], loop: Optional[str], loop_due: float, ts: float) -> None:
        """记下「没说完结果的那件事」。

        以前只记一件，新的直接覆盖旧的：对方一次提了三件事，只有最后一件会被跟下去，
        前面两件从此没人问。现在同时记最多 LOOP_SLOTS_MAX 件（按**每个用户**），
        同一件事不重复记、不往后挪。
        """
        if not loop:
            return
        try:
            due = _num(loop_due)
        except (TypeError, ValueError):
            due = 0.0
        if due <= 0:
            return
        text = str(loop).strip()[:CONVERSATION_TRUNCATE_LENGTH]
        if not text:
            return
        items = loop_entries(u)
        for item in items:
            if str(item.get("about", "")).strip() == text:
                return                      # 同一件事不往后挪
        items.append({
            "about": text,
            "due": due,
            "at": ts,
            "expire_at": due + LOOP_LATE_HOURS * 3600.0,
            "retry_at": 0.0,
            "tries": 0,
        })
        # 满了先挤掉**到期最晚**的，留下最紧要的几件（live_loops 取的就是最早到期的）。
        items.sort(key=lambda i: _num(i.get("due")))
        u["loops"] = items[:LOOP_SLOTS_MAX]
        # 平铺字段写「最新那件」（按录入时间）：老代码与盘上的旧结构都还能认
        newest = max(u["loops"], key=lambda i: _num(i.get("at")))
        u["loop"] = str(newest.get("about", ""))
        u["loop_due"] = _num(newest.get("due"))
        u["loop_at"] = _num(newest.get("at")) or ts
        u["loop_expire_at"] = _num(newest.get("expire_at"))
        u["loop_retry_at"] = 0.0
        u["loop_tries"] = 0

    def record_outgoing(
        self,
        bid: str,
        uid: str,
        text: str,
        msg_type: Optional[str] = None,
        about: str = "",
        count_proactive: bool = True,
        expect_reply: bool = True,
        why: str = "",
        store_text: bool = True,
    ) -> Dict[str, Any]:
        """记录 AI 发出的主动消息。

        Args:
            bid: bot ID
            uid: 用户 ID
            text: 消息文本
            msg_type: 消息类型（用于反重复）
            count_proactive: 是否计入主动联系统计。连发的后续几条传 False，
                一次联系只算一个「回合」，避免回复率分母虚增
            why: 这一条为什么这时候发（写给主人看的依据，不进 prompt）
            expect_reply: 这句话是不是在等对方回。早安/晚安/收场传 False：
                它们本来就不要求回复，进了「等回话」计时器就会把「没回早安」
                记成「被冷落」
            store_text: 是否持久化消息正文（`store_message_text`）。关掉后只留统计
                与短标签，正文不落盘。

                这条路径原来**完全不受开关约束**：`record_incoming` 与
                `record_spoken` 都看这个开关，群聊侧还专门修过一次「只关了一半」，
                唯独主动消息这里没有这个参数 —— 私聊里她的每句主动消息照样写进
                `conversation` 与 `proactive_log`，面板上那个「更隐私」的承诺是空的。

        Returns:
            用户状态 dict
        """
        u = self.user(bid, uid)
        ts = self._now()
        body = str(text or "")[:CONVERSATION_TRUNCATE_LENGTH] if store_text else ""

        # 对话历史
        conv = u.setdefault("conversation", [])
        conv.append({"text": body, "ts": ts, "dir": "out"})
        del conv[:-MAX_CONVERSATION_HISTORY]

        # 主动消息 7 天日志：只记自己主动发的（含连发的后续条），按（bid,uid）隔离。
        # 写入时修剪掉超过 7 天的旧条目与超额条数。
        log = u.setdefault("proactive_log", [])
        log.append({
            "ts": ts,
            "text": body,
            "cat": str(msg_type or ""),
            # 她**为什么**这时候发这条。以前日志里只有正文，主人看到的是一句没头没尾
            # 的话，既没法理解也没法排障——「她为什么这时候想起我」是最该被看见的一件事。
            "why": str(why or "")[:WHY_MAX_CHARS],
            # 这次开口说的**是哪件事**（由头原文）。跨用户撞车就在这一层发生：
            # 同一件事可以用完全不同的措辞发给不同的人，文本相似度只有 0.02~0.06，
            # 靠比措辞是抓不到的。有了它才能判「这件事最近已经派给别人了」。
            "about": str(about or "")[:60],
        })
        cutoff = ts - PROACTIVE_LOG_DAYS * 86400.0
        # 时间戳容错：state.json 被手改或写入损坏时，一条脏 ts 不该把整个发送流程
        # 抛穿（抛出去会作废整轮心跳，而且此时 mark_dirty 还没执行，内存里的
        # conversation 不会被标脏）
        pruned = [e for e in log if isinstance(e, dict) and _safe_ts(e.get("ts")) >= cutoff]
        u["proactive_log"] = pruned[-PROACTIVE_LOG_MAX:]

        # 主动消息统计（仅首条计入冷却与回复率）
        if count_proactive:
            u["last_sent"] = ts
            u["last_targeted"] = ts
            # TA 沉默期里她主动发了几条（每条主动消息都算，不论等不等回）。
            # 它比 `no_reply_streak` 灵敏：后者要等回复窗口过了才 +1，
            # 于是「一直发、他一句不回」要好多小时后才显形。这个实时，
            # 用来做「连着好几条没回应就静下来」。
            try:
                u["silent_sent"] = int(u.get("silent_sent", 0) or 0) + 1
            except (TypeError, ValueError):
                u["silent_sent"] = 1
            if expect_reply:
                u["proactive_sent"] = int(u.get("proactive_sent", 0)) + 1
                u["last_proactive_sent"] = ts
                u["last_proactive_replied"] = False
                # 念头落地，开始等对方接
                desire.after_send(u, ts)
            else:
                # 只是一句不指望回复的话：同样要把念头落地（说完了，别马上又想）
                # 但不进回复率分母——早安没人回不是「被冷落」
                desire.after_send(u, ts, expect_reply=False)
            # 到点的由头不在这里清：那是「因为想起那件事才开口」的那一类
            # （category=="cue"）才该消费的。问候排在候选最前面，一句早安把
            # 「TA 说明天面试」吃掉、那件事再也没人问，就是这么来的。
            # 具体清理由 engine 按 category 显式做，与 loop 同一个写法。

        # 消息类型追踪（反重复）
        if msg_type:
            types = u.setdefault("recent_msg_types", [])
            types.append(msg_type)
            del types[:-RECENT_MSG_TYPES_COUNT]

        # 主动发出去的那句也是「她说的话」，追问判定要能看见它。
        # 正文同样受 store_message_text 约束（本函数的 body 与 record_spoken 都是这个
        # 口径）：关掉「更隐私」之后，主动消息原文不该还落 120 字在盘上。
        u["last_spoken"] = ts
        u["last_spoken_text"] = str(text or "")[:120] if store_text else ""
        # 暂存完整文本：同步写进 AstrBot 会话历史是后台任务，可能还没写完用户就
        # 回了；那时仍要靠 on_llm_request 临时注入一次。写成功后会打 in_history
        # 标记，消费时跳过，避免模型看到两遍。连发（burst）时追加而不是覆盖。
        pending = u.get("pending_proactive_context")
        if isinstance(pending, str):
            # 旧状态里的字符串格式，升级时归一成列表
            pending = (
                [{"text": pending, "ts": ts, "in_history": False}] if pending else []
            )
        elif not isinstance(pending, list):
            pending = []
        # 已经进会话历史的条目不再需要临时注入，顺手丢掉，状态不会越攒越大
        pending = [m for m in pending if isinstance(m, dict) and not m.get("in_history")]
        # 关掉「保存消息正文」时**不能**把原文塞进这里。这条路径原来完全不受开关
        # 约束：`conversation` 与 `proactive_log` 都按 store_text 写空串了，只有这里
        # 还留着完整原文（`str(text or "")` 连截断都没有），而它同样会落进 state.json。
        # 关掉开关的用户以为「她说过的话不落盘」，实际整段原文还躺在盘上。
        # 不追加的代价是：这条主动消息在用户回话前不会被临时注入进 prompt——
        # 但那本来就是「没有正文可注入」，宁可少注入一次，也不能偷偷留正文。
        if store_text:
            pending.append({"text": str(text or ""), "ts": ts, "in_history": False})
        del pending[:-PENDING_PROACTIVE_KEEP]
        u["pending_proactive_context"] = pending

        self.mark_dirty()
        return u

    def anchor_recent_users(self, bid: str, now: float, hours: float = 2.0) -> dict:
        """这个角色最近把哪些由头派给了谁。`{about: [uid, ...]}`。

        跨用户撞车真正发生在**「哪件事」这一层**，不在「怎么说的」那一层：
        同一个「刚忙完个案笔记」可以派给 12 个人，措辞各不相同，文本相似度只有 0.02~0.06，
        靠比措辞是抓不到的（`cross_user_repeat_ratio` 那个阈值就是为这件事准备的，
        但它比错了东西）。
        """
        cutoff = now - float(hours) * 3600.0
        out: dict = {}
        for uid, u in (self.bot(bid).get("users") or {}).items():
            for e in (u.get("proactive_log") or []):
                if not isinstance(e, dict):
                    continue
                if _safe_ts(e.get("ts")) < cutoff:
                    continue
                about = str(e.get("about") or "").strip()
                if not about:
                    continue
                out.setdefault(about, [])
                if uid not in out[about]:
                    out[about].append(uid)
        return out

    def recent_proactive_others(
        self, bid: str, uid: str, now: Optional[float] = None, hours: float = 2.0
    ) -> List[Tuple[str, str]]:
        """这个角色**最近发给别人的**消息，返回 [(发给谁, 正文)]。

        `recent_proactive` 按 (bid,uid) 分开存，所以每个人只跟自己比——跨用户撞车
        结构上就看不见。而素材是共用的一份（Core 的日程是角色级），于是同一句话会
        原样发给一串人。32 条真实记录里 1 小时 41 分内有 12 个人分别收到
        「刚忙完个案笔记」。这条是给那道检查取数用的。
        """
        now = self._now() if now is None else float(now)
        cutoff = now - float(hours) * 3600.0
        rows: List[Tuple[float, str, str]] = []
        for other, u in (self.bot(bid).get("users") or {}).items():
            if str(other) == str(uid):
                continue
            for e in (u.get("proactive_log") or []):
                if not isinstance(e, dict):
                    continue
                ts = _safe_ts(e.get("ts"))
                if ts < cutoff:
                    continue
                text = str(e.get("text", "") or "").strip()
                if text:
                    rows.append((ts, str(other), text))
        rows.sort(key=lambda r: -r[0])          # 最近的排前面
        return [(who, text) for _ts, who, text in rows]

    # ─── v2.1.0：跨群去重 / 反问频控 ────────────────────
    #
    # 为什么账要记在**角色级**：她的生活只有一份（Core 的日程挂在角色上），而群有好
    # 几个。_run_group_topics 每轮只挑一个群，于是同一件事会在一小时里挨个群播一遍
    # ——线上 11-06 16:40 五个群收到的是同一套「刚睡醒/端牛奶/窝沙发」。文本级去重
    # （比措辞）抓不住「换了个说法的同一件事」，所以素材这一层再记一本账。

    def note_life_used(
        self, bid: str, line: str, gid: str, now: float, hours: float = 6.0
    ) -> None:
        """记一条「她自己的生活」素材已经给哪个群用过（角色级，跨群去重）。"""
        key = _norm_life_key(line)
        if not key:
            return
        bot = self.bot(bid)
        used = bot.get("life_used")
        if not isinstance(used, dict):
            used = {}
            bot["life_used"] = used
        cutoff = float(now) - max(0.0, float(hours)) * 3600.0
        rows = [
            r for r in (used.get(key) or [])
            if isinstance(r, dict) and _safe_ts(r.get("ts")) >= cutoff
        ]
        rows.append({"gid": str(gid), "ts": float(now)})
        used[key] = rows[-20:]
        # 顺手丢掉过期的键：角色级字典，跑久了会攒成一本没用的账。
        for other in [k for k in used if k != key]:
            keep = [
                r for r in (used.get(other) or [])
                if isinstance(r, dict) and _safe_ts(r.get("ts")) >= cutoff
            ]
            if keep:
                used[other] = keep
            else:
                used.pop(other, None)
        self.mark_dirty()

    def life_used_elsewhere(
        self, bid: str, line: str, gid: str, now: float, hours: float = 6.0,
        same_group: bool = False,
    ) -> bool:
        """这条素材最近是不是已经用过了。

        `same_group=False`（旧行为）：只算**别的群**用过的——防止同一件事在五个群
        各播一遍。

        `same_group=True`（v2.2.7 / issue #2）：**本群**用过也算。线上「旧票据」
        在同一个群 15:22 说了两遍、15:45 又说两遍，正是旧判据 `gid != 本群`
        把同群重复全部放行的结果。同一个群里，同一件事六小时内说第二遍就是复读。
        """
        key = _norm_life_key(line)
        if not key:
            return False
        used = self.bot(bid).get("life_used")
        if not isinstance(used, dict):
            return False
        cutoff = float(now) - max(0.0, float(hours)) * 3600.0
        for r in (used.get(key) or []):
            if not isinstance(r, dict):
                continue
            if _safe_ts(r.get("ts")) < cutoff:
                continue
            if same_group:
                return True
            if str(r.get("gid", "")) != str(gid):
                return True
        return False

    def anchor_used_for(
        self, bid: str, uid: str, about: str, now: float, hours: float = 12.0
    ) -> bool:
        """这个由头最近是不是已经派给**这个人**了（v2.2.7 / issue #13）。

        旧逻辑只看「是不是发给过别人」（`anchor_recent_users` + `uid not in _used_by`），
        于是同一个人可以连着收到同一件事：「外头才5度 + 上午去超市 + 硬邦邦」
        在 85 秒内说了两遍，第二遍还换了个比喻。
        """
        want = str(about or "").strip()
        if not want:
            return False
        cutoff = float(now) - max(0.0, float(hours)) * 3600.0
        u = (self.bot(bid).get("users") or {}).get(str(uid)) or {}
        for e in (u.get("proactive_log") or []):
            if not isinstance(e, dict):
                continue
            if _safe_ts(e.get("ts")) < cutoff:
                continue
            if str(e.get("about") or "").strip() == want:
                return True
        return False

    def life_lines_used_in_group(
        self, bid: str, gid: str, now: float, hours: float = 6.0
    ) -> List[str]:
        """最近**在这个群**用过的生活素材（归一化后的键）。

        v2.2.7 / issue #2：素材层已经按同群去重剔过一遍，但模型仍可能把「旧票据」
        换个说法再讲一次。这道取数给发送前的质量闸用：生成的正文如果和本群刚用过
        的素材高度重合，就是同一件事又讲了一遍。
        """
        cutoff = float(now) - max(0.0, float(hours)) * 3600.0
        used = self.bot(bid).get("life_used")
        out: List[str] = []
        if not isinstance(used, dict):
            return out
        for key, rows in used.items():
            for r in (rows or []):
                if not isinstance(r, dict):
                    continue
                if _safe_ts(r.get("ts")) < cutoff:
                    continue
                if str(r.get("gid", "")) == str(gid):
                    out.append(str(key))
                    break
        return out

    def recent_group_self_others(
        self, bid: str, gid: str, now: float, hours: float = 2.0
    ) -> List[Tuple[str, str]]:
        """这个角色**最近在别的群**说过的话，返回 [(gid, 正文)]，最近的排前面。

        私聊侧早就有 recent_proactive_others（跨用户撞车），群侧一直没有对应物：
        起话头/破冰只跟**本群**自己的近期发言比，于是同一句话可以在五个群各说一遍。
        """
        cutoff = float(now) - max(0.0, float(hours)) * 3600.0
        rows: List[Tuple[float, str, str]] = []
        for other_gid, g in (self.bot(bid).get("groups") or {}).items():
            if str(other_gid) == str(gid) or not isinstance(g, dict):
                continue
            for e in (g.get("samples") or []):
                if not isinstance(e, dict) or not e.get("self"):
                    continue
                ts = _safe_ts(e.get("ts"))
                if ts < cutoff:
                    continue
                text = str(e.get("text", "") or "").strip()
                if text:
                    rows.append((ts, str(other_gid), text))
        rows.sort(key=lambda r: -r[0])          # 最近的排前面
        return [(who, text) for _ts, who, text in rows]

    def note_send_shape(self, bid: str, text: str, now: float) -> None:
        """记一条她**发出去**的话是不是问句（角色级，跨群/跨会话共用一本账）。

        线上 67 条主动群发里 16 条（24%）以问句收尾，且「你们今天都干嘛呢」这种句式
        跨五个群重复——那不是她在问，是模板在问。
        """
        bot = self.bot(bid)
        rows = bot.get("send_shape")
        if not isinstance(rows, list):
            rows = []
        rows.append({"ts": float(now), "q": 1 if _is_question_text(text) else 0})
        bot["send_shape"] = [r for r in rows if isinstance(r, dict)][-12:]
        self.mark_dirty()

    def question_pressure(self, bid: str, window: int = 3) -> float:
        """最近 window 条里问句占几成。生成侧据此决定「这条不许问」。"""
        rows = [r for r in (self.bot(bid).get("send_shape") or []) if isinstance(r, dict)]
        rows = rows[-max(1, int(window)):]
        if not rows:
            return 0.0
        return sum(1 for r in rows if int(r.get("q", 0) or 0)) / float(len(rows))

    def recent_proactive(
        self, bid: str, uid: str, now: Optional[float] = None, days: float = PROACTIVE_LOG_DAYS
    ) -> List[Dict[str, Any]]:
        """返回某用户最近 days 天内发过的主动消息日志（按时间升序）。

        按（bid,uid）取，天然不串台。用于生成时防重复与主人指令审计。
        """
        now = self._now() if now is None else float(now)
        cutoff = now - float(days) * 86400.0
        u = self.user(bid, uid)
        out: List[Dict[str, Any]] = []
        for e in u.get("proactive_log", []) or []:
            if not isinstance(e, dict):
                continue
            ts = _safe_ts(e.get("ts"))
            if ts >= cutoff:
                out.append(e)
        return out

    def block_send(self, bid: str, uid: str, until: float, reason: str) -> None:
        """把一个发送持续失败（非好友 / 平台不支持主动消息）的用户隔离到 until。

        隔离期内不再选中他，避免每个心跳都去撞同一堆“请添加对方为好友”。
        """
        u = self.user(bid, uid)
        u["send_blocked_until"] = float(until)
        u["send_blocked_reason"] = str(reason or "")[:120]
        self.mark_dirty()

    def is_send_blocked(self, bid: str, uid: str, now: float) -> bool:
        """该用户是否仍在发送隔离期内。"""
        u = self.user(bid, uid)
        try:
            return float(u.get("send_blocked_until", 0) or 0) > now
        except (TypeError, ValueError):
            return False

    # ─── 话题提取 ───────────────────────────────────────

    @staticmethod
    def _extract_topics(u: Dict[str, Any], text: str, max_topics: int = 8,
                        now: float = 0.0) -> None:
        """从用户消息中提取话题关键词。

        策略：
        - 按标点和空格切分为短语段
        - 长度 2~TOPIC_MAX_CHARS 的整段作为候选（过滤停用词）
        - 长度超过 TOPIC_MAX_CHARS 的短语跳过，不做切尾
          （避免"我最近在玩塞尔达传说"被切出"尔达传说"这类垃圾）
        - 英文单词单独提取（3+字母）

        v1.28.7：同时记 `topic_seen_at[话题] = now`（这个话题最后一次出现）。

        为什么：`topics` 从这一版起还是**由头来源**（`anchors.derive_topic_relations`
        拿它问「你上次说的那个X，后来呢」）。由头有时效，而关键词表本身不带时间戳
        ——没有这个字段就分不清「上周聊的塞尔达」和「去年聊的塞尔达」，
         只能一律当新事问。记**最后一次出现**：话题被重新提起就是它又新鲜了。

        上限与 `topics` 对齐（`max_topics`）：这是每用户每轮心跳都会碰的字段，
        不能跟着消息数无界增长。`topic_seen_at` 留一倍余量，被 `topics` 挤出去的
        话题时间戳由末尾那段一起收。
        """
        topics = u.get("topics")
        if not isinstance(topics, list):
            topics = []
            u["topics"] = topics
        else:
            # 列表本身没问题、元素被手改坏的情况（`["塞尔达", 42]`）也要挡住：
            # 下面 `t.lower()` 会抛 AttributeError，而这个函数在每一条用户消息上
            # 都会跑——一个人坏了就等于这个用户此后每条消息都炸。非字符串话题本来
            # 也参与不了匹配，直接丢掉，不要为了保住一个脏值把整条链路搞挂。
            cleaned = [t for t in topics if isinstance(t, str)]
            if len(cleaned) != len(topics):
                topics[:] = cleaned
        seen = u.get("topic_seen_at")
        if not isinstance(seen, dict):
            seen = {}
            u["topic_seen_at"] = seen
        stamp = round(float(now or 0.0), 1)
        segments = re.split(_CHINESE_PUNCT_PATTERN, text)

        for seg in segments:
            if not seg:
                continue

            # 先提取英文单词（3+字母）
            for w in re.findall(_ENGLISH_WORD_PATTERN, seg):
                w_lower = w.lower()
                # 避免重复（检查原词和小写）
                if w_lower not in [t.lower() for t in topics] and w not in topics:
                    topics.insert(0, w)
                # **已经在表里也要刷新时间戳**：`topic_seen_at` 记的是「最后一次
                # 出现」，由头那边按它判 21 天新鲜度。只在 insert 时记的话，
                # 一个天天聊的话题会在第 21 天被判成旧账——而它昨天还在聊。
                if w in topics:
                    seen[w] = stamp

            # 提取中文部分
            chinese = re.sub(_NON_CHINESE_PATTERN, "", seg)
            if len(chinese) < 2 or len(chinese) > TOPIC_MAX_CHARS:
                continue
            if chinese in _STOP_WORDS:
                continue
            if chinese not in topics:
                topics.insert(0, chinese)
            if chinese in topics:
                seen[chinese] = stamp

        # 限制数量
        del topics[max_topics:]
        # `topic_seen_at` 跟着 `topics` 一起收：被挤出上限的话题，时间戳也该走。
        # 不然这个 dict 按「曾经见过的所有话题」单调增长。
        if len(seen) > max_topics * 2:
            live = set(topics)
            for key in [k for k in seen if k not in live]:
                seen.pop(key, None)

    # ─── 统计 ───────────────────────────────────────────

    def reply_rate(self, bid: str, uid: str) -> float:
        """用户对主动消息的回复率（0.0-1.0）。

        Args:
            bid: bot ID
            uid: 用户 ID

        Returns:
            回复率，新用户返回 0.5（中性值）
        """
        u = self.user(bid, uid)
        sent = int(u.get("proactive_sent", 0))
        if sent == 0:
            return 0.5  # 新用户中性值
        return min(1.0, int(u.get("proactive_replied", 0)) / sent)