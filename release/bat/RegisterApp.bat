@echo off
title Register App to Windows Notification Center
setlocal enabledelayedexpansion

:: Get the directory where this batch file is located (should be same as exe)
set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

:: Find the first .exe in that directory
for %%f in ("%APP_DIR%\*.exe") do (
    set "APP_EXE=%%f"
    goto :found
)

:found
if not defined APP_EXE (
    echo Error: No .exe file found in current directory.
    echo Please place this script next to your exe program.
    pause
    exit /b 1
)

:: Fixed AUMID
set "AUMID=PlanetEditorX.SyncClipboard"

:: Generate app name from exe filename (without extension)
for %%i in ("%APP_EXE%") do set "APP_NAME=%%~ni"

:: Icon path (relative to exe directory)
set "ICON_PATH=%APP_DIR%\gui\icon\icon-active.png"

echo ========================================
echo Registering the following application:
echo   Exe Path: %APP_EXE%
echo   App Name: %APP_NAME%
echo   AUMID: %AUMID%
echo   Icon Path: %ICON_PATH%
echo ========================================
echo.

:: Check for administrator rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrator privileges...
    powershell start -verb runas '%~f0'
    exit /b
)

echo Writing to registry...
reg add "HKLM\SOFTWARE\Classes\AppUserModelId\%AUMID%" /f >nul
reg add "HKLM\SOFTWARE\Classes\AppUserModelId\%AUMID%" /v "DisplayName" /t REG_EXPAND_SZ /d "%APP_NAME%" /f >nul

if exist "%ICON_PATH%" (
    reg add "HKLM\SOFTWARE\Classes\AppUserModelId\%AUMID%" /v "IconUri" /t REG_EXPAND_SZ /d "%ICON_PATH%" /f >nul
    echo Icon registered.
) else (
    echo Warning: Icon file not found - %ICON_PATH%
    echo Notification will show no icon.
)

echo Creating Start Menu shortcut...
set "START_MENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "SHORTCUT_PATH=%START_MENU%\%APP_NAME%.lnk"
powershell -Command "$WS = New-Object -ComObject WScript.Shell; $SC = $WS.CreateShortcut('%SHORTCUT_PATH%'); $SC.TargetPath = '%APP_EXE%'; $SC.Save()"

if exist "%SHORTCUT_PATH%" (
    echo Shortcut created: %SHORTCUT_PATH%
) else (
    echo Warning: Failed to create shortcut, check permissions.
)

echo ========================================
echo Registration complete!
echo Please reboot or log off and back on.
echo Then go to: Settings ^> System ^> Notifications ^> Advanced settings
echo You should see "%APP_NAME%" in the list.
echo ========================================
pause