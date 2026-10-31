# SPDX-License-Identifier: GPL-3.0-or-later
"""校验场景动作解析：把每条场景解析出的动作按详情页的样子打印出来。

用法（项目根目录、项目虚拟环境）：

    .venv\\Scripts\\python.exe tools\\check_scene_actions.py

只读：拉场景列表并解析，不执行任何场景。
"""

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
    scenes = svc.scene_list(None)
    if not scenes:
        print("没有取到场景")
        return 1

    for scene in scenes:
        print(f"\n【{scene.name}】 家庭={scene.home_name or '未知'}  "
              f"动作={len(scene.actions)} 个  id={scene.scene_id}")
        if not scene.actions:
            print("    （接口没有返回动作明细）")
            continue
        for i, action in enumerate(scene.actions, start=1):
            delay = f"延时 {action.delay}s " if action.delay else ""
            print(f"    {i}. {delay}{action.device_name or '(无设备名)'}"
                  f"  →  {action.text or '(无描述)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
