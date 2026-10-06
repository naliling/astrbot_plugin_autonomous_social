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
"""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional

MEMORY_PLUGIN_ID = "astrbot_plugin_memory_companion"
# 宿主注册表里可能出现这些身份（name / display_name / 目录名）。
MEMORY_PLUGIN_NAMES = {
    MEMORY_PLUGIN_ID,
    "MemoryCompanion",
    "我会牢牢记住你",
}

SOURCE_PLUGIN = "astrbot_plugin_autonomous_social"


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
        return None
    try:
        bridge = factory()
    except Exception:
        return None
    return bridge if bridge is not None else None


def find_memory_bridge(context: Any) -> Optional[Any]:
    """返回 memory_companion 的桥接对象；没装 / 没就绪返回 None。"""
    if context is None:
        return None
    get_all_stars = getattr(context, "get_all_stars", None)
    get_registered_star = getattr(context, "get_registered_star", None)
    if not (callable(get_all_stars) or callable(get_registered_star)):
        # 老 AstrBot / 单测：没有注册表 API 就当作没装，绝不 importlib 造模块。
        return None

    seen: set = set()

    def inspect(meta: Any) -> Optional[Any]:
        if meta is None or id(meta) in seen:
            return None
        seen.add(id(meta))
        if not _identity_matches(meta):
            return None
        if not bool(getattr(meta, "activated", True)):
            return None
        return _bridge_from(meta)

    if callable(get_all_stars):
        try:
            stars = list(get_all_stars() or [])
        except Exception:
            stars = []
        for meta in stars:
            bridge = inspect(meta)
            if bridge is not None:
                return bridge
    if callable(get_registered_star):
        try:
            meta = get_registered_star(MEMORY_PLUGIN_ID)
        except Exception:
            meta = None
        bridge = inspect(meta)
        if bridge is not None:
            return bridge
    return None


def _session_context(umo: str, uid: str) -> Dict[str, Any]:
    """拼一个 memory_companion 认的会话上下文（它同时接受 dict）。"""
    return {
        "session_id": str(umo or ""),
        "scope": "private",
        "user_id": str(uid or ""),
        "platform": str(umo or "").split(":", 1)[0],
    }


async def fetch_memory_text(
    bridge: Any, umo: str, uid: str, *, query: str = "", max_chars: int = 600,
) -> str:
    """拉这个人的长期记忆（拼好的注入文本）；拿不到返回空串。"""
    if bridge is None:
        return ""
    fn = getattr(bridge, "compose_context", None)
    if not callable(fn):
        return ""
    try:
        text = await fn(
            query=query,
            session_context=_session_context(umo, uid),
            top_k=6,
            max_chars=max_chars,
        )
    except Exception:
        return ""
    return str(text or "").strip()


async def write_proactive(bridge: Any, uid: str, content: str, *, umo: str = "", ts: float = 0.0) -> None:
    """把一条主动消息写回记忆库；失败静默（记忆不是主链路）。"""
    text = str(content or "").strip()
    if bridge is None or not text:
        return
    fn = getattr(bridge, "record_external_memory", None)
    if not callable(fn):
        return
    try:
        await fn(
            user_id=str(uid or ""),
            content=f"（她主动找 TA 说的）{text}",
            source_plugin=SOURCE_PLUGIN,
            idempotency_key=f"proactive:{umo}:{int(ts or 0)}",
            importance=0.5,
            tags=["proactive"],
        )
    except Exception:
        pass
