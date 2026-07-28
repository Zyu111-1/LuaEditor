@echo off
chcp 65001 >nul
echo ========================================
echo   直接运行源码（无需打包，改动即时生效）
echo   如需独立 exe，请运行 build.bat
echo ========================================
echo.
python lua_table_editor.py
if %errorlevel% neq 0 (
    echo.
    echo 运行失败，请确认已安装依赖：pip install -r requirements.txt
    pause
)
