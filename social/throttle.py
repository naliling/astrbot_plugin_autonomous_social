"""日志节流：同类失败按时间窗口聚合，窗口内只记第一条，末尾汇总被省略的条数。

为什么需要：插件的失败路径全在定时任务里，一次故障会让同一条日志每轮心跳、
每条群消息重复一次。key 失效、平台掉线、磁盘满——出问题的恰恰是「持续出问题」
的场景，不节流的话日志文件会在几分钟内被同一行刷爆，真正有用的信息反而被埋掉。

窗口内不更新时间戳：只有真的打出去了才推进窗口，这样「窗口结束后再打一条 +
汇总计数」成立，而不是一旦进入窗口就永远静默。
"""

from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

# 默认窗口（秒）
DEFAULT_WINDOW = 300.0


class LogThrottle:
    def __init__(self) -> None:
        self._last: Dict[str, Tuple[float, int]] = {}

    def allow(self, key: str, window: float = DEFAULT_WINDOW) -> bool:
        """这条日志现在该不该打。返回 False 表示被节流掉了。"""
        now = time.monotonic()
        last, pending = self._last.get(key, (0.0, 0))
        if now - last >= window:
            self._last[key] = (now, 0)
            return True
        self._last[key] = (last, pending + 1)
        return False

    def pending(self, key: str) -> int:
        """自上次真正打出之后，被省略了多少条。"""
        return self._last.get(key, (0.0, 0))[1]

    def summary(self, key: str) -> str:
        """拼一句「期间另有 N 次同类已省略」，没有就返回空串。"""
        n = self.pending(key)
        return f"（期间另有 {n} 次同类，已省略）" if n else ""

    def reset(self, key: Optional[str] = None) -> None:
        if key is None:
            self._last.clear()
        else:
            self._last.pop(key, None)

    def stats(self) -> Dict[str, int]:
        """当前仍在节流窗口内、且已经积累了省略次数的条目。"""
        return {k: v[1] for k, v in self._last.items() if v[1] > 0}


# 引擎与生成器共用一个实例：它们报的本来就是同一类问题，分开节流等于没节流
throttle = LogThrottle()
