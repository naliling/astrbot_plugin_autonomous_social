"""自主拟人社交 —— AstrBot 适配层。

v1.14.3：主动消息上下文改为一次性注入，生成时合并双向对话，回复时临时回填。
优先读 Humanoid Core v2.14 导出的契约快照（身体轴 + 体感 + 说话形式），
并把「刚主动找过谁」「被冷落几次」写回独立的信号文件；拿不到契约时退回旧字段。
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional, Tuple

try:
    # AstrBot v4 新版 API
    from astrbot.api.star import Context, Star
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.event.filter import EventMessageType, command, event_message_type
    from astrbot.api import logger, AstrBotConfig
except ImportError:
    # 兼容旧版导入路径
    from astrbot.api.star import Context, Star
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api import logger, AstrBotConfig
    command = filter.command
    event_message_type = filter.event_message_type
    EventMessageType = filter.EventMessageType

# 「消息发出去之后」这个钩子是未完话题判定的地基：没有它，插件只看得见对方说的话，
# 永远不知道她上一句问了什么。老版本没有这个钩子时退化成空装饰器——功能照常运行，
# 只是追问会明显变弱（启动日志里会说清楚）。
after_message_sent = None
try:
    from astrbot.api.event.filter import after_message_sent  # type: ignore[assignment]
except Exception:
    try:
        # 不依赖顶部只在旧路径才绑定的 filter，直接重新导入 filter 模块，避免 NameError 被吞
        from astrbot.api.event import filter as _filter_mod  # type: ignore[assignment]
        after_message_sent = getattr(_filter_mod, "after_message_sent", None)
    except Exception as _e:
        logger.info(f"[autonomous_social] 未找到 after_message_sent 钩子（{_e}），追问会变弱")
        after_message_sent = None

if after_message_sent is None:
    # 空装饰器：handler 照样定义，只是永远不会被框架调到
    def after_message_sent(_func=None, **_kwargs):
        if _func is not None:
            return _func

        def _decorator(func):
            return func

        return _decorator

    HOOK_AFTER_SENT = False
else:
    HOOK_AFTER_SENT = True

# on_llm_request 钩子：把主动消息作为本轮临时上下文补回 LLM 请求。
# 主动消息走 context.send_message，不经过 respond 阶段，因此不会自动进入会话历史。
# 新版用 TextPart.mark_as_temp()：本轮模型看得到，但 AstrBot 不会把它持久化。
# 老版本没有临时块 API 时，才退回追加 req.contexts。
on_llm_request = None
try:
    from astrbot.api.event.filter import on_llm_request  # type: ignore[assignment]
except Exception:
    try:
        from astrbot.api.event import filter as _filter_mod2  # type: ignore[assignment]
        on_llm_request = getattr(_filter_mod2, "on_llm_request", None)
    except Exception as _e:
        logger.info(f"[autonomous_social] 未找到 on_llm_request 钩子（{_e}），回复主动消息时可能失忆")
        on_llm_request = None

if on_llm_request is None:
    def on_llm_request(_func=None, **_kwargs):
        if _func is not None:
            return _func

        def _decorator(func):
            return func

        return _decorator

    HOOK_LLM_REQUEST = False
else:
    HOOK_LLM_REQUEST = True

# ProviderRequest：用于类型提示，拿不到就用 Any
try:
    from astrbot.api.provider import ProviderRequest  # type: ignore[assignment]
except Exception:
    from typing import Any as ProviderRequest  # type: ignore[misc, assignment]

try:
    from astrbot.core.agent.message import TextPart  # type: ignore[assignment]
except Exception:
    TextPart = None  # type: ignore[assignment,misc]

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:
    get_astrbot_data_path = None

from .social import __version__
from .social.config import migrate_legacy_defaults
from .social.engine import SocialEngine

# 插件数据子目录
DATA_SUBDIR = ("plugin_data", "astrbot_plugin_autonomous_social")


class AutonomousSocial(Star):
    """自主拟人社交插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._engine: Optional[SocialEngine] = None
        self._task: Optional[asyncio.Task] = None

        # 解析数据目录
        data_dir = self._resolve_data_dir()
        state_path = os.path.join(data_dir, *DATA_SUBDIR, "state.json")

        # 旧配置里停在旧默认值上的项提升一次：AstrBot 只补默认值、从不覆盖已有值，
        # 不调这一步的话，升级后老用户实际跑的仍是上一版的节奏与长度
        try:
            changed = migrate_legacy_defaults(
                config, os.path.dirname(state_path), __version__
            )
            if changed:
                logger.info(
                    "[autonomous_social] 这些配置从未被手动改过、但停在旧版本的默认值上，"
                    "已提升到本版默认：" + "；".join(changed)
                )
        except Exception as e:
            logger.warning(f"[autonomous_social] 配置默认值迁移失败（不影响运行）: {e}")

        self._engine = SocialEngine(
            context=context,
            config=config,
            state_path=state_path,
            data_dir=data_dir,
        )

    def _resolve_data_dir(self) -> str:
        """解析 AstrBot 数据目录，兼容多种获取方式。"""
        # 方式1: 优先使用官方 API
        if get_astrbot_data_path is not None:
            try:
                path = get_astrbot_data_path()
                if path and os.path.isdir(path):
                    return path
            except Exception:
                pass

        # 方式2: 从 context 获取（新版 AstrBot）
        try:
            if hasattr(self.context, "get_plugin_data_dir"):
                path = self.context.get_plugin_data_dir()
                if path:
                    # get_plugin_data_dir 返回插件专属目录 .../data/plugin_data/<插件名>，
                    # 目标是 .../data。先看直接父级是不是 plugin_data，是则回退两级拿 data。
                    # （旧写法先 dirname 两次拿到 data 再比 basename=="plugin_data"，永远为假，
                    # 于是总掉到方式3 拿进程 cwd 下的 data，部署目录不对时 state 会写错地方。）
                    plugin_parent = os.path.dirname(path)
                    if os.path.basename(plugin_parent) == "plugin_data":
                        return os.path.dirname(plugin_parent)
                    # 不是预期的 plugin_data 布局时，退一级也好过直接用 cwd
                    return plugin_parent
        except Exception:
            pass

        # 方式3: 回退到相对路径
        return os.path.abspath("data")

    # ─── 生命周期 ───────────────────────────────────────

    async def initialize(self):
        """插件初始化（旧版生命周期钩子，兼容）。"""
        await self._start_engine()

    async def on_start(self):
        """插件启动（新版生命周期钩子）。"""
        await self._start_engine()

    async def _start_engine(self):
        """启动社交引擎和后台任务。"""
        if self._engine is None:
            logger.error("[autonomous_social] 引擎未初始化，无法启动")
            return
        if self._task is not None and not self._task.done():
            logger.warning("[autonomous_social] 后台循环已在运行，跳过重复启动")
            return

        try:
            self._engine.start()
            self._task = asyncio.create_task(
                self._engine.run(),
                name="autonomous-social-loop",
            )
            logger.info(f"[autonomous_social] v{__version__} 已启动")
            if self._engine.cfg.track_own_replies and not HOOK_AFTER_SENT:
                logger.warning(
                    "[autonomous_social] 这个 AstrBot 版本没有 after_message_sent 钩子，"
                    "插件看不到 bot 自己说过的话：追问会退化成只按对方最后那句判断。"
                    "升级 AstrBot 到 4.x 后重开插件即可。"
                )
            if not HOOK_LLM_REQUEST:
                logger.warning(
                    "[autonomous_social] 这个 AstrBot 版本没有 on_llm_request 钩子，"
                    "主动消息发出去后用户回复时，AI 看不到自己刚才说了什么。"
                    "升级 AstrBot 到支持该钩子的版本即可修复。"
                )
        except Exception as e:
            logger.error(f"[autonomous_social] 启动失败: {e}")
            raise

    # ─── 事件处理 ───────────────────────────────────────

    @event_message_type(EventMessageType.ALL)
    async def observe_message(self, event: AstrMessageEvent):
        """观察并记录所有消息，用于社交决策。"""
        if self._engine is None:
            return
        try:
            await self._engine.observe(event)
        except Exception as exc:
            logger.warning(f"[autonomous_social] 观察消息失败: {exc}")

    @after_message_sent()
    async def observe_outgoing(self, event: AstrMessageEvent):
        """记下 bot 自己说出去的话。

        AstrBot 在 respond 阶段把消息发完之后才调这个钩子（此时 clear_result() 还没跑，
        event.get_result() 拿得到刚发出去的内容），而插件自己主动发的消息走
        context.send_message，不经过 respond 阶段，因此不会在这里重复记账。
        """
        if self._engine is None:
            return
        try:
            await self._engine.note_spoken(event)
        except Exception as exc:
            logger.warning(f"[autonomous_social] 记录已发送消息失败: {exc}")

    @on_llm_request()
    async def inject_proactive_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """把用户回复前主动发出的消息作为本轮临时上下文注入。

        主动消息不会进入 AstrBot 的会话历史；这里用官方临时内容块让模型在
        本轮看到自己刚说过的话，消费后清空，不会污染后续会话。旧版没有
        mark_as_temp() 时才退回 req.contexts。
        """
        if self._engine is None:
            return
        try:
            if not self._engine.cfg.enabled:
                return
            if self._engine.cfg.private_only and self._is_group(event):
                return
            contexts = getattr(req, "contexts", None)
            extra_parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(contexts, list) and not isinstance(extra_parts, list):
                return
            bid = self._engine._get_bot_id(event)
            uid = self._engine._get_sender_id(event)
            if not bid or not uid or uid == bid:
                return
            temp_factory = getattr(TextPart, "mark_as_temp", None) if TextPart else None
            can_use_temp = isinstance(extra_parts, list) and callable(temp_factory)
            if not can_use_temp and not isinstance(contexts, list):
                return
            text = self._engine.consume_pending_proactive_context(bid, uid)
            if not text:
                return
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            if not lines:
                return
            hint = (
                "<autonomous_social_proactive_context>\n"
                "以下是你在当前对话中刚刚主动发出的原文；这是你已经说过的话，"
                "不是用户的新消息，只在本次回复中参考：\n"
                + "\n".join(f"{idx}. {line}" for idx, line in enumerate(lines, 1))
                + "\n</autonomous_social_proactive_context>"
            )
            if can_use_temp:
                extra_parts.append(TextPart(text=hint).mark_as_temp())
                logger.debug(
                    f"[autonomous_social] 主动消息已临时注入本轮上下文（{len(text)} 字）: {text[:40]}…"
                )
                return
            if isinstance(contexts, list):
                for line in lines:
                    contexts.append({"role": "assistant", "content": line})
                logger.warning(
                    "[autonomous_social] 当前 AstrBot 不支持临时内容块，主动消息已退回写入本轮 contexts"
                )
                return
            logger.warning(
                "[autonomous_social] 当前请求没有可用的临时内容块或 contexts，主动消息未注入"
            )
        except Exception as exc:
            logger.warning(f"[autonomous_social] 主动消息补入上下文失败: {exc}")

    @command("自主社交状态")
    async def social_status(self, event: AstrMessageEvent):
        """查询自主社交当前状态。仅机器人主人（需时加白名单）可用。"""
        if self._engine is None:
            yield event.plain_result("自主社交插件未正常初始化。")
            return
        allowed, why = self._check_owner(event)
        if not allowed:
            if self._is_group(event):
                # 群里不回应：拒绝本身会告诉所有群成员「这里有个能用的指令」
                logger.info(f"[autonomous_social] 忽略群内查看状态：{why}")
                return
            yield event.plain_result("这个指令只有机器人的主人能用。")
            return
        try:
            text = await self._engine.status_text()
        except Exception as e:
            logger.error(f"[autonomous_social] 获取状态失败: {e}")
            yield event.plain_result(f"获取状态失败: {e}")
            return
        yield event.plain_result(text + "\n" + self._permission_text(event))

    @command("触发社交")
    async def trigger_social(self, event: AstrMessageEvent):
        """手动触发一次主动联系（绕过念头与冷却），挑最想说的人立即发送。

        默认仅机器人主人（全局配置 admins_id）可用；需要放开给别人就在配置里关掉
        owner_only_commands 并填入白名单。
        """
        if self._engine is None:
            yield event.plain_result("自主社交插件未正常初始化。")
            return
        allowed, why = self._check_owner(event)
        if not allowed:
            if self._is_group(event):
                logger.info(f"[autonomous_social] 忽略群内手动触发：{why}")
                return
            yield event.plain_result("这个指令只有机器人的主人能用。")
            return
        try:
            bid = self._engine._get_bot_id(event)
        except Exception:
            bid = "default"
        try:
            result = await self._engine.trigger_once(bid)
        except Exception as e:
            logger.error(f"[autonomous_social] 手动触发失败: {e}")
            yield event.plain_result(f"触发失败: {e}")
            return
        yield event.plain_result(result)

    @command("主动消息记录")
    async def proactive_log(self, event: AstrMessageEvent):
        """查看最近 7 天发过的主动消息（按用户隔离不串台）。仅机器人主人可用。

        可选带一个用户 ID：「主动消息记录 12345」只看那个人；不带则列最近发过的几个人。
        """
        if self._engine is None:
            yield event.plain_result("自主社交插件未正常初始化。")
            return
        allowed, why = self._check_owner(event)
        if not allowed:
            if self._is_group(event):
                logger.info(f"[autonomous_social] 忽略群内查看主动消息记录：{why}")
                return
            yield event.plain_result("这个指令只有机器人的主人能用。")
            return
        uid_filter = ""
        try:
            raw = str(getattr(event, "message_str", "") or "").strip()
            # 去掉指令本身，剩下的当用户 ID
            parts = raw.split(None, 1)
            if len(parts) > 1:
                uid_filter = parts[1].strip()
        except Exception:
            uid_filter = ""
        try:
            text = self._engine.proactive_log_text(uid_filter)
        except Exception as e:
            logger.error(f"[autonomous_social] 获取主动消息记录失败: {e}")
            yield event.plain_result(f"获取失败: {e}")
            return
        yield event.plain_result("最近 7 天主动消息记录（按用户隔离）：\n" + text)

    @command("群社交状态")
    async def group_social_status(self, event: AstrMessageEvent):
        """查看群聊心流在管的群：最近活跃、心流窗口、是否被隔离（被踢/会话失效）。仅机器人主人可用。"""
        if self._engine is None:
            yield event.plain_result("自主社交插件未正常初始化。")
            return
        allowed, why = self._check_owner(event)
        if not allowed:
            if self._is_group(event):
                logger.info(f"[autonomous_social] 忽略群内查看群社交状态：{why}")
                return
            yield event.plain_result("这个指令只有机器人的主人能用。")
            return
        try:
            text = self._engine.group_status_text()
        except Exception as e:
            logger.error(f"[autonomous_social] 获取群社交状态失败: {e}")
            yield event.plain_result(f"获取失败: {e}")
            return
        yield event.plain_result(text)

    @staticmethod
    def _norm_id(x: object) -> str:
        """归一化 ID：去空白、去前缀、大小写不敏感，避免面板里多个空格就匹配不上。"""
        s = str(x or "").strip().lower()
        for prefix in ("/", "qq:", "qq：", "user_id:", "uid:"):
            if s.startswith(prefix):
                s = s[len(prefix):].strip()
        return s

    def _owner_ids(self) -> Tuple[set, set]:
        """返回（主人 ID 集合, 额外白名单集合），均已归一化。"""
        owners: set = set()
        whitelist: set = set()
        try:
            admins = self.context.get_config().get("admins_id", []) or []
            owners = {self._norm_id(x) for x in admins if self._norm_id(x)}
        except Exception:
            pass
        try:
            if self._engine and self._engine.cfg:
                if not self._engine.cfg.owner_only_commands:
                    whitelist = {
                        self._norm_id(x)
                        for x in self._engine.cfg.allowed_trigger_uid_set()
                        if self._norm_id(x)
                    }
        except Exception:
            pass
        return owners, whitelist

    def _check_owner(self, event: AstrMessageEvent) -> Tuple[bool, str]:
        """判定发起者能否使用管理指令，返回 (是否允许, 供日志/面板看的依据)。"""
        sender = ""
        try:
            sender = self._norm_id(event.get_sender_id())
        except Exception:
            pass
        owners, whitelist = self._owner_ids()
        if not sender:
            return False, "拿不到发送者 ID"
        if sender in owners:
            return True, f"{sender} 在主人列表里"
        if sender in whitelist:
            return True, f"{sender} 在额外白名单里"
        try:
            if hasattr(event, "is_admin") and bool(event.is_admin()):
                return True, f"{sender} 被 AstrBot 标为 admin"
        except Exception:
            pass
        detail = (
            f"sender={sender or '空'} 主人={sorted(owners) or '未配置'} "
            f"白名单={'已关闭(owner_only_commands)' if not whitelist and self._owner_only() else sorted(whitelist)}"
        )
        return False, detail

    def _owner_only(self) -> bool:
        try:
            return bool(self._engine and self._engine.cfg.owner_only_commands)
        except Exception:
            return True

    def _permission_text(self, event: AstrMessageEvent) -> str:
        """把权限判定依据附在状态输出末尾：主人照着这一行就能看出 ID 填得对不对。"""
        owners, whitelist = self._owner_ids()
        try:
            sender = self._norm_id(event.get_sender_id())
        except Exception:
            sender = "?"
        return (
            "权限：仅主人可用（全局配置 admins_id）\n"
            f"  你的 ID：{sender}\n"
            f"  主人列表：{', '.join(sorted(owners)) or '（空，所以谁都会被拒）'}\n"
            f"  额外白名单：{('关闭（owner_only_commands=true）') if not whitelist else ', '.join(sorted(whitelist))}"
        )

    def _is_group(self, event: AstrMessageEvent) -> bool:
        """是否群消息：拿不准时当作群聊（宁可静默也不在群里多嘴）。"""
        try:
            if self._engine is not None:
                return bool(self._engine._detect_is_group(event))
        except Exception:
            pass
        try:
            return not bool(event.is_private_chat())
        except Exception:
            return True

    # ─── 停止清理 ───────────────────────────────────────

    async def terminate(self):
        """插件停止（旧版生命周期钩子，兼容）。"""
        await self._stop_engine()

    async def on_stop(self):
        """插件停止（新版生命周期钩子）。"""
        await self._stop_engine()

    async def _stop_engine(self):
        """停止引擎并清理资源。"""
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception as e:
                logger.error(f"[autonomous_social] 引擎停止出错: {e}")

        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning("[autonomous_social] 后台任务取消超时，强制终止")
            except Exception as e:
                logger.error(f"[autonomous_social] 任务清理异常: {e}")
            self._task = None

        logger.info("[autonomous_social] 已停止")
