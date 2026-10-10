"""
avatar_providers.py — server side of the pluggable avatar layer.

The API key never leaves this process: the backend mints a short-lived
session token and hands only that to the UI, whose JS adapter (ui/app.js)
connects over WebRTC and makes the avatar speak.

To add another provider (Tavus, Simli, D-ID...), subclass AvatarProvider,
register it in PROVIDERS, and add a matching adapter in ui/app.js.
"""

from __future__ import annotations

import httpx

from .config import secret

LIVEAVATAR_API = "https://api.liveavatar.com"
LIVEAVATAR_SANDBOX_AVATAR = "dd73ea75-1218-4ef3-92ce-606d5f7fbc0a"  # "Wayne", free 1-min sessions


class AvatarProvider:
    name = "none"

    def configured(self) -> bool:
        return False

    async def create_session(self) -> dict:
        raise NotImplementedError

    async def stop_session(self, token: str) -> None:
        pass


class NoAvatar(AvatarProvider):
    """Local animated orb + Windows voice. Free, offline."""
    name = "local"

    def configured(self) -> bool:
        return True

    async def create_session(self) -> dict:
        return {"provider": "local"}


class LiveAvatarProvider(AvatarProvider):
    """HeyGen LiveAvatar (FULL mode).

    We keep the microphone muted on their side and call session.repeat(text)
    from the UI, so the avatar speaks exactly what *our* orchestrator says —
    their built-in LLM never answers.
    """
    name = "liveavatar"

    def __init__(self, config: dict):
        self.config = config

    @property
    def sandbox(self) -> bool:
        return secret("LIVEAVATAR_SANDBOX").lower() in ("1", "true", "yes")

    def configured(self) -> bool:
        return bool(secret("LIVEAVATAR_API_KEY")) and (self.sandbox or bool(secret("LIVEAVATAR_AVATAR_ID")))

    async def create_session(self) -> dict:
        persona = {"language": self.config.get("avatar_language", "en")}
        if secret("LIVEAVATAR_VOICE_ID"):
            persona["voice_id"] = secret("LIVEAVATAR_VOICE_ID")
        if secret("LIVEAVATAR_CONTEXT_ID"):
            persona["context_id"] = secret("LIVEAVATAR_CONTEXT_ID")
        body = {
            "mode": "FULL",
            "avatar_id": LIVEAVATAR_SANDBOX_AVATAR if self.sandbox else secret("LIVEAVATAR_AVATAR_ID"),
            "avatar_persona": persona,
        }
        if self.sandbox:
            body["is_sandbox"] = True
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{LIVEAVATAR_API}/v1/sessions/token",
                json=body,
                headers={"X-API-KEY": secret("LIVEAVATAR_API_KEY"), "Content-Type": "application/json"},
            )
        if r.status_code >= 400:
            raise RuntimeError(f"LiveAvatar token request failed ({r.status_code}): {r.text[:500]}")
        data = r.json().get("data", {})
        return {
            "provider": "liveavatar",
            "session_id": data.get("session_id"),
            "session_token": data.get("session_token"),
            "sandbox": self.sandbox,
        }

    async def stop_session(self, token: str) -> None:
        if not token:
            return
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{LIVEAVATAR_API}/v1/sessions/stop", headers={"Authorization": f"Bearer {token}"})


def get_provider(config: dict) -> AvatarProvider:
    name = config.get("avatar_provider", "liveavatar")
    if name == "liveavatar":
        p = LiveAvatarProvider(config)
        if p.configured():
            return p
    return NoAvatar()
