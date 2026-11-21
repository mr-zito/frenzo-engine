"""Live recordings, kept for the app's Recordings tab.

- finished hourly parts are converted from .ts to .mp4 (a fast copy, same quality) so they can be played
  and scrubbed in the app; their loudness is measured for the activity graph,
- two consecutive parts can be merged into one file (their overlap is removed),
- a clip can be cut from a part by hand (as is, or edited by Claude inside your selection),
- parts are only deleted by you (or after N days if you set auto_delete_recordings_days).
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import numpy as np

from .util import NO_WINDOW, log, run

NAME = re.compile(r"^(twitch|kick|youtube)_(.+)_(\d{8}_\d{6})$")
BUCKET = 5


# ------------------------------------------------------------------------------------------ parts
def _side(stem, folder):
    return os.path.join(folder, stem + ".json")


def load_side(folder, stem):
    try:
        with open(_side(stem, folder), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_side(folder, data):
    tmp = _side(data["stem"], folder) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _side(data["stem"], folder))


def _guess_side(folder, fname):
    """Recordings made before hourly parts have no .json: build one from the file name."""
    stem = os.path.splitext(fname)[0]
    m = NAME.match(stem)
    plat, login, stamp = (m.group(1), m.group(2), m.group(3)) if m else ("twitch", stem, None)
    try:
        t0 = dt.datetime.strptime(stamp, "%Y%m%d_%H%M%S").timestamp() if stamp else None
    except ValueError:
        t0 = None
    t0 = t0 or os.path.getmtime(os.path.join(folder, fname))
    return {"stem": stem, "file": fname, "platform": plat, "login": login, "display": login, "title": "",
            "game": "", "session": stem, "part": 0, "t0": t0, "ended": os.path.getmtime(os.path.join(folder, fname)),
            "chat": {"bucket": BUCKET, "counts": []}, "moments": []}


def list_parts(folder):
    """Every recording part in the folder: its .json data + file info."""
    out = []
    if not os.path.isdir(folder):
        return out
    names = set(os.listdir(folder))
    for fname in sorted(names):
        stem, ext = os.path.splitext(fname)
        if ext not in (".ts", ".mp4") or fname.endswith(".tmp.mp4"):
            continue
        if ext == ".ts" and stem + ".mp4" in names:      # being converted: show the .ts until done
            continue
        side = load_side(folder, stem)
        if side is None:
            try:
                side = _guess_side(folder, fname)
                save_side(folder, side)
            except OSError:
                continue
        path = os.path.join(folder, fname)
        try:
            st = os.stat(path)
        except OSError:
            continue
        side.update({"file": fname, "path": path, "size": st.st_size, "mtime": st.st_mtime, "ext": ext})
        out.append(side)
    return out


def duration(path):
    try:
        out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture=True)
        return float(out.decode().strip().splitlines()[0])
    except Exception:
        return 0.0


def loudness_curve(path, bucket=BUCKET, rate=2000):
    """Loudness (dB) per `bucket` seconds of the whole file, read in one pass at a tiny sample rate."""
    proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-vn", "-ac", "1",
                             "-ar", str(rate), "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    step = rate * bucket * 2
    out = []
    while True:
        chunk = proc.stdout.read(step)
        if not chunk:
            break
        a = np.frombuffer(chunk[: len(chunk) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        if len(a):
            out.append(round(float(20 * np.log10(np.sqrt((a ** 2).mean()) + 1e-6)), 1))
    proc.wait()
    return out


def convert(folder, part):
    """Finished .ts part -> .mp4 (stream copy: fast, same quality, playable in the app) + loudness."""
    src = os.path.join(folder, part["file"])
    stem = part["stem"]
    tmp = os.path.join(folder, stem + ".tmp.mp4")
    dst = os.path.join(folder, stem + ".mp4")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts", "-i", src,
         "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", tmp])
    d = duration(tmp)
    if d <= 1:
        os.remove(tmp)
        raise RuntimeError("the converted file is empty")
    os.replace(tmp, dst)
    side = load_side(folder, stem) or _guess_side(folder, part["file"])
    side.update({"file": stem + ".mp4", "duration": round(d, 2), "converted": time.time()})
    try:
        side["loud"] = loudness_curve(dst)
    except Exception:
        side["loud"] = []
    save_side(folder, side)
    for p in (src, src + ".log"):
        try:
            os.remove(p)
        except OSError:
            pass
    return side


def delete(folder, part):
    for p in (os.path.join(folder, part["file"]), os.path.join(folder, part["stem"] + ".ts.log"),
              _side(part["stem"], folder)):
        try:
            os.remove(p)
        except OSError:
            pass


def _pid_alive(pid):
    """Is this process still running? (Windows: asks the system; never sends it a signal.)"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259            # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def clean_temp(max_age_hours=3):
    """Leftover temporary folders from interrupted jobs (past-stream downloads, frames, builds).
    A folder whose job is still running (.in_use holds its process id) is never deleted."""
    freed = 0
    root = tempfile.gettempdir()
    for name in os.listdir(root):
        if not name.startswith(("frenzo_vod_", "frenzo_ai_", "frenzo_cam_", "frenzo_build_", "frenzo_censor_")):
            continue
        path = os.path.join(root, name)
        try:
            with open(os.path.join(path, ".in_use"), "r") as f:
                if _pid_alive(int(f.read().strip() or 0)):
                    continue
        except (OSError, ValueError):
            pass
        try:
            newest, size = os.path.getmtime(path), 0
            for dp, _, files in os.walk(path):
                for f in files:
                    fp = os.path.join(dp, f)
                    newest = max(newest, os.path.getmtime(fp))
                    size += os.path.getsize(fp)
            if time.time() - newest > max_age_hours * 3600:
                shutil.rmtree(path, ignore_errors=True)
                freed += size
        except OSError:
            continue
    return freed


