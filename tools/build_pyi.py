# SPDX-License-Identifier: GPL-3.0-or-later
"""PyInstaller 打包（纯 Python 版，不依赖 bash）。

用法：

    .venv\\Scripts\\python.exe tools\\build_pyi.py            # 增量打包
    .venv\\Scripts\\python.exe tools\\build_pyi.py --clean    # 丢缓存重建

行为与 build_pyi.sh 一致：
- onedir 产物：dist/MiHome-Windows.exe + dist/_internal/
- 旧 dist **只挪不删**（归档到 .pyi-cache/build-archive）
- 打包成功后才替换 dist；失败保留上一版
- 隐私自检：产物里出现个人数据文件即失败
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".pyi-cache"
BUILD_DIR = CACHE / "build"
ARCHIVE = CACHE / "build-archive"
STAGE = ROOT / ".pyi-staged"
DIST = ROOT / "dist"
EXE_NAME = "MiHome-Windows"

PERSONAL_FILES = (
    "auth.json", "devices_cache.json", "board.json", "schedules.json",
    "settings.json", "tray.json", "workbench.json", "device_icons.json",
    "product_names.json",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def archive_dist() -> None:
    """旧产物只挪不删：受限环境下删除上千个文件会被安全层拦下并整目录转存。"""
    if not DIST.exists():
        return
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    target = ARCHIVE / f"dist-{int(time.time())}"
    shutil.move(str(DIST), str(target))
    log(f"      旧产物已归档: {target.name}")


def find_personal_files(root: Path) -> list[Path]:
    hits: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.name in PERSONAL_FILES:
            hits.append(path)
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 MiHome-Windows")
    parser.add_argument("--clean", action="store_true", help="丢弃 PyInstaller 缓存后重建")
    args = parser.parse_args()

    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("[ERROR] 缺少 PyInstaller，先执行：")
        log("        .venv\\Scripts\\python.exe -m pip install pyinstaller "
            "-i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1

    # 程序还在跑时替换 dist 会失败，提前退出更省时间
    running = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}.exe"],
        capture_output=True, text=True, errors="ignore")
    if EXE_NAME in running.stdout:
        log(f"[ERROR] {EXE_NAME}.exe 正在运行，请先退出（含托盘图标）再打包")
        return 1

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    if STAGE.exists():
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        shutil.move(str(STAGE), str(ARCHIVE / f"staged-{int(time.time())}"))

    cmd = [
        str(python), "-m", "PyInstaller",
        "--noconfirm",
        "--windowed", "--onedir",
        "--name", EXE_NAME,
        "--icon", str(ROOT / "app" / "ui" / "icon.ico"),
        "--add-data", f"{ROOT / 'app' / 'ui' / 'icon.png'}{os.pathsep}app/ui",
        "--add-data", f"{ROOT / 'app' / 'ui' / 'tray_icon.png'}{os.pathsep}app/ui",
        "--add-data", f"{ROOT / 'app' / 'ui' / 'tray_icon_light.png'}{os.pathsep}app/ui",
        "--add-data",
        f"{ROOT / '.venv' / 'Lib' / 'site-packages' / 'PySide6' / 'translations'}"
        f"{os.pathsep}PySide6/translations",
        "--collect-data", "qtawesome",
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pytest",
        "--distpath", str(STAGE),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        str(ROOT / "run.py"),
    ]
    if args.clean:
        cmd.insert(3, "--clean")

    log("[1/3] 开始打包（首次几分钟，之后约 1 分钟）...")
    started = time.time()
    result = subprocess.run(cmd)
    if result.returncode != 0:
        log("[ERROR] PyInstaller 打包失败")
        return result.returncode

    new = STAGE / EXE_NAME
    if not (new / f"{EXE_NAME}.exe").exists():
        log("[ERROR] 未生成可执行文件")
        return 1

    leaked = find_personal_files(new)
    if leaked:
        log("[ERROR] 产物里发现用户数据文件，已中止（不允许打包分发）：")
        for path in leaked[:8]:
            log(f"        {path.relative_to(new)}")
        return 1
    log("      隐私自检通过：产物内未包含登录凭据与设备数据")

    log("[2/3] 替换产物...")
    archive_dist()
    shutil.move(str(new), str(DIST))
    if STAGE.exists():
        shutil.move(str(STAGE), str(ARCHIVE / f"staged-{int(time.time())}"))

    size_mb = sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file()) / 1024 / 1024
    log(f"[3/3] 完成：{DIST / (EXE_NAME + '.exe')}")
    log(f"      耗时 {time.time() - started:.0f}s | 产物 {size_mb:.0f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
