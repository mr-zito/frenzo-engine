"""Frenzo app: a local window (black & yellow) to run live clipping, clip past streams and manage clips.

Runs only on your PC (http://127.0.0.1:8765). The engine itself runs as background jobs started from
here; their output is shown in the app instead of a terminal. Closing the window keeps live clipping
running; open the Frenzo shortcut again to get the window back, or use "Quit Frenzo" in the app.
"""
import base64
import collections
import datetime as dt
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from .. import campaign
from ..config import ROOT, load_config

STATIC = Path(__file__).parent / "static"
FONTS = ROOT / "assets" / "fonts"
IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0
LINE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[([^\]]+)\] (.*)$")


# ----------------------------------------------------------------------------------------- helpers
def _python():
    """python.exe (not pythonw.exe) so engine jobs get a hidden console, not a flashing window."""
    exe = sys.executable
    if IS_WIN and exe.lower().endswith("pythonw.exe"):
        cand = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(cand):
            return cand
    return exe


def _fix_path():
    """Launched from the Desktop shortcut the venv isn't 'activated': make tools findable."""
    parts = [os.path.dirname(sys.executable)]
    if IS_WIN:
        parts.append(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"))
        parts.append(os.path.expandvars(r"%APPDATA%\npm"))
    parts.append(os.path.expanduser("~/.local/bin"))
    cur = os.environ.get("PATH", "")
    for p in parts:
        if p and os.path.isdir(p) and p not in cur:
            cur = p + os.pathsep + cur
    os.environ["PATH"] = cur
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONUNBUFFERED"] = "1"


def _kill_tree(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _open_path(path, select=False):
    try:
        if IS_WIN:
            if select:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if select else "", path] if select else ["open", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) if select else path])
    except Exception:
        pass


def _cid(rel):
    return base64.urlsafe_b64encode(rel.replace("\\", "/").encode("utf-8")).decode().rstrip("=")


def _uncid(cid):
    pad = "=" * (-len(cid) % 4)
    return base64.urlsafe_b64decode(cid + pad).decode("utf-8")


# -------------------------------------------------------------------------------------------- logs
class LogBuffer:
    def __init__(self, keep=6000):
        self.lines = collections.deque(maxlen=keep)
        self.seq = 0
        self.lock = threading.Lock()
        self.file = None

    def open_file(self, folder):
        try:
            os.makedirs(folder, exist_ok=True)
            self.file = open(os.path.join(folder, ".frenzo_app.log"), "a", encoding="utf-8")
        except OSError:
            self.file = None

    def add(self, job, text):
        text = text.rstrip("\r\n")
        if not text.strip():
            return
        m = LINE.match(text)
        t, who, msg = (m.group(1), m.group(2), m.group(3)) if m else (dt.datetime.now().strftime("%H:%M:%S"), "", text)
        low = msg.lower()
        level = ("saved" if ("saved clip" in low or "re-edited" in low or "ready" in low and "your cut" in low
                             or "re-edit done" in low) else
                 "moment" if "moment flagged" in low else
                 "live" if low.startswith("live:") or "recording now" in low else
                 "error" if any(w in low for w in ("error", "failed", "traceback", "exception", "couldn't",
                                                    "could not", "not found", "missing")) else
                 "info")
        with self.lock:
            self.seq += 1
            self.lines.append({"seq": self.seq, "t": t, "job": job, "who": who, "text": msg, "level": level})
        if level == "saved" and "saved clip" in low and HOOKS["on_saved"]:
            try:
                HOOKS["on_saved"](who, msg)
            except Exception:
                pass
        with self.lock:
            if self.file:
                try:
                    self.file.write(f"[{dt.date.today()} {t}] [{job}] [{who}] {msg}\n")
                    self.file.flush()
                except OSError:
                    pass

    def since(self, seq, job=None, limit=400):
        with self.lock:
            out = [l for l in self.lines if l["seq"] > seq and (job is None or l["job"] == job)]
        return out[-limit:]


LOGS = LogBuffer()
HOOKS = {"on_saved": None, "on_quit": None}          # set by the tray icon
WINDOW = {"seen": 0.0, "focus": False}               # the Frenzo.exe window's heartbeat


# -------------------------------------------------------------------------------------------- jobs
class Job:
    _n = 0

    def __init__(self, kind, label, args, target=None):
        Job._n += 1
        self.id = f"{kind}{Job._n}"
        self.kind, self.label, self.args, self.target = kind, label, args, target
        self.status = "queued"
        self.created = time.time()
        self.started = self.ended = None
        self.proc = None
        self.returncode = None
        self.last = ""

    def public(self):
        return {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
                "created": self.created, "started": self.started, "ended": self.ended,
                "target": self.target, "last": self.last}


LONG_KINDS = ("past",)      # hours-long jobs: they get their own lane so your edits never wait behind them


class Jobs:
    """One live-clipping process + two lanes of other tasks, each run one at a time:
    the long lane (past streams) and the quick lane (your cuts, re-edits, learning, everything else)."""

    def __init__(self, cfg_path, cfg):
        self.cfg_path, self.cfg = cfg_path, cfg
        self.live = None
        self.live_started_cfg_mtime = 0.0
        self.queue = []
        self.history = collections.deque(maxlen=40)
        self.lock = threading.Lock()
        self.wakes = {"quick": threading.Event(), "long": threading.Event()}
        self.stop_file = os.path.join(cfg["storage"]["output_dir"], ".stop_live")
        for lane in self.wakes:
            threading.Thread(target=self._worker, args=(lane,), daemon=True).start()

    @staticmethod
    def _lane(job):
        return "long" if job.kind in LONG_KINDS else "quick"

    def _spawn(self, job, extra_env=None):
        cmd = [_python(), "-u", "-m", "frenzo", "--config", self.cfg_path] + job.args
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1", **(extra_env or {}))
        env.pop("ANTHROPIC_API_KEY", None)       # always use the Claude plan, never paid API credits
        kw = {"creationflags": NO_WINDOW} if IS_WIN else {"start_new_session": True}
        job.proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env, **kw)
        job.status, job.started = "running", time.time()
        LOGS.add(job.id, f"[{time.strftime('%H:%M:%S')}] [app] started: {job.label}")

        def reader():
            for raw in iter(job.proc.stdout.readline, b""):
                text = raw.decode("utf-8", "replace")
                LOGS.add(job.id, text)
                m = LINE.match(text.strip())
                job.last = (m.group(3) if m else text.strip())[:200] or job.last
            job.proc.wait()
            job.returncode = job.proc.returncode
            job.ended = time.time()
            if job.status in ("stopping", "cancelled"):
                job.status = "stopped"
            else:
                job.status = "done" if job.returncode == 0 else "failed"
            LOGS.add(job.id, f"[{time.strftime('%H:%M:%S')}] [app] {job.label}: {job.status}")
        threading.Thread(target=reader, daemon=True).start()

    # ---- live ----
    def live_running(self):
        return self.live is not None and self.live.status in ("running", "stopping")

    def start_live(self):
        with self.lock:
            if self.live_running():
                return self.live
            job = Job("live", "Live clipping", ["run"])
            try:
                os.remove(self.stop_file)
            except OSError:
                pass
            self._spawn(job, {"FRENZO_STOP_FILE": self.stop_file})
            self.live = job
            self.live_started_cfg_mtime = _mtime(self.cfg_path)
            self.history.appendleft(job)
            return job

    def stop_live(self, wait=45):
        job = self.live
        if not job or job.status != "running":
            return
        job.status = "stopping"
        LOGS.add(job.id, f"[{time.strftime('%H:%M:%S')}] [app] stopping live clipping (finishing recordings)...")
        try:
            os.makedirs(os.path.dirname(self.stop_file), exist_ok=True)
            open(self.stop_file, "w").close()
        except OSError:
            _kill_tree(job.proc)

        def watchdog():
            end = time.time() + wait
            while time.time() < end and job.proc.poll() is None:
                time.sleep(1)
            _kill_tree(job.proc)
        threading.Thread(target=watchdog, daemon=True).start()

    # ---- queue ----
    def add(self, kind, label, args, target=None):
        job = Job(kind, label, args, target)
        with self.lock:
            self.queue.append(job)
            self.history.appendleft(job)
        self.wakes[self._lane(job)].set()
        return job

    def cancel(self, jid):
        with self.lock:
            for job in list(self.queue):
                if job.id == jid:
                    self.queue.remove(job)
                    job.status = "cancelled"
                    return True
            for job in self.history:
                if job.id == jid and job.status == "running" and job.kind != "live":
                    job.status = "cancelled"
                    _kill_tree(job.proc)
                    return True
        if self.live and self.live.id == jid:
            self.stop_live()
            return True
        return False

    def _worker(self, lane):
        wake = self.wakes[lane]
        while True:
            wake.wait(2)
            wake.clear()
            while True:
                with self.lock:
                    job = next((j for j in self.queue if self._lane(j) == lane), None)
                    if job:
                        self.queue.remove(job)
                if not job:
                    break
                self._spawn(job)
                job.proc.wait()
                while job.status == "running":   # reader thread sets the final status
                    time.sleep(0.2)

    def add_target_alias(self, job, target):
        job.aliases = getattr(job, "aliases", []) + [target]

    def busy_targets(self):
        with self.lock:
            out = {}
            for j in list(self.history):
                if j.status in ("queued", "running"):
                    for t in [j.target] + getattr(j, "aliases", []):
                        if t:
                            out[t] = j.status
            return out

    def public(self):
        with self.lock:
            return [j.public() for j in self.history]

    def shutdown(self):
        with self.lock:
            for j in self.queue:
                j.status = "cancelled"
            self.queue.clear()
            running = [j for j in self.history if j.status == "running" and j.kind != "live"]
        for j in running:
            _kill_tree(j.proc)
        if self.live_running():
            self.stop_live(wait=30)
            end = time.time() + 32
            while time.time() < end and self.live.proc.poll() is None:
                time.sleep(0.5)


