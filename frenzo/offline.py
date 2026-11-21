"""Test mode: run the clip pipeline on a video file you already have (e.g. a downloaded VOD)."""
import os

import numpy as np

from .ai import ClipJudge
from .analyzer import Transcriber, extract_audio
from .pipeline import Candidate, Pipeline
from .util import log, require_binary, run


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path], capture=True)
    return float(out.decode().strip())


def loud_peaks(path, top, min_gap=60):
    """Find the loudest moments of a whole file (cheap 4 kHz pass)."""
    dur = duration(path)
    rate = 4000
    audio = extract_audio(path, 0, dur, rate=rate)
    n = len(audio) // rate
    if n == 0:
        return []
    rms = np.sqrt((audio[: n * rate].reshape(n, rate) ** 2).mean(axis=1)) + 1e-9
    db = 20 * np.log10(rms)
    smooth = np.convolve(db, np.ones(5) / 5, mode="same")
    order = np.argsort(-smooth)
    picks = []
    for i in order:
        if all(abs(i - p) >= min_gap for p in picks):
            picks.append(int(i))
        if len(picks) >= top:
            break
    return sorted(picks)


def run_test(cfg, video, at=None, top=3, streamer="teststreamer", no_ai=False):
    require_binary("ffmpeg")
    require_binary("ffprobe")
    if not os.path.exists(video):
        raise SystemExit(f"ERROR: video not found: {video}")
    video = os.path.abspath(video)
    if at:
        moments = [(a, "test mode (chosen time)") for a in at]
    else:
        from .voice import scan_file
        log("listening to the whole video for streamer reactions...", "test")
        found = scan_file(video, cfg["detection"].get("voice_sensitivity", "normal"))
        found = sorted(found, key=lambda e: -e[2])[:top]
        moments = [(sec, f"streamer reaction: {label}") for sec, label, _ in sorted(found)]
        for sec, label in moments:
            log(f"  {sec:7.1f}s  {label}", "test")
        if not moments:
            log("no clear reactions found; using the loudest moments instead", "test")
            moments = [(a, "test mode (loud moment)") for a in loud_peaks(video, top)]
    anchors = [m[0] for m in moments]
    log(f"testing {len(anchors)} moment(s) at: {', '.join(f'{a:.0f}s' for a in anchors)}", "test")
    scfg = next((s for s in cfg["streamers"] if s["login"] == streamer.lower()),
                {"login": streamer.lower(), "tags": []})
    pipeline = Pipeline(cfg, Transcriber(cfg["transcription"]), ClipJudge(cfg, enabled=not no_ai))
    made = []
    for a, source in moments:
        c = Candidate(login=scfg["login"], display_name=streamer, rec_path=video, anchor=float(a),
                      source=source,
                      streamer_cfg=scfg, game=cfg["game"]["name"], platform="test")
        folder = pipeline.process(c)
        if folder:
            made.append(folder)
    log(f"done: {len(made)} clip(s) saved", "test")
    return made
