"""Humanoid Core 只读桥接。

读取 Humanoid Core 的 state.json 并转换为标准化快照。
支持通过预加载 root dict 避免重复磁盘 IO。
mode != standalone 但读不到数据时，会打一次性 warning。

v1.6.1：
- 使用 AstrBot 统一的 logger 替代标准 logging
- 增加类型注解
- 改进错误处理
- 增强路径解析健壮性

v2.0.0（审核 social v1.28.17：P2-10 / P2-13）：
- 契约新鲜度：用 `generated_at` 判定契约是否过期（默认 45 分钟）。过期契约里的
  「此刻状态」（day/wake/routine/activity/event/ongoing/outfit/weather）不再当作现在，
  `_build_self_view` 会清空并打上 `contract_stale` / `contract_age_seconds` 标记；
  user/relationship 数据仍可用。对外提供 `contract_freshness()` / `contract_stale()`。
- 契约握手校验：读取 `schema` / `capabilities` / `fields_hash`，结构对不上时
  一次性 warning 并在视图里标记 `contract_handshake`（降级继续，绝不抛异常）。
"""

from __future__ import annotations

import hashlib
import json
import math
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

# ─── 契约新鲜度（P2-10）─────────────────────────────────

#: 契约 generated_at 比「现在」旧超过这么久，这份契约里的**此刻状态**就不当作现在。
#:
#: Core 正常在身体推进时重写契约（每分钟一次），45 分钟远大于正常重写间隔；而线上
#: 真实数据见过 14 天前的契约（Core 重启后角色实例惰性创建，空窗期只能读到旧文件）。
#: 阈值再小会误伤「用户刚启动、Core 还没来得及重写」的头几分钟。
CONTRACT_STALE_SECONDS = 45 * 60.0

# ─── 契约握手校验（P2-13）───────────────────────────────