def _clip_title(folder):
    try:
        with open(os.path.join(folder, "meta.json"), "r", encoding="utf-8") as f:
            m = json.load(f)
        return (m.get("title_bar") or m.get("title") or os.path.basename(folder))[:48]
    except (OSError, ValueError):
        return os.path.basename(folder)[:48]


def _said(folder):
    """What the streamer says in the clip, read back from its captions file."""
    try:
        with open(os.path.join(folder, "captions.ass"), "r", encoding="utf-8") as f:
            lines = [l.split("}", 1)[-1].strip() for l in f if l.startswith("Dialogue:") and ",Mini," in l]
        return " ".join(l for l in lines if l)
    except OSError:
        return ""


def _write_caption_txt(folder, meta, camp=None):
    m = re.search(r"starts at ([\d.]+)s", str(meta.get("raw_clip", "")))
    files = list(meta.get("files") or {}) or sorted(f for f in os.listdir(folder) if f.startswith("clip") and f.endswith(".mp4"))
    campaign.write_caption_txt(folder, meta, files, float(m.group(1)) if m else None, camp)


# ---- posting: open the platform's upload page, show the right file, copy the text ----
UPLOAD_PAGES = {
    "tiktok": "https://www.tiktok.com/tiktokstudio/upload",
    "instagram": "https://www.instagram.com/",
    "youtube": "https://www.youtube.com/upload",
    "x": "https://x.com/intent/post?text={text}",
    "facebook": "https://www.facebook.com/reels/create",
}


def file_for_platform(folder, files, platform, camp=None):
    """The version with this platform's handle (clip_tiktok+instagram.mp4 for TikTok...), else the clean one.
    camp with watermark: false -> always the clean one (the campaign doesn't allow your handle)."""
    names = sorted(f for f in os.listdir(folder) if f.startswith("clip") and f.endswith(".mp4"))
    return campaign.post_file(names, platform, camp)


def set_clipboard(text):
    """Copy text (emoji included) to the Windows clipboard."""
    if not IS_WIN:
        return False
    try:
        ps = "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())"
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], input=text.encode("utf-8"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, creationflags=NO_WINDOW)
        return True
    except Exception:
        return False


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


