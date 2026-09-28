"""Orbit Cache 行为测试。

覆盖三件最容易出错、也最不该出事的地方：
  1. NapCat 文件缓存的阈值/最旧优先/最小年龄/符号链接护栏
  2. SweepEngine 的条件判定：没到条件绝不删，force 才会无视阈值
  3. 插件模块清理只按精确模块前缀动，拒绝对自己下手
另外静态核对前端 id 与后端返回字段是否对得上。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "astrbot_plugin_orbit_command"


class _Awaitable:
    def __init__(self, value):
        self._value = value

    async def __call__(self, default=None):
        return self._value


# ---------- AstrBot 最小仿真桩 ----------

def install_stub(data_path: Path) -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api_logger = types.ModuleType("astrbot.api.logger")

    class _Logger:
        def __getattr__(self, _):
            return lambda *a, **k: None

    api.logger = _Logger()
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        def plain_result(self, text):
            return text

    class _Pass:
        def __init__(self, *a, **k):
            pass

        def __call__(self, func):
            return func

    class _Command:
        def __init__(self, name):
            self.name = name

        def __call__(self, func):
            func._command_name = self.name
            return func

    class _Filter:
        PermissionType = types.SimpleNamespace(ADMIN="admin")

        def command(self, name, **k):
            return _Command(name)

        def permission_type(self, *a, **k):
            return _Pass()

        def on_astrbot_loaded(self, **k):
            return _Pass()

        def on_plugin_unloaded(self, **k):
            return _Pass()

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter()
    api.event = event_mod

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context
            self.name = "astrbot_plugin_orbit_command"

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name):
            target = data_path / "plugin_data" / plugin_name
            target.mkdir(parents=True, exist_ok=True)
            return target

    star_mod.Star = Star
    star_mod.Context = object
    star_mod.StarTools = StarTools
    api.star = star_mod

    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.json_response = lambda data, status_code=200: {"__json__": data}
    web_mod.error_response = lambda message, status_code=400, **extra: {
        "__error__": message, "status": status_code, **extra}

    web_mod.request = types.SimpleNamespace(json=_Awaitable({}), query=_FakeQuery())
    api.web = web_mod

    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: str(data_path)
    cleaner_mod = types.ModuleType("astrbot.core.utils.storage_cleaner")

    class StorageCleaner:
        def __init__(self, _):
            pass

        def get_status(self):
            block = {"size_bytes": 0, "file_count": 0, "path": "x"}
            return {"logs": dict(block), "cache": dict(block), "total_bytes": 0}

        def cleanup(self, _target):
            return {"removed_bytes": 0, "processed_files": 0, "failed_files": 0}

    cleaner_mod.StorageCleaner = StorageCleaner
    core.utils = utils
    utils.astrbot_path = path_mod
    utils.storage_cleaner = cleaner_mod

    aiohttp = types.ModuleType("aiohttp")

    class _ClientError(Exception):
        pass

    class _Session:
        def __init__(self, **kw):
            self.closed = False

        async def close(self):
            self.closed = True

    aiohttp.ClientError = _ClientError
    aiohttp.ClientTimeout = lambda **kw: None
    aiohttp.ClientSession = _Session
    sys.modules["aiohttp"] = aiohttp

    astrbot.api = api
    astrbot.core = core
    for name, mod in [
        ("astrbot", astrbot), ("astrbot.api", api), ("astrbot.api.logger", api_logger),
        ("astrbot.api.event", event_mod), ("astrbot.api.star", star_mod),
        ("astrbot.api.web", web_mod), ("astrbot.core", core),
        ("astrbot.core.utils", utils), ("astrbot.core.utils.astrbot_path", path_mod),
        ("astrbot.core.utils.storage_cleaner", cleaner_mod),
    ]:
        sys.modules[name] = mod


class _StarMeta:
    def __init__(self, name, activated=True, root_dir_name=None, version="1.0.0"):
        self.name = name
        self.activated = activated
        self.root_dir_name = root_dir_name or name
        self.version = version


class _Context:
    def __init__(self, stars=()):
        self._stars = list(stars)
        self.web_apis = []
        self.cron_manager = _FakeCronManager()

    def get_all_stars(self):
        return self._stars

    def register_web_api(self, route, handler, methods, desc):
        self.web_apis.append((route, handler, methods, desc))


class _FakeQuery:
    def get(self, key, default=None, type=None):
        return default


class _FakeCronManager:
    """模拟 astrbot.core.cron.manager.CronJobManager 的用到的部分。"""

    def __init__(self):
        self.jobs = {}
        self.add_calls = []
        self._seq = 0
        self.fail_expression = None

    async def list_jobs(self, job_type=None):
        return list(self.jobs.values())

    async def delete_job(self, job_id):
        self.jobs.pop(job_id, None)

    async def add_basic_job(self, *, name, cron_expression, handler, description=None,
                            timezone=None, payload=None, enabled=True, persistent=False):
        self.add_calls.append({
            "name": name, "cron": cron_expression, "persistent": persistent,
            "enabled": enabled, "handler": handler,
        })
        if self.fail_expression and self.fail_expression == cron_expression:
            raise ValueError("bad crontab")
        self._seq += 1
        job = types.SimpleNamespace(job_id=f"job{self._seq}", name=name)
        self.jobs[job.job_id] = job
        return job

    def get_next_run_time(self, job_id):
        from datetime import datetime, timedelta, timezone

        if job_id not in self.jobs:
            return None
        return datetime.now(timezone.utc) + timedelta(minutes=15)


class _Config(dict):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.saved = 0

    def save_config(self):
        self.saved += 1


# ---------- 被测对象 ----------

class OrbitTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.data_path = Path(cls._tmp.name) / "astrbot"
        cls.data_path.mkdir(parents=True, exist_ok=True)
        install_stub(cls.data_path)
        sys.path.insert(0, str(PLUGIN_DIR.parent))
        import importlib

        cls.maint = importlib.import_module("astrbot_plugin_orbit_command.maintenance")
        cls.sched = importlib.import_module("astrbot_plugin_orbit_command.scheduler")
        cls.fmt = importlib.import_module("astrbot_plugin_orbit_command.formatting")
        cls.disc = importlib.import_module("astrbot_plugin_orbit_command.discovery")
        cls.spaces = importlib.import_module("astrbot_plugin_orbit_command.spaces")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        for key in [k for k in sys.modules if k.startswith("data.plugins.test_")]:
            del sys.modules[key]

    def _plugin(self, tmp, **cfg):
        import importlib

        main = importlib.import_module("astrbot_plugin_orbit_command.main")
        defaults = dict(
            onebot_base_url="http://127.0.0.1:3000",
            onebot_access_token="",
            auto_enabled=True,
            auto_clean_plugin_modules=True,
            sweep_cron="*/15 * * * *",
            napcat_cache_dirs=[],
            napcat_config_dirs=[],
            napcat_cache_threshold_mb=1,
            napcat_cache_min_age_minutes=0,
            astrbot_cache_threshold_mb=1,
            napcat_protocol_interval_hours=1,
        )
        defaults.update(cfg)
        context = _Context([_StarMeta("astrbot_plugin_orbit_command", activated=True)])
        plugin = main.OrbitCachePlugin(context, _Config(**defaults))
        plugin.engine.napcat_client = _FakeNapCat(
            endpoints=[_FakeEndpoint(r["url"], r["token"], r["enable"],
                                     r["protocol_interval_hours"])
                       for r in plugin._instance_rows()])
        plugin.engine.astrbot_cache = _FakeAstrBotCache(size=0)
        plugin.engine.module_cache = _FakeModuleCache()
        return plugin, context

    def _engine(self, tmp, **cfg):
        defaults = dict(
            onebot_base_url="http://127.0.0.1:3000",
            napcat_cache_dirs=[],
            napcat_cache_threshold_mb=1,
            napcat_cache_min_age_minutes=0,
            astrbot_cache_threshold_mb=1,
            napcat_protocol_interval_hours=1,
            auto_enabled=True,
            auto_clean_plugin_modules=True,
            sweep_cron="*/15 * * * *",
        )
        client = cfg.pop("napcat", None)
        defaults.update(cfg)
        config = _Config(**defaults)
        napcat = client or _FakeNapCat()
        engine = self.sched.SweepEngine(
            config=config,
            napcat_client=napcat,
            astrbot_cache=_FakeAstrBotCache(size=0),
            module_cache=_FakeModuleCache(),
            data_dir=Path(tmp),
        )
        return engine, config, napcat

    def _request_obj(self):
        """拿到 main 实际绑定的 request 对象。

        install_stub 每个测试类都会重跑，而 main 是用 `from ... import request`
        直接绑定的，sys.modules 里那一份可能不是同一个对象。
        """
        import importlib

        return importlib.import_module("astrbot_plugin_orbit_command.main").request

    def _set_request_json(self, payload):
        self._request_obj().json = _Awaitable(payload)

    def _set_request_query(self):
        self._request_obj().query = _FakeQuery()


# ---------- 1. NapCat 文件缓存清理器 ----------

class TestFileCleaner(OrbitTestBase):
    def _make_dir(self, root: Path, specs: list[tuple[str, int, int]]) -> Path:
        """specs: (文件名, 字节数, 年龄秒数)"""
        root.mkdir(parents=True, exist_ok=True)
        import os
        import time

        now = time.time()
        for name, size, age in specs:
            path = root / name
            path.write_bytes(b"x" * size)
            stamp = now - age
            os.utime(path, (stamp, stamp))
        return root

    def test_under_threshold_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_dir(Path(tmp) / "c", [("a", 1024, 3600), ("b", 1024, 3600)])
            cleaner = self.maint.NapCatCacheDirectoryCleaner(
                [str(root)], threshold_mb=1, min_age_minutes=0
            )
            results = cleaner.clean_sync()
            self.assertEqual(results[0]["deleted_files"], 0)
            self.assertEqual(len(list(root.iterdir())), 2)

    def test_over_threshold_deletes_oldest_until_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 阈值 1MiB，target=0.85MiB；10 个各 200KiB 共 2000KiB
            specs = [(f"f{i:02d}", 200 * 1024, 10000 - i * 100) for i in range(10)]
            root = self._make_dir(Path(tmp) / "c", specs)
            cleaner = self.maint.NapCatCacheDirectoryCleaner(
                [str(root)], threshold_mb=1, min_age_minutes=0
            )
            result = cleaner.clean_sync()[0]
            # 2000KiB -> 850KiB 需要删 6 个（2000-6*200=800 <= 850）
            self.assertEqual(result["deleted_files"], 6)
            remaining = sorted(p.name for p in root.iterdir())
            self.assertEqual(remaining, ["f06", "f07", "f08", "f09"])
            self.assertLessEqual(result["remaining_bytes"], 850 * 1024)

    def test_young_files_are_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 阈值极小触发清理，但文件年龄都是 0 秒，最小年龄 10 分钟
            root = self._make_dir(Path(tmp) / "c", [(f"f{i}", 200 * 1024, 0) for i in range(10)])
            cleaner = self.maint.NapCatCacheDirectoryCleaner(
                [str(root)], threshold_mb=1, min_age_minutes=10
            )
            result = cleaner.clean_sync()[0]
            self.assertEqual(result["deleted_files"], 0)
            self.assertEqual(len(list(root.iterdir())), 10)

    def test_symlinks_are_skipped_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_dir(Path(tmp) / "c", [("real", 512 * 1024, 3600)])
            outside = Path(tmp) / "outside.bin"
            outside.write_bytes(b"y" * 1024)
            try:
                (root / "link.bin").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("当前环境不支持创建符号链接")
            cleaner = self.maint.NapCatCacheDirectoryCleaner(
                [str(root)], threshold_mb=1, min_age_minutes=0
            )
            scans = cleaner.scan_sync()[0]
            self.assertEqual(scans.skipped_symlinks, 1)
            cleaner.clean_sync()
            self.assertTrue(outside.exists(), "不应删除符号链接指向的文件")
            self.assertTrue((root / "link.bin").is_symlink())

    def test_protected_paths_are_rejected(self):
        from pathlib import Path as P

        cases = {
            "相对路径": "relative/dir",
            "用户目录祖先": str(P.home().parent),
            "工作区": str(P.cwd()),
        }
        for label, value in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self.maint.NapCatCacheDirectoryCleaner([value])

    def test_target_ratio_bounds(self):
        with self.assertRaises(ValueError):
            self.maint.NapCatCacheDirectoryCleaner([], threshold_mb=1, target_ratio=0)


class TestMediaFileCleaner(OrbitTestBase):
    """自选目录层：只删白名单后缀，其余一律不碰。误删的风险全靠这几个用例钉住。"""

    MEDIA = frozenset({".jpg", ".png", ".mp4", ".mp3"})

    def _cleaner(self, root, **kw):
        params = dict(threshold_mb=1, min_age_minutes=0)
        params.update(kw)
        return self.maint.NapCatCacheDirectoryCleaner(
            [str(root)], extensions=self.MEDIA, **params
        )

    def _make(self, base, specs):
        root = Path(base) / "media"
        root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for name, size, age in specs:
            path = root / name
            path.write_bytes(b"x" * size)
            stamp = now - age
            os.utime(path, (stamp, stamp))
        return root

    def test_non_media_files_are_never_deleted(self):
        """即使体积远超阈值，.db / .json / 无扩展名 / 未知后缀一个都不能少。"""
        with tempfile.TemporaryDirectory() as tmp:
            keep = [
                ("nt_msg.db", 900 * 1024, 86400 * 90),
                ("config.json", 900 * 1024, 86400 * 90),
                ("onebot11_1.json", 900 * 1024, 86400 * 90),
                ("README", 900 * 1024, 86400 * 90),
                ("archive.tar.zst", 900 * 1024, 86400 * 90),
                ("data.bin", 900 * 1024, 86400 * 90),
            ]
            root = self._make(tmp, keep)
            result = self._cleaner(root).clean_sync()[0]
            self.assertEqual(result["deleted_files"], 0)
            for name, _, _ in keep:
                self.assertTrue((root / name).exists(), f"{name} 不应被删")

    def test_only_media_is_counted_towards_the_threshold(self):
        """非媒体文件不计入体积，否则一个巨大的 .db 会把整层逼到阈值之上。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make(tmp, [("nt_msg.db", 10 * 1024 * 1024, 86400 * 90)])
            scans = self._cleaner(root).scan_sync()[0]
            self.assertEqual(scans.size_bytes, 0)
            self.assertEqual(scans.file_count, 0)

    def test_media_beyond_age_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make(tmp, [
                (f"old{i}.jpg", 200 * 1024, 86400 * 60) for i in range(10)
            ])
            result = self._cleaner(root).clean_sync()[0]
            self.assertGreater(result["deleted_files"], 0)
            # 删到阈值的 85% 为止，不是删光
            self.assertLessEqual(result["remaining_bytes"], 850 * 1024)
            self.assertEqual(
                len(list(root.iterdir())), 10 - result["deleted_files"]
            )

    def test_fresh_media_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make(tmp, [
                (f"f{i}.jpg", 200 * 1024, 0) for i in range(10)
            ])
            result = self._cleaner(root, min_age_minutes=60 * 24 * 30).clean_sync()[0]
            self.assertEqual(result["deleted_files"], 0)
            self.assertEqual(len(list(root.iterdir())), 10)

    def test_age_gate_applies_even_under_force(self):
        """force 也不能绕过最小保留天数：这是最后一道锁。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make(tmp, [
                (f"f{i}.jpg", 200 * 1024, 0) for i in range(10)
            ])
            result = self._cleaner(
                root, min_age_minutes=60 * 24 * 30
            ).clean_sync(force=True)[0]
            self.assertEqual(result["deleted_files"], 0)
            self.assertEqual(len(list(root.iterdir())), 10)

    def test_extension_match_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = [("SHOUT.JPG", 200 * 1024, 86400 * 90)]
            specs += [(f"n{i}.jpg", 200 * 1024, 86400 * 60) for i in range(10)]
            root = self._make(tmp, specs)
            self._cleaner(root).clean_sync()
            self.assertFalse((root / "SHOUT.JPG").exists(), "大写后缀也算命中")

    def test_default_keeps_whole_directory_semantics(self):
        """extensions=None 时行为必须和原来完全一致，不能把现有那层改坏。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make(tmp, [
                ("a.jpg", 400 * 1024, 86400 * 90),
                ("b.db", 400 * 1024, 86400 * 90),
                ("c", 400 * 1024, 86400 * 90),
            ])
            cleaner = self.maint.NapCatCacheDirectoryCleaner(
                [str(root)], threshold_mb=1, min_age_minutes=0
            )
            self.assertIsNone(cleaner.extensions)
            scans = cleaner.scan_sync()[0]
            self.assertEqual(scans.file_count, 3, "默认应把目录里所有文件都算进去")
            self.assertEqual(scans.size_bytes, 1200 * 1024)
            cleaner.clean_sync()
            survivors = {p.name for p in root.iterdir()}
            # 1200KiB -> 删 1 个即 800KiB <= 850KiB。三者 mtime 相同，按路径排序
            # 先删 a.jpg。只删了一个这件事本身就说明 .db 与无扩展名文件都被算进了
            # 体积——否则总量 400KiB 根本不超阈值，一个都不会删。
            self.assertEqual(survivors, {"b.db", "c"})

    def test_filtered_out_files_do_not_inflate_symlink_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make(tmp, [("x.db", 1024, 3600), ("y.jpg", 1024, 3600)])
            scans = self._cleaner(root).scan_sync()[0]
            self.assertEqual(scans.skipped_symlinks, 0)


