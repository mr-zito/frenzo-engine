"""Audio-only sound pass with the GTA SFX library (the audio logic of the skill's render_reel.py):

  1. the clip's game audio, cut with the same jump cuts as the picture;
  2. every cue placed by its alignment (peak / end / start, from the library's peak_s), with gain, max_len,
     the "stop before the hit" (game audio near-silent just before t) and ducking (-9 dB) under voice/music;
  3. two-pass loudnorm to -14 LUFS, true peak under -1 dBFS (+ limiter, so it stays there after AAC);
  4. the mix goes into every platform version with -c:v copy (the picture is never re-encoded for sound).

check_reel.py (from the skill) then checks the finished clip: loudness, true peak, cues on sync, masked cues.
One automatic fix is tried; if it still fails the clip is marked "needs review" and the report is kept."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from . import soundlib
from .util import NO_WINDOW, run

QUIET_KINDS = {"whoosh", "transition", "riser", "ui", "bed"}
_rr = None


def render_reel(lib):
    """The skill's render_reel.py as a module (its helpers are reused, its main() is not run)."""
    global _rr
    if _rr is None:
        sd = soundlib.scripts_dir(lib)
        if sd is None:
            raise RuntimeError("the sound skill's scripts folder is missing")
        spec = importlib.util.spec_from_file_location("frenzo_render_reel", str(sd / "render_reel.py"))
        _rr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_rr)
    return _rr


def windows(ranges):
    """ffmpeg expression that is 1 inside any (a, b) window, else 0 (same as render_reel.windows)."""
    if not ranges:
        return "0"
    return "min(1," + "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges) + ")"


def cut_game_audio(rec, start, segments, out_wav, speeds=None):
    """The game audio of the clip with its jump cuts: segments are (a, b) seconds relative to `start`.
    speeds: optional speed per segment (0.5 = slow motion)."""
    segments = [(float(a), float(b)) for a, b in segments]
    span = max(b for _, b in segments)
    k = len(segments)
    g = f"[0:a]aresample=48000:async=1:first_pts=0,aformat=channel_layouts=stereo,asplit={k}" + \
        "".join(f"[as{i}]" for i in range(k))
    for i, (a, b) in enumerate(segments):
        sp = float((speeds or [1.0] * k)[i] or 1.0)
        tempo = ""
        if sp < 0.5:                                  # a freeze: the game is silent while the picture is held
            piece = (b - a) / sp
            tempo = f",volume=0,apad=whole_dur={piece:.3f},atrim=0:{piece:.3f}"
        elif abs(sp - 1.0) > 1e-3:
            parts, rest = [], sp
            while rest < 0.5:
                parts.append("atempo=0.5")
                rest /= 0.5
            parts.append(f"atempo={rest:.4f}")
            tempo = "," + ",".join(parts)
        g += f";[as{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS{tempo}[ap{i}]"
    g += ";" + "".join(f"[ap{i}]" for i in range(k)) + f"concat=n={k}:v=0:a=1[a]"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{span + 0.2:.3f}",
         "-i", rec, "-filter_complex", g, "-map", "[a]", "-c:a", "pcm_s16le", out_wav])
    return out_wav


def placed(cues, lib):
    """Cue list (output timeline) -> sheet rows with where each sound starts after alignment."""
    entries = soundlib.load(lib)
    rows = []
    for c in sorted(cues, key=lambda c: float(c["t"])):
        e = entries.get(c["sfx"])
        if not e:
            continue
        full = float(e.get("duration") or 0)
        L = min(full, float(c.get("max_len") or 8.0))
        align = c.get("align") if c.get("align") in ("peak", "end", "start") else "start"
        off = {"start": 0.0, "peak": min(float(e.get("peak_s") or 0), L), "end": L}[align]
        t = float(c["t"])
        start, head = t - off + float(c.get("nudge") or 0), 0.0
        if start < 0:                         # the sound would begin before the clip: cut its head instead
            head, start = -start, 0.0
        rows.append({"t": round(t, 3), "starts": round(start, 3), "align": align, "sfx": c["sfx"],
                     "file": str(Path(lib) / e["file"]), "kind": c.get("kind") or "hit", "len": round(L - head, 3),
                     "head": round(head, 3), "L": round(L, 3), "gain_db": float(c.get("gain_db") or 0),
                     "stop": float(c.get("stop") or 0), "duck": bool(c.get("duck", c.get("kind") in ("voice", "music"))),
                     "fx": list(c.get("fx") or []), "caption": c.get("caption") or "", "role": c.get("role") or "",
                     "mute_after": float(c.get("mute_after") or 0),
                     "mute_game": float(c.get("mute_game") or 0)})
    return rows


