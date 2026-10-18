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
import time
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

# Core state.json 可能的位置（相对 data 目录）。这只是快路径：用户可能改过插件目录名，
# 穷举路径永远会漏，所以找不到时还会做一次有界搜索。
_STATE_FILENAMES: List[str] = [
    "plugin_data/humanoid_core/state.json",
    "plugin_data/astrbot_plugin_humanoid_core/state.json",
]

# 没找到时的负缓存时间窗（秒）。Core 后装、插件目录后建、或一次扫盘超限之后，
# 不能永久断链：命中才永久缓存，未命中只静默这么久再重试一次。
_SEARCH_RETRY_SECONDS = 300.0

# 有界搜索的限制。搜索必须有硬上限：在超大目录树上无界遍历会把面板卡死。
_SEARCH_MAX_DEPTH = 3
_SEARCH_MAX_ENTRIES = 20000
_SEARCH_SKIP_DIRS = {
    "proc", "sys", "dev", "usr", "lib", "lib64", "bin", "sbin", "etc",
    "var", "snap", "node_modules", "__pycache__", ".git", "venv", "site-packages",
}
# 目录名里带这些词才算可能是 Core
_SEARCH_HINTS = ("humanoid", "core")

# 从 Core 拉「对方说过什么」时最多取几条。Core 那边自己也有上限，这里再卡一道：
# 这是塞进 prompt 的素材，不是给人看的完整历史。
CORE_SAID_MAX = 3


