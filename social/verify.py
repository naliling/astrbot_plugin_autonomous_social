"""验收层：一条主动消息在**发出去之前**要过的五道检查。

为什么要有
----------
`SEND` / `NO` 协议只保证**格式**——第一行是不是 SEND、后面有没有正文。
它完全不保证内容。v1.19 之前的 197 条真实记录里，这些直接进了用户手机：

* **动机泄漏** ~40 条：`表达赖床的慵懒和饿意`、`想找个借口跟你说话`、
  `想表达此刻的孤独和饥饿感`。这些是**关于说话**的描述，不是话。
* **结构残缺** ~10 条：`(把毯子裹得紧紧的`、`(窝在沙发里，手里抓着零食`
* **只剩动作没有正文** 9 条：`(把毛毯裹紧` —— 正文被截光了
* **复读**：某一个晚上 23 条全是「窝在沙发追剧…困了…晚安」

## 判据怎么定
**不做词表匹配。** 用这批真实数据的措辞去写正则（`^(想|要)(表达|说|分享…)`），
下一批数据换个说法它就失效——那正是这个插件被改了 80 版的原因。改成**结构判据**：

* 「这句话在描述说话这个动作」= 句子的主干是「想/要/打算/准备 + 动��」
* 「括号没配平」= 计数，与内容无关
* 「去掉括号后没内容了」= 结构，与内容无关
* 「和最近发过的太像」= 字符二元组重合度，与内容无关
* 「去掉括号与标点后不剩几个字」= 结构

判不过就是**不发**（不重生成、不退回）。宁可少发一条，不可发一条废话。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── 结构常量 ──────────────────────────────────────────────────

# 括号动作：（）与半角 ()，以及书名号式强调。
#
# **不设长度上限。** 原来写作 `{0,80}`，于是括号内超过 80 字的纯舞台指示整条免检：
# 实测括号内 80 字仍被拦，90 字就放行，一条 107 字的纯括号动作
# `verify_message` 返回 `(True, '')`——用户收到一条没有任何台词的括号。
# 这正是模块自陈要拦的「只剩动作没有正文」，而语C 人设写长括号很常见。
# 长度不是「这算不算一句话」的判据：够不够长由 `_check_thin` 与调用方的 max_len 管。
_ACTION_BLOCKS = re.compile(r"[（(【\[][^（()【\][]*[）)】\]]")
_OPENERS = "（(【["
_CLOSERS = "）)】]"

# 只剩标点与空白
_PUNCT_ONLY = re.compile(r"[\s，。！？、；：,.!?;:\-—~～…·「」『』\"'“”‘’]+")

# 「有内容的字符」：汉字、假名、拉丁字母、数字、emoji。
#
# 判「只剩符号、没有正文」**不能**靠上面那张标点表反过来做——标点表永远列不全：
# 《》〈〉〖〗、全角 ASCII 符号、以及 `# @ { } | / \ * & ^ % $ + = _` 全在漏网，
# 于是 `###`、`《》〈〉`、`〖〗〖〗`、`@@@@` 这种长度 ≥3 的纯符号正文一路通过
# `_check_structure`，再因为 `_check_thin` 只拒 `len(body) <= 2` 而直接发给用户。
# 反过来问「有没有一个字符是真正的内容」才是封闭判据：不随标点表增补而失效。
_CONTENT_CHAR = re.compile(
    r"[0-9A-Za-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\uff10-\uff19\uff21-\uff3a\uff41-\uff5a"
    r"\U0001F000-\U0001FAFF\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d]"
)

# ── 短句豁免 ─────────────────────────────────────────────────
# 这两张表解决的是同一个投诉：「发东西都没过脑子，根本不是在讲话的样子」。
#
# 旧实现只拒「去掉标点后 ≤2 个字」的正文，不看这句话是什么类型。于是
# 「早安」「晚安」「嗯」「那我先睡了」这些**本来就该短**的话全被判空泛，
# 模型为了过检只能把两字问候扩写成「早上好呀，新的一天也要元气满满哦」——
# 那才是真正的机器人腔。长短不是判据，场景才是。
#
# 反过来，197 条记录里「只剩两个字的空泛」确实存在，所以豁免只给这几类：
# 问候、随口招呼、应和、报个到、收个尾。其它类型（分享、回应、念想……）
# 照旧两字门槛，别把这条豁免当万能通行证。
SHORT_OK_TYPES = frozenset({"greeting", "casual_hello", "react_time", "check_in", "closer"})

# 问候按天去重用的字面表：这些句子一天说一次是作息，说两次才是复读。
# 不按 `_bigrams` 判——两三个字的句子二元组几乎只有它自己，
# 判据对它们没有分辨率（「早安」和「晚安」在旧判据下都只是「一条短问候」）。
_GREETING_TEXTS = frozenset({"早安", "早", "晚安", "醒了", "醒啦", "在吗", "早呀", "晚安啦"})


# ── 「关于说话」的判据 ────────────────────────────────────────
# 关键不是「开头是不是 想/要」，而是**这句话有多大比例在描述说话这个动作**。
# 语料里泄漏的写法五花八门：
#   「表达赖床的慵懒和饿意」        —— 整条
#   「赖床中，想分享这种懒洋洋的感觉并关心对方」 —— 前半句是状态，后半句才是泄漏
#   「饿了，想找主人讨点关注」      —— 前两个字才是内容
# 只匹配开头会漏掉后两种；所以改成：把「说话动作」从句抠掉，看**剩下多少**。
# 剩下的比例低于阈值就判定为「整条都在说自己要说这句」。
#
# ⚠️ 动词表只收**元动词**（描述「说话」这个动作本身的词）与少数自述词。
# 原来还混着 `找/说/聊/来/逗` 这类**日常口语动词**，它们太泛，把正常开口整个抠掉：
#   `我想找你聊聊`(残余占比 0.17)、`我来告诉你个好消息`(0.20)、
#   `我要说的是这个`(0.14)、`顺便说一句`(0.00) —— 全判成动机泄漏。
# `找` 保留，但要求它的宾语**不是对方**：`想找你聊聊` 是内容（要说给对方听），
# `想找主人讨点关注` 才是自述动机。这与 177 行 `_ADDRESS` 的口径一致。
_INTENT_CLAUSE = re.compile(
    r"(?:想|要|打算|准备|该|想要|想着|顺便|顺手|试着)\s*"
    r"(?:表达|分享|说出|说点|说句|说说|告诉|提起|提一嘴|提一|关心|问问|问一"
    r"|撒个娇|撒|调情|勾引|调侃|撩|刷存在感|找点存在感|找(?!你|您|咱|TA|ta)"
    r"|回应|回复|接话|讲|抱怨|吐槽|撒娇)"
    r"[^，。！？；、,.!?;]{0,40}"
)
# 句尾挂着自述尾巴。
# 注意**不把「顺便」单列**：它本身就是正常中文连接词（语料里有一句
# 「想确认一下人还在不在，顺便继续撩拨一下」是好消息）。要抓的是
# 「顺便…不指望回」这种搭配，以及「想找个借口」这种自述动机。
_INTENT_TAIL = re.compile(
    r"(?:顺便[^，。！？]{0,12}不指望回|不指望(?:你|TA)?回|不用回我|不需要回复|不用回复"
    r"|不解释|不细说|想找(?:个|点)借口|找个借口|想找点乐子|说给自己听|写给自己)"
)
# 整条以「抱怨/吐槽/撒娇/调侃」这类**描述性**动词开头，且没有第二人称对象
_NO_TARGET = re.compile(r"^\s*(?:表达|分享|抱怨|吐槽|撒娇|调侃|陈述|描述|说明|传达)")
# 泄漏从句占满整条时，剩下的内容比例上限
_INTENT_RESIDUAL = 0.4

# 段首的「说话动作」从句 + 段内有没有指向对方。
# 「想问问你明天有空吗」动词命中了（问问），但**对象是对方**，那是内容；
# 「想分享下此时此刻偷懒的状态」是描述自己的动作，没有对象——那才是元叙述。
_ADDRESS = re.compile(r"你|您|TA|ta|他|她|咱")
_INTENT_HEAD_ONLY = re.compile(
    r"^\s*[（(\[]*\s*(?:想|要|打算|准备|该|想要|想着)\s*"
    r"(?:表达|分享|说出|说点|说一|说说|告诉|提起|提一嘴|提一|关心|问问|问一"
    r"|聊|聊聊|撒个娇|撒|调情|勾引|逗|逗弄|调侃|撩|刷存在感|找点存在感|找|回应|回复"
    r"|接话|说|讲|抱怨|吐槽|撒娇)"
)

# 独立的元评段：真实记录里出现过 `…晚安。｜表达想念，结束一天的对话` 这种——
# 后面半截是对自己这条消息的注解，不该发出去。
# 只收**无歧义**的元评动词。抱怨/吐槽/撒娇/分享/结束 这类在正常聊天里也会出现
# （「肚子饿了想撒娇，想让对方弄点吃的」是好消息），放进段判据会误杀。
_META_SEGMENT = re.compile(
    r"(?:^|[｜|‖⏎\n])[^｜|‖\n]{0,6}?"
    r"(?:表达|陈述|描述|说明|传达|拉近距离|制造氛围)"
    r"[^｜|‖\n]{2,20}"
)


# ── 工具 ──────────────────────────────────────────────────────

def strip_actions(text: str) -> str:
    """把括号动作摘掉，只留下「话」的部分。"""
    return _ACTION_BLOCKS.sub("", str(text or "")).strip()


# 「现在就回我」这类：要求对方**立刻**给出回应。
# 「回我」不能单独当成要求——「你还好吗，回我一句就行」是给对方台阶，
# 「我想起你昨天回我的那句话」更是叙述。缺了「快/必须」这类急迫词就不算催。
_DEMAND_NOW = re.compile(
    r"(快|赶紧|立刻|马上|现在就|快点)[^。！？\n]{0,6}回"
    r"|(必须|一定|得)[^。！？\n]{0,4}回"
    r"|(催|逼)[^。！？\n]{0,4}回"
)
# 「你一直不回我」这类：把「对方没回」当成一件对方欠的事。
_SILENCE_CHARGE = re.compile(
    r"(一直|整天|一天|一晚上|半天|好久|从来|老是|多少次)[^。！？\n]{0,8}(没|不|未)[^。！？\n]{0,2}回"
    r"|(你|都)[^。！？\n]{0,8}(不理我|不理|不回我|不回|没回我|没回|没理我|没理)"
)


def _s(x: Any) -> str:
    try:
        return str(x or "").strip()
    except Exception:
        return ""


def paren_balanced(text: str) -> bool:
    """括号是否配平。

    这条**不是**清洗链的锅（清洗链里没有任何一处会动 `）`）——真凶是
    `_truncate_at` 的切点落在未闭合括号内部。验收层在这里挡住，同时把
    清洗前的原文记下来，下次就能分辨是模型漏写还是截断。
    """
    s = str(text or "")
    depth = 0
    for ch in s:
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a | b))


# ── 五道检查 ──────────────────────────────────────────────────

def _check_intent_leak(text: str) -> Optional[str]:
    """动机泄漏：这条消息大部分在描述「说话」这个动作，而不是在说话。

    **只看最终发出去的那段文本**，不看模型的原始输出。原始输出里的 `>`／`#` 行
    本来就是「要说的事」，把它一起判进去等于让模型自己给自己判失败。
    """
    # ⚠️ 必须在**保留标点**的文本上匹配：_INTENT_CLAUSE 靠标点做边界
    # （「想撒娇，想让对方弄点吃的」是两件事）。先去标点再匹配，从句会一路
    # 吞到句尾，把正常话判成泄漏。占比计算时再去标点。
    raw = strip_actions(text).strip()
    if not raw:
        return None
    body = _PUNCT_ONLY.sub("", raw).strip()
    if not body:
        return None
    if _INTENT_TAIL.search(raw):
        return "动机泄漏（挂着「顺便/不指望回」这类自述）"
    if _NO_TARGET.match(body) and not re.search(r"你|您|TA|他|她", body):
        return "动机泄漏（整条是「表达…/抱怨…」这种描述）"
    # 「整条以说自己要说这句开头」——哪怕后面还有正文，开头那行也是元叙述。
    # 真实记录里有一句 `想分享下此时此刻偷懒的状态，顺便勾引一下用户A。⏎(…动作…)⏎阳光晃得我心烦…`
    # 后半句是真话，但用户看到的第一行是「我想分享…顺便勾引你」，那不是话。
    head = _INTENT_HEAD_ONLY.match(raw)
    if head and not _ADDRESS.search(raw[:head.end() + 12]):
        return "动机泄漏（开头是在说自己要说这句）"
    # 独立成段的元评（`…晚安。｜表达想念，结束一天的对话`）
    seg = _META_SEGMENT.search(raw)
    if seg and not _ADDRESS.search(seg.group(0)):
        return "动机泄漏（正文后面跟着一段对自己的注解）"
    # 占比判据：把「说话动作」从句抠掉，剩不下多少就说明整条都在说自己要说这句
    residual_raw = _INTENT_CLAUSE.sub("", raw)
    if residual_raw != raw:
        residual = _PUNCT_ONLY.sub("", residual_raw).strip()
        ratio = len(residual) / float(len(body) or 1)
        if ratio < _INTENT_RESIDUAL:
            return "动机泄漏（整条是在描述「我要说这句」，不是话本身）"
    return None


def _check_structure(text: str) -> Optional[str]:
    if not paren_balanced(text):
        return "括号不配平（多半是超长被截断切在了括号里）"
    body = _PUNCT_ONLY.sub("", strip_actions(text)).strip()
    if not body:
        return "只剩括号动作，没有正文"
    if not _CONTENT_CHAR.search(body):
        return f"只有符号，没有正文（{body[:8]!r}）"
    return None


def _check_thin(
    text: str,
    called: str = "",
    allow_short: bool = False,
    msg_type: str = "",
) -> Optional[str]:
    """空泛：去掉括号与标点之后几乎不剩字。

    `allow_short` 给群心流开：「嗯」「哈哈」这类短应和是群聊常态，不该一律拦下。
    `msg_type` 给 `SHORT_OK_TYPES` 那几类开：问候/应和/报到/收尾本来就该短，
    对它们只拦「一个字都没有」，不判空泛（理由见常量处的注释）。
    """
    body = _PUNCT_ONLY.sub("", strip_actions(text)).strip()
    if not body:
        return None                       # 已由 _check_structure 拦
    if msg_type in SHORT_OK_TYPES:
        # 只有「正文彻底空掉」才算问题；而那种情况 `_check_structure`
        # 在上面已经拦掉了，这里不必重复判。
        return None
    if len(body) <= 2 and not allow_short:
        if called and called in body:
            return None
        return f"空泛（正文只有 {body!r}）"
    return None


def _greeting_repeat_exempt(text: str, recent: Sequence[str], msg_type: str) -> bool:
    """这一条是不是「今天第一次说」的问候。

    问候的二元组没有分辨率：「早安」「晚安」「在吗」的骨架各自就那两个字，
    窗口里只要有过任意一条短问候，`_check_repeat` 就可能在 0.72 上误判。
    实测模型一天里发两三次问候是正常作息，不是复读——把「晚安」判成
    复读「早安」，它只能换个更绕的说法，越换越不像人。
    所以问候类按「同一句当天只发一次」处理：窗口里没有同一句才豁免，
    有同一句说明确实在重复，照常交给下面的重合度判据。

    窗口数据用现成的 `recent`：它没有时间戳，只能把「在窗口里」当作
    「20 小时内」；调用方给的本来就是最近若干条消息，够用且不新增状态。
    """
    if msg_type not in SHORT_OK_TYPES:
        return False
    body = _PUNCT_ONLY.sub("", strip_actions(text)).strip()
    if body not in _GREETING_TEXTS:
        return False
    for old in recent or []:
        if _PUNCT_ONLY.sub("", strip_actions(_s(old))).strip() == body:
            return False
    return True


def _check_repeat(
    text: str,
    recent: Sequence[str],
    threshold: float = 0.72,
    msg_type: str = "",
) -> Optional[str]:
    """与最近发过的太像。

    判据是**字符二元组重合度**，不是逐字相同——复读的措辞每次都不同
    （23 条沙发追剧，每条都不一样），但骨架高度重合。
    """
    cur = _bigrams(strip_actions(text))
    if not cur:
        return None
    if _greeting_repeat_exempt(text, recent, msg_type):
        return None
    for old in list(recent or [])[-12:]:
        old_text = _s(old)
        if not old_text:
            continue
        if _jaccard(cur, _bigrams(strip_actions(old_text))) >= threshold:
            return f"复读（和最近发过的一条重合度 {threshold:.0%} 以上）"
    return None


# 至少这么长的动作标签才算「有辨识度」，短标签（轻笑/休息/沉默）不算——
# 那是人设的固定招牌，拦它等于不许她用自己的人设。
_ACTION_TAG_MIN = 4


def _action_tags(text: str) -> List[str]:
    """取出这条消息里的括号动作，归一化后取前 4 个字。

    取前缀而不是全串：`(停下进食的动作)` 与 `(停下进食)`、`(停下进食的动作，看着他)`
    在用户眼里是同一个动作，逐字比会全放过。
    """
    out: List[str] = []
    for m in _ACTION_BLOCKS.finditer(str(text or "")):
        # `_ACTION_BLOCKS` 没有捕获组（它先于这个检查存在），从整段里剥掉首尾括号
        raw = m.group(0).strip().strip(_OPENERS + _CLOSERS)
        tag = _PUNCT_ONLY.sub("", raw).strip()
        if len(tag) >= _ACTION_TAG_MIN:
            out.append(tag[:_ACTION_TAG_MIN])
    return out


def _check_action_repeat(text: str, recent: Sequence[str], limit: int = 20) -> Optional[str]:
    """同一个动作标签在最近几条里已经用过。

    线上真实数据（3 小时 20 条里有 4 条撞上）：`(停下进食的动作)` 用了两次、
    `(放下餐具)` 两次。正文措辞每条都不同，所以 `_check_repeat` 的二元组重合度
    抓不到；用户看到的却是「她又来了同一句」，而且这类动作往往跟当下在聊的事
    毫无关系（「放下手里的活」——她在跟人聊微信，哪来的活）。

    只比**同一人**最近几条（`recent` 就是这个人的发言），且只认 ≥4 字的标签。
    窗口给 20：8 条只盖住一次连发，隔了十几条换个措辞挂同一个动作照样「重复出现」。
    """
    cur = _action_tags(text)
    if not cur:
        return None
    olds = [_action_tags(o) for o in list(recent or [])[-limit:]]
    for tag in cur:
        for old_tags in olds:
            if tag in old_tags:
                return f"动作标签复读（「{tag}」最近已经用过）"
    return None


def _check_cross_user(
    text: str, others: Sequence[Tuple[str, str]], threshold: float = 0.6
) -> Optional[str]:
    """这条和**发给别人的**那些太像。

    与 `_check_repeat` 分开：那条只跟同一个人的最近几条比，而**跨用户撞车结构上
    抓不到**——日志按 (bid,uid) 分开存，每个人只跟自己比。

    而素材是共用的一份：Core 的日程挂在角色级（`mood` 在用户级，`daily_schedule`
    在角色级），所以「刚忙完个案笔记」这句话对同一 bot 下的所有人**逐字相同**。
    32 条真实记录里，16:10~16:51 这 1 小时 41 分里有 12 个不同的人分别收到
    「刚忙完个案笔记」——那不是复读，是 12 个人读了同一行字幕。在用户眼里就是群发。

    `others` 是 (发给谁, 正文) 列表，调用方只放**别人**的。
    """
    cur = _bigrams(strip_actions(text))
    if not cur or not others:
        return None
    for who, old_text in others:
        old_text = _s(old_text)
        if not old_text:
            continue
        ratio = _jaccard(cur, _bigrams(strip_actions(old_text)))
        if ratio >= threshold:
            return f"和刚发给{who}的那条太像（{ratio:.0%}），换一句"
    return None


def _check_cross_user_action(
    text: str, others: Sequence[Tuple[str, str]], min_others: int = 1, limit: int = 40
) -> Optional[str]:
    """这条的**括号动作**已经被套用在好几个聊天对象身上了。

    线上的真实形态（524 条主动消息里统计出来的）：15 个动作标签被套在多个聊天对象
    身上——「蜷在沙发」10 个人、「伸个懒腰」9 个、「窝在沙发」6 个。正文各不相同，
    所以 `_check_cross_user`（比正文）不响；而按人比对的 `_check_action_repeat`
    只看得到「这一位」的历史，也永远不响。用户看到的是：她跟谁都窝在沙发里。

    `min_others` 默认 2：只跟**一个**别人撞过的动作可能是巧合（同一时段两个人都在
    说累），只有到处都在用才说明这是模板。门槛低会把发送量砍掉一截。
    """
    cur = _action_tags(text)
    if not cur:
        return None
    for tag in cur:
        hit_by = [
            other_uid
            for other_uid, other_text in list(others or [])[:limit]
            if tag in _action_tags(other_text)
        ]
        if len(set(hit_by)) >= min_others:
            return (
                f"动作标签「{tag}」已经被套在 {len(set(hit_by))} 个人身上了"
                f"（不止一位刚收到过类似的），换一个跟她这句话有关的"
            )
    return None


# 首行标题：把「为什么现在说这件事」当成了开场白。
#
# 229 条真实记录里 26 条是这个形状。首行全是 `anchor_fact` 的原话——由头给模型的
# 句子是「你自己这边刚发生的事：刚忙完个案笔记」，模型把后半句提上来当标题：
#     刚忙完醒神 / 早餐时刻 / 处理完个案笔记 / 刚忙完早餐的早晨
# 后面才是真正要说的话。对话框里那条「刚忙完个案笔记」像系统消息，不像人开口。
#
# 判据：**首行短、没有第二人称、结尾不是句末标点**。三条同时满足才算标题——
# 人真要写的第一句通常有主语或「你」，也通常带句号。
_TITLE_MAX = 12
# 旧实现写的是字符类 `[你您他她咱大家TA ta]`：字符类里空格与单个字母都成了
# 独立判据，于是任何带空格的首行、任何含 a/t 的英文词（`data`、`chat`）都被
# 当成「有第二人称」，标题判据形同虚设（P3-18）。改成交替式后只有整词
# 「大家」「你」这类才算；`ta` 加前后非字母断言，避免 `data` 里的 ta 误命中。
_TITLE_NO_PEER = re.compile(
    r"你|您|他|她|咱|大家|(?<![A-Za-z])ta(?![A-Za-z])|[，。！？；、,.!?;~～…]",
    re.I,
)


# 这些短句本身就是一句话，不是标题。
#
# 「早安\n(伸个懒腰)」剥掉「早安」就只剩动作了。
_TITLE_KEEP = ("早安", "晚安", "午安", "你好", "您好", "在吗", "在不在", "嗨", "喂", "在")

# **自述、语气词、连接词开头的行是人话，不是标题。**
#
# 「短 + 没有第二人称 + 没有句末标点」这个判据本身太松，实测把六种正常开头都剥了：
# 「我先说」「嗯嗯」「说起来」「对了」——它们都是人在开口说话，不是小标题。
# 所以再加一道否决：以第一人称/这些词开头的，一律不当标题。
_TITLE_NOT_HEAD = (
    "我", "你", "咱", "他", "她", "它", "第",
    "嗯", "哎", "诶", "啊", "哦", "噢", "唔", "嘿", "哈", "呵",
    "对", "那", "这", "好", "行", "是", "不", "没", "别", "先", "再",
    "说", "想", "看", "听", "算", "来", "去", "走", "在", "从", "把", "被", "给",
    "总", "反", "其", "如", "毕", "竟", "果", "然", "于", "至",
    "话", "接", "顺", "另", "换", "改", "问", "答", "讲", "聊",
)


# 剥完剩下的正文太短 → 那不是标题，是被换行切开的一个词组。
#
# 「早\n起了」剥掉「早」只剩「起了」，而那条换行本来就是停顿
# （`_join_lines` 特意保留换行就是为了这个）。真实标题剥完后面一定有完整的话。
_MIN_AFTER = 6


# 单行「光杆标题」判据 —— **只给单行用**（多行走 `is_title_like` 的「有没有第二行」）。
#
# 原来单行沿用的是「短 + 没有第二人称 + 没有句末标点」，实测退化成**首字抽签**：
# 15 条正常人话里 9 条被拒（`今天加班到九点`/`会议改到下午了`/`路上买了束花`/
# `今晚早点睡吧`/`突然想吃火锅了`/`今天天气真好`/`今天走了两万步`/`今天地铁特别挤`/
# `刚看完那部剧`），理由都是「整条只有一行标题、没有正文」；而同样长度、只因为首字
# 落在 `_TITLE_NOT_HEAD` 里的 `我把那本书看完了` 就放行。一次误杀 = 这条不发
# + 由头被 `mark_failed` 作废 + 白烧一次 LLM 调用（`engine.py:3717-3735`）。
#
# 真实语料里的标题是**光杆动词短语**（`处理完个案笔记`、`早餐时刻`）——它像标签：
# 没有时间、没有体标记、没有语气词。而人主动发的一句话几乎总占其中一样，所以单行
# 只在「一样都不占」时才当标题。下面几张表收的都是**虚词与时间词（封闭类）**，
# 不是内容词表——判的仍然是结构。
#
# 2.0.8（报告 P1-4）：上面两张表还是不够。`蹄子真不好使`（6 字）、`窝在客厅没光的
# 角落`（9 字）两张表都不占，照旧被判成「整条只有一行标题、没有正文」，线上实测
# 这类误杀 28 次。而且**单行文本本来就没有「正文」可剥**，「单行 = 只有标题」这个
# 前提本身就站不住；所以单行改成「明确长得像标签才判死」，多出第三张表
# `_TITLE_PREDICATE_WORD`——只要出现陈述性虚词就一律放行。
_TITLE_TIME_WORD = re.compile(
    r"今|明|昨|后|前|天|日|周|月|年|点|分|钟|号|刚|才|已|正|马上|最近|刚才|现在"
)
_TITLE_FUNCTION_WORD = re.compile(r"[了着过吧呢啊啦嘛呗哦呀噢嘞]")

# 陈述性标记 —— 只给**单行**判据用，把「人话」和「光杆标题」分开。
#
# 2.0.7 的单行判据只查时间词与语气词，实测仍然误杀了两条正常短句：
#     '蹄子真不好使'（6 字）、'窝在客厅没光的角落'（9 字）
# 它们既没有时间词也没有语气词，于是被判成「整条只有一行标题、没有正文」——
# 整条不发 + 由头被 mark_failed 作废 + 白烧一次 LLM 调用。
#
# 光杆标题是**名词性/动宾短语**（`处理完个案笔记`、`个案笔记整理`、`早餐时刻`），
# 里面不会出现副词、否定、判断/存在动词、能愿动词、结构助词；而人主动说的一句话
# 几乎总要占其中一样（「真不好使」「在客厅」「没光的角落」）。所以单行只要命中
# 这张表就放行。表里收的同样是**封闭类虚词**，不是内容词——判的还是结构。
_TITLE_PREDICATE_WORD = re.compile(
    r"不|没|别|真|太|很|挺|好|更|最|在|是|有|会|能|要|想|得|地|的|吗|么|呢"
    r"|就|也|都|还|又|才|把|被|给|让|跟|和|对|从|往"
)


def _bare_title_shaped(line: str) -> bool:
    """单行像不像「光杆标题」——见 `_TITLE_TIME_WORD` 上面的说明。

    只有「既没有时间/体貌标记，也没有句末语气词，也没有陈述性虚词」的行才算标题。
    真人开口的一句话几乎总带其中一个（「今天加班到九点」有时间、「会议改到下午了」
    有「了」、「蹄子真不好使」有副词和否定、「窝在客厅没光的角落」有存在动词与
    结构助词），所以这个判据不会把正常人话当成标题。
    """
    text = str(line or "").strip()
    if _TITLE_TIME_WORD.search(text):
        return False
    if _TITLE_FUNCTION_WORD.search(text):
        return False
    if _TITLE_PREDICATE_WORD.search(text):
        return False                    # 2.0.8：有陈述性虚词就是人话，不是标签
    return True


def strip_title_line(text: str) -> str:
    """剥掉像标题的首行，返回剥完之后的正文（剥不出就原样返回）。"""
    raw = str(text or "")
    lines = raw.splitlines()
    while len(lines) > 1:
        head = lines[0].strip()
        if not head:
            lines.pop(0)
            continue
        if len(head) > _TITLE_MAX or _TITLE_NO_PEER.search(head):
            break
        if head.startswith(("（", "(", "［", "[")):
            break                       # 括号开头是语C动作，不是标题
        if any(head.startswith(k) for k in _TITLE_KEEP):
            break
        if head.startswith(_TITLE_NOT_HEAD):
            break                       # 自述/语气词/连接词开头是人话
        rest = "\n".join(lines[1:]).strip()
        if len(rest) < _MIN_AFTER:
            break                       # 剥完就没内容了，那它不是标题
        lines.pop(0)
    rest = "\n".join(lines).strip()
    # 剥完空了 = 整条只有一行标题，那这条本身就没内容，不该发
    return rest if rest else raw


def strip_echoed_label(text: str) -> str:
    """剥掉「第一行是把正文里的话又说了一遍」的那种标签行。

    ## 为什么不再用「首字抽签」去判标题

    `strip_title_line` 判的是**形状**（短、没有第二人称、没有句末标点）。它挡住了
    一部分，但线上仍然在念标题，漏掉的全是形状合法的：

        '你的状态\\n(揉眼)还在想你说的「状态」，饿得慌，你呢？'
        '想念\\n 特尔瓦大帅'
        '有点想你了\\n(伸个懒腰) 暮色余晖，刚才咨询时总走神，想你了。'

    这些首行有第二人称（你的状态）、有语气词（想你了），形状判据天然要放过它们。
    再给形状加条件就是在抽签——每加一条都可能误伤一句真人话。

    ## 换一条与措辞无关的判据：内容重复

    这些标签的共同点不是长什么样，而是**它的内容已经出现在正文里**。所以这里只看
    一件事：首行的字有多少出现在后面的正文里。出现 ≥80% 就说明它是复述，删掉不会
    丢信息（那层意思正文里有），删了就是净收益。

    反例都应该留下：「刚忙完\\n好累啊」（刚忙完三个字不在正文里）、
    「今天加班到九点\\n累死了」、「嗯\\n(笑) 好啊」。纯问候也不必特殊照顾：
    「早安\\n……早安」里的「早安」删掉后正文里还有一个。

    只剥**第一行**，且只在后面还有正文时剥；整条只有一行时它就是一个完整消息，
    不归这里管（那是 `is_title_like` 的事）。
    """
    raw = str(text or "")
    lines = raw.split("\n")
    if len(lines) < 2:
        return raw
    head = lines[0].strip()
    rest = "\n".join(lines[1:]).strip()
    if not head or not rest:
        return raw
    # 太长的不可能是标签；带句末标点的是一整句话（「我回来了」这种开头常被误判）。
    if len(head) > 16 or head[-1] in "。！？!?…":
        return raw
    if head.startswith(("（", "(", "［", "[", "【")):
        return raw                       # 括号开头是语C动作，不是标签
    # 首行里的「实字」（去掉标点与空白）有多少在后文出现过。
    core = [ch for ch in head if not ch.isspace() and ch not in "，。！？；、,.!?;~～…「」『』\"'“”‘’（）()【】"]
    if len(core) < 2:
        return raw                       # 一两个字（「早」「嗯」）不是标签
    hit = sum(1 for ch in core if ch in rest)
    if hit / len(core) < 0.8:
        return raw
    return rest


def merge_lead_line(text: str) -> str:
    """把「短首行 + 括号动作 + 正文」并成一段，消掉标题的形状。

    前面两道（`strip_title_line` / `strip_echoed_label`）管的是**该删**的首行；
    这一道管**不该删、但也不该单独成行**的那种，而且只认一种确凿的形状——
    正文以括号动作开头：

        '有点想你了\\n(伸个懒腰) 今天风大，你那边冷不冷'
        '你的状态\\n(揉眼) 还在想你说的那句'
        '想你了\\n(轻轻舒展羽翼) 突然很想你，昨晚睡得好吗？'

    这三条的内容都没问题，问题是第一行孤零零几个字，读起来就是一条小标题。
    并成一段之后它至少是一条正常消息，内容一字不动。

    **只认括号动作这一种形状**：「早\\n起了」这种是同一句话被换行切成两半
    （`_join_lines` 特意保留换行就是为了这个停顿），并起来会把停顿也拼掉。
    判据越窄越安全——这一层宁可少并一条，也不能把正常断句拼成一句。
    """
    raw = str(text or "")
    lines = raw.split("\n")
    if len(lines) < 2:
        return raw
    head = lines[0].strip()
    rest = "\n".join(lines[1:]).strip()
    if not head or not rest:
        return raw
    if len(head) > 12 or head[-1] in "。！？!?…，、；：":
        return raw
    if head.startswith(("（", "(", "［", "[", "【", "#")):
        return raw
    if not rest.startswith(("（", "(", "［", "[")):
        return raw
    return f"{head}{rest}"


def _title_shaped(line: str) -> bool:
    """这一行**像不像**小标题——只看形状，不看整条够不够长。"""
    first = str(line or "").strip()
    if not first or len(first) > _TITLE_MAX or _TITLE_NO_PEER.search(first):
        return False
    if first.startswith(("（", "(", "［", "[")):
        return False
    if any(first.startswith(k) for k in _TITLE_KEEP):
        return False
    if first.startswith(_TITLE_NOT_HEAD):
        return False
    return True


def is_title_like(text: str) -> bool:
    """整条是不是「只有一行标题、没有正文」。

    判据必须落在**有没有第二行正文**上。原来末行量的是整条长度
    （`len(str(text or "").strip()) >= _MIN_AFTER`），于是「刚忙完\\n好累啊」这种两行
    短消息会被判成标题 —— 而 `strip_title_line` 在这种情况**故意不剥**（见它 321 行的
    注释：首行后面那句太短，说明那不是标题，是被换行切开的一个词组）。结果这条消息
    既没被剥、又被判成「只有标题」，**整条不发**。

    单行走的是另一条窄判据（`_bare_title_shaped`）：真实语料里的标题都是
    `标题\\n正文` 这个形状，单行只可能是「光杆动词短语」，不再靠长度与首字抽签。

    2.0.8（报告 P1-4）：单行本来就是「一整条消息」，没有正文可剥，所以判据收紧成
    「时间词、语气词、陈述性虚词一样都不占」才判死。实测放行 `蹄子真不好使`、
    `窝在客厅没光的角落`、`今天加班到九点`、`我把那本书看完了`、`早安`；
    仍然拒掉 `处理完个案笔记`、`个案笔记整理` 这类光杆标签。
    """
    lines = [ln.strip() for ln in str(text or "").split("\n")]
    if any(ln for ln in lines[1:]):
        return False                      # 有第二行正文，就不是「只有标题」
    first = lines[0] if lines else ""
    # 单行也要够长才算标题：一两个字的「早」「嗯」是极简回话，不是小标题。
    if len(first) < _MIN_AFTER:
        return False
    if not _title_shaped(first):
        return False
    return _bare_title_shaped(first)


# 协议残留：模型把 SEND/NO 协议的话漏进了正文。
#
# 真实记录里抓到过两条，一条把模型的整个思考过程当众发给了用户：
#     「你今天打算怎么过？\nNO\n现在是下午三点，不符合早上早安的设定要求…」
#     「别总自己闷着\n>」
#
# `_SEND_LEADING` 只剥**开头**的 SEND，正文里独立成行的标记一个都不管。
# 判据是**独立成行**——中文正文里出现「NO」是要紧的事，而模型把协议标记放在
# 自己的行上。
_PROTOCOL_LINE = re.compile(
    r"^\s*(?:>\s*)+$"                      # 引用符：>
    r"|^\s*(?:SEND|NO|YES|OK)\s*[:：]?\s*$",  # 裸的协议词，独占一行
    re.I | re.M,
)


def _check_protocol_residue(text: str) -> Optional[str]:
    """正文里混进了 SEND/NO 协议标记或引用符——直接退回。"""
    m = _PROTOCOL_LINE.search(str(text or ""))
    if not m:
        return None
    hit = str(m.group(0)).strip()
    if not hit:
        hit = "空行"
    return f"协议残留（正文里混进了独立成行的「{hit}」）"


# ─── 第四面墙：她活在对话里，不活在设备里 ───────────────────────
#
# 用户原话：「现实→网友关键是这个会突然拿个手机出来 或者看着屏幕上的消息」。
#
# 真实数据（714 条角色会话里逐条扫）：
#   「屏幕」50 处、「手机」19 处、「打字」3 处、「电脑」1 处。
# 例：
#   (放下杯子…蹄子在桌面上轻敲了几下，起身去打开界面。)
#   我把手机按在胸口，深深吸了一口气，仿佛还能透过屏幕闻到你…
#   可惜我现在只能打字给你…
#
# 这不是人设教的——五个人设里都没有这些词（阿莉娜那条「日常使用手机联系」
# 是描述姐姐的背景设定）。是**模型的通病**：聊天气泡里的对话，它很自然地
# 借用现实中的通讯隐喻。
#
# 为什么插件要拦：她是角色，不是客户端。她一「看屏幕」，用户那边瞬间出戏——
# 而且这类句子几乎总是长句、带着环境描写，正好是最容易暴露「这是 AI 在演」
# 的那一类。
#
# 判据用词表 + 只在**正文**里查（`strip_actions` 之后）：
# 括号里的动作描写同样要查——上面第一条例子整个破墙点就在括号里。
# 耳机/充电宝从词表里拿掉了：它们是日常物件，「耳机没电了」是人话，
# 不是「在用客户端」。真正破墙的是手机/屏幕这类**通讯设备隐喻**。
_WALL_DEVICE = re.compile(
    r"手机|屏幕|电脑|平板|键盘|鼠标|充电器"
)
# 头像/昵称/上线/下线也拿掉了：「你头像真好看」「我先下线了」都是正常
# 聊天（P2-01 实测被误杀），留着「在线状态」「账号」这类平台自指即可。
_WALL_UI = re.compile(
    r"对话框|聊天框|聊天窗|聊天记录|消息记录|未读|弹窗|通知栏|状态栏|界面|页面|窗口"
    r"|表情包|语音条|打字|输入法|发送键|回车|回车键"
    r"|在线状态|账号|加好友|好友申请"
)
# 「ID/uid」这类只在**中文语境**下才算破墙：裸的 `ID:` / `id=` 大量出现在
# 调试回显、CronJob 日志、`<ignore id="…">` 这类框架标记里（实测全都误伤过），
# 而她真提的时候会说「你的账号」「这个昵称」「你叫什么名字」。
# 要求前面最多三个汉字：`id="3802…"` 前面是空格或引号，匹配不上。
_WALL_ID = re.compile(r"[\u4e00-\u9fa5]{1,3}(?:ID|id|uid|UID)")
# 「作为 AI / 我的训练数据」是自报家门，无条件拦：她不该有这些自我描述。
_WALL_AI = re.compile(
    r"作为?(?:一个)?.{0,4}(?:AI|人工智能|语言模型|机器人|程序|算法)"
    r"|我的?(?:训练|模型|参数|数据库|代码)"
)
# 「这个 APP / 这个软件」是直接指着客户端说话，也算自指，无条件拦。
# 注意这里不能把「系统/服务」放进来：它们单独出现时多半是日常话题
# （「系统又更新了」「这家服务不错」），交给下面的弱词组按语境判。
_WALL_PLATFORM = re.compile(r"这个?(?:APP|app|应用|平台|软件)")
# 技术状态词单独一组，只在**同一条已经在演角色**时才拦（见 _check_wall）。
# 这些词在正常聊天里到处都是：「游戏延迟好高」「系统又更新了」——旧实现
# 无条件子串匹配，把日常吐槽全判成破墙（P2-01）。
_WALL_TECH = re.compile(
    r"服务器|断线|重连|延迟|卡顿|bug|BUG|Bug|系统|服务"
)


def _check_wall(text: str) -> Optional[str]:
    """打破第四面墙：把自己写成一个「在用设备的人」。

    只查**中文语境**下的自指。技术词/平台名（APP/bug/延迟/系统）容易误伤
    开发向、游戏向的日常聊天，所以它们只在整条明显是在演角色时才拦——
    判据是同一条里已经有括号动作（语C 的舞台指示），这是「她真的在演」
    最明确的信号；没有括号动作的吐槽放行。
    """
    # 查**原文**，不查 strip_actions 之后的正文字。
    #
    # 为什么：破墙点大量出现在括号里的动作描写中，而 strip_actions 恰恰把括号
    # 整段剥掉——线上那条「起身去打开界面」剥完就只剩「真是的，这么着急呀。」，
    # 干净得像个乖宝宝。所以这里必须连动作描写一起查。
    body = str(text or "")
    for name, pat in (
        ("设备", _WALL_DEVICE),
        ("界面", _WALL_UI),
        ("账号", _WALL_ID),
        ("平台", _WALL_PLATFORM),
        ("AI自指", _WALL_AI),
    ):
        hit = pat.search(body)
        if hit:
            return f"打破第四面墙（提了{name}「{hit.group(0)}」——她活在对话里，不活在设备里）"
    # 弱词（技术/平台名）必须和括号动作同条才拦：单独出现时它只是聊天内容，
    # 不是「她在用客户端」的证据。
    if _ACTION_BLOCKS.search(body):
        hit = _WALL_TECH.search(body)
        if hit:
            return f"打破第四面墙（演角色时提了技术词「{hit.group(0)}」——她活在对话里，不活在设备里）"
    return None


# 「念想」通道用：这一类**必须**指向对方。别的通道不查——
# 「刚熬的汤，趁热喝了吧」这种祈使句没写「你」，却是说给 TA 听的。
_PEER_REF = re.compile(r"你|您|TA|ta|他|她|咱|大家|人呢|宝宝|亲爱的|老公|老婆")


def _check_pressure(text: str) -> str:
    """拒「把自己的情绪负担推给对方」。

    容器实测拿到过这么一条（110 字 4 句）：她因为对方一晚上没回消息而发这一串，
    里面有「快回我一句」。这不是她想说的话，是她在**要债**——把自己的不安
    变成对方的任务，对方不接就成了她的问题。

    按**结构**判，不按词表：「要求对方立刻回应」并且「拿没回消息说事」，
    两个都在才拦。单独一个「回我一个字」在真生气的语境里是合理的，
    只在它变成要债的载体时才该拒。
    """
    body = strip_actions(text)
    demands_now = _DEMAND_NOW.search(body)
    if not demands_now:
        return ""
    # 「她今天一天没理我，可我这边一直等着」——有期待，所以催一下是合理的；
    # 「我等了一晚上你都不回」——把等待的时间算到对方头上，要债。
    if not _SILENCE_CHARGE.search(body):
        return ""
    return "把自己的不安推给对方（催回+计较没回）"


# ─── 占位符残留（v2.2.7 / issue #11）────────────────────────────
#
# 线上真实事故：`04:55:48` 一条消息原样发出了
# `(Model response will be rendered here)`。
#
# 这串不是插件生成的——全仓 grep 无命中，它是上游/前端模板里的占位文字被模型
# 或转发链当成正文吐了出来。但**发出去就是插件的事**：用户看到的是一个括号里
# 写着「模型回复将在此处渲染」的消息。
#
# 旧实现 `sanitize.looks_like_framework_only()` 只认「整条都是框架块」，
# 这条一旦混在正常句子里（`今天好累 (Model response will be rendered here)`）
# 就 `framework_only=False` 直接放行。所以这里改成**子串级**兜底。
_PLACEHOLDER_RESIDUE = re.compile(
    r"model\s+response\s+will\s+be\s+rendered\s+here"
    r"|will\s+be\s+rendered\s+here"
    r"|rendered\s+here"
    r"|模型(?:的)?回复(?:将|会)?(?:在)?此(?:处|地)渲染"
    r"|回复将(?:在)?此处(?:渲染|显示)"
    r"|\{\{\s*(?:response|reply|content|message)\s*\}\}",
    re.I,
)


# ─── 开头轮换（v2.2.7 / issue #1）────────────────────────────────
#
# 线上真实数据：破冰 27 条里 15 条以「刚」开头（「刚用魔法烧了壶水」
# 「刚用蹄子夹着杯子泡茶，洒了半桌」「刚醒，睡0小时」…）。
#
# 这不是模型懒：生成侧提示词写着「说一件你刚做完的小事」，模型把「刚做完」
# 读成了「以刚字开头」——**提示词教的句式，模型照抄了**。
#
# 私聊侧早就有这道闸（`generator.py:2934` 的「你最近开头用过：…这几个这次都
# 别用了」），群聊侧一直没有。这里补上，判据是**统计**不是禁词：
# 最近 6 条里同一个开头字出现 ≥3 次，才禁这一个字，不是永久禁用。
_OPENING_TRIM = " \t\r\n（(【[「『\"'·—…,，。.、!！?？~～；;：:"


def opening_head(text: str) -> str:
    """一条消息的「开头字」：剥掉括号动作与标点后的第一个字。

    `（趴在床上把翅膀摊开）才睡0小时就醒了` → `才`
    `刚用蹄子夹着杯子泡茶` → `刚`
    """
    body = strip_actions(str(text or "")).strip()
    body = body.lstrip(_OPENING_TRIM)
    return body[:1]


def _opening_repeat_issue(
    text: str, openings: Sequence[str], window: int = 6, limit: int = 3
) -> Optional[str]:
    """这条的开头字在最近几条里已经用滥了。"""
    heads = [opening_head(o) for o in list(openings or [])[-window:]]
    heads = [h for h in heads if h]
    cur = opening_head(text)
    if not cur or not heads:
        return None
    hit = heads.count(cur)
    if hit >= limit:
        return (
            f"开头重复（最近 {len(heads)} 条里 {hit} 条以「{cur}」开头，换个说法起头）"
        )
    return None


def _check_placeholder_residue(text: str) -> Optional[str]:
    """正文（含括号内）混进了模板占位符——无条件拒。"""
    hit = _PLACEHOLDER_RESIDUE.search(str(text or ""))
    if hit:
        return f"占位符残留（「{hit.group(0)[:32]}」不是她能说的话）"
    return None


# ─── 物种/身体一致（v2.2.7 / issue #10）──────────────────────────
#
# 线上真实样本：露娜是**蹄类**，却说出「我刚醒手还有点僵」。
#
# 为什么放在验收层而不是只靠提示词：物种信息（`brief["her"]["species"]`）
# 已经注入了提示词，模型照样写「手」——这类错误是**概率性的**，提示词权重再高
# 也会漏。而它是硬事实错误，发出去立刻穿帮，属于必须拦的一类。
#
# 只对**明确蹄类/兽类**角色生效：物种为空（旧 Core、没配物种）时不查，
# 避免把「人手」当错。词表也刻意只认**身体部位用法**，不认「手机/手套/顺手/
# 手艺」这类构词——`顺手把杯子放下` 是蹄子也说得通的人话。
_HOOF_SPECIES = re.compile(
    r"马|鹿|羊|牛|驴|驼|麒麟|独角兽|半人马|人马|蹄|兽|龙|狼|狐|猫|犬|兔|鸟|鹰|羽"
)
_HAND_BODY = re.compile(
    # 「手还有点僵」「手冷」「手抖」「手冻得发僵」——身体状态。
    # 中间只允许程度/时态小词，不允许任意字：否则「顺手把凉水倒了」这种
    # 蹄子也说得通的人话会被误判成「手」。
    r"手(?:(?:还|都|也|好|很|太|有点|有些|一点儿?|发|冻得|凉得|僵得)){0,3}"
    r"(?:僵|冷|冰|凉|抖|酸|疼|痛|麻|湿|软|硬|热|暖|脏)"
    # 「抬起手」「搓了搓手」「捧着…手」
    r"|(?:抬起|举起|伸出|摊开|握着|攥着|搓|揉|捧|捏|拍|摸|吹|甩)(?:了)?(?:双|一只|两只|两)?手"
    # 明确的身体部位词
    r"|手(?:心|背|掌|指|腕|肘|臂)"
    r"|(?:双|两只|一只|左|右)手",
)


def _check_species(text: str, species: str = "") -> Optional[str]:
    """蹄类/兽类角色不许出现「手」这类人形身体部位。

    `species` 为空或非蹄类时一律放行——宁可不查，也不要把正常说法判死。
    """
    sp = _s(species).strip()
    if not sp or not _HOOF_SPECIES.search(sp):
        return None
    hit = _HAND_BODY.search(str(text or ""))
    if hit:
        return (
            f"身体不一致（她是{sp}，没有「{hit.group(0)}」这种东西）"
        )
    return None


# ─── 说教/连续纠正对方（v2.2.7 / issue #6）───────────────────────
#
# 线上真实样本（娜莉灵对同一个人 6 条里 4 条）：
#   「酥牧别刨根问底了」「酥牧别再吵我了」「酥牧问这个干嘛」
#   「酥牧别胡思乱想」「酥牧别一惊一乍的」「少自作多情了」「算你老实」
#
# 单看一句都像角色在逗弄，**连着看就是在管对方**：你问多了、你烦、你想错了、
# 你大惊小怪。用户原话是「自以为是」。
#
# 所以判据不是禁词，是**频次**：最近 5 条里已经有 2 条在纠正对方，
# 这一条又想纠正 → 拒。偶尔一句「别闹」是情趣，连着五句是训话。
_PREACHY = re.compile(
    r"别(?:再)?(?:问|管|吵|闹|想|猜|打听|刨根问底|胡思乱想|一惊一乍|自作多情|瞎想|乱猜)"
    r"|别[^，。！？、,.!?;]{0,6}了"
    r"|少(?:自作多情|来这套|管闲事|废话|装)"
    # v2.2.10：线上真有一条「少在这装可怜了」——`少` 后面跟的是地点/对象再跟动词，
    # 上面那支漏了。这里只认**小句开头**的「少…了/吧」，避免「多少/减少/很少」误伤。
    r"|(?:(?<=[，,、；;：:。！？!?\s])|^)少[^，。！？、,.!?;]{0,6}(?:了|吧)"
    r"|不(?:许|准)[^，。！？、,.!?;]{0,6}(?:了|吧)?"
    r"|算你(?:老实|识相|识趣)"
    r"|问(?:这个|这些|这么多)?(?:干嘛|干什么)"
    r"|多管闲事|大惊小怪|一惊一乍|胡思乱想|自作多情"
)


# 句内的**祈使否定小句**：`，别闹` / `酥牧别胡思乱想` / `不许碰翅根`。
# v2.2.10：清理时只剪掉这一小段，不整句删——线上 22:39:53 那条
# 「（嘟囔着拱了拱）没睡透，别闹。上次冷落我可还记着仇呢，不给我补偿可别想我理你。」
# 被整句删光，结果是**她这一轮一句话没说**，比说教更糟。
_PREACHY_CLAUSE_LEFT = "，,、；;：:"
_PREACHY_CLAUSE_END = "。！？!?，,、；;…"


def _strip_preachy_clauses(sentence: str) -> str:
    """把句内「别…/不许…/少…」这类小句剪掉，保留同一句里别的内容。

    从右往左剪（位置不会错乱）；左边界取句首或最近的逗号/顿号/分号，
    右边界取最近的句末或分隔标点。剪完可能只剩称呼（「酥牧。」），
    调用方按残余长度决定要不要丢这一句。
    """
    text = str(sentence or "")
    hits = list(_PREACHY.finditer(text))
    for m in reversed(hits):
        start = m.start()
        left = 0
        for sep in _PREACHY_CLAUSE_LEFT:
            idx = text.rfind(sep, 0, start)
            if idx + 1 > left:
                left = idx + 1
        end = len(text)
        for i in range(m.end(), len(text)):
            if text[i] in _PREACHY_CLAUSE_END:
                end = i + (1 if text[i] in "。！？!?…" else 0)
                break
        text = text[:left] + text[end:]
    return text


def _preachy_hits(text: str) -> int:
    """这条里有几处「在纠正/指挥对方」。"""
    return len(_PREACHY.findall(str(text or "")))


def _check_preachy(text: str, recent: Sequence[str], window: int = 5, limit: int = 2) -> Optional[str]:
    """最近几条已经连续在纠正对方，这条还来 → 拒。

    `limit=2`：窗口里已有 2 条在管对方，就说明进入「说教模式」了。
    一条都不拦——单句逗弄是人物性格，不是毛病。
    """
    olds = [str(o or "") for o in list(recent or [])[-window:]]
    if sum(1 for o in olds if _preachy_hits(o)) < limit:
        return None
    if _preachy_hits(text) <= 0:
        return None
    return "又在纠正对方（最近几条已经连着在管 TA 了，换成回应 TA 或说自己的事）"


# ─── 回声「也」（v2.2.8 / issue #9）─────────────────────────────
#
# 线上真实样本（同一个人 3 条）：
#   「嗯 我也爱你 亲爱的。外面好冷 我刚醒手还有点僵 快过来让我抱抱。」
#   「(穿着睡衣趴着) 刚才那句话太噎人啦，不过我也爱你。」
#   「(把收拾好的旧票据推到一边) 我也爱你呀。」
#
# 「也」是**回声词**：它必须回指对方刚说过的话。对方没说「爱你」，她说
# 「我也爱你」，等于替对方捏了一句没说出口的话——用户原话是「如果只是主动
# 消息那就更不行了，不要能说『也』」。
#
# 所以判据是**上下文**，不是禁词：上下文里没人说过对应的由头，这条就不许用
# 「我也…」接。只认**情感回声**（爱/想/喜欢/心疼/舍不得/惦记/一样），不认
# 「我也想去」这种普通并列——那种「也」是「同样」，不是回声。
_ECHO_ALSO = re.compile(
    r"我也(?:一样)?"
    r"((?:很|好|真|挺|超|最)?(?:爱你|想你|喜欢你|心疼你|舍不得你|惦记你|念着你))"
    r"|我也一样(?=[，。！？、,.!?;\s]|$)"
)
# 上下文里出现这些，才说明对方确实说过由头。
_ECHO_TRIGGER = re.compile(
    r"爱(?:你|您)|想你|喜欢(?:你|您)|心疼|舍不得|惦记|念着你|一样"
)


def _echo_also_hit(text: str) -> Optional[re.Match]:
    """这条里有没有「我也…」式的回声。"""
    return _ECHO_ALSO.search(strip_actions(str(text or "")))


def _context_allows_echo(context: Sequence[str]) -> bool:
    """上下文里有没有能接住「也」的由头。"""
    ctx = " ".join(str(c or "") for c in (context or []))
    return bool(_ECHO_TRIGGER.search(ctx))


def _check_echo_also(
    text: str, context: Sequence[str] = ()
) -> Optional[str]:
    """没有由头却说「我也爱你」——等于替对方说话。"""
    hit = _echo_also_hit(text)
    if not hit:
        return None
    if _context_allows_echo(context):
        return None
    return (
        f"「{hit.group(0)[:10]}」里的「也」在接对方的话，"
        "可对方没说这句（换个说法，别用「也」）"
    )


def tidy_outgoing_text(
    text: str, *, species: str = "", recent: Sequence[str] = (),
    context: Sequence[str] = (),
) -> str:
    """主链路回复的**确定性清理**（`on_decorating_result` 用，v2.2.7）。

    主动消息那条链有完整验收，普通对话回复却一道闸都没有——线上被投诉的说教句、
    「手还有点僵」、占位符原文，全部来自普通回复（`state.json` 里 `src=chat`）。
    但普通回复的提示词不在本插件手里（由人设/主链路决定），所以这里只做**不改语义**
    的三件事：

    1. 删掉模板占位符（「(Model response will be rendered here)」）；
    2. 蹄类角色把身体部位的「手」改成「蹄子」；
    3. 最近已经在连着纠正对方时，删掉这条里继续纠正对方的那几句；
    4. 没有由头却说「我也爱你」时，把「也」去掉（`context` 由调用方传入
       对方刚说的话；主动消息没有上下文，一律去掉）。

    改不动就原样返回；删空了返回空串，由调用方决定这条不发。
    """
    body = str(text or "")
    if not body.strip():
        return body
    body = _PLACEHOLDER_RESIDUE.sub("", body)
    # 占位符常常是**带括号**的：删掉文字会留下「今天好累 ()」这种空壳，
    # 发出去比原文更怪。把只剩括号的一对一起清掉。
    body = re.sub(r"[（(【\[「『]\s*[）)】\]」』]", "", body)
    if _echo_also_hit(body) and not _context_allows_echo(context):
        body = _ECHO_ALSO.sub(
            lambda m: ("我" + m.group(1)) if m.group(1) else "我也是", body
        )
    sp = _s(species).strip()
    if sp and _HOOF_SPECIES.search(sp):
        for old, new in (
            ("双手", "两只蹄子"),
            ("两只手", "两只蹄子"),
            ("一只手", "一只蹄子"),
            ("左手", "左蹄"),
            ("右手", "右蹄"),
        ):
            body = body.replace(old, new)
        body = _HAND_BODY.sub(
            lambda m: m.group(0).replace("手", "蹄子", 1), body
        )
    if _preachy_hits(body):
        olds = [str(o or "") for o in list(recent or [])[-5:]]
        if sum(1 for o in olds if _preachy_hits(o)) >= 2:
            # v2.2.10：**剪小句，不整句删**。整句删会把回复删空（线上真发生过：
            # 「没睡透，别闹。…可别想我理你。」→ 空 → 这一轮她一个字没说）。
            # 先按句末标点切，句内剪掉「，别…」这类祈使小句；剪完只剩括号动作/
            # 标点的句子才丢；全丢光就保留原文（宁可带点脾气，也不能不吭声）。
            kept: list = []
            for piece in re.split(r"(?<=[。！？!?])", body):
                if not piece.strip():
                    continue
                if _preachy_hits(piece):
                    piece = _strip_preachy_clauses(piece)
                    piece = re.sub(r"[，,、；;]\s*(?=[。！？!?]|$)", "", piece)
                    piece = re.sub(r"[，,、；;]{2,}", "，", piece).strip()
                    residual = strip_actions(piece).strip(
                        "，。！？!?…~～、；;：: 　"
                    )
                    # 剪完只剩称呼（「酥牧。」）或虚词（「了。」）→ 这一句整句不要。
                    if len(residual) < 3:
                        continue
                kept.append(piece)
            # 剪掉中间的小句后，前后两截会直接粘在一起（「没睡透上次冷落我…」），
            # 补个逗号才是人话；前一截已经有句末标点就不补。
            buf = ""
            for piece in kept:
                if buf and not buf.rstrip().endswith(tuple("。！？!?…，,、；;：:")):
                    buf = buf.rstrip() + "，"
                buf += piece
            joined = buf.strip()
            body = joined or body
    body = re.sub(r"[ \t]{2,}", " ", body).strip()
    return body


def verify_message(
    text: str,
    *,
    recent: Sequence[str] = (),
    called: str = "",
    require_about_peer: bool = False,
    allow_short: bool = False,
    msg_type: str = "",
    species: str = "",
    context: Sequence[str] = (),
) -> Tuple[bool, str]:
    """验收一条即将发出的消息。返回 (通过, 原因)。

    `require_about_peer` 只给「念想」通道开：那一类本来就是「闲下来想起一个人」，
    整条却没有第二人称就说明它在讲自己——那正是 197 条记录里最扎手的问题。
    `allow_short` 只给群心流开：群里「嗯」「哈哈」这类短应和是常态。
    `msg_type` 由调用方给出这条消息的类型（greeting/closer/...）：`SHORT_OK_TYPES`
    里的类型按场景放宽空泛与复读判据，默认空串 = 老行为，不影响既有调用方。
    """
    if not _s(text):
        return False, "空消息"
    reason = _check_structure(text)
    if reason:
        return False, reason
    reason = _check_intent_leak(text)
    if reason:
        return False, reason
    reason = _check_thin(text, called, allow_short=allow_short, msg_type=msg_type)
    if reason:
        return False, reason
    reason = _check_repeat(text, recent, msg_type=msg_type)
    if reason:
        return False, reason
    reason = _check_action_repeat(text, recent)
    if reason:
        return False, reason
    reason = _check_protocol_residue(text)
    if reason:
        return False, reason
    # v2.2.7 / issue #11：占位符子串级兜底（旧实现只认整条框架块）。
    reason = _check_placeholder_residue(text)
    if reason:
        return False, reason
    reason = _check_pressure(text)
    if reason:
        return False, reason
    # P3-06：最后通牒/逼表态。这个检查器定义后一直没被调用（死代码）——
    # 模型输出「到底想怎样，直说」时没有任何运行时兜底，而生成侧提示词明确要求
    # 「不下最后通牒」。放在 `_check_pressure` 之后：「催回 + 计较没回」那种要债
    # 已有更准的结构判据，先让它命中（同一个坏样本会得到更具体的原因）。
    reason = _check_ultimatum(text)
    if reason:
        return False, reason
    # v2.2.7 / issue #6：连续纠正对方（「别…」「少…」「算你…」）。
    reason = _check_preachy(text, recent)
    if reason:
        return False, reason
    # v2.2.8 / issue #9：没有由头却用「也」接对方的话（「我也爱你」）。
    # 主动消息没有上下文（`context` 为空）→ 一律不许出现情感回声。
    reason = _check_echo_also(text, context)
    if reason:
        return False, reason
    reason = _check_wall(text)
    if reason:
        return False, reason
    # v2.2.7 / issue #10：物种与身体部位一致性（蹄类不许说「手」）。
    reason = _check_species(text, species)
    if reason:
        return False, reason
    # 剥掉标题行之后**还是**标题样子的，说明整条只有一句标题没有正文。
    # 这种退回重生成没用——模型换个标题再来一遍。直接不发。
    if is_title_like(strip_title_line(text)):
        return False, "整条只有一行标题、没有正文"
    if require_about_peer and not _PEER_REF.search(strip_actions(text)):
        return False, "没有指向对方（这一类要说给 TA 听，不是讲她自己）"
    return True, ""

# 最后通牒式：把对方逼到必须给个说法。线上真实样本 `到底想怎样，直说`。
# 只认**命令/逼问**的固定说法，不碰普通的问句——她当然可以问事情。
#
# 词表刻意**不含裸「直说」「说清楚」**：它们在正常口语里太常见
# （「有话就直说，别憋着」是商量语气，不是逼人表态），做成判据会大面积误杀。
# 审核样本 `到底想怎样，直说` 靠前半句就能命中，去掉裸词不丢这个样本。
_ULTIMATUM = re.compile(
    r"到底想怎样|你到底要|给个说法|把话说清楚|少来这套"
    r"|别装(?:了|蒜|死|傻|没事|无辜)?(?=[，。！？、,.!?;\s]|$)"
)


def _check_ultimatum(text: str) -> Optional[str]:
    """这条是不是在逼对方表态（把话堵死）。"""
    body = strip_actions(text)
    hit = _ULTIMATUM.search(body)
    if hit:
        return f"把话说死了（「{hit.group(0)}」）——她不会逼人表态"
    return None
