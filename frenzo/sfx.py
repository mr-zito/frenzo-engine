"""Sound effects for clips.

Your own files win: put .mp3/.wav/.ogg files in assets/sfx/<type>/ (for example assets/sfx/sus/) and the
engine picks one at random. If a folder is empty, a built-in sound made by the engine itself is used.
Types: sus, boom, dramatic, fail, whoosh, ding
"""
import os
import random
import wave
from pathlib import Path

import numpy as np

from .config import ROOT

TYPES = ("sus", "boom", "dramatic", "fail", "whoosh", "ding")
RATE = 44100
SFX_DIR = ROOT / "assets" / "sfx"


def _env(n, attack=0.005, release=0.3):
    t = np.arange(n) / RATE
    a = np.clip(t / attack, 0, 1)
    r = np.exp(-t / release)
    return a * r


def _reverb(x, delays=(0.031, 0.047, 0.071, 0.097), decay=0.35):
    out = np.concatenate([x, np.zeros(int(RATE * 0.6))])
    for i, d in enumerate(delays):
        k = int(d * RATE)
        out[k:k + len(x)] += x * decay ** (i + 1)
    return out


def _tone(freq, dur, harmonics=(1, 0.5, 0.25), glide_to=None):
    n = int(dur * RATE)
    f = np.full(n, float(freq)) if glide_to is None else np.linspace(freq, glide_to, n)
    phase = 2 * np.pi * np.cumsum(f) / RATE
    return sum(a * np.sin(phase * (i + 1)) for i, a in enumerate(harmonics))


def _make(kind, seed=7):
    rng = np.random.default_rng(seed)
    if kind == "boom":        # deep cinematic hit
        n = int(1.4 * RATE)
        x = _tone(80, 1.4, (1, 0.35, 0.12), glide_to=38) * _env(n, 0.003, 0.45)
        x += 0.25 * rng.standard_normal(n) * _env(n, 0.001, 0.03)
        x = np.tanh(2.2 * x)
    elif kind == "sus":       # suspicious low two-note sting
        a = _tone(146.8, 0.45, (1, 0.6, 0.3, 0.15)) * _env(int(0.45 * RATE), 0.01, 0.35)
        b = _tone(138.6, 0.9, (1, 0.6, 0.3, 0.15)) * _env(int(0.9 * RATE), 0.01, 0.6)
        x = np.concatenate([a, b])
        x += 0.5 * np.concatenate([np.zeros(int(0.45 * RATE)), _tone(69.3, 0.9, (1, 0.4)) * _env(int(0.9 * RATE), 0.01, 0.6)])
        x = _reverb(np.tanh(1.5 * x))
    elif kind == "dramatic":  # rising tension then a hit
        n = int(1.2 * RATE)
        rise = _tone(110, 1.2, (1, 0.5, 0.3), glide_to=440) * np.linspace(0, 1, n) ** 2
        noise = rng.standard_normal(n) * np.linspace(0, 0.4, n) ** 2
        hit = _make("boom")
        x = np.concatenate([0.5 * rise + 0.3 * noise, hit])
    elif kind == "fail":      # sad descending "wah wah"
        parts = []
        for f, d in ((311, 0.35), (294, 0.35), (277, 0.35), (262, 0.9)):
            m = int(d * RATE)
            vib = 1 + (0.012 * np.sin(2 * np.pi * 5.5 * np.arange(m) / RATE) if d > 0.5 else 0)
            parts.append(_tone(f, d, (1, 0.7, 0.5, 0.3), glide_to=f * (0.94 if d > 0.5 else 1)) * vib * _env(m, 0.02, d))
        x = np.tanh(1.3 * np.concatenate(parts))
    elif kind == "whoosh":    # fast air swoosh
        n = int(0.6 * RATE)
        noise = rng.standard_normal(n)
        spec = np.fft.rfft(noise)
        freqs = np.fft.rfftfreq(n, 1 / RATE)
        spec *= np.exp(-((freqs - 1800) / 1500) ** 2)
        x = np.fft.irfft(spec, n) * np.sin(np.linspace(0, np.pi, n)) ** 2 * 6
    else:                     # ding: bright bell
        n = int(1.2 * RATE)
        x = (_tone(1318.5, 1.2, (1,)) + 0.4 * _tone(2637, 1.2, (1,)) + 0.2 * _tone(3951, 1.2, (1,))) * _env(n, 0.002, 0.35)
    x = x / (np.abs(x).max() + 1e-9) * 0.9
    return x


def builtin_path(kind):
    out = SFX_DIR / "_builtin" / f"{kind}.wav"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        data = (_make(kind) * 32767).astype(np.int16)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(data.tobytes())
    return str(out)


def pick(kind):
    kind = (kind or "").lower().strip()
    if kind not in TYPES:
        return None
    folder = SFX_DIR / kind
    own = [str(p) for p in folder.glob("*") if p.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a")] if folder.exists() else []
    return random.choice(own) if own else builtin_path(kind)


def ensure_folders():
    for k in TYPES:
        (SFX_DIR / k).mkdir(parents=True, exist_ok=True)
