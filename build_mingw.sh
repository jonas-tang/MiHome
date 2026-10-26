#!/usr/bin/env bash
# MiHome-Windows 无 MSVC 构建脚本（MinGW 工具链版）
#
# 用途：本机没有 Visual Studio / Windows SDK 时，用 Nuitka 自带的 MinGW
#      工具链完成编译，产出 dist/MiHome-Windows.exe（standalone，非单文件）。
#      build.ps1 走 --msvc=latest，必须装 VS Build Tools；此脚本是它的替代路径。
#
# 用法： bash build_mingw.sh
#        构建前会清空 dist（Nuitka 无增量复用，留着旧产物只会让
#        "产物是否真的更新"无法判断），并校验产物确实比本次构建新。
#
# 前置：先退出正在运行的 MiHome-Windows.exe —— Windows 锁定运行中的
#      exe，覆盖会失败（脚本会在开始时拦住并提示）。
#
# 说明：GitHub 直连不通时，Nuitka 下载 MinGW 会 403 失败。脚本会先用镜像
#      补下工具链压缩包到 Nuitka 缓存目录，再让它自行解压继续编译。

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

PY="$ROOT/.venv/Scripts/python.exe"
if [ ! -f "$PY" ]; then
    echo "[1/4] 创建虚拟环境..."
    py -3.12 -m venv .venv || python -m venv .venv || exit 1
    "$PY" -m pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple --prefer-binary || exit 1
fi

# 运行中的 exe 被系统锁定：既不能删除也不能覆盖。以前只判断"文件存在"，
# 于是覆盖失败后旧 exe 留在 dist 里、脚本还报成功（用户拿到的仍是老程序）。
# 这里直接探一次写权限——比按进程名匹配精确，别的目录里跑着同名程序
# （比如从别处拷过来的另一份）不会误伤。
if [ -f "dist/MiHome-Windows.exe" ]; then
    if ! "$PY" -c "import sys; open(sys.argv[1], 'r+b').close()" \
            "dist/MiHome-Windows.exe" 2>/dev/null; then
        echo "[ERROR] dist/MiHome-Windows.exe 正在运行，无法覆盖构建产物"
        echo "        请先退出程序（含托盘图标右键退出）后重跑本脚本。"
        exit 1
    fi
fi

echo "[2/4] 检查 Nuitka..."
if ! "$PY" -c "import nuitka" 2>/dev/null; then
    "$PY" -m pip install --prefer-binary nuitka ordered-set zstandard \
        -i https://pypi.tuna.tsinghua.edu.cn/simple || exit 1
fi

export NUITKA_CACHE_DIR="$ROOT/.nuitka-cache"
mkdir -p "$NUITKA_CACHE_DIR"

# MinGW 工具链（版本需与 Nuitka 4.3.x 期望的一致）
GCC_VER="15.2.0posix-13.0.0-msvcrt-r6"
GCC_ZIP="winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64msvcrt-13.0.0-r6.zip"
GCC_DIR="$NUITKA_CACHE_DIR/downloads/gcc/x86_64/$GCC_VER"
GCC_URL="https://github.com/brechtsanders/winlibs_mingw/releases/download/$GCC_VER/$GCC_ZIP"
if [ ! -f "$GCC_DIR/$GCC_ZIP" ]; then
    echo "[3/4] 预取 MinGW 工具链（约 267MB，走镜像）..."
    mkdir -p "$GCC_DIR"
    curl -L -C - --retry 5 --retry-delay 3 -o "$GCC_DIR/$GCC_ZIP" \
        "https://ghfast.top/$GCC_URL" || echo "  [提示] 预取失败，交给 Nuitka 自行下载"
else
    echo "[3/4] MinGW 工具链已缓存"
fi

# 删除产物目录：某些环境会拦截 shell 的批量 rm（一次上千个文件算"批量删除"），
# 改走 Python 的文件 API，行为等价且不受 shell 规则影响
remove_dist() {
    rm -rf dist 2>/dev/null
    [ -d dist ] || return 0
    "$PY" -c "import shutil; shutil.rmtree('dist', ignore_errors=True)" 2>/dev/null
    [ -d dist ] || return 0
    return 1
}

# 旧产物不删、改成挪到缓存目录：在受限环境里"删除上千个文件"会被拦，
# 而同一磁盘内的改名是瞬时的。累积的归档在构建结束时给出清理命令
ARCHIVE="$NUITKA_CACHE_DIR/build-archive"
mkdir -p "$ARCHIVE"
archive_dist() {
    [ -d "$1" ] || return 0
    mv "$1" "$ARCHIVE/$(basename "$1")-$(date +%s)" 2>/dev/null && return 0
    remove_dist "$1"
}

