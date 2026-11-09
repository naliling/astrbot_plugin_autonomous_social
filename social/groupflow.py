"""群聊心流：主动回复的闸门、冷场破冰的时机、发言参考库的风格提炼。

v1.11.0 新增。这一层是纯逻辑（不碰 IO、不 import astrbot），可单测：
- 心流（flow）：bot 在群里说过话后开一个「关注窗口」，窗口内群消息若值得接，
  就无需被 @ 主动接一句。保守档——只有自己刚发过言才开窗，不主动盯整个群。
- 破冰（icebreak）：群安静太久时主动抛个轻话题。
- 参考库：把近期群友发言提炼成一段「这个群平时怎么说话」，注入生成，让接话不端着。
- 归因（attribution，v1.29.0）：这条群消息到底是不是在叫她。**只有 At 才算点名**，
  名字/第三人称只用来解释「为什么不算」——同名群名片、第三人称谈论都不会再被误判。

真正调模型/发消息的接线在 engine.py，写台词在 generator.py。这里只回答
「现在该不该接 / 该不该破冰」和「拿什么当风格参考」。
"""

from __future__ import annotations

import math
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

# emoji 与变体选择符的字符类只有一份，在 `social/emoji.py`；
# `style_profile` / `generator` 引用的是同一个对象。
from .emoji import EMOJI as _EMOJI
# 「外头」前缀的判据同样只有一份（Core 的 weather.env 往往已是整句）。
from .textutil import outside_lead
# 「忙手里的活 / 休着」这种空话的判据也只有一份：群素材里「她自己的生活」
# 和私聊由头是同一批事实，不能一边过滤一边不过滤（否则群破冰会说出
# 「正在忙自己的事」这种等于没说的话）。`anchors` 不 import 本模块，无环。
from .anchors import _wake_cue, is_vague_doing

# S2（v1.28.0）：群破冰/起话头时「她自己的生活」最多给几条、每行多长。
# 与 `anchors.ANCHOR_TEXT_MAX` 不是同一个用途：那边是私聊由头的存储上限；
# 这里是给提示词的一行素材，太长会把「生活」写成流水账。
LIFE_MAX_LINES = 4
LIFE_LINE_MAX_CHARS = 40


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


# ─── 严格提及归因（v1.29.0）─────────────────────────────
#
# `addressed_to` 只回答「开头是不是叫了某个名字」。拿它当「在叫我吗」用，会误判三类：
#   1. 同名群名片——群里有人的名片和 bot 一样，那句其实在叫那个人；
#   2. 第三人称谈论——「小助手刚才说…」「她刚才说…」是在**说她**，不是在叫她；
#   3. 句中随口提到——「我觉得小助手说得对」是评论，不是点名。
# 误判的代价很具体：她会插一句本不该接的话，群里观感就是「乱插嘴」。
#
# 所以归因的口径反过来——**只有 At 才算点名**。文本证据只用来解释「为什么不算」：
# 出现名字只算 `name_only`，第三人称只算 `third_person`，两者都不构成 addressed。
# 宁可少接一句，也别乱插一句；这与 `flow_should_consider` 的保守档是同一个取向。

# reason 的取值。`mention` 刻意与 `throttle.BYPASS_REASONS` 里的同名项一致：
# 群硬收口只放行真正的点名，归因结果可以直接当「理由」喂给 `closeout_allows`，
# 不必在调用方再翻译一层——两处各写一份映射，迟早会漂。
ATTR_MENTION = "mention"
ATTR_NAME_ONLY = "name_only"
ATTR_THIRD_PERSON = "third_person"
ATTR_NONE = "none"
ATTR_EMPTY = "empty"

# 「@全体」的几种写法。平台给的值不统一（有的给 "all"，有的给 "@all"/"atall"），
# 先归成同一个 `AT_ALL` 再比，免得换个适配器就漏判——漏判 @全体等于她该在的时候不在。
AT_ALL = "all"
_AT_ALL_ALIASES = frozenset({"all", "@all", "atall", "at_all", "everyone"})

