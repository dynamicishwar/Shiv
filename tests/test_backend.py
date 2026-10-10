"""Backend tests — run with:  python -m pytest tests -q   (no API keys or mic needed)"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from avatar import computer_tools  # noqa: E402
from avatar.config import DEFAULTS  # noqa: E402
from avatar.llm import ClaudeProvider  # noqa: E402
from avatar.orchestrator import Orchestrator, to_speech  # noqa: E402


def cfg(**kw):
    c = dict(DEFAULTS)
    c.update(kw)
    return c


# ---------------------------------------------------------------- speech text
def test_to_speech_strips_markdown_and_code():
    s = to_speech("Here you go:\n```python\nprint(1)\n```\nSee **docs** at https://x.y/z")
    assert "print" not in s and "**" not in s and "https" not in s
    assert "chat" in s.lower()


def test_to_speech_truncates_long_text():
    s = to_speech("This is a sentence. " * 100, limit=200)
    assert len(s) < 260 and s.endswith("The rest is in the chat.")


# ---------------------------------------------------------------- risk policy
def test_risk_levels():
    reg = computer_tools.build_registry(cfg())
    assert not reg["read_file"].needs_confirm({"path": "a.txt"})
    assert reg["run_command"].needs_confirm({"command": "dir"})
    assert reg["mouse_click"].needs_confirm({"x": 1, "y": 2})
    assert reg["open_path"].needs_confirm({"path": "C:/x/setup.exe"})
    assert not reg["open_path"].needs_confirm({"path": "C:/x/notes.txt"})


def test_config_overrides():
    reg = computer_tools.build_registry(cfg(auto_approve_tools=["scroll"], always_confirm_tools=["read_file"]))
    assert not reg["scroll"].needs_confirm({"amount": 3})
    assert reg["read_file"].needs_confirm({"path": "a"})


def test_system_metrics_runs():
    m = computer_tools.system_metrics()
    assert "cpu_percent" in m and "ram_percent" in m


# ---------------------------------------------------------------- orchestrator
class FakeProvider:
    """Calls one tool, then answers."""
    name = "fake"

    def __init__(self, tool, args):
        self.tool, self.args, self.seen = tool, args, None

    def reset(self):
        pass

    async def run_turn(self, text, emit, execute):
        self.seen = await execute(self.tool, self.args)
        await emit({"type": "assistant_delta", "text": "done"})
        return "done"


def make_orch(tool, args, clients=True):
    events = []

    async def bc(e):
        events.append(e)

    o = Orchestrator(cfg(), bc, lambda: clients)
    fake = FakeProvider(tool, args)
    o._providers["claude"] = fake
    return o, fake, events


@pytest.mark.asyncio
async def test_confirm_denied_when_no_ui():
    o, fake, events = make_orch("run_command", {"command": "echo hi"}, clients=False)
    await o.handle_user_message("run it")
    assert "declined" in fake.seen["error"]
    assert events[-1]["type"] == "assistant_done"


@pytest.mark.asyncio
async def test_confirm_approve_and_always():
    o, fake, events = make_orch("get_clipboard", {}, clients=True)
    o.tools["get_clipboard"].risk = "confirm"
    o.tools["get_clipboard"].fn = lambda: {"clipboard": "hello"}

    async def approver():
        while True:
            await asyncio.sleep(0.01)
            req = [e for e in events if e["type"] == "confirm_request"]
            if req:
                o.resolve_confirm(req[0]["id"], True, always=True)
                return

    await asyncio.gather(o.handle_user_message("clip?"), approver())
    assert fake.seen == {"clipboard": "hello"}
    # second time: "always" means no new prompt
    n = len([e for e in events if e["type"] == "confirm_request"])
    await o.handle_user_message("again")
    assert len([e for e in events if e["type"] == "confirm_request"]) == n


@pytest.mark.asyncio
async def test_cancel_stops_turn():
    o, fake, events = make_orch("run_command", {"command": "x"}, clients=True)

    async def canceller():
        while not any(e["type"] == "confirm_request" for e in events):
            await asyncio.sleep(0.01)
        o.cancel()

    await asyncio.gather(o.handle_user_message("go"), canceller())
    assert fake.seen is None  # tool never ran
    assert any(e["type"] == "assistant_done" and e["text"] == "(stopped)" for e in events)


@pytest.mark.asyncio
async def test_fallback_to_ollama_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    events = []

    async def bc(e):
        events.append(e)

    o = Orchestrator(cfg(ollama_url="http://127.0.0.1:9"), bc, lambda: True)
    await o.handle_user_message("hi")
    done = [e for e in events if e["type"] == "assistant_done"][0]
    assert "Ollama" in done["text"]  # both unavailable → friendly error naming Ollama
    assert any(e["type"] == "notice" and "Falling back" in e["text"] for e in events)


# ---------------------------------------------------------------- Claude tool loop (mocked API)
class _Block(SimpleNamespace):
    def model_dump(self, **_):
        return {k: v for k, v in vars(self).items()}


class _Stream:
    def __init__(self, msg, texts):
        self.msg, self.texts = msg, texts

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for t in self.texts:
                yield SimpleNamespace(type="text", text=t)
        return gen()

    async def get_final_message(self):
        return self.msg


@pytest.mark.asyncio
async def test_claude_loop_with_screenshot_tool():
    reg = computer_tools.build_registry(cfg())
    p = ClaudeProvider(cfg(), "sk-test", "sys", reg)
    calls = []
    script = [
        (SimpleNamespace(stop_reason="tool_use", content=[
            _Block(type="text", text="Looking."),
            _Block(type="tool_use", id="tu1", name="take_screenshot", input={}),
        ]), ["Looking."]),
        (SimpleNamespace(stop_reason="end_turn", content=[_Block(type="text", text="I see a desktop.")]), ["I see a desktop."]),
    ]

    def stream(**kw):
        calls.append(json.loads(json.dumps(kw["messages"])))
        return _Stream(*script[len(calls) - 1])

    p.client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    emitted = []

    async def emit(e):
        emitted.append(e)

    async def execute(name, args):
        return {"width": 10, "height": 10, "_image_b64": "iVBORw0KGgo="}

    out = await p.run_turn("what's on screen?", emit, execute)
    assert out == "I see a desktop."
    tr = calls[1][-1]["content"][0]
    assert tr["type"] == "tool_result" and tr["content"][0]["type"] == "image"
    assert "_image_b64" not in tr["content"][1]["text"]
    assert [e["type"] for e in emitted].count("assistant_delta") == 2
    assert len(p.history) == 4


@pytest.mark.asyncio
async def test_claude_rollback_on_error():
    p = ClaudeProvider(cfg(), "sk-test", "sys", computer_tools.build_registry(cfg()))

    def stream(**kw):
        raise RuntimeError("boom")

    p.client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    with pytest.raises(RuntimeError):
        await p.run_turn("hi", None, None)
    assert p.history == []


# ---------------------------------------------------------------- server security
def test_ws_requires_token_and_origin():
    from fastapi.testclient import TestClient
    from avatar.server import create_app

    app, backend = create_app(cfg(), token="secret", enable_devices=False)
    with TestClient(app) as c:
        assert c.get("/api/state").status_code == 403
        assert c.get("/api/state", headers={"x-shiv-token": "secret"}).status_code == 200
        assert c.get("/api/state", headers={"x-shiv-token": "secret", "origin": "https://evil.example"}).status_code == 403
        with pytest.raises(Exception):
            with c.websocket_connect("/ws?t=wrong") as ws:
                ws.receive_text()
        with c.websocket_connect("/ws?t=secret") as ws:
            first = json.loads(ws.receive_text())
            assert first["type"] == "state" and first["assistant_name"] == "Shiv"
