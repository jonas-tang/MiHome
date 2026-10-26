# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""产品页中文名持久化：model -> 中文商品名（或 None=确认没有）。

米家 APP 对未改名设备显示的是产品库里的本地化商品名，而设备列表接口
只给英文默认名（第三方/国际版产品尤甚）。中文名只能从 miot-spec 的
产品页（/p/<model>）抓，单型号一次约 2~3 秒。

以前这份结果只存在内存里：每次启动都要把二十多个型号重新抓一遍，
排在后面的型号（比如空调网关）要等半分钟以上才轮到，用户看到的一直
是英文名。落盘之后第二次启动即刻可用，只有从没见过的新型号才发请求。
"""

from app.core import _json_store

_CACHE_VERSION = 1
_FILENAME = "product_names.json"


def load() -> dict[str, str | None]:
    """读磁盘缓存；无效或损坏返回空 dict。None 表示已确认没有中文名。"""
    raw = _json_store.read_json(_json_store.data_file(_FILENAME), {})
    if raw.get("version") != _CACHE_VERSION:
        return {}
    names = raw.get("names")
    if not isinstance(names, dict):
        return {}
    result: dict[str, str | None] = {}
    for model, name in names.items():
        if not isinstance(model, str) or not model:
            continue
        result[model] = name if isinstance(name, str) and name else None
    return result


def save(names: dict[str, str | None]) -> None:
    _json_store.write_json(
        _json_store.data_file(_FILENAME),
        {"version": _CACHE_VERSION, "names": dict(names)},
    )
