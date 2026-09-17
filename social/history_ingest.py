"""播种用户：让插件不必等「先聊过」才认识一个人。

v1.9.0 之前，插件的用户池只有一条入口：消息观察。谁私聊过 bot，谁才进 state.json，
念头才会在 TA 身上攒起来。刚装好的插件眼里一个人都没有，自主社交无从谈起。

这里补两条入口，都不依赖插件装好后又聊过一句：

- 历史导入：AstrBot 自己的会话数据库（data/data_v4.db，conversations 表）里存着
  装插件之前聊过的所有会话。启动时与每半小时读一次（只读，不写），把还没进过
  state.json 的私聊对象导进来：umo、聊过多少条、最后活跃时间。导进来的人
  立刻开始攒念头——攒满的速度取决于对方最后一次说话是多久之前。
- 播种名单（seed_users）：连历史都没有的人也能写进名单：她按较慢的节奏
  （urge_scale=0.5）慢慢攒，攒到点就走正常的闸门与模型把关，主动找上门。
  名单里的人被冷落够多次后自然被压下去，不会变成骚扰。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .state import SocialState

# AstrBot 数据目录下的会话数据库文件名：v4 起是 data_v4.db，更早期 4.x 是 data_v3.db
_DB_FILENAMES = ("data_v4.db", "data_v3.db")

# 一次导入最多看多少个会话。按更新时间倒序，足够覆盖「最近活跃的人」，
# 又不至于把数据库里成千上万的旧会话全部拉进插件状态
_MAX_CONVERSATIONS = 300

# umo 里表示「私聊」的 message_type 写法（各平台不完全统一，全部认）
_PRIVATE_TYPES = {"private", "p2p", "c2c", "friend", "single", "1:1"}
_GROUP_TYPES = {"group", "g"}


def find_astrbot_db(data_dir: Optional[str]) -> Optional[str]:
    """找 AstrBot 会话数据库文件，找不到返回 None。"""
    if not data_dir:
        return None
    for name in _DB_FILENAMES:
        path = os.path.join(data_dir, name)
        if os.path.isfile(path):
            return path
    return None


def parse_umo(umo: str) -> Tuple[str, str, str]:
    """拆统一消息来源。

    AstrBot 的 umo 形如 ``aiocqhttp:private:12345``（platform:message_type:session_id）。
    拆不开时按最后一节当用户 ID，前缀原样保留，发送目标不会丢。
    """
    parts = str(umo or "").split(":")
    if len(parts) >= 3:
        return parts[0], parts[1].lower(), ":".join(parts[2:])
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return "", "", str(umo or "")


def umo_kind(umo: str) -> str:
    """private / group / unknown。"""
    _, msg_type, _ = parse_umo(umo)
    if msg_type in _PRIVATE_TYPES:
        return "private"
    if msg_type in _GROUP_TYPES:
        return "group"
    return "unknown"


def _to_ts(value: Any) -> float:
    """把数据库里的 updated_at 转成 unix 时间戳，读不出来给 0。

    兼容三种存法：epoch 数字、ISO 字符串（Z 后缀、带时区、SQLite 的
    「YYYY-MM-DD HH:MM:SS(.ffffff)±HH:MM」空格分隔写法）。"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        try:
            return float(s)
        except ValueError:
            pass
        try:
            from datetime import datetime

            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            elif len(s) >= 11 and s[10] == " ":
                s = s[:10] + "T" + s[11:]  # SQLite 的空格分隔写法
            return float(datetime.fromisoformat(s).timestamp())
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def _message_text(item: Any) -> str:
    """从一条会话消息里取纯文本。content 可能是字符串，也可能是分段列表。"""
    if not isinstance(item, dict):
        return ""
    content = item.get("content", item.get("text", ""))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for seg in content:
            if isinstance(seg, str):
                parts.append(seg)
            elif isinstance(seg, dict):
                t = seg.get("text") or seg.get("content")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return ""


def count_user_messages(content: Any) -> int:
    """会话里的用户消息条数（驱动「聊过多少」的熟悉度）。"""
    if not isinstance(content, list):
        return 0
    n = 0
    for item in content:
        if isinstance(item, dict) and str(item.get("role", "")).lower() == "user":
            n += 1
    return n


def last_user_text(content: Any) -> str:
    """会话里最后一条用户消息的文本（没有就是空串）。"""
    if not isinstance(content, list):
        return ""
    for item in reversed(content):
        if isinstance(item, dict) and str(item.get("role", "")).lower() == "user":
            return _message_text(item).strip()
    return ""


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """只读连接：插件永远不写 AstrBot 的库。"""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)


def iter_conversations(
    db_path: str, limit: int = _MAX_CONVERSATIONS
) -> Iterator[Dict[str, Any]]:
    """按更新时间倒序逐个会话读出 user_id(umo)、updated_at、content。

    表不存在 / 库打不开时静默结束（调用方记一次日志），不抛异常。
    """
    try:
        conn = _connect_ro(db_path)
        try:
            cursor = conn.execute(
                "SELECT user_id, updated_at, content FROM conversations "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
            for row in cursor:
                umo, updated, raw = row
                try:
                    content = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, json.JSONDecodeError):
                    content = None
                yield {"umo": str(umo or ""), "updated_at": _to_ts(updated), "content": content}
        finally:
            conn.close()
    except sqlite3.Error:
        return


