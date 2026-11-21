"""Clip harvest: take a streamer's existing viewer Twitch clips of GTA VI, find the best moments Frenzo hasn't
already made, and edit them with that streamer's settings.

    frenzo harvest <streamer> [--since 24h | --last-stream | --from ISO --to ISO] [--top 10] [--min-clippers 2]

1. clips of the channel in the time range (official API with keys, else Twitch's public web API), kept only if
   their game is GTA VI (game id looked up once and cached), bots and the broadcaster ignored, clips already
   harvested skipped (cache);
2. overlapping clips = one moment (VOD offset when known, else the clip's own time); ranked by distinct clippers,
   featured, views; minimums applied; moments Frenzo already clipped are skipped;
3. context: a wider 1080p piece from the VOD (30 s before, 10 s after the clips by default), or the clip file
   itself when the VOD is gone / sub-only;
4. the same judging (with the clip titles, clippers and views) and editing as every other moment."""
import datetime as dt
import glob
import json
import os
import re
import shutil
import tempfile
import threading
import time

from . import crowd
from .util import log

_lock = threading.Lock()


# ------------------------------------------------------------------------------------------ time range
def parse_since(text):
    """'24h', '90m', '2d', '3600' (seconds) -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(text or "").lower())
    if not m:
        raise ValueError(f"bad --since {text!r} (use e.g. 24h, 90m, 2d)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def parse_when(text):
    """ISO date/time ('2026-11-21T10:00', '2026-11-21 10:00', '2026-11-21') -> unix time (local time if no zone)."""
    t = str(text).strip().replace(" ", "T")
    try:
        d = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"bad date {text!r} (use e.g. 2026-11-21T10:00)")
    return d.timestamp()


def time_range(api, uid, since=None, last_stream=False, t_from=None, t_to=None, now=None):
    """-> (start ts, end ts, vod or None)."""
    now = now or time.time()
    if last_stream:
        vids = api.videos(uid, first=1)
        if not vids:
            raise SystemExit("No past broadcast found for this streamer (VODs off?): use --since instead.")
        v = vids[0]
        from .replay import twitch_duration
        a = crowd.parse_ts(v["created_at"])
        return a, a + twitch_duration(v.get("duration")) + 600, v
    if t_from is not None:
        return parse_when(t_from), (parse_when(t_to) if t_to else now), None
    return now - parse_since(since or "24h"), now, None


# ------------------------------------------------------------------------------------------- the cache
def _done_path(cfg):
    return os.path.join(cfg["storage"]["output_dir"], ".harvest_done.json")


def done_ids(cfg):
    try:
        with open(_done_path(cfg), "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def mark_done(cfg, ids):
    with _lock:
        ids = done_ids(cfg) | set(ids)
        os.makedirs(os.path.dirname(_done_path(cfg)), exist_ok=True)
        with open(_done_path(cfg) + ".tmp", "w", encoding="utf-8") as f:
            json.dump(sorted(ids)[-20000:], f)
        os.replace(_done_path(cfg) + ".tmp", _done_path(cfg))


# ------------------------------------------------------------------------------- already clipped by Frenzo
def clipped_spans(cfg, login):
    """Wall-time spans [(a, b)] and VOD spans {vod_url: [(a, b)]} of the clips Frenzo already made for a streamer."""
    wall, vod = [], {}
    root = cfg["storage"]["output_dir"]
    for m in glob.glob(os.path.join(root, login, "*", "*", "meta.json")):
        try:
            with open(m, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        dur = float(meta.get("duration") or 30)
        if isinstance(meta.get("start_wall"), (int, float)):
            wall.append((meta["start_wall"], meta["start_wall"] + dur))
        if meta.get("vod_url") and isinstance(meta.get("vod_start"), (int, float)):
            vod.setdefault(meta["vod_url"], []).append((meta["vod_start"], meta["vod_start"] + dur))
        for c in meta.get("harvest_clips") or meta.get("viewer_clips") or []:
            if isinstance(c, dict) and c.get("vod_offset") is not None and c.get("video_id"):
                url = f"https://www.twitch.tv/videos/{c['video_id']}"
                vod.setdefault(url, []).append((c["vod_offset"], c["vod_offset"] + float(c.get("duration") or 30)))
        if meta.get("recording") and isinstance(meta.get("start_in_recording"), (int, float)) \
                and not isinstance(meta.get("start_wall"), (int, float)):
            side = os.path.join(cfg["storage"]["recordings_dir"], os.path.splitext(meta["recording"])[0] + ".json")
            try:
                with open(side, "r", encoding="utf-8") as f:
                    t0 = json.load(f).get("t0")
            except (OSError, ValueError):
                t0 = None
            if not t0:                       # recording deleted: its file name says when it started (local time)
                mt = re.search(r"_(\d{8}_\d{6})", meta["recording"])
                if mt:
                    t0 = dt.datetime.strptime(mt.group(1), "%Y%m%d_%H%M%S").timestamp()
            if t0:
                wall.append((t0 + meta["start_in_recording"], t0 + meta["start_in_recording"] + dur))
    return wall, vod


def _overlaps(a, b, spans, margin=10.0):
    return any(a < y + margin and x - margin < b for x, y in spans)


# --------------------------------------------------------------------------------------------- listing
def find_moments(cfg, api, login, start, end, min_clippers=2, min_views=0, top=10, skip_done=True):
    """-> (ranked moments, stats). Each moment: crowd.ClipMoment with .key, .vod_id, .timeline ('vod'|'wall')."""
    det = cfg["detection"]
    uid = api.users([login]).get(login.lower())
    if not uid:
        raise SystemExit(f"Twitch channel '{login}' not found.")
    gta = api.game_id(cfg["game"]["name"])
    raw = api.clips_between(uid, crowd.iso(start), crowd.iso(end))
    stats = {"clips_found": len(raw)}
    done = done_ids(cfg) if skip_done else set()
    bots = det.get("clip_bots") or crowd.DEFAULT_BOTS
    clips, not_gta, already = [], 0, 0
    for r in raw:
        c = crowd.from_helix(r)
        if gta and c.game_id and c.game_id != str(gta):
            not_gta += 1
            continue
        if crowd.ignored(c, bots, uid):
            continue
        if c.id in done:
            already += 1
            continue
        clips.append(c)
    stats.update(not_gta=not_gta, harvested_before=already, gta_clips=len(clips))
    # group: by VOD offset when known (per VOD), else by the clip's own wall time
    by_vod, loose = {}, []
    for c in clips:
        if c.video_id and c.vod_offset is not None:
            by_vod.setdefault(c.video_id, []).append(c)
        else:
            loose.append(c)
    moments = []
    for vid, cs in by_vod.items():
        for m in crowd.group(cs, span_of=crowd.vod_span):
            m.vod_id, m.timeline = vid, "vod"
            moments.append(m)
    for m in crowd.group(loose, span_of=lambda c: (c.created - c.duration, c.created)):
        m.vod_id, m.timeline = None, "wall"
        moments.append(m)
    stats["moments"] = len(moments)
    wall_spans, vod_spans = clipped_spans(cfg, login)
    kept, skipped_clipped, below = [], 0, 0
    for m in moments:
        if m.timeline == "vod":
            url = f"https://www.twitch.tv/videos/{m.vod_id}"
            if _overlaps(m.start, m.end, vod_spans.get(url, [])) or _overlaps(*_vod_wall(m), wall_spans):
                skipped_clipped += 1
                continue
        elif _overlaps(m.start, m.end, wall_spans):
            skipped_clipped += 1
            continue
        if m.clippers < min_clippers and not m.featured or m.views < min_views:
            below += 1
            continue
        m.key = m.clips[0].id
        kept.append(m)
    kept.sort(key=lambda m: -m.strength())
    stats.update(skipped_already_clipped=skipped_clipped, below_minimums=below, ranked=len(kept))
    return kept[:top] if top else kept, stats


def _vod_wall(m):
    """A VOD moment's wall-time span: the VOD started at (clip created - offset - duration) roughly; use the first
    clip: created ~ end of its span in wall time."""
    c = m.clips[0]
    vod_start_wall = c.created - (c.vod_offset or 0) - c.duration
    return vod_start_wall + m.start, vod_start_wall + m.end


# -------------------------------------------------------------------------------------------- editing
def _context_piece(cfg, m, work, tag):
    """-> (file, where it starts on the moment's timeline, 'vod'|'clip'). The wider VOD piece when possible."""
    pc = cfg.get("past") or {}
    lead, tail = float(pc.get("piece_lead", 30)), float(pc.get("piece_tail", 10))
    pad = float(cfg["render"].get("raw_padding", 60))
    if m.timeline == "vod" and m.vod_id:
        from .vods import _find, _yt_dlp
        a, b = max(0.0, m.start - lead - pad), m.end + tail + pad
        stem = f"vod_{m.key[:20]}"
        try:
            _yt_dlp(["-f", "best[height<=1080]/bestvideo[height<=1080]+bestaudio/best", "--download-sections",
                     f"*{a:.0f}-{b:.0f}", "--merge-output-format", "mp4", "-o", f"{stem}.%(ext)s",
                     f"https://www.twitch.tv/videos/{m.vod_id}"], work)
            seg = _find(work, stem)
            if seg:
                # the piece's real start: match the first clip's sound inside it
                clip_file = crowd.download_clip(m.clips[0].url, os.path.join(work, "c_" + m.key[:12]))
                found = crowd.locate(clip_file, seg, m.clips[0].vod_offset - a, search=90, min_len=5.0, piece_max=90)
                start = a if found is None else m.clips[0].vod_offset - found
                return seg, start, "vod"
        except Exception as e:
            log(f"VOD piece unavailable ({str(e)[:100]}): using the clip file itself", tag)
    # the clip itself (VOD gone, sub-only, or a live clip with no position)
    path = crowd.download_clip(max(m.clips, key=lambda c: c.duration + c.views / 1e6).url, os.path.join(work, "clip_" + m.key[:12]))
    best = max(m.clips, key=lambda c: c.duration + c.views / 1e6)
    start = (best.vod_offset if m.timeline == "vod" else best.created - best.duration)
    return path, start, "clip"


def harvest(cfg, login, since=None, last_stream=False, t_from=None, t_to=None, top=None, min_clippers=None,
            min_views=None, only=None, report=None, dry_run=False):
    """Run a harvest. Returns the list of (moment, candidate) processed."""
    from .ai import ClipJudge
    from .analyzer import Transcriber
    from .pipeline import Candidate, Pipeline
    from .twitch import api_for
    hc = cfg.get("harvest") or {}
    if not hc.get("enabled", True):
        raise SystemExit("Harvest is turned off (harvest.enabled: false in config.yaml).")
    login = login.lower().lstrip("@")
    scfg = next((s for s in cfg["streamers"] if s["login"] == login), None)
    if scfg and scfg["platform"] == "kick":
        log("Kick clips aren't reachable for harvest: skipped", f"kick:{login}")
        return []
    scfg = scfg or {"login": login, "key": f"twitch:{login}", "platform": "twitch", "tags": []}
    tag = scfg.get("key", login)
    api = api_for(cfg)
    if api is None:
        raise SystemExit("No Twitch access: add Twitch keys, or set twitch.public_api: true.")
    t0 = time.time()
    uid = api.users([login]).get(login)
    start, end, vod = time_range(api, uid, since, last_stream, t_from, t_to)
    log(f"harvest: clips from {dt.datetime.fromtimestamp(start):%a %H:%M} to {dt.datetime.fromtimestamp(end):%a %H:%M}"
        + (f" (last stream: {vod.get('title', '')[:50]})" if vod else ""), tag)
    moments, stats = find_moments(cfg, api, login, start, end,
                                  min_clippers=int(min_clippers if min_clippers is not None else hc.get("min_clippers", 2)),
                                  min_views=int(min_views if min_views is not None else hc.get("min_views", 0)),
                                  top=int(top if top is not None else hc.get("top", 10)))
    if only:
        moments = [m for m in moments if m.key in set(only)]
    log("harvest: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in stats.items()), tag)
    for i, m in enumerate(moments):
        log(f"  {i + 1}. {m.clippers} clipper(s), {m.views} views{', featured' if m.featured else ''}: "
            f"{'; '.join(t[:40] for t in m.titles[:2])}", tag)
    if report is not None:
        report["stats"], report["listing_seconds"] = stats, round(time.time() - t0, 1)
        report["moments"] = [{"key": m.key, "clippers": m.clippers, "views": m.views, "featured": m.featured,
                              "titles": m.titles, "clips": [c.as_dict() for c in m.clips], "timeline": m.timeline,
                              "vod_id": m.vod_id, "span": [round(m.start, 1), round(m.end, 1)]} for m in moments]
    if dry_run or not moments:
        return []
    pipe = Pipeline(cfg, Transcriber(cfg["transcription"]), ClipJudge(cfg))
    det = cfg["detection"]
    work = tempfile.mkdtemp(prefix="frenzo_harvest_")
    done = []
    try:
        for m in moments:
            t_m = time.time()
            try:
                path, piece_start, source = _context_piece(cfg, m, work, tag)
            except Exception as e:
                log(f"couldn't get this moment's video ({str(e)[:120]})", tag)
                continue
            a, b = m.start - piece_start, m.end - piece_start
            c = Candidate(login=login, display_name=scfg.get("display") or login, rec_path=path,
                          anchor=max(1.0, b - 2), source=f"viewer clips (harvest): {m.clippers} clipper(s), {m.views} views"
                          + (", featured" if m.featured else ""), streamer_cfg=scfg, game=cfg["game"]["name"],
                          platform="twitch", stream_title=(vod or {}).get("title", ""), flagged_wall=time.time())
            c.clips = list(m.clips)
            c.clip_span = (max(0.0, a), max(a + 1, b))
            if crowd.strong(m, cfg):
                c.keep_bonus = int(det.get("clip_keep_bonus", 1))
            if source == "clip":                 # the clip itself: the whole file is the window
                c.context_before, c.context_after = c.anchor, 2.0
            pipe.process(c)
            mark_done(cfg, [x.id for x in m.clips])
            if c.folder:
                try:
                    mp = os.path.join(c.folder, "meta.json")
                    with open(mp, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    meta.update({"source": "harvest", "context_source": source,
                                 "harvest_clips": [x.as_dict() for x in m.clips],
                                 "clip_ids": [x.id for x in m.clips], "clip_urls": [x.url for x in m.clips]})
                    if m.timeline == "vod":
                        meta["vod_url"] = f"https://www.twitch.tv/videos/{m.vod_id}"
                        meta["vod_start"] = round(piece_start + float(meta.get("start_in_recording") or 0), 2)
                    with open(mp, "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=2)
                except (OSError, ValueError):
                    pass
            done.append((m, c))
            if report is not None:
                d = c.judged or {}
                report.setdefault("rows", []).append({
                    "key": m.key, "clip_ids": [x.id for x in m.clips], "clip_urls": [x.url for x in m.clips],
                    "clippers": m.clippers, "views": m.views, "featured": m.featured, "context_source": source,
                    "claude_score": d.get("viral_score"), "kept": bool(c.folder), "folder": c.folder,
                    "status": c.status or ("kept" if c.folder else "rejected"), "reason": d.get("reason"),
                    "seconds": round(time.time() - t_m, 1), "claude_cost_usd": d.get("_cost_usd")})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log(f"harvest done: {sum(1 for _, c in done if c.folder)} clip(s) made from {len(done)} moment(s)", tag)
    return done