# 转述动词：名字/代词后面跟这些，说明是在**说**某人，不是在**叫**某人。
_SPEECH_VERBS = "说|讲|提|问|聊|吐槽|觉得|认为|回复|答|喊|嚷"
# 名字与动词之间允许出现的时间副词（「小助手刚才说」）。
_TIME_WORDS = (
    "刚才|刚刚|刚|之前|上次|上回|昨天|前天|今天|早上|中午|晚上|好像|又|也|还|已经|就|才"
)
# 分句边界：判断「名字后面这半句里有没有『你』」时用得到。
_CLAUSE_END = "，,。.！!？?；;、~～…\n"

# 第三人称代词 + 转述动词。前面的 `(?:^|(?<=[...]))` 是「句首或分句边界」，
# 挡掉「其他」「吉他」这类词里夹着的「他」——那是在说别的，不是在转述她。
_THIRD_PERSON_RE = re.compile(
    rf"(?:^|(?<=[\s{_CLAUSE_END}]))(?:他|她|它|TA|ta|Ta|tA)(?:们)?\s*"
    rf"(?:{_TIME_WORDS})?\s*(?:{_SPEECH_VERBS})"
)


def _iter_values(value: Any) -> List[Any]:
    """把契约里的列表字段读成 `list`：None/单个字符串/读不出迭代的都兜住。

    `at_ids` / `bot_names` / `known_names` 都是跨适配器、跨插件来的值：旧版 Core
    可能没有新字段，调用方也可能手滑传成字符串。归因在群消息链路上，抛一次就是
    整条消息丢掉，所以这里只认「能列出来的」，其余当空。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return list(value)
    except TypeError:
        return []


def _clean_ids(value: Any) -> List[str]:
    """At 目标 id 归一化成去重后的字符串列表（保留原顺序）。"""
    out: List[str] = []
    for item in _iter_values(value):
        if item is None or isinstance(item, (dict, list, tuple, set)):
            continue
        try:
            text = str(item).strip()
        except Exception:
            continue
        if text and text not in out:
            out.append(text)
    return out


def _clean_names(value: Any) -> List[str]:
    """名字归一化成去重后的字符串列表，**单字名字一律不要**。

    单字名字（「小」「他」）做子串匹配等于匹配一切，比不判还糟。`addressed_to`
    也是 `len >= 2` 这条口径，这里保持一致。按长度倒序，让「小助手」先于「助手」
    命中，targets 里给出的是最具体的那个。
    """
    out: List[str] = []
    for item in _iter_values(value):
        if item is None or isinstance(item, (dict, list, tuple, set)):
            continue
        try:
            text = str(item).strip()
        except Exception:
            continue
        if len(text) < 2 or text in out:
            continue
        out.append(text)
    out.sort(key=len, reverse=True)
    return out


def _clause_of(text: str) -> str:
    """从当前位置到下一个分句边界之前的那一段。"""
    for index, char in enumerate(text):
        if char in _CLAUSE_END:
            return text[:index]
    return text


def _lead_trim(text: str) -> str:
    """去掉名字后面紧跟的标点/空白（「小助手，你觉得呢」→「你觉得呢」）。"""
    return text.lstrip(_CLAUSE_END + " \t")


def _at_hits(ids: List[str], bot_id: Any) -> List[str]:
    """`at_ids` 里命中 bot 的那几个，归一化成 `"all"` 或 bot_id 字符串。"""
    try:
        me = str(bot_id or "").strip()
    except Exception:
        me = ""
    hits: List[str] = []
    for raw in ids:
        target = AT_ALL if raw.lower() in _AT_ALL_ALIASES else raw
        if target == AT_ALL or (me and target == me):
            if target not in hits:
                hits.append(target)
    return hits


def _calls_by_name(body: str, names: List[str]) -> str:
    """正文里有没有「<bot 名>你…」这种当面叫，有就返回那个名字。

    这是「名字 + 你/您 在同一分句」的判据，比「名字后面紧跟你」宽一点：
    「小助手，你倒是说说」这种中间隔了逗号的倒装也认。
    """
    for name in names:
        index = body.find(name)
        while index >= 0:
            tail = _lead_trim(body[index + len(name) :])
            clause = _clause_of(tail)
            if "你" in clause or "您" in clause:
                return name
            index = body.find(name, index + 1)
    return ""


def _says_about(body: str, names: List[str]) -> str:
    """正文里有没有「<名字>（刚才）说/讲…」这种转述，有就返回那个名字。

    转述与叫人的分界线是**同一分句里有没有「你/您」**：
      「小助手刚才说的那个」   → 转述（说她）；
      「小助手，说说你的看法」 → 叫她（分句里有「你」）。
    所以同一句里既出现名字又出现「说」，不能只看词序，得看有没有第二人称。
    """
    for name in names:
        index = body.find(name)
        while index >= 0:
            tail = _lead_trim(body[index + len(name) :])
            clause = _clause_of(tail)
            if "你" not in clause and "您" not in clause:
                if re.match(rf"(?:{_TIME_WORDS})?\s*(?:{_SPEECH_VERBS})", tail):
                    return name
            index = body.find(name, index + 1)
    return ""


def attribution(
    text: str,
    *,
    at_ids: Any = (),
    bot_id: Any = "",
    bot_names: Any = (),
    known_names: Any = (),
) -> Dict[str, Any]:
    """严格归因：这条群消息到底是不是在叫 bot。纯函数，不读状态、不碰 IO。

    参数：
      text         消息正文（At 组件已由适配器从正文里摘掉）
      at_ids       本轮消息里 At 组件的目标 id 列表；`"all"` 表示 @全体
      bot_id       bot 自己的 id（字符串或数字都行）
      bot_names    bot 的名字/别名（「小助手」这类）
      known_names  群里其他人的名字——用来识别「小明刚才说…」这种转述

    返回 `{"addressed": bool, "reason": 码, "targets": tuple}`：
      mention      at_ids 里有 bot_id 或 @全体 —— **唯一算点名的**
      name_only    正文里出现 bot 的名字（含「小助手你…」这种当面叫，但不是 At）
      third_person 正文在第三人称谈论（「他/她/它/XX 刚才说…」）
      none         以上都不是
      empty        没有正文，也没有 At

    判定顺序就是证据强弱：At > 当面叫名字 > 第三人称转述 > 只是提到名字 > 什么都没有。
    第三人称排在「当面叫名字」之后、单纯提到之前：同样一句「小助手刚才说」，
    有「你」是在叫她，没「你」是在说她。

    纯 @（正文为空）仍然算 mention：那是明着叫她，只是没打字；`empty` 只留给
    「没 @、也没正文」这种真的什么信息都没有的消息。这条与「空文本 empty」的字面
    顺序不同，是**有意**的——把 @ 判成 empty 等于让她无视一次点名。
    """
    ids = _clean_ids(at_ids)
    names = _clean_names(bot_names)
    others = _clean_names(known_names)
    body = str(text or "").strip()

    # ① At：唯一能判成点名的证据。@全体也算——她也在「全体」里。
    hits = _at_hits(ids, bot_id)
    if hits:
        return {"addressed": True, "reason": ATTR_MENTION, "targets": tuple(hits)}

    if not body:
        return {"addressed": False, "reason": ATTR_EMPTY, "targets": ()}

    # ② 当面叫名字（「小助手你…」）：比 At 弱，但至少不是「在说她」。
    called = _calls_by_name(body, names)
    if called:
        return {"addressed": False, "reason": ATTR_NAME_ONLY, "targets": (called,)}

    # ③ 第三人称转述：bot 的名字或**别人的**名字后面跟着「说/讲/提…」。
    # 别人的名字也要认——「小明刚才说…」原来会被 `addressed_to` 当成在叫小明，
    # 于是她以为那句话是冲着自己来的。
    said = _says_about(body, names + others)
    if said:
        return {"addressed": False, "reason": ATTR_THIRD_PERSON, "targets": (said,)}
    pronoun = _THIRD_PERSON_RE.search(body)
    if pronoun:
        return {
            "addressed": False,
            "reason": ATTR_THIRD_PERSON,
            "targets": (pronoun.group(0).strip(),),
        }

    # ④ 只是提到名字：评论（「我觉得小助手说得对」）也落在这一类。
    for name in names:
        if name in body:
            return {"addressed": False, "reason": ATTR_NAME_ONLY, "targets": (name,)}

    return {"addressed": False, "reason": ATTR_NONE, "targets": ()}


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


def flow_context_note(recent_speakers: List[str], addressed_name: str) -> str:
    """把「这个群现在在干什么」写成一句给模型看的语境（S3，v1.28.0）。

    以前这两件事是**硬拦**：两人对聊、或者这句话在叫别人，心流一律不插。用户明确
    说「不是必须，但是能不能插嘴看模型」——插件只把语境给出来，插不插由模型判断；
    频率/窗口等护栏仍在外层。返回空串 = 没什么要提醒的（正常群聊）。
    """
    name = str(addressed_name or "").strip()
    recent = [
        str(s or "").strip() for s in (recent_speakers or []) if str(s or "").strip()
    ]
    duet = ""
    if two_person_exchange(recent):
        names: List[str] = []
        for item in recent:
            if item not in names:
                names.append(item)
        if len(names) == 2:
            duet = f"群里现在是 {names[0]} 和 {names[1]} 在来回聊"
    if name and duet:
        return f"{duet}；这一句是在叫「{name}」，不是跟你说"
    if duet:
        return duet
    if name:
        return f"群里这句话是在叫「{name}」，不是跟你说"
    return ""


# ─── 群活跃度分数（v1.28.16）─────────────────────────
#
# 从 koishi-plugin-chatluna-proactive-trigger 的 `ActivityScorer` 移植
# （refs/koishi-check/ptr/package/lib/index.js 的 activity.ts 段）。那边的用途是
# 「够活跃就主动接话」，**这里反过来只用它做抑制**：分数低 = 群里刚安静下来，
# 不硬接。分数高不构成任何新的开口理由——开口的资格仍由下面的硬闸单独决定。
#
# 窗口与阈值照抄原实现：
#   recent 90s / instant 20s / burst 30s 三个窗口，都折算成「条/分钟」；
#   持续阈值 10、瞬时阈值 9（logistic 的陡度分别是 /3、/2）、突发阈值 12；
#   权重 0.65 / 0.35；突发超过 12 条/分额外加至多 0.25；
#   新鲜度按 60 秒半衰，乘 0.55 + 0.45 * fresh；最后 clamp 到 [0, 1]。

FLOW_ACTIVITY_MIN = 0.15

# 三个观察窗（秒）与各自的阈值（条/分钟）。名字保持与原实现一致，便于对照。
_ACTIVITY_WINDOW_RECENT = 90.0
_ACTIVITY_WINDOW_INSTANT = 20.0
_ACTIVITY_WINDOW_BURST = 30.0
_ACTIVITY_THRESHOLD_SUSTAINED = 10.0
_ACTIVITY_THRESHOLD_INSTANT = 9.0
_ACTIVITY_THRESHOLD_BURST = 12.0
_ACTIVITY_WEIGHT_SUSTAINED = 0.65
_ACTIVITY_WEIGHT_INSTANT = 0.35
_ACTIVITY_BURST_BONUS = 0.25
_ACTIVITY_FRESH_HALF_LIFE = 60.0


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _logistic(x: float) -> float:
    """原实现的 logistic。夹一下 x：`math.exp(±大数)` 会 OverflowError。"""
    x = max(-60.0, min(60.0, float(x)))
    return 1.0 / (1.0 + math.exp(-x))


def _activity_rate(timestamps: List[float], now: float, window: float) -> float:
    """指定窗口内的消息速率（条/分钟）。窗口外的、读不出数的一律不计。"""
    cutoff = now - window
    count = 0
    for ts in timestamps:
        if ts >= cutoff:
            count += 1
    return count / window * 60.0


def activity_score(timestamps: Any, now: float) -> float:
    """群里「现在有多热闹」的分数，0~1（纯函数，不改任何状态）。

    `timestamps` 是真人发言的时刻列表（秒，epoch）。空、全是坏值、或读不出 `now`
    时返回 0.0——「没有可判的活跃度」按最低分处理，只会导致不接话，不会导致乱接。

    数值口径逐条对应原实现的 `calculateScore`：
      1. 三个窗口各算一个速率；
      2. 持续/瞬时速率过 logistic 平滑后按 0.65 / 0.35 加权；
      3. 突发速率 >12 条/分时补 `clamp((burst-12)/4, 0, 1) * 0.25`；
      4. 按「最后一条消息距现在多久」算新鲜度（60 秒半衰），
         乘 `0.55 + 0.45 * fresh`；
      5. clamp 到 [0, 1]。

    与 JS 版唯一的有意差异（都只会让分数更保守，不会让它变高）：未来时间戳
    按「刚刚」算（Python 的 `math.exp` 负数过大时会溢出，JS 的 `Math.exp` 只给
    Infinity）；坏时间戳直接跳过而不是污染分数。
    """
    try:
        now = float(now)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(now):
        return 0.0
    valid: List[float] = []
    try:
        items = list(timestamps or [])
    except TypeError:
        items = []
    for raw in items:
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(ts):
            valid.append(ts)
    if not valid:
        return 0.0
    recent_rate = _activity_rate(valid, now, _ACTIVITY_WINDOW_RECENT)
    instant_rate = _activity_rate(valid, now, _ACTIVITY_WINDOW_INSTANT)
    burst_rate = _activity_rate(valid, now, _ACTIVITY_WINDOW_BURST)
    sustained = _logistic(
        (recent_rate - _ACTIVITY_THRESHOLD_SUSTAINED) / 3.0
    )
    instant = _logistic(
        (instant_rate - _ACTIVITY_THRESHOLD_INSTANT) / 2.0
    )
    score = (
        sustained * _ACTIVITY_WEIGHT_SUSTAINED
        + instant * _ACTIVITY_WEIGHT_INSTANT
    )
    if burst_rate > _ACTIVITY_THRESHOLD_BURST:
        score += _clamp01((burst_rate - _ACTIVITY_THRESHOLD_BURST) / 4.0) * (
            _ACTIVITY_BURST_BONUS
        )
    # 新鲜度：最后一条消息越久远，分数越靠 0.55 那边收（原实现同一公式）。
    elapsed = max(0.0, now - max(valid))
    freshness = math.exp(-elapsed / _ACTIVITY_FRESH_HALF_LIFE * math.log(2.0))
    score *= 0.55 + 0.45 * freshness
    return _clamp01(score)


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
    interject_enabled: bool = True,
    context_note: str = "",
    interject_gap_seconds: float = 0.0,
    attempt_gap_seconds: float = 0.0,
    activity: float = 1.0,
    activity_min: float = 0.0,
) -> Tuple[bool, str]:
    """心流闸门：现在这个群该不该考虑主动接话。返回 (是否可接, 不可接的原因)。

    比另起话题的闸门更宽（追热聊本来就发生在刚聊完），但有几道防刷屏/防尬聊的硬闸：
    - 安静时段——私聊侧五道闸都查了这项，只有群心流漏了，而它又是事件驱动的
      （群消息一到就走，不经过心跳），于是深夜群里有人说话她照样插一句；
    - 她正在睡觉（v2.1.8 起不再硬拦）——用户原话「群聊心流就不该有在睡觉必须不能
      接话的，谁说不能迷迷糊糊的」。睡着时只跳过「安静时段」这一道，其余硬闸照旧；
      但要求群比清醒时更热（`activity_min`，engine 传 0.6），而且 engine 侧另加
      「同一个群 30 分钟最多一条 drowsy」的节流——迷糊着接一句是可爱的，连着接
      三条就是刷屏；
    - 关注窗口没开（bot 最近没在这个群说过话）——保守档的核心；
    - 自己刚在主链路说完话后的冷却期（`flow_hold_until`）——不然「被 @ 回一句」后马上又接，
      观感是同一个她在连续刷屏；
    - 发送隔离中（被踢/会话失效）；
    - 本窗口接够了 / 每小时插话到顶 / 距上一句插话太近；
    - 连续插话没人接，已经到退出阈值；
    - **群活跃度**（v1.28.16，`activity`）：只做抑制——低于 `FLOW_ACTIVITY_MIN`
      说明群里刚安静下来，别硬接。默认 1.0 时与旧行为逐字等价；
      睡着时门槛由 `activity_min` 抬高（engine 传 0.6），见上一条；
      **分数高不构成任何新的开口理由**；
    - **语境**（v1.28.0）：两人对聊、或这句话在叫某个群友时，**不再硬拦**——用户
      明确说「能不能插嘴看模型」。这里只保留两道护栏：`interject_enabled=False`
      退回旧语义（这种场合一律不插）；刚插过话时别人还在对聊就先缓一缓，别在她
      上一句刚落、语境已过时的时候又扎进去。
    """
    if is_quiet and not asleep:
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
    # **尝试节流**（v1.28.1）。
    #
    # 上面的 flow_last_reply_at 只在**成功发送**时更新，于是「模型选择不接」这条路
    # 完全不碰任何配额：活跃群里每来一条群消息就重新走一遍闸门 + 一次 LLM。
    # 线上实测某个群 1 分钟内 14 次「模型选择不接或生成为空」，每次都真打了模型——
    # 既烧额度又刷屏。
    #
    # 用独立的 flow_last_attempt_at：只节流「多久试一次」，不改上面那几个配额的语义
    # （flow_replies / flow_ignored / flow_hour_count 仍然只数真正发出去的）。
    try_gap = now - _f(group.get("flow_last_attempt_at", 0))
    if try_gap < max(0.0, attempt_gap_seconds):
        return False, f"心流刚试过（{int(try_gap)} 秒前），先歇会儿"
    # **群活跃度抑制**（v1.28.16）。只做抑制，**绝不**因为分数高就放行：
    # 分数低于门槛 = 群里刚安静下来（最后一条真人消息很旧、或者刚才根本没人说话），
    # 这时候接一句是「硬接」。放在既有硬闸之后、语境判断之前：硬闸管「她还能不能
    # 开口」，这里管「这会儿群还热不热」。默认 1.0 时与旧行为逐字等价。
    try:
        act = float(activity)
    except (TypeError, ValueError):
        act = 1.0
    if not math.isfinite(act):
        # nan/inf 是「算不出来」，按不抑制处理（与字段没给同一个口径）；
        # 注意 nan < 0.15 本身就恒假，这里显式写出来只是别让读代码的人误会。
        act = 1.0
    # v2.1.8：睡着时门槛更高（engine 传 0.6）。不是「睡着更该说话」，而是反过来的：
    # 迷糊时判断力差，群里不够热闹就别硬接；热闹到值得插一句时，迷糊着说一句反而自然。
    try:
        _extra_min = float(activity_min or 0.0)
    except (TypeError, ValueError):
        _extra_min = 0.0
    _act_min = max(FLOW_ACTIVITY_MIN, _extra_min)
    if act < _act_min:
        if asleep and _act_min > FLOW_ACTIVITY_MIN:
            return False, "群里不够热闹，迷糊着就不硬接了"
        return False, "群里刚安静下来，不硬接"
    # 语境：她要看这个群**现在在干什么**，而不只是「这一条消息过不过关」。
    # 放在配额检查之后：配额是「她还能不能开口」，语境是「这会儿该不该开口」，
    # 前者更该出现在日志里（调参看的是它）。
    note = str(context_note or "").strip()
    if note:
        if not interject_enabled:
            return False, "别人正在对聊/在叫别人，按配置不插话"
        try:
            need_gap = float(interject_gap_seconds or 0.0)
        except (TypeError, ValueError):
            need_gap = 0.0
        if need_gap > 0 and gap < need_gap:
            return False, "刚插过话，别人对聊时先缓一缓"
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
    # v2.2.0：`*_try_at` 也要算进来——失败路径（模型决定不说 / 三次仍是问句 /
    # 跨群撞车 / 验收打回）原来一个时间戳都不写，于是冷群一旦 due 就永久 due，
    # 每轮心跳都重新生成一次，`due[0]` 还永久占着不让别的群轮上。冷却下限 10 分钟，
    # 保证「试过之后」下一轮就轮到别的群。
    last_proactive = max(
        _f(group.get("icebreak_at", 0)),
        _f(group.get("topic_at", 0)),
        _f(group.get("icebreak_try_at", 0)),
        _f(group.get("topic_try_at", 0)),
    )
    cooldown = max(600.0, idle_hours * 1800.0)
    if now - max(last_proactive, _f(group.get("last_bot_spoke", 0))) < cooldown:
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


def _as_text_list(value: Any) -> List[str]:
    """把契约里的列表字段读成 `[str]`：非列表 / 脏元素 / 空串一律跳过。

    `day.done` / `day.dreams` / `ongoing` 都是跨插件来的值：旧版 Core 没有新字段，
    手改坏时还可能是字符串或 `[None]`。破冰这条链抛一次就是整轮心跳丢一个角色，
    所以这里只认列表，其余当「没有这件事」。
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value:
        if item is None or isinstance(item, (dict, list, tuple, set)):
            continue
        try:
            text = str(item).strip()
        except Exception:
            continue
        if text:
            out.append(text)
    return out


