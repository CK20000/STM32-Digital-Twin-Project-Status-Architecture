@echo off
title Digital Twin - Unified UI Launcher
cd /d "C:\Users\user\Desktop\automation\kicad_plugin"

echo ================================================
echo   STM32 Digital Twin - Unified UI
echo ================================================
echo.

start "" pythonw dashboard_unified.py
ping -n 4 127.0.0.1 > nul
exit