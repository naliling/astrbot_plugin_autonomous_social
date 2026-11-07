"""未完话题：三种「话没说完」不是同一件事，所以走三条不同的路。

混在一起做就会变成「隔半天问一句在吗」——那是最不像人的追问方式。拆开之后：

1. **这一场话还没完（thread）**：以「双方都不说话了多久」为触发。里面再分两种说法：
   - `probe`：对方答得很薄（「还行」「就那样」），或她上一句问了事没得到实答
     → 就着**那件事**再问一句。
   - `presence`：刚才确实在来回聊，突然安静了 → 问一句人还在不在。
   关键点：**不管最后一句是谁说的**。旧版要求「最后一句必须是对方的」，而主链路回过话
   之后最后一句永远是她自己的，于是这条最该触发的路径在实际聊天里几乎永远不成立——
   表现出来就是「用户随便发几句，模型不会回来追问」。
2. **隔一阵回访那件事（loop）**：对方提到一件有后续结果的事（面试、复查、搬家）却没说
   完。当时不适合追（人家在忙），过几小时再问「后来呢」才对。到点即发，不看 urge。
3. **没人回也不空着（closer）**：她主动找的话对方一直没回。旧版从此把这个人压住不再
   开口，真人却是过一阵自己冒一句把尴尬揭过去。一次 pending 只收一次场。
4. **她说完之后对方就没声了（continue）**：对方回了、她也回了，然后对面消失。这条
   以前被「她回过话就收尾」那条早退挡死，永远接不上话——而真人这时会说一句
   「人呢」或者接着上一句往下说。窗口比追问宽（默认 90 分钟 ~ 8 小时）。

四条都不进 urge 模型：攒十几小时才开口那是「另起一个新话题」该有的样子，而话说到一半
的事，真人以分钟和小时计。
"""

from __future__ import annotations

import math
import random
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .clock import city_epoch, city_now
from .reasoning import (
    busy_signal,
    is_leaving,
    is_thin,
    last_direction,
    said_goodnight,
    sleep_signal,
)

# ─── 1. 这一场话还没完 ──────────────────────────────



# 至少来回过两句才算「在聊」，否则只是对方偶尔发了一句就走了
# 「还在聊」与「说断了」的分界：对方在这段时间里说过这么多条，就不是断点，
# 是在跟你一来一回。两个人隔着十分钟各说一句，那才可能是话头断了。
ACTIVE_CHAT_WINDOW = 30 * 60.0
ACTIVE_CHAT_MIN_MSGS = 2

THREAD_CONTEXT_MIN = 2


def _is_inbound(item: Any) -> bool:
    """这条账是不是**对方说的**。

    口径统一在这里：`dir == "out"` 才是她自己说的，**缺 `dir` 一律算对方说的**。
    写侧（state 的 record_incoming / record_outgoing、engine 归档历史）一律显式带 dir，
    所以缺 dir 只可能是老数据；老数据的 conversation 来自「只记对方说过的话」那会儿，
    而且 `engine` 归一化历史时写的就是「不是 out 就是 in」。

    统一到「缺 dir 当对方说的」比统一到「当自己说的」安全得多：前者最多多看一句
    对方说过的话，后者会把对方真说过的那句当成她自己的而整条丢掉——`last_in_text`
    会因此判不出「回得敷衍」，`recent_exchange` 会退化成空。全库其余地方
    （anchors / reasoning / style_profile）也都是「只认 == "out" 是她说的」。
    """
    return isinstance(item, dict) and str(item.get("dir", "") or "") != "out"


