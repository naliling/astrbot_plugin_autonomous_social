"""自主拟人社交 —— AstrBot 适配层。

v1.16.0：不再替 AI 说话。删掉 45 条固定例句与约 80 条虚构经历，动机改由模型自己
产生；新增从真实对话统计的说话习惯画像，并把 Humanoid Core 算好但此前没接上的
生物钟夜、问句倾向、能否连发接进来。详见 CHANGELOG。
优先读 Humanoid Core v2.14 导出的契约快照（身体轴 + 体感 + 说话形式），
并把「刚主动找过谁」「被冷落几次」写回独立的信号文件；拿不到契约时退回旧字段
并在 /自主社交状态 里明确列出因此停用的能力。
"""

from __future__ import annotations

import asyncio
import os
import time
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



try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:
    get_astrbot_data_path = None

from .social import __version__
from .social.config import migrate_legacy_defaults
from .social.engine import SocialEngine, _ago_text, _num

# 插件数据子目录
DATA_SUBDIR = ("plugin_data", "astrbot_plugin_autonomous_social")


def _selfcheck_settle_text(settled: object, prev_settled: object, now: float) -> str:
    """`/社交自检` 里的「上次结算」：一律从时间戳算，不再写死「刚刚」。

    自检本身也会结算一次（`dry_run` 把 `last_settled` 改写成「现在」），所以只报
    `last_settled` 的话这一行永远是「刚刚」——等于什么也没说。真实信息在
    `prev_settled`：自检覆盖之前，上一次心跳是什么时候结算的。两个都给，而且都按
    真实相对时间显示：写死的时间比不写更糟，它看着像证据。
    """
    settled_v = _num(settled or 0.0)
    prev_v = _num(prev_settled or 0.0)
    if settled_v <= 0:
        return "还没跑过"
    text = f"{_ago_text(settled_v, now)}（这次自检刚结算完）"
    if prev_v > 0:
        text += f"；自检前那次心跳结算：{_ago_text(prev_v, now)}"
    else:
        text += "；自检前这次启动还没跑过心跳结算"
    return text


