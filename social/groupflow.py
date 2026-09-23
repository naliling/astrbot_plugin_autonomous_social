"""群聊心流：主动回复的闸门、冷场破冰的时机、发言参考库的风格提炼。

v1.11.0 新增。这一层是纯逻辑（不碰 IO、不 import astrbot），可单测：
- 心流（flow）：bot 在群里说过话后开一个「关注窗口」，窗口内群消息若值得接，
  就无需被 @ 主动接一句。保守档——只有自己刚发过言才开窗，不主动盯整个群。
- 破冰（icebreak）：群安静太久时主动抛个轻话题。
- 参考库：把近期群友发言提炼成一段「这个群平时怎么说话」，注入生成，让接话不端着。

真正调模型/发消息的接线在 engine.py，写台词在 generator.py。这里只回答
「现在该不该接 / 该不该破冰」和「拿什么当风格参考」。
"""

from __future__ import annotations

import random
import re
from typing import Any, Dict, List, Tuple

# 一条群消息「值不值得让模型看一眼」的便宜预筛：挡在 LLM 调用之前省钱。
# 纯 @、纯表情/图片、太短的语气词、命令回显都先筛掉——它们接不出有意义的话。
_STICKER_ONLY = re.compile(r"^[\s\[\]（）()【】.。~!！?？…、,，]*$")
_AT_PREFIX = re.compile(r"^\s*@\S+\s*$")
# 一眼就是废话/语气词，接了也是尬聊
_LOW_INFO = {
    "哈哈", "哈哈哈", "hhh", "233", "666", "6", "。", "？", "?", "！", "!",
    "在吗", "顶", "up", "沙发", "已阅", "收到", "ok", "好的", "嗯", "嗯嗯",
    "哦", "哦哦", "啊", "？？", "??",
}


def flow_prefilter(text: str, is_command: bool) -> bool:
    """这条群消息值不值得让模型判断要不要接。True=可以进下一步（可能调模型）。

    只做便宜的常识过滤，真正「该不该接、接什么」交给模型。
    """
    if is_command:
        return False
    body = str(text or "").strip()
    if len(body) < 3:
        return False
    if body.startswith("/"):
        return False
    if _STICKER_ONLY.match(body) or _AT_PREFIX.match(body):
        return False
    if body in _LOW_INFO:
        return False
    return True


def flow_should_consider(
    group: Dict[str, Any],
    now: float,
    *,
    max_replies: int,
    min_gap_seconds: float,
    hourly_cap: int,
    hour_count: int,
    ignored_exit: int,
) -> Tuple[bool, str]:
    """心流闸门：现在这个群该不该考虑主动接话。返回 (是否可接, 不可接的原因)。

    比另起话题的闸门更宽（追热聊本来就发生在刚聊完），但有几道防刷屏/防尬聊的硬闸：
    - 关注窗口没开（bot 最近没在这个群说过话）——保守档的核心；
    - 发送隔离中（被踢/会话失效）；
    - 本窗口接够了 / 每小时插话到顶 / 距上一句插话太近；
    - 连续插话没人接，已经到退出阈值。
    """
    if float(group.get("blocked_until", 0) or 0) > now:
        return False, "发送隔离中（被踢/会话失效）"
    if float(group.get("flow_open_until", 0) or 0) <= now:
        return False, "关注窗口没开（bot 最近没在这个群说话）"
    if int(group.get("flow_ignored", 0) or 0) >= max(1, ignored_exit):
        return False, "连着插话没人接，先安静下来"
    if int(group.get("flow_replies", 0) or 0) >= max(1, max_replies):
        return False, "这个窗口已经接够了"
    if hour_count >= max(1, hourly_cap):
        return False, "这一小时插话到上限了"
    gap = now - float(group.get("flow_last_reply_at", 0) or 0)
    if gap < max(0.0, min_gap_seconds):
        return False, "距上一句插话太近"
    return True, ""


def icebreak_due(
    group: Dict[str, Any],
    now: float,
    *,
    idle_hours: float,
    daily_cap: int,
    stale_days: int,
    today: str,
    is_quiet: bool,
    has_history_minimum: int = 5,
) -> bool:
    """这个群现在该不该破冰。

    条件：非安静时段 & 未被隔离 & 此前真的有人聊过（不是刚进的空群/机器人群）&
    没超 stale（超了当已退群）& 安静时长落在 [idle_hours, stale_days] 之间 &
    今天没超破冰上限。
    """
    if is_quiet:
        return False
    if float(group.get("blocked_until", 0) or 0) > now:
        return False
    if int(group.get("msg_count", 0) or 0) < has_history_minimum:
        return False
    last_seen = float(group.get("last_seen", 0) or 0)
    if last_seen <= 0:
        return False
    idle = now - last_seen
    if idle < idle_hours * 3600.0:
        return False
    # 超过 stale_days 没消息：当已经不在这个群，不破冰（交给 prune 清理）
    if idle > max(1, stale_days) * 86400.0:
        return False
    # bot 自己最近刚在群里说过话，就别又破冰（含刚破过冰）
    if now - float(group.get("last_bot_spoke", 0) or 0) < idle_hours * 3600.0:
        return False
    # 今天破冰次数上限
    if str(group.get("icebreak_day", "") or "") == today:
        if int(group.get("icebreak_count_day", 0) or 0) >= max(1, daily_cap):
            return False
    return True


def build_style_reference(texts: List[str]) -> str:
    """把近期群友发言拼成一段「这个群平时这么说话」的风格参考（只留正文，匿名）。

    给生成侧看群里真实的说话口吻/句长/用词，让 bot 融进去，而不是端着一股 AI 腔。
    """
    lines = [str(t or "").strip() for t in texts if str(t or "").strip()]
    if not lines:
        return ""
    body = "\n".join(f"  · {ln}" for ln in lines)
    return (
        "这个群平时就是这么说话的（看语气、长短、用词，别照抄内容）：\n" + body
    )


# ─── 破冰的动机（喂给生成侧，从池子里随机挑一句） ───

_ICEBREAK_REASONS: List[str] = [
    "群里安静半天了，你闲着想找点话头把气氛暖一下。",
    "刷了会儿手机没人说话，你想在群里起个轻松的话头。",
    "群冷了好一阵，你想随口抛个不痛不痒的话题让大家搭句话。",
    "有点无聊，想在群里开个不需要谁必须回的话头。",
    "群里没动静，你想起个大家都能接一句的小话题。",
]


def icebreak_reason() -> str:
    return random.choice(_ICEBREAK_REASONS)


def flow_meta() -> Dict[str, Any]:
    """心流接话的 preset 元数据（供日志/生成侧标识用）。"""
    return {"category": "flow", "intent": "flow", "mode": "flow", "msg_type": "group_flow"}


def icebreak_meta() -> Dict[str, Any]:
    return {"category": "icebreak", "intent": "icebreak", "mode": "icebreak", "msg_type": "group_icebreak"}
