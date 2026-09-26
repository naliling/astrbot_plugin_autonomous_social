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

from .clock import city_epoch, city_now


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


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

# 以前这里是 7 个时段 × 8 条共 56 句「你现在可能在想什么」——
# “楼下卖早餐的香味飘上来了”“咖啡喝到第二杯了，还是有点困”“食堂今天居然有我爱吃的菜”。
# 它被当作动机塞进 prompt，模型就顺着往下编，于是她讲的是根本没发生过的事：
# 掷一次骰子决定她今天吃了什么、心情如何。这不是拟人，是替她编记忆。
# 动机现在由模型自己在 decide 阶段产生（见 generator 的输出格式），这里只保留时段本身。
TIME_SLOT_LABELS = {
    "early_morning": "清晨",
    "morning": "上午",
    "lunch": "午休",
    "afternoon": "下午",
    "evening": "傍晚",
    "late_night": "夜里",
    "deep_night": "深夜",
}


# ─── 消息类型定义 ────────────────────────────────────
#
# 每种类型只有三样东西：基础权重、描述（这条要干什么）、风格提示（怎么说才对）。
#
# 这里**不再放例句**。以前每个类型带五六句现成的话（“楼下的猫又在晒太阳”、
# “脑子突然冒出个念头”…），加上 generator 那边十四条通用正例，一共四十多句交给模型。
# 那些句子都通用、彼此只差几个字，模型会直接复用，最后每轮都在这几十句里轮着挑——
# 看着像人，其实在背稿子。style_hint 约束的是感觉和分寸（别铺垫、别硬编、别复述），
# 具体说什么由模型自己造。
#
# 关系档位的影响仍然体现在 weights 上（越亲近越敢表达、吐槽），不需要靠例句演示。

