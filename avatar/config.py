"""
config.py — loads avatar_config.json and .env (API keys) from the Shiv folder.
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # D:\Shiv
CONFIG_PATH = ROOT / "avatar_config.json"
ENV_PATH = ROOT / ".env"

DEFAULTS = {
    "assistant_name": "Shiv",
    "user_name": "",
    "llm_provider": "claude",
    "claude_model": "claude-sonnet-5-5",
    "claude_max_tokens": 2048,
    "fallback_to_ollama": True,
    "ollama_model": "qwen2.5:7b",
    "ollama_url": "http://localhost:11434",
    "max_agent_steps": 12,
    "max_history_messages": 40,
    "server_host": "127.0.0.1",
    "server_port": 8765,
    "avatar_provider": "liveavatar",
    "avatar_idle_minutes": 5,
    "avatar_language": "en",
    "local_tts": True,
    "local_tts_rate": 185,
    "local_tts_voice_contains": "",
    "stt_model": "base.en",
    "stt_device": "cpu",
    "stt_compute_type": "int8",
    "listening_mode": "ptt",
    "wake_words": ["shiv", "hey shiv", "ok shiv"],
    "vad_energy_threshold": 0.015,
    "vad_silence_seconds": 0.9,
    "global_hotkey": "<ctrl>+<alt>+<space>",
    "metrics_interval_seconds": 5,
    "context_poll_seconds": 3,
    "trash_folder_name": "Shiv_Trash",
    "auto_approve_tools": [],
    "always_confirm_tools": [],
    "ui_mode": "edge",
    "start_minimized": False,
    "always_on_top": False,
}


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH, override=False)
    except ImportError:
        # Minimal fallback parser so a missing python-dotenv isn't fatal.
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: Path = CONFIG_PATH) -> dict:
    _load_env()
    cfg = dict(DEFAULTS)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def secret(name: str) -> str:
    return (os.environ.get(name) or "").strip()
