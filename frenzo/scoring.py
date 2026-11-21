"""Combined moment score: how strong each signal was, how many different signals agree, and crowd evidence
(viewer clips, YouTube "Most replayed" peaks, chat replay). It orders the editing queue (best first), decides
which moment the hourly cap keeps, and is shown to Claude with its breakdown."""
import math
import re

# signal family -> pattern in a moment's "source" text, and how its number becomes a strength (~ a z-score)
_FAMILIES = [
    ("chat_spike", re.compile(r"chat spike: ([\d.]+)x"), lambda x: min(4.0, max(0.5, (x - 1.0) * 0.8))),
    ("chat_words", re.compile(r"chat (?:thinks it's sus|is thirsty|says bug|reacting to an NPC|hyped by the gameplay|"
                              r"said something)[^:]*: (\d+)"), lambda n: min(3.0, 0.5 + n / 6.0)),
    ("voice", re.compile(r"streamer reaction: ([a-z ]+)"), lambda s: 2.0 if ("laugh" in s or "yell" in s) else 1.5),
    ("chaos", re.compile(r"gameplay chaos: (?:picture went )?([\d.]+)x"), lambda x: min(4.0, max(0.5, (x - 1.5) * 0.9))),
    ("speech", re.compile(r"(hot take|streamer talking about an NPC|streamer said something)"), lambda _s: 1.5),
    ("campaign", re.compile(r"campaign content"), lambda _s: 1.0),
    ("loudness", re.compile(r"loudness jump of ([\d.]+) dB"), lambda x: min(3.0, x / 8.0)),
    ("chat_replay", re.compile(r"chat replay: ([\d.]+)x"), lambda x: min(4.0, max(0.5, (x - 1.0) * 0.8))),
    ("heatmap", re.compile(r"most replayed: ([\d.]+)"), lambda x: min(4.0, 1.0 + 3.0 * x)),
]


def breakdown(cand):
    """{family: strength} for one moment, from its source text, its signals dict and its viewer clips."""
    out = {}
    src = getattr(cand, "source", "") or ""
    for name, pat, f in _FAMILIES:
        for m in pat.finditer(src):
            g = m.group(1) if m.groups() else None
            try:
                val = f(float(g) if g and re.fullmatch(r"[\d.]+", g) else g)
            except (TypeError, ValueError):
                continue
            out[name] = max(out.get(name, 0.0), round(val, 2))
    for k, v in (getattr(cand, "signals", None) or {}).items():
        try:
            out[k] = max(out.get(k, 0.0), round(float(v), 2))
        except (TypeError, ValueError):
            pass
    clips = getattr(cand, "clips", None) or []
    if clips:
        from .crowd import ClipMoment
        m = ClipMoment(0, 0, clips)
        out["viewer_clips"] = round(1.5 * m.clippers + (3.0 if m.featured else 0.0) + 0.5 * math.log10(1 + m.views), 2)
    return out


def combined(parts):
    """Sum of the signal strengths + a bonus for every extra signal that agrees (several signals = real moment)."""
    if not parts:
        return 0.0
    agree = max(0, len([v for v in parts.values() if v > 0]) - 1)
    return round(sum(parts.values()) + 0.75 * agree, 2)


def priority(cand):
    return combined(breakdown(cand))


def describe(cand):
    """One line for Claude: the combined score and its breakdown."""
    parts = breakdown(cand)
    if not parts:
        return ""
    return (f"\nCOMBINED SIGNAL SCORE: {combined(parts)} (typical 2-4, strong 6+): "
            + ", ".join(f"{k} {v}" for k, v in sorted(parts.items(), key=lambda kv: -kv[1]))
            + ". It says how sure the detectors and the crowd are; judge the clip by what actually happens.")
