"""节流：日志节流（按时间窗口聚合同类失败）+ 群硬收口（连续被唤醒后的一段静默）。

日志节流为什么需要：插件的失败路径全在定时任务里，一次故障会让同一条日志每轮心跳、
每条群消息重复一次。key 失效、平台掉线、磁盘满——出问题的恰恰是「持续出问题」
的场景，不节流的话日志文件会在几分钟内被同一行刷爆，真正有用的信息反而被埋掉。

窗口内不更新时间戳：只有真的打出去了才推进窗口，这样「窗口结束后再打一条 +
汇总计数」成立，而不是一旦进入窗口就永远静默。

群硬收口（v1.29.0，本文件后半段）：她在群里连续被唤醒发言太密时，一段时间内只放行
真正的点名/回复。两件事放在同一个模块，是因为它们的共同点是「**从已有记录重算**」，
都不新增一份可变状态。
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

# 默认窗口（秒）
DEFAULT_WINDOW = 300.0


class LogThrottle:
    def __init__(self) -> None:
        self._last: Dict[str, Tuple[float, int]] = {}

    def allow(self, key: str, window: float = DEFAULT_WINDOW) -> bool:
        """这条日志现在该不该打。返回 False 表示被节流掉了。"""
        now = time.monotonic()
        entry = self._last.get(key)
        if entry is None:
            # 这个 key 从没打过，必须放行。
            #
            # 不能用 `get(key, (0.0, 0))` 再比 `now - 0.0 >= window`：`time.monotonic()`
            # 在 Linux 与 Windows 上都是「开机以来的秒数」，机器/容器刚起来时它小于
            # window，于是第一条日志会被当成「窗口内重复」吞掉，还会把 pending 记成 1 ——
            # 调用方紧接着打出「另有 1 次同类已省略」，而那次根本不存在。
            self._last[key] = (now, 0)
            return True
        last, pending = entry
        if now - last >= window:
            # 推进窗口但**保留** pending：调用方紧接着用 summary() 读它。
            # 这里清零的话「另有 N 次已省略」永远是 0（旧实现就是这个毛病）。
            self._last[key] = (now, pending)
            return True
        self._last[key] = (last, pending + 1)
        return False

    def pending(self, key: str) -> int:
        """自上次真正打出之后，被省略了多少条。"""
        return self._last.get(key, (0.0, 0))[1]

    def summary(self, key: str) -> str:
        """拼一句「期间另有 N 次同类已省略」，没有就返回空串。读后清零。"""
        n = self._last.get(key, (0.0, 0))[1]
        if not n:
            return ""
        self._last[key] = (self._last[key][0], 0)
        return f"（期间另有 {n} 次同类，已省略）"

    def reset(self, key: Optional[str] = None) -> None:
        if key is None:
            self._last.clear()
        else:
            self._last.pop(key, None)


# 引擎与生成器共用一个实例：它们报的本来就是同一类问题，分开节流等于没节流
throttle = LogThrottle()


# ─── 群硬收口（v1.29.0）─────────────────────────────
#
# 语义照抄 `companion_v2/conversation/group_high_intensity.py`：她在群里连续被唤醒
# `threshold` 次（首尾都落在 `window_seconds` 内）之后，接下来 `cooldown_seconds`
# 只放行「真正的点名/回复」，其余一律不开口。群里她话太密的时候，宁可不接——
# 这是「硬」的那一半，软的一半（心流窗口、每小时配额）在 groupflow / engine 里。
#
# 与参考实现唯一的口径差异：时间用 epoch 秒（`time.time()`）而不是 aware datetime。
# 本插件全程用 float 时间戳（`blocked_until` / `flow_open_until` 那些字段都是），
# 这里换成 datetime 等于给同一份状态造两套时间口径。

BYPASS_REASONS = frozenset(
    (
        "mention",  # At 到了她（`groupflow.attribution` 的 ATTR_MENTION）
        "native_wakeup",  # 平台原生唤醒
        "quoted_reply",  # 引用回复
        "native_quote",  # 平台原生引用
        "direct_word",  # 直接叫名字
        "active_follow_up",  # 已经确认的追问
        "reply_follow_up",  # 回复链上的追问
    )
)

# 默认值照参考实现：5 分钟内 3 轮就算「密」，之后静默 15 分钟。
# 群里 5 分钟被叫 3 次，基本等于她一个人在刷屏了。
CLOSEOUT_WINDOW_SECONDS = 300
CLOSEOUT_THRESHOLD = 3
CLOSEOUT_COOLDOWN_SECONDS = 900


def closeout_state(
    rounds: Any,
    at: Any,
    *,
    window_seconds: int = CLOSEOUT_WINDOW_SECONDS,
    threshold: int = CLOSEOUT_THRESHOLD,
    cooldown_seconds: int = CLOSEOUT_COOLDOWN_SECONDS,
) -> Dict[str, Any]:
    """从「已确认送达」的轮次时间戳重算群硬收口状态（纯函数）。

    `rounds` 是她**已确认送达**的轮次时间戳（epoch 秒）。只数确认送达的，不数
    「打算说」或「模型生成了但没发出去」的——收口要压的是群里真出现的话，
    不是尝试次数。

    **为什么从历史重算，而不是记一个「冷却到什么时候」的可变字段**：
      1. 跨进程/重放不安全。那个字段一旦落盘就有多个写者（事件链、心跳、重试），
         各读一份旧状态、谁后写谁说了算，冷却会被回滚；回滚成 0 就等于没有收口，
         而且这种 bug 只在并发/重试时才出现，本地永远复现不出来。
      2. 时钟。绝对截止时间戳怕时钟回拨和时区切换；重算只依赖「这些轮次之间隔了多久」
         这个相对量，系统时间怎么跳都不影响判定。
      3. 状态文件被手改坏时（缺字段、写成字符串），可变字段要么恒真要么恒假，
         而且没人能从坏值反推出「本该什么时候结束」；重算永远只依赖那串时间戳本身，
         坏字段最多让它退回「没在收口」（fail-open），不会把她永久禁言。
    `rounds` 本来就是有界历史（调用方只留最近几十条），重算是 O(n)，换来的是
    「同一段历史永远得到同一个结论」——重启、重试、换进程都不会让收口漂移。

    参数校验照参考实现，越界抛 ValueError（那是调用方写错了，不该被静默吞掉）：
    `at` 必须是有限数值；`window_seconds` ≥ 1 的整数；`threshold` ≥ 2 的整数
    （1 轮就算「密」等于每条都收口）；`cooldown_seconds` ≥ 0 的整数。
    历史里的脏值（非数、nan、inf、晚于 `at` 的未来时间）一律跳过，不抛——
    那是数据，不是调用方的错，跳过等于「这一轮不存在」。

    返回：
      active            现在是否处于收口期
      recent_wakeups    最近 `window_seconds` 内她送达了几轮
      threshold / window_seconds / cooldown_seconds  本次判定用的参数（回显给日志与配置面板）
      until             收口结束时刻（epoch 秒）；不 active 时为 0.0，
                        与状态里 `blocked_until` 等字段「0 = 没有」的口径一致
      remaining_seconds 还剩多少秒（不 active 时为 0）
    """
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        raise ValueError("群硬收口需要一个 epoch 秒（int/float）")
    now = float(at)
    if not math.isfinite(now):
        raise ValueError("群硬收口的时间必须是有限值")
    if type(window_seconds) is not int or window_seconds < 1:
        raise ValueError("群硬收口窗口不合法（需要 ≥ 1 的整数秒）")
    if type(threshold) is not int or threshold < 2:
        raise ValueError("群硬收口阈值不合法（至少 2 轮才算「密」）")
    if type(cooldown_seconds) is not int or cooldown_seconds < 0:
        raise ValueError("群硬收口冷却不合法（需要 ≥ 0 的整数秒）")

    try:
        items = list(rounds or [])
    except TypeError:
        items = []
    eligible: List[float] = []
    for raw in items:
        # bool 是 int 的子类，`True` 会被读成 1.0 秒——那不是时间戳，是脏值。
        if isinstance(raw, bool):
            continue
        try:
            when = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(when) and when <= now:
            eligible.append(when)
    eligible.sort()

    # 每一个「连续 threshold 轮、首尾落在窗口内」的 burst 都会开一段冷却；
    # 取所有 burst 里最晚的结束时刻。这样「她 5 分钟被叫 3 次」和「之后又来 3 次」
    # 叠加起来是延长的收口，而不是被后一个 burst 覆盖成更早的截止时间。
    until = 0.0
    for end in range(threshold - 1, len(eligible)):
        burst = eligible[end - threshold + 1 : end + 1]
        if burst[-1] - burst[0] <= window_seconds:
            candidate = burst[-1] + cooldown_seconds
            if candidate > until:
                until = candidate

    active = until > 0.0 and now < until
    start = now - window_seconds
    recent = 0
    for when in eligible:
        if when >= start:
            recent += 1
    return {
        "active": active,
        "recent_wakeups": recent,
        "threshold": threshold,
        "window_seconds": window_seconds,
        "cooldown_seconds": cooldown_seconds,
        "until": until if active else 0.0,
        "remaining_seconds": max(0, round(until - now)) if active else 0,
    }


def closeout_allows(reason: str, closeout: Dict[str, Any]) -> bool:
    """收口期内这个理由还能不能开口。True = 可以说话。

    只放行 `BYPASS_REASONS`：真正的点名、原生唤醒、引用回复、直接叫名字、
    以及已经确认的追问。疑问句、兴趣词、冷场起话头、复读、行为猜测一律不放行
    ——收口的意义就是「她这会儿少说几句」，放宽一条就等于没有收口。

    **坏 state 一律 fail-open 返回 True**：收口是「少说一句」的优化，不是安全边界。
    状态读不出来（不是 dict、字段被手改成怪值、调用方传了 None）时，宁可让她照常
    说话，也不能因为一个坏字段把她永久禁言——那才是真正的事故。理由本身读不出来
    （非字符串且转不成字符串）同样 fail-open。
    """
    try:
        if not isinstance(closeout, dict):
            return True
        # 只认真正的 True：`"true"` / `1` 这种被手改坏的字段按「没在收口」处理。
        if closeout.get("active") is not True:
            return True
        text = str(reason or "").strip()
    except Exception:
        return True
    return text in BYPASS_REASONS
