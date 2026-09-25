"""社交引擎：念头积累、选谁、该不该说、说完之后。

v1.7.4：从定时器改成念头驱动
- 不再「每几分钟掷一次骰子，掷中了就发」：每个用户身上有一个随时间积累的 urge，
  越在意的人、对方越可能醒着的时段，攒得越快；刚聊过直接清零
- 攒满也不等于就发：先过规则闸门（热聊/冷脸/作息/睡意），再让模型自己判断要不要说
- 发送后的体验被记下来：被接话会抬高在意度，没人理会压低并抬高下次门槛
- 冷却项降为护栏，不再是节奏的唯一来源
"""

from __future__ import annotations

import asyncio
import json
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

from . import __version__, desire
from . import history_ingest
from .clock import city_now
from .config import SocialConfig
from .core_bridge import CoreBridge
from .signals import FILE_NAME as SIGNALS_FILE_NAME, SignalsWriter
from .generator import MessageGenerator
from .persona import resolve_persona
from .reasoning import (
    extract_cue,
    generate_reason,
    greeting_due,
    greeting_window_kind,
    greet_meta,
    greet_reason,
    live_cue,
    sleep_signal,
    time_slot,
)
from .state import SocialState
from . import groupflow
from .threads import (
    closer_meta,
    closer_reason,
    extract_open_loop,
    live_loop,
    loop_meta,
    loop_reason,
    thread_meta,
    thread_reason,
)

# ─── 常量定义 ───────────────────────────────────────

# 时间常量（秒）
MIN_INTERVAL_SECONDS = 480       # 检查间隔最小值（调整到8分钟，避免频繁轮询）
MAX_INTERVAL_SECONDS = 900       # 检查间隔最大值（调整到15分钟）
FLUSH_INTERVAL_SECONDS = 30      # 状态刷盘间隔
HOT_CHAT_THRESHOLD = 900         # 十分钟内还在聊，不插话
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

# 错误重试
SEND_RETRY_COUNT = 2
SEND_RETRY_DELAY = 8.0  # 重试间隔改为8秒，避免快速刷屏

# 发送持续失败（非好友 / 平台不支持主动消息 / 会话失效）后，隔离这个人多久不再试
SEND_BLOCK_HOURS = 24
# 发送失败文案里命中这些关键词，就当作“对这个人发不了”而非一次性抵达，隔离他
# 这些标记命中才判定「对这个目标根本发不了」（非好友/会话失效/平台不支持），
# 错误把它们隔离 24h。匹配范围必须很窄：风控、限流、网络抖动的临时失败里
# 也可能含 "blocked"/"friend"/"deleted" 字样，宽匹配会把健康用户误隔离一整天，
# 表现就是「几百小时才发一条」。
_UNREACHABLE_ERROR_MARKERS = (
    "请添加对方为好友", "添加对方为好友", "不是好友", "非好友", "对方不在你的好友列表",
    "好友关系不存在", "not friend", "add friend", "unfriend", "friendship",
    "未找到匹配会话平台", "不支持主动", "qq_official",
    "会话已失效", "session expired", "session not found",
    "已被删除", "已删除好友", "删除好友", "拉黑", "已拉黑",
)

# 回访那件事之前，这场话至少凉下来多久：还在你来我往时问「后来呢」太急
LOOP_QUIET_GAP_SECONDS = 1800.0

# 早晚问候只对最近有来往的人发：几天没说话的人每天收一句早安，那不是问候是群发
GREET_FRESH_SECONDS = 4 * 86400.0

