"""私聊收尾沉默守卫：双方互相道晚安时，这一轮不该再回。

为什么需要：私聊里「晚安」是会互相传染的。她说一句晚安，对方也回一句晚安，于是
她又补一句「晚安啦」——两边都觉得自己只是在礼貌收尾，实际却把对方一次次从被窝里
叫回来。群聊里沉默是常态，私聊里「不回」反而需要一个理由，所以这条判据必须单独
拿出来：只有**本轮消息纯粹是收尾**、并且**她窗口内已经收过尾**（或对方上一句也已经
收尾）时，才允许这一轮不回。

保守到什么程度：

* 没有可用历史、时间戳读不出来、历史时间晚于当前输入——一律照常回（fail-open）；
* 本轮必须整句都是收尾词（≤48 字、无问号、无请求/情绪/媒体/新内容），一句话里只要
  还夹着别的东西就不静默；
* 「她已经收尾」只认强收尾（晚安/道别/感谢/早安/亲昵）。她只回一句「好的」是应声，
  不是收尾——对方第一次说晚安仍然值得一句回应；
* 窗口外（默认 30 分钟）的晚安不算数：隔了很久再补一句晚安，那是新的对话。

这里是纯函数：不读配置、不写存储、不 sleep、不发消息，输入只有调用方给的文本、
social 历史（{"dir": "in"|"out", "text", "ts"}）和当前时刻。身份校验、配置读取、
真正跳过模型这些仍然留在 engine 侧。
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Dict, Optional, Tuple

# 纯收尾天生很短，这个上限同时挡掉「最后两个字是晚安」的长段落——长段落里一定有
# 别的内容，那就不该被判成收尾。
MAX_CLOSING_CHARS = 48
# 她自己那句可以更长，也可以带一句收尾的尾巴，例如「今天辛苦了，好好休息，晚安」。
# 仍然要封顶，否则一整篇小作文都能因为最后一个词是「晚安」而算收尾。
MAX_BOT_CLOSING_CHARS = 120
# 默认 30 分钟：晚安的时效就是「这会儿」，隔太久再说的晚安属于新对话。
DEFAULT_WINDOW_SECONDS = 1800

# 最长优先只影响可读性：下面的扫描器在每个位置都会把所有词试一遍，再退到填充词。
GOODNIGHT_TOKENS = (
    "晚安", "安安", "好梦", "做个好梦", "做一个好梦", "做个梦", "梦里见", "梦里相见",
    "睡个好觉", "好好睡", "好好休息", "早点休息", "早点睡", "早睡", "快去睡", "去睡觉",
    "去睡了", "去睡啦", "睡觉去", "睡觉了", "睡觉啦", "该睡了", "该睡啦", "要睡了",
    "要睡啦", "我睡了", "我睡啦", "我先睡了", "我先睡啦", "那我先睡了", "那我睡了",
    "我困了", "困了", "休息了", "我先休息了", "我先休息啦", "先休息了", "先休息啦",
    "睡一下", "眯一会儿", "眯一会", "眯会儿", "躺一会儿", "躺一会", "躺了", "上床了",
    "关灯了", "这就睡", "这就去睡", "马上睡", "马上睡啦", "立刻睡", "先睡了", "先睡啦",
    "睡啦", "睡喽", "睡咯", "睡了", "睡吧", "睡觉", "我睡喽", "我睡吧", "该睡喽",
    "要睡喽", "去睡", "快睡", "还要早起", "要早起", "得早起", "安",
    "goodnight", "nightynight", "night", "gn", "sweetdreams", "sleepwell",
)
FAREWELL_TOKENS = (
    "拜拜", "拜", "白白", "再见", "再会", "回见", "回聊", "明天见", "明儿见",
    "早上见", "晚上见", "下次见", "改天见", "改天聊", "下次聊", "回头聊", "有空聊",
    "先这样", "先不聊了", "不聊了", "先撤了", "先撤", "撤了", "走了", "我走了",
    "下线了", "下了", "先下了", "我去忙了", "忙去了", "去忙了", "先忙了", "你忙吧",
    "去吃饭了", "吃饭去了", "出门了", "我出门了", "去洗澡了", "洗澡去了", "路上小心",
    "一路顺风", "goodbye", "bye", "seeyou", "seeya",
)
THANKS_TOKENS = (
    "谢谢", "多谢", "感谢", "辛苦", "麻烦", "感恩", "谢啦", "谢了",
    "thanks", "thankyou", "thx",
)
ACK_TOKENS = (
    "好的", "好", "好哒", "好嘞", "好呀", "好啊", "好吧", "好嘛", "好啦", "行", "行吧",
    "行呀", "行啦", "可以", "可以的", "可以啦", "收到", "知道了", "知道啦", "明白",
    "明白了", "明白啦", "了解", "了解啦", "没事", "没事的", "没关系", "那就好", "那好",
    "嗯", "嗯嗯", "嗯呢", "嗯哼", "哦", "噢", "喔", "安啦", "ok", "okay", "alright",
    "sure", "fine",
)
MORNING_TOKENS = (
    "早安", "早上好", "早", "早呀", "早啊", "晨安", "morning", "goodmorning",
)
AFFECTION_TOKENS = (
    "爱你", "么么哒", "么么", "亲亲", "抱抱", "啵啵", "mua",
)
TOKEN_KINDS = {
    "goodnight": GOODNIGHT_TOKENS,
    "farewell": FAREWELL_TOKENS,
    "thanks": THANKS_TOKENS,
    "ack": ACK_TOKENS,
    "morning": MORNING_TOKENS,
    "affection": AFFECTION_TOKENS,
}
# 只有强收尾能证明「她已经把话收完了」。一句「好的」/「嗯」只是应声，不是道别：
# 要是它也算数，对方第一次说晚安就会被吞掉，等于她连一句晚安都没回过。
STRONG_KINDS = frozenset(("goodnight", "farewell", "thanks", "morning", "affection"))
_TOKEN_KIND: Dict[str, str] = {}
for _kind, _tokens in TOKEN_KINDS.items():
    for _token in _tokens:
        _TOKEN_KIND.setdefault(_token, _kind)
_TOKENS = tuple(sorted(_TOKEN_KIND, key=len, reverse=True))
# 填充词只负责把同一句收尾说得更自然（「晚安啦」「那我也睡了」）。它们在每个位置都
# 排在词之后尝试，所以填充词永远抢不走一个已知收尾词的开头。
_FILLERS = tuple(sorted({
    "啦", "呀", "啊", "呢", "吧", "哈", "嘛", "咯", "喽", "哒", "滴", "嘞", "哟", "呦",
    "嘿", "诶", "欸", "了", "的", "我", "你", "您", "也", "都", "就", "先", "那", "那么",
    "再", "快", "乖", "是", "宝贝", "亲爱的", "宝宝", "小可爱", "大家", "今天", "今晚",
    "明天", "早上", "晚上", "现在", "已经", "一会儿", "一下",
} - set(_TOKEN_KIND), key=len, reverse=True))
_STRONG_TOKENS = tuple(sorted(
    (token for token, kind in _TOKEN_KIND.items() if kind in STRONG_KINDS),
    key=len, reverse=True))

# 媒体占位符先剥掉：她发的是图，正文里那行「[图片]」不是内容，不该影响判定；
# 但 has_media 仍然会让这一轮照常回（见 closing_decision）。
_MEDIA_PLACEHOLDER = re.compile(
    r"(?:\[(?:图片|语音|音频|视频|文件|合并转发|引用消息|分享卡片)[^\]]*\])"
    r"|(?:【(?:图片|语音|音频|视频|文件|合并转发|引用消息|分享卡片)[^】]*】)")
# 问句/请求/索取回应：她自己那句里只要出现这些，就不算「已经把话收完了」。
_QUESTION_OR_REQUEST = re.compile(
    r"[?？]|吗|几点|要不要|好不好|行不行|可以吗|好吗|你说|你觉得|想不想|去不去|"
    r"吃了吗|睡了吗|在吗|有没有|是不是|会不会|能不能|需要我|记得|提醒|叫我|告诉我|"
    r"发给我|帮我|陪我|听我说")


def normalize(text: str) -> str:
    """去掉媒体标签、标点、符号、空白并转小写。

    表情属于符号（category 以 S 开头），所以纯表情消息归一化后是空串，不会被当成
    收尾。数字保留——「23:00 晚安」因此不是纯收尾：一个具体时间点本身就是内容。
    """
    compact = _MEDIA_PLACEHOLDER.sub("", str(text or ""))
    kept = []
    for char in compact:
        if char.isspace() or unicodedata.category(char)[0] in "PSCZ":
            continue
        kept.append(char)
    return "".join(kept).casefold()


def _scan(compact: str) -> Tuple[str, ...]:
    """把整段压缩文本按「收尾词 + 填充词」消费干净。

    返回按首次出现排序的类别；只要有一个字符不属于这套小词表就返回 ()。
    每个位置先试词再试填充词，所以「晚安啦」读作 晚安 + 啦，收尾词不会输给语气词。
    """
    index, kinds = 0, []
    while index < len(compact):
        for token in _TOKENS:
            if compact.startswith(token, index):
                index += len(token)
                kind = _TOKEN_KIND[token]
                if kind not in kinds:
                    kinds.append(kind)
                break
        else:
            for filler in _FILLERS:
                if compact.startswith(filler, index):
                    index += len(filler)
                    break
            else:
                return ()
    return tuple(kinds) if kinds else ()


def closing_categories(text: str) -> tuple:
    """整条消息都是收尾时返回类别元组，否则返回 ()。"""
    raw = str(text or "")
    if "?" in raw or "？" in raw:
        # 带问号就是提问，再短也不算收尾。
        return ()
    compact = normalize(raw)
    if not compact or len(compact) > MAX_CLOSING_CHARS:
        return ()
    return _scan(compact)


def is_closing(text: str) -> bool:
    return bool(closing_categories(text))


def _ends_with_strong_closing(compact: str) -> bool:
    if not compact:
        return False
    # 「晚安啦」「晚安宝贝」：最多剥几层尾部的语气词/称呼，每剥一层再查一次强收尾后缀。
    for _ in range(3):
        if any(compact.endswith(token) for token in _STRONG_TOKENS):
            return True
        for filler in _FILLERS:
            if compact.endswith(filler) and len(compact) > len(filler):
                compact = compact[: -len(filler)]
                break
        else:
            return False
    return False


def role_closed(text: str) -> bool:
    """她自己那句话是不是一个强收尾、且没有在问/要东西。

    允许这句话前面还有别的正文，但必须以强收尾结尾，而且不能带问句或请求。
    只回一句「好的」不算：对方第一次说晚安，她仍然该正经回一句。
    """
    raw = str(text or "")
    if not raw.strip():
        return False
    if _QUESTION_OR_REQUEST.search(raw):
        return False
    compact = normalize(raw)
    if not compact or len(compact) > MAX_BOT_CLOSING_CHARS:
        return False
    kinds = _scan(compact)
    if kinds and any(kind in STRONG_KINDS for kind in kinds):
        return True
    return _ends_with_strong_closing(compact)


def _epoch(value: Any) -> Optional[float]:
    """把 social 的 epoch 秒时间戳读成 float；读不出来就返回 None（交给上层 fail-open）。

    bool 显式排除：True 是 int 的子类，会被 float() 收成 1.0，变成一个 1970 年的
    假时间戳，比「不知道」更糟。
    """
    if isinstance(value, bool):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp if math.isfinite(stamp) else None


def _last_exchange(dialogue) -> Tuple[Optional[dict], Optional[dict]]:
    """从正序历史里取「她最后一条 out」和「它之前最近的一条 in」。

    为什么不直接取最后一条消息：当前这轮输入可能已经落进历史，也可能还没落，两种
    情况下「她上一句 + 它之前对方那句」都是稳定的锚点；用最后一条则会随落库时机漂移。
    """
    items = tuple(item for item in (dialogue or ()) if isinstance(item, dict))
    assistant_index = None
    for index in range(len(items) - 1, -1, -1):
        if items[index].get("dir") == "out":
            assistant_index = index
            break
    if assistant_index is None:
        return None, None
    prior_user = None
    for index in range(assistant_index - 1, -1, -1):
        if items[index].get("dir") == "in":
            prior_user = items[index]
            break
    return items[assistant_index], prior_user


def _coerce_window(value: Any) -> int:
    """窗口容错：脏配置不该让私聊要么永远不回、要么永远不回。

    只挡非正数/非有限值，取默认 30 分钟。不夹上下界是故意的——窗口是调用方的显式
    选择，这里替它改语义会让「我就想看最近 1 分钟」这种调用悄悄失效。
    """
    try:
        window = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_WINDOW_SECONDS
    if not math.isfinite(window) or window <= 0:
        return DEFAULT_WINDOW_SECONDS
    return int(window)


def _base_decision(window: int) -> Dict[str, Any]:
    return {
        "suppress_reply": False,
        "action": "reply",
        "reason": "not_evaluated",
        "last_reply_at": None,
        "categories": (),
        "last_assistant_closing": False,
        "prior_user_closing": False,
        "window_seconds": window,
    }


def _decide(
    *,
    current_text: str,
    dialogue,
    at: Any,
    has_media: bool,
    window: int,
    enabled: bool,
    base: Dict[str, Any],
) -> Dict[str, Any]:
    if not enabled:
        return {**base, "reason": "disabled"}
    categories = closing_categories(current_text)
    assistant, prior_user = _last_exchange(dialogue)
    evidence = dict(base)
    role_closed_flag, prior_closed, delta = False, False, None
    if assistant is not None:
        role_at = _epoch(assistant.get("ts"))
        now = _epoch(at)
        if role_at is not None and now is not None:
            delta = now - role_at
            if 0 <= delta <= window:
                role_closed_flag = role_closed(assistant.get("text"))
                prior_closed = bool(
                    prior_user is not None and is_closing(prior_user.get("text"))
                )
                evidence.update(
                    last_reply_at=role_at,
                    last_assistant_closing=role_closed_flag,
                    prior_user_closing=prior_closed,
                )
    if categories:
        evidence["categories"] = categories
    if has_media:
        # 有图/语音/文件就一定有东西要看，哪怕正文只有「晚安」也得回。
        return {**evidence, "reason": "media_input"}
    if not categories:
        # 本轮不是纯收尾，永远不该被静默；证据留着给 closing_instruction 用。
        return {**evidence, "reason": "not_a_closing"}
    if assistant is None:
        return {**evidence, "reason": "no_role_reply"}
    if delta is None:
        return {**evidence, "reason": "history_time_unknown"}
    # 这四个异常分支原来返回 {**base}，把上面刚收集的证据（categories、
    # last_reply_at、双方是否已收尾）全丢了，closing_instruction 拿到的是
    # 「什么都没发生」——而它恰恰要靠这些证据决定要不要补一句（P3-23）。
    if delta < 0:
        # 这两条分支的 evidence 里没有 last_reply_at（上面的 update 只在
        # 0<=delta<=window 时才填），所以要把角色回复时刻单独带上——
        # 旧实现只给了它、丢了 categories，改过来时不能反过来丢这个。
        return {**evidence, "last_reply_at": role_at, "reason": "history_after_input"}
    if delta > window:
        return {**evidence, "last_reply_at": role_at, "reason": "outside_window"}
    if role_closed_flag:
        return {**evidence, "suppress_reply": True, "action": "silent",
                "reason": "role_already_closed"}
    if prior_closed:
        return {**evidence, "suppress_reply": True, "action": "silent",
                "reason": "repeated_user_closing"}
    return {**evidence, "reason": "role_did_not_close"}


def closing_decision(
    *,
    current_text: str,
    dialogue,
    at: float,
    has_media: bool = False,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    enabled: bool = True,
) -> dict:
    """判定这一轮私聊是不是「收尾循环里多余的那一半」。

    dialogue 是 social 历史：按时间正序的 [{"dir": "in"|"out", "text": str, "ts": float}]，
    "out" 是她说的。当前这轮输入不必已经在里面。

    返回的 dict 里 suppress_reply=True 才表示可以跳过模型；其余所有异常路径都回到
    base（reason 为具体原因，suppress_reply=False），也就是照常回。
    """
    window = _coerce_window(window_seconds)
    base = _base_decision(window)
    try:
        return _decide(
            current_text=current_text,
            dialogue=dialogue,
            at=at,
            has_media=has_media,
            window=window,
            enabled=enabled,
            base=base,
        )
    except Exception:
        # 这条判据的价值是「少回一句」，不是「保证回话」。任何意外都不该变成静默，
        # 所以统一退回 base：reason=not_evaluated，action=reply。
        return {**base, "reason": "not_evaluated"}


def closing_instruction(decision) -> str:
    """给「不能直接跳过」的情况一句当轮提示；没有可提示的就返回空串。

    为什么要提示而不是直接不回：她上一轮已经收尾、而对方这句带了新内容（比如「今天
    好累」），这一轮必须回；可模型很容易顺口又补一句「晚安」，收尾就又被续上了。
    提示只约束「别再补同义道别」，不改变回不回。
    """
    if not isinstance(decision, dict):
        return ""
    if decision.get("suppress_reply"):
        return ""
    if decision.get("last_assistant_closing"):
        return (
            "【对话收尾】上一轮你已经用晚安、道别或感谢一类话收过尾了。TA 这句如果仍然只是"
            "收尾、致谢或一句简短应声，就不要再补一条同义的道别，也别另起话题、追问或要 TA 回话；"
            "让对话自然停在这儿。真出现新的问题、请求或需要接住的明确情绪，再正常回应。"
        )
    if decision.get("prior_user_closing"):
        return (
            "【对话收尾】TA 上一句已经在道别或收尾了。这一轮如果还是收尾，不必为了礼貌再追一句；"
            "有新问题、请求或需要回应的事实，再正常说。"
        )
    return ""
