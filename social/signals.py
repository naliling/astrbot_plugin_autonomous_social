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

# ignored_by 按 uid 记，永不清理的话会随用户数**单调增长**，而这个文件每轮心跳
# 全量重写。超出上限时丢掉最久没更新的条目（dict 保序，更新会把它移到末尾）。
_IGNORED_BY_MAX = 500


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
        # 我们自己的保鲜期（秒），写进文件让 Core 按它判断过期。
        # 0 = 还没声明，Core 用它自己的默认值。
        self._ttl_seconds = 0.0

    def set_ttl_seconds(self, seconds: float) -> None:
        """声明「这份信号多久之内算新鲜」，由**写方**决定。

        Core 那边的过期阈值原来是写死的 900 秒，而这里的心跳间隔是**可配的**
        （`heartbeat_max_minutes`：默认 15 分钟、最大 120 分钟）。`beat()` 每轮都重写
        mtime 来对抗这件事，但只要心跳间隔超过 15 分钟，Core 就会在两次心跳之间
        把整份信号判成过期、当成没有——被冷落计数读成 0，刚主动开过口也当没发生。
        所以把真实保鲜期写进文件（`ttl_seconds`），让对面按我们的节奏算。
        """
        try:
            value = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return
        if value == self._ttl_seconds:
            return
        self._ttl_seconds = value
        self._dirty = True

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
        try:
            when = float(self._time() if at is None else at)
        except (TypeError, ValueError):
            # at 是调用方传进来的值，坏值不该让「刚替她开过口」这件事整个丢掉
            when = float(self._time())
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
        try:
            before = int(per.get(uid) or 0)
        except (TypeError, ValueError):
            # 盘上旧值可能是字符串/对象；按「还没有记录」处理，别让整轮心跳抛
            before = 0
        if before == value:
            return
        # 先删再插：让最近更新的条目排在末尾，超限时从头（最久没更新）丢
        per.pop(uid, None)
        per[uid] = value
        while len(per) > _IGNORED_BY_MAX:
            per.pop(next(iter(per)), None)
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
        try:
            current = int(slot.get("ignored_streak") or 0)
        except (TypeError, ValueError):
            current = 0
        if current == value:
            return
        slot["ignored_streak"] = value
        if getattr(self, "_roles", 1) <= 1:
            self._payload["ignored_streak"] = value
        self._dirty = True
        self.flush()

    def beat(self) -> bool:
        """每个周期无条件重写一遍：内容没变也要刷新 mtime。

        这个文件的全部价值就在于 Core 那边按 mtime 判断「社交层还活着吗」。内容没变
        就不写的话，mtime 会被心跳间隔拖过 Core 的过期阈值，Core 读到 stale 就把一切
        当没有——被冷落计数被读成 0，刚主动开过口也当没发生过。

        心跳间隔是可配的（最大 120 分钟），所以除了每轮重写 mtime，`flush` 还会把
        我们真实的保鲜期写进 `ttl_seconds`，让 Core 按我们的节奏判过期（见
        `set_ttl_seconds`）。这一个几百字节的小文件，每轮心跳重写一次不算什么。
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
        if self._ttl_seconds > 0:
            # 让 Core 按我们真实的保鲜期算过期，而不是它写死的 900 秒。
            payload["ttl_seconds"] = round(self._ttl_seconds, 1)
        directory = os.path.dirname(self.path) or "."
        tmp: str | None = None
        try:
            os.makedirs(directory, exist_ok=True)
            handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                writer = os.fdopen(handle, "w", encoding="utf-8")
            except BaseException:
                # fdopen 失败时句柄还没被接管，得自己关掉，避免泄漏 fd
                try:
                    os.close(handle)
                except OSError:
                    pass
                raise
            with writer:
                json.dump(payload, writer, ensure_ascii=False, separators=(",", ":"))
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(tmp, self.path)
            tmp = None
            # fsync 文件只保证内容落盘；os.replace 的目录项变更要 fsync 父目录才算落地
            # （Windows 上目录 fsync 不可用，必须容错）
            try:
                dfd = os.open(directory, os.O_RDONLY)
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
        except Exception as exc:
            # 原来只捕获 OSError：json.dump 抛 TypeError 或 flush 抛别的异常时，
            # tmp 已创建且**永不删除**，而 beat() 每轮心跳都调 → 每 8~15 分钟泄漏
            # 一个临时文件；而且 _last_error 不会被设置，状态页上什么都看不到。
            self._last_error = f"{type(exc).__name__}: {exc}"
            return False
        finally:
            # 成功时 tmp 已被 os.replace 消费掉（tmp 置 None）；失败/异常时清理
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        self._last_error = ""
        self._written_at = now
        self._dirty = False
        return True
