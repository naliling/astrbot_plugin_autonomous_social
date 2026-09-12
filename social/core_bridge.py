"""Humanoid Core 只读桥接。

读取 Humanoid Core 的 state.json 并转换为标准化快照。
支持通过预加载 root dict 避免重复磁盘 IO。
mode != standalone 但读不到数据时，会打一次性 warning。

v1.6.1：
- 使用 AstrBot 统一的 logger 替代标准 logging
- 增加类型注解
- 改进错误处理
- 增强路径解析健壮性
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

# Core state.json 可能的位置（相对 data 目录）
_STATE_FILENAMES: List[str] = [
    "plugin_data/humanoid_core/state.json",
    "plugin_data/astrbot_plugin_humanoid_core/state.json",
]


class CoreBridge:
    """Humanoid Core 状态桥接（只读）。"""

    def __init__(self, mode: str, data_dir: Optional[str] = None):
        self.mode = mode
        self._data_dir = data_dir  # 可选：AstrBot 数据根目录
        self._warned_missing = False  # 一次性 warning 标记
        self._warned_no_role: set = set()  # 每个 bid 只警告一次
        self._cache_fingerprint: Optional[Tuple[str, int, int]] = None
        self._cache_root: Optional[Dict[str, Any]] = None

    # ─── 路径解析 ───────────────────────────────────────

    def _resolve_path(self) -> Optional[str]:
        """解析 Humanoid Core state.json 的路径。

        Returns:
            找到的文件路径，找不到返回 None
        """
        if self.mode == "standalone":
            return None

        bases: List[str] = []
        if self._data_dir:
            bases.append(self._data_dir)
        bases.append(os.path.abspath("data"))

        for base in bases:
            for rel in _STATE_FILENAMES:
                p = os.path.join(base, rel)
                if os.path.exists(p):
                    return p
        return None

    # ─── 磁盘读取 ───────────────────────────────────────

    def read_root(self) -> Optional[Dict[str, Any]]:
        """读取整个 state.json 的根 dict。每个周期调用一次即可。

        按 (mtime_ns, size) 指纹缓存解析结果：Core 只要收到消息就会标脏重写整个文件，
        空闲时文件不变，命中缓存即省掉一次全量 json.load（用户数上千时上百毫秒）。
        命中的指纹来自原子 replace 后的文件，不存在读到半成品的问题；返回的是缓存对象
        本身，调用方只能读、不能改。

        Returns:
            state.json 根 dict，失败或不存在返回 None
        """
        p = self._resolve_path()
        if not p:
            if self.mode != "standalone" and not self._warned_missing:
                self._warned_missing = True
                logger.warning(
                    "[autonomous_social] 未找到 Humanoid Core state.json，联动未生效。"
                    "请确认已安装 astrbot_plugin_humanoid_core 且模式配置正确。"
                    f"当前模式：{self.mode}"
                )
            return None

        try:
            stat = os.stat(p)
        except OSError as e:
            if not self._warned_missing:
                self._warned_missing = True
                logger.warning(f"[autonomous_social] Humanoid Core state.json 读取失败: {e}")
            return None

        fingerprint = (p, stat.st_mtime_ns, stat.st_size)
        if fingerprint == self._cache_fingerprint and self._cache_root is not None:
            return self._cache_root

        try:
            with open(p, encoding="utf-8") as f:
                root = json.load(f)
        except json.JSONDecodeError as e:
            if not self._warned_missing:
                self._warned_missing = True
                logger.warning(f"[autonomous_social] Humanoid Core state.json 格式损坏: {e}")
            return None
        except OSError as e:
            if not self._warned_missing:
                self._warned_missing = True
                logger.warning(f"[autonomous_social] Humanoid Core state.json 读取失败: {e}")
            return None
        except Exception as e:
            if not self._warned_missing:
                self._warned_missing = True
                logger.warning(f"[autonomous_social] Humanoid Core state.json 读取异常: {e}")
            return None

        self._cache_fingerprint = fingerprint
        self._cache_root = root
        return root

    # ─── 快照生成 ───────────────────────────────────────

    def bot_self_state(
        self,
        bot_id: str,
        root: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """获取 bot 级别的自身状态（精力、社交能量等全局字段）。

        直接读 roles[bot_id].self，不再用假 uid。
        如果角色不存在且不是 standalone 模式，打一次性 warning。

        Args:
            bot_id: bot 的 ID
            root: 预加载的 state.json 根 dict（避免重复读盘）

        Returns:
            bot 自身状态 dict，不存在返回 None
        """
        if root is None:
            root = self.read_root()
        if not root:
            return None

        role = root.get("roles", {}).get(str(bot_id), {})
        selfs = role.get("self", {}) if isinstance(role, dict) else {}

        if not selfs and self.mode != "standalone" and bot_id not in self._warned_no_role:
            self._warned_no_role.add(bot_id)
            all_roles = list(root.get("roles", {}).keys())
            logger.warning(
                "[autonomous_social] state.json 中不存在角色 %r，联动可能未生效。"
                "可能是 bot_id 取值错误。state.json 中的角色：%s",
                bot_id, all_roles,
            )

        if not selfs:
            return None

        return self._self_view(selfs, str(bot_id))

    # 契约里的身体轴：v1 开始承诺的字段，只加不改。
    _CONTRACT_BODY_KEYS = (
        "sleep_pressure", "sleep_debt", "hunger", "discomfort", "arousal", "social_desire",
    )

    def _self_view(self, selfs: Dict[str, Any], bot_id: str) -> Dict[str, Any]:
        """把 Core 的角色自身状态整理成社交层要的视图。

        Core v2.14 会在 `self.contract` 里导出带版本号的身体快照。读到就用它（能拿到
        睡意、饥饿、想说话的程度这些旧字段里根本没有的量），读不到则退回旧字段，
        对老版 Core 保持兼容。
        """
        view: Dict[str, Any] = {
            "energy": selfs.get("energy"),
            "social_energy": selfs.get("social_energy"),
            "cycle": selfs.get("current_cycle_day"),
            "process": selfs.get("current_process"),
            "schedule": selfs.get("daily_schedule"),
            "weather": selfs.get("_cached_weather_obj"),
            "contract_v": None,
        }
        contract = selfs.get("contract")
        if not isinstance(contract, dict) or int(contract.get("v") or 0) < 1:
            return view
        body = contract.get("body") if isinstance(contract.get("body"), dict) else {}
        view["contract_v"] = int(contract.get("v") or 0)
        if body.get("energy") is not None:
            view["energy"] = body.get("energy")
        if body.get("social_energy") is not None:
            view["social_energy"] = body.get("social_energy")
        if body.get("cycle_day") is not None:
            view["cycle"] = body.get("cycle_day")
        for key in self._CONTRACT_BODY_KEYS:
            view[key] = body.get(key)
        view["asleep"] = bool(body.get("asleep"))
        view["feelings"] = contract.get("feelings") or []
        view["form"] = contract.get("form") or {}
        view["activity"] = contract.get("activity") or {}
        view["core_time"] = contract.get("time") or {}
        if contract.get("weather"):
            view["weather"] = {"env": contract.get("weather")}
        proc = view.get("process")
        if isinstance(proc, dict) and view["activity"].get("phase"):
            proc = dict(proc)
            proc["phase"] = view["activity"]["phase"]
            view["process"] = proc
        return view

    def load_snapshot(
        self,
        bot_id: str,
        user_id: str,
        root: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """生成单个用户的完整快照（bot 自身状态 + 用户情绪数据）。

        Args:
            bot_id: bot 的 ID
            user_id: 用户 ID
            root: 预加载的 state.json 根 dict（避免重复读盘）

        Returns:
            快照 dict，或 None
        """
        if self.mode == "standalone":
            return None

        if root is None:
            root = self.read_root()
        if not root:
            return None

        role = root.get("roles", {}).get(str(bot_id), {})
        if not isinstance(role, dict):
            return None

        selfs = role.get("self", {}) if isinstance(role, dict) else {}
        user = role.get("users", {}).get(str(user_id), {})
        if not isinstance(user, dict):
            user = {}

        # mood 嵌套结构：users[uid].mood.affection
        mood_data = user.get("mood")
        affection = None
        if isinstance(mood_data, dict):
            affection = mood_data.get("affection")

        # last_message 是 {"text": ..., "timestamp": ...} 结构
        last_msg = user.get("last_message")
        if isinstance(last_msg, dict):
            last_msg = last_msg.get("text", "")

        return {
            **self._self_view(selfs, str(bot_id)),
            "mood": mood_data,
            "affection": affection,
            "last_message": last_msg,
            "last_interaction": user.get("last_interaction"),
        }

    # ─── 紧凑输出 ───────────────────────────────────────

    def compact(self, s: Optional[Dict[str, Any]]) -> str:
        """将快照转为人类可读的简要文本（供 prompt 使用）。

        Args:
            s: 快照 dict

        Returns:
            格式化的状态描述文本
        """
        if not s:
            return "没有可用的 Humanoid Core 状态。"

        parts: List[str] = []

        energy = s.get("energy")
        if energy is not None and energy != "":
            parts.append(f"能量: {energy}")

        social = s.get("social_energy")
        if social is not None and social != "":
            parts.append(f"社交能量: {social}")

        cycle = s.get("cycle")
        if cycle is not None and cycle != "":
            parts.append(f"生理周期: 第{cycle}天")

        proc = s.get("process")
        if isinstance(proc, dict) and proc:
            name = proc.get("name", "")
            phase = proc.get("phase", "")
            if name:
                desc = f"当前过程: {name}"
                if phase:
                    desc += f"（{phase}）"
                parts.append(desc)

        sched = s.get("schedule")
        if isinstance(sched, list) and sched:
            parts.append(f"日程: 今日{len(sched)}个时段")

        weather = s.get("weather")
        if isinstance(weather, dict) and weather:
            w = weather.get("weather", "")
            if w:
                parts.append(f"天气: {w}")

        affection = s.get("affection")
        if affection is not None and affection != "":
            parts.append(f"好感度: {affection}")

        mood = s.get("mood")
        if isinstance(mood, dict) and mood:
            dims = []
            for k, label in (("libido", "亲近欲"), ("aggression", "攻击性")):
                v = mood.get(k)
                if v is not None:
                    dims.append(f"{label}{v}")
            if dims:
                parts.append(f"情绪: {' / '.join(dims)}")

        return "\n".join(parts) if parts else "Humanoid Core 当前没有可用的简要状态。"
