"""入口清洗：把框架注入的内容从「用户说的话」里摘出去。

为什么必须有这一步
----------------
`event.message_str` 不保证只有用户打的字。装了几个插件之后，一条消息里可能夹着：

* Humanoid Core 追加的**身体事实块**——`〔她的身体与生活 v10 uid=xxx〕` 开头，
  后面整段都是关于「她自己」的事实（Core 的 `main.py:126` 写得很清楚：
  「事实块走的是用户消息后面」）；
* AstrBot 或其它插件塞的 `<system_reminder>…</system_reminder>`。

而本插件把 `message_str` **原样**存进 state（`last_message`、`conversation`、
话题提取的输入、给生成器的「对方最后说的是」）。于是三件事同时出错：

1. 下一轮主动消息的提示词里会出现「TA 刚才说：……〔她的身体与生活 v10〕」，
   模型会把一份关于**她自己**的说明书当成对方说的话；
2. 话题与时间锚点提取会把块里的「今天是……」当成用户内容，凭空多出一条由头；
3. 旧块里的版本号与当前状态冲突（v10 的「她刚睡醒」和现在的实际状态同时成立）。

Core 自己清理历史时用的规则就一条：**从 `〔她的身体与生活` 这个标记处截断**
（`humanoid/prompt_builder.py` 的 `MARK_PREFIX`）。这里沿用同一条，不另造标准——
两边认的是同一个标记，改一处不会让另一处失效。

## 清洗强度
只认**已知的、明确的**框架标记，不做「看起来像系统提示就删」那种启发式判断：
删错了就是吞掉用户真说的话，那比留着噪音严重得多。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

# Humanoid Core 的身体事实块标记。只认这个前缀（含版本号与 uid 变体），
# 与 Core 侧 MARK_PREFIX 同源。
# ── 通用「成对尖括号块」 ───────────────────────────────────────────
#
# 逐个记插件名是打地鼠：这个容器里 8 个插件往同一份 prompt 里塞东西，本版清理时
# 已经见过两种（Core 的〔〕块、system_reminder），下一次又冒出来第三个
# （`<MemoryCompanion-Context>`）。而它们都是**成对尖括号包起来的一整块**。
#
# 所以改成一条通吃：**带名字的尖括号块**一律去掉。只认「成对闭合」的——
# 半截尖括号（`<3`、`<=`）不匹配，照常留在正文里。
_GENERIC_BLOCK = re.compile(
    r"<([A-Za-z][A-Za-z0-9_.:-]{2,40})\b[^>]*>.*?</\1\s*>", re.S | re.I
)
# 同理，方括号块。Core 的事实块是〔〕不是 []，但别的插件未必。
_GENERIC_BRACKET_BLOCK = re.compile(r"[\u3014\u3015\u3010\u3011]([^\u3014\u3015\u3010\u3011]{1,40})[\u3014\u3015\u3010\u3011]")

# 兜底：这些只可能出现在框架注入的文本里，正文里出现就是被污染了。
# 逐个记特征也是地鼠，但这条**很便宜**——只在清洗之后扫一遍正文，命中就截断。
_FRAME_MARKERS = (
    "uid=webchat", "uid=", "今天是20", "〔她的身体与生活",
    "你现在在想要不要", "这是一个[插件", "retrieval_intent",
)

CORE_BLOCK_MARK = "〔她的身体与生活"

# 整块剥掉的标签。必须成对闭合才算，不闭合的留着（可能是用户在聊这段文字本身）。
_TAG_BLOCKS = (
    re.compile(r"<system_reminder\b[^>]*>.*?</system_reminder\s*>", re.S | re.I),
    re.compile(r"<system_reminder\b[^>]*/>", re.I),
    # AstrBot 的 @ 提及标记：不是用户打的字
    re.compile(r"\[At:[^\]]{0,64}\]", re.I),
)

# 清洗后残留的连续空行压成一行
_BLANK_RUN = re.compile(r"\n{3,}")


def sanitize_incoming(text: Any) -> str:
    """把一条「用户发来的消息」洗成真正的用户正文。

    Args:
        text: 原始 message_str / 会话库里的历史正文

    Returns:
        清洗后的正文；清洗后为空则返回空串（调用方据此认为这条没有实质内容）
    """
    if not isinstance(text, str):
        return ""
    out = text

    # 1) 身体事实块：从标记处**截断**，后面整段都是框架内容。
    #    Core 也是这么清的（`main.py:126` 的 find + rstrip）。
    idx = out.find(CORE_BLOCK_MARK)
    if idx >= 0:
        out = out[:idx]

    # 2) 成对的框架标签：整块删掉
    for pat in _TAG_BLOCKS:
        out = pat.sub("", out)

    # 3) 任何带名字的尖括号块。这一步是**兜底**，不是逐个记插件名：逐个记是打地鼠，
    #    这个容器里 8 个插件抢同一份 prompt，上一版清理时已经见过
    #    `<system_reminder>`，又冒出来 `<MemoryCompanion-Context>`。成对闭合是它们的
    #    共同形状。半截尖括号（<3、<=）不匹配，照常留在正文里。
    out = _GENERIC_BLOCK.sub("", out)
    out = _GENERIC_BRACKET_BLOCK.sub("", out)

    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = _BLANK_RUN.sub("\n\n", out)

    # 4) 最后一层：正文里还留着只可能来自框架的特征，就从这里截断。
    #    上面几步都是「按块剥」，这一道是**认内容**——万一下一个插件用了我们没见过的
    #    块形状，只要它带着这些特征之一，正文就不会带着它进下一轮。
    for marker in _FRAME_MARKERS:
        i = out.find(marker)
        if i >= 0:
            out = out[:i]
    out = _BLANK_RUN.sub("\n\n", out)
    return out.strip()


def looks_like_framework_only(text: Any) -> bool:
    """整条消息清洗完就没了 —— 说明这条其实只有框架内容，不该建档。

    这种情况在开了 Core 又发生消息合并时很常见：合并进来的可能只有事实块。
    """
    original = text if isinstance(text, str) else ""
    return bool(original.strip()) and not sanitize_incoming(original)


def sanitize_history_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """历史导入的一行。顺带丢掉「只有框架内容」的行。"""
    text = sanitize_incoming(row.get("text"))
    if not text:
        return {}
    out = dict(row)
    out["text"] = text
    return out


def sanitize_user_record(u: Dict[str, Any]) -> bool:
    """就地洗一个用户记录里的所有正文。返回是否改动了什么。

    迁移用：盘上已经存着的那些是清洗规则上线之前录进去的。
    """
    changed = False

    text = sanitize_incoming(u.get("last_message"))
    if text != u.get("last_message"):
        u["last_message"] = text
        changed = True

    pending = u.get("pending_proactive_context")
    if isinstance(pending, str) and pending:
        cleaned = sanitize_incoming(pending)
        if cleaned != pending:
            u["pending_proactive_context"] = cleaned
            changed = True

    conv = u.get("conversation")
    if isinstance(conv, list):
        for item in conv:
            if not isinstance(item, dict):
                continue
            text = sanitize_incoming(item.get("text"))
            if text != item.get("text"):
                item["text"] = text
                changed = True
        # 清洗后变空的条目直接去掉，别留一条空消息在上下文里
        kept: List[Dict[str, Any]] = [i for i in conv if isinstance(i, dict)
                                      and str(i.get("text", "") or "").strip()]
        if len(kept) != len(conv):
            u["conversation"] = kept
            changed = True

    log = u.get("proactive_log")
    if isinstance(log, list):
        for item in log:
            if not isinstance(item, dict):
                continue
            text = sanitize_incoming(item.get("text"))
            if text != item.get("text"):
                item["text"] = text
                changed = True

    # 话题是从正文里提的，污染过的正文提出来的话题同样该清一遍
    topics = u.get("topics")
    if isinstance(topics, list):
        cleaned_topics = []
        for t in topics:
            t = sanitize_incoming(t) if isinstance(t, str) else t
            if isinstance(t, str) and t.strip() and t not in cleaned_topics:
                cleaned_topics.append(t)
        if cleaned_topics != topics:
            u["topics"] = cleaned_topics
            changed = True

    return changed


def sanitize_state(state: Dict[str, Any]) -> int:
    """就地洗整个 state。返回被清洗的用户条数。"""
    bots = state.get("bots")
    if not isinstance(bots, dict):
        return 0
    touched = 0
    for bot in bots.values():
        if not isinstance(bot, dict):
            continue
        users = bot.get("users")
        if not isinstance(users, dict):
            continue
        for u in users.values():
            if isinstance(u, dict) and sanitize_user_record(u):
                touched += 1
    return touched
