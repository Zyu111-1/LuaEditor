@echo off
chcp 65001 >nul
echo ========================================
echo   Lua Table Editor - 打包为独立 exe
echo ========================================
echo.

echo [1/2] 检查并安装依赖...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo 依赖安装失败，请检查网络连接
    pause
    exit /b 1
)

echo.
echo [2/2] 开始打包...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "LuaTableEditor" ^
    --add-data "requirements.txt;." ^
    --clean ^
    lua_table_editor.py

if %errorlevel% equ 0 (
    echo.
    echo ========================================
    echo   打包成功！
    echo   输出文件: dist\LuaTableEditor.exe
    echo ========================================
) else (
    echo.
    echo 打包失败，请检查错误信息
)

pause
