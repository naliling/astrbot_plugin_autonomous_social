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

# 括号动作：（）与半角 ()，以及书名号式强调
_ACTION_BLOCKS = re.compile(r"[（(【\[][^（()【\][]{0,80}[）)】\]]")
_OPENERS = "（(【["
_CLOSERS = "）)】]"

# 只剩标点与空白
_PUNCT_ONLY = re.compile(r"[\s，。！？、；：,.!?;:\-—~～…·「」『』\"'“”‘’]+")

# ── 「关于说话」的判据 ────────────────────────────────────────
# 关键不是「开头是不是 想/要」，而是**这句话有多大比例在描述说话这个动作**。
# 语料里泄漏的写法五花八门：
#   「表达赖床的慵懒和饿意」        —— 整条
#   「赖床中，想分享这种懒洋洋的感觉并关心对方」 —— 前半句是状态，后半句才是泄漏
#   「饿了，想找主人讨点关注」      —— 前两个字才是内容
# 只匹配开头会漏掉后两种；所以改成：把「说话动作」从句抠掉，看**剩下多少**。
# 剩下的比例低于阈值就判定为「整条都在说自己要说这句」。
_INTENT_CLAUSE = re.compile(
    r"(?:想|要|打算|准备|该|想要|想着|顺便|顺手|试着|来)\s*"
    r"(?:表达|分享|说出|说点|说一|说句|说说|告诉|提起|提一嘴|提一|关心|问问|问一"
    r"|聊|聊聊|撒个娇|撒|调情|勾引|逗|逗弄|调侃|撩|刷存在感|找点存在感|找|回应|回复"
    r"|接话|说|讲|抱怨|吐槽|撒娇)"
    r"[^，。！？；、,.!?;]{0,40}"
)
# 句尾挂着自述尾巴。
# 注意**不把「顺便」单列**：它本身就是正常中文连接词（语料里有一句
# 「想确认一下人还在不在，顺便继续撩拨一下」是好消息）。要抓的是
# 「顺便…不指望回」这种搭配，以及「想找个借口」这种自述动机。
_INTENT_TAIL = re.compile(
    r"(?:顺便[^，。！？]{0,12}不指望回|不指望(?:你|TA)?回|不用回我|不需要回复|不用回复"
    r"|而已|不解释|不细说|想找(?:个|点)借口|找个借口|想找点乐子|说给自己听|写给自己)"
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
    # 真实记录里有一句 `想分享下此时此刻偷懒的状态，顺便勾引一下啊散。⏎(…动作…)⏎阳光晃得我心烦…`
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
    return None


def _check_thin(text: str, called: str = "", allow_short: bool = False) -> Optional[str]:
    """空泛：去掉括号与标点之后几乎不剩字。

    `allow_short` 给群心流开：「嗯」「哈哈」这类短应和是群聊常态，不该一律拦下。
    """
    body = _PUNCT_ONLY.sub("", strip_actions(text)).strip()
    if not body:
        return None                       # 已由 _check_structure 拦
    if len(body) <= 2 and not allow_short:
        if called and called in body:
            return None
        return f"空泛（正文只有 {body!r}）"
    return None


def _check_repeat(text: str, recent: Sequence[str], threshold: float = 0.72) -> Optional[str]:
    """与最近发过的太像。

    判据是**字符二元组重合度**，不是逐字相同——复读的措辞每次都不同
    （23 条沙发追剧，每条都不一样），但骨架高度重合。
    """
    cur = _bigrams(strip_actions(text))
    if not cur:
        return None
    for old in list(recent or [])[-12:]:
        old_text = _s(old)
        if not old_text:
            continue
        if _jaccard(cur, _bigrams(strip_actions(old_text))) >= threshold:
            return f"复读（和最近发过的一条重合度 {threshold:.0%} 以上）"
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
_TITLE_NO_PEER = re.compile(r"[你您他她咱大家TA ta]|[，。！？；、,.!?;~～…]")


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
    "总", "反", "其", "如", "如", "其", "毕", "竟", "果", "然", "于", "至",
    "话", "接", "顺", "接", "另", "换", "改", "问", "答", "讲", "聊",
)


# 剥完剩下的正文太短 → 那不是标题，是被换行切开的一个词组。
#
# 「早\n起了」剥掉「早」只剩「起了」，而那条换行本来就是停顿
# （`_join_lines` 特意保留换行就是为了这个）。真实标题剥完后面一定有完整的话。
_MIN_AFTER = 6


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


def is_title_like(text: str) -> bool:
    first = str(text or "").split("\n")[0].strip()
    if not first or len(first) > _TITLE_MAX or _TITLE_NO_PEER.search(first):
        return False
    if first.startswith(("（", "(", "［", "[")):
        return False
    if any(first.startswith(k) for k in _TITLE_KEEP):
        return False
    if first.startswith(_TITLE_NOT_HEAD):
        return False
    return len(str(text or "").strip()) >= _MIN_AFTER


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


def verify_message(
    text: str,
    *,
    recent: Sequence[str] = (),
    called: str = "",
    require_about_peer: bool = False,
    allow_short: bool = False,
) -> Tuple[bool, str]:
    """验收一条即将发出的消息。返回 (通过, 原因)。

    `require_about_peer` 只给「念想」通道开：那一类本来就是「闲下来想起一个人」，
    整条却没有第二人称就说明它在讲自己——那正是 197 条记录里最扎手的问题。
    `allow_short` 只给群心流开：群里「嗯」「哈哈」这类短应和是常态。
    """
    if not _s(text):
        return False, "空消息"
    reason = _check_structure(text)
    if reason:
        return False, reason
    reason = _check_intent_leak(text)
    if reason:
        return False, reason
    reason = _check_thin(text, called, allow_short=allow_short)
    if reason:
        return False, reason
    reason = _check_repeat(text, recent)
    if reason:
        return False, reason
    reason = _check_protocol_residue(text)
    if reason:
        return False, reason
    reason = _check_pressure(text)
    if reason:
        return False, reason
    # 剥掉标题行之后**还是**标题样子的，说明整条只有一句标题没有正文。
    # 这种退回重生成没用——模型换个标题再来一遍。直接不发。
    if is_title_like(strip_title_line(text)):
        return False, "整条只有一行标题、没有正文"
    if require_about_peer and not _PEER_REF.search(strip_actions(text)):
        return False, "没有指向对方（这一类要说给 TA 听，不是讲她自己）"
    return True, ""
