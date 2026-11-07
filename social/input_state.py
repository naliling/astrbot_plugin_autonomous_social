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


# ─── 对方话没说完时，「这一轮先不开口」的理由码 ────────────
#
# 「等她说完再回」不是一种输入状态，而是这一轮**不开口**的理由；状态页与日志要能
# 看出「为什么没回」。判据本身在 threads.unfinished_signal（纯函数，只看那一条消息），
# 这里只做映射，不重复判、不缓存，也不碰下面那套「正在输入」上报。
#
# 只有 unfinished 才给码：uncertain 的意思是「像被截断，但看不出她会不会继续」，
# 拿一次静默去赌这个猜测，代价是对方以为没人理——照常回一句更安全（fail-open）。
INPUT_WAIT_REASONS = {
    "unfinished": "await_unfinished",
}


def wait_reason(signal: Any) -> str:
    """半截话判断结果 → 这一轮该等的理由码；该回就返回空串。

    signal 是 threads.unfinished_signal 的返回值（只读）。读不出来、state 不认识、
    或对方其实已经说完，一律返回空串：宁可照常回一句，也不因为一个坏字段把她
    变成永远不开口。
    """
    try:
        data = signal if isinstance(signal, dict) else {}
        state = str(data.get("state", "") or "")
        base = INPUT_WAIT_REASONS.get(state, "")
        if not base:
            return ""
        detail = str(data.get("reason", "") or "")
        # 带上判据码（await_unfinished:explicit_marker）：日志里要能分清是「她明说
        # 还要补充」还是「话停在半截」，这两种在排查时不是一回事。
        return f"{base}:{detail}" if detail else base
    except Exception:
        return ""


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
                # 失败只记日志（在 _report 里），下一个 interval 继续重试：
                # 一次抖动不该让整段生成时间里都不显示「正在输入」。
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
            # 只清掉**还是自己**的那条记录：stop() 与 start() 可能交织，
            # 不加这个判断的话旧任务的收尾会把刚起的新任务记录删掉，
            # 新任务就再也没人能停（只能等 timeout，期间一直显示「正在输入」）。
            current = asyncio.current_task()
            entry = self._tasks.get(umo)
            if entry is not None and entry[0] is current:
                self._tasks.pop(umo, None)

    async def _report(self, api: Any, user_id: str) -> bool:
        """报一次「正在输入」。失败返回 False（**不抛**）。

        原来这里失败后 re-raise，被 `_run` 的外层 except 捕获并**直接结束任务**：
        一次网络抖动或 NapCat 重启，就让本次生成剩余时间里的「正在输入」全部消失。
        现在交给调用方在每个 interval 重试。
        """
        try:
            await api.call_action("set_input_status", user_id=str(user_id), event_type=1)
            return True
        except Exception as exc:
            self._log.debug(f"[autonomous_social] set_input_status 失败（可能不是 NapCat）: {exc}")
            return False
