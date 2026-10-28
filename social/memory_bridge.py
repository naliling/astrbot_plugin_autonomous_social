"""与 `astrbot_plugin_memory_companion`（我会牢牢记住你）的联动。

主动消息走 `context.send_message` 发出去，**不经过 AstrBot 的 respond 阶段**，
所以主链的记忆注入够不到它。这里补上这一环：

- 生成前：拉这个人的长期记忆摘要，揉进提示词，让主动开口「记得住事」；
- 发出后：把这条主动消息写回记忆库，记忆插件那边也知道她主动找过谁。

**拿桥接对象必须查宿主注册表，不能 importlib。** AstrBot 不保证插件模块以某个别名
留在 `sys.modules` 里，而 `import_module` 在没装时会凭空造一个空模块（「没装却说装了」）。
注册表可用却没找到 = 真的没装，到此为止，不再翻 `sys.modules`。写法与 memory_companion
自己的 `core/capability_probe.py` 一致。

没装 memory_companion 时全部返回空/None，主动消息照常发，只是没有记忆可用。

失败不再静默：拿不到桥接 / 接口对不上 / 调用抛异常，按「没装、签名不符、抛异常」三类
各记一条日志（`LogThrottle` 限流，同类一个窗口内只报一次，末尾汇总省略条数），并经
`last_error()` 留一个只读出口——「装了个寂寞」与「压根没装」要能分得开。

幂等键：`ts` 缺失或 `<= 0` 时改用内容摘要，避免退化成 `proactive:{umo}:0` 这个常量
（否则同一用户的每条记忆共用一个键，被对面按幂等去重后只剩第一条）。

## v2.0.0 修复（审核 social v1.28.17：P1-3 / P2-06 / P2-07 / P2-14 / P2-15）

* **P1-3（发布阻断）**：`fetch_memory_text` 默认预算 600 → 1800。对面
  `compose_context(max_chars=)` 的语义是「**整块注入**上限」（里面含约 700~900
  字符的固定指令骨架），不是「正文长度」：传 600 连骨架都放不下，只会返回一句
  「记忆内容因预算不足未展开」的壳，正文一个字都没有。1800 = 对面
  `memory_injection.max_chars` 的默认值。
* **P2-06**：`record_external_memory` 的返回 dict 不再丢弃。`{ok: False, ...}`
  是「对面**正常返回**的拒绝」（配置关、参数非法……），按 `FAIL_REJECTED` 记失败
  （带 `state` / `error_code`），**不清** `last_error`；只有确认成功才
  `_note_success()`。
* **P2-07**：新增 `status()` / `reset()` / `status_text()` / `bridge_alive()`，给引擎
  状态页与 TTL 重试使用。`last_error()` 语义不变（成功清空、失败留分类串）。
* **P2-14**：发现桥后若它提供 `probe_capability_snapshot()`，校验 `methods` 里有没有
  `compose_context` / `record_external_memory`；缺失按 `FAIL_SIGNATURE` 告警一次并
  记录；取不到 probe / 版本时降级为「未校验」，**不影响**读写。
* **P2-15**：读 6s / 写 8s 超时（`asyncio.wait_for`，超时按无记忆降级）；群聊直接
  跳过读取；读结果 60s 缓存（按 umo/uid/query/预算，并绑定桥对象——桥换了缓存自然
  失效）。

## 边界与成本

**记忆联动仅私聊生效**：写进对面的是该用户的 `private_pair` 私有记忆，群聊作用域
检索不到，所以群聊读取直接跳过（省一次必然为空的检索）。**写入会产生 embedding 成本**：
对面开了 `retrieval.embedding_enabled` 时，每条主动消息 ≈ 1 次 embedding 请求
（失败只告警、不影响主链路）。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .throttle import LogThrottle

# 上架硬规则：logger 必须且只能 `from astrbot.api import logger`，严禁内置 logging 模块。
# v1.28.1 被自动安全审核退回，唯一必改项就是这个——social/config.py 与 social/memory_bridge.py
# 各有一处 `except: import logging` 兜底。本插件只在 AstrBot 内运行，该分支永远走不到，
# 删掉它与 Core v2.27.29 删 generator.py/persona.py 同类兜底是同一个理由。
from astrbot.api import logger

MEMORY_PLUGIN_ID = "astrbot_plugin_memory_companion"
# 宿主注册表里可能出现这些身份（name / display_name / 目录名）。
MEMORY_PLUGIN_NAMES = {
    MEMORY_PLUGIN_ID,
    "MemoryCompanion",
    "我会牢牢记住你",
}

SOURCE_PLUGIN = "astrbot_plugin_autonomous_social"

# umo 里表示「群」的消息类型。与 `state._GROUP_UMO_TYPES` / `history_ingest._GROUP_TYPES`
# 必须是同一套写法（`guildmessage` 曾经只出现在其中一个里，于是 QQ 频道的幽灵永远清不掉）。
# 这里**故意复制**而不 import：`history_ingest` 顶部是无保护的 `from astrbot.api import logger`，
# 而本模块的 astrbot 导入是 try/except 的（插件外也能 import，见文件头）。漂移由
# `tests/test_v12733_state_gen.py::ScopeSetDriftTest` 钉住：三处不一致就直接失败。
_GROUP_UMO_TYPES = frozenset({"group", "g", "groupmessage", "guild", "guildmessage"})

# 三类失败原因分开记：区分「对方插件没装」与「装了但坏了」，排查时一眼看得出方向。
FAIL_MISSING = "missing"        # 注册表里没有它（没装 / 未激活 / 宿主没有注册表 API）
FAIL_SIGNATURE = "signature"    # 找到了它，但桥接工厂或所需方法不可调用（接口对不上）
FAIL_EXCEPTION = "exception"    # 注册表、工厂或桥接方法调用时抛了异常
# 对面装了、也激活了，但把桥接开关关了（`private_companion_bridge.enabled`）。
# 这是**用户在配置页上做的一个选择**，不是故障——降级到「没有记忆」是预期行为。
FAIL_DISABLED = "disabled"
# 对面**正常返回**了一次拒绝（`{ok: False, state: "rejected", error_code: ...}`）。
# P2-06：旧实现把它当成功，于是写入被静默丢弃、错误还被清掉。
FAIL_REJECTED = "rejected"
# 调用超时（P2-15）：不是异常，按「这次没有记忆」降级。
FAIL_TIMEOUT = "timeout"
# 读回来的是对面「预算不足」的兜底壳（P1-3 的另一种表现）：没有正文，按没有记忆处理。
FAIL_BUDGET = "budget"

# ─── 读取预算（P1-3，发布阻断）────────────────────────────

#: `compose_context(max_chars=...)` 的语义是**整个注入块的上限**，不是正文长度：
#: 对面先拼一段约 700~900 字符的固定指令骨架，剩下的预算才给记忆正文。传 600 时骨架
#: 都放不下，对面退化成 229 字符的「记忆内容因预算不足未展开」壳（线上 2.3.2 与本地
#: 2.3.5 实测一致）。1800 = 对面 `memory_injection.max_chars` 的默认值，能装下
#: 骨架 + 若干条正文；至少也要 ≥1200（实测 1000 才刚出现正文，留一点余量）。
DEFAULT_MAX_CHARS = 1800
#: 低于这个预算对面基本给不出正文（实测 800 仍是壳）。调用方显式传更小的值属于自找
#: 「没有记忆」，这里只把它当常量语义说明，不强制改调用方的参数。
MIN_USEFUL_MAX_CHARS = 1200
#: 对面预算不足时兜底文案里必然出现的那句话。只在**短文本**里才判成壳（见
#: `_looks_like_budget_shell`），避免正文里恰好提到「预算不足」被误杀。
_BUDGET_SHELL_MARKERS = ("因预算不足未展开", "记忆内容因预算不足")

# ─── 超时与读缓存（P2-15）────────────────────────────────

#: 读超时：检索侧若配了 embedding/rerank，最坏会慢到 provider 超时；主动消息生成
#: 不能被它拖住，超时按「没有记忆」降级（不记异常，记 FAIL_TIMEOUT）。
READ_TIMEOUT_SECONDS = 6.0
#: 写超时：写入在后台任务里，卡住会堆积任务；对面写库 + 排 embedding 的正常耗时远
#: 小于这个值。
WRITE_TIMEOUT_SECONDS = 8.0
#: 读缓存 TTL：同一轮里「真实发送」与「预览」两条路径可能读同一份记忆，缓存掉一次
#: 检索调用。60s 远小于主动消息的心跳间隔，不会让记忆变成旧的。
READ_CACHE_TTL_SECONDS = 60.0
_READ_CACHE_MAX_ENTRIES = 64
#: key = (umo, uid, query, max_chars)；value = (过期 monotonic, 桥对象, 文本)。
#: 存桥对象本体而不是 id()：桥换了（对面重载）缓存自然失效，也避免 id 复用误命中。
_READ_CACHE: Dict[Tuple[str, str, str, int], Tuple[float, Any, str]] = {}

#: 建议的发现重试间隔（秒）。引擎在失败后按这个节奏重查；成功缓存多久由引擎自己的
#: TTL 决定。`status()["retry_in"]` 用它给出「还有多久值得再试」。
RETRY_SECONDS = 60.0

#: P2-14：本插件真正依赖的两个桥方法。能力快照里缺任何一个都说明对面版本不兼容。
REQUIRED_METHODS = ("compose_context", "record_external_memory")

#: 「对面把外部写入关了」是用户在配置页上的选择，不是故障 → 日志降到 debug。
_USER_CHOICE_ERROR_CODES = frozenset({"external_records_disabled", "records_disabled"})

# 同类失败按窗口限流，别每轮心跳刷屏。模块自己持有一个实例，不占用引擎那本共享账。
_THROTTLE = LogThrottle()
# 最近一次失败分类 + 原因；空串表示这轮没出过问题。
_LAST_ERROR = ""
#: 一次性告警集合（能力门控 / 预算壳）：同一类问题进程内只提醒一次，`reset()` 复位。
_ONCE_WARNED: set = set()

# ─── 发现/能力/计数状态（P2-07 状态页用）────────────────────

#: 最近一次 `find_memory_bridge()` 的结果。**只作状态展示**，不在这里缓存桥对象：
#: 桥的生命周期由引擎的 TTL 重试和 `reset()` 管，本模块只负责「发现 + 记账」。
_DISCOVERY: Dict[str, Any] = {
    "state": "unresolved",  # unresolved / connected / missing / disabled / error
    "failure": "",          # FAIL_* 分类
    "reason": "",           # 人话原因（状态页/日志直接显示）
    "at": 0.0,              # time.monotonic()
}
#: P2-14 能力门控结果（最近一次发现时算的）。
_CAPABILITY: Dict[str, Any] = {
    "checked": False,
    "ok": None,
    "version": "",
    "version_checked": False,
    "methods": [],
    "problems": [],
    "at": 0.0,
}


def _new_counters() -> Dict[str, Dict[str, int]]:
    """读/写计数。状态页与排障靠它区分「没调用」「调了被拒」「超时」。"""
    return {
        "read": {
            "ok": 0, "empty": 0, "error": 0, "timeout": 0,
            "budget_shell": 0, "skipped_group": 0, "cache_hit": 0,
        },
        "write": {"ok": 0, "error": 0, "timeout": 0, "rejected": 0},
    }


_COUNTERS: Dict[str, Dict[str, int]] = _new_counters()


def _now() -> float:
    """单调时钟（缓存/窗口用；不受系统时间回拨影响）。"""
    return time.monotonic()


def _bump(kind: str, name: str) -> None:
    bucket = _COUNTERS.get(kind)
    if isinstance(bucket, dict):
        bucket[name] = int(bucket.get(name, 0)) + 1


def last_error() -> str:
    """最近一次记忆桥失败的原因（含类别）；空串表示没有失败记录。"""
    return _LAST_ERROR


# 失败分类 → 状态页的粗粒度 state。
_FAILURE_STATES = {
    FAIL_MISSING: "missing",
    FAIL_DISABLED: "disabled",
    FAIL_SIGNATURE: "error",
    FAIL_EXCEPTION: "error",
    FAIL_REJECTED: "error",
    FAIL_TIMEOUT: "error",
    FAIL_BUDGET: "error",
}


def _split_error(text: str) -> Tuple[str, str]:
    """把「<category>: <detail>」拆回两段；形状不对时整段当 detail。"""
    value = str(text or "")
    if ": " in value:
        reason, detail = value.split(": ", 1)
        return reason.strip(), detail.strip()
    return value.strip(), ""


def _update_discovery_from_error() -> None:
    """发现失败后，把 `_LAST_ERROR` 翻译成状态页能用的 state/reason。"""
    reason, detail = _split_error(_LAST_ERROR)
    _DISCOVERY.update(
        {
            "state": _FAILURE_STATES.get(reason, "error"),
            "failure": reason,
            "reason": detail or _LAST_ERROR or "未解析",
            "at": _now(),
        }
    )


def reset() -> None:
    """清空本模块的所有缓存/状态（测试、配置热重载、用户手动重连时用）。

    只清**桥侧**的记账与读缓存；引擎手里的桥对象缓存由引擎自己的 TTL / `reset()`
    逻辑管（本模块不持有桥对象）。
    """
    global _LAST_ERROR
    _LAST_ERROR = ""
    _ONCE_WARNED.clear()
    _READ_CACHE.clear()
    _THROTTLE.reset()
    _DISCOVERY.update({"state": "unresolved", "failure": "", "reason": "", "at": 0.0})
    _CAPABILITY.update(
        {
            "checked": False,
            "ok": None,
            "version": "",
            "version_checked": False,
            "methods": [],
            "problems": [],
            "at": 0.0,
        }
    )
    _COUNTERS.clear()
    _COUNTERS.update(_new_counters())


def status() -> Dict[str, Any]:
    """给 `/自主社交状态` 与引擎 TTL 重试看的记忆联动快照。

    键固定（引擎按这些名字渲染，改名视为破坏性变更）：

    * `state`: "unresolved" | "connected" | "missing" | "disabled" | "error"
    * `connected` / `resolved`: 状态页用的布尔
    * `failure`: `FAIL_*` 分类（空串 = 没失败）
    * `reason`: 人话原因（中文一行，可直接进状态页）
    * `last_error`: 原始分类串（等价于 `last_error()`）
    * `capability`: P2-14 能力门控 {"checked","ok","version","version_checked",
      "methods","problems"}；`ok is None` 表示「未校验」（旧版 / 取不到）
    * `read` / `write`: 调用计数（ok/empty/error/timeout/skipped_group/cache_hit…）
    * `at`: 最近一次发现的 monotonic 时刻
    * `retry_in`: 发现失败后建议再试的剩余秒数（成功或未解析时为 0）
    """
    now = _now()
    state = str(_DISCOVERY.get("state") or "unresolved")
    failure = str(_DISCOVERY.get("failure") or "")
    reason = "" if state == "connected" else str(_DISCOVERY.get("reason") or "")
    at = float(_DISCOVERY.get("at") or 0.0)
    retry_in = 0.0
    if state in ("missing", "disabled", "error") and at:
        retry_in = max(0.0, RETRY_SECONDS - (now - at))
    return {
        "state": state,
        "connected": state == "connected",
        "resolved": state != "unresolved",
        "failure": failure,
        "reason": reason,
        "last_error": _LAST_ERROR,
        "capability": {
            "checked": bool(_CAPABILITY.get("checked")),
            "ok": _CAPABILITY.get("ok"),
            "version": str(_CAPABILITY.get("version") or ""),
            "version_checked": bool(_CAPABILITY.get("version_checked")),
            "methods": list(_CAPABILITY.get("methods") or []),
            "problems": list(_CAPABILITY.get("problems") or []),
        },
        "read": dict(_COUNTERS["read"]),
        "write": dict(_COUNTERS["write"]),
        "at": at,
        "retry_in": retry_in,
    }


def status_text(*, enabled: bool = True) -> str:
    """状态页【联动】段的「记忆」一行；引擎直接把返回值拼进 sections。

    `enabled` 传 `cfg.memory_bridge_enabled`：关掉时这行只说「已关闭」，
    不去查注册表也不显示历史失败（否则用户以为坏了，其实是他自己关的）。
    """
    if not enabled:
        return "记忆：已关闭（memory_bridge_enabled=false）"
    info = status()
    cap = info.get("capability") or {}
    if info.get("connected"):
        version = str(cap.get("version") or "")
        head = "✅ 记忆：已连接 memory_companion" + (f" v{version}" if version else "")
        if cap.get("checked") and cap.get("ok") is True:
            tail = "能力校验通过"
            if not cap.get("version_checked"):
                tail += "，版本未校验"
        elif cap.get("checked") and cap.get("ok") is False:
            problems = "；".join(str(x) for x in (cap.get("problems") or []))
            tail = "⚠️ 能力校验未通过" + (f"（{problems[:120]}）" if problems else "")
        else:
            tail = "能力/版本未校验"
        return f"{head}；{tail}"
    state = str(info.get("state") or "unresolved")
    reason = str(info.get("reason") or "").strip()
    if state == "missing":
        return f"⚠️ 记忆：未安装（{reason or '宿主注册表里没有 memory_companion'}）"
    if state == "disabled":
        return (
            "⚠️ 记忆：对面关闭了桥接"
            f"（{reason or 'private_companion_bridge.enabled=false'}）"
        )
    if state == "unresolved":
        return "⚠️ 记忆：尚未解析（首轮心跳 / 状态刷新后重试）"
    return f"❌ 记忆：解析失败（{reason or '未知原因'}）"


def _log(level: str, message: str) -> None:
    """按级别打日志；测试会整体替换 `logger`，所以取属性而不是静态调用。"""
    fn = getattr(logger, level, None)
    if not callable(fn):
        fn = logger.warning
    fn(message)


def _note_failure(reason: str, detail: str, *, level: str = "") -> None:
    """记一次失败：留痕（`last_error`）总是更新，日志按同类窗口限流。

    「没装」和「对面关了桥接」都是正常降级，走 debug；
    「签名不符」「调用抛异常」「对面拒绝」「超时」是真故障，走 warning。
    `level` 显式指定时优先（例如「对面关了外部写入」是用户配置，走 debug）。
    """
    global _LAST_ERROR
    _LAST_ERROR = f"{reason}: {detail}"
    if not _THROTTLE.allow(reason):
        return
    message = (
        f"[autonomous_social] memory_companion 桥接不可用（{reason}）：{detail}"
        + _THROTTLE.summary(reason)
    )
    try:
        if level:
            _log(level, message)
        elif reason in (FAIL_MISSING, FAIL_DISABLED):
            logger.debug(message)
        else:
            logger.warning(message)
    except Exception:  # 日志本身失败绝不能反过来影响发送链路
        pass


def _note_failure_once(key: str, reason: str, detail: str, *, level: str = "warning") -> None:
    """只告警一次（进程内），但仍更新 `last_error`。

    给「能力门控缺方法」「预算不足壳」这类**每轮都可能重复、但一次就够看**的问题用：
    引擎每 5 分钟重查一次桥，不能每次都往日志里写同一条。
    """
    global _LAST_ERROR
    _LAST_ERROR = f"{reason}: {detail}"
    if key in _ONCE_WARNED:
        return
    _ONCE_WARNED.add(key)
    try:
        _log(
            level,
            f"[autonomous_social] memory_companion 桥接降级（{reason}）：{detail}",
        )
    except Exception:
        pass


def _note_success() -> None:
    """一次调用真的成功了，就把上一次的失败记录清掉。

    `_LAST_ERROR` 原来只被 `_note_failure` 赋值、**永不重置**，于是 `last_error()` 会永久
    返回历史失败：状态页上看不出「刚才坏了、现在好了」，只看到一条永远不消失的旧报错。
    `signals.py` 的成功路径是清空的（`self._last_error = ""`），两边语义应该一致。
    """
    global _LAST_ERROR
    _LAST_ERROR = ""


def _brief(exc: BaseException, limit: int = 160) -> str:
    """异常一句话（带类型、截断）：够定位问题，又不把整段内容抄进日志。"""
    try:
        text = f"{type(exc).__name__}: {exc}"
    except Exception:  # 连 __str__ 都坏了的异常
        text = type(exc).__name__
    return text if len(text) <= limit else text[:limit] + "…"


def _text_of(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _identity_matches(meta: Any) -> bool:
    try:
        names = {
            _text_of(getattr(meta, attr, ""))
            for attr in ("name", "display_name", "root_dir_name", "module_path")
        }
    except Exception:
        return False
    return bool(names & MEMORY_PLUGIN_NAMES)


def _module_of(meta: Any) -> Any:
    module = getattr(meta, "module", None)
    if module is not None:
        return module
    star_cls = getattr(meta, "star_cls", None)
    mod_name = getattr(star_cls, "__module__", "") if star_cls is not None else ""
    return sys.modules.get(mod_name) if mod_name else None


def _bridge_from(meta: Any) -> Optional[Any]:
    factory = getattr(_module_of(meta), "get_memory_companion_bridge", None)
    if not callable(factory):
        _note_failure(
            FAIL_SIGNATURE,
            "已找到 memory_companion，但没有可调用的 get_memory_companion_bridge 工厂",
        )
        return None
    try:
        bridge = factory()
    except Exception as exc:
        _note_failure(FAIL_EXCEPTION, f"get_memory_companion_bridge() 抛异常：{_brief(exc)}")
        return None
    if bridge is None:
        # 工厂返回 None 只说明 `_ACTIVE_BRIDGE` 是空的，而那个变量由**对面的配置开关**
        # 决定：`private_companion_bridge.enabled` 关掉时它被设成 None。所以这不是
        # 「接口签名不符」，而是「对面没开桥接」——用户自己的选择。按 WARNING 报会在
        # 日志里刷成故障，按 debug 记并把开关名写出来。
        _note_failure(
            FAIL_DISABLED,
            "memory_companion 的桥接开关是关的（配置页「记忆插件协同 → 启用桥接 API」"
            "= private_companion_bridge.enabled）；要用记忆就把那个开关打开",
        )
    return bridge if bridge is not None else None


def _bridge_version(bridge: Any) -> str:
    """尽力取对面插件版本；取不到返回空串（调用方据此降级为「版本未校验」）。

    memory_companion 2.3.x 的桥对象没有版本属性，`probe_capability_snapshot()` 里的
    `contract_revision` 是 C4 契约版本、不是插件版本——不能拿来冒充。所以这里
    只认明确的版本字段，拿不到就诚实地空着，状态页写「版本未校验」。
    """
    for attr in ("plugin_version", "version", "PLUGIN_VERSION", "metadata_version"):
        try:
            value = getattr(bridge, attr, None)
        except Exception:
            continue
        if callable(value) or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return str(value)
        text = _text_of(value)
        if text:
            return text
    return ""


def _close_awaitable(value: Any) -> None:
    """同步路径拿到协程时关掉它，避免 "coroutine was never awaited" 的 RuntimeWarning。"""
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _probe_capability(bridge: Any) -> Dict[str, Any]:
    """P2-14：发现桥后校验它自己声明的能力清单（同步、纯读、不碰存储）。

    只做「告警 + 记录 + 降级」，**绝不**因为门控不过就不让读写：对面老版本没有
    probe（或 probe 自己坏了）时降级为「未校验」，读写照旧、失败由各调用点按
    signature/exception 分类记录。缺失必需方法时告警一次并记进 `_CAPABILITY`。
    """
    version = _bridge_version(bridge)
    cap: Dict[str, Any] = {
        "checked": False,
        "ok": None,
        "version": version,
        "version_checked": bool(version),
        "methods": [],
        "problems": [],
        "at": _now(),
    }
    probe = getattr(bridge, "probe_capability_snapshot", None)
    if not callable(probe):
        cap["problems"] = [
            "桥接对象没有 probe_capability_snapshot（旧版/未文档化接口），"
            "能力与版本降级为「未校验」"
        ]
        return cap
    try:
        snapshot = probe()
    except Exception as exc:
        cap["problems"] = [
            f"probe_capability_snapshot() 抛异常：{_brief(exc)}，降级为「未校验」"
        ]
        return cap
    if inspect.isawaitable(snapshot):
        _close_awaitable(snapshot)
        cap["problems"] = [
            "probe_capability_snapshot() 返回协程（同步发现路径无法 await），"
            "降级为「未校验」"
        ]
        return cap
    if not isinstance(snapshot, dict):
        cap["problems"] = [
            f"能力快照形状不对（{type(snapshot).__name__}），降级为「未校验」"
        ]
        return cap
    methods = snapshot.get("methods")
    if not isinstance(methods, (list, tuple, set)):
        cap["problems"] = ["能力快照没有 methods 列表，降级为「未校验」"]
        return cap
    cap["checked"] = True
    cap["methods"] = [str(item).strip() for item in methods if str(item).strip()]
    missing = [name for name in REQUIRED_METHODS if name not in cap["methods"]]
    if missing:
        cap["ok"] = False
        detail = (
            "probe_capability_snapshot() 的 methods 缺 "
            + "、".join(missing)
            + "（对面版本不兼容，读写调用可能静默失效）"
        )
        cap["problems"] = [detail]
        _note_failure_once("capability:" + ",".join(missing), FAIL_SIGNATURE, detail)
    else:
        cap["ok"] = True
    return cap


def bridge_alive(bridge: Any) -> bool:
    """桥对象是否还能用（对面插件重载/卸载后 `deactivate()` 会把 active 置 False）。

    引擎缓存着桥对象时，TTL 到点先问这个：死对象不该继续用，`reset()` 后重查即可。
    没有存活接口的旧桥（或测试桩）按「可用」处理——那是本函数出现之前的行为。
    """
    if bridge is None:
        return False
    probe = getattr(bridge, "bridge_lifecycle_status", None)
    if callable(probe):
        try:
            data = probe()
        except Exception:
            return False
        if isinstance(data, dict) and "active" in data:
            return bool(data.get("active"))
    active = getattr(bridge, "_active", None)
    if isinstance(active, bool):
        return active
    return True


def _discover_bridge(context: Any) -> Optional[Any]:
    """查一次宿主注册表（纯发现，不带缓存）。`find_memory_bridge` 的发现主体。"""
    if context is None:
        return None
    get_all_stars = getattr(context, "get_all_stars", None)
    get_registered_star = getattr(context, "get_registered_star", None)
    if not (callable(get_all_stars) or callable(get_registered_star)):
        # 老 AstrBot / 单测：没有注册表 API 就当作没装，绝不 importlib 造模块。
        _note_failure(
            FAIL_MISSING, "宿主没有 get_all_stars/get_registered_star，无法确认对面是否安装"
        )
        return None

    seen: set = set()
    saw_identity = False
    lookup_failed = False

    def inspect(meta: Any) -> Optional[Any]:
        nonlocal saw_identity
        if meta is None or id(meta) in seen:
            return None
        seen.add(id(meta))
        if not _identity_matches(meta):
            return None
        saw_identity = True
        if not bool(getattr(meta, "activated", True)):
            _note_failure(FAIL_MISSING, "memory_companion 已安装但未激活")
            return None
        return _bridge_from(meta)

    if callable(get_all_stars):
        try:
            stars = list(get_all_stars() or [])
        except Exception as exc:
            lookup_failed = True
            _note_failure(FAIL_EXCEPTION, f"get_all_stars() 抛异常：{_brief(exc)}")
            stars = []
        for meta in stars:
            bridge = inspect(meta)
            if bridge is not None:
                return bridge
    if callable(get_registered_star):
        try:
            meta = get_registered_star(MEMORY_PLUGIN_ID)
        except Exception as exc:
            lookup_failed = True
            _note_failure(FAIL_EXCEPTION, f"get_registered_star() 抛异常：{_brief(exc)}")
            meta = None
        bridge = inspect(meta)
        if bridge is not None:
            return bridge
    if not saw_identity and not lookup_failed:
        # 注册表可用却查无此人 = 真的没装。查不动（上面已记异常）时不再补一条误导性的「没装」。
        _note_failure(FAIL_MISSING, "宿主注册表里没有 memory_companion（没装）")
    return None


def find_memory_bridge(context: Any) -> Optional[Any]:
    """返回 memory_companion 的桥接对象；没装 / 没就绪返回 None。

    **纯发现、不带缓存**：每次都真查宿主注册表。引擎负责 TTL 重试（成功缓存多久、
    失败多久重查是引擎的事），本函数只保证「这次查得动就查、查不到把原因记清楚」。
    P2-07 之前桥对象在引擎里只解析一次并永久缓存，对面后开开关/重载都接不上。

    发现成功后会做一次 P2-14 能力门控（`probe_capability_snapshot`）：结果记进
    `status()["capability"]`，缺失必需方法时按 FAIL_SIGNATURE 告警一次；但**不**因此
    拒绝返回桥——读写各自的失败路径会把问题记成 signature/exception。
    """
    if context is None:
        _DISCOVERY.update(
            {
                "state": "error",
                "failure": "",
                "reason": "没有 context，无法查宿主注册表",
                "at": _now(),
            }
        )
        return None
    previous_state = str(_DISCOVERY.get("state") or "unresolved")
    bridge = _discover_bridge(context)
    if bridge is not None:
        # 从「没装/关了/坏了」恢复成「连上了」：先把历史失败清掉，状态页才看得出现在
        # 好了；**再**做能力门控——门控自己发现的问题必须留在 last_error 里，不能被
        # 这次「发现成功」顺手清掉（顺序反了就等于 P2-14 的告警静默失效）。
        if previous_state != "connected":
            _note_success()
        _CAPABILITY.update(_probe_capability(bridge))
        _DISCOVERY.update(
            {"state": "connected", "failure": "", "reason": "", "at": _now()}
        )
        return bridge
    _update_discovery_from_error()
    return None


def _umo_parts(umo: str) -> Tuple[str, str]:
    """拆出 (平台, 消息类型)。与 `history_ingest.parse_umo` 同一套规则，但不 import 它。

    AstrBot 的 umo 形如 `aiocqhttp:private:12345` / `aiocqhttp:group:67890`；
    拆不开时平台退回冒号前那段（与旧实现一致），类型给空串。
    """
    parts = str(umo or "").split(":")
    if len(parts) >= 2:
        return parts[0], parts[1].strip().lower()
    if parts and parts[0]:
        return parts[0], ""
    return "", ""


def _session_context(umo: str, uid: str) -> Dict[str, Any]:
    """拼一个 memory_companion 认的会话上下文（它同时接受 dict）。

    `scope` 以前硬编码 "private"：群聊里检索长期记忆时，对面会按私聊作用域去过滤，
    群里聊过的事可能整段检索不到。这里按 umo 的真实类型给；认不出来时仍按私聊，
    与旧行为一致。

    注意：群聊**读取**在 `fetch_memory_text` 里已经直接跳过了（对面写入固定
    `private_pair`，群作用域必然检索不到），本函数保留给「对面将来支持群聊读」
    与测试/e2e 用。
    """
    platform, msg_type = _umo_parts(umo)
    return {
        "session_id": str(umo or ""),
        "scope": "group" if msg_type in _GROUP_UMO_TYPES else "private",
        "user_id": str(uid or ""),
        "platform": platform,
    }


def _looks_like_budget_shell(text: str) -> bool:
    """这段返回是不是对面「预算不足」的兜底壳（没有记忆正文）。

    兜底壳是一条很短的固定文案（实测 229 字符），里面带「记忆内容因预算不足未展开」。
    把它塞进 prompt 既浪费 token，又会让模型看到「记忆没展开」而回避相关话题，所以
    按「没有记忆」处理并记一次降级（P1-3 的另一种表现）。
    """
    value = str(text or "")
    if not value or len(value) > 480:
        return False
    return any(marker in value for marker in _BUDGET_SHELL_MARKERS)


def _read_cache_get(key: Tuple[str, str, str, int], bridge: Any) -> Optional[str]:
    entry = _READ_CACHE.get(key)
    if not entry:
        return None
    expires_at, cached_bridge, text = entry
    if cached_bridge is not bridge or _now() >= expires_at:
        _READ_CACHE.pop(key, None)
        return None
    return text


def _read_cache_put(key: Tuple[str, str, str, int], bridge: Any, text: str) -> None:
    if len(_READ_CACHE) >= _READ_CACHE_MAX_ENTRIES:
        # 读缓存只是省一次检索，丢一批没有任何副作用。
        for stale_key in list(_READ_CACHE)[: max(1, len(_READ_CACHE) // 2)]:
            _READ_CACHE.pop(stale_key, None)
    _READ_CACHE[key] = (_now() + READ_CACHE_TTL_SECONDS, bridge, text)


async def fetch_memory_text(
    bridge: Any, umo: str, uid: str, *, query: str = "", max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """拉这个人的长期记忆（拼好的注入文本）；拿不到返回空串。

    **记忆联动仅私聊生效**：对面写入固定落在该用户的 `private_pair` 私有作用域，
    群聊作用域检索不到，所以群聊直接跳过（省一次必然为空的检索）。确实要给群聊
    读记忆，得先在对面打开 group_recall / 改可见性策略，不是本插件能决定的。

    `max_chars` 是**整块注入预算**而不是正文长度（见 `DEFAULT_MAX_CHARS`）：
    低于 `MIN_USEFUL_MAX_CHARS` 时对面只会回一句「预算不足」的壳。默认 1800。

    读调用有 6 秒超时：记忆检索若配了 embedding/rerank，最坏会慢到 provider 超时，
    主动消息生成不能被它拖住——超时按「没有记忆」降级。成功结果按
    (umo, uid, query, 预算) 缓存 60 秒，同一轮里多条路径读同一份记忆只检索一次。
    """
    if bridge is None:
        return ""
    msg_type = _umo_parts(umo)[1]
    if msg_type in _GROUP_UMO_TYPES:
        _bump("read", "skipped_group")
        return ""
    fn = getattr(bridge, "compose_context", None)
    if not callable(fn):
        _note_failure(FAIL_SIGNATURE, "桥接对象没有可调用的 compose_context（接口签名不符）")
        _bump("read", "error")
        return ""
    try:
        budget = int(max_chars)
    except (TypeError, ValueError):
        budget = DEFAULT_MAX_CHARS
    key = (str(umo or ""), str(uid or ""), str(query or ""), budget)
    cached = _read_cache_get(key, bridge)
    if cached is not None:
        _bump("read", "cache_hit")
        return cached
    try:
        prompt_text = await asyncio.wait_for(
            fn(
                query=query,
                session_context=_session_context(umo, uid),
                top_k=6,
                max_chars=budget,
            ),
            timeout=READ_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        _note_failure(
            FAIL_TIMEOUT,
            f"compose_context() 超过 {READ_TIMEOUT_SECONDS:g}s 未返回，"
            "本次按没有记忆降级（检索侧可能在跑 embedding/rerank）",
        )
        _bump("read", "timeout")
        return ""
    except Exception as exc:
        _note_failure(FAIL_EXCEPTION, f"compose_context() 调用失败：{_brief(exc)}")
        _bump("read", "error")
        return ""
    text = str(prompt_text or "").strip()
    if not text:
        # 对面真的说「没有相关记忆」：这是正常结果，不算失败。
        _note_success()
        _bump("read", "empty")
        return ""
    if _looks_like_budget_shell(text):
        _note_failure_once(
            "budget_shell",
            FAIL_BUDGET,
            "compose_context() 返回的是「预算不足」兜底壳，没有记忆正文"
            f"（max_chars={budget}；DEFAULT_MAX_CHARS={DEFAULT_MAX_CHARS}）。"
            f"请用默认预算，别传到 {MIN_USEFUL_MAX_CHARS} 以下",
        )
        _bump("read", "budget_shell")
        return ""
    _note_success()
    _read_cache_put(key, bridge, text)
    _bump("read", "ok")
    return text


def _safe_stamp(ts: Any) -> float:
    """把调用方的 `ts` 收敛成秒级浮点；缺失 / 非数字 / NaN / 非正数一律当作「没有时间戳」。

    旧实现直接 `int(ts or 0)`：脏值抛 `ValueError` 被吞掉 → 整条记忆静默不写。
    """
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return 0.0
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return 0.0
    return value


def _content_digest(text: str) -> str:
    """内容摘要（跨进程稳定：内置 hash() 按进程加盐，不能用来做幂等键）。"""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _idempotency_key(umo: str, payload: str, ts: Any) -> str:
    """幂等键：有时间戳就用时间戳，没有就用内容摘要。

    `ts` 缺失 / `<= 0` 时若退化成常量（旧实现 `int(0.0)` → `proactive:{umo}:0`），同一
    用户的不同内容会共用一个键，被对面按幂等去重后只剩第一条。用内容摘要则保证
    「不同内容 → 不同键、相同内容 → 相同键」。
    """
    stamp = _safe_stamp(ts)
    if stamp > 0.0:
        return f"proactive:{umo}:{int(stamp)}"
    return f"proactive:{umo}:c:{_content_digest(payload)}"


async def _record_external(
    bridge: Any,
    uid: str,
    payload: str,
    *,
    idempotency_key: str,
    importance: float,
    tags: List[str],
) -> None:
    """调对面的 `record_external_memory`，并按返回契约记账（P2-06 / P2-15）。

    返回 dict 里的 `ok` 是契约的一部分：

    * `ok is True` → 真成功，清 last_error；
    * `ok is not True`（对面带 `state` / `error_code` 正常返回的拒绝）→ 记
      `FAIL_REJECTED`，**不清** last_error，也绝不当成功；
    * 返回结构里**没有** ok（老桥/测试桩返回 `None` / `{"memory_id": ...}`）→
      无法判定，保持旧行为按成功处理（对面 2.3.x 一定返回 `{ok: ...}`，这条只是
      兼容面）。

    **写入会产生 embedding 成本**：对面开 `retrieval.embedding_enabled` 时，每次写入
    会在它那边排一次 embedding 任务（失败只告警）。写调用有 8 秒超时，卡住的任务不会
    永远堆积。
    """
    fn = getattr(bridge, "record_external_memory", None)
    if not callable(fn):
        _note_failure(
            FAIL_SIGNATURE, "桥接对象没有可调用的 record_external_memory（接口签名不符）"
        )
        _bump("write", "error")
        return
    try:
        result = await asyncio.wait_for(
            fn(
                user_id=uid,
                content=payload,
                source_plugin=SOURCE_PLUGIN,
                idempotency_key=idempotency_key,
                importance=importance,
                tags=tags,
            ),
            timeout=WRITE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        _note_failure(
            FAIL_TIMEOUT,
            f"record_external_memory() 超过 {WRITE_TIMEOUT_SECONDS:g}s 未返回"
            "（对面可能在跑 embedding），本次写入放弃",
        )
        _bump("write", "timeout")
        return
    except Exception as exc:
        _note_failure(FAIL_EXCEPTION, f"record_external_memory() 调用失败：{_brief(exc)}")
        _bump("write", "error")
        return
    if isinstance(result, dict) and "ok" in result and result.get("ok") is not True:
        state = _text_of(result.get("state")) or "rejected"
        code = _text_of(result.get("error_code")) or _text_of(result.get("code")) or "unknown"
        detail = f"record_external_memory() 被对面拒绝：state={state} error_code={code}"
        # 「对面把外部写入开关关了」是用户配置，不是故障 → debug；其余拒绝按 warning。
        level = "debug" if code in _USER_CHOICE_ERROR_CODES else "warning"
        _note_failure(FAIL_REJECTED, detail, level=level)
        _bump("write", "rejected")
        return
    _note_success()
    _bump("write", "ok")


async def write_proactive(
    bridge: Any, uid: str, content: str, *, umo: str = "", ts: float = 0.0, about: str = ""
) -> None:
    """把一条主动消息写回记忆库；失败只记日志，不影响主链路。

    `about` 是这次开口的**由头**（`generate_reason` 说的那件事）。带上它，记忆库里
    这条才不是一句孤零零的话——将来按由头检索时，它就是「这件事她开过口」的证据；
    不带的话，三个月后谁都看不出她当时为什么忽然说这句话。

    `uid` 是必填的：记忆插件那边 `user_id` 是**唯一**的身份字段，整条记忆固定写进这个
    用户的私有作用域。空 uid 会写出一条没有主人的记录（谁检索都可能撞上），所以直接跳过。

    **写入会产生 embedding 成本**（对面开 `retrieval.embedding_enabled` 时每条一次），
    且仅私聊可见（写进 `private_pair`，群聊检索不到）。超时 8 秒。
    """
    text = str(content or "").strip()
    uid = str(uid or "").strip()
    if bridge is None or not text or not uid:
        return
    topic = str(about or "").strip()[:60]
    head = f"（她主动找 TA 说的，由头：{topic}）" if topic else "（她主动找 TA 说的）"
    payload = f"{head}{text}"
    await _record_external(
        bridge,
        uid,
        payload,
        idempotency_key=_idempotency_key(umo, payload, ts),
        importance=0.5,
        tags=["proactive"],
    )


async def write_open_closed(
    bridge: Any,
    uid: str,
    about: str,
    *,
    result: str = "",
    kind: str = "loop",
    umo: str = "",
    ts: float = 0.0,
) -> None:
    """一件「还开着的事」有了结果时写回记忆库；失败只记日志。

    这是中间层（`life_log`）交棒给长期层的那一刻：这件事在插件里到此为止——
    不再回访、也不再进注入——但它不该就此消失，所以在这里落一条长期记忆。

    幂等键按**这件事本身**算（内容摘要），不按时刻：同一件事被重放两次只该留一条。
    但键里必须带上 `uid`：这条记忆写进的是 `user_id=uid` 的私有作用域，而同一个群
    （同一个 umo）里两个人完全可以有同名的事（都要「面试结果」），只按 umo + 内容
    算键的话，第二个人那条会被对面按幂等当成重复丢掉。

    `uid` 与 `topic` 一样是必填的：空 uid 会写出一条没有主人的记录。

    **写入会产生 embedding 成本**（对面开 `retrieval.embedding_enabled` 时每条一次），
    且仅私聊可见（写进 `private_pair`，群聊检索不到）。超时 8 秒。
    """
    topic = str(about or "").strip()[:80]
    uid = str(uid or "").strip()
    if bridge is None or not topic or not uid:
        return
    outcome = str(result or "").strip()[:80]
    payload = f"（这件之前一直没完的事有结果了）{topic}"
    if outcome:
        payload = f"{payload}：{outcome}"
    key = f"closed:{umo}:{uid}:{kind}:{_content_digest(topic)}"
    await _record_external(
        bridge,
        uid,
        payload,
        idempotency_key=key,
        # 比一条主动消息重：`promise` / `open_loop` 正是记忆插件给最高权重的那两类。
        importance=0.62,
        tags=["open_loop" if kind != "promise" else "promise"],
    )