class TestSpacesAndJunk(OrbitTestBase):
    """空间地图、客观垃圾、日志保留。

    这一档的删除安全全靠「不推断」：只删不满足条件就是垃圾的东西。

    stub 的 get_astrbot_data_path() 是类级别固定的，所以每个用例用
    唯一前缀建目录、tearDown 整块清掉，不互相干扰。
    """

    def setUp(self):
        # 自建数据根并打桩：类级 data_path 是全模块共享的，
        # 别的测试类会在自己的 setUpClass 里把 stub 重新指走。
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.data = Path(self._tmpdir.name) / "astrbot"
        self.logs = self.data / "logs"
        self.plugins = self.data / "plugins"
        self.logs.mkdir(parents=True)
        self.plugins.mkdir(parents=True)
        self._saved_root = self.spaces._data_root
        self.spaces._data_root = lambda: self.data
        self.addCleanup(self._restore_root)

    def _restore_root(self):
        self.spaces._data_root = self._saved_root

    @staticmethod
    def rmtree(path):
        import shutil as _sh

        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            _sh.rmtree(path, ignore_errors=True)

    def _plugin(self, name, *, complete=True, files=1, size=1024):
        path = self.plugins / name
        path.mkdir(parents=True, exist_ok=True)
        if complete:
            (path / "metadata.yaml").write_text("name: x\nversion: '1'\n")
        for i in range(files):
            (path / f"f{i}.bin").write_bytes(b"x" * size)
        return path

    @staticmethod
    def _tmp_tree(tmp):
        return Path(tmp) / "outside"

    # ---------- 空间地图 ----------

    def test_usage_map_ranks_by_size(self):
        big = self.data / "big_dir"
        big.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (big / f"f{i}").write_bytes(b"x" * 1000)
        small = self.data / "small_dir"
        small.mkdir(parents=True, exist_ok=True)
        (small / "a").write_bytes(b"x" * 10)
        out = self.spaces.usage_map_sync(media_dirs=[], cache_dirs=[])
        group = next(g for g in out["groups"] if g["title"] == "AstrBot 数据目录")
        sizes = [r["size_bytes"] for r in group["rows"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True), "必须按体积从大到小排")
        by_label = {r["label"]: r["size_bytes"] for r in group["rows"]}
        self.assertEqual(by_label.get("big_dir"), 5000)
        self.assertEqual(by_label.get("small_dir"), 10)
        self.assertGreater(out["total_bytes"], 0)
        self.rmtree(big)
        self.rmtree(small)

    def test_usage_map_never_writes(self):
        """只读：统计前后的目录内容必须完全一致。"""
        probe = self._plugin("probe", files=2, size=2048)
        before = {
            str(p.relative_to(self.data)): p.stat().st_mtime_ns
            for p in self.data.rglob("*")
            if p.is_file()
        }
        self.spaces.usage_map_sync(media_dirs=[], cache_dirs=[])
        after = {
            str(p.relative_to(self.data)): p.stat().st_mtime_ns
            for p in self.data.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after, "空间地图不许动任何文件")
        self.rmtree(probe)

    def test_usage_map_includes_configured_media_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = self._tmp_tree(tmp)
            media.mkdir()
            (media / "a.jpg").write_bytes(b"x" * 2000)
            out = self.spaces.usage_map_sync(media_dirs=[str(media)], cache_dirs=[])
            group = next(
                (g for g in out["groups"] if g["title"] == "你配置的自选媒体目录"), None
            )
            self.assertIsNotNone(group, out["groups"])
            self.assertEqual(group["rows"][0]["size_bytes"], 2000)

    # ---------- 客观垃圾 ----------

    def test_broken_dir_without_metadata_is_junk(self):
        good = self._plugin("good")
        broken = self._plugin("broken", complete=False)
        out = self.spaces.plugin_junk_sync()
        self.assertIn(str(broken), [r["path"] for r in out["broken"]])
        self.assertNotIn(str(good), [r["path"] for r in out["broken"]])
        self.assertGreater(out["total_bytes"], 0)

    def test_complete_plugin_is_never_junk(self):
        self._plugin("good", files=3)
        out = self.spaces.plugin_junk_sync()
        self.assertEqual(
            out["broken"], [], "只装了完整插件，不该被判为未装完"
        )

    def test_leftover_zip_and_pycache_are_junk(self):
        good = self._plugin("good")
        zipped = self.plugins / "installer.zip"
        zipped.write_bytes(b"z" * 500)
        cache = good / "__pycache__"
        cache.mkdir()
        (cache / "m.cpython-311.pyc").write_bytes(b"c" * 700)
        out = self.spaces.plugin_junk_sync()
        self.assertIn(str(zipped), [r["path"] for r in out["zips"]])
        self.assertIn(str(cache), [r["path"] for r in out["pycache"]])
        self.assertEqual(
            [r["size_bytes"] for r in out["zips"] if r["path"] == str(zipped)], [500]
        )
        self.rmtree(zipped)
        self.rmtree(good)

    def test_junk_under_missing_plugins_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = self._tmp_tree(tmp)
            (missing / "plugins").mkdir(parents=True)
            saved = self.spaces._data_root
            self.spaces._data_root = lambda: missing
            try:
                out = self.spaces.plugin_junk_sync()
                self.assertTrue(out["root_exists"])
                self.assertEqual(out["total_bytes"], 0)
                missing2 = self._tmp_tree(tmp) / "nope"
                self.spaces._data_root = lambda: missing2
                out2 = self.spaces.plugin_junk_sync()
                self.assertFalse(out2["root_exists"])
                self.assertEqual(out2["total_bytes"], 0)
            finally:
                self.spaces._data_root = saved

    def test_remove_junk_works_for_each_kind(self):
        broken = self._plugin("broken", complete=False)
        good = self._plugin("good")
        zipped = self.plugins / "installer.zip"
        zipped.write_bytes(b"z" * 500)
        cache = good / "__pycache__"
        cache.mkdir()
        (cache / "m.pyc").write_bytes(b"c" * 700)

        r1 = self.spaces.remove_junk_sync("broken", str(broken))
        self.assertTrue(r1["ok"], r1)
        self.assertFalse(broken.exists())
        self.assertTrue(good.exists(), "不能连带删掉正常插件")

        r2 = self.spaces.remove_junk_sync("zip", str(zipped))
        self.assertTrue(r2["ok"], r2)
        self.assertFalse(zipped.exists())

        r3 = self.spaces.remove_junk_sync("pycache", str(cache))
        self.assertTrue(r3["ok"], r3)
        self.assertFalse(cache.exists())
        self.assertTrue((good / "metadata.yaml").exists())
        self.rmtree(good)

    def test_remove_junk_refuses_outside_plugins_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = self._tmp_tree(tmp)
            victim = outside / "precious"
            victim.mkdir(parents=True)
            (victim / "keep.txt").write_text("别删我")
            for kind, target in (
                ("broken", str(victim)),
                ("zip", str(victim / "keep.txt")),
                ("pycache", str(self.data)),
                ("broken", str(self.plugins)),
            ):
                out = self.spaces.remove_junk_sync(kind, target)
                self.assertFalse(out["ok"], f"{kind} {target} 不该被删")
            self.assertTrue((victim / "keep.txt").exists())
            self.assertTrue(self.plugins.exists())

    def test_remove_junk_refuses_wrong_kind(self):
        """正常插件目录不能靠谎报类型删掉。"""
        good = self._plugin("good")
        for kind in ("broken", "zip", "pycache"):
            out = self.spaces.remove_junk_sync(kind, str(good))
            self.assertFalse(out["ok"], f"{kind} 不该能删正常插件")
        self.assertTrue((good / "metadata.yaml").exists())

    def test_remove_junk_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = self._tmp_tree(tmp)
            outside.mkdir()
            (outside / "x.txt").write_text("x")
            link = self.plugins / "linked"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("当前环境不支持创建符号链接")
            out = self.spaces.remove_junk_sync("broken", str(link))
            self.assertFalse(out["ok"])
            self.assertTrue((outside / "x.txt").exists())
            self.rmtree(link)

    def test_remove_junk_refuses_symlinked_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = self._tmp_tree(tmp)
            outside.mkdir()
            real = outside / "real.zip"
            real.write_bytes(b"z" * 100)
            link = self.plugins / "fake.zip"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("当前环境不支持创建符号链接")
            out = self.spaces.remove_junk_sync("zip", str(link))
            self.assertFalse(out["ok"], "不能顺着符号链接把外面的文件删了")
            self.assertTrue(real.exists())
            self.rmtree(link)

    # ---------- 日志保留 ----------

    def _log(self, name, size, age_days):
        path = self.logs / name
        path.write_bytes(b"x" * size)
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_retention_keeps_recent_and_current_logs(self):
        old = self._log("astrbot.log.1", 1000, 40)
        fresh = self._log("astrbot.log.2", 1000, 0.04)
        # 真正叫 astrbot.log 的那个不能带 tag，所以单独造再删
        current = self.logs / "astrbot.log"
        current.write_bytes(b"c" * 2000)
        stamp = time.time() - 90 * 86400
        os.utime(current, (stamp, stamp))
        try:
            plan = self.spaces.log_retention_plan_sync(7)
            names = {Path(p["path"]).name for p in plan["candidates"]}
            self.assertEqual(
                names, {old.name},
                "只删超期的轮转日志，当前日志即使很旧也不动",
            )
            self.assertEqual(plan["kept_current"], 1)
        finally:
            self.rmtree(current)
            self.rmtree(old)
            self.rmtree(fresh)

    def test_retention_zero_means_threshold_mode(self):
        old = self._log("astrbot.log.1", 1000, 400)
        plan = self.spaces.log_retention_plan_sync(0)
        self.assertEqual(plan["mode"], "threshold")
        self.assertEqual(plan["candidates"], [])
        self.rmtree(old)

    def test_retention_clean_deletes_only_expired(self):
        old = self._log("astrbot.log.1", 1000, 40)
        fresh = self._log("astrbot.log.2", 1000, 0.04)
        current = self.logs / "astrbot.log"
        current.write_bytes(b"c" * 2000)
        try:
            result = self.spaces.log_retention_clean_sync(7)
            self.assertEqual(result["deleted_files"], 1)
            self.assertEqual(result["freed_bytes"], 1000)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(current.exists(), "当前日志绝不能动")
        finally:
            self.rmtree(current)
            self.rmtree(fresh)

    def test_logs_layer_uses_retention_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, log_retention_days=7)
            self.assertEqual(engine.settings()["log_retention_days"], 7)
            _, error = engine.update_settings({"log_retention_days": -1})
            self.assertTrue(error)
            self.assertEqual(engine.settings()["log_retention_days"], 7)

    def test_dryrun_includes_retention_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, log_retention_days=7)
            report = asyncio.run(engine.dry_run())
            self.assertIn("log_retention", report)
            self.assertEqual(report["log_retention"]["mode"], "retention")


class TestMediaLayer(OrbitTestBase):
    """自选目录层在引擎里的接线：默认关着、阈值独立、非法目录当场报错。"""

    def test_layer_is_off_without_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            overview = asyncio.run(engine.collect())
            layer = overview["layers"]["media_files"]
            self.assertFalse(layer["enabled"])
            self.assertFalse(layer["due"])
            self.assertIn("未填目录", layer["amount"])

    def test_sweep_skips_when_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            result = asyncio.run(engine.sweep(layers=["media_files"]))
            record = result["records"][0]
            self.assertFalse(record["acted"])
            self.assertIn("默认关着", record["skipped"])

    def test_filling_a_dir_enables_the_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pics"
            root.mkdir()
            engine, _, _ = self._engine(tmp)
            engine._config["media_dirs"] = [str(root)]
            overview = asyncio.run(engine.collect())
            self.assertTrue(overview["layers"]["media_files"]["enabled"])

    def test_media_never_touches_the_astrbot_data_dir(self):
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            _, error = engine.update_settings({"media_dirs": [str(P.cwd())]})
            self.assertIn("自选目录配置有误", error)

    def test_media_threshold_is_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(
                tmp, media_threshold_mb=1, napcat_cache_dirs=[]
            )
            self.assertEqual(engine.settings()["media_threshold_mb"], 1)
            self.assertEqual(engine.settings()["media_min_age_days"], 30)

    def test_media_settings_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            _, error = engine.update_settings({"media_min_age_days": 0})
            self.assertTrue(error)
            self.assertEqual(engine.settings()["media_min_age_days"], 30)
            settings, error = engine.update_settings({"media_min_age_days": 7})
            self.assertEqual(error, "")
            self.assertEqual(settings["media_min_age_days"], 7)

    def test_dryrun_covers_media_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pics"
            root.mkdir()
            now = time.time()
            for i in range(10):
                p = root / f"old{i}.jpg"
                p.write_bytes(b"x" * (200 * 1024))
                os.utime(p, (now - 86400 * 60, now - 86400 * 60))
            # 2000KiB -> 850KiB 目标，与现有文件缓存层同一套算法，应删 6 个
            engine, _, _ = self._engine(
                tmp, media_dirs=[str(root)], media_threshold_mb=1
            )
            report = asyncio.run(engine.dry_run())
            self.assertEqual(report["media_would_delete_files"], 6)
            self.assertEqual(len(report["media_directories"]), 1)
            self.assertTrue(report["media_directories"][0]["samples"])

    def test_dryrun_media_ignores_fresh_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pics"
            root.mkdir()
            for i in range(10):
                (root / f"today{i}.jpg").write_bytes(b"x" * (200 * 1024))
            engine, _, _ = self._engine(
                tmp,
                media_dirs=[str(root)],
                media_threshold_mb=1,
                media_min_age_days=30,
            )
            report = asyncio.run(engine.dry_run())
            self.assertEqual(report["media_would_delete_files"], 0)


# ---------- 2. 插件模块清理 ----------

class TestModuleCache(OrbitTestBase):
    def test_purge_only_touches_exact_prefix(self):
        sys.modules["data.plugins.test_a"] = types.ModuleType("x")
        sys.modules["data.plugins.test_a.sub"] = types.ModuleType("x")
        sys.modules["data.plugins.test_ab"] = types.ModuleType("x")
        mgr = self.maint.ModuleCacheManager(_Context(), "test_self")
        removed = mgr.purge_loaded("test_a")
        self.assertEqual(removed, ["data.plugins.test_a", "data.plugins.test_a.sub"])
        self.assertIn("data.plugins.test_ab", sys.modules, "同前缀但不同插件不能被误删")

    def test_purge_refuses_self_and_bad_id(self):
        mgr = self.maint.ModuleCacheManager(_Context(), "test_self")
        with self.assertRaises(self.maint.ModuleCacheError):
            mgr.purge_loaded("test_self")
        with self.assertRaises(self.maint.ModuleCacheError):
            mgr.purge_loaded("not-an-identifier")
        with self.assertRaises(self.maint.ModuleCacheError):
            mgr.purge_loaded("class")  # Python 关键字

    def test_clean_plugin_requires_inactive(self):
        sys.modules["data.plugins.test_on"] = types.ModuleType("x")
        active = self.maint.ModuleCacheManager(
            _Context([_StarMeta("test_on", activated=True)]), "test_self"
        )
        with self.assertRaises(self.maint.ModuleCacheError):
            active.clean_plugin("test_on")
        self.assertIn("data.plugins.test_on", sys.modules)

    def test_clean_inactive_skips_active_and_self(self):
        sys.modules["data.plugins.test_off"] = types.ModuleType("x")
        sys.modules["data.plugins.test_on2"] = types.ModuleType("x")
        sys.modules["data.plugins.test_self"] = types.ModuleType("x")
        mgr = self.maint.ModuleCacheManager(
            _Context([
                _StarMeta("test_off", activated=False),
                _StarMeta("test_on2", activated=True),
                _StarMeta("test_self", activated=False),
            ]),
            "test_self",
        )
        result = mgr.clean_inactive_plugins()
        self.assertIn("test_off", result["cleaned"])
        self.assertNotIn("test_on2", result["cleaned"])
        self.assertNotIn("test_self", result["cleaned"])
        self.assertIn("data.plugins.test_on2", sys.modules)
        self.assertNotIn("data.plugins.test_off", sys.modules)

    def test_is_active_reflects_metadata(self):
        mgr = self.maint.ModuleCacheManager(
            _Context([
                _StarMeta("test_on", activated=True),
                _StarMeta("test_off", activated=False),
            ]),
            "test_self",
        )
        self.assertTrue(mgr.is_active("test_on"))
        self.assertFalse(mgr.is_active("test_off"))
        # 不在注册表里的（残留模块）视为不活跃，可清理
        self.assertFalse(mgr.is_active("test_ghost"))
        with self.assertRaises(self.maint.ModuleCacheError):
            mgr.is_active("bad-id")

    def test_describe_shape(self):
        sys.modules["data.plugins.test_desc"] = types.ModuleType("x")
        mgr = self.maint.ModuleCacheManager(
            _Context([_StarMeta("test_desc", activated=False)]), "test_self"
        )
        row = next(r for r in mgr.describe() if r["plugin_id"] == "test_desc")
        self.assertEqual(
            set(row),
            {"scope", "plugin_id", "display_name", "version", "activated", "module_count", "self"},
        )
        self.assertFalse(row["activated"])
        self.assertEqual(row["module_count"], 1)


# ---------- 3. SweepEngine 条件判定 ----------

class _FakeEndpoint:
    def __init__(self, url, token="", enable=True, protocol_interval_hours=12):
        self.url = url
        self.token = token
        self.enable = enable
        self.protocol_interval_hours = protocol_interval_hours

    @property
    def label(self):
        return self.url


class _FakeNapCat:
    """多实例版 NapCatCacheClient 的替身。"""

    def __init__(self, endpoints=None, connected=True, accounts=None):
        if endpoints is None:
            endpoints = [_FakeEndpoint("http://127.0.0.1:3000", "", True, 12)]
        self.connected = connected
        self.protocol_cleans = 0
        self.endpoints = list(endpoints)
        self.accounts = dict(accounts or {})
        self.login_calls = 0
        self.status_calls = 0
        self._by_url = {e.url: e for e in self.endpoints}
        self._last_errors = {}

    def _endpoint_by_url(self, url):
        return self._by_url.get(url)

    def set_token(self, url, token):
        ep = self._by_url.get(url)
        if ep is not None:
            ep.token = token

    @property
    def enabled_endpoints(self):
        return [e for e in self.endpoints if getattr(e, "enable", True)]

    @property
    def endpoint_label(self):
        if not self.endpoints:
            return "未配置"
        if len(self.endpoints) == 1:
            return self.endpoints[0].label
        return f"{len(self.endpoints)} 个实例"

    async def _call(self, endpoint, action):
        if endpoint.url in self._last_errors:
            raise self._last_errors.pop(endpoint.url)
        if action == "clean_cache":
            self.protocol_cleans += 1
        if action == "get_login_info":
            self.login_calls += 1
            account = self.accounts.get(endpoint.url)
            if account is None:
                raise RuntimeError("取不到登录信息")
            return {"user_id": account, "nickname": "bot"}
        return {"ok": True}

    async def status(self):
        self.status_calls += 1
        rows = []
        for ep in self.endpoints:
            rows.append({
                "url": ep.label, "enable": getattr(ep, "enable", True),
                "connected": self.connected, "error": "" if self.connected else "boom",
            })
        return rows

    async def clean_protocol_cache(self):
        out = []
        for ep in self.enabled_endpoints:
            await self._call(ep, "clean_cache")
            out.append({"url": ep.label, "acted": True, "error": "", "detail": "ok"})
        return out


class _FakeAstrBotCache:
    """上游 StorageCleaner.get_status() 一次就返回 logs 与 cache 两块，形状要照实。"""

    def __init__(self, size=0, logs_size=0):
        self.size = size
        self.logs_size = logs_size
        self.cleans = 0
        self.cleaned_targets = []

    def _block(self, target):
        size = self.logs_size if target == "logs" else self.size
        return {"size_bytes": size, "file_count": 1, "path": "x"}

    async def status(self):
        cache = self._block("cache")
        logs = self._block("logs")
        return {
            "logs": logs,
            "cache": cache,
            "total_bytes": cache["size_bytes"] + logs["size_bytes"],
        }

    async def size_bytes(self, target="cache"):
        return self._block(target)["size_bytes"]

    async def clean(self, target="cache"):
        """释放多少得与实际占用一致：真实 StorageCleaner 不会凭空变出字节。"""
        self.cleans += 1
        self.cleaned_targets.append(target)
        freed = self.logs_size if target == "logs" else self.size
        if target == "logs":
            self.logs_size = 0
        else:
            self.size = 0
        return {
            "removed_bytes": freed,
            "processed_files": 1 if freed else 0,
            "failed_files": 0,
        }


