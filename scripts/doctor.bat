@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
echo.
echo ==============================================
echo   Copilot CLI Doctor
echo ==============================================
echo.

set "FAILS=0"
set "WARNS=0"
set "FIXES="

REM ---- 1. Python --------------------------------------------------------
echo [1/8] Python interpreter...
where python >nul 2>&1
if errorlevel 1 (
    echo   FAIL  python is not on PATH
    set /a FAILS+=1
    set "FIXES=!FIXES! - install Python 3.9+ from https://www.python.org/downloads/ ^(check 'Add Python to PATH'^)\n"
    goto :skip_pip
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYV=%%v"
echo   OK    python !PYV!
for /f "delims=" %%p in ('where python') do (
    echo         %%p
    goto :py_done
)
:py_done

REM Verify Python >= 3.9
for /f "tokens=1,2 delims=." %%a in ("!PYV!") do (
    set "PYMAJ=%%a"
    set "PYMIN=%%b"
)
if !PYMAJ! LSS 3 (
    echo   FAIL  Python !PYV! is too old; need 3.9+
    set /a FAILS+=1
)
if !PYMAJ! EQU 3 if !PYMIN! LSS 9 (
    echo   FAIL  Python !PYV! is too old; need 3.9+
    set /a FAILS+=1
)

REM ---- 2. pip -----------------------------------------------------------
echo.
echo [2/8] pip...
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo   FAIL  pip not available
    set /a FAILS+=1
    set "FIXES=!FIXES! - run: python -m ensurepip --upgrade\n"
    goto :skip_ssl
)
for /f "delims=" %%v in ('python -m pip --version 2^>nul') do echo   OK    %%v

REM ---- 3. SSL / corporate proxy ---------------------------------------
echo.
echo [3/8] SSL connectivity to pypi.org...
python -c "import urllib.request,ssl; urllib.request.urlopen('https://pypi.org/simple/pip/', timeout=8).read(64)" >nul 2>&1
if errorlevel 1 (
    echo   WARN  pypi.org TLS verification failed ^(corporate proxy?^)
    set /a WARNS+=1
    set "FIXES=!FIXES! - bootstrap: pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org pip-system-certs\n"
) else (
    echo   OK    pypi.org reachable with TLS verified
)

REM ---- 4. pip-system-certs --------------------------------------------
echo.
echo [4/8] pip-system-certs ^(uses Windows trust store^)...
python -c "import pip_system_certs" >nul 2>&1
if errorlevel 1 (
    echo   WARN  pip-system-certs not installed
    set /a WARNS+=1
    set "FIXES=!FIXES! - pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org pip-system-certs\n"
) else (
    echo   OK    installed
)

:skip_ssl
:skip_pip

REM ---- 5. copilot-cli-wrapper package ---------------------------------
echo.
echo [5/8] copilot-cli-wrapper package...
python -m pip show copilot-cli-wrapper >nul 2>&1
if errorlevel 1 (
    echo   FAIL  package not installed
    set /a FAILS+=1
    set "FIXES=!FIXES! - cd to repo dir and run: pip install -e .\n"
) else (
    for /f "tokens=2" %%v in ('python -m pip show copilot-cli-wrapper 2^>nul ^| findstr /b "Version:"') do echo   OK    version %%v
)

REM ---- 6. copilot script on PATH --------------------------------------
echo.
echo [6/8] copilot script on PATH...
where copilot >nul 2>&1
if errorlevel 1 (
    echo   WARN  'copilot' not on PATH
    set /a WARNS+=1
    REM Find the Scripts dir to suggest adding.
    for /f "delims=" %%d in ('python -c "import sysconfig; print(sysconfig.get_path('scripts'))" 2^>nul') do (
        echo         try adding to PATH: %%d
        set "SCRIPTS_DIR=%%d"
    )
    set "FIXES=!FIXES! - add !SCRIPTS_DIR! to PATH ^(see setup.bat^), or use: python -m copilot_cli\n"
) else (
    for /f "delims=" %%p in ('where copilot') do echo   OK    %%p
)

REM ---- 7. Microsoft Edge ----------------------------------------------
echo.
echo [7/8] Microsoft Edge...
set "EDGE1=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
set "EDGE2=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if exist "%EDGE1%" (
    echo   OK    %EDGE1%
) else if exist "%EDGE2%" (
    echo   OK    %EDGE2%
) else (
    echo   FAIL  Edge not found in standard locations
    set /a FAILS+=1
    set "FIXES=!FIXES! - install Microsoft Edge ^(https://www.microsoft.com/edge^)\n"
)

REM ---- 8. Playwright + msedge driver ----------------------------------
echo.
echo [8/8] Playwright ^+ msedge driver...
python -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo   FAIL  playwright python package not installed
    set /a FAILS+=1
    set "FIXES=!FIXES! - pip install playwright^>=1.45\n"
) else (
    python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(channel='msedge',headless=True); b.close(); p.stop()" >nul 2>&1
    if errorlevel 1 (
        echo   WARN  msedge driver shim not available
        set /a WARNS+=1
        set "FIXES=!FIXES! - playwright install msedge\n"
    ) else (
        echo   OK    msedge channel launches
    )
)

REM ---- summary --------------------------------------------------------
echo.
echo ==============================================
if !FAILS! GTR 0 (
    echo   FAIL: !FAILS!  WARN: !WARNS!
) else if !WARNS! GTR 0 (
    echo   All required checks passed. !WARNS! warning^(s^).
) else (
    echo   All checks passed. You should be able to run 'copilot' or 'copilot-gui'.
)
echo ==============================================

if not "!FIXES!" == "" (
    echo.
    echo Suggested fixes:
    echo !FIXES!
    echo Run setup.bat to apply most of them automatically.
)

endlocal
exit /b 0
