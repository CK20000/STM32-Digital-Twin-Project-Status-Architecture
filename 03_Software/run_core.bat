:: ============================================================
:: run_core.bat — kept for backward compatibility
:: NOTE: dashboard_unified.py now runs this pipeline internally.
:: Use run_dashboard.bat for the unified experience.
:: ============================================================
@echo off
title Digital Twin - Core Pipeline (standalone)
cd /d "C:\Users\user\Desktop\automation\kicad_plugin"

echo ================================================
echo   STM32 Digital Twin - Core Pipeline (standalone)
echo ================================================
echo.

echo [PRE-FLIGHT 1/4] Classifying components...
python netlist_analyzer.py
if errorlevel 1 goto :fail

echo.
echo [PRE-FLIGHT 2/4] Compiling SPICE netlist...
python spice_compiler.py
if errorlevel 1 goto :fail

echo.
echo [PRE-FLIGHT 3/4] Injecting trace resistance...
python inject_trace_resistance.py
if errorlevel 1 goto :fail

echo.
echo [PRE-FLIGHT 4/4] Extracting firmware observables...
python get_symbols.py
if errorlevel 1 goto :fail

echo.
echo ================================================
echo   Pre-flight complete. Starting services...
echo ================================================
echo.

echo [1/5] Starting Renode (virtual STM32)...
start "Renode - Virtual MCU" cmd /k "renode --port 1234 machine.resc"
timeout /t 10 /nobreak > nul

echo [2/5] Starting Python Bridge (UART -^> ZMQ)...
start "Bridge - UART to ZMQ" cmd /k "python stm32_bridge.py"
timeout /t 3 /nobreak > nul

echo [3/5] Starting Renode RAM Reader...
start "Reader - Renode RAM" cmd /k "python renode_reader.py"
timeout /t 3 /nobreak > nul

echo [4/5] Starting SPICE Solver (ngspice)...
start "Solver - ngspice" cmd /k "python pyspice_solver.py"
timeout /t 2 /nobreak > nul

echo [5/5] Starting Subscriber...
start "Subscriber - Live Data" cmd /k "python test_sub.py"

echo.
echo ================================================
echo   Core pipeline running (5 windows).
echo   Viewers launch separately.
echo ================================================
timeout /t 5 /nobreak > nul
exit

:fail
echo.
echo [!] Pre-flight step failed. Aborting.
pause
exit /b 1