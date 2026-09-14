"""读取 AstrBot 内置人格设定（人设 / Persona）。

主动消息应该由 bot 在该会话里真正在用的那个人格来说。多角色场景下不能在插件里
配一个全局人格：AstrBot 的 unified_msg_origin 格式是
``platform_id:message_type:session_id``（message_session.py），第一段就是平台适
配器实例，而配置画像（含 persona_id）按 ``platform_id::`` 路由到具体角色
（umop_config_router.py）。所以按目标会话的 umo 解析，天然就是按角色隔离的，
一个机器人接多个人设、多个角色各用人设都自动正确。

解析链（与 AstrBot 正常聊天完全一致，对照 persona_mgr.resolve_selected_persona）：
会话级强制指定 → 对话上设定的人格 → 该会话配置画像的默认人格 → 全局默认人格

只使用 Context 上公开的 persona_manager / conversation_manager 属性。
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

try:
    from astrbot.api import logger
except Exception:  # 非 AstrBot 环境兜底
    import logging

    logger = logging.getLogger("autonomous_social")

# 人设原文注入上限：人设可能写得很长，主动消息只需要它的语气与身份，
# 全文塞进去会淹没「发一句很短的话」这个任务本身。
PERSONA_PROMPT_MAX = 800

# AstrBot 里表示「这个会话显式不使用任何人格」的哨兵值
NO_PERSONA_MARKER = "[%None]"


def _truncate(prompt: str) -> str:
    """按行边界截断人设，避免把一句话切成半截。"""
    if len(prompt) <= PERSONA_PROMPT_MAX:
        return prompt
    cut = prompt[:PERSONA_PROMPT_MAX]
    idx = cut.rfind("\n")
    if idx >= PERSONA_PROMPT_MAX // 2:
        cut = cut[:idx]
    return cut.rstrip() + "\n（以上是完整人设的开头部分，按这个感觉说话就行。）"


async def _conversation_persona_id(context: Any, umo: str) -> Optional[str]:
    """取该会话当前对话上设定的人格 id；取不到返回 None。"""
    conv_mgr = getattr(context, "conversation_manager", None)
    if conv_mgr is None or not umo:
        return None
    try:
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            return None
        conv = await conv_mgr.get_conversation(umo, cid)
        return getattr(conv, "persona_id", None) if conv else None
    except Exception as exc:
        logger.debug(f"[autonomous_social] 读取会话人格失败，改用默认人格: {exc}")
        return None


async def _resolve_effective(persona_mgr: Any, context: Any, umo: str) -> Any:
    """解析该会话最终生效的人格对象。"""
    conv_persona_id = await _conversation_persona_id(context, umo)

    resolver = getattr(persona_mgr, "resolve_selected_persona", None)
    if resolver is not None:
        try:
            resolved = await resolver(
                umo=umo,
                conversation_persona_id=conv_persona_id,
                platform_name="",
                provider_settings=None,
            )
            # 返回 (persona_id, persona, force_applied_id, use_webchat_default)
            if isinstance(resolved, tuple) and len(resolved) >= 2:
                if resolved[0] == NO_PERSONA_MARKER:
                    return None
                if resolved[1]:
                    return resolved[1]
        except Exception as exc:
            logger.debug(f"[autonomous_social] 生效人格解析失败，改用默认人格: {exc}")

    default_getter = getattr(persona_mgr, "get_default_persona_v3", None)
    if default_getter is not None:
        try:
            return await default_getter(umo)
        except Exception as exc:
            logger.debug(f"[autonomous_social] 默认人格读取失败: {exc}")

    return getattr(persona_mgr, "selected_default_persona_v3", None)


def _persona_name(persona: Any) -> str:
    try:
        if isinstance(persona, dict):
            return str(persona.get("name") or "").strip()
        return str(getattr(persona, "name", "") or "").strip()
    except Exception:
        return ""


def _persona_prompt(persona: Any) -> str:
    try:
        prompt = persona.get("prompt") if isinstance(persona, dict) else getattr(persona, "prompt", "")
    except Exception:
        return ""
    return _truncate(str(prompt or "").strip())


async def resolve_persona(context: Any, umo: str) -> Tuple[str, str]:
    """返回该会话当前生效人格的 (名字, 设定原文)；拿不到时为 ("", "")。

    名字只用于状态展示与日志，原文用于拼进 prompt。
    """
    persona_mgr = getattr(context, "persona_manager", None)
    if persona_mgr is None:
        return "", ""

    try:
        persona = await _resolve_effective(persona_mgr, context, umo)
    except Exception as exc:
        logger.warning(f"[autonomous_social] 读取 AstrBot 人格设定失败: {exc}")
        return "", ""

    if not persona:
        return "", ""
    # 人设存在但没名字时给中性占位；NO_PERSONA_MARKER 的含义是「不用人格」，不能拿来当名字显示
    return _persona_name(persona) or "（未命名人设）", _persona_prompt(persona)
