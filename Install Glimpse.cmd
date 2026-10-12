@echo off
rem Double-click to install or update Glimpse. No administrator rights needed.
title Install Glimpse
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\install.ps1" %*
if errorlevel 1 (
  echo.
  echo Something went wrong. The messages above say what; you can run this again after fixing it.
)
echo.
pause
