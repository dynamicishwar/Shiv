"""
shiv_avatar.py — start Shiv as a 24/7 talking avatar assistant.

    python shiv_avatar.py              # desktop app (tray + avatar window)
    python shiv_avatar.py --minimized  # start hidden in the tray
    python shiv_avatar.py --no-ui      # backend only; prints a URL to open in a browser

The original text-only Shiv (shiv.py) still works unchanged.
"""

import argparse
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # so `import tools`, `context_watcher` resolve

# Under pythonw.exe (Maya.exe / run_avatar.bat) there's no console: stdout and
# stderr are None, which crashes libraries that try to print. Give them a sink.
NO_CONSOLE = sys.stdout is None
if sys.stdout is None or sys.stderr is None:
    import os

    _null = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _null
    sys.stderr = sys.stderr or _null

if sys.platform == "win32":
    try:  # make screenshots and mouse coordinates use real pixels on HiDPI screens
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def setup_logging():
    import logging
    from logging.handlers import RotatingFileHandler

    log_dir = HERE / "logs"
    log_dir.mkdir(exist_ok=True)
    handlers = [RotatingFileHandler(log_dir / "shiv_avatar.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
    if not NO_CONSOLE:  # pythonw has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main():
    ap = argparse.ArgumentParser(description="Shiv avatar assistant")
    ap.add_argument("--no-ui", action="store_true", help="run backend only")
    ap.add_argument("--minimized", action="store_true", help="start in the tray")
    args = ap.parse_args()
    setup_logging()

    from avatar.config import load_config

    config = load_config()
    host, port = "127.0.0.1", int(config.get("server_port", 8765))

    import logging

    logging.getLogger("main").info("starting (ui=%s)", not args.no_ui)
    if not args.no_ui:
        from avatar import shell

        if shell.already_running():
            logging.getLogger("main").info("another Shiv is already running; asked it to show itself")
            return 0

    import uvicorn

    from avatar.server import create_app

    app, backend = create_app(config)
    origin = f"http://{host}:{port}"
    url = f"{origin}/?t={backend.token}"
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", log_config=None))

    if args.no_ui:
        print(f"\n  {config.get('assistant_name', 'Shiv')} backend running. Open this URL in Chrome/Edge:\n\n  {url}\n")
        server.run()
        return 0

    t = threading.Thread(target=server.run, daemon=True, name="backend")
    t.start()
    if not shell.wait_for_backend(port):
        logging.getLogger("main").error("Backend didn't start on port %s. Is another copy running, or the port taken?", port)
        return 1

    def stop_backend():
        server.should_exit = True

    def before_quit():
        import asyncio

        if backend.loop:
            asyncio.run_coroutine_threadsafe(backend.broadcast({"type": "shutdown"}), backend.loop)

    return shell.run_shell(config, url, origin, stop_backend, Path(__file__).resolve(),
                           args.minimized or bool(config.get("start_minimized", False)), before_quit)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback

        (HERE / "logs").mkdir(exist_ok=True)
        with open(HERE / "logs" / "crash.log", "a", encoding="utf-8") as f:
            f.write("\n" + traceback.format_exc())
        raise
