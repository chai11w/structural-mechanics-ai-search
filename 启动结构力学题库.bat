@echo off
setlocal

set "PROJECT_DIR="
for /d %%I in ("F:\cc\7-题库检索") do (
  if exist "%%~fI\scripts\start_tiku_bot.ps1" set "PROJECT_DIR=%%~fI"
)

if not defined PROJECT_DIR (
    echo Could not find tiku project under F:\cc\7-题库检索
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\start_tiku_bot.ps1"
endlocal
