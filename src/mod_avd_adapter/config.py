"""ModAvd 模拟器适配配置。"""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

from auto_mas_core import (
    STANDARD_BLOCKED_DIRS,
    ConfigCollection,
    Trigger,
    collection,
    path_policy,
    ui,
)
from auto_mas_core.adapters.game import trigger_field
from auto_mas_core.devices import ManagedEmulatorDevice, ManagedEmulatorEntry
from pydantic import Field

if TYPE_CHECKING:
    from .control import Control


class ModAvdDevice(ManagedEmulatorDevice):
    pass


class ModAvdGame(ManagedEmulatorEntry):
    class Info(ManagedEmulatorEntry.Info):
        path: Annotated[
            Path | None, path_policy(kind="file", blocked_dirs=STANDARD_BLOCKED_DIRS)
        ] = Field(
            default=None,
            description="ModAVD 的 sdk/emulator/emulator.exe；组件未安装时可暂缺",
        )

    class Action(ManagedEmulatorEntry.Action):
        check_components: Trigger = Field(
            default=None, description="检查组件与运行环境"
        )
        check_update: Trigger = Field(default=None, description="检查 ModAVD 更新")
        update: Trigger = Field(default=None, description="更新 ModAVD")

    class Creation(ManagedEmulatorEntry.Creation):
        memory_mb: int | None = Field(default=None, description="内存 MB")
        cpu: int | None = Field(default=None, description="CPU 核数")
        data_partition_gb: int | None = Field(default=None, description="数据分区 GB")
        fps: int | None = Field(default=None, description="帧率")
        resolution: int | None = Field(default=None, description="分辨率档位")

    info: Info = Field(default_factory=Info)  # pyright: ignore[reportIncompatibleVariableOverride]
    action: Annotated[Action, ui(visibility=lambda entry: entry._vis_actions())] = (  # pyright: ignore[reportIncompatibleVariableOverride]
        Field(default_factory=Action)
    )
    creation: Creation = Field(default_factory=Creation)  # pyright: ignore[reportIncompatibleVariableOverride]

    @trigger_field("action.create_instance", finish_on_cancel=True)
    async def on_create_instance(self):
        options = {
            key: getattr(self.creation, key)
            for key in ("memory_mb", "cpu", "data_partition_gb", "fps", "resolution")
            if getattr(self.creation, key) is not None
        }
        control = cast("Control", self._control)
        await control.create_instance(self.creation.name or None, options=options)

    @trigger_field("action.check_components")
    async def on_check_components(self):
        await self._control.maintenance("components")

    @trigger_field("action.check_update")
    async def on_check_update(self):
        await self._control.maintenance("check_update")

    @trigger_field("action.update", finish_on_cancel=True)
    async def on_update(self):
        await self._control.maintenance("update")

    devices: ConfigCollection[ModAvdDevice] = collection(ModAvdDevice)  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]