def mix(game_wav, rows, out_wav, total, duck_db=-9.0, lufs=-14.0, game_volume=1.0):
    """Game audio + placed cues -> out_wav, two-pass loudnorm to `lufs` with true peak < -1 dBFS."""
    inputs, chains, mixl, duck, stops = ["-i", game_wav], [], [], [], []
    for k, r in enumerate(rows, start=1):
        inputs += ["-i", r["file"]]
        chains.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo,atrim={r['head']:.3f}:{r['L']:.3f},"
                      f"asetpts=PTS-STARTPTS,volume={r['gain_db']}dB,adelay={int(r['starts'] * 1000)}:all=1[x{k}]")
        mixl.append(f"[x{k}]")
        if r["duck"]:
            duck.append((r["starts"], r["starts"] + r["len"]))
        if r["stop"]:
            stops.append((max(0.0, r["t"] - r["stop"]), r["t"]))
        if r.get("mute_after"):                       # the game goes silent after it (staring at the camera)
            stops.append((r["t"] + 0.05, r["t"] + r["mute_after"]))
        if r.get("mute_game"):                        # you asked to remove a sound of the stream: silent under this one
            stops.append((max(0.0, r["starts"] - 0.15), r["starts"] + r["mute_game"]))
    dip = 10 ** (float(duck_db) / 20)
    level = f"(1-{1 - dip:.3f}*{windows(duck)})*(1-0.97*{windows(stops)})"
    graph = ";".join([f"[0:a]aresample=48000,aformat=channel_layouts=stereo,volume='{game_volume}*{level}':eval=frame[g]"]
                     + chains + [f"[g]{''.join(mixl)}amix=inputs={len(mixl) + 1}:normalize=0:duration=first[aout]"])
    tmp = out_wav + ".pre.wav"
    gfile = out_wav + ".graph.txt"
    with open(gfile, "w", encoding="utf-8") as f:
        f.write(graph)
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + inputs +
        ["-filter_complex_script", gfile, "-map", "[aout]", "-t", f"{total:.3f}", "-c:a", "pcm_s24le", tmp])
    ln = f"loudnorm=I={lufs}:TP=-2:LRA=11"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", tmp, "-af", ln + ":print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, creationflags=NO_WINDOW if os.name == "nt" else 0).stderr
    m = json.loads(r[r.rfind("{"):r.rfind("}") + 1])
    ln2 = (f"{ln}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
           f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true,aresample=48000,"
           f"alimiter=limit=0.75:level=0:attack=1:release=40")       # true peak stays under -1 dBFS after AAC
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", tmp, "-af", ln2, "-c:a", "pcm_s24le", out_wav])
    # peaky mixes (a metal pipe on a quiet scene): the limiter pulls the loudness under the target; measure again
    # and correct (gain + the same limiter), at most twice
    for _ in range(2):
        got = measure_lufs(out_wav)
        if got is None or abs(got - lufs) <= 0.5:
            break
        fixed = out_wav + ".fix.wav"
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", out_wav, "-af",
             f"volume={lufs - got:.2f}dB,alimiter=limit=0.75:level=0:attack=1:release=40", "-c:a", "pcm_s24le", fixed])
        os.replace(fixed, out_wav)
    for p in (tmp, gfile):
        try:
            os.remove(p)
        except OSError:
            pass
    return out_wav