def _life_line(text: Any) -> str:
    """一行生活素材：压成单行、截到 `LIFE_LINE_MAX_CHARS`。"""
    return " ".join(str(text or "").split())[:LIFE_LINE_MAX_CHARS]


def _want_line(text: Any) -> str:
    """把「今天想做的事」写成一句顺口的话，别拼出「今天想去去那家书店」。"""
    body = str(text or "").strip()
    if not body:
        return ""
    if body.startswith(("想", "要", "去", "来", "拐去", "拐到", "过去")):
        return f"今天{body}"
    return f"今天想去{body}"


def life_lines(body: Dict[str, Any], now: Optional[float] = None) -> List[str]:
    """S2：她自己的生活里**可以分享的具体小事**（群破冰/起话头的素材）。

    用户原话是「他就不会分享事物吗」——天气不是生活，刚做完 / 正在做 / 今天想去 /
    还没做完 / 昨晚梦到才是。顺序即优先级（先挑最像「我这边有件事想说」的），
    去重、每行 ≤ `LIFE_LINE_MAX_CHARS` 字、最多 `LIFE_MAX_LINES` 条。

    `body` 是 Core 契约（或 `core_bridge` 的 self view）：旧版 Core 没有
    `day.dreams` / `day.wants`，缺字段与脏数据一律当「没有这件事」，绝不抛。

    `now` 给「刚醒」那一条判新鲜度用（`anchors._wake_cue`）：不传就只问形状，
    由引擎侧的 TTL 兜着。
    """
    body = body if isinstance(body, dict) else {}
    day = body.get("day") if isinstance(body.get("day"), dict) else {}
    out: List[str] = []
    seen: set = set()

    def add(line: Any) -> None:
        line = _life_line(line)
        if not line or line in seen:
            return
        seen.add(line)
        out.append(line)

    # ① 刚做完的：`day.done` 本身已经带「下午/傍晚」这类时间词，直接说就是一件具体
    # 的事。「才醒」不算做了件事（那是状态），vague doing 也是空话——两条都滤掉。
    for text in _as_text_list(day.get("done"))[:2]:
        if "才醒" in text or is_vague_doing(text):
            continue
        add(text)

    # ①-bis 她刚醒（v1.28.6，Core 2.28 起契约的 `wake`）。群里也会有人搭一句
    # 「你起好晚」——跟私聊由头是同一批事实（同一个 `anchors._wake_cue`），
    # 不能一边读一边不读。排在 done 之后、doing 之前：醒来头几分钟最想说话。
    wake = _wake_line(body, now)
    if wake:
        add(wake)

    # ② 正在做的。`day.doing` 是「此刻正在做」，不是「刚做完」——群素材只按现在说。
    # 与私聊由头同一条口径：doing 本身以「在」开头时不再补「正在」（避免「正在在…」）。
    doing = str(day.get("doing") or "").strip()
    if doing and not is_vague_doing(doing):
        add(f"正在{doing}" if not doing.startswith("在") else doing)

    # ②-bis 接下来要做的（v1.28.6）。`day.next` 是**列表**（Core 给最多两条），
    # 原来只用第一条，白丢一条。排在 doing 之后、wants 之前：时间线是
    # 刚做完 → 刚醒 → 正在做 → 接下来 → 今天想去。
    for text in _as_text_list(day.get("next"))[:2]:
        if "才醒" in text or is_vague_doing(text):
            continue
        add(f"接下来{text[:LIFE_LINE_MAX_CHARS - 3]}")

    # ③ 今天想做的（Core 2.28 才有；没有这个字段就是空的）。
    for text in _as_text_list(day.get("wants"))[:2]:
        add(_want_line(text))

    # ④ 还没做完的（Core 跨天跟着的那件事）。
    for task in _as_text_list(body.get("ongoing"))[:1]:
        if not is_vague_doing(task):
            add(f"还在{task}")

    # ⑤ 昨晚/白天补觉时梦到的（Core 2.28 才有）。梦是最像「分享」的一类：
    # 具体、有画面、不需要对方接。
    for dream in _as_text_list(day.get("dreams"))[:1]:
        add(f"梦到{dream[:16]}")

    return out[:LIFE_MAX_LINES]


