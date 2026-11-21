"""Past streams kept for cutting by hand.

After a past stream is processed, its small preview (and what was found in it: detected moments with their
scores, viewer clips, chat density, YouTube heatmap peaks) is kept in <output_dir>/.streams/<id>/ for
past.keep_preview_hours (48 h by default). You can then cut clips yourself:

    frenzo cut <stream-id-or-path> 1:23:45-1:24:30 5025-5070 [--note "..."] [--no-judge]

or in the app (Recordings page, "Past streams"). For each range the 1080p piece is downloaded (if only the
preview is local), then the same Claude judging and editing runs. Claude stays inside your range (it may trim,
never move out), manual cuts are always kept, and every cut is saved as a "missed moment" lesson with your note."""
import datetime as dt
import json
import os
import re
import shutil
import tempfile
import time

from .util import log, slugify


def root(cfg):
    return os.path.join(cfg["storage"]["output_dir"], ".streams")


def safe_id(vod_id):
    return slugify(str(vod_id).replace("url_", ""), max_len=80) or "stream"


# ------------------------------------------------------------------------------------------- ranges
_T = r"(?:\d+:)?(?:\d+:)?\d+(?:\.\d+)?"


def parse_time(text):
    """'1:23:45', '83:20', '5025', '5025.5' -> seconds."""
    parts = str(text).strip().split(":")
    if not parts or any(p == "" for p in parts) or len(parts) > 3:
        raise ValueError(f"bad time: {text!r}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def parse_ranges(items):
    """['1:23:45-1:24:30', '5025-5070,6000-6030'] -> [(5025.0, 5070.0), ...] (sorted, each at least 3 s)."""
    out = []
    for item in items or []:
        for chunk in re.split(r"[,\s]+", re.sub(r"\s*-\s*", "-", str(item).strip())):
            if not chunk:
                continue
            m = re.fullmatch(rf"({_T})\s*-\s*({_T})", chunk)
            if not m:
                raise ValueError(f"bad range: {chunk!r} (use start-end, e.g. 1:23:45-1:24:30 or 5025-5070)")
            a, b = parse_time(m.group(1)), parse_time(m.group(2))
            if b - a < 3:
                raise ValueError(f"range {chunk!r} is shorter than 3 seconds (or ends before it starts)")
            out.append((a, b))
    return sorted(out)


# ------------------------------------------------------------------------------------------ keeping
def keep(cfg, scfg, vod, preview, moments, found, chat=None):
    """Move a processed stream's preview into .streams/<id>/ with what was found in it."""
    hours = float((cfg.get("past") or {}).get("keep_preview_hours", 48) or 0)
    if hours <= 0 or not preview or not os.path.exists(preview):
        return None
    sid = safe_id(vod["id"])
    dest = os.path.join(root(cfg), sid)
    os.makedirs(dest, exist_ok=True)
    ext = os.path.splitext(preview)[1] or ".mp4"
    path = os.path.join(dest, "preview" + ext)
    try:
        shutil.move(preview, path)
    except OSError as e:
        log(f"couldn't keep the preview ({e})", scfg.get("key", ""))
        return None
    from .offline import duration
    from .recordings import loudness_curve
    dur = float(vod.get("duration") or 0) or duration(path)
    viewer, heat = [], []
    for _sec, why, _st, extra in found or []:
        for c in extra.get("clips") or []:
            if c.span:
                viewer.append({"a": round(c.span[0], 1), "b": round(c.span[1], 1), "title": c.title[:80],
                               "views": c.views, "creator": c.creator, "url": c.url})
        if why.startswith("most replayed"):
            heat.append({"t": round(_sec, 1), "v": (extra.get("signals") or {}).get("heatmap")})
    counts = []
    if chat is not None and len(chat):
        n = int((chat.end or dur) // 5) + 1
        counts = [0] * n
        for t, _u, _x in chat.msgs:
            i = int(t // 5)
            if 0 <= i < n:
                counts[i] += 1
    try:
        loud = loudness_curve(path)
    except Exception:
        loud = []
    data = {"id": sid, "vod_id": vod["id"], "url": vod.get("url"), "title": vod.get("title") or "",
            "author": vod.get("author") or scfg.get("login"), "login": scfg.get("login"), "key": scfg.get("key"),
            "platform": scfg.get("platform"), "duration": round(dur, 1), "file": os.path.basename(path),
            "kept": time.time(), "expires": time.time() + hours * 3600,
            "moments": [{"t": round(m["sec"], 1), "why": m["why"][:200], "score": m.get("score")} for m in moments or []],
            "viewer": viewer, "heat": heat, "chat": {"bucket": 5, "counts": counts}, "loud": loud}
    with open(os.path.join(dest, "stream.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)
    log(f"preview kept for cutting by hand: frenzo cut {sid} <start>-<end>  (until "
        f"{dt.datetime.fromtimestamp(data['expires']).strftime('%a %H:%M')})", scfg.get("key", ""))
    cleanup(cfg)
    return dest


def cleanup(cfg):
    """Delete kept previews whose time is up."""
    r = root(cfg)
    if not os.path.isdir(r):
        return 0
    n = 0
    for sid in os.listdir(r):
        meta = load(cfg, sid)
        if meta is None or meta.get("expires", 0) < time.time():
            shutil.rmtree(os.path.join(r, sid), ignore_errors=True)
            n += 1
    return n


def load(cfg, sid):
    """A kept stream by its folder id (or by the original id, e.g. youtube_abc / url_https://...)."""
    for name in dict.fromkeys([str(sid), safe_id(sid)]):
        if not name or "/" in name or "\\" in name or ".." in name:
            continue
        try:
            with open(os.path.join(root(cfg), name, "stream.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return None


def list_streams(cfg):
    r = root(cfg)
    out = []
    for sid in sorted(os.listdir(r)) if os.path.isdir(r) else []:
        m = load(cfg, sid)
        if m and m.get("expires", 0) >= time.time():
            m = {k: v for k, v in m.items() if k not in ("chat", "loud")}
            m["size"] = os.path.getsize(os.path.join(r, sid, m["file"])) if os.path.exists(os.path.join(r, sid, m["file"])) else 0
            out.append(m)
    return sorted(out, key=lambda m: -m.get("kept", 0))


def media_path(cfg, sid):
    m = load(cfg, sid)
    return os.path.join(root(cfg), m["id"], m["file"]) if m else None


# --------------------------------------------------------------------------------------------- cut
def _resolve(cfg, target):
    """A kept stream id, or a path to a downloaded stream file -> (meta, local file)."""
    if os.path.isfile(target):
        return {"id": safe_id(os.path.splitext(os.path.basename(target))[0]), "url": None,
                "title": os.path.basename(target), "login": None, "duration": 0}, os.path.abspath(target)
    m = load(cfg, safe_id(target)) or load(cfg, target)
    if not m:
        raise SystemExit(f"No kept stream '{target}' (frenzo-engine keeps past streams {cfg['past'].get('keep_preview_hours', 48)} h"
                         f" in {root(cfg)}), and it isn't a file either.")
    return m, os.path.join(root(cfg), m["id"], m["file"])


def _is_preview(path):
    """Low resolution (the preview) -> the 1080p piece must be downloaded for a real clip."""
    import subprocess
    try:
        h = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30).stdout.strip()
        return int(h or 0) < 720
    except Exception:
        return True


def _piece(cfg, meta, local, a, b, work, tag):
    """(file, where it starts in the stream) holding [a, b] in 1080p with raw.mp4's padding around it."""
    pad = float(cfg["render"].get("raw_padding", 60))
    if not meta.get("url") or not _is_preview(local):
        return local, 0.0
    from .vods import _find, _seg_start, _yt_dlp
    lo, hi = max(0.0, a - pad), b + pad
    for attempt in range(3):
        stem = f"cut_{int(a)}_{attempt}"
        _yt_dlp(["-f", "best[height<=1080]/bestvideo[height<=1080]+bestaudio/best", "--download-sections",
                 f"*{lo:.0f}-{hi:.0f}", "--merge-output-format", "mp4", "-o", f"{stem}.%(ext)s", meta["url"]], work)
        seg = _find(work, stem)
        if not seg:
            break
        got = _seg_start(local, seg, lo)
        start = lo if got is None else got
        if start <= a + 0.5 and start + 1 >= lo - 5:      # the range is inside the piece
            return seg, start
        log(f"downloaded piece came back {start - lo:+.0f}s off - asking again", tag)
        os.remove(seg)
        lo, hi = max(0.0, lo - (start - lo)), hi - (start - lo)
    log("couldn't get the 1080p piece: cutting from the preview (lower quality)", tag)
    return local, 0.0


def cut(cfg, target, ranges, note=None, no_judge=False, mode="claude", title=None, streamer=None):
    """Cut clips from a kept past stream (or a stream file). Returns the clip folders."""
    from .ai import ClipJudge
    from .analyzer import Transcriber
    from .learning import add_example
    from .pipeline import Candidate, Pipeline
    meta, local = _resolve(cfg, target)
    login = (streamer or meta.get("login") or "pastclips").lower()
    scfg = next((s for s in cfg["streamers"] if s["login"] == login),
                {"login": login, "key": meta.get("key") or login, "platform": meta.get("platform") or "video", "tags": []})
    tag = scfg.get("key", login)
    judge = ClipJudge(cfg, enabled=mode != "manual")
    pipe = Pipeline(cfg, Transcriber(cfg["transcription"]), judge)
    work = tempfile.mkdtemp(prefix="frenzo_cut_")
    folders = []
    try:
        for a, b in ranges:
            cm = {}
            log(f"your cut: {a / 60:.0f}:{a % 60:04.1f} -> {b / 60:.0f}:{b % 60:04.1f}", tag)
            path, start = _piece(cfg, meta, local, a, b, work, tag)
            src = ("CUT BY HAND by the page owner from a past stream: this whole window is the moment they want. Make "
                   "the best clip from it (keep=true). Stay INSIDE this window: you may trim it, never go outside")
            if no_judge:
                src += ". Do NOT judge it: viral_score 0, just make the edit plan (cuts, hook, titles, sound)"
            if note:
                src += f'. The owner\'s note: "{note.strip()}" (this is TRUE: base what_happens and the title on it)'
            t = (title or "").strip() or f"{meta.get('author') or login} on GTA 6"
            manual = {"what_happens": (note or "").strip() or "Cut by hand from a past stream.", "keep": True,
                      "viral_score": 0, "category": "manual", "start": 0.0, "end": b - a, "title_bar": t,
                      "highlight": "", "emoji": "", "segments": [[0.0, b - a]], "callouts": [], "cues": [], "title": t,
                      "caption": f"{meta.get('author') or login} on stream 🎮", "hashtags": list(scfg.get("tags") or []),
                      "spoiler": False, "nudity": False, "reason": "cut by hand from a past stream"}
            c = Candidate(login=login, display_name=meta.get("author") or login, rec_path=path, anchor=b - start,
                          source=src, context_before=b - a, context_after=0.0, streamer_cfg=scfg,
                          game=cfg["game"]["name"], platform=scfg.get("platform") or "video",
                          stream_title=meta.get("title") or "", manual=True,
                          forced=manual if mode == "manual" else None, fallback=manual, user_context=(note or "").strip())
            folder = pipe.process(c)
            if not folder:
                log("this cut couldn't be made (see above)", tag)
                continue
            folders.append(folder)
            try:
                mp = os.path.join(folder, "meta.json")
                with open(mp, "r", encoding="utf-8") as f:
                    cm = json.load(f)
                cm.update({"source": "manual_past", "cut_start": round(a, 2), "cut_end": round(b, 2),
                           "stream_id": meta.get("id"), "stream_url": meta.get("url"), "cut_note": note or None})
                with open(mp, "w", encoding="utf-8") as f:
                    json.dump(cm, f, ensure_ascii=False, indent=2)
            except (OSError, ValueError):
                pass
            # every manual cut is a "missed moment" lesson
            near = [m for m in meta.get("moments") or [] if a - 60 <= m["t"] <= b + 60]
            crowd = [v for v in meta.get("viewer") or [] if v["b"] >= a and v["a"] <= b]
            d = c.judged or {}
            add_example(cfg, "missed", {
                "login": login, "platform": scfg.get("platform"), "source": "manual_past", "stream": meta.get("id"),
                "start": round(a, 1), "end": round(b, 1), "context": (note or "")[:500], "title": cm.get("title_bar") if folder else t,
                "claude_description": str(d.get("what_happens") or "")[:400],
                "engine_flags": [{"t": m["t"], "why": m["why"][:120], "score": m.get("score")} for m in near][:6],
                "viewer_clips": len(crowd),
                "verdict": "never flagged" if not near else "flagged (judged or skipped) but you cut it yourself",
                "clip": os.path.basename(folder)})
            log("lesson saved: a moment you cut by hand (missed by the engine)", "learn")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return folders
