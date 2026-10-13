@echo off
title Digital Twin - Board Health (single check)
cd /d "C:\Users\user\Desktop\automation\kicad_plugin"
python board_health.py --once
echo.
echo Exit code: %ERRORLEVEL%
pause