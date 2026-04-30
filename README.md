# copilot-cli-wrapper

A Claude-Code-style coding CLI that drives **Microsoft 365 Copilot** through a
persistent Edge profile. It does not require GitHub Copilot, an Anthropic key,
or any Graph admin consent — it uses the same browser session you already use
for Copilot, so all corporate policy (DLP, sensitivity labels, web-grounding
toggle, Conditional Access) applies automatically.

> Status: experimental. The browser path is unsupported by Microsoft and
> selectors may shift; check `selectors.yaml` and tweak as needed.

## How it works

```
   you ──► CLI ──► (XML system prompt) ──► Playwright drives Edge
                                          │
                                          ▼
                                  m365.cloud.microsoft/chat
                                          │
                                          ▼
                          assistant emits <tool_use>...</tool_use>
                                          │
                                          ▼
                       CLI parses, runs the tool locally,
                       feeds <tool_result> back into the next turn
```

* **Backend**: Playwright with `launch_persistent_context(channel="msedge")`
  against `https://m365.cloud.microsoft/chat`. First run opens a real Edge
  window for interactive sign-in; the dedicated profile dir keeps you signed
  in afterwards.
* **Streaming**: primary path is direct **WebSocket capture** of the substrate
  SignalR frames (`wss://substrate.office.com/m365chat/...`) — yields the
  assistant's text token-by-token as Copilot emits it. If no recognised frame
  arrives within `ws_first_delta_timeout` (default 4 s) the backend falls back
  to DOM polling. Frame parser lives in
  `src/copilot_cli/backend/substrate_capture.py` and is unit-tested without
  Playwright; if Microsoft changes the schema, that's the file to update.
* **Tool calling on a non-tool-calling model**: XML tags
  (`<tool_use><name>…</name><args>{…}</args></tool_use>`), one tool per turn,
  stream parser detects the closing tag and stops generation early.
* **Edits**: Aider-style `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks.
* **Tools**: `read_file`, `write_file`, `edit_file`, `list_dir`, `grep`, `glob`, `run_bash`. `read_file` matches Claude Code's behaviour: `cat -n` formatted output, 2000-line default, 2000-char-per-line truncation, 256 KB hard size gate, 25k-token output cap, 1-indexed `offset`. `run_bash` defaults to a 120 s timeout and 50 KB output cap.
* **Model selection**: M365 Copilot exposes a model picker (GPT-5.x Quick / Thinking / Auto / sometimes Claude). The CLI auto-discovers it on the page (no hard-coded selectors — uses an accessible-name heuristic). Pick with `--model "GPT-5.4 Thinking"` or the `/model X` REPL command; list options with `--list-models` or `/models`.
* **Context window**: tracked locally with `tiktoken` (`o200k_base`, the GPT-4o/5 encoding) — falls back to a 4-chars/token heuristic if `tiktoken` isn't installed. Default budget is 128 K tokens (BizChat's current cap for GPT-5 family). Warns at 80 %, auto-compacts at 92 %. `/compact` triggers it manually, `/context` shows current usage.
* **Permissions**: read/list/grep run silently; write/edit/bash prompt with
  `[once] [session] [prefix-allow (bash)] [no]`. `--autopilot` skips prompts.
* **Sessions**: every turn appended to `<datadir>/sessions/<id>.jsonl`; resume
  with `copilot --resume <id>`.

## Install (Windows)

```powershell
# 1. Install Python 3.9+ from python.org (Microsoft Store works too)
# 2. Clone and install
git clone <this repo> copilot-cli-wrapper
cd copilot-cli-wrapper
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

