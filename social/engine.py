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

from . import __version__, anchors, anchors as anchor_mod, desire
from . import history_ingest
from .clock import city_now
from .config import SocialConfig
from .core_bridge import CoreBridge
from .sanitize import sanitize_incoming
from .anchors import anchor_meta, anchor_sentence, prepare_round_anchors
from .verify import (
    _bigrams,
    _check_cross_user,
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
    greet_reason,
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
)

# ─── 常量定义 ───────────────────────────────────────

# 上一轮结束后到下一轮开始，随机等这么久（一律秒）。心跳是「想起来看一眼手机」的时机，
# 它的粒度决定了所有分钟级配置的实际精度：心跳 15 分钟时，把 probe_after_minutes
# 从 2 调到 3 不会有任何区别。真正的发送节奏由 urge_refill_hours 与各种冷却决定，
# 心跳变快只是让「到点了」被发现的时机更准，不会凭空多发。
MIN_HEARTBEAT_SECONDS = 60      # 心跳间隔下限，防止有人把配置填成 0 变成忙循环
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
# 重试次数上限：每次重试都要跑一遍完整 LLM，不设上限就是持续烧 token。
# 与 expire_at 双保险——有效期管「多久」，次数管「多少次」
CUE_MAX_TRIES = 3

# 错误重试
# 主动消息对时效性不敏感，重试三次意义不大；而 try_once 是串行的：每次失败的发送要
# 2×8 秒的等待，一轮里遇到几个失败的人就能把整个心跳占住几十秒，期间所有用户都停摆。
# 宁可少试、快点失败——下一轮心跳本来还会再试。
SEND_RETRY_COUNT = 1
SEND_RETRY_DELAY = 3.0

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

# 被冷落得越多，两条消息之间拉得越开。这是「自然疏远」：真人被连着无视几次会自己
# 往后退，而不是靠一个每日条数上限硬掐。
GAP_STREAK_STRETCH = 0.6
GAP_STREAK_MAX = 3.0

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

# 手动触发最多试几个候选：第一个发不出去就换下一个（回退）
TRIGGER_CANDIDATE_LIMIT = 3

# 发送失败隔离这个人多久不再试
SEND_BLOCK_HOURS = 24
# 模型否决后的轻退避：推多久再试。由头不消耗，所以这只是「别每轮都问同一句」。
VETO_BACKOFF_SECONDS = 1800.0
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


def _num(value: Any) -> float:
    """容错取数。state.json 里的脏数据不该让正常流程抛异常。"""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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

