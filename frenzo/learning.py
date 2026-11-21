"""Learning: the engine learns from the clips you make by hand, delete, retitle and re-time.

Files, all in <output_dir>/.learning/ :
  decisions.jsonl   one line per moment the engine judged (Claude or backup rule): why it was flagged, the score...
  examples.jsonl    your "lessons": MISSED moments you cut from a recording (with measured signals),
                    BAD PICKS you deleted, TITLE FIXES and TIMING FIXES
  learned.json      what was learned (editor notes, per-streamer detection values, keywords, margins)
  history/          every previous learned.json (so a learning run can be undone)
  state.json        when the last learning run happened and how many examples it saw

A learning run summarises ALL examples (stats, not raw dumps), asks Claude once what to change, and keeps the
answer inside hard safety limits. Live clipping uses learned.json after a restart.
"""
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time

from .util import NO_WINDOW, log

_lock = threading.Lock()

# ---------------------------------------------------------------------------------------- safety limits
LIMITS = {                       # absolute bounds whatever Claude answers
    "spike_multiplier": (1.8, 5.0),
    "chaos_ratio": (1.8, 4.0),
    "min_messages_in_window": (5, 40),
    "min_score": (4, 9),
    "lead_in": (0.0, 6.0),
    "tail": (0.0, 6.0),
}
INT_KEYS = {"min_messages_in_window", "min_score"}
MAX_STEP = 0.25                  # a value moves at most 25% per run (min_score: at most 1 point)
VOICE_LEVELS = ["low", "normal", "high"]
STREAMER_KEYS = ("spike_multiplier", "min_messages_in_window", "voice_sensitivity", "chaos_ratio", "min_score",
                 "hot_takes")
MAX_KEYWORDS, MAX_KEYWORD_LEN = 20, 40
MIN_EXAMPLES = 3                 # never learn from one or two examples
AUTO_NEW_EXAMPLES, AUTO_MIN_GAP = 5, 2 * 3600


def clamp_step(key, old, new):
    """Keep a learned number inside its absolute bounds and at most one small step away from the current value."""
    lo, hi = LIMITS[key]
    try:
        new = float(new)
    except (TypeError, ValueError):
        return old
    if new != new:                                   # NaN
        return old
    if old is not None:
        old = float(old)
        if key == "min_score":
            new = max(old - 1, min(old + 1, new))
        else:
            span = abs(old) * MAX_STEP
            new = max(old - span, min(old + span, new))
    new = max(lo, min(hi, new))
    if key in INT_KEYS:
        new = int(round(new))
        new = max(int(lo), min(int(hi), new))
    else:
        new = round(new, 2)
    return new


def clean_keywords(items, limit=MAX_KEYWORDS):
    """Plain words / phrases only: trimmed, no control characters, at most 40 characters, no duplicates.
    They are always escaped (re.escape) before they are used, so they can never act as a regex."""
    out, seen = [], set()
    for k in items if isinstance(items, list) else []:
        if not isinstance(k, str):
            continue
        k = re.sub(r"\s+", " ", k).strip()
        if not (2 <= len(k) <= MAX_KEYWORD_LEN) or any(ord(ch) < 32 for ch in k):
            continue
        if k.lower() in seen:
            continue
        seen.add(k.lower())
        out.append(k)
        if len(out) >= limit:
            break
    return out


def keyword_pattern(words):
    """Learned keywords -> one case-insensitive pattern matching any of them as whole words / phrases."""
    words = clean_keywords(words)
    if not words:
        return None
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(r"(?<!\w)(?:" + alts + r")(?!\w)", re.IGNORECASE)


def _clean_notes(items, limit, maxlen=200):
    out = []
    for n in items if isinstance(items, list) else []:
        if isinstance(n, str) and n.strip():
            out.append(re.sub(r"\s+", " ", n).strip()[:maxlen])
        if len(out) >= limit:
            break
    return out