#: 社交层真正消费的契约能力。capabilities 缺任何一项，就说明对面的契约结构与本插件
#: 的读取假设不一致 → 一次性 warning + 在视图里标记降级，绝不抛异常。
CONTRACT_EXPECTED_CAPABILITIES = (
    "day",
    "ongoing",
    "routine",
    "wake",
    "event",
    "identity",
    "outfit",
)
#: 契约结构标识前缀（Core 2.28.x 起导出 schema="humanoid.contract/2"）。
CONTRACT_SCHEMA_PREFIX = "humanoid.contract/"


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
        # 握手校验（P2-13）的一次性告警标记。**故意不放进 _warned**：那个字典在
        # read_root 每成功读一次就整体复位，而契约结构问题会一直存在，放进去等于每个
        # 心跳都告警一次。这里只在问题消失（握手校验通过）时重新武装。
        self._handshake_warned: Dict[str, bool] = {}

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

    @staticmethod
    def _text_list(value: Any) -> List[str]:
        """把跨插件传来的列表字段读成 `[str]`：非列表 / 脏元素 / 空串一律跳过。

        `day.dreams` 是 Core 2.28 才有的字段：旧版 Core 根本没有它（None），手改坏
        时可能是字符串、`[None]`、或者夹着字典。缺字段必须容错成 []——「没有梦可
        分享」不是错误；夹一个 `"None"` 进去更糟，社交层会把它当真事说出去。
        """
        if not isinstance(value, list):
            return []
        out: List[str] = []
        for item in value:
            if item is None or isinstance(item, (dict, list, tuple, set)):
                continue
            try:
                text = str(item).strip()
            except Exception:
                continue
            if text:
                out.append(text)
        return out

    # ─── 契约新鲜度 / 握手校验（P2-10 / P2-13）──────────────

    def contract_freshness(
        self,
        contract: Any,
        *,
        now: Optional[float] = None,
        max_age: Optional[float] = None,
    ) -> Dict[str, Any]:
        """判断一份契约快照的**新鲜度**（P2-10）：generated_at 距今多久、还能不能信。

        Core 在契约里导出 generated_at（epoch 秒），但契约本身不参与 state.json 的
        重写判定——Core 重启后角色实例按首条消息惰性创建，空窗期社交侧只能读到旧文件
        （线上真实数据见过 14 天前的契约）。旧实现从不看 generated_at，于是会把
        几天前的 day.doing（「正在工作/在睡」）、wake、routine 当成此刻状态生成主动消息。

        本方法只判定、不改数据；`_build_self_view` 按结果把「此刻状态」清空
        （见 `_neutralize_stale_contract`）。**无法判定时不算过期**：旧版 Core 没有
        generated_at，按未校验处理（与加这个机制之前逐字等价）。

        返回 dict（键固定）：

        * checked: 是否拿到了可用时间戳
        * generated_at: 契约时间（epoch 秒）或 None
        * age_seconds: 距今秒数（未来时间按 0 处理）或 None
        * max_age_seconds: 本次使用的阈值
        * fresh: True/False；未校验时为 None
        * stale: True 表示「此刻状态不可信」（未校验时 False）
        * reason: 人话说明
        """
        limit = float(max_age if max_age is not None else CONTRACT_STALE_SECONDS)
        info: Dict[str, Any] = {
            "generated_at": None,
            "age_seconds": None,
            "max_age_seconds": limit,
            "checked": False,
            "fresh": None,
            "stale": False,
            "reason": "",
        }
        if not isinstance(contract, dict):
            info["reason"] = "契约不是 dict，无法判断新鲜度"
            return info
        raw = contract.get("generated_at")
        try:
            generated_at = float(raw)
        except (TypeError, ValueError):
            info["reason"] = "契约没有可解析的 generated_at（旧版 Core），新鲜度未校验"
            return info
        if (
            generated_at != generated_at  # NaN
            or generated_at in (float("inf"), float("-inf"))
            or generated_at <= 0.0
        ):
            info["reason"] = "契约 generated_at 不是有效时间戳，新鲜度未校验"
            return info
        now_value = float(now) if now is not None else time.time()
        age = max(0.0, now_value - generated_at)
        info.update({"generated_at": generated_at, "age_seconds": age, "checked": True})
        if age > limit:
            info.update(
                {
                    "fresh": False,
                    "stale": True,
                    "reason": (
                        f"契约 generated_at 距今约 {age / 60.0:.0f} 分钟"
                        f"> {limit / 60.0:.0f} 分钟：此刻状态不当作现在"
                    ),
                }
            )
        else:
            info.update({"fresh": True, "reason": ""})
        return info

    def contract_stale(
        self,
        contract: Any,
        *,
        now: Optional[float] = None,
        max_age: Optional[float] = None,
    ) -> bool:
        """契约是否已过期（`contract_freshness` 的布尔快捷方式）。"""
        return bool(
            self.contract_freshness(contract, now=now, max_age=max_age).get("stale")
        )

    @staticmethod
    def _fields_hash(contract: Dict[str, Any]) -> str:
        """按 Core 的算法重算握手结构指纹（排序后的键路径 sha256 前 16 位）。

        与 core/humanoid/link.py::_fields_hash 同一套算法（只 walk dict、键路径排序、
        sha256）。Core 写 fields_hash 时已经先把 schema / capabilities 放进 dict，
        所以重算时排除 fields_hash 本身即可。算法对不上（旧版/未来改算法）时也只会
        得到不一致 → 一次性 warning，不影响读取。
        """
        paths: List[str] = []

        def walk(node: Any, prefix: str) -> None:
            if not isinstance(node, dict):
                return
            for key in sorted(node):
                path = f"{prefix}.{key}" if prefix else str(key)
                paths.append(path)
                walk(node[key], path)

        walk({k: v for k, v in contract.items() if k != "fields_hash"}, "")
        raw = "\n".join(sorted(paths)).encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]

    def _handshake_problems(self, contract: Dict[str, Any]) -> List[str]:
        """P2-13：校验契约的握手三键（schema / capabilities / fields_hash）。

        只返回问题列表，**绝不抛**：这是「把猜变成校验」的观测点，结构对不上时本该
        留一条能搜到的日志 + 在视图里标记降级，不能让主动消息发不出去。
        v1 老契约没有这三个键 → 返回空列表（未校验，不告警）。
        """
        problems: List[str] = []
        version = self._contract_v(contract)
        if version < 1:
            # 契约版本号是「只加不改」的地基：读不出 v 时其它字段一个都不该信。
            problems.append(f"契约 v 不可用：{contract.get('v')!r}")
        schema = contract.get("schema")
        if schema is not None:
            if not isinstance(schema, str) or not schema.startswith(CONTRACT_SCHEMA_PREFIX):
                problems.append(
                    f"契约 schema 不认识：{schema!r}"
                    f"（期望 {CONTRACT_SCHEMA_PREFIX}<n>）"
                )
            elif version >= 1:
                expected = f"{CONTRACT_SCHEMA_PREFIX}{version}"
                if schema != expected:
                    problems.append(f"契约 schema 与 v 不一致：schema={schema!r}，v={version}")
        capabilities = contract.get("capabilities")
        if capabilities is not None:
            if not isinstance(capabilities, (list, tuple, set)):
                problems.append(
                    f"契约 capabilities 形状不对：{type(capabilities).__name__}"
                )
            else:
                present = {str(item) for item in capabilities}
                missing = [
                    name for name in CONTRACT_EXPECTED_CAPABILITIES if name not in present
                ]
                if missing:
                    problems.append(f"契约 capabilities 缺本插件消费的项：{missing}")
        fields_hash = contract.get("fields_hash")
        if fields_hash is not None:
            if not isinstance(fields_hash, str) or not fields_hash.startswith("sha256:"):
                problems.append(f"契约 fields_hash 格式不认识：{fields_hash!r}")
            else:
                recomputed = self._fields_hash(contract)
                if recomputed and recomputed != fields_hash:
                    problems.append(
                        f"契约 fields_hash 与结构不一致：声明 {fields_hash}，重算 {recomputed}"
                    )
        return problems

    def _warn_handshake_once(self, problem: str) -> None:
        """同一类握手问题只 warning 一次（问题消失后由 `_check_handshake` 重新武装）。"""
        if self._handshake_warned.get(problem):
            return
        self._handshake_warned[problem] = True
        logger.warning(
            "[autonomous_social] Humanoid Core 契约握手校验未通过："
            f"{problem}（按降级继续，不抛异常）"
        )

    def _check_handshake(self, contract: Any) -> Optional[Dict[str, Any]]:
        """校验握手键并（出问题时）一次性告警；返回视图标记，未校验时返回 None。

        返回 `{"checked": True, "ok": bool, "problems": [...]}`，只在契约**带**了
        握手键时出现（v1 老契约没有这三键 → 未校验，不占视图字段）。
        """
        if not isinstance(contract, dict):
            return None
        if not any(key in contract for key in ("schema", "capabilities", "fields_hash")):
            return None
        problems = self._handshake_problems(contract)
        if not problems:
            # 结构对了：重新武装一次性告警，将来再漂移还能提示。
            self._handshake_warned.clear()
            return {"checked": True, "ok": True, "problems": []}
        for problem in problems:
            self._warn_handshake_once(problem)
        return {"checked": True, "ok": False, "problems": problems}

    @staticmethod
    def _neutralize_stale_contract(view: Dict[str, Any]) -> None:
        """契约过期时，把「此刻状态」清空（P2-10）：旧答案比没有答案更糟。

        清的都是**由头/上下文派生**字段：day（正在做什么/刚做过/接下来/想做的事/梦）、
        wake（刚醒）、routine/night_window（作息）、activity（在哪）、event（刚发生的事）、
        ongoing（手上没做完的事）、outfit（此刻穿着）、weather（此刻天气）。
        它们回答「她现在在干什么」，几天前的答案会让模型一本正经地说瞎话。

        保留：identity（她是谁，稳定）、body 标量（energy 等，读方本来按「最近已知值」
        用）、feelings/form（表达约束，不是时刻事实）、contract_v 与新鲜度标记。
        """
        view["activity"] = {}
        view["routine"] = {}
        view["night_window"] = None
        view["wake"] = {}
        view["event"] = {}
        view["ongoing"] = []
        view["outfit"] = {}
        view["weather"] = {}
        view["day"] = {
            "doing": "",
            "done": [],
            "next": [],
            "wants": [],
            "dreams": [],
            "event": {},
            "wake": {},
        }

    @staticmethod
    def _wake_block(value: Any) -> Dict[str, Any]:
        """把契约的 `wake` 块读成干净的 dict；拿不到就是空 dict。

        Core 2.28 起给，六个键：`asleep` / `wake_at` / `inertia_until` /
        `inertia_minutes_left` / `disturbed` / `last_sleep_hours`。
        社交层（anchors._wake_cue）只用 `asleep` / `wake_at` / `last_sleep_hours`，
        但**整块照抄**：这是跨插件契约，缺字段就是缺信息，而不是「把它变成
        我们要的形状」。与 `_event_block` 的分工：那个只留 phrase（Core 故意
        不给会变的秒数），这个块的值本身稳定，抄下来就够。

        类型必须逐键洗：契约是外部输入，手改坏时可能是字符串/None。
        `_num_or_none` 把读不出来的归 None，由头派生据此当「没有这个信息」，
        而不是抛穿 `_build_self_view` → `load_snapshot` 两层。
        """
        if not isinstance(value, dict):
            return {}

        def _num_or_none(raw: Any) -> Optional[float]:
            try:
                out = float(raw)
            except (TypeError, ValueError):
                return None
            return out if math.isfinite(out) else None

        wake_at = _num_or_none(value.get("wake_at"))
        inertia = _num_or_none(value.get("inertia_until"))
        hours = _num_or_none(value.get("last_sleep_hours"))
        left = _num_or_none(value.get("inertia_minutes_left"))
        return {
            "asleep": bool(value.get("asleep")),
            "wake_at": wake_at,
            "inertia_until": inertia,
            "inertia_minutes_left": int(left) if left is not None else None,
            "disturbed": bool(value.get("disturbed")),
            "last_sleep_hours": hours,
        }

    def _event_block(self, value: Any) -> Dict[str, Any]:
        """把契约的 `event` 块读成干净的单键 dict；拿不到就是空 dict。

        契约是跨插件来的：旧版 Core 没有这个字段（None）、手改坏时可能是字符串
        或列表。**缺字段与脏数据一律当「这会儿没有这样一件事」**——社交层不该
        因为 Core 升级就凭空多出一件没发生过的事。

        只保留 `phrase`：Core 故意不在契约里给 `age_seconds`（它每秒都在变，
        会让那半小时里每分钟重写一遍全量 state.json）。由头有时效，
        那是 anchors 的 TTL 在管，不是这里的活。
        """
        if not isinstance(value, dict):
            return {}
        phrase = str(value.get("phrase") or "").strip()
        if not phrase:
            return {}
        return {"phrase": phrase}

    @staticmethod
    def _clean_text(value: Any) -> str:
        """跨插件来的**单行文本**字段：缺字段 / 脏容器 / 布尔一律空串，绝不抛。

        `nickname` / `display_name` / `char_name` 这类值会被拼进提示词给模型照着叫，
        所以只收 str 与数字（数字名如「123」也确实是有的），容器（手改坏时的 `{}`、
        `[]`）直接当「没有」——`str({})` 会变成「{}」然后被当成名字念出去。
        """
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, bool) or value is None:
            return ""
        if isinstance(value, (int, float)):
            return str(value).strip()
        return ""

    @staticmethod
    def _identity_block(value: Any) -> Dict[str, Any]:
        """契约的 `identity` 块（Core 2.29 起给）：她的名字 / 人设标签。

        缺字段（老 Core）、脏数据（字符串/列表）一律空 dict——这一层只是**加分项**，
        不该因为另一个插件升级或手改坏就把整轮心跳弄挂。
        """
        if not isinstance(value, dict):
            return {}
        identity = {
            "char_name": CoreBridge._clean_text(value.get("char_name")),
            "persona_label": CoreBridge._clean_text(value.get("persona_label")),
        }
        return identity if (identity["char_name"] or identity["persona_label"]) else {}

    @staticmethod
    def _outfit_block(value: Any) -> Dict[str, Any]:
        """契约的 `outfit` 块（Core 2.29 起给）：她今天穿什么。

        形状 `{enabled, text, source}`；缺字段 / 脏数据一律空 dict（理由同
        `_identity_block`）。`enabled=False` 本身是有效信息（穿搭关闭），只要块不是空的
        就照抄，由消费方自己决定用不用。
        """
        if not isinstance(value, dict):
            return {}
        outfit = {
            "enabled": bool(value.get("enabled")),
            "text": CoreBridge._clean_text(value.get("text")),
            "source": CoreBridge._clean_text(value.get("source")),
        }
        return outfit if (outfit["enabled"] or outfit["text"] or outfit["source"]) else {}

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
            # 醒来状态（Core 2.28 起）。旧版 Core / 读不到契约时是空 dict，
            # 由头派生按「这会儿没刚醒」处理。契约里它在顶层，视图照抄。
            "wake": {},
            "persona": "",
            "holiday": "",
            # 她自己的身份/穿搭（Core 2.29 起给：契约 identity / outfit 块）。
            # 旧版 Core 没有这两个键 → 空 dict，消费方按「不知道」处理；
            # 这两个块**不进 compact()**（名字是称呼，不是状态摘要）。
            "identity": {},
            "outfit": {},
            # 契约新鲜度 / 握手校验（P2-10 / P2-13）。没有契约时保持与旧版逐字等价：
            # contract_fresh=None 表示未校验、contract_stale=False 表示没判过期。
            "contract_fresh": None,
            "contract_stale": False,
            "contract_age_seconds": None,
        }
        contract = selfs.get("contract")
        # 版本号必须容错：契约里的 v 是跨插件来的值，非数字时裸 int() 会抛穿
        # _build_self_view → _self_view → bot_self_state/load_snapshot 四层。
        if not isinstance(contract, dict) or self._contract_v(contract) < 1:
            return view
        freshness = self.contract_freshness(contract)
        stale = bool(freshness.get("stale"))
        handshake = self._check_handshake(contract)
        body = contract.get("body") if isinstance(contract.get("body"), dict) else {}
        view["contract_v"] = self._contract_v(contract)
        view["contract_fresh"] = freshness.get("fresh")
        view["contract_stale"] = stale
        view["contract_age_seconds"] = freshness.get("age_seconds")
        if handshake is not None:
            # 只在契约带了握手键时出现（v1 老契约 → 不占字段）。
            view["contract_handshake"] = handshake
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
            # 她昨晚（或白天补觉时）梦到的事：Core 2.28 起给。群主动素材与私聊
            # 由头都拿它当「她自己的生活」的一类；旧版 Core 没有这个字段 → []。
            "dreams": self._text_list(day.get("dreams")),
            # 刚刚发生的事够重（被夸/被骂/被敷衍）：Core 2.29 起给，在契约
            # **顶层**（与 ongoing/wants 同级，不在 day 里）。旧版 Core 没有
            # 这个字段 → {}，由头派生按「这会儿没有这样一件事」处理。
            "event": {},
        }
        # 醒来状态（Core 2.28 起给，v1.28.6 起当由头）：睡没睡够、醒来多久。
        # 旧版 Core 没有这个字段 → {}，由头派生按「这会儿没刚醒」处理。
        # 契约里它在**顶层**（与 ongoing/wants/day 同级），视图照抄同一处；
        # `day` 里再放一份同一个对象，两处共用（与 `event` 同一套做法）——
        # `anchors._wake_cue` 与 `groupflow.life_lines` 读的是不同入口。
        # 与 `event` 不同：这个块的键是数值/布尔，不能走 `_event_block`
        # （那个只留 `phrase`），所以单独一个读取器。
        _wake = self._wake_block(contract.get("wake"))
        view["wake"] = _wake
        view["day"]["wake"] = _wake
        # 事件从契约顶层读一次，两处共用同一个 dict：`anchors._derive_from_state`
        # 读顶层 `body["event"]`（与 ongoing 同级），`groupflow` 读 `day["event"]`
        # （与 wants/dreams 同级）。给同一个对象，不会出现两份不一致。
        _event = self._event_block(contract.get("event"))
        view["event"] = _event
        view["day"]["event"] = _event
        # 她手上还没做完的事：社交层拿它当由头（「还在整理那份个案笔记」）。
        view["ongoing"] = [
            str(x) for x in (contract.get("ongoing") or []) if str(x).strip()
        ]
        view["persona"] = str(contract.get("persona") or "")
        view["holiday"] = str(contract.get("holiday") or "").strip()
        # 她自己的名字 / 人设标签、她今天穿什么（Core 2.29 起给）。
        # 旧版 Core 没有这两个键 → 空 dict（与加这个字段之前逐字等价：compact 不读它们）。
        view["identity"] = self._identity_block(contract.get("identity"))
        view["outfit"] = self._outfit_block(contract.get("outfit"))
        # 契约里的 weather 是**故意的**空串（说明书/过期/未配置都导成空）。
        # 不能因为空就退回 `_cached_weather_obj`——那是从不清的旧缓存，
        # 停机几天后仍会拿几天前的天气派生由头（Core 那边正是要避免这个）。
        if "weather" in contract:
            _env = str(contract.get("weather") or "").strip()
            view["weather"] = {"env": _env} if _env else {}
        if stale:
            # 契约太旧（P2-10）：清掉「此刻状态」，别把几天前的 day.doing/wake 当现在。
            # 身份/身体标量/情绪等「最近已知值」保留，消费方本来就有不知道的兜底。
            self._neutralize_stale_contract(view)
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
        # 称呼三级回退（老 Core 只有 nickname；2.29 起才有 display_name）：
        #   1. `nickname`  —— Core「认定」过的称呼（auto_nickname 校验很严，多数人是空）
        #   2. `display_name` —— 名片名，已清洗（Core 2.29 起给）
        #   3. `name` —— 最后兜底：Core 用户档里若有原始名就用它（老数据也可能没有）
        # 三级都空时 `call_name` 就是空串：宁可明确「不知道叫什么」，也不自己编。
        nickname = self._clean_text(user.get("nickname"))
        display_name = self._clean_text(user.get("display_name"))
        local_name = self._clean_text(user.get("name"))
        if nickname:
            call_name, name_source = nickname, "nickname"
        elif display_name:
            call_name, name_source = display_name, "display_name"
        elif local_name:
            call_name, name_source = local_name, "local"
        else:
            call_name, name_source = "", ""
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
            # 场景态（Core 2.28.7 起把这几根键写全）：在不在场景里、到哪个阶段、
            # 今天几次、余韵窗口到什么时候。老版本 Core 根本没有这些键 → None，
            # 调用方按「不知道」处理；不能拿 0 当「没发生过」，否则老 Core 会被
            # 面板说成「不在场景里」（其实只是它没说过）。
            "ccb_stage": self._float_or_none(user.get("ccb_stage")),
            "ccb_turns": self._float_or_none(user.get("ccb_turns")),
            "ccb_aftercare_until": self._float_or_none(
                user.get("ccb_aftercare_until")
            ),
            # 亲近欲与基线（mood 档案里的两个轴）：面板与主动消息上下文都想知道
            # 「她这会儿有多想」。base_libido 真的可能是 0（她从没什么欲望），
            # 所以只能用 None 当「没给」——`base or libido` 会把 0 悄悄吃掉。
            "libido": self._float_or_none(
                mood_data.get("libido") if isinstance(mood_data, dict) else None
            ),
            "base_libido": self._float_or_none(
                mood_data.get("base_libido") if isinstance(mood_data, dict) else None
            ),
            # 她该怎么称呼这个人（Core 从 TA 自报的昵称里认定，改口会跟着变）
            "nickname": nickname,
            "nickname_src": str(user.get("nickname_src", "") or "").strip(),
            # 名片名（Core 2.29 起给：已清洗、≤24 字）。Core 的 `auto_nickname` 校验很严，
            # 带数字/emoji/装饰的名片一律不认，所以大多数用户 `nickname` 是空——名片名是
            # 主动消息**唯一**能叫得出名字的来源，不能只躺在 state.json 里。
            "display_name": display_name,
            # 她开口时该怎么叫 TA：Core 认定的称呼 → 名片名 → 本地记下的名片名 → 空。
            # `name_source` 让生成侧决定措辞（「你一直叫TA」 vs 「TA 的名片名是」）。
            "call_name": call_name,
            "name_source": name_source,
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

        # 亲密状态单独一行：模型要看得出「她这会儿在不在状态」，而不是只看到一个
        # 裸的亲近欲数字。只在 Core 真的给了 ccb 字段时才出——老版本 Core / ccb
        # 关闭时，这条主动消息的上下文必须与以前逐字一致。
        intimacy = self._intimacy_line(s)
        # 亲近欲只在「情绪」行里出现一次：新行已经给了「x/50（基线 y）」，再在
        # 情绪行重复一遍同一个数只烧 token，还会让模型以为是两个不同的量。
        show_libido_in_mood = "亲近欲" not in intimacy

        mood = s.get("mood")
        if isinstance(mood, dict) and mood:
            dims = []
            for k, label in (("libido", "亲近欲"), ("aggression", "攻击性")):
                if k == "libido" and not show_libido_in_mood:
                    continue
                v = mood.get(k)
                if v is not None:
                    dims.append(f"{label}{v}")
            if dims:
                parts.append(f"情绪: {' / '.join(dims)}")

        if intimacy:
            parts.append(intimacy)

        return "\n".join(parts) if parts else "Humanoid Core 当前没有可用的简要状态。"

    @staticmethod
    def _intimacy_line(s: Dict[str, Any]) -> str:
        """把 ccb 场景与亲近欲拼成 prompt 里的一行；拿不到就回空串。

        两件事分开说：**在不在场景里**（行为层面的状态，决定她说话的分寸）与
        **亲近欲到哪了**（身体层面的量）。只有亲近欲没有场景时也照样报——那正是
        「有点想但还没到」的状态，模型以前完全看不见。
        """
        if not isinstance(s, dict):
            return ""
        mood = s.get("mood") if isinstance(s.get("mood"), dict) else {}
        stage = CoreBridge._float_or_none(s.get("ccb_stage"))
        satisfy = CoreBridge._float_or_none(s.get("ccb_satisfy"))
        if stage is None:
            # 场景字段缺失，但只要有历史/次数，就说明 Core 在记这条线，别整行吞掉。
            has_history = any(
                s.get(k) is not None
                for k in ("ccb_turns", "ccb_last_at", "ccb_done_count",
                          "ccb_aftercare_until", "ccb_satisfy")
            )
            if not has_history:
                return ""
        libido = CoreBridge._float_or_none(s.get("libido"))
        if libido is None:
            libido = CoreBridge._float_or_none(mood.get("libido"))
        base_libido = CoreBridge._float_or_none(s.get("base_libido"))
        if base_libido is None:
            base_libido = CoreBridge._float_or_none(mood.get("base_libido"))
        bits: List[str] = []
        if stage is not None:
            if stage > 0:
                bits.append(f"在场景里·阶段{int(stage)}")
                if satisfy is not None:
                    bits.append(f"满足{int(round(satisfy))}/100")
            else:
                bits.append("未在场景")
        if libido is not None:
            text = f"亲近欲 {libido:g}/50"
            if base_libido is not None:
                text += f"（基线 {base_libido:g}）"
            bits.append(text)
        until = CoreBridge._float_or_none(s.get("ccb_aftercare_until"))
        if until is not None and until > time.time():
            bits.append(f"余韵还剩 {max(0, int((until - time.time()) / 60))} 分钟")
        if not bits:
            return ""
        return "亲密: " + " · ".join(bits)