def _conversation(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    """账本里能当条目看的那几条。

    `conversation` 是 state.json 里的字段，可能被手改、被迁移写坏、被写成字符串或数字。
    这里统一收成「元素是 dict」的列表：`reversed("abc")` 会把每个字符当一条
    （再 `.get` 就抛 AttributeError），`reversed(nan)` 直接 TypeError，而这条路径在
    心跳里每轮都走。**只筛 dict，不筛 text**——空文本的条目在调用点各有各的处理，
    在这里筛掉会悄悄改变「在聊」的判定。
    """
    conv = user.get("conversation")
    if not isinstance(conv, list):
        return []
    return [item for item in conv if isinstance(item, dict)]


def last_in_text(user: Dict[str, Any], now: float, within_seconds: float) -> str:
    """对方最近一句说了什么（跳过她自己说的话）。找不到就返回空串。

    判「回得敷衍」必须看**对方**那句，而账本里最后一条往往是她自己的回复。
    """
    for item in reversed(_conversation(user)):
        if not _is_inbound(item):
            continue
        ts = _num(item.get("ts", 0))
        if ts <= 0:
            # 时间戳不可信（缺字段 / nan / 脏字符串）：跳过这一条，别把很久以前那句
            # 当成「刚说的」。原来走裸 float + except，nan 会让 `now - ts > within`
            # 恒为 False，于是脏时间戳反而被当成最新的一句。
            continue
        if now - ts > within_seconds:
            return ""
        text = str(item.get("text", "") or "").strip()
        if text:
            return text
    return ""


def thread_reason(
    user: Dict[str, Any],
    now: float,
    *,
    probe_after_seconds: float,
    presence_after_seconds: float,
    max_seconds: float,
    context_seconds: float,
    presence_floor_seconds: Optional[float] = None,
    continue_after_seconds: float = 0.0,
    continue_max_seconds: float = 0.0,
) -> Tuple[str, str]:
    """这一场话是不是说断了。返回 (种类, 动机)，种类为 probe / presence / continue / ""。

    沉默从**最后一个人说话**算起，不要求那句是对方说的：主链路一说话，最后一条永远是
    她的，按旧规则判这条就永远不成立——那正是「用户随便发几句，模型不回来追问」的成因。

    两种说法的耐心不一样：追问那件事是有由头的，两三分钟就成立；光问「在吗」得再等会儿，
    不然像在催人。

    `continue_*` 两个参数默认 0 = 关闭，老调用（老测试、老行为）逐字不变。打开时多出
    第三种情况：**她说完之后对方就没声了**——这不是「对方那句悬着」，所以上面那条
    「她回过话就收尾」的早退会把它挡死，得单独判。
    """
    # 「在吗」的门槛取 max(配置值, 两个最小间隔)。对方每小时都在说话的话，
    # 沉默最长也就一小时，永远够不到这个门槛——**每小时都在聊的人不需要被问
    # 在不在**。这比「每两小时问一次」合理得多。
    floor = presence_after_seconds
    if presence_floor_seconds:
        floor = max(floor, _num(presence_floor_seconds))
    seen = _num(user.get("last_seen", 0))
    spoken = _num(user.get("last_spoken", 0))
    last_said = max(seen, spoken)
    if last_said <= 0 or seen <= 0:
        return "", ""
    last_msg = str(user.get("last_message", "") or "")
    # ─── 接话：她回完、对方就没声了 ────────────────────────────────
    #
    # 这一段必须放在下面两句「沉默超过 max_seconds 就算了」**之前**：continue 的窗口
    # （默认 90 分钟 ~ 8 小时）比追问窗口（默认 70 分钟）长得多，放后面会被那两句先
    # 挡死——而这条路本来就是为「她说完之后对方消失，永远接不上话」开的。
    #
    # 判据里没有「最近还在聊」那一道：`spoken >= seen` 意味着最后一句是她说的，而
    # 「对方还在连着说话」会让 `seen > spoken`，两种条件本来就互斥。
    if continue_max_seconds > 0:
        asked_at = _num(user.get("thread_asked_at", 0))
        since_spoken = now - spoken
        # v2.0.6（m00055）：接话之前先确认「确实有一场能接的话」。
        #
        # v2.0.5 这里只看时间戳：她说过话、对方沉默了、时间落在窗口里，就判「接话」。
        # 于是下面三种情况它也会开口——
        #   1) 她自己那句已经是收尾（「晚安」「先这样吧」），再接着说等于把人叫回来；
        #   2) 账本里根本没有可接的内容（对方只发过「嗯」/她只回过一句「好」），
        #      生成侧拿不到话头，只能吐一句「继续」「然后呢」——「一句回一句」就是
        #      这么来的；
        #   3) 她上一句已经在问对方话了，再来一句问句就成了连问。
        # 前两条在这里用硬条件拦掉：话头两端都得在（对方说过、她也回过），且她那句
        # 不是道别。第三条是写法问题，交给生成侧的提示词（thread_meta 带 asked_question）。
        her_text = str(user.get("last_spoken_text", "") or "").strip()
        peer_text = str(user.get("last_message", "") or "").strip()
        _near = [
            item for item in _conversation(user)
            if 0 < now - _num(item.get("ts", 0)) <= continue_max_seconds
        ]
        _has_exchange = (
            (any(_is_inbound(it) for it in _near) and any(not _is_inbound(it) for it in _near))
            or bool(peer_text and her_text)
        )
        if (
            spoken >= seen                                  # 最后一句是她说的
            and continue_after_seconds <= since_spoken <= continue_max_seconds
            and now - seen <= continue_max_seconds          # 对方也没消失太久
            and str(user.get("pending_result", "") or "") != "waiting"
            and not (asked_at > 0 and seen <= asked_at)     # 这段沉默只接一次
            and not (
                is_leaving(last_msg) or sleep_signal(user, now) or busy_signal(user, now)
            )
            and _has_exchange                               # 真的有一场话可以接
            and not is_leaving(her_text)                    # 她自己那句不是在道别
            and not said_goodnight(her_text)                # 也不是在说晚安
        ):
            return "continue", ""
    # 断点必须是**对方**那句还热着的时候。只看 max(seen, spoken) 的话，她在群里或跟
    # 别人说过话就会把沉默计时重置，于是隔了半天还在追问人家早上那句——真人不会这样。
    if now - seen > max_seconds:
        return "", ""
    silence = now - last_said
    if silence > max_seconds:
        return "", ""
    # 这个断点已经追过了吗？
    #
    # 判据**不能是「thread_for == last_said」**。last_said = max(对方说的, 她说的)，
    # 而她每次追问都会把 last_spoken 推到发送时刻——于是这个等式在第二次追问时必然
    # 不成立，守卫形同虚设。真实表现：一个每小时回一句的用户，7 天被追问 44 次。
    #
    # 正确的问法是「**追过一次之后，对方说过新话吗**」：说过 → 是新断点，可以再追；
    # 没说 → 还是那个断点，别追。跟时间戳是否相等无关。
    asked_at = _num(user.get("thread_asked_at", 0))
    if asked_at > 0 and seen <= asked_at:
        return "", ""
    # 她**已经回过**、之后双方都没再说话 → 这段已经收尾了，不是新断点。
    # 以前用 max(seen, spoken) 判断点，把「她刚回完」也算成「对方的话还悬着」，
    # 于是每一轮主链路回复都会造出一个新断点。
    #
    # 注意这一条**只**管 probe/presence：上面 continue 分支已经把「她回完、对方消失」
    # 接走了，这里再早退一次就等于没接。
    if spoken >= seen and (now - spoken) > presence_after_seconds:
        return "", ""
    if str(user.get("pending_result", "") or "") == "waiting":
        # 她上一条主动发的话还悬着：这时候再补一句就是追着人要回复
        return "", ""
    # T1：对方最近在忙（不一定是最后一句）就别追问——真忙与敷衍要分开，
    # 「在忙」之后回一个「嗯」不是敷衍，是在忙。
    if is_leaving(last_msg) or sleep_signal(user, now) or busy_signal(user, now):
        return "", ""
    # 「说断了」不是「对方停了一下」。
    #
    # 以前只判「对方沉默了多久」，而 probe_after_minutes 默认 2 分钟——于是对方每发
    # 一条消息、隔两分钟，她就去问「怎么突然没声了」。仿真里一个每小时回一句的
    # 用户，7 天被她追问了 44 次。
    #
    # 真正的断点是**这场话本来在继续、却停在这儿了**：对方最近还在连着说话
    # （半小时内两条以上），那就是聊得正热，不是断了。
    recent_in_count = sum(
        1 for item in _conversation(user)
        if _is_inbound(item)
        and 0 < now - _num(item.get("ts", 0)) <= ACTIVE_CHAT_WINDOW
    )
    if recent_in_count >= ACTIVE_CHAT_MIN_MSGS:
        return "", ""

    # 得先「在聊」：断点之前还有话挨着，才叫聊到一半
    nearby = [
        item for item in _conversation(user)
        if _num(item.get("ts", 0)) < last_said
        and last_said - _num(item.get("ts", 0)) <= context_seconds
    ]
    if len(nearby) + 1 < THREAD_CONTEXT_MIN:
        return "", ""

    # 追问的由头有两条：对方最近那句话几乎没带信息（「还行」「就那样」），
    # 或者她自己上一句在问事。两条都不要求「最后一句是谁说的」——主链路一回复，
    # 最后一条永远是她的，按谁最后说话判就会把这条路径堵死。
    recent_in = last_in_text(user, now, context_seconds) or last_msg
    thin = bool(recent_in) and is_thin(recent_in)
    # 「她上一句在问事」只用来把 presence 说得更像接话，不当作快速追问的理由：
    # 她问了、对方没回，隔三分钟再追一句就是催，那个得等。
    # 第二项（reason）一律空串：动机由模型自己产生，插件只决定「这一条属于哪类」
    if thin and silence >= probe_after_seconds:
        return "probe", ""
    # 「在吗」是给「人没了」用的，判据必须是**对方**冷了多久，不是「最后一条距今多久」。
    # 用 last_said 的话，她自己刚发完的那条会把计时按住，而对方每小时都说话的人
    # 照样每两小时（最小间隔一过）就收到一句「在吗」——7 天 30 次。
    # 对方还在说话，就不是人没了。
    peer_silence = now - seen
    if peer_silence >= floor and peer_silence < max_seconds:
        return "presence", ""
    return "", ""


def thread_meta(
    kind: str,
    about: str = "",
    asked: str = "",
    silence_minutes: float = 0.0,
) -> Dict[str, Any]:
    """把「说什么」交给生成器时的类型信息。

    `about` 是对方最后说的那句、`asked` 是她上一句问的那件——带着这两样才写得出
    一句接着话头的话，而不是干巴巴的「在吗」。

    `silence_minutes` 只有 continue 用：同一句「你人呢」在沉默 20 分钟和 5 小时后
    是两种说法，得让生成侧知道到底隔了多久。
    """
    if kind == "continue":
        # 与 probe/presence 的区别：那两条是**对方那句悬着**，这条是**她说完之后
        # 对方消失**。所以说法也要换：短沉默接着上一个话题往下说，长沉默问一句人去哪了；
        # 两种都不许查岗、不许提「你怎么不回」——那是催，不是接话。
        minutes = int(silence_minutes or 0)
        away = f"{minutes} 分钟" if minutes < 120 else f"{minutes / 60:.1f} 小时"
        # v2.0.6（m00055）：沉默多长、她上一句是不是问句，生成侧都要知道。
        # v2.0.5 的 _MODE_NOTES["continue"] 本来就写着「沉默还短就顺着说、沉默久了
        # 就问人去哪了」，但这份时长从来没进过提示词——模型无从分支，只能回一句
        # 「继续」「然后呢」。这里把它作为字段交出去。
        asked_text = str(asked or "").strip()
        asked_question = asked_text.endswith("？") or asked_text.endswith("?")
        return {
            "category": "continue",
            "intent": "followup",
            "mode": "continue",
            "msg_type": "continue_topic",
            "msg_type_desc": f"接着刚才那半句话往下说（TA 已经 {away}没声了）",
            "style_hint": (
                "短沉默就顺着上一个话题往下说；长沉默就问一句人怎么突然没了。"
                "别查岗、别问「你怎么不回」、别连着问两句"
            ),
            "about": about,
            "asked": asked,
            "silence_minutes": minutes,
            "short_silence": minutes <= 120,
            "asked_question": asked_question,
        }
    if kind == "probe":
        return {
            "category": "probe",
            "intent": "followup",
            "mode": "probe",
            # v2.0.6（m00055）：原来这里也写 continue_topic，于是面板/日志里
            # 「接话」和「追问」永远是同一个 cat，主人没法判断到底是哪条路在发。
            "msg_type": "probe_topic",
            "msg_type_desc": "就着刚才那件事再问一句",
            # 原来这里给五条现成的话（「后来呢」「真的假的」…）让模型照着挑。
            # 那是替她写台词：追问的说法就那么几种，模型每轮都在这几条里轮，
            # 同一个人的「追问」听起来永远一模一样。现在只给写法要求。
            "style_hint": "直接问那件事的进展，别绕成寒暄；一两句说完就停，别连着追问",
            "about": about,
            "asked": asked,
        }
    return {
        "category": "presence",
        "intent": "followup",
        "mode": "presence",
        "msg_type": "check_in",
        "msg_type_desc": "把说到一半断掉的话接上",
        "style_hint": "轻轻问一句还在不在就行；别质问、别要求对方解释为什么没回",
        "about": about,
        "asked": asked,
    }


# ─── 2. 隔一阵回访那件事 ────────────────────────────

# 提到这些多半意味着后面还有个结果没说出来，值得过几小时问一句。
# 只用两个字以上的词：单个「考」会把「考虑」也抓走。
_LOOP_WORDS = (
    "面试", "考试", "答辩", "汇报", "演讲", "宣讲", "培训", "复查", "体检", "手术",
    "看诊", "挂号", "打针", "拆线", "检查结果", "报告", "成绩", "分数", "录取",
    "offer", "入职", "离职", "跳槽", "转正", "评审", "上线", "交付", "验收",
    "搬家", "退租", "看房", "装修", "出差", "旅行", "旅游", "开学",
    "比赛", "约了", "在弄", "在改", "在写", "在等", "等结果", "等通知",
    "吵架", "分手", "表白", "闹翻", "冷战", "相亲", "复合", "摊牌",
    "坏了", "丢了", "找不到", "弄砸",
)

# 已经说了结果的，就别再问了。
#
# **只收「结果」词，不收时间副词。** 这张表原来还混着「后来 / 已经 / 早就」，
# 而「面试还没消息，已经等了好几天」「后来一直没消息」恰恰是**还开着**的线索 ——
# 时间副词只说明过了多久，不说明有结果。混在一起会把还开着的线索直接关掉，
# 于是那件事再也不会被回访（`loops_resolved_by` 也拿它当「已经有结果了」的判据）。
#
# 光有词表还不够：`word in body` 是**无词边界子串匹配**，而「通过 / 过了 / 算了」
# 是最容易被借用的三个——「通过朋友打听也没用」「我算了算时间」「路过了那家公司」，
# 正常聊天里一句就把整条线索关掉。所以判据拆成两层：
#   1. `_LOOP_CLOSED_ANY`：换个语境也还是结果，出现即算；
#   2. `_LOOP_CLOSED_TAIL`：得看清楚**整句是不是在给这件事收场**——词尾要收住
#      （句读或句末，可以先带一个完成体语气词），词首不能是否定/将来，也不能长在
#      别的词里（路过了 / 经过了）。「还没结束」「还没搞定」是还没有结果，不算收场。
_LOOP_CLOSED_ANY = (
    "不说了", "不提了", "就这样", "结果就是",
)
_LOOP_CLOSED_TAIL = (
    "完了", "搞定", "结束", "黄了", "挂掉", "没通过",
    "过了", "通过了", "通过", "算了",
)
_LOOP_CLOSED_RE = re.compile("|".join(_LOOP_CLOSED_TAIL))
# 词尾：可以断句，也可以先带一个完成体语气词再断句（「结束了」「搞定了」「过了，还行」）。
_LOOP_CLOSED_TAIL_RE = re.compile(r"^[了啦咯哟嘞呀的吧呢]?(?:[，,。！!？?；;、~～…\s]|$)")
# 词首：否定或将来——「还没结束」「快结束了」都还开着，不是结果。
_LOOP_OPEN_PREFIXES = (
    "没", "未", "别", "想", "快", "将", "要", "还", "马上", "就要", "即将", "打算", "准备",
)
# 「过了」长在这些字后面是路过，不是「这件事过了」。
_LOOP_PASS_PREFIXES = ("路", "经", "跨", "越")


def _closes_loop(body: str) -> bool:
    """这句话是不是在给某件事**收场**（说了结果）。

    `extract_open_loop`（还开不开一条线索）与 `loops_resolved_by`（关掉哪条线索）
    共用这一个判据：两边口径必须一致，否则会出现「该关的不关」或「开着却被关掉」。
    """
    text = str(body or "")
    if not text:
        return False
    if any(word in text for word in _LOOP_CLOSED_ANY):
        return True
    for match in _LOOP_CLOSED_RE.finditer(text):
        word = match.group(0)
        head = text[: match.start()]
        tail = text[match.end():]
        if tail and not _LOOP_CLOSED_TAIL_RE.match(tail):
            continue
        if any(head.endswith(prefix) for prefix in _LOOP_OPEN_PREFIXES):
            continue
        if word == "过了" and any(head.endswith(p) for p in _LOOP_PASS_PREFIXES):
            continue
        return True
    return False


LOOP_MIN_HOURS = 2.5      # 当时追问不礼貌，至少隔这么久
LOOP_MAX_HOURS = 14.0     # 再晚就不像「记得」，像翻旧账
LOOP_LATE_HOURS = 40.0    # 过了到期点多久之内还算「想起来问」
LOOP_TEXT_MAX = 34


def extract_open_loop(
    text: str,
    now: float,
    clock_offset_minutes: Optional[int] = None,
    min_hours: float = LOOP_MIN_HOURS,
    max_hours: float = LOOP_MAX_HOURS,
) -> Tuple[Optional[str], float]:
    """对方话里有没有一件「还没说完结果」的事。

    与时间锚点（cue）互斥使用：说了「明天面试」的走 cue（对方自己给了钟点），
    只说「今天面试了」的走这里——没有锚点，但事没完。
    """
    body = str(text or "").strip()
    if len(body) < 4 or len(body) > 300:
        return None, 0.0
    if _closes_loop(body):
        return None, 0.0
    hit: Optional[Tuple[int, str]] = None
    for word in _LOOP_WORDS:
        idx = body.find(word)
        if idx >= 0 and (hit is None or idx < hit[0]):
            hit = (idx, word)
    if hit is None:
        return None, 0.0
    start = max(0, hit[0] - 10)
    snippet = body[start: min(len(body), hit[0] + LOOP_TEXT_MAX)].strip()
    if not snippet or _closes_loop(snippet):
        return None, 0.0
    # 顺嘴说要去洗澡/开会（一两小时就能完的事），拿它当「隔半天回访」不成立
    if is_leaving(body) and len(body) <= 24:
        return None, 0.0
    return snippet, _loop_due(now, clock_offset_minutes, min_hours, max_hours)


def _hours(value: Any, default: float) -> float:
    """回访间隔的小时数：读不出来或非有限值退回默认。

    `min_hours`/`max_hours` 正常来自 `config.py`（那里有 floor/ceil 夹取），但这两个值
    一旦是 nan，`now + nan * 3600.0` 会让 `stamp` 变成 nan —— 这条回访就**永远不到点**
    （`live_loops` 的 `now < due` 对 nan 为假，等于「还没到」永真）。inf 同理，
    永远到不了。所以这里不许放行，退回默认值。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _loop_due(
    now: float,
    clock_offset_minutes: Optional[int] = None,
    min_hours: float = LOOP_MIN_HOURS,
    max_hours: float = LOOP_MAX_HOURS,
) -> float:
    """回访时间：至少隔几小时，且落在她当地的白天。"""
    low = max(0.5, _hours(min_hours, LOOP_MIN_HOURS))
    # 上界同时受 `low * 2.2`（「大概睡一觉就问」的旧体感）和 `max_hours` 约束：
    # 以前只写 `low * 2.2`，`max_hours` 只在最后做一次 `min()` 截断，
    # 于是抽出来的样本有约 1/29 正好堆在 max_hours 上——那批线索看着像「掐着表问的」，
    # 而 `LOOP_MAX_HOURS = 14` 想表达的「再晚就不像记得」，在默认配置下也永远用不到
    # （2.5~5.5h 全在 14h 以内）。现在让它参与抽样本身：抽不到，就不会有那撮堆积。
    high = max(low, min(_hours(max_hours, LOOP_MAX_HOURS), low * 2.2))
    stamp = now + random.uniform(low, min(high, 6.0 if low <= 6.0 else high)) * 3600.0
    local = city_now(stamp, clock_offset_minutes)
    if local.hour < 9:
        # 凌晨到点的事留到当天上午：真人不会半夜三点问「你面试咋样了」
        moved = local.replace(hour=10, minute=random.randint(0, 50), second=0, microsecond=0)
        stamp = city_epoch(moved, clock_offset_minutes)
    return min(stamp, now + _hours(max_hours, LOOP_MAX_HOURS) * 3600.0)


# 一个人身上同时记几件「提了但没说结果的事」。以前只记一件，新的直接覆盖旧的——
# 对方一次说了三件事，就只有最后一件会被跟下去。5 件是够用又不至于变成待办清单的上限。
LOOP_SLOTS_MAX = 5

# ─── 她自己许下的诺 ────────────────────────────────────────────────
# 最多同时记几件（按每个用户），以及单件字数上限。
PROMISE_SLOTS_MAX = 3
PROMISE_MAX_CHARS = 24
# 许诺之后多久算「该兑现了」。不是精确日期——模型不写日期，插件也不去猜，
# 到点她自己会挑一个合适的时机把这件事做了。
PROMISE_WAIT_SECONDS = 20 * 3600.0

# 许诺正文的「像不像一件事」检查。
#
# 这个字段会**原样**进下一轮的 prompt（`【你现在知道的】· 你们说好的：…`），
# 所以它既是记忆也是素材。线上真实样本里混进来过：
#     · 你们说好的：了强烈的红光，你猛的惊喜回头望去发现这红光的指向点正是…
#     · ？：…
# 一条是从半句开始、括号都没闭合的正文碎片；一条是纯符号。模型会接着它们演一段
# 根本不存在的剧情。宁可少记一件，也不能把这种东西喂回去。
_PROMISE_MAX_SANE_CHARS = 16
_PROMISE_BAD_HEAD = "？?：:…，,。.、）)】」》"


def _promise_looks_sane(text: str) -> bool:
    """这句「许诺」像不像一件真事。"""
    s = str(text or "").strip()
    if not s or len(s) > _PROMISE_MAX_SANE_CHARS:
        return False
    if s[0] in _PROMISE_BAD_HEAD:
        return False
    # 括号不配平 = 从一段带括注的正文里切出来的碎片
    if s.count("（") + s.count("(") != s.count("）") + s.count(")"):
        return False
    # 至少得有中文/字母/数字，纯标点不算
    return bool(re.search(r"[0-9A-Za-z\u4e00-\u9fff]", s))


def promise_entries(user: Dict[str, Any]) -> list:
    """她自己许下、还没兑现的那些事。老数据里没有这个字段，返回空列表。"""
    raw = user.get("promises")
    out: list = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and str(item.get("about", "") or "").strip():
                out.append(item)
    return out


def _promise_due(item: Dict[str, Any]) -> float:
    """这条诺**该兑现**的时刻（epoch 秒）；判不出来返回 0。

    老数据里只有 `at`、没有 `due`（写侧后来才补上 `due` 字段），原来的
    `if due <= 0: continue` 会让这批诺**永久静默**——既不会到点被兑现，
    还一直占着 `PROMISE_SLOTS_MAX` 的名额，新的诺挤不进来。
    补的基准取 `at`：那是「她自己许下这句话」的那一刻，与写侧 `note_promise` 同源
    （due = at + PROMISE_WAIT_SECONDS）。**不拿 now 当基准**——那等于一读到就立刻到点，
    上线瞬间把陈年旧诺全抖出来，反而更像 bug。
    """
    due = _num(item.get("due"))
    if due > 0:
        return due
    at = _num(item.get("at"))
    if at > 0:
        return at + PROMISE_WAIT_SECONDS
    return 0.0


def live_promises(user: Dict[str, Any], now: float, limit: int = PROMISE_SLOTS_MAX) -> list:
    """到点、还没兑现的诺，按到期先后排。"""
    out: list = []
    dead: list = []
    for item in promise_entries(user):
        due = _promise_due(item)
        if due <= 0:
            # 连「什么时候许的」都没有（不是本插件写的条目）→ 永远不会到点。
            # 留着只会白占名额，顺手摘掉（engine 的 save 会落盘）。
            dead.append(item)
            continue
        if now < due:
            continue
        out.append((due, str(item.get("about", "")).strip(), item))
    if dead:
        raw = user.get("promises")
        if isinstance(raw, list):
            user["promises"] = [item for item in raw if item not in dead]
    out.sort(key=lambda x: x[0])
    return out[:max(1, int(limit))]


def promise_meta(about: str) -> Dict[str, Any]:
    """兑现承诺那一类：模型要知道「当初答应的是什么」，并且**这次是要去做它**。"""
    return {
        "category": "promise",
        "intent": "promise",
        "mode": "promise",
        "msg_type": "share_daily",
        "msg_type_desc": "把之前答应过TA的那件事做了给他看",
        "style_hint": "别写成邀功（我说了我会做到吧），也别解释你为什么现在才做；"
                      "就当那件事本来就要做，做完顺手说一句",
        "about": about,
    }


def _num(x: Any) -> float:
    """容错取时间戳/计数，读不出来或非有限值一律当 0.0。

    nan 不能放行：`due <= 0 or now < due` 这类守卫对 nan 全为 False，一条
    `due=nan` 的事就**永远到点**；而 `int(nan)` 会直接抛 ValueError，
    带走这个用户这一整轮的观察。inf 同理（会永久占住一个槽位）。
    """
    try:
        out = float(x or 0)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def loop_entries(user: Dict[str, Any]) -> list:
    """这件事的候选条目（老数据里是单条平铺字段，这里读出来统一成列表）。

    老结构（loop/loop_due/...）照旧能读：升级时不必改盘上的文件，跑到这一行才顺手搬。
    """
    out: list = []
    raw = user.get("loops")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and str(item.get("about", "") or "").strip():
                out.append(item)
    text = str(user.get("loop", "") or "").strip()
    if text and not any(str(i.get("about", "")).strip() == text for i in out):
        out.append({
            "about": text,
            "due": _num(user.get("loop_due")),
            "expire_at": _num(user.get("loop_expire_at")),
            "retry_at": _num(user.get("loop_retry_at")),
            "tries": int(_num(user.get("loop_tries"))),
            "at": _num(user.get("loop_at")),
        })
    return out


def live_loops(user: Dict[str, Any], now: float, limit: int = LOOP_SLOTS_MAX) -> list:
    """到点、没在重试冷却里、也没凉过头的那几件事，按到期先后排。"""
    out: list = []
    for item in loop_entries(user):
        due = _num(item.get("due"))
        if due <= 0 or now < due:
            continue
        retry_at = _num(item.get("retry_at"))
        if retry_at > 0 and now < retry_at:
            continue
        expire_at = _num(item.get("expire_at"))
        if now > (expire_at if expire_at > 0 else due + LOOP_LATE_HOURS * 3600.0):
            continue
        out.append((due, str(item.get("about", "")).strip(), item))
    out.sort(key=lambda x: x[0])
    return out[:max(1, int(limit))]


def live_loop(user: Dict[str, Any], now: float) -> Optional[str]:
    """到点且还没回访过、也没凉过头的那件事。分工同 live_cue。"""
    hits = live_loops(user, now, limit=1)
    return hits[0][1] if hits else None


def loops_resolved_by(user: Dict[str, Any], msg: str) -> List[str]:
    """T2：对方这条消息里带了某件未完事的**结果**——那件就不必再回访了。

    「问到了就记住」：不是把结果存成记忆，只是别让一个已经结束的话题继续挂着——
    对方明明已经说了「面试过了」，过几小时还去问「后来怎么样」。
    返回被关掉的那些 about（供调用方落账/日志）。
    """
    body = str(msg or "").strip()
    if not body or not _closes_loop(body):
        return []
    out: List[str] = []
    for item in loop_entries(user):
        about = str(item.get("about", "")).strip()
        if not about:
            continue
        # 待回访那件事的关键词还在对方这句里 → 这句说的是它的结果
        if any(w in body for w in _LOOP_WORDS if w in about):
            out.append(about)
    return out


# ─── 2.1 追问过之后的第一轮：关不关交同轮模型 ──────────
#
# 旧版「关键词命中就关」的问题不在词表，而在**判断权**：一句话里出现「面试」和
# 「过了」就关掉，可「面试过了初面，还要等复面」也长这样。关错一次就等于永久丢掉
# 一件事——之后再也不会回访。参考 companion_v2 的做法：插件只负责标出「这一轮是在
# 回那条追问」，由同轮模型看原话判 complete / edit；它看得见上下文，插件看不见。

FOLLOWUP_REVIEW_WINDOW_SECONDS = 48 * 3600.0  # 追问后两天内，对方的回复才算「回这条追问」
SURFACE_COOLDOWN_SECONDS = 3 * 86400.0        # 同一件事提过一次，三天内别再提
MAX_CONTEXT_SURFACES = 3                       # 一件事最多被拿出来说几次

# 这些词说明对方在**给结果**，而不只是提到这件事。关键词只能证明「这句在说这件事」，
# 证明不了「这件事有结果了」——所以复核版要多这一道。
_LOOP_RESULT_WORDS = (
    "过了", "通过了", "没通过", "搞定", "完了", "结束", "黄了", "挂掉", "算了",
    "出结果", "有结果", "结果", "录取", "拿到", "收到", "定下来", "没戏", "凉了",
    "成了", "没成", "成功", "失败",
)


def _loop_item(
    user: Dict[str, Any], about: str, *, migrate: bool = False
) -> Optional[Dict[str, Any]]:
    """按 about 找到那条**真实**的条目对象——只有改它才落得进盘。

    老平铺数据（user["loop"]）在 loop_entries 里是现造的一条 dict，改它等于改完就扔。
    migrate=True 时先按 engine._defer_loop 的既有做法把迁移结果写回 user["loops"]，
    后面写 last_followup_at 才有地方落。
    """
    key = str(about or "").strip()
    if not key:
        return None
    items = loop_entries(user)
    for item in items:
        if str(item.get("about", "")).strip() == key:
            if migrate:
                user["loops"] = items
            return item
    return None


def loop_review_pending(user: Dict[str, Any], about: str, now: float) -> bool:
    """这条线索这一轮该不该交模型复核：她刚追问过，而对方的回复还没看过。

    窗口取 48 小时：再久对方的回复就不是「回这条追问」了，拿旧追问去套新消息，
    只会把两件事搅在一起。窗口内每轮都复核一次是故意的——插件没有「复核已做」
    的回执通道，少复核一次就可能把一句「算了，不弄了」当成还要继续。
    """
    try:
        item = _loop_item(user, about)
        if item is None:
            return False
        at = _num(now)
        last = _num(item.get("last_followup_at"))
        if at <= 0 or last <= 0 or at < last:
            return False
        if at - last > FOLLOWUP_REVIEW_WINDOW_SECONDS:
            return False
        # 追问之后已经复核过（有别的代码写 last_context_at 时才生效）就不必再塞提示。
        return _num(item.get("last_context_at")) < last
    except Exception:
        return False


def loop_surfaced(user: Dict[str, Any], about: str, now: float) -> bool:
    """这件事现在拿出来提合不合适（冷却 + 次数上限；复核期例外）。

    两个上限来自参考实现：同一件事三天内不提第二次，一件最多提三次——否则「回访」
    会变成反复追问同一件事。但**复核期必须放行**：她刚追问过、对方还没回，这时
    拦下来就成了「追问完就不管了」，比提多了更糟。

    读不出来就当「不该提」：少提一次只损失一次机会，提错一次会显得没在听人说话。
    """
    try:
        item = _loop_item(user, about)
        if item is None:
            return False
        at = _num(now)
        last_follow = _num(item.get("last_followup_at"))
        last_context = _num(item.get("last_context_at"))
        if (
            last_follow > 0
            and at >= last_follow
            and at - last_follow <= FOLLOWUP_REVIEW_WINDOW_SECONDS
            and last_context < last_follow
        ):
            return True
        if int(_num(item.get("context_count"))) >= MAX_CONTEXT_SURFACES:
            return False
        if last_context > 0 and at >= last_context and at - last_context < SURFACE_COOLDOWN_SECONDS:
            return False
        return True
    except Exception:
        return False


def mark_followup_sent(user: Dict[str, Any], about: str, now: float) -> None:
    """记下「这次追问真的送到对方手上了」——**只能由调用方在回执确认送达后调**。

    为什么不在发送前记：发送失败、被闸门拦下、对方根本没收到时，记了就把 48 小时
    复核窗口白白用掉，对方之后真回了，也不会再被当成「回这条追问」。
    找不到这条线索就安静返回：状态被并发清掉时，这里不该把整轮带走。
    """
    try:
        at = _num(now)
        if at <= 0:
            return
        item = _loop_item(user, about, migrate=True)
        if item is None:
            return
        item["last_followup_at"] = at
        item["followup_count"] = int(_num(item.get("followup_count"))) + 1
    except Exception:
        return


def loop_meta(text: str, *, review: bool = False) -> Dict[str, Any]:
    """回访那件事的素材。review=True 时多一条复核要求，**这里不判死**。

    谁传 review：调用方先算好 `loop_review_pending(user, about, now)` 再传进来
    （本函数只拿得到 about，看不到 last_followup_at）。窗口内每轮都会复核，
    模型判 complete 后线索被关掉，提示自然消失——插件自己永远不替模型下结论。
    """
    meta: Dict[str, Any] = {
        "category": "loop",
        "intent": "followup",
        "mode": "loop",
        "msg_type": "check_in",
        "msg_type_desc": "问一句TA之前提的那件事后来怎么样了",
        "style_hint": "要明确提到那件事是什么，别只说「那个呢」；问完就停",
        "about": text,
    }
    if review:
        meta["followup_review"] = True
        meta["review_hint"] = (
            "她刚主动追问过这件事，这是对方之后的回复：结合追问内容和这一轮原话判断——"
            "明确收尾、应下、表示知道了、软拒绝、不想继续、转移话题，就判 complete 把这件事结束；"
            "事情还有下文但状态变了（换了时间、换了说法、有了新进展），就判 edit 更新；"
            "只有确实没答到点上才留着。别把「好的」「嗯嗯」「知道了」机械地当结束，"
            "也别只口头带过就接着聊别的。"
        )
    return meta


def loops_resolved_by_review(
    user: Dict[str, Any], msg: str, *, confident: bool = False
) -> List[str]:
    """复核版：只有**确定是在收场**才关这条线索，误关一次就等于永远不再回访。

    比 loops_resolved_by 多一道闸：`_closes_loop` 与关键词命中只说明「这句在说这件
    事的结果」，还得有明确结果词（或调用方已经用模型判过 confident=True）。
    「面试过了初面，还要等复面」没有收尾语义 → 不关。判不准就留着：多问一句比丢
    一件事便宜。旧函数原样保留，没把握的调用点可以先用这个。
    """
    body = str(msg or "").strip()
    if not body or not _closes_loop(body):
        return []
    if not confident and not any(word in body for word in _LOOP_RESULT_WORDS):
        return []
    out: List[str] = []
    for item in loop_entries(user):
        about = str(item.get("about", "")).strip()
        if not about:
            continue
        # 待回访那件事的关键词还在对方这句里 → 这句说的是它的结果
        if any(w in body for w in _LOOP_WORDS if w in about):
            out.append(about)
    return out


def loop_reason(text: str) -> str:
    """回访那件事的动机句。现在返回空串——素材里有「TA提过还没下文的事：xxx」，
    怎么问是模型自己的事；以前这里给四条现成问法，模型每轮都在里面轮。"""
    return ""


# ─── 3. 没人回也不空着 ──────────────────────────────


# 没人回之后至少隔多久才适合自己收场：太短就成了「你怎么不回我」
CLOSER_MIN_HOURS = 5.0
# 一共允许收几次场。之后这条路径对这个人关闭，直到对方重新接话。
MAX_CLOSERS = 3


def _closer_count(user: Dict[str, Any]) -> int:
    """她**实际收过几次场**（`MAX_CLOSERS` 要数的是这个）。

    `MAX_CLOSERS` 写的是「一共允许收几次场」，可原来读的是 `no_reply_streak`——
    那是**被冷落的次数**：desire 里要 `after_ignored` 满 12 小时才 +1，
    和收场用的 `CLOSER_MIN_HOURS`（5 小时）根本不同源。于是
    「被冷落 3 次、一次场都没收过」的人被永久禁掉收场，
    而「被冷落 1 次、已经收了 5 次场」的人照收不误——两边都反着来。

    真正的计数只能在**把收场那句发出去**的那一处 +1（engine 发 closer 时）。
    读侧先认新字段 `closer_count`；老数据没有这个字段时退回沿用 `no_reply_streak`
    （也就是维持现状，不会因为这次改动突然对老用户多收几场场），
    等写侧补上以后自然按真实次数走。
    """
    key = "closer_count" if "closer_count" in user else "no_reply_streak"
    try:
        return max(0, int(user.get(key, 0) or 0))
    except (TypeError, ValueError):
        # 状态文件是外部可改的：读坏了就当作没收过场，别让心跳在这里崩
        return 0


def closer_reason(
    user: Dict[str, Any],
    now: float,
    *,
    after_seconds: float,
) -> str:
    """她主动发的话没人接，隔够了时间自己冒一句把话收掉。

    计时从**她上次主动说话**算起，而不是从 pending 结算算起：「没人回」要等 12 小时才会
    被结算成 ignored，收场那句不该排在结算之后——那会儿对方早就把这事忘了。
    """
    pend = str(user.get("pending_result", "") or "")
    if pend not in ("waiting", "ignored"):
        return ""
    sent = _num(user.get("last_sent", 0))
    if sent <= 0:
        return ""
    if _num(user.get("closer_for", 0)) == sent:
        return ""
    if now - sent < max(after_seconds, CLOSER_MIN_HOURS * 3600.0):
        return ""
    if last_direction(user) != "out":
        # 对方后来又说了一句、只是没接她那句：那不算悬着，不用去收场
        return ""
    # 收过三次场还在没人回，那就不是尴尬是没兴趣了。没有这条会变成一个
    # 永远不接话的人每天被收一次场——收场那句本身会刷新 last_sent。
    if _closer_count(user) >= MAX_CLOSERS:
        return ""
    # 前置条件全过了就给一句人能读的说明。（早前这里写成「到这里仍 return ""」、
    # 于是整条收场链路是死代码——那个 bug 已修，这里改成直接返回理由。）
    # 返回值只被当作「选不选这个人」的开关（reason 本身自 v1.16.0 起
    # 已不再进 prompt，动机由模型自己产），所以给一句人能读的说明即可。
    return f"她上次主动说的那句过了 {int((now - sent) / 3600)} 小时还没人接"


def recent_exchange(u: Dict[str, Any], limit: int = 3) -> List[str]:
    """最近这几句来回里，对方说过什么。

    「已读续接」要用：对方回过一句（正常聊天链路自己就回了，**不是插件的功劳**）
    之后又没声了，这时候补一句该接着那几句说，而不是干巴巴一句「没事我就是想说说话」。
    """
    out: List[str] = []
    want = int(limit)
    if want <= 0:
        return []
    # 只看最后 `limit * 2` 条，再筛 `dir == "in"`：她连着发三条（她自己接自己的话茬）时，
    # 这几条会把窗口整个占满，一条对方的话都取不到，于是「已读续接」退化成了
    # 「没事我就是想说说话」。改成从尾巴往回**收集够 limit 条对方的话**再正序返回。
    for item in reversed(_conversation(u)):
        if not _is_inbound(item):
            continue
        text = str(item.get("text", "") or "").strip()
        if text:
            out.append(text)
        if len(out) >= want:
            break
    out.reverse()
    return out


def closer_meta(about: str = "", exchange: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """收场/续接那句：明确不要求对方回复，否则又变成一次索取。

    `exchange` 是对方最近说过的话。有它的时候，收场就变成**接着刚才那几句说**，
    而不是凭空找话——真人「对方回过一句然后又没声了」时，补的通常就是那件事的后续。
    """
    turns = [str(t).strip() for t in (exchange or []) if str(t).strip()]
    return {
        "category": "closer",
        "intent": "closer",
        "mode": "closer",
        "msg_type": "share_thought",
        "msg_type_desc": (
            f"顺着刚才那几句接着说（对方说过：{'；'.join(turns)}）"
            if turns else "给自己上次那句没人接的话收个尾"
        ),
        "style_hint": (
            "重点是轻：让TA不用回也没压力；别追问怎么没回，也别道歉。"
            + ("顺着对方刚才那句往下说，别另起话题" if turns else "")
        ),
        # 两边都填：模板用的是 {about}，而 _mode_note 的判据是「该模板实际用到的
        # 占位符有值」。只填一个时另一处会判成没值，把整句引文丢掉。
        "about": about,
        "asked": about,
        "exchange": turns,
    }


# ─── 5. 这条消息说完了吗 ────────────────────────────
#
# 对方发「跟你说个事…」的时候，她要是当完整消息回，就是抢话——人家下文还没来；
# 可要是插件自己把两条合并、或者干脆不开口，那又变成替人做主。所以这里只**判断**：
# 返回三态与判据，不合并、不缓存、不阻塞，等不等、怎么回交给同一轮的模型。
# 判据参考 companion_v2 的 turn_state：判断的是「这句话说完了没有」，不是「能不能答」。
# 「什么是日食？」问得完整，也是 complete。

# 明确表示「还要补充」的说法。中英都有：有人打中文，也有人中英混着打。
# 长词在前：短的落在长的里面时只报长的那一个（见 _marker_hits）。
UNFINISHED_MARKERS = (
    "我还没说完", "还没说完", "没说完呢", "我还没讲完", "还没讲完",
    "等我补充", "等我讲完", "马上补充", "我马上补", "我接着说", "接着跟你说",
    "先别急着回", "先别回", "先别答", "别急着回", "等一下再说", "等下我再",
    "回头再跟你说",
    "i'm not done", "i am not done", "not finished yet", "haven't finished",
    "hold on", "one sec", "wait a sec", "let me finish",
)

# 整条只有起头词（去掉尾巴标点后完全相等）：话刚开了个头。
_HEAD_ONLY = (
    "跟你说个事", "跟你说件事", "跟你说件事儿", "跟你讲个事", "我跟你说个事",
    "我跟你说", "我跟你讲", "问你个事", "问你件事", "告诉你个事",
    "有个事", "有件事", "那个", "这个", "那个啥", "就是那个",
    "我想", "我说", "就是", "其实", "对了", "然后", "但是", "不过",
    "而且", "所以", "因为", "还有", "你猜", "你猜猜",
)
# 以起头词收尾：前面还有点东西，但话停在「那个」「然后」上，也是没说完。
_HEAD_TAIL = (
    "那个", "这个", "那个啥", "我想", "我说", "就是", "其实", "对了", "然后",
    "但是", "不过", "而且", "所以", "因为", "还有", "你猜",
)

# 收尾的悬空标点：省略号、逗号、冒号、破折号——写到这里就是还要往下说。
_ELLIPSIS_TAILS = ("……", "…", "...", "。。。", "。。")
_DANGLING_TAILS = ("，", ",", "、", "：", ":", "——", "—")

# 20 字以内、又以悬空标点收尾的，几乎只可能是「刚开了个头」；长句以省略号收尾
# 更像被截断，只能给 uncertain——看不出她会不会继续，就不能替她等。
_DANGLING_MAX_CHARS = 20
# 4 个字以内、没有句读、也不是起头词：像只打了个头，或者被截断。
_SHORT_MAX_CHARS = 4

_PLACEHOLDER_RE = re.compile(r"^\[[^\[\]]{1,8}\]$")
_HAS_WORD_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_QUESTION_TAILS = ("？", "?", "吗", "么", "呢")
_STRIP_TAIL = "。！!～~… \t\r\n"
_PUNCT_CHARS = "，,。.！!？?；;：:、~～…—"


def _signal(state: str, reason: str, markers: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """三态结果。reason 用稳定 ASCII 码（日志/状态页认它），markers 是命中的原文。"""
    return {"state": state, "reason": reason, "markers": tuple(markers)}


def _marker_hits(low: str) -> Tuple[str, ...]:
    """命中的「还要补充」说法，去掉被更长说法包含的短词。

    英文短语必须整词命中：「someone second」里也含「one sec」，不设边界就会把一句
    正常英文判成「话没说完」。
    """
    hits: List[str] = []
    for marker in UNFINISHED_MARKERS:
        if marker.isascii():
            found = re.search(r"(?<![a-z])" + re.escape(marker) + r"(?![a-z])", low) is not None
        else:
            found = marker in low
        if found and not any(marker in hit for hit in hits):
            hits.append(marker)
    return tuple(hits)


def _tail_token(text: str) -> str:
    for token in _ELLIPSIS_TAILS + _DANGLING_TAILS:
        if text.endswith(token):
            return token
    return ""


def unfinished_signal(text: str) -> Dict[str, Any]:
    """这条消息说完了没有：complete / unfinished / uncertain。

    返回 {"state", "reason", "markers"}。**只看这一条消息**：不看历史、不猜下文，
    也不替调用方决定等不等——那是同一轮模型的事。

    判据（按优先级）：
      - 显式「还要补充」（UNFINISHED_MARKERS）→ unfinished；
      - 纯表情/纯标点 → 没有可判的语义，带省略号的当 uncertain，其余 complete；
      - 问句 → complete（问得完整就是在等她答，不是在等自己补）；
      - 20 字内以悬空标点收尾 → unfinished；更长的同款 → uncertain（像被截断）；
      - 整条只有起头词、或以起头词收尾 → unfinished；
      - 4 字内、无句读 → uncertain（可能是截断，也可能是「嗯」这种极短回应）。

    任何异常都按 complete 放行：宁可正常回一句，也不能因为她这条消息格式怪就
    永远不开口（fail-open）。
    """
    try:
        stripped = str(text or "").strip()
        if not stripped:
            return _signal("complete", "empty")

        low = stripped.lower()
        hits = _marker_hits(low)
        if hits:
            return _signal("unfinished", "explicit_marker", hits)

        if _PLACEHOLDER_RE.match(stripped):
            # 「[图片]」「[表情]」是素材占位，不是她打到一半的话。
            return _signal("complete", "placeholder")

        if not _HAS_WORD_RE.search(stripped):
            # 纯表情/纯标点。只有省略号、逗号这类「还要往下写」的标点才当没说完；
            # 表情包、单个句号都当完整——那本来就不需要接话。
            if _tail_token(stripped) and _tail_token(stripped) in _ELLIPSIS_TAILS + _DANGLING_TAILS:
                return _signal("uncertain", "ellipsis_only")
            return _signal("complete", "non_verbal")

        core = stripped.rstrip(_STRIP_TAIL)
        if core.endswith(_QUESTION_TAILS):
            return _signal("complete", "question")

        tail = _tail_token(stripped)
        if tail:
            # 带问号的先算问句：「你知道吗…」是在问她，不是在等自己补下文。
            if "？" in stripped or "?" in stripped:
                return _signal("complete", "question")
            if len(stripped) <= _DANGLING_MAX_CHARS:
                return _signal("unfinished", "dangling_tail", (tail,))
            return _signal("uncertain", "truncated_ellipsis", (tail,))

        head = stripped.strip("…。，、,：:～~ \t")
        if head in _HEAD_ONLY:
            return _signal("unfinished", "dangling_head", (head,))
        if len(head) <= 12 and any(head.endswith(word) for word in _HEAD_TAIL):
            return _signal("unfinished", "dangling_head", (head,))

        if len(stripped) <= _SHORT_MAX_CHARS and not any(ch in stripped for ch in _PUNCT_CHARS):
            return _signal("uncertain", "short_no_punct")
        return _signal("complete", "complete")
    except Exception:
        return _signal("complete", "error")