class _FakeModuleCache:
    def __init__(self):
        self.purged = 0
        self.active_ids = set()

    def clean_inactive_plugins(self):
        if self.purged:
            return {"cleaned": {}, "skipped": []}
        self.purged += 1
        return {"cleaned": {"test_x": ["data.plugins.test_x"]}, "skipped": []}

    def purge_loaded(self, plugin_id):
        return [f"data.plugins.{plugin_id}"]

    def describe(self):
        return [{"scope": "data.plugins", "plugin_id": "test_x", "display_name": "X",
                 "version": "1", "activated": False, "module_count": 1, "self": False}]

    def is_active(self, plugin_id):
        return plugin_id in self.active_ids


class TestSweepEngine(OrbitTestBase):
    def test_conditions_gate_every_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            result = asyncio.run(engine.sweep())
            self.assertTrue(result["ok"], result.get("error"))
            by_layer = {r["layer"]: r for r in result["records"]}
            # 缓存两层必须等阈值达成
            for layer in ("astrbot", "napcat_files"):
                self.assertFalse(
                    by_layer[layer]["acted"], f"{layer} 不该在未达阈值时动手"
                )
                self.assertTrue(by_layer[layer]["skipped"], f"{layer} 应记录未处理原因")
            # 模块层是例外：清内存零风险，只要有就清
            self.assertTrue(by_layer["modules"]["acted"])
            # 协议层首次运行必然执行，之后受间隔约束
            self.assertTrue(by_layer["napcat_protocol"]["acted"])
            self.assertEqual(result["freed_bytes"], 0)

    def test_protocol_interval_blocks_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, napcat = self._engine(tmp)
            first = asyncio.run(engine.sweep(force=False))
            by_layer = {r["layer"]: r for r in first["records"]}
            self.assertTrue(by_layer["napcat_protocol"]["acted"])
            self.assertEqual(napcat.protocol_cleans, 1)

            second = asyncio.run(engine.sweep(force=False))
            by_layer = {r["layer"]: r for r in second["records"]}
            self.assertFalse(by_layer["napcat_protocol"]["acted"])
            self.assertIn("未到清理时间", by_layer["napcat_protocol"]["skipped"])
            self.assertEqual(napcat.protocol_cleans, 1, "间隔内不应重复调用 NapCat")

    def test_force_acts_regardless_of_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            engine.astrbot_cache = _FakeAstrBotCache(size=4096)
            result = asyncio.run(engine.sweep(force=True))
            acted = {r["layer"] for r in result["records"] if r["acted"]}
            self.assertIn("modules", acted)
            self.assertIn("astrbot", acted)
            self.assertIn("napcat_protocol", acted)
            self.assertGreater(result["freed_bytes"], 0)

    def test_single_layer_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            result = asyncio.run(engine.sweep(force=True, layers=["astrbot"]))
            self.assertEqual([r["layer"] for r in result["records"]], ["astrbot"])

    def test_astrbot_triggers_only_over_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, astrbot_cache_threshold_mb=1024)
            result = asyncio.run(engine.sweep(layers=["astrbot"]))
            self.assertFalse(result["records"][0]["acted"])
            self.assertIn("未超过阈值", result["records"][0]["skipped"])

    def test_astrbot_logs_has_its_own_threshold(self):
        """日志与缓存阈值互相独立：两边超没超各不相同。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(
                tmp, astrbot_cache_threshold_mb=1, astrbot_logs_threshold_mb=1
            )
            # 缓存超 1 MiB，日志远低于 1 MiB
            engine.astrbot_cache = _FakeAstrBotCache(
                size=4 * 1024 * 1024, logs_size=1024
            )
            result = asyncio.run(engine.sweep(layers=["astrbot", "astrbot_logs"]))
            by_layer = {r["layer"]: r for r in result["records"]}
            self.assertTrue(by_layer["astrbot"]["acted"])
            self.assertFalse(by_layer["astrbot_logs"]["acted"])
            self.assertIn("未超过阈值", by_layer["astrbot_logs"]["skipped"])

    def test_astrbot_logs_cleans_its_own_target(self):
        """清日志必须传 logs，传错成 cache 会把另一层一起清掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, astrbot_logs_threshold_mb=1)
            cache = _FakeAstrBotCache(size=4096, logs_size=4 * 1024 * 1024)
            engine.astrbot_cache = cache
            asyncio.run(engine.sweep(layers=["astrbot_logs"]))
            self.assertEqual(cache.cleaned_targets, ["logs"])
            self.assertEqual(cache.logs_size, 0)
            self.assertEqual(cache.size, 4096, "只清 logs 不应顺带碰 cache")

    def test_astrbot_logs_size_is_counted_in_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, astrbot_logs_threshold_mb=1)
            engine.astrbot_cache = _FakeAstrBotCache(
                size=2048, logs_size=4 * 1024 * 1024
            )
            overview = asyncio.run(engine.collect())
            self.assertEqual(
                overview["totals"]["reclaimable_bytes"], 2048 + 4 * 1024 * 1024
            )
            layer = overview["layers"]["astrbot_logs"]
            self.assertEqual(layer["current"], 4 * 1024 * 1024)
            self.assertTrue(layer["due"])

    def test_dryrun_reports_both_astrbot_targets(self):
        """试运行是「到底会不会真的删东西」的唯一依据，漏报一层就是骗人。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, astrbot_logs_threshold_mb=1)
            engine.astrbot_cache = _FakeAstrBotCache(
                size=0, logs_size=4 * 1024 * 1024
            )
            report = asyncio.run(engine.dry_run())
            self.assertEqual(report["astrbot_logs_size_bytes"], 4 * 1024 * 1024)
            self.assertEqual(report["astrbot_logs_threshold_bytes"], 1024 * 1024)

    def test_logs_threshold_is_validated_like_the_others(self):
        """越界值只报错不写入：沿用其它可调项「不钳制」的既有行为。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            _, error = engine.update_settings({"astrbot_logs_threshold_mb": 0})
            self.assertTrue(error)
            self.assertEqual(engine.settings()["astrbot_logs_threshold_mb"], 16)
            settings, error = engine.update_settings({"astrbot_logs_threshold_mb": 32})
            self.assertEqual(error, "")
            self.assertEqual(settings["astrbot_logs_threshold_mb"], 32)

    def test_acted_is_false_when_nothing_was_freed(self):
        """force 跑完但一个字节没释放时不能报「已清理」——
        之前无条件 acted=True，面弹窗显示「N 层执行了清理，释放 0 B」，
        看着就像按钮没反应。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, astrbot_cache_threshold_mb=1)
            cache = _FakeAstrBotCache(size=0, logs_size=0)
            engine.astrbot_cache = cache
            result = asyncio.run(engine.sweep(force=True, layers=["astrbot", "astrbot_logs"]))
            by_layer = {r["layer"]: r for r in result["records"]}
            for layer in ("astrbot", "astrbot_logs"):
                self.assertFalse(by_layer[layer]["acted"], f"{layer} 不该谎报已清理")
                self.assertIn("已执行", by_layer[layer]["skipped"])
            self.assertEqual(result["freed_bytes"], 0)
            self.assertEqual(result["acted"], [])
            # 接口确实被调到了，只是没什么可删
            self.assertEqual(sorted(cache.cleaned_targets), ["cache", "logs"])

    def test_total_freed_bytes_accumulates(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, astrbot_cache_threshold_mb=1)
            engine.astrbot_cache = _FakeAstrBotCache(size=4096)
            asyncio.run(engine.sweep(force=True, layers=["astrbot"]))
            first = asyncio.run(engine.collect())["totals"]
            self.assertEqual(first["last_freed_bytes"], 4096)
            self.assertEqual(first["total_freed_bytes"], 4096)
            engine.astrbot_cache = _FakeAstrBotCache(size=2048)
            asyncio.run(engine.sweep(force=True, layers=["astrbot"]))
            second = asyncio.run(engine.collect())["totals"]
            self.assertEqual(second["last_freed_bytes"], 2048)
            self.assertEqual(second["total_freed_bytes"], 6144, "累计值应累加")

    def test_total_freed_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, astrbot_cache_threshold_mb=1)
            engine.astrbot_cache = _FakeAstrBotCache(size=4096)
            asyncio.run(engine.sweep(force=True, layers=["astrbot"]))
            reopened = self.sched.SweepEngine(
                config=engine._config,
                napcat_client=_FakeNapCat(),
                astrbot_cache=_FakeAstrBotCache(),
                module_cache=_FakeModuleCache(),
                data_dir=Path(tmp),
            )
            self.assertEqual(reopened._state["total_freed_bytes"], 4096)

    def test_layer_failure_does_not_block_others(self):
        class Boom(_FakeAstrBotCache):
            async def size_bytes(self, target="cache"):
                raise RuntimeError("disk on fire")

        with tempfile.TemporaryDirectory() as tmp:
            config = _Config(
                napcat_cache_dirs=[], astrbot_cache_threshold_mb=1,
                napcat_protocol_interval_hours=1, auto_enabled=True,
                auto_clean_plugin_modules=True, sweep_cron="*/15 * * * *",
            )
            engine = self.sched.SweepEngine(
                config=config,
                napcat_client=_FakeNapCat(),
                astrbot_cache=Boom(),
                module_cache=_FakeModuleCache(),
                data_dir=Path(tmp),
            )
            result = asyncio.run(engine.sweep(force=True))
            self.assertFalse(result["ok"])
            by_layer = {r["layer"]: r for r in result["records"]}
            self.assertIn("disk on fire", by_layer["astrbot"]["error"])
            self.assertTrue(by_layer["napcat_protocol"]["acted"], "其他层应继续执行")

    def test_state_and_history_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            asyncio.run(engine.sweep(force=True))
            state = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            history = json.loads((Path(tmp) / "history.json").read_text(encoding="utf-8"))
            self.assertEqual(state["sweep_count"], 1)
            self.assertEqual(len(history), len(self.sched.LAYERS))
            reopened = self.sched.SweepEngine(
                config=engine._config,
                napcat_client=_FakeNapCat(),
                astrbot_cache=_FakeAstrBotCache(),
                module_cache=_FakeModuleCache(),
                data_dir=Path(tmp),
            )
            self.assertEqual(reopened.history(10)[0]["layer"], history[0]["layer"])

    def test_settings_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, config, _ = self._engine(tmp)
            _, error = engine.update_settings({"sweep_cron": "bad cron"})
            self.assertIn("五段", error)
            self.assertEqual(engine.sweep_cron, "*/15 * * * *")
            settings, error = engine.update_settings(
                {"sweep_cron": "30 3 * * *", "astrbot_cache_threshold_mb": 0}
            )
            self.assertTrue(error)
            self.assertEqual(settings["sweep_cron"], "30 3 * * *")
            self.assertEqual(settings["astrbot_cache_threshold_mb"], 1)
            self.assertGreater(config.saved, 0)

    def test_overview_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            overview = asyncio.run(engine.collect())
            self.assertEqual(set(overview["layers"]), set(self.sched.LAYERS))
            for key in ("schedule", "totals", "napcat", "module_rows"):
                self.assertIn(key, overview)
            text = self.fmt.format_overview(overview)
            self.assertIn("AstrBot 磁盘缓存", text)
            self.assertIn("未注册", text)


# ---------- 4. 目录探测 ----------

class TestDeadInstances(OrbitTestBase):
    """死实例探测：三条判据的置信度分档，以及「删错了比不删更糟」的底线。"""

    @staticmethod
    def _client(urls, **kw):
        return _FakeNapCat(
            endpoints=[_FakeEndpoint(u, "", True, 12) for u in urls], **kw
        )

    def _collect(self, engine):
        """跑一次 collect() 推进失联计数（时间闸在这里没意义，临时放开）。"""
        saved = self.sched._REACH_ADVANCE_INTERVAL
        self.sched._REACH_ADVANCE_INTERVAL = 0.0
        try:
            return asyncio.run(engine.collect())
        finally:
            self.sched._REACH_ADVANCE_INTERVAL = saved

    def test_duplicate_url_is_high_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(["http://a:3000", "http://a:3000", "http://b:3000"])
            engine, _, _ = self._engine(tmp, napcat=client)
            rows = asyncio.run(engine.dead_instances())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["url"], "http://a:3000")
            self.assertEqual(rows[0]["index"], 1, "保留第一条，应标记后出现的")
            self.assertEqual(rows[0]["occurrence"], 1)
            self.assertTrue(rows[0]["removable"])
            self.assertIn("完全相同", rows[0]["reason"])

    def test_three_identical_urls_report_each_extra_occurrence(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(
                ["http://a:3000", "http://a:3000", "http://a:3000"]
            )
            engine, _, _ = self._engine(tmp, napcat=client)
            rows = asyncio.run(engine.dead_instances())
            self.assertEqual([r["occurrence"] for r in rows], [1, 2])
            self.assertEqual([r["index"] for r in rows], [1, 2])
            for row in rows:
                self.assertIn("第 1 行", row["reason"])

    def test_same_account_across_urls_is_high_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(
                ["http://a:3000", "http://b:3000"],
                accounts={"http://a:3000": "12345", "http://b:3000": "12345"},
            )
            engine, _, _ = self._engine(tmp, napcat=client)
            rows = asyncio.run(engine.dead_instances())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["url"], "http://b:3000")
            self.assertTrue(rows[0]["removable"])
            self.assertIn("12345", rows[0]["reason"])

    def test_distinct_accounts_produce_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(
                ["http://a:3000", "http://b:3000"],
                accounts={"http://a:3000": "111", "http://b:3000": "222"},
            )
            engine, _, _ = self._engine(tmp, napcat=client)
            self.assertEqual(asyncio.run(engine.dead_instances()), [])

    def test_unreachable_is_never_removable(self):
        """连不上与已停用无法区分，绝不能给删除按钮。"""
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(
                ["http://a:3000", "http://b:3000"], connected=False
            )
            engine, _, _ = self._engine(tmp, napcat=client)
            for _ in range(4):
                self._collect(engine)
            rows = asyncio.run(engine.dead_instances())
            self.assertTrue(rows, "连续多次失联应被提示")
            for row in rows:
                self.assertEqual(row["confidence"], "low")
                self.assertFalse(row["removable"])

    def test_sweep_does_not_probe_reachability(self):
        """sweep 里那轮多余探测就是「点强制清理像没反应」的元凶，不能回来。"""
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(["http://a:3000", "http://b:3000"])
            engine, _, _ = self._engine(tmp, napcat=client)
            before = client.status_calls
            asyncio.run(engine.sweep(force=True))
            self.assertEqual(
                client.status_calls, before, "sweep 不应再单独探测一次可达性"
            )

    def test_counter_advances_from_collect_not_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(["http://a:3000", "http://b:3000"], connected=False)
            engine, _, _ = self._engine(tmp, napcat=client)
            for _ in range(4):
                asyncio.run(engine.sweep(layers=[]))
            self.assertEqual(engine._unreachable_counters(), {})
            self._collect(engine)
            self.assertEqual(
                set(engine._unreachable_counters()), {"http://a:3000", "http://b:3000"}
            )

    def test_counter_advance_is_time_gated(self):
        """面板每 30 秒刷一次；不设闸的话半小时就能把计数推到阈值。"""
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(["http://a:3000", "http://b:3000"], connected=False)
            engine, _, _ = self._engine(tmp, napcat=client)
            for _ in range(20):
                asyncio.run(engine.collect())
            # 第一次必进（建立基线），之后被闸挡住：20 次刷新只累计 1，
            # 没有闸的话这里会是 20，早就超过「连续 3 次」的提示门槛。
            self.assertEqual(engine._unreachable_counters(), {
                "http://a:3000": 1, "http://b:3000": 1,
            })

    def test_panel_read_does_not_advance_the_counter(self):
        """面板每 30 秒刷一次；刷得勤不等于 bot 死得多。"""
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(["http://a:3000", "http://b:3000"], connected=False)
            engine, _, _ = self._engine(tmp, napcat=client)
            for _ in range(20):
                asyncio.run(engine.dead_instances())  # 只读，不推进
            self.assertEqual(engine._unreachable_counters(), {})
            self.assertEqual(asyncio.run(engine.dead_instances()), [])

    def test_counter_stops_growing_once_reachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(["http://a:3000", "http://b:3000"], connected=False)
            engine, _, _ = self._engine(tmp, napcat=client)
            self._collect(engine)
            self._collect(engine)
            self.assertTrue(engine._unreachable_counters())
            client.connected = True
            self._collect(engine)
            self.assertEqual(engine._unreachable_counters(), {})

    def test_counter_drops_urls_no_longer_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(["http://a:3000", "http://b:3000"], connected=False)
            engine, _, _ = self._engine(tmp, napcat=client)
            self._collect(engine)
            self.assertEqual(
                set(engine._unreachable_counters()), {"http://a:3000", "http://b:3000"}
            )
            client.endpoints = client.endpoints[:1]
            self._collect(engine)
            self.assertEqual(set(engine._unreachable_counters()), {"http://a:3000"})

    def test_single_instance_is_never_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(["http://a:3000"])
            engine, _, _ = self._engine(tmp, napcat=client)
            self.assertEqual(asyncio.run(engine.dead_instances()), [])

    def test_probe_failure_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(["http://a:3000", "http://b:3000"])
            client._last_errors = {}
            engine, _, _ = self._engine(tmp, napcat=client)
            # accounts 为空 → 每个 get_login_info 都抛异常
            self.assertEqual(asyncio.run(engine.dead_instances()), [])

    def test_remove_touches_config_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "http://a:3000", "token": "t1", "enable": True,
                 "protocol_interval_hours": 12},
                {"url": "http://b:3000", "token": "t2", "enable": True,
                 "protocol_interval_hours": 12},
                {"url": "http://c:3000", "token": "t3", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            self._set_request_json({"url": "http://b:3000"})
            response = asyncio.run(plugin.api_remove_instance())
            payload = response["__json__"]
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["remaining"], 2)
            kept = [row["url"] for row in plugin.config["onebot_instances"]]
            self.assertEqual(kept, ["http://a:3000", "http://c:3000"])

    def test_remove_matches_url_not_index(self):
        """原始配置里若有空 url 行，下标就对不齐；必须按 url 匹配。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "", "token": "", "enable": True, "protocol_interval_hours": 12},
                {"url": "http://a:3000", "token": "t1", "enable": True,
                 "protocol_interval_hours": 12},
                {"url": "http://b:3000", "token": "t2", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            self._set_request_json({"url": "http://a:3000"})
            payload = asyncio.run(plugin.api_remove_instance())["__json__"]
            self.assertTrue(payload["ok"])
            kept = [row["url"] for row in plugin.config["onebot_instances"]]
            self.assertEqual(kept, ["", "http://b:3000"], "只能删 url 匹配的那一行")

    def test_remove_rejects_unknown_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "http://a:3000", "token": "t1", "enable": True,
                 "protocol_interval_hours": 12},
                {"url": "http://b:3000", "token": "t2", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            self._set_request_json({"url": "http://evil:3000"})
            response = asyncio.run(plugin.api_remove_instance())
            self.assertIn("__error__", response)
            self.assertEqual(len(plugin.config["onebot_instances"]), 2)

    def test_remove_refuses_the_last_instance(self):
        """删光实例等于让机器人彻底哑掉；停用有 enable 开关，不必走删除。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "http://a:3000", "token": "t1", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            self._set_request_json({"url": "http://a:3000"})
            response = asyncio.run(plugin.api_remove_instance())
            self.assertIn("__error__", response)
            self.assertIn("最后一个实例", response["__error__"])
            self.assertEqual(len(plugin.config["onebot_instances"]), 1)

    def test_remove_requires_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "http://a:3000", "token": "t", "enable": True,
                 "protocol_interval_hours": 12},
                {"url": "http://b:3000", "token": "t2", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            self._set_request_json({})
            self.assertIn("__error__", asyncio.run(plugin.api_remove_instance()))

    def test_remove_keeps_first_duplicate_when_asked(self):
        """同地址配了多行时，删的是「第 occurrence 个」，不能误删要保留的那一行。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "http://a:3000", "token": "keep", "enable": True,
                 "protocol_interval_hours": 12},
                {"url": "http://a:3000", "token": "drop-me", "enable": True,
                 "protocol_interval_hours": 12},
                {"url": "http://b:3000", "token": "t3", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            self._set_request_json({"url": "http://a:3000", "occurrence": 1})
            payload = asyncio.run(plugin.api_remove_instance())["__json__"]
            self.assertTrue(payload["ok"])
            kept = [row["token"] for row in plugin.config["onebot_instances"]]
            self.assertEqual(kept, ["keep", "t3"], "应保留第一条")

    def test_remove_occurrence_out_of_range_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "http://a:3000", "token": "t", "enable": True,
                 "protocol_interval_hours": 12},
                {"url": "http://b:3000", "token": "t2", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            self._set_request_json({"url": "http://a:3000", "occurrence": 5})
            self.assertIn("__error__", asyncio.run(plugin.api_remove_instance()))
            self.assertEqual(len(plugin.config["onebot_instances"]), 2)

    def test_remove_rejects_negative_occurrence(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "http://a:3000", "token": "t", "enable": True,
                 "protocol_interval_hours": 12},
                {"url": "http://b:3000", "token": "t2", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            self._set_request_json({"url": "http://a:3000", "occurrence": -1})
            self.assertIn("__error__", asyncio.run(plugin.api_remove_instance()))
            self.assertEqual(len(plugin.config["onebot_instances"]), 2)

    def test_dead_api_reports_note_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            payload = asyncio.run(plugin.api_dead_instances())["__json__"]
            self.assertIn("rows", payload)
            self.assertIn("note", payload)
            self.assertEqual(payload["checked"], len(plugin.napcat.endpoints))


class TestDiscovery(OrbitTestBase):
    def test_scan_shape(self):
        out = self.disc.scan_sync([])
        for key in ("configs", "cache_dirs", "searched_roots", "truncated"):
            self.assertIn(key, out)
        for row in out["cache_dirs"]:
            self.assertEqual(
                set(row),
                {"path", "exists", "file_count", "size_bytes",
                 "skipped_symlinks", "truncated", "error"},
            )
            self.assertTrue(Path(row["path"]).is_absolute())

    def test_env_override_is_measured(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "napcat-cache"
            target.mkdir()
            (target / "a.bin").write_bytes(b"z" * 4096)
            old = os.environ.get("NAPCAT_TEMP_DIR")
            os.environ["NAPCAT_TEMP_DIR"] = str(target)
            try:
                rows = {r["path"]: r for r in self.disc.cache_dirs_sync([])}
            finally:
                if old is None:
                    os.environ.pop("NAPCAT_TEMP_DIR", None)
                else:
                    os.environ["NAPCAT_TEMP_DIR"] = old
            hit = rows.get(str(target.resolve()))
            self.assertIsNotNone(hit, "环境变量指定的目录应被探测")
            self.assertEqual(hit["size_bytes"], 4096)


# ---------- 5. 前后端字段对齐 ----------

class TestFrontendContract(OrbitTestBase):
    def test_every_dom_id_exists_in_html(self):
        # 字符集必须包含连字符：之前写成 [a-z0-9_] 时，btn-sweep、token-list
        # 这 33 个 id 一个都没被检查过，token-list 缺失就漏了过去。
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        html = (PLUGIN_DIR / "pages" / "orbit" / "index.html").read_text(encoding="utf-8")
        ids = set(re.findall(r'\$\("([a-z0-9_-]+)"\)', app))
        self.assertGreater(len(ids), 30, "id 提取正则又退化了？")
        missing = sorted(i for i in ids if f'id="{i}"' not in html)
        self.assertEqual(missing, [], f"app.js 引用了不存在的元素 id：{missing}")

    def test_backend_keys_used_by_frontend_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _Config(
                napcat_cache_dirs=[], astrbot_cache_threshold_mb=1,
                napcat_protocol_interval_hours=1, auto_enabled=True,
                auto_clean_plugin_modules=True, sweep_cron="*/15 * * * *",
            )
            engine = self.sched.SweepEngine(
                config=config,
                napcat_client=_FakeNapCat(),
                astrbot_cache=_FakeAstrBotCache(),
                module_cache=_FakeModuleCache(),
                data_dir=Path(tmp),
            )
            payload = {**asyncio.run(engine.collect()), "settings": engine.settings()}
            app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
            for field in ("schedule", "totals", "napcat", "layers", "module_rows", "settings"):
                self.assertIn(field, payload)
                self.assertIn(f".{field}", app)
            for layer in payload["layers"].values():
                for field in ("label", "amount", "ratio", "due", "enabled", "detail",
                              "config_error", "last"):
                    self.assertIn(field, layer)
            history = engine.history(5)
            if history:
                for field in ("layer", "label", "at", "acted", "freed_bytes", "error"):
                    self.assertIn(field, history[0])

    def test_sweep_records_render(self):
        records = [
            {"layer": "astrbot", "label": "AstrBot", "acted": True, "freed_bytes": 2048,
             "detail": "处理 2 个文件", "skipped": "", "error": ""},
            {"layer": "modules", "label": "模块", "acted": False, "freed_bytes": 0,
             "detail": "", "skipped": "没有停用插件仍占用内存", "error": ""},
            {"layer": "napcat_files", "label": "文件缓存", "acted": False, "freed_bytes": 0,
             "detail": "", "skipped": "", "error": "目录配置有误"},
        ]
        text = self.fmt.format_sweep_records(records, force=True)
        self.assertIn("全量强制清理完成", text)
        self.assertIn("已清理", text)
        self.assertIn("未处理", text)
        self.assertIn("失败", text)
        self.assertIn("2.00 KiB", text)


# ---------- 6. 插件装配：cron / 钩子 / Web API ----------

class TestPluginWiring(OrbitTestBase):
    def test_registers_all_web_apis(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, context = self._plugin(tmp)
            routes = {route for route, _, _, _ in context.web_apis}
            self.assertEqual(
                routes,
                {
                    f"/astrbot_plugin_orbit_command/overview",
                    f"/astrbot_plugin_orbit_command/plugins",
                    f"/astrbot_plugin_orbit_command/history",
                    f"/astrbot_plugin_orbit_command/discover",
                    f"/astrbot_plugin_orbit_command/sweep",
                    f"/astrbot_plugin_orbit_command/settings",
                    f"/astrbot_plugin_orbit_command/purge",
                    f"/astrbot_plugin_orbit_command/token",
                    f"/astrbot_plugin_orbit_command/dryrun",
                    f"/astrbot_plugin_orbit_command/dead-instances",
                    f"/astrbot_plugin_orbit_command/instance",
                    f"/astrbot_plugin_orbit_command/spaces",
                    f"/astrbot_plugin_orbit_command/junk",
                },
            )
            self.assertTrue(all(callable(h) for _, h, _, _ in context.web_apis))

    def test_cron_job_registered_non_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, context = self._plugin(tmp)
            asyncio.run(plugin.initialize())
            calls = context.cron_manager.add_calls
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["cron"], "*/15 * * * *")
            self.assertFalse(calls[0]["persistent"], "basic job 的 handler 只在内存里，不能落库")
            self.assertTrue(plugin.engine._schedule["next_run"])

    def test_hot_reload_restores_schedule(self):
        """热重载走 PluginManager.reload()，不重新触发 on_astrbot_loaded，
        所以定时任务必须由 initialize() 重建，否则重载后自动清理静默失效。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, context = self._plugin(tmp)
            asyncio.run(plugin.initialize())
            asyncio.run(plugin.terminate())
            self.assertEqual(context.cron_manager.jobs, {})

            # 重载 = 旧实例 terminate 掉，新实例重新 initialize
            reloaded, ctx2 = self._plugin(tmp)
            asyncio.run(reloaded.initialize())
            self.assertEqual(len(ctx2.cron_manager.jobs), 1)
            self.assertTrue(reloaded.engine._schedule["next_run"])

    def test_reload_does_not_duplicate_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, context = self._plugin(tmp)
            for _ in range(3):
                asyncio.run(plugin.initialize())
            self.assertEqual(len(context.cron_manager.jobs), 1, "热重载不应堆积孤儿任务")

    def test_auto_disabled_registers_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, context = self._plugin(tmp, auto_enabled=False)
            asyncio.run(plugin.initialize())
            self.assertEqual(context.cron_manager.add_calls, [])
            self.assertEqual(plugin.engine._schedule["cron"], "已暂停")

    def test_terminate_removes_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, context = self._plugin(tmp)
            asyncio.run(plugin.initialize())
            self.assertEqual(len(context.cron_manager.jobs), 1)
            asyncio.run(plugin.terminate())
            self.assertEqual(context.cron_manager.jobs, {})

    def test_bad_cron_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, context = self._plugin(tmp)
            context.cron_manager.fail_expression = "*/15 * * * *"
            asyncio.run(plugin.initialize())
            self.assertEqual(plugin.engine._schedule["next_run"], "注册失败")

    def test_unload_hook_purges_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            calls = []

            class SpyModuleCache(_FakeModuleCache):
                def purge_loaded(self, plugin_id):
                    calls.append(plugin_id)
                    return super().purge_loaded(plugin_id)

            plugin.module_cache = SpyModuleCache()
            plugin.engine.module_cache = plugin.module_cache
            asyncio.run(plugin.on_plugin_unloaded(_StarMeta("other", root_dir_name="other")))
            self.assertEqual(calls, ["other"])

    def test_unload_hook_respects_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, auto_clean_plugin_modules=False)
            calls = []
            spy = type(
                "M", (_FakeModuleCache,),
                {"purge_loaded": lambda self, pid: calls.append(pid) or []},
            )()
            plugin.module_cache = spy
            plugin.engine.module_cache = spy
            asyncio.run(plugin.on_plugin_unloaded(_StarMeta("other", root_dir_name="other")))
            self.assertEqual(calls, [])

    def test_overview_api_returns_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            response = asyncio.run(plugin.api_overview())
            self.assertIn("__json__", response)
            payload = response["__json__"]
            self.assertIn("settings", payload)
            self.assertIn("layers", payload)
            self.assertEqual(payload["settings"]["sweep_cron"], "*/15 * * * *")

    def test_sweep_api_rejects_unknown_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            self._set_request_json({"force": True, "layers": ["nope"]})
            response = asyncio.run(plugin.api_sweep())
            self.assertIn("__error__", response)
            self.assertIn("nope", response["__error__"])

    def test_sweep_api_single_layer_only_touches_that_layer(self):
        """面板每层卡片上的「强清这层」：force 全开但只跑一层。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            self._set_request_json({"force": True, "layers": ["astrbot"]})
            payload = asyncio.run(plugin.api_sweep())["__json__"]
            self.assertEqual([r["layer"] for r in payload["records"]], ["astrbot"])
            self.assertTrue(payload["forced"])

    def test_frontend_layer_button_exists(self):
        """每层卡片上的「强清这层」按钮由 renderLayers 生成，key 要透传下去。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        html = (PLUGIN_DIR / "pages" / "orbit" / "index.html").read_text(encoding="utf-8")
        self.assertIn("data-force", app)
        self.assertIn("forceLayer", app)
        self.assertIn("layers: layers || null", app, "必须把单层参数发回后端")
        self.assertIn('id="layers"', html)

    def test_no_native_dialogs(self):
        """面板跑在 iframe 里，sandbox 缺 allow-modals 时 confirm() 被静默拦掉
        并返回 false、alert() 返回 null。后果是「强清」等按钮点了毫无反应、
        也没有任何提示——而且不报错，很难定位。必须全部自绘。"""
        import re as _re

        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        # 先剥注释。本仓库的注释一律是「// + 空格」，而 URL 是「http://1」这种
        # 无空格的，用这个差别就能区分，不会误伤字符串里的 URL。
        code = _re.sub(r"// .*$", "", app, flags=_re.M)
        code = _re.sub(r"/\*.*?\*/", "", code, flags=_re.S)
        self.assertNotRegex(code, r"(?<![.\w])confirm\s*\(", "仍有原生 confirm()")
        self.assertNotRegex(code, r"(?<![.\w])alert\s*\(", "仍有原生 alert()")
        self.assertNotIn("window.confirm", code)
        self.assertIn("function askConfirm", code)
        self.assertIn("function toast", code)

    def test_confirm_gated_actions_use_the_page_dialog(self):
        """这几个按钮当初全部哑火，就是卡在原生 confirm 上。逐个钉住。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn('if (await askConfirm("强制全清', app)
        self.assertIn("await askConfirm(`强制清理「${label}」", app)
        self.assertIn("await askConfirm(`确定立即清理 ${pluginId}", app)
        self.assertIn("await askConfirm(`确定从配置里删掉 ${url}", app)

    def test_dialog_markup_exists(self):
        html = (PLUGIN_DIR / "pages" / "orbit" / "index.html").read_text(encoding="utf-8")
        for element_id in ("ui-layer", "ui-msg", "ui-cancel", "ui-ok", "ui-toast"):
            self.assertIn(f'id="{element_id}"', html, element_id)

    def test_every_html_class_has_a_css_rule(self):
        """index.html 里写了但 CSS 里没规则的 class 是悬空的——
        作者以为有样式，实际什么都没发生。`ghost` / `small` 就这样悬了很多年。"""
        import re as _re

        css = (PLUGIN_DIR / "pages" / "orbit" / "style.css").read_text(encoding="utf-8")
        html = (PLUGIN_DIR / "pages" / "orbit" / "index.html").read_text(encoding="utf-8")
        defined = set(_re.findall(r"\.([a-zA-Z][\w-]*)", css))
        used = set()
        for value in _re.findall(r'class="([^"]*)"', html):
            used |= {c for c in value.split() if c}
        missing = sorted(c for c in used if c not in defined)
        self.assertEqual(missing, [], f"index.html 用了没有样式规则的 class：{missing}")

    def test_every_css_var_is_defined_and_used(self):
        """var(--x) 引用了但没定义的变量，在浏览器里会静默失效。"""
        import re as _re

        css = (PLUGIN_DIR / "pages" / "orbit" / "style.css").read_text(encoding="utf-8")
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        defined = set(_re.findall(r"(--[a-z0-9-]+)\s*:", css))
        # 逐个元素设的变量（如星星的 --lo/--hi）只在行内样式里出现，不在主题块里
        defined |= set(_re.findall(r"(--[a-z0-9-]+)\s*:", app))
        used = set(_re.findall(r"var\((--[a-z0-9-]+)", css))
        self.assertEqual(
            sorted(used - defined), [], "有 var() 引用了未定义的变量"
        )

    def test_all_themes_define_the_same_variables(self):
        """四套主题必须变量齐全，否则换肤时会有属性突然变透明。"""
        import re as _re

        css = (PLUGIN_DIR / "pages" / "orbit" / "style.css").read_text(encoding="utf-8")
        blocks = dict(
            _re.findall(
                r'html\[data-theme="(\w+)"\]\s*\{(.*?)\n\}', css, _re.S
            )
        )
        self.assertEqual(
            set(blocks), {"deep", "cyber", "matrix", "light"}, "主题块数量不对"
        )
        base = set(
            _re.findall(r"(--[a-z0-9-]+)\s*:", ":root,\n" + blocks["deep"] + "\n}")
        )
        for name, body in blocks.items():
            got = set(_re.findall(r"(--[a-z0-9-]+)\s*:", body))
            self.assertEqual(
                sorted(base - got), [], f"主题 {name} 缺少变量"
            )

    def test_theme_picker_is_wired(self):
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        html = (PLUGIN_DIR / "pages" / "orbit" / "index.html").read_text(encoding="utf-8")
        for theme in ("deep", "cyber", "matrix", "light"):
            self.assertIn(f'data-theme-set="{theme}"', html, theme)
        self.assertIn('id="theme-accent"', html)
        self.assertIn('id="theme-reset"', html)
        self.assertIn("bindThemeBar()", app)
        self.assertIn("localStorage", app, "换肤选择应当记住")
        # 用户手动选过之后就不该再被宿主的深浅色覆盖
        self.assertIn("if (load(THEME_KEY)) return;", app)

    def test_intro_is_opt_in_from_js(self):
        """开场遮罩默认 display:none，只有 JS 加上 html.intro-on 才亮出来。

        反过来写（一上来就显示、等 JS 去藏）的话，脚本一旦没跑起来或中途报错，
        整块面板就会被一块黑幕永久挡死——一个纯装饰功能能砖掉整个插件。
        """
        css = (PLUGIN_DIR / "pages" / "orbit" / "style.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.intro\s*\{\s*display:\s*none")
        self.assertRegex(css, r"html\.intro-on \.intro\s*\{[^}]*display:\s*flex")
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn('documentElement.classList.add("intro-on")', app)

    def test_intro_timing_matches_the_spec(self):
        """节奏刻意慢：入场 1.4s，副标题 1.1s 且与标题重叠（1.6s 起），
        到位后停 1.2s 再退幕。缓冲给得足，退幕不会卡在副标题刚到位的那一瞬。"""
        import re as _re

        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        values = dict(
            (m.group(1), int(m.group(2)))
            for m in _re.finditer(r"const (INTRO_\w+) = (\d+);", app)
        )
        self.assertEqual(values["INTRO_STEP"], 300)
        self.assertEqual(values["INTRO_MAIN"], 1400)
        self.assertEqual(values["INTRO_SUB_DELAY"], 1600)
        self.assertEqual(values["INTRO_SUB"], 600)
        self.assertEqual(values["INTRO_HOLD"], 1200)
        # 副标题必须在标题落定前入场，否则就不是重叠而是接续
        self.assertLess(
            values["INTRO_SUB_DELAY"],
            values["INTRO_STEP"] + values["INTRO_MAIN"],
            "副标题应与标题重叠入场",
        )
        # 兜底必须覆盖 到位 + 停顿 + 退幕，否则动画会被半路砍掉
        self.assertGreater(
            values["INTRO_FALLBACK"],
            values["INTRO_SUB_DELAY"] + values["INTRO_SUB"]
            + values["INTRO_HOLD"] + values["INTRO_EXIT"],
            "兜底时间要盖过整条时间线",
        )
        self.assertIn("Promise.all([titleDone, subDone])", app,
                      "两个动画必须并行起跑；串行会让副标题的延迟变成负数")
        # 串行写法：先 await 标题、再算副标题延迟
        self.assertNotIn("INTRO_SUB_DELAY - INTRO_STEP - INTRO_MAIN", app,
                         "不要把副标题延迟写成相对差值，会算成负数")

    def test_intro_breathe_keeps_the_centering_translate(self):
        """呼吸关键帧里只写 scale 会把居中的 translate(-50%) 顶掉，
        元素会突然向右跳半个身位。"""
        css = (PLUGIN_DIR / "pages" / "orbit" / "style.css").read_text(encoding="utf-8")
        m = re.search(r"@keyframes breathe\s*\{(.*?)\n\}", css, re.S)
        self.assertIsNotNone(m, "缺少 breathe 关键帧")
        body = m.group(1)
        self.assertIn("translate(-50%", body)
        self.assertEqual(body.count("translate(-50%"), 2, "0%/100% 与 50% 都要带")

    def test_interpolate_param_name_matches_its_use(self):
        """fadeOnly 写成 fx 的话，动画每一帧都会抛 ReferenceError，
        整个开场直接不见。这个名字对不对，只能靠查。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        sig = re.search(r"function interpolate\(([^)]*)\)", app).group(1)
        self.assertIn("fadeOnly", sig, "形参名要和函数体里用的一致")
        self.assertNotIn(" fx", sig)

    def test_intro_has_a_failsafe(self):
        """无论 rAF 被挂起还是中途抛错，到点必须退幕，不能把面板永久挡住。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const INTRO_FALLBACK", app)
        self.assertIn("finishIntro(stage), INTRO_FALLBACK", app)
        # 点一下/敲一下就能跳过
        self.assertIn('stage.addEventListener("click", skip)', app)
        self.assertIn('document.addEventListener("keydown", skip)', app)
        # 结束时 opacity 写死为 1，不能清空回落到 CSS 里的 0
        self.assertIn('el.style.opacity = "1"', app)

    def test_intro_respects_reduced_motion_in_js(self):
        """CSS 里有媒体查询不够，JS 也得判：否则动画照样播。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", app)
        self.assertIn("if (prefersReducedMotion()) return;", app)

    def test_intro_title_hands_off_to_css(self):
        """行内 transform 会一直压着 CSS 动画，结束时必须清掉才能交棒。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn('el.style.transform = ""', app)
        self.assertIn('title.classList.add("breathe")', app)

    def test_reduced_motion_is_respected(self):
        css = (PLUGIN_DIR / "pages" / "orbit" / "style.css").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", css)

    def test_token_list_container_exists(self):
        """renderTokenConfigs() 往 $("token-list") 里写东西，而这个容器以前
        根本不存在——点「重新解析 Token」就会报错。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn('const host = $("token-list");', app)
        html = (PLUGIN_DIR / "pages" / "orbit" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="token-list"', html)

    def test_apply_found_is_defined(self):
        """「填入地址」按钮调用的 applyFound 以前根本没定义，点一下就抛错。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("applyFound(file, address);", app)
        self.assertIn("function applyFound(", app)

    def test_every_called_function_is_defined(self):
        """app.js 里裸调用的函数必须都有定义——全局作用域下未定义函数不会报错，
        只会等到用户点下去才炸（applyFound 就是这么漏出去的）。"""
        import re as _re

        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        defined = set(_re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", app))
        defined |= set(_re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", app))
        # 形参也算已定义：interpolate(..., ease, delay) 里的 ease 不是未定义函数
        for params in _re.findall(r"function\s+[A-Za-z_$][\w$]*\s*\(([^)]*)\)", app):
            defined |= set(_re.findall(r"[A-Za-z_$][\w$]*", params))
        for params in _re.findall(r"\(([^()]*)\)\s*=>", app):
            defined |= set(_re.findall(r"[A-Za-z_$][\w$]*", params))
        # 先把字符串/模板字面量抹掉：CSS 里的 blur( scale( translate( rgba(
        # 长得就像函数调用，会把这一整类误报成「未定义」。
        code = _re.sub(r"`(?:\\.|[^`\\])*`", "``", app)
        code = _re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
        code = _re.sub(r"'(?:\\.|[^'\\])*'", "''", code)
        called = set(_re.findall(r"(?<![\w.$])([a-z][\w$]*)\s*\(", code))
        skip = {
            # 控制流与内建
            "if", "for", "while", "switch", "catch", "return", "typeof",
            "function", "await", "async", "of", "in", "new", "isNaN",
            "setTimeout", "setInterval", "clearTimeout", "clearInterval",
            "requestAnimationFrame", "alert", "confirm", "prompt",
            "parseInt", "parseFloat", "String", "Number", "Boolean",
            "Array", "Object", "Promise", "require",
        }
        missing = sorted(called - defined - skip)
        self.assertEqual(missing, [], f"app.js 调用了未定义的函数：{missing}")

    def test_token_placeholder_is_never_sent_to_the_browser(self):
        """旧版后端把 *** 当占位符下发，前端会把它写回配置覆盖真 Token。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("token_set", app, "前端应改用 token_set 标记而不是假值")
        self.assertIn("留空保持不变", app)
        # 后端仍可以保留 *** 作为「当留空处理」的兼容判断，但不能出现在总览里
        main = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        overview = main.split("async def api_overview", 1)[1].split("async def ", 1)[0]
        self.assertNotIn('"***"', overview, "总览仍在把 *** 送到浏览器")

    def test_empty_token_keeps_the_stored_one(self):
        """面板永远拿不到真 token，所以「留空」必须由后端按 url 继承旧值。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "http://a:3000", "token": "REAL-SECRET", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            self._set_request_json({
                "action": "save_instances",
                "instances": [{"url": "http://a:3000", "token": "",
                                "enable": True, "protocol_interval_hours": 12}],
            })
            response = asyncio.run(plugin.api_token_apply())
            self.assertNotIn("__error__", response, response.get("__error__"))
            rows = plugin.config["onebot_instances"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["token"], "REAL-SECRET",
                             "Token 留空时必须保住原来的值")

    def test_overview_never_returns_a_token_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "http://a:3000", "token": "REAL-SECRET", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            payload = asyncio.run(plugin.api_overview())["__json__"]
            for row in payload["connection"]["instances"]:
                self.assertEqual(row["token"], "", "不能把 token 值送到浏览器")
                self.assertTrue(row["token_set"])

    def test_asterisk_sentinel_is_treated_as_empty(self):
        """老版本前端可能还在提交 ***，不能让它当成真 token 写进去。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "http://a:3000", "token": "REAL-SECRET", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            self._set_request_json({
                "action": "save_instances",
                "instances": [{"url": "http://a:3000", "token": "***",
                                "enable": True, "protocol_interval_hours": 12}],
            })
            asyncio.run(plugin.api_token_apply())
            self.assertEqual(plugin.config["onebot_instances"][0]["token"], "REAL-SECRET")

    def test_auto_adopt_switch_is_wired(self):
        """这个开关在 HTML 里有，但从来没被提交/回填过：面板上怎么点都不生效。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        html = (PLUGIN_DIR / "pages" / "orbit" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="auto_adopt_cache_dirs"', html)
        self.assertIn('auto_adopt_cache_dirs: $("auto_adopt_cache_dirs").checked', app)
        self.assertIn('$("auto_adopt_cache_dirs").checked = Boolean(s.auto_adopt_cache_dirs)', app)

    def test_transport_down_actually_disables_the_token_input(self):
        """TRANSPORT_DOWN 以前定义了却没用，README 说的「禁用输入框」是假的。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("state.transportDown = TRANSPORT_DOWN.has(auth.mode);", app)
        self.assertIn('host.querySelectorAll(\'[data-f="token"]\')', app)
        self.assertIn("el.disabled = true;", app)

    def test_destructive_button_is_honest_about_deleting(self):
        """「立即体检」会真的删文件。改名 + 先列出将动手的层再确认。"""
        html = (PLUGIN_DIR / "pages" / "orbit" / "index.html").read_text(encoding="utf-8")
        self.assertIn("按规则立即清理", html)
        self.assertNotIn(">立即体检<", html)
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("l.enabled && l.due", app, "确认框应列出超阈值的层")

    def test_poll_does_not_clobber_unsaved_input(self):
        """30 秒轮询会无条件回填表单，把用户改到一半的输入吹掉。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function formDirty()", app)
        self.assertIn("if (formDirty()) return;", app, "fillSettings 需跳过脏表单")
        self.assertIn("if (!formDirty()) {", app, "实例列表也需跳过脏表单")
        self.assertIn("markDirty();", app)
        self.assertIn("document.visibilityState === \"visible\"", app,
                      "页面在后台时不该轮询")

    def test_intro_subtitle_does_not_drift(self):
        """副标题之前从 +72px 升上来，漂移太大发飘；现在要「只渐变、位置完全不动」。

        不只是 y 固定：连 scale 和 blur 都不能留。interpolate 的 fadeOnly
        分支一进去，transform / filter 就完全不被写。
        """
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("y: 72", app, "副标题不该再带位移")
        self.assertRegex(
            app, r"INTRO_SUB,\s*easeOutExpo,\s*INTRO_SUB_DELAY,\s*true,",
            "副标题必须走 fadeOnly：不写 transform 也不写 filter",
        )
        self.assertIn("if (!fadeOnly) {", app)
        css = (PLUGIN_DIR / "pages" / "orbit" / "style.css").read_text(encoding="utf-8")
        # .intro-sub 有两处：共享的「.intro-title, .intro-sub {」定位块，
        # 加上后面单独的排版块。两条都要看。
        blocks = re.findall(r"(?m)^[.]intro-sub \{([^}]*)\}", css)
        self.assertTrue(blocks, "缺少 .intro-sub 规则")
        # 先剥注释：那段注释里正好写着「不用 translate(-50%)」，
        # 直接搜关键字会被自己的说明文字绊倒。
        sub = re.sub(r"/\*.*?\*/", "", "\n".join(blocks), flags=re.S)
        # 居中不能靠 translate(-50%)，否则永远移不掉位移
        self.assertIn("text-align: center", sub)
        self.assertNotIn("translate", sub)
        self.assertIn("const INTRO_HOLD", app, "到位后应停一拍再退幕")
        self.assertIn("await new Promise((r) => setTimeout(r, INTRO_HOLD));", app)

    def test_intro_has_a_starfield(self):
        """开场周围要有一圈小圆点。位置必须由固定种子生成，
        否则每次刷新星星都换位置，看着很跳。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        html = (PLUGIN_DIR / "pages" / "orbit" / "index.html").read_text(encoding="utf-8")
        css = (PLUGIN_DIR / "pages" / "orbit" / "style.css").read_text(encoding="utf-8")
        self.assertIn('id="intro-stars"', html)
        self.assertIn("function makeStars(", app)
        self.assertIn("makeStars(stars, INTRO_STARS);", app)
        self.assertIn("const INTRO_STARS =", app)
        # 固定种子
        self.assertRegex(app, r"let seed = \d+;")
        # 小圆点：极小的直径 + 圆形 + 呼吸
        self.assertRegex(css, r"\.intro-stars i\s*\{[^}]*border-radius:\s*50%")
        self.assertIn("@keyframes twinkle", css)
        self.assertIn("--lo", css)
        # 动画错开，避免整片同频闪
        self.assertIn("animation-delay:${delay}s", app)
        self.assertRegex(app, r"delay = \(-rand\(\) \* \d")

    def test_stars_avoid_the_text(self):
        """星星别压在文字正中间，否则两边都在闪、字反而看不清。"""
        css = (PLUGIN_DIR / "pages" / "orbit" / "style.css").read_text(encoding="utf-8")
        stars = css.split(".intro-stars {", 1)[1].split("}", 1)[0]
        self.assertIn("pointer-events: none", stars)
        # 最亮也别超过纯白，留点余量
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("hi = (0.55 + rand() * 0.45)", app)

    def test_invalid_media_dirs_are_not_persisted(self):
        """校验失败时必须**不写盘**。之前是「先报错、再把值存进去」，
        面板说没保存，配置其实已经变了。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, media_dirs=[])
            _, error = engine.update_settings({"media_dirs": ["相对路径/不行"]})
            self.assertIn("自选目录配置有误", error)
            self.assertEqual(
                engine.settings()["media_dirs"], [],
                "不合法的目录不该被写进配置",
            )

    def test_valid_media_dirs_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "pics"
            good.mkdir()
            engine, _, _ = self._engine(tmp, media_dirs=[])
            settings, error = engine.update_settings({"media_dirs": [str(good)]})
            self.assertEqual(error, "")
            self.assertEqual(settings["media_dirs"], [str(good)])

    def test_cron_is_really_validated(self):
        """之前只数是不是五段，`99 99 99 99 99` 也能存进去，
        然后定时任务注册静默失败。"""
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, sweep_cron="*/15 * * * *")
            for bad in ("99 99 99 99 99", "* * * *", "*/0 * * * *",
                        "0-99 * * * *", "1:2:3:4:5:6", "abc * * * *",
                        "5-1 * * * *", "0 0 32 1 0", "0 0 * 13 *", "0 0 * * 8"):
                _, error = engine.update_settings({"sweep_cron": bad})
                self.assertTrue(error, f"{bad!r} 应被判为非法")
                self.assertEqual(engine.settings()["sweep_cron"], "*/15 * * * *",
                                 f"{bad!r} 非法时不该覆盖原值")
            for good in ("*/15 * * * *", "30 3 * * *", "0 0 1 1 0", "0 0 * * 7",
                         "5-59/10 * * * *", "0 0 * jan-mar mon"):
                _, error = engine.update_settings({"sweep_cron": good})
                self.assertEqual(error, "", f"{good!r} 应被判为合法")
                self.assertEqual(engine.settings()["sweep_cron"], good)
            self.assertIsNotNone(P)

    def test_cron_error_names_the_offending_field(self):
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            _, error = engine.update_settings({"sweep_cron": "0 99 * * *"})
            self.assertIn("小时", error)
            self.assertTrue(P)

    def test_errors_are_human_readable(self):
        """面板上只甩一个 TimeoutError 等于什么都没说。"""
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            import astrbot_plugin_orbit_command.main as mod

            friendly = mod._friendly_error("统计空间", TimeoutError("slow"))
            self.assertIn("统计空间", friendly)
            self.assertIn("超时", friendly)
            self.assertNotIn("TimeoutError", friendly.split("详细信息")[0],
                             "技术名不该出现在主文案里")
            self.assertIn("TimeoutError", friendly.split("详细信息")[1])
            perm = mod._friendly_error("扫描插件目录", PermissionError("denied"))
            self.assertIn("权限", perm)
            self.assertIn("PermissionError", perm)
            self.assertIsNotNone(P)

    def test_friendly_error_reaches_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            import astrbot_plugin_orbit_command.spaces as spaces

            def boom(*_a, **_k):
                raise TimeoutError("slow")

            saved = spaces.usage_map_sync
            spaces.usage_map_sync = boom
            try:
                response = asyncio.run(plugin.api_spaces())
            finally:
                spaces.usage_map_sync = saved
            self.assertIn("__error__", response)
            self.assertIn("超时", response["__error__"])
            self.assertIn("TimeoutError", response["__error__"])

    def test_napcat_snippet_avoids_used_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.config["onebot_instances"] = [
                {"url": "http://127.0.0.1:3000", "token": "", "enable": True,
                 "protocol_interval_hours": 12},
            ]
            snip = plugin._napcat_snippet([])
            self.assertNotIn(":3000", snip["address"], "端口应避开已占用的")
            self.assertTrue(snip["address"].startswith("http://127.0.0.1:"))
            import json as _json
            doc = _json.loads(snip["json"])
            self.assertTrue(doc["network"]["httpServers"][0]["enable"])
            self.assertIn("网络配置", snip["where"])

    def test_token_api_ships_a_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            payload = asyncio.run(plugin.api_token_get())["__json__"]
            self.assertIn("snippet", payload)
            self.assertIn("json", payload["snippet"])
            self.assertIn("address", payload["snippet"])

    def test_setup_guide_is_wired(self):
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        html = (PLUGIN_DIR / "pages" / "orbit" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="setup-card"', html)
        self.assertIn('id="setup-body"', html)
        self.assertIn("function renderSetup(", app)
        self.assertIn("renderSetup(res);", app)
        # 连上之后就该藏起来，别一直挡着
        self.assertIn('const CONNECTED = new Set(["ok", "ok_no_token"]);', app)

    def test_appearance_is_read_back_from_the_backend(self):
        """**存进去还不够，必须读回来。**

        之前只改了「存」：写入走 API 配置，但 initTheme() 仍然只读 localStorage。
        iframe 里 storage 一空，刷新后读不到任何东西，看起来就等于完全没保存——
        这就是「这个版本一模一样」的真正原因。
        """
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function lookFrom(settings)", app)
        self.assertIn("s.ui_theme || load(THEME_KEY)", app,
                      "取值必须以服务端配置为先")
        self.assertIn("s.ui_accent || load(ACCENT_KEY)", app)
        # refresh() 里必须真的调它
        self.assertIn("const look = lookFrom(overview.settings);", app)
        self.assertIn("applyTheme(look.theme, look.accent);", app)
        # initTheme 只是“先猜一个”，不能是唯一取值来源
        self.assertRegex(
            app, r"function initTheme\(\) \{[^}]*load\(THEME_KEY\)",
        )

    def test_pending_local_edit_is_not_clobbered_by_polling(self):
        """saveLook 有 600ms debounce，这期间轮询不能把用户刚选的颜色弹回旧值。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("let lookOverride = null;", app)
        self.assertIn("if (lookOverride) return lookOverride;", app)
        self.assertIn("lookOverride = { theme, accent };", app)
        # 存成功后才交回服务端说了算
        self.assertIn("lookOverride = null;\n      }", app)

    def test_every_look_change_goes_through_setlook(self):
        """挑色、切配色、恢复默认，三条路都要走同一条：本地生效 + 落盘。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function setLook(theme, accent) {", app)
        self.assertIn("el.onclick = () => setLook(el.dataset.themeSet, load(ACCENT_KEY));", app)
        self.assertIn("const pick = () => setLook(load(THEME_KEY) || \"deep\", accent.value);", app)
        self.assertIn('setLook("deep", "");', app)
        # 不允许再有裸的 store + applyTheme 组合，容易漏掉落盘
        self.assertNotIn('store(THEME_KEY, el.dataset.themeSet);', app)

    def test_dirty_guard_survived_the_look_change(self):
        """改外观时别把实例列表的脏守卫弄丢——那会让轮询吹掉用户输入。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        block = app.split("const look = lookFrom(overview.settings);", 1)[1][:400]
        self.assertIn("if (!formDirty()) {", block)

    def test_appearance_is_saved_to_the_backend(self):
        """自定义主色要落到插件配置，不能只靠 localStorage。

        面板跑在 iframe 里，sandbox 缺 allow-same-origin 时 localStorage
        直接抛 SecurityError——而 store() 静默吞掉异常，表现为「设置过了又变回去」。
        """
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function saveLook(", app)
        self.assertIn('bridge.apiPost("settings", { ui_theme: theme, ui_accent: accent })', app)
        # localStorage 不可用时必须让用户知道没存上
        self.assertIn("外观没能保存到配置", app)

    def test_color_persists_on_both_events(self):
        """只绑 oninput 不够：有的浏览器或 WebView 只触发 change。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        self.assertIn("accent.oninput = pick;", app)
        self.assertRegex(app, r"accent\.onchange = \(\) => \{\s*pick\(\);")
        self.assertIn("const pick = () =>", app)

    def test_reset_actually_clears_the_accent(self):
        """行内样式优先级高于主题块，不 removeProperty 的话
        「恢复默认」点了等于没点——这是真发生过的。"""
        app = (PLUGIN_DIR / "pages" / "orbit" / "app.js").read_text(encoding="utf-8")
        for prop in ("--primary", "--glow-1", "--on-primary"):
            self.assertIn(f'removeProperty("{prop}")', app, prop)
        # 取色器也要复位，不能一直显示旧颜色
        self.assertIn('if (picker) picker.value = accent || "";', app)

    def test_appearance_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            self.assertEqual(engine.settings()["ui_theme"], "")
            self.assertEqual(engine.settings()["ui_accent"], "")
            settings, error = engine.update_settings(
                {"ui_theme": "cyber", "ui_accent": "#FF7A45"}
            )
            self.assertEqual(error, "")
            self.assertEqual(settings["ui_theme"], "cyber")
            self.assertEqual(settings["ui_accent"], "#ff7a45", "应归一化成小写")

    def test_appearance_settings_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, ui_accent="#ff7a45", ui_theme="cyber")
            for bad in ("red", "#12345", "#gggggg", "38bdf8"):
                _, error = engine.update_settings({"ui_accent": bad})
                self.assertTrue(error, f"{bad} 应被判为非法")
                self.assertEqual(engine.settings()["ui_accent"], "#ff7a45",
                                 "非法时不该覆盖已存的值")
            _, error = engine.update_settings({"ui_theme": "不存在的主题"})
            self.assertTrue(error)
            self.assertEqual(engine.settings()["ui_theme"], "cyber")
            settings, error = engine.update_settings({"ui_accent": "#abc"})
            self.assertEqual(error, "", "三位短写法应接受")
            self.assertEqual(settings["ui_accent"], "#abc")

    def test_appearance_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            engine.update_settings({"ui_theme": "matrix", "ui_accent": "#22d3ee"})
            reopened = self.sched.SweepEngine(
                config=engine._config,
                napcat_client=_FakeNapCat(),
                astrbot_cache=_FakeAstrBotCache(),
                module_cache=_FakeModuleCache(),
                data_dir=Path(tmp),
            )
            self.assertEqual(reopened.settings()["ui_theme"], "matrix")
            self.assertEqual(reopened.settings()["ui_accent"], "#22d3ee")

    def test_hidden_class_is_defined(self):
        """`class="... hidden"` 以前在 CSS 里根本没定义，token-banner 和
        死实例说明栏一直显示着（空白的框），只是没人注意。"""
        css = (PLUGIN_DIR / "pages" / "orbit" / "style.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.hidden\s*\{[^}]*display:\s*none")

    def test_sweep_api_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            self._set_request_json({"force": True})
            response = asyncio.run(plugin.api_sweep())
            self.assertIn("__json__", response)
            self.assertEqual(len(response["__json__"]["records"]), len(self.sched.LAYERS))

    def test_settings_api_rebuilds_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, context = self._plugin(tmp)
            self._set_request_json({"sweep_cron": "30 3 * * *", "auto_enabled": True})
            response = asyncio.run(plugin.api_settings())
            self.assertTrue(response["__json__"]["ok"], response["__json__"]["error"])
            self.assertEqual(context.cron_manager.add_calls[-1]["cron"], "30 3 * * *")

    def test_purge_api_requires_plugin_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            self._set_request_json({})
            self.assertIn("__error__", asyncio.run(plugin.api_purge()))
            self._set_request_json({"plugin_id": "other"})
            self.assertIn("__json__", asyncio.run(plugin.api_purge()))

    def test_purge_refuses_running_plugin(self):
        """运行中的插件不能被抽掉模块，否则它的惰性 import 会拿到重复的模块对象。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            plugin.engine.module_cache.active_ids.add("other")
            self._set_request_json({"plugin_id": "other"})
            response = asyncio.run(plugin.api_purge())
            self.assertIn("__error__", response)
            self.assertIn("仍在运行", response["__error__"])

    def test_discovery_api_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            payload = asyncio.run(plugin.api_discover())["__json__"]
            self.assertIn("candidates", payload)
            self.assertIn("configured", payload)

    def test_history_api_respects_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            asyncio.run(plugin.engine.sweep(force=True))
            self._set_request_query()
            payload = asyncio.run(plugin.api_history())["__json__"]
            self.assertEqual(len(payload["history"]), len(self.sched.LAYERS))


# ---------- 6.5 WebSocket 传输 ----------

class _Msg:
    def __init__(self, type_, data):
        self.type = type_
        self.data = data


class _FakeWS:
    def __init__(self, frames, log):
        self._frames = list(frames)
        self.log = log

    async def send_str(self, text):
        self.log["sent"].append(json.loads(text))

    async def receive(self):
        if not self._frames:
            raise AssertionError("收到了比预期更多的帧")
        return self._frames.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeWsSession:
    def __init__(self, frames, log, *, fail=None):
        self._frames = frames
        self.log = log
        self._fail = fail

    def ws_connect(self, url, **kw):
        self.log["url"] = url
        self.log["kw"] = kw
        if self._fail:
            return _FailingCtx(self._fail)
        return _FakeWS(self._frames, self.log)

    async def close(self):
        return None


class _FailingCtx:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


def _ok(data, echo):
    return json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": echo})


class TestWebsocketTransport(OrbitTestBase):
    def _transport(self, frames, log, **kw):
        import importlib

        transport_mod = importlib.import_module("astrbot_plugin_orbit_command.transport")
        session = _FakeWsSession(frames, log, fail=kw.pop("fail", None))
        return transport_mod.OneBotTransport(
            "ws://127.0.0.1:3001", kw.pop("token", ""),
            session=session, request_interval_ms=0, **kw
        )

    def test_accepts_ws_schemes(self):
        from astrbot_plugin_orbit_command.transport import validate_base_url

        for url in ("ws://h:1", "wss://h:1", "http://h:1", "https://h:1"):
            self.assertEqual(validate_base_url(url), url)
        for bad in ("ftp://h:1", "h:1", "ws://", "ws://u:p@h:1", "ws://h:1?x=1"):
            with self.assertRaises(ValueError, msg=bad):
                validate_base_url(bad)

    def test_is_websocket_flag(self):
        import importlib

        t = importlib.import_module("astrbot_plugin_orbit_command.transport")
        self.assertTrue(t.OneBotTransport("ws://h:1").is_websocket)
        self.assertTrue(t.OneBotTransport("wss://h:1").is_websocket)
        self.assertFalse(t.OneBotTransport("http://h:1").is_websocket)

    def _echo_after_probe(self, log, action="get_version_info"):
        """先跑一次把 frames 耗尽，借 transport 的计数拿到 echo。"""
        import importlib

        t = importlib.import_module("astrbot_plugin_orbit_command.transport")
        transport = self._transport([], log)
        with self.assertRaises(AssertionError):
            asyncio.run(transport._request(action))
        del t
        return transport

    def test_sends_action_in_body_and_returns_data(self):
        log = {"sent": []}
        probe = self._echo_after_probe(log)
        echo = f"orbit-{probe._echo}"
        transport = self._transport([_Msg("TEXT", _ok({"app": "NapCat"}, echo))], log)
        result = asyncio.run(transport._request("get_version_info"))
        self.assertEqual(result, {"app": "NapCat"})
        self.assertEqual(log["url"], "ws://127.0.0.1:3001")
        self.assertEqual(log["sent"][-1]["action"], "get_version_info")
        self.assertEqual(log["sent"][-1]["params"], {})
        self.assertEqual(log["sent"][-1]["echo"], f"orbit-{transport._echo}")
        self.assertNotIn("get_version_info", log["url"], "WS 地址里不该拼 action")

    def test_sends_bearer_header_when_token_set(self):
        log = {"sent": []}

        async def run():
            tr = self._transport([], log, token="sekret")
            try:
                await tr._request("get_status")
            except AssertionError:
                pass
            return tr

        tr = asyncio.run(run())
        self.assertEqual(log["kw"]["headers"].get("Authorization"), "Bearer sekret")

        log2 = {"sent": []}

        async def run2():
            tr = self._transport([], log2)
            try:
                await tr._request("get_status")
            except AssertionError:
                pass

        asyncio.run(run2())
        self.assertEqual(log2["kw"]["headers"], {})

    def test_skips_unrelated_frames(self):
        async def run():
            log = {"sent": []}
            tr = self._transport([], log)
            try:
                await tr._request("get_status")
            except AssertionError:
                pass
            echo = f"orbit-{tr._echo}"
            frames = [
                _Msg("TEXT", _ok({"heartbeat": 1}, "other-echo")),
                _Msg("BINARY", b"\x00\x01"),
                _Msg("TEXT", "not json at all"),
                _Msg("TEXT", _ok({"good": True}, echo)),
            ]
            tr2 = self._transport(frames, log)
            return await tr2._request("get_status")

        self.assertEqual(asyncio.run(run()), {"good": True})

    def test_closed_socket_raises_transport_error(self):
        import importlib

        tmod = importlib.import_module("astrbot_plugin_orbit_command.transport")

        async def run():
            log = {"sent": []}
            tr = self._transport([], log)
            try:
                await tr._request("get_status")
            except AssertionError:
                pass
            echo = f"orbit-{tr._echo}"
            tr2 = self._transport([_Msg("CLOSE", "")], log)
            return await tr2._request("get_status")

        with self.assertRaises(tmod.OneBotTransportError) as ctx:
            asyncio.run(run())
        self.assertIn("被服务端关闭", str(ctx.exception))

    def test_error_retcode_over_ws_becomes_action_error(self):
        import importlib

        t = importlib.import_module("astrbot_plugin_orbit_command.transport")

        async def run():
            log = {"sent": []}
            tr = self._transport([], log)
            try:
                await tr._request("clean_cache")
            except AssertionError:
                pass
            echo = f"orbit-{tr._echo}"
            frame = _Msg("TEXT", json.dumps(
                {"status": "failed", "retcode": 1403, "wording": "no such api", "echo": echo}))
            tr2 = self._transport([frame], log)
            return await tr2._request("clean_cache")

        with self.assertRaises(t.OneBotActionError) as ctx:
            asyncio.run(run())
        self.assertEqual(ctx.exception.retcode, 1403)

    def test_handshake_failure_becomes_transport_error(self):
        import importlib

        t = importlib.import_module("astrbot_plugin_orbit_command.transport")
        # 必须用 transport 自己绑定的那个 aiohttp：桩在每个测试类都会重建模块
        fail_exc = t.aiohttp.ClientError("Cannot connect to host 127.0.0.1:3001")

        async def run():
            log = {"sent": []}
            tr = self._transport([], log, fail=fail_exc)
            return await tr._request("get_status")

        with self.assertRaises(t.OneBotTransportError) as ctx:
            asyncio.run(run())
        self.assertIn("WebSocket", str(ctx.exception))

    def test_full_napcat_client_over_ws(self):
        """端到端：NapCatCacheClient 走 WS 完成 clean_protocol_cache。"""
        import importlib

        napcat_mod = importlib.import_module("astrbot_plugin_orbit_command.napcat")
        t = importlib.import_module("astrbot_plugin_orbit_command.transport")
        log = {"sent": []}

        async def run():
            probe = self._transport([], log)
            try:
                await probe._request("get_status")
            except AssertionError:
                pass
            echo = f"orbit-{probe._echo}"
            frame = _Msg("TEXT", _ok({"status": "ok"}, echo))
            transport = self._transport([frame], log)
            endpoint = _FakeEndpoint("ws://127.0.0.1:3001", "t")
            endpoint.transport = transport
            client = napcat_mod.NapCatCacheClient([endpoint])
            return await client.clean_protocol_cache()

        rows = asyncio.run(run())
        self.assertEqual(rows[0]["acted"], True)
        self.assertEqual(log["sent"][-1]["action"], "clean_cache")

    def test_rejects_bad_action_before_connecting(self):
        import importlib

        t = importlib.import_module("astrbot_plugin_orbit_command.transport")
        log = {"sent": []}
        transport = self._transport([], log)
        with self.assertRaises(ValueError):
            asyncio.run(transport._request("bad action; rm -rf /"))


# ---------- 6.8 缓存目录自动接管与误删防护 ----------


class _NapcatTree:
    """复刻用户的真实布局：4 个实例根 + config/ + cache/ + home/.config/QQ/NapCat/temp"""

    def __init__(self, base: Path, *, instances=2):
        self.root = base / "napcat_instances"
        self.instances = []
        for i in range(instances):
            tag = f"qq{i + 1}_17873903378147"
            inst = self.root / f"{tag}_napcat"
            (inst / "config").mkdir(parents=True)
            (inst / "config" / f"onebot11_363282349{i}.json").write_text(
                json.dumps({"network": {"httpServers": [
                    {"enable": True, "port": 3000, "token": "T"}]}}), encoding="utf-8")
            (inst / "napcat.mjs").write_text("// program", encoding="utf-8")
            (inst / "cache").mkdir()
            (inst / "cache" / "img.bin").write_bytes(b"x" * 18000)
            home = self.root / f"{tag}_home" / ".config" / "QQ" / "NapCat" / "temp"
            home.mkdir(parents=True)
            self.instances.append(inst)

    @property
    def cache_dirs(self):
        return [str(p / "cache") for p in self.instances]


class TestAutoAdopt(OrbitTestBase):
    def test_instance_root_is_never_adopted(self):
        """最关键的安全保证：含 config/ 与程序本体的实例根绝不能被自动接管。"""
        with tempfile.TemporaryDirectory() as tmp:
            tree = _NapcatTree(Path(tmp))
            rows = self.disc.cache_dirs_sync([str(tree.root)])
            adoptable = {r["path"] for r in rows if r.get("safe")}
            for inst in tree.instances:
                self.assertNotIn(
                    str(inst.resolve()), adoptable,
                    "实例根目录绝不能进入可自动接管集合",
                )
            # 顺带确认：即便它因为任何原因出现在结果里，也必须是不安全且带原因的
            for row in rows:
                if str(row["path"]) in {str(i.resolve()) for i in tree.instances}:
                    self.assertFalse(row["safe"])
                    self.assertTrue(row["reason"], "不安全的目录必须说明原因")

    def test_cache_subdirs_are_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _NapcatTree(Path(tmp))
            rows = {r["path"]: r for r in self.disc.cache_dirs_sync([str(tree.root)])}
            for expected in tree.cache_dirs:
                row = rows.get(str(Path(expected).resolve()))
                self.assertIsNotNone(row, f"{expected} 应被扫到")
                self.assertTrue(row["safe"], f"{expected} 应可自动接管")
                self.assertEqual(row["reason"], "")

    def test_dir_with_config_json_is_rejected(self):
        """名字像缓存、但里面躺着关键配置的目录，仍不得自动接管。"""
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "qq9_1_napcat"
            cache = inst / "cache"
            cache.mkdir(parents=True)
            (cache / "onebot11_1.json").write_text("{}", encoding="utf-8")
            # 从上一级扫：实例目录是作为扫描根的「子目录」被发现的
            row = next(
                r for r in self.disc.cache_dirs_sync([str(tmp)])
                if r["path"] == str(cache.resolve())
            )
            self.assertFalse(row["safe"], "内含 OneBot 配置的目录不能自动接管")
            self.assertIn("关键配置文件", row["reason"])

    def test_cleaner_never_deletes_protected_files(self):
        """即使目录被手动填成实例根，onebot11*.json 与 napcat.mjs 也不能被删。"""
        with tempfile.TemporaryDirectory() as tmp:
            tree = _NapcatTree(Path(tmp), instances=1)
            inst = tree.instances[0]
            # 阈值单位是 MiB 且最小 1，样本必须超过 1MiB 才会触发删除
            (inst / "cache" / "old.bin").write_bytes(b"x" * 900 * 1024)
            (inst / "cache" / "junk.bin").write_bytes(b"y" * 900 * 1024)
            import os, time

            old = time.time() - 86400
            for f in (inst / "cache" / "old.bin", inst / "cache" / "junk.bin"):
                os.utime(f, (old, old))
            os.utime(inst / "napcat.mjs", (old, old))
            os.utime(inst / "config" / "onebot11_3632823490.json", (old, old))

            cleaner = self.maint.NapCatCacheDirectoryCleaner(
                [str(inst)], threshold_mb=1, min_age_minutes=0
            )
            cleaner.clean_sync()
            self.assertTrue((inst / "napcat.mjs").exists(), "程序本体不能被删")
            self.assertTrue(
                (inst / "config" / "onebot11_3632823490.json").exists(),
                "OneBot 配置（也是本插件的 Token 来源）不能被删",
            )
            self.assertFalse((inst / "cache" / "old.bin").exists(), "普通缓存该被删")
            self.assertTrue((inst / "cache").exists(), "目录本身不能被删")

    def test_manual_config_wins_over_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _NapcatTree(Path(tmp))
            engine, _, _ = self._engine(
                tmp, napcat_cache_dirs=[str(Path(tree.cache_dirs[0]).resolve())]
            )
            engine._auto_dirs = [{"path": "/auto/one", "safe": True}]
            engine._auto_dirs_at = 1e18  # 假装缓存很新
            dirs, source = engine.active_dirs()
            self.assertEqual(dirs, [str(Path(tree.cache_dirs[0]).resolve())])
            self.assertEqual(source, "手动配置")
            self.assertEqual(asyncio.run(engine.auto_dirs()), [], "手填后不再自动接管")

    def test_unmeasured_dir_is_never_auto_adopted(self):
        """未被测量的目录 size_bytes=0，接管了会让层显示 0 字节——
        正是「看着只有几 KB」那个假象的来源。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            base = Path(tmp) / "opt"
            (base / "NapCat" / "temp").mkdir(parents=True)
            rows = [{"path": str(base / "NapCat" / "temp"), "safe": True,
                     "truncated": True, "size_bytes": 0, "reason": "预算耗尽"}]
            # auto_dirs() 是函数内 import 的，得打在 discovery 模块上
            original = self.disc.cache_dirs_sync

            def fake(_explicit):
                return rows

            self.disc.cache_dirs_sync = fake
            try:
                adopted = asyncio.run(engine.auto_dirs(force=True))
            finally:
                self.disc.cache_dirs_sync = original
            self.assertEqual(adopted, [], "未测量的目录不应被接管")
            self.assertEqual(engine.active_dirs(), ([], "未发现可自动接管的缓存目录"))

    def test_auto_adopt_can_be_switched_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp, auto_adopt_cache_dirs=False)
            self.assertEqual(asyncio.run(engine.auto_dirs()), [])
            dirs, source = engine.active_dirs()
            self.assertEqual(dirs, [])
            self.assertIn("已关闭", source)

    def test_auto_adopt_picks_up_all_instance_cache_dirs(self):
        """用户的 4 个实例目录应当被一次性全部接管，无需逐个点。"""
        with tempfile.TemporaryDirectory() as tmp:
            tree = _NapcatTree(Path(tmp), instances=4)
            engine, _, _ = self._engine(tmp, napcat_config_dirs=[str(tree.root)])
            adopted = asyncio.run(engine.auto_dirs())
            paths = {row["path"] for row in adopted}
            for expected in tree.cache_dirs:
                self.assertIn(str(Path(expected).resolve()), paths)
            dirs, source = engine.active_dirs()
            self.assertEqual(source, "自动接管")
            # 每个实例有 cache/ 与 home 下的 temp 两个合法目录，实例越多目录越多
            self.assertGreaterEqual(len(dirs), 4)
            for expected in tree.cache_dirs:
                self.assertIn(str(Path(expected).resolve()), dirs)

    def test_sweep_uses_auto_adopted_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _NapcatTree(Path(tmp), instances=1)
            engine, _, _ = self._engine(
                tmp, napcat_config_dirs=[str(tree.root)],
                napcat_cache_threshold_mb=1, napcat_cache_min_age_minutes=0,
            )
            import os, time

            junk = Path(tree.cache_dirs[0]) / "big.bin"
            junk.write_bytes(b"z" * 1600 * 1024)
            old = time.time() - 86400
            os.utime(junk, (old, old))
            result = asyncio.run(engine.sweep(layers=["napcat_files"]))
            record = result["records"][0]
            self.assertTrue(record["acted"], record["skipped"])
            self.assertFalse(junk.exists(), "自动接管的目录应真的被清理")

    def test_settings_expose_auto_adopt_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            self.assertIn("auto_adopt_cache_dirs", engine.settings())
            settings, _ = engine.update_settings({"auto_adopt_cache_dirs": False})
            self.assertFalse(settings["auto_adopt_cache_dirs"])
            self.assertFalse(engine.auto_adopt_enabled)


# ---------- 7. NapCat 配置识别与鉴权探测 ----------


_ONEBOT_SAMPLE = {
    "network": {
        "httpServers": [
            {"name": "HTTP", "enable": True, "host": "0.0.0.0", "port": 3000,
             "token": "s3cret-token"},
        ],
        "websocketServers": [],
    }
}


def _patch_transport(good_tokens, *, no_token_ok=False):
    """把 auth 模块里的 OneBotTransport 换成可控假件。

    good_tokens: 能通过鉴权的 token 集合；不在其中的 token 一律 401。
    注意要打在 **auth 模块** 上——auth._try 是按模块全局名去拿的。
    """
    import importlib

    auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
    transport_mod = importlib.import_module("astrbot_plugin_orbit_command.transport")
    calls = []

    class FakeTransport:
        def __init__(self, base_url, token="", **kw):
            self.base_url = base_url
            self.access_token = token
            self.closed = False

        async def start(self):
            return None

        async def close(self):
            self.closed = True

        async def _request(self, action, params=None):
            calls.append((self.base_url, self.access_token, action))
            if self.access_token in good_tokens or (not self.access_token and no_token_ok):
                return {"app": "x"}
            raise transport_mod.OneBotActionError(action, 401, "unauthorized")

    auth.OneBotTransport = FakeTransport
    return calls, FakeTransport


class TestDiscoverySearch(OrbitTestBase):
    def _tree(self, tmp, *, token="s3cret-token", port=3000, with_cache=True):
        base = Path(tmp)
        napcat = base / "opt" / "NapCat" / "config"
        napcat.mkdir(parents=True)
        (napcat / "onebot11_123456.json").write_text(
            json.dumps({"network": {"httpServers": [
                {"name": "HTTP", "enable": True, "host": "0.0.0.0",
                 "port": port, "token": token},
            ]}}), encoding="utf-8")
        if with_cache:
            (napcat.parent / "temp").mkdir()
            (napcat.parent / "temp" / "a.bin").write_bytes(b"z" * 2048)
        return base

    def test_search_finds_config_in_unknown_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._tree(tmp)
            # 只给一个毫无特征的根，模拟「路径完全猜不到」
            found = self.disc.onebot_configs_sync([str(base / "opt")])
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["account"], "123456")
            self.assertEqual(found[0]["servers"][0]["token"], "s3cret-token")
            self.assertEqual(found[0]["servers"][0]["address"], "http://127.0.0.1:3000")

    def test_search_finds_cache_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._tree(tmp)
            rows = self.disc.cache_dirs_sync([str(base / "opt")])
            paths = [r["path"] for r in rows]
            self.assertTrue(any(p.endswith("NapCat/temp") for p in paths), paths)
            row = next(r for r in rows if r["path"].endswith("NapCat/temp"))
            self.assertEqual(row["size_bytes"], 2048)
            self.assertEqual(row["file_count"], 1)

    def test_scan_reports_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._tree(tmp)
            out = self.disc.scan_sync([str(base / "opt")])
            self.assertIn("configs", out)
            self.assertIn("cache_dirs", out)
            self.assertTrue(out["searched_roots"])
            self.assertIn("truncated", out)

    def test_ignores_webui_and_other_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._tree(tmp)
            (d / "opt" / "NapCat" / "config" / "webui.json").write_text(
                json.dumps({"token": "webui-token"}), encoding="utf-8")
            (d / "opt" / "NapCat" / "config" / "napcat.json").write_text(
                json.dumps({"fileLog": True}), encoding="utf-8")
            (d / "opt" / "NapCat" / "config" / "readme.txt").write_text("x", encoding="utf-8")
            found = self.disc.onebot_configs_sync([str(d / "opt")])
            tokens = [s["token"] for e in found for s in e["servers"]]
            self.assertNotIn("webui-token", tokens)
            self.assertEqual(tokens, ["s3cret-token"])

    def test_respects_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "deep"
            current = d
            for i in range(8):
                current = current / f"lvl{i}"
            current.mkdir(parents=True)
            (current / "onebot11_9.json").write_text(
                json.dumps(_ONEBOT_SAMPLE), encoding="utf-8")
            found = self.disc.onebot_configs_sync([str(d)])
            self.assertEqual(found, [], "超出深度上限的目录不应被找到")

    def test_exhausted_budget_does_not_swallow_candidates(self):
        """预算耗尽时静默丢目录 = 「一个都没找到」，而面板上只剩几 KB，
        看着就像真的就这么小。必须标成未测量而不是消失。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._tree(tmp)
            saved = self.disc._MEASURE_BUDGET_ENTRIES
            self.disc._MEASURE_BUDGET_ENTRIES = 0
            try:
                rows = self.disc.cache_dirs_sync([str(base / "opt")])
                out = self.disc.scan_sync([str(base / "opt")])
            finally:
                self.disc._MEASURE_BUDGET_ENTRIES = saved
            self.assertTrue(rows, "预算耗尽也不能把候选目录从结果里抹掉")
            self.assertTrue(all(r["truncated"] for r in rows), rows)
            self.assertTrue(all("预算" in r["reason"] for r in rows), rows)
            self.assertEqual(rows[0]["size_bytes"], 0)
            self.assertTrue(out["truncated"], "顶层 truncated 也要反映测量预算耗尽")

    def test_measure_budget_is_separate_from_search_budget(self):
        """测量遍历整个文件树，四个 QQ 实例轻松吃掉四万条；与搜索共用一份
        预算的话，后面的候选目录会直接变成「未测量」。"""
        self.assertGreater(self.disc._MEASURE_BUDGET_ENTRIES, 0)
        with tempfile.TemporaryDirectory() as tmp:
            base = self._tree(tmp)
            saved = self.disc._MAX_SCAN_ENTRIES
            self.disc._MAX_SCAN_ENTRIES = 1
            try:
                out = self.disc.scan_sync([str(base / "opt")])
            finally:
                self.disc._MAX_SCAN_ENTRIES = saved
            measured = [r for r in out["cache_dirs"] if not r["truncated"]]
            self.assertTrue(
                measured, "搜索预算耗尽不应连带把测量预算也吃掉"
            )

    def test_redact_strips_token(self):
        entries = [{"file": "/x/onebot11_1.json", "account": "1", "error": "",
                    "servers": [{"name": "H", "enable": True, "host": "", "port": 3000,
                                 "address": "http://127.0.0.1:3000",
                                 "has_token": True, "token": "s3cret-token"}]}]
        safe = self.disc.redact_configs(entries)
        self.assertEqual(safe[0]["servers"][0]["token"], "")
        self.assertTrue(safe[0]["servers"][0]["has_token"])
        self.assertNotIn("s3cret-token", json.dumps(safe, ensure_ascii=False))


class TestTokenResolver(OrbitTestBase):
    def _resolver(self, tmp, **cfg):
        import importlib

        auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
        defaults = dict(onebot_base_url="http://127.0.0.1:3000", onebot_access_token="")
        defaults.update(cfg)
        return auth.TokenResolver(
            config=_Config(**defaults),
            data_path=Path(tmp),
            explicit_dirs=lambda: [],
            timeout=2,
        )

    def test_no_token_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = self._resolver(tmp)
            calls, _ = _patch_transport(set(), no_token_ok=True)
            result = asyncio.run(resolver.resolve("http://127.0.0.1:3000"))
            self.assertTrue(result["reachable"])
            self.assertEqual(result["token"], "")
            self.assertFalse(result["needs_token"])
            self.assertEqual(result["source"], "无需 Token")
            self.assertEqual(len(calls), 1, "不该再去找 token")

    def test_picks_working_token_from_napcat_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            cfg_dir.mkdir()
            (cfg_dir / "onebot11_1.json").write_text(
                json.dumps(_ONEBOT_SAMPLE), encoding="utf-8")
            import importlib

            auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
            resolver = auth.TokenResolver(
                config=_Config(onebot_access_token=""),
                data_path=Path(tmp),
                explicit_dirs=lambda: [str(cfg_dir)],
                timeout=2,
            )
            _patch_transport({"s3cret-token"})
            result = asyncio.run(resolver.resolve("http://127.0.0.1:3000"))
            self.assertEqual(result["token"], "s3cret-token")
            self.assertIn("onebot11_1.json", result["source"])
            self.assertTrue(result["needs_token"])

    def test_manual_config_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = self._resolver(tmp, onebot_access_token="manual")
            _patch_transport({"manual"})
            result = asyncio.run(resolver.resolve("http://127.0.0.1:3000"))
            self.assertEqual(result["token"], "manual")
            self.assertEqual(result["source"], "手动配置")

    def test_reads_token_from_astrbot_platform_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            (data / "config").mkdir(parents=True)
            (data / "cmd_config.json").write_text(
                json.dumps({"platform": [
                    {"type": "aiocqhttp", "token": "from-astrbot"},
                ]}), encoding="utf-8")
            import importlib

            auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
            resolver = auth.TokenResolver(
                config=_Config(onebot_access_token=""),
                data_path=data,
                explicit_dirs=lambda: [],
                timeout=2,
            )
            _patch_transport({"from-astrbot"})
            result = asyncio.run(resolver.resolve("http://127.0.0.1:3000"))
            self.assertEqual(result["token"], "from-astrbot")
            self.assertEqual(result["source"], "AstrBot 平台配置")

    def test_skips_wrong_token_and_keeps_trying(self):
        with tempfile.TemporaryDirectory() as tmp:
            import os

            os.environ["NAPCAT_TOKEN"] = "good-env"
            try:
                resolver = self._resolver(tmp, onebot_access_token="wrong")
                calls, _ = _patch_transport({"good-env"})
                result = asyncio.run(resolver.resolve("http://127.0.0.1:3000"))
                self.assertEqual(result["token"], "good-env")
                self.assertTrue(len(calls) >= 3, "应先试空、再试错的手动、最后试对的")
            finally:
                os.environ.pop("NAPCAT_TOKEN", None)

    def test_gives_up_cleanly_when_all_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = self._resolver(tmp, onebot_access_token="nope")
            _patch_transport(set())
            result = asyncio.run(resolver.resolve("http://127.0.0.1:3000"))
            self.assertTrue(result["needs_token"])
            self.assertEqual(result["token"], "")
            self.assertIn("手动填写", result["note"])

    def test_cache_and_invalidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = self._resolver(tmp, onebot_access_token="t1")
            calls, _ = _patch_transport({"t1"})
            asyncio.run(resolver.resolve("http://127.0.0.1:3000"))
            n1 = len(calls)
            asyncio.run(resolver.resolve("http://127.0.0.1:3000"))
            self.assertEqual(len(calls), n1, "TTL 内应命中缓存")
            resolver.invalidate("http://127.0.0.1:3000")
            asyncio.run(resolver.resolve("http://127.0.0.1:3000"))
            self.assertGreater(len(calls), n1, "invalidate 后必须重找")

    def test_token_rotation_is_picked_up(self):
        """这是「每次重启 token 都不一样」的核心保障。"""
        with tempfile.TemporaryDirectory() as tmp:
            resolver = self._resolver(tmp, onebot_access_token="old")
            _patch_transport({"old"})
            asyncio.run(resolver.resolve("http://127.0.0.1:3000"))
            resolver._cache.clear()
            # NapCat 重启，token 变了
            _patch_transport({"new"})
            resolver._config["onebot_access_token"] = "new"
            result = asyncio.run(resolver.resolve("http://127.0.0.1:3000", force=True))
            self.assertEqual(result["token"], "new")

    def test_probe_state_modes(self):
        import importlib

        auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
        _patch_transport(set(), no_token_ok=True)
        state = asyncio.run(auth.probe_state("http://127.0.0.1:3000", ""))
        self.assertEqual(state["mode"], "ok_no_token")
        _patch_transport(set())
        state = asyncio.run(auth.probe_state("http://127.0.0.1:3000", ""))
        self.assertEqual(state["mode"], "auth_required")
        _patch_transport(set())
        state = asyncio.run(auth.probe_state("http://127.0.0.1:3000", "wrong"))
        self.assertEqual(state["mode"], "auth_failed")
        state = asyncio.run(auth.probe_state("", ""))
        self.assertEqual(state["mode"], "no_url")


class TestAuthRecovery(OrbitTestBase):
    def test_401_triggers_reauth_then_retry(self):
        """收到 401 必须重新解析 token 并重试一次，而不是直接判失败。"""
        import importlib

        napcat = importlib.import_module("astrbot_plugin_orbit_command.napcat")
        transport_mod = importlib.import_module("astrbot_plugin_orbit_command.transport")
        state = {"reauth": 0}

        class FakeTransport:
            def __init__(self, url, token="", **kw):
                self.base_url, self.access_token = url, token

            async def _request(self, action, params=None):
                if self.access_token != "fresh":
                    raise transport_mod.OneBotActionError(action, 401, "no")
                return {"status": "ok"}

        endpoint = _FakeEndpoint("ws://h:1", "stale")
        endpoint.transport = FakeTransport(endpoint.url, "stale")

        def reauth(url):
            state["reauth"] += 1
            endpoint.transport.access_token = "fresh"   # 重新解析拿到了有效 token

        client = napcat.NapCatCacheClient([endpoint], reauth=reauth)
        rows = asyncio.run(client.clean_protocol_cache())
        self.assertEqual(rows[0]["acted"], True)
        self.assertEqual(rows[0]["error"], "")
        self.assertEqual(state["reauth"], 1)

    def test_non_auth_error_is_not_retried(self):
        import importlib

        napcat = importlib.import_module("astrbot_plugin_orbit_command.napcat")
        transport_mod = importlib.import_module("astrbot_plugin_orbit_command.transport")
        state = {"reauth": 0}

        class FakeTransport:
            def __init__(self, url, token="", **kw):
                self.access_token, self.base_url = token, url

            async def _request(self, action, params=None):
                raise transport_mod.OneBotActionError(action, 1403, "bad action")

        endpoint = _FakeEndpoint("http://h:1", "t")
        endpoint.transport = FakeTransport(endpoint.url, "t")
        client = napcat.NapCatCacheClient([endpoint], reauth=lambda url: state.update(reauth=1))
        rows = asyncio.run(client.clean_protocol_cache())
        self.assertTrue(rows[0]["error"])
        self.assertEqual(state["reauth"], 0, "非鉴权错误不该触发重解析")

    def test_no_reauth_hook_means_single_attempt(self):
        import importlib

        napcat = importlib.import_module("astrbot_plugin_orbit_command.napcat")
        transport_mod = importlib.import_module("astrbot_plugin_orbit_command.transport")
        calls = []

        class FakeTransport:
            def __init__(self, url, token="", **kw):
                self.access_token, self.base_url = token, url

            async def _request(self, action, params=None):
                calls.append(action)
                raise transport_mod.OneBotActionError(action, 401, "no")

        endpoint = _FakeEndpoint("http://h:1", "t")
        endpoint.transport = FakeTransport(endpoint.url, "t")
        client = napcat.NapCatCacheClient([endpoint])
        rows = asyncio.run(client.clean_protocol_cache())
        self.assertTrue(rows[0]["error"])
        self.assertEqual(len(calls), 1)


class TestTokenApi(OrbitTestBase):
    def test_refresh_auth_writes_resolved_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, onebot_base_url="http://127.0.0.1:3000")
            import importlib

            auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
            _patch_transport({"from-file"})
            (Path(tmp) / "onebot11_7.json").write_text(
                json.dumps({"network": {"httpServers": [
                    {"enable": True, "port": 3000, "token": "from-file"},
                ]}}), encoding="utf-8")
            plugin.resolver._explicit_dirs = lambda: [str(Path(tmp))]
            result = asyncio.run(plugin.refresh_auth(force=True))
            self.assertEqual(result["token"], "from-file")
            self.assertEqual(plugin._instance_rows()[0]["token"], "from-file")
            self.assertEqual(plugin.napcat.endpoints[0].token, "from-file")
            self.assertEqual(plugin.auth_state["mode"], "ok")

    def test_save_instances_replaces_legacy_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, onebot_base_url="http://old:3000",
                                     onebot_access_token="old")
            self._set_request_json({"action": "save_instances", "instances": [
                {"url": "http://a:3000", "token": "t1", "enable": True,
                 "protocol_interval_hours": 6},
                {"url": "ws://b:3001", "token": "", "enable": False,
                 "protocol_interval_hours": 3},
            ]})
            payload = asyncio.run(plugin.api_token_apply())["__json__"]
            self.assertTrue(payload["ok"], payload.get("note"))
            self.assertEqual(payload["count"], 2)
            self.assertEqual(plugin.config["onebot_base_url"], "", "旧字段要清空否则会串味")
            self.assertEqual(len(plugin.napcat.endpoints), 2)
            self.assertEqual(len(plugin.napcat.enabled_endpoints), 1)

    def test_save_instances_rejects_bad_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            self._set_request_json({"action": "save_instances", "instances": [
                {"url": "ftp://x", "token": ""},
            ]})
            self.assertIn("__error__", asyncio.run(plugin.api_token_apply()))

    def test_resolve_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            import importlib

            auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
            _patch_transport(set(), no_token_ok=True)
            self._set_request_json({"action": "resolve"})
            payload = asyncio.run(plugin.api_token_apply())["__json__"]
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["auth"]["mode"], "ok_no_token")

    def test_rotated_token_is_persisted_to_config(self):
        """NapCat 换了 token 后，解析结果要落盘，重启后不用重新找。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            cfg_dir.mkdir()
            (cfg_dir / "onebot11_9.json").write_text(
                json.dumps({"network": {"httpServers": [
                    {"enable": True, "port": 3000, "token": "rotated"},
                ]}}), encoding="utf-8")
            plugin, _ = self._plugin(tmp, onebot_base_url="http://127.0.0.1:3000",
                                     onebot_access_token="stale",
                                     napcat_config_dirs=[str(cfg_dir)])
            import importlib

            auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
            _patch_transport({"rotated"})
            plugin.resolver._explicit_dirs = lambda: [str(cfg_dir)]
            asyncio.run(plugin.refresh_auth(force=True))
            self.assertEqual(plugin.config["onebot_access_token"], "rotated")
            self.assertEqual(plugin.napcat.endpoints[0].token, "rotated")
            self.assertEqual(plugin.auth_state["mode"], "ok")

    def test_missing_token_is_not_reported_as_ok(self):
        """「需要 Token 但没解析出来」绝不能报成「无需 Token」，否则会误导用户。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, onebot_base_url="http://127.0.0.1:3000")
            import importlib

            auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
            _patch_transport(set())          # 全部候选都 401
            asyncio.run(plugin.refresh_auth(force=True))
            self.assertEqual(plugin.auth_state["mode"], "auth_required")
            self.assertFalse(plugin.auth_state["resolved"])

    def test_confirmed_no_token_reports_ok_no_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, onebot_base_url="http://127.0.0.1:3000")
            import importlib

            auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
            _patch_transport(set(), no_token_ok=True)
            asyncio.run(plugin.refresh_auth(force=True))
            self.assertEqual(plugin.auth_state["mode"], "ok_no_token")
            self.assertTrue(plugin.auth_state["resolved"])

    def test_overview_exposes_connection_and_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, onebot_access_token="zzz")
            payload = asyncio.run(plugin.api_overview())["__json__"]
            self.assertIn("connection", payload)
            self.assertIn("auth", payload["connection"])
            self.assertEqual(len(payload["connection"]["instances"]), 1)
            row = payload["connection"]["instances"][0]
            self.assertEqual(row["url"], "http://127.0.0.1:3000")
            self.assertTrue(row["token_set"], "token 只回传是否存在，不回传明文")
            self.assertNotIn("zzz", json.dumps(payload, ensure_ascii=False))

    def test_websocket_client_token_is_discovered(self):
        """只配了 websocketClients（反向连接）时，token 也必须能被扫到。

        这正是用户容器里的真实情况：httpServers 为空，只有反向 WS。
        """
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "onebot11_7.json").write_text(json.dumps({
                "network": {
                    "httpServers": [], "httpClients": [], "websocketServers": [],
                    "websocketClients": [
                        {"name": "astrbot", "enable": True,
                         "url": "ws://localhost:6199/ws", "token": "reverse-token"},
                    ],
                }
            }), encoding="utf-8")
            entry = self.disc.onebot_configs_sync([str(d)])[0]
            self.assertEqual(entry["error"], "", "不该因为没有 httpServers 就报错")
            server = entry["servers"][0]
            self.assertEqual(server["kind"], "ws_client")
            self.assertEqual(server["token"], "reverse-token")
            self.assertTrue(server["has_token"])
            self.assertFalse(server["can_connect"], "反向连接不是可连端点")
            self.assertEqual(server["address"], "")

    def test_all_four_sections_are_parsed(self):
        doc = {"network": {
            "httpServers": [{"enable": True, "host": "0.0.0.0", "port": 3000, "token": "h"}],
            "websocketServers": [{"enable": True, "host": "0.0.0.0", "port": 3001, "token": "w"}],
            "httpClients": [{"enable": True, "url": "http://x/y", "token": "hc"}],
            "websocketClients": [{"enable": True, "url": "ws://x/y", "token": "wc"}],
        }}
        from astrbot_plugin_orbit_command.discovery import _servers_of

        servers = _servers_of(doc)
        kinds = [s["kind"] for s in servers]
        self.assertEqual(kinds, ["http_server", "ws_server", "http_client", "ws_client"])
        self.assertEqual([s["can_connect"] for s in servers], [True, True, False, False])
        self.assertEqual([s["token"] for s in servers], ["h", "w", "hc", "wc"])
        self.assertEqual(servers[1]["address"], "ws://127.0.0.1:3001")

    def test_protocol_hint_for_reverse_only_deployment(self):
        """用户容器：四个 section 只有 websocketClients 有配置。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            configs = [{"file": "/x/onebot11_1.json", "account": "1", "error": "", "servers": [
                {"name": "astrbot", "kind": "ws_client", "can_connect": False,
                 "enable": True, "host": "", "port": 0, "address": "",
                 "url": "ws://localhost:6199/ws", "has_token": True, "token": "t"},
            ]}]
            hint = plugin._protocol_hint(configs)
            self.assertIn("反向连接", hint)
            self.assertIn("HTTP 服务端", hint)
            self.assertIn("连不上", hint)

    def test_protocol_hint_lists_connectable_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            configs = [{"file": "/x/onebot11_1.json", "account": "1", "error": "", "servers": [
                {"name": "HTTP", "kind": "http_server", "can_connect": True,
                 "enable": True, "host": "127.0.0.1", "port": 3000,
                 "address": "http://127.0.0.1:3000", "url": "",
                 "has_token": False, "token": ""},
            ]}]
            hint = plugin._protocol_hint(configs)
            self.assertIn("http://127.0.0.1:3000", hint)

    def test_protocol_hint_when_no_config_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            hint = plugin._protocol_hint([])
            self.assertIn("没读到", hint)
            self.assertIn("手动填写", hint)

    def test_unreachable_is_never_blamed_on_token(self):
        """地址连不上时，提示绝不能说「Token 无效/请手填」。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, onebot_access_token="whatever")
            import importlib

            auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
            transport_mod = importlib.import_module("astrbot_plugin_orbit_command.transport")

            class Down:
                def __init__(self, *a, **k):
                    self.access_token = a[1] if len(a) > 1 else ""

                async def start(self): return None

                async def close(self): return None

                async def _request(self, action, params=None):
                    # 真实场景：连接被拒，底层是 ConnectionRefusedError
                    try:
                        raise ConnectionRefusedError(111, "Connection refused")
                    except ConnectionRefusedError as cause:
                        raise transport_mod.OneBotTransportError(
                            "get_version_info HTTP 请求失败: "
                            "Cannot connect to host 127.0.0.1:3000"
                        ) from cause

            auth.OneBotTransport = Down
            res = asyncio.run(plugin.refresh_auth(force=True))
            self.assertEqual(plugin.auth_state["mode"], "unreachable")
            self.assertIn("与 Token 无关", res["note"])
            self.assertNotIn("手动填写", res["note"])
            self.assertIn("服务没启动", res["note"], "应识别为「服务没启动」而非笼统的连不上")
            self.assertIn("Cannot connect to host", res["note"])

    def test_last_error_is_surfaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)

            class Boom(_FakeAstrBotCache):
                async def size_bytes(self, target="cache"):
                    raise RuntimeError("disk on fire")

            plugin.engine.astrbot_cache = Boom()
            asyncio.run(plugin.engine.sweep(force=True))
            overview = asyncio.run(plugin.api_overview())["__json__"]
            self.assertIn("last_error", overview)
            self.assertIn("disk on fire", overview["last_error"])

    def test_transport_failures_are_classified(self):
        """传输层错误要按成因分类：拒连 / 超时 / DNS，措辞各不相同。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            import importlib

            auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
            tm = importlib.import_module("astrbot_plugin_orbit_command.transport")
            import socket

            cases = {
                "refused": (ConnectionRefusedError(111, "refused"), "服务没启动"),
                "timeout": (TimeoutError(), "不响应"),
                "dns": (socket.gaierror(-2, "Name or service not known"), "主机名解析失败"),
            }
            for label, (cause, expect) in cases.items():
                with self.subTest(label):
                    class Down:
                        def __init__(self, *a, **k):
                            self.access_token = a[1] if len(a) > 1 else ""

                        async def start(self): return None

                        async def close(self): return None

                        async def _request(self, action, params=None):
                            try:
                                raise cause
                            except BaseException as c:
                                raise tm.OneBotTransportError(
                                    f"{action} HTTP 请求失败") from c

                    auth.OneBotTransport = Down
                    res = asyncio.run(plugin.refresh_auth(force=True))
                    self.assertEqual(plugin.auth_state["mode"], "unreachable")
                    self.assertIn(expect, res["note"])
                    self.assertIn("与 Token 无关", res["note"])

    def test_auth_failure_is_not_labelled_as_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, onebot_access_token="whatever")
            import importlib

            auth = importlib.import_module("astrbot_plugin_orbit_command.auth")
            tm = importlib.import_module("astrbot_plugin_orbit_command.transport")

            class Deny:
                def __init__(self, *a, **k):
                    self.access_token = a[1] if len(a) > 1 else ""

                async def start(self): return None

                async def close(self): return None

                async def _request(self, action, params=None):
                    raise tm.OneBotActionError(action, 401, "unauthorized")

            auth.OneBotTransport = Deny
            res = asyncio.run(plugin.refresh_auth(force=True))
            self.assertEqual(plugin.auth_state["mode"], "auth_required")
            self.assertNotIn("与 Token 无关", res["note"], "鉴权问题不能说与 Token 无关")

    def test_discover_api_returns_search_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            payload = asyncio.run(plugin.api_discover())["__json__"]
            for key in ("candidates", "configs", "searched_roots", "truncated"):
                self.assertIn(key, payload)


# ---------- 8. 多实例与试运行 ----------

class TestMultiInstance(OrbitTestBase):
    def test_legacy_single_instance_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, onebot_base_url="http://127.0.0.1:3000",
                                     onebot_access_token="legacy")
            rows = plugin._instance_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["url"], "http://127.0.0.1:3000")
            self.assertEqual(rows[0]["token"], "legacy")

    def test_instances_take_precedence_over_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(
                tmp, onebot_base_url="http://old:3000", onebot_access_token="old",
                onebot_instances=[
                    {"url": "http://a:3000", "token": "t1", "enable": True,
                     "protocol_interval_hours": 6},
                    {"url": "ws://b:3001", "token": "", "enable": False,
                     "protocol_interval_hours": 3},
                ])
            rows = plugin._instance_rows()
            self.assertEqual([r["url"] for r in rows], ["http://a:3000", "ws://b:3001"])
            self.assertEqual(len(plugin.napcat.endpoints), 2)
            self.assertEqual([e.label for e in plugin.napcat.enabled_endpoints],
                             ["http://a:3000"])

    def test_protocol_layer_cleans_every_enabled_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, onebot_instances=[
                {"url": "http://a:3000", "enable": True, "protocol_interval_hours": 1},
                {"url": "http://b:3000", "enable": True, "protocol_interval_hours": 1},
                {"url": "http://c:3000", "enable": False, "protocol_interval_hours": 1},
            ])
            record = {"layer": "napcat_protocol", "label": "x", "at": "now", "acted": False,
                      "freed_bytes": 0, "items": 0, "skipped": "", "error": "", "detail": ""}
            asyncio.run(plugin.engine._run_napcat_protocol(record, force=True))
            self.assertTrue(record["acted"])
            self.assertEqual(record["items"], 2, "停用的实例不该被清理")
            self.assertIn("http://a:3000", record["detail"])
            self.assertIn("http://b:3000", record["detail"])
            self.assertNotIn("http://c:3000", record["detail"])

    def test_per_instance_interval_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, onebot_instances=[
                {"url": "http://a:3000", "enable": True, "protocol_interval_hours": 12},
                {"url": "http://b:3000", "enable": True, "protocol_interval_hours": 1},
            ])
            record = {"layer": "napcat_protocol", "label": "x", "at": "now", "acted": False,
                      "freed_bytes": 0, "items": 0, "skipped": "", "error": "", "detail": ""}
            asyncio.run(plugin.engine._run_napcat_protocol(record, force=True))
            # 给 a 写一个「刚清理过」，b 不写
            engine = plugin.engine
            engine._state["layers"].setdefault("napcat_protocol", {})["instances"] = {
                "http://a:3000": {"last_run_at": __import__("datetime").datetime.now().isoformat()}
            }
            record2 = {"layer": "napcat_protocol", "label": "x", "at": "now", "acted": False,
                       "freed_bytes": 0, "items": 0, "skipped": "", "error": "", "detail": ""}
            asyncio.run(engine._run_napcat_protocol(record2, force=False))
            self.assertEqual(record2["items"], 1, "只有 b 到了清理时间")

    def test_record_layer_preserves_per_instance_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, _ = self._engine(tmp)
            engine._state["layers"]["napcat_protocol"] = {
                "instances": {"http://a:3000": {"last_run_at": "2024-01-01"}}
            }
            engine._record_layer("napcat_protocol", {
                "at": "2024-02-02", "freed_bytes": 5, "acted": True, "error": "",
            })
            entry = engine._state["layers"]["napcat_protocol"]
            self.assertIn("instances", entry, "逐实例状态不能被整块覆盖掉")
            self.assertEqual(entry["instances"]["http://a:3000"]["last_run_at"], "2024-01-01")
            self.assertEqual(entry["last_run_at"], "2024-02-02")

    def test_overview_lists_every_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp, onebot_instances=[
                {"url": "http://a:3000", "enable": True, "protocol_interval_hours": 1},
                {"url": "http://b:3000", "enable": True, "protocol_interval_hours": 1},
            ])
            payload = asyncio.run(plugin.api_overview())["__json__"]
            urls = [r["url"] for r in payload["connection"]["instances"]]
            self.assertEqual(urls, ["http://a:3000", "http://b:3000"])
            live = [r["url"] for r in payload["napcat"]["instances"]]
            self.assertEqual(len(live), 2)