def _wake_line(body: Dict[str, Any], now: Optional[float] = None) -> str:
    """她刚醒：一行群素材。没醒/不新鲜就空串。

    判据与私聊由头**同一个函数**（`anchors._wake_cue`）：两处各写一份的话，
    㑹私聊说「刚睡醒」而群里说三天前那次醒。这里只把它转成群素材的一行。
    """
    try:
        cue = _wake_cue(body, now)
    except Exception:
        return ""
    return cue or ""


def topic_materials(
    *,
    life: Optional[List[str]] = None,
    holiday: str = "",
    weather: str = "",
) -> str:
    """K1/S2：破冰/起话头可用的现成素材。**她自己的生活优先**，节日其次，天气最后。

    用户原话：「为啥只在破冰是总发天气的，他就不会分享事物吗？」——所以生活素材
    排在最前面。天气只是备选：它是唯一「永远有、永远具体」的素材，摆在前面就会
    变成唯一话头（调用方还得先过「变了 + 6 小时」的闸，见 engine）。
    """
    lines: List[str] = []
    seen: set = set()
    for item in list(life or [])[:LIFE_MAX_LINES]:
        text = _life_line(item)
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(f"- {text}")
    holiday = str(holiday or "").strip()
    if holiday:
        lines.append(f"- 今天是{holiday}")
    weather = str(weather or "").strip()
    if weather:
        lines.append(f"- {outside_lead(weather)}")
    return "\n".join(lines)