MESSAGE_TYPES: Dict[str, Dict[str, Any]] = {
    "share_thought": {
        "weight": 22,
        "desc": "说一件脑子里刚冒出来的事",
        "style_hint": "像随手想到什么就说一句；别把它讲成一件大事，也别用「我突然想起」这种开场",
    },
    "express_feeling": {
        "weight": 18,
        "desc": "自然地表达一点情绪或感受",
        "style_hint": "直接说感受本身，不要铺垫，也不要解释自己为什么会这样",
    },
    "share_daily": {
        "weight": 16,
        "desc": "分享一件日常小事",
        "style_hint": "像顺手拍给对方看一眼，有画面就行，不用交代来龙去脉",
    },
    "continue_topic": {
        "weight": 14,
        "desc": "接着之前聊过的话题往下说",
        "style_hint": "直接接上那件事；别重新起个头，也别先复述一遍之前说了什么",
    },
    "casual_hello": {
        "weight": 10,
        "desc": "没什么事，就是随口说一句",
        "style_hint": "既然真的没什么事，就别硬编一件事出来；一句就够",
    },
    "complain": {
        "weight": 8,
        "desc": "吐槽一件小事",
        "style_hint": "嘟囔一件具体的小事，别讲大道理、别抱怨人生",
    },
    "check_in": {
        "weight": 6,
        "desc": "顺口关心一句",
        "style_hint": "关心得落在具体的事上，别只是「你还好吗」这种空的问候",
    },
    "ask_advice": {
        "weight": 4,
        "desc": "问对方一个小意见",
        "style_hint": "像是真想听听 ta 的想法，问完别自己接着答",
    },
    "react_time": {
        "weight": 2,
        "desc": "对时间或状态的一句反应",
        "style_hint": "就这一下，别顺势展开成聊天",
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


def _at_days_ahead(now: float, days: int, hour: int, minute: int, offset: Optional[int] = None) -> float:
    """now 往后第 days 天的 hour:minute；若那个时刻已过去则再挨后一天。

    hour:minute 是「她那里」的时刻，所以先按她城市的挂钟时间算，再换回 epoch
    （UTC 基准，不受容器时区影响）。
    """
    base = city_now(now, offset)
    target = (base + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    stamp = city_epoch(target, offset)
    if stamp <= now:
        stamp += 86400.0
    return stamp


def _next_weekday(now: float, weekday: int, hour: int = 10, minute: int = 0, offset: Optional[int] = None) -> float:
    """下一个星期 weekday（0=周一）的 hour:minute。今天就是这个星期几且已过点则算下周。"""
    base = city_now(now, offset)
    delta = (weekday - base.weekday()) % 7
    target = (base + timedelta(days=delta)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    stamp = city_epoch(target, offset)
    if stamp <= now:
        stamp += 7 * 86400.0
    return stamp


def _anchor_due(now: float, pattern: str, days: int, hour: int, minute: int, offset: Optional[int] = None) -> float:
    """把锚点算成一个具体时间点（她所在城市的时间）。"""
    if days == -1:
        return _next_weekday(now, 5, 12, 0, offset)
    if days == 0 and hour == 0 and minute == 0:
        return now + _CUE_REL_SECONDS
    return _at_days_ahead(now, days, hour, minute, offset)


def extract_cue(
    text: str,
    now: Optional[float] = None,
    clock_offset_minutes: Optional[int] = None,
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
    """有没有已经到点、还没用过、也没凉过头的由头。

    三个时间各管一件事：cue_due 是锚点本身（记下就不再变），cue_expire_at 是它的绝对
    寿命，cue_retry_at 只是「上次没发成、最早什么时候再想」。只推迟重试而不动寿命，
    同一件事才不会被每 6 小时重试一次、永不作废。
    """
    cue = str(user.get("cue", "") or "")
    try:
        due = float(user.get("cue_due", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not cue or due <= 0:
        return None
    if now < due:
        return None
    try:
        retry_at = float(user.get("cue_retry_at", 0) or 0)
    except (TypeError, ValueError):
        retry_at = 0.0
    if retry_at > 0 and now < retry_at:
        return None
    try:
        expire_at = float(user.get("cue_expire_at", 0) or 0)
    except (TypeError, ValueError):
        expire_at = 0.0
    if now > (expire_at if expire_at > 0 else due + CUE_LATE_LIMIT):
        return None
    return cue


# ─── 睡意信号 ────────────────────────────────

_SLEEP_WORDS = (
    "晚安", "要睡了", "去睡", "先睡", "睡了", "困死", "困了", "眯一会",
    "眯会儿", "熬不住", "准备睡", "睡觉了", "躺了",
)
SLEEP_SUPPRESS_SECONDS = 8 * 3600


def sleep_signal(user: Dict[str, Any], now: float) -> bool:
    """对方最近说过要睡了：这时候再去发消息（尤其别再说「该睡了」）很不对。

    只看**对方**（dir=='in'）说的话：她自己道的晚安不算对方要睡。
    旧实现用 `text in _SLEEP_WORDS` 先 continue，把正好等于一个睡意词的消息（如「晚安」）
    跳掉了，导致对方明明道了晚安却判不出来；也没按 dir 过滤，bot 自己的「晚安」也会被扫到。
    """
    conv = user.get("conversation") or []
    try:
        recent = [m for m in conv if str(m.get("text", ""))][-4:]
    except TypeError:
        return False
    for m in reversed(recent):
        # 只看对方说的；她自己说的（dir=='out'）跳过
        if str(m.get("dir", "")) == "out":
            continue
        if now - float(m.get("ts", 0) or 0) > SLEEP_SUPPRESS_SECONDS:
            return False
        if any(w in str(m.get("text", "")) for w in _SLEEP_WORDS):
            return True
    msg = str(user.get("last_message", "") or "")
    if msg and any(w in msg for w in _SLEEP_WORDS):
        return now - float(user.get("last_seen", 0) or 0) <= SLEEP_SUPPRESS_SECONDS
    return False

# ─── 离开语 ────────────────────────────────

# 未完话题的判定都放在 threads.py；这里只留两个被多处复用的底层判断。

# 对方明确说了要离开：这时候再问「在吗」是没听见人说话
_LEAVING_WORDS = (
    "去忙", "先忙", "忙着", "在忙", "有事", "先走了", "出门", "开车",
    "洗澡", "吃饭去", "去吃饭", "做饭", "上班", "上课", "开会", "下班",
    "健身", "赶个", "交材料", "写作业", "辅导", "接孩子", "取快递",
    "不聊了", "先不聊", "回头聊", "待会说", "回头再说", "下次再聊",
    "改天说", "先这样", "先这样吧",
)


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


# ─── 消息类型权重调整 ────────────────────────────────

CONTINUITY_BOOST = 40           # 有可延续话题时的权重加成
NEW_USER_EXPRESS_WEIGHT = 3    # 新用户表达感受类权重上限
# 新用户：关心类与招呼类要压下去。这两种最容易写出「在吗」「最近怎么样」这类没内容的
# 开场白，而它们正是 generator._SENTENCE_RULES 里点名的「不带自身信息的问候」
NEW_USER_CHECKIN_SCALE = 0.4
NEW_USER_HELLO_SCALE = 0.4
# 新用户：反过来抬高真正有东西可讲的两种
NEW_USER_SHARE_BOOST = 1.4
# 长时间未联系：倍率而不是绝对加成
LONG_ABSENCE_CHECKIN_BOOST = 3.0   # 关心类 ×(1+3.0)
LONG_ABSENCE_SHARE_BOOST = 1.0     # 分享想法 ×(1+1.0)
ANTI_REPEAT_DIVISOR = 3           # 反重复权重除数
MIN_WEIGHT = 1                    # 最小权重

# 新用户消息数量阈值
NEW_USER_THRESHOLD = 3
# 长时间未联系阈值（秒）- 2天
LONG_ABSENCE_THRESHOLD = 172800

# 长时间未联系的动机池：之前两条分支各自只有一句话，同一个用户反复命中会生成雷同的消息



# ─── 一句话里有没有「没问完的问题」 ───────────────────

_QUESTION_TAIL = re.compile(r"[?？]\s*$|[吗呢吧呗]\s*[!！。~～…]*\s*$")
_QUESTION_WORDS = (
    "怎么", "怎样", "为什么", "为啥", "什么", "啥", "哪里", "哪儿", "哪个", "几点",
    "多少", "是不是", "有没有", "行不行", "可不可以", "要不要", "会不会", "能不能",
    "谁", "几点", "咋", "怎样", "如何", "呢吗",
)


def is_question(text: str) -> bool:
    """这句话像是在问事。她上一句是不是问句，决定了对方没接时该不该追问。"""
    body = str(text or "").strip()
    if not body:
        return False
    if _QUESTION_TAIL.search(body):
        return True
    return any(word in body for word in _QUESTION_WORDS)


# 对方接话接得太短，基本等于没答：「嗯」「还行」「就那样」。
_THIN_MAX_CHARS = 8
_THIN_WORDS = (
    "嗯", "哦", "噢", "喔", "啊", "额", "呃", "行", "好", "好的", "还行", "还好", "一般",
    "随便", "都行", "可以", "没事", "没啥", "不知道", "忘了", "算了", "不聊", "先不",
    "哈哈", "嘿嘿", "呵呵", "emm", "emmm", "ok", "嗯嗯", "是", "对", "有过", "就那样",
)


def is_thin(text: str) -> bool:
    """对方这句回答基本没带信息：需要追问才能把话接下去。"""
    body = str(text or "").strip().lower()
    if not body:
        return True
    cleaned = body.strip("。！~～. 　")
    if len(cleaned) <= _THIN_MAX_CHARS:
        if any(word in cleaned for word in _THIN_WORDS):
            return True
    return len(cleaned) <= 2


# ─── 早晚问候（时间性触发） ─────────────────────────

# 问候不靠念头攒：它是「一天两次的固定由头」，到了窗口就该说，跟想不想聊无关。
# 默认窗口（小时，按她所在城市的钟走）：早安 7-11，晚安 21-24。
GREETING_MORNING_WINDOW = (7, 11)
GREETING_NIGHT_WINDOW = (21, 24)

_GREET_REASON_MORNING: List[str] = [
    "早上刚醒，脑子还糊着，顺手跟TA说句早安。",
    "洗漱完摸到手机，想跟TA道个早安。",
    "醒了一会儿了，今天也想跟TA说句早安。",
    "外面天刚亮，起来第一件事想跟TA道声早。",
    "闹钟响过了还没完全醒，想跟TA说个早安。",
]

_GREET_REASON_NIGHT: List[str] = [
    "今天到最后了，想跟TA道一句晚安。",
    "准备睡了，睡前想跟TA说声晚安。",
    "今天聊得挺舒服，睡前道个晚安。",
    "夜深了，躺下了想跟TA说句晚安再睡。",
    "眼睛已经睁不开了，想先跟TA道个晚安。",
]


def greeting_window_kind(
    hour: int,
    morning: Tuple[int, int] = GREETING_MORNING_WINDOW,
    night: Tuple[int, int] = GREETING_NIGHT_WINDOW,
) -> Optional[str]:
    """她那里的这个钟点落在哪个问候窗口里。返回 'morning' / 'night' / None。"""
    for start, end in (morning, night):
        if start <= end:
            if start <= hour < end:
                return "morning" if (start, end) == morning else "night"
        else:  # 跨午夜的窗口（如 22 → 2）
            if hour >= start or hour < end:
                return "morning" if (start, end) == morning else "night"
    return None


def greeting_due(user: Dict[str, Any], day: str, kind: str) -> bool:
    """这个人今天（按她所在城市算的 day）还有没有这个窗口的问候可发。"""
    return not (
        str(user.get("greet_day", "") or "") == day
        and str(user.get("greet_kind", "") or "") == kind
    )


def greet_reason(kind: str) -> str:
    """问候的动机（从池子里随机挑一句，喂给生成侧）。"""
    return random.choice(
        _GREET_REASON_NIGHT if kind == "night" else _GREET_REASON_MORNING
    )


def greet_meta(kind: str) -> Dict[str, Any]:
    """问候的 preset 元数据：跟未完话题同一条路走，交给生成器写台词。

    原来这里带四条现成问候（「早 刚醒还迷糊着」「晚安 今天聊得挺开心的」…），
    那等于每天早上替她写好一句照着念。删掉后只剩写法要求，句子由她自己造。
    """
    if kind == "night":
        return {
            "category": "greet",
            "intent": "greet",
            "mode": "greet_night",
            "kind": "night",
            "msg_type": "greeting",
            "msg_type_desc": "睡前跟TA道一句晚安",
            "style_hint": "像今天到此为止一样自然地收尾；别只说晚安两个字，也别解释今天怎么样",
        }
    return {
        "category": "greet",
        "intent": "greet",
        "mode": "greet_morning",
        "kind": "morning",
        "msg_type": "greeting",
        "msg_type_desc": "早上跟TA道个早安",
        "style_hint": "像刚醒没多久随口说的；带一句你自己的状态或今天头一件小事就够，别一上来就问一串",
    }


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

    权重受多种拟人化因素影响：
    - 关系远近：越亲近越敢表达感受和吐槽，不熟的人以分享日常和想法为主
    - 久未联系：越久没聊越容易说「想你」，久到一定程度又会变得客气
    - 精力状态：累的时候抱怨多、分享少；精力好的时候日常分享多
    - 社交能量：高的时候更主动搭话，低的时候更倾向于「随口说一句」

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
    affection = user_data.get("_affection")  # 0-100，可能为 None
    body = user_data.get("_body")  # Core 契约里的身体状态，可能为 None

    # 复制基础权重
    weights: Dict[str, float] = {
        key: float(info["weight"])
        for key, info in MESSAGE_TYPES.items()
    }

    # ─── 关系远近影响类型分布 ──────────────────────────
    # 越亲近，表达感受和吐槽的比例越高；不熟的人以分享日常和想法为主
    if affection is not None:
        aff = clamp(float(affection) / 100.0, 0.0, 1.0)
        # express_feeling：亲近的人表达感受更多（2.2x），不熟的人少一点（0.7x）
        weights["express_feeling"] *= 0.7 + 1.5 * aff
        # complain：越亲近越敢吐槽（越熟越不掩饰）
        weights["complain"] *= 0.5 + 1.8 * aff
        # ask_advice：关系好才会问个人建议
        weights["ask_advice"] *= 0.6 + 1.2 * aff
        # casual_hello：越不熟越用招呼开场
        weights["casual_hello"] *= 1.4 - 0.7 * aff
        # share_daily：日常分享是中性的，跟关系远近关系不大，微调
        weights["share_daily"] *= 0.9 + 0.3 * aff

    # ─── 久未联系：越久越容易说想你，但太久了又会变客气 ───
    days_absent = age / 86400.0
    if days_absent > 0.5:  # 半天以上没聊
        # 「想你」的峰值在 1-3 天左右，之后慢慢回落（太久了说想你有点突兀）
        missing_peak = min(days_absent / 1.5, 1.0) if days_absent < 3 else max(0.3, 1.0 - (days_absent - 3) / 10)
        if affection is not None and float(affection) >= 60:
            # 只有关系够近才会说想你
            weights["express_feeling"] *= 1.0 + missing_peak * 1.5
        # 久未联系时 check_in 也会增加
        weights["check_in"] *= 1.0 + min(days_absent / 2.0, 1.5)
        # 太久没聊了，casual_hello 反而更自然
        if days_absent > 5:
            weights["casual_hello"] *= 1.3

    # ─── 身体状态影响类型 ──────────────────────────
    if body and isinstance(body, dict):
        energy = body.get("energy")
        social_desire = body.get("social_desire")
        sleep_pressure = body.get("sleep_pressure")
        hunger = body.get("hunger")
        discomfort = body.get("discomfort")

        # 累/困的时候：抱怨多、分享想法少、更多表达感受
        if energy is not None and float(energy) < 35:
            weights["complain"] *= 1.4
            weights["share_thought"] *= 0.7
            weights["express_feeling"] *= 1.2
            weights["share_daily"] *= 0.8

        # 不舒服的时候：更想找人说话（表达感受 + 吐槽）
        if discomfort is not None and float(discomfort) >= 50:
            weights["express_feeling"] *= 1.3
            weights["complain"] *= 1.2

        # 饿的时候：吐槽食物相关，分享日常（吃的）更多
        if hunger is not None and float(hunger) >= 70:
            weights["share_daily"] *= 1.2
            weights["complain"] *= 1.1

        # 社交欲望高的时候：更想搭话，招呼类更多
        if social_desire is not None and float(social_desire) >= 70:
            weights["casual_hello"] *= 1.3
            weights["express_feeling"] *= 1.1

        # 社交欲望低的时候：更少主动搭话，更多分享（就是随手发一句）
        if social_desire is not None and float(social_desire) < 30:
            weights["casual_hello"] *= 0.7
            weights["share_daily"] *= 1.2

    # 有到点的由头：这次就是去问那件事的，其他类型都往后排
    if live_cue(user_data, now):
        weights["continue_topic"] = weights.get("continue_topic", 20) + CONTINUITY_BOOST * 2
        weights["casual_hello"] = max(MIN_WEIGHT, weights["casual_hello"] / 3)

    # 有可延续的对话时，提升话题延续类权重
    if age < TOPIC_CONTINUE_THRESHOLD and (user_data.get("topics") or user_data.get("last_message")):
        weights["continue_topic"] = weights.get("continue_topic", 20) + CONTINUITY_BOOST

    # 新用户（消息少）：避免过于情绪化/亲密的类型
    if msg_count < NEW_USER_THRESHOLD:
        # 这三行原来用的是绝对赋值（=25），把上面按 affection 算好的调整整个覆盖掉了；
        # 而且 check_in + casual_hello 合计占到总权重一半以上，而这两种恰恰是最容易写出
        # 「在吗」「最近怎么样」这类没内容的寒暄，正好是 _SENTENCE_RULES 点名的写法。
        # 注释说的是「避免情绪化」，代码却在猛推寒暄。方向一起改过来。
        weights["express_feeling"] = min(
            float(weights.get("express_feeling", NEW_USER_EXPRESS_WEIGHT)),
            float(NEW_USER_EXPRESS_WEIGHT),
        )
        weights["check_in"] *= NEW_USER_CHECKIN_SCALE
        weights["casual_hello"] *= NEW_USER_HELLO_SCALE
        weights["complain"] = max(MIN_WEIGHT, weights.get("complain", 8) * 0.5)
        weights["share_daily"] *= NEW_USER_SHARE_BOOST
        weights["share_thought"] *= NEW_USER_SHARE_BOOST

    # 长时间未联系：提升关心类权重
    if age > LONG_ABSENCE_THRESHOLD:
        # 原来也是绝对加成（+=30）：久未联系的人几乎必然走「在吗/最近怎么样」这类开场。
        # 改成按倍数放大，同样偏向关心，但不会压倒其他类型
        weights["check_in"] *= 1.0 + LONG_ABSENCE_CHECKIN_BOOST
        weights["share_thought"] *= 1.0 + LONG_ABSENCE_SHARE_BOOST

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
        - style_hint: 这一类的写法要求（不包含任何例句）
    """
    age = now - float(user_data.get("last_seen", now))
    recent_types = user_data.get("recent_msg_types", [])

    msg_type, msg_info = select_message_type(user_data, now, recent_types)

    def meta(category: str, **extra: Any) -> Dict[str, Any]:
        m: Dict[str, Any] = {
            "category": category,
            "msg_type": msg_type,
            "msg_type_desc": msg_info["desc"],
            "style_hint": msg_info.get("style_hint", ""),
        }
        m.update(extra)
        return m

    # 下面这些分支选的是「这一条属于哪类事」，决定用哪种写法和约束；
    # 返回的 reason 一律是空串——动机由模型自己在 decide 阶段产生。
    # 插件能给的只有素材（距上次说话多久、TA 最后说了什么、她今天在干什么），
    # 至于她此刻想到了什么，那是她自己的事。

    # 0. 到点的由头：这才是真人主动开口的常态 —— 想起一件具体的事
    cue = live_cue(user_data, now)
    if cue:
        return "", meta("cue", cue=cue)

    # 1. 对话连续性
    if _detect_continuity(user_data, now):
        return "", meta("continuity")

    # 2. 长时间未联系
    if age > LONG_ABSENCE_THRESHOLD:
        return "", meta("long_absence")
    if age > 86400:
        return "", meta("absence")

    # 3. 其余按当时段
    slot = time_slot(hour if hour is not None else datetime.fromtimestamp(now).hour)
    return "", meta("time", slot=slot)
