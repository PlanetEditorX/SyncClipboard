@echo off
title Remove App Registration from Windows Notification Center
setlocal enabledelayedexpansion

set "AUMID=PlanetEditorX.SyncClipboard"
set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

:: Find the exe to determine shortcut name
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
    echo [Warning] No exe found, will try to remove default name shortcuts.
)

:: Check for administrator rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrator privileges...
    powershell start -verb runas '%~f0'
    exit /b
)

echo ========================================
echo Uninstalling registration entries...
echo ========================================

reg delete "HKLM\SOFTWARE\Classes\AppUserModelId\%AUMID%" /f >nul 2>&1
if %errorLevel% equ 0 (
    echo [Success] Removed registry key: HKLM\SOFTWARE\Classes\AppUserModelId\%AUMID%
) else (
    echo [Info] Registry key not found or already removed.
)

set "START_MENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "SHORTCUT_PATH=%START_MENU%\%APP_NAME%.lnk"

if exist "%SHORTCUT_PATH%" (
    del /f "%SHORTCUT_PATH%" >nul 2>&1
    echo [Success] Deleted shortcut: %SHORTCUT_PATH%
) else (
    echo [Info] Shortcut not found: %SHORTCUT_PATH%
    :: Try fallback common names
    if exist "%START_MENU%\SyncClipboard.lnk" (
        del /f "%START_MENU%\SyncClipboard.lnk" >nul 2>&1
        echo [Success] Deleted shortcut: %START_MENU%\SyncClipboard.lnk
    )
    if exist "%START_MENU%\PlanetEditorX.SyncClipboard.lnk" (
        del /f "%START_MENU%\PlanetEditorX.SyncClipboard.lnk" >nul 2>&1
        echo [Success] Deleted shortcut: %START_MENU%\PlanetEditorX.SyncClipboard.lnk
    )
)

echo ========================================
echo Uninstall complete. A reboot is recommended.
echo ========================================
pause