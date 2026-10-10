"""
orchestrator.py — the LLM Orchestrator.

Takes a user message (typed or spoken), adds desktop context, runs the
provider's tool loop, enforces the confirmation policy for risky tools, and
streams events to the UI (which forwards the final reply to the avatar).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import platform
import re
import uuid
from typing import Awaitable, Callable

from . import computer_tools
from .config import secret
from .llm import ClaudeProvider, OllamaProvider, ProviderUnavailable

Broadcast = Callable[[dict], Awaitable[None]]

CONFIRM_TIMEOUT_S = 120


def build_system_prompt(cfg: dict) -> str:
    name = cfg.get("assistant_name", "Shiv")
    user = cfg.get("user_name") or "the user"
    return f"""You are {name}, a 24/7 desktop assistant living on {user}'s {platform.system()} computer. \
You appear as a talking avatar: everything you write in your final reply is also spoken aloud.

How to talk:
- Be warm, direct and brief. Default to 1-3 short spoken sentences.
- If the answer needs detail (code, lists, file contents), put it in the reply but start with one \
short spoken-style summary sentence. Code blocks are shown in chat, not read aloud.
- No filler like "Great question".

How to act:
- When a task needs the computer, use your tools instead of telling {user} to do it.
- Prefer precise tools (files, run_command, focus_window, open_url, press_keys) over clicking around.
- For GUI work: take_screenshot first, then click using coordinates from that screenshot, then \
take another screenshot to verify the result.
- Risky tools (writing files, commands, killing processes, mouse clicks, typing, key presses) ask \
{user} for approval outside of you. If a result says the user declined, accept it and don't retry.
- Never do destructive or irreversible things {user} didn't ask for.
- Each user message may start with a <desktop_context> block (active window, clipboard). Use it \
only when relevant.

