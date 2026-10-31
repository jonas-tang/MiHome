@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  MiHome-Windows 打包（PyInstaller，目录版）
echo ============================================
".venv\Scripts\python.exe" "tools\build_pyi.py" %*
if errorlevel 1 (
    echo.
    echo [失败] 打包未完成，请看上面的错误信息
) else (
    echo.
    echo [成功] 产物：dist\MiHome-Windows.exe
)
echo.
pause
