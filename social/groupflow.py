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

import math
import random
import re
from typing import Any, Dict, List, Optional, Tuple

# emoji 与变体选择符的字符类只有一份，在 `social/emoji.py`；
# `style_profile` / `generator` 引用的是同一个对象。
from .emoji import EMOJI as _EMOJI
# 「外头」前缀的判据同样只有一份（Core 的 weather.env 往往已是整句）。
from .textutil import outside_lead


def _f(x: Any) -> float:
    """容错取时间戳/计数：读不出来、或非有限值，一律当 0.0（等同字段没写）。

    群状态里这些字段（`blocked_until` / `flow_open_until` / `last_seen` …）都是
    外部可改的 JSON，裸 `float()` 有两个方向相反的后果：
      * 坏字符串 → 未捕获的 ValueError，群消息链路与心跳当场断掉；
      * nan → 所有 `> now` / `<= now` 比较恒为 False，于是**发送隔离失效**
        （`blocked_until=nan` 不再拦），而 `flow_open_until=nan` 又让
        **关注窗口恒开、心流永远可接**。
    归零后语义统一成「这个字段没写」：隔离不生效（与缺字段一致），
    关注窗口关闭（`0 <= now` 为真 → 走保守那条分支）。
    """
    try:
        out = float(x or 0)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _i(x: Any) -> int:
    """同上，取整数计数。`int(nan)` 会直接抛 ValueError，`int(inf)` 抛 OverflowError。"""
    try:
        return int(_f(x))
    except (TypeError, ValueError, OverflowError):
        return 0

# 一条群消息「值不值得让模型看一眼」的便宜预筛：挡在 LLM 调用之前省钱。
# 纯 @、纯表情/图片、太短的语气词、命令回显都先筛掉——它们接不出有意义的话。
_STICKER_ONLY = re.compile(r"^[\s\[\]（）()【】.。~!！?？…、,，]*$")
_AT_PREFIX = re.compile(r"^\s*@\S+\s*$")
# 原来「纯表情先筛掉」只是句空话——正则里一个 emoji 范围都没有，于是一条
# 「😀😀😀」长度过得了 3 字符下限、又匹配不上任何标点，照样被送去调模型。
# 字符类现在只有一份，在 `social/emoji.py`（见顶部 import）。
# 平台塞进群消息正文的系统占位符（图片/表情/语音…）。它们不是群友说的话，
# ⚠️ 别把方括号里的一律当占位符：`[图片]` 是占位符，「说[重点]」里的正文不该被吃掉，
# 所以这里列的是常见的固定名词，而不是任意中文方括号串。
_PLACEHOLDER_CONTENT = (
    "图片|表情|动画表情|语音|视频|文件|位置|链接|音乐|红包|转账|分享|合并转发"
)
_MEDIA_PLACEHOLDER = re.compile(rf"[\[【](?:{_PLACEHOLDER_CONTENT})[\]】]")


def _has_no_body(text: str) -> bool:
    """这条消息剥掉标点/空白/emoji/占位符之后，还剩不剩下正文。

    预筛要对齐 docstring 里那句「纯 @、纯表情/图片…先筛掉」，而光靠一条正则同时
    覆盖标点、emoji、`[图片]` 三类会写成谁也读不懂的巨型字符类。分成两步：
    `_STICKER_ONLY` 管纯标点，这里管「装饰剥完就没正文」。
    """
    body = _MEDIA_PLACEHOLDER.sub("", str(text or ""))
    body = _EMOJI.sub("", body)
    return not re.sub(rf"[\s\W_]+", "", body)


# 一眼就是废话/语气词，接了也是尬聊
_LOW_INFO = {
    "哈哈", "哈哈哈", "hhh", "233", "666", "6", "。", "？", "?", "！", "!",
    "在吗", "顶", "up", "沙发", "已阅", "收到", "ok", "好的", "嗯", "嗯嗯",
    "哦", "哦哦", "啊", "？？", "??",
}


def flow_prefilter(text: str, is_command: bool) -> bool:
    """这条群消息值不值得让模型判断要不要接。True=可以进下一步（可能调模型）。

    只做便宜的常识过滤，真正「该不该接、接什么」交给模型。
    除了 `_STICKER_ONLY` 挡掉的纯标点，纯 emoji（`😀😀😀`）与纯占位符（`[图片]`）也在
    这一层筛掉——它们同样接不出有意义的话，却照样会白烧一次模型调用。
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
    if _has_no_body(body):
        return False
    if body in _LOW_INFO:
        return False
    return True


def addressed_to(text: str, known_names: List[str]) -> str:
    """这句话开头是不是在叫某个群友（「小明你昨天…」）。返回那个名字，没有就空串。

    没有 @、但一开口就叫了名字，等价于对着某个人说——心流不该插进去。原来只看
    `@`，于是「小明你那个面试怎么样了」这种明显是一对一的话，她照样会接一句。
    只认**开头**：句中提到（「我觉得小明说得对」）是评论，不是叫人说。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    names = sorted(
        {str(n or "").strip() for n in (known_names or []) if len(str(n or "").strip()) >= 2},
        key=len,
        reverse=True,
    )
    for name in names:
        if body.startswith(name):
            return name
    return ""