def measure_lufs(path):
    """Integrated loudness (LUFS) of a sound file."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True, creationflags=NO_WINDOW if os.name == "nt" else 0).stderr
    import re
    m = re.search(r"I:\s+(-?[\d.]+) LUFS", r[r.rfind("Summary:"):])
    return float(m.group(1)) if m else None


def write_cues_json(clip_path, rows, total, fps=30):
    # fps 30 like the skill's reels: "off sync" = more than 2 frames (66 ms) late or early
    """<clip>.cues.json in render_reel.py's format, read by check_reel.py."""
    sheet = [{k: r[k] for k in ("t", "starts", "align", "sfx", "kind", "len", "gain_db", "fx", "caption")} for r in rows]
    es = {"fps": fps, "total": round(total * fps),
          "events": [{"f": round(s["t"] * fps, 2), "kind": s["kind"], "weight": 1, "id": f"reel:{s['kind']}:{n}"}
                     for n, s in enumerate(sheet)]}
    with open(str(clip_path) + ".cues.json", "w", encoding="utf-8") as f:
        json.dump({**es, "sheet": sheet}, f, indent=1)


def remux(video, wav, out=None):
    """Put the mixed sound into a finished clip without re-encoding the picture."""
    out = out or video
    tmp = video + ".remux.mp4"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", video, "-i", wav, "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", tmp])
    os.replace(tmp, out)
    return out


def check(clip_path, lib, out_dir):
    """Run the skill's check_reel.py on a finished clip. Returns its qa.json (dict) or None."""
    sd = soundlib.scripts_dir(lib)
    if sd is None:
        return None
    subprocess.run([sys.executable, str(sd / "check_reel.py"), str(clip_path), "--out", out_dir],
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   creationflags=NO_WINDOW if os.name == "nt" else 0)
    try:
        with open(os.path.join(out_dir, "qa.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def failing(qa):
    """The problems that matter: cues off sync or masked, loudness or true peak out of range."""
    if not qa:
        return []
    bad = [f"{r['t']:.2f}s {r['sfx']}: {r['verdict']}" for r in qa.get("cues") or []
           if r.get("verdict") == "masked" or r.get("verdict") == "off sync" and r.get("kind") not in QUIET_KINDS]
    # (transitions / whooshes / risers a few frames off are listed in the report, not a reason to review)
    lufs, tp = qa.get("lufs"), qa.get("true_peak_dbfs")
    if lufs is not None and abs(lufs + 14) > 1.0:
        bad.append(f"loudness {lufs} LUFS")
    if tp is not None and tp > -1.0:
        bad.append(f"true peak {tp} dBFS")
    return bad


def autofix(cues, qa):
    """One automatic fix from check_reel's verdicts: masked -> +4 dB and a short stop before the hit;
    off sync -> a small miss (< 150 ms) moves the sound so its hit lands on t, a big one switches the alignment
    (peak <-> start). Returns (new cues, list of changes)."""
    verdict, onset = {}, {}
    for r in (qa or {}).get("cues") or []:
        verdict[round(float(r["t"]), 2)] = r.get("verdict")
        onset[round(float(r["t"]), 2)] = r.get("onset_ms")
    out, changes = [], []
    for c in cues:
        c = dict(c)
        v = verdict.get(round(float(c["t"]), 2))
        if v == "masked":
            c["gain_db"] = min(4.0, float(c.get("gain_db") or 0) + 4.0)
            if not c.get("stop") and c.get("kind", "hit") not in QUIET_KINDS:
                c["stop"] = 0.25
            changes.append(f"{c['t']:.2f}s {c['sfx']}: +4 dB" + (", stop 0.25 s" if c.get("stop") == 0.25 else ""))
        elif v == "off sync":
            ms = onset.get(round(float(c["t"]), 2))
            if isinstance(ms, (int, float)) and abs(ms) < 150:
                c["nudge"] = round(float(c.get("nudge") or 0) - ms / 1000.0, 3)
                changes.append(f"{c['t']:.2f}s {c['sfx']}: moved {-ms:+.0f} ms")
            else:
                c["align"] = "start" if c.get("align") == "peak" else "peak"
                changes.append(f"{c['t']:.2f}s {c['sfx']}: align -> {c['align']}")
        out.append(c)
    return out, changes