Today is {dt.date.today():%A, %d %B %Y}."""


_CODE_RE = re.compile(r"```.*?```", re.S)
_URL_RE = re.compile(r"https?://\S+")


def to_speech(text: str, limit: int = 600) -> str:
    """Turn a chat reply into something sensible to say out loud."""
    had_code = bool(_CODE_RE.search(text))
    s = _CODE_RE.sub(" ", text)
    s = _URL_RE.sub("the link", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*\|.*\|\s*$", " ", s, flags=re.M)
    s = re.sub(r"[*_~>#|]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        cut = s[:limit]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        s = (cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "…") + " The rest is in the chat."
    if had_code and "chat" not in s.lower():
        s = (s + " I've put the details in the chat.").strip()
    return s


class Orchestrator:
    def __init__(self, config: dict, broadcast: Broadcast, has_clients: Callable[[], bool], context_watcher=None):
        self.config = config
        self.broadcast = broadcast
        self.has_clients = has_clients
        self.context_watcher = context_watcher
        self.tools = computer_tools.build_registry(config)
        self.system_prompt = build_system_prompt(config)
        self.provider_name = config.get("llm_provider", "claude")
        self._providers: dict = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._always_allowed: set[str] = set()
        self._lock = asyncio.Lock()
        self._current: asyncio.Task | None = None

    # -- providers ----------------------------------------------------------

    def _provider(self, name: str):
        if name not in self._providers:
            if name == "claude":
                self._providers[name] = ClaudeProvider(
                    self.config, secret("ANTHROPIC_API_KEY"), self.system_prompt, self.tools
                )
            elif name == "ollama":
                self._providers[name] = OllamaProvider(self.config, self.system_prompt, self.tools)
            else:
                raise ValueError(f"Unknown provider '{name}'")
        return self._providers[name]

    def set_provider(self, name: str):
        if name not in ("claude", "ollama"):
            raise ValueError(name)
        self.provider_name = name

    def reset(self):
        for p in self._providers.values():
            p.reset()
        self._always_allowed.clear()

    # -- confirmation -------------------------------------------------------

    async def _confirm(self, tool, args: dict) -> bool:
        if tool.name in self._always_allowed:
            return True
        if not self.has_clients():
            return False  # nobody watching → never run risky actions unattended
        cid = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        summary = tool.describe(args) if tool.describe else f"{tool.name}({json.dumps(args)[:400]})"
        await self.broadcast({"type": "confirm_request", "id": cid, "tool": tool.name, "summary": summary})
        try:
            approved, always = await asyncio.wait_for(fut, CONFIRM_TIMEOUT_S)
        except asyncio.TimeoutError:
            approved, always = False, False
            await self.broadcast({"type": "confirm_closed", "id": cid})
        finally:
            self._pending.pop(cid, None)
        if approved and always:
            self._always_allowed.add(tool.name)
        return approved

    def resolve_confirm(self, cid: str, approved: bool, always: bool = False):
        fut = self._pending.get(cid)
        if fut and not fut.done():
            fut.set_result((bool(approved), bool(always)))

    async def _execute(self, name: str, args: dict) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"Unknown tool '{name}'."}
        await self.broadcast({"type": "tool_start", "tool": name, "args": _preview_args(args)})
        if tool.needs_confirm(args) and not await self._confirm(tool, args):
            res = {"error": "The user declined this action (or nobody was there to approve it)."}
        else:
            res = await asyncio.to_thread(computer_tools.run_tool, tool, args)
        await self.broadcast({
            "type": "tool_result", "tool": name, "ok": "error" not in res,
            "summary": _preview_result(res),
            "image": res.get("_image_b64") if name == "take_screenshot" else None,
        })
        return res

    # -- turns --------------------------------------------------------------

    def _context_block(self) -> str:
        if not self.context_watcher:
            return ""
        snap = self.context_watcher.snapshot()
        if not any(snap.values()):
            return ""
        return (
            "<desktop_context>\n"
            f"active_window: {snap.get('active_window')!r}\n"
            f"clipboard_preview: {snap.get('clipboard_preview')!r}\n"
            "</desktop_context>\n\n"
        )

    async def handle_user_message(self, text: str, source: str = "text"):
        text = (text or "").strip()
        if not text:
            return
        await self.broadcast({"type": "user_message", "text": text, "source": source})
        async with self._lock:
            self._current = asyncio.current_task()
            try:
                await self._run(text)
            except asyncio.CancelledError:
                await self.broadcast({"type": "assistant_done", "text": "(stopped)", "speech": ""})
            finally:
                self._current = None

    async def _run(self, text: str):
        content = self._context_block() + text
        await self.broadcast({"type": "thinking", "on": True, "provider": self.provider_name})
        order = [self.provider_name]
        if self.provider_name == "claude" and self.config.get("fallback_to_ollama", True):
            order.append("ollama")
        final, last_err = None, None
        try:
            for name in order:
                try:
                    provider = self._provider(name)
                    if name != self.provider_name:
                        await self.broadcast({"type": "notice", "text": f"Falling back to local model ({name}): {last_err}"})
                    final = await provider.run_turn(content, self.broadcast, self._execute)
                    break
                except ProviderUnavailable as e:
                    last_err = str(e)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # model/API error — report, don't crash
                    last_err = f"{type(e).__name__}: {e}"
                    break
        finally:
            await self.broadcast({"type": "thinking", "on": False})
        if final is None:
            final = f"Sorry, I couldn't reach my brain just now. ({last_err})"
        await self.broadcast({"type": "assistant_done", "text": final, "speech": to_speech(final)})

    def cancel(self):
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result((False, False))
        if self._current and not self._current.done():
            self._current.cancel()


def _preview_args(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        if isinstance(s, str) and len(s) > 200:
            s = s[:200] + "…"
        out[k] = s
    return out


def _preview_result(res: dict) -> str:
    clean = {k: v for k, v in res.items() if not k.startswith("_")}
    s = json.dumps(clean, ensure_ascii=False, default=str)
    return s if len(s) < 400 else s[:400] + "…"
