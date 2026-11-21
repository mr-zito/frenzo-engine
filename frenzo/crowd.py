"""Crowd evidence: viewer Twitch clips (and, for past streams, chat replay and YouTube's "Most replayed").

A viewer clip is a real person deciding a moment was worth saving, so clips confirm or create moments
instead of being one more detector:
  - clips whose spans overlap are ONE moment; its strength is the number of distinct clippers
    (clip bots and the broadcaster's own clips don't count), a featured clip counts as very strong;
  - live, Twitch gives no position for a clip (vod_offset is null until the stream ends): its span is
    estimated from created_at - duration - delay, then found exactly by matching the clip's sound
    against our own recording (cross-correlation of the loudness envelopes).
"""
import datetime as dt
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

DEFAULT_BOTS = ("streamelements", "nightbot", "moobot", "fossabot", "streamlabs", "wizebot", "sery_bot",
                "soundalerts", "botrixoficial", "kofistreambot", "clipbot", "clip_bot", "twitchclipsbot")


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text):
    return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


@dataclass
class ViewerClip:
    id: str
    url: str = ""
    creator_id: str = ""
    creator: str = ""
    title: str = ""
    created: float = 0.0              # wall time the clip was made
    duration: float = 30.0
    views: int = 0
    featured: bool = False
    video_id: str = ""                # the VOD it belongs to ("" while the stream is live)
    vod_offset: Optional[float] = None
    game_id: str = ""
    broadcaster_id: str = ""
    span: Optional[tuple] = None      # (a, b) on the timeline in use (recording seconds, VOD seconds...)
    aligned: bool = False             # span found by matching the sound (exact) rather than estimated
    views_hist: list = field(default_factory=list)   # [(wall time, views)]

    def as_dict(self):
        return {"id": self.id, "url": self.url, "creator": self.creator, "title": self.title,
                "views": self.views, "featured": self.featured, "duration": round(self.duration, 1),
                "created": iso(self.created) if self.created else None, "video_id": self.video_id or None,
                "vod_offset": self.vod_offset, "aligned": self.aligned}


def from_helix(c):
    """A Get Clips item -> ViewerClip."""
    created = parse_ts(c["created_at"]) if c.get("created_at") else 0.0
    off = c.get("vod_offset")
    return ViewerClip(
        id=str(c.get("id")), url=c.get("url") or f"https://clips.twitch.tv/{c.get('id')}",
        creator_id=str(c.get("creator_id") or ""), creator=str(c.get("creator_name") or ""),
        title=str(c.get("title") or ""), created=created, duration=float(c.get("duration") or 30.0),
        views=int(c.get("view_count") or 0), featured=bool(c.get("is_featured")),
        video_id=str(c.get("video_id") or ""), vod_offset=float(off) if off is not None else None,
        game_id=str(c.get("game_id") or ""), broadcaster_id=str(c.get("broadcaster_id") or ""),
        views_hist=[(created, 0)] if created else [])


def ignored(clip, bots=DEFAULT_BOTS, broadcaster_id=None):
    """Clip bots and clips the broadcaster made themselves don't count as viewers."""
    name = (clip.creator or "").lower()
    if name and name in {b.lower() for b in bots}:
        return True
    if broadcaster_id and clip.creator_id and clip.creator_id == str(broadcaster_id):
        return True
    return False


def live_span(clip, delay=6.0):
    """Wall-time estimate of what a clip made during a live stream shows: it ends about `delay` seconds
    before it was created (the clipper watches a little behind live) and lasts `duration`."""
    end = clip.created - delay
    return end - clip.duration, end


def vod_span(clip):
    if clip.vod_offset is None:
        return None
    return clip.vod_offset, clip.vod_offset + clip.duration


@dataclass
class ClipMoment:
    start: float
    end: float
    clips: list = field(default_factory=list)

    @property
    def clippers(self):
        return len({c.creator_id or c.creator or c.id for c in self.clips})

    @property
    def featured(self):
        return any(c.featured for c in self.clips)

    @property
    def views(self):
        return sum(c.views for c in self.clips)

    @property
    def titles(self):
        return list(dict.fromkeys(c.title.strip() for c in self.clips if c.title.strip()))

    def strength(self):
        return strength(self.clippers, self.featured, self.views)


def strength(clippers, featured=False, views=0):
    """Distinct clippers first; a featured clip (streamer or mod chose it) is very strong; views break ties."""
    return clippers + (3.0 if featured else 0.0) + math.log10(1 + max(0, views)) * 0.5


