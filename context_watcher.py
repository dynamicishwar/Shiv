"""
context_watcher.py — a quiet background thread that keeps track of what
you're currently doing (active window title + clipboard text), so Shiv can
answer "what am I working on" style questions or use that as context without
you having to explain it every time.

Nothing here is written to disk or sent anywhere on its own — it's just kept
in memory and handed to the agent as part of its context for your next
message.
"""

import threading
import time

import pyperclip

try:
    import pygetwindow as gw
except Exception:
    gw = None


class ContextWatcher:
    def __init__(self, poll_seconds: float = 3.0):
        self.poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._active_window = None
        self._clipboard = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                win_title = None
                if gw is not None:
                    win = gw.getActiveWindow()
                    win_title = win.title if win else None
                clip = pyperclip.paste()
            except Exception:
                win_title, clip = None, None
            with self._lock:
                self._active_window = win_title
                self._clipboard = clip
            self._stop.wait(self.poll_seconds)

    def snapshot(self) -> dict:
        """A short, current picture of what the user's doing, for the model's context."""
        with self._lock:
            clip = self._clipboard
            win = self._active_window
        if clip and len(clip) > 500:
            clip = clip[:500] + " …(truncated)"
        return {"active_window": win, "clipboard_preview": clip}
