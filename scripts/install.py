"""All-in-one installer. Replaces the brittle setup.bat logic with Python
so we don't keep tripping over cmd.exe parsing quirks.

What it does (in order):
  0. Detects an optional tenant pip wrapper (scripts/pipinstall.bat etc.)
     and uses it for every pip install.
  1. Bootstraps pip-system-certs (Windows trust store) if no tenant wrapper
     and pypi.org is reachable.
  2. Installs copilot-cli-wrapper from this repo with [gui,tokens] extras.
  3. Installs Playwright's msedge driver shim.
  4. Offers to add Python's Scripts dir to user PATH on Windows.

Usage:
  python scripts/install.py
  python scripts/install.py --skip-playwright    # don't run `playwright install msedge`
  python scripts/install.py --skip-path-update   # don't touch user PATH
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
REPO_ROOT = SCRIPT_DIR.parent

WRAPPER_NAMES = ("pipinstall.bat", "pipinstall.cmd", "pipinstall.exe", "pipinstall.ps1")


def banner(text: str) -> None:
    print()
    print("=" * 50)
    print(f"  {text}")
    print("=" * 50)


def step(num: int, total: int, text: str) -> None:
    print(f"\n[{num}/{total}] {text}")


def find_tenant_wrapper() -> Optional[Path]:
    for name in WRAPPER_NAMES:
        p = SCRIPT_DIR / name
        if p.exists():
            return p
    return None


def run(cmd: list[str], **kwargs) -> int:
    """Run a subprocess, stream output to our stdout/stderr, return rc."""
    # Use list2cmdline for the displayed form so the user sees properly
    # quoted paths-with-spaces (Python's subprocess does this internally
    # before calling CreateProcess on Windows).
    print(f"  $ {subprocess.list2cmdline(cmd)}")
    try:
        return subprocess.call(cmd, **kwargs)
    except FileNotFoundError as e:
        print(f"  ERROR: command not found: {e}")
        return 127


def pip_install(args: list[str], wrapper: Optional[Path], cwd: Optional[Path] = None) -> int:
    """Either invoke the tenant wrapper with the given args (positional after
    'install', following the existing wrapper convention) or run plain
    `python -m pip install ...`. Path-with-spaces safe on Windows: bat/cmd
    wrappers go through cmd.exe with subprocess.list2cmdline-quoted args;
    everything else uses subprocess.call's list form which CreateProcess
    quotes for us."""
    kwargs: dict = {}
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if wrapper is not None:
        suf = wrapper.suffix.lower()
        if suf in (".bat", ".cmd") and os.name == "nt":
            # On Windows, a .bat must be invoked via cmd.exe. Build the
            # command string via list2cmdline so paths/args with spaces are
            # quoted correctly, then feed cmd /s /c the whole quoted string.
            inner = subprocess.list2cmdline([str(wrapper), *args])
            cmd = ["cmd.exe", "/s", "/c", inner]
        elif suf == ".ps1":
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(wrapper), *args]
        else:
            cmd = [str(wrapper), *args]
    else:
        cmd = [sys.executable, "-m", "pip", "install", *args]
    return run(cmd, **kwargs)


def can_reach_pypi() -> bool:
    import urllib.request, ssl
    try:
        urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=5).read(64)
        return True
    except Exception:
        return False


def has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def add_to_user_path(target: Path) -> None:
    if os.name != "nt":
        print("  not on Windows; skipping PATH update")
        return
    # PowerShell single-quoted strings only need '' to escape an apostrophe.
    # Backslashes in Windows paths are fine inside single quotes.
    target_escaped = str(target).replace("'", "''")
    ps = (
        "$p = [Environment]::GetEnvironmentVariable('PATH','User');"
        f"$d = '{target_escaped}';"
        "if ($p -notlike '*' + $d + '*') {"
        "  [Environment]::SetEnvironmentVariable('PATH', $p + ';' + $d, 'User');"
        "  Write-Host '  added to user PATH (open a new shell to pick it up)'"
        "} else { Write-Host '  already in user PATH' }"
    )
    rc = run(["powershell", "-NoProfile", "-Command", ps])
    if rc != 0:
        print(f"  PATH update failed (rc={rc})")


def main(argv: list[str]) -> int:
    skip_playwright = "--skip-playwright" in argv
    skip_path = "--skip-path-update" in argv
    quiet_path = "--no-prompt" in argv  # auto-yes for path update

    banner("Copilot CLI Setup")

    # Show interpreter info early so the user can confirm.
    print(f"python: {sys.executable}")
    print(f"version: {sys.version.splitlines()[0]}")

    if sys.version_info < (3, 9):
        print(f"\nERROR: Python {sys.version.split()[0]} is too old; need 3.9+.")
        return 1

    # ---- 0. tenant wrapper detection ----
    wrapper = find_tenant_wrapper()
    if wrapper is not None:
        print(f"\nDetected tenant pipinstall wrapper: {wrapper}")
        print("Using it for every pip install in this session.")
    else:
        print("\nNo tenant pipinstall wrapper found in scripts/.")

    # ---- 1. pip-system-certs (skip when wrapper handles certs) ----
    if wrapper is None:
        step(1, 4, "pip-system-certs (corporate TLS-proxy fix, idempotent)")
        if has_module("pip_system_certs"):
            print("  already installed")
        else:
            ok = can_reach_pypi()
            print(f"  pypi.org reachable+verified: {ok}")
            args = ["--trusted-host", "pypi.org",
                    "--trusted-host", "files.pythonhosted.org",
                    "--trusted-host", "pypi.python.org",
                    "pip-system-certs"]
            rc = pip_install(args, wrapper=None)
            if rc != 0:
                print("\nERROR: pip-system-certs install failed.")
                return rc

    # ---- 2. install the package itself ----
    step(2, 4, "Installing copilot-cli-wrapper (with [gui,tokens] extras)…")
    # Use a relative path '.[gui,tokens]' with cwd=REPO_ROOT so the repo
    # location never has to be quoted. Earlier we passed the absolute
    # f"{REPO_ROOT}[gui,tokens]" which broke when REPO_ROOT contained
    # spaces (Windows paths under "My Documents", "Program Files", etc.) —
    # the [gui,tokens] suffix made it impossible to quote the path
    # cleanly because pip parses extras off the end of the requirement.
    rc = pip_install(["-e", ".[gui,tokens]"], wrapper=wrapper, cwd=REPO_ROOT)
    if rc != 0:
        print("\nERROR: install failed.")
        return rc
    print("  ...installed")

    # ---- 3. playwright msedge driver ----
    if skip_playwright:
        step(3, 4, "Skipping Playwright msedge install (--skip-playwright)")
    else:
        step(3, 4, "Installing Playwright msedge driver…")
        rc = run([sys.executable, "-m", "playwright", "install", "msedge"])
        if rc != 0:
            print("  WARN: playwright install msedge failed; you can retry "
                  "this manually. Continuing.")

    # ---- 4. PATH ----
    step(4, 4, "Checking 'copilot' command on PATH…")
    found = shutil.which("copilot")
    if found:
        print(f"  OK on PATH: {found}")
    else:
        scripts_dir = Path(sysconfig.get_path("scripts"))
        print(f"  Python's Scripts dir is: {scripts_dir}")
        if skip_path:
            print("  --skip-path-update: not modifying PATH")
        elif os.name != "nt":
            print(f"  not on Windows; add {scripts_dir} to your PATH manually")
        else:
            if quiet_path:
                yes = True
            else:
                ans = input(f"  Add {scripts_dir} to your USER PATH? [y/N] ").strip().lower()
                yes = ans in ("y", "yes")
            if yes:
                add_to_user_path(scripts_dir)
            else:
                print("  skipped. You can run 'python -m copilot_cli' instead.")

    banner("Setup complete.")
    print("\nTry it:")
    print("  copilot           (CLI)")
    print("  copilot-gui       (graphical UI)")
    print("\nIf 'copilot' isn't found, open a new cmd window first")
    print("so it picks up the updated PATH.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