# 连发：后续每条的补发间隔（秒）
BURST_DELAY_MIN = 60
BURST_DELAY_MAX = 180
# 群聊心流连发：同一个念头拆成几句快速补发（秒）——群里节奏快，不像私聊隔几分钟
GROUP_BURST_DELAY_MIN = 3
GROUP_BURST_DELAY_MAX = 9

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
        # 写回给 Humanoid Core 的信号（它替她主动开过口、被谁冷落了几次）。写自己目录下的
        # 单独文件，两边不会并发写同一个文件。
        self.signals = SignalsWriter(
            os.path.join(os.path.dirname(state_path), SIGNALS_FILE_NAME), time_source=self._time
        )
        self.generator = MessageGenerator(context, self.cfg, time_source=self._time)
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
        # ── 运行指标（只记数字与短原因，不含任何正文）────────────────────
        # 为什么要有：状态页原来只写「心跳：每 8~15 分钟一次」这种**静态说明**，
        # 于是「没触发」时根本分不清是没到时间、provider 取不到、用户被隔离、
        # 模型否决、平台失败，还是后台循环压根没跑。容器侧更没法看——插件自己的
        # debug 日志默认关着，AstrBot 的文件日志也可能没开。
        # 每角色「这一小时已经发了几个」。安全闸，防 180 人同时攒满时一次涌出。
        # 计数是滚动的一小时窗口（记时间戳列表），不是自然小时——跨零点不会归零重来。
        self._hourly: Dict[str, List[float]] = {}
        # 清洗前的原始模型输出：验收不过时记下来，下次能分辨
        # 「模型自己漏写了括号」还是「清洗/截断弄坏的」
        self._raw_generated = ""
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
            "blocked_reasons": {},      # 各闸门各拦下多少（近 N 轮滚动）
        }
        # 累计类指标从上次落盘的地方接上。`last_*` 那几个不接——它们本来就该是
        # 「最近一次」，接了会让人以为这一轮刚跑过。
        try:
            _prev = self.state.runtime_metrics()
        except Exception:
            _prev = {}
        for _key in ("rounds", "sent_total", "rejected_total", "rule_total",
                     "veto_total", "anchor_cross_user_total"):
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

    # ─── 消息观察 ───────────────────────────────────────

    async def observe(self, event: Any) -> None:
        """记录用户消息。只标记脏，不立即刷盘。

        Args:
            event: AstrBot 消息事件对象
        """
        if not self.cfg.enabled:
            return

        # 获取消息文本。**必须先清洗**：`message_str` 里可能夹着框架注入的内容
        # （Core 的「〔她的身体与生活 v10〕」事实块、<system_reminder>），
        # 原样存进 state 会被当成「对方说的话」喂回给模型，还会污染话题与时间锚点提取。
        msg = ""
        try:
            msg = sanitize_incoming(getattr(event, "message_str", ""))
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
        # 挡掉 bot 自己的消息。note_spoken 与群聊的 is_bot 都做了这个判断，只有私聊这条
        # 路漏了：适配器把自消息回灌进 EventMessageType.ALL 时（自消息回环、多端同步），
        # bot 会被建成一个「用户」，记下指向自己会话的 umo、攒念头、给自己私聊发「我好想你」
        if uid == bid:
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
        # 群消息同样可能带框架块（进了群参考库就会一直被当「这个群怎么说话」的样本）
        msg = sanitize_incoming(msg)
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
        is_quiet, asleep = self._flow_person_state(bid, now)
        ok, why = groupflow.flow_should_consider(
            g, now,
            max_replies=self.cfg.flow_max_replies_per_window,
            min_gap_seconds=self.cfg.flow_min_gap_seconds,
            hourly_cap=self.cfg.flow_hourly_cap,
            hour_count=self.state.flow_hour_count(g, now),
            ignored_exit=self.cfg.flow_ignored_exit,
            is_quiet=is_quiet,
            asleep=asleep,
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
        is_quiet, asleep = self._flow_person_state(bid, now)
        ok, why = groupflow.flow_should_consider(
            g, now,
            max_replies=self.cfg.flow_max_replies_per_window,
            min_gap_seconds=self.cfg.flow_min_gap_seconds,
            hourly_cap=self.cfg.flow_hourly_cap,
            hour_count=self.state.flow_hour_count(g, now),
            ignored_exit=self.cfg.flow_ignored_exit,
            is_quiet=is_quiet,
            asleep=asleep,
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
            if throttle.allow(f"flow.generate.{bid}"):
                logger.warning(
                    f"[autonomous_social] 群心流生成失败: {e}" + throttle.summary(f"flow.generate.{bid}")
                )
            return
        if not parts:
            self.log(f"群 {gid} 心流：模型选择不接或生成为空")
            return
        text = parts[0]
        ok_send, err, peer_unreachable = await self._send_with_retry(umo, text, bid)
        if not ok_send:
            if peer_unreachable:
                self.state.block_group(bid, gid, self._time() + SEND_BLOCK_HOURS * 3600.0, err)
                logger.warning(f"[autonomous_social] 群 {gid}（bid={bid}）发送失败，隔离 {SEND_BLOCK_HOURS}h：{err}")
            else:
                self.log(f"群 {gid} 心流发送失败：{err}")
            return
        sent_ts = self._time()
        self.state.note_flow_reply(
            bid, gid, sent_ts, text,
            sample_cap=self.cfg.group_ref_sample_size,
            store_text=self.cfg.group_store_message_text,
        )
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
            # 她在这个群自己说过的话：用不用 emoji 由此判定，不是配置开关
            "emoji": emoji_habit(
                [str(s.get("text", "") or "") for s in samples if s.get("self")]
            ),
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
        is_quiet = self._quiet_now(bid, dt.hour)
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
        if self.send_breaker_left(bid) > 0:
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
            if throttle.allow(f"icebreak.generate.{bid}"):
                logger.warning(
                    f"[autonomous_social] 群破冰生成失败: {e}" + throttle.summary(f"icebreak.generate.{bid}")
                )
            return False
        if not parts:
            return False
        text = parts[0]
        ok_send, err, peer_unreachable = await self._send_with_retry(umo, text, bid)
        if not ok_send:
            if peer_unreachable:
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
        # 记下 Core 算出的生物钟夜：本函数的 bot_state 由 try_once 传进来，
        # 安静时段要用它判（见 _quiet_now）
        self._remember_night(bid, bot_state)
        quiet = self._quiet_now(bid, dt.hour)
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
        """
        asleep = False
        try:
            body = self.core.bot_self_state(bid)
            self._remember_night(bid, body)
            asleep = bool((body or {}).get("asleep"))
        except Exception:
            body = None
        try:
            is_quiet = self._quiet_now(bid, self._moment_of(bid, now).hour)
        except Exception:
            is_quiet = False
        return is_quiet, asleep

    def _remember_night(self, bid: str, body: Optional[Dict[str, Any]]) -> None:
        """记下 Core 算出的生物钟夜。读不到就清掉，退回插件自己的配置。"""
        window = (body or {}).get("night_window") if isinstance(body, dict) else None
        key = str(bid or "")
        if not key:
            return
        try:
            if window and len(window) == 2:
                self._night_windows[key] = (float(window[0]) % 24.0, float(window[1]) % 24.0)
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

    def _count_block(self, reason: str) -> None:
        """记一次「本来要发但没发」。同一轮里反复撞同一道闸会重复计——"""
        cat = self._block_category(reason)
        # 用 `blocked_reasons` 这个已有字段：它从 v1.x 就声明在初始 metrics 里、
        # 注释写着「各闸门各拦下多少」，但**从来没有代码写过它**——一个空壳。
        # 这次把它填上，不再另起一个名字。
        table = self._m.setdefault("blocked_reasons", {})
        table[cat] = int(table.get(cat, 0) or 0) + 1

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
        if body and body.get("asleep"):
            return "她正在睡觉"
        # 「累」的闸不在这里，而在 `_speak`：那里才是所有开口的唯一收口。
        # 放这儿只挡住了有由头的那一路（实测 61 条里过了 57 条）。
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
        if body and body.get("asleep"):
            return "她正在睡觉"
        # 「刚聊过」也要拦。追问与「在吗」都是**接续型**——对方刚说完话就追一句
        # 是打断。对方一小时一条「在吗」，她每两小时（最小间隔一过）追问一次，
        # 7 天 44 次。
        if now - float(u.get("last_seen", 0) or 0) < self.cfg.recent_talk_minutes * 60:
            return "刚聊过，不追"
        last_said = self._last_said(u)
        if last_said > 0 and float(u.get("thread_for", 0) or 0) == last_said:
            return "这段沉默已经接过一回了，再问就是催"
        if now - float(u.get("thread_at", 0) or 0) < self.cfg.followup_cooldown_minutes * 60:
            return "刚接过一次，给 TA 点时间"
        if self._quiet_now(bid, self._moment_of(bid, now).hour):
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
                presence_floor_seconds=self.cfg.min_gap_minutes * 120.0,
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
        if body and body.get("asleep"):
            return "她正在睡觉"
        if kind != "night" and sleep_signal(u, now):
            return "TA 说过要去睡了，这时发过去不合适"
        if self._quiet_now(bid, self._moment_of(bid, now).hour):
            return "现在是安静时段"
        return ""

    def _miss_floor(self, tier: str) -> float:
        """「念想」这一类按关系档位各要多少好感。

        统一门槛（配置项 `miss_affection_min`，默认 55）当**中档**用；
        热的关系门槛更低，压根不熟的关系要更高。
        """
        base = float(getattr(self.cfg, "miss_affection_min", 55) or 55)
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
                aff = float(u.get("interest", 0.35) or 0.35) * 100.0
            except (TypeError, ValueError):
                aff = None
        tier = anchors.relation_tier(
            u, affection=aff, base_affection=base, first_met=first_met, now=now
        )
        warmth = (aff - base) if (aff is not None and base is not None) else None
        return tier, aff, warmth

    def _gap_left(self, u: Dict[str, Any], tier: str, now: float) -> float:
        """按关系档位的最小间隔。档位越高近得越理所当然——她常聊那个人。"""
        try:
            base = float(self.cfg.min_gap_minutes)
        except (TypeError, ValueError):
            base = 120.0
        minutes = float(anchors.TIER_MIN_GAP_MINUTES.get(tier, base))
        minutes = min(max(minutes, 1.0), 1440.0)
        try:
            streak = max(0, int(u.get("no_reply_streak", 0) or 0))
        except (TypeError, ValueError):
            streak = 0
        # 被冷落过就再拉远一点：真人不会一直追一个不回话的人
        minutes *= min(3.0, 1.0 + 0.4 * streak)
        last = float(u.get("last_sent", 0) or 0)
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
        """问候：没事干的时候随口说一句，看 TA 回不回。

        触发条件不是「到点了 + 今天还没问候过」——那是日历。真人打招呼看的是
        **距上次说上话多久了**，所以这里是纯静音时长驱动：隔了够久就可以说，
        一天说几次、说什么，由关系档和这句话当下的分量决定，不受「一天一次」的框。

        「她有多长时间没回我」记在 ``reply_gap_hours`` 里，每轮刷新，写进运行指标。
        """
        out: List[Tuple[str, Dict[str, Any], str]] = []
        dt = self._moment_of(bid, now)
        hour = dt.hour
        for uid, u in list(self.state.bot(bid).get("users", {}).items()):
            if not u.get("umo") or self.state.is_send_blocked(bid, uid, now):
                continue
            if self._quiet_now(bid, hour):
                continue
            if body and body.get("asleep"):
                continue
            # 只问候最近有来往的人：对素未谋面或早就不聊的人道早安，像定时群发
            if now - self._last_said(u) > GREET_FRESH_SECONDS:
                continue
            tier, _aff, _warmth = self._tier_of(bid, uid, u, core_root, now)
            if self._gap_left(u, tier, now) > 0:
                continue
            if self.greet_gate(bid, uid, u, now, body, kind=self._greet_kind(hour, u)):
                continue
            if self._quiesced(u):
                continue
            out.append((uid, u, self._greet_kind(hour, u)))
        return out

    def _greet_kind(self, hour: int, u: Dict[str, Any]) -> str:
        """这一刻该打招呼还是道晚安。按她的钟点，不看日历。"""
        return "night" if hour >= 20 or hour < 5 else "morning"

    @staticmethod
    def _quiesced(u: Dict[str, Any]) -> bool:
        """停发：连着几次没发出去就算了，除非 TA 主动说话。

        真人不会一直追一个不回话的人。解除条件只有一个——对方先开口。
        """
        try:
            seen = float(u.get("last_seen", 0) or 0)
            stopped = float(u.get("quiesced_at", 0) or 0)
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
        if body and body.get("asleep"):
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
                tier=tier, affection=affection, warmth=warmth,
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
            if body and body.get("asleep"):
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
            if str(u.get("miss_day", "") or "") == day:
                continue
            if body and body.get("asleep"):
                continue
            if self._quiet_now(bid, self._moment_of(bid, now).hour):
                continue
            if self._min_gap_left(u) > 0:
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
            if aff is None or aff < self._miss_floor(tier):
                continue
            try:
                picks = anchors.prepare_round_anchors(
                    u, {}, now, limit=1, affection=aff, relational=True
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
            for a in picks:
                if str(a.get("kind")) == anchors.KIND_MISS or "你" in str(a.get("about", "")):
                    out.append((uid, u, a))
                    break
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
                return float(aff)
            except (TypeError, ValueError):
                pass
        try:
            return float(u.get("interest", 0.35) or 0.35) * 100.0
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
            # 只在这个开关开着时才算：关着时（默认）引擎压根不用作息画像挑点，
            # 提示词里却写着「按对方作息这个点可能不在线」——和上一段
            # 「时机已经替你查过」直接打架，模型会被推着答 NO。
            "rhythm_off": (
                self.cfg.respect_user_rhythm
                and desire.rhythm_factor(u, self._moment_of(bid, now).hour) == desire.RHYTHM_OFF
            ),
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
                if (x.get("users") or {})]
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
        bots = [
            b for b, x in self.state.data.get("bots", {}).items()
            if (x.get("users") or {})
        ]
        if not bots:
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
                ready = self.settle_minds(bid, now, self._moment_of(bid, now), core_root, bot_state)
                # 只留下**手上真有由头**的。念头够不够由 settle_minds 管，
                # 但没有由头的人不进队——这就是「没什么可说的就别说话」。
                for uid, u, urge in ready:
                    picks = self._anchor_pick(u, bodies.get(bid), now)
                    if not picks:
                        self.log(f"{uid} 念头到了但没由头，不找话说")
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
            for bid in bots:
                self.signals.set_ignored_streak(
                    self.state.max_no_reply_streak(bid), bid
                )
            self.signals.set_role_count(len(bots))
            # 无条件重写：Core 那边靠 mtime 判信号是否过期（900 秒），而心跳默认
            # 8~15 分钟才一次，靠「内容没变就跳过」是撑不到那个阈值的。
            self.signals.beat()
        except Exception as e:
            self.log(f"写回社交信号失败: {e}")

        self._m["last_settled"] = now
        self._m["last_settled_users"] = settled_users
        self._m["last_failure"] = str(self._m.get("last_failure", "") or "")
        self._m["last_ready"] = len(anchored) + len(greets)
        self._m["last_candidate"] = (len(anchored) + len(greets) + len(threads)
                                     + len(loops) + len(promises) + len(closers)
                                     + len(misses))

        # ⚠️ 早退条件必须**逐个列出**所有候选桶。漏掉 misses 时，念想通道虽然被收集了
        # 却永远走不到发送段（它在最后面），表现是「好感 90% 的人一条都收不到」。
        if not greets and not anchored and not threads and not loops and not closers \
                and not promises and not misses:
            self.log("没有人攒够念头，本轮只是把时间补算上")
            # 没人可找时仍可能有冷群该破冰（破冰走群数据，不依赖 per-user 念头）
            if self.cfg.group_icebreak_enabled:
                await self._run_icebreaks(bots, now)
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

        async def speak(
            bid: str,
            uid: str,
            u: Dict[str, Any],
            urge: float,
            preset: Optional[Tuple[str, Dict[str, Any]]],
            note: str,
            require_about_peer: bool = False,
        ) -> bool:
            """发一条、记账、记账后稍隔几秒。发不出（被否决/失败）不耗配额。"""
            if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                return False
            # 锁在这一层，而不是每个调用点：`_speak` 里有 LLM 调用（秒级），这段时间里
            # `observe` 可以从另一个协程插进来改**同一个** user dict（收到新消息会清
            # urge、刷 last_seen、追加 conversation）。不锁的后果不是崩，是慢慢漂：
            # urge 少加一点、last_seen 被覆盖、conversation 顺序错乱。
            # 锁是 per-(bot,uid) 的，所以发 A 的时候不会把发 B 一起堵住。
            async with self._user_lock(bid, uid):
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
                sent, result = await self._speak(
                    bid, uid, u, urge, now, preset=preset,
                    require_about_peer=require_about_peer,
                )
            self.log(f"{note}{uid} → {result}")
            if not sent:
                # 模型自己否决的另记在 veto_total；这里记的是「这一条最终没发出去」。
                # 两者会重叠，但方向不同：一个说「她不想说」，一个说「没发成」。
                self._count_block(result)
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
        # 问候：没有正事，随口说一句，看 TA 回不回。
        #
        # 触发不再是「到点了 + 今天还没问候过」——那是日历，真人打招呼看的是
        # **距上次说上话多久了**。所以这里纯静音时长驱动，隔了够久就能说，一天说几次
        # 由关系档和这句话当下的分量决定。
        for bid, uid, u, kind, _reason in greets:
            if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                continue
            tier, aff, warmth = self._tier_of(bid, uid, u, core_root, now)
            picks = self._anchor_pick(
                u, bodies.get(bid), now, limit=1, tier=tier, affection=aff, warmth=warmth
            )
            if not picks:
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
            if await speak(
                bid, uid, u, 0.0, (anchor_sentence(a), meta), f"问候（{label}） "
            ):
                served.add((bid, uid))

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

        # 轮转：先按「最久没联系的优先」，念头只做同分次序。
        #
        # 原来是按念头降序。真实记录里 129 条有 100 多条落在同样 6 个人身上
        # （2047181070 收了 3 次、2163874801 收 3 次、3231002996 收 3 次…），
        # 另外 166 个人一条没收到。原因：**不回复的人的念头会一路涨到封顶 2.80，
        # 每轮都排第一**；会回复的人发完就清零，永远排在后面。插件等于只在跟
        # 从不回复的人说话。按 last_sent 升序就能把它摊开。
        anchored.sort(key=lambda x: (float(x[2].get("last_sent", 0) or 0), -x[3]))
        for bid, uid, u, urge, a in anchored:
            if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                continue
            why = self.gate_reason(bid, uid, u, now, bodies.get(bid))
            if why:
                self._count_block(why)
                self.log(f"有由头但没说（{uid}）：{why}")
                continue
            await speak(
                bid, uid, u, urge,
                (anchor_sentence(a), anchor_meta(a)),
                f"因为{a.get('about')} urge={urge:.2f} ",
            )

        # 兑现承诺排在「另起话题」之后：她是自己答应过这件事的，理由比随口找个话题足。
        for bid, uid, u, about in promises:
            if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                continue
            await speak(
                bid, uid, u, 0.0, (f"该兑现「{about}」了", promise_meta(about)),
                f"兑现承诺「{about}」 ",
            )

        # 念想型：没有正事可说时不占「有正事」的优先级，但也别排到最后——
        # 它本来就是「闲下来想起一个人」的状态。
        for bid, uid, u, a in misses:
            if budget.get(bid, 0) <= 0 or (bid, uid) in served:
                continue
            if await speak(
                bid, uid, u, 0.0,
                (anchor_sentence(a), anchor_meta(a)),
                f"念想「{a.get('about')}」 ",
                require_about_peer=True,
            ):
                # 每天只有这么几条，记上日子
                st_u = self.state.user(bid, uid)
                st_u["miss_day"] = self._moment_of(bid, self._time()).strftime("%Y-%m-%d")
                self.state.mark_dirty()

        for bid, uid, u, reason in closers:
            if budget.get(bid, 0) <= 0:
                continue
            hung = (now - float(u.get("last_sent", 0) or 0)) / 3600.0
            # 已读续接：对方回过（那句回话走的是正常聊天链路，不是插件发的）之后又
            # 没声了，这时候补的一句该**接着那几句说**。没有素材时退回老做法。
            await speak(
                bid, uid, u, 0.0,
                (
                    reason,
                    closer_meta(
                        str(u.get("last_spoken_text", "") or ""),
                        recent_exchange(u, 3),
                    ),
                ),
                f"收场（那句悬了 {hung:.1f} 小时）",
            )

        # 运行指标刷盘：容器侧不用开日志就能从 state.json 的 _runtime 段判断
        # 「她到底跑起来没、卡在哪一步」。只存数字与短标签。
        self._persist_metrics()

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
        require_about_peer: bool = False,
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
        try:
            energy = float((body or {}).get("energy"))
        except (TypeError, ValueError):
            energy = 100.0
        if energy < self.cfg.gate_energy_floor:
            self.log(f"没发（{uid}）：她太累了（精力 {energy:.0f}）")
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
        gap_left = self._gap_left(u, _tier, self._time())
        if gap_left > 0 and not self._urge_is_eager(u):
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
        fail_left = float(u.get("send_fail_until", 0) or 0) - self._time()
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
                    core_context = self.core.compact(user_core)
        except Exception as e:
            self.log(f"加载用户 {uid} Core 快照失败: {e}")

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
                msgs = int(u.get("message_count", 0) or 0)
            except (TypeError, ValueError):
                msgs = 0
            blended = (
                AFFECTION_INTEREST_WEIGHT * float(u.get("interest", 0.35) or 0.35)
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
        # 接了 Core v2.14 的契约时这里有完整的身体：困不困、饿不饿、想说话的程度，
        # 以及她此刻正手上的事。拿不到契约时为空，生成器会退回旧的两个标量。
        u_copy["_body"] = user_core if (user_core or {}).get("contract_v") else None
        # 「记仇」的三道限在这里记账，生成器只读 target（它是 u 的副本，改不回去）
        self._track_aggression(u, user_core, now)
        u_copy["_aggr_high_n"] = int(u.get("aggr_high_n", 0) or 0)
        u_copy["_aggr_recently_acted"] = bool(
            now - float(u.get("aggr_note_at", 0) or 0) < AGGR_NOTE_COOLDOWN
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

        thread_anchor = self._last_said(u)
        parts: Optional[List[str]] = None
        veto_note = ""
        # 承诺只从 decide 的结构化输出里取（generate 路径没有那行 >），所以两条分支
        # 都得有这个变量——原来只在 llm_gate 分支里定义，关掉闸门时下面读它会 NameError。
        decision = None
        if allow_veto and self.cfg.llm_gate:
            try:
                decision = await self.generator.decide(
                    umo, u_copy, core_context, reason, reason_meta, persona_prompt, mind,
                    at=now, clock_offset=self._clock_offset_for(bid),
                )
            except Exception as e:
                if throttle.allow(f"speak.decide.{bid}"):
                    logger.warning(
                        f"[autonomous_social] 判断该不该说时出错: {e}"
                        + throttle.summary(f"speak.decide.{bid}")
                    )
            self._raw_generated = str(getattr(decision, "raw_text", "") or "")
            if decision is None:
                # LLM 不可用/退避中/超时：这是基础设施故障，不是「模型觉得不该说」。
                # 两者走同一条 defer_cue 会把一个真实的由头（如「明天面试」）静默作废：
                # 一次持续十几小时的 key 失效就够把 3 次重试预算全烧光。
                note = "LLM 不可用（provider 取不到或调用失败），本轮未发送"
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
                # 轻退避：把「多久之后可以再试」往后挪一点，但**不动寿命**。
                # 以前是走 defer_cue 去推 retry_at，而那条只处理有由头的路径；纯念头
                # 否决时反而什么都没记，于是同一条由头每轮心跳重试一次——仿真里 7 天
                # 626 次调用就是这么来的。
                u["retry_at"] = max(
                    float(u.get("retry_at", 0) or 0), now + VETO_BACKOFF_SECONDS
                )
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
        if self._time() - float(latest.get("last_seen", 0)) < FINAL_CHECK_GAP_SECONDS:
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
                    self._m["last_failure"] = (
                        f"[{bid}]「{_about[:16]}」刚发给过别人了，换一个"
                    )
                    # 换个由头再试一次，而不是直接放弃这一轮
                    self.state.drop_anchor(u, _about)
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
        if not ok:
            self._m["last_failure"] = f"[{bid}] 验收不过：{why}"
            self._m["last_rejected"] = (self._raw_generated or parts[0])[:200]
            self._m["rejected_total"] = int(self._m.get("rejected_total", 0)) + 1
            self._m["rule_total"] = int(self._m.get("rule_total", 0)) + 1
            if throttle.allow("verify.rejected", window=1800.0):
                logger.info(
                    f"[autonomous_social][{bid}] 验收不过，未发送：{why}｜"
                    f"原文：{(self._raw_generated or parts[0])[:120]!r}"
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
        self._m["rejected_total"] = int(self._m.get("rejected_total", 0))

        # 由头在**真的发出去**这一刻才消费。前面任何一步失败都不算——
        # 一次模型抽风不该让这个话题冷掉 7 天。
        sent_ok, err, peer_unreachable = await self._send_with_retry(umo, parts[0], bid)
        if not sent_ok:
            # 发送失败是可以重试的（换一轮心跳、换个时间再来），但不该拿掉这件事
            # 本身——所以推的是 retry_at，寿命 expire_at 不动。
            self._defer_cue_only(u, reason_meta)
            self._mark_send_failed(u)
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
                max(0.0, sent_ts - float(u.get("last_seen", 0) or 0)) / 3600.0, 1
            )
        except (TypeError, ValueError):
            pass
        _a = (reason_meta or {}).get("anchor")
        if isinstance(_a, dict) and _a.get("about"):
            anchor_mod.consume(u, str(_a.get("about")), sent_ts)
        self._hourly_take(bid, sent_ts)
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
            kept = [
                i for i in promise_entries(u)
                if str(i.get("about", "")).strip() != str((reason_meta or {}).get("about") or "").strip()
            ]
            u["promises"] = kept
        # 问候与收场本来就不要求对方回：它们不进「等一句回话」的计时器，
        # 否则「从来没回过早安」会被当成「连着被冷落」，念头天花板永久压死，
        # 而衰减的锚点（last_sent）又每天被问候刷新，永远等不到。
        _sent_about = str((reason_meta or {}).get("about") or "")
        if not _sent_about:
            _a = (reason_meta or {}).get("anchor")
            if isinstance(_a, dict):
                _sent_about = str(_a.get("about") or "")
        self.state.record_outgoing(
            bid, uid, parts[0], msg_type,
            about=_sent_about,
            expect_reply=category not in ("greet", "closer"),
            why=self._why_now(bid, u, category, reason, reason_meta, sent_ts),
        )
        # 发成功了，之前那次失败就不再约束他
        u["send_fail_count"] = 0
        u["send_fail_until"] = 0.0
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
        elif category == "cue" and u.get("cue_due") and float(u["cue_due"]) <= sent_ts:
            # 只有「因为想起这件具体的事才开口」才消费它。问候排在候选最前面，
            # 一句早安把「TA 说明天面试」吃掉、那件事再也没人问，就是这么来的。
            u["cue"] = ""
            u["cue_due"] = 0.0
            u["cue_expire_at"] = 0.0
            u["cue_retry_at"] = 0.0
            u["cue_tries"] = 0
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
        self.signals.note_proactive(uid, sent_ts, bid)
        if len(parts) > 1:
            self._schedule_burst(bid, uid, parts[1:], sent_ts)
            veto_note = f"\n（稍后还会自然补 {len(parts) - 1} 条）"
        self._last_llm_error.pop(bid, None)
        return True, (
            f"已主动联系 {uid}（类型={msg_type}，念头={urge:.2f}）：\n{parts[0]}{veto_note}"
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
            prev = int(u.get("aggr_high_n", 0) or 0)
        except (TypeError, ValueError):
            prev = 0
        n = prev + 1 if value >= AGGR_NOTE_THRESHOLD else 0
        if n != prev:
            u["aggr_high_n"] = n
            self.state.mark_dirty()
        if n >= AGGR_NOTE_SAMPLES and now - float(u.get("aggr_note_at", 0) or 0) >= AGGR_NOTE_COOLDOWN:
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
            urge = float(u.get("urge", 0.0) or 0.0)
        except (TypeError, ValueError):
            urge = 0.0
        if urge > 0:
            try:
                rate = 0.0
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
        elif category.startswith("greet_"):
            kind = {"greet_morning": "早安", "greet_night": "晚安", "greet_midday": "午间招呼"}
            bits.append(f"{kind.get(category, '问候')}窗口")
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
        if cap <= 0:
            return 999999
        window = [ts for ts in self._hourly.get(bid, []) if now - ts < 3600.0]
        self._hourly[bid] = window
        return max(0, cap - len(window))

    def _hourly_take(self, bid: str, now: float) -> None:
        try:
            self._hourly.setdefault(bid, []).append(now)
        except Exception:
            pass

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
            urge = float(u.get("urge", 0.0) or 0.0)
            gate = float(u.get("fire_gate") or desire.FIRE_THRESHOLD)
        except (TypeError, ValueError):
            return False
        return urge >= max(gate, desire.FIRE_THRESHOLD) * EAGER_URGE_RATIO

    def _defer_cue_only(
        self, u: Dict[str, Any], reason_meta: Optional[Dict[str, Any]]
    ) -> None:
        """由头/挂着的这件事这次没说成：只推重试节奏，不动寿命。"""
        category = str((reason_meta or {}).get("category") or "")
        if category == "cue":
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
                float(u.get("last_seen", 0) or 0),
                float(u.get("last_sent", 0) or 0),
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
            seen_gap = now - float(u.get("last_seen", 0) or 0)
            if seen_gap < HOT_CHAT_THRESHOLD:
                tried.append(
                    f"{uid} 正在热聊（{int(seen_gap / 60)} 分钟前还在说话），跳过"
                )
                continue
            try:
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
            if MessageChain is not None:
                try:
                    message_chain = MessageChain().message(text)
                    ok = await self.context.send_message(target, message_chain)
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
                except Exception as e:
                    # 其余异常（多半是 MessageChain 构造失败）才是真的没发出去，可以回退
                    logger.warning(
                        f"[autonomous_social] MessageChain 发送未成功，回退纯文本: {e}"
                    )
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
                raise _SendFailed(
                    f"context.send_message 调用失败: {e}",
                    peer_unreachable=self._is_unreachable_error(str(e)),
                ) from e

        # 方式2: 通过 platform 客户端发送（旧版兼容）
        if hasattr(self.context, "platform") and hasattr(self.context.platform, "send_message"):
            await self.context.platform.send_message(target, text)
            return

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
                    if throttle.allow(f"send.unknown.{bid or '-'}"):
                        logger.warning(
                            f"[autonomous_social] 发送结果未知（不重试，避免重复投递）: {e}"
                            + throttle.summary(f"send.unknown.{bid or '-'}")
                        )
                    self._note_send_failure(bid, last_err, peer_unreachable)
                    return False, last_err, False
            except Exception as e:
                last_err = str(e)
            if attempt < SEND_RETRY_COUNT:
                if throttle.allow(f"send.retry.{bid or '-'}"):
                    logger.warning(
                        f"[autonomous_social] 发送失败（第{attempt + 1}次），稍后重试: {last_err}"
                        + throttle.summary(f"send.retry.{bid or '-'}")
                    )
                await asyncio.sleep(SEND_RETRY_DELAY)
            elif throttle.allow(f"send.failed.{bid or '-'}"):
                logger.warning(
                    f"[autonomous_social] 发送失败（已重试{SEND_RETRY_COUNT}次）: {last_err}"
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
        """连续发不出去就把生成也停下来：写好的消息送不出去，那次调用就是纯浪费。"""
        if peer_unreachable:
            # 平台明说「这个人发不了」：是这一个用户的事，不该把所有人一起停掉
            return
        slot = self._breaker_slot(bid)
        slot["streak"] = int(slot.get("streak", 0)) + 1
        slot["err"] = str(err or "")
        if slot["streak"] < SEND_BREAKER_TRIP:
            return
        delay = min(
            SEND_BREAKER_BASE_SECONDS * (2 ** (slot["streak"] - SEND_BREAKER_TRIP)),
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
            n = int(u.get("giveup_count", 0) or 0) + 1
        except (TypeError, ValueError):
            n = 1
        u["giveup_count"] = n
        if n >= GIVEEUP_TRIES:
            u["quiesced_at"] = now
            self.log(f"{u.get('__uid', '')} 连着 {n} 次发不出去，先不主动找 TA 了（等 TA 先开口）")

    def _mark_send_failed(self, u: Dict[str, Any]) -> None:
        """这个人的发送刚失败过：往后推一段，别下一轮心跳又给他写一遍。"""
        try:
            n = int(u.get("send_fail_count", 0) or 0) + 1
        except (TypeError, ValueError):
            n = 1
        delay = min(
            SEND_FAIL_COOLDOWN_BASE * (2 ** (n - 1)), SEND_FAIL_COOLDOWN_MAX
        )
        u["send_fail_count"] = n
        u["send_fail_until"] = self._time() + delay
        self.state.mark_dirty()

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
                if self.send_breaker_left(bid) > 0:
                    return
                u = self.state.user(bid, uid)
                if float(u.get("last_seen", 0)) > sent_ts:
                    self.log(f"用户 {uid} 已有新消息，取消连发补充")
                    return
                umo = str(u.get("umo", "") or "")
                if not umo:
                    return
                ok, _, _ = await self._send_with_retry(umo, text, bid)
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
                if self.send_breaker_left(bid) > 0:
                    return
                now = self._time()
                if self.state.is_group_blocked(bid, gid, now):
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
                    hot += 1 if float(u.get("urge", 0.0) or 0.0) >= desire.FIRE_THRESHOLD else 0
                    last_send = max(last_send, float(u.get("last_sent", 0) or 0))
                    streak = max(streak, int(u.get("no_reply_streak", 0) or 0))
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
        since_hb = now - float(last_run)
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
        vetoed = int(m.get("veto_total", 0) or 0)
        if vetoed:
            lines.append(
                f"模型否决：累计 {vetoed} 次（她说此刻不该找这个人——由头留着，"
                f"下轮状态变了还可以说）"
            )
        rejected = int(m.get("rejected_total", 0) or 0)
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
            total_blocked = sum(int(v or 0) for v in blocked.values())
            lines.append(f"没发出去的去向（累计 {total_blocked} 次）：")
            for cat, cnt in sorted(blocked.items(), key=lambda kv: -int(kv[1] or 0))[:8]:
                lines.append(f"  {cat}：{int(cnt)} 次")
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
        cleaned = int(self.state.data.get("_sanitized_users", 0) or 0)
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
                why = str(e.get("why", "") or "").strip()
                lines.append(f"    {when}  {text}" + (f"\n              ↳ {why}" if why else ""))
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
            f"运行指标：\n{self._metrics_text()}\n"
            f"写回 Core：{self._signals_status_text()}\n"
            f"LLM 生成：{self._llm_status_text()}\n"
            f"发送通道：{self._send_status_text()}\n"
            f"心跳：每 {self.cfg.heartbeat_min_minutes}~{self.cfg.heartbeat_max_minutes} 分钟一次"
            f"（分钟级配置的实际精度就是它）\n"
            f"节奏：念头驱动（最在意的人约 {self.cfg.urge_refill_hours} 小时攒满一次；"
            f"刚聊完 {self.cfg.recent_talk_minutes} 分钟内不另起）\n"
            f"发送前把关：{'模型判断该不该说' if self.cfg.llm_gate else '仅规则闸门'}\n"
            f"时机：{'按对方作息挑点' if self.cfg.respect_user_rhythm else '不看作息'}"
            f"、{'记得对方说的约定' if self.cfg.cue_followup else '不跟由头'}\n"
            f"活跃度：{self.cfg.activity_level}（整体想说话 ×{self.cfg.mood_scale:.2f}）\n"
            f"当前时段：{slot_names.get(slot, '未知')} {dt.strftime('%H:%M')}"
            f"{'（安静时段，念头长得极慢）' if self._quiet_now(ref_bid, dt.hour) else ''}"
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
            + "括号动作/emoji：看她自己的说话习惯\n"
            + f"各角色：\n{self._per_bot_status_text()}\n"
            + f"念头面板：\n{self.urge_panel_text()}\n"
            + f"最近联系：\n{self.last_contact_text()}"
        )