# ------------------------------------------------------------------------------------------ merge
def merge(folder, path_a, path_b):
    """Two consecutive parts of the same stream -> one file, without their overlap."""
    from .vods import _seg_start
    parts = {p["path"]: p for p in list_parts(folder)}
    a, b = parts.get(os.path.abspath(path_a)) or parts.get(path_a), parts.get(os.path.abspath(path_b)) or parts.get(path_b)
    if not a or not b:
        raise SystemExit("One of the two recordings wasn't found.")
    if (a.get("t0") or 0) > (b.get("t0") or 0):
        a, b = b, a
    if a.get("session") != b.get("session"):
        raise SystemExit("These two recordings are from different streams: only consecutive parts can be merged.")
    for p in (a, b):
        if p["ext"] == ".ts":
            log(f"converting {p['file']} first", "recordings")
            p.update(convert(folder, p), path=os.path.join(folder, p["stem"] + ".mp4"), ext=".mp4")
    pa, pb = os.path.join(folder, a["file"]), os.path.join(folder, b["file"])
    da = duration(pa)
    guess = (b["t0"] - a["t0"]) if a.get("t0") and b.get("t0") else da
    guess = max(0.0, min(da, guess))
    # the start times are known to within a few seconds; matching the sound makes the seam exact,
    # but only a match close to that time is trusted
    exact = _seg_start(pa, pb, guess, search=20)
    seam = exact if exact is not None and abs(exact - guess) <= 15 else guess
    log(f"merging {a['file']} + {b['file']} (part 2 starts at {seam / 60:.1f} min of part 1)", "recordings")
    stem = a["stem"] + "_merged"
    lst = os.path.join(folder, stem + ".txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.write(f"file '{os.path.basename(pa)}'\noutpoint {seam:.3f}\nfile '{os.path.basename(pb)}'\n")
    tmp = os.path.join(folder, stem + ".tmp.mp4")
    try:
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
             "-c", "copy", "-movflags", "+faststart", tmp], cwd=folder)
    finally:
        os.remove(lst)
    d = duration(tmp)
    if d < seam:
        os.remove(tmp)
        raise SystemExit("Merging failed (the new file is too short). Nothing was deleted.")
    os.replace(tmp, os.path.join(folder, stem + ".mp4"))
    nb = int(seam // BUCKET)
    chat_a, chat_b = a.get("chat", {}).get("counts", []), b.get("chat", {}).get("counts", [])
    aliases = {k: v for k, v in (a.get("aliases") or {a["stem"]: 0.0}).items()}
    for k, v in (b.get("aliases") or {b["stem"]: 0.0}).items():
        aliases[k] = round(v + seam, 2)
    side = dict(a, stem=stem, file=stem + ".mp4", ended=b.get("ended"), duration=round(d, 2),
                parts=sorted(set((a.get("parts") or [a.get("part", 0)]) + (b.get("parts") or [b.get("part", 0)]))),
                aliases=aliases,
                chat={"bucket": BUCKET, "counts": (chat_a + [0] * nb)[:nb] + chat_b},
                moments=[m for m in a.get("moments", []) if m["t"] < seam]
                + [dict(m, t=round(m["t"] + seam, 1)) for m in b.get("moments", [])])
    for k in ("path", "size", "mtime", "ext"):
        side.pop(k, None)
    try:
        side["loud"] = loudness_curve(os.path.join(folder, stem + ".mp4"))
    except Exception:
        side["loud"] = []
    save_side(folder, side)
    delete(folder, a)
    delete(folder, b)
    log(f"merged into {stem}.mp4 ({d / 60:.0f} min)", "recordings")
    return side


# ------------------------------------------------------------------------------------------- cut
def cut_clip(cfg, path, start, end, mode="manual", title=None, highlight=None, emoji=None, context=None):
    """Make a clip from a recording, from your own selection. mode: manual (as is) or claude."""
    from .ai import ClipJudge
    from .analyzer import Transcriber
    from .pipeline import Candidate, Pipeline
    folder, fname = os.path.split(os.path.abspath(path))
    stem = os.path.splitext(fname)[0]
    side = load_side(folder, stem) or _guess_side(folder, fname)
    login, plat = side.get("login") or "streamer", side.get("platform") or "twitch"
    display = side.get("display") or login
    scfg = next((s for s in cfg["streamers"] if s["login"] == login and s["platform"] == plat),
                {"login": login, "key": f"{plat}:{login}", "platform": plat, "tags": []})
    start, end = max(0.0, float(start)), float(end)
    if end - start < 3:
        raise SystemExit("The selection must be at least 3 seconds long.")
    judge = ClipJudge(cfg)
    if mode == "claude" and judge.client is None and not judge.claude_code:
        raise SystemExit("Claude isn't available (Claude Code not found): use 'Make clip as is' instead.")
    tags = list(dict.fromkeys(["#gta6"] + list(scfg.get("tags") or [])))
    t = (title or "").strip() or f"{display} on GTA 6"
    manual = {
        "what_happens": (context or "").strip() or "Cut by hand from the recording.", "keep": True,
        "viral_score": 0, "category": "manual", "start": 0.0, "end": end - start,
        "title_bar": t, "highlight": (highlight or "").strip(), "emoji": emoji or "",
        "segments": [[0.0, end - start]], "callouts": [], "sfx": [], "title": t,
        "caption": f"{display} on stream 🎮", "hashtags": tags, "spoiler": False, "nudity": False,
        "reason": "cut by hand from the recording",
    }
    src = ("CUT BY HAND by the page owner from the recording: this whole window is the moment they want. Make the "
           "best clip from it (keep=true), title it, use jump cuts only to remove dead air")
    if context and context.strip():
        src += (f". THE PAGE OWNER WATCHED IT and says what happens: \"{context.strip()}\". This is TRUE: base "
                f"what_happens and the title on it")
    if title and title.strip():
        src += f". Their title idea: \"{title.strip()}\" (use it if it fits)"
    c = Candidate(login=login, display_name=display, rec_path=os.path.abspath(path), anchor=end,
                  source=src, context_before=end - start, context_after=0.0, streamer_cfg=scfg,
                  game=side.get("game") or cfg["game"]["name"], platform=plat, stream_title=side.get("title", ""),
                  manual=True, forced=None if mode == "claude" else manual, fallback=manual,
                  user_context=(context or "").strip())
    pipe = Pipeline(cfg, Transcriber(cfg["transcription"]), judge)
    log(f"making a clip from {fname}: {start / 60:.0f}:{start % 60:04.1f} -> {end / 60:.0f}:{end % 60:04.1f}"
        + (" (Claude edits it)" if mode == "claude" else " (as is)"), login)
    out = pipe.process(c)
    if not out:
        raise SystemExit("Claude's hourly limit is reached: try again later, or use 'Make clip as is'."
                         if c.status == "claude_limit" else "The clip couldn't be made (see the log above).")
    try:                                   # your exact selection, for learning (the clip may be trimmed inside it)
        mp = os.path.join(out, "meta.json")
        with open(mp, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta.update({"cut_start": round(start, 2), "cut_end": round(end, 2), "cut_mode": mode})
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except (OSError, ValueError):
        pass
    return out
