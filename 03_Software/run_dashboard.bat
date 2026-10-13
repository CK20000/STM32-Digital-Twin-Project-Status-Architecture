@echo off
title Digital Twin - Dashboard Launcher
cd /d "C:\Users\user\Desktop\automation\kicad_plugin"

echo ================================================
echo   STM32 Digital Twin - Dashboard
echo ================================================
echo.
echo [1] Starting core pipeline (Renode + solver)...
start "DT - Pipeline" cmd /c run_core.bat

echo.
echo [2] Waiting for SPICE solver to start publishing...
python wait_for_solver.py
if errorlevel 1 (
    echo.
    echo [!] Solver did not come online. Aborting viewer launch.
    pause
    exit /b 1
)

echo.
echo [3] Launching viewers (hidden consoles)...
start "" pythonw pyqt6_control.py
timeout /t 1 /nobreak > nul
start "" pythonw pcb_viewer.py
timeout /t 1 /nobreak > nul
start "" pythonw pcb_current_view.py
timeout /t 1 /nobreak > nul
start "" pythonw pcb_voltage_view.py
timeout /t 1 /nobreak > nul
start "" pythonw pcb_heat_view.py
timeout /t 1 /nobreak > nul
start "" pythonw pcb_thermal_field_v2.py
timeout /t 1 /nobreak > nul
start "" pythonw pcb_resistance_view.py

echo.
echo [4] Waiting for figure windows to appear...
timeout /t 12 /nobreak > nul

echo [5] Tiling windows...
python tile_windows.py

echo.
echo ================================================
echo   Dashboard ready.
echo   Stop everything with stop_all.bat.
echo ================================================
timeout /t 4 /nobreak > nul
exit