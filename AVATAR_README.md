# Shiv Avatar: a 24/7 talking desktop agent

The avatar version of Shiv sits in your tray. It can talk with a photorealistic avatar or a local animated orb, it listens when you press a hotkey or say "Hey Shiv", and it uses your computer through tools. Before it does anything risky, it asks you first.

The original text-only Shiv (`shiv.py`) still works and hasn't changed.

```
┌──────────────── DESKTOP SHELL  (PyQt6 + Qt WebEngine)  avatar/shell.py ────────────────┐
│  Tray icon · always-on-top · start with Windows · single instance                      │
│  ┌──────────── avatar/ui (HTML/JS) ────────────┐                                       │
│  │ Avatar stage: LiveAvatar video (WebRTC)     │  Chat · confirm card · mic · modes    │
│  │   or local orb + Windows voice (fallback)   │                                       │
│  └───────────────▲────────────────────────────┘                                       │
└──────────────────┼─────────────── WebSocket ws://127.0.0.1:8765/ws (token + origin) ───┘
                   │
┌──────────────────▼─────────── PYTHON AGENT BACKEND CORE  avatar/server.py ─────────────┐
│ Orchestrator (orchestrator.py)        Computer Control Toolkit (computer_tools.py)      │
│  Claude (streaming, tool use)  ─────►  files · commands · psutil metrics · processes    │
│  └ falls back to Ollama (llm.py)       screenshots (mss) · mouse/keys (pyautogui)       │
│  confirm policy · desktop context      windows · URLs · Shiv's original tools.py        │
│ Voice (voice.py): faster-whisper STT (push-to-talk / wake word) · SAPI TTS             │
│ Avatar tokens (avatar_providers.py): LiveAvatar API key never leaves the backend       │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

## Setup (one time)

1. Run **`setup_avatar.bat`**. It creates `avatar_env`, installs the packages, makes a `.env` file and opens it in Notepad.
2. In `.env`, paste your **`ANTHROPIC_API_KEY`**.
   - Optional: add a **`LIVEAVATAR_API_KEY`** from <https://app.liveavatar.com>. With `LIVEAVATAR_SANDBOX=true` you get free 1‑minute test sessions. For real use, set it to `false` and fill in `LIVEAVATAR_AVATAR_ID` (and `LIVEAVATAR_VOICE_ID` if you like).
3. Keep Ollama installed (`ollama pull qwen2.5:7b`) if you want Shiv to keep working when Claude isn't reachable.

## Run

- `run_avatar.bat` starts Shiv without a console window.
- `run_avatar.bat debug` starts it with a console so you can see the logs.
- `avatar_env\Scripts\python shiv_avatar.py --no-ui` runs the backend only and prints a URL you can open in Edge or Chrome.

From the tray menu you can choose **Start with Windows** (makes Shiv run 24/7), **Always on top**, **Reload UI** and **Quit**. Closing the window only hides it.

## Talking to it

| Mode | How |
|---|---|
| Typing | Type and press Enter. Shift+Enter adds a new line. Esc stops. |
| Push to talk | Hold the mic button, or press **Ctrl+Alt+Space** anywhere to start and again to stop |
| Wake word | Say "Hey Shiv, open my downloads folder". For about 12 seconds afterwards you can ask follow-ups without the wake word. |

The first time you use voice, Shiv downloads the Whisper `base.en` model, which is about 150 MB. Starting to talk interrupts the avatar.

## Safety model

- Shiv **runs these without asking**: reading, listing and searching files, system metrics, process list, screenshots, the clipboard, the active window, opening URLs, and opening non-executable files.
- Shiv **asks first** for: writing files, running commands, moving files to trash, killing processes, mouse clicks, typing, key presses, scrolling, and launching `.exe/.bat/.ps1/...` files.
- If **nobody has the window connected**, Shiv automatically denies risky actions. It never acts unattended.
- **Always (session)** auto-approves that one tool until you restart Shiv.
- To slam the brakes on mouse or keyboard control, move the mouse into a screen corner (pyautogui failsafe).
- The backend only listens on `127.0.0.1`, and every call needs a random per-launch token plus a matching origin, so websites in your normal browser can't control Shiv.
- You can tune all of this in `avatar_config.json` with `auto_approve_tools` and `always_confirm_tools`.

## Costs

LiveAvatar FULL mode costs about 2 credits per minute while a session is open. Shiv opens a session only when it needs to speak, and closes it after `avatar_idle_minutes` (5 by default) of silence. While idle, the free local orb takes over. Use the **Avatar** dropdown and choose *Local orb* to never use credits.

## Config (`avatar_config.json`)

| Key | Meaning |
|---|---|
| `llm_provider` | `claude` or `ollama` (you can also switch this live in the UI) |
| `claude_model` | Default `claude-sonnet-5-5`, which is fast and good for voice. Use `claude-opus-5-5` for harder tasks. |
| `fallback_to_ollama` | Use local Ollama if Claude is unreachable or has no key |
| `listening_mode` | Mode at startup: `off`, `ptt` or `wake` |
| `wake_words`, `vad_energy_threshold` | Raise the threshold if a noisy room keeps triggering Shiv |
| `stt_model` | `tiny.en` / `base.en` / `small.en`. Use `stt_device: "cuda"` with `stt_compute_type: "float16"` on your NVIDIA GPU. |
| `local_tts_voice_contains` | Pick a Windows voice, e.g. `"Zira"` or `"Heera"` |
| `avatar_idle_minutes` | Close the paid avatar stream after this much silence |
| `auto_approve_tools` / `always_confirm_tools` | Override the safety tiers |

## Adding another avatar provider (Tavus, Simli, D‑ID…)

1. **Backend:** subclass `AvatarProvider` in `avatar/avatar_providers.py`. Its job is to mint a session token, and the provider's API key stays in the backend. Register the subclass in `get_provider()`.
2. **UI:** add an adapter class in `avatar/ui/app.js` with `start()`, `speak(text)`, `interrupt()` and `stop()`, like `LiveAvatarAdapter`.

## Files

| Path | Purpose |
|---|---|
| `shiv_avatar.py` | Entry point |
| `avatar/shell.py` | PyQt6 window, tray, autostart, single instance |
| `avatar/server.py` | FastAPI + WebSocket IPC, wires everything together |
| `avatar/orchestrator.py` | Agent loop, confirmation policy, system prompt, speech shaping |
| `avatar/llm.py` | Claude and Ollama providers |
| `avatar/computer_tools.py` | Tool registry and the new computer-control tools |
| `avatar/voice.py` | Speech-to-text (faster-whisper) and local text-to-speech (SAPI) |
| `avatar/avatar_providers.py` | LiveAvatar token minting (pluggable) |
| `avatar/ui/` | Front end; `vendor/liveavatar-sdk.js` is HeyGen's MIT SDK, bundled |
| `tests/test_backend.py` | `avatar_env\Scripts\python -m pytest tests -q` |
