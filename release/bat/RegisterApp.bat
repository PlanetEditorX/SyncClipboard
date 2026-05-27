@echo off
title 自动注册应用到 Windows 通知中心
setlocal enabledelayedexpansion

:: 获取脚本所在目录（即 exe 所在目录）
set "APP_DIR=%~dp0"
:: 去除末尾反斜杠
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

:: 查找该目录下的第一个 .exe 文件（通常只有你的主程序）
for %%f in ("%APP_DIR%\*.exe") do (
    set "APP_EXE=%%f"
    goto :found
)

:found
if not defined APP_EXE (
    echo 错误：在当前目录下未找到任何 .exe 文件。
    echo 请将此脚本放在你的 exe 程序旁边运行。
    pause
    exit /b 1
)

:: 设定固定 AUMID
set "AUMID=PlanetEditorX.SyncClipboard"

:: 自动生成应用名称（取 exe 文件名，不带扩展名）
for %%i in ("%APP_EXE%") do set "APP_NAME=%%~ni"

:: 自动生成图标路径（exe 所在目录\icon\icon-active.png）
set "ICON_PATH=%APP_DIR%\gui\icon\icon-active.png"

echo ========================================
echo 即将注册以下应用：
echo   程序路径: %APP_EXE%
echo   应用名称: %APP_NAME%
echo   AUMID: %AUMID%
echo   图标路径: %ICON_PATH%
echo ========================================
echo.

:: 检查是否以管理员权限运行
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo 正在请求管理员权限...
    powershell start -verb runas '%~f0'
    exit /b
)

echo 正在写入注册表...
:: 创建 AUMID 注册表项
reg add "HKLM\SOFTWARE\Classes\AppUserModelId\%AUMID%" /f >nul
reg add "HKLM\SOFTWARE\Classes\AppUserModelId\%AUMID%" /v "DisplayName" /t REG_EXPAND_SZ /d "%APP_NAME%" /f >nul

if exist "%ICON_PATH%" (
    reg add "HKLM\SOFTWARE\Classes\AppUserModelId\%AUMID%" /v "IconUri" /t REG_EXPAND_SZ /d "%ICON_PATH%" /f >nul
    echo 图标已注册。
) else (
    echo 警告：图标文件不存在 - %ICON_PATH%
    echo 将继续注册，但通知不会显示图标。
)

echo 正在创建开始菜单快捷方式...
set "START_MENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "SHORTCUT_PATH=%START_MENU%\%APP_NAME%.lnk"
powershell -Command "$WS = New-Object -ComObject WScript.Shell; $SC = $WS.CreateShortcut('%SHORTCUT_PATH%'); $SC.TargetPath = '%APP_EXE%'; $SC.Save()"

if exist "%SHORTCUT_PATH%" (
    echo 快捷方式已创建：%SHORTCUT_PATH%
) else (
    echo 警告：快捷方式创建失败，请检查权限。
)

echo ========================================
echo 注册完成！
echo 请重启电脑，或注销后重新登录。
echo 重启后，进入：设置 -> 系统 -> 通知与操作
echo 你应该能看到 "%APP_NAME%" 出现在应用列表中。
echo 点击它可以单独关闭通知声音。
echo ========================================
pause