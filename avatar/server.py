"""
server.py — the Python Agent Backend Core, exposed to the desktop shell
over HTTP + a WebSocket (the IPC channel in the architecture diagram).

Security: binds to 127.0.0.1 only, rejects WebSocket/API calls whose Origin
isn't this server, and requires a random per-launch token, so a web page
open in your normal browser can't drive the agent.
"""

from __future__ import annotations

import asyncio
import logging
import json
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import computer_tools
from .avatar_providers import get_provider
from .orchestrator import Orchestrator
from .voice import LocalTTS, VoiceIO

UI_DIR = Path(__file__).parent / "ui"


class Backend:
    def __init__(self, config: dict, token: str | None = None, enable_devices: bool = True):
        self.config = config
        self.token = token or secrets.token_urlsafe(24)
        self.enable_devices = enable_devices
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.orch: Orchestrator | None = None
        self.voice: VoiceIO | None = None
        self.tts: LocalTTS | None = None
        self.avatar = get_provider(config)
        self.context_watcher = None
        self._bg: list[asyncio.Task] = []
        self._hotkey = None

    # -- lifecycle ------------------------------------------------------------

    async def start(self):
        self.loop = asyncio.get_running_loop()
        if self.enable_devices:
            try:
                from context_watcher import ContextWatcher  # Shiv's original

                self.context_watcher = ContextWatcher(self.config.get("context_poll_seconds", 3))
                self.context_watcher.start()
            except Exception as e:
                logging.getLogger("backend").warning(f"context watcher disabled: {e}")
        self.orch = Orchestrator(self.config, self.broadcast, lambda: bool(self.clients), self.context_watcher)
        if self.enable_devices:
            self.tts = LocalTTS(
                self.config.get("local_tts_rate", 185),
                self.config.get("local_tts_voice_contains", ""),
                on_state=self._on_tts_state,
            ) if self.config.get("local_tts", True) else None
            self.voice = VoiceIO(self.config, self.loop, self.broadcast, self._on_transcript)
            self.voice.set_mode(self.config.get("listening_mode", "ptt"))
            self.voice.warm_up()
            self._start_hotkey()
        self._bg.append(asyncio.create_task(self._metrics_loop()))
        logging.getLogger("backend").info("started: %s", self.state())

    async def stop(self):
        for t in self._bg:
            t.cancel()
        if self.context_watcher:
            self.context_watcher.stop()
        if self._hotkey:
            try:
                self._hotkey.stop()
            except Exception:
                pass

    def _start_hotkey(self):
        combo = self.config.get("global_hotkey")
        if not combo:
            return
        try:
            from pynput import keyboard

            def toggle():
                if not self.voice:
                    return
                if self.voice._ptt_active:
                    self.loop.call_soon_threadsafe(self.voice.ptt_stop)
                else:
                    self.loop.call_soon_threadsafe(self._interrupt_speech)
                    self.loop.call_soon_threadsafe(self.voice.ptt_start)

            self._hotkey = keyboard.GlobalHotKeys({combo: toggle})
            self._hotkey.start()
        except Exception as e:
            logging.getLogger("backend").warning(f"global hotkey disabled: {e}")

    # -- events ---------------------------------------------------------------

    async def broadcast(self, event: dict):
        if not self.clients:
            return
        data = json.dumps(event, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def _on_tts_state(self, speaking: bool):
        if self.voice:
            self.voice.speaking = speaking
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.broadcast({"type": "tts_state", "speaking": speaking}), self.loop)

    async def _on_transcript(self, text: str):
        asyncio.create_task(self.orch.handle_user_message(text, source="voice"))

    def _interrupt_speech(self):
        if self.tts:
            self.tts.stop()
        asyncio.ensure_future(self.broadcast({"type": "interrupt"}))

    async def _metrics_loop(self):
        interval = max(2, int(self.config.get("metrics_interval_seconds", 5)))
        computer_tools.quick_metrics()  # prime psutil's cpu counter
        while True:
            await asyncio.sleep(interval)
            try:
                await self.broadcast({"type": "metrics", **computer_tools.quick_metrics()})
            except Exception:
                pass

    def state(self) -> dict:
        return {
            "assistant_name": self.config.get("assistant_name", "Shiv"),
            "user_name": self.config.get("user_name", ""),
            "llm_provider": self.orch.provider_name if self.orch else self.config.get("llm_provider"),
            "avatar_provider": self.avatar.name,
            "avatar_idle_minutes": self.config.get("avatar_idle_minutes", 5),
            "listening_mode": self.voice.mode if self.voice else "off",
            "stt_available": bool(self.voice and self.voice.available),
            "tts_available": bool(self.tts and self.tts.available),
        }

    # -- websocket messages ---------------------------------------------------

    async def handle(self, msg: dict):
        t = msg.get("type")
        o = self.orch
        if t == "user_message":
            asyncio.create_task(o.handle_user_message(msg.get("text", ""), source="text"))
        elif t == "confirm_response":
            o.resolve_confirm(msg.get("id", ""), msg.get("approved", False), msg.get("always", False))
        elif t == "cancel":
            o.cancel()
            self._interrupt_speech()
        elif t == "reset":
            o.reset()
            await self.broadcast({"type": "notice", "text": "Conversation cleared."})
        elif t == "set_provider":
            try:
                o.set_provider(msg.get("provider", ""))
                await self.broadcast({"type": "state", **self.state()})
            except ValueError:
                pass
        elif t == "ptt_start" and self.voice:
            self._interrupt_speech()
            self.voice.ptt_start()
        elif t == "ptt_stop" and self.voice:
            self.voice.ptt_stop()
        elif t == "set_listening" and self.voice:
            self.voice.set_mode(msg.get("mode", "off"))
            await self.broadcast({"type": "state", **self.state()})
        elif t == "speak_local" and self.tts:
            self.tts.speak(msg.get("text", ""))
        elif t == "stop_speaking" and self.tts:
            self.tts.stop()
        elif t == "log":
            lvl = {"warn": logging.WARNING, "error": logging.ERROR}.get(msg.get("level"), logging.INFO)
            logging.getLogger("ui").log(lvl, "js: %s", str(msg.get("msg", ""))[:1000])
        elif t == "open_link":
            await asyncio.to_thread(computer_tools.open_url, str(msg.get("url", "")))
        elif t == "avatar_speaking" and self.voice:
            self.voice.speaking = bool(msg.get("on"))


