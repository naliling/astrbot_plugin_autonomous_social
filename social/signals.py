"""把社交层的近况写回给 Humanoid Core。

两边各写各的文件：Core 写它的 state.json（内含 contract），这里写 `humanoid_signals.json`。
没有并发写同一个文件的问题，Core 那边只读，并且超过 15 分钟就不采信。

写回的只有三件事，都是 Core 自己算不出来、但会影响她「现在是什么状态」的：
刚替她主动开过口（想说的心思该泄掉）、被谁连着冷落了几次（会变成一句压着的挂念）、
以及当前想说话的程度（纯展示用）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Callable

FILE_NAME = "humanoid_signals.json"
# 内容没变时最多每这么久重写一次，保持 mtime 新鲜（Core 靠它判断信号是否过期）。
HEARTBEAT_SECONDS = 120.0


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
            "desire": None,
        }
        self._written_at = 0.0
        self._dirty = True

    def note_proactive(self, target_uid: str, at: float | None = None) -> None:
        """一条主动消息真的发出去了。"""
        self._payload["last_proactive_at"] = float(self._time() if at is None else at)
        self._payload["last_target_uid"] = str(target_uid or "")
        self._dirty = True
        self.flush()

    def set_ignored_streak(self, streak: int) -> None:
        try:
            value = max(0, int(streak))
        except (TypeError, ValueError):
            return
        if int(self._payload.get("ignored_streak") or 0) == value:
            return
        self._payload["ignored_streak"] = value
        self._dirty = True
        self.flush()

    def set_desire(self, desire: float | None) -> None:
        if desire is None:
            return
        try:
            value = round(float(desire), 1)
        except (TypeError, ValueError):
            return
        if self._payload.get("desire") == value:
            return
        self._payload["desire"] = value
        self._dirty = True
        self.flush()

    def flush(self, force: bool = False) -> bool:
        """内容真变了就立刻写；没变时只按心跳刷新 mtime。

        心跳存在的意义是 Core 那边按 mtime 判断信号是否过期（超过 15 分钟不采信），
        而不是用来挡住新数据——早先把两者合在一起写，会让刚变的冷落计数被拖到两分钟后。
        """
        now = self._time()
        if not force and not self._dirty and now - self._written_at < HEARTBEAT_SECONDS:
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
        except OSError:
            # 写不回去不影响发送：Core 那边读不到信号就是不联动。
            return False
        self._written_at = now
        self._dirty = False
        return True
