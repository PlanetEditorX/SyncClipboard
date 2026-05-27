@echo off
title 从 Windows 通知中心移除应用注册
setlocal enabledelayedexpansion

chcp 65001 >nul 2>&1

set "AUMID=PlanetEditorX.SyncClipboard"
set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

:: 查找 exe 以确定快捷方式名称
set "APP_EXE="
for %%f in ("%APP_DIR%\*.exe") do (
    set "APP_EXE=%%f"
    goto :found
)
:found

if defined APP_EXE (
    for %%i in ("%APP_EXE%") do set "APP_NAME=%%~ni"
) else (
    set "APP_NAME=SyncClipboard"
    echo [警告] 未找到 exe，将尝试删除默认名称快捷方式。
)

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo 正在请求管理员权限...
    powershell start -verb runas '%~f0'
    exit /b
)

echo ========================================
echo 正在卸载注册项...
echo ========================================

reg delete "HKLM\SOFTWARE\Classes\AppUserModelId\%AUMID%" /f >nul 2>&1
if %errorLevel% equ 0 (
    echo [成功] 已删除注册表项: HKLM\SOFTWARE\Classes\AppUserModelId\%AUMID%
) else (
    echo [信息] 未找到注册表项或已删除。
)

set "START_MENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "SHORTCUT_PATH=%START_MENU%\%APP_NAME%.lnk"

if exist "%SHORTCUT_PATH%" (
    del /f "%SHORTCUT_PATH%" >nul 2>&1
    echo [成功] 已删除快捷方式: %SHORTCUT_PATH%
) else (
    echo [信息] 未找到快捷方式: %SHORTCUT_PATH%
    :: 尝试删除常见备选名称
    if exist "%START_MENU%\SyncClipboard.lnk" (
        del /f "%START_MENU%\SyncClipboard.lnk" >nul 2>&1
        echo [成功] 已删除快捷方式: %START_MENU%\SyncClipboard.lnk
    )
    if exist "%START_MENU%\PlanetEditorX.SyncClipboard.lnk" (
        del /f "%START_MENU%\PlanetEditorX.SyncClipboard.lnk" >nul 2>&1
        echo [成功] 已删除快捷方式: %START_MENU%\PlanetEditorX.SyncClipboard.lnk
    )
)

echo ========================================
echo 卸载完成！建议重启电脑。
echo ========================================
pause