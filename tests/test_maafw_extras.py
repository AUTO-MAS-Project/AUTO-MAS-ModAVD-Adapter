from __future__ import annotations

from pathlib import Path

from mod_avd_adapter.avd.maafw_extras import (
    avd_extras_supported,
    build_maafw_avd_config,
    modavd_extras_library,
)


def _root_with_library(tmp_path: Path) -> Path:
    library = modavd_extras_library(tmp_path)
    library.parent.mkdir(parents=True)
    library.write_bytes(b"")
    return tmp_path


def test_without_root_only_avd_extras():
    config = build_maafw_avd_config("adb.exe", 31099, 31100)

    assert config["extras"] == {"avd": {"enable": True}}
    assert config["command"]["EmuWebrtcStart"] == [
        "adb.exe",
        "-P",
        "31099",
        "-s",
        "emulator-31100",
        "emu",
        "screenrecord webrtc start",
    ]
    assert config["command"]["EmuWebrtcStop"] == ["adb.exe", "-P", "31099", "version"]


def test_root_with_library_adds_modavd_extras(tmp_path):
    root = _root_with_library(tmp_path)

    config = build_maafw_avd_config("adb.exe", 31099, 31100, modavd_root=root)

    assert config["extras"]["avd"] == {"enable": True}
    assert config["extras"]["modavd"] == {
        "enable": True,
        "path": str(root),
        "console_port": 31100,
    }
    # AVDExtras 那份照旧完整，5.x 的 MaaFramework 不认 modavd 键
    assert "EmuWebrtcStart" in config["command"]


def test_root_without_library_keeps_avd_only(tmp_path):
    config = build_maafw_avd_config("adb.exe", 31099, 31100, modavd_root=tmp_path)

    assert "modavd" not in config["extras"]


def test_library_path_matches_maafw_lookup(tmp_path):
    # MaaFramework 拼的是 <path>/extras/modavd_extras(.dll)
    assert modavd_extras_library(tmp_path) == tmp_path / "extras" / "modavd_extras.dll"


def test_avd_extras_version_gate_unchanged():
    assert avd_extras_supported("5.6.9") is False
    assert avd_extras_supported("5.7.0") is True
    assert avd_extras_supported("6.0.0b3") is True
    assert avd_extras_supported(None) is None
