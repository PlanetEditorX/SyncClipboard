@echo off
setlocal
cd /d "%~dp0"
if exist .idea rmdir /s /q .idea
if exist .gradle rmdir /s /q .gradle
if exist build rmdir /s /q build
if exist app\build rmdir /s /q app\build
echo Project caches removed.
echo Reopen this folder in Android Studio and select JDK 17.
pause
