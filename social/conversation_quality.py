"""Quality gates for proactive messages that must continue recent peer context."""
from __future__ import annotations

import math
import re
from typing import Any, Iterable, List

WEATHER_CONTEXT_MAX_AGE = 6 * 60 * 60.0
_MAX_CONTEXT_ITEMS = 50
_WEATHER_RE = re.compile(
    r"天气|气温|温度|下雨|下雪|下冰雹|雨夹雪|雷阵雨|雷雨|暴雨|小雨|中雨|大雨|降雨|降温|升温|降雪|大风|刮风|寒潮|热浪|湿度|雾霾|晴天|阴天|多云|外面冷|外面热"
)
_GENERIC_STATUS_RE = re.compile(
    r"(?:^|[，,。！？!?\s])(?:你)?(?:在吗|在不在|在忙吗|在干嘛|在干什么|干嘛呢|干什么呢|做什么呢|忙吗|最近怎么样|最近还好吗|最近忙什么|最近在做什么|今天怎么样|今天过得怎么样)(?:呢|呀|啊|吗|嘛|哦|哈)?[？?。！!]*\s*$"
)


def _recent_peer_texts(
    entries: Any, now: float, *, group: bool = False
) -> List[str]:
    try:
        current = float(now)
    except (TypeError, ValueError, OverflowError):
        return []
    if not math.isfinite(current) or not isinstance(entries, (list, tuple)):
        return []

    out: List[str] = []
    for item in list(entries)[-_MAX_CONTEXT_ITEMS:]:
        if not isinstance(item, dict):
            continue
        if group:
            if item.get("self") is not False:
                continue
        elif str(item.get("dir") or "").lower() != "in":
            continue
        try:
            timestamp = float(item.get("ts"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(timestamp) or timestamp <= 0 or timestamp > current:
            continue
        if current - timestamp > WEATHER_CONTEXT_MAX_AGE:
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            continue
        out.append(text.strip())
    return out


def has_recent_weather_mention(entries: Any, now: float, *, group: bool = False) -> bool:
    return any(_WEATHER_RE.search(text) for text in _recent_peer_texts(entries, now, group=group))


def proactive_content_issue(
    text: Any, entries: Any, now: float, *, group: bool = False
) -> str:
    body = str(text or "").strip()
    if _GENERIC_STATUS_RE.search(body):
        return "主动消息是泛化近况询问"
    if _WEATHER_RE.search(body) and not has_recent_weather_mention(entries, now, group=group):
        return "主动消息提到天气但近期对方没有开启这个话题"
    return ""
