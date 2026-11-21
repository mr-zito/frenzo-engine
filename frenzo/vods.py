"""Past-streams mode: clip broadcasts that already ended (Twitch past broadcasts, YouTube past live streams,
or any video URL you give, including Kick VODs).

For each past stream it:
  1. downloads only the AUDIO (small and fast),
  2. listens for streamer reactions (yelling, laughing, outbursts) and picks the strongest moments,
  3. downloads only ~90 s of VIDEO around each moment,
  4. runs the normal clip pipeline, saving into the same per-streamer folders as live clips.
Finished streams are remembered so they are never clipped twice.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from . import campaign, crowd, replay, scoring
from .ai import ClipJudge
import numpy as np

from .analyzer import Transcriber, extract_audio
from .offline import loud_peaks
from .pipeline import Candidate, Pipeline
from .platforms import YouTubePlatform, game_ok
from .util import log, require_binary, youtube_cookies
from .voice import scan_file


def chaos_scan(path, dcfg, chunk=60):
    from .chaos import ChaosAnalyzer, motion_series
    from .offline import duration
    an = ChaosAnalyzer(dcfg["cooldown_seconds"], dcfg.get("chaos_ratio", 2.6))
    total, out, pos = duration(path), [], 0.0
    while pos < total:
        d, t = motion_series(path, pos, chunk + 1)
        out += an.feed(d, t)
        pos += chunk
    return out


def take_scan(path, cfg, chunk=60, extra=None, only_extra=False):
    """Hot takes + NPC talk in the whole file. extra: a campaign phrase pattern found in the same pass
    (kind "campaign", kept apart so it never changes the regular moments); only_extra: only that."""
    from .analyzer import extract_audio
    from .hottake import NPC_TALK, _transcriber, find_takes
    from .offline import duration
    tr, total, out, pos, last, last_c = _transcriber(cfg), duration(path), [], 0.0, -1e9, -1e9
    while pos < total:
        words, _ = tr.transcribe(extract_audio(path, pos, chunk + 3))
        hits = []
        if not only_extra:
            hits = [(t, s, st, "take") for t, s, st in find_takes(words) if st >= 14]
            hits += [(t, s, st + 2, "npc") for t, s, st in find_takes(words, min_words=4, pattern=NPC_TALK) if st + 2 >= 8]
        for t, snip, strength, kind in sorted(hits):
            if pos + t - last > 90:
                out.append((pos + t, snip, strength, kind))
                last = pos + t
        if extra is not None:
            for t, snip, strength in sorted(find_takes(words, min_words=2, pattern=extra)):
                if pos + t - last_c > 90:
                    out.append((pos + t, snip, strength, "campaign"))
                    last_c = pos + t
        pos += chunk
    return out


class _Quiet:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _ydl(extra=None):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "logger": _Quiet(), "socket_timeout": 30}
    if youtube_cookies():
        opts["cookiefile"] = youtube_cookies()
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


def list_past_streams(scfg, since_ts, max_items=8):
    p = scfg["platform"]
    if p == "twitch":
        url = f"https://www.twitch.tv/{scfg['login']}/videos?filter=archives&sort=time"
    elif p == "youtube":
        url = YouTubePlatform.live_url(scfg["raw_login"])[: -len("/live")] + "/streams"
    elif p == "kick":
        url = f"https://kick.com/{scfg['login']}/videos"
    else:
        return []
    try:
        with _ydl({"extract_flat": "in_playlist", "playlistend": max_items}) as ydl:
            listing = ydl.extract_info(url, download=False)
    except Exception as e:
        log(f"could not list past streams ({str(e)[:120]})", scfg["key"])
        return []
    found = []
    for e in (listing or {}).get("entries") or []:
        vurl = e.get("url") or e.get("webpage_url") or ""
        if p == "youtube" and not vurl.startswith("http"):
            vurl = f"https://www.youtube.com/watch?v={e.get('id')}"
        try:
            with _ydl() as ydl:
                info = ydl.extract_info(vurl, download=False, process=False)
        except Exception:
            continue
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            continue  # still live: the live engine handles it
        ts = info.get("timestamp") or info.get("release_timestamp") or 0
        if ts and ts < since_ts:
            continue
        found.append({
            "id": f"{p}_{info.get('id')}", "url": vurl, "title": info.get("title") or "",
            "categories": " ".join(str(x) for x in (info.get("categories") or []) + (info.get("tags") or [])
                                   + [e.get("title") or "", " ".join(e.get("categories") or [])]),
            "duration": info.get("duration") or 0, "timestamp": ts,
            "author": info.get("uploader") or info.get("channel") or scfg["login"],
        })
    return found


def _yt_dlp(args, cwd):
    cmd = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-part",
           "--concurrent-fragments", "8"] + args
    cookies = youtube_cookies()
    if cookies and any("youtube.com" in str(a) or "youtu.be" in str(a) for a in args):
        cmd[3:3] = ["--cookies", cookies]          # signed-in YouTube session (see util.youtube_cookies)
    res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode("utf-8", "replace")[-400:])


def _find(cwd, stem):
    for f in os.listdir(cwd):
        if f.startswith(stem + ".") and not f.endswith((".part", ".ytdl")):
            return os.path.join(cwd, f)
    return None


def _seg_start(audio_path, seg_path, expected, search=480):
    """Where the downloaded video piece really starts in the stream, in seconds.

    Some platforms (Kick) cut --download-sections minutes away from the asked time, so the piece is matched
    against the full audio by its loudness pattern. Returns None if no confident match is found."""
    from .crowd import locate
    return locate(seg_path, audio_path, expected, search)


def _keep_alive(work, every=600):
    """Mark the working folder as in use: our process id + a file touched every 10 minutes. Long past streams
    scan for hours without writing anything, and the app's cleanup deleted their folder mid-run."""
    import threading
    stop = threading.Event()
    beat = os.path.join(work, ".in_use")

    def loop():
        while True:
            try:
                with open(beat, "w") as f:
                    f.write(str(os.getpid()))
            except OSError:
                pass
            if stop.wait(every):
                return
    threading.Thread(target=loop, daemon=True).start()
    return stop


