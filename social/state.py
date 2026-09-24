"""Social 状态持久化。

v1.7.4：
- 用户条目新增「念头」相关字段：urge / interest / 作息画像 / 冷落计数 / 待回复状态 / 由头
- 记录消息时顺带结算上一次主动联系的结局（被接话 or 没人理）
- 作息画像按小时统计并对过老的样本做半衰衰减，让「TA 一般几点玩手机」跟着实际变化
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import desire
from .reasoning import is_question

# ─── 常量定义 ───────────────────────────────────────

STATE_VERSION = 2
DEFAULT_BOT_KEY = "default"

# 对话历史保留条数
MAX_CONVERSATION_HISTORY = 10
# 消息文本截断长度
MESSAGE_TRUNCATE_LENGTH = 500
# 对话历史单条截断长度
CONVERSATION_TRUNCATE_LENGTH = 200
# 最近消息类型追踪数量
RECENT_MSG_TYPES_COUNT = 6
# 每用户主动消息日志保留天数：只记自己主动发出的那几条，按（bid,uid）天然隔离不串台，
# 既用于生成时防重复，也供主人指令审计。超过保留期的条目写入时自动清。
PROACTIVE_LOG_DAYS = 7
# 主动消息日志最多留多少条（防止单个用户日志无限涨）
PROACTIVE_LOG_MAX = 60
# 话题提取允许的最大单段长度（超过此长度不提取）
TOPIC_MAX_CHARS = 6

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
        # 时间源可注入：所有落盘时间戳与调用方的判断必须同源，否则「刚聊过」「超时没回」
        # 这类判断会因为两处各取一次时钟而错位；测试与仿真也需要能推进它。
        self._now = time_source or time.time
        self.load()

    # ─── 基础访问 ───────────────────────────────────────

    def bot(self, bid: str) -> Dict[str, Any]:
        """获取或创建 bot 状态。"""
        return self.data.setdefault("bots", {}).setdefault(
            bid,
            {"users": {}, "last_global_send": 0.0},
        )

    def user(self, bid: str, uid: str) -> Dict[str, Any]:
        """获取或创建用户状态。"""
        return (
            self.bot(bid)
            .setdefault("users", {})
            .setdefault(uid, self._default_user())
        )

    @staticmethod
    def _default_user() -> Dict[str, Any]:
        """返回默认用户状态结构。"""
        return {
            "name": "",
            "umo": "",
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
            # v1.7.4 由头：对方话里的时间锚点（「明天面试」）与它的到期时间
            "cue": "",
            "cue_due": 0.0,
            "cue_at": 0.0,
            # v1.8.0 她在主链路里自己说的话（after_message_sent 记账）：
            # 看不到这些时，插件以为她每说完一句对方都会回，未完话题也就无从判断。
            "last_spoken": 0.0,
            "last_spoken_text": "",
            "last_spoken_question": False,
            # 未完话题：thread_for 记下「为哪一次断点追过」，一段沉默只追一次
            "thread_for": 0.0,
            "thread_at": 0.0,
            # 隔一阵回访那件事：loop 是没说完的事，loop_due 是大概什么时候再问
            "loop": "",
            "loop_due": 0.0,
            "loop_at": 0.0,
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
            # v2: 回复时间统计
            "avg_reply_seconds": 0.0,
            "reply_samples": 0,
            # v2: 提取的话题
            "topics": [],
            # v2: 最近消息类型（反重复）
            "recent_msg_types": [],
            # 主动消息 7 天日志：只记自己主动发出的那几条（{ts, text, cat}），
            # 按（bid,uid）隔离不串台，写入时自动修剪过期条目
            "proactive_log": [],
            # 发送失败（非好友/不支持主动消息等）隔离：隔离到什么时候、为什么
            "send_blocked_until": 0.0,
            "send_blocked_reason": "",
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
            "flow_ignored": 0,       # 连续插话没人接的计数
            "flow_hour_bucket": 0,   # 小时级插话计数的时间桶（epoch 小时）
            "flow_hour_count": 0,
            "last_flow_reply_text": "",  # bot 上一句心流接的话，给生成侧防重复
            "icebreak_day": "",      # 最近破冰的日期（按角色时钟）
            "icebreak_count_day": 0,
            "icebreak_at": 0.0,
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
    ) -> Dict[str, Any]:
        """记下一条群消息：刷新 last_seen/umo，并（需要时）把群友发言存进参考库。

        is_bot=True 时不进参考库（不学自己），但会刷 last_seen。参考库只存真人发言。
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
        # 收到群消息说明这个群现在能通：清掉之前的发送隔离
        if float(g.get("blocked_until", 0) or 0) > 0:
            g["blocked_until"] = 0.0
            g["blocked_reason"] = ""
        if is_bot:
            # bot 自己在群里说的话也存进 samples（标 self）：这样生成上下文里能看到自己
            # 刚说了什么，接话不至于自说自话/重复。风格参考库会滤掉 self，不学自己。
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
        # bot 自己的话也占额度，缓冲放宽一点，别让几句自己的话把群友样本挤没了
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

    def note_flow_reply(self, bid: str, gid: str, now: float, text: str) -> None:
        """记一次心流接话：窗口计数 +1、“连续自说自话”计数 +1、刷小时桶、记下接的话防重复。

        flow_ignored 在这里递增：统计“自上次真正被搞话（被@回）以来连着插了几句”。
        插够 flow_ignored_exit 次都没人正式搞你，就安静下来，别自说自话。不延长关注窗口。
        """
        g = self.group(bid, gid)
        g["flow_replies"] = int(g.get("flow_replies", 0) or 0) + 1
        g["flow_ignored"] = int(g.get("flow_ignored", 0) or 0) + 1
        g["flow_last_reply_at"] = now
        g["last_bot_spoke"] = now
        g["last_flow_reply_text"] = str(text or "")[:120]
        # 自己插的话也进近期发言缓冲，生成下一句时看得到
        self._append_group_sample(g, text, now, len(g.get("samples") or []) or 30, self_flag=True)
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

    def record_group_self_text(
        self, bid: str, gid: str, text: str, now: float, sample_cap: int = 30
    ) -> None:
        """记下 bot 自己在群里补发的一句（连发续句）：只进近期发言缓冲供下一句参考，
        不动 last_seen/msg_count、不过心流闸、不计额。"""
        g = self.group(bid, gid)
        g["last_bot_spoke"] = now
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

    def load(self) -> None:
        """从磁盘加载状态。"""
        try:
            if not os.path.exists(self.path):
                return

            with open(self.path, encoding="utf-8") as f:
                x = json.load(f)

            if isinstance(x, dict) and isinstance(x.get("bots"), dict):
                self.data = x
                self._migrate()
                self._dirty = False
        except json.JSONDecodeError:
            # JSON 损坏，备份旧文件并重新开始
            self._backup_corrupted_file()
            self.data = {"version": STATE_VERSION, "bots": {}}
            self._dirty = True
        except OSError:
            # 读取失败，使用空状态
            self.data = {"version": STATE_VERSION, "bots": {}}
            self._dirty = False
        except Exception:
            # 其他未知错误
            self.data = {"version": STATE_VERSION, "bots": {}}
            self._dirty = False

    def _backup_corrupted_file(self) -> None:
        """备份损坏的状态文件。"""
        try:
            backup_path = f"{self.path}.corrupted.{int(time.time())}"
            if os.path.exists(self.path):
                os.rename(self.path, backup_path)
        except OSError:
            pass

    # v1.7.7 的「悬空跟进」在 v1.8.0 换成了不分谁说话的 thread_*，旧键留着只会让人
    # 以为还有人在读它。
    _RETIRED_USER_KEYS = ("followup_for", "followup_at")

    def _migrate(self) -> None:
        """确保所有已存在的用户都有当前版本字段，并清掉已经没人读的旧键。"""
        try:
            for bot in self.data.get("bots", {}).values():
                if not isinstance(bot, dict):
                    continue
                for uid, u in bot.get("users", {}).items():
                    if not isinstance(u, dict):
                        continue
                    for k, v in self._default_user().items():
                        u.setdefault(k, v)
                    for k in self._RETIRED_USER_KEYS:
                        if k in u:
                            u.pop(k, None)
                            self._dirty = True
                # v1.11.0 群会话：补齐新键（加法式，不动已有 users）
                groups = bot.get("groups")
                if isinstance(groups, dict):
                    for g in groups.values():
                        if not isinstance(g, dict):
                            continue
                        for k, v in self._default_group().items():
                            g.setdefault(k, v)
                # v1.12.0：清掉旧版历史导入误进 users 的群会话幽灵。旧版按 updated_at
                # 取前 300 个会话再滤群，`GroupMessage` 类型不在识别表里被判 unknown
                # 放行，群会话被当「人」导入、拿群 umo 去发主动消息。群消息在观察链路
                # 里从不建 per-user 记录，所以 users 里 umo 是群会话的一定是幽灵。
                users = bot.get("users")
                if isinstance(users, dict):
                    for uid, u in list(users.items()):
                        if not isinstance(u, dict):
                            continue
                        segs = str(u.get("umo", "") or "").lower().split(":")
                        mtype = segs[1] if len(segs) >= 3 else ""
                        if mtype in ("group", "g", "groupmessage", "guild"):
                            users.pop(uid)
                            self._dirty = True
            self.data["_ghosts_cleaned"] = True
        except Exception:
            pass

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
        if real_bid not in bots or not bots[real_bid].get("users"):
            # 目标 bid 是空的，直接搬过去
            bots[real_bid] = default_bot
            del bots[DEFAULT_BOT_KEY]
            self.data["_default_migrated"] = True
            self._dirty = True
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
            self._dirty = True

    # ─── 过期数据清理 ───────────────────────────────────

    def settle_pending(self, bid: str, uid: str, now: float, window_seconds: float) -> str:
        """结算上一次主动联系的结局：窗口内没等到回复就算没人理。

        对方后来发了消息的情况在 record_incoming 里已经处理掉了，这里负责的是
        「发出去之后 TA 再也没说话」这种永远不会触发记录回调的情况。

        Returns:
            结算后的状态：waiting / ignored，已结算过的返回当前值
        """
        u = self.user(bid, uid)
        pending = float(u.get("pending_since", 0) or 0)
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
                for key in RETENTION_HEAVY_TEXT_FIELDS:
                    u[key] = ""
                u["active_hours"] = [0.0] * RETENTION_RHYTHM_HOURS
                u["rhythm_samples"] = 0
                u["cue_due"] = 0.0
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
        """标记状态已变更但暂不写入磁盘。"""
        self._dirty = True

    def save(self) -> None:
        """立即写入磁盘（原子操作）。"""
        try:
            self._write()
            self._dirty = False
        except Exception as e:
            raise RuntimeError(f"保存状态失败: {e}") from e

    def flush(self) -> bool:
        """如果有变更则写入磁盘。适合周期性调用。

        Returns:
            True 表示执行了写入，False 表示没有变更
        """
        if self._dirty:
            try:
                self._write()
                self._dirty = False
                return True
            except Exception:
                return False
        return False

    def _write(self) -> None:
        """原子写入状态到磁盘。

        紧凑格式：这份文件每次保存都要全量序列化，indent=2 在用户数上千时
        会把单次 dumps 拖到上百毫秒，而它跑在事件循环上。不加缩进后体积约降四分
        之一、序列化快 3 倍。格式变更不影响读取（json.load 不关心缩进）。
        """
        dir_path = os.path.dirname(self.path)
        os.makedirs(dir_path, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            prefix="social_", suffix=".tmp", dir=dir_path
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        finally:
            # 清理临时文件
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ─── 消息记录 ───────────────────────────────────────

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
        pending_window = pending_window_seconds or reply_window_seconds
        pending = float(u.get("pending_since", 0) or 0)
        if pending > 0 and u.get("pending_result") == "waiting":
            if ts - pending <= pending_window:
                desire.after_reply(u, ts)
            else:
                desire.after_ignored(u, ts)

        # 回复检测：只有在回复窗口内才算
        last_pro = float(u.get("last_proactive_sent", 0))
        if last_pro > 0 and not u.get("last_proactive_replied", True):
            if ts - last_pro <= reply_window_seconds:
                u["last_proactive_replied"] = True
                u["proactive_replied"] = int(u.get("proactive_replied", 0)) + 1
                # 滚动平均回复时间
                reply_time = ts - last_pro
                samples = int(u.get("reply_samples", 0))
                old_avg = float(u.get("avg_reply_seconds", 0))
                if samples == 0:
                    u["avg_reply_seconds"] = reply_time
                else:
                    u["avg_reply_seconds"] = (old_avg * samples + reply_time) / (samples + 1)
                u["reply_samples"] = samples + 1
            # else: 超过窗口了，不算回复，但 last_proactive_replied 保持 False
            # （下一次主动消息会重置它）

        # 提取话题（store_text 关闭时不提取，避免落盘派生关键词）
        if store_text:
            self._extract_topics(u, text, max_topics)
            self._note_cue(u, cue, cue_due, ts)
            self._note_loop(u, loop, loop_due, ts)

        # 作息画像：TA 一般几点说话，决定下次念头想不想往这个人上靠。
        # 记账与判断必须用同一个小时，否则画像与闸门差几个钟头，永远判成「点不对」。
        self._note_rhythm(u, ts, hour_override)

        # 更新基础统计
        u["message_count"] = int(u.get("message_count", 0)) + 1
        u["last_seen"] = ts
        u["last_message"] = text[-MESSAGE_TRUNCATE_LENGTH:] if store_text else ""
        if name:
            u["name"] = name
        # 刚说过话，此刻没有「再主动找 TA」的念头
        u["urge"] = 0.0
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
            hours[hour] = float(hours[hour] or 0) + 1.0
        except (TypeError, ValueError):
            hours[hour] = 1.0
        total = sum(float(x or 0) for x in hours)
        if total > RHYTHM_DECAY_TOTAL:
            # 折半而不是清零：保留大致轮廓，但让最近的样本占主导
            hours = [float(x or 0) * 0.5 for x in hours]
            total *= 0.5
        u["active_hours"] = [round(x, 3) for x in hours]
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
        old_due = float(u.get("cue_due", 0) or 0)
        if old_due > ts and due < old_due:
            # 已经有一个更晚到点的约定在等，不被更早的顶掉
            return
        u["cue"] = str(cue)[:CONVERSATION_TRUNCATE_LENGTH]
        u["cue_due"] = due
        u["cue_at"] = ts

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
        u["last_spoken_question"] = is_question(body)
        self.mark_dirty()
        return u

    @staticmethod
    def _note_loop(u: Dict[str, Any], loop: Optional[str], loop_due: float, ts: float) -> None:
        """记下「没说完结果的那件事」。同一件事不往后挪，新的事才覆盖旧的。"""
        if not loop:
            return
        try:
            due = float(loop_due or 0)
        except (TypeError, ValueError):
            due = 0.0
        if due <= 0:
            return
        text = str(loop).strip()
        if text and text == str(u.get("loop", "") or ""):
            return
        u["loop"] = text[:CONVERSATION_TRUNCATE_LENGTH]
        u["loop_due"] = due
        u["loop_at"] = ts

    def record_outgoing(
        self,
        bid: str,
        uid: str,
        text: str,
        msg_type: Optional[str] = None,
        count_proactive: bool = True,
    ) -> Dict[str, Any]:
        """记录 AI 发出的主动消息。

        Args:
            bid: bot ID
            uid: 用户 ID
            text: 消息文本
            msg_type: 消息类型（用于反重复）
            count_proactive: 是否计入主动联系统计。连发的后续几条传 False，
                一次联系只算一个「回合」，避免回复率分母虚增

        Returns:
            用户状态 dict
        """
        u = self.user(bid, uid)
        ts = self._now()

        # 对话历史
        conv = u.setdefault("conversation", [])
        conv.append({"text": text[:CONVERSATION_TRUNCATE_LENGTH], "ts": ts, "dir": "out"})
        del conv[:-MAX_CONVERSATION_HISTORY]

        # 主动消息 7 天日志：只记自己主动发的（含连发的后续条），按（bid,uid）隔离。
        # 写入时修剪掉超过 7 天的旧条目与超额条数。
        log = u.setdefault("proactive_log", [])
        log.append({"ts": ts, "text": str(text or "")[:CONVERSATION_TRUNCATE_LENGTH], "cat": str(msg_type or "")})
        cutoff = ts - PROACTIVE_LOG_DAYS * 86400.0
        pruned = [e for e in log if isinstance(e, dict) and float(e.get("ts", 0) or 0) >= cutoff]
        u["proactive_log"] = pruned[-PROACTIVE_LOG_MAX:]

        # 主动消息统计（仅首条计入冷却与回复率）
        if count_proactive:
            u["proactive_sent"] = int(u.get("proactive_sent", 0)) + 1
            u["last_proactive_sent"] = ts
            u["last_proactive_replied"] = False
            u["last_sent"] = ts
            u["last_targeted"] = ts
            # 念头落地，开始等对方接
            desire.after_send(u, ts)
            if u.get("cue_due") and float(u["cue_due"]) <= ts:
                # 这个由头已经说过了，别再拿它当理由
                u["cue"] = ""
                u["cue_due"] = 0.0

        # 消息类型追踪（反重复）
        if msg_type:
            types = u.setdefault("recent_msg_types", [])
            types.append(msg_type)
            del types[:-RECENT_MSG_TYPES_COUNT]

        # 主动发出去的那句也是「她说的话」，追问判定要能看见它
        u["last_spoken"] = ts
        u["last_spoken_text"] = str(text or "")[:120]
        u["last_spoken_question"] = is_question(str(text or ""))
        # 保存完整文本，等用户回复时补进 LLM 上下文（主动消息走 context.send_message，
        # 不经过 respond 阶段，不会自动进会话历史，用户回复时 AI 看不到自己刚说了什么）。
        # on_llm_request 里消费掉这个字段后清空，避免每条消息都重复注入。
        # 连发（burst）时追加而不是覆盖：两条都得让 AI 看得见。
        existing = str(u.get("pending_proactive_context", "") or "")
        if existing and len(existing) < 500:
            u["pending_proactive_context"] = existing + "\n" + str(text or "")[:300]
        else:
            u["pending_proactive_context"] = str(text or "")[:500]

        self.mark_dirty()
        return u

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
            try:
                ts = float(e.get("ts", 0) or 0)
            except (TypeError, ValueError):
                continue
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
    def _extract_topics(u: Dict[str, Any], text: str, max_topics: int = 8) -> None:
        """从用户消息中提取话题关键词。

        策略：
        - 按标点和空格切分为短语段
        - 长度 2~TOPIC_MAX_CHARS 的整段作为候选（过滤停用词）
        - 长度超过 TOPIC_MAX_CHARS 的短语跳过，不做切尾
          （避免"我最近在玩塞尔达传说"被切出"尔达传说"这类垃圾）
        - 英文单词单独提取（3+ 字母）
        """
        topics = u.setdefault("topics", [])
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

            # 提取中文部分
            chinese = re.sub(_NON_CHINESE_PATTERN, "", seg)
            if len(chinese) < 2 or len(chinese) > TOPIC_MAX_CHARS:
                continue
            if chinese in _STOP_WORDS:
                continue
            if chinese not in topics:
                topics.insert(0, chinese)

        # 限制数量
        del topics[max_topics:]

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