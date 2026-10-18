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
"""

from __future__ import annotations

import hashlib
import sys
from typing import Any, Dict, Optional, Tuple

from .throttle import LogThrottle

try:  # 插件外（脚本/测试）导入时退化为标准库，别让日志变成硬依赖
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("autonomous_social")

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

# 同类失败按窗口限流，别每轮心跳刷屏。模块自己持有一个实例，不占用引擎那本共享账。
_THROTTLE = LogThrottle()
# 最近一次失败分类 + 原因；空串表示这轮没出过问题。
_LAST_ERROR = ""


def last_error() -> str:
    """最近一次记忆桥失败的原因（含类别）；空串表示没有失败记录。"""
    return _LAST_ERROR


def _note_failure(reason: str, detail: str) -> None:
    """记一次失败：留痕（`last_error`）总是更新，日志按同类窗口限流。

    「没装」和「对面关了桥接」都是正常降级，走 debug；
    「签名不符」「调用抛异常」是真故障，走 warning。
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
        if reason in (FAIL_MISSING, FAIL_DISABLED):
            logger.debug(message)
        else:
            logger.warning(message)
    except Exception:  # 日志本身失败绝不能反过来影响发送链路
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



def find_memory_bridge(context: Any) -> Optional[Any]:
    """返回 memory_companion 的桥接对象；没装 / 没就绪返回 None。"""
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


def _umo_parts(umo: str) -> Tuple[str, str]:
    """拆出 (平台, 消息类型)。与 `history_ingest.parse_umo` 同一套规则，但不 import 它。

    AstrBot 的 umo 形如 ``aiocqhttp:private:12345`` / ``aiocqhttp:group:67890``；
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
    """
    platform, msg_type = _umo_parts(umo)
    return {
        "session_id": str(umo or ""),
        "scope": "group" if msg_type in _GROUP_UMO_TYPES else "private",
        "user_id": str(uid or ""),
        "platform": platform,
    }


async def fetch_memory_text(
    bridge: Any, umo: str, uid: str, *, query: str = "", max_chars: int = 600,
) -> str:
    """拉这个人的长期记忆（拼好的注入文本）；拿不到返回空串。"""
    if bridge is None:
        return ""
    fn = getattr(bridge, "compose_context", None)
    if not callable(fn):
        _note_failure(FAIL_SIGNATURE, "桥接对象没有可调用的 compose_context（接口签名不符）")
        return ""
    try:
        text = await fn(
            query=query,
            session_context=_session_context(umo, uid),
            top_k=6,
            max_chars=max_chars,
        )
    except Exception as exc:
        _note_failure(FAIL_EXCEPTION, f"compose_context() 调用失败：{_brief(exc)}")
        return ""
    _note_success()
    return str(text or "").strip()


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


async def write_proactive(
    bridge: Any, uid: str, content: str, *, umo: str = "", ts: float = 0.0, about: str = ""
) -> None:
    """把一条主动消息写回记忆库；失败只记日志，不影响主链路。

    `about` 是这次开口的**由头**（`generate_reason` 说的那件事）。带上它，记忆库里
    这条才不是一句孤零零的话——将来按由头检索时，它就是「这件事她开过口」的证据；
    不带的话，三个月后谁都看不出她当时为什么忽然说这句话。

    `uid` 是必填的：记忆插件那边 `user_id` 是**唯一**的身份字段，整条记忆固定写进这个
    用户的私有作用域。空 uid 会写出一条没有主人的记录（谁检索都可能撞上），所以直接跳过。
    """
    text = str(content or "").strip()
    uid = str(uid or "").strip()
    if bridge is None or not text or not uid:
        return
    fn = getattr(bridge, "record_external_memory", None)
    if not callable(fn):
        _note_failure(
            FAIL_SIGNATURE, "桥接对象没有可调用的 record_external_memory（接口签名不符）"
        )
        return
    topic = str(about or "").strip()[:60]
    head = f"（她主动找 TA 说的，由头：{topic}）" if topic else "（她主动找 TA 说的）"
    payload = f"{head}{text}"
    try:
        await fn(
            user_id=uid,
            content=payload,
            source_plugin=SOURCE_PLUGIN,
            idempotency_key=_idempotency_key(umo, payload, ts),
            importance=0.5,
            tags=["proactive"],
        )
    except Exception as exc:
        _note_failure(FAIL_EXCEPTION, f"record_external_memory() 调用失败：{_brief(exc)}")
        return
    _note_success()


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
    """
    topic = str(about or "").strip()[:80]
    uid = str(uid or "").strip()
    if bridge is None or not topic or not uid:
        return
    fn = getattr(bridge, "record_external_memory", None)
    if not callable(fn):
        _note_failure(
            FAIL_SIGNATURE, "桥接对象没有可调用的 record_external_memory（接口签名不符）"
        )
        return
    outcome = str(result or "").strip()[:80]
    payload = f"（这件之前一直没完的事有结果了）{topic}"
    if outcome:
        payload = f"{payload}：{outcome}"
    key = f"closed:{umo}:{uid}:{kind}:{_content_digest(topic)}"
    try:
        await fn(
            user_id=uid,
            content=payload,
            source_plugin=SOURCE_PLUGIN,
            idempotency_key=key,
            # 比一条主动消息重：`promise` / `open_loop` 正是记忆插件给最高权重的那两类。
            importance=0.62,
            tags=["open_loop" if kind != "promise" else "promise"],
        )
    except Exception as exc:
        _note_failure(FAIL_EXCEPTION, f"record_external_memory() 调用失败：{_brief(exc)}")
        return
    _note_success()

