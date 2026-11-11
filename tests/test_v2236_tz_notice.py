"""v2.2.36：时区配错不能悄悄过——启动时喊一次、状态页显示一次。

线上真实情况：插件时区配着 UTC+2，宿主在 UTC+8。于是她的安静时段 23–7 落在宿主
时间 05:00–13:00（整个上午都不在），问候语、每日额度、状态页时间也全按 UTC+2 走，
而日志和面板一个字都不提。时区是配置项、插件不替用户改，但必须说出来。
"""

from __future__ import annotations

import io
import logging
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("aso_v2236_stub")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


_install_stub()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import social.clock as clock  # noqa: E402
import social.engine as engine_mod  # noqa: E402


def _engine(tz="UTC+2", quiet_start=23, quiet_end=7):
    eng = engine_mod.SocialEngine.__new__(engine_mod.SocialEngine)
    eng.cfg = types.SimpleNamespace(
        timezone_offset=tz, quiet_start=quiet_start, quiet_end=quiet_end
    )
    return eng


def _src() -> str:
    return io.open(
        Path(PLUGIN_ROOT) / "social" / "engine.py", encoding="utf-8", errors="replace"
    ).read()


class HostOffsetTest(unittest.TestCase):
    def test_host_offset_is_a_plausible_minutes_value(self):
        value = clock.host_utc_offset_minutes()
        self.assertIsInstance(value, int)
        self.assertGreaterEqual(value, -12 * 60)
        self.assertLessEqual(value, 14 * 60)

    def test_format_roundtrip(self):
        value = clock.host_utc_offset_minutes()
        label = clock.format_utc_offset(value)
        self.assertTrue(label.startswith("UTC"))
        self.assertEqual(clock.parse_utc_offset(label), value)


class MismatchNoteTest(unittest.TestCase):
    def test_big_mismatch_is_reported(self):
        note = engine_mod.SocialEngine._tz_mismatch_note(_engine("UTC+2"))
        self.assertIn("UTC+2", note)
        self.assertIn("本机是", note)
        # 差 6 小时必须写在里面（宿主是 UTC+8 时才会这样；别的机器跳过这条断言）
        if clock.host_utc_offset_minutes() == 480:
            self.assertIn("差了 6 小时", note)
            self.assertIn("5 点到 13 点", note)
            self.assertIn("UTC+8", note)

    def test_same_clock_is_silent(self):
        host = clock.format_utc_offset(clock.host_utc_offset_minutes())
        self.assertEqual(engine_mod.SocialEngine._tz_mismatch_note(_engine(host)), "")

    def test_auto_is_silent(self):
        self.assertEqual(engine_mod.SocialEngine._tz_mismatch_note(_engine("auto")), "")

    def test_one_hour_drift_is_silent(self):
        host = clock.host_utc_offset_minutes()
        note = engine_mod.SocialEngine._tz_mismatch_note(
            _engine(clock.format_utc_offset(host + 60))
        )
        self.assertEqual(note, "")

    def test_broken_config_value_is_silent(self):
        self.assertEqual(
            engine_mod.SocialEngine._tz_mismatch_note(_engine("not-a-timezone")), ""
        )

    def test_note_does_not_touch_config(self):
        eng = _engine("UTC+2")
        engine_mod.SocialEngine._tz_mismatch_note(eng)
        self.assertEqual(eng.cfg.timezone_offset, "UTC+2")
        self.assertEqual((eng.cfg.quiet_start, eng.cfg.quiet_end), (23, 7))


class WiringTest(unittest.TestCase):
    def test_startup_logs_the_note(self):
        src = _src()
        started = src.index('logger.info("[autonomous_social] 后台循环已启动")')
        following = src[started : started + 900]
        self.assertIn("_tz_mismatch_note", following)
        self.assertIn("logger.warning", following)

    def test_status_page_shows_the_note(self):
        src = _src()
        self.assertIn("⚠️ 时区：", src)

    def test_constant_is_module_level(self):
        self.assertEqual(engine_mod.TZ_MISMATCH_MINUTES, 120)
        self.assertNotIn("    TZ_MISMATCH_MINUTES", _src())


if __name__ == "__main__":
    unittest.main()
