"""上下文推理：由头先行、消息类型选择、多样化理由、反重复机制。

v1.7.4：
- 新增时间锚点提取（extract_cue）：对方说了「明天面试」，过一天才有得问
- 新增 live_cue()：由头到点才算「想起来了」，没由头就别没话找话
- 新增 sleep_signal()：对方说过晚安/要睡了，就别再去催睡或道晚安
- 理由优先级重排：由头 > 长时间未联系 > 时段
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# ─── 时段定义 ───────────────────────────────────────

_TIME_SLOTS: List[Tuple[int, int, str]] = [
    (6, 9, "early_morning"),
    (9, 12, "morning"),
    (12, 14, "lunch"),
    (14, 18, "afternoon"),
    (18, 22, "evening"),
    (22, 24, "late_night"),
    (0, 6, "deep_night"),
]

_SLOT_NAMES_CN: Dict[str, str] = {
    "early_morning": "清晨",
    "morning": "上午",
    "lunch": "午休",
    "afternoon": "下午",
    "evening": "傍晚",
    "late_night": "深夜",
    "deep_night": "凌晨",
}

# ─── 时段理由 ───────────────────────────────────────

_TIME_REASONS: Dict[str, List[str]] = {
    "early_morning": [
        "刚醒，脑子还糊着，顺手摸到了手机。",
        "醒得早，窗外还灰蒙蒙的，没什么事干。",
        "被闹钟拽起来之前那段最迷糊，想说句话。",
        "早上头一件事就是刷眼手机。",
        "醒了就睡不着了，躺着发了会儿呆。",
        "今天居然没赖床，有点不习惯。",
    ],
    "morning": [
        "上午有点走神，手头的事提不起劲。",
        "刚忙完一小段，喘口气的功夫。",
        "上午阳光挺好，看着窗外发了会儿呆。",
        "开了个没什么用的会，出来透透气。",
        "咖啡喝到第二杯了，还是有点困。",
        "今天状态还行，就是闲不住想唠两句。",
    ],
    "lunch": [
        "中午吃完没什么精神，犯困。",
        "午休刷手机刷到一半，看到个东西。",
        "今天吃的不太合胃口，心情也一般。",
        "困得不行，但又不想睡，随便打几个字。",
        "中午难得闲下来，脑子开始乱飘。",
        "吃完饭散了会儿步，回来有点想说话。",
    ],
    "afternoon": [
        "事情做到一半卡住了，摸会儿鱼缓缓。",
        "下午快结束了，松了口气。",
        "下午的太阳晒得人发懒。",
        "刚被一堆事轰炸完，终于安静了。",
        "坐着坐着突然有点无聊。",
        "今天过得意外的快。",
    ],
    "evening": [
        "忙完一天窝着，心里挺松的。",
        "傍晚的天色挺好看，看了半天。",
        "吃完饭瘫着，今天过得很快。",
        "晚风很舒服，在楼下晃了一圈回来。",
        "心情说不上好也说不上坏，就想说说话。",
        "剧看到一半，突然想换个脑子。",
    ],
    "late_night": [
        "还没睡，安静的时候脑子最活跃。",
        "躺床上刷来刷去，没什么想看的。",
        "这个点有点感性，话到嘴边就想找个地方说。",
        "夜宵刚吃完，有点罪恶感。",
        "戴着耳机随机播放，听到一首老歌。",
        "明明困了但就是不想先睡。",
    ],
    "deep_night": [
        "凌晨了，迷迷糊糊还没睡。",
        "醒了之后睡不着了，轻手轻脚打了几个字。",
        "这个点醒着的人不多，有种奇怪的安全感。",
        "半夜饿了，翻完冰箱坐在这发呆。",
        "睡不着，索性不睡了。",
    ],
}

# ─── 消息类型定义 ────────────────────────────────────
#
# 每种类型包含：基础权重、描述（用于 prompt）、示例（引导模型）。
# 示例为 (文本, 最低关系档位) 元组：低熟络关系下不展示亲昵/越界的示例，
# 避免「保持分寸」的抽象指令被亲密示例带偏。档位：0刚认识 1一般熟 2朋友 3亲近 4非常亲近。
#
# 核心设计：大多数类型不是问句。只有约 10% 的消息可能是关心类型带问题。
# 示例不得与 generator._BAD_PATTERNS 撞车（已互斥清洗）。

MESSAGE_TYPES: Dict[str, Dict[str, Any]] = {
    "share_thought": {
        "weight": 30,
        "desc": "分享一个突然冒出来的想法或感受",
        "examples": [
            ("刚才看到一个东西挺有意思", 0),
            ("说实话我现在有点不想动", 0),
            ("今天也不知道怎么的，心情还行", 0),
            ("脑子里突然冒出个念头", 0),
            ("想起之前聊的那个，其实我后来又想了一下", 1),
        ],
    },
    "express_feeling": {
        "weight": 20,
        "desc": "自然地表达一点情绪或感受",
        "examples": [
            ("今天好累啊", 0),
            ("有点无聊", 0),
            ("刚才心情不太好，现在好点了", 1),
            ("突然觉得还挺想跟你说话的", 2),
            ("突然有点想你了", 3),
        ],
    },
    "continue_topic": {
        "weight": 20,
        "desc": "自然地延续之前聊过的话题",
        "examples": [
            ("对了之前那个后来怎么样了", 0),
            ("那个东西我后来查了一下", 0),
            ("你上次提的那个事我去看了下", 1),
        ],
    },
    "casual_hello": {
        "weight": 15,
        "desc": "不打招呼的招呼，像随口说的一句",
        "examples": [
            ("嘿", 0),
            ("冒个泡", 0),
            ("也没什么事 就是想说句话", 0),
            ("突然想跟你说句话", 1),
        ],
    },
    "check_in": {
        "weight": 10,
        "desc": "自然的关心，但不要太像问候语",
        "examples": [
            ("最近降温了 你那边冷不冷", 0),
            ("忙完这阵了没", 1),
            ("你上次说的那个事 后来顺不顺利", 2),
        ],
    },
    "react_time": {
        "weight": 5,
        "desc": "基于当前时间或状态的反应",
        "examples": [
            ("这个点了还没睡", 0),
            ("刚忙完 终于可以歇会了", 0),
            ("天都快亮了", 0),
            ("午休终于可以摸会鱼了", 0),
        ],
    },
}

# ─── 关系等级定义 ────────────────────────────────────

_RELATIONSHIP_TIERS: List[Tuple[int, str, str]] = [
    (20, "acquaintance", "刚认识不久，还不够了解，保持自然的礼貌和分寸。"),
    (40, "casual", "算是一般熟悉，可以轻松随意地聊天。"),
    (60, "friendly", "算是朋友了，可以更自然地表达关心和好奇。"),
    (80, "close", "关系比较亲近，可以更直接、更温暖，偶尔带点撒娇或依赖。"),
    (101, "intimate", "关系非常亲近，可以完全放松地表达想念和在意，语气可以亲密自然。"),
]

# ─── 能量等级阈值 ────────────────────────────────────

ENERGY_THRESHOLDS: List[Tuple[int, str]] = [
    (15, "exhausted"),
    (30, "tired"),
    (60, "normal"),
    (80, "good"),
]

SOCIAL_ENERGY_THRESHOLDS: List[Tuple[int, str]] = [
    (15, "drained"),
    (30, "low"),
    (60, "normal"),
    (80, "good"),
]

# ─── 对话连续性检测阈值（秒） ────────────────────────

TOPIC_CONTINUE_THRESHOLD = 14400    # 4小时内的话题延续
RECENT_CHAT_THRESHOLD = 3600        # 1小时内的刚聊完

# ─── 时间锚点（由头） ──────────────────────────────
#
# 真人主动开口，大多因为「想起一件具体的事」。对方话里带时间锚点时，这件事会在
# 锚点过后变成一句自然的跟进；没这件事的时候宁可不发。
# 表内顺序即优先级：先匹配到哪个算哪个（「大后天」不能被「后天」抢走）。
_CUE_REL_SECONDS = 2 * 3600        # 「待会/晚点」推多久

# 表内顺序即优先级：先匹配到哪个算哪个（「大后天」不能被「后天」抢走）
# 形式：(正则, 天数偏移, 时, 分)；天数/时/分全为 0 表示相对锚点（走 _CUE_REL_SECONDS）
_CUE_ANCHORS = (
    (r"大后天", 3, 10, 0),
    (r"后天", 2, 10, 0),
    (r"明天|明儿", 1, 10, 0),
    (r"今晚|今晚上|今天晚上", 0, 20, 30),
    (r"下周|下个星期|下星期", 7, 10, 0),
    (r"周末|星期天|星期日", -1, 12, 0),      # -1 表示下一个周六
    (r"待会|等会|等一下|一会儿|晚点|回头", 0, 0, 0),  # 相对锚点
)

# 「周X/星期X」单独算：要推到下一个那个星期几
_CUE_WEEKDAY = re.compile(r"(?:下周|星期|周)([一二三四五六日天])")
_WEEKDAY_CN = "一二三四五六日"

# 同一句里出现这些说法时，锚点只是客套，不值得跟进
_CUE_NEGATIONS = (
    "明天见", "明天再说", "明天聊", "明天再聊", "下次再", "回头再说",
    "先不", "算了", "晚安", "睡了", "困了",
)

# 锚点短语保留长度（作为 prompt 里的「那件事」的线索）
CUE_TEXT_MAX = 40
# 由头最多记多久：超过这个时间的约定早就黄了，别拿它当理由
CUE_TTL_SECONDS = 14 * 86400
# 过了到期时间多久之内还算「刚过点」（再晚就不适合问了）
CUE_LATE_LIMIT = 36 * 3600


def _at_days_ahead(now: float, days: int, hour: int, minute: int, offset: int = 0) -> float:
    """now 往后第 days 天的 hour:minute；若那个时刻已过去则再挨后一天。

    hour:minute 是「她那里」的时刻，所以先按偏移量折成她城市的挂钟时间，再换回 epoch。
    """
    base = datetime.fromtimestamp(now + offset * 60.0)
    target = (base + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    stamp = target.timestamp() - offset * 60.0
    if stamp <= now:
        stamp += 86400.0
    return stamp


def _next_weekday(now: float, weekday: int, hour: int = 10, minute: int = 0, offset: int = 0) -> float:
    """下一个星期 weekday（0=周一）的 hour:minute。今天就是这个星期几且已过点则算下周。"""
    base = datetime.fromtimestamp(now + offset * 60.0)
    delta = (weekday - base.weekday()) % 7
    target = (base + timedelta(days=delta)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    stamp = target.timestamp() - offset * 60.0
    if stamp <= now:
        stamp += 7 * 86400.0
    return stamp


def _anchor_due(now: float, pattern: str, days: int, hour: int, minute: int, offset: int = 0) -> float:
    """把锚点算成一个具体时间点（她所在城市的时间）。"""
    if days == -1:
        return _next_weekday(now, 5, 12, 0, offset)
    if days == 0 and hour == 0 and minute == 0:
        return now + _CUE_REL_SECONDS
    return _at_days_ahead(now, days, hour, minute, offset)


def extract_cue(
    text: str,
    now: Optional[float] = None,
    clock_offset_minutes: int = 0,
) -> Tuple[Optional[str], float]:
    """从对方消息里提取可跟进的时间锚点。

    Args:
        text: 对方刚发的消息
        now: 当前时间戳，默认取现在
        clock_offset_minutes: 她所在城市相对本机的分钟偏移（「明天十点面试」按她的钟点算）

    Returns:
        (锚点短语, 到期时间戳)；没有锚点返回 (None, 0.0)
    """
    msg = str(text or "").strip()
    if len(msg) < 2 or len(msg) > 300:
        return None, 0.0
    now = time.time() if now is None else float(now)

    candidates: List[Tuple[int, str, float]] = []

    for pattern, days, hour, minute in _CUE_ANCHORS:
        match = re.search(pattern, msg)
        if not match:
            continue
        candidates.append((match.start(), pattern,
                           _anchor_due(now, pattern, days, hour, minute, clock_offset_minutes)))

    wd = _CUE_WEEKDAY.search(msg)
    if wd:
        ch = wd.group(1)
        idx = _WEEKDAY_CN.find(ch)
        weekday = 6 if idx < 0 else idx
        candidates.append((wd.start(), "weekday",
                           _next_weekday(now, weekday, offset=clock_offset_minutes)))

    if not candidates:
        return None, 0.0

    # 句子里最早出现的那个锚点才是正事（「明天面试，今晚先不聊了」跟的是面试）
    start, pattern, due = min(candidates, key=lambda x: x[0])
    snippet = msg[max(0, start - 12): min(len(msg), start + CUE_TEXT_MAX)]
    if any(neg in snippet for neg in _CUE_NEGATIONS):
        return None, 0.0
    if due <= now or due - now > CUE_TTL_SECONDS:
        return None, 0.0
    return snippet.strip(), due


def live_cue(user: Dict[str, Any], now: float) -> Optional[str]:
    """有没有已经到点、还没用过的由头。"""
    cue = str(user.get("cue", "") or "")
    try:
        due = float(user.get("cue_due", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not cue or due <= 0:
        return None
    if now < due:
        return None
    if now - due > CUE_LATE_LIMIT:
        return None
    return cue


# ─── 睡意信号 ────────────────────────────────

_SLEEP_WORDS = (
    "晚安", "要睡了", "去睡", "先睡", "睡了", "困死", "困了", "眯一会",
    "眯会儿", "熬不住", "准备睡", "睡觉了", "躺了",
)
SLEEP_SUPPRESS_SECONDS = 8 * 3600


def sleep_signal(user: Dict[str, Any], now: float) -> bool:
    """对方最近说过要睡了：这时候再去发消息（尤其别再说「该睡了」）很不对。"""
    conv = user.get("conversation") or []
    try:
        recent = [m for m in conv if str(m.get("text", ""))][-4:]
    except TypeError:
        return False
    for m in reversed(recent):
        if str(m.get("text", "")) in _SLEEP_WORDS:
            continue
        if now - float(m.get("ts", 0) or 0) > SLEEP_SUPPRESS_SECONDS:
            return False
        if any(w in str(m.get("text", "")) for w in _SLEEP_WORDS):
            return True
    msg = str(user.get("last_message", "") or "")
    if msg and any(w in msg for w in _SLEEP_WORDS):
        return now - float(user.get("last_seen", 0) or 0) <= SLEEP_SUPPRESS_SECONDS
    return False

# ─── 悬空对话 ────────────────────────────────

# 话说到一半对方没声了，跟「想不想找 TA 说话」是两个问题。后者得攒十几小时念头（那
# 才像有自己的生活），但前者不需要：真人刚聊过天、对方突然消失，十几分钟就会问一句。
# 所以它走一单独的路径，不进 urge 模型。

# 至少来回过两句才算「聊到一半」，否则只是对方偶尔发了一句就走了
DANGLING_CONTEXT_MIN = 2

# 对方明确说了要离开：这时候再问「在吗」是没听见人说话
_LEAVING_WORDS = (
    "去忙", "先忙", "忙着", "在忙", "有事", "先走了", "出门", "开车",
    "洗澡", "吃饭去", "去吃饭", "做饭", "上班", "上课", "开会", "下班",
    "健身", "赶个", "交材料", "写作业", "辅导", "接孩子", "取快递",
    "不聊了", "先不聊", "回头聊", "待会说", "回头再说", "下次再聊",
    "改天说", "先这样", "先这样吧",
)

_DANGLING_REASONS: List[str] = [
    "刚才还在聊，话说到一半 TA 那边没声了，不知道是不是被什么事打断。",
    "正说着呢突然没人了，有点挂心，问一句还在不在。",
    "TA 上一条话还没说完就没动静了，想顺嘴接一句。",
    "聊得好好的突然安静下来，担心是自个儿哪句话把天聊死了。",
    "刚才那段话停在 TA 那里，手机拿起来又放下了，还是问一句。",
]


def is_leaving(text: str) -> bool:
    """对方最后那句是不是在说「我要离开了」。"""
    body = str(text or "")
    if not body:
        return False
    return any(word in body for word in _LEAVING_WORDS)


def last_direction(user: Dict[str, Any]) -> str:
    """对话历史里最后一句是谁说的：'in' / 'out' / ''。"""
    for item in reversed(user.get("conversation") or []):
        direction = str(item.get("dir") or "")
        if direction in ("in", "out"):
            return direction
    return ""


def dangling_reason(
    user: Dict[str, Any],
    now: float,
    *,
    after_seconds: float,
    max_seconds: float,
    context_seconds: float,
) -> str:
    """话说到一半断了：返回一句开口的动机，空串表示现在不该跟。

    只看「刚才在聊、现在突然没声」这一件事。对方没接她的话不算（那是追着人说），
    对方说了要去做事也不算。
    """
    last_seen = float(user.get("last_seen", 0) or 0)
    if last_seen <= 0:
        return ""
    silence = now - last_seen
    if silence < after_seconds or silence > max_seconds:
        return ""
    if last_direction(user) != "in":
        # 最后一句是她自己说的：再开口就是追着人问「你怎么不回」
        return ""
    if str(user.get("pending_result", "") or "") == "waiting":
        return ""
    if is_leaving(user.get("last_message")) or sleep_signal(user, now):
        return ""
    # 得先「在聊」：最后一条之前还有话挨着，才叫聊到一半，而不是偶尔一句就走了
    nearby = [
        item for item in (user.get("conversation") or [])
        if float(item.get("ts", 0) or 0) < last_seen
        and last_seen - float(item.get("ts", 0) or 0) <= context_seconds
    ]
    if len(nearby) + 1 < DANGLING_CONTEXT_MIN:
        return ""
    return random.choice(_DANGLING_REASONS)


def dangling_meta() -> Dict[str, Any]:
    """跟进断掉的话时的消息类型。示例都是问句，与「大多数时候别说问句」相反，
    所以调用方靠 intent=followup 告诉生成器这一条例外。"""
    return {
        "category": "dangling",
        "intent": "followup",
        "msg_type": "check_in",
        "msg_type_desc": "把说到一半断掉的话接上，问一句还在不在、是不是被什么事打断了",
        "msg_examples": [
            ("还在吗", 0),
            ("人呢", 0),
            ("刚才是不是卡了", 0),
            ("被啥事叫走了？", 0),
            ("你先忙，忙完说一声", 1),
        ],
    }


# ─── 消息类型权重调整 ────────────────────────────────

CONTINUITY_BOOST = 40           # 有可延续话题时的权重加成
NEW_USER_EXPRESS_WEIGHT = 3    # 新用户表达感受类权重
NEW_USER_CHECKIN_WEIGHT = 25   # 新用户关心类权重
NEW_USER_HELLO_WEIGHT = 25     # 新用户招呼类权重
LONG_ABSENCE_CHECKIN_BOOST = 30  # 长时间未联系时关心类加成
LONG_ABSENCE_SHARE_BOOST = 10    # 长时间未联系时分享类加成
ANTI_REPEAT_DIVISOR = 3           # 反重复权重除数
MIN_WEIGHT = 1                    # 最小权重

# 新用户消息数量阈值
NEW_USER_THRESHOLD = 3
# 长时间未联系阈值（秒）- 2天
LONG_ABSENCE_THRESHOLD = 172800

# 长时间未联系的动机池：之前两条分支各自只有一句话，同一个用户反复命中会生成雷同的消息
_LONG_ABSENCE_REASONS: List[str] = [
    "好几天没说话了，想看看对方最近怎么样。",
    "隔了好几天没聊，刚才提到一半的事突然又想起来了。",
    "这几天都没动静，不知道对方在忙什么。",
    "翻手机看到跟TA的聊天记录停在几天前。",
]

_ABSENCE_REASONS: List[str] = [
    "有一天多没联系了。",
    "昨天聊到一半就去睡了，今天还没说上话。",
    "隔了一天，有点想接上前天的话。",
]


# ─── 工具函数 ───────────────────────────────────────

def time_slot(hour: int) -> str:
    """根据小时获取时段名称。

    Args:
        hour: 小时（0-23）

    Returns:
        时段 key
    """
    for start, end, name in _TIME_SLOTS:
        if start <= hour < end:
            return name
    return "deep_night"


def slot_name_cn(slot: str) -> str:
    """获取时段的中文名称。"""
    return _SLOT_NAMES_CN.get(slot, "未知")


def relationship_tier(affection: Optional[float]) -> Tuple[str, str]:
    """根据好感度获取关系等级。

    Args:
        affection: 好感度（0-100），None 表示未知

    Returns:
        (等级 key, 等级描述)
    """
    if affection is None:
        return "unknown", "关系未知，保持自然的社交距离。"
    for threshold, name, desc in _RELATIONSHIP_TIERS:
        if affection < threshold:
            return name, desc
    return "intimate", "关系非常亲近，可以完全放松地表达想念和在意，语气可以亲密自然。"


# 关系档位序数：用于按熟络程度过滤消息示例（unknown 视作一般熟）
_TIER_RANK: Dict[str, int] = {
    "acquaintance": 0,
    "casual": 1,
    "friendly": 2,
    "close": 3,
    "intimate": 4,
}


def tier_rank(affection: Optional[float]) -> int:
    """好感度 → 关系档位序数（0-4，未知按 1）。"""
    key, _ = relationship_tier(affection)
    return _TIER_RANK.get(key, 1)


def examples_for(msg_type: str, affection: Optional[float]) -> List[str]:
    """取某消息类型在当前关系档位下可展示的示例文本。"""
    rank = tier_rank(affection)
    info = MESSAGE_TYPES.get(msg_type, {})
    return [t for t, min_rank in info.get("examples", []) if min_rank <= rank]


def energy_descriptor(energy: Optional[float]) -> str:
    """根据精力值获取描述词。"""
    if energy is None:
        return "unknown"
    for threshold, desc in ENERGY_THRESHOLDS:
        if energy < threshold:
            return desc
    return "energetic"


def social_energy_descriptor(social_energy: Optional[float]) -> str:
    """根据社交能量值获取描述词。"""
    if social_energy is None:
        return "unknown"
    for threshold, desc in SOCIAL_ENERGY_THRESHOLDS:
        if social_energy < threshold:
            return desc
    return "full"


# ─── 对话连续性检测 ─────────────────────────────────

def _detect_continuity(user_data: Dict[str, Any], now: float) -> Optional[str]:
    """检测是否有自然的理由延续之前的对话。

    注意：不以「对方有未回答的问题」为理由——那会让主动消息变成迟回复，
    穿帮「我有自己的生活、只是随口说一句」的人设；回复由 AstrBot 主链路负责。

    Args:
        user_data: 用户状态数据
        now: 当前时间戳

    Returns:
        连续性理由文本，没有则返回 None
    """
    msg = str(user_data.get("last_message", ""))
    age = now - float(user_data.get("last_seen", now))

    # 有可延续的话题：主动方视角（我这边有了下文），不是回答对方
    topics = user_data.get("topics", [])
    if topics and age < TOPIC_CONTINUE_THRESHOLD:
        topic = topics[0]
        return f"之前聊到过「{topic}」，我这边后来有了点下文，想顺嘴提一句。"

    # 刚聊完
    if age < RECENT_CHAT_THRESHOLD and msg:
        return "刚聊完，还想再说点什么。"

    return None


# ─── 消息类型选择 ───────────────────────────────────

def select_message_type(
    user_data: Dict[str, Any],
    now: float,
    recent_types: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """选择消息类型，避免重复。

    Args:
        user_data: 用户状态 dict
        now: 当前时间戳
        recent_types: 最近使用的消息类型列表（用于反重复）

    Returns:
        (type_key, type_info) 元组
    """
    recent_types = recent_types or []
    age = now - float(user_data.get("last_seen", now))
    msg_count = int(user_data.get("message_count", 0))

    # 复制基础权重
    weights: Dict[str, float] = {
        key: float(info["weight"])
        for key, info in MESSAGE_TYPES.items()
    }

    # 有到点的由头：这次就是去问那件事的，其他类型都往后排
    if live_cue(user_data, now):
        weights["continue_topic"] = weights.get("continue_topic", 20) + CONTINUITY_BOOST * 2
        weights["casual_hello"] = max(MIN_WEIGHT, weights["casual_hello"] / 3)

    # 有可延续的对话时，提升话题延续类权重
    if age < TOPIC_CONTINUE_THRESHOLD and (user_data.get("topics") or user_data.get("last_message")):
        weights["continue_topic"] = weights.get("continue_topic", 20) + CONTINUITY_BOOST

    # 新用户（消息少）：避免过于情绪化/亲密的类型
    if msg_count < NEW_USER_THRESHOLD:
        weights["express_feeling"] = NEW_USER_EXPRESS_WEIGHT
        weights["check_in"] = NEW_USER_CHECKIN_WEIGHT
        weights["casual_hello"] = NEW_USER_HELLO_WEIGHT

    # 长时间未联系：提升关心类权重
    if age > LONG_ABSENCE_THRESHOLD:
        weights["check_in"] += LONG_ABSENCE_CHECKIN_BOOST
        weights["share_thought"] += LONG_ABSENCE_SHARE_BOOST

    # 反重复：降低最近使用过的类型权重
    for t in recent_types[-3:]:
        if t in weights:
            weights[t] = max(MIN_WEIGHT, weights[t] / ANTI_REPEAT_DIVISOR)

    # 加权随机选择
    total = sum(weights.values())
    if total <= 0:
        return "share_thought", MESSAGE_TYPES["share_thought"]

    r = random.uniform(0, total)
    cumulative = 0.0
    for key, w in weights.items():
        cumulative += w
        if r <= cumulative:
            return key, MESSAGE_TYPES[key]

    # 兜底
    return "share_thought", MESSAGE_TYPES["share_thought"]


# ─── 理由生成 ───────────────────────────────────────

def generate_reason(
    user_data: Dict[str, Any],
    now: float,
    hour: Optional[int] = None,
) -> Tuple[str, Dict[str, Any]]:
    """生成丰富的、上下文相关的主动联系理由 + 消息类型。

    Args:
        user_data: 用户状态数据
        now: 当前时间戳
        hour: 她那里现在是几点。不传则退回本机小时（时区换算会选错时段理由）

    Returns:
        (reason_text, context_dict) 元组
        context_dict 包含：
        - category: 理由类别
        - msg_type: 选中的消息类型 key
        - msg_type_desc: 消息类型描述（用于 prompt）
        - msg_examples: 消息类型示例（用于 prompt）
    """
    age = now - float(user_data.get("last_seen", now))
    msg_count = int(user_data.get("message_count", 0))
    recent_types = user_data.get("recent_msg_types", [])

    # 选择消息类型；示例按当前关系档位过滤（engine 传入的 u_copy 带 _affection）
    msg_type, msg_info = select_message_type(user_data, now, recent_types)
    examples = examples_for(msg_type, user_data.get("_affection"))

    def meta(category: str, **extra: Any) -> Dict[str, Any]:
        m: Dict[str, Any] = {
            "category": category,
            "msg_type": msg_type,
            "msg_type_desc": msg_info["desc"],
            "msg_examples": examples,
        }
        m.update(extra)
        return m

    # 0. 到点的由头：这才是真人主动开口的常态 —— 想起一件具体的事
    cue = live_cue(user_data, now)
    if cue:
        return (
            f"TA 之前说过「{cue}」，现在差不多到点了，想顺嘴问一句后来怎么样。",
            meta("cue", cue=cue),
        )

    # 1. 对话连续性
    continuity = _detect_continuity(user_data, now)
    if continuity:
        return continuity, meta("continuity")

    # 2. 长时间未联系（固定句子会让模型每次都写出同一句，改成小池子）
    if age > LONG_ABSENCE_THRESHOLD:
        return random.choice(_LONG_ABSENCE_REASONS), meta("long_absence")
    if age > 86400:
        return random.choice(_ABSENCE_REASONS), meta("absence")

    # 3. 基于时间的理由（用调用方给的时刻，避免与引擎的判断各取一次时钟）
    slot = time_slot(hour if hour is not None else datetime.fromtimestamp(now).hour)
    reason = random.choice(_TIME_REASONS.get(slot, _TIME_REASONS["afternoon"]))

    # 添加熟悉度上下文
    if msg_count >= 10:
        reason += " 关系挺熟的，可以很随意。"
    elif msg_count >= 3:
        reason += " 算是有点熟了。"
    else:
        reason += " 还不太熟。"

    return reason, meta("time", slot=slot)