class TestDryRun(OrbitTestBase):
    def test_preview_reports_but_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "napcat" / "cache"
            cache.mkdir(parents=True)
            import os, time

            for i in range(4):
                f = cache / f"f{i}.bin"
                f.write_bytes(b"x" * 700 * 1024)
                old = time.time() - 86400
                os.utime(f, (old, old))
            engine, _, _ = self._engine(
                tmp, napcat_config_dirs=[str(cache.parent.parent)],
                napcat_cache_threshold_mb=1, napcat_cache_min_age_minutes=0)
            res = asyncio.run(engine.dry_run())
            self.assertGreater(res["would_delete_files"], 0)
            self.assertGreater(res["would_delete_bytes"], 0)
            self.assertTrue(any("f0.bin" in s for s in res["directories"][0]["samples"]))
            # 关键：一个文件都没被删
            self.assertEqual(len(list(cache.iterdir())), 4)

    def test_preview_respects_threshold_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "napcat" / "cache"
            cache.mkdir(parents=True)
            (cache / "small.bin").write_bytes(b"x" * 1024)
            engine, _, _ = self._engine(
                tmp, napcat_config_dirs=[str(cache.parent.parent)],
                napcat_cache_threshold_mb=10, napcat_cache_min_age_minutes=0)
            res = asyncio.run(engine.dry_run())
            self.assertEqual(res["would_delete_files"], 0)
            self.assertEqual(res["directories"][0]["over_threshold"], False)
            forced = asyncio.run(engine.dry_run(force=True))
            self.assertEqual(forced["would_delete_files"], 1)

    def test_force_actually_deletes_below_threshold(self):
        """占用低于阈值时，「强制全清」也必须真的删——这是用户最容易撞上的坑。"""
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "napcat" / "cache"
            cache.mkdir(parents=True)
            import os, time

            f = cache / "tiny.bin"
            f.write_bytes(b"x" * 4096)
            old = time.time() - 86400
            os.utime(f, (old, old))
            engine, _, _ = self._engine(
                tmp, napcat_config_dirs=[str(cache.parent.parent)],
                napcat_cache_threshold_mb=10, napcat_cache_min_age_minutes=0)
            asyncio.run(engine.sweep(force=True, layers=["napcat_files"]))
            self.assertFalse(f.exists(), "force 必须无视阈值真的删")

    def test_dryrun_api_does_not_touch_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self._plugin(tmp)
            before = json.loads((Path(tmp) / "history.json").read_text("utf-8")) \
                if (Path(tmp) / "history.json").exists() else []
            self._set_request_json({})
            res = asyncio.run(plugin.api_dryrun())["__json__"]
            self.assertIn("would_delete_files", res)
            after = json.loads((Path(tmp) / "history.json").read_text("utf-8")) \
                if (Path(tmp) / "history.json").exists() else []
            self.assertEqual(before, after, "试运行不该写历史")


if __name__ == "__main__":
    unittest.main(verbosity=2)
