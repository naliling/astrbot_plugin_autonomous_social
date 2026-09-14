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
import os
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

try:
    from astrbot.api.event import MessageChain
except ImportError:
    MessageChain = None

from . import __version__, desire
from .config import SocialConfig
from .core_bridge import CoreBridge
from .signals import FILE_NAME as SIGNALS_FILE_NAME, SignalsWriter
from .generator import MessageGenerator
from .persona import resolve_persona
from .reasoning import (
    extract_cue,
    generate_reason,
    live_cue,
    sleep_signal,
    time_slot,
)
from .state import SocialState
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
MIN_INTERVAL_SECONDS = 180       # 检查间隔最小值（只是结算时机，不是节奏来源）
MAX_INTERVAL_SECONDS = 480       # 检查间隔最大值
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
SEND_RETRY_DELAY = 1.0

# 回访那件事之前，这场话至少凉下来多久：还在你来我往时问「后来呢」太急
LOOP_QUIET_GAP_SECONDS = 1800.0

# 连发：第二条的补发间隔（秒）
BURST_DELAY_MIN = 60
BURST_DELAY_MAX = 180

# 过期用户数据清理的扫描间隔（秒）
PRUNE_INTERVAL_SECONDS = 3600


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
        self.running = False
        self._last_flush = 0.0
        self._last_prune = 0.0
        self._last_llm_error = ""
        # bid -> 本周期实际用到的人格名（仅用于状态展示，不落盘）
        self._last_persona: Dict[str, str] = {}
        # bid -> 她所在城市相对本机的分钟偏移（从 Core 契约里拿）。没有契约时不设，
        # 时段判断退回本机时钟：宁可用错一个时钟，也不能把她的城市当成 UTC。
        self._city_offset: Dict[str, int] = {}
        self._burst_tasks: set = set()

    # ─── 生命周期 ───────────────────────────────────────

    def start(self) -> None:
        """启动引擎。"""
        self.running = True
        logger.info(f"[autonomous_social] 引擎启动，模式: {self.cfg.mode}")

    def stop(self) -> None:
        """停止引擎：取消未发的连发补句，确保状态落盘。"""
        self.running = False
        for task in list(self._burst_tasks):
            task.cancel()
        self._burst_tasks.clear()
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

        # 判定是否群消息（优先事件属性，否则看 message_obj.group_id）
        if self.cfg.private_only and self._detect_is_group(event):
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
                    msg, self._time(), self._clock_offset_for(bid) or 0
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
                    self._clock_offset_for(bid) or 0,
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
        if self.cfg.private_only and self._detect_is_group(event):
            return
        user = self.state.user(bid, uid)
        # 同一条消息可能被分几条发：跟上一条重复就不另记一次
        if str(user.get("last_spoken_text", "") or "") == text[:120]:
            return
        self.state.record_spoken(
            bid, uid, text, store_text=self.cfg.store_message_text
        )

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
        """判定是否群消息。

        优先事件自带的 is_group 布尔属性；否则回退到 message_obj.group_id：
        群消息 group_id 非空且非 0，私聊为 0/空/None。无法判定时按私聊处理。
        """
        try:
            ig = getattr(event, "is_group", None)
            if isinstance(ig, bool):
                return ig
        except Exception:
            pass
        try:
            mo = getattr(event, "message_obj", None)
            if mo is not None:
                gid = getattr(mo, "group_id", None)
                if gid is None:
                    gid = getattr(mo, "group", None)
                if gid in (None, "", 0):
                    return False
                return True
        except Exception:
            pass
        return False

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
        """她那里现在是几点。没接到 Core 时退回本机时间。"""
        offset = self._city_offset.get(bid) if self.cfg.use_core_clock else None
        if not offset:
            return datetime.fromtimestamp(now)
        return datetime.fromtimestamp(now + offset * 60.0)

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

            # 上次主动联系有没有被接住（对方再也没回的情况在这里结算）
            self.state.settle_pending(bid, uid, now, reply_window)

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
        """一次心跳：结算所有人的念头，最想开口的那件过了闸门就说出去。

        心跳本身不决定发不发 —— 它只是「想起来看一眼手机」的时机。真正的节奏长在
        urge 里：谁攒满了、为什么攒满、现在适不适合说，都跟这个定时器无关。
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

        # 每个角色只读一次 Core 状态
        core_root = None
        try:
            core_root = self.core.read_root()
        except Exception as e:
            logger.warning(f"[autonomous_social] 读取 Core 状态失败: {e}")

        # 各角色分别结算念头，取每个角色里最想开口的那个排个序
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
            if ready:
                uid, u, urge = ready[0]
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

        # 把近况写回给 Core：她被冷落了几个、现在多想说话。写失败不影响发送。
        try:
            self.signals.set_ignored_streak(max_streak)
            self.signals.set_desire(top_desire)
        except Exception as e:
            self.log(f"写回社交信号失败: {e}")

        if not contenders and not threads and not loops and not closers:
            self.log("没有人攒够念头，本轮只是把时间补算上")
            return

        # 全局护栏对所有路一视同仁：同一轮里不能既追问又另起话题
        def guard_left(bid: str) -> float:
            last = float(self.state.bot(bid).get("last_global_send", 0) or 0)
            return last + self.cfg.global_cooldown_minutes * 60 - now

        # 优先级：这场话还热着时的追问 > 到点回访 > 另起话题 > 给悬着的话收场。
        # 收场排最后，是因为前三条都是「有正事说」，收场是「没正事也别冷着」。
        for bid, uid, u, kind, reason, gap in sorted(threads, key=lambda item: item[5]):
            left = guard_left(bid)
            if left > 0:
                self.log(f"{bid} 全局护栏未过（还需 {left / 60:.0f} 分钟），接话也先不发")
                continue
            meta = thread_meta(
                kind,
                about=str(u.get("last_message", "") or ""),
                asked=str(u.get("last_spoken_text", "") or ""),
            )
            sent, note = await self._speak(
                bid, uid, u, 0.0, now, preset=(reason, meta)
            )
            self.log(f"{kind} {uid}（沉默 {gap / 60:.0f} 分钟）urge=0 → {note}")
            if sent:
                return

        for bid, uid, u, about, overdue in sorted(loops, key=lambda item: -item[4]):
            left = guard_left(bid)
            if left > 0:
                self.log(f"{bid} 全局护栏未过，回访也先不发")
                continue
            sent, note = await self._speak(
                bid, uid, u, 0.0, now, preset=(loop_reason(about), loop_meta(about))
            )
            self.log(f"回访「{about}」{uid}（到期 {overdue / 60:.0f} 分钟后）→ {note}")
            if sent:
                return

        contenders.sort(key=lambda x: -x[3])
        for bid, uid, u, urge in contenders:
            left = guard_left(bid)
            if left > 0:
                self.log(f"{bid} 全局护栏未过（还需 {left / 60:.0f} 分钟），本轮不发")
                continue
            why = self.gate_reason(bid, uid, u, now, bodies.get(bid))
            if why:
                self.log(f"念头到了但没说（{uid}）：{why}")
                continue
            sent, note = await self._speak(bid, uid, u, urge, now)
            self.log(f"{uid} urge={urge:.2f} → {note}")
            return

        for bid, uid, u, reason in closers:
            left = guard_left(bid)
            if left > 0:
                self.log(f"{bid} 全局护栏未过，收场也先不发")
                continue
            sent, note = await self._speak(
                bid,
                uid,
                u,
                0.0,
                now,
                preset=(reason, closer_meta(str(u.get("last_spoken_text", "") or ""))),
            )
            self.log(f"收场 {uid}（那句悬了 {(now - float(u.get('last_sent', 0) or 0)) / 3600.0:.1f} 小时）→ {note}")
            if sent:
                return

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

        # 用户级 Core 快照（复用调用方读到的 root 之外再读一次代价很小）
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
        bot = self.state.bot(bid)
        bot["last_global_send"] = sent_ts
        self.state.save()
        self.signals.note_proactive(uid, sent_ts)
        if len(parts) > 1:
            self._schedule_burst(bid, uid, parts[1], sent_ts)
            veto_note = "\n（稍后还会自然补一条）"
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
            return "还没有任何用户记录（没有人私聊过 bot），无法触发。可先和 bot 说句话再来试。"

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

    def _schedule_burst(self, bid: str, uid: str, text: str, sent_ts: float) -> None:
        """把连发的第二条挂成后台任务，不阻塞主循环与命令回显。"""
        task = asyncio.create_task(self._send_burst_followup(bid, uid, text, sent_ts))
        self._burst_tasks.add(task)
        task.add_done_callback(self._burst_tasks.discard)

    async def _send_burst_followup(self, bid: str, uid: str, text: str, sent_ts: float) -> None:
        """隔 1-3 分钟补发连发的第二条；期间对方发了新消息或插件停止则取消。"""
        try:
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
            if ok:
                self.state.record_outgoing(bid, uid, text, count_proactive=False)
                self.state.save()
                self.log(f"已连发补充给 {uid}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[autonomous_social] 连发补充失败: {e}")

    # ─── 后台循环 ───────────────────────────────────────

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
            name, _ = await self._persona(rep_umo)
            lines.append(f"  · {bid}：{name or '未取到人设（用插件默认口吻）'}")
        if len(bots) > 8:
            lines.append(f"  …另有 {len(bots) - 8} 个角色未列出")
        return "\n".join(lines)

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
            return "  还没有用户记录（没人私聊过 bot）"
        rows.sort(key=lambda x: -x[2])
        lines: List[str] = []
        for bid, uid, urge, interest, pend, seen in rows[:limit]:
            hours = (now - seen) / 3600.0 if seen else 0.0
            state = {"waiting": "等回复中", "replied": "接了话", "ignored": "没接话"}.get(pend, "")
            bar = "★想说" if urge >= desire.FIRE_THRESHOLD else ""
            lines.append(
                f"  · {uid}（{bid}）念头 {urge:.2f}/{desire.FIRE_THRESHOLD:.0f}"
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
        best: Optional[Tuple[float, str, str]] = None
        skip: Optional[Tuple[float, str, str]] = None
        for bid, bot in self.state.data.get("bots", {}).items():
            for uid, u in (bot.get("users") or {}).items():
                sent = float(u.get("last_sent", 0) or 0)
                if sent and (best is None or sent > best[0]):
                    text = ""
                    for m in reversed(u.get("conversation") or []):
                        if m.get("dir") == "out":
                            text = str(m.get("text", ""))[:40]
                            break
                    best = (sent, uid, text)
                skipped = float(u.get("last_skip_at", 0) or 0)
                if skipped and (skip is None or skipped > skip[0]):
                    skip = (skipped, uid, str(u.get("last_skip_reason", ""))[:60])
        lines: List[str] = []
        if best:
            lines.append(
                f"  上次主动找：{best[1]}（{int((now - best[0]) / 60)} 分钟前）「{best[2]}」"
            )
        if skip and (best is None or skip[0] > best[0]):
            lines.append(
                f"  上次想过没说：{skip[1]}（{int((now - skip[0]) / 60)} 分钟前）{skip[2]}"
            )
        return "\n".join(lines) if lines else "  还没有主动联系过任何人"

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
            f"护栏冷却：全局 {self.cfg.global_cooldown_minutes} 分钟 / 同一人 "
            f"{self.cfg.user_cooldown_minutes} 分钟\n"
            f"记录 bot：{len(bots)}（{bot_list}）\n"
            f"候选用户：{users}\n"
            f"主动消息发送/回复：{reply_rate}\n"
            f"回复窗口：{self.cfg.reply_window_hours}小时\n"
            f"连发：{'开' if self.cfg.allow_burst else '关'}　"
            f"emoji：{'保留' if self.cfg.allow_emoji else '去掉'}　"
            f"括号动作：{'去掉' if self.cfg.strip_roleplay_actions else '保留'}\n"
            f"念头面板：\n{self.urge_panel_text()}\n"
            f"最近联系：\n{self.last_contact_text()}"
        )