def group(clips, span_of=lambda c: c.span, gap=2.0):
    """Clips whose spans overlap (or nearly touch) -> one moment each. Clips without a span are left out."""
    items = sorted(((span_of(c), c) for c in clips if span_of(c)), key=lambda x: x[0][0])
    moments = []
    for (a, b), c in items:
        if moments and a <= moments[-1].end + gap:
            m = moments[-1]
            m.end = max(m.end, b)
            if all(x.id != c.id for x in m.clips):
                m.clips.append(c)
        else:
            moments.append(ClipMoment(a, b, [c]))
    return moments


def velocity(hist):
    """Views per minute between the last two checks (0 if unknown)."""
    if len(hist) < 2:
        return 0.0
    (t0, v0), (t1, v1) = hist[-2], hist[-1]
    return max(0.0, (v1 - v0) / max(1.0, (t1 - t0) / 60))


def strong(m, cfg):
    """A moment strong enough to count as a lesson / lower the keep threshold."""
    d = cfg.get("detection") or {}
    return m.featured or m.clippers >= int(d.get("clip_strong_clippers", 3)) or \
        m.views >= int(d.get("clip_strong_views", 1000))


# ------------------------------------------------------------------------------------------- audio matching
def envelope(audio, rate=8000, bin_s=0.1):
    """Loudness (dB) every bin_s seconds."""
    step = int(rate * bin_s)
    n = len(audio) // step
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    a = np.asarray(audio[: n * step], dtype=np.float32).reshape(n, step)
    return (20 * np.log10(np.sqrt((a ** 2).mean(axis=1)) + 1e-9)).astype(np.float32)


def best_offset(seg, full, min_corr=0.6):
    """Where the envelope `seg` sits inside `full` (index), by normalised cross-correlation; None if unsure."""
    seg, full = np.asarray(seg, dtype=np.float32), np.asarray(full, dtype=np.float32)
    if len(seg) < 20 or len(full) < len(seg):
        return None, 0.0
    s = (seg - seg.mean()) / (seg.std() + 1e-6)
    win = np.lib.stride_tricks.sliding_window_view(full, len(seg))
    w = (win - win.mean(axis=1, keepdims=True)) / (win.std(axis=1, keepdims=True) + 1e-6)
    corr = w @ s / len(s)
    i = int(np.argmax(corr))
    if corr[i] < min_corr:
        return None, float(corr[i])
    return i, float(corr[i])


def locate(piece_path, full_path, expected, search=480, min_len=20.0, min_corr=0.6, piece_max=600):
    """Where a short video/audio file (a clip, a downloaded piece) starts inside a longer recording, in seconds,
    searching expected +- search. None if no confident match."""
    from .analyzer import extract_audio
    try:
        seg = envelope(extract_audio(piece_path, 0, piece_max, rate=8000))
        lo = max(0.0, expected - search)
        full = envelope(extract_audio(full_path, lo, 2 * search + len(seg) / 10, rate=8000))
    except Exception:
        return None
    if len(seg) < min_len * 10:
        return None
    i, _ = best_offset(seg, full, min_corr)
    return None if i is None else lo + i / 10


def download_clip(clip_url, out_dir=None):
    """The clip's mp4 (small). yt-dlp knows Twitch clip pages."""
    out_dir = out_dir or tempfile.mkdtemp(prefix="frenzo_clip_")
    cmd = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-part", "-f", "best[height<=720]/best",
           "-o", os.path.join(out_dir, "clip.%(ext)s"), clip_url]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode("utf-8", "replace")[-300:])
    for f in os.listdir(out_dir):
        if f.startswith("clip."):
            return os.path.join(out_dir, f)
    raise RuntimeError("no clip file")


def prompt_text(clips):
    """What Claude is told about the viewer clips of a moment."""
    if not clips:
        return ""
    m = ClipMoment(0, 0, list(clips))
    titles = "; ".join(f'"{t[:80]}"' for t in m.titles[:6])
    return (f"\n\nVIEWER CLIPS: {m.clippers} different viewer(s) clipped this moment"
            + (" (one is FEATURED: the streamer or a mod picked it)" if m.featured else "")
            + f", {m.views} views so far." + (f" Viewers titled their clips: {titles}. Use these only to understand "
                                              "the scene; never copy them as our title." if titles else ""))
