"""自主拟人社交 —— AstrBot 适配层。

v1.7.6：优先读 Humanoid Core v2.14 导出的契约快照（身体轴 + 体感 + 说话形式），
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
        after_message_sent = filter.after_message_sent  # type: ignore[name-defined]
    except Exception:
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
                    # get_plugin_data_dir 返回的是插件专属目录，需要回退两级到 data/
                    parent = os.path.dirname(os.path.dirname(path))
                    if os.path.basename(parent) == "plugin_data":
                        return os.path.dirname(parent)
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

    @after_message_sent
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
