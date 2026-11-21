"""The first 3 seconds and the pacing, enforced in code (Claude chooses, code checks):

- hook types: cold_open (1-1.5 s of the payoff, then a rewind/record-scratch back to the setup), reaction_first
  (the streamer's peak reaction, then what caused it), tension (start one beat before it goes wrong, a riser
  ending on the hit), quote (the confident line right before the fail);
- something moves (frame 0 zooms out 1.1 -> 1.0), the hook text is on screen and a sound lands in the first 1.0 s;
- never open on dead air (leading quiet is trimmed); the clip ends 1-2 s after the reaction (loops cleanly);
- pacing: no quiet, wordless stretch longer than ~1.5 s inside a piece (cut out), one short 0.5x slow-mo allowed."""
import statistics

TYPES = ("cold_open", "reaction_first", "tension", "quote")
DEFAULT_SFX = {"cold_open": ["record-scratch", "tape-rewind"], "reaction_first": ["record-scratch", "whoosh-fast"],
               "tension": ["riser-air"], "quote": ["vine-boom-slowed", "whoosh-fast"]}
OPEN_SFX = ["whoosh-fast", "vine-boom-slowed", "deep-boom", "whoosh"]


def mapper(segments, speeds=None, offset=0.0):
    """Window time -> output time through the pieces (and their speeds); None if that moment was cut out.
    offset: output seconds before the first piece (the hook piece)."""
    speeds = list(speeds or [1.0] * len(segments))

    def f(t):
        out = offset
        for (a, b), sp in zip(segments, speeds):
            if a <= t <= b:
                return out + (t - a) / sp
            out += (b - a) / sp
        return None
    return f


def hook_piece(d, win_dur):
    """(type, (in, out) window seconds or None) from Claude's answer, lengths clamped by type."""
    ht = d.get("hook_type") if d.get("hook_type") in TYPES else None
    try:
        a, b = float(d.get("hook_in")), float(d.get("hook_out"))
    except (TypeError, ValueError):
        return ht, None
    a, b = max(0.0, min(a, win_dur)), max(0.0, min(b, win_dur))
    if b - a < 0.5:
        return ht, None
    if ht == "cold_open":
        b = min(b, a + 1.5)
        a = min(a, b - 1.0)
    elif ht == "reaction_first":
        b = min(b, a + 2.5)
    return ht, (max(0.0, a), b)


def trim_leading_quiet(segs, loud, protect=None, max_trim=2.0, drop_db=10.0):
    """Don't open on dead air: drop up to max_trim s of quiet (loudness drop_db under the window's median) at the
    start of the first piece, never past `protect` (the flagged second)."""
    if not segs or not loud:
        return segs
    med = statistics.median(loud)
    a, b = segs[0]
    t = a
    while t < a + max_trim and t + 1 < b and int(t) < len(loud) and loud[int(t)] < med - drop_db:
        if protect is not None and t + 1 > protect - 1:
            break
        t += 1.0
    return [[t, b]] + [list(s) for s in segs[1:]]


def trim_dead_air(segs, words, loud, protect=None, gap=1.5, keep=0.3, drop_db=8.0):
    """Pacing: inside each piece, cut stretches longer than `gap` s with no words AND quiet sound (drop_db under the
    median), keeping `keep` s on each side. Never within 1.5 s of `protect` (the payoff)."""
    if not loud:
        return segs
    med = statistics.median(loud)
    spoken = [(float(w["start"]), float(w["end"])) for w in words or []]
    out = []
    for a, b in segs:
        cur = a
        t = a
        quiet_from = None
        while t < b:
            s = int(t)
            silent = s < len(loud) and loud[s] < med - drop_db and not any(x < t + 1 and t < y for x, y in spoken)
            near = protect is not None and abs(t - protect) < 1.5
            if silent and not near:
                quiet_from = t if quiet_from is None else quiet_from
            else:
                if quiet_from is not None and t - quiet_from >= gap:
                    out.append([cur, quiet_from + keep])
                    cur = t - keep
                quiet_from = None
            t += 1.0
        if quiet_from is not None and b - quiet_from >= gap + 1:
            b = quiet_from + keep
        if b - cur >= 1.0:
            out.append([cur, b])
    return [s for s in out if s[1] - s[0] >= 0.8] or [list(s) for s in segs]


def apply_slowmo(segs, slow, max_len=2.0):
    """One short 0.5x slow-motion on the payoff: [a, b] window seconds inside a piece. -> (segs, speeds)."""
    speeds = [1.0] * len(segs)
    try:
        a, b = float(slow[0]), float(slow[1])
    except (TypeError, ValueError, IndexError):
        return segs, speeds
    b = min(b, a + max_len)
    if b - a < 0.4:
        return segs, speeds
    out, sp = [], []
    done = False
    for (x, y) in segs:
        if not done and x <= a and b <= y:
            for p, s in (((x, a), 1.0), ((a, b), 0.5), ((b, y), 1.0)):
                if p[1] - p[0] >= 0.2:
                    out.append(list(p))
                    sp.append(s)
            done = True
        else:
            out.append([x, y])
            sp.append(1.0)
    return out, sp


def ensure_opening_sound(cues, hook_type, hook_len, lib, fix_id, kind_of=None):
    """A sound must land in the first 1.0 s; a cold open / reaction-first gets its rewind or scratch on the cut back.
    Returns (cues, notes)."""
    notes = []
    cues = list(cues)
    if hook_type in ("cold_open", "reaction_first") and hook_len:
        if not any(abs(c["t"] - hook_len) < 0.25 for c in cues):
            sid = next((fix_id(s, lib) for s in DEFAULT_SFX[hook_type] if fix_id(s, lib)), None)
            if sid:
                cues.append({"t": round(hook_len, 3), "sfx": sid, "role": "support", "align": "start", "gain_db": -3.0,
                             "stop": 0.0, "duck": False, "kind": "transition", "max_len": 1.5, "fx": [], "auto": True})
                notes.append(f"added {sid} on the cut back at {hook_len:.2f}s")
    if not any(c["t"] < 1.0 for c in cues):
        sid = next((fix_id(s, lib) for s in OPEN_SFX if fix_id(s, lib)), None)
        if sid:
            cues.append({"t": 0.15, "sfx": sid, "role": "support", "align": "peak" if "boom" in sid else "start",
                         "gain_db": -4.0, "stop": 0.0, "duck": False, "kind": "hit", "max_len": 1.5, "fx": [], "auto": True})
            notes.append(f"added {sid} at 0.15s (a sound must land in the first second)")
    return sorted(cues, key=lambda c: c["t"]), notes
