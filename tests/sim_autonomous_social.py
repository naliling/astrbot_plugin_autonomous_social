"""自主拟人社交的使用体验仿真。

    python3 tests/sim_autonomous_social.py            # 跑全部场景
    python3 tests/sim_autonomous_social.py --prompts  # 额外打印真实 prompt 全文

为什么要有这个
------------
回归测试只能证明「某个函数返回了某个值」。这个插件真正会坏的地方全在**跑起来
才发作**的那一层：故障时一次逻辑调用会不会放大成五次网络请求、平台发不出去时
还会不会先把消息生成出来、日志会不会每 30 秒刷一条、连着几小时反复写同一条
发不出去的消息。

所以这里不是 mock 一个函数，而是把整套跑起来：假 LLM（能返回正常/NO/超时/异常/
鉴权失败/协议乱写/带❤️）、假平台（成功/平台未就绪/对方不可达/超时未知）、模拟
时钟连跑若干天，逐轮统计**请求放大倍数、日志条数、真正发出去的消息**，
并把 prompt 原文打出来人眼看。

看的是使用体验，不是绿灯。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import shutil
import sys
import tempfile
import datetime
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "astrabot_plugin_autonomous_social"


# ─── 最小 astrbot 桩 ──────────────────────────────────────────────
class _MessageChain:
    def __init__(self):
        self.parts: List[str] = []

    def message(self, text):
        self.parts.append(text)
        return self

    def plain(self, text):
        self.parts.append(text)
        return self


def _install_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("sim")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = _MessageChain
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT))

# ⚠️ 换插件版本做 A/B 时，这条 import 是**绑定**的：
# 下面 `from social.engine import SocialEngine` 在此刻就把类抓进了本模块的命名空间。
# 之后再改 sys.path、再删 sys.modules['social.*'] 都换不掉已经拿到的那个类——
# 实测两次「跑不同版本」的数字逐字节相同，因为两次跑的都是本文件所在目录旁边那份。
# 要真的换版本，必须让 `PLUGIN_ROOT` 指向另一份**在 import 之前**，也就是把整个
# tests/ 目录复制到那份插件旁边再跑；或者接受「同一份代码里只切换一个变量」的 A/B。

from astrbot.api import logger as sim_logger  # noqa: E402

from social import engine as engine_mod  # noqa: E402
from social.config import SocialConfig  # noqa: E402
from social.engine import SocialEngine  # noqa: E402

random.seed(20260927)

# 发送失败后的重试等待是真实 sleep（默认 3 秒）。仿真里等的是墙钟，等下去不产生任何
# 额外信息，只会把一次仿真拖成几分钟——语义不受影响，所以直接置 0。
engine_mod.SEND_RETRY_DELAY = 0.0


# ─── 记录器 ───────────────────────────────────────────────────────
class LogCapture(logging.Handler):
    """抓全部日志，按级别与关键词统计。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: List[str] = []
        self.by_level: Dict[str, int] = {}

    def emit(self, record):
        self.records.append(record.getMessage())
        self.by_level[record.levelname] = self.by_level.get(record.levelname, 0) + 1

    def reset(self):
        self.records.clear()
        self.by_level = {}


CAPTURE = LogCapture()
sim_logger.addHandler(CAPTURE)
sim_logger.setLevel(logging.WARNING)
sim_logger.propagate = False


# ─── 假 LLM ───────────────────────────────────────────────────────
class FakeLLM:
    """按剧本返回的假 provider。会记下每一次**真实请求**。

    剧本是一个可调用对象：给它 prompt，返回 str（正常）或抛异常/返回错对象。
    """

    def __init__(self, script, tag="llm"):
        self.id = "fake-provider"
        self.meta = types.SimpleNamespace(id=self.id)
        self._script = script
        self.calls: List[str] = []          # 每次调用的 prompt 全文
        self.tag = tag

    async def text_chat(self, prompt=None, **kw):
        self.calls.append(prompt or "")
        outcome = self._script(prompt or "", len(self.calls))
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, str):
            return _Resp(outcome)
        return outcome

    # 另两种「也存在于 provider 上」的方法：旧实现会挨个试过去，
    # 现在应该一次都不被调到。它们一旦被调就说明回退链又漏了。
    async def chat(self, prompt, **kw):
        self.calls.append("[chat]" + (prompt or ""))
        return _Resp("chat 路被调用了——回退链漏了")

    async def generate(self, prompt, **kw):
        self.calls.append("[generate]" + (prompt or ""))
        return "generate 路被调用了——回退链漏了"


