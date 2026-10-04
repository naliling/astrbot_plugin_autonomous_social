"""主动消息的「正在输入」上报。

和 Humanoid Core 那边同一套做法（照市场插件 astrbot_plugin_input_state_by_nc）：

* 每 interval 秒向 NapCat 报一次 `set_input_status(user_id, event_type=1)`，
  QQ 的状态约 8~10 秒自己会消失，报一次不够；
* 只在私聊生效（这个接口的 user_id 就是私聊里的对方）；
* 只对 aiocqhttp 平台打这些接口，其它平台（qq_official 等）静默跳过。

区别只在一处：社交这边是**主动发话**，手上没有事件对象，客户端要从 umo 反查平台实例拿
（`context.get_platform_inst(platform_id)` → `get_client()`）。

纯装饰：任何一步出错都只记一条 debug 日志，既不影响发消息，也不会让状态一直亮着
（每个任务自己带 timeout 兜底）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Tuple

from .history_ingest import parse_umo, umo_kind

_Entry = Tuple[asyncio.Task, asyncio.Event]


class InputStateNotifier:
    """按会话维护主动消息的「正在输入」上报。"""

    def __init__(
        self,
        context: Any,
        config_provider: Any,
        logger: Any,
        time_source=None,
        interval: float = 0.5,
        timeout: float = 120.0,
    ) -> None:
        self.context = context
        self._config = config_provider
        self._log = logger
        self._time = time_source or time.monotonic
        self._tasks: Dict[str, _Entry] = {}
        self.interval = max(0.05, float(interval))
        self.timeout = max(1.0, float(timeout))

    # ------------------------------------------------------------------

    def configure(self, interval: float, timeout: float) -> None:
        self.interval = max(0.05, float(interval))
        self.timeout = max(1.0, float(timeout))

    def _enabled(self) -> bool:
        try:
            return bool(getattr(self._config(), "input_state_enabled", True))
        except Exception:
            return False

    def _client_for(self, umo: str) -> Optional[Any]:
        """umo → 平台客户端。不是 aiocqhttp、或拿不到客户端就返回 None。"""
        try:
            platform_id, _, _ = parse_umo(umo)
            if not platform_id:
                return None
            getter = getattr(self.context, "get_platform_inst", None)
            if not callable(getter):
                return None
            inst = getter(platform_id)
            if inst is None:
                return None
            meta = inst.meta() if callable(getattr(inst, "meta", None)) else None
            if getattr(meta, "name", "") != "aiocqhttp":
                return None
            get_client = getattr(inst, "get_client", None)
            client = get_client() if callable(get_client) else None
            if client is None:
                return None
            api = getattr(client, "api", None)
            if api is None or not hasattr(api, "call_action"):
                return None
            return api
        except Exception:
            return None

    # ------------------------------------------------------------------

    async def start(self, umo: str, uid: str) -> None:
        """要开始给这个人写消息了：报「正在输入」。已在跑就跳过。"""
        if not umo or not self._enabled():
            return
        if umo_kind(umo) != "private":
            return
        existing = self._tasks.get(umo)
        if existing is not None and not existing[0].done():
            return
        api = self._client_for(umo)
        if api is None:
            return
        user_id = str(uid or "")
        if not user_id:
            return
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            self._run(umo, api, user_id, stop_event),
            name=f"social-input-state:{umo}",
        )
        self._tasks[umo] = (task, stop_event)

    async def stop(self, umo: str) -> None:
        entry = self._tasks.pop(umo, None)
        if entry is None:
            return
        task, stop_event = entry
        stop_event.set()
        if not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover
                self._log.debug(f"[autonomous_social] 输入状态任务收尾异常: {exc}")

    async def stop_all(self) -> None:
        for umo in list(self._tasks.keys()):
            await self.stop(umo)

    # ------------------------------------------------------------------

    async def _run(self, umo: str, api: Any, user_id: str, stop_event: asyncio.Event) -> None:
        deadline = self._time() + self.timeout
        try:
            while not stop_event.is_set():
                await self._report(api, user_id)
                if self._time() >= deadline:
                    self._log.debug(f"[autonomous_social] 输入状态上报超时（{self.timeout:.0f}s），停止")
                    return
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
                    return
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 纯装饰
            self._log.debug(f"[autonomous_social] 输入状态上报失败: {exc}")
        finally:
            self._tasks.pop(umo, None)

    async def _report(self, api: Any, user_id: str) -> None:
        try:
            await api.call_action("set_input_status", user_id=str(user_id), event_type=1)
        except Exception as exc:
            self._log.debug(f"[autonomous_social] set_input_status 失败（可能不是 NapCat）: {exc}")
            raise