def two_person_exchange(speakers: List[str], *, lookback: int = 4) -> bool:
    """最近的真人发言是不是「恰好两个人在来回」。

    两个人对聊时插一句是最招人烦的一种主动：他们的话都是冲着对方说的，第三个人
    接一句既接不上、也打断。原来心流完全不看这个，只要「没有 @」就当可以接。

    判定刻意便宜：取最近 `lookback` 条**真人**发言，去重后恰好 2 个人，且相邻两条
    不同人（A,B,A 或 A,B,A,B）。样本不足 3 条一律 False——宁可让它接，
    也别用一两条消息就断定「他们在对聊」。
    """
    recent = [str(s or "").strip() for s in (speakers or []) if str(s or "").strip()]
    recent = recent[-max(3, int(lookback)) :]
    if len(recent) < 3:
        return False
    if len(set(recent)) != 2:
        return False
    return all(recent[i] != recent[i + 1] for i in range(len(recent) - 1))


def flow_should_consider(
    group: Dict[str, Any],
    now: float,
    *,
    max_replies: int,
    min_gap_seconds: float,
    hourly_cap: int,
    hour_count: int,
    ignored_exit: int,
    is_quiet: bool = False,
    asleep: bool = False,
    recent_speakers: Optional[List[str]] = None,
    addressed_to_name: str = "",
) -> Tuple[bool, str]:
    """心流闸门：现在这个群该不该考虑主动接话。返回 (是否可接, 不可接的原因)。

    比另起话题的闸门更宽（追热聊本来就发生在刚聊完），但有几道防刷屏/防尬聊的硬闸：
    - 安静时段 / 她正在睡觉——私聊侧五道闸都查了这两项，只有群心流漏了，而它又是
      事件驱动的（群消息一到就走，不经过心跳），于是深夜群里有人说话她照样插一句；
    - 关注窗口没开（bot 最近没在这个群说过话）——保守档的核心；
    - 自己刚在主链路说完话后的冷却期（`flow_hold_until`）——不然「被 @ 回一句」后马上又接，
      观感是同一个她在连续刷屏；
    - 发送隔离中（被踢/会话失效）；
    - 本窗口接够了 / 每小时插话到顶 / 距上一句插话太近；
    - 连续插话没人接，已经到退出阈值；
    - **语境**（v1.27.36）：这句话开头在叫某个群友（没 @ 也算），或者群里就是
      两个人在来回对聊——都不是「可以插一句」的场合。
    """
    if asleep:
        return False, "她正在睡觉"
    if is_quiet:
        return False, "现在是安静时段"
    if _f(group.get("blocked_until", 0)) > now:
        return False, "发送隔离中（被踢/会话失效）"
    if _f(group.get("flow_open_until", 0)) <= now:
        return False, "关注窗口没开（bot 最近没在这个群说话）"
    if _f(group.get("flow_hold_until", 0)) > now:
        return False, "她自己刚说完话，先缓一下"
    if _i(group.get("flow_ignored", 0)) >= max(1, ignored_exit):
        return False, "连着插话没人接，先安静下来"
    if _i(group.get("flow_replies", 0)) >= max(1, max_replies):
        return False, "这个窗口已经接够了"
    if hour_count >= max(1, hourly_cap):
        return False, "这一小时插话到上限了"
    gap = now - _f(group.get("flow_last_reply_at", 0))
    if gap < max(0.0, min_gap_seconds):
        return False, "距上一句插话太近"
    # 语境：她要看这个群**现在在干什么**，而不只是「这一条消息过不过关」。
    # 放在配额检查之后：配额是「她还能不能开口」，这两条是「这会儿该不该开口」，
    # 前者更该出现在日志里（调参看的是它）。
    if str(addressed_to_name or "").strip():
        return False, f"这句话是冲着「{str(addressed_to_name).strip()}」说的"
    if two_person_exchange(recent_speakers or []):
        return False, "两个人正在对聊，先不插"
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
    asleep: bool = False,
    has_history_minimum: int = 5,
) -> bool:
    """这个群现在该不该破冰。

    条件：非安静时段 & **她醒着** & 未被隔离 & 此前真的有人聊过（不是刚进的空群/机器人群）&
    没超 stale（超了当已退群）& 安静时长落在 [idle_hours, stale_days] 之间 &
    今天没超破冰上限。
    """
    if asleep:
        return False
    if is_quiet:
        return False
    if _f(group.get("blocked_until", 0)) > now:
        return False
    if _i(group.get("msg_count", 0)) < has_history_minimum:
        return False
    last_seen = _f(group.get("last_seen", 0))
    if last_seen <= 0:
        return False
    idle = now - last_seen
    if idle < idle_hours * 3600.0:
        return False
    # 超过 stale_days 没消息：当已经不在这个群，不破冰（交给 prune 清理）
    if idle > max(1, stale_days) * 86400.0:
        return False
    # bot 自己最近刚在群里说过话，就别又破冰（含刚破过冰）。
    # `topic_at` 是「上一次主动起话头」的时间戳，和 `icebreak_at` 是同一条对称
    # 守卫的两半：`group_topic_due` 会读 `icebreak_at` 避免「刚破完冰又起话头」，
    # 反过来这里也得看「刚起完话头又去破冰」——「刚说过话」是同一件事，取两者较晚者。
    # （原来只读 `last_bot_spoke`，那条会被 `open_flow` 刷新，起话头后未必挡得住破冰。）
    last_proactive = max(
        _f(group.get("icebreak_at", 0)),
        _f(group.get("topic_at", 0)),
    )
    if now - max(last_proactive, _f(group.get("last_bot_spoke", 0))) < idle_hours * 3600.0:
        return False
    # 今天破冰次数上限
    if str(group.get("icebreak_day", "") or "") == today:
        if _i(group.get("icebreak_count_day", 0)) >= max(1, daily_cap):
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