def local_materials(now: float, *, count: int = 2) -> List[str]:
    """没有 Core 时的兜底素材：拿「现在几点」当材料。

    原来没有 Core 时七个素材源全灭，节日和天气也来自 Core，
    `topic_materials` 拿到三个空参数就返回空串——起话头一句话都起不来。
    这里只用不会错的事实（此刻是几点）编一两条她自己的小场景，
    保证插件脱离 Core 也能开口。
    """
    try:
        hour = int(time.localtime(float(now)).tm_hour)
    except (TypeError, ValueError, OSError):
        return []
    buckets = (
        ((5, 8), ("这会儿天刚亮，屋里还有点凉", "刚醒，眼睛还没完全睁开")),
        ((8, 11), ("上午的光从窗缝里斜进来，落在地板上", "刚把杯子洗了，手还是湿的")),
        ((11, 13), ("中午了，肚子有点空", "午饭刚咽下去，人有点懒")),
        ((13, 17), ("下午这会儿最闷，坐着坐着就走神", "窗外有人走来走去，声音断断续续")),
        ((17, 19), ("天开始暗了，屋里还没开灯", "傍晚的风从窗缝里钻进来")),
        ((19, 23), ("这会儿屋里就我这儿还亮着灯", "晚上安静下来，能听见自己翻身的动静")),
    )
    lines: List[str] = []
    for (lo, hi), texts in buckets:
        if lo <= hour < hi:
            lines = list(texts)
            break
    if not lines:
        lines = ["夜深了，还没睡", "这时候连楼下都没声音了"]
    try:
        offset = int(float(now) // 86400) % len(lines)
    except (TypeError, ValueError):
        offset = 0
    rotated = lines[offset:] + lines[:offset]
    try:
        want = max(1, int(count or 1))
    except (TypeError, ValueError):
        want = 1
    return rotated[:want]


def icebreak_topics(holiday: str, weather: str) -> str:
    """K1：破冰/起话头可用的现成素材（节日 / 天气）。保留为兼容包装。

    v1.28.0 起真正的素材组装在 `topic_materials`（多了「她自己的生活」并且排在最
    前）；这个入口的调用方与测试仍在用，所以名字与「节日在前、天气在后」的旧语义
    都保留，只是转调新函数。
    """
    return topic_materials(holiday=holiday, weather=weather)



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
    # v2.2.0：与破冰同一套冷却——把「试过」（topic_try_at / icebreak_try_at）也算进
    # 来，失败之后下一轮就轮到别的群，不再每轮心跳重打一次模型。冷却下限 10 分钟。
    # 单位修正：gap_hours 是「小时」（schema：群内起话头间隔），不是半小时。
    cooldown = max(600.0, max(1.0, _f(gap_hours)) * 3600.0)
    last_proactive = max(
        _f(group.get("last_bot_spoke", 0)),
        _f(group.get("icebreak_at", 0)),
        _f(group.get("topic_at", 0)),
        _f(group.get("icebreak_try_at", 0)),
        _f(group.get("topic_try_at", 0)),
    )
    if now - last_proactive < cooldown:
        return False
    if str(group.get("topic_day", "") or "") == today:
        if _i(group.get("topic_count_day", 0)) >= max(1, daily_cap):
            return False
    return True