def _seed_target_bid(state: SocialState) -> str:
    """播种进哪个 bot 名下。

    只有一个角色就归 TA；多个角色时归 "default"，等第一条真实消息进来
    migrate_default_bot 会把这批人搬到真实 bot_id 下。
    """
    bots = [k for k in state.data.get("bots", {}) if k != "default"]
    if len(bots) == 1:
        return bots[0]
    return "default"


def _uid_of(umo: str) -> str:
    _, _, session = parse_umo(umo)
    return session or str(umo)


def _existing_uids(state: SocialState) -> set:
    out = set()
    for bot in state.data.get("bots", {}).values():
        out.update((bot or {}).get("users", {}).keys())
    return out


def seed_from_history(
    state: SocialState,
    data_dir: Optional[str],
    *,
    private_only: bool = True,
    store_text: bool = True,
    now: Optional[float] = None,
) -> int:
    """从 AstrBot 会话数据库导入还没进插件状态的历史用户。

    已在 state.json 里的用户一律不动：插件装好之后攒下的实时数据永远比数据库里的
    旧快照新。返回新导入的人数。
    """
    db_path = find_astrbot_db(data_dir)
    if not db_path:
        return 0
    now = float(now if now is not None else time.time())
    existing = _existing_uids(state)
    bid = _seed_target_bid(state)
    added = 0
    for conv in iter_conversations(db_path):
        umo = conv["umo"]
        kind = umo_kind(umo)
        if private_only and kind == "group":
            continue
        uid = _uid_of(umo)
        if not uid or uid in existing:
            continue
        u = state.user(bid, uid)
        u["umo"] = umo
        u["source"] = "history"
        ts = conv["updated_at"]
        u["last_seen"] = ts if ts > 0 else now
        # 念头从「最后聊天的时刻」开始攒：三天前聊过的人，攒不满三个小时就找上门
        u["urge_at"] = ts if ts > 0 else now
        content = conv["content"]
        if content is not None:
            u["message_count"] = min(count_user_messages(content), 500)
            if store_text:
                last = last_user_text(content)
                if last:
                    u["last_message"] = last[-500:]
        existing.add(uid)
        added += 1
    if added:
        state.mark_dirty()
    return added


def normalize_seed_entries(raw: Any, default_platform: str) -> List[Tuple[str, str, str]]:
    """把 seed_users 配置归一成 (platform, user_id, msg_type) 列表。

    每项接受三种写法：
    - ``12345``：用户 ID，平台取默认（seed_platform）
    - ``aiocqhttp:12345``：带平台的用户 ID
    - ``aiocqhttp:private:12345``：完整 umo，照用
    """
    items: Any = raw
    if isinstance(raw, str):
        items = raw.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
    elif not isinstance(raw, (list, tuple)):
        items = [raw] if raw else []

    out: List[Tuple[str, str]] = []
    seen: set = set()
    for x in items:
        if x is None:
            continue
        s = str(x).strip()
        if not s:
            continue
        parts = s.split(":")
        if len(parts) >= 3:
            platform, msg_type, uid = parts[0], parts[1], ":".join(parts[2:])
        elif len(parts) == 2:
            platform, uid = parts[0], parts[1]
        else:
            platform, uid = default_platform, s
        platform, msg_type, uid = (
            platform.strip() or default_platform,
            msg_type.strip().lower() if len(parts) >= 3 else "private",
            uid.strip(),
        )
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append((platform, uid, msg_type))
    return out


def apply_seed_list(
    state: SocialState,
    raw_entries: Any,
    default_platform: str,
    *,
    now: Optional[float] = None,
) -> int:
    """把播种名单写成插件用户：没历史也能被她主动找。

    名单里的人按 urge_scale=0.5 攒念头（比正常节奏慢一倍），被冷落够多次后
    天花板机制会自然把她压下去。返回新增的人数。
    """
    now = float(now if now is None else time.time())
    existing = _existing_uids(state)
    bid = _seed_target_bid(state)
    added = 0
    for entry in normalize_seed_entries(raw_entries, default_platform):
        platform, uid, msg_type = entry
        if uid in existing:
            continue
        umo = f"{platform}:{msg_type}:{uid}"
        u = state.user(bid, uid)
        if not u.get("umo"):
            u["umo"] = umo
        u["source"] = u.get("source") or "seed"
        if float(u.get("last_seen", 0) or 0) <= 0:
            u["last_seen"] = now
            u["urge_at"] = now
        # 没聊过的人，攒念头的速度打五折
        u["urge_scale"] = 0.5
        existing.add(uid)
        added += 1
    if added:
        state.mark_dirty()
    return added
