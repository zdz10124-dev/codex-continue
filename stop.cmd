@echo off
taskkill /IM CodexContinue.exe /F >nul 2>nul
if errorlevel 1 (
  echo CodexContinue is not running.
) else (
  echo CodexContinue stopped.
)
