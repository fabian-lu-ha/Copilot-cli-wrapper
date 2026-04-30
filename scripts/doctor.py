"""All-in-one diagnostic. Replaces the brittle doctor.bat logic with Python
so we don't trip over cmd.exe's `for /f` parsing of paths with spaces, etc.

Each check prints OK / WARN / FAIL and an absolute-path-or-version string.
Always runs every check (none of them are destructive).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).resolve().parent
WRAPPER_NAMES = ("pipinstall.bat", "pipinstall.cmd", "pipinstall.exe", "pipinstall.ps1")


fails: list[str] = []
warns: list[str] = []
fixes: list[str] = []


def banner(text: str) -> None:
    print()
    print("=" * 50)
    print(f"  {text}")
    print("=" * 50)


def check_step(num: int, total: int, name: str) -> None:
    print(f"\n[{num}/{total}] {name}")


def ok(msg: str) -> None:
    print(f"  OK    {msg}")


def warn(msg: str, fix: Optional[str] = None) -> None:
    print(f"  WARN  {msg}")
    warns.append(msg)
    if fix:
        fixes.append(fix)


def fail(msg: str, fix: Optional[str] = None) -> None:
    print(f"  FAIL  {msg}")
    fails.append(msg)
    if fix:
        fixes.append(fix)


def find_tenant_wrapper() -> Optional[Path]:
    for n in WRAPPER_NAMES:
        p = SCRIPT_DIR / n
        if p.exists():
            return p
    return None


def main() -> int:
    banner("Copilot CLI Doctor")

    wrapper = find_tenant_wrapper()
    if wrapper:
        print(f"\n[0/8] Tenant pipinstall wrapper detected: {wrapper}")
        print("        Fix suggestions will use this wrapper.")

    # 1. Python
    check_step(1, 8, "Python interpreter")
    print(f"  python: {sys.executable}")
    print(f"  version: {sys.version.splitlines()[0]}")
    if sys.version_info < (3, 9):
        fail(f"Python {sys.version.split()[0]} is too old; need 3.9+",
             "install Python 3.9+ from https://www.python.org/downloads/")
    else:
        ok(f"python {'.'.join(map(str, sys.version_info[:3]))}")

    # 2. pip
    check_step(2, 8, "pip")
    rc = subprocess.call([sys.executable, "-m", "pip", "--version"])
    if rc != 0:
        fail("pip not available",
             "run: python -m ensurepip --upgrade")
    else:
        ok("pip is callable (version printed above)")

    # 3. SSL / pypi reachability
    check_step(3, 8, "SSL connectivity to pypi.org (5s timeout)")
    try:
        import urllib.request
        urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=5).read(64)
        ok("pypi.org reachable, TLS verified")
    except Exception as e:
        msg = str(e)
        if "CERTIFICATE_VERIFY_FAILED" in msg or "self-signed" in msg.lower():
            fix = ("use the tenant wrapper" if wrapper else
                   "pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org pip-system-certs")
            warn(f"TLS verification failed (corp proxy intercepting): {msg[:80]}", fix)
        else:
            warn(f"could not reach pypi.org: {msg[:80]}",
                 "check network/proxy settings")

    # 4. pip-system-certs
    check_step(4, 8, "pip-system-certs (Windows trust store)")
    if wrapper is not None:
        print("  SKIP  not needed when using tenant pipinstall wrapper")
    else:
        try:
            import pip_system_certs  # noqa: F401
            ok("installed")
        except ImportError:
            warn("pip-system-certs not installed",
                 "pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org pip-system-certs")

    # 5. copilot-cli-wrapper package
    check_step(5, 8, "copilot-cli-wrapper package")
    try:
        try:
            from importlib.metadata import version, PackageNotFoundError
        except ImportError:
            from importlib_metadata import version, PackageNotFoundError  # type: ignore
        v = version("copilot-cli-wrapper")
        ok(f"version {v}")
    except Exception:
        if wrapper:
            fix_cmd = f'cd to repo dir and run: call "{wrapper}" -e .[gui,tokens]'
        else:
            fix_cmd = "cd to repo dir and run: pip install -e .[gui,tokens]"
        fail("package not installed", fix_cmd)

    # 6. copilot + copilot-gui scripts on PATH
    check_step(6, 8, "copilot / copilot-gui commands on PATH")
    cli_path = shutil.which("copilot")
    gui_path = shutil.which("copilot-gui")
    scripts_dir = sysconfig.get_path("scripts")
    if cli_path:
        ok(f"copilot     -> {cli_path}")
    else:
        warn(f"'copilot' not on PATH (Python Scripts dir: {scripts_dir})",
             f"add {scripts_dir} to PATH (run setup.bat) or use: python -m copilot_cli")
    if gui_path:
        ok(f"copilot-gui -> {gui_path}")
    else:
        # The GUI extras may not have been installed (PySide6 missing) or
        # the tenant pip wrapper stripped the entry point. Either way the
        # `python -m` form still works if the module is importable.
        try:
            import importlib
            importlib.import_module("copilot_cli.gui.app")
            warn("'copilot-gui' not on PATH but module imports fine",
                 "run: python -m copilot_cli.gui.app")
        except Exception as e:
            warn(f"'copilot-gui' missing AND module import fails: {e}",
                 f"reinstall with [gui] extras, or run setup.bat. If a tenant pipinstall wrapper "
                 f"strips entry points, try: pip install -e \".[gui]\" --force-reinstall")

    # 7. Microsoft Edge
    check_step(7, 8, "Microsoft Edge")
    if os.name == "nt":
        candidates = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        candidates = []
    for c in candidates:
        if c and os.path.exists(c):
            ok(c)
            break
    else:
        if not candidates:
            warn("no standard Edge install location for this OS; manual check needed")
        else:
            fail("Edge not found in standard locations",
                 "install Microsoft Edge: https://www.microsoft.com/edge")

    # 8. Playwright + msedge driver
    check_step(8, 8, "Playwright + msedge driver")
    try:
        import playwright  # noqa: F401
    except ImportError:
        fail("playwright python package not installed",
             ("call \"" + str(wrapper) + "\" playwright>=1.45") if wrapper else "pip install playwright>=1.45")
    else:
        try:
            from playwright.sync_api import sync_playwright
            print("  testing playwright launch msedge (15s timeout)...")
            p = sync_playwright().start()
            try:
                b = p.chromium.launch(channel="msedge", headless=True)
                b.close()
                ok("msedge channel launches")
            finally:
                p.stop()
        except Exception as e:
            warn(f"msedge driver shim not available: {str(e)[:120]}",
                 "python -m playwright install msedge")

    # Summary
    banner("Summary")
    if fails:
        print(f"  FAIL: {len(fails)}  WARN: {len(warns)}")
    elif warns:
        print(f"  All required checks passed. {len(warns)} warning(s).")
    else:
        print("  All checks passed. You should be able to run 'copilot' or 'copilot-gui'.")

    if fixes:
        print("\nSuggested fixes:")
        for f in fixes:
            print(f"  - {f}")
        print("\nRun setup.bat to apply most of them automatically.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