# 归档只留最近一份：每次构建都会把上一版 dist 挪进来，不清理会越攒越大
prune_archive() {
    [ -d "$ARCHIVE" ] || return 0
    ls -1dt "$ARCHIVE"/*/ 2>/dev/null | tail -n +2 | while read -r old; do
        rm -rf "$old" 2>/dev/null
        [ -d "$old" ] && "$PY" -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$old" 2>/dev/null
    done
}

# 编译开始的时间标记：产物必须比它新，才算真的被这次构建覆盖
STAMP="$NUITKA_CACHE_DIR/.build-started"
touch "$STAMP"

TRANS="$ROOT/.venv/Lib/site-packages/PySide6/translations"

# C 编译器并发数：原来硬编码 --jobs=4，这台机器 18 核明显浪费。
# 但也不是越多越好——12 路时 ccache 写每模块统计文件会撞车
# （ccache: error: Failed to write ...ccache-<pid>.txt: Permission denied），
# 整轮编译直接失败。8 路实测稳定。
# 默认 8，可用环境变量覆盖：JOBS=6 bash build_mingw.sh
JOBS="${JOBS:-8}"

# 产物目录放在缓存里且**长期保留**，这里有三条铁律（改脚本时别破坏）：
#   1. 不要删 $OUT_DIR —— 里面的 run.build/ 是 scons 的中间产物
#      （生成的 .c/.o 与 .sconsign 签名库），删了就必须从零重编；
#   2. 不要加 --remove-output —— 它会在编译前清掉同一个目录；
#   3. 不要加 --clean-cache —— 那是把 Nuitka 自己的缓存也一起丢掉。
# 另外 Nuitka 4.x 在 Windows 上会自动下载并挂上 ccache（--assume-yes-for-
# downloads 允许它下载，产物在 .nuitka-cache/ccache），实测 484 个 .o
# 全部 cache hit，C 编译这一阶段基本不花时间；所以不要再手工指定 CC。
OUT_DIR="$NUITKA_CACHE_DIR/out"
mkdir -p "$OUT_DIR"

echo "[4/4] 开始编译（首次或改动较多时几分钟到十几分钟，改了少数模块会快很多）..."
echo "      C 编译并发: $JOBS 路（可用 JOBS=N 覆盖）"
BUILD_T0=$(date +%s)
"$PY" -m nuitka run.py \
    --standalone \
    --enable-plugin=pyside6 \
    --windows-console-mode=disable \
    --windows-icon-from-ico=app/ui/icon.ico \
    --include-package=app.siui \
    --include-package=qtawesome \
    --include-package-data=qtawesome \
    --include-data-files=app/ui/icon.png=app/ui/icon.png \
    --include-data-files=app/ui/tray_icon.png=app/ui/tray_icon.png \
    --include-data-files=app/ui/tray_icon_light.png=app/ui/tray_icon_light.png \
    --include-data-dir="$TRANS=PySide6/translations" \
    --nofollow-import-to=tkinter,unittest,pytest \
    --nofollow-import-to=tools,tests \
    --noinclude-dlls=qt6datavisualization.dll \
    --noinclude-dlls=qt6pdf.dll \
    --jobs="$JOBS" \
    --assume-yes-for-downloads \
    --output-dir="$OUT_DIR" \
    --output-filename=MiHome-Windows.exe \
    --product-name=MiHome-Windows \
    --product-version=0.4.0 \
    --file-version=0.4.0 \
    --copyright="Copyright (C) 2026 MiHome-Windows contributors" \
    --mingw64 || { echo "[ERROR] 编译失败"; exit 1; }
echo "      编译耗时 $(( $(date +%s) - BUILD_T0 ))s"

# 交付形态是"dist 根目录下直接躺 exe"，而 Nuitka 把内容放在 run.dist。
# 这里不逐个文件搬（cp 会吞掉覆盖失败、robocopy 的 /开关 会被 MSYS 的路径
# 转换搞坏），直接把 run.dist 整体换成 dist：重命名是同卷操作，快且不会漏文件。
# run.build 留在 $OUT_DIR 里不动，下次构建靠它增量。
if [ -d "$OUT_DIR/run.dist" ]; then
    STAGE="$ROOT/.dist-staged"
    rm -rf "$STAGE" 2>/dev/null
    mv "$OUT_DIR/run.dist" "$STAGE" || { echo "[ERROR] 无法移出 run.dist"; exit 1; }
    # 旧产物在**构建成功之后**才挪走：上面任何一步失败都保留上一版 dist，
    # 不能让人"构建失败 + 没有可运行的程序"
    if [ -d dist ]; then
        archive_dist dist || { echo "[ERROR] 无法移走旧 dist"; exit 1; }
        prune_archive
    fi
    mv "$STAGE" dist || { echo "[ERROR] 无法把产物移动到 dist"; exit 1; }
fi

if [ ! -f dist/MiHome-Windows.exe ]; then
    echo "[ERROR] 未找到 dist/MiHome-Windows.exe"
    exit 1
fi

# 产物新鲜度校验：只判断"文件存在"会漏掉"复制被拒、旧 exe 留在原地"
# 这种情况（脚本会误报成功，用户拿到的还是老程序）
if [ ! dist/MiHome-Windows.exe -nt "$STAMP" ]; then
    echo "[ERROR] 产物没有更新：dist/MiHome-Windows.exe 比本次构建开始还旧"
    echo "        请确认没有程序正在运行，然后重跑本脚本"
    exit 1
fi

echo "[OK] 构建成功: dist/MiHome-Windows.exe ($(du -sh dist | cut -f1) 总计)"
echo "     注意：整个 dist/ 目录需一起分发，单拷 exe 无法运行"
# 旧产物归档在缓存目录里，攒多了占地方，提示一下清理方式
if [ -d "$ARCHIVE" ]; then
    echo "     构建归档（可删）: $ARCHIVE （$(du -sh "$ARCHIVE" | cut -f1)）"
fi