# 连发：后续每条的补发间隔（秒）
BURST_DELAY_MIN = 60
BURST_DELAY_MAX = 180
# 群聊心流连发：同一个念头拆成几句快速补发（秒）——群里节奏快，不像私聊隔几分钟
GROUP_BURST_DELAY_MIN = 3
GROUP_BURST_DELAY_MAX = 9

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
        self._time = time_source or time.time

        if state_path is None:
            state_path = os.path.abspath(
                "data/plugin_data/astrbot_plugin_autonomous_social/state.json"
            )
        self.state = SocialState(state_path, time_source=self._time)
        self.core = CoreBridge(self.cfg.mode, data_dir=data_dir)
        # 写回给 Humanoid Core 的信号（它替她主动开过口、被谁冷落了几次）。写自己目录下的
        # 单独文件，两边不会并发写同一个文件。
        self.signals = SignalsWriter(
            os.path.join(os.path.dirname(state_path), SIGNALS_FILE_NAME), time_source=self._time
        )
        self.generator = MessageGenerator(context, self.cfg)
        self._data_dir = data_dir
        self.running = False
        self._last_flush = 0.0
        self._last_prune = 0.0
        self._last_seed = 0.0
        self._last_llm_error = ""
        # 最近一次播种/导入的结果（仅用于状态展示，不落盘）
        self._last_import_note = ""
        # bid -> 本周期实际用到的人格名（仅用于状态展示，不落盘）
        self._last_persona: Dict[str, str] = {}
        # bid -> 她所在城市相对本机的分钟偏移（从 Core 契约里拿）。没有契约时不设，
        # 时段判断退回本机时钟：宁可用错一个时钟，也不能把她的城市当成 UTC。
        self._city_offset: Dict[str, int] = {}
        self._burst_tasks: set = set()
        # 群心流接话的后台任务（观察链路不阻塞）
        self._bg_tasks: set = set()
        # 正在生成心流接话的群 (bid, gid)：一个群同一时刻只允许一条接话在途。
        # 闸门计数要等接话发出后才更新（中间隔着几秒 LLM 生成），没有这道锁时
        # 活跃群里连来几条消息会各派一个任务、全部抢在计数更新前过闸 → 同时发好
        # 几句，把 min_gap/每窗上限/每小时上限一起击穿。
        self._flow_inflight: Set[Tuple[str, str]] = set()

    # ─── 生命周期 ───────────────────────────────────────

    def start(self) -> None:
        """启动引擎。"""
        self.running = True
        # 先把信号文件放下去：Core 那边靠它判断「社交层活着」，不能等到第一次真的发消息才写。
        if self.cfg.mode != "standalone":
            try:
                self.signals.beat()
            except Exception as exc:  # 写不进去不影响启动
                logger.warning(f"[autonomous_social] 信号文件初始化失败: {exc}")
        # 启动即播种：读回 AstrBot 会话库里的历史私聊对象 + 应用播种名单，
        # 不用等装好后再聊一句才开始认识人
        self._seed_tick(force=True)
        logger.info(f"[autonomous_social] 引擎启动，模式: {self.cfg.mode}")

    def stop(self) -> None:
        """停止引擎：取消未发的连发补句，确保状态落盘。"""
        self.running = False
        for task in list(self._burst_tasks):
            task.cancel()
        self._burst_tasks.clear()
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
        self._flow_inflight.clear()
        try:
            self.state.save()
            logger.info("[autonomous_social] 引擎已停止，状态已保存")
        except Exception as e:
            logger.error(f"[autonomous_social] 停止时保存状态失败: {e}")

    def log(self, message: str) -> None:
        """调试日志输出。"""
        if self.cfg.debug:
            logger.info(f"[autonomous_social] {message}")

    # ─── 消息观察 ───────────────────────────────────────

    async def observe(self, event: Any) -> None:
        """记录用户消息。只标记脏，不立即刷盘。

        Args:
            event: AstrBot 消息事件对象
        """
        if not self.cfg.enabled:
            return

        # 获取消息文本
        msg = ""
        try:
            msg = (getattr(event, "message_str", "") or "").strip()
        except Exception:
            pass

        if not msg or msg.startswith("/"):
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

        # 获取用户 ID
        uid = self._get_sender_id(event)
        if not uid:
            return

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
        if umo and u.get("umo") != umo:
            u["umo"] = umo
            self.state.mark_dirty()
        # 对方又发消息了：说明这个会话现在能通，清掉之前的发送隔离
        if float(u.get("send_blocked_until", 0) or 0) > 0:
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
                        bid, gid, umo, "", "", text, True, now, store_text=False
                    )
                    self.state.open_flow(bid, gid, now, self.cfg.flow_window_minutes * 60.0)
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
                }
            )
        if not session_history:
            return ledger[-limit:]

        merged = list(session_history)
        seen = {
            (str(item.get("dir", "") or ""), str(item.get("text", "") or ""))
            for item in merged
            if isinstance(item, dict)
        }
        for item in ledger:
            key = (item["dir"], item["text"])
            if key in seen:
                continue
            merged.append(item)
            seen.add(key)
        return merged[-limit:]

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
                text = str(content or "").strip()
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
        """这条群消息是不是 @ 了 bot。用作心流「有人接我插的话」的硬信号。

        扫消息链里的 At 段（qq/target/user_id 字段 ≡ bot 自己的 id）。
        """
        if not bid:
            return False
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
            for comp in chain:
                for attr in ("qq", "target", "user_id"):
                    val = getattr(comp, attr, None)
                    if val is not None and str(val) == str(bid):
                        return True
        except Exception:
            pass
        return False

    # ─── 群聊心流（v1.11.0） ───────────────────

    async def _observe_group(self, event: Any, msg: str) -> None:
        """观察一条群消息：进参考库、刷 last_seen；如果心流窗口开着且值得接，派一个后台任务去接。

        心流、破冰、参考库只要有一个开着就需要 last_seen/umo，所以只要不是全关就记录。
        """
        if not (self.cfg.group_flow_enabled or self.cfg.group_icebreak_enabled or self.cfg.group_ref_lib_enabled):
            return
        bid = self._get_bot_id(event)
        gid = self._get_group_id(event)
        if not bid or not gid:
            return
        uid = self._get_sender_id(event)
        # bot 自己的群发言不进参考库（不学自己）；主链路回复另走 note_spoken 开窗
        is_bot = bool(uid) and uid == bid
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
            bid, gid, umo, group_name, sender_name, msg, is_bot, now,
            store_text=store_text, sample_cap=self.cfg.group_ref_sample_size,
        )
        # 有人 @ 了 bot 且心流窗口开着：算「接住了我刚插的话」，把连着插没人理的计数清零，
        # 允许继续接。不是 @ 的普通群聊不算接话（避免一有人说话就当受欢迎继续无脑插）。
        if (
            not is_bot
            and self._mentions_bot(event, bid)
            and float(g.get("flow_open_until", 0) or 0) > now
        ):
            self.state.note_flow_pickup(bid, gid)
        # 心流：窗口开着、不是 bot 自己发的、预筛过了，才去试着接一句
        if not self.cfg.group_flow_enabled or is_bot:
            return
        if float(g.get("flow_open_until", 0) or 0) <= now:
            return
        if not groupflow.flow_prefilter(msg, is_command=False):
            return
        ok, why = groupflow.flow_should_consider(
            g, now,
            max_replies=self.cfg.flow_max_replies_per_window,
            min_gap_seconds=self.cfg.flow_min_gap_seconds,
            hourly_cap=self.cfg.flow_hourly_cap,
            hour_count=self.state.flow_hour_count(g, now),
            ignored_exit=self.cfg.flow_ignored_exit,
        )
        if not ok:
            self.log(f"群 {gid} 心流本轮不接：{why}")
            return
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

    async def _flow_reply(self, bid: str, gid: str, trigger_ts: float) -> None:
        """包一层 in-flight 释放：无论生成/发送成不成功、中途抛不抛，都把
        该群的在途标记拿掉，让后续消息能重新触发（此时计数/min_gap 已落账）。"""
        try:
            await self._flow_reply_inner(bid, gid, trigger_ts)
        finally:
            self._flow_inflight.discard((bid, gid))

    async def _flow_reply_inner(self, bid: str, gid: str, trigger_ts: float) -> None:
        """在关注窗口内无 @ 接一句。生成前重校闸（状态可能变了），模型可选择不接。"""
        if not self.running or not self.cfg.enabled or not self.cfg.group_flow_enabled:
            return
        now = self._time()
        g = self.state.group(bid, gid)
        umo = str(g.get("umo", "") or "")
        if not umo:
            return
        ok, why = groupflow.flow_should_consider(
            g, now,
            max_replies=self.cfg.flow_max_replies_per_window,
            min_gap_seconds=self.cfg.flow_min_gap_seconds,
            hourly_cap=self.cfg.flow_hourly_cap,
            hour_count=self.state.flow_hour_count(g, now),
            ignored_exit=self.cfg.flow_ignored_exit,
        )
        if not ok:
            return
        group_ctx = self._build_group_ctx(bid, gid, g)
        try:
            _persona_name, persona_prompt = await self._persona(umo, bid)
        except Exception as e:
            self.log(f"群 {gid} 解析人格失败: {e}")
            persona_prompt = ""
        try:
            parts = await self.generator.group_message(
                umo, "flow", group_ctx, persona_prompt,
                at=now, clock_offset=self._clock_offset_for(bid),
            )
        except Exception as e:
            logger.warning(f"[autonomous_social] 群心流生成失败: {e}")
            return
        if not parts:
            self.log(f"群 {gid} 心流：模型选择不接或生成为空")
            return
        text = parts[0]
        ok_send, err = await self._send_with_retry(umo, text)
        if not ok_send:
            if self._is_unreachable_error(err):
                self.state.block_group(bid, gid, self._time() + SEND_BLOCK_HOURS * 3600.0, err)
                logger.warning(f"[autonomous_social] 群 {gid}（bid={bid}）发送失败，隔离 {SEND_BLOCK_HOURS}h：{err}")
            else:
                self.log(f"群 {gid} 心流发送失败：{err}")
            return
        sent_ts = self._time()
        self.state.note_flow_reply(bid, gid, sent_ts, text)
        self.state.save()
        self.log(f"群 {gid} 心流接话→ {text}")
        # 同一句拆成了多段：剩下的快速补发。算同一次插话（不再各自计额/过闸）。
        if len(parts) > 1:
            self._schedule_group_burst(bid, gid, umo, parts[1:], sent_ts)

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
        return {
            "recent": recent,
            "style_ref": style_ref,
            "last_flow_text": str(g.get("last_flow_reply_text", "") or ""),
            "group_name": str(g.get("name", "") or ""),
        }

    async def _run_icebreaks(self, bots: List[str], now: float) -> None:
        """每个角色一轮最多给一个冷得最久的群破冰（别一口气把好几个群都点一遍）。"""
        for bid in bots:
            due = self._icebreak_pick(bid, now)
            if not due:
                continue
            due.sort(key=lambda item: float(item[1].get("last_seen", 0) or 0))
            gid, g = due[0]
            await self._icebreak(bid, gid, g, now)

    def _icebreak_pick(self, bid: str, now: float) -> List[Tuple[str, Dict[str, Any]]]:
        """这个角色名下，哪些群冷场到该破冰了。返回 [(gid, g)]。"""
        if not self.cfg.group_icebreak_enabled:
            return []
        dt = self._moment_of(bid, now)
        is_quiet = self.cfg.in_quiet_hours(dt.hour)
        today = dt.strftime("%Y-%m-%d")
        out: List[Tuple[str, Dict[str, Any]]] = []
        for gid, g in self.state.groups(bid).items():
            if not g.get("umo"):
                continue
            if groupflow.icebreak_due(
                g, now,
                idle_hours=self.cfg.group_idle_hours,
                daily_cap=self.cfg.icebreak_daily_cap,
                stale_days=self.cfg.group_stale_days,
                today=today,
                is_quiet=is_quiet,
            ):
                out.append((gid, g))
        return out

    async def _icebreak(self, bid: str, gid: str, g: Dict[str, Any], now: float) -> bool:
        """向一个冷群抛一句破冰。发出返回 True（并开心流窗口）。"""
        umo = str(g.get("umo", "") or "")
        if not umo:
            return False
        group_ctx = self._build_group_ctx(bid, gid, g)
        group_ctx["reason"] = groupflow.icebreak_reason()
        try:
            _persona_name, persona_prompt = await self._persona(umo, bid)
        except Exception as e:
            self.log(f"群 {gid} 解析人格失败: {e}")
            persona_prompt = ""
        try:
            parts = await self.generator.group_message(
                umo, "icebreak", group_ctx, persona_prompt,
                at=now, clock_offset=self._clock_offset_for(bid),
            )
        except Exception as e:
            logger.warning(f"[autonomous_social] 群破冰生成失败: {e}")
            return False
        if not parts:
            return False
        text = parts[0]
        ok_send, err = await self._send_with_retry(umo, text)
        if not ok_send:
            if self._is_unreachable_error(err):
                self.state.block_group(bid, gid, self._time() + SEND_BLOCK_HOURS * 3600.0, err)
                logger.warning(f"[autonomous_social] 群 {gid}（bid={bid}）破冰发送失败，隔离 {SEND_BLOCK_HOURS}h：{err}")
            else:
                self.log(f"群 {gid} 破冰发送失败：{err}")
            return False
        sent_ts = self._time()
        self.state.note_icebreak(bid, gid, sent_ts, self._moment_of(bid, sent_ts).strftime("%Y-%m-%d"))
        # 破冰也是“bot 在群里说了话”：开心流关注窗口，后面有人搭话就能接
        self.state.open_flow(bid, gid, sent_ts, self.cfg.flow_window_minutes * 60.0)
        self.state.save()
        self.log(f"群 {gid} 破冰→ {text}")
        return True

    # ─── 她的时钟 ─────────────────────────────────────

    def _remember_clock(self, bid: str, body: Optional[Dict[str, Any]]) -> None:
        """从 Core 契约里记下她所在城市的时区偏移。"""
        if not self.cfg.use_core_clock or not body:
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
        quiet = self.cfg.in_quiet_hours(dt.hour)
        reply_window = self._pending_window()

        ready: List[Tuple[str, Dict[str, Any], float]] = []
        for uid, u in users.items():
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
                except Exception as e:
                    self.log(f"加载用户 {uid} Core 快照失败: {e}")

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
            cap = (
                desire.urge_cap(u, bool(cue))
                if self.cfg.adaptive_reply_rate
                else desire.URGE_CEILING
            )
            if urge > cap:
                # 被冷落够了，没正事就攒不过这道坎
                u["urge"] = round(cap, 4)
                urge = cap
                self.state.mark_dirty()  # cap 改了内存 urge 就落盘，否则这轮无人 ready 时不写盘
            # 到点的由头本身就是一次念头落地：「想起一件具体的事」不需要再等时候攒满
            if cue:
                urge = max(urge, desire.FIRE_THRESHOLD)
                qualified = urge >= min_urge
            else:
                try:
                    gate = float(u.get("fire_gate") or desire.FIRE_THRESHOLD)
                except (TypeError, ValueError):
                    gate = desire.FIRE_THRESHOLD
                # 门槛抖动只服务于自动循环；手动触发（min_urge 降到 0）要把所有人都列出来
                threshold = max(min_urge, gate) if min_urge >= desire.FIRE_THRESHOLD else min_urge
                qualified = urge >= threshold
            if qualified:
                ready.append((uid, u, urge))

        if ready:
            self.state.mark_dirty()
        ready.sort(key=lambda x: -x[2])
        return ready

    def _pending_window(self) -> float:
        """多久没回算「没接话」：在回复统计窗口基础上至少给半天。"""
        return max(self.cfg.reply_window_hours, PENDING_IGNORE_MIN_HOURS) * 3600

    @staticmethod
    def _is_unreachable_error(err: str) -> bool:
        """发送失败是不是“对这个人根本发不了”（非好友 / 不支持主动 / 会话失效）。"""
        low = str(err or "").lower()
        return any(m.lower() in low for m in _UNREACHABLE_ERROR_MARKERS)

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
        if body and body.get("asleep"):
            return "她正在睡觉"
        seen = float(u.get("last_seen", 0) or 0)
        if now - seen < HOT_CHAT_THRESHOLD:
            return "刚聊上，不插话"
        if now - seen < self.cfg.recent_talk_minutes * 60:
            return "不久前才说过话，再另起一句很突兀"
        if str(u.get("pending_result", "") or "") == "waiting":
            return "上一条主动发的还没回，现在再发就是追着说"
        if now - float(u.get("last_sent", 0) or 0) < self.cfg.user_cooldown_minutes * 60:
            return "护栏冷却未过"
        if now - float(u.get("last_skip_at", 0) or 0) < self.cfg.skip_cooldown_minutes * 60:
            return "刚想过一次决定不说，缓缓"
        if sleep_signal(u, now):
            return "TA 说过要去睡了，这时发过去不合适"
        # 念头攒满了也得看一眼表：这个点对方多半不在线，那就把话留着，等到合适的点再说
        hour = self._moment_of(bid, now).hour
        if self.cfg.respect_user_rhythm and desire.rhythm_factor(u, hour) == desire.RHYTHM_OFF:
            return "按TA 平时的作息，这个点 TA 不玩手机"
        if self.cfg.in_quiet_hours(hour):
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
        if body and body.get("asleep"):
            return "她正在睡觉"
        last_said = self._last_said(u)
        if last_said > 0 and float(u.get("thread_for", 0) or 0) == last_said:
            return "这段沉默已经接过一回了，再问就是催"
        if now - float(u.get("thread_at", 0) or 0) < self.cfg.followup_cooldown_minutes * 60:
            return "刚接过一次，给 TA 点时间"
        if self.cfg.in_quiet_hours(self._moment_of(bid, now).hour):
            return "现在是安静时段"
        return ""

    @staticmethod
    def _last_said(u: Dict[str, Any]) -> float:
        """这一段对话里最后一个人说话的时刻。沉默从这一刻算起。"""
        return max(float(u.get("last_seen", 0) or 0), float(u.get("last_spoken", 0) or 0))

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
    ) -> str:
        """问候的闸门。跟另起话题的区别：

        - 不查 recent_talk：早上第一句话往往就在昨晚那句之后没几小时，问候本来就是时间性触发；
        - 不查「上一条没人回」：真人说完晚安没人理，早上照样道早安，那不算追着说；
        - 不查对方作息：问候窗口本身就是「该说这句的时候」，画像里没早上样本的人也该收到早安。
        """
        if body and body.get("asleep"):
            return "她正在睡觉"
        if now - float(u.get("last_sent", 0) or 0) < self.cfg.user_cooldown_minutes * 60:
            return "护栏冷却未过"
        if self.cfg.in_quiet_hours(self._moment_of(bid, now).hour):
            return "现在是安静时段"
        return ""

    def _greet_pick(
        self,
        bid: str,
        now: float,
        body: Optional[Dict[str, Any]],
    ) -> List[Tuple[str, Dict[str, Any], str, str]]:
        """这个角色名下，所有今天还没被问候、而这个点正落在问候窗口里的人。

        返回 [(uid, u, kind, 动机)]，按最近来往排前。问候是时间性触发：不看念头
        攒没攒满，窗口到了、今天还没说过这一句，就该说了。跟旧调度「一轮只挑
        一个」不同，问候按用户独立成立——A 的早安不该挤掉 B 的。
        """
        if not self.cfg.greeting_enabled:
            return []
        dt = self._moment_of(bid, now)
        morning, night = self.cfg.greeting_windows()
        kind = greeting_window_kind(dt.hour, morning, night)
        if not kind:
            return []
        day = dt.strftime("%Y-%m-%d")
        due: List[Tuple[float, str, Dict[str, Any]]] = []
        for uid, u in self.state.bot(bid).get("users", {}).items():
            if not u.get("umo"):
                continue
            if self.state.is_send_blocked(bid, uid, now):
                continue
            if not greeting_due(u, day, kind):
                continue
            # 只问候最近有来往的人：对素未谋面或早就不聊的人每天道早安，像定时群发
            if now - self._last_said(u) > GREET_FRESH_SECONDS:
                continue
            if self.greet_gate(bid, uid, u, now, body):
                continue
            due.append((float(u.get("last_seen", 0) or 0), uid, u))
        # 最近还在说话的人先问候；窗口只有几小时，后面的人下一轮心跳（几分钟）也来得及
        due.sort(key=lambda x: -x[0])
        return [(uid, u, kind, greet_reason(kind)) for _, uid, u in due]

    def loop_gate(
        self,
        bid: str,
        uid: str,
        u: Dict[str, Any],
        now: float,
        body: Optional[Dict[str, Any]] = None,
    ) -> str:
        """回访那件事的闸门：不看 recent_talk（那件事本来就是聊出来的），但要等这场话凉下来。"""
        if body and body.get("asleep"):
            return "她正在睡觉"
        if self.cfg.in_quiet_hours(self._moment_of(bid, now).hour):
            return "现在是安静时段"
        if now - self._last_said(u) < LOOP_QUIET_GAP_SECONDS:
            return "还在聊，这会儿问「后来呢」太急"
        if str(u.get("pending_result", "") or "") == "waiting":
            return "上一条主动发的还没回"
        return ""

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
            about = live_loop(u, now)
            if not about:
                continue
            if self.loop_gate(bid, uid, u, now, body):
                continue
            # 等得越久的越该先问；同一轮里多个候选时按到期时间排
            due = float(u.get("loop_due", 0) or 0)
            weight = -(now - due) - float(u.get("interest", 0) or 0) * 3600.0
            if best is None or weight < best[0]:
                best = (weight, uid, u, about)
        if best is None:
            return None
        _, uid, u, about = best
        return uid, u, about, now - float(u.get("loop_due", 0) or 0)

    def closer_gate(
        self,
        bid: str,
        uid: str,
        u: Dict[str, Any],
        now: float,
        body: Optional[Dict[str, Any]] = None,
    ) -> str:
        """收场那句的闸门：它存在的意义恰恰是绕过「上一条没人回」，所以不查那条。"""
        if body and body.get("asleep"):
            return "她正在睡觉"
        hour = self._moment_of(bid, now).hour
        if self.cfg.in_quiet_hours(hour):
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
            waited = now - float(u.get("last_sent", 0) or 0)
            if best is None or waited > best[0]:
                best = (waited, uid, u, reason)
        if best is None:
            return None
        return best[1], best[2], best[3]

    def _mind(self, bid: str, u: Dict[str, Any], now: float, urge: float) -> Dict[str, Any]:
        """把念头状态整理成给模型看的内心描述。"""
        return {
            "urge": urge,
            "interest": float(u.get("interest", 0.35) or 0.35),
            "silence_hours": (now - float(u.get("last_seen", 0) or 0)) / 3600.0,
            "pending_result": str(u.get("pending_result", "") or ""),
            "no_reply_streak": int(u.get("no_reply_streak", 0) or 0),
            "they_slept": sleep_signal(u, now),
            "rhythm_off": desire.rhythm_factor(u, self._moment_of(bid, now).hour) == desire.RHYTHM_OFF,
        }

    @staticmethod
    def pick_best(items: List[Tuple[str, Dict[str, Any], float]]) -> Tuple[str, Dict[str, Any], float]:
        """返回念头最强的候选（手动触发时挑「最想说的那个」）。"""
        if not items:
            raise ValueError("候选列表为空")
        return max(items, key=lambda x: x[2])

    async def _persona(self, umo: str, bid: str = "") -> Tuple[str, str]:
        """取目标会话当前生效的 AstrBot 人格，返回 (名字, 设定原文)。

        按 umo 解析意味着多角色天然隔离：每个角色的每个会话用的就是 AstrBot
        在该会话里本来就在用的人格，插件不再插手。
        """
        try:
            name, prompt = await resolve_persona(self.context, umo)
        except Exception as e:
            logger.warning(f"[autonomous_social] 人格设定读取失败，改用默认口吻: {e}")
            return "", ""
        if bid:
            self._last_persona[bid] = name
        return name, prompt

    # ─── 主动联系主循环 ─────────────────────────────────

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
        if not self.cfg.enabled:
            return
        now = self._time()

        # 周期性刷盘
        if now - self._last_flush > FLUSH_INTERVAL_SECONDS:
            self.state.flush()
            self._last_flush = now

        bots = [
            b for b, x in self.state.data.get("bots", {}).items()
            if x.get("users")
        ]
        if not bots:
            return

        # ===== 新增：检测Bot是否在线/可用 =====
        # 在处理每个Bot之前，先验证它是否真的可用（有provider）
        # 避免向不存在或已关闭的Bot不断发送
        available_bots = []
        for bid in bots:
            # 从该Bot的任一用户中取umo，测试能否获取provider
            users = self.state.bot(bid).get("users", {})
            if not users:
                continue
            # 取第一个有umo的用户测试
            test_umo = None
            for uid, u in users.items():
                test_umo = str(u.get("umo", "") or "")
                if test_umo:
                    break
            if not test_umo:
                self.log(f"Bot {bid} 的所有用户都没有umo，跳过")
                continue
            # 测试能否获取provider
            try:
                test_provider = await self.generator._get_provider(test_umo)
                if test_provider is None:
                    logger.warning(f"[autonomous_social] Bot {bid} 无法获取LLM provider，可能已离线或未配置，本轮跳过")
                    continue
                available_bots.append(bid)
            except Exception as e:
                logger.warning(f"[autonomous_social] Bot {bid} provider检测失败: {e}，本轮跳过")
                continue
        
        bots = available_bots
        if not bots:
            self.log("所有Bot都不可用（无法获取provider），本轮跳过")
            return
        # ===== 检测结束 =====

        # 每个角色只读一次 Core 状态
        core_root = None
        try:
            core_root = self.core.read_root()
        except Exception as e:
            logger.warning(f"[autonomous_social] 读取 Core 状态失败: {e}")

        # 各类候选按用户收集，不挑唯一：同一类里可以同时有多个人等着
        # 早晚问候（时间性触发，窗口过了就没了）
        greets: List[Tuple[str, str, Dict[str, Any], str, str]] = []
        # 念头攒满、想另起话题的
        contenders: List[Tuple[str, str, Dict[str, Any], float]] = []
        # 话说到一半断掉的人：这比「另起一个话题」紧迫，排在念头前面处理
        threads: List[Tuple[str, str, Dict[str, Any], str, str, float]] = []
        # 挂着没回访的那件事，以及该自己收场的那些
        loops: List[Tuple[str, str, Dict[str, Any], str, float]] = []
        closers: List[Tuple[str, str, Dict[str, Any], str]] = []
        max_streak = 0
        top_desire = None
        # 按角色存住身体快照：后面要用它过闸门，拿循环残留的变量会把 A 的身体套到 B 头上
        bodies: Dict[str, Dict[str, Any]] = {}
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
                streak = self.state.max_no_reply_streak(bid)
                max_streak = max(max_streak, streak)
                if top_desire is None and bot_state.get("social_desire") is not None:
                    top_desire = bot_state.get("social_desire")
            ready = self.settle_minds(bid, now, self._moment_of(bid, now), core_root, bot_state)
            # 念头攒满的每个人都进队：不再只取最想说的那个——B 的念头不该被 A 挤掉
            for uid, u, urge in ready:
                contenders.append((bid, uid, u, urge))
            picked = self._thread_pick(bid, now, bot_state)
            if picked:
                threads.append((bid, *picked))
            looped = self._loop_pick(bid, now, bot_state)
            if looped:
                loops.append((bid, *looped))
            closed = self._closer_pick(bid, now, bot_state)
            if closed:
                closers.append((bid, *closed))
            for uid, u, kind, reason in self._greet_pick(bid, now, bot_state):
                greets.append((bid, uid, u, kind, reason))

        # 把近况写回给 Core：她被冷落了几个、现在多想说话。写失败不影响发送。
        try:
            self.signals.set_ignored_streak(max_streak)
            self.signals.set_desire(top_desire)
            # 两个 setter 都可能因为「值没变」而提前 return，那样文件永远不会被创建、
            # mtime 也不会新鲜，Core 会一直报「读不到信号」。每周期再刷一次心跳。
            self.signals.beat()
        except Exception as e:
            self.log(f"写回社交信号失败: {e}")

        if not greets and not contenders and not threads and not loops and not closers:
            self.log("没有人攒够念头，本轮只是把时间补算上")
            # 没人可找时仍可能有冷群该破冰（破冰走群数据，不依赖 per-user 念头）
            if self.cfg.group_icebreak_enabled:
                await self._run_icebreaks(bots, now)
            return

        # 每个角色这一轮的发送配额：只是防刷屏的护栏。节奏本身长在各自的
        # user_cooldown 与念头上，跟「上一个被找的人是谁」无关
        budget: Dict[str, int] = {
            bid: max(1, self.cfg.max_sends_per_round) for bid in bots
        }
        # 同一个人一轮里只服务一次：既到问候窗口、念头又攒满时，说一句早安就够
        served: Set[Tuple[str, str]] = set()

        async def speak(
            bid: str,
            uid: str,
            u: Dict[str, Any],
            urge: float,
            preset: Optional[Tuple[str, Dict[str, Any]]],
            note: str,
        ) -> bool:
            """发一条、记账、记账后稍隔几秒。发不出（被否决/失败）不耗配额。"""
            if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                return False
            sent, result = await self._speak(bid, uid, u, urge, now, preset=preset)
            self.log(f"{note}{uid} → {result}")
            if not sent:
                return False
            budget[bid] -= 1
            served.add((bid, uid))
            if budget.get(bid, 0) > 0:
                # 这一轮还会找下一个人：稍微隔开几秒，别一口气把所有话说完
                await asyncio.sleep(random.uniform(2.0, 5.0))
            return True

        # 优先级：问候（有时间窗，错过就没了）> 追问（话正热着）> 回访（到点的事）
        # > 另起话题（念头攒满）> 收场。收场排最后：前三条都是「有正事说」，
        # 收场是「没正事也别冷着」。
        for bid, uid, u, kind, reason in greets:
            if budget.get(bid, 0) <= 0:
                continue
            label = "早安" if kind == "morning" else "晚安"
            await speak(bid, uid, u, 0.0, (reason, greet_meta(kind)), f"问候（{label}） ")

        for bid, uid, u, kind, reason, gap in sorted(threads, key=lambda item: item[5]):
            if budget.get(bid, 0) <= 0:
                continue
            meta = thread_meta(
                kind,
                about=str(u.get("last_message", "") or ""),
                asked=str(u.get("last_spoken_text", "") or ""),
            )
            await speak(bid, uid, u, 0.0, (reason, meta), f"{kind}（沉默 {gap / 60:.0f} 分钟）")

        for bid, uid, u, about, overdue in sorted(loops, key=lambda item: -item[4]):
            if budget.get(bid, 0) <= 0:
                continue
            await speak(
                bid, uid, u, 0.0,
                (loop_reason(about), loop_meta(about)),
                f"回访「{about}」（到期 {overdue / 60:.0f} 分钟后）",
            )

        contenders.sort(key=lambda x: -x[3])
        for bid, uid, u, urge in contenders:
            if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                continue
            why = self.gate_reason(bid, uid, u, now, bodies.get(bid))
            if why:
                self.log(f"念头到了但没说（{uid}）：{why}")
                continue
            await speak(bid, uid, u, urge, None, f"另起话题 urge={urge:.2f} ")

        for bid, uid, u, reason in closers:
            if budget.get(bid, 0) <= 0:
                continue
            hung = (now - float(u.get("last_sent", 0) or 0)) / 3600.0
            await speak(
                bid, uid, u, 0.0,
                (reason, closer_meta(str(u.get("last_spoken_text", "") or ""))),
                f"收场（那句悬了 {hung:.1f} 小时）",
            )

        # 群冷场破冰：每个角色一轮最多给一个冷群破冰，别一口气把好几个群都点一遍。
        # 破冰与私聊发送各走各的配额，不与 per-user 互抢。
        if self.cfg.group_icebreak_enabled:
            await self._run_icebreaks(bots, now)

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
    ) -> Tuple[bool, str]:
        """把念头变成一条消息。返回 (是否发出, 说明文本)。

        allow_veto=True 时模型可以回答「现在不该说」，这是自动循环的默认；手动触发
        传 False —— 主人明确要看效果，就别再替他否决了。

        preset 用于「跟进断掉的话」：那条的动机不是从念头里长出来的，不能交给
        generate_reason 重新找一个开口的理由。
        """
        umo = str(u.get("umo", "") or "")
        if not umo:
            return False, "没有可用的会话来源（umo），发不出去"

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
            core_root = self.core.read_root()
            if core_root:
                user_core = self.core.load_snapshot(bid, uid, root=core_root)
                if user_core:
                    core_context = self.core.compact(user_core)
        except Exception as e:
            self.log(f"加载用户 {uid} Core 快照失败: {e}")

        u_copy = dict(u)
        affection = user_core.get("affection") if user_core else None
        if affection is None:
            # 没接 Core 时好感度是空的，关系档位会永远停在「未知」；用在意程度估一个，
            # 它本来就由熟悉度与回复情况合成，比一律按陌生人写要准
            affection = round(float(u.get("interest", 0.35) or 0.35) * 100, 1)
        u_copy["_affection"] = affection
        u_copy["_energy"] = user_core.get("energy") if user_core else None
        u_copy["_social_energy"] = user_core.get("social_energy") if user_core else None
        # 接了 Core v2.14 的契约时这里有完整的身体：困不困、饿不饿、想说话的程度，
        # 以及她此刻正手上的事。拿不到契约时为空，生成器会退回旧的两个标量。
        u_copy["_body"] = user_core if (user_core or {}).get("contract_v") else None

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

        thread_anchor = self._last_said(u)

        def defer_cue() -> None:
            """这次因由头或挂着的事开口但没说成：往后推，别每个心跳都重试同一件事。"""
            category = str((reason_meta or {}).get("category") or "")
            if category == "cue":
                u["cue_due"] = self._time() + CUE_RETRY_DELAY_SECONDS
            elif category == "loop":
                u["loop_due"] = self._time() + CUE_RETRY_DELAY_SECONDS

        parts: Optional[List[str]] = None
        veto_note = ""
        if allow_veto and self.cfg.llm_gate:
            decision = None
            try:
                decision = await self.generator.decide(
                    umo, u_copy, core_context, reason, reason_meta, persona_prompt, mind,
                    at=now, clock_offset=self._clock_offset_for(bid),
                )
            except Exception as e:
                logger.warning(f"[autonomous_social] 判断该不该说时出错: {e}")
            if decision is None:
                self._last_llm_error = "LLM 不可用（provider 取不到或调用失败），本轮未发送"
                defer_cue()
                return False, self._last_llm_error
            if not decision.send:
                desire.after_skip(u, now)
                u["last_skip_reason"] = decision.why_not
                defer_cue()
                self.state.mark_dirty()
                self.state.save()
                return False, f"想过，但觉得现在不该说：{decision.why_not}"
            parts = decision.parts
        else:
            try:
                parts = await self.generator.generate(
                    umo, u_copy, core_context, reason, reason_meta, persona_prompt, mind,
                    at=now, clock_offset=self._clock_offset_for(bid),
                )
            except Exception as e:
                logger.warning(f"[autonomous_social] 生成消息失败: {e}")
            if not parts:
                self._last_llm_error = "LLM 生成为空（provider 不可用或调用失败）"
                defer_cue()
                return False, self._last_llm_error

        # 生成期间对方可能发了新消息
        latest = self.state.user(bid, uid)
        if self._time() - float(latest.get("last_seen", 0)) < FINAL_CHECK_GAP_SECONDS:
            defer_cue()
            return False, "对方刚发了新消息，不插话"

        sent_ok, err = await self._send_with_retry(umo, parts[0])
        if not sent_ok:
            defer_cue()
            # 非好友 / 平台不支持主动消息 / 会话失效：不是“这一次没发成”而是“对这个人发不了”，
            # 把他隔离一段时间，别每个心跳都去撞同一堆“请添加对方为好友”、白燒冷却
            if self._is_unreachable_error(err):
                self.state.block_send(
                    bid, uid, self._time() + SEND_BLOCK_HOURS * 3600.0, err
                )
                logger.warning(
                    f"[autonomous_social] {uid}（bid={bid}, umo={umo}）发送持续失败，"
                    f"隔离 {SEND_BLOCK_HOURS}h：{err}"
                )
            return False, f"消息写好了但发送失败：{err}"

        sent_ts = self._time()
        msg_type = reason_meta.get("msg_type") if reason_meta else None
        self.state.record_outgoing(bid, uid, parts[0], msg_type)
        category = str((reason_meta or {}).get("category") or "")
        if category in ("probe", "presence"):
            # 这段沉默已经接过一回了：对方再不回，也不能追第二遍
            u["thread_for"] = thread_anchor
            u["thread_at"] = sent_ts
        elif category == "loop":
            # 问过了就不再挂着，否则每次心跳都会把它捡回来问一遍
            u["loop"] = ""
            u["loop_due"] = 0.0
            u["loop_at"] = sent_ts
        elif category == "closer":
            u["closer_for"] = float(u.get("last_sent", 0) or 0) or sent_ts
            u["closer_at"] = sent_ts
        elif category == "greet":
            # 今天这个窗口已经问候过：记下日期与种类，同一窗口不再发第二遍
            u["greet_kind"] = str((reason_meta or {}).get("kind") or "")
            u["greet_day"] = self._moment_of(bid, sent_ts).strftime("%Y-%m-%d")
            u["greet_at"] = sent_ts
            # 问候本来就是「不用回」的话（晚安没人理很正常）：预先记成已收场，
            # 别让收场路径 8 小时后给一句没被回的早安补「没事 我就是随口一说」
            u["closer_for"] = sent_ts
        bot = self.state.bot(bid)
        bot["last_global_send"] = sent_ts
        self.state.save()
        self.signals.note_proactive(uid, sent_ts)
        if len(parts) > 1:
            self._schedule_burst(bid, uid, parts[1:], sent_ts)
            veto_note = f"\n（稍后还会自然补 {len(parts) - 1} 条）"
        self._last_llm_error = ""
        return True, (
            f"已主动联系 {uid}（类型={msg_type}，念头={urge:.2f}）：\n{parts[0]}{veto_note}"
        )

    async def trigger_once(self, bid: str) -> str:
        """手动触发一次主动联系：绕过念头是否攒满，直接挑最想说的人说一句。

        仍然避开「对方正在热聊」的情况 —— 主人要看效果，不代表可以去打扰正在聊天的人。

        Args:
            bid: 发起命令的 bot ID

        Returns:
            结果描述文本（供命令回显）
        """
        if not self.cfg.enabled:
            return "插件已停用（enabled=false），无法触发。"

        now = self._time()
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
        except Exception as e:
            logger.warning(f"[autonomous_social] 触发时读 Core 失败: {e}")

        # min_urge=0：不管攒没攒满都列出来，手动触发挑念头最高的那个
        ranked = self.settle_minds(
            bid, now, self._moment_of(bid, now), core_root, bot_state, min_urge=0.0
        )
        if not ranked:
            return "没有可联系的用户（都没有会话来源，或都还在刚聊完的窗口里）。"

        uid, u, urge = self.pick_best(ranked)
        seen_gap = now - float(u.get("last_seen", 0) or 0)
        if seen_gap < HOT_CHAT_THRESHOLD:
            return f"用户 {uid} 正在热聊（{int(seen_gap / 60)} 分钟前还在说话），为避免打扰未发送。"

        sent, note = await self._speak(bid, uid, u, urge, now, allow_veto=False)
        self.log(f"手动触发 {uid}：{note}")
        return note if sent else f"未发送：{note}"
    async def _send_message(self, target: str, text: str) -> None:
        """发送消息，兼容多种 API。

        Args:
            target: 目标（统一消息来源）
            text: 消息文本

        Raises:
            Exception: 发送失败时抛出
        """
        if not target:
            raise RuntimeError("unified_msg_origin 为空，无法发送主动消息")

        # 方式1: AstrBot 官方主动消息 API —— 优先使用 MessageChain
        # 注意：官方 send_message 找不到匹配平台时不抛异常而是返回 False
        # （会话来源失效、或平台不支持主动消息如 qq_official），必须检查返回值。
        if hasattr(self.context, "send_message"):
            if MessageChain is not None:
                try:
                    message_chain = MessageChain().message(text)
                    ok = await self.context.send_message(target, message_chain)
                    if ok is False:
                        raise RuntimeError(
                            "send_message 返回 False：未找到匹配会话平台"
                            "（unified_msg_origin 可能已失效，或平台不支持主动消息，如 qq_official）"
                        )
                    return
                except RuntimeError:
                    raise
                except Exception as e:
                    logger.warning(
                        f"[autonomous_social] MessageChain 发送未成功，回退纯文本: {e}"
                    )

            # 部分兼容版本可能接受纯文本
            try:
                ok = await self.context.send_message(target, text)
                if ok is False:
                    raise RuntimeError(
                        "send_message 返回 False：未找到匹配会话平台"
                        "（unified_msg_origin 可能已失效，或平台不支持主动消息，如 qq_official）"
                    )
                return
            except Exception as e:
                raise RuntimeError(f"context.send_message 调用失败: {e}") from e

        # 方式2: 通过 platform 客户端发送（旧版兼容）
        if hasattr(self.context, "platform") and hasattr(self.context.platform, "send_message"):
            await self.context.platform.send_message(target, text)
            return

        # 方式3: 兜底
        raise RuntimeError("无法找到可用的发送消息 API")

    async def _send_with_retry(self, target: str, text: str) -> Tuple[bool, str]:
        """发送一条消息，失败自动重试。返回 (是否成功, 最后错误)。"""
        last_err = ""
        for attempt in range(SEND_RETRY_COUNT + 1):
            try:
                await self._send_message(target, text)
                return True, ""
            except Exception as e:
                last_err = str(e)
                if attempt < SEND_RETRY_COUNT:
                    logger.warning(
                        f"[autonomous_social] 发送失败（第{attempt + 1}次），稍后重试: {e}"
                    )
                    await asyncio.sleep(SEND_RETRY_DELAY)
                else:
                    logger.warning(f"[autonomous_social] 发送失败（已重试{SEND_RETRY_COUNT}次）: {e}")
        return False, last_err

    def _schedule_burst(self, bid: str, uid: str, rest: List[str], sent_ts: float) -> None:
        """把连发的后续几条挂成后台任务，不阻塞主循环与命令回显。"""
        task = asyncio.create_task(self._send_burst_followup(bid, uid, rest, sent_ts))
        self._burst_tasks.add(task)
        task.add_done_callback(self._burst_tasks.discard)

    async def _send_burst_followup(
        self, bid: str, uid: str, rest: List[str], sent_ts: float
    ) -> None:
        """隔 1-3 分钟逐条补发连发的剩余消息；期间对方回了话或插件停止则停下。

        v1.10.2 之前只补发第二条（第三条起直接丢弃）：模型把一件事拆成三段时，
        对方永远只收到前两段，后半句悬在空中。对方回了话就停——TA 已经接上了，
        再把剩下的旧话发过去就是答非所问。
        """
        try:
            for text in rest:
                await asyncio.sleep(random.randint(BURST_DELAY_MIN, BURST_DELAY_MAX))
                if not self.running:
                    return
                u = self.state.user(bid, uid)
                if float(u.get("last_seen", 0)) > sent_ts:
                    self.log(f"用户 {uid} 已有新消息，取消连发补充")
                    return
                umo = str(u.get("umo", "") or "")
                if not umo:
                    return
                ok, _ = await self._send_with_retry(umo, text)
                if not ok:
                    return
                self.state.record_outgoing(bid, uid, text, count_proactive=False)
                self.state.save()
                self.log(f"已连发补充给 {uid}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[autonomous_social] 连发补充失败: {e}")

    def _schedule_group_burst(
        self, bid: str, gid: str, umo: str, rest: List[str], sent_ts: float
    ) -> None:
        """把群聊心流连发的后续几句挂成后台任务，不阻塞观察链路。"""
        task = asyncio.create_task(
            self._send_group_burst_followup(bid, gid, umo, rest, sent_ts)
        )
        self._burst_tasks.add(task)
        task.add_done_callback(self._burst_tasks.discard)

    async def _send_group_burst_followup(
        self, bid: str, gid: str, umo: str, rest: List[str], sent_ts: float
    ) -> None:
        """隔几秒逐条补发心流连发的剩余句子（同一个念头拆成的）。

        这几句不再各自过心流闸/计额（算同一次插话），只写进自己的近期发言供下一句参考。
        插件停了、群被隔离就不再补。
        """
        try:
            for text in rest:
                await asyncio.sleep(
                    random.randint(GROUP_BURST_DELAY_MIN, GROUP_BURST_DELAY_MAX)
                )
                if not self.running or not self.cfg.enabled:
                    return
                now = self._time()
                if self.state.is_group_blocked(bid, gid, now):
                    return
                ok, _ = await self._send_with_retry(umo, text)
                if not ok:
                    return
                self.state.record_group_self_text(
                    bid, gid, text, now, sample_cap=self.cfg.group_ref_sample_size
                )
                self.state.save()
                self.log(f"群 {gid} 心流连发补充→ {text}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[autonomous_social] 群心流连发补充失败: {e}")

    # ─── 后台循环 ───────────────────────────────────────

    def _seed_tick(self, force: bool = False) -> None:
        """播种用户：把还没进插件状态的人补进来（历史导入 + 播种名单）。

        历史导入是启动一次 + 每半小时一次：数据库在长，插件状态里的「认识的人」
        要跟上。已经在 state.json 里的用户一律不动，实时数据永远比旧快照新。
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
                    logger.warning(f"[autonomous_social] {import_diag}")
                added += history_ingest.seed_from_history(
                    self.state,
                    self._data_dir,
                    private_only=self.cfg.private_only,
                    store_text=self.cfg.store_message_text,
                    now=self._time(),
                )
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

    async def run(self) -> None:
        """后台主循环。"""
        logger.info("[autonomous_social] 后台循环已启动")
        self._prune_expired()
        while self.running:
            try:
                # 检查间隔随机（2-9 分钟）
                await asyncio.sleep(random.randint(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS))
                if not self.running:
                    break
                self._prune_expired()
                self._seed_tick()
                await self.try_once()
            except asyncio.CancelledError:
                logger.info("[autonomous_social] 后台循环被取消")
                raise
            except Exception as e:
                logger.warning(f"[autonomous_social] 后台循环异常: {e}")
                await asyncio.sleep(30)
        logger.info("[autonomous_social] 后台循环已退出")

    # ─── 状态展示 ───────────────────────────────────────

    def _core_status_text(self) -> str:
        """检查 Humanoid Core 联动状态，返回描述文本。"""
        if self.cfg.mode == "standalone":
            return "独立模式（不联动 Core）"

        try:
            root = self.core.read_root()
        except Exception as e:
            return f"❌ Core 读取失败: {e}"

        if not root:
            return "❌ 未找到 Core state.json"

        roles = list(root.get("roles", {}).keys())
        our_bots = set(self.state.data.get("bots", {}).keys())
        matched = our_bots & set(roles)

        if not matched:
            our_bot_list = list(our_bots)[:3]
            return f"⚠️ Core 已找到但角色不匹配（Core: {roles[:3]}..., 本插件: {our_bot_list}）"

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
        detail = f"契约 v{contract_v}（身体轴参与决策）" if contract_v else "旧字段，只能看到精力与社交能量（建议升级 Core 到 v2.14+）"
        return f"✅ 联动正常（Core 角色 {len(roles)} 个，匹配 {len(matched)} 个；{detail}）"

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
                    ts = float(u.get("last_seen", 0) or 0)
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
                    urge = float(u.get("urge", 0) or 0)
                    interest = float(u.get("interest", 0) or 0)
                except (TypeError, ValueError):
                    continue
                rows.append((
                    bid, uid, urge, interest,
                    str(u.get("pending_result", "") or ""),
                    float(u.get("last_seen", 0) or 0),
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
                )
                if float(u.get("thread_for", 0) or 0) == last_said:
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
            sent = float(u.get("last_sent", 0) or 0)
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
                sent = float(u.get("last_sent", 0) or 0)
                if sent and (best is None or sent > best[0]):
                    text = ""
                    for m in reversed(u.get("conversation") or []):
                        if m.get("dir") == "out":
                            text = str(m.get("text", ""))[:40]
                            break
                    best = (sent, bid, uid, text)
                skipped = float(u.get("last_skip_at", 0) or 0)
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
        return "\n".join(lines) if lines else "  还没有主动联系过任何人"

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
                last_ts = max((float(e.get("ts", 0) or 0) for e in log), default=0.0)
                rows.append((last_ts, bid, uid, log))
        if not rows:
            if want:
                return f"  {want}：最近 7 天没给 TA 发过主动消息"
            return "  最近 7 天还没给任何人发过主动消息"
        rows.sort(key=lambda x: -x[0])
        lines: List[str] = []
        for _last, bid, uid, log in rows[:limit_users]:
            name = str(self.state.user(bid, uid).get("name", "") or uid)
            lines.append(f"◆ {name}（{uid} @ {self._bot_label(bid)}）最近 7 天发过 {len(log)} 条：")
            for e in log[-limit_each:]:
                try:
                    ts = float(e.get("ts", 0) or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                when = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "??"
                text = str(e.get("text", "") or "").strip()
                lines.append(f"    {when}  {text}")
        if len(rows) > limit_users:
            lines.append(f"  …另有 {len(rows) - limit_users} 人未列出（用「主动消息记录 <用户ID>」看单个人）")
        return "\n".join(lines)

    def group_status_text(self, limit: int = 20) -> str:
        """列出各角色在管的群：最近活跃、心流窗口、是否被隔离，供主人核对“精准识别”与踢群是否真停。"""
        now = self._time()
        self._refresh_clocks()
        head = (
            "群聊心流："
            + ("开" if self.cfg.group_flow_enabled else "关")
            + " / 破冰：" + ("开" if self.cfg.group_icebreak_enabled else "关")
            + " / 参考库：" + ("开" if self.cfg.group_ref_lib_enabled else "关")
        )
        rows: List[Tuple[float, str]] = []
        for bid, bot in self.state.data.get("bots", {}).items():
            for gid, g in (bot.get("groups") or {}).items():
                seen = float(g.get("last_seen", 0) or 0)
                idle_h = (now - seen) / 3600.0 if seen > 0 else -1
                flow_open = float(g.get("flow_open_until", 0) or 0) > now
                blocked = float(g.get("blocked_until", 0) or 0) > now
                name = str(g.get("name", "") or gid)
                mark = "🔕" if blocked else ("🟢" if flow_open else "・")
                idle_txt = f"{idle_h:.1f}h前" if idle_h >= 0 else "未知"
                rows.append((
                    seen,
                    f"  {mark} {name}（{gid} @ {self._bot_label(bid)}）最近 {idle_txt}，样本 {len(g.get('samples') or [])} 条"
                    + (f"，隔离中（{g.get('blocked_reason','')[:20]}）" if blocked else "")
                ))
        if not rows:
            return head + "\n  还没在任何群里观察到消息（群里有人说话后才会出现在这里）。"
        rows.sort(key=lambda x: -x[0])
        body = "\n".join(r for _s, r in rows[:limit])
        more = f"\n  …另有 {len(rows) - limit} 个群未列出" if len(rows) > limit else ""
        return head + "\n" + body + more

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
        users = sum(len(x.get("users", {})) for x in bots.values())
        total_sent = 0
        total_replied = 0
        for bot_data in bots.values():
            for u in bot_data.get("users", {}).values():
                total_sent += int(u.get("proactive_sent", 0))
                total_replied += int(u.get("proactive_replied", 0))
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

        return (
            f"自主拟人社交 v{__version__}\n"
            f"状态：{'启用' if self.cfg.enabled else '停用'}\n"
            f"模式：{self.cfg.mode}\n"
            f"人格（按会话解析，各角色互不影响）：\n{persona_text}\n"
            f"Core 联动：{self._core_status_text()}\n"
            f"LLM 生成：{self._last_llm_error if self._last_llm_error else '暂无失败记录'}\n"
            f"节奏：念头驱动（最在意的人约 {self.cfg.urge_refill_hours} 小时攒满一次；"
            f"刚聊完 {self.cfg.recent_talk_minutes} 分钟内不另起）\n"
            f"发送前把关：{'模型判断该不该说' if self.cfg.llm_gate else '仅规则闸门'}\n"
            f"时机：{'按对方作息挑点' if self.cfg.respect_user_rhythm else '不看作息'}"
            f"、{'记得对方说的约定' if self.cfg.cue_followup else '不跟由头'}\n"
            f"活跃度：{self.cfg.activity_level}（整体想说话 ×{self.cfg.mood_scale:.2f}）\n"
            f"当前时段：{slot_names.get(slot, '未知')} {dt.strftime('%H:%M')}"
            f"{'（安静时段，念头长得极慢）' if self.cfg.in_quiet_hours(dt.hour) else ''}"
            f"　{'按她所在城市' if self._city_offset.get(ref_bid) is not None else '按本机时钟'}\n"
            f"未完话题："
            + (
                f"开（对方答得敷衍 {self.cfg.probe_after_minutes} 分钟就追问那件事；"
                f"聊到一半断了 {self.cfg.followup_after_minutes} 分钟问一句；一段沉默只接一次）"
                if self.cfg.followup_enabled
                else "关"
            )
            + "\n"
            + (
                f"回访挂事：开（提过没说结果的事，隔 {self.cfg.loop_min_hours:g}~"
                f"{self.cfg.loop_max_hours:g} 小时问后来）"
                if self.cfg.loop_enabled
                else "回访挂事：关"
            )
            + "\n"
            + (
                f"没人回收场：开（她主动发的话 {self.cfg.closer_after_hours} 小时没人回，"
                "自己冒一句揭过去）"
                if self.cfg.closer_enabled
                else "没人回收场：关"
            )
            + "\n"
            + (
                "看得见她说过的话：开"
                if self.cfg.track_own_replies
                else "看得见她说过的话：关（未完话题的判定会明显变弱）"
            )
            + "\n"
            + (
                f"早晚问候：开（早安 {self.cfg.greeting_morning_start}~{self.cfg.greeting_morning_end} 点、"
                f"晚安 {self.cfg.greeting_night_start}~{self.cfg.greeting_night_end % 24} 点，"
                "每人每天每窗口一次）"
                if self.cfg.greeting_enabled
                else "早晚问候：关"
            )
            + "\n"
            + (
                f"群聊心流：开（bot 在群里说过话后 {self.cfg.flow_window_minutes} 分钟内无需@接话；"
                f"同群同小时最多 {self.cfg.flow_hourly_cap} 条）"
                if self.cfg.group_flow_enabled
                else "群聊心流：关"
            )
            + f"　破冰{'开' if self.cfg.group_icebreak_enabled else '关'}"
            + f"　参考库{'开' if self.cfg.group_ref_lib_enabled else '关'}"
            + f"　在管群 {sum(len(b.get('groups') or {}) for b in self.state.data.get('bots', {}).values())} 个（详见「群社交状态」）"
            + "\n"
            + f"护栏冷却：同一人 {self.cfg.user_cooldown_minutes} 分钟 / "
            + f"每轮每角色最多 {self.cfg.max_sends_per_round} 条（不同人互不排队）\n"
            + f"播种：历史导入{'开' if self.cfg.history_ingest else '关'}　"
            + f"名单 {len(history_ingest.normalize_seed_entries(self.cfg.seed_users, self.cfg.seed_platform))} 人（没聊过也能找）\n"
            + self._recognition_text() + "\n"
            + f"记录 bot：{len(bots)}（{bot_list}）\n"
            + f"候选用户：{users}\n"
            + f"主动消息发送/回复：{reply_rate}\n"
            + f"回复窗口：{self.cfg.reply_window_hours}小时\n"
            + f"连发：{'开' if self.cfg.allow_burst else '关'}　"
            + f"emoji：{'保留' if self.cfg.allow_emoji else '去掉'}　"
            + f"括号动作：{'去掉' if self.cfg.strip_roleplay_actions else '保留'}\n"
            + f"念头面板：\n{self.urge_panel_text()}\n"
            + f"最近联系：\n{self.last_contact_text()}"
        )