# ----------------------------------------------------------------------------------- config editing
def _yaml():
    from ruamel.yaml import YAML
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    y.representer.add_representer(type(None), lambda r, d: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    return y


def _take_trailing(node):
    """ruamel attaches the comment lines that FOLLOW a block (e.g. '# --- Lacy ---' before the next streamer)
    to the last key inside it: take them off (returned as text) so deleting/replacing the block keeps them."""
    from ruamel.yaml.comments import CommentedMap
    if not isinstance(node, CommentedMap) or not len(node):
        return ""
    last = list(node.keys())[-1]
    deeper = _take_trailing(node[last])
    it = node.ca.items.get(last)
    tok = it[2] if it and len(it) > 2 else None
    own = ""
    if tok is not None and "\n" in tok.value:
        i = tok.value.index("\n")
        own, tok.value = tok.value[i + 1:], tok.value[: i + 1]
        if not tok.value.strip():                 # nothing left on the key's own line
            it[2] = None
    return deeper + own


def _give_trailing(node, text):
    """Put comment lines taken with _take_trailing back after the (new) last key of a mapping."""
    from ruamel.yaml.comments import CommentedMap
    from ruamel.yaml.error import CommentMark
    from ruamel.yaml.tokens import CommentToken
    if not text or not isinstance(node, CommentedMap) or not len(node):
        return
    last = list(node.keys())[-1]
    if isinstance(node[last], CommentedMap) and len(node[last]):
        return _give_trailing(node[last], text)
    it = node.ca.items.setdefault(last, [None, None, None, None])
    while len(it) < 4:
        it.append(None)
    if it[2] is not None:
        it[2].value = it[2].value.rstrip("\n") + "\n" + text
    else:
        it[2] = CommentToken("\n" + text, CommentMark(0), None)


def _del_key(node, key):
    """Delete a key without losing the comment lines that follow it."""
    if key not in node:
        return
    keys = list(node.keys())
    text = _take_trailing(node) if keys[-1] == key else ""
    del node[key]
    _give_trailing(node, text)


def _set_key(node, key, value):
    """Set / replace a key without losing the comment lines that follow it."""
    keys = list(node.keys())
    text = _take_trailing(node) if keys and (keys[-1] == key or key not in node) else ""
    node[key] = value
    _give_trailing(node, text)


class ConfigFile:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def read(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return _yaml().load(f)

    def write(self, data):
        tmp = self.path + ".tmp"
        try:                                     # keep the file's own line endings (LF or CRLF)
            with open(self.path, "rb") as f:
                nl = "\r\n" if b"\r\n" in f.read(4096) else "\n"
        except OSError:
            nl = None
        with open(tmp, "w", encoding="utf-8", newline=nl) as f:
            _yaml().dump(data, f)
        shutil.copyfile(self.path, self.path + ".bak")
        os.replace(tmp, self.path)

    def edit(self, fn):
        with self.lock:
            data = self.read()
            result = fn(data)
            self.write(data)
            return result


def _norm_login(platform, login):
    login = str(login or "").strip()
    if platform == "youtube":
        login = re.sub(r"^https?://(www\.)?youtube\.com/", "", login.lower()).strip("/").replace("/live", "")
        return login.split("/")[-1].lstrip("@")
    return login.lower().lstrip("@")


def parse_streamer_link(text, platform_hint=None):
    """'https://twitch.tv/lacy' / 'kick.com/n3on' / 'youtube.com/@IShowSpeed' / 'lacy' -> (platform, login)."""
    t = (text or "").strip()
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", t, re.I)
    if m:
        return "twitch", m.group(1).lower()
    m = re.search(r"kick\.com/([A-Za-z0-9_\-]+)", t, re.I)
    if m:
        return "kick", m.group(1).lower()
    m = re.search(r"youtube\.com/(@[\w.\-]+|channel/UC[\w-]{22}|c/[\w.\-]+|user/[\w.\-]+)", t, re.I)
    if m:
        part = m.group(1)
        return "youtube", part if part.startswith("@") else "https://www.youtube.com/" + part
    if re.fullmatch(r"@?[\w.\-]{2,40}", t):
        return (platform_hint or "twitch"), (t if platform_hint == "youtube" else t.lstrip("@").lower())
    return None, None


def streamer_url(platform, login):
    if platform == "twitch":
        return f"https://www.twitch.tv/{login}"
    if platform == "kick":
        return f"https://kick.com/{login}"
    if str(login).startswith("http"):
        return login
    return f"https://www.youtube.com/@{str(login).lstrip('@')}"


# ------------------------------------------------------------------------------------------ clips
class ClipIndex:
    def __init__(self, root):
        self.root = root
        self.cache = {}   # meta path -> (mtime, summary)

    def folder(self, cid):
        rel = _uncid(cid)
        path = os.path.normpath(os.path.join(self.root, rel))
        if not path.startswith(os.path.normpath(self.root)) or not os.path.isfile(os.path.join(path, "meta.json")):
            raise KeyError(cid)
        return path

    def _summary(self, folder, meta):
        rel = os.path.relpath(folder, self.root).replace("\\", "/")
        cid = _cid(rel)
        names = set(os.listdir(folder))
        videos = sorted(n for n in names if n.startswith("clip") and n.endswith(".mp4"))
        main = "clip_clean.mp4" if "clip_clean.mp4" in names else (videos[0] if videos else None)
        try:
            ver = int(os.path.getmtime(os.path.join(folder, main))) if main else 0
        except OSError:
            ver = 0
        m = re.search(r"starts at ([\d.]+)s", str(meta.get("raw_clip", "")))
        cuts = meta.get("jump_cuts") or [[0, meta.get("duration") or 0]]
        span = float(cuts[-1][1]) if cuts and cuts[-1] else float(meta.get("duration") or 0)
        return {
            "id": cid, "rel": rel, "login": meta.get("login") or rel.split("/")[0],
            "streamer": meta.get("streamer") or meta.get("login") or rel.split("/")[0],
            "platform": meta.get("platform"), "title": meta.get("title_bar") or meta.get("heading") or meta.get("title") or "(no title)",
            "highlight": meta.get("highlight"), "emoji": meta.get("emoji") or "", "category": meta.get("category") or "",
            "score": meta.get("viral_score"), "duration": meta.get("duration"), "created": meta.get("created") or "",
            "posted": meta.get("posted") or {}, "what_happens": meta.get("what_happens") or "",
            "reason": meta.get("reason") or "", "flagged_by": meta.get("flagged_by") or "",
            "post_title": meta.get("title") or "", "caption": meta.get("caption") or "", "hashtags": meta.get("hashtags") or "",
            "old_title": meta.get("old_title"), "stream_title": meta.get("stream_title") or "",
            "campaign": meta.get("campaign") or "", "campaign_match": meta.get("campaign_match"),
            "nudity": bool(meta.get("nudity")), "censored": meta.get("censored"),
            "campaign_note": meta.get("campaign_note") or "",
            "user_context": meta.get("user_context") or "",
            "recording": os.path.splitext(str(meta.get("recording") or ""))[0],
            "post_texts": meta.get("post_texts") or {}, "links": meta.get("links") or {},
            "start_in_recording": meta.get("start_in_recording"),
            "videos": videos, "main": main, "has_raw": "raw.mp4" in names, "has_thumb": "thumb.jpg" in names,
            "raw_offset": float(m.group(1)) if m else 0.0, "span": span, "jump_cuts": cuts,
            "facecam": meta.get("facecam"), "focus_x": meta.get("focus_x"), "layout": meta.get("layout"),
            "version": ver, "files": meta.get("files") or {},
        }

    def list(self):
        out, seen = [], set()
        if not os.path.isdir(self.root):
            return out
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if "meta.json" not in files:
                continue
            mp = os.path.join(dirpath, "meta.json")
            seen.add(mp)
            try:
                mt = os.path.getmtime(mp) + sum(os.path.getmtime(os.path.join(dirpath, f))
                                                for f in files if f.endswith(".mp4"))
            except OSError:
                continue
            hit = self.cache.get(mp)
            if hit and hit[0] == mt:
                out.append(hit[1])
                continue
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                s = self._summary(dirpath, meta)
            except (OSError, ValueError):
                continue
            self.cache[mp] = (mt, s)
            out.append(s)
        for k in list(self.cache):
            if k not in seen:
                del self.cache[k]
        out.sort(key=lambda s: s["created"], reverse=True)
        return out


# --------------------------------------------------------------------------------------------- app
def create_app(cfg_path):
    from flask import Flask, abort, jsonify, request, send_file, send_from_directory

    cfg_path = str(Path(cfg_path) if Path(cfg_path).is_absolute() else ROOT / cfg_path)
    if not os.path.exists(cfg_path):
        example = ROOT / "config.example.yaml"
        if example.exists():
            shutil.copyfile(example, cfg_path)
    state = {"cfg": load_config(cfg_path, required=False)}
    out_dir = state["cfg"]["storage"]["output_dir"]
    LOGS.open_file(out_dir)
    jobs = Jobs(cfg_path, state["cfg"])
    conf = ConfigFile(cfg_path)
    clips = ClipIndex(out_dir)
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False

    def cfg():
        mt = _mtime(cfg_path)
        if state.get("mt") != mt:
            try:
                state["cfg"] = load_config(cfg_path, required=False)
            except SystemExit:
                pass
            state["mt"] = mt
        return state["cfg"]

    def err(msg, code=400):
        return jsonify({"ok": False, "error": msg}), code

    # ---- static ----
    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html", max_age=0)

    @app.get("/static/<path:name>")
    def static_file(name):
        return send_from_directory(STATIC, name, max_age=0)

    @app.get("/fonts/<path:name>")
    def fonts(name):
        return send_from_directory(FONTS, name, max_age=86400)

    @app.get("/api/ping")
    def ping():
        return jsonify({"app": "frenzo"})

    # ---- state & logs ----
    def live_info():
        st = {}
        try:
            with open(os.path.join(out_dir, ".live_status.json"), "r", encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            pass
        running = jobs.live_running()
        if not running or time.time() - st.get("updated", 0) > 90:
            st = dict(st, recording={} if not running else st.get("recording", {}))
        job = jobs.live
        return {"running": running, "status": job.status if job else "stopped",
                "started": job.started if job else None, "job": job.id if job else None,
                "recording": st.get("recording", {}), "queued": st.get("queued", 0),
                "restart_needed": bool(running and _mtime(cfg_path) > jobs.live_started_cfg_mtime + 0.5)}

    # ---- recordings (hourly parts of live streams) ----
    rec_dir = state["cfg"]["storage"]["recordings_dir"]
    recs = {"converting": None, "bytes": 0, "last_clean": 0.0}

    def rec_in_use():
        """Parts the live engine still writes or reads. None = unknown (engine running, status stale)."""
        if not jobs.live_running():
            return set()
        try:
            with open(os.path.join(out_dir, ".live_status.json"), "r", encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            return None
        if time.time() - st.get("updated", 0) > 30 or "in_use" not in st:
            return None
        return {os.path.normcase(os.path.abspath(p)) for p in st["in_use"]}

    def part_busy(p, busy):
        if p["ext"] != ".ts":
            return False
        if busy is None or time.time() - p["mtime"] < 120:
            return True
        return os.path.normcase(os.path.abspath(p["path"])) in busy

    def rec_worker():
        from .. import recordings as R
        while True:
            try:
                busy = rec_in_use()
                parts = R.list_parts(rec_dir)
                recs["bytes"] = sum(p["size"] for p in parts)
                for p in parts:
                    if p["ext"] == ".ts" and not part_busy(p, busy):
                        recs["converting"] = p["stem"]
                        LOGS.add("recordings", f"[{time.strftime('%H:%M:%S')}] [recordings] preparing {p['file']} "
                                                f"for the Recordings tab ({p['size'] / 1e9:.1f} GB)")
                        try:
                            R.convert(rec_dir, p)
                            LOGS.add("recordings", f"[{time.strftime('%H:%M:%S')}] [recordings] {p['stem']} is ready")
                        except Exception as e:
                            LOGS.add("recordings", f"[{time.strftime('%H:%M:%S')}] [recordings] couldn't convert "
                                                    f"{p['file']}: {str(e)[:150]}")
                            time.sleep(300)
                        finally:
                            recs["converting"] = None
                days = float(cfg()["storage"].get("auto_delete_recordings_days") or 0)
                if days > 0:
                    for p in R.list_parts(rec_dir):
                        if not part_busy(p, busy) and time.time() - (p.get("ended") or p["mtime"]) > days * 86400:
                            R.delete(rec_dir, p)
                            LOGS.add("recordings", f"[{time.strftime('%H:%M:%S')}] [recordings] deleted {p['file']} "
                                                    f"(older than {days:g} days)")
                if time.time() - recs["last_clean"] > 2 * 3600:          # leftover temp files, every 2 hours
                    recs["last_clean"] = time.time()
                    freed = R.clean_temp()
                    if freed > 50e6:
                        LOGS.add("recordings", f"[{time.strftime('%H:%M:%S')}] [cleanup] removed {freed / 1e9:.1f} GB "
                                                f"of leftover temporary files")
            except Exception as e:
                LOGS.add("recordings", f"[{time.strftime('%H:%M:%S')}] [recordings] {str(e)[:150]}")
            time.sleep(60)
    threading.Thread(target=rec_worker, daemon=True).start()

    def disk():
        try:
            os.makedirs(rec_dir, exist_ok=True)
            du = shutil.disk_usage(rec_dir)
            return {"free": du.free, "total": du.total, "recordings": recs["bytes"]}
        except OSError:
            return {}

    def rec_markers(side, all_clips):
        names = set((side.get("aliases") or {side["stem"]: 0.0}).keys()) | {side["stem"]}
        off = side.get("aliases") or {}
        out = []
        for c in all_clips:
            if c.get("recording") in names and isinstance(c.get("start_in_recording"), (int, float)):
                out.append({"id": c["id"], "title": c["title"], "emoji": c["emoji"],
                            "t": round(c["start_in_recording"] + float(off.get(c["recording"], 0.0)), 1),
                            "dur": c.get("duration") or 0})
        return out

    @app.get("/api/recordings")
    def api_recordings():
        from .. import recordings as R
        busy = rec_in_use()
        all_clips = clips.list()
        sessions = {}
        for p in R.list_parts(rec_dir):
            status = ("recording" if part_busy(p, busy) else "converting" if recs["converting"] == p["stem"]
                      else "waiting" if p["ext"] == ".ts" else "ready")
            item = {"id": _cid(p["stem"]), "stem": p["stem"], "file": p["file"], "part": p.get("part", 0),
                    "parts": p.get("parts") or [p.get("part", 0)], "t0": p.get("t0"), "ended": p.get("ended"),
                    "duration": p.get("duration") or ((p.get("ended") or p["mtime"]) - (p.get("t0") or p["mtime"])),
                    "size": p["size"], "status": status, "moments": len(p.get("moments") or []),
                    "clips": len(rec_markers(p, all_clips))}
            sess = sessions.setdefault(p.get("session") or p["stem"], {
                "session": p.get("session") or p["stem"], "platform": p.get("platform"), "login": p.get("login"),
                "display": p.get("display") or p.get("login"), "title": p.get("title") or "", "parts": []})
            if p.get("title"):
                sess["title"] = p["title"]
            sess["parts"].append(item)
        out = list(sessions.values())
        for sess in out:
            sess["parts"].sort(key=lambda x: (x["t0"] or 0))
            sess["t0"] = sess["parts"][0]["t0"] or 0
            sess["size"] = sum(x["size"] for x in sess["parts"])
        out.sort(key=lambda x: x["t0"], reverse=True)
        return jsonify({"sessions": out, "disk": disk()})

    def _part(rid):
        from .. import recordings as R
        stem = _uncid(rid)
        for p in R.list_parts(rec_dir):
            if p["stem"] == stem:
                return p
        raise KeyError(rid)

    @app.get("/api/recordings/<rid>")
    def api_recording(rid):
        try:
            p = _part(rid)
        except (KeyError, ValueError):
            return err("Recording not found", 404)
        busy = rec_in_use()
        return jsonify({"id": rid, "stem": p["stem"], "file": p["file"], "display": p.get("display"),
                        "login": p.get("login"), "platform": p.get("platform"), "title": p.get("title"),
                        "t0": p.get("t0"), "duration": p.get("duration"), "ready": p["ext"] == ".mp4",
                        "status": "recording" if part_busy(p, busy) else ("ready" if p["ext"] == ".mp4" else "waiting"),
                        "chat": p.get("chat") or {"bucket": 5, "counts": []}, "loud": p.get("loud") or [],
                        "moments": p.get("moments") or [], "clips": rec_markers(p, clips.list())})

    @app.get("/media-rec/<rid>")
    def media_rec(rid):
        try:
            p = _part(rid)
        except (KeyError, ValueError):
            abort(404)
        if p["ext"] != ".mp4":
            abort(404)
        return send_file(p["path"], conditional=True, max_age=0, mimetype="video/mp4")

    @app.delete("/api/recordings/<rid>")
    def del_recording(rid):
        from .. import recordings as R
        try:
            p = _part(rid)
        except (KeyError, ValueError):
            return err("Recording not found", 404)
        if part_busy(p, rec_in_use()) or recs["converting"] == p["stem"] or f"rec:{p['stem']}" in jobs.busy_targets():
            return err("This part is still being recorded or used. Try again when it's finished.")
        R.delete(rec_dir, p)
        return jsonify({"ok": True, "freed": p["size"]})

    @app.post("/api/recordings/merge")
    def merge_recordings():
        from .. import recordings as R
        body = request.get_json(force=True)
        try:
            a, b = _part(body["a"]), _part(body["b"])
        except (KeyError, ValueError, TypeError):
            return err("Pick two parts to merge")
        if a.get("session") != b.get("session"):
            return err("Only two consecutive parts of the same stream can be merged.")
        same = sorted((p for p in R.list_parts(rec_dir) if p.get("session") == a.get("session")),
                      key=lambda p: p.get("t0") or 0)
        idx = [x["stem"] for x in same]
        i, j = sorted((idx.index(a["stem"]), idx.index(b["stem"])))
        if j - i != 1:
            return err("Only two consecutive parts can be merged (pick parts next to each other).")
        busy = rec_in_use()
        if part_busy(a, busy) or part_busy(b, busy):
            return err("One of these parts is still being recorded. Merge them when the hour is finished.")
        first, second = same[i], same[j]
        job = jobs.add("merge", f"Merge {first['stem'][-6:]} + {second['stem'][-6:]}",
                       ["merge", first["path"], second["path"]], target=f"rec:{first['stem']}")
        jobs.add_target_alias(job, f"rec:{second['stem']}")
        return jsonify({"ok": True, "job": job.id})

    @app.post("/api/recordings/<rid>/cut")
    def cut_recording(rid):
        body = request.get_json(force=True)
        try:
            p = _part(rid)
            start, end = float(body["start"]), float(body["end"])
        except (KeyError, ValueError, TypeError):
            return err("Pick a start and an end")
        if p["ext"] != ".mp4":
            return err("This part isn't ready yet.")
        if end - start < 3:
            return err("The selection must be at least 3 seconds long")
        if end - start > 600:
            return err("The selection is longer than 10 minutes: pick a shorter moment")
        args = ["cutrec", p["path"], "--start", f"{start:.2f}", "--end", f"{end:.2f}",
                "--mode", "claude" if body.get("mode") == "claude" else "manual"]
        for k in ("title", "highlight", "emoji", "context"):
            if (body.get(k) or "").strip():
                args += [f"--{k}", str(body[k]).strip()[:800]]
        label = (body.get("title") or "").strip() or f"{p.get('display') or p.get('login')} {start / 60:.0f}:{start % 60:02.0f}"
        job = jobs.add("cut", "New clip: " + label[:48], args, target=f"rec:{p['stem']}")
        return jsonify({"ok": True, "job": job.id})

    @app.get("/api/state")
    def api_state():
        from ..ai import _find_claude_code
        if "claude" not in state:
            state["claude"] = bool(_find_claude_code())
        return jsonify({"live": live_info(), "jobs": jobs.public(), "log_seq": LOGS.seq,
                        "busy": jobs.busy_targets(), "claude": state["claude"], "output_dir": out_dir,
                        "disk": disk(), "converting": recs["converting"]})

    @app.get("/api/logs")
    def api_logs():
        return jsonify(LOGS.since(int(request.args.get("since", 0)), request.args.get("job") or None,
                                  int(request.args.get("limit", 400))))

    # ---- live ----
    @app.post("/api/live/start")
    def live_start():
        if not cfg()["streamers"]:
            return err("Add at least one streamer first.")
        job = jobs.start_live()
        return jsonify({"ok": True, "job": job.id})

    @app.post("/api/live/stop")
    def live_stop():
        jobs.stop_live()
        return jsonify({"ok": True})

    @app.post("/api/live/restart")
    def live_restart():
        def go():
            jobs.stop_live()
            end = time.time() + 50
            while jobs.live_running() and time.time() < end:
                time.sleep(0.5)
            jobs.start_live()
        threading.Thread(target=go, daemon=True).start()
        return jsonify({"ok": True})

    # ---- streamers ----
    @app.get("/api/streamers")
    def api_streamers():
        data = conf.read() or {}
        counts = collections.Counter(c["login"] for c in clips.list())
        rec = live_info()["recording"]
        out = []
        for s in data.get("streamers") or []:
            if isinstance(s, str):
                s = {"login": s}
            plat = str(s.get("platform", "twitch")).lower()
            raw = str(s.get("login", ""))
            if not raw or raw.lstrip("@").upper().startswith("PASTE_"):
                continue
            login = _norm_login(plat, raw)
            key = f"{plat}:{login}"
            camp = campaign.normalize(s.get("campaign"))
            out.append({"key": key, "platform": plat, "login": raw, "name": login,
                        "url": streamer_url(plat, raw), "hot_takes": bool(s.get("hot_takes")),
                        "any_game": bool(s.get("any_game")), "tags": list(s.get("tags") or []),
                        "clips": counts.get(login, 0), "recording": rec.get(key),
                        "campaign": camp, "has_campaign": campaign.has_rules(camp)})
        return jsonify(out)

    @app.post("/api/streamers")
    def add_streamer():
        from ruamel.yaml.comments import CommentedMap, CommentedSeq
        body = request.get_json(force=True)
        plat, login = parse_streamer_link(body.get("link", ""), body.get("platform"))
        if not plat:
            return err("Paste a link like twitch.tv/name, kick.com/name or youtube.com/@name")
        key = f"{plat}:{_norm_login(plat, login)}"

        def fn(data):
            seq = data.get("streamers")
            if seq is None:
                seq = CommentedSeq()
                data["streamers"] = seq
            for s in seq:
                if isinstance(s, dict) and f"{str(s.get('platform', 'twitch')).lower()}:{_norm_login(str(s.get('platform', 'twitch')).lower(), s.get('login'))}" == key:
                    raise ValueError("This streamer is already in your list.")
            item = CommentedMap()
            item["platform"] = plat
            item["login"] = login
            if body.get("hot_takes"):
                item["hot_takes"] = True
            if body.get("any_game"):
                item["any_game"] = True
            name = _norm_login(plat, login)
            tags = CommentedSeq([f"#{re.sub(r'[^a-z0-9_]', '', name.lower())}", "#gta6"])
            tags.fa.set_flow_style()
            item["tags"] = tags
            seq.append(item)
        try:
            conf.edit(fn)
        except ValueError as e:
            return err(str(e))
        return jsonify({"ok": True, "key": key})

    def _find_item(data, key):
        for i, s in enumerate(data.get("streamers") or []):
            if not isinstance(s, dict):
                continue
            plat = str(s.get("platform", "twitch")).lower()
            if f"{plat}:{_norm_login(plat, s.get('login'))}" == key:
                return i, s
        raise KeyError(key)

    @app.patch("/api/streamers/<path:key>")
    def edit_streamer(key):
        body = request.get_json(force=True)
        from ruamel.yaml.comments import CommentedSeq

        def fn(data):
            _, s = _find_item(data, key)
            # _set_key / _del_key keep the comment lines that follow this streamer (e.g. the next one's header)
            for flag in ("hot_takes", "any_game"):
                if flag in body:
                    if body[flag]:
                        if s.get(flag) is not True:
                            _set_key(s, flag, True)
                    else:
                        _del_key(s, flag)
            if "tags" in body:
                tags = CommentedSeq([t if t.startswith("#") else f"#{t}" for t in body["tags"] if t.strip()])
                tags.fa.set_flow_style()
                _set_key(s, "tags", tags)
            if "facecam" in body:
                if body["facecam"]:
                    from ruamel.yaml.comments import CommentedMap
                    box = CommentedMap((k, round(float(body["facecam"][k]), 4)) for k in ("x", "y", "w", "h"))
                    box.fa.set_flow_style()
                    _set_key(s, "facecam", box)
                else:
                    _del_key(s, "facecam")
            if "campaign" in body:                  # campaign rules (editing and writing only)
                camp = campaign.normalize(body["campaign"]) if body["campaign"] else None
                if not camp or not campaign.has_rules(camp):
                    _del_key(s, "campaign")
                else:
                    _set_key(s, "campaign", _campaign_yaml(camp))
        try:
            conf.edit(fn)
        except KeyError:
            return err("Streamer not found", 404)
        return jsonify({"ok": True})

    def _campaign_yaml(camp):
        """Campaign dict -> a tidy YAML block (lists on one line, the instructions as a | block)."""
        from ruamel.yaml.comments import CommentedMap, CommentedSeq
        from ruamel.yaml.scalarstring import LiteralScalarString
        m = CommentedMap()
        for k in ("name", "link", "instructions", "hashtags", "mentions", "platforms", "min_seconds", "max_seconds",
                  "watermark", "captions", "sound_effects", "overlay", "look_for", "chat_keywords", "speech_keywords"):
            v = camp[k]
            if k in ("overlay", "look_for", "chat_keywords", "speech_keywords") and v == campaign.DEFAULT[k]:
                continue                            # unused new options aren't written
            if k in ("instructions", "look_for"):
                v = LiteralScalarString(v.replace("\r\n", "\n").rstrip() + "\n") if v.strip() else ""
            elif isinstance(v, list):
                v = CommentedSeq(v)
                v.fa.set_flow_style()
            elif isinstance(v, dict):
                v = CommentedMap((a, int(b) if isinstance(b, float) and b.is_integer() else b) for a, b in v.items())
                v.fa.set_flow_style()
            elif isinstance(v, float):
                v = int(v) if v.is_integer() else v
            m[k] = v
        return m

    OVERLAY_DIR = ROOT / "assets" / "overlays"

    @app.post("/api/streamers/<path:key>/campaign-overlay")
    def upload_overlay(key):
        """The campaign's overlay image (PNG/JPG/WebP): checked, converted to PNG, kept in assets/overlays."""
        f = request.files.get("file")
        if not f:
            return err("Pick an image file")
        try:
            from PIL import Image
            img = Image.open(f.stream)
            img.load()
            if img.width < 16 or img.height < 16 or img.width * img.height > 40_000_000:
                return err("This image is too small or too big")
            img = img.convert("RGBA")
        except Exception:
            return err("This file isn't an image Frenzo can read (use PNG, JPG or WebP)")
        os.makedirs(OVERLAY_DIR, exist_ok=True)
        name = f"{re.sub(r'[^a-z0-9_-]+', '-', key.lower()).strip('-')}-{int(time.time())}.png"
        img.save(OVERLAY_DIR / name, "PNG")
        return jsonify({"ok": True, "file": f"assets/overlays/{name}", "width": img.width, "height": img.height})

    @app.get("/overlay/<path:name>")
    def overlay_file(name):
        return send_from_directory(OVERLAY_DIR, os.path.basename(name), max_age=0)

    @app.post("/api/streamers/<path:key>/apply-campaign")
    def apply_campaign(key):
        """Updates the post text of all this streamer's clips (hashtags, credit, campaign name). No re-render."""
        scfg = next((s for s in cfg()["streamers"] if s["key"] == key), None)
        if not scfg:
            return err("Streamer not found", 404)
        camp = campaign.of(scfg)
        if not camp:
            return err("This streamer has no campaign rules yet: save them first.")
        busy = jobs.busy_targets()
        changed = skipped = 0
        for c in clips.list():
            if c["login"] != scfg["login"]:
                continue
            if c["id"] in busy:
                skipped += 1
                continue
            folder = clips.folder(c["id"])
            meta = _read_meta(folder)
            if meta is None:
                continue
            if campaign.apply_to_meta(meta, camp):
                with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                changed += 1
            _write_caption_txt(folder, meta, camp)
        LOGS.add("app", f"[{time.strftime('%H:%M:%S')}] [campaign] {scfg['login']}: post text updated on {changed} clip(s)"
                        + (f", {skipped} busy clip(s) skipped" if skipped else ""))
        return jsonify({"ok": True, "changed": changed, "skipped": skipped})

    @app.delete("/api/streamers/<path:key>")
    def del_streamer(key):
        def fn(data):
            i, _ = _find_item(data, key)
            del data["streamers"][i]
        try:
            conf.edit(fn)
        except KeyError:
            return err("Streamer not found", 404)
        return jsonify({"ok": True})

    # ---- YouTube sign-in for downloads (a cookies file exported from a signed-in session) ----
    YT_COOKIES = ROOT / "youtube_cookies.txt"

    def yt_status():
        try:
            text = YT_COOKIES.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"present": False}
        rows = [l.split("\t") for l in text.splitlines() if l.strip() and not l.startswith("#") or l.startswith("#HttpOnly_")]
        yt = [r for r in rows if len(r) >= 7 and "youtube.com" in r[0]]
        signed = any(r[5] in ("SID", "__Secure-3PSID", "LOGIN_INFO", "SAPISID") for r in yt)
        return {"present": True, "updated": YT_COOKIES.stat().st_mtime, "youtube_cookies": len(yt), "signed_in": signed}

    @app.get("/api/youtube-cookies")
    def yt_get():
        return jsonify(yt_status())

    @app.post("/api/youtube-cookies")
    def yt_upload():
        f = request.files.get("file")
        if not f:
            return err("Pick the cookies .txt file")
        raw = f.read(5_000_000)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        lines = [l for l in text.replace("\r\n", "\n").split("\n")]
        rows = [l.split("\t") for l in lines if l.strip() and (not l.startswith("#") or l.startswith("#HttpOnly_"))]
        yt = [r for r in rows if len(r) >= 7 and "youtube.com" in r[0]]
        if not yt:
            return err("This isn't a cookies.txt with YouTube cookies. Export it on youtube.com with "
                       "'Get cookies.txt LOCALLY' (Netscape format).")
        if not text.startswith("# Netscape HTTP Cookie File") and not text.startswith("# HTTP Cookie File"):
            text = "# Netscape HTTP Cookie File\n" + text
        tmp = str(YT_COOKIES) + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
        os.replace(tmp, YT_COOKIES)
        st = yt_status()
        LOGS.add("app", f"[{time.strftime('%H:%M:%S')}] [youtube] cookies file saved ({st.get('youtube_cookies')} YouTube "
                        f"cookies{', signed in' if st.get('signed_in') else ', NOT signed in'})")
        return jsonify(dict(st, ok=True))

    @app.delete("/api/youtube-cookies")
    def yt_delete():
        try:
            YT_COOKIES.unlink()
        except OSError:
            pass
        return jsonify({"ok": True})

    # ---- past streams ----
    @app.get("/api/past/list")
    def past_list():
        from ..vods import list_past_streams
        key = request.args.get("key", "")
        hours = float(request.args.get("hours", 72))
        scfg = next((s for s in cfg()["streamers"] if s["key"] == key), None)
        if not scfg:
            return err("Pick a streamer from your list")
        try:
            with open(os.path.join(out_dir, ".past_streams_done.json"), "r", encoding="utf-8") as f:
                done = set(json.load(f))
        except (OSError, ValueError):
            done = set()
        try:
            vods = list_past_streams(scfg, time.time() - hours * 3600, max_items=int(request.args.get("max", 10)))
        except Exception as e:
            return err(f"Couldn't list past streams: {str(e)[:150]}")
        for v in vods:
            v["done"] = v["id"] in done or f"url_{v['url']}" in done
        return jsonify(vods)

    @app.post("/api/past/clip")
    def past_clip():
        body = request.get_json(force=True)
        urls = [u.strip() for u in body.get("urls") or [] if u.strip().startswith("http")]
        if not urls:
            return err("Pick at least one past stream or paste a link")
        name = (body.get("streamer") or "").strip().lstrip("@").lower()
        if not name:
            plat, login = parse_streamer_link(urls[0])
            name = _norm_login(plat, login) if plat and "/videos/" not in urls[0] else "pastclips"
        top = body.get("top")
        top = 0 if top in (None, "", 0, "0") else max(0, int(top))      # 0 = no limit on clips per stream
        args = ["vods", "--streamer", name, "--top", str(top)]
        for u in urls:
            args += ["--url", u]
        if body.get("any_game"):
            args.append("--any-game")
        job = jobs.add("past", f"Past streams: {len(urls)} for {name}", args)
        return jsonify({"ok": True, "job": job.id})

    # ---- clips ----
    @app.get("/api/clips")
    def api_clips():
        return jsonify(clips.list())

    @app.get("/media/<cid>/<name>")
    def media(cid, name):
        try:
            folder = clips.folder(cid)
        except (KeyError, ValueError):
            abort(404)
        if "/" in name or "\\" in name or name not in os.listdir(folder):
            abort(404)
        return send_file(os.path.join(folder, name), conditional=True, max_age=0)

    def _read_meta(folder):
        try:
            with open(os.path.join(folder, "meta.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _meta_edit(cid, fn):
        folder = clips.folder(cid)
        mp = os.path.join(folder, "meta.json")
        with open(mp, "r", encoding="utf-8") as f:
            meta = json.load(f)
        fn(meta)
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    @app.post("/api/clips/<cid>/posted")
    def posted(cid):
        body = request.get_json(force=True)

        def fn(meta):
            p = meta.setdefault("posted", {})
            p[str(body.get("platform"))] = bool(body.get("value"))
        try:
            _meta_edit(cid, fn)
        except (KeyError, ValueError, OSError):
            return err("Clip not found", 404)
        return jsonify({"ok": True})

    @app.post("/api/clips/<cid>/text")
    def clip_text(cid):
        """Post title / caption / hashtags / your own context: saved right away, no re-render."""
        body = request.get_json(force=True)

        def fn(meta):
            for src, dst in (("post_title", "title"), ("caption", "caption"), ("hashtags", "hashtags"),
                             ("user_context", "user_context")):
                if src in body:
                    meta[dst] = str(body[src] or "").strip()
            if any(k in body for k in ("post_title", "caption", "hashtags")):
                campaign.apply_to_meta(meta, _camp(meta))     # required hashtags + credit stay in the text
        try:
            _meta_edit(cid, fn)
            folder = clips.folder(cid)
            with open(os.path.join(folder, "meta.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)
            _write_caption_txt(folder, meta, _camp(meta))
        except (KeyError, ValueError, OSError):
            return err("Clip not found", 404)
        return jsonify({"ok": True, "caption": meta.get("caption"), "hashtags": meta.get("hashtags")})

    def _camp(meta):
        """Campaign rules of the clip's streamer (None if none)."""
        try:
            return campaign.for_login(cfg(), meta.get("login"), meta.get("platform") or None) \
                or campaign.for_login(cfg(), meta.get("login"))
        except Exception:
            return None

    @app.post("/api/clips/<cid>/retitle")
    def retitle(cid):
        """New title on the video: re-render with the same cut (jump cuts and callouts kept)."""
        body = request.get_json(force=True)
        title = (body.get("title") or "").strip()
        if not title:
            return err("Write a title first")
        if cid in jobs.busy_targets():
            return err("This clip is already being re-edited.")
        try:
            folder = clips.folder(cid)
            c = next(x for x in clips.list() if x["id"] == cid)
        except (KeyError, ValueError, StopIteration):
            return err("Clip not found", 404)
        if not c["has_raw"]:
            return err("This clip has no raw.mp4, so the title on the video can't be changed.")
        start = float(c["raw_offset"])
        end = start + float(c["span"] or c["duration"] or 10)
        args = ["recut", folder, "--start", f"{start:.2f}", "--end", f"{end:.2f}", "--keep-cuts",
                "--title", title, "--highlight", (body.get("highlight") or "").strip(),
                "--emoji", body.get("emoji") or ""]
        try:                                    # learning: how you want titles
            from .. import learning
            learning.title_fix(cfg(), _read_meta(folder) or {}, title, "idea" if body.get("idea") else "typed")
        except Exception as e:
            LOGS.add("learn", f"[{time.strftime('%H:%M:%S')}] [learn] couldn't save the title fix: {str(e)[:100]}")
        job = jobs.add("recut", "New title: " + title[:48], args, target=cid)
        return jsonify({"ok": True, "job": job.id})

    @app.post("/api/clips/<cid>/suggest-titles")
    def suggest(cid):
        from ..ai import suggest_titles
        body = request.get_json(force=True)
        context = (body.get("context") or "").strip()
        if len(context) < 5:
            return err("Write what happens in the clip first")
        try:
            folder = clips.folder(cid)
            _meta_edit(cid, lambda m: m.__setitem__("user_context", context))
            with open(os.path.join(folder, "meta.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (KeyError, ValueError, OSError):
            return err("Clip not found", 404)
        try:
            scfg = next((s for s in cfg()["streamers"] if s["login"] == meta.get("login")), None)
            ideas = suggest_titles(cfg(), meta.get("streamer") or meta.get("login") or "", context, _said(folder),
                                   meta.get("title_bar") or "", scfg=scfg)
        except Exception as e:
            return err(f"Claude couldn't suggest titles: {str(e)[:160]}")
        return jsonify({"ok": True, "ideas": ideas})

    @app.post("/api/clips/<cid>/post-open")
    def post_open(cid):
        """Open the upload page in your browser, show the right file in its folder, copy the text."""
        import urllib.parse
        body = request.get_json(force=True)
        plat = str(body.get("platform") or "")
        if plat not in UPLOAD_PAGES:
            return err("Unknown platform")
        try:
            folder = clips.folder(cid)
        except (KeyError, ValueError):
            return err("Clip not found", 404)
        fname = file_for_platform(folder, {}, plat, _camp(_read_meta(folder) or {}))
        if not fname:
            return err("This clip has no video file")
        text = str(body.get("copy") or "")
        if text:
            set_clipboard(text)
        if body.get("reveal", True):
            _open_path(os.path.join(folder, fname), select=True)
        url = UPLOAD_PAGES[plat].replace("{text}", urllib.parse.quote(str(body.get("x_text") or text)[:280]))
        if body.get("open_page", True):
            webbrowser.open(url)
        return jsonify({"ok": True, "file": fname, "url": url})

    @app.post("/api/clips/<cid>/post-texts")
    def post_texts(cid):
        body = request.get_json(force=True)

        def fn(meta):
            texts = meta.setdefault("post_texts", {})
            for plat, val in (body or {}).items():
                if plat in UPLOAD_PAGES and isinstance(val, dict):
                    texts[plat] = {k: str(v)[:5000] for k, v in val.items()}
        try:
            _meta_edit(cid, fn)
        except (KeyError, ValueError, OSError):
            return err("Clip not found", 404)
        return jsonify({"ok": True})

    @app.post("/api/clips/<cid>/post-done")
    def post_done(cid):
        body = request.get_json(force=True)
        plat, link = str(body.get("platform") or ""), str(body.get("link") or "").strip()
        if plat not in UPLOAD_PAGES:
            return err("Unknown platform")
        if link and not re.match(r"^https?://", link):
            return err("Paste the full link of the post (starting with https://)")

        def fn(meta):
            meta.setdefault("posted", {})[plat] = True
            if link:
                meta.setdefault("links", {})[plat] = link
            meta.setdefault("posted_at", {})[plat] = dt.datetime.now().isoformat(timespec="seconds")
        try:
            _meta_edit(cid, fn)
        except (KeyError, ValueError, OSError):
            return err("Clip not found", 404)
        return jsonify({"ok": True})

    @app.delete("/api/clips/<cid>")
    def delete_clip(cid):
        try:
            folder = clips.folder(cid)
        except (KeyError, ValueError):
            return err("Clip not found", 404)
        if cid in jobs.busy_targets():
            return err("This clip is being re-edited right now.")
        try:                                    # learning: one of the engine's picks you didn't want
            meta = _read_meta(folder)
            if meta and not meta.get("cut_by_hand"):
                from .. import learning
                learning.bad_pick(cfg(), meta)
        except Exception as e:
            LOGS.add("learn", f"[{time.strftime('%H:%M:%S')}] [learn] couldn't save the deleted clip as a lesson: {str(e)[:100]}")
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify({"ok": True})

    @app.post("/api/clips/<cid>/open")
    def open_folder(cid):
        try:
            folder = clips.folder(cid)
        except (KeyError, ValueError):
            return err("Clip not found", 404)
        name = (request.get_json(silent=True) or {}).get("file")
        if name and name in os.listdir(folder):
            _open_path(os.path.join(folder, name), select=True)
        else:
            _open_path(folder)
        return jsonify({"ok": True})

    @app.post("/api/clips/<cid>/reedit")
    def reedit(cid):
        body = request.get_json(force=True)
        try:
            folder = clips.folder(cid)
        except (KeyError, ValueError):
            return err("Clip not found", 404)
        args = ["reedit", folder]
        if (body.get("context") or "").strip():
            ctx = body["context"].strip()[:800]
            _meta_edit(cid, lambda m: m.__setitem__("user_context", ctx))
            args += ["--context", ctx]
        if (body.get("note") or "").strip():
            args += ["--note", body["note"].strip()[:500]]
        if body.get("start") is not None and body.get("end") is not None:
            args += ["--start", f"{float(body['start']):.2f}", "--end", f"{float(body['end']):.2f}"]
        job = jobs.add("reedit", "Claude re-edit: " + _clip_title(folder), args, target=cid)
        return jsonify({"ok": True, "job": job.id})

    @app.post("/api/clips/<cid>/recut")
    def recut(cid):
        body = request.get_json(force=True)
        try:
            folder = clips.folder(cid)
            start, end = float(body["start"]), float(body["end"])
        except (KeyError, ValueError, TypeError):
            return err("Pick a start and an end")
        if end - start < 3:
            return err("The cut must be at least 3 seconds long")
        args = ["recut", folder, "--start", f"{start:.2f}", "--end", f"{end:.2f}"]
        for k in ("title", "highlight", "emoji"):
            if body.get(k) is not None:
                args += [f"--{k}", str(body[k])]
        if body.get("focus") is not None:
            args += ["--focus", f"{float(body['focus']):.3f}"]
        if body.get("keep_cuts"):
            args.append("--keep-cuts")
        cam = body.get("cam")
        if cam == "none":
            args += ["--cam", "none"]
        elif isinstance(cam, dict):
            args += ["--cam", ",".join(f"{float(cam[k]):.4f}" for k in ("x", "y", "w", "h"))]
        if not body.get("keep_cuts"):           # learning: you changed where one of the engine's clips starts/ends
            try:
                meta = _read_meta(folder) or {}
                c = next((x for x in clips.list() if x["id"] == cid), None)
                if c and not meta.get("cut_by_hand"):
                    from .. import learning
                    old_s = float(c["raw_offset"])
                    learning.timing_fix(cfg(), meta, old_s, old_s + float(c["span"] or c["duration"] or 0), start, end)
            except Exception as e:
                LOGS.add("learn", f"[{time.strftime('%H:%M:%S')}] [learn] couldn't save the timing fix: {str(e)[:100]}")
        job = jobs.add("recut", "Your cut: " + _clip_title(folder), args, target=cid)
        return jsonify({"ok": True, "job": job.id})

    @app.post("/api/fix-all")
    def fix_all():
        job = jobs.add("fix", "Re-edit all old clips", ["fix"])
        return jsonify({"ok": True, "job": job.id})

    @app.post("/api/jobs/<jid>/cancel")
    def cancel(jid):
        return jsonify({"ok": jobs.cancel(jid)})

    @app.post("/api/open-output")
    def open_output():
        os.makedirs(out_dir, exist_ok=True)
        _open_path(out_dir)
        return jsonify({"ok": True})

    # ---- learning (the engine learns from the clips you make, delete, retitle and re-time) ----
    def learn_busy():
        return any(j.kind == "learn" and j.status in ("queued", "running") for j in list(jobs.history))

    def queue_learn(why):
        if learn_busy():
            return None
        return jobs.add("learn", f"Learning from your clips ({why})", ["learn"])

    def learn_worker():
        from .. import learning
        while True:
            time.sleep(90)
            try:
                if learning.auto_due(cfg()):
                    queue_learn("automatic")
            except Exception as e:
                LOGS.add("learn", f"[{time.strftime('%H:%M:%S')}] [learn] {str(e)[:150]}")
    threading.Thread(target=learn_worker, daemon=True).start()

    @app.get("/api/learning")
    def api_learning():
        from .. import learning
        try:
            data = learning.overview(cfg())
        except Exception as e:
            return err(f"Couldn't read the learning files: {str(e)[:150]}", 500)
        data["busy"] = learn_busy()
        return jsonify(data)

    @app.post("/api/learning/run")
    def learning_run():
        from .. import learning
        n = len(learning.examples(cfg()))
        if n < learning.MIN_EXAMPLES:
            return err(f"Only {n} example(s) so far: learning needs at least {learning.MIN_EXAMPLES} "
                       "(it never learns from one).")
        job = queue_learn("Learn now")
        if not job:
            return err("A learning run is already going.")
        return jsonify({"ok": True, "job": job.id})

    @app.post("/api/learning/undo")
    def learning_undo():
        from .. import learning
        if learn_busy():
            return err("Wait until the learning run is finished.")
        restored = learning.undo(cfg())
        LOGS.add("learn", f"[{time.strftime('%H:%M:%S')}] [learn] last learning undone"
                          + ("" if restored else " (back to nothing learned)") + ": live clipping uses it after a restart")
        return jsonify({"ok": True, "restored": restored})

    @app.post("/api/learning/reset")
    def learning_reset():
        from .. import learning
        if learn_busy():
            return err("Wait until the learning run is finished.")
        learning.reset(cfg())
        LOGS.add("learn", f"[{time.strftime('%H:%M:%S')}] [learn] learning reset (your examples are kept)")
        return jsonify({"ok": True})

    @app.post("/api/learning/note-remove")
    def learning_note_remove():
        from .. import learning
        try:
            removed = learning.remove_note(cfg(), int((request.get_json(force=True) or {}).get("index")))
        except (IndexError, TypeError, ValueError):
            return err("That note isn't there anymore")
        LOGS.add("learn", f"[{time.strftime('%H:%M:%S')}] [learn] editor note removed: {removed[:120]}")
        return jsonify({"ok": True})

    # ---- settings ----
    SETTINGS = [
        ("learning", "auto", "bool"),
        ("anthropic", "claude_code_model", "str"), ("anthropic", "max_checks_per_hour", "int"),
        ("clips", "min_score", "int"), ("clips", "max_per_hour_total", "int"),
        ("clips", "max_per_hour_per_streamer", "int"), ("clips", "nudity", "nudity"),
        ("clips", "lead_in", "float"), ("clips", "tail", "float"),
        ("render", "captions", "captions"), ("render", "jump_cuts", "bool"),
        ("detection", "voice_triggers", "bool"), ("detection", "chaos_triggers", "bool"),
        ("storage", "segment_minutes", "int"), ("storage", "auto_delete_recordings_days", "float"),
    ]

    @app.get("/api/settings")
    def get_settings():
        c = cfg()
        out = {f"{a}.{b}": c.get(a, {}).get(b) for a, b, _ in SETTINGS}
        out["storage.output_dir"] = out_dir
        return jsonify(out)

    @app.post("/api/settings")
    def set_settings():
        body = request.get_json(force=True)
        from ruamel.yaml.comments import CommentedMap

        def fn(data):
            for a, b, kind in SETTINGS:
                k = f"{a}.{b}"
                if k not in body:
                    continue
                v = body[k]
                if kind == "int":
                    v = int(v)
                elif kind == "float":
                    v = float(v)
                elif kind == "bool":
                    v = bool(v)
                elif kind == "nudity":
                    v = v if v in ("blur", "skip", "keep") else "blur"
                elif kind == "captions":
                    v = "mini" if v in (True, "mini") else False
                if data.get(a) is None:
                    data[a] = CommentedMap()
                data[a][b] = v
        try:
            conf.edit(fn)
        except (ValueError, TypeError) as e:
            return err(f"Invalid value: {e}")
        return jsonify({"ok": True})

    @app.post("/api/window/alive")
    def window_alive():
        WINDOW["seen"] = time.time()
        focus, WINDOW["focus"] = WINDOW["focus"], False
        return jsonify({"focus": focus})

    @app.post("/api/window/focus")
    def window_focus():
        is_open = time.time() - WINDOW["seen"] < 5
        if is_open:
            WINDOW["focus"] = True
        return jsonify({"window_open": is_open})

    @app.post("/api/quit")
    def quit_app():
        def go():
            jobs.shutdown()
            if HOOKS["on_quit"]:
                try:
                    HOOKS["on_quit"]()
                except Exception:
                    pass
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=go, daemon=True).start()
        return jsonify({"ok": True})

    app.jobs = jobs
    return app


# ------------------------------------------------------------------------------------------ window
def _browser_app_window(url):
    """Open the app in its own window (Edge/Chrome 'app' mode: no tabs, no address bar)."""
    cands = []
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
            cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    elif sys.platform == "darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        cands += [shutil.which("google-chrome") or "", shutil.which("chromium") or "", shutil.which("microsoft-edge") or ""]
    for exe in cands:
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1440,920", "--start-maximized", "--no-first-run"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue
    webbrowser.open(url)


EXE = ROOT / "FrenzoApp" / "Frenzo" / "Frenzo.exe"


def open_window(url):
    """The Frenzo.exe desktop window if it was built, otherwise an Edge/Chrome app window."""
    if IS_WIN and EXE.exists():
        try:
            subprocess.Popen([str(EXE)], cwd=str(ROOT))
            return
        except OSError:
            pass
    _browser_app_window(url)


def _tray(app, url):
    """Icon next to the clock: Frenzo keeps clipping when the window is closed. Returns the icon or None."""
    try:
        import pystray
        from PIL import Image
    except Exception:            # not installed, or no desktop to show it on
        return None
    jobs = app.jobs
    img = Image.open(STATIC / "icon.png")

    def status(item):
        return "Live clipping: ON" if jobs.live_running() else "Live clipping: off"

    def do_open(icon, item):
        if time.time() - WINDOW["seen"] < 5:
            WINDOW["focus"] = True
        else:
            open_window(url)

    def do_quit(icon, item):
        def go():
            jobs.shutdown()
            icon.stop()
            os._exit(0)
        threading.Thread(target=go, daemon=True).start()

    icon = pystray.Icon("Frenzo", img, "Frenzo", menu=pystray.Menu(
        pystray.MenuItem("Open Frenzo", do_open, default=True),
        pystray.MenuItem(status, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Frenzo", do_quit)))

    def notify(text):
        try:
            icon.notify(text, "Frenzo")
        except Exception:
            pass

    def on_saved(who, msg):
        if time.time() - WINDOW["seen"] > 5:             # only when the window is closed
            notify(f"New clip from {who}")
    HOOKS["on_saved"] = on_saved
    HOOKS["on_quit"] = icon.stop

    def watch_window():
        was_open = False
        while True:
            time.sleep(2)
            is_open = time.time() - WINDOW["seen"] < 5
            if was_open and not is_open and jobs.live_running():
                notify("Frenzo is still clipping in the background. Open it from this icon.")
            was_open = is_open
    threading.Thread(target=watch_window, daemon=True).start()
    return icon


def _already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == "frenzo"
    except Exception:
        return False


def run_app(cfg_path="config.yaml", port=8765, window=True, tray=True):
    if sys.stdout is None or sys.stderr is None:      # started from the shortcut (pythonw): keep a log file
        f = open(ROOT / "app_window.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
    _fix_path()
    url = f"http://127.0.0.1:{port}/"
    if _already_running(port):
        if window:
            open_window(url)
        return
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit(f"Port {port} is used by another program. Try: python -m frenzo app --port 8766")
    app = create_app(cfg_path)
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)     # no line per request
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", port, app, threaded=True)
    if window:
        threading.Timer(0.8, open_window, args=(url,)).start()
    print(f"Frenzo app running at {url} (close this to quit)", flush=True)
    icon = _tray(app, url) if tray else None
    try:
        if icon:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                icon.run()                   # until "Quit Frenzo"
            except Exception as e:           # no tray on this desktop: keep serving without it
                print(f"tray icon unavailable ({str(e)[:80]})", flush=True)
                HOOKS["on_saved"] = HOOKS["on_quit"] = None
                while True:
                    time.sleep(3600)
        else:
            srv.serve_forever()
    except KeyboardInterrupt:
        app.jobs.shutdown()


def make_shortcut():
    """Desktop shortcut 'Frenzo' that opens the app without a terminal window."""
    if not IS_WIN:
        print("Shortcut creation is for Windows. On Mac, run: python -m frenzo app")
        return
    icon = ROOT / "assets" / "frenzo.ico"
    if EXE.exists():
        target, args, icon = str(EXE), "", EXE
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        target, args = str(pyw if pyw.exists() else Path(sys.executable)), "-m frenzo app"
    ps = (
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Frenzo.lnk'));"
        f"$s.TargetPath='{target}';$s.Arguments='{args}';$s.WorkingDirectory='{ROOT}';"
        f"$s.IconLocation='{icon}';$s.Description='Frenzo clipping app';$s.Save()"
    )
    res = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode == 0:
        print("Frenzo shortcut created on your Desktop.")
    else:
        print("Couldn't create the shortcut: " + res.stderr.decode("utf-8", "replace")[:300])


def build_exe(if_needed=False):
    """Build FrenzoApp/Frenzo/Frenzo.exe (the desktop window) with PyInstaller, on this PC."""
    import tempfile
    src = Path(__file__).with_name("window.py")
    if if_needed and EXE.exists() and EXE.stat().st_mtime >= src.stat().st_mtime:
        print("Frenzo.exe is up to date.")
        return True
    try:
        import PyInstaller  # noqa: F401
        import webview  # noqa: F401
    except ImportError:
        print("Missing packages: run  python -m pip install -r requirements-app.txt")
        return False
    tmp = tempfile.mkdtemp(prefix="frenzo_build_")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
           "--name", "Frenzo", "--icon", str(ROOT / "assets" / "frenzo.ico"),
           "--distpath", str(ROOT / "FrenzoApp"), "--workpath", os.path.join(tmp, "build"), "--specpath", tmp,
           "--hidden-import", "webview.platforms.winforms", "--hidden-import", "webview.platforms.edgechromium",
           "--hidden-import", "clr", str(src)]
    print("Building Frenzo.exe (1-3 minutes)...", flush=True)
    res = subprocess.run(cmd, cwd=str(ROOT))
    shutil.rmtree(tmp, ignore_errors=True)
    if res.returncode != 0 or not EXE.exists():
        print("Building Frenzo.exe failed: the app will open in an Edge window instead.")
        return False
    print(f"Built {EXE}")
    return True
