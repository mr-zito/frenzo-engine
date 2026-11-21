"""Learned chat delay, per streamer.

Chat reacts a few seconds after what it sees, and how late depends on the platform and the streamer's
audience (Kick and YouTube are usually slower than Twitch low-latency). Each time the streamer has a voice
reaction (yell / laugh / outburst), the chat rate in the 25 s after it is recorded, relative to the minute
before. Averaging those curves over many reactions is a cross-correlation of chat activity with the
streamer's reactions: its peak is the delay. A rolling store keeps the last 40 reactions per streamer;
with fewer than 5 the platform's fallback is used."""
import json
import os
import threading

import numpy as np

FALLBACK = {"twitch": 6.0, "kick": 8.0, "youtube": 10.0}
MAX_LAG = 25            # seconds after the reaction that are looked at
MIN_EVENTS = 5
KEEP = 40
_lock = threading.Lock()


def _path(cfg):
    return os.path.join(cfg["storage"]["output_dir"], ".chat_delay.json")


def _load(cfg):
    try:
        with open(_path(cfg), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def curve(chat, t_event, lag=MAX_LAG, base_s=60):
    """Chat messages per second after a reaction at t_event, minus the per-second rate of the minute before
    (so 0 = normal). None if chat was too quiet to say anything."""
    base = chat.count_between(t_event - base_s, t_event - 2) / max(1.0, base_s - 2)
    per_s = [chat.count_between(t_event + i, t_event + i + 1) for i in range(lag + 1)]
    if sum(per_s) < 5:
        return None
    return [round(v - base, 2) for v in per_s]


def estimate(curves):
    """Delay in seconds = the peak of the average (3-second smoothed) chat curve after reactions."""
    if len(curves) < MIN_EVENTS:
        return None
    m = np.mean(np.array([c[:MAX_LAG + 1] for c in curves if len(c) >= MAX_LAG + 1]), axis=0)
    if m.size == 0 or m.max() <= 0:
        return None
    sm = np.convolve(m, np.ones(3) / 3, mode="same") + 1e-3 * m    # smoothed; ties go to the raw peak
    return float(int(np.argmax(sm[1:])) + 1)          # chat can't react at lag 0


def add_curve(cfg, key, c):
    with _lock:
        data = _load(cfg)
        entry = data.setdefault(key, {"curves": []})
        entry["curves"] = (entry["curves"] + [c])[-KEEP:]
        d = estimate(entry["curves"])
        entry["delay"] = d
        try:
            os.makedirs(os.path.dirname(_path(cfg)), exist_ok=True)
            tmp = _path(cfg) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, _path(cfg))
        except OSError:
            pass
        return d


def delay_for(cfg, key, platform="twitch"):
    """The learned delay for a streamer (clamped 1-20 s), else the platform's fallback."""
    fb = FALLBACK.get(platform, float((cfg.get("detection") or {}).get("chat_delay_seconds", 6)))
    if not (cfg.get("detection") or {}).get("learned_chat_delay", True):
        return fb
    d = (_load(cfg).get(key) or {}).get("delay")
    return max(1.0, min(20.0, float(d))) if isinstance(d, (int, float)) else fb


def learned(cfg, key):
    """The measured delay (None until enough reactions were measured)."""
    d = (_load(cfg).get(key) or {}).get("delay")
    return float(d) if isinstance(d, (int, float)) else None
