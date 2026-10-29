#!/usr/bin/env bash
# MiHome-Windows 打包脚本（PyInstaller 版）
#
# 相比 Nuitka：不编译 C，构建 76 秒左右（Nuitka 关掉 ccache 后要 9~13 分钟）。
# 产物是目录版（onedir）：dist/MiHome-Windows.exe + dist/_internal/。
# 单文件（--onefile）每次启动都要往 %TEMP% 解压 90MB，强杀还会残留目录，不用。
#
# 用法：
#   bash build_pyi.sh          # 增量构建（快）
#   CLEAN=1 bash build_pyi.sh  # 丢弃 PyInstaller 缓存后重建
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# PyInstaller 是原生 Windows 程序，不认 MSYS 的 /d/... 路径，统一转成 D:/...
ROOT_WIN="$(cd "$ROOT" && pwd -W)"
PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || { echo "[ERROR] 未找到虚拟环境: $PY"; exit 1; }

CACHE_DIR="$ROOT/.pyi-cache"
ARCHIVE="$CACHE_DIR/build-archive"
BUILD_DIR="$CACHE_DIR/build"
STAGE="$ROOT/.pyi-staged"
STAMP="$CACHE_DIR/.build-started"
mkdir -p "$ARCHIVE" "$BUILD_DIR"

# 旧产物**只挪不删**：在受限环境里删除上千个文件会被安全机制拦下并整目录
# 转存进 modify_backup（一次能攒出几个 GB），同卷改名没有这个副作用。
archive_dist() {
    [ -d "$1" ] || return 0
    mv "$1" "$ARCHIVE/$(basename "$1")-$(date +%s)" || {
        echo "[ERROR] 无法移走 $1（文件被占用？程序还在运行？）"
        exit 1
    }
}

echo "[1/3] 检查依赖..."
"$PY" -c "import PyInstaller, PySide6, qtawesome" 2>/dev/null || {
    echo "[ERROR] 缺少打包依赖，先执行："
    echo "        .venv/Scripts/python.exe -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple"
    exit 1
}

# 程序还在跑就打包会失败在最后的替换步骤，提前退出更省时间
if tasklist //FI "IMAGENAME eq MiHome-Windows.exe" 2>/dev/null | grep -qi "MiHome-Windows.exe"; then
    echo "[ERROR] MiHome-Windows.exe 正在运行，请先退出（含托盘图标）再打包"
    exit 1
fi

echo "[2/3] 开始打包..."
BUILD_T0=$(date +%s)
touch "$STAMP"
[ -d "$STAGE" ] && mv "$STAGE" "$ARCHIVE/staged-$(date +%s)"

CLEAN_ARG=""
[ "${CLEAN:-0}" = "1" ] && CLEAN_ARG="--clean"

"$PY" -m PyInstaller \
    --noconfirm $CLEAN_ARG \
    --windowed \
    --onedir \
    --name MiHome-Windows \
    --icon "$ROOT_WIN/app/ui/icon.ico" \
    --add-data "$ROOT_WIN/app/ui/icon.png;app/ui" \
    --add-data "$ROOT_WIN/app/ui/tray_icon.png;app/ui" \
    --add-data "$ROOT_WIN/app/ui/tray_icon_light.png;app/ui" \
    --add-data "$ROOT_WIN/.venv/Lib/site-packages/PySide6/translations;PySide6/translations" \
    --collect-data qtawesome \
    --exclude-module tkinter \
    --exclude-module unittest \
    --exclude-module pytest \
    --distpath "$ROOT_WIN/.pyi-staged" \
    --workpath "$ROOT_WIN/.pyi-cache/build" \
    --specpath "$ROOT_WIN/.pyi-cache/build" \
    "$ROOT_WIN/run.py" || { echo "[ERROR] PyInstaller 打包失败"; exit 1; }

echo "[3/3] 替换产物..."
NEW="$STAGE/MiHome-Windows"
[ -f "$NEW/MiHome-Windows.exe" ] || {
    echo "[ERROR] 未生成 MiHome-Windows.exe"
    exit 1
}

# 隐私自检：产物里绝不能出现用户数据。配置/缓存都写在数据目录
# （源码模式=项目根，构建版=%LOCALAPPDATA%），打包不该带进来；
# 这里在替换前兜底检查，一旦命中就当构建失败，避免分发时泄露。
PERSONAL_FILES="auth.json devices_cache.json board.json schedules.json settings.json tray.json workbench.json device_icons.json product_names.json"
LEAKED=""
for name in $PERSONAL_FILES; do
    hit=$(find "$NEW" -name "$name" -type f 2>/dev/null | head -3)
    [ -n "$hit" ] && LEAKED="${LEAKED}${hit}"$'\n'
done
if [ -n "$LEAKED" ]; then
    echo "[ERROR] 产物里发现用户数据文件，已中止（不允许打包分发）："
    printf "%b" "$LEAKED"
    exit 1
fi
echo "     隐私自检通过：产物内未包含登录凭据与设备数据"
# 旧产物在**打包成功之后**才挪走：前面任何一步失败都保留上一版 dist，
# 不能出现"构建失败 + 没有可运行的程序"
[ -d dist ] && archive_dist dist
mv "$NEW" dist
[ -d "$STAGE" ] && mv "$STAGE" "$ARCHIVE/staged-$(date +%s)"

EXE="$ROOT/dist/MiHome-Windows.exe"
SIZE=$(du -sh "$ROOT/dist" | cut -f1)
echo "      编译耗时 $(( $(date +%s) - BUILD_T0 ))s | 产物 $SIZE"
echo "[OK] $EXE"
echo "     归档清理（手动、按需）：rm -rf $ARCHIVE"
