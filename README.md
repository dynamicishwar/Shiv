# Shiv — a local desktop AI companion

Shiv lives in your Windows system tray. Click it to open a chat window, and
it can read/search/write files, open files and apps, run commands, and
notice what's currently on screen/clipboard — all powered by a **local**
model through [Ollama](https://ollama.com), so nothing you say to it leaves
your computer and there's no API key or subscription cost.

## What it can do

- Chat and answer questions
- List, search, read, and write files
- Open files and launch applications
- Run shell commands (always asks you to confirm first)
- Remove files (moves them to a `Shiv_Trash` folder next to them — never a
  permanent delete)
- Notice your active window title and clipboard contents, so you don't have
  to explain context every time

## One-time setup

1. **Install Python 3.10+** if you don't have it: https://www.python.org/downloads/
   (when installing, check "Add python.exe to PATH")

2. **Install Ollama** (the local model runner): https://ollama.com/download/windows
   Run the installer, then open a terminal (PowerShell or Command Prompt) and
   pull a model that supports tool calling:

   ```
   ollama pull qwen2.5:7b
   ```

   That model needs roughly 5–6 GB of free RAM. If your machine is more
   limited, a smaller alternative that still supports tools is:

   ```
   ollama pull qwen2.5:3b
   ```

   (If you use a different model name, update `"model"` in `config.json` to
   match.)

3. **Install Shiv's Python dependencies.** Open a terminal in this folder
   (`D:\Shiv`) and run:

   ```
   pip install -r requirements.txt
   ```

## Running Shiv

Make sure Ollama is running (it usually starts automatically after install
and sits in the tray; if not, run `ollama serve` in a terminal), then:

```
python shiv.py
```

Shiv's icon appears in your system tray. Click it (or right-click → Open
Chat) to start talking to it. Right-click → Quit to close it.

## Making it start automatically (optional)

Press `Win+R`, type `shell:startup`, and hit Enter — this opens your
Windows Startup folder. Create a shortcut there that points to:

```
pythonw.exe D:\Shiv\shiv.py
```

(Using `pythonw.exe` instead of `python.exe` avoids a console window popping
up alongside the tray icon.)

## Safety notes — please read

- **Running a command** or **removing a file** always shows you a plain
  Windows Yes/No dialog with the exact command or path *before* anything
  happens. Nothing destructive happens silently.
- Removing a file moves it to a `Shiv_Trash` folder next to where it was,
  rather than deleting it for good — you can always get it back from there.
- Shiv only sees your clipboard and active window title while it's running;
  nothing is logged to disk or sent anywhere beyond your own computer's
  local Ollama process.
- Shiv can run *any* shell command you approve — treat the confirmation
  dialog as a real checkpoint, not a formality, especially for anything you
  didn't ask it to do yourself.

## Files in this folder

| File | Purpose |
|---|---|
| `shiv.py` | Entry point — tray icon + chat window |
| `agent.py` | Talks to the local Ollama model, runs the tool-call loop |
| `tools.py` | The actual actions Shiv can take (files, commands, etc.) |
| `context_watcher.py` | Background thread tracking active window/clipboard |
| `config.json` | Settings — model name, confirmation toggles, etc. |
| `requirements.txt` | Python packages to install |

## Tuning it

Everything adjustable lives in `config.json`:

- `"model"` — which Ollama model to use
- `"confirm_before_run_command"` / `"confirm_before_delete"` /
  `"confirm_before_overwrite"` — set any of these to `false` if you'd rather
  Shiv act without asking (not recommended to turn all of these off at once)
- `"context_poll_seconds"` — how often it checks your clipboard/active window
- `"max_agent_steps"` — how many tool-call rounds it'll do before giving up
  on a single request
