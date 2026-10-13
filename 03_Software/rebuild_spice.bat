@echo off
title Digital Twin - Rebuild SPICE
cd /d "C:\Users\user\Desktop\automation\kicad_plugin"

echo [1/3] Classifying components...
python netlist_analyzer.py
if errorlevel 1 goto :fail

echo.
echo [2/3] Compiling SPICE netlist...
python spice_compiler.py
if errorlevel 1 goto :fail

echo.
echo [3/3] Injecting trace resistance...
python inject_trace_resistance.py
if errorlevel 1 goto :fail

echo.
echo ================================================
echo   SPICE rebuild complete.
echo   Restart pyspice_solver.py to pick up changes.
echo ================================================
pause
exit

:fail
echo.
echo [!] Step failed. See error above.
pause
exit /b 1