def _merge_moments(found, gap=60):
    """[(sec, why, strength, extras)] -> merged moments (several signals within `gap` s = one stronger moment).
    Crowd signals come first in `found`, so a moment's time follows the crowd when it has one."""
    moments = []
    for sec, why, strength, extra in found:
        near = [m for m in moments if abs(m["sec"] - sec) < gap]
        if near:
            m = near[0]
            if why.split(":")[0] not in m["why"]:
                m["why"] += " + " + why
            m["clips"] += [c for c in extra.get("clips") or [] if all(c.id != x.id for x in m["clips"])]
            for k, v in (extra.get("signals") or {}).items():
                m["signals"][k] = max(m["signals"].get(k, 0), v)
            if extra.get("span"):
                a, b = extra["span"]
                m["span"] = (min(a, m["span"][0]), max(b, m["span"][1])) if m["span"] else (a, b)
                m["sec"] = m["span"][1] - 2
            continue
        moments.append({"sec": sec, "why": why, "clips": list(extra.get("clips") or []),
                        "signals": dict(extra.get("signals") or {}), "span": extra.get("span"),
                        "crowd": bool(extra) or why.startswith("chat replay")})
    for m in moments:
        m["score"] = scoring.priority(replay.as_cand(m["why"], m["clips"], m["signals"]))
    return moments


def _report_row(report, vod, m, c, stage_s):
    if not report:
        return
    d = c.judged or {}
    report.append({"stream": vod["id"], "time": f"{int(m['sec'] // 3600)}:{int(m['sec'] % 3600 // 60):02d}:{int(m['sec'] % 60):02d}",
                   "second": round(m["sec"], 1), "signals": m["why"][:300], "crowd": m["crowd"],
                   "combined_score": m["score"], "claude_score": d.get("viral_score"),
                   "kept": bool(c.folder), "status": c.status or ("kept" if c.folder else "rejected"),
                   "reason": str(d.get("reason") or c.status or "")[:300], "folder": c.folder or "",
                   "seconds_to_clip": round(stage_s, 1), "claude_cost_usd": d.get("_cost_usd")})