def sanitize(answer, current, cfg):
    """Claude's JSON -> a learned.json that respects every safety limit (whatever Claude answered)."""
    det, clips = cfg["detection"], cfg["clips"]
    cur_streamers = (current or {}).get("streamers") or {}
    known = {s["login"]: s for s in cfg.get("streamers") or []}
    out = {"editor_notes": _clean_notes(answer.get("editor_notes"), 12),
           "title_notes": _clean_notes(answer.get("title_notes"), 6),
           "chat_keywords": clean_keywords(answer.get("chat_keywords")),
           "speech_keywords": clean_keywords(answer.get("speech_keywords")),
           "summary": re.sub(r"\s+", " ", str(answer.get("summary") or "")).strip()[:1200],
           "streamers": {}}
    for key in ("lead_in", "tail"):
        old = (current or {}).get(key, clips.get(key))
        if answer.get(key) is not None:
            out[key] = clamp_step(key, old, answer[key])
        elif (current or {}).get(key) is not None:
            out[key] = current[key]
    for login, vals in (answer.get("streamers") or {}).items():
        login = str(login).lower().split(":")[-1].lstrip("@")
        if login not in known or not isinstance(vals, dict):
            continue
        cur = dict(cur_streamers.get(login) or {})
        new = {}
        for key in ("spike_multiplier", "min_messages_in_window", "chaos_ratio", "min_score"):
            base = cur.get(key, clips["min_score"] if key == "min_score" else det.get(key))
            if vals.get(key) is not None:
                new[key] = clamp_step(key, base, vals[key])
            elif key in cur:
                new[key] = cur[key]
        vs = vals.get("voice_sensitivity")
        base_vs = cur.get("voice_sensitivity", det.get("voice_sensitivity", "normal"))
        if vs in VOICE_LEVELS:                      # one level per run at most
            i, j = VOICE_LEVELS.index(base_vs if base_vs in VOICE_LEVELS else "normal"), VOICE_LEVELS.index(vs)
            new["voice_sensitivity"] = VOICE_LEVELS[i + max(-1, min(1, j - i))]
        elif "voice_sensitivity" in cur:
            new["voice_sensitivity"] = cur["voice_sensitivity"]
        if isinstance(vals.get("hot_takes"), bool):
            new["hot_takes"] = vals["hot_takes"]
        elif "hot_takes" in cur:
            new["hot_takes"] = cur["hot_takes"]
        if new:
            out["streamers"][login] = new
    for login, cur in cur_streamers.items():       # streamers Claude didn't mention keep their values
        if login not in out["streamers"] and login in known:
            out["streamers"][login] = cur
    return out


# ---------------------------------------------------------------------------------------------- files
def ldir(cfg_or_dir):
    root = cfg_or_dir if isinstance(cfg_or_dir, str) else cfg_or_dir["storage"]["output_dir"]
    return os.path.join(root, ".learning")


def _path(cfg, name):
    return os.path.join(ldir(cfg), name)


