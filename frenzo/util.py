import datetime as _dt
import os
import re
import shutil
import subprocess
import sys
import threading

_print_lock = threading.Lock()

# Programs started from the windowless Frenzo app would each pop up an empty console/terminal tab on Windows
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW


def log(msg, who="engine"):
    stamp = _dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] [{who}] {msg}"
    with _print_lock:
        print(line, flush=True)
        path = os.environ.get("FRENZO_LOG")   # optional copy of the output in a file
        if path:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    print(line, file=f)
            except OSError:
                pass


def require_binary(name):
    path = shutil.which(name)
    if not path:
        sys.exit(
            f"ERROR: '{name}' was not found. Install it and make sure it is on your PATH "
            f"(see README, step 1)."
        )
    return path


def run(cmd, cwd=None, capture=False, input_bytes=None):
    """Run a command; raise with a readable message if it fails."""
    res = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_bytes,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
    )
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace")[-1500:]
        raise RuntimeError(f"Command failed: {' '.join(map(str, cmd[:6]))} ...\n{err}")
    return res.stdout if capture else None


def slugify(text, max_len=40):
    text = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return (text[:max_len].rstrip("-")) or "clip"


def hex_to_ass(hex_color, alpha=0):
    """'#RRGGBB' -> ASS '&HAABBGGRR'."""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def fmt_ts(seconds):
    """Seconds -> ASS timestamp H:MM:SS.cc"""
    seconds = max(0.0, seconds)
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def youtube_cookies():
    """frenzo-engine/youtube_cookies.txt (exported from a signed-in YouTube session) if it's there, else None.
    YouTube blocks downloads that aren't signed in ("Sign in to confirm you're not a bot"); the downloader uses
    this file to look signed in. It holds a YouTube login: keep it private, never share or zip it."""
    from .config import ROOT
    p = ROOT / "youtube_cookies.txt"
    try:
        return str(p) if p.is_file() and p.stat().st_size > 50 else None
    except OSError:
        return None
