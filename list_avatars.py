"""
list_avatars.py — list the LiveAvatar avatars (and voices) your API key can use.

    avatar_env\\Scripts\\python list_avatars.py

Prints them and saves the list to logs\\avatars.txt. Copy an avatar id into
.env as LIVEAVATAR_AVATAR_ID and set LIVEAVATAR_SANDBOX=false.
"""

import json
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from avatar.config import load_config, secret  # noqa: E402

API = "https://api.liveavatar.com"


def fetch(client, path):
    try:
        r = client.get(API + path)
        if r.status_code >= 400:
            return None, f"{r.status_code} {r.text[:200]}"
        return r.json(), None
    except Exception as e:
        return None, str(e)


def items(data):
    d = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(d, dict):
        d = d.get("results") or d.get("avatars") or d.get("voices") or d.get("items") or []
    return d if isinstance(d, list) else []


def main():
    load_config()
    key = secret("LIVEAVATAR_API_KEY")
    if not key:
        print("LIVEAVATAR_API_KEY is missing from .env")
        return 1
    out = []
    with httpx.Client(timeout=30, headers={"X-API-KEY": key}) as client:
        for title, path in [
            ("YOUR AVATARS", "/v1/avatars?page_size=100"),
            ("PUBLIC AVATARS", "/v1/avatars/public?page_size=100"),
            ("VOICES", "/v1/voices?page_size=100"),
        ]:
            data, err = fetch(client, path)
            out.append(f"\n=== {title}  ({path}) ===")
            if err:
                out.append(f"  (not available: {err})")
                continue
            rows = items(data)
            if not rows:
                out.append("  (none)")
                out.append("  raw: " + json.dumps(data)[:500])
            for a in rows:
                fields = {k: a.get(k) for k in ("id", "name", "type", "status", "gender", "language", "default_voice_id", "voice_id") if a.get(k) is not None}
                out.append("  " + json.dumps(fields, ensure_ascii=False))
    text = "\n".join(out)
    print(text)
    (HERE / "logs").mkdir(exist_ok=True)
    (HERE / "logs" / "avatars.txt").write_text(text, encoding="utf-8")
    print("\nSaved to logs\\avatars.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