class AutonomousSocial(Star):
    """自主拟人社交插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._engine: Optional[SocialEngine] = None
        self._task: Optional[asyncio.Task] = None

        # 解析数据目录
        data_dir = self._resolve_data_dir()
        state_path = os.path.join(data_dir, *DATA_SUBDIR, "state.json")

        # 旧配置里停在旧默认值上的项只提示一次：AstrBot 只补默认值、从不覆盖已有值，
        # 但插件凭什么断定用户没手动改过？擅自改写用户的设置比“节奏没跟上”更糟。
        try:
            hints = migrate_legacy_defaults(
                config, os.path.dirname(state_path), __version__
            )
            if hints:
                logger.info(
                    "[autonomous_social] 以下配置仍停在本插件旧版本的默认值上（插件不会替你改，"
                    "想跟新版节奏就在面板里自行调整）：" + "；".join(hints)
                )
        except Exception as e:
            logger.warning(f"[autonomous_social] 检查旧默认值失败（不影响运行）: {e}")

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

        # 方式2: 从 context 获取（预留：AstrBot 4.29.3 的 Context 已无此方法，
        # core/star/base.py 也不再提供；保留 hasattr 分支只为兼容自定义/旧版 Context）。
        # 正常安装不会走到这里，真正的兜底是方式1的 get_astrbot_data_path()。
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
        """插件初始化：启动引擎与后台任务。

        AstrBot 4.29.3 只调用 initialize()/terminate()（core/star/base.py 的
        Star 基类），旧版曾额外声明同义的 on_start()/on_stop()，但框架全树没有
        任何调用点（审核 P3-01）——已删除，避免维护者误以为有两条生命周期路径。
        """
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

    @after_message_sent()
    async def observe_outgoing(self, event: AstrMessageEvent):
        """记下 bot 自己说出去的话。

        AstrBot 在 respond 阶段把消息发完之后才调这个钩子（此时 clear_result() 还没跑，
        event.get_result() 拿得到刚发出去的内容），而插件自己主动发的消息走
        context.send_message，不经过 respond 阶段，因此不会在这里重复记账
        （它由引擎在发送成功后单独补写会话历史）。
        """
        if self._engine is None:
            return
        try:
            await self._engine.note_spoken(event)
        except Exception as exc:
            logger.warning(f"[autonomous_social] 记录已发送消息失败: {exc}")

    async def tidy_outgoing(self, event: AstrMessageEvent):
        """普通对话回复保持原样，不由插件改写。"""
        return



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
            # 私聊里把「当前对话者」的 ID 传下去，【亲密】段才能显示与这个人的
            # 场景/亲近欲；群里不传（群聊里不该出现任何人的私密数字）。拿不到
            # ID 时保持空串，引擎会退回只报能力/配置。
            bid = uid = ""
            if not self._is_group(event):
                try:
                    bid = self._engine._get_bot_id(event)
                    uid = self._engine._get_sender_id(event)
                except Exception:
                    bid = uid = ""
            text = await self._engine.status_text(bid=bid, uid=uid)
        except Exception as e:
            logger.error(f"[autonomous_social] 获取状态失败: {e}")
            yield event.plain_result(f"获取状态失败: {e}")
            return
        yield event.plain_result(text + "\n" + self._permission_text(event))

    @command("社交自检")
    async def social_selfcheck(self, event: AstrMessageEvent):
        """立刻空跑一轮（不发送、不调用模型），回答「链路是通的吗」。

        以前想验证链路只能等一个心跳间隔，而那期间零发送是完全正常的，
        看不出是正常还是坏了。这个指令几毫秒出结果。
        """
        if self._engine is None:
            yield event.plain_result("自主社交插件未正常初始化。")
            return
        allowed, why = self._check_owner(event)
        if not allowed:
            if self._is_group(event):
                logger.info(f"[autonomous_social] 忽略群内自检：{why}")
                return
            yield event.plain_result("这个指令只有机器人的主人能用。")
            return
        try:
            m = await self._engine.dry_run()
        except Exception as e:
            yield event.plain_result(f"自检失败: {e}")
            return
        settled = m.get("last_settled") or 0
        try:
            now = self._engine._time()
        except Exception:
            now = time.time()
        yield event.plain_result(
            "自检完成（没有发送任何消息，也没有调用模型）：\n"
            f"  已认识的人：{m.get('last_settled_users', 0)}\n"
            f"  念头已攒够、闸门也放行的：{m.get('last_ready', 0)}\n"
            f"  上次结算：{_selfcheck_settle_text(settled, m.get('prev_settled'), now)}\n"
            f"  说明：{m.get('last_failure') or '链路正常，等下一个心跳就会尝试主动联系'}"
        )

    @command("触发社交")
    async def trigger_social(self, event: AstrMessageEvent):
        """手动触发一次主动联系（绕过念头与冷却），挑最想说的人立即发送。

        默认仅机器人主人（全局配置 admins_id）与 AstrBot 管理员可用；需要放开给别人，
        先关掉 owner_only_commands，再把对方 ID 填进 allowed_trigger_uids。

        多台 bot 同群时（trigger_group_requires_at，默认开）：群里只有被 @ 的那台
        才执行——不然一条「/触发社交」会同时叫醒群里所有 bot，每台各自发一条，
        直观上就是「三四个 Bot 同时串台」。被 @ 的是别人、或谁都没 @ 时，
        本台静默跳过（只记日志，不在群里回绝）。
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
        # 群内定向检查：@ 了本 bot 才执行；@ 了别的 id 或谁都没 @ 都跳过。
        # 只对群聊生效，私聊保持原样（私聊天然只会送到一台）。
        if self._is_group(event) and self._engine.cfg.trigger_group_requires_at:
            try:
                targets = self._engine._at_targets(event)
            except Exception:
                targets = []
            # @全体成员也算命中：它本来就该叫醒群里所有 bot。
            if not targets or not ("all" in targets or str(bid) in {str(t) for t in targets}):
                logger.info(
                    f"[autonomous_social] 忽略群内手动触发（bid={bid}）："
                    f"消息 @ 的是 {targets or '没有人'}，不是本台（可在配置里关掉「群内触发须@本bot」）"
                )
                return
        try:
            result = await self._engine.trigger_once(bid)
        except Exception as e:
            logger.error(f"[autonomous_social] 手动触发失败: {e}")
            yield event.plain_result(f"触发失败: {e}")
            return
        yield event.plain_result(result)

    @command("主动消息预览")
    async def preview_proactive(self, event: AstrMessageEvent):
        """只生成、不发送：看一眼她此刻打算对谁说什么。仅机器人主人可用。

        可选带一个用户 ID：「主动消息预览 12345」只看那个人。
        全程不开「正在输入」、不消费由头、不计账——生成完毕即丢，方便调提示词。
        """
        if self._engine is None:
            yield event.plain_result("自主社交插件未正常初始化。")
            return
        allowed, why = self._check_owner(event)
        if not allowed:
            if self._is_group(event):
                logger.info(f"[autonomous_social] 忽略群内主动消息预览：{why}")
                return
            yield event.plain_result("这个指令只有机器人的主人能用。")
            return
        uid_filter = ""
        try:
            parts = str(getattr(event, "message_str", "") or "").strip().split(None, 1)
            if len(parts) > 1:
                uid_filter = parts[1].strip()
        except Exception:
            uid_filter = ""
        try:
            bid = self._engine._get_bot_id(event)
        except Exception:
            bid = "default"
        try:
            result = await self._engine.preview_once(bid, uid_filter)
        except Exception as e:
            logger.error(f"[autonomous_social] 主动消息预览失败: {e}")
            yield event.plain_result(f"预览失败: {e}")
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
            # 白名单始终读出来：一是状态页要显示「填了谁」，二是
            # owner_only_commands=False 时它就是放行依据。是否**采用**它由
            # _check_owner 按 owner_only_commands 决定（开=仅主人与管理员）。
            if self._engine and self._engine.cfg:
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
        # owner_only_commands=True（默认）：只认主人与 AstrBot 管理员，
        # 白名单不参与——面板上写着「仅主人可用」就必须真的仅主人可用。
        # False：才把额外白名单放进来（README 里的「关掉开关再填白名单」）。
        if not self._owner_only() and sender in whitelist:
            return True, f"{sender} 在额外白名单里（owner_only_commands 已关闭）"
        try:
            if hasattr(event, "is_admin") and bool(event.is_admin()):
                return True, f"{sender} 被 AstrBot 标为 admin"
        except Exception:
            pass
        detail = (
            f"sender={sender or '空'} 主人={sorted(owners) or '未配置'} "
            f"白名单={sorted(whitelist) or '未配置'}"
        )
        if self._owner_only():
            detail += "（owner_only_commands 开启：白名单不生效）"
        return False, detail

    def _owner_only(self) -> bool:
        """管理指令是否仅主人可用。

        True（默认）：只有机器人主人与 AstrBot 管理员能执行，白名单不生效；
        False：主人 + AstrBot 管理员 + allowed_trigger_uids 白名单都能执行。
        """
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
        if self._owner_only():
            mode = "仅主人可用（白名单未生效；要放开请关掉 owner_only_commands）"
        else:
            mode = "主人 + 白名单可用（owner_only_commands 已关闭）"
        return (
            f"权限：{mode}\n"
            f"  你的 ID：{sender}\n"
            f"  主人列表：{', '.join(sorted(owners)) or '（空，所以谁都会被拒）'}\n"
            f"  额外白名单：{', '.join(sorted(whitelist)) or '（空）'}"
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
        """插件停止：停掉引擎并清理后台任务（框架唯一的停止入口）。"""
        await self._stop_engine()

    async def _stop_engine(self):
        """停止引擎并清理资源。"""
        if self._engine is not None:
            try:
                await self._engine.stop()
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
