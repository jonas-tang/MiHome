# SPDX-License-Identifier: GPL-3.0-or-later
"""打印场景动作的原始字段，用于补文案映射。

用法：

    .venv\\Scripts\\python.exe tools\\probe_scene_raw.py

只读：拉场景列表并把每条动作的 payload_json 原样打出，不执行场景。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "MIHOME_DATA_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "MiHome-Windows"),
)

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)
from app.core.service import MijiaService  # noqa: E402


def main() -> int:
    svc = MijiaService()
    raw_scenes = svc._api.get_scenes_list(None)
    if not raw_scenes:
        print("没有取到场景")
        return 1

    for scene in raw_scenes:
        name = scene.get("name")
        actions = (scene.get("scene_action") or {}).get("actions") or []
        print(f"\n=== 【{name}】 {len(actions)} 个动作 ===")
        for i, action in enumerate(actions, start=1):
            payload = action.get("payload_json") or {}
            print(f"[{i}] command={payload.get('command')!r} "
                  f"device={payload.get('device_name')!r}")
            print("    payload_json =",
                  json.dumps(payload, ensure_ascii=False)[:700])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
