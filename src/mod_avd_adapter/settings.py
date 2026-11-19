"""适配器实例参数校验和变更冲突检查。"""

from typing import Any

_RANGES: dict[str, tuple[int, int]] = {
    "width": (160, 4096),
    "height": (160, 4096),
    "dpi": (80, 640),
    "cpu": (1, 64),
    "memoryMb": (512, 65536),
    "fps": (1, 240),
}


def _as_int(raw: Any) -> int | None:
    """只认真正的整数值。

    ``bool`` 是 ``int`` 的子类，得单独挡掉；浮点只在恰好是整数时接受
    （雷电偶尔把内存写成 ``2048.0``）。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    return None


def validate_changes(changes: dict[str, Any]) -> dict[str, int]:
    """校验并归一化一批改动。非法输入直接抛，带上人能读的原因。"""
    if not changes:
        raise ValueError("没有要修改的设置项")

    cleaned: dict[str, int] = {}
    for name, raw in changes.items():
        if name not in _RANGES:
            raise ValueError(f"未知的设置项: {name}")
        value = _as_int(raw)
        if value is None:
            raise ValueError(f"设置项 {name} 需要一个整数")
        low, high = _RANGES[name]
        if not low <= value <= high:
            raise ValueError(f"设置项 {name} 超出可接受范围 {low}~{high}: {value}")
        cleaned[name] = value

    # 成对修改宽高，保证得到用户指定的分辨率。
    if ("width" in cleaned) != ("height" in cleaned):
        raise ValueError("分辨率的宽和高必须同时提供")

    return cleaned
