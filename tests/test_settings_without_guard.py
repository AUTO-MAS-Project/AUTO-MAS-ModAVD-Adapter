"""ModAVD 不再做配置守卫与稳定模式：只有用户保存设置和任务期望会调 ``modavd set``。

假的 modavd 照 ModAVD v0.43.0 的 ``--json`` 契约回信封（``internal/emu/types.go`` 的
``InstanceInfo`` / ``UpdateResult``、``internal/cli/commands.go`` 的 ``HelpResult``），
不起任何进程、不碰真实安装。
"""

from __future__ import annotations

import asyncio
import copy
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from auto_mas_core.adapters.game import TRIGGER_TICKET
from auto_mas_core.devices import EmulatorExpect, EmulatorRuntimePolicy

import mod_avd_adapter.control as control_module
from mod_avd_adapter.avd.cli import ModAvdCli, ProcessOutput
from mod_avd_adapter.avd.manager import AvdManager
from mod_avd_adapter.control import Control

_PRESETS = {1080: (1920, 1080, 280), 720: (1280, 720, 240)}


def _instance(index: int, config: dict) -> dict:
    """``modavd --json info <i>`` 的 ``data``（v0.43.0 ``InstanceInfo``，一台关着的实例）。"""
    console = 21000 + index * 2
    return {
        "index": index,
        "name": f"mas_{index}",
        "title": f"mas_{index}",
        "status": "offline",
        "running": False,
        "booted": False,
        "window": False,
        "busy": "",
        "bridge": "installed",
        "play_store": False,
        "play_store_partial": False,
        "gles32": True,
        "creation": "",
        "creation_error": "",
        "creation_log": "",
        "creation_progress": 0,
        "pid": 0,
        "started_at": "",
        "started_at_ms": 0,
        "serial": f"emulator-{console}",
        "adb_address": f"127.0.0.1:{console + 1}",
        "ports": {"console": console, "adb": console + 1, "grpc": 8554 + index},
        "screencap": {"shm": f"SHM_videmulator{console}", "shm_ready": False},
        "config": copy.deepcopy(config),
        "initialized": True,
        "launcher": "org.modavd.launcher",
        "renderer": "host",
        "software_renderer": False,
        "diagnostics": False,
        "boss_key": "",
        "path": f"C:\\ModAVD\\avd\\mas_{index}.avd",
    }


class FakeModAvd:
    """按参数回 ``--json`` 信封的 modavd，记下每一次 ``set``。"""

    def __init__(self) -> None:
        width, height, dpi = _PRESETS[1080]
        self.config = {
            "memory_mb": 6144,
            "cpu": 6,
            "fps": 60,
            "resolution": 1080,
            "data_partition_gb": 64,
            "data_disk_mode": "manual",
            "width": width,
            "height": height,
            "dpi": dpi,
            "dpi_setting": 0,
            "orientation": "landscape",
            "sound": False,
        }
        self.calls: list[list[str]] = []
        self.sets: list[list[str]] = []

    @staticmethod
    def _ok(data: object) -> ProcessOutput:
        return ProcessOutput(0, json.dumps({"ok": True, "data": data}), "")

    def _set(self, index: int, args: list[str]) -> dict:
        options = dict(zip(args[::2], args[1::2], strict=True))
        names = {
            "--memory": "memory_mb",
            "--cpu": "cpu",
            "--fps": "fps",
            "--resolution": "resolution",
        }
        applied: dict[str, object] = {}
        for flag, raw in options.items():
            name = names[flag]
            self.config[name] = int(raw)
            applied[name] = int(raw)
        if "resolution" in applied:
            width, height, dpi = _PRESETS[int(applied["resolution"])]
            self.config.update(width=width, height=height, dpi=dpi)
        return {
            "instance": _instance(index, self.config),
            "applied": applied,
            "applies_next_boot": False,
        }

    async def runner(self, args, timeout, timeout_handler=None, breakaway=False, **_):
        command = list(args[args.index("--json") + 1 :])
        self.calls.append(command)
        name, rest = command[0], command[1:]
        if name == "help":
            return self._ok(
                {
                    "usage": "modavd [--root 目录] [--json] <命令> [参数]",
                    "commands": [
                        {
                            "name": rest[0],
                            "usage": f"modavd {rest[0]}",
                            "summary": "",
                            "details": "",
                            "options": [
                                {"flag": "--resolution", "help": "分辨率：1080 / 720"}
                            ],
                        }
                    ],
                }
            )
        if name == "info":
            return self._ok(_instance(int(rest[0]), self.config))
        if name == "set":
            self.sets.append(rest)
            return self._ok(self._set(int(rest[0]), rest[1:]))
        raise AssertionError(f"测试没有准备 modavd {name}")


class _Node:
    """配置节点的替身：只有字段和 ``commit``。"""

    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _Devices(dict):
    async def commit(self) -> None:
        pass


