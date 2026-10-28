"""Humanoid Core 只读门面（契约 v2 的主路径）。

## 为什么不是「继续读 state.json」

读文件这条路有三个治不好的毛病：快照会旧（`time` 块为了不让契约变成常驻写盘源，
刻意不参与重写判定）、形状靠猜（`feelings` 写 `list[str]`、读按 `list[dict]`，
两边都不报错，只是那条由头永不产出）、以及两边各拼一套上下文（普通回复用 Core 的
框架句，主动消息只有这里自己拼的 `compact()` 摘要）。

门面在**同一个 AstrBot 进程**里直接问 Core，按需现算、结构化返回。

## 怎么找到它

宿主注册表 → `meta.star_cls.__module__` → `sys.modules[模块]` → `get_core_facade()`。
这套写法本仓库已经给 memory_companion 用过一遍（见 `social/memory_bridge.py`），
Core 那边也有同一份——不是新机制，是把已有的两处接线互相接上。

## 失败怎么处理

* 没装 / 没起来 / 版本太旧 / 调用抛异常 → 一律返回 `None`，调用方退回读文件
  （`core_bridge`），**行为与今天完全一致**；
* 找不到只做短期负缓存（`_RETRY_SECONDS`），Core 后装能自己接上，不用重载插件；
* 所有失败原因都记在 `status()` 里，`/自主社交状态` 会打印，不用去翻日志。

## v2.0.0 修复（审核 social v1.28.17：P2-09 门面侧）

门面缓存原来**一次命中就永久返回**（`_CACHE["facade"]` 命中即 return，`_RETRY_SECONDS`
只约束「没找到之后的重试」）。Core 热重载/卸载后，旧 `CoreFacade` 绑的是已经
`stop()` 的旧 `RoleManager`，它的 `role_ids()` 从此永远为空，但社交侧仍拿着旧对象、
状态页还写「已连接」——只有重载 social 才恢复。

现在缓存命中前先做**存活探测**，并且每 `PROBE_INTERVAL_SECONDS`（60s）最多探一次：

* 新门面（Core 3.0.0+）有 `is_current()`：它按实例代次判活，是首选；
* 旧门面有 `role_ids()`：**空列表**视为失效（Core 重载后旧 RoleManager 的 `get_all()`
  返回空；Core 2.28.x 的 `role_ids()` 空/异常都返回 []，方向一致）；
* 两样都没有的旧门面无法判定，按「可用」处理（那是本探测出现之前的行为）。

探测到失效就清缓存、在**同一次调用里**重新发现（返回新门面或 None），不会永久断链。
`find_core_facade(force=True)` 仍然无条件重查；`probe()` 供引擎主动探测，
`reset()` 供配置热重载/手动重连使用。
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional

from astrbot.api import logger

#: 宿主注册表里可能出现的身份（name / display_name / 目录名 / 模块路径）。
CORE_PLUGIN_NAMES = {
    "astrbot_plugin_humanoid_core",
    "humanoid_core_plugin",
    "HumanoidCore",
    "humanoid_core",
    "人形化伴侣插件",
}

#: 找不到时的重试间隔（秒）。Core 后装 / 目录后建 / 一次查表失败之后不能永久断链。
_RETRY_SECONDS = 300.0

#: 门面至少要有的方法。少一个就当作「版本太旧」，退回文件。
_REQUIRED = ("api_version", "capabilities", "clock", "brief", "state_fingerprint")

#: 缓存命中时做存活探测的最小间隔（秒）。Core 热重载后旧门面必须能被发现，
#: 但也不能每次 `find_core_facade()` 都去问一遍 `is_current()` / `role_ids()`。
PROBE_INTERVAL_SECONDS = 60.0

# 模块级缓存：每个进程一个。`reset()` 供测试与热重载用。
_CACHE: Dict[str, Any] = {
    "facade": None,
    "resolved": False,
    "at": 0.0,
    "reason": "",
    # 存活探测（P2-09）：最近一次探测的时刻/结果，以及累计判死次数（状态页可看）。
    "checked_at": 0.0,
    "alive": False,
    "dead_count": 0,
}


def reset() -> None:
    """清掉缓存（测试、热重载、用户手动重连时用）。"""
    _CACHE.update(
        {
            "facade": None,
            "resolved": False,
            "at": 0.0,
            "reason": "",
            "checked_at": 0.0,
            "alive": False,
            "dead_count": 0,
        }
    )


def status() -> Dict[str, Any]:
    """给 `/自主社交状态` 看的解析结果。

    旧键（connected/api_version/capabilities/reason/resolved）**保持不变**；
    v2.0.0 增加 `alive`（最近一次探测是否还活着）、`probed_at`（最近探测的 monotonic
    时刻，从未探测为 0）、`dead_count`（累计判死次数，Core 反复重载时能看出来）。
    """
    facade = _CACHE.get("facade")
    return {
        "connected": facade is not None,
        "api_version": _safe_int(getattr(facade, "api_version", None)),
        "capabilities": capabilities(facade),
        "reason": str(_CACHE.get("reason") or ""),
        "resolved": bool(_CACHE.get("resolved")),
        "alive": bool(_CACHE.get("alive")),
        "probed_at": float(_CACHE.get("checked_at") or 0.0),
        "dead_count": int(_CACHE.get("dead_count") or 0),
    }


def capabilities(facade: Any) -> List[str]:
    """门面声明的能力列表；拿不到 / 形状不对一律 []（绝不抛）。

    `capabilities()` 是门面**唯一**的版本谈判方式：API_VERSION 只加不改，
    所以「有没有子能力」不能看版本号猜，只能问它。主动消息这边用它判断
    `user`（称呼/好感/说过的话）与 `identity`（她自己的名字）是否直给。
    """
    if facade is None:
        return []
    try:
        raw = facade.capabilities()
    except Exception as exc:
        logger.debug(f"[autonomous_social] 门面 capabilities 失败: {exc}")
        return []
    if not isinstance(raw, (list, tuple, set)):
        return []
    out: List[str] = []
    for item in raw:
        text = _text_of(item)
        if text and text not in out:
            out.append(text)
    return out


def has_capability(facade: Any, name: str) -> bool:
    """门面有没有某项子能力（见 `capabilities()`）。拿不到就当没有。"""
    want = _text_of(name)
    return bool(want) and want in capabilities(facade)


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value())
    except Exception:
        return None


def _text_of(value: Any) -> str:
    return str(value or "").strip()


def probe(facade: Any) -> bool:
    """这个门面现在还活着吗？（P2-09：Core 热重载后旧门面必须能被判死）

    探测顺序：

    1. `is_current()`（Core 3.0.0 新增）：按插件实例代次判活，最准；
    2. `role_ids()`：**空列表**视为失效。Core 重载/卸载后旧 `RoleManager.get_all()`
       永远返回空；Core 2.28.x 的 `role_ids()` 在异常时也返回 []，方向一致；
    3. 两样都没有的旧门面：无法判定，按「可用」处理（退回本探测出现之前的行为）。

    只探测、不改动作：结果写进 `_CACHE["alive"]` / `checked_at`，供 `status()` 展示。
    """
    if facade is None:
        _CACHE["alive"] = False
        return False
    alive: Optional[bool] = None
    current = getattr(facade, "is_current", None)
    if callable(current):
        try:
            alive = bool(current())
        except Exception:
            alive = False
    if alive is None:
        role_ids = getattr(facade, "role_ids", None)
        if callable(role_ids):
            try:
                roles = role_ids()
            except Exception:
                roles = None
            if roles is not None:
                try:
                    alive = bool(list(roles))
                except TypeError:
                    alive = bool(roles)
    if alive is None:
        # 既没有 is_current 也没有 role_ids：老门面无法判定，保持旧行为（可用）。
        alive = True
    _CACHE["alive"] = bool(alive)
    _CACHE["checked_at"] = time.monotonic()
    if not alive:
        _CACHE["dead_count"] = int(_CACHE.get("dead_count") or 0) + 1
    return bool(alive)


#: `probe()` 的同义词：引擎侧若写成 `alive(facade)` 也能工作。
alive = probe


def _identity_matches(meta: Any) -> bool:
    """宿主里的这条记录是不是 Core。认 name / display_name / 目录名 / 模块路径。"""
    try:
        names = {
            _text_of(getattr(meta, attr, ""))
            for attr in ("name", "display_name", "root_dir_name", "module_path")
        }
    except Exception:
        return False
    return bool(names & CORE_PLUGIN_NAMES)


def _module_of(meta: Any) -> Any:
    module = getattr(meta, "module", None)
    if module is not None:
        return module
    star_cls = getattr(meta, "star_cls", None)
    mod_name = getattr(star_cls, "__module__", "") if star_cls is not None else ""
    return sys.modules.get(mod_name) if mod_name else None


def _facade_from(meta: Any) -> Optional[Any]:
    """从宿主记录里取出门面。取不到就返回 None，并把原因记下来。"""
    module = _module_of(meta)
    factory = getattr(module, "get_core_facade", None)
    if not callable(factory):
        _CACHE["reason"] = (
            "找到 humanoid_core，但它没有导出 get_core_facade()——"
            "说明装的是旧版 Core（门面是 2.27.39 起的），会继续读 state.json"
        )
        return None
    try:
        facade = factory()
    except Exception as exc:
        _CACHE["reason"] = f"get_core_facade() 抛异常：{exc!r}"
        return None
    if facade is None:
        _CACHE["reason"] = "Core 已加载但门面还没就绪（插件正在启动），本轮读 state.json"
        return None
    missing = [name for name in _REQUIRED if not callable(getattr(facade, name, None))]
    if missing:
        _CACHE["reason"] = f"Core 的门面缺方法 {missing}，当作版本太旧，退回读 state.json"
        return None
    return facade


def find_core_facade(context: Any = None, *, force: bool = False) -> Optional[Any]:
    """拿到 Core 的只读门面；拿不到返回 `None`（调用方退回读文件）。

    * `force=True`：**无条件重查**——先清掉缓存（包括已命中的正缓存），再查一次
      宿主注册表。用于状态页刷新、配置热重载、以及探测到门面失效之后的重连。
    * 缓存命中时（P2-09）：每 `PROBE_INTERVAL_SECONDS` 至少探测一次存活；探测到失效
      就清缓存并在**同一次调用里**重新发现，返回新门面或 None。不会永久断链。
    * 未命中（负缓存）仍受 `_RETRY_SECONDS` 约束：Core 后装能自己接上，又不会每次
      调用都查一遍注册表。
    """
    if force:
        _CACHE.update(
            {
                "facade": None,
                "resolved": False,
                "at": 0.0,
                "reason": "",
                "checked_at": 0.0,
                "alive": False,
            }
        )
    cached = _CACHE.get("facade")
    if cached is not None:
        now = time.monotonic()
        last_probe = float(_CACHE.get("checked_at") or 0.0)
        if now - last_probe < PROBE_INTERVAL_SECONDS:
            return cached
        if probe(cached):
            return cached
        # 旧门面失效（Core 重载/卸载）：清缓存、同一次调用里重新发现一次。
        dead = int(_CACHE.get("dead_count") or 0)
        _CACHE.update(
            {
                "facade": None,
                "resolved": False,
                "at": 0.0,
                "reason": "门面已失效（Core 重载或卸载后旧实例不可用），正在重新发现",
                "checked_at": 0.0,
                "dead_count": dead,
            }
        )
        logger.debug(
            "[autonomous_social] Core 门面已失效（is_current/role_ids 探测失败），"
            "清缓存后重新查找"
        )
    now = time.monotonic()
    if (
        not force
        and _CACHE.get("resolved")
        and (now - float(_CACHE.get("at") or 0.0)) < _RETRY_SECONDS
    ):
        return None
    _CACHE["resolved"] = True
    _CACHE["at"] = now

    if context is None:
        _CACHE["reason"] = "没有 context，无法查宿主注册表"
        return None
    get_all_stars = getattr(context, "get_all_stars", None)
    get_registered_star = getattr(context, "get_registered_star", None)
    if not (callable(get_all_stars) or callable(get_registered_star)):
        _CACHE["reason"] = "这个 AstrBot 版本没有 get_all_stars/get_registered_star，无法发现 Core"
        return None

    seen: set = set()

    def inspect(meta: Any) -> Optional[Any]:
        if meta is None or id(meta) in seen:
            return None
        seen.add(id(meta))
        if not _identity_matches(meta):
            return None
        if not bool(getattr(meta, "activated", True)):
            _CACHE["reason"] = "humanoid_core 已安装但未激活"
            return None
        return _facade_from(meta)

    if callable(get_registered_star):
        try:
            for pid in ("astrbot_plugin_humanoid_core", "humanoid_core_plugin"):
                facade = inspect(get_registered_star(pid))
                if facade is not None:
                    return _remember(facade)
        except Exception as exc:
            logger.debug(f"[autonomous_social] get_registered_star 查 Core 失败: {exc}")

    if callable(get_all_stars):
        try:
            for meta in list(get_all_stars() or []):
                facade = inspect(meta)
                if facade is not None:
                    return _remember(facade)
        except Exception as exc:
            logger.debug(f"[autonomous_social] get_all_stars 查 Core 失败: {exc}")

    if not _CACHE.get("reason"):
        _CACHE["reason"] = "宿主注册表里没有 humanoid_core（没装），或它还没加载完"
    return None


def _remember(facade: Any) -> Any:
    _CACHE["facade"] = facade
    _CACHE["reason"] = ""
    # 刚拿到的门面视为存活：这一次的发现就是最可靠的存在性证明。
    _CACHE["checked_at"] = time.monotonic()
    _CACHE["alive"] = True
    try:
        version = facade.api_version()
        caps = ", ".join(facade.capabilities() or [])
        logger.info(
            f"[autonomous_social] 已接上 Humanoid Core 门面 v{version}"
            f"（能力：{caps}）；主动消息的上下文改由 Core 直接给，不再读 state.json"
        )
    except Exception:
        pass
    return facade


# ─── 便捷封装：调用方不写 try/except ───────────────────


def clock(facade: Any, role_id: str) -> Dict[str, Any]:
    if facade is None:
        return {}
    try:
        out = facade.clock(str(role_id))
        return out if isinstance(out, dict) else {}
    except Exception as exc:
        logger.debug(f"[autonomous_social] 门面 clock 失败: {exc}")
        return {}


def intimacy(facade: Any, role_id: str, user_id: str) -> Dict[str, Any]:
    """门面 `intimacy()` 的容错封装：拿不到返回 {}，绝不抛。

    与 `brief()` 同一条纪律：门面是**可选**上下文，坏了只让这一条事实消失，
    不能让主动消息发不出去。
    """
    if facade is None:
        return {}
    fn = getattr(facade, "intimacy", None)
    if not callable(fn):
        return {}
    try:
        data = fn(str(role_id or ""), str(user_id or ""))
    except Exception as exc:
        logger.debug(f"[autonomous_social] 门面 intimacy 失败: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def brief(facade: Any, role_id: str, user_id: str = "", *, task: str = "chat",
          text: str = "", char_name: str = "", user_name: str = "") -> Optional[Dict[str, Any]]:
    """门面 `brief()` 的容错封装：拿不到返回 `None`，调用方退回读 state.json。

    契约只加不改，但 `brief()` 的**签名**长过好几次（`task` → `text` →
    `char_name`/`user_name`）。一次调用失败时逐级退到更小的调用，而不是让一个
    稍旧的 Core 把整条门面路径判死——主动消息的上下文比那几个可选参数值钱。
    """
    if facade is None:
        return None
    fn = getattr(facade, "brief", None)
    if not callable(fn):
        return None
    attempts = (
        {"task": task, "text": text, "char_name": char_name, "user_name": user_name},
        {"task": task, "text": text},
        {"task": task},
        {},
    )
    last_exc: Optional[Exception] = None
    for kwargs in attempts:
        try:
            out = fn(str(role_id), str(user_id or ""), **kwargs)
        except TypeError as exc:
            # 只可能是「这个旧门面不认某个关键字参数」：退一级再试。
            last_exc = exc
            continue
        except Exception as exc:
            logger.debug(f"[autonomous_social] 门面 brief 失败: {exc}")
            return None
        return out if isinstance(out, dict) else None
    if last_exc is not None:
        logger.debug(f"[autonomous_social] 门面 brief 签名不兼容（已逐级降级）: {last_exc}")
    return None


def fingerprint(facade: Any, role_id: str) -> str:
    if facade is None:
        return ""
    try:
        return str(facade.state_fingerprint(str(role_id)) or "")
    except Exception:
        return ""
