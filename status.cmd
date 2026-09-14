@echo off
setlocal
cd /d "%~dp0"
echo ==== CodexContinue process ====
tasklist /FI "IMAGENAME eq CodexContinue.exe"
echo.
echo ==== Recent log ====
if exist "%~dp0logs\watchdog.log" (
  powershell -NoProfile -Command "Get-Content -LiteralPath '%~dp0logs\watchdog.log' -Tail 12 -Encoding UTF8"
) else (
  echo No log yet.
)
endlocal
