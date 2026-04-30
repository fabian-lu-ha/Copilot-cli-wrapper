@echo off
REM Tiny dispatcher. All real install logic lives in scripts\install.py so we
REM don't keep tripping over cmd.exe's parser quirks (multi-line if blocks,
REM goto inside parens, for /f reading files with spaces in %TEMP%, chcp
REM 65001 weirdness, etc.). Anything broken here gets fixed in Python.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python is not on PATH. Install Python 3.9+ first.
    pause
    exit /b 1
)

python "%~dp0install.py" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