# 3. Install Playwright's Edge driver shim (uses your already-installed Edge)
playwright install msedge
```

If you don't have ripgrep (`rg`) installed, `grep` falls back to a Python
walker — fine for small repos, slow on big ones. Install via
`winget install BurntSushi.ripgrep.MSVC`.

### Behind a corporate TLS proxy (Zscaler, Netskope, etc.)

If `pip install` dies with `SSLCertVerificationError: unable to get local
issuer certificate`, your corp proxy is re-signing HTTPS with a private root
CA that Python doesn't trust. Three fixes, best to worst:

**1. Use the Windows trust store (recommended).** IT has already installed
the corporate root cert system-wide for Edge — make Python use it too:

```powershell
# Bootstrap pip-system-certs through the trusted-host workaround:
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org pip-system-certs
# Now everything else works normally:
pip install -e .
playwright install msedge
```

**2. Point pip at the corporate CA bundle.** If your IT publishes the cert
file (often `corp-root.pem` or `zscaler.pem` somewhere in `C:\ProgramData`):

```powershell
pip config set global.cert "C:\path\to\corp-bundle.pem"
[Environment]::SetEnvironmentVariable("SSL_CERT_FILE", "C:\path\to\corp-bundle.pem", "User")
[Environment]::SetEnvironmentVariable("REQUESTS_CA_BUNDLE", "C:\path\to\corp-bundle.pem", "User")
```

**3. Quick-and-dirty (no SSL verification).** Don't do this on a network
you don't trust, but it gets you unblocked:

```powershell
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org -e .
```

The `playwright install msedge` step downloads from `playwright.azureedge.net`,
which the corp proxy also intercepts — same fix applies. Set
`NODE_TLS_REJECT_UNAUTHORIZED=0` in the same shell as a last resort if
Playwright's downloader chokes.

## Quick install (Windows)

Two `.bat` scripts in `scripts/` automate the messy parts:

```cmd
scripts\setup.bat     :: bootstraps pip-system-certs (corp TLS), installs the
                      :: package with [gui] extras, installs Playwright's msedge
                      :: driver, optionally adds Python's Scripts dir to PATH
scripts\doctor.bat    :: diagnoses what's working and prints exact fix commands
                      :: for whatever isn't (Python version, SSL, missing edge,
                      :: missing playwright driver, copilot not on PATH, etc.)
