"""
tools.py — the actions Shiv is allowed to take on your computer.

Every function here is a "tool" the local model can call. Each one is kept
small and single-purpose, and the risky ones (running a command, deleting
something) go through a confirmation callback before they actually do
anything — see shiv.py for how that confirmation dialog is wired up.

Nothing here permanently deletes a file: move_to_trash() moves things into
a Shiv_Trash folder next to wherever they were, so you can get them back.
"""

import os
import re
import shutil
import subprocess
import fnmatch
from pathlib import Path

import pyperclip

try:
    import pygetwindow as gw
except Exception:
    gw = None


# ---------------------------------------------------------------------------
# Tool schemas — this is what gets sent to the model so it knows what it can
# call and what arguments each tool takes. Follows the OpenAI-style function
# calling format that Ollama's /api/chat endpoint understands.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the files and folders inside a directory on this computer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or ~-relative folder path to list."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text contents of a file (best for text/code/config files; not images or binaries).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to read."},
                    "max_chars": {"type": "integer", "description": "Maximum characters to return (default 8000)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new text file, or overwrite an existing one, with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to write to."},
                    "content": {"type": "string", "description": "Text content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for files under a folder whose name matches a pattern (e.g. '*.pdf', 'invoice*').",
            "parameters": {
                "type": "object",
                "properties": {
                    "root": {"type": "string", "description": "Folder to search under."},
                    "pattern": {"type": "string", "description": "Filename glob pattern to match, e.g. '*.docx'."},
                    "max_results": {"type": "integer", "description": "Cap on number of matches (default 50)."},
                },
                "required": ["root", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_path",
            "description": "Open a file or launch an application/executable using the OS default handler.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File, folder, or executable path to open/launch."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command on this computer and return its output. "
                "Use this for anything list_dir/open_path/search_files can't do directly. "
                "The user will be asked to confirm before this actually runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The exact command to run."}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_to_trash",
            "description": (
                "Remove a file or folder by moving it to a local Shiv_Trash folder (never a permanent delete). "
                "The user will be asked to confirm before this happens."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or folder to remove."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clipboard",
            "description": "Read the current text on the clipboard.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_window",
            "description": "Get the title of the application window currently focused on screen.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def list_dir(path: str) -> dict:
    p = _expand(path)
    if not p.exists():
        return {"error": f"'{p}' does not exist."}
    if not p.is_dir():
        return {"error": f"'{p}' is not a folder."}
    entries = []
    for child in sorted(p.iterdir()):
        try:
            entries.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size_bytes": child.stat().st_size if child.is_file() else None,
            })
        except OSError:
            continue
    return {"path": str(p), "entries": entries}


def read_file(path: str, max_chars: int = 8000) -> dict:
    p = _expand(path)
    if not p.exists() or not p.is_file():
        return {"error": f"'{p}' is not a readable file."}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": f"Could not read '{p}': {e}"}
    truncated = len(text) > max_chars
    return {"path": str(p), "content": text[:max_chars], "truncated": truncated}


def write_file(path: str, content: str, _overwrite_confirmed: bool = False, confirm=None) -> dict:
    p = _expand(path)
    if p.exists() and confirm is not None and not _overwrite_confirmed:
        ok = confirm(f"Shiv wants to OVERWRITE the existing file:\n\n{p}\n\nAllow this?")
        if not ok:
            return {"error": "User declined to overwrite the file."}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": str(p), "bytes_written": len(content.encode("utf-8"))}


def search_files(root: str, pattern: str, max_results: int = 50) -> dict:
    r = _expand(root)
    if not r.exists() or not r.is_dir():
        return {"error": f"'{r}' is not a folder."}
    matches = []
    for dirpath, dirnames, filenames in os.walk(r):
        for name in filenames:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                matches.append(str(Path(dirpath) / name))
                if len(matches) >= max_results:
                    return {"root": str(r), "pattern": pattern, "matches": matches, "truncated": True}
    return {"root": str(r), "pattern": pattern, "matches": matches, "truncated": False}


def open_path(path: str) -> dict:
    p = _expand(path)
    if not p.exists():
        return {"error": f"'{p}' does not exist."}
    try:
        os.startfile(str(p))  # Windows-only: launches with the default handler
        return {"opened": str(p)}
    except Exception as e:
        return {"error": f"Could not open '{p}': {e}"}


def run_command(command: str, confirm=None) -> dict:
    if confirm is not None:
        ok = confirm(f"Shiv wants to RUN this command:\n\n{command}\n\nAllow this?")
        if not ok:
            return {"error": "User declined to run this command."}
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=60
        )
        return {
            "command": command,
            "exit_code": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out after 60 seconds."}
    except Exception as e:
        return {"error": f"Could not run command: {e}"}


def move_to_trash(path: str, trash_folder_name: str = "Shiv_Trash", confirm=None) -> dict:
    p = _expand(path)
    if not p.exists():
        return {"error": f"'{p}' does not exist."}
    if confirm is not None:
        ok = confirm(f"Shiv wants to REMOVE (move to trash):\n\n{p}\n\nAllow this?")
        if not ok:
            return {"error": "User declined to remove this item."}
    trash_dir = p.parent / trash_folder_name
    trash_dir.mkdir(exist_ok=True)
    dest = trash_dir / p.name
    counter = 1
    while dest.exists():
        dest = trash_dir / f"{p.stem}_{counter}{p.suffix}"
        counter += 1
    shutil.move(str(p), str(dest))
    return {"moved_to": str(dest)}


def get_clipboard() -> dict:
    try:
        return {"clipboard": pyperclip.paste()}
    except Exception as e:
        return {"error": f"Could not read clipboard: {e}"}


def get_active_window() -> dict:
    if gw is None:
        return {"error": "pygetwindow is not available."}
    try:
        win = gw.getActiveWindow()
        return {"active_window_title": win.title if win else None}
    except Exception as e:
        return {"error": f"Could not read active window: {e}"}


# Tools that need the confirm() callback threaded through
_NEEDS_CONFIRM = {"write_file", "run_command", "move_to_trash"}

_DISPATCH = {
    "list_dir": list_dir,
    "read_file": read_file,
    "write_file": write_file,
    "search_files": search_files,
    "open_path": open_path,
    "run_command": run_command,
    "move_to_trash": move_to_trash,
    "get_clipboard": get_clipboard,
    "get_active_window": get_active_window,
}


def dispatch(name: str, args: dict, confirm=None) -> dict:
    """Run a tool by name with the given arguments. `confirm` is a callable
    confirm(message: str) -> bool, used only by tools that need a yes/no
    before doing something risky."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"Unknown tool '{name}'."}
    try:
        if name in _NEEDS_CONFIRM:
            return fn(**args, confirm=confirm)
        return fn(**args)
    except TypeError as e:
        return {"error": f"Bad arguments for '{name}': {e}"}
    except Exception as e:
        return {"error": f"'{name}' failed: {e}"}