def create_app(config: dict, token: str | None = None, enable_devices: bool = True) -> tuple[FastAPI, Backend]:
    backend = Backend(config, token, enable_devices)
    port = config.get("server_port", 8765)
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @asynccontextmanager
    async def lifespan(app):
        await backend.start()
        yield
        await backend.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def no_cache(request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/ui/"):
            resp.headers["Cache-Control"] = "no-cache"  # always pick up UI updates on reload
        return resp

    def check(request_or_ws, token_value: str | None):
        origin = request_or_ws.headers.get("origin")
        if origin and origin not in allowed_origins:
            raise HTTPException(403, "bad origin")
        if not token_value or not secrets.compare_digest(token_value, backend.token):
            raise HTTPException(403, "bad token")

    @app.get("/")
    async def index():
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/api/state")
    async def state(request: Request):
        check(request, request.headers.get("x-shiv-token"))
        return backend.state()

    @app.post("/api/avatar/session")
    async def avatar_session(request: Request):
        check(request, request.headers.get("x-shiv-token"))
        try:
            return await backend.avatar.create_session()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.post("/api/avatar/stop")
    async def avatar_stop(request: Request):
        check(request, request.headers.get("x-shiv-token"))
        body = await request.json()
        try:
            await backend.avatar.stop_session(body.get("session_token", ""))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        try:
            check(ws, ws.query_params.get("t"))
        except HTTPException:
            await ws.close(code=4403)
            return
        await ws.accept()
        backend.clients.add(ws)
        await ws.send_text(json.dumps({"type": "state", **backend.state()}))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    await backend.handle(json.loads(raw))
                except Exception as e:
                    logging.getLogger("backend").warning("bad message: %s", e)
        except WebSocketDisconnect:
            pass
        finally:
            backend.clients.discard(ws)

    return app, backend