def clip_past_stream(cfg, scfg, vod, pipeline, top, report=None):
    tag = scfg.get("key", scfg["login"])
    work = tempfile.mkdtemp(prefix="frenzo_vod_")
    stop_beat = _keep_alive(work)              # the app's temp cleanup must never delete this folder while we use it
    before, after = cfg["clips"]["context_before"], cfg["clips"]["context_after"]
    det, pc = cfg["detection"], cfg.get("past") or {}
    saved = saved_extra = 0
    times = {}
    try:
        # ---- crowd signals first (viewer clips, heatmap, chat replay), in parallel with the preview download ----
        crowd_res = {}

        def crowd_job():
            t = time.time()
            try:
                info = None
                if "youtu" in vod["url"] and pc.get("heatmap", True):
                    try:
                        with _ydl() as ydl:
                            info = ydl.extract_info(vod["url"], download=False)
                    except Exception as e:
                        log(f"couldn't read the video's details ({str(e)[:100]})", tag)
                api = None
                if replay.twitch_vod_id(vod["url"]):
                    from .config import placeholder
                    tw = cfg["twitch"]
                    if not placeholder(tw["client_id"]) and not placeholder(tw["client_secret"]):
                        from .twitch import TwitchAPI
                        api = TwitchAPI(tw["client_id"], tw["client_secret"])
                chat_dir = os.path.join(work, "chat")
                os.makedirs(chat_dir, exist_ok=True)
                crowd_res["found"], crowd_res["summary"], crowd_res["chat"] = replay.gather(cfg, scfg, vod, chat_dir, api, info)
            except Exception as e:
                log(f"crowd signals failed ({str(e)[:120]})", tag)
                crowd_res["found"], crowd_res["summary"], crowd_res["chat"] = [], {}, None
            times["crowd_signals"] = round(time.time() - t, 1)
        crowd_thread = threading.Thread(target=crowd_job, daemon=True)
        crowd_thread.start()

        t = time.time()
        log(f"past stream: '{vod['title'][:60]}' ({vod['duration'] / 3600:.1f} h) - downloading a small preview", tag)
        # a tiny low-resolution copy (sound + picture): enough to hear reactions and see gameplay chaos
        try:
            _yt_dlp(["-f", "worst[height>=144][acodec!=none]/worst[acodec!=none]/worst", "-o", "preview.%(ext)s", vod["url"]], work)
        except Exception:
            _yt_dlp(["-f", "bestaudio/Audio_Only/audio_only/worst", "-o", "preview.%(ext)s", vod["url"]], work)
        preview = _find(work, "preview")
        if not preview:
            raise RuntimeError("preview download produced no file")
        times["preview_download"] = round(time.time() - t, 1)

        found = []      # (second, why, strength, extras)
        t = time.time()
        log("listening for streamer reactions...", tag)
        for sec, label, strength in scan_file(preview, det.get("voice_sensitivity", "normal")):
            found.append((sec, f"streamer reaction: {label}", strength, {}))
        times["voice_scan"] = round(time.time() - t, 1)
        if det.get("chaos_triggers", True):
            t = time.time()
            log("watching the gameplay for bugs / crazy physics...", tag)
            try:
                found += [(sec, f"gameplay chaos: {r:.1f}x wilder than normal (bug/physics?)", r * 4, {})
                          for sec, r in chaos_scan(preview, det)]
            except Exception as e:
                log(f"chaos scan skipped ({str(e)[:80]})", tag)
            times["chaos_scan"] = round(time.time() - t, 1)
        # campaign: EXTRA content it wants (searched on top of everything; the regular moments stay the same)
        camp = campaign.of(scfg)
        camp_speech = campaign.speech_pattern(camp)
        extra_found = []
        if scfg.get("hot_takes") or camp_speech is not None:
            t = time.time()
            log("listening for " + ("hot takes and NPC moments" if scfg.get("hot_takes") else "")
                + (" + " if scfg.get("hot_takes") and camp_speech is not None else "")
                + ("the campaign's phrases" if camp_speech is not None else "") + " (this takes a while)...", tag)
            try:
                for sec, snip, strength, kind in take_scan(preview, cfg, extra=camp_speech,
                                                           only_extra=not scfg.get("hot_takes")):
                    if kind == "campaign":
                        extra_found.append((sec, f'campaign content: streamer said "{snip[:90]}"', strength))
                    else:
                        found.append((sec, (f'streamer talking about an NPC / something weird: "{snip[:90]}"'
                                            if kind == "npc" else f'hot take: "{snip[:90]}"'), strength, {}))
            except Exception as e:
                log(f"hot-take scan skipped ({str(e)[:80]})", tag)
            times["speech_scan"] = round(time.time() - t, 1)

        crowd_thread.join()
        crowd_found = sorted(crowd_res.get("found") or [], key=lambda e: -e[2])
        chat = crowd_res.get("chat")
        if crowd_res.get("summary"):
            log("crowd signals: " + ", ".join(f"{k} {v}" for k, v in crowd_res["summary"].items()), tag)
        # crowd first, then the detectors (strongest first inside each group) -> merged moments, best first
        moments = _merge_moments(crowd_found + sorted(found, key=lambda e: -e[2]))
        moments.sort(key=lambda m: -m["score"])
        if top:
            moments = moments[: top * 2]
        if not moments:
            moments = [{"sec": s, "why": "loud moment", "clips": [], "signals": {}, "span": None, "crowd": False,
                        "score": 0.0} for s in loud_peaks(preview, top * 2 if top else 20)]
        n_crowd = sum(1 for m in moments if m["crowd"])
        log(f"{len(moments)} candidate moment(s) found ({n_crowd} with crowd signals, "
            f"{len(moments) - n_crowd} from the detectors only)", tag)
        if report is not None:
            report_meta = report.setdefault("_meta", {})
            report_meta[vod["id"]] = {"stages": times, "crowd": crowd_res.get("summary") or {},
                                      "moments": len(moments), "crowd_moments": n_crowd}
        # campaign moments: an extra group (not already covered by a regular moment), with its own limit
        extra = []
        for sec, why, strength in sorted(extra_found, key=lambda e: -e[2]):
            if all(abs(m["sec"] - sec) >= 60 for m in moments) and all(abs(x["sec"] - sec) >= 60 for x in extra) \
                    and (not top or len(extra) < top * 2):
                extra.append({"sec": sec, "why": why, "clips": [], "signals": {}, "span": None, "crowd": False,
                              "score": scoring.priority(replay.as_cand(why))})
        if extra:
            log(f"{len(extra)} extra moment(s) with the campaign's content", tag)
        todo = [(m, "main") for m in moments] + [(m, "campaign") for m in extra]
        clip_lead = float(det.get("clip_lead_in", 25))
        for i, (m, group) in enumerate(todo):
            if top and (saved if group == "main" else saved_extra) >= top:
                continue
            t_moment = time.time()
            sec = m["sec"]
            pad = cfg["render"].get("raw_padding", 60)          # extra minute each side for raw.mp4
            lo = min(sec - before, (m["span"][0] - clip_lead) if m["span"] else sec - before)
            hi = max(sec + after, (m["span"][1] + 10) if m["span"] else sec + after)
            a = max(0.0, lo - pad)
            b = hi + pad
            seg, start = None, a
            for attempt in range(3):
                stem = f"seg{i}_{attempt}"
                try:
                    _yt_dlp(["-f", "best[height<=1080]/bestvideo[height<=1080]+bestaudio/best",
                             "--download-sections", f"*{a:.0f}-{b:.0f}", "--merge-output-format", "mp4",
                             "-o", f"{stem}.%(ext)s", vod["url"]], work)
                except Exception as e:
                    log(f"could not download moment at {sec:.0f}s ({str(e)[:100]})", tag)
                    break
                seg = _find(work, stem)
                if not seg:
                    break
                got = _seg_start(preview, seg, a)
                start = a if got is None else got
                drift = start - a
                if abs(drift) <= 5 or before <= sec - start <= before + pad + 20:
                    break                            # close enough: the moment is inside with its context
                if attempt == 2:
                    log(f"video piece for {sec / 60:.0f} min still {drift:+.0f}s off - skipping it", tag)
                    seg = None
                    break
                log(f"video piece for {sec / 60:.0f} min came back {drift:+.0f}s off - asking again", tag)
                try:
                    os.remove(seg)
                except OSError:
                    pass
                seg = None
                a, b = max(0.0, a - drift), b - drift
            if not seg:
                continue
            c = Candidate(
                login=scfg["login"], display_name=vod["author"], rec_path=seg, anchor=sec - start,
                source=f"past stream, {m['why']} at {sec / 60:.0f} min", streamer_cfg=scfg,
                game=cfg["game"]["name"], platform=scfg["platform"], stream_title=vod["title"],
                signals=dict(m["signals"]), flagged_wall=time.time(),
            )
            if m["clips"]:
                c.clips = list(m["clips"])
                c.clip_span = (m["span"][0] - start, m["span"][1] - start) if m["span"] else None
                if crowd.strong(crowd.ClipMoment(0, 0, c.clips), cfg):
                    c.keep_bonus = int(det.get("clip_keep_bonus", 1))
            if chat is not None:                     # the chat replay, read like the live chat
                c.chat, c.t0 = chat, start
            c.priority = m["score"]
            pipeline.process(c)
            if c.folder:
                if group == "main":
                    saved += 1
                else:
                    saved_extra += 1
            _report_row(report.setdefault("rows", []) if report is not None else None, vod, m, c,
                        time.time() - t_moment)
            try:
                os.remove(seg)
            except OSError:
                pass
        _keep_preview(cfg, scfg, vod, preview, moments, crowd_res.get("summary") or {})
    finally:
        stop_beat.set()
        shutil.rmtree(work, ignore_errors=True)
    log(f"past stream done: {saved} clip(s) saved" + (f" + {saved_extra} with the campaign's content"
                                                       if saved_extra else ""), tag)
    return saved + saved_extra


