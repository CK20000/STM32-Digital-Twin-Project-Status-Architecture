:: ============================================================
:: stop_all.bat — kill every Digital Twin process
:: (renode, ngspice, viewer/cmd windows from run_core, dashboards)
:: ============================================================
@echo off
title Digital Twin - Stop All
echo Stopping all Digital Twin processes...

echo [1/6] Renode...
taskkill /F /IM renode.exe > nul 2>&1

echo [2/6] ngspice...
taskkill /F /IM ngspice_con.exe > nul 2>&1

echo [3/6] pythonw (dashboard + embedded viewers)...
taskkill /F /IM pythonw.exe > nul 2>&1

echo [4/6] cmd windows whose title starts with "DT -" or "Renode" or "Bridge"...
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq cmd.exe" /FO CSV /NH 2^>nul') do (
    for /f "tokens=*" %%T in ('tasklist /FI "PID eq %%~P" /FO LIST /NH 2^>nul ^| findstr /I "Window Title"') do (
        echo %%~T | findstr /I "DT - Renode Digital Twin" > nul && taskkill /F /PID %%~P > nul 2>&1
    )
)

echo [5/6] python.exe started from the project folder...
for /f "tokens=2" %%i in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST ^| findstr "PID:"') do (
    wmic process where "ProcessId=%%i" get CommandLine 2>nul | findstr /I "kicad_plugin" >nul
    if not errorlevel 1 (
        taskkill /F /PID %%i > nul 2>&1
    )
)

echo [6/6] leftover run_core console windows...
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq cmd.exe" /FO CSV /NH 2^>nul') do (
    for /f "tokens=*" %%T in ('tasklist /FI "PID eq %%~P" /FO LIST /NH 2^>nul ^| findstr /I "Window Title"') do (
        echo %%~T | findstr /I "Subscriber Bridge Reader Solver" > nul && taskkill /F /PID %%~P > nul 2>&1
    )
)

echo.
echo Done. All Digital Twin processes stopped.
timeout /t 2 > nul
exit