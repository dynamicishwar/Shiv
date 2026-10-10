"""
llm.py — LLM providers for the orchestrator.

Both providers expose the same interface:

    await provider.run_turn(user_text, emit, execute) -> final reply text

  emit(event: dict)            async; streams UI events (text deltas etc.)
  execute(name, args) -> dict  async; runs a tool (with confirmation) and
                               returns its result dict. A result may carry
                               "_image_b64" (PNG) for screenshots.

Each provider keeps its own conversation history. If a turn fails or is
cancelled half-way, history is rolled back so it never ends in a dangling
tool call.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable

import httpx

Emit = Callable[[dict], Awaitable[None]]
Execute = Callable[[str, dict], Awaitable[dict]]


class ProviderUnavailable(Exception):
    """Provider can't be used at all right now (no key, offline, ...)."""


def _result_text(result: dict) -> str:
    clean = {k: v for k, v in result.items() if not k.startswith("_")}
    text = json.dumps(clean, ensure_ascii=False, default=str)
    return text if len(text) <= 12000 else text[:12000] + " …(truncated)"


def _trim(history: list, max_msgs: int, is_turn_start: Callable[[dict], bool]) -> None:
    """Drop the oldest messages, but only cut at the start of a user turn so
    tool_use/tool_result pairs are never split."""
    if len(history) <= max_msgs:
        return
    cut = len(history) - max_msgs
    while cut < len(history) and not is_turn_start(history[cut]):
        cut += 1
    del history[:cut]


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

class ClaudeProvider:
    name = "claude"

    def __init__(self, config: dict, api_key: str, system_prompt: str, tools: dict):
        if not api_key:
            raise ProviderUnavailable("ANTHROPIC_API_KEY is not set in D:\\Shiv\\.env")
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2, timeout=90)
        self.config = config
        self.system_prompt = system_prompt
        self.tool_specs = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools.values()
        ]
        self.history: list[dict] = []

    def reset(self):
        self.history.clear()

    @staticmethod
    def _is_turn_start(msg: dict) -> bool:
        if msg["role"] != "user":
            return False
        c = msg["content"]
        return isinstance(c, str) or not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in c
        )

    def _prune_old_images(self):
        """Keep only the most recent screenshot in history (they're expensive)."""
        seen_latest = False
        for msg in reversed(self.history):
            if msg["role"] != "user" or isinstance(msg["content"], str):
                continue
            for block in msg["content"]:
                if block.get("type") != "tool_result" or not isinstance(block.get("content"), list):
                    continue
                if any(b.get("type") == "image" for b in block["content"]):
                    if seen_latest:
                        block["content"] = [b for b in block["content"] if b.get("type") != "image"] + [
                            {"type": "text", "text": "[older screenshot removed to save context]"}
                        ]
                    seen_latest = True

    async def run_turn(self, user_text: str, emit: Emit, execute: Execute) -> str:
        start_len = len(self.history)
        self.history.append({"role": "user", "content": user_text})
        try:
            return await self._loop(emit, execute)
        except BaseException as e:
            del self.history[start_len:]  # roll back half-finished turn
            a = self._anthropic
            if isinstance(e, (a.AuthenticationError, a.PermissionDeniedError, a.APIConnectionError)):
                raise ProviderUnavailable(f"Claude unavailable: {e}") from e
            raise

    async def _loop(self, emit: Emit, execute: Execute) -> str:
        max_steps = int(self.config.get("max_agent_steps", 12))
        for step in range(max_steps):
            _trim(self.history, int(self.config.get("max_history_messages", 40)), self._is_turn_start)
            self._prune_old_images()
            if step:
                await emit({"type": "assistant_break"})
            async with self.client.messages.stream(
                model=self.config["claude_model"],
                max_tokens=int(self.config.get("claude_max_tokens", 2048)),
                system=self.system_prompt,
                messages=self.history,
                tools=self.tool_specs,
                cache_control={"type": "ephemeral"},
            ) as stream:
                async for ev in stream:
                    if ev.type == "text":
                        await emit({"type": "assistant_delta", "text": ev.text})
                msg = await stream.get_final_message()

            self.history.append({
                "role": "assistant",
                "content": [b.model_dump(mode="json", exclude_none=True) for b in msg.content],
            })

            tool_uses = [b for b in msg.content if b.type == "tool_use"]
            if msg.stop_reason != "tool_use" or not tool_uses:
                return "".join(b.text for b in msg.content if b.type == "text").strip()

            results = []
            for tu in tool_uses:
                res = await execute(tu.name, dict(tu.input or {}))
                block: dict = {"type": "tool_result", "tool_use_id": tu.id}
                if res.get("_image_b64"):
                    block["content"] = [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": res["_image_b64"]}},
                        {"type": "text", "text": _result_text(res)},
                    ]
                else:
                    block["content"] = _result_text(res)
                if "error" in res:
                    block["is_error"] = True
                results.append(block)
            self.history.append({"role": "user", "content": results})

        return "I took quite a few steps without finishing — want me to keep going?"


# ---------------------------------------------------------------------------
# Ollama (local)
# ---------------------------------------------------------------------------

class OllamaProvider:
    name = "ollama"

    def __init__(self, config: dict, system_prompt: str, tools: dict):
        self.config = config
        self.system_prompt = system_prompt
        self.tool_specs = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools.values()
        ]
        self.history: list[dict] = []

    def reset(self):
        self.history.clear()

    @staticmethod
    def _is_turn_start(msg: dict) -> bool:
        return msg["role"] == "user"

    async def _chat(self, messages: list) -> dict:
        url = self.config["ollama_url"].rstrip("/") + "/api/chat"
        payload = {"model": self.config["ollama_model"], "messages": messages, "tools": self.tool_specs, "stream": False}
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(url, json=payload)
        except httpx.ConnectError as e:
            raise ProviderUnavailable(f"Can't reach Ollama at {self.config['ollama_url']} — is it running?") from e
        if resp.status_code >= 400:
            raise RuntimeError(f"Ollama returned {resp.status_code}: {resp.text[:1500]}")
        return resp.json()

    async def run_turn(self, user_text: str, emit: Emit, execute: Execute) -> str:
        start_len = len(self.history)
        self.history.append({"role": "user", "content": user_text})
        try:
            max_steps = int(self.config.get("max_agent_steps", 12))
            for _ in range(max_steps):
                _trim(self.history, int(self.config.get("max_history_messages", 40)), self._is_turn_start)
                data = await self._chat([{"role": "system", "content": self.system_prompt}] + self.history)
                msg = data.get("message", {})
                calls = msg.get("tool_calls") or []
                self.history.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
                if not calls:
                    text = (msg.get("content") or "").strip() or "(no response)"
                    await emit({"type": "assistant_delta", "text": text})
                    return text
                if msg.get("content"):
                    await emit({"type": "assistant_delta", "text": msg["content"]})
                    await emit({"type": "assistant_break"})
                for call in calls:
                    fn = call.get("function", {})
                    raw = fn.get("arguments", {})
                    args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                    res = await execute(fn.get("name", ""), args)
                    if res.get("_image_b64"):
                        res = {**res, "note": "Screenshot captured, but this local model can't see images."}
                    self.history.append({"role": "tool", "content": _result_text(res)})
            return "I went through several steps without a final answer — try rephrasing?"
        except BaseException:
            del self.history[start_len:]
            raise
