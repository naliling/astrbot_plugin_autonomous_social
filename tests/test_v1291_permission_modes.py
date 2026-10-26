"""v1.28.11：`owner_only_commands` 真正生效。

背景：这个面板项（「指令仅主人可用」，默认开）此前读进配置却**从不参与判断**——
`_check_owner()` 永远放行「主人 ∪ 白名单 ∪ AstrBot 管理员」，`_owner_only()`
定义了 0 次调用。于是拨到关什么都不会变，面板在说谎。

现在按名字与 README 的语义实现：

* 开（默认）：只有机器人主人与 AstrBot 管理员能用，**白名单不生效**；
* 关：主人 + AstrBot 管理员 + `allowed_trigger_uids` 白名单都能用。

白名单仍**始终读取**（状态页要显示填了谁、关闭时要能立刻生效），只是是否
采用由开关决定。这里钉住四种身份 × 两种模式的矩阵，以及状态页的权限说明。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from test_v1290_commands import _Event, _load_main


class OwnerOnlyPermissionTest(unittest.TestCase):
    """owner_only_commands 的两种模式：谁能用、谁不能用。"""

    def _plugin(self, owner_only: bool):
        main_mod = _load_main()
        obj = object.__new__(main_mod.AutonomousSocial)
        obj.context = SimpleNamespace(get_config=lambda: {"admins_id": ["100"]})
        obj._engine = SimpleNamespace(
            cfg=SimpleNamespace(
                owner_only_commands=owner_only,
                allowed_trigger_uid_set=lambda: ["200", "qq:300"],
            )
        )
        return main_mod, obj

    def _allowed(self, obj, uid: str, *, admin: bool = False) -> bool:
        ok, _why = obj._check_owner(_Event(sender=uid, admin=admin))
        return ok

    # ── 开（默认）：仅主人与管理员，白名单不生效 ──────────────────

    def test_on_owner_is_allowed(self):
        _mod, obj = self._plugin(True)
        self.assertTrue(self._allowed(obj, "100"))

    def test_on_whitelist_is_denied(self):
        _mod, obj = self._plugin(True)
        self.assertFalse(self._allowed(obj, "200"), "开了「仅主人可用」白名单还能用")
        self.assertFalse(self._allowed(obj, "300"))

    def test_on_admin_is_allowed(self):
        _mod, obj = self._plugin(True)
        self.assertTrue(self._allowed(obj, "999", admin=True))

    def test_on_stranger_is_denied_and_reason_says_why(self):
        _mod, obj = self._plugin(True)
        ok, why = obj._check_owner(_Event(sender="888"))
        self.assertFalse(ok)
        self.assertIn("白名单不生效", why)

    # ── 关：白名单才生效 ────────────────────────────────────────

    def test_off_whitelist_is_allowed(self):
        _mod, obj = self._plugin(False)
        self.assertTrue(self._allowed(obj, "200"), "关掉后白名单仍被拦")
        self.assertTrue(self._allowed(obj, "300"))

    def test_off_owner_still_allowed(self):
        _mod, obj = self._plugin(False)
        self.assertTrue(self._allowed(obj, "100"))

    def test_off_stranger_is_still_denied(self):
        _mod, obj = self._plugin(False)
        self.assertFalse(self._allowed(obj, "888"), "关掉开关不等于所有人都能用")

    # ── 状态页的权限说明必须跟着模式走 ──────────────────────────

    def test_permission_text_reflects_the_mode(self):
        _mod, obj = self._plugin(True)
        on = obj._permission_text(_Event(sender="100"))
        self.assertIn("仅主人可用", on)
        self.assertIn("白名单未生效", on)

        _mod2, obj2 = self._plugin(False)
        off = obj2._permission_text(_Event(sender="200"))
        self.assertIn("白名单可用", off)

    # ── 白名单始终被读取（关闭时才能立刻生效）────────────────────

    def test_whitelist_is_read_in_both_modes(self):
        _mod, obj = self._plugin(True)
        _owners, whitelist = obj._owner_ids()
        self.assertEqual(whitelist, {"200", "300"})

        _mod2, obj2 = self._plugin(False)
        _owners2, whitelist2 = obj2._owner_ids()
        self.assertEqual(whitelist2, {"200", "300"})


class OwnerOnlyDefaultTest(unittest.TestCase):
    """默认值必须是「开」：升级后不改变老用户的权限（仍然是仅主人）。"""

    def test_default_is_on(self):
        from social.config import SocialConfig

        cfg = SocialConfig.from_astrbot({})
        self.assertTrue(cfg.owner_only_commands)


if __name__ == "__main__":
    unittest.main()
