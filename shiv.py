"""
shiv.py — entry point. Run this with `python shiv.py`.

Shiv lives in your system tray. Click the icon (or right-click > Open Chat)
to talk to it. It uses a local Ollama model as its brain, and a small set of
tools (tools.py) to read/write/search files, open things, run commands, and
notice what you're doing (context_watcher.py). Running a command or removing
a file always asks you to confirm first, in a normal Windows dialog box.
"""

import json
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from context_watcher import ContextWatcher
from agent import ShivAgent

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def make_tray_icon_image() -> Image.Image:
    """A simple generated icon so Shiv doesn't need an external image file."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=(88, 101, 242, 255))
    draw.text((20, 18), "S", fill=(255, 255, 255, 255))
    return img


class ShivApp:
    def __init__(self):
        self.config = load_config()

        # Tkinter root — kept hidden; the chat window is a Toplevel opened on demand.
        self.root = tk.Tk()
        self.root.withdraw()

        self.chat_window = None
        self.chat_text = None
        self.chat_entry = None

        self.context_watcher = ContextWatcher(self.config.get("context_poll_seconds", 3))
        self.context_watcher.start()

        # Confirmation dialogs are requested from a worker thread but must be
        # shown on the Tkinter main thread — this queue+event pair bridges that.
        self._confirm_queue: "queue.Queue[str]" = queue.Queue()
        self._confirm_result = threading.Event()
        self._confirm_answer = False
        self.root.after(100, self._poll_confirm_queue)

        self.agent = ShivAgent(self.config, self.context_watcher, self._request_confirm)

        self.icon = pystray.Icon(
            self.config.get("app_name", "Shiv"),
            make_tray_icon_image(),
            self.config.get("app_name", "Shiv"),
            menu=pystray.Menu(
                pystray.MenuItem("Open Chat", self._on_open_chat_clicked, default=True),
                pystray.MenuItem("Quit", self._on_quit_clicked),
            ),
        )

    # -- confirmation bridge (worker thread -> main thread) -----------------

    def _request_confirm(self, message: str) -> bool:
        """Called from the agent's worker thread. Blocks that thread until
        the user answers a Yes/No dialog shown on the main thread."""
        self._confirm_result.clear()
        self._confirm_queue.put(message)
        self._confirm_result.wait(timeout=120)
        return self._confirm_answer

    def _poll_confirm_queue(self):
        try:
            message = self._confirm_queue.get_nowait()
        except queue.Empty:
            message = None
        if message is not None:
            self._confirm_answer = messagebox.askyesno("Shiv wants to do something", message)
            self._confirm_result.set()
        self.root.after(150, self._poll_confirm_queue)

    # -- tray callbacks (run on pystray's own thread) ------------------------

    def _on_open_chat_clicked(self, icon=None, item=None):
        self.root.after(0, self._open_chat_window)

    def _on_quit_clicked(self, icon=None, item=None):
        self.context_watcher.stop()
        self.icon.stop()
        self.root.after(0, self.root.destroy)

    # -- chat window (main thread only) --------------------------------------

    def _open_chat_window(self):
        if self.chat_window is not None and tk.Toplevel.winfo_exists(self.chat_window):
            self.chat_window.deiconify()
            self.chat_window.lift()
            return

        win = tk.Toplevel(self.root)
        win.title(self.config.get("app_name", "Shiv"))
        win.geometry("480x560")
        win.protocol("WM_DELETE_WINDOW", win.withdraw)  # hide, don't destroy — keeps history

        text = scrolledtext.ScrolledText(win, wrap=tk.WORD, state=tk.DISABLED)
        text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))

        entry_frame = tk.Frame(win)
        entry_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        entry = tk.Entry(entry_frame)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda e: self._on_send(entry, text))

        send_btn = tk.Button(entry_frame, text="Send", command=lambda: self._on_send(entry, text))
        send_btn.pack(side=tk.LEFT, padx=(6, 0))

        self.chat_window = win
        self.chat_entry = entry
        self.chat_text = text

        self._append_chat(f"{self.config.get('app_name', 'Shiv')} is ready. Ask me anything.\n")
        entry.focus_set()

    def _append_chat(self, line: str):
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.insert(tk.END, line + "\n")
        self.chat_text.configure(state=tk.DISABLED)
        self.chat_text.see(tk.END)

    def _on_send(self, entry: tk.Entry, text: tk.Text):
        user_text = entry.get().strip()
        if not user_text:
            return
        entry.delete(0, tk.END)
        self._append_chat(f"You: {user_text}")
        self._append_chat("Shiv is thinking…")

        def worker():
            reply = self.agent.handle_message(user_text)
            self.root.after(0, lambda: self._show_reply(reply))

        threading.Thread(target=worker, daemon=True).start()

    def _show_reply(self, reply: str):
        # Replace the "thinking…" placeholder line with the real reply.
        self.chat_text.configure(state=tk.NORMAL)
        content = self.chat_text.get("1.0", tk.END)
        if content.rstrip("\n").endswith("Shiv is thinking…"):
            last_newline = content.rstrip("\n").rfind("\n")
            self.chat_text.delete("1.0", tk.END)
            self.chat_text.insert(tk.END, content[: last_newline + 1] if last_newline != -1 else "")
        self.chat_text.configure(state=tk.DISABLED)
        self._append_chat(f"Shiv: {reply}")

    # -- lifecycle ------------------------------------------------------------

    def run(self):
        # pystray's icon needs its own loop; run it in a background thread so
        # Tkinter's mainloop can own the main thread (required on Windows).
        threading.Thread(target=self.icon.run, daemon=True).start()
        self.root.mainloop()


if __name__ == "__main__":
    ShivApp().run()