class CoreBridge:
    """Humanoid Core 状态桥接（只读）。"""

    def __init__(self, mode: str, data_dir: Optional[str] = None):
        self.mode = mode
        self._data_dir = data_dir  # 可选：AstrBot 数据根目录
        # 一次性 warning 标记，**按失败原因分开**：共用一个标记时，先发生的故障
        # 会把后面所有故障都静默掉（"未找到" 置位后，"格式损坏" 就再也不提示了，
        # 而用户刚装完 Core 最需要的恰恰是那条消息）。读到一次就整体复位。
        self._warned: Dict[str, bool] = {}
        self._warned_no_role: set = set()  # 每个 bid 只警告一次
        self._cache_fingerprint: Optional[Tuple[str, int, int]] = None
        self._cache_root: Optional[Dict[str, Any]] = None
        # 每个角色只算一次自身视图。settle_minds 会对该角色下**每个用户**调一次
        # load_snapshot，_speak 又会再调一次——1000 人的时候一遍心跳就要重建上千遍
        # 同一个视图，而它的输入（角色自身状态）压根没变。read_root 换文件时清空。
        self._view_cache: Dict[str, Dict[str, Any]] = {}
        # 搜索结果缓存：命中一次就记住路径；未命中只做短期负缓存（见 _SEARCH_RETRY_SECONDS）
        self._searched = False
        self._searched_at = 0.0
        self._search_hit: Optional[str] = None

    def _warn_once(self, reason: str, message: str) -> None:
        """按失败原因各警告一次。不同故障互不遮蔽。"""
        if self._warned.get(reason):
            return
        self._warned[reason] = True
        logger.warning(message)

    # ─── 路径解析 ───────────────────────────────────────

    def _search_for_state(self) -> Optional[str]:
        """有界搜索 Core 的 state.json：目录名带 humanoid/core 且里面有 state.json。

        只搜 data 目录及其下级（以及进程 cwd），深度和条目数都有硬上限。
        """
        if self._search_hit:
            return self._search_hit
        now = time.monotonic()
        # 未命中只做短期负缓存：原来 `_searched` 在搜索**之前**就置 True 且永不重试，
        # 于是 Core 后装、目录后建、或一次扫盘超限都会让联动**永久断链**（只能重载插件）。
        if self._searched and (now - self._searched_at) < _SEARCH_RETRY_SECONDS:
            return None
        self._searched = True
        self._searched_at = now
        roots: List[str] = []
        if self._data_dir:
            roots.append(self._data_dir)
        roots.append(os.path.abspath("data"))
        # 去掉重复，并保持顺序
        seen = set()
        roots = [r for r in roots if r and not (r in seen or seen.add(r))]

        entries = 0
        for root in roots:
            base_depth = root.rstrip(os.sep).count(os.sep)
            for dirpath, dirnames, filenames in os.walk(root):
                if entries > _SEARCH_MAX_ENTRIES:
                    self._warn_once(
                        "search_limit",
                        f"[autonomous_social] 搜索 Humanoid Core 超过 {_SEARCH_MAX_ENTRIES} "
                        f"个目录仍未找到，已停止本次扫盘（{int(_SEARCH_RETRY_SECONDS)} 秒后会再试）",
                    )
                    return None
                entries += 1
                depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
                if depth >= _SEARCH_MAX_DEPTH:
                    dirnames[:] = []
                else:
                    dirnames[:] = [
                        d for d in dirnames
                        if d not in _SEARCH_SKIP_DIRS and not d.startswith(".")
                    ]
                if "state.json" not in filenames:
                    continue
                low = os.path.basename(dirpath).lower()
                if any(h in low for h in _SEARCH_HINTS):
                    self._search_hit = os.path.join(dirpath, "state.json")
                    logger.info(
                        f"[autonomous_social] 已自动定位到 Humanoid Core 状态文件：{self._search_hit}"
                    )
                    return self._search_hit
        return self._search_hit

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
        # 已知路径都没命中：搜一次（结果会缓存，不会每个心跳重扫）
        return self._search_for_state()

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
            if self.mode != "standalone":
                self._warn_once(
                    "missing",
                    "[autonomous_social] 未找到 Humanoid Core state.json，联动未生效。"
                    "请确认已安装 astrbot_plugin_humanoid_core 且模式配置正确。"
                    f"当前模式：{self.mode}",
                )
            return None

        try:
            stat = os.stat(p)
        except OSError as e:
            self._warn_once(
                "oserror", f"[autonomous_social] Humanoid Core state.json 读取失败: {e}"
            )
            return None

        fingerprint = (p, stat.st_mtime_ns, stat.st_size)
        if fingerprint == self._cache_fingerprint and self._cache_root is not None:
            return self._cache_root

        try:
            with open(p, encoding="utf-8") as f:
                root = json.load(f)
        except json.JSONDecodeError as e:
            self._warn_once(
                "corrupt", f"[autonomous_social] Humanoid Core state.json 格式损坏: {e}"
            )
            return None
        except OSError as e:
            self._warn_once(
                "oserror", f"[autonomous_social] Humanoid Core state.json 读取失败: {e}"
            )
            return None
        except Exception as e:
            self._warn_once(
                "read_error", f"[autonomous_social] Humanoid Core state.json 读取异常: {e}"
            )
            return None

        # 成功读到一次：复位所有失败标记，下一次真出故障时还能提示（原来从不复位，
        # 启动瞬间一次瞬时故障就把后续所有提示永久静音了）。
        self._warned.clear()
        self._cache_fingerprint = fingerprint
        self._cache_root = root
        self._view_cache.clear()
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
        try:
            if root is None:
                root = self.read_root()
            if not root:
                return None

            # roles / self 都要校验类型：Core 换结构或 state.json 被手改时，
            # 裸 .get 链会 AttributeError，而这是每个用户每轮心跳都走的入口。
            roles = root.get("roles")
            role = roles.get(str(bot_id), {}) if isinstance(roles, dict) else {}
            selfs = role.get("self") if isinstance(role, dict) else None
            if not isinstance(selfs, dict):
                selfs = {}

            if not selfs and self.mode != "standalone" and bot_id not in self._warned_no_role:
                self._warned_no_role.add(bot_id)
                all_roles = list(roles.keys()) if isinstance(roles, dict) else []
                logger.warning(
                    "[autonomous_social] state.json 中不存在角色 %r，联动可能未生效。"
                    "可能是 bot_id 取值错误。state.json 中的角色：%s",
                    bot_id, all_roles,
                )

            if not selfs:
                return None

            return self._self_view(selfs, str(bot_id))
        except Exception as e:
            # 兜底：坏数据只该让这一层退化成「没有自身状态」，不该抛穿整轮心跳
            self._warn_once(
                "self_state_error",
                f"[autonomous_social] 读取 Humanoid Core 角色自身状态失败（本轮跳过）: {e}",
            )
            return None

    # 契约里的身体轴：v1 开始承诺的字段只加不改。
    _CONTRACT_BODY_KEYS = (
        "sleep_pressure", "sleep_debt", "hunger", "discomfort", "arousal", "social_desire",
        "last_sleep_hours",
    )

    @staticmethod
    def _night_window(routine: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        """从契约的 routine 里取她真实的作息夜 (起始小时, 结束小时)。

        取不到返回 None——调用方据此退回插件自己的 quiet_start/quiet_end，
        而不是拿 0 当成「零点开始」。
        """
        if not isinstance(routine, dict):
            return None
        start, end = routine.get("night_start_hour"), routine.get("night_end_hour")
        try:
            if start is None or end is None:
                return None
            return float(start) % 24.0, float(end) % 24.0
        except (TypeError, ValueError):
            return None

    def _self_view(self, selfs: Dict[str, Any], bot_id: str) -> Dict[str, Any]:
        """角色自身的身体视图。同一份 root 下只算一次，调用方只能读不能改。"""
        cached = self._view_cache.get(bot_id)
        if cached is not None:
            return cached
        try:
            view = self._build_self_view(selfs, bot_id)
        except Exception as e:
            # 兜底：这一层的结果会被 load_snapshot 展开进快照，抛出去会连累整轮心跳
            self._warn_once(
                "self_view_error",
                f"[autonomous_social] 构建 Humanoid Core 自身视图失败（退化为空视图）: {e}",
            )
            view = {}
        self._view_cache[bot_id] = view
        return view

    @staticmethod
    def _contract_v(contract: Any) -> int:
        """契约版本号。旧版 Core 没有这个字段；值被手改坏时当 0，**绝不抛**。"""
        try:
            return int(float(contract.get("v") or 0))
        except (TypeError, ValueError, AttributeError):
            return 0

    def _build_self_view(self, selfs: Dict[str, Any], bot_id: str) -> Dict[str, Any]:
        """把 Core 的角色自身状态整理成社交层要的视图。

        Core v2.14 会在 `self.contract` 里导出带版本号的身体快照。读到就用它（能拿到
        睡意、饥饿、想说话的程度这些旧字段里根本没有的量），读不到则退回旧字段，
        对老版 Core 保持兼容。
        """
        view: Dict[str, Any] = {
            "energy": selfs.get("energy"),
            "social_energy": selfs.get("social_energy"),
            "cycle": selfs.get("current_cycle_day"),
            "schedule": selfs.get("daily_schedule"),
            "weather": selfs.get("_cached_weather_obj"),
            "contract_v": None,
            "clock_offset_minutes": None,
            "routine": {},
            "night_window": None,
            "energy_text": "",
            "day": {},
            # 她手上还没做完的事（Core 跨天跟着的 ongoing）：社交层拿它当由头
            "ongoing": [],
            "persona": "",
            "holiday": "",
        }
        contract = selfs.get("contract")
        # 版本号必须容错：契约里的 v 是跨插件来的值，非数字时裸 int() 会抛穿
        # _build_self_view → _self_view → bot_self_state/load_snapshot 四层。
        if not isinstance(contract, dict) or self._contract_v(contract) < 1:
            return view
        body = contract.get("body") if isinstance(contract.get("body"), dict) else {}
        view["contract_v"] = self._contract_v(contract)
        if body.get("energy") is not None:
            view["energy"] = body.get("energy")
        if body.get("social_energy") is not None:
            view["social_energy"] = body.get("social_energy")
        if body.get("cycle_day") is not None:
            view["cycle"] = body.get("cycle_day")
        for key in self._CONTRACT_BODY_KEYS:
            view[key] = body.get(key)
        view["asleep"] = bool(body.get("asleep"))
        # Core 已经把能量翻成人话了（"精神不错"/"有点发沉"），比自己再翻一遍准
        view["energy_text"] = str(body.get("energy_text") or "").strip()
        view["feelings"] = contract.get("feelings") or []
        view["form"] = contract.get("form") or {}
        view["activity"] = contract.get("activity") or {}
        core_time = contract.get("time") or {}
        view["core_time"] = core_time
        # 老版 Core 不导出偏移：这里必须是 None 而不是 0，否则会把她的城市当成 UTC。
        offset = core_time.get("utc_offset_minutes")
        try:
            view["clock_offset_minutes"] = int(offset) if offset is not None else None
        except (TypeError, ValueError):
            view["clock_offset_minutes"] = None
        view["routine"] = contract.get("routine") or {}
        # 她实际的生物钟夜。Core 从今天的日程推出来的（夜猫子人格凌晨四点睡），
        # 比本插件配置里的 23:00 硬判准得多——安静时段应该用它，没接上就只能拿默认值。
        view["night_window"] = self._night_window(view["routine"])
        # 今天这条时线：刚做过什么 / 正在做什么 / 接下来做什么。主动消息靠它说得出
        # 「我刚从健身房回来」，而不是只会说「我在休息」。
        day = contract.get("day") if isinstance(contract.get("day"), dict) else {}
        view["day"] = {
            "doing": str(day.get("doing") or ""),
            "done": [str(x) for x in (day.get("done") or []) if str(x).strip()],
            "next": [str(x) for x in (day.get("next") or []) if str(x).strip()],
            # 她自己想做的事：社交层拿它当由头（「她今天想去那家书店」）。
            "wants": [str(x) for x in (contract.get("wants") or []) if str(x).strip()],
        }
        # 她手上还没做完的事：社交层拿它当由头（「还在整理那份个案笔记」）。
        view["ongoing"] = [
            str(x) for x in (contract.get("ongoing") or []) if str(x).strip()
        ]
        view["persona"] = str(contract.get("persona") or "")
        view["holiday"] = str(contract.get("holiday") or "").strip()
        # 契约里的 weather 是**故意的**空串（说明书/过期/未配置都导成空）。
        # 不能因为空就退回 `_cached_weather_obj`——那是从不清的旧缓存，
        # 停机几天后仍会拿几天前的天气派生由头（Core 那边正是要避免这个）。
        if "weather" in contract:
            _env = str(contract.get("weather") or "").strip()
            view["weather"] = {"env": _env} if _env else {}
        return view

    def load_snapshot(
        self,
        bot_id: str,
        user_id: str,
        root: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """生成单个用户的完整快照（bot 自身状态 + 用户情绪数据）。

        这是每次心跳、每个用户都会调的取数入口，所以最外层必须兜底：Core 契约里
        任何一个字段坏掉，都只让这个用户本轮没有快照，不该抛穿整轮心跳。

        Args:
            bot_id: bot 的 ID
            user_id: 用户 ID
            root: 预加载的 state.json 根 dict（避免重复读盘）

        Returns:
            快照 dict，或 None
        """
        try:
            return self._load_snapshot_inner(bot_id, user_id, root)
        except Exception as e:
            self._warn_once(
                "snapshot_error",
                f"[autonomous_social] 生成 Humanoid Core 快照失败（本轮跳过该用户）: {e}",
            )
            return None

    def _load_snapshot_inner(
        self,
        bot_id: str,
        user_id: str,
        root: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if self.mode == "standalone":
            return None

        if root is None:
            root = self.read_root()
        if not root:
            return None

        roles = root.get("roles")
        if not isinstance(roles, dict):
            return None
        role = roles.get(str(bot_id), {})
        if not isinstance(role, dict):
            return None

        selfs = role.get("self") if isinstance(role, dict) else None
        if not isinstance(selfs, dict):
            selfs = {}
        users = role.get("users")
        user = users.get(str(user_id), {}) if isinstance(users, dict) else {}
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

        # 这几个字段 Core 一直在记，而且在自己的契约里把它们列为稳定路径
        # （paths.user_nickname / user_said），社交层却从来没读过：
        # 于是她主动找人说话，既不按记住的称呼叫 TA，也不知道 TA 这阵子说过什么。
        said: List[Dict[str, Any]] = []
        said_raw = user.get("said")
        if isinstance(said_raw, list):
            for item in said_raw[:CORE_SAID_MAX]:
                if isinstance(item, dict):
                    text = str(item.get("said", "") or "").strip()
                    if text:
                        said.append({"said": text, "at": item.get("at")})
                elif str(item or "").strip():
                    said.append({"said": str(item).strip(), "at": 0.0})
        attention = user.get("attention")
        # 基线与「认识多久了」。
        #
        # 有运营为了让用户更好攻略，会把初始好感度设成 40 之类；聊久了直接飙到 80。
        # 只看**绝对值**的话，这种人和「一直停在 80 但从不回话」的人是同一档——
        # 而该被多找的恰恰是前者。Core 自己存着 base_affection 和 user 级 first_met
        # （prune 会搬、不会丢），所以**涨幅和认识时长**是拿得到的。
        base_aff = None
        # base_affection 是**用户级**数据（"有运营把初始好感度设成 40"这类），
        # 原来回退到角色级 selfs 里取，取到也不是这个人的基线；而且取值被
        # `isinstance(mood_data, dict)` 守着，mood 不是 dict 时整个分支被跳过
        # —— 那恰恰是回退本该发挥作用的场景。
        raw_base = mood_data.get("base_affection") if isinstance(mood_data, dict) else None
        if raw_base is None:
            raw_base = user.get("base_affection")
        try:
            base_aff = float(raw_base) if raw_base is not None else None
        except (TypeError, ValueError):
            base_aff = None
        try:
            first_met = float(user.get("first_met") or 0) or None
        except (TypeError, ValueError):
            first_met = None
        return {
            **self._self_view(selfs, str(bot_id)),
            "mood": mood_data,
            "affection": affection,
            "base_affection": base_aff,
            "first_met": first_met,
            "last_message": last_msg,
            "last_interaction": user.get("last_interaction"),
            # 亲密历史（成人档在用户级写的几个数）：社交层靠它判断
            # 「有没有和对方色色过 / 上一次多久前」，从而决定要不要主动问一句。
            "ccb_done_count": user.get("ccb_done_count"),
            "ccb_last_at": user.get("ccb_last_at"),
            "ccb_satisfy": user.get("ccb_satisfy"),
            # 她该怎么称呼这个人（Core 从 TA 自报的昵称里认定，改口会跟着变）
            "nickname": str(user.get("nickname", "") or "").strip(),
            "nickname_src": str(user.get("nickname_src", "") or "").strip(),
            # 她自己记着对方说过什么（原话、最新在前）
            "said": said,
            # 对方此刻的状态标签（Core 情绪分析出的短标签）
            "mood_tag": str(user.get("mood_tag", "") or "").strip(),
            # 她有多在意这个人
            "attention": (
                self._float_or_none(attention.get("care"))
                if isinstance(attention, dict)
                else None
            ),
        }

    @staticmethod
    def _float_or_none(value: Any) -> Optional[float]:
        """把跨插件来的值转成 float；非数字一律 None（同函数里 base_aff 已是这个口径）。"""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ─── 紧凑输出 ───────────────────────────────────────

    def compact(self, s: Optional[Dict[str, Any]], *, include_weather: bool = True) -> str:
        """将快照转为人类可读的简要文本（供 prompt 使用）。

        Args:
            s: 快照 dict
            include_weather: 要不要把天气写进来。主动消息传 `False`——天气是唯一
                「永远有、永远具体」的素材，摆在背景里模型就会顺手拿去凑一句，
                于是「不是天气就是天气」。它要出现只能作为由头（由 `anchors` 管着
                「变了没有、隔了多久」）。

        Returns:
            格式化的状态描述文本
        """
        if not s:
            return "没有可用的 Humanoid Core 状态。"

        parts: List[str] = []

        energy = s.get("energy")
        # Core 已经把能量翻成人话了（“精神不错”/“有点发沉”），直接用它的；
        # 没拿到契约时才退回数字。
        energy_text = str(s.get("energy_text") or "").strip()
        if energy_text:
            parts.append(f"状态: {energy_text}")
        elif energy is not None and energy != "":
            parts.append(f"能量: {energy}")

        social = s.get("social_energy")
        if social is not None and social != "":
            parts.append(f"社交能量: {social}")

        cycle = s.get("cycle")
        if cycle is not None and cycle != "":
            parts.append(f"生理周期: 第{cycle}天")

        act = s.get("activity")
        if isinstance(act, dict) and act.get("name"):
            loc = str(act.get("location") or "").strip()
            desc = f"此刻在: {act['name']}"
            if loc:
                desc += f"（{loc}）"
            parts.append(desc)

        sched = s.get("schedule")
        if isinstance(sched, list) and sched:
            parts.append(f"日程: 今日{len(sched)}个时段")

        day = s.get("day") or {}
        today_bits: List[str] = []
        if day.get("done"):
            today_bits.append("刚做过 " + "、".join(str(x) for x in day["done"][:2]))
        if day.get("doing"):
            today_bits.append(f"正在 {day['doing']}")
        if day.get("next"):
            today_bits.append("接下来 " + "、".join(str(x) for x in day["next"][:1]))
        if today_bits:
            parts.append("今天: " + "；".join(today_bits))

        weather = s.get("weather")
        if include_weather and isinstance(weather, dict) and weather:
            # 契约路径写的是 {"env": ...}，旧字段路径写的是 {"weather": ..., "env": ...}。
            # 原来只读 "weather"，于是接了 Core 的新契约时天气这一行恒为空。
            w = str(weather.get("env") or weather.get("weather") or "").strip()
            # 旧版 Core 的 env 自带「天气：」前缀，这里再加一个标签就成了
            # 「天气: 天气：晴…」。剥掉前缀（新 Core 已经不写了）。
            if w.startswith("天气："):
                w = w[len("天气："):].strip()
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
