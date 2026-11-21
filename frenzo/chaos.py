"""Chaos detector: finds bug / crazy-physics moments (car flying off a bridge, ragdolls, spinning camera)
by watching the gameplay itself, no chat or voice needed.

Every few seconds it looks at the newest ~12 s of video in small grey frames and measures how much the
picture changes from frame to frame. Each stream has its own normal level (driving fast is always busy),
learned over the last minutes. A moment is flagged when the change is suddenly far above that normal
for at least half a second. A hard cut (menu, map, loading screen) is a single huge jump between two
calm frames: that is ignored.
"""
import collections
import statistics
import subprocess

import numpy as np

FPS = 8
SIZE = (128, 72)


def motion_series(path, start, dur):
    """Frame-to-frame change (0-255 scale) for each frame step, plus the time of each step."""
    w, h = SIZE
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", path,
         "-vf", f"fps={FPS},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    n = len(raw) // (w * h)
    if n < 3:
        return np.zeros(0), np.zeros(0)
    f = np.frombuffer(raw[: n * w * h], np.uint8).reshape(n, h, w).astype(np.float32)
    f = f[:, int(h * 0.12): int(h * 0.88)]          # ignore top/bottom HUD rows
    d = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))
    t = start + (np.arange(1, n) / FPS)
    return d, t


def remove_cuts(d):
    """A hard cut is one huge step between calm steps: replace it by its neighbours' level."""
    d = d.copy()
    if len(d) < 3:
        return d
    med = float(np.median(d)) + 1e-6
    for i in range(1, len(d) - 1):
        if d[i] > 4 * med and d[i - 1] < 2 * med and d[i + 1] < 2 * med:
            d[i] = (d[i - 1] + d[i + 1]) / 2
    return d


class ChaosAnalyzer:
    """Feed consecutive motion series; returns chaos moments as (second, strength)."""

    def __init__(self, cooldown=60, ratio=2.6, history_seconds=300):
        self.ratio = ratio
        self.cooldown = cooldown
        self.hist = collections.deque(maxlen=history_seconds * FPS)   # (time, value)
        self.last_t = -1e9
        self.last_trigger = -1e9

    def feed(self, d, t):
        d = remove_cuts(d)
        new = t > self.last_t
        events = []
        base_vals = [v for _, v in self.hist]
        if len(base_vals) >= 40 * FPS // 2 and new.any():
            base = statistics.median(base_vals)
            mad = statistics.median([abs(v - base) for v in base_vals]) + 1e-6
            thresh = max(base * self.ratio, base + 6 * mad, 6.0)
            k = FPS // 2                                   # at least half a second of chaos
            if len(d) >= k:
                sustained = np.convolve(d, np.ones(k) / k, mode="valid")
                for i, v in enumerate(sustained):
                    tc = t[i + k // 2]
                    if v > thresh and tc > self.last_t and tc - self.last_trigger > self.cooldown:
                        events.append((float(tc), float(v / max(base, 1e-6))))
                        self.last_trigger = tc
        for ti, vi in zip(t[new], d[new]):
            self.hist.append((float(ti), float(vi)))
        if len(t):
            self.last_t = max(self.last_t, float(t[-1]))
        return events


class ChaosDetector:
    """Live version, used by a recording session."""

    def __init__(self, recorder, cfg, every=8):
        self.recorder = recorder
        self.every = every
        self.an = ChaosAnalyzer(cfg["cooldown_seconds"], cfg.get("chaos_ratio", 2.6))
        self.last_check = 0.0

    def check(self, now):
        rec = self.recorder.recorded_seconds()
        if now - self.last_check < self.every or rec < 30:
            return None
        self.last_check = now
        start = max(0.0, rec - 14)
        try:
            d, t = motion_series(self.recorder.path, start, 12)
        except Exception:
            return None
        events = self.an.feed(d, t)
        if not events:
            return None
        sec, strength = max(events, key=lambda e: e[1])
        return self.recorder.t0 + sec, strength