def _setup(tmp_path: Path, monkeypatch, *, baselines: dict | None = None):
    root = tmp_path / "ModAVD"
    exe = root / "sdk" / "emulator" / "emulator.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    uid = uuid4()
    device = _Node(
        uid=uid,
        info=SimpleNamespace(index="0", tombstone=False, slot=0),
        session=SimpleNamespace(backup={}),
    )
    config = _Node(
        uid=uuid4(),
        info=SimpleNamespace(
            path=exe,
            max_wait_time=300,
            force_kill_on_close=False,
            # 存量配置里的守卫和稳定模式开关：适配不再读它们
            stable_mode=True,
            config_guard=True,
            baselines={str(uid): {"cpu": 2, "memoryMb": 2048}}
            if baselines is None
            else baselines,
        ),
        devices=_Devices({uid: device}),
    )
    fake = FakeModAvd()
    backend = AvdManager(
        exe,
        300,
        False,
        runtime_policy=EmulatorRuntimePolicy(
            silence_enabled=lambda: True,
            block_ads_enabled=lambda: False,
            resource_path=tmp_path,
        ),
        cli=ModAvdCli(root, runner=fake.runner),
    )
    control = Control(config=config)  # type: ignore[arg-type]
    control._backend = backend

    @asynccontextmanager
    async def scope(lock: asyncio.Lock):
        async with lock:
            yield

    control.operation_scope = scope  # type: ignore[method-assign]

    async def flush() -> None:
        pass

    monkeypatch.setattr(control_module, "config_manager", SimpleNamespace(flush=flush))
    # 设备锁文件落在测试目录，不进系统临时目录里 MAS 自己用的那份
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return control, backend, fake, config, device, uid


def test_subscribe_without_expect_never_sets(tmp_path, monkeypatch):
    control, _, fake, config, device, uid = _setup(tmp_path, monkeypatch)
    before = copy.deepcopy(config.info.baselines)

    async def scenario() -> None:
        await control._on_device_subscribed(uid, None)
        await control._on_device_unsubscribed(uid)

    asyncio.run(scenario())

    assert fake.sets == []
    assert device.session.backup == {}
    assert config.info.baselines == before


def test_manual_open_and_close_never_set(tmp_path, monkeypatch):
    # 控制器只弱引用配置，测试里要一直拿着它
    control, backend, fake, config, _, uid = _setup(tmp_path, monkeypatch)
    lifecycle: list[tuple[str, str]] = []

    async def open_(idx, package_name="", *, manual=False):
        lifecycle.append(("open", idx))

    async def close(idx):
        lifecycle.append(("close", idx))

    async def refresh() -> None:
        pass

    monkeypatch.setattr(backend, "open", open_)
    monkeypatch.setattr(backend, "close", close)
    monkeypatch.setattr(control, "_refresh", refresh)

    async def scenario() -> None:
        await control.open(uid, ticket=TRIGGER_TICKET)
        await control.close(uid, ticket=TRIGGER_TICKET)

    asyncio.run(scenario())

    assert lifecycle == [("open", "0"), ("close", "0")]
    assert fake.sets == []
    assert fake.calls == []
    assert config.info.config_guard is True


def test_save_settings_writes_without_touching_baselines(tmp_path, monkeypatch):
    control, _, fake, config, _, uid = _setup(
        tmp_path, monkeypatch, baselines={"other": {"cpu": 6}}
    )

    settings = asyncio.run(
        control.save_settings(uid, {"cpu": 4}, None, ticket=TRIGGER_TICKET)
    )

    assert fake.sets == [["0", "--cpu", "4"]]
    assert settings.values()["cpu"] == 4
    assert config.info.baselines == {"other": {"cpu": 6}}
    assert config.commits == 0


def test_expect_backup_and_restore_unchanged(tmp_path, monkeypatch):
    control, _, fake, config, device, uid = _setup(tmp_path, monkeypatch)
    before = copy.deepcopy(config.info.baselines)

    async def subscribe() -> None:
        await control._on_device_subscribed(uid, EmulatorExpect(1280, 720, 240))

    asyncio.run(subscribe())

    # 只按任务期望改分辨率，存量基准里的 CPU / 内存不写回
    assert fake.sets == [["0", "--resolution", "720"]]
    assert device.session.backup == {
        "width": 1920,
        "height": 1080,
        "dpi": 280,
        "cpu": 6,
        "memoryMb": 6144,
        "fps": 60,
    }
    assert (fake.config["width"], fake.config["height"]) == (1280, 720)

    asyncio.run(control._on_device_unsubscribed(uid))

    assert fake.sets[1] == [
        "0",
        "--memory",
        "6144",
        "--cpu",
        "6",
        "--fps",
        "60",
        "--resolution",
        "1080",
    ]
    assert len(fake.sets) == 2
    assert (fake.config["width"], fake.config["height"]) == (1920, 1080)
    assert device.session.backup == {}
    assert config.info.baselines == before


def test_guard_and_stable_mode_entries_fall_back_to_host(tmp_path, monkeypatch):
    control, backend, fake, config, _, uid = _setup(tmp_path, monkeypatch)
    assert config.info.stable_mode is True

    with pytest.raises(NotImplementedError, match="配置守卫"):
        asyncio.run(control.capture_baseline(uid, ticket=TRIGGER_TICKET))
    with pytest.raises(NotImplementedError, match="稳定模式"):
        asyncio.run(control.apply_stable_mode(uid, ticket=TRIGGER_TICKET))
    for name in (
        "apply_stable_mode",
        "read_stable_mode",
        "read_instance_overview",
        "restart",
    ):
        assert not hasattr(backend, name)
    assert fake.calls == []
