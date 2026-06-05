@echo off
title Gmax Studios AI Content Scanner
mode con lines=30 cols=100
cls

:: Automatically maps the sandbox to the current folder, regardless of drive letter
set "CURRENT_DIR=%~dp0"
set "PATH=%CURRENT_DIR%node-v24.16.0-win-x64;%PATH%"

:: Switch context directly to this directory and run the app
cd /d "%CURRENT_DIR%"
node log-video.js

echo.
echo 🎬 Session closed.
pause