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
import time

from . import campaign
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


def clip_past_stream(cfg, scfg, vod, pipeline, top):
    tag = scfg.get("key", scfg["login"])
    work = tempfile.mkdtemp(prefix="frenzo_vod_")
    stop_beat = _keep_alive(work)              # the app's temp cleanup must never delete this folder while we use it
    before, after = cfg["clips"]["context_before"], cfg["clips"]["context_after"]
    saved = saved_extra = 0
    try:
        log(f"past stream: '{vod['title'][:60]}' ({vod['duration'] / 3600:.1f} h) - downloading a small preview", tag)
        # a tiny low-resolution copy (sound + picture): enough to hear reactions and see gameplay chaos
        try:
            _yt_dlp(["-f", "worst[height>=144][acodec!=none]/worst[acodec!=none]/worst", "-o", "preview.%(ext)s", vod["url"]], work)
        except Exception:
            _yt_dlp(["-f", "bestaudio/Audio_Only/audio_only/worst", "-o", "preview.%(ext)s", vod["url"]], work)
        preview = _find(work, "preview")
        if not preview:
            raise RuntimeError("preview download produced no file")

        found = []      # (second, why, strength)
        log("listening for streamer reactions...", tag)
        for sec, label, strength in scan_file(preview, cfg["detection"].get("voice_sensitivity", "normal")):
            found.append((sec, f"streamer reaction: {label}", strength))
        if cfg["detection"].get("chaos_triggers", True):
            log("watching the gameplay for bugs / crazy physics...", tag)
            try:
                found += [(sec, f"gameplay chaos: {r:.1f}x wilder than normal (bug/physics?)", r * 4)
                          for sec, r in chaos_scan(preview, cfg["detection"])]
            except Exception as e:
                log(f"chaos scan skipped ({str(e)[:80]})", tag)
        # campaign: EXTRA content it wants (searched on top of everything; the regular moments stay the same)
        camp = campaign.of(scfg)
        camp_speech = campaign.speech_pattern(camp)
        extra_found = []
        if scfg.get("hot_takes") or camp_speech is not None:
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
                                            if kind == "npc" else f'hot take: "{snip[:90]}"'), strength))
            except Exception as e:
                log(f"hot-take scan skipped ({str(e)[:80]})", tag)
        # strongest first, one moment per minute; several signals close together = stronger
        found.sort(key=lambda e: -e[2])
        moments = []
        for sec, why, strength in found:
            near = [m for m in moments if abs(m[0] - sec) < 60]
            if near:
                if why.split(":")[0] not in near[0][1]:
                    near[0][1] += " + " + why
                continue
            moments.append([sec, why])
            if top and len(moments) >= top * 2:         # top 0 = no limit: every moment found is tried
                break
        moments = [tuple(m) for m in moments]
        if not moments:
            moments = [(s, "loud moment") for s in loud_peaks(preview, top * 2 if top else 20)]
        log(f"{len(moments)} candidate moment(s) found", tag)
        # campaign moments: an extra group (not already covered by a regular moment), with its own limit
        extra = []
        for sec, why, strength in sorted(extra_found, key=lambda e: -e[2]):
            if all(abs(m[0] - sec) >= 60 for m in list(moments) + extra) and (not top or len(extra) < top * 2):
                extra.append((sec, why))
        if extra:
            log(f"{len(extra)} extra moment(s) with the campaign's content", tag)
        todo = [(sec, why, "main") for sec, why in sorted(moments)] + [(sec, why, "campaign") for sec, why in sorted(extra)]
        for i, (sec, why, group) in enumerate(todo):
            if top and (saved if group == "main" else saved_extra) >= top:
                continue
            pad = cfg["render"].get("raw_padding", 60)          # extra minute each side for raw.mp4
            a = max(0.0, sec - before - pad)
            b = sec + after + pad
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
                found = _seg_start(preview, seg, a)
                start = a if found is None else found
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
                source=f"past stream, {why} at {sec / 60:.0f} min", streamer_cfg=scfg,
                game=cfg["game"]["name"], platform=scfg["platform"], stream_title=vod["title"],
            )
            if pipeline.process(c):
                if group == "main":
                    saved += 1
                else:
                    saved_extra += 1
            try:
                os.remove(seg)
            except OSError:
                pass
    finally:
        stop_beat.set()
        shutil.rmtree(work, ignore_errors=True)
    log(f"past stream done: {saved} clip(s) saved" + (f" + {saved_extra} with the campaign's content"
                                                       if saved_extra else ""), tag)
    return saved + saved_extra


def run_vods(cfg, hours=36, top=0, urls=None, streamer=None, any_game=False, no_ai=False):
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
    for scfg, vod in jobs:
        try:
            total += clip_past_stream(cfg, scfg, vod, pipeline, top)
            mark(vod["id"])
        except Exception as e:
            log(f"failed: {str(e)[:200]}", scfg.get("key", scfg["login"]))
    log(f"all past streams done: {total} clip(s) saved in {cfg['storage']['output_dir']}")
