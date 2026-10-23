"""把社交层的近况写回给 Humanoid Core。

两边各写各的文件：Core 写它的 state.json（内含 contract），这里写 `humanoid_signals.json`。
没有并发写同一个文件的问题，Core 那边只读，并且超过 15 分钟就不采信。

写回的都是 Core 自己算不出来、但会影响她「现在是什么状态」的：刚替她主动开过口
（想说的心思该泄掉）、被谁连着冷落了几次（会变成一句压着的挂念）、她手上还开着的事
（对话历史被压缩后，她仍然记得自己答应过谁什么）。

v1.19.0（多 Bot）：
- 文件里多一份 `by_bid: {bid: {...}}`，每个角色各记各的。这是**为将来准备的**：Core
  那边升级成按角色读之后，不用再改这个文件。
- 顶层那几个字段是 Core 目前真正会读的，而 Core 每个角色实例都读同一个文件、且不按
  角色过滤——于是「A 主动发了一条」会让 B 的 Core 也以为刚说过话，把 B 的
  social_desire 也泄掉 40。**多 Bot 时干脆不写顶层**（单 Bot 照旧拿满收益），
  代价只有一个：多 Bot 下没人替 social_desire 泄，它更容易顶格，而现有的
  mood_multiplier 仍会限速。

v2.24.0：
- 心跳改成**无条件重写**。Core 判信号过期的阈值是 900 秒，而插件的心跳默认
  8~15 分钟才跑一次；原实现里内容没变就靠 120 秒闸门跳过，于是 mtime 最长能拖到
  15 分钟以上不刷新。Core 于是周期性判 stale，把 ignored_streak 直接读成 0——
  「被冷落几次」在默认配置下本来就是会自己清零的。
- 删掉 `desire`：Core 全项目没有任何一处读它，而且它写回去的就是 Core 自己通过
  契约给过来的 social_desire，一个没人接收的闭环，还让每轮心跳多触发一次文件重写。
- 写失败不再静默：记下来，状态页能看见（原来返回 False 就完事，表现是社交侧一切
  正常、Core 侧永远显示「装了个寂寞」）。

v1.27.31（schema 1 → 2）：
- 多写一件事：`open_items` —— 她手上**还开着**的事（对方提了没下文的、她自己许下还没
  兑现的）。Core 那边把它渲染成「昨天/三天前」的相对时间放进 system_prompt 的稳定段，
  于是对话历史被压缩之后，她还记得自己手上开着什么。
- `schema` 升到 2 是**只加不改**：既有字段名与语义一个都没动，两边版本可以互相容忍
  （旧 Core 忽略多出来的字段，新 Core 在字段缺失时按「没有开着的事」处理）。
- 这条通道的方向是单向的：`open_items` 由社交层算好发出去，Core **不许**把它当成
  「她最近做过什么」再喂回来——那会形成闭环（见 `core_bridge` 里 `life_log` 只认 `did`）。

v1.28.4：
- 多写一件事：`last_proactive_text` —— 最近一次主动开口的**原文**。
  为什么需要它：主动消息走 `context.send_message`，不经过 AstrBot 的 respond 阶段，
  Core 侧收不到任何事件（`after_message_sent` 只在 respond 阶段触发，接不到它）。
  Core v2.28.4 起按这个字段把「她刚主动说了什么」补记进 `mood.spoke_log`，
  并推进这个人的 `last_interaction`。
- 只加不改：旧 Core 忽略这个字段；旧社交插件没有它时 Core 按「只知道她开过口」
  降级，不会拿占位文本污染 `spoke_log`。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Callable

FILE_NAME = "humanoid_signals.json"

# ignored_by 按 uid 记，永不清理的话会随用户数**单调增长**，而这个文件每轮心跳
# 全量重写。超出上限时丢掉最久没更新的条目（dict 保序，更新会把它移到末尾）。
_IGNORED_BY_MAX = 500

# 发给 Core 的「她手上还开着的事」条数上限。这段会被渲染进 system_prompt 的**稳定段**
# （靠前缀缓存省成本），条数直接换算成每轮固定开销，所以宁可少给：她同时开着的正事
# 本来也就一两件。
MAX_OPEN_ITEMS = 4
# 单条字数上限。Core 侧还会再截，这里先截是为了别把这个每轮全量重写的文件写大。
OPEN_ITEM_TEXT_MAX = 40
# 主动消息原文的上限。Core 的 `mood.note_spoke()` 本来就只留 200 字，这里再大没有意义；
# 这个文件每轮心跳全量重写，一条主动消息的原文不该让它持续变大。
PROACTIVE_TEXT_MAX = 200
# 允许的 kind。写死白名单而不是原样透传：Core 按 kind 分类渲染，一个拼错的 kind 会落进
# 「不认识」的分支，症状是那条事项静默消失，而这边看起来一切正常。
_OPEN_ITEM_KINDS = ("loop", "promise")


class SignalsWriter:
    def __init__(
        self,
        path: str,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self._time = time_source
        self._payload: dict[str, Any] = {
            "schema": 2,
            "last_proactive_at": 0.0,
            "last_target_uid": "",
            # 主动消息的原文（v1.28.4）：Core 靠它把这句话补记进情绪账本。
            "last_proactive_text": "",
            "ignored_streak": 0,
            # 她手上还开着的事（见 note_open_items）。默认写空列表而不是省略这个键：
            # 「没有开着的事」是一个**有意义的结论**，Core 靠它判断「这件事结了没有」。
            "open_items": [],
            # 每个角色各记各的（Core 将来按角色读时直接用）
            "by_bid": {},
        }
        self._written_at = 0.0
        self._dirty = True
        self._last_error: str = ""
        # 角色数：>1 时不写会串台的顶层字段
        self._roles = 1
        # 我们自己的保鲜期（秒），写进文件让 Core 按它判断过期。
        # 0 = 还没声明，Core 用它自己的默认值。
        self._ttl_seconds = 0.0

    def set_ttl_seconds(self, seconds: float) -> None:
        """声明「这份信号多久之内算新鲜」，由**写方**决定。

        Core 那边的过期阈值原来是写死的 900 秒，而这里的心跳间隔是**可配的**
        （`heartbeat_max_minutes`：默认 15 分钟、最大 120 分钟）。`beat()` 每轮都重写
        mtime 来对抗这件事，但只要心跳间隔超过 15 分钟，Core 就会在两次心跳之间
        把整份信号判成过期、当成没有——被冷落计数读成 0，刚主动开过口也当没发生。
        所以把真实保鲜期写进文件（`ttl_seconds`），让对面按我们的节奏算。
        """
        try:
            value = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return
        if value == self._ttl_seconds:
            return
        self._ttl_seconds = value
        self._dirty = True

    def _slot(self, bid: str, *, create: bool = True) -> dict[str, Any]:
        """按角色取那一格。`create=False` 是给**读路径**用的。

        读路径（`ignored_by_uid`）以前也走 `setdefault`：查一个人就凭空建出一个空
        slot 并改 payload，而 `beat()` 的 `flush(force=True)` 是无条件整份重写的，
        于是这个空壳被写进文件。查一个不存在的人不该在盘上留下痕迹。
        """
        by_bid = self._payload.get("by_bid")
        if not isinstance(by_bid, dict):
            if not create:
                return {}
            by_bid = {}
            self._payload["by_bid"] = by_bid
        key = str(bid or "")
        slot = by_bid.get(key)
        if isinstance(slot, dict):
            return slot
        if not create:
            return {}
        slot = {"last_proactive_at": 0.0,
                "last_target_uid": "",
                "last_proactive_text": "",
                "ignored_streak": 0,
                "open_items": []}
        by_bid[key] = slot
        return slot

    def set_role_count(self, count: int) -> None:
        """当前有几个角色在跑。>1 时不再写会串台的顶层字段（见模块说明）。

        从 1 变 2 时还要把**已经写下的**顶层标量清掉：它们是单角色时留下的，而 Core
        每个角色实例都读同一份文件、不按角色过滤（见模块说明），留着的话 B 会读到 A 的
        「刚跟谁说过话 / 被冷落了几次」。`open_items` 那格原先只在 `note_open_items` 里
        处理，这里一并补上，免得第二个角色在下次心跳之前先读到 A 的事。
        """
        was = max(0, int(getattr(self, "_roles", 1) or 1))
        self._roles = max(0, int(count))
        if self._roles > 1 and was <= 1:
            for key, empty in (("last_proactive_at", 0.0),
                               ("last_target_uid", ""),
                               ("last_proactive_text", ""),
                               ("ignored_streak", 0),
                               ("open_items", [])):
                if self._payload.get(key) != empty:
                    self._payload[key] = empty
        self._dirty = True

    def note_proactive(self, target_uid: str, at: float | None = None,
                       bid: str = "", text: str = "") -> None:
        """一条主动消息真的发出去了。

        `text` 是**真的发出去的那句原文**（v1.28.4）。Core 收不到任何主动消息事件，
        只能靠这里把话带过去；缺了它，Core 只知道「她开过口」，不知道说了什么，
        `spoke_log` 就补不上。

        原文截到 `PROACTIVE_TEXT_MAX`：Core 那边 `note_spoke()` 也只留 200 字，
        而这个信号文件每轮心跳全量重写，留着长文只会让每次心跳多写几 KB。
        """
        try:
            when = float(self._time() if at is None else at)
        except (TypeError, ValueError):
            # at 是调用方传进来的值，坏值不该让「刚替她开过口」这件事整个丢掉
            when = float(self._time())
        body = str(text or "")[:PROACTIVE_TEXT_MAX]
        slot = self._slot(bid)
        slot["last_proactive_at"] = when
        slot["last_target_uid"] = str(target_uid or "")
        slot["last_proactive_text"] = body
        if getattr(self, "_roles", 1) <= 1:
            # 单 Bot：Core 只会把这一个角色的消息认成自己的，照旧写顶层
            self._payload["last_proactive_at"] = when
            self._payload["last_target_uid"] = str(target_uid or "")
            self._payload["last_proactive_text"] = body
        self._dirty = True
        self.flush()

    def note_ignored(self, target_uid: str, streak: int, bid: str = "") -> None:
        """「主动消息发出去、没被回」——**按人记**。

        原来只有角色级的 `ignored_streak`（她被冷落了几次），而好感是**用户级**的：
        Core 那边能把冷落接到身体（社交能量）上，接不到具体某个人。
        少了这个 per-user 计数，「被冷落」就没法换算成「对 TA 的好感下降」——
        只能知道「我最近有点冷清」，不知道是谁。
        """
        uid = str(target_uid or "").strip()
        if not uid:
            return
        try:
            value = max(0, int(streak))
        except (TypeError, ValueError):
            return
        slot = self._slot(bid)
        per = slot.get("ignored_by")
        if not isinstance(per, dict):
            per = {}
        try:
            before = int(per.get(uid) or 0)
        except (TypeError, ValueError):
            # 盘上旧值可能是字符串/对象；按「还没有记录」处理，别让整轮心跳抛
            before = 0
        # 先删再插：让最近更新的条目排在末尾，超限时从头（最久没更新）丢。
        #
        # 这一步必须在 `before == value` 的提前返回**之前**做：同一个 streak 被反复确认
        # 是这条路径上最常见的情形（每轮心跳都报同一个数），值没变，但它正是「这个人还
        # 在被记着」的证据，位置必须刷新——否则最活跃的那个人永远停在队首，`_IGNORED_BY_MAX`
        # 满了先被淘汰的偏偏是他。
        per.pop(uid, None)
        per[uid] = value
        while len(per) > _IGNORED_BY_MAX:
            per.pop(next(iter(per)), None)
        slot["ignored_by"] = per
        if before == value:
            # 值没变：只留着重排后的顺序在内存里，交给下一次真实写入（或 `beat()` 的
            # 强制 flush）落盘，不为一个位置变化单独触发一次写。
            return
        self._dirty = True
        self.flush()

    def ignored_by_uid(self, uid: str, bid: str = "") -> int:
        """给 Core 读：这个人在社交层的累计「没理我」次数。"""
        slot = self._slot(bid, create=False)
        per = slot.get("ignored_by")
        if not isinstance(per, dict):
            return 0
        try:
            return max(0, int(per.get(str(uid or "")) or 0))
        except (TypeError, ValueError):
            return 0

    def set_ignored_streak(self, streak: int, bid: str = "") -> None:
        try:
            value = max(0, int(streak))
        except (TypeError, ValueError):
            return
        slot = self._slot(bid)
        try:
            current = int(slot.get("ignored_streak") or 0)
        except (TypeError, ValueError):
            current = 0
        if current == value:
            return
        slot["ignored_streak"] = value
        if getattr(self, "_roles", 1) <= 1:
            self._payload["ignored_streak"] = value
        self._dirty = True
        self.flush()

    def note_open_items(self, items: Any, bid: str = "") -> None:
        """把「她手上还开着的事」整份发布出去（每轮心跳重算并**整体覆盖**）。

        为什么必须整体覆盖、而且空列表也有意义：Core 靠这份列表判断「这件事结了没有」。
        只增不减的话，一件对方早就给出结果的事会永远挂在她提示词里——正是「过程系统
        锁死」那个老毛病的另一个入口。所以没有开着的事时也要写 `[]`，而不是保留上一轮。

        `at` 一律取这件事**被记下的那一刻**（loop 的 `at`、promise 的 `at`），绝不能盖成
        now：Core 把它渲染成「三天前答应的事」这种相对时间，而这段文本进的是 system_prompt
        的稳定段。每轮重盖会让语义漂移（三天前的事变成「刚答应的」），并且文本每轮都变，
        把整段前缀缓存打掉——比不发布更贵。
        """
        normalized = self._normalize_open_items(items)
        slot = self._slot(bid)
        # 多 Bot 时顶层那格必须留空：顶层以前被单 Bot 那条路径写过（角色数从 1 变 2 之后
        # 它不会自己消失），留着的话 Core 会把 A 手上开着的事认成 B 自己的。
        top = normalized if getattr(self, "_roles", 1) <= 1 else []
        if slot.get("open_items") == normalized \
                and self._payload.get("open_items") == top:
            # 内容没变就不标脏：`flush` 的 120 秒闸门本来就是为这个准备的，
            # 别让每轮心跳都白重写一遍整份文件。
            return
        slot["open_items"] = normalized
        self._payload["open_items"] = top
        self._dirty = True
        self.flush()

    @staticmethod
    def _normalize_open_items(items: Any) -> list[dict[str, Any]]:
        """规范化 + 截断 + 排序，产出**可逐字节比较**的稳定列表。

        排序必须确定（at 降序，同 at 再按 kind/text/uid）：同一批内容每轮渲染出不同顺序
        = 文本每轮都变 = Core 那边的前缀缓存整段失效。所以既不能依赖 dict/set 的迭代顺序，
        也不能把调用方给的顺序当数——去重也放在排序**之后**，「留下哪条」才与传入顺序无关。
        """
        if not isinstance(items, (list, tuple)):
            return []
        out: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            # 空白折叠：Core 按整行渲染，一条里带换行会把后面几行挤出它自己的格式
            text = " ".join(str(raw.get("text", "") or "").split())
            text = text[:OPEN_ITEM_TEXT_MAX]
            if not text:
                continue
            kind = str(raw.get("kind", "") or "").strip()
            if kind not in _OPEN_ITEM_KINDS:
                continue
            try:
                at = float(raw.get("at", 0) or 0)
            except (TypeError, ValueError):
                at = 0.0
            if at != at:                # NaN：坏时间戳当 0，别让它污染排序（NaN 比较恒假）
                at = 0.0
            out.append({
                "text": text,
                "uid": str(raw.get("uid", "") or ""),
                "at": at,
                "kind": kind,
            })
        out.sort(key=lambda i: (-i["at"], i["kind"], i["text"], i["uid"]))
        # 同一件事可能从两个来源各来一次（她许的诺也可能被抽成未完话题）：
        # 按 (kind, text) 去重，排序之后的第一条胜出。
        seen: set = set()
        uniq: list[dict[str, Any]] = []
        for item in out:
            key = (item["kind"], item["text"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(item)
        return uniq[:MAX_OPEN_ITEMS]

    def beat(self) -> bool:
        """每个周期无条件重写一遍：内容没变也要刷新 mtime。

        这个文件的全部价值就在于 Core 那边按 mtime 判断「社交层还活着吗」。内容没变
        就不写的话，mtime 会被心跳间隔拖过 Core 的过期阈值，Core 读到 stale 就把一切
        当没有——被冷落计数被读成 0，刚主动开过口也当没发生过。

        心跳间隔是可配的（最大 120 分钟），所以除了每轮重写 mtime，`flush` 还会把
        我们真实的保鲜期写进 `ttl_seconds`，让 Core 按我们的节奏判过期（见
        `set_ttl_seconds`）。这一个几百字节的小文件，每轮心跳重写一次不算什么。
        """
        return self.flush(force=True)

    def last_error(self) -> str:
        """最近一次写失败的原因；空串表示现在写得进去。"""
        return self._last_error

    def flush(self, force: bool = False) -> bool:
        """内容真变了就立刻写；没变时只在心跳里刷新 mtime。"""
        now = self._time()
        if not force and not self._dirty and now - self._written_at < 120.0:
            return False
        payload = dict(self._payload)
        payload["updated_at"] = round(now, 1)
        if self._ttl_seconds > 0:
            # 让 Core 按我们真实的保鲜期算过期，而不是它写死的 900 秒。
            payload["ttl_seconds"] = round(self._ttl_seconds, 1)
        directory = os.path.dirname(self.path) or "."
        tmp: str | None = None
        try:
            os.makedirs(directory, exist_ok=True)
            handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                writer = os.fdopen(handle, "w", encoding="utf-8")
            except BaseException:
                # fdopen 失败时句柄还没被接管，得自己关掉，避免泄漏 fd
                try:
                    os.close(handle)
                except OSError:
                    pass
                raise
            with writer:
                json.dump(payload, writer, ensure_ascii=False, separators=(",", ":"))
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(tmp, self.path)
            tmp = None
            # fsync 文件只保证内容落盘；os.replace 的目录项变更要 fsync 父目录才算落地
            # （Windows 上目录 fsync 不可用，必须容错）
            try:
                dfd = os.open(directory, os.O_RDONLY)
            except OSError:
                dfd = -1
            if dfd >= 0:
                try:
                    os.fsync(dfd)
                except OSError:
                    pass
                finally:
                    try:
                        os.close(dfd)
                    except OSError:
                        pass
        except Exception as exc:
            # 原来只捕获 OSError：json.dump 抛 TypeError 或 flush 抛别的异常时，
            # tmp 已创建且**永不删除**，而 beat() 每轮心跳都调 → 每 8~15 分钟泄漏
            # 一个临时文件；而且 _last_error 不会被设置，状态页上什么都看不到。
            self._last_error = f"{type(exc).__name__}: {exc}"
            return False
        finally:
            # 成功时 tmp 已被 os.replace 消费掉（tmp 置 None）；失败/异常时清理
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        self._last_error = ""
        self._written_at = now
        self._dirty = False
        return True
