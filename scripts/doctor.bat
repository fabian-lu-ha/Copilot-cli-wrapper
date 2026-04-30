@echo off
REM Tiny dispatcher. All real diagnostic logic lives in scripts\doctor.py
REM so we don't keep tripping over cmd.exe parser quirks (e.g. `for /f`
REM reading a file path that contains a space, which silently exits the
REM whole script on some Windows setups).

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python is not on PATH. Install Python 3.9+ first.
    pause
    exit /b 1
)

python "%~dp0doctor.py" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
