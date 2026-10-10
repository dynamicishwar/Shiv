"""
shell.py — the Desktop Shell (PyQt6 + Qt WebEngine).

Hosts the UI (avatar video over WebRTC + chat) in a Chromium view, lives in
the system tray so it's always available, and can start with Windows.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Must be set before QtWebEngine starts.
# --disable-features=AsyncDns makes the embedded Chromium use Windows' own DNS
# resolver (its built-in one failed to resolve LiveKit's *.host.livekit.cloud).
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--autoplay-policy=no-user-gesture-required --disable-features=AsyncDns,DnsOverHttps",
)
import logging

from PyQt6.QtCore import QUrl, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon

INSTANCE_KEY = "ShivAvatar-" + (os.environ.get("USERNAME") or "user")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def make_icon(color="#5865f2") -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white"))
    p.setFont(QFont("Segoe UI", 26, QFont.Weight.Bold))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "S")
    p.end()
    return QIcon(pm)


class LocalOnlyPage(QWebEnginePage):
    """Keeps the view on our local UI; real links open in the normal browser."""

    def __init__(self, origin: str, parent=None):
        super().__init__(parent)
        self.origin = origin

    def javaScriptConsoleMessage(self, level, message, line, source):
        logging.getLogger("ui").info("js: %s (%s:%s)", message, source.rsplit("/", 1)[-1], line)

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if not is_main_frame or url.toString().startswith(self.origin):
            return True
        QDesktopServices.openUrl(url)
        return False


class MainWindow(QMainWindow):
    def __init__(self, url: str, origin: str, title: str, on_top: bool):
        super().__init__()
        self.setWindowTitle(title)
        self.setWindowIcon(make_icon())
        self.resize(440, 780)
        self.view = QWebEngineView(self)
        self.page = LocalOnlyPage(origin, self.view)
        self.view.setPage(self.page)
        s = self.page.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        self.setCentralWidget(self.view)
        self.view.setUrl(QUrl(url))
        self.set_on_top(on_top)
        self._quitting = False
        self._told_tray = False

    def set_on_top(self, on: bool):
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if visible:
            self.show()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
            return
        event.ignore()  # closing hides to tray; Quit is in the tray menu
        self.hide()
        if not self._told_tray and hasattr(self, "tray"):
            self.tray.showMessage(self.windowTitle(), "Still running in the tray. Ctrl+Alt+Space to talk.", self.windowIcon(), 3000)
            self._told_tray = True

    def summon(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()


# ---------------------------------------------------------------------------
# Edge "app mode" window (default). Qt WebEngine's WebRTC couldn't resolve
# LiveKit's media servers on Windows, while Edge handles them fine.
# ---------------------------------------------------------------------------

def find_edge() -> str | None:
    import shutil

    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return shutil.which("msedge")


class EdgeWindow:
    """Same interface as MainWindow, but the UI runs in an Edge --app window
    with its own private profile (so it never touches your normal Edge)."""

    def __init__(self, edge: str, url: str, title: str, on_top: bool, profile_dir: Path):
        self.edge, self.url, self.title = edge, url, title
        self.on_top = on_top
        self.profile_dir = profile_dir
        self.proc = None
        self.log = logging.getLogger("shell")

    def _hwnd(self):
        try:
            import pygetwindow as gw

            for w in gw.getWindowsWithTitle(self.title):
                if w.title.strip() == self.title:
                    return w
        except Exception:
            pass
        return None

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def launch(self):
        import subprocess

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            self.edge,
            f"--app={self.url}",
            f"--user-data-dir={self.profile_dir}",
            "--window-size=460,820",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            "--autoplay-policy=no-user-gesture-required",
        ]
        self.log.info("launching Edge app window")
        self.proc = subprocess.Popen(args)
        if self.on_top:
            QTimer.singleShot(2500, lambda: self.set_on_top(True))

    def show(self):
        self.summon()

    def summon(self):
        w = self._hwnd()
        if w is None:
            if not self._alive():
                self.launch()
            return
        try:
            if w.isMinimized:
                w.restore()
            w.activate()
        except Exception:
            pass

    def set_on_top(self, on: bool):
        self.on_top = on
        w = self._hwnd()
        if w is None:
            return
        try:
            import win32con
            import win32gui

            flag = win32con.HWND_TOPMOST if on else win32con.HWND_NOTOPMOST
            win32gui.SetWindowPos(w._hWnd, flag, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        except Exception as e:
            self.log.warning("always-on-top failed: %s", e)

    def reload(self):
        w = self._hwnd()
        if w is not None:
            try:
                w.activate()
                import pyautogui

                pyautogui.press("f5")
            except Exception:
                pass

    def close(self):
        if self._alive():
            self.proc.terminate()


def _autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "ShivAvatar")
            return True
    except OSError:
        return False


def _set_autostart(on: bool, script: Path):
    if sys.platform != "win32":
        return
    import winreg

    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "ShivAvatar", 0, winreg.REG_SZ, f'"{exe}" "{script}" --minimized')
        else:
            try:
                winreg.DeleteValue(k, "ShivAvatar")
            except OSError:
                pass


def make_app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Shiv")
    return app


def already_running() -> bool:
    """If Shiv is already running, ask it to show itself and return True."""
    make_app()
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        return True
    return False


def run_shell(config: dict, url: str, origin: str, stop_backend, script: Path, start_minimized: bool,
              before_quit=None) -> int:
    app = make_app()
    QLocalServer.removeServer(INSTANCE_KEY)
    server = QLocalServer()
    server.listen(INSTANCE_KEY)

    name = config.get("assistant_name", "Shiv")
    on_top = bool(config.get("always_on_top", False))
    edge = find_edge() if config.get("ui_mode", "edge") == "edge" and sys.platform == "win32" else None
    if edge:
        win = EdgeWindow(edge, url, f"{name} Assistant", on_top, script.parent / ".edge_profile")
    else:
        if config.get("ui_mode", "edge") == "edge":
            logging.getLogger("shell").warning("Edge not found; using the built-in Qt window")
        win = MainWindow(url, origin, name, on_top)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), win.summon()))

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip(name)
    win.tray = tray
    menu = QMenu()
    act_show = QAction(f"Open {name}", menu, triggered=win.summon)
    act_top = QAction("Always on top", menu, checkable=True, checked=bool(config.get("always_on_top", False)))
    act_top.toggled.connect(win.set_on_top)
    act_auto = QAction("Start with Windows", menu, checkable=True, checked=_autostart_enabled())
    act_auto.toggled.connect(lambda on: _set_autostart(on, script))
    act_auto.setEnabled(sys.platform == "win32")
    act_reload = QAction("Reload UI", menu, triggered=(win.reload if edge else win.view.reload))
    act_browser = QAction("Open in web browser", menu, triggered=lambda: QDesktopServices.openUrl(QUrl(url)))

    def quit_all():
        win._quitting = True
        if before_quit:
            before_quit()  # tells the UI to close the paid avatar session
        if not edge:
            win.page.runJavaScript("window.shivShutdown && window.shivShutdown()")
        tray.hide()
        QTimer.singleShot(1500, lambda: (win.close() if edge else None, stop_backend(), app.quit()))

    act_quit = QAction("Quit", menu, triggered=quit_all)
    for a in (act_show, act_top, act_auto, act_reload, act_browser):
        menu.addAction(a)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: win.summon() if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None
    )
    tray.show()

    if not start_minimized:
        win.show()
    return app.exec()


def wait_for_backend(port: int, timeout: float = 20) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False
