@echo off
rem Double-click to install or update Kalends. No administrator rights needed.
title Install Kalends
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\install.ps1" %*
if errorlevel 1 (
  echo.
  echo Something went wrong. The messages above say what; you can run this again after fixing it.
)
echo.
pause
