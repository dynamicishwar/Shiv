"""
computer_tools.py — the Computer Control Toolkit.

Combines Shiv's original tools (tools.py: files, commands, clipboard, ...)
with new ones for system metrics (psutil), processes, screenshots and
mouse/keyboard control (pyautogui).

Every tool has a risk level:
  - "auto"    : runs without asking (read-only / harmless)
  - "confirm" : the user must approve in the UI before it runs
  - callable  : decides per call, e.g. open_path only asks for executables
The confirmation itself happens in orchestrator.py, not here.
"""

from __future__ import annotations

import base64
import io
import os
import platform
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psutil

import tools as shiv_tools  # Shiv's original toolset (D:\Shiv\tools.py)

try:
    import pyautogui

    pyautogui.FAILSAFE = True  # slam the mouse into a screen corner to abort
    pyautogui.PAUSE = 0.05
except Exception:  # headless / not installed
    pyautogui = None

try:
    import mss
except Exception:
    mss = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., dict]
    risk: Any = "auto"  # "auto" | "confirm" | Callable[[dict], bool]
    describe: Callable[[dict], str] | None = None  # human text for the confirm dialog

    def needs_confirm(self, args: dict) -> bool:
        if callable(self.risk):
            return bool(self.risk(args))
        return self.risk == "confirm"


_state: dict = {"shot_scale": 1.0, "shot_origin": (0, 0)}

EXECUTABLE_EXT = {".exe", ".bat", ".cmd", ".ps1", ".msi", ".vbs", ".js", ".jse", ".wsf", ".scr", ".com", ".lnk", ".reg"}


# ---------------------------------------------------------------------------
# New tool implementations
# ---------------------------------------------------------------------------

def system_metrics() -> dict:
    vm = psutil.virtual_memory()
    out = {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "cpu_count": psutil.cpu_count(),
        "ram_used_gb": round(vm.used / 1e9, 2),
        "ram_total_gb": round(vm.total / 1e9, 2),
        "ram_percent": vm.percent,
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "os": f"{platform.system()} {platform.release()}",
        "disks": [],
    }
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
            out["disks"].append({"mount": part.mountpoint, "free_gb": round(u.free / 1e9, 1), "percent": u.percent})
        except Exception:
            continue
    bat = getattr(psutil, "sensors_battery", lambda: None)()
    if bat:
        out["battery"] = {"percent": bat.percent, "plugged_in": bat.power_plugged}
    return out


def quick_metrics() -> dict:
    """Cheap, non-blocking version for the UI's HUD."""
    vm = psutil.virtual_memory()
    return {"cpu": psutil.cpu_percent(interval=None), "ram": vm.percent}


