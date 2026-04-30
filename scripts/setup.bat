@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
echo.
echo ==============================================
echo   Copilot CLI Setup
echo ==============================================
echo.
echo This will:
echo   1. Bootstrap pip-system-certs ^(corporate TLS-proxy fix^)
echo   2. Install copilot-cli-wrapper ^(with [gui] extras^)
echo   3. Install Playwright's msedge driver
echo   4. Offer to add Python's Scripts dir to your user PATH
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python is not on PATH. Install Python 3.9+ first.
    pause
    exit /b 1
)

REM ---- step 1: pip-system-certs --------------------------------------
echo.
echo [1/4] Bootstrapping pip-system-certs ^(once-only TLS workaround^)...
python -c "import pip_system_certs" >nul 2>&1
if errorlevel 1 (
    python -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org pip-system-certs
    if errorlevel 1 (
        echo ERROR: pip-system-certs install failed.
        pause
        exit /b 1
    )
) else (
    echo   already installed
)

REM ---- step 2: install package ---------------------------------------
echo.
echo [2/4] Installing copilot-cli-wrapper...
pushd "%~dp0\.."
python -m pip install -e ".[gui,tokens]"
if errorlevel 1 (
    echo ERROR: pip install failed.
    popd
    pause
    exit /b 1
)
popd

REM ---- step 3: playwright msedge driver ------------------------------
echo.
echo [3/4] Installing Playwright msedge driver...
python -m playwright install msedge
if errorlevel 1 (
    echo WARN: 'playwright install msedge' failed.
    echo       You can retry it manually; the rest of setup will continue.
)

REM ---- step 4: PATH --------------------------------------------------
echo.
echo [4/4] Checking 'copilot' command on PATH...
where copilot >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%p in ('where copilot') do echo   OK on PATH: %%p
    goto :done
)

for /f "delims=" %%d in ('python -c "import sysconfig; print(sysconfig.get_path('scripts'))" 2^>nul') do set "SCRIPTS_DIR=%%d"
if "!SCRIPTS_DIR!" == "" (
    echo   could not detect Python's Scripts dir; skipping PATH update
    goto :done
)

echo.
echo Python's Scripts directory is:
echo   !SCRIPTS_DIR!
echo.
choice /c yn /n /m "Add this directory to your USER PATH? [y/n] "
if errorlevel 2 goto :path_skipped

REM Use PowerShell to safely append without truncating.
powershell -NoProfile -Command ^
    "$p = [Environment]::GetEnvironmentVariable('PATH','User'); ^
     if ($p -notlike '*' + $env:SCRIPTS_DIR + '*') { ^
         [Environment]::SetEnvironmentVariable('PATH', $p + ';' + $env:SCRIPTS_DIR, 'User'); ^
         Write-Host '  added to user PATH (open a new shell to pick it up)' } ^
     else { Write-Host '  already in user PATH' }"
set "SCRIPTS_DIR=!SCRIPTS_DIR!"

goto :done

:path_skipped
echo   skipped. You can run 'python -m copilot_cli' instead.

:done
echo.
echo ==============================================
echo   Setup complete.
echo ==============================================
echo.
echo Try it:
echo   copilot           ^(CLI^)
echo   copilot-gui       ^(graphical UI^)
echo.
echo If 'copilot' isn't found, open a new cmd window first
echo so it picks up the updated PATH.
echo.
endlocal
exit /b 0