def _append(path, obj):
    with _lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_jsonl(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_learned(cfg_or_dir):
    data = _read_json(os.path.join(ldir(cfg_or_dir), "learned.json"), {})
    return data if isinstance(data, dict) else {}


def examples(cfg):
    return _read_jsonl(_path(cfg, "examples.jsonl"))


def decisions(cfg):
    return _read_jsonl(_path(cfg, "decisions.jsonl"))


def state(cfg):
    return _read_json(_path(cfg, "state.json"), {}) or {}


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------ 1. decisions
def log_decision(cfg, c, d, saved, win_start=None, note=None):
    """One line per moment the engine judged (live or past stream), never for your own cuts or re-edits."""
    try:
        d = d or {}
        _append(_path(cfg, "decisions.jsonl"), {
            "time": now_iso(), "login": c.login, "platform": c.platform,
            "recording": os.path.splitext(os.path.basename(c.rec_path))[0],
            "sec": round(float(c.anchor), 1), "window_start": None if win_start is None else round(win_start, 1),
            "source": (c.source or "")[:300], "past": (c.source or "").startswith("past stream"),
            "viral_score": d.get("viral_score"), "keep": d.get("keep"), "category": d.get("category"),
            "reason": str(note or d.get("reason") or "")[:300],
            "what_happens": str(d.get("what_happens") or "")[:300],
            "judged": note is None, "saved": bool(saved)})
    except Exception as e:          # learning must never break clipping
        log(f"couldn't write the decision log ({str(e)[:80]})", "learn")


# ------------------------------------------------------------------------------------------- 2. examples
def add_example(cfg, kind, data):
    ex = dict(data, type=kind, time=now_iso(), id=f"{kind}-{int(time.time() * 1000)}")
    _append(_path(cfg, "examples.jsonl"), ex)
    return ex


def remove_examples(cfg, pred):
    """Rewrite examples.jsonl without the examples matching pred (used to remove test examples)."""
    path = _path(cfg, "examples.jsonl")
    with _lock:
        rows = _read_jsonl(path)
        keep = [r for r in rows if not pred(r)]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in keep:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    return len(rows) - len(keep)


def bad_pick(cfg, meta):
    """You deleted one of the engine's clips."""
    return add_example(cfg, "deleted", {
        "login": meta.get("login"), "title": meta.get("title_bar") or meta.get("heading") or meta.get("title"),
        "category": meta.get("category"), "score": meta.get("viral_score"), "reason": str(meta.get("reason") or "")[:300],
        "flagged_by": str(meta.get("flagged_by") or "")[:300], "what_happens": str(meta.get("what_happens") or "")[:300],
        "duration": meta.get("duration"), "recording": os.path.splitext(str(meta.get("recording") or ""))[0]})


def title_fix(cfg, meta, new_title, source="typed"):
    old = meta.get("title_bar") or meta.get("heading") or meta.get("title") or ""
    if not new_title or new_title.strip() == old.strip():
        return None
    return add_example(cfg, "retitled", {
        "login": meta.get("login"), "old": old, "new": new_title.strip(), "source": source,
        "context": str(meta.get("user_context") or "")[:400], "category": meta.get("category"),
        "by_engine": not meta.get("cut_by_hand")})


def reedit_lesson(cfg, before, after, note=None, context=None, window=None):
    """You asked Claude to re-edit a clip ("Re-edit with Claude"): your note / context, and what changed."""
    def span(m):
        s = m.get("start_in_recording")
        return (float(s), float(s) + float(m.get("duration") or 0)) if isinstance(s, (int, float)) else None
    old_t = before.get("title_bar") or before.get("heading") or before.get("title") or ""
    new_t = after.get("title_bar") or after.get("heading") or after.get("title") or ""
    ex = {"login": after.get("login") or before.get("login"), "by_engine": not before.get("cut_by_hand"),
          "note": str(note or "").strip()[:400], "context": str(context or "").strip()[:400],
          "window_chosen": bool(window), "old_title": old_t, "new_title": new_t,
          "old_category": before.get("category"), "new_category": after.get("category"),
          "old_score": before.get("viral_score"), "new_score": after.get("viral_score"),
          "old_duration": before.get("duration"), "new_duration": after.get("duration"),
          "flagged_by": str(before.get("flagged_by") or "")[:200],
          "what_happens": str(after.get("what_happens") or "")[:300]}
    a, b = span(before), span(after)
    if a and b and before.get("recording") == after.get("recording"):
        ex["added_before"], ex["added_after"] = round(a[0] - b[0], 2), round(b[1] - a[1], 2)
    if not ex["note"] and not ex["context"] and not ex["window_chosen"] and old_t == new_t:
        return None                                 # nothing asked and nothing changed: no lesson
    return add_example(cfg, "reedited", ex)


def timing_fix(cfg, meta, old_start, old_end, new_start, new_end):
    added_before, added_after = round(old_start - new_start, 2), round(new_end - old_end, 2)
    if abs(added_before) < 0.5 and abs(added_after) < 0.5:
        return None
    return add_example(cfg, "timing", {
        "login": meta.get("login"), "title": meta.get("title_bar") or meta.get("title"),
        "category": meta.get("category"), "added_before": added_before, "added_after": added_after,
        "old_duration": round(old_end - old_start, 2), "new_duration": round(new_end - new_start, 2)})


# -------------------------------------------------------------------------------- 2a. measuring a missed moment
def _ratio(v, base):
    return round(float(v) / max(float(base), 1e-6), 2)


def measure_missed(cfg, folder):
    """A clip you cut by hand from a recording = a moment the engine missed. Measure every signal the engine
    could have used at that moment and save the example."""
    from . import recordings as R
    meta = _read_json(os.path.join(folder, "meta.json"), {}) or {}
    stem = os.path.splitext(str(meta.get("recording") or ""))[0]
    rec_dir = cfg["storage"]["recordings_dir"]
    part = next((p for p in R.list_parts(rec_dir) if p["stem"] == stem), None)
    if not part:
        raise RuntimeError(f"the recording {stem} isn't there anymore")
    path = part["path"]
    start = float(meta.get("start_in_recording") or 0.0)
    span = meta.get("jump_cuts") or [[0, meta.get("duration") or 0]]
    end = start + float(span[-1][1] if span and span[-1] else meta.get("duration") or 0)
    if meta.get("cut_start") is not None and meta.get("cut_end") is not None:   # your exact selection
        start, end = float(meta["cut_start"]), float(meta["cut_end"])
    login = meta.get("login") or part.get("login")
    det = cfg["detection"]
    ex = {"login": login, "platform": meta.get("platform") or part.get("platform"), "recording": stem,
          "start": round(start, 1), "end": round(end, 1), "title": meta.get("title_bar") or meta.get("title"),
          "context": str(meta.get("user_context") or "")[:500],
          "claude_description": (str(meta.get("what_happens") or "")[:400] if meta.get("category") != "manual" else ""),
          "clip": os.path.basename(folder)}
    bucket = float((part.get("chat") or {}).get("bucket") or R.BUCKET)

    # chat activity at the moment vs this part's normal level. The 5 s buckets arrive in bursts (a bucket is often
    # 0 and the next one has the batch), so both are compared as messages per 30 s.
    counts = (part.get("chat") or {}).get("counts") or []
    if sum(counts) > 0:
        k = max(1, int(round(30 / bucket)))
        sums = [sum(counts[i:i + k]) for i in range(0, max(1, len(counts) - k + 1))]
        i0, i1 = int(max(0, (start - 10) // bucket)), int((end + 10) // bucket) + 1
        win = sums[max(0, i0 - k + 1):min(len(sums), i1)] or [0]
        med = statistics.median(sums)
        ex["chat"] = {"peak_per_30s": max(win), "median_per_30s": med, "ratio": _ratio(max(win), max(med, 1))}
    else:
        ex["chat"] = None
    # loudness jump vs the part's median
    loud = part.get("loud") or []
    if loud:
        i0, i1 = int(start // R.BUCKET), int(end // R.BUCKET) + 1
        win = loud[i0:i1] or [loud[min(i0, len(loud) - 1)]]
        ex["loudness"] = {"peak_db": max(win), "median_db": statistics.median(loud),
                          "jump_db": round(max(win) - statistics.median(loud), 1)}
    else:
        ex["loudness"] = None

    # voice reactions: same analyser as live, fed 90 s before the moment so it knows the streamer's normal voice
    from .analyzer import RATE, Transcriber, extract_audio
    from .voice import FPS, VoiceAnalyzer
    pre = min(90.0, start)
    try:
        audio = extract_audio(path, start - pre, (end - start) + pre)
        found = {}
        for level in VOICE_LEVELS:
            an = VoiceAnalyzer(level, cooldown=5)
            evs = []
            step = 12 * RATE
            for i in range(0, len(audio), step):
                evs += an.feed(int(round(i / RATE * FPS)), audio[i:i + step])
            found[level] = sorted({lbl for t, lbl, _ in evs if t >= pre - 2})
        ex["voice"] = {"normal": found["normal"], "would_flag_at": [lv for lv in VOICE_LEVELS if found[lv]],
                       "current_sensitivity": det.get("voice_sensitivity", "normal")}
    except Exception as e:
        audio = None
        ex["voice"] = {"error": str(e)[:120]}

    # gameplay motion: the chaos detector's baseline logic (2 min before) vs the moment
    from .chaos import FPS as CFPS, motion_series, remove_cuts
    import numpy as np
    try:
        before = min(120.0, start)
        d, t = motion_series(path, start - before, (end - start) + before)
        d = remove_cuts(d)
        base_vals = [v for v, tt in zip(d, t) if tt < start]
        if len(base_vals) >= 20 and len(d) >= CFPS // 2:
            base = statistics.median(base_vals)
            k = CFPS // 2
            sustained = np.convolve(d, np.ones(k) / k, mode="valid")
            tt = t[k // 2: k // 2 + len(sustained)]
            in_win = [v for v, x in zip(sustained, tt) if start <= x <= end]
            peak = max(in_win) if in_win else 0.0
            ex["motion"] = {"ratio": _ratio(peak, base), "current_chaos_ratio": det.get("chaos_ratio", 2.6)}
        else:
            ex["motion"] = None
    except Exception as e:
        ex["motion"] = {"error": str(e)[:120]}

    # transcript + which phrase patterns it matches
    from .detector import BUG, GAMEPLAY, NPC, SPICY, SUS
    from .hottake import NPC_TALK, TAKE
    try:
        a = audio[int(pre * RATE):] if audio is not None else extract_audio(path, start, end - start)
        words, _ = Transcriber(cfg["transcription"]).transcribe(a)
        text = " ".join(w["word"].strip() for w in words)
        ex["transcript"] = text[:600]
        pats = {"hot_take": TAKE, "npc_talk": NPC_TALK, "npc": NPC, "sus": SUS, "spicy": SPICY, "bug": BUG,
                "gameplay": GAMEPLAY}
        ex["phrases"] = sorted(k for k, p in pats.items() if p.search(text))
    except Exception as e:
        ex["transcript"], ex["phrases"] = "", []
        ex["transcript_error"] = str(e)[:120]

    # did the engine notice it? flagged moments within 60 s, and its decision
    aliases = part.get("aliases") or {stem: 0.0}
    moms = [m for m in part.get("moments") or [] if start - 60 <= float(m.get("t", -1e9)) <= end + 60]
    ex["engine_flags"] = [{"t": m.get("t"), "why": str(m.get("why") or "")[:120]} for m in moms][:6]
    hits = []
    for dline in decisions(cfg):
        if dline.get("recording") in aliases:
            sec = float(dline.get("sec") or 0) + float(aliases.get(dline["recording"], 0.0))
            if start - 60 <= sec <= end + 60:
                hits.append(dline)
    ex["engine_decisions"] = [{"score": h.get("viral_score"), "keep": h.get("keep"), "saved": h.get("saved"),
                               "judged": h.get("judged"), "reason": str(h.get("reason") or "")[:160],
                               "source": str(h.get("source") or "")[:100]} for h in hits][:4]
    if not moms and not hits:
        ex["verdict"] = "never flagged"
    elif hits and any(h.get("saved") for h in hits):
        ex["verdict"] = "flagged and clipped, but you cut it again"
    elif hits and any(h.get("judged") for h in hits):
        best = max((h for h in hits if h.get("judged")), key=lambda h: int(h.get("viral_score") or 0))
        ex["verdict"] = f"flagged but scored {best.get('viral_score')}: {str(best.get('reason') or '')[:120]}"
    elif hits:
        ex["verdict"] = f"flagged but not judged: {str(hits[0].get('reason') or '')[:120]}"
    else:
        ex["verdict"] = "flagged but never judged (engine stopped or not logged yet)"
    return add_example(cfg, "missed", ex)


def measure_in_background(cfg, cfg_path, folder):
    """Start the measuring in its own hidden process, so making the clip isn't slower."""
    from .config import ROOT
    try:
        os.makedirs(ldir(cfg), exist_ok=True)
        logf = open(os.path.join(ldir(cfg), "measure.log"), "a", encoding="utf-8")
    except OSError:
        logf = subprocess.DEVNULL
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("FRENZO_LOG", None)
    flags = (NO_WINDOW | 0x00000200) if os.name == "nt" else 0       # hidden + its own process group
    kw = {"creationflags": flags} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([sys.executable, "-m", "frenzo", "--config", cfg_path, "learn", "--measure", folder],
                     cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, env=env, **kw)


# ---------------------------------------------------------------------------------------- 3. learning run
def _med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 2) if xs else None


def build_summary(cfg, exs, decs, learned, max_chars=14000):
    """Compact stats of all examples (per streamer and type), not raw dumps."""
    det, clips = cfg["detection"], cfg["clips"]
    by_type = {}
    for e in exs:
        by_type.setdefault(e.get("type"), []).append(e)
    lines = [f"EXAMPLES: {len(exs)} in total: " + ", ".join(f"{k} {len(v)}" for k, v in sorted(by_type.items()))]

    missed = by_type.get("missed", [])
    if missed:
        lines.append("\n== MISSED moments (the owner cut them by hand from a recording) ==")
        per = {}
        for e in missed:
            per.setdefault(e.get("login") or "?", []).append(e)
        for login, es in sorted(per.items(), key=lambda kv: -len(kv[1])):
            verdicts = {}
            for e in es:
                v = str(e.get("verdict", "?"))
                v = "never flagged" if v.startswith("never") else ("flagged, scored too low" if "scored" in v else
                                                                  "flagged, not judged" if "not judged" in v or "never judged" in v else v[:40])
                verdicts[v] = verdicts.get(v, 0) + 1
            chat_r = _med([(e.get("chat") or {}).get("ratio") for e in es])
            loud_j = _med([(e.get("loudness") or {}).get("jump_db") for e in es])
            mot = _med([(e.get("motion") or {}).get("ratio") for e in es])
            voice_any = sum(1 for e in es if (e.get("voice") or {}).get("normal"))
            voice_high = sum(1 for e in es if "high" in ((e.get("voice") or {}).get("would_flag_at") or [])
                             and not (e.get("voice") or {}).get("normal"))
            phr = {}
            for e in es:
                for p in e.get("phrases") or []:
                    phr[p] = phr.get(p, 0) + 1
            lines.append(f"- {login}: {len(es)} missed | " + ", ".join(f"{k}: {n}" for k, n in verdicts.items())
                         + f" | median chat peak/median ratio {chat_r} | median loudness jump {loud_j} dB"
                         + f" | median motion ratio {mot} | voice reaction found (normal sensitivity) in {voice_any}"
                         + f", only at high in {voice_high} | phrase patterns in speech: "
                         + (", ".join(f"{k}x{n}" for k, n in sorted(phr.items(), key=lambda kv: -kv[1])) or "none"))
        lines.append("Missed moments (title | owner's context | what the streamer says):")
        for e in missed[-18:]:
            lines.append(f"  * [{e.get('login')}] {str(e.get('title') or '')[:70]} | {str(e.get('context') or '')[:110]}"
                         f" | \"{str(e.get('transcript') or '')[:150]}\" | {str(e.get('verdict') or '')[:90]}")

    dels = by_type.get("deleted", [])
    if dels:
        lines.append("\n== BAD PICKS (engine clips the owner deleted) ==")
        cats = {}
        for e in dels:
            cats[e.get("category") or "?"] = cats.get(e.get("category") or "?", 0) + 1
        lines.append("by category: " + ", ".join(f"{k} {n}" for k, n in sorted(cats.items(), key=lambda kv: -kv[1]))
                     + f" | median score {_med([e.get('score') for e in dels])}")
        for e in dels[-12:]:
            lines.append(f"  * [{e.get('login')}] score {e.get('score')} {e.get('category')}: "
                         f"{str(e.get('title') or '')[:60]} | flagged by {str(e.get('flagged_by') or '')[:60]}"
                         f" | {str(e.get('what_happens') or '')[:100]}")

    rts = by_type.get("retitled", [])
    if rts:
        lines.append("\n== TITLE FIXES (old -> new) ==")
        for e in rts[-15:]:
            lines.append(f"  * {str(e.get('old') or '')[:70]} -> {str(e.get('new') or '')[:70]}"
                         + (f" (context: {str(e.get('context'))[:80]})" if e.get("context") else "")
                         + (" [AI idea]" if e.get("source") == "idea" else ""))

    tms = by_type.get("timing", [])
    if tms:
        lines.append("\n== TIMING FIXES (seconds the owner added (+) or removed (-)) ==")
        lines.append(f"{len(tms)} fixes | median before {_med([e.get('added_before') for e in tms])} s"
                     f" | median after {_med([e.get('added_after') for e in tms])} s")

    res = by_type.get("reedited", [])
    if res:
        lines.append("\n== RE-EDITS THE OWNER ASKED CLAUDE FOR (their note / what really happens -> what changed) ==")
        lines.append(f"{len(res)} re-edits | {sum(1 for e in res if e.get('by_engine'))} of the engine's own clips"
                     f" | median seconds added before {_med([e.get('added_before') for e in res])}, after "
                     f"{_med([e.get('added_after') for e in res])} | {sum(1 for e in res if e.get('old_title') != e.get('new_title'))}"
                     f" got a new title")
        for e in res[-15:]:
            lines.append(f"  * [{e.get('login')}] note: \"{str(e.get('note') or '-')[:110]}\""
                         + (f" | context: \"{str(e.get('context'))[:90]}\"" if e.get("context") else "")
                         + f" | title: {str(e.get('old_title') or '')[:55]} -> {str(e.get('new_title') or '')[:55]}"
                         + (f" | {e.get('old_duration')}s -> {e.get('new_duration')}s" if e.get("old_duration") else ""))

    if decs:
        lines.append("\n== ENGINE DECISIONS (last 2000) ==")
        per = {}
        for d in decs[-2000:]:
            per.setdefault(d.get("login") or "?", []).append(d)
        for login, ds in sorted(per.items(), key=lambda kv: -len(kv[1])):
            judged = [d for d in ds if d.get("judged")]
            lines.append(f"- {login}: {len(ds)} flagged, {len(judged)} judged, {sum(1 for d in ds if d.get('saved'))} saved,"
                         f" median score {_med([d.get('viral_score') for d in judged])}")

    lines.append("\n== CURRENT SETTINGS ==")
    lines.append(f"global: spike_multiplier {det.get('spike_multiplier')}, min_messages_in_window "
                 f"{det.get('min_messages_in_window')}, voice_sensitivity {det.get('voice_sensitivity')}, chaos_ratio "
                 f"{det.get('chaos_ratio')}, min_score {clips.get('min_score')}, lead_in {clips.get('lead_in')}, "
                 f"tail {clips.get('tail')}")
    lines.append("streamers (login: hot_takes): " + ", ".join(
        f"{s['login']}: {bool(s.get('hot_takes'))}" for s in cfg.get("streamers") or []))
    lines.append("CURRENT LEARNED STATE: " + json.dumps({k: learned.get(k) for k in (
        "editor_notes", "title_notes", "streamers", "chat_keywords", "speech_keywords", "lead_in", "tail")
        if learned.get(k)}, ensure_ascii=False)[:2500])
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n(...cut to keep this short)"


LEARN_PROMPT = """You tune an automatic clipping engine for a viral GTA 6 clip page (TikTok / Reels / Shorts).
The engine watches livestreams, flags moments (chat spikes, chat keywords, the streamer yelling/laughing, gameplay \
chaos measured as picture motion vs normal, hot takes heard in speech) and a clip judge (Claude) scores each moment \
0-10; clips scoring at least min_score are kept.
The page owner teaches it: MISSED moments are clips they cut by hand from recordings (with the signals measured at \
that moment and whether the engine flagged it); BAD PICKS are engine clips they deleted; TITLE FIXES and TIMING \
FIXES show how they want titles and margins. RE-EDITS are clips the owner sent back to Claude with a note (what was \
wrong / what they want): repeated notes are the clearest editor_notes and title_notes.

RULES:
- Only change something when SEVERAL examples point the same way. Never change anything because of one example.
- Prefer small steps (the engine also limits every change).
- Lower spike_multiplier / min_messages_in_window / chaos_ratio, or raise voice_sensitivity, only for a streamer \
whose missed moments show that signal was present but under the threshold. Raise them for a streamer whose deleted \
clips were flagged by that signal.
- min_score: lower it for a streamer only if several missed moments were flagged but scored just under it; raise it if \
many deleted clips scored at or just above it.
- editor_notes: short concrete rules for the clip judge about what the owner likes or dislikes (max 12).
- chat_keywords / speech_keywords: plain words or short phrases (no regex), that appear in several missed moments.
- title_notes: rules drawn from the title fixes (max 6). lead_in / tail: only if timing fixes agree.
- Leave out (or keep as they are) every value you have no reason to change.

{summary}

Reply with ONLY one JSON object, explain nothing outside it:
{{"editor_notes": [...], "streamers": {{"<login>": {{"spike_multiplier": x, "min_messages_in_window": n, \
"voice_sensitivity": "low|normal|high", "chaos_ratio": x, "min_score": n, "hot_takes": true/false}}}}, \
"chat_keywords": [...], "speech_keywords": [...], "title_notes": [...], "lead_in": x, "tail": x, \
"summary": "3-6 plain sentences for the page owner: what was learned and what changed"}}"""


def ask_claude(cfg, prompt):
    from .ai import ClipJudge
    judge = ClipJudge(cfg)
    model = (cfg.get("learning") or {}).get("model") or "sonnet"
    fallback = cfg["anthropic"].get("claude_code_model", "haiku")
    if judge.claude_code:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)           # your Claude plan, never paid API credits
        last = ""
        for m in dict.fromkeys([model, fallback]):
            res = subprocess.run([judge.claude_code, "-p", "--output-format", "json", "--model", m, "--max-turns", "1"],
                                 input=prompt.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=300, env=env, creationflags=NO_WINDOW)
            out = res.stdout.decode("utf-8", "replace")
            try:
                wrapper = json.loads(out)
                if wrapper.get("is_error"):
                    last = str(wrapper.get("result", ""))[:200]
                    continue
                return wrapper.get("result", out), m
            except ValueError:
                last = (out or res.stderr.decode("utf-8", "replace"))[:200]
                if "{" in out:
                    return out, m
        raise RuntimeError(f"Claude didn't answer: {last}")
    if judge.client is not None:
        msg = judge.client.messages.create(model=cfg["anthropic"]["model"], max_tokens=2500,
                                           messages=[{"role": "user", "content": prompt}])
        return "".join(getattr(b, "text", "") for b in msg.content), cfg["anthropic"]["model"]
    raise RuntimeError("Claude isn't available on this PC (Claude Code not found).")


def _save_learned(cfg, data):
    """Keep the current learned.json in history/, then write the new one."""
    d = ldir(cfg)
    cur = os.path.join(d, "learned.json")
    if os.path.exists(cur):
        hist = os.path.join(d, "history")
        os.makedirs(hist, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        os.replace(cur, os.path.join(hist, f"{stamp}.json"))
    _write_json(cur, data)


def run_learning(cfg, dry_run=False, examples_file=None, force=False):
    exs = _read_jsonl(examples_file) if examples_file else examples(cfg)
    if len(exs) < MIN_EXAMPLES and not force:
        log(f"only {len(exs)} example(s) so far: learning needs at least {MIN_EXAMPLES} (it never learns from one)", "learn")
        return None
    learned = load_learned(cfg)
    summary = build_summary(cfg, exs, decisions(cfg), learned)
    log(f"learning from {len(exs)} examples ({len(summary)} characters of stats)...", "learn")
    text, model = ask_claude(cfg, LEARN_PROMPT.format(summary=summary))
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise RuntimeError("Claude's answer had no JSON: " + (text or "")[:160])
    answer = json.loads(m.group(0))
    new = sanitize(answer, learned, cfg)
    new.update({"updated": now_iso(), "examples": len(exs), "model": model})
    log("learned: " + (new.get("summary") or "(no summary)"), "learn")
    for login, vals in new["streamers"].items():
        log(f"{login}: " + ", ".join(f"{k} {v}" for k, v in vals.items()), "learn")
    if new["editor_notes"]:
        log(f"{len(new['editor_notes'])} editor notes, {len(new['chat_keywords'])} chat words, "
            f"{len(new['speech_keywords'])} speech phrases", "learn")
    if dry_run:
        log("dry run: nothing saved", "learn")
        return new
    _save_learned(cfg, new)
    st = state(cfg)
    st.update({"last_run": time.time(), "last_run_iso": now_iso(), "examples_at_last_run": len(exs),
               "summary": new.get("summary") or ""})
    _write_json(_path(cfg, "state.json"), st)
    log("saved: live clipping uses it after a restart", "learn")
    return new


def undo(cfg):
    """learned.json <- the newest file in history/ (that file is removed from history)."""
    d = ldir(cfg)
    hist = os.path.join(d, "history")
    files = sorted(f for f in os.listdir(hist) if f.endswith(".json")) if os.path.isdir(hist) else []
    if not files:
        try:
            os.remove(os.path.join(d, "learned.json"))
        except OSError:
            pass
        return False
    os.replace(os.path.join(hist, files[-1]), os.path.join(d, "learned.json"))
    return True


def reset(cfg):
    """Empty learned.json (the current one goes to history, so this can be undone). Examples are kept."""
    _save_learned(cfg, {"updated": now_iso(), "reset": True})


def remove_note(cfg, index):
    data = load_learned(cfg)
    notes = list(data.get("editor_notes") or [])
    if not 0 <= index < len(notes):
        raise IndexError(index)
    removed = notes.pop(index)
    data = dict(data, editor_notes=notes, updated=now_iso())
    _save_learned(cfg, data)
    return removed


def new_since_last_run(cfg):
    return max(0, len(examples(cfg)) - int(state(cfg).get("examples_at_last_run") or 0))


def auto_due(cfg):
    if not (cfg.get("learning") or {}).get("auto", True):
        return False
    st = state(cfg)
    return (new_since_last_run(cfg) >= AUTO_NEW_EXAMPLES and len(examples(cfg)) >= MIN_EXAMPLES
            and time.time() - float(st.get("last_run") or 0) >= AUTO_MIN_GAP)


def overview(cfg):
    """Everything the app's Learning card shows."""
    exs = examples(cfg)
    learned = load_learned(cfg)
    st = state(cfg)
    counts = {k: 0 for k in ("missed", "deleted", "retitled", "timing", "reedited")}
    for e in exs:
        if e.get("type") in counts:
            counts[e["type"]] += 1
    det, clips = cfg["detection"], cfg["clips"]
    defaults = {"spike_multiplier": det.get("spike_multiplier"), "min_messages_in_window": det.get("min_messages_in_window"),
                "voice_sensitivity": det.get("voice_sensitivity"), "chaos_ratio": det.get("chaos_ratio"),
                "min_score": clips.get("min_score")}
    streamers = []
    for s in cfg.get("streamers") or []:
        lv = (learned.get("streamers") or {}).get(s["login"]) or {}
        if lv:
            streamers.append({"login": s["login"], "learned": lv,
                              "defaults": dict(defaults, hot_takes=bool(s.get("hot_takes")))})
    hist = os.path.join(ldir(cfg), "history")
    return {"counts": counts, "total": len(exs), "since_last": new_since_last_run(cfg),
            "last_run": st.get("last_run_iso"), "summary": st.get("summary") or learned.get("summary") or "",
            "editor_notes": learned.get("editor_notes") or [], "title_notes": learned.get("title_notes") or [],
            "chat_keywords": learned.get("chat_keywords") or [], "speech_keywords": learned.get("speech_keywords") or [],
            "lead_in": learned.get("lead_in"), "tail": learned.get("tail"),
            "default_lead_in": clips.get("lead_in"), "default_tail": clips.get("tail"),
            "streamers": streamers, "auto": (cfg.get("learning") or {}).get("auto", True),
            "can_undo": os.path.isdir(hist) and any(f.endswith(".json") for f in os.listdir(hist)),
            "decisions": sum(1 for _ in open(_path(cfg, "decisions.jsonl"), encoding="utf-8"))
            if os.path.exists(_path(cfg, "decisions.jsonl")) else 0,
            "min_examples": MIN_EXAMPLES, "auto_every": AUTO_NEW_EXAMPLES, "folder": ldir(cfg)}


# ------------------------------------------------------------------------------ 4. using what was learned
def apply_to_config(cfg):
    """Called by load_config: learned values go into cfg["learned"] and each streamer's "learned" dict.
    Nothing is ever written into config.yaml."""
    try:
        data = load_learned(cfg)
    except Exception:
        data = {}
    cfg["learned"] = data
    per = data.get("streamers") or {}
    for s in cfg.get("streamers") or []:
        s["learned"] = dict(per.get(s["login"]) or per.get(s.get("key", "")) or {})
    return cfg


def clip_value(cfg, key, default=None):
    """lead_in / tail: the learned value replaces the config one."""
    v = (cfg.get("learned") or {}).get(key)
    if isinstance(v, (int, float)):
        return float(v)
    return cfg["clips"].get(key, default)


def detection_for(cfg, scfg):
    """Global detection settings + this streamer's learned overrides."""
    det = dict(cfg["detection"])
    for k in ("spike_multiplier", "min_messages_in_window", "voice_sensitivity", "chaos_ratio"):
        v = (scfg.get("learned") or {}).get(k)
        if v is not None:
            det[k] = v
    return det


def hot_takes_for(scfg):
    v = (scfg.get("learned") or {}).get("hot_takes")
    return v if isinstance(v, bool) else bool(scfg.get("hot_takes"))


def min_score_for(cfg, scfg):
    v = ((scfg or {}).get("learned") or {}).get("min_score")
    return int(v) if isinstance(v, (int, float)) else cfg["clips"]["min_score"]


def prompt_notes(cfg, titles_only=False):
    """The learned notes for Claude's prompts (kept short)."""
    data = cfg.get("learned") or {}
    notes = [] if titles_only else list(data.get("editor_notes") or [])[:12]
    tnotes = list(data.get("title_notes") or [])[:6]
    if not notes and not tnotes:
        return ""
    out = "\n\nWHAT THE PAGE OWNER LIKES (learned from the clips they made and deleted):"
    out += "".join(f"\n- {n}" for n in notes)
    if tnotes:
        out += "\nTitles:" + "".join(f"\n- {n}" for n in tnotes)
    return out


# ------------------------------------------------------------------------------------------------ command
def main(cfg, cfg_path, measure=None, dry_run=False, examples_file=None, force=False):
    if measure:
        log(f"measuring the moment you cut ({os.path.basename(measure)}) for learning...", "learn")
        ex = measure_missed(cfg, measure)
        log(f"lesson saved: {ex.get('verdict')} | chat x{(ex.get('chat') or {}).get('ratio')} | loudness "
            f"+{(ex.get('loudness') or {}).get('jump_db')} dB | motion x{(ex.get('motion') or {}).get('ratio')} | "
            f"voice {(ex.get('voice') or {}).get('normal')} | phrases {ex.get('phrases')}", "learn")
        return ex
    return run_learning(cfg, dry_run=dry_run, examples_file=examples_file, force=force)