def _keep_preview(cfg, scfg, vod, preview, moments, crowd_summary):
    """Phase 6 keeps the preview for cutting by hand; until then, nothing."""
    return None


def run_vods(cfg, hours=36, top=0, urls=None, streamer=None, any_game=False, no_ai=False, report_dir=None):
    require_binary("ffmpeg")
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        raise SystemExit("ERROR: yt-dlp is not installed. Run: pip install -r requirements.txt")
    vcfg = copy.deepcopy(cfg)
    vcfg["clips"]["max_per_hour_total"] = 0           # hourly caps are for live mode only
    vcfg["clips"]["max_per_hour_per_streamer"] = 10 ** 6
    pipeline = Pipeline(vcfg, Transcriber(cfg["transcription"]), ClipJudge(cfg, enabled=not no_ai))

    done_path = os.path.join(cfg["storage"]["output_dir"], ".past_streams_done.json")
    os.makedirs(cfg["storage"]["output_dir"], exist_ok=True)
    try:
        with open(done_path, "r", encoding="utf-8") as f:
            done = set(json.load(f))
    except (OSError, ValueError):
        done = set()

    def mark(vid):
        done.add(vid)
        with open(done_path, "w", encoding="utf-8") as f:
            json.dump(sorted(done), f)

    jobs = []
    if urls:
        name = (streamer or "pastclips").lower()
        scfg = next((s for s in cfg["streamers"] if s["login"] == name), None) or {
            "login": name, "key": name, "platform": "video", "tags": []}
        for u in urls:
            plat = "kick" if "kick.com" in u else "youtube" if "youtu" in u else "twitch" if "twitch" in u else "video"
            jobs.append((dict(scfg, platform=scfg.get("platform") or plat),
                         {"id": f"url_{u}", "url": u, "title": "", "duration": 0, "author": scfg["login"]}))
    else:
        since = time.time() - hours * 3600
        chosen = cfg["streamers"]
        if streamer:   # only this streamer's past streams
            chosen = [s for s in chosen if s["login"] == streamer.lower().lstrip("@")]
            if not chosen:
                raise SystemExit(f"ERROR: '{streamer}' is not in the streamers list of config.yaml")
        for s in chosen:
            vods = list_past_streams(s, since)
            log(f"{len(vods)} past stream(s) in the last {hours} h", s["key"])
            for v in vods:
                if v["id"] in done:
                    continue
                if not any_game and not s.get("any_game") and not game_ok(cfg, s, v["title"], v.get("categories", "")):
                    log(f"skipped (title doesn't mention the game): '{v['title'][:60]}'", s["key"])
                    continue
                jobs.append((s, v))

    log(f"{len(jobs)} past stream(s) to clip")
    total = 0
    report = {} if report_dir else None
    for scfg, vod in jobs:
        try:
            total += clip_past_stream(cfg, scfg, vod, pipeline, top, report)
            if not report_dir:                   # test runs don't mark the stream as done
                mark(vod["id"])
        except Exception as e:
            log(f"failed: {str(e)[:200]}", scfg.get("key", scfg["login"]))
        if report_dir:
            write_report(report_dir, report)
    log(f"all past streams done: {total} clip(s) saved in {cfg['storage']['output_dir']}")


def write_report(folder, report):
    """moments.csv (one line per moment tried) + report.json (stage times, crowd summary, Claude cost)."""
    import csv
    os.makedirs(folder, exist_ok=True)
    rows = report.get("rows") or []
    cols = ["stream", "time", "second", "signals", "crowd", "combined_score", "claude_score", "kept", "status",
            "reason", "folder", "seconds_to_clip", "claude_cost_usd"]
    with open(os.path.join(folder, "moments.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})
    meta = dict(report.get("_meta") or {})
    costs = [r["claude_cost_usd"] for r in rows if isinstance(r.get("claude_cost_usd"), (int, float))]
    meta["_totals"] = {"moments_tried": len(rows), "kept": sum(1 for r in rows if r.get("kept")),
                       "crowd_moments_tried": sum(1 for r in rows if r.get("crowd")),
                       "claude_cost_usd": round(sum(costs), 4) if costs else None}
    with open(os.path.join(folder, "report.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
