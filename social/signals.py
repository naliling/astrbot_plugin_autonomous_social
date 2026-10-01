"""把社交层的近况写回给 Humanoid Core。

两边各写各的文件：Core 写它的 state.json（内含 contract），这里写 `humanoid_signals.json`。
没有并发写同一个文件的问题，Core 那边只读，并且超过 15 分钟就不采信。

写回的只有两件事，都是 Core 自己算不出来、但会影响她「现在是什么状态」的：
刚替她主动开过口（想说的心思该泄掉）、被谁连着冷落了几次（会变成一句压着的挂念）。

v1.19.0（多 Bot）：
- 文件里多一份 `by_bid: {bid: {...}}`，每个角色各记各的。这是**为将来准备的**：Core
  那边升级成按角色读之后，不用再改这个文件。
- 顶层那几个字段是 Core 目前真正会读的，而 Core 每个角色实例都读同一个文件、且不按
  角色过滤——于是「A 主动发了一条」会让 B 的 Core 也以为刚说过话，把 B 的
  social_desire 也泄掉 40。**多 Bot 时干脆不写顶层**（单 Bot 照旧拿满收益），
  代价只有一个：多 Bot 下没人替 social_desire 泄，它更容易顶格，而现有的
  mood_multiplier 仍会限速。

v2.24.0：
- 心跳改成**无条件重写**。Core 判信号过期的阈值是 900 秒，而插件的心跳默认
  8~15 分钟才跑一次；原实现里内容没变就靠 120 秒闸门跳过，于是 mtime 最长能拖到
  15 分钟以上不刷新。Core 于是周期性判 stale，把 ignored_streak 直接读成 0——
  「被冷落几次」在默认配置下本来就是会自己清零的。
- 删掉 `desire`：Core 全项目没有任何一处读它，而且它写回去的就是 Core 自己通过
  契约给过来的 social_desire，一个没人接收的闭环，还让每轮心跳多触发一次文件重写。
- 写失败不再静默：记下来，状态页能看见（原来返回 False 就完事，表现是社交侧一切
  正常、Core 侧永远显示「装了个寂寞」）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Callable

FILE_NAME = "humanoid_signals.json"


class SignalsWriter:
    def __init__(
        self,
        path: str,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self._time = time_source
        self._payload: dict[str, Any] = {
            "schema": 1,
            "last_proactive_at": 0.0,
            "last_target_uid": "",
            "ignored_streak": 0,
            # 每个角色各记各的（Core 将来按角色读时直接用）
            "by_bid": {},
        }
        self._written_at = 0.0
        self._dirty = True
        self._last_error: str = ""
        # 角色数：>1 时不写会串台的顶层字段
        self._roles = 1

    def _slot(self, bid: str) -> dict[str, Any]:
        by_bid = self._payload.setdefault("by_bid", {})
        return by_bid.setdefault(str(bid or ""), {"last_proactive_at": 0.0,
                                                   "last_target_uid": "",
                                                   "ignored_streak": 0})

    def set_role_count(self, count: int) -> None:
        """当前有几个角色在跑。>1 时不再写会串台的顶层字段（见模块说明）。"""
        self._roles = max(0, int(count))
        self._dirty = True

    def note_proactive(self, target_uid: str, at: float | None = None,
                       bid: str = "") -> None:
        """一条主动消息真的发出去了。"""
        when = float(self._time() if at is None else at)
        slot = self._slot(bid)
        slot["last_proactive_at"] = when
        slot["last_target_uid"] = str(target_uid or "")
        if getattr(self, "_roles", 1) <= 1:
            # 单 Bot：Core 只会把这一个角色的消息认成自己的，照旧写顶层
            self._payload["last_proactive_at"] = when
            self._payload["last_target_uid"] = str(target_uid or "")
        self._dirty = True
        self.flush()

    def note_ignored(self, target_uid: str, streak: int, bid: str = "") -> None:
        """「主动消息发出去、没被回」——**按人记**。

        原来只有角色级的 `ignored_streak`（她被冷落了几次），而好感是**用户级**的：
        Core 那边能把冷落接到身体（社交能量）上，接不到具体某个人。
        少了这个 per-user 计数，「被冷落」就没法换算成「对 TA 的好感下降」——
        只能知道「我最近有点冷清」，不知道是谁。
        """
        uid = str(target_uid or "").strip()
        if not uid:
            return
        try:
            value = max(0, int(streak))
        except (TypeError, ValueError):
            return
        slot = self._slot(bid)
        per = slot.get("ignored_by")
        if not isinstance(per, dict):
            per = {}
        before = int(per.get(uid) or 0)
        if before == value:
            return
        per[uid] = value
        slot["ignored_by"] = per
        self._dirty = True
        self.flush()

    def ignored_by_uid(self, uid: str, bid: str = "") -> int:
        """给 Core 读：这个人在社交层的累计「没理我」次数。"""
        slot = self._slot(bid)
        per = slot.get("ignored_by")
        if not isinstance(per, dict):
            return 0
        try:
            return max(0, int(per.get(str(uid or "")) or 0))
        except (TypeError, ValueError):
            return 0

    def set_ignored_streak(self, streak: int, bid: str = "") -> None:
        try:
            value = max(0, int(streak))
        except (TypeError, ValueError):
            return
        slot = self._slot(bid)
        if int(slot.get("ignored_streak") or 0) == value:
            return
        slot["ignored_streak"] = value
        if getattr(self, "_roles", 1) <= 1:
            self._payload["ignored_streak"] = value
        self._dirty = True
        self.flush()

    def beat(self) -> bool:
        """每个周期无条件重写一遍：内容没变也要刷新 mtime。

        这个文件的全部价值就在于 Core 那边按 mtime 判断「社交层还活着吗」，而 Core
        的过期阈值是 900 秒。内容没变就不写的话，mtime 会被心跳间隔（默认 8~15 分钟）
        拖过那个阈值，Core 读到 stale 就把一切当没有——被冷落计数被读成 0，
        刚主动开过口也当没发生过。这一个几百字节的小文件，每轮心跳重写一次不算什么。
        """
        return self.flush(force=True)

    def last_error(self) -> str:
        """最近一次写失败的原因；空串表示现在写得进去。"""
        return self._last_error

    def flush(self, force: bool = False) -> bool:
        """内容真变了就立刻写；没变时只在心跳里刷新 mtime。"""
        now = self._time()
        if not force and not self._dirty and now - self._written_at < 120.0:
            return False
        payload = dict(self._payload)
        payload["updated_at"] = round(now, 1)
        directory = os.path.dirname(self.path)
        try:
            os.makedirs(directory, exist_ok=True)
            handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as writer:
                json.dump(payload, writer, ensure_ascii=False, separators=(",", ":"))
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            self._last_error = str(exc)
            return False
        self._last_error = ""
        self._written_at = now
        self._dirty = False
        return True
