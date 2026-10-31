# SPDX-License-Identifier: GPL-3.0-or-later
"""探测米家"场景详情"接口：确认能否取到场景的动作明细。

用法（在项目根目录、用项目虚拟环境跑）：

    .venv\\Scripts\\python.exe tools\\probe_scene.py

脚本只发起**只读**请求：拉场景列表、逐条试候选的详情接口，
把每个候选的返回码和字段结构打出来。不会执行场景、不会改任何配置。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "MIHOME_DATA_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "MiHome-Windows"),
)

from PySide6.QtWidgets import QApplication  # noqa: E402  (需要 QApplication 才能建 Service)

app = QApplication(sys.argv)
from app.core.service import MijiaService  # noqa: E402


def brief(obj, limit: int = 600) -> str:
    text = json.dumps(obj, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def try_call(api, uri: str, data: dict, label: str) -> dict | None:
    """发一次请求，打印结果；异常也打印，不让脚本中断。"""
    try:
        ret = api._request(uri, data)
    except Exception as exc:  # noqa: BLE001 - 探测脚本要看到所有失败
        print(f"  [{label}] 异常: {type(exc).__name__}: {exc}")
        return None
    print(f"  [{label}] 返回类型={type(ret).__name__}")
    if isinstance(ret, dict):
        print(f"    顶层键: {list(ret.keys())[:12]}")
        print(f"    内容: {brief(ret, 400)}")
    else:
        print(f"    内容: {brief(ret, 400)}")
    return ret if isinstance(ret, (dict, list)) else None


def main() -> int:
    svc = MijiaService()
    api = svc._api
    homes = svc._home_ids_map()
    print("家庭:", homes)

    # ---- 1. 现有列表接口：对比 get_type 的不同取值 ----
    for home_name, home_id in list(homes.items())[:1]:
        print(f"\n=== 家庭 {home_name} ({home_id}) ===")
        owner = api._get_home_owner(home_id)
        print("owner_uid:", owner)
        uri = "/appgateway/miot/appsceneservice/AppSceneService/GetSimpleSceneList"
        scenes_raw = None
        for get_type in (2, 1, 0, 3):
            print(f"\n-- GetSimpleSceneList get_type={get_type} --")
            ret = try_call(api, uri, {"app_version": 12, "get_type": get_type,
                                      "home_id": str(home_id), "owner_uid": owner},
                           f"get_type={get_type}")
            if ret and scenes_raw is None and ret.get("manual_scene_info_list"):
                scenes_raw = ret["manual_scene_info_list"]

        if not scenes_raw:
            print("\n未拿到场景列表，后续探测跳过")
            return 1

        print(f"\n场景数: {len(scenes_raw)}")
        first = scenes_raw[0]
        print("第一条的完整字段:")
        print(json.dumps(first, ensure_ascii=False, indent=2)[:1500])
        scene_id = str(first.get("scene_id") or "")
        print(f"\n取第一个场景 scene_id={scene_id} 探测详情接口")

        # ---- 2. 候选详情接口 ----
        candidates = [
            ("/appgateway/miot/appsceneservice/AppSceneService/GetScene",
             {"app_version": 12, "scene_id": scene_id, "home_id": str(home_id), "owner_uid": owner}),
            ("/appgateway/miot/appsceneservice/AppSceneService/GetSceneDetail",
             {"app_version": 12, "scene_id": scene_id, "home_id": str(home_id), "owner_uid": owner}),
            ("/appgateway/miot/appsceneservice/AppSceneService/GetSceneInfo",
             {"app_version": 12, "scene_id": scene_id, "home_id": str(home_id), "owner_uid": owner}),
            ("/appgateway/miot/appsceneservice/AppSceneService/LoadScene",
             {"app_version": 12, "scene_id": scene_id, "home_id": str(home_id), "owner_uid": owner}),
            ("/appgateway/miot/appsceneservice/AppSceneService/GetManualScene",
             {"app_version": 12, "scene_id": scene_id, "home_id": str(home_id), "owner_uid": owner}),
        ]
        for uri2, data2 in candidates:
            print(f"\n-- {uri2.rsplit('/', 1)[-1]} --")
            try_call(api, uri2, data2, uri2.rsplit('/', 1)[-1])

    print("\n完成。重点看：哪个候选返回里出现 action / action_list / 动作数组。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
