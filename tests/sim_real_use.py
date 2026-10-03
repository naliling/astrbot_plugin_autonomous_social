"""真实使用仿真：把插件当成真人在用好几天，逐条读她发出来的话。

与 `sim_autonomous_social.py` 的区别：那个测的是**故障与指标**（请求放大、熔断、
日志刷屏）。这个测的是**用起来像不像人**——会不会说人话、会不会重复、群聊里
接话有没有接对、用户冷了她会不会继续刷。所以它：

* 模拟一个**会回话的用户**：机器人发消息后，按人设逐条回（有时热情、有时敷衍、
  有时干脆不回），而不是只让机器人单方面输出；
* 把每一条**发出去的**消息连同"当时是什么由头、用户上一句是什么"一起按时间线打出来，
  供人眼读；
* 覆盖私聊主动、群聊心流、念想三个通道。

读法：不要只看"发出几条"，要读每一条像不像真人会说的话。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import random
import sys
import types
from pathlib import Path

# 复用主仿真基建（假 LLM / 假平台 / 状态构造）
HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("_sim_base", HERE / "sim_autonomous_social.py")
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

PLUGIN_ROOT = HERE.parent / "astrbot_plugin_autonomous_social"
sys.path.insert(0, str(PLUGIN_ROOT))

from social.engine import SocialEngine  # noqa: E402
from social.config import SocialConfig  # noqa: E402

random.seed(20261003)


# ─── 一个"会说话"的假用户：回消息内容取决于她发的是什么 ─────────────
def user_reply_to(bot_text: str, turn: int) -> str | None:
    """看机器人说了什么，决定用户回什么。返回 None = 这次不回。"""
    t = str(bot_text or "")
    # 三成概率不回（真人也会忙）
    if turn % 5 == 3:
        return None
    # 机器人问了具体的事 → 顺着答
    if any(w in t for w in ("怎么样", "过了吗", "面试", "后来", "几点")):
        return ["还行吧，就那样", "过了！累死我了", "还没定，下周说", "就正常那样"][turn % 4]
    if any(w in t for w in ("在想", "想你", "在干嘛", "干嘛呢", "陪我")):
        return ["我也在想你", "刚忙完，在呢", "哈哈，在加班呢", "怎么了这是"][turn % 4]
    if any(w in t for w in ("早", "晚安", "睡")):
        return ["早呀", "晚安，你也早点睡", "嗯嗯睡了", "这么晚还没睡啊"][turn % 4]
    # 普通分享 → 敷衍或热情交替
    return ["哈哈", "嗯嗯", "这个我不懂诶", "真的假的", "哦哦好的", "6", "今天累死了有点不想说话"][turn % 7]


class RealisticSim:
    """一个用户 + 一个群，跑若干天，把发出的每条消息连同上下文打出来。"""

    def __init__(self, name: str, *, days: float = 3.0, group: bool = False,
                 persona_prompt: str = "你是小夜，说话简短随意，别端着。",
                 user_activity: float = 0.35):
        self.name = name
        self.days = days
        self.with_group = group
        self.user_activity = user_activity
        self.tmp = _base.Path(_base.tempfile.mkdtemp(prefix="real-"))
        self.core_path = self.tmp / "core" / "state.json"
        _base.write_core_state(self.core_path, persona="小夜", nickname="阿澈",
                               energy=62.0)
        self.provider = _base.FakeLLM(self._script, tag=name)
        self.platform = _base.FakePlatform(lambda *a: _base.FakePlatform.OK,
                                           now=lambda: self.clock)
        self.ctx = _base.FakeContext(self.provider, self.platform, persona_prompt)
        self.clock = 1_700_000_000.0
        self.state_path = self.tmp / "state.json"
        cfg = SocialConfig()
        self.engine = SocialEngine(
            context=self.ctx, config=cfg, state_path=str(self.state_path),
            data_dir=str(self.tmp), time_source=lambda: self.clock,
        )
        self.engine._seed_bootstrap = None
        cfg.history_ingest = False
        cfg.seed_users = []
        cfg.group_flow_enabled = group
        cfg.group_icebreak_enabled = group
        cfg.group_ref_lib_enabled = group
        self.turn = 0
        self.log_lines: list[str] = []
        self._setup()

    def _setup(self):
        bot = self.engine.state.bot("bot1")
        bot.setdefault("users", {})["u1"] = {
            "umo": "aiocqhttp:FriendMessage:u1",
            "last_seen": self.clock - 2 * 86400,
            "last_spoken": self.clock - 2 * 86400,
            "last_message": "今天好累啊",
            "last_spoken_text": "./.",
            "interest": 0.62,
            "urge": 1.2,
            "conversation": [
                {"dir": "in", "text": "今天好累啊"},
                {"dir": "out", "text": "那就别干了，躺着"},
                {"dir": "in", "text": "哈哈也是"},
            ],
            "topics": ["面试", "加班"],
        }
        if self.with_group:
            self.engine.state.record_group_message(
                "bot1", "g1", "aiocqhttp:GroupMessage:g1", "摸鱼群", "阿澈",
                "今天加班到九点", False, self.clock,
            )
        self.engine.state.mark_dirty()

    # ─── 假 LLM：按提示词里的信号生成像样的回复 ───
    def _script(self, prompt: str, n: int):
        p = prompt or ""
        # 群聊心流
        if "群里最新的一句" in p or "你刚才在这个群里" in p:
            if n % 4 == 0:
                return "NO\n接不上"
            return "SEND\n我记得那事也挺悬的"
        if "你想在群里抛一个轻松的话头" in p:
            return "今天有人摸鱼成功吗"
        # 私聊：多数 SEND，偶尔 NO
        if n % 6 == 0:
            return "NO\n刚聊过，没什么想说的"
        if "不是有事，就是想起了这个人" in p:
            return f"SEND\n#想你了\n{n % 2 and '有点想你了，在忙吗' or '突然想起你，你最近咋样'}"
        if "群里最新" in p:
            return "SEND\n我这会儿也刚忙完"
        bodies = [
            "刚改完方案脑子有点木",
            "你那个面试后来怎么样了",
            "楼下便利店的关东煮今天有蟹棒诶",
            "我今天效率好低，一半时间在发呆",
            "刚睡醒，人还是懵的",
            "刚把周报交了，松口气",
            "今天这咖啡不太行，太淡了",
            "刚下班，路上那排银杏黄了一半",
            "外面在下雨，窗户都是雾的",
        ]
        body = bodies[n % len(bodies)]
        # 三成的概率分成两段（模拟模型的 --- 输出）
        if n % 3 == 0:
            body = f"{body}\n---\n你呢，今天怎么样"
        return f"SEND\n#改方案\n{body}"

    # ─── 用户发一条消息 → 机器人主链路回一句（模拟 AstrBot） ───
    def user_speaks(self, text: str):
        self.engine.state.record_incoming("bot1", "u1", text, reply_window_seconds=21600)
        self.log_lines.append(f"  用户 → {text}")
        # 主链路回复（模拟框架调 LLM 后回一句）——不喂给插件，只让插件看到"她说过话"
        reply = self._main_reply(text)
        if reply:
            self.engine.state.record_spoken("bot1", "u1", reply)
            self.log_lines.append(f"  小夜(主链路) → {reply}")

    def _main_reply(self, text: str) -> str:
        if "面试" in text or "过了" in text:
            return "那就好，累坏了吧，早点歇着"
        if any(w in text for w in ("在呢", "我也在想你", "哈哈")):
            return "嗯，我在的"
        if "不想说话" in text:
            return "行，那你先歇着，我不吵你"
        return ["嗯嗯", "好呀", "知道了", "那你忙", "哈哈行"][self.turn % 5]

    def _record_outgoing(self, text: str, why: str):
        self.log_lines.append(f"  小夜(主动) → {text}   〔{why}〕")

    async def run(self):
        end = self.clock + self.days * 86400
        lo = self.engine.cfg.heartbeat_min_minutes * 60
        hi = self.engine.cfg.heartbeat_max_minutes * 60
        last_sent_count = 0
        while self.clock < end:
            self.clock += random.randint(lo, hi)
            self.engine._time_high = self.clock
            self._advance_core()
            # 用户按概率主动说话（模拟真实聊天）；user_activity=0 即完全不回
            if random.random() < self.user_activity:
                self.turn += 1
                self.user_speaks(
                    ["在干嘛呢", "今天累死了", "面试过了！", "嗯嗯", "刚下班",
                     "你睡了吗", "哈哈哈哈哈", "明天还要早起"][self.turn % 8]
                )
            await self.engine.try_once()
            # 打印这一轮新发出去的消息（带由头）
            new = self.platform.sent[last_sent_count:]
            for item in new:
                why = ""
                u = self.engine.state.user("bot1", "u1")
                for e in reversed(u.get("proactive_log") or []):
                    if str(e.get("text", "")).strip() == item["text"].strip():
                        why = str(e.get("why", "") or "")
                        break
                self._record_outgoing(item["text"], why or "（未知）")
            last_sent_count = len(self.platform.sent)
            # 有群的话偶尔来条群消息
            if self.with_group and random.random() < 0.4:
                self._group_noise()
        self.engine.state.save()

    def _group_noise(self):
        st = self.engine.state
        turn = random.random()
        if turn < 0.5:
            st.record_group_message("bot1", "g1", "aiocqhttp:GroupMessage:g1", "摸鱼群",
                                    "老王", ["今天真冷", "谁在", "下班了没",
                                            "刚吃了个超难吃的汉堡"][self.turn % 4], False, self.clock)
        # 让心流有机会触发（主链路群回复开窗）
        if random.random() < 0.3:
            g = st.group("bot1", "g1")
            st.record_group_message("bot1", "g1", "aiocqhttp:GroupMessage:g1", "摸鱼群",
                                    "", "", "嗯，我也刚下班", True, self.clock)
            st.open_flow("bot1", "g1", self.clock, self.engine.cfg.flow_window_minutes * 60)
            st.hold_flow("bot1", "g1", self.clock, self.engine.cfg.flow_hold_seconds)

    def _advance_core(self):
        self.engine  # no-op; core day 变化由 base 的 _pump 逻辑模拟，这里简化

    def dump(self):
        print(f"\n{'='*70}\n【{self.name}】{self.days} 天，用户会回话\n{'='*70}")
        for line in self.log_lines:
            print(line)
        print(f"\n  合计：发出 {len(self.platform.sent)} 条，LLM 调用 {len(self.provider.calls)} 次")

    def cleanup(self):
        _base.shutil.rmtree(self.tmp, ignore_errors=True)


async def main():
    sims = [
        RealisticSim("私聊 · 3 天", days=3.0),
        RealisticSim("私聊 + 群聊 · 2 天", days=2.0, group=True),
    ]
    for s in sims:
        await s.run()
        s.dump()
        s.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