class _Resp:
    def __init__(self, text="", role="assistant"):
        self.completion_text = text
        self.role = role


# ─── 假平台 ───────────────────────────────────────────────────────
class FakePlatform:
    """按剧本决定每次发送的结果。"""

    OK = "ok"
    PLATFORM_DOWN = "platform_down"
    PEER_GONE = "peer_gone"
    TIMEOUT = "timeout"

    def __init__(self, script, now=None):
        self._script = script
        self._now = now or (lambda: 0.0)
        self.sent: List[Dict[str, str]] = []
        self.attempts = 0
        self.failures = 0

    async def send_message(self, target, chain, **kw):
        self.attempts += 1
        text = "".join(getattr(chain, "parts", []) or [str(chain)])
        mode = self._script(target, text, self.attempts)
        if mode == self.OK:
            self.sent.append({"target": target, "text": text, "at": self._now()})
            return True
        self.failures += 1
        if mode == self.PLATFORM_DOWN:
            raise engine_mod._SendFailed(
                "未找到匹配的会话平台（该平台不支持主动消息）"
            )
        if mode == self.PEER_GONE:
            raise engine_mod._SendFailed("对方不是你的好友，请添加对方为好友", peer_unreachable=True)
        if mode == self.TIMEOUT:
            raise engine_mod._SendFailed("connection reset by peer", unknown=True)
        raise RuntimeError("未预期的发送失败")


