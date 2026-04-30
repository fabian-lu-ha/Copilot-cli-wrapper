@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
echo.
echo ==============================================
echo   Copilot CLI Setup
echo ==============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python is not on PATH. Install Python 3.9+ first.
    pause
    exit /b 1
)

REM ---- detect tenant pipinstall wrapper ------------------------------
REM If the user has dropped a tenant-specific wrapper into this folder
REM (e.g. one that pre-configures the corporate proxy / CA bundle / index),
REM use it for every pip install. Otherwise fall back to the public-PyPI
REM flow with the pip-system-certs bootstrap. The wrapper is gitignored;
REM it is expected to be a drop-in replacement for 'pip install ...' args.
set "PIPINSTALL="
if exist "%~dp0pipinstall.bat" set "PIPINSTALL=%~dp0pipinstall.bat"
if exist "%~dp0pipinstall.cmd" set "PIPINSTALL=%~dp0pipinstall.cmd"
if exist "%~dp0pipinstall.exe" set "PIPINSTALL=%~dp0pipinstall.exe"

if defined PIPINSTALL (
    echo Using tenant pipinstall wrapper:
    echo   !PIPINSTALL!
    echo This will:
    echo   1. Install copilot-cli-wrapper ^(with [gui] extras^)
    echo   2. Install Playwright's msedge driver
    echo   3. Offer to add Python's Scripts dir to your user PATH
) else (
    echo No tenant pipinstall wrapper found in this folder.
    echo Falling back to public PyPI ^+ pip-system-certs bootstrap.
    echo This will:
    echo   1. Bootstrap pip-system-certs ^(corporate TLS-proxy fix^)
    echo   2. Install copilot-cli-wrapper ^(with [gui] extras^)
    echo   3. Install Playwright's msedge driver
    echo   4. Offer to add Python's Scripts dir to your user PATH
)
echo.

REM ---- step 1: pip-system-certs (only without tenant wrapper) ---------
if not defined PIPINSTALL (
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
    echo.
)

REM ---- step 2: install package ---------------------------------------
echo [2/4] Installing copilot-cli-wrapper...
pushd "%~dp0\.."
REM Isolate the tenant wrapper in a child cmd: some corporate pipinstall.bat
REM scripts use 'exit' (not 'exit /b') and would otherwise terminate this
REM script silently right after install - looking like a random "stops after
REM pip" bug with no error.
if defined PIPINSTALL (
    cmd /s /c ""!PIPINSTALL!" -e ".[gui,tokens]""
) else (
    python -m pip install -e ".[gui,tokens]"
)
set "INSTALL_RC=!ERRORLEVEL!"
popd
if not "!INSTALL_RC!" == "0" (
    echo ERROR: install failed ^(exit code !INSTALL_RC!^).
    pause
    exit /b 1
)
echo   ...installed
echo.

REM ---- step 3: playwright msedge driver ------------------------------
echo [3/4] Installing Playwright msedge driver...
python -m playwright install msedge
if errorlevel 1 (
    echo WARN: 'playwright install msedge' failed.
    echo       You can retry it manually; the rest of setup will continue.
)
echo.

REM ---- step 4: PATH --------------------------------------------------
echo [4/4] Checking 'copilot' command on PATH...
where copilot >nul 2>&1
if errorlevel 1 goto :detect_scripts_dir

REM 'copilot' is on PATH already - show where and we're done. Avoid the
REM for/goto-inside-parens pattern (cmd's parser sometimes mishandles it
REM and exits the whole script silently).
echo   OK on PATH:
where copilot
goto :done

:detect_scripts_dir
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

set "SCRIPTS_DIR=!SCRIPTS_DIR!"
powershell -NoProfile -Command ^
    "$p = [Environment]::GetEnvironmentVariable('PATH','User'); ^
     if ($p -notlike '*' + $env:SCRIPTS_DIR + '*') { ^
         [Environment]::SetEnvironmentVariable('PATH', $p + ';' + $env:SCRIPTS_DIR, 'User'); ^
         Write-Host '  added to user PATH (open a new shell to pick it up)' } ^
     else { Write-Host '  already in user PATH' }"
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
REM Keep the window open if the user ran this by double-clicking from
REM Explorer - without pause, the cmd window would close immediately on
REM success and they'd never see the "Setup complete" banner.
pause
endlocal
exit /b 0