```

`doctor.bat` is non-destructive — run it first if anything's off.

**Tenant-specific install wrapper.** If your company gives you a script that
already configures the corp proxy / CA bundle / private index (commonly named
`pipinstall.bat`), drop it into `scripts/pipinstall.bat`. Both `setup.bat` and
`doctor.bat` auto-detect it and use it as a drop-in for `pip install …`,
skipping the public-PyPI bootstrap. The wrapper is gitignored
(`scripts/pipinstall.*`, `scripts/*-tenant.*`, `scripts/*.local.*`) so it
never accidentally ends up in version control.

## GUI

A PySide6 desktop app with a ChatGPT-style dark theme, markdown + Pygments
syntax highlighting in code blocks, streaming token rendering, collapsible
tool-call boxes, sidebar with session history, model picker, and a context
indicator. Same backend as the CLI (Playwright + agent loop), so anything that
works in the terminal works here too.

```cmd
copilot-gui                          :: launch the desktop UI
copilot-gui --workdir C:\some\repo   :: pin tools to a different repo
copilot-gui --model "GPT-5.4 Thinking"
```

Install needs the `gui` extras: `pip install -e ".[gui]"` (or run
`scripts\setup.bat`, which does it for you).

## Run

```powershell
copilot                                      # interactive REPL
copilot -p "explain this repo"               # one-shot
copilot --list-models                        # discover models in the UI and exit
copilot --model "GPT-5.4 Thinking"           # pick a model on startup
copilot --autopilot                          # auto-approve every tool call (risky)
copilot --resume <session-id>                # continue a prior session
```

REPL commands:

| command           | effect |
| ---               | --- |
| `/new`            | reset the upstream Copilot conversation (keeps local transcript) |
| `/yolo`           | toggle autopilot |
| `/models`         | list discovered models |
| `/model <name>`   | switch model (substring match, case-insensitive) |
| `/context`        | show token usage vs. budget |
| `/compact`        | summarize older turns to free context |
| `/help`           | list commands |
| `/exit`           | quit |

For better token counting accuracy, install tiktoken: `pip install tiktoken`
(it's not a hard dependency because some corporate networks block its model
download; without it the CLI uses a chars/4 heuristic).

## First-run sign-in

When you start `copilot` the first time, an Edge window opens to
`m365.cloud.microsoft/chat`. Sign in normally (SSO/MFA/Conditional Access all
work because it's a real Edge session). The CLI waits for the chat input box
to appear, then takes over. The Edge profile is stored under
`%LOCALAPPDATA%\copilot-cli\edge-profile` and reused on subsequent runs.

Override the profile dir with `COPILOT_CLI_PROFILE_DIR`.

## When the Copilot UI changes

Microsoft tweaks the Copilot page selectors fairly often. If the CLI hangs
after sending a prompt or never finds the input box, drop a
`selectors.yaml` next to your config dir
(`%APPDATA%\copilot-cli\selectors.yaml`):

```yaml
chat_url: https://m365.cloud.microsoft/chat
input_box: 'div[contenteditable="true"][role="textbox"]'
send_button: 'button[aria-label="Send"]'
response_messages: '[data-content="ai-message"]'
new_chat_button: 'button[aria-label="New chat"]'
```

Use Edge DevTools (F12) → Inspector to find the right selectors for your
tenant's variant.

## Compliance posture

* The wrapper **never bypasses Copilot**. Every prompt goes through the same
  web UI you would use manually, on the same authenticated session.
* It does **not** extract cookies to disk in plaintext; the Edge persistent
  context dir is DPAPI-encrypted by Edge on Windows.
* It does **not** call any reverse-engineered API (`substrate.office.com`
  WebSocket, etc.) — only Edge does, just like normal browsing.
* Anything Copilot blocks server-side (web grounding off, DLP, sensitivity
  labels) is enforced server-side and applies automatically.
* Tools that touch your filesystem or shell are **gated by per-call approval
  prompts** unless you opt into autopilot.

If your IT department forbids browser automation specifically, don't use
this. The unsupported-by-Microsoft caveat means it could break with any
M365 update.

## Why not the official Graph API?

`POST /beta/copilot/conversations/{id}/chatOverStream` exists and is the
clean path, but it requires:

* an **M365 Copilot license** ($30/user/mo enterprise add-on),
* **tenant admin consent** for seven scopes (`Sites.Read.All`, `Mail.Read`,
  `People.Read.All`, `OnlineMeetingTranscript.Read.All`, `Chat.Read`,
  `ChannelMessage.Read.All`, `ExternalItem.Read.All`),
* delegated-only auth — no app/service principal access today.

If your tenant happens to have all that set up, a Graph backend implementing
`CopilotBackend` (`src/copilot_cli/backend/base.py`) is straightforward to
add and would be more reliable than the browser path.

## Project layout

```
src/copilot_cli/
├── cli.py                     # argparse + REPL
├── config.py                  # paths, settings, selectors.yaml loader
├── backend/
│   ├── base.py                # CopilotBackend ABC
│   └── playwright_backend.py  # Edge persistent-context driver
├── agent/
│   ├── system_prompt.py       # tool-use XML format spec
│   ├── parser.py              # streaming XML/final-tag detector
│   ├── transcript.py          # session.jsonl
│   └── loop.py                # send → stream → tool → repeat
├── tools/
│   ├── registry.py
│   ├── fs.py                  # read/write/edit/list/grep + path jail
│   └── shell.py               # run_bash with PowerShell on Windows
└── ui/
    ├── permissions.py         # approval gate + session allowlist
    └── render.py              # streaming token renderer
```

## Tests

```powershell
pip install -e ".[dev]"
pytest -q
```

The Playwright backend itself isn't unit-tested (it requires a live Copilot
session); tests cover the parser, filesystem tools, permission gate, and
shell denylist.

## License

MIT.