# ─── 假 Core 状态 ─────────────────────────────────────────────────
def write_core_state(path: Path, persona="小夜", nickname="阿澈", said=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    now = 1_700_000_000.0
    state = {
        "roles": {
            "bot1": {
                "self": {
                    "energy": 62.0,
                    "social_energy": 55.0,
                    "current_cycle_day": 3,
                    "daily_schedule": [],
                    "contract": {
                        "v": 1,
                        "core_version": "2.23.3",
                        "time": {"utc_offset_minutes": 480, "city": "上海", "is_night": False},
                        "body": {
                            "energy": 62.0,
                            "energy_text": "精神还不错",
                            "sleep_pressure": 30.0,
                            "sleep_debt": 0.0,
                            "hunger": 40.0,
                            "discomfort": 20.0,
                            "arousal": 35.0,
                            "asleep": False,
                            "last_sleep_hours": 7.5,
                            "social_energy": 55.0,
                            "social_desire": 48.0,
                            "cycle_day": 3,
                            "cycle_phase": "排卵期",
                        },
                        "feelings": ["手上正在改方案", "有点想吃甜的"],
                        "form": {
                            "max_chars": 90,
                            "question_bias": 0.3,
                            "long_reply_ok": True,
                            "burst_ok": True,
                        },
                        "activity": {"name": "改方案", "phase": "专注", "schedule_event": "改方案", "location": "家"},
                        "day": {"doing": "改方案", "done": ["取快递"], "next": ["晚饭"]},
                        "persona": persona,
                        "routine": {"night_start_hour": 23.0, "night_end_hour": 7.0, "night_source": "schedule"},
                        "weather": "外面在下雨",
                    },
                },
                "users": {
                    "u1": {
                        "mood": {"affection": 78.0, "libido": 20.0, "aggression": 5.0},
                        "nickname": nickname,
                        "nickname_src": "self_report",
                        "mood_tag": "有点累",
                        "attention": {"care": 72.0, "care_at": now},
                        "last_message": {"text": "今天好累啊", "timestamp": now},
                        "last_interaction": now,
                        "said": said or [{"said": "我下周要面试", "at": now - 86400}],
                    }
                },
            }
        }
    }
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return path


# ─── 假 context ───────────────────────────────────────────────────
class FakeContext:
    def __init__(self, provider, platform, persona_prompt="你是小夜，说话简短随意。"):
        self._provider = provider
        self.platform = platform          # engine 优先用 context.send_message
        self._persona_prompt = persona_prompt
        self.persona_manager = _PersonaManager(persona_prompt)

    async def get_using_provider_async(self, umo=None):
        return self._provider

    async def send_message(self, target, message, **kw):
        # 引擎走的是 context.send_message(umo, MessageChain)
        return await self.platform.send_message(target, message, **kw)


class _PersonaManager:
    def __init__(self, prompt):
        self._prompt = prompt

    async def resolve_selected_persona(self, umo=None, **kw):
        return ("小夜", {"name": "小夜", "prompt": self._prompt}, None, False)

    async def get_default_persona_v3(self, umo=None):
        return {"name": "小夜", "prompt": self._prompt}


# ─── 场景 ─────────────────────────────────────────────────────────
def script_normal(prompt: str, n: int):
    """正常：多数时候 SEND，偶尔 NO。

    **要照提示词走**：提示词里出现「念想」那一段时（说明这次是要说给 TA 听），
    台词就得有第二人称。真实模型会照做，假模型不会——不补这一条，验收层的
    「必须指向对方」就会把每一次念想都拦下，测出来的「0 条」是假的。
    """
    if n % 4 == 0:
        return "NO\n刚聊过，没什么想说的"
    if "你不是有事，就是想起了这个人" in (prompt or ""):
        peer = [
            "你今天忙到几点啊",
            "有点想你了，你在干嘛",
            "你上次说累，后来好点没",
            "要不要一起吃个饭",
            "刚看到个东西就想到你了",
        ]
        return f"SEND\n#想到你了\n{peer[n % len(peer)]}"
    # 台词池要**足够大且每次不同**。原来只有 5 句，来回用会被验收层的复读判据
    # 全拦掉——那测的是「假模型复读」，不是插件行为。
    lines = [
        "刚改完方案脑子有点木",
        "楼下便利店的关东煮今天有蟹棒诶",
        "你那个面试改到哪一步了",
        "我今天效率好低，一半时间在发呆",
        "刚睡醒，人还是懵的",
        "刚把周报交了，松口气",
        "今天这咖啡不太行，太淡了",
        "刚下班，路上那排银杏黄了一半",
        "嗯，刚忙完手头的事",
        "我今天学会了一道新菜，明天做给你",
        "刚发完呆，回过神来天都黑了",
        "你猜我今天几点起的",
        "刚收拾完桌子，屋子终于像个样子了",
        "这雨下了一整天，窗户都是雾的",
    ]
    body = lines[n % len(lines)]
    if n % 11 == 0:
        body = f"{body}（第{n}次说这句）"
    if n % 7 == 0:
        body = "(把手机扣在桌上)\n" + body
    return f"SEND\n#改方案\n{body}"


def script_bad_key(prompt: str, n: int):
    # 真实的 provider 报错会带上可判定的理由（401 / invalid api key / 额度不足），
    # 插件据此决定「这类失败等再久也一样」。这里不写理由的话就算另一条路径了。
    return _Resp("Error code: 401 - invalid api key", role="err")


def script_timeout(prompt: str, n: int):
    return RuntimeError("读超时")


def script_garbage(prompt: str, n: int):
    return "我觉得现在不太合适，还是算了吧"


def script_emoji_persona(prompt: str, n: int):
    """一个本来就爱用 emoji 的人格。

    台词也要有足够变化，否则测的是「假模型复读」而不是插件行为。
    """
    if n % 5 == 0:
        return "NO\n没什么想说的"
    bodies = [
        "刚下班路上看到只很胖的橘猫，它一直盯着我看✨",
        "今天的云特别好看，像谁打翻的棉花糖☁️",
        "刚买的奶茶好喝到跺脚🧋",
        "楼下那只狗又冲我摇尾巴了🐕",
        "刚洗完澡整个人都活过来了🛁",
        "楼下的桂花开了，走过去一路都是香的🌼",
    ]
    return f"SEND\n#下班路上\n{bodies[n % len(bodies)]}"


class Sim:
    """一次仿真：建环境、跑若干天心跳、出报告。"""

    def __init__(self, name, *, llm_script, send_script, days=3, users=("u1",),
                 persona="小夜", nickname="阿澈", said=None, verbose=False,
                 own_history=None, persona_prompt="你是小夜，说话简短随意。"):
        self.name = name
        self.days = days
        self.verbose = verbose
        self.tmp = Path(tempfile.mkdtemp(prefix="socialsim-"))
        self.core_path = self.tmp / "core" / "state.json"
        write_core_state(self.core_path, persona=persona, nickname=nickname, said=said)

        self.provider = FakeLLM(llm_script, tag=name)
        # send_script 可以是一个固定档位（FakePlatform.OK），也可以是按次决策的函数
        self.platform = FakePlatform(
            send_script if callable(send_script) else (lambda *a, _m=send_script: _m),
            now=lambda: self.clock,
        )
        self.ctx = FakeContext(self.provider, self.platform, persona_prompt)
        self.clock = 1_700_000_000.0
        self.state_path = self.tmp / "state.json"
        self.own_history = own_history or []

        cfg = SocialConfig()
        self.engine = SocialEngine(
            context=self.ctx, config=cfg, state_path=str(self.state_path),
            data_dir=str(self.tmp), time_source=lambda: self.clock,
        )
        self.engine._seed_bootstrap = None
        # 关掉历史导入/播种：仿真里我们要的就是干净可控的初始状态
        cfg.history_ingest = False
        cfg.seed_users = []
        cfg.group_flow_enabled = False
        cfg.group_icebreak_enabled = False
        cfg.group_ref_lib_enabled = False
        for uid in users:
            self._add_user(uid)

    def _add_user(self, uid):
        bot = self.engine.state.bot("bot1")
        bot.setdefault("users", {})[uid] = {
            "umo": f"aiocqhttp:FriendMessage:{uid}",
            "last_seen": self.clock - 3 * 86400,
            "last_sent": 0.0,
            "last_spoken": 0.0,
            "last_message": "今天好累啊",
            "last_spoken_text": "你那个面试后来怎么样了",
            "interest": 0.6,
            "urge": 0.9,
            "no_reply_streak": 0,
            "conversation": [
                {"dir": "in", "text": "今天好累啊"},
                {"dir": "out", "text": "那就别干了，躺着"},
                {"dir": "in", "text": "哈哈也是"},
                {"dir": "out", "text": "你那个面试后来怎么样了"},
            ] + [{"dir": "out", "text": t} for t in self.own_history],
            "topics": ["面试", "加班"],
        }
        self.engine.state.mark_dirty()

    async def run(self):
        await self._pump(days=self.days)
        return self.report()

    def rhythm(self) -> str:
        """把「用户真的会回消息」这件事纳入统计：发出去的每一条对应一个时间戳。"""
        stamps = [s["at"] for s in self.platform.sent]
        if not stamps:
            return "（一条都没发出去）"
        day = 86400.0
        span = max(1e-6, stamps[-1] - stamps[0])
        per_day = (len(stamps) - 1) / (span / day)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        gaps.sort()
        return (
            f"{len(stamps)} 条 / {span/ day:.1f} 天 = {per_day:.1f} 条每天；"
            f"间隔中位数 {gaps[len(gaps)//2]/3600:.1f} 小时"
            f"（最短 {gaps[0]/3600:.2f} 小时）"
            if gaps else f"{len(stamps)} 条"
        )

    async def _pump(self, days: float):
        """按心跳节奏推进模拟时钟，直接调 try_once（不跑 run 的 sleep）。

        **Core 状态要跟着时间变**：由头是从「她今天在干什么」派生的，状态不变的话
        派生的那几件一次就用完，之后每一轮都返回空——那样测的就只是「安静」，
        发送路径一次都走不到。真实 Core 的 day.doing 一直在变，这里照着变。
        """
        end = self.clock + days * 86400
        lo = self.engine.cfg.heartbeat_min_minutes * 60
        hi = self.engine.cfg.heartbeat_max_minutes * 60
        while self.clock < end:
            self.clock += random.randint(lo, hi)
            self.engine._time_high = self.clock
            self._advance_core()
            await self.engine.try_once()
        self.engine.state.save()

    def _advance_core(self) -> None:
        """让 Core 的日程/天气随模拟时间变化，模拟真实的一天。"""
        hour = datetime.datetime.fromtimestamp(self.clock + 8 * 3600).hour
        # 一天里换几件不同的事
        plan = [
            (0, "睡觉", "早饭"),
            (5, "起床", "早饭"),
            (9, "改方案", "午饭"),
            (12, "吃午饭", "下午茶"),
            (15, "写周报", "下班"),
            (19, "做饭", "散步"),
            (22, "追剧", "睡觉"),
        ]
        doing = nxt = ""
        for start, d, n in plan:
            if start <= hour:
                doing, nxt = d, n
        weather = ("外面在下雨", "外面出太阳", "风有点大")[int(self.clock // 3600) % 3]
        data = json.loads(self.core_path.read_text(encoding="utf-8"))
        for role in data.get("roles", {}).values():
            selfs = role.get("self") or {}
            contract = selfs.get("contract") or {}
            if "day" in contract:
                contract["day"] = {"doing": doing, "done": [], "next": [nxt]}
            if "activity" in contract:
                contract["activity"]["schedule_event"] = doing
            contract["weather"] = weather
            if "time" in contract:
                contract["time"]["local"] = datetime.datetime.fromtimestamp(
                    self.clock + 8 * 3600).strftime("%Y-%m-%d %H:%M")
        self.core_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def user_state(self, uid="u1") -> str:
        """把「关系账本」几个关键值打出来：只看发了多少条，看不出这些。"""
        u = self.engine.state.bot("bot1").get("users", {}).get(uid, {})
        return (
            f"no_reply_streak={u.get('no_reply_streak')} "
            f"pending={u.get('pending_result')!r} "
            f"发/回={u.get('proactive_sent')}/{u.get('proactive_replied')} "
            f"urge={u.get('urge'):.2f} "
            f"cue={u.get('cue')!r}"
        )

    def report(self) -> Dict[str, Any]:
        prompts = [c for c in self.provider.calls if not c.startswith("[")]
        fallback_hits = [c for c in self.provider.calls if c.startswith("[")]
        return {
            "场景": self.name,
            "真实LLM请求": len(prompts),
            "回退链多打的请求": len(fallback_hits),
            "发送尝试": self.platform.attempts,
            "真正送达": len(self.platform.sent),
            "发送失败": self.platform.failures,
            "WARNING日志": CAPTURE.by_level.get("WARNING", 0),
            "ERROR日志": CAPTURE.by_level.get("ERROR", 0),
            "日志总条数": len(CAPTURE.records),
            "熔断剩余秒": round(self.engine.send_breaker_left()),
        }

    def dump(self, limit=6):
        print(f"\n===== {self.name} · 真正发出去的消息 =====")
        if not self.platform.sent:
            print("（一条都没发出去）")
        for item in self.platform.sent[:limit]:
            print(f"  → [{item['target'].split(':')[-1]}] {item['text']}")
        if len(self.platform.sent) > limit:
            print(f"  …（另有 {len(self.platform.sent) - limit} 条）")
        print(f"  账本：{self.user_state()}")

    def dump_prompts(self, limit=1):
        prompts = [c for c in self.provider.calls if not c.startswith("[")]
        print(f"\n===== {self.name} · 真实 prompt 全文（共 {len(prompts)} 条，显示前 {limit} 条）=====")
        for p in prompts[:limit]:
            print(p)
            print("-" * 70)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ─── 多 Bot：一个角色坏掉，另一个必须照常 ──────────────────────────

class MultiBotSim:
    """两个角色：A 的平台掉线且 key 过期，B 一切正常。

    验三件事（都是「进程内单例共享状态」漏出来的）：
      1. A 的发送熔断不会把 B 一起停掉；
      2. A 的 LLM 退避不会把 B 一起停掉，也不会被 B 的成功抹掉；
      3. A 的脏数据掀翻 settle_minds 时，B 那一轮照常。
    """

    def __init__(self, days=3.0):
        self.days = days
        self.tmp = Path(tempfile.mkdtemp(prefix="multibot-"))
        self.core_path = self.tmp / "core" / "state.json"
        write_core_state(self.core_path)
        for extra in ("bot2", "bot3"):
            write_core_state(self.core_path)
            # 把另外两个角色补进同一份 Core 状态
            data = json.loads(self.core_path.read_text(encoding="utf-8"))
            import copy
            data["roles"][extra] = copy.deepcopy(data["roles"]["bot1"])
            self.core_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        self.clock = 1_700_000_000.0
        self.provider = FakeLLM(lambda p, n: "SEND\n今天还行")
        self.platform = FakePlatform(lambda *a: FakePlatform.OK, now=lambda: self.clock)
        self.ctx = FakeContext(self.provider, self.platform)
        self.engine = SocialEngine(
            context=self.ctx, config=SocialConfig(),
            state_path=str(self.tmp / "state.json"),
            data_dir=str(self.tmp), time_source=lambda: self.clock,
        )
        for bid in ("bot1", "bot2", "bot3"):
            bot = self.engine.state.bot(bid)
            bot.setdefault("users", {})["u1"] = {
                "umo": f"aiocqhttp:FriendMessage:{bid}-u1",
                "last_seen": self.clock - 3 * 86400,
                "last_sent": 0.0, "interest": 0.6, "urge": 0.9,
                "no_reply_streak": 0, "conversation": [],
                "topics": [],
            }
        self.engine.state.mark_dirty()
        self.seen = {bid: 0 for bid in ("bot1", "bot2", "bot3")}

    async def run(self):
        for _ in range(4):
            self.engine._note_send_failure("bot1", "平台未就绪", False)
        for _ in range(3):
            self.engine.generator._note_llm_failure("prov-1", "401 invalid api key", fatal=True)
        breaker_left = self.engine.send_breaker_left("bot1")
        backoff_left = self.engine.generator.llm_backoff_remaining("prov-1")
        # B 一切正常：成功发送 + 成功生成都不该影响 A
        self.engine._note_send_ok("bot2")
        self.engine.generator._note_llm_ok("prov-2")
        return {
            "A 熔断(分)": round(breaker_left / 60),
            "B 熔断(秒)": round(self.engine.send_breaker_left("bot2")),
            "A 退避(分)": round(backoff_left / 60),
            "B 退避(秒)": round(self.engine.generator.llm_backoff_remaining("prov-2")),
            "A 熔断仍在": self.engine.send_breaker_left("bot1") > 0,
            "A 退避仍在": self.engine.generator.llm_backoff_remaining("prov-1") > 0,
        }

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


async def multibot():
    sim = MultiBotSim()
    out = await sim.run()
    sim.cleanup()
    print("多 Bot 隔离：")
    for k, v in out.items():
        print(f"  {k:14s} {v}")
    return out


# ─── 跑 ───────────────────────────────────────────────────────────
SCENARIOS = [
    ("正常", script_normal, FakePlatform.OK),
    ("emoji 人格（她本来就爱用）", script_emoji_persona, FakePlatform.OK),
    ("平台整体挂掉（发不出去）", script_normal, FakePlatform.PLATFORM_DOWN),
    ("key 失效（provider 报鉴权错）", script_bad_key, FakePlatform.OK),
    ("模型读超时", script_timeout, FakePlatform.OK),
    ("模型不按协议作答", script_garbage, FakePlatform.OK),
    ("对方已注销（只该隔离这一个人）", script_normal, FakePlatform.PEER_GONE),
    ("发送超时、投递结果未知", script_normal, FakePlatform.TIMEOUT),
]

# 某些场景要额外带上「她自己的历史说话习惯」或人设原文，才能看出 emoji 判定跟不跟人走
EXTRA = {
    "emoji 人格（她本来就爱用）": dict(
        persona_prompt="你是小夜，一个爱用 emoji 的活泼女孩，发消息爱带✨😂这种。",
        own_history=["今天好累呀😮‍💨", "晚上吃啥🍜", "楼下那只猫好可爱🐱", "困了先睡啦😴"],
    ),
}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", action="store_true", help="额外打印真实 prompt 全文")
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--only", default="", help="只跑名字里含这个的场景")
    args = ap.parse_args()

    rows = []
    for name, llm, send in SCENARIOS:
        if args.only and args.only not in name:
            continue
        CAPTURE.reset()
        sim = Sim(name, llm_script=llm, send_script=send, days=args.days,
                  **EXTRA.get(name, {}))
        try:
            await sim.run()
        except Exception as exc:  # 场景本身崩了也算结果
            rows.append({"场景": name, "异常": repr(exc)})
            sim.cleanup()
            continue
        sim.dump()
        if args.prompts:
            sim.dump_prompts()
        rows.append(sim.report())
        sim.cleanup()

    if not args.only or "多 Bot" in args.only:
        print()
        await multibot()

    print("\n" + "=" * 96)
    cols = ["场景", "真实LLM请求", "回退链多打的请求", "发送尝试", "真正送达",
            "发送失败", "WARNING日志", "ERROR日志", "日志总条数", "熔断剩余秒", "异常"]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("-" * 96)
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
    print("=" * 96)


if __name__ == "__main__":
    asyncio.run(main())
