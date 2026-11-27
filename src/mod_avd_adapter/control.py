"""ModAvd 实例控制、资源占用与配置恢复。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID, uuid5

from auto_mas_core import LockTicket, config_manager
from auto_mas_core.adapters.game import DeviceSubscribeError
from auto_mas_core.devices import (
    DeviceInfo,
    DeviceRef,
    DeviceStatus,
    EmulatorControl,
    EmulatorExpect,
    EmulatorRuntimePolicy,
    InstanceSettings,
)
from auto_mas_core.utils import file_lock, get_logger

from .avd import updater
from .avd.components import manager_key_exe
from .avd.constants import AvdAdbInfo
from .avd.maafw_extras import avd_extras_supported, build_maafw_avd_config
from .avd.manager import AvdHostAdb, AvdManager, build_manager, opening_instances
from .config import ModAvdDevice, ModAvdGame

logger = get_logger("ModAvd 模拟器适配")


class Control(EmulatorControl[ModAvdGame, ModAvdDevice]):
    kind = "avd"

    device_type = ModAvdDevice

    capabilities = (
        "open",
        "close",
        "show",
        "hide",
        "create_instance",
        "delete_instance",
        "install_app",
        "settings",
        "instance_options",
        "components",
        "update",
    )

    def recovery_pending(self, uid: UUID) -> bool:
        return bool(self.config.devices[uid].session.backup)

    def __init__(self, config: ModAvdGame | None = None) -> None:
        super().__init__(config=config)
        self._backend: AvdManager | None = None
        self._operation = asyncio.Lock()
        self._resources: dict[UUID, BinaryIO] = {}
        self._maintenance: dict[str, Any] = {}

    @property
    def busy(self) -> bool:
        return self._operation.locked()

    async def build_backend(self, policy: EmulatorRuntimePolicy) -> AvdManager:
        info = self.config.info
        if info.path is None:
            raise ValueError("尚未选择 ModAVD 安装")
        return await build_manager(
            str(info.path),
            info.max_wait_time,
            info.force_kill_on_close,
            runtime_policy=policy,
        )

    async def backend(self) -> AvdManager:
        if self._backend is None:
            self._backend = await self.build_backend(
                EmulatorRuntimePolicy(
                    silence_enabled=lambda: self.runtime.silent,
                    block_ads_enabled=lambda: self.runtime.block_ad,
                    resource_path=self.runtime.resource_path,
                )
            )
        return self._backend

    def device(self, uid: UUID) -> ModAvdDevice:
        device = self.config.devices[uid]
        if device.info.tombstone:
            raise KeyError("所选模拟器实例已删除")
        return device

    async def _refresh(self) -> None:
        backend = await self.backend()
        devices = await backend.getInfo(None)
        entry = self.config
        existing = {
            d.info.index: d for d in entry.devices.values() if not d.info.tombstone
        }
        next_slot = max((d.info.slot for d in entry.devices.values()), default=-1) + 1
        for index, info in devices.items():
            device = existing.get(str(index))
            if device is None:
                uid = uuid5(entry.uid, f"{index}:{next_slot}")
                entry.devices.add(
                    self.device_type,
                    uid=uid,
                    payload={"info": {"index": str(index), "slot": next_slot}},
                )
                await entry.devices.commit()
                device = entry.devices[uid]
                next_slot += 1
            device.info.name = info.title
            device.info.status = DeviceStatus(int(info.status))
            device.info.adb_address = info.adb_address
            await device.commit()
        # 发现暂缺不删除条目，尤其不能丢掉残留备份与有效绑定。
        for index, device in existing.items():
            if index not in devices:
                device.info.status = DeviceStatus.NOT_FOUND
                await device.commit()

    async def refresh(self) -> None:
        async with self.operation_scope(self._operation):
            await self._refresh()

    async def _acquire_resource(self, uid: UUID) -> None:
        device = self.device(uid)
        path = self.config.info.path
        if path is None:
            raise ValueError("尚未配置模拟器管理程序")
        identity = (
            f"{self.kind}:{os.path.normcase(str(path.resolve()))}:{device.info.index}"
        )
        key = hashlib.sha256(identity.encode()).hexdigest()
        directory = Path(tempfile.gettempdir()) / "AUTO-MAS" / "device-locks"
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / key).open("a+b")
        try:
            file_lock.acquire(handle)
        except OSError as error:
            handle.close()
            raise DeviceSubscribeError(
                "设备已被其他托管或 MAS 进程占用", usage="其他设备任务"
            ) from error
        self._resources[uid] = handle

    async def _backup(self, uid: UUID) -> dict:
        settings = await self.read_settings(uid)
        return {
            key: value for key, value in settings.values().items() if value is not None
        }

    async def _restore(self, uid: UUID, backup: dict) -> None:
        await self._write_settings(uid, backup)

    async def _retry_restore(self, uid: UUID) -> None:
        device = self.config.devices[uid]
        if device.session.backup:
            await self._restore(uid, device.session.backup)
            device.session.backup = {}
            await device.commit()
            await config_manager.flush()

    async def _on_device_subscribed(
        self, device_uid: UUID, expect: object | None
    ) -> None:
        if expect is not None and not isinstance(expect, EmulatorExpect):
            raise TypeError("模拟器期望须为 EmulatorExpect")
        await self._acquire_resource(device_uid)
        async with self.operation_scope(self._operation):
            device = self.device(device_uid)
            try:
                await self._retry_restore(device_uid)
            except Exception as error:
                logger.warning(f"上次配置仍未恢复，保留备份并继续本轮任务：{error}")
            if expect is not None:
                if not device.session.backup:
                    device.session.backup = await self._backup(device_uid)
                    await device.commit()
                    await config_manager.flush()
                await self.apply_expect(device_uid, expect)

    async def _on_device_unsubscribed(self, device_uid: UUID) -> None:
        if device_uid not in self._resources:
            return
        try:
            async with self.operation_scope(self._operation):
                await self._retry_restore(device_uid)
        finally:
            handle = self._resources.pop(device_uid, None)
            if handle is not None:
                try:
                    file_lock.release(handle)
                finally:
                    handle.close()

    async def apply_expect(self, uid: UUID, expect: EmulatorExpect) -> None:
        changes = {
            key: value
            for key, value in asdict(expect).items()
            if key in {"width", "height", "dpi"} and value is not None
        }
        await self._write_settings(uid, changes)

    async def open(
        self, device_uid: UUID, package: str | None = None, *, ticket: LockTicket
    ) -> None:
        self.check_subscription(device_uid, ticket)
        async with self.operation_scope(self._operation):
            backend = await self.backend()
            await self.open_backend(backend, device_uid, package, ticket=ticket)
            await self._refresh()

    async def open_backend(
        self, backend: AvdManager, uid: UUID, package: str | None, *, ticket: LockTicket
    ) -> None:
        from auto_mas_core.adapters.game import TRIGGER_TICKET

        await backend.open(
            self.device(uid).info.index, package or "", manual=ticket is TRIGGER_TICKET
        )

    async def close(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        self.check_subscription(device_uid, ticket)
        async with self.operation_scope(self._operation):
            backend = await self.backend()
            await backend.close(self.device(device_uid).info.index)
            await self._refresh()

    async def _visible(self, uid: UUID, ticket: LockTicket, visible: bool) -> None:
        self.check_subscription(uid, ticket)
        async with self.operation_scope(self._operation):
            backend = await self.backend()
            await backend.setVisible(self.device(uid).info.index, visible)
            await self._refresh()

    async def show(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        await self._visible(device_uid, ticket, True)

    async def hide(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        await self._visible(device_uid, ticket, False)

    async def minimize(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        self.check_subscription(device_uid, ticket)
        async with self.operation_scope(self._operation):
            await self.window_state(device_uid, minimized=True)

    async def maximize(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        self.check_subscription(device_uid, ticket)
        async with self.operation_scope(self._operation):
            await self.window_state(device_uid, minimized=False)

    async def window_state(self, uid: UUID, *, minimized: bool) -> None:
        raise NotImplementedError("当前模拟器不提供窗口最小化或最大化能力")

    async def launch_app(
        self, device_uid: UUID, package: str, *, ticket: LockTicket
    ) -> None:
        self.check_subscription(device_uid, ticket)
        backend = await self.backend()
        result = await backend.launch_app(self.device(device_uid).info.index, package)
        if not result.ok:
            raise RuntimeError(f"应用启动失败：{result.reason}: {result.detail}")

    async def open_store(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        self.check_subscription(device_uid, ticket)
        backend = await self.backend()
        result = await backend.open_store(self.device(device_uid).info.index)
        if not result.ok:
            raise RuntimeError(f"应用商店启动失败：{result.reason}: {result.detail}")

    async def create_instance(
        self, name: str | None = None, *, options: dict[str, int] | None = None
    ) -> UUID:
        async with self.operation_scope(self._operation):
            backend = await self.backend()
            index = str(await backend.create_instance(name, **(options or {})))
            await self._refresh()
            return next(
                d.uid
                for d in self.config.devices.values()
                if d.info.index == index and not d.info.tombstone
            )

    async def delete_instance(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        self.check_subscription(device_uid, ticket)
        async with self.operation_scope(self._operation):
            device = self.device(device_uid)
            backend = await self.backend()
            await backend.delete_instance(device.info.index)
            device.info.tombstone = True
            device.info.status = DeviceStatus.NOT_FOUND
            await device.commit()

    async def install_app(
        self, device_uid: UUID, path: Path, *, ticket: LockTicket
    ) -> None:
        self.check_subscription(device_uid, ticket)
        async with self.operation_scope(self._operation):
            backend = await self.backend()
            await backend.install_apk(self.device(device_uid).info.index, path)

    async def device_info(self, uid: UUID) -> DeviceInfo:
        backend = await self.backend()
        return (await backend.getInfo(self.device(uid).info.index))[
            self.device(uid).info.index
        ]

    def resolve_device(self, uid: UUID) -> DeviceRef:
        return DeviceRef(
            self.kind, str(self.config.info.path), self.device(uid).info.index
        )

    def get_adb_path(self) -> Path | None:
        return self._backend.get_adb_path() if self._backend is not None else None

    async def read_settings(self, uid: UUID) -> InstanceSettings:
        backend = await self.backend()
        return await backend.read_instance_settings(self.device(uid).info.index)

    async def _write_settings(
        self,
        uid: UUID,
        changes: dict[str, int],
        expected: dict[str, int | None] | None = None,
    ) -> dict[str, int]:
        backend = await self.backend()
        return await backend.write_instance_settings(
            self.device(uid).info.index, changes, expected
        )

    async def write_settings(
        self,
        uid: UUID,
        changes: dict[str, int],
        expected: dict[str, int | None] | None,
        *,
        ticket: LockTicket,
    ) -> dict[str, int]:
        self.check_subscription(uid, ticket)
        async with self.operation_scope(self._operation):
            return await self._write_settings(uid, changes, expected)

    async def save_settings(
        self,
        uid: UUID,
        changes: dict[str, int],
        expected: dict[str, int | None] | None,
        *,
        ticket: LockTicket,
    ) -> InstanceSettings:
        await self.write_settings(uid, changes, expected, ticket=ticket)
        return await self.read_settings(uid)

    async def sweep_backups(self) -> None:
        for device in list(self.config.devices.values()):
            if device.session.backup and not device.info.tombstone:
                try:
                    await self._acquire_resource(device.uid)
                except DeviceSubscribeError:
                    continue
                try:
                    await self._on_device_unsubscribed(device.uid)
                except Exception as error:
                    logger.warning(f"启动恢复失败，留待下次尝试：{error}")

    async def startup(self):
        await updater.startup(host=self._update_host())
        await self.sweep_backups()

    async def teardown(self):
        if self._backend is not None:
            await self._backend.stop_observers()

    @property
    def maintenance_snapshot(self):
        return dict(self._maintenance)

    def _update_host(self):
        async def publish(root, progress):
            self._maintenance["progress"] = asdict(progress)
            self.runtime_changed()

        return updater.UpdateHost(
            settings=lambda: updater.UpdateSettings(
                **asdict(self.runtime.update_options)
            ),
            environment=lambda: self.runtime.process_environment,
            managed_roots=lambda: [self.installation_root(None)],
            opening_instances=opening_instances,
            publish_progress=publish,
        )

    async def maintenance(self, action):
        root = self.installation_root(None)
        host = self._update_host()
        if action == "components":
            self._maintenance["components"] = await self.component_status()
        elif action in {"status", "check_update"}:
            self._maintenance["update"] = await updater.status(
                str(root), check=action == "check_update", host=host
            )
        elif action == "update":
            self._maintenance["update"] = await updater.update(str(root), host=host)
            job = updater._runtime(root).job
            if job is not None:
                await asyncio.shield(job)
            self._maintenance["update"] = await updater.status(str(root), host=host)
        else:
            raise ValueError("未知 ModAVD 维护操作")
        self.runtime_changed()
        return self.maintenance_snapshot

    async def instance_options(self, uid: UUID) -> dict[str, Any]:
        backend = await self.backend()
        return await backend.instance_options(self.device(uid).info.index)

    async def component_status(self):
        backend = await self.backend()
        return await backend._cli.check()

    async def close_all(self):
        backend = await self.backend()
        result = await backend._cli.stop_all()
        if not isinstance(result, dict) or result.get("failed"):
            raise RuntimeError("部分 ModAVD 实例未能正常关闭")
        await self.refresh()

    async def search_updates(self):
        backend = await self.backend()
        return await backend._cli.update("check")

    async def adb_info(self, uid: UUID) -> AvdAdbInfo:
        backend = await self.backend()
        return await backend.adb_info(self.device(uid).info.index)

    async def host_adb(self, uid: UUID) -> AvdHostAdb:
        backend = await self.backend()
        return await backend.host_adb(self.device(uid).info.index)

    async def maafw_adb_extra(self, uid: UUID) -> dict[str, object]:
        info = await self.adb_info(uid)
        return build_maafw_avd_config(
            Path(info.adb_exe),
            info.adb_server_port,
            info.ports.console,
            modavd_root=self.installation_root(uid),
        )

    def supports_maafw_adb_extra_version(self, version: str | None) -> bool | None:
        return avd_extras_supported(version)

    def installation_root(self, uid: UUID | None) -> Path:
        from .avd.components import root_from_manager_exe

        path = self.config.info.path
        if path is None:
            raise ValueError("尚未配置 ModAVD 安装位置")
        return root_from_manager_exe(path)

    async def process_env(self, uid: UUID) -> dict[str, str] | None:
        import os

        from .avd import host

        root = self.installation_root(uid)
        try:
            await host.ensure_script_adb_server(root)
        except host.ScriptAdbPortConflict:
            raise
        except Exception:
            if not host.script_adb_port_known(root):
                raise
        return dict(os.environ) | host.script_adb_env(root)


def resolve_installation(raw: str) -> str:
    path = Path(raw)
    if path.name.casefold() == "emulator.exe":
        return str(path)
    return str(manager_key_exe(path))


async def search() -> list[ModAvdGame]:
    """ModAVD 安装根目录由用户选择，系统卸载列表不包含其 CLI。"""
    return []
