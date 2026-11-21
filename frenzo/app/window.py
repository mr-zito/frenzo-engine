"""Frenzo.exe: the Frenzo desktop window.

Built into a real Windows program (Frenzo.exe, with the Frenzo icon) by  run_windows.bat app .
It starts the Frenzo engine in the background if it isn't running yet (that part lives in the
system tray, next to the clock, and keeps clipping when the window is closed), then shows the app in
its own native window. Only uses the standard library + pywebview, so the .exe stays small.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

PORT = 8765
URL = f"http://127.0.0.1:{PORT}/"
IS_WIN = os.name == "nt"


def engine_root():
    """The frenzo-engine folder: the one that contains frenzo/__main__.py (searched upwards)."""
    start = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    d = start
    for _ in range(5):
        if os.path.isfile(os.path.join(d, "frenzo", "__main__.py")):
            return d
        d = os.path.dirname(d)
    return None


def _req(path, method="GET", timeout=1.5):
    r = urllib.request.Request(URL + path.lstrip("/"), method=method, data=b"{}" if method == "POST" else None,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def server_up():
    try:
        return _req("api/ping").get("app") == "frenzo"
    except Exception:
        return False


def start_server(root):
    """Start the engine + app server in the background (no console window), detached from this window."""
    pyw = os.path.join(root, ".venv", "Scripts", "pythonw.exe") if IS_WIN else os.path.join(root, ".venv", "bin", "python")
    if not os.path.exists(pyw):
        pyw = sys.executable if not getattr(sys, "frozen", False) else None
    if not pyw:
        raise RuntimeError("The Python environment (.venv) wasn't found. Run run_windows.bat app once.")
    flags = 0
    if IS_WIN:
        flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen([pyw, "-m", "frenzo", "app", "--no-window", "--tray"], cwd=root, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)


SPLASH = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#070707;color:#f3f2ec;font-family:'Segoe UI',system-ui,sans-serif}
body{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px}
.b{font:800 64px/1 'Montserrat','Segoe UI Black','Arial Black',sans-serif;color:#FFD60A;letter-spacing:-.04em}
.s{color:#8c8b84;font-size:14px;letter-spacing:.2em;text-transform:uppercase}
.bar{width:180px;height:3px;background:#1d1d1d;border-radius:3px;overflow:hidden}
.bar i{display:block;height:100%;width:40%;background:#FFD60A;border-radius:3px;animation:m 1.1s ease-in-out infinite}
@keyframes m{0%{margin-left:-40%}100%{margin-left:100%}}
.e{color:#ff5a4f;max-width:520px;text-align:center;font-size:14px;line-height:1.5}
</style></head><body><div class="b">frenzo.</div><div class="s">%MSG%</div>%EXTRA%</body></html>"""


def splash(msg, extra='<div class="bar"><i></i></div>'):
    return SPLASH.replace("%MSG%", msg).replace("%EXTRA%", extra)


def main():
    import webview

    root = engine_root()
    up = server_up()
    if up:
        # already open? bring that window to the front instead of opening a second one
        try:
            if _req("api/window/focus", "POST").get("window_open"):
                return
        except Exception:
            pass

    window = webview.create_window(
        "Frenzo", html=splash("starting" if not up else "opening"), width=1440, height=920,
        min_size=(1080, 700), background_color="#070707", text_select=True, maximized=True)
    state = {"loaded": False}

    def boot():
        try:
            if not server_up():
                if not root:
                    raise RuntimeError("Frenzo.exe must stay inside the frenzo-engine folder.")
                start_server(root)
                end = time.time() + 90
                while time.time() < end and not server_up():
                    time.sleep(0.5)
                if not server_up():
                    raise RuntimeError("The Frenzo engine didn't start. Open app_window.log in the frenzo-engine "
                                       "folder to see why, or run run_windows.bat app once.")
            window.load_url(URL)
            state["loaded"] = True
        except Exception as e:
            window.load_html(splash("couldn't start", f'<div class="e">{str(e)}</div>'))
            return
        # heartbeat: tells the engine a window is open; brings this window to the front when asked;
        # closes the window if Frenzo was quit (from the app's Quit button or the tray icon)
        fails = 0
        while True:
            time.sleep(1.5)
            try:
                r = _req("api/window/alive", "POST")
                fails = 0
                if r.get("focus"):
                    try:
                        window.maximize()   # brought back full window, not shrunk to 1440x920
                        window.show()
                        window.on_top = True
                        window.on_top = False
                    except Exception:
                        pass
            except Exception:
                fails += 1
                if fails >= 4:
                    try:
                        window.destroy()
                    except Exception:
                        pass
                    return

    webview.start(boot, private_mode=False,
                  storage_path=os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Frenzo", "webview"))
    os._exit(0)     # window closed: the engine keeps running in the tray


if __name__ == "__main__":
    main()