# ─── 破冰的动机 ───
#
# 原来这里有五条现成理由（“群里安静半天了，你闲着想找点话头把气氛暖一下”…）
# 随机挑一句喂给生成侧。那是替她写好一段心理活动，她再顺着写——于是一个人的破冰
# 永远从那五句里来。现在返回空串：破冰的话由模型自己造，插件只提供群里的事实。

def icebreak_reason() -> str:
    """破冰动机句。现在恒为空——动机由模型自己产生。"""
    return ""


def icebreak_topics(holiday: str, weather: str) -> str:
    """K1：破冰/起话头可用的现成素材（节日 / 天气）。只给事实，不给台词。

    破冰动机句早就删了（由模型自己产生），但模型手里没有「今天是什么日子」这类
    客观事实，只能拿平庸话题凑。这里把节日/天气递进去当话头的种子，说什么仍由她定。
    """
    lines: List[str] = []
    holiday = str(holiday or "").strip()
    weather = str(weather or "").strip()
    if holiday:
        lines.append(f"今天是{holiday}")
    if weather:
        lines.append(outside_lead(weather))
    return "\n".join(f"- {x}" for x in lines)


def member_names(samples: List[Dict[str, Any]], limit: int = 6) -> List[str]:
    """G3：近期在群里说过话的人名（去重、最新在前）。给「点名」用。"""
    out: List[str] = []
    for item in reversed(samples or []):
        if not isinstance(item, dict) or item.get("self"):
            continue
        name = str(item.get("name", "") or "").strip()
        if not name or name in out:
            continue
        out.append(name)
        if len(out) >= max(1, limit):
            break
    return out


def group_topic_due(
    group: Dict[str, Any],
    now: float,
    *,
    gap_hours: float,
    daily_cap: int,
    stale_days: int,
    today: str,
    is_quiet: bool,
    asleep: bool = False,
    has_history_minimum: int = 5,
) -> bool:
    """G1：这个群现在该不该由她**主动起个话头**（不必等群彻底冷掉）。

    比破冰宽松（不要求群安静很久），但仍旧保守：得有人聊过、她自己在群里静了够久、
    今天没起够、群没凉透、不在安静时段、**她醒着**。
    """
    if asleep:
        return False
    if is_quiet:
        return False
    if _f(group.get("blocked_until", 0)) > now:
        return False
    if _i(group.get("msg_count", 0)) < has_history_minimum:
        return False
    last_seen = _f(group.get("last_seen", 0))
    if last_seen <= 0:
        return False
    if now - last_seen > max(1, stale_days) * 86400.0:
        return False
    gap = max(1.0, _f(gap_hours)) * 3600.0
    if now - _f(group.get("last_bot_spoke", 0)) < gap:
        return False
    # 刚破过冰就别又起话头——那是同一件事连着做两遍。
    if now - _f(group.get("icebreak_at", 0)) < gap:
        return False
    if str(group.get("topic_day", "") or "") == today:
        if _i(group.get("topic_count_day", 0)) >= max(1, daily_cap):
            return False
    return True