def list_processes(sort_by: str = "memory", limit: int = 15, name_contains: str = "") -> dict:
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            info = p.info
            if name_contains and name_contains.lower() not in (info["name"] or "").lower():
                continue
            procs.append({
                "pid": info["pid"],
                "name": info["name"],
                "memory_mb": round((info["memory_info"].rss if info["memory_info"] else 0) / 1e6, 1),
                "cpu_percent": info["cpu_percent"],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    key = "cpu_percent" if sort_by == "cpu" else "memory_mb"
    procs.sort(key=lambda d: d[key] or 0, reverse=True)
    return {"processes": procs[: max(1, min(limit, 100))]}


def kill_process(pid: int) -> dict:
    try:
        p = psutil.Process(pid)
        name = p.name()
        p.terminate()
        try:
            p.wait(timeout=5)
        except psutil.TimeoutExpired:
            p.kill()
        return {"terminated": pid, "name": name}
    except psutil.NoSuchProcess:
        return {"error": f"No process with pid {pid}."}
    except psutil.AccessDenied:
        return {"error": f"Access denied terminating pid {pid}."}


def take_screenshot(monitor: int = 1, max_width: int = 1280) -> dict:
    if mss is None:
        return {"error": "Screenshots need the 'mss' package."}
    from PIL import Image

    with mss.mss() as sct:
        mons = sct.monitors
        idx = monitor if 0 <= monitor < len(mons) else 1
        mon = mons[idx]
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    scale = 1.0
    if img.width > max_width:
        scale = max_width / img.width
        img = img.resize((max_width, int(img.height * scale)))
    _state["shot_scale"] = scale
    _state["shot_origin"] = (mon["left"], mon["top"])
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return {
        "width": img.width,
        "height": img.height,
        "note": "Coordinates for mouse tools are in THIS image's pixel space.",
        "_image_b64": base64.b64encode(buf.getvalue()).decode(),
    }


def _to_screen(x: float, y: float) -> tuple[int, int]:
    s = _state["shot_scale"] or 1.0
    ox, oy = _state["shot_origin"]
    return int(ox + x / s), int(oy + y / s)


def _need_gui() -> dict | None:
    if pyautogui is None:
        return {"error": "Mouse/keyboard control needs the 'pyautogui' package."}
    return None


def mouse_click(x: float, y: float, button: str = "left", clicks: int = 1) -> dict:
    if (e := _need_gui()):
        return e
    sx, sy = _to_screen(x, y)
    pyautogui.click(sx, sy, clicks=max(1, min(clicks, 3)), button=button)
    return {"clicked": [sx, sy], "button": button, "clicks": clicks}


def mouse_move(x: float, y: float) -> dict:
    if (e := _need_gui()):
        return e
    sx, sy = _to_screen(x, y)
    pyautogui.moveTo(sx, sy, duration=0.15)
    return {"moved_to": [sx, sy]}


def scroll(amount: int, x: float | None = None, y: float | None = None) -> dict:
    if (e := _need_gui()):
        return e
    if x is not None and y is not None:
        sx, sy = _to_screen(x, y)
        pyautogui.scroll(int(amount), x=sx, y=sy)
    else:
        pyautogui.scroll(int(amount))
    return {"scrolled": amount}


def type_text(text: str) -> dict:
    if (e := _need_gui()):
        return e
    # pyautogui.write can't type non-ASCII; paste those via clipboard instead.
    if all(ord(c) < 128 for c in text):
        pyautogui.write(text, interval=0.01)
    else:
        import pyperclip

        old = pyperclip.paste()
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.2)
        pyperclip.copy(old)
    return {"typed_chars": len(text)}


def press_keys(keys: list[str]) -> dict:
    if (e := _need_gui()):
        return e
    keys = [k.lower() for k in keys]
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)
    return {"pressed": "+".join(keys)}


def open_url(url: str) -> dict:
    if not url.lower().startswith(("http://", "https://")):
        return {"error": "Only http(s) URLs are allowed."}
    webbrowser.open(url)
    return {"opened": url}


def list_windows() -> dict:
    try:
        import pygetwindow as gw

        titles = [t for t in gw.getAllTitles() if t.strip()]
        return {"windows": titles[:60]}
    except Exception as e:
        return {"error": f"Could not list windows: {e}"}


def focus_window(title_contains: str) -> dict:
    try:
        import pygetwindow as gw

        for w in gw.getAllWindows():
            if title_contains.lower() in (w.title or "").lower():
                if w.isMinimized:
                    w.restore()
                w.activate()
                return {"focused": w.title}
        return {"error": f"No window title contains '{title_contains}'."}
    except Exception as e:
        return {"error": f"Could not focus window: {e}"}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def _open_path_risky(args: dict) -> bool:
    p = str(args.get("path", ""))
    return Path(os.path.expandvars(os.path.expanduser(p))).suffix.lower() in EXECUTABLE_EXT


def _wrap_shiv(name: str):
    def call(**kwargs):
        # Confirmation already happened in the orchestrator, so pass confirm=None.
        return shiv_tools.dispatch(name, kwargs, confirm=None)
    return call


def build_registry(config: dict) -> dict[str, Tool]:
    reg: dict[str, Tool] = {}

    # Shiv's originals, with risk levels attached
    shiv_risk = {
        "list_dir": "auto",
        "read_file": "auto",
        "search_files": "auto",
        "get_clipboard": "auto",
        "get_active_window": "auto",
        "open_path": _open_path_risky,
        "write_file": "confirm",
        "run_command": "confirm",
        "move_to_trash": "confirm",
    }
    shiv_desc = {
        "write_file": lambda a: f"Write file:\n{a.get('path')}\n({len(a.get('content', ''))} characters)",
        "run_command": lambda a: f"Run command:\n{a.get('command')}",
        "move_to_trash": lambda a: f"Move to trash:\n{a.get('path')}",
        "open_path": lambda a: f"Launch program:\n{a.get('path')}",
    }
    for spec in shiv_tools.TOOLS:
        fn = spec["function"]
        name = fn["name"]
        reg[name] = Tool(
            name=name,
            description=fn["description"],
            parameters=fn.get("parameters") or _obj({}),
            fn=_wrap_shiv(name),
            risk=shiv_risk.get(name, "confirm"),
            describe=shiv_desc.get(name),
        )

    new = [
        Tool("system_metrics", "Get CPU, RAM, disk, battery and uptime for this computer.", _obj({}), system_metrics),
        Tool(
            "list_processes",
            "List running processes sorted by memory or CPU.",
            _obj({
                "sort_by": {"type": "string", "enum": ["memory", "cpu"]},
                "limit": {"type": "integer"},
                "name_contains": {"type": "string", "description": "Optional filter on process name."},
            }),
            list_processes,
        ),
        Tool(
            "kill_process", "Terminate a running process by PID.",
            _obj({"pid": {"type": "integer"}}, ["pid"]), kill_process, "confirm",
            lambda a: f"Terminate process PID {a.get('pid')}",
        ),
        Tool(
            "take_screenshot",
            "Capture the screen so you can see what is on it. Use before clicking. Returns an image.",
            _obj({"monitor": {"type": "integer", "description": "1 = primary monitor (default), 0 = all monitors."}}),
            take_screenshot,
        ),
        Tool(
            "mouse_click",
            "Click at (x, y) given in the pixel space of the most recent screenshot.",
            _obj({
                "x": {"type": "number"}, "y": {"type": "number"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer", "description": "1 or 2 (double-click)."},
            }, ["x", "y"]),
            mouse_click, "confirm",
            lambda a: f"{a.get('button', 'left').title()}-click {a.get('clicks', 1)}x at ({a.get('x')}, {a.get('y')})",
        ),
        Tool("mouse_move", "Move the mouse to (x, y) in screenshot pixel space.",
             _obj({"x": {"type": "number"}, "y": {"type": "number"}}, ["x", "y"]), mouse_move),
        Tool(
            "scroll", "Scroll the mouse wheel. Positive = up, negative = down.",
            _obj({"amount": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"}}, ["amount"]),
            scroll, "confirm", lambda a: f"Scroll {a.get('amount')}",
        ),
        Tool(
            "type_text", "Type text into whatever currently has keyboard focus.",
            _obj({"text": {"type": "string"}}, ["text"]), type_text, "confirm",
            lambda a: f"Type text:\n{a.get('text', '')[:300]}",
        ),
        Tool(
            "press_keys", "Press a key or a key combination, e.g. ['ctrl','s'] or ['enter'].",
            _obj({"keys": {"type": "array", "items": {"type": "string"}}}, ["keys"]),
            press_keys, "confirm", lambda a: f"Press keys: {'+'.join(a.get('keys', []))}",
        ),
        Tool("open_url", "Open a web page in the default browser.",
             _obj({"url": {"type": "string"}}, ["url"]), open_url),
        Tool("list_windows", "List the titles of open windows.", _obj({}), list_windows),
        Tool("focus_window", "Bring a window to the front by (part of) its title.",
             _obj({"title_contains": {"type": "string"}}, ["title_contains"]), focus_window),
    ]
    for t in new:
        reg[t.name] = t

    # User overrides from avatar_config.json
    for name in config.get("auto_approve_tools", []):
        if name in reg:
            reg[name].risk = "auto"
    for name in config.get("always_confirm_tools", []):
        if name in reg:
            reg[name].risk = "confirm"
    return reg


def run_tool(tool: Tool, args: dict) -> dict:
    try:
        return tool.fn(**args)
    except TypeError as e:
        return {"error": f"Bad arguments for '{tool.name}': {e}"}
    except Exception as e:
        return {"error": f"'{tool.name}' failed: {type(e).__name__}: {e}"}
