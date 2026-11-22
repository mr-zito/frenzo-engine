"""Fix existing clips: re-edit every clip folder from its raw.mp4 with the current engine.

With Claude (your plan): Claude watches the moment again in raw.mp4 (which has a minute of context on
each side) and re-cuts it from scratch: an accurate title (Lucia / Jason / NPCs not mixed up), the setup
shown at the start, the reaction kept at the end, jump cuts, captions, webcam, focus, one version per handle.
Without Claude: the clip keeps its moment and title and is restyled, with the new context margins added.
Folders already fixed with this version are skipped, so it can be run again safely (e.g. after
Claude's hourly limit stops it).
"""
import json
import os
import re
import shutil

from . import editor
from . import layout as layout_mod
from . import analyzer, campaign, censor, titlecard, viral
from .ai import ClipJudge
from .learning import clip_value
from .pipeline import Candidate, Pipeline
from .sfx import pick
from .util import log, require_binary, run, slugify

REDO_VERSION = 7


USER_FIELDS = ("title", "caption", "hashtags", "posted", "user_context")


def keep_user_edits(meta_path, meta, fields=USER_FIELDS):
    """Re-renders take a while: anything edited in the app meanwhile (post text, posted marks, your
    context) is read back from disk so the re-render doesn't overwrite it."""
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            now = json.load(f)
    except (OSError, ValueError):
        return meta
    for k in fields:
        if k in now:
            meta[k] = now[k]
    return meta


def _clip_offset_in_raw(meta):
    m = re.search(r"starts at ([\d.]+)s", str(meta.get("raw_clip", "")))
    return float(m.group(1)) if m else 0.0     # older versions: raw.mp4 started with the clip


def _fix_raw_sync(folder):
    """Re-encode raw.mp4 with audio gaps filled, so it stays in sync (older versions copied it)."""
    src = os.path.join(folder, "raw.mp4")
    tmp = os.path.join(folder, "raw_fixed.mp4")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-map", "0:v:0", "-map", "0:a:0?",
         "-af", "aresample=48000:async=1:first_pts=0", "-fps_mode", "cfr", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", tmp])
    os.replace(tmp, src)


def cues_in_raw(meta):
    """The clip's library sound cues (output timeline) -> raw.mp4 seconds, through the clip's jump cuts."""
    cues = ((meta.get("sound") or {}).get("cues")) or []
    if not cues:
        return []
    off = _clip_offset_in_raw(meta)
    pieces = meta.get("jump_cuts") or [[0, meta.get("duration") or 0]]
    out = []
    for cu in cues:
        t, acc = float(cu["t"]), 0.0
        for a, b in pieces:
            if t <= acc + (b - a) + 1e-6:
                out.append(dict(cu, raw_t=off + a + (t - acc)))
                break
            acc += b - a
    return out


def _restyle(pipe, transcriber, cfg, rcfg, dirpath, meta, login, span=None, over=None):
    """No Claude: keep the clip's moment and title, rebuild it in the current style with margins.
    span=(start, end) in raw.mp4 seconds: a manual cut (no margins added). over: manual title /
    highlight / emoji / focus / cam ("none" = no webcam) chosen in the app."""
    over = over or {}
    raw = os.path.join(dirpath, "raw.mp4")
    meta_path = os.path.join(dirpath, "meta.json")
    offset = _clip_offset_in_raw(meta)
    dur = float(meta.get("duration") or 30)
    # context margins: a bit more before the setup and after the reaction
    ccfg = cfg["clips"]
    length = _raw_length(raw) or (offset + dur)
    if span:
        new_off = max(0.0, min(float(span[0]), length - 1))
        dur = max(1.0, min(length, float(span[1])) - new_off)
    else:
        new_off = max(0.0, offset - float(clip_value(cfg, "lead_in", 1.5)))
        dur = min(length - new_off, dur + (offset - new_off) + float(clip_value(cfg, "tail", 2.0)))
    shift, offset = offset - new_off, new_off
    # "keep cuts": same moment, same jump cuts (e.g. only the title changes) -> pieces relative to offset
    segs = [(float(a), float(b)) for a, b in (over.get("segments") or [])] or None
    span_len = segs[-1][1] if segs else dur
    if segs:
        dur = sum(b - a for a, b in segs)
        shift = 0.0
    scfg = next((s for s in cfg["streamers"] if s["login"] == login), {"login": login, "tags": []})
    camp = campaign.of(scfg)                       # campaign rules: no captions / no sus sound if it says so
    rcfg = campaign.render_cfg(rcfg, camp)
    pipe.judge.last_player_x = None
    pipe.__dict__.get("_cams", {}).clear()
    cam = scfg.get("facecam")
    if over.get("cam") == "none":
        cam = None
    elif isinstance(over.get("cam"), dict):
        cam = dict(over["cam"], how="set by hand in the app")
    elif not cam and isinstance(meta.get("facecam"), dict) and span:
        cam = meta["facecam"]                      # manual cut: keep the webcam the clip already had
    elif not cam and rcfg.get("layout", "reel") in ("reel", "auto"):
        cam = pipe._facecam(Candidate(login, meta.get("streamer") or login, raw, offset,
                                      "fix", platform=f"fix_{dirpath}"), offset, dur)
    lay = rcfg.get("layout", "reel")
    if lay in ("reel", "auto"):
        lay = "reel" if cam else "focus"
    crop_w = (editor.W / editor.REEL_GAME_H) * (1 / rcfg.get("game_zoom", 1.0)) * 9 / 16
    if lay == "focus":
        crop_w = (editor.W / editor.H) * 9 / 16
    focus = over.get("focus")
    if focus is None and span and isinstance(meta.get("focus_x"), (int, float)):
        focus = meta["focus_x"]
    if focus is None:
        focus = getattr(pipe.judge, "last_player_x", None)
    focus = float(focus) if focus is not None else layout_mod.action_focus(raw, offset, dur, crop_w, cam)

    heading = meta.get("heading") or meta.get("hook_text") or meta.get("title") or ""
    sfx = []
    # library sound cues: the clip's own cues, moved onto the new cut (cues in parts cut out are dropped)
    lib_cues, lib = [], None
    if rcfg.get("sound_effects", True) and rcfg.get("sound_library", True) and (meta.get("sound") or {}).get("cues"):
        from . import soundlib
        lib = soundlib.find_library(rcfg)
        if lib is not None:
            to_new = viral.mapper([(float(a), float(b)) for a, b in segs] if segs else [(0.0, dur)])
            allowed = set(soundlib.guide(rcfg, lib))       # sounds you removed from your list are dropped
            for cu in cues_in_raw(meta):
                if allowed and cu.get("sfx") not in allowed:
                    continue
                t = to_new(cu["raw_t"] - offset)
                if t is not None and 0 <= t < dur:
                    lib_cues.append({k: v for k, v in dict(cu, t=round(t, 3)).items() if k != "raw_t"})
    if lib is not None:
        pass
    elif rcfg.get("sound_effects", True) and str(meta.get("category", "")).lower() in ("sus", "spicy"):
        at = next((e.get("at") for e in meta.get("sound_effects") or [] if "at" in e), dur * 0.4) + shift
        path = pick("sus")
        if path and 0 <= float(at) < dur:
            sfx.append((path, float(at)))
    # viral style: title card + small captions (transcribed again from raw.mp4)
    title = over.get("title") or meta.get("title_bar") or (heading[:1].upper() + heading[1:].lower() if heading.isupper() else heading)
    title = re.sub(r"\bgta 6\b|\bgta vi\b|\bgta6\b", "GTA 6", title, flags=re.I)
    highlight = over.get("highlight") if over.get("highlight") is not None else meta.get("highlight")
    highlight = highlight if highlight is not None else ("GTA 6" if "GTA 6" in title else max(title.split(), key=len, default=""))
    emoji = over.get("emoji") if over.get("emoji") is not None else meta.get("emoji") or {"sus": "😳", "spicy": "😱", "funny": "😭", "rage": "💀",
                                  "fail": "💀", "shock": "😳"}.get(str(meta.get("category", "")).lower(), "💀")
    words = []
    try:
        audio = analyzer.extract_audio(raw, offset, span_len)
        words, _ = transcriber.transcribe(audio)
        if segs:                                   # captions follow the jump cuts
            to_out = viral.mapper(segs)
            words = [dict(w, start=to_out(w["start"]), end=to_out(w["end"]) or to_out(w["start"]) + 0.3)
                     for w in words if to_out(w["start"]) is not None]
    except Exception as e:
        log(f"couldn't transcribe for captions ({str(e)[:80]})", login)
    title_png = os.path.join(dirpath, "title.png")
    if not titlecard.render_title(viral.censor(title), highlight, emoji, title_png, size=rcfg.get("title_size", 58)):
        title_png = None
    callouts = [c for c in (meta.get("callouts") or []) if isinstance(c, dict)] if segs else []
    if rcfg.get("caption_style", "bold") == "bold" and lay in ("reel", "focus"):     # the current caption style
        ass = viral.build_ass_bold(words, callouts, dur, rcfg, lay, editor.REEL_CAM_H,
                                   punch_words=meta.get("punch_words") if isinstance(meta.get("punch_words"), list) else None,
                                   title=title)
    else:
        ass = viral.build_ass(words, callouts, dur, rcfg, lay, editor.REEL_CAM_H)

    for old in os.listdir(dirpath):
        if old.startswith("clip") and (old.endswith(".mp4") or old.endswith(".cues.json")):
            os.remove(os.path.join(dirpath, old))
    rcopy = dict(rcfg, save_raw=False)
    mixed, game_wav, rows = None, None, []
    if lib is not None and lib_cues:
        from . import soundpass
        try:
            game_wav = soundpass.cut_game_audio(raw, offset, segs or [(0.0, dur)], os.path.join(dirpath, "_game.wav"))
            rows = soundpass.placed(lib_cues, lib)
            mixed = soundpass.mix(game_wav, rows, os.path.join(dirpath, "_mix.wav"), dur)
        except Exception as e:
            log(f"sound pass failed ({str(e)[:100]}): game audio only", login)
            mixed = None
    fx_events = [(cu["t"], cu["fx"]) for cu in lib_cues if cu.get("fx")] if mixed else None
    files_out, _ = editor.render(raw, offset, dur, dirpath, ass, lay, cam, focus, sfx, rcopy, segments=segs,
                                 title_png=title_png, mixed_audio=mixed, fx_events=fx_events)
    # nudity: the clip had pixelated spots -> pixelate again (same cut: same spots; new cut: find them again)
    if meta.get("nudity") and censor.mode(cfg) != "keep":
        if segs and meta.get("censor_regions") and over.get("segments"):
            regs, info = [tuple(r) for r in meta["censor_regions"]], dict(meta.get("censored") or {}, reused=True)
        elif meta.get("nudity_raw"):              # Claude's nudity times (raw.mp4 seconds) inside the new cut
            ranges = [(max(0.0, a - offset), min(dur, b - offset)) for a, b in meta["nudity_raw"]
                      if b - offset > 0 and a - offset < dur]
            if ranges:
                regs, info = censor.plan(os.path.join(dirpath, files_out[0]), ranges, dur, lay, chosen=True)
            else:                                  # the nudity is outside your new cut: only what the detector finds
                regs, n_det, _ = censor.regions(censor.detect(os.path.join(dirpath, files_out[0])), [], dur, lay)
                info = {"detector_spots": n_det, "claude_spots": 0}
        else:                                      # times unknown: treat the whole clip as unsafe
            regs, info = censor.plan(os.path.join(dirpath, files_out[0]), [(0.0, dur)], dur, lay, chosen=True)
        if regs:
            files_out, _ = editor.render(raw, offset, dur, dirpath, ass, lay, cam, focus, sfx,
                                         dict(rcopy, censor_regions=regs), segments=segs, title_png=title_png,
                                         mixed_audio=mixed, fx_events=fx_events)
            log(f"nudity pixelated again: {len(regs)} spot(s)", login)
        else:
            log("no nudity left in your new cut: nothing pixelated", login)
        meta["censored"] = dict(info, spots=len(regs)) if regs else None
        meta["censor_regions"] = [list(r) for r in regs] if regs else None
    if title_png and os.path.exists(title_png):
        os.remove(title_png)
    if mixed:
        from .pipeline import sound_check
        meta["sound"] = sound_check(dirpath, files_out, lib_cues, rows, lib, game_wav, dur, rcfg, login)
        meta["needs_review"] = bool(meta["sound"].get("needs_review")) or None
    elif meta.get("sound"):
        meta["sound"] = dict(meta["sound"], cues=[])          # every cue was in a part you cut out
    for tmp in ("_game.wav", "_mix.wav"):
        try:
            os.remove(os.path.join(dirpath, tmp))
        except OSError:
            pass

    groups = dict(editor.handle_groups(rcfg))
    meta.update({
        "heading": heading, "title_bar": title, "highlight": highlight, "emoji": emoji, "layout": lay, "facecam": cam, "focus_x": round(focus, 3),
        "files": {f: groups.get(f[5:-4], "no handle") for f in files_out},
        "sound_effects": [{"file": os.path.basename(p), "at": round(t, 2)} for p, t in sfx],
        "raw_clip": f"raw.mp4 has the context around the clip; the clip starts at {offset:.1f}s in raw.mp4",
        "duration": round(dur, 2), "callouts": callouts,
        "jump_cuts": [[round(a, 2), round(b, 2)] for a, b in segs] if segs else [[0, round(dur, 2)]],
        "start_in_recording": (round(meta["start_in_recording"] - _clip_offset_in_raw(meta) + offset, 2)
                               if isinstance(meta.get("start_in_recording"), (int, float)) else meta.get("start_in_recording")),
        "redo_version": REDO_VERSION,
    })
    keep_user_edits(meta_path, meta)
    campaign.apply_to_meta(meta, camp)             # required hashtags + credit, campaign name
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    with open(os.path.join(dirpath, "caption.txt"), "w", encoding="utf-8") as f:
        f.write(campaign.caption_txt(meta, files_out, offset, camp))


def _raw_length(path):
    from .offline import duration
    try:
        return float(duration(path))
    except Exception:
        return 0.0


def _reedit_with_claude(pipe, cfg, dirpath, meta, login, note=None, span=None, context=None):
    """Claude re-watches the moment in raw.mp4 and re-cuts it. Returns (ok, status).
    note: what the user wants changed. span=(start, end): Claude may only cut inside it."""
    raw = os.path.join(dirpath, "raw.mp4")
    offset = _clip_offset_in_raw(meta)
    dur = float(meta.get("duration") or 30)
    length = _raw_length(raw)
    if length < 10:
        return False, "raw too short"
    lo, hi = (max(0.0, float(span[0])), min(length, float(span[1]))) if span else (0.0, length)
    anchor = min(hi - 0.5, max(lo + 0.5, offset + dur * 0.6))
    scfg = next((s for s in cfg["streamers"] if s["login"] == login), {"login": login, "tags": []})
    old_title = meta.get("title_bar") or meta.get("heading") or meta.get("title") or "(none)"
    keep = dict(meta, _offset_in_raw=offset, _redo_version=REDO_VERSION)
    # the old cut in THIS window's own timeline (the window starts at lo, your selection's start, not at 0)
    cuts = meta.get("jump_cuts") or []
    span_len = float(cuts[-1][1]) if cuts and cuts[-1] else dur
    old_a, old_b = offset - lo, offset + span_len - lo
    win_len = hi - lo
    where = f"{max(0.0, old_a):.0f}s-{min(win_len, old_b):.0f}s of this window"
    if old_a < -0.5 or old_b > win_len + 0.5:
        where += (f" (the old cut went from {old_a:.0f}s to {old_b:.0f}s: it starts before and/or ends after the "
                  f"window you chose, so only the part inside the window is available)")
    c = Candidate(
        login=login, display_name=meta.get("streamer") or login, rec_path=raw, anchor=anchor,
        source=(f"RE-EDIT of a clip we already made: the old cut was {where} and its old title was "
                f"'{old_title}'. If that title is TRUE (matches what the frames and "
                f"transcript show), KEEP it as title_bar (it was written with the chat's context and is often funnier); "
                f"only change it if it is wrong or misleading, and then keep its joke if you can. Re-cut the clip with "
                f"enough setup at the start and the reaction at the end"
                + (f". THE PAGE OWNER WATCHED THIS CLIP and says what really happens: \"{context.strip()}\". "
                   f"This is TRUE: trust it over your own reading of the frames, base what_happens and the title on it"
                   if context and context.strip() else "")
                + (f". THE PAGE OWNER ASKS: \"{note.strip()}\" (follow this)" if note and note.strip() else "")
                + (". The owner chose this window by hand: use the moment inside it" if span else "")),
        streamer_cfg=scfg, game=meta.get("game") or cfg["game"]["name"], platform=meta.get("platform", "twitch"),
        stream_title=meta.get("stream_title", ""), context_before=anchor - lo, context_after=max(1.0, hi - anchor - 0.05),
        out_folder=dirpath, keep_meta=keep,
    )
    pipe.judge.last_player_x = None
    pipe.__dict__.get("_cams", {}).clear()
    folder = pipe.process(c)
    return bool(folder), c.status


def _rename(dirpath, meta_path):
    """New title -> new folder name (keeps the time and score at the front)."""
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        name = os.path.basename(dirpath)
        prefix = "_".join(name.split("_")[:2])
        new = os.path.join(os.path.dirname(dirpath), f"{prefix}_{slugify(meta.get('title_bar') or meta.get('title') or '')}")
        if new != dirpath and not os.path.exists(new):
            os.rename(dirpath, new)
            return new
    except OSError:
        pass
    return dirpath


def run_redo(cfg, only_streamer=None):
    require_binary("ffmpeg")
    root = cfg["storage"]["output_dir"]
    if not os.path.isdir(root):
        raise SystemExit(f"No clips folder found at {root}")
    rcfg = dict(cfg["render"])
    transcriber = analyzer.Transcriber(cfg["transcription"])
    pipe = Pipeline(cfg, transcriber, ClipJudge(cfg))
    has_claude = pipe.judge.client is not None or bool(pipe.judge.claude_code)
    if has_claude:
        log("Claude will re-watch each clip and re-cut it (new title, setup at the start, reaction at the end)", "fix")
    else:
        log("Claude isn't available: clips keep their titles and only get the new style and margins", "fix")
    folders = [d for d, _, files in os.walk(root) if "meta.json" in files]
    done = skipped = failed = 0
    for dirpath in folders:
        meta_path = os.path.join(dirpath, "meta.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        login = meta.get("login") or os.path.basename(os.path.dirname(os.path.dirname(dirpath)))
        if only_streamer and login != only_streamer.lower():
            continue
        if meta.get("redo_version") == REDO_VERSION:
            skipped += 1
            continue
        if not os.path.exists(os.path.join(dirpath, "raw.mp4")):
            log(f"no raw.mp4, can't fix: {dirpath}", "fix")
            skipped += 1
            continue
        try:
            log(f"fixing {os.path.basename(dirpath)}", login)
            if int(meta.get("redo_version") or 0) < 6:
                _fix_raw_sync(dirpath)
            ok, status = False, ""
            if has_claude:
                ok, status = _reedit_with_claude(pipe, cfg, dirpath, meta, login, context=meta.get("user_context"))
                if status == "claude_limit":
                    log("Claude's hourly check limit reached: stopping here. Run fix again later to continue "
                        "(finished clips are skipped).", "fix")
                    break
                if not ok:
                    log(f"Claude re-edit didn't work ({status or 'no answer'}): restyling with the old title", login)
            if not ok:
                _restyle(pipe, transcriber, cfg, rcfg, dirpath, meta, login)
            _rename(dirpath, meta_path)
            done += 1
        except Exception as e:
            failed += 1
            log(f"could not fix {dirpath}: {str(e)[:200]}", login)
    log(f"fixed {done} clip(s), {skipped} already up to date or without raw.mp4, {failed} failed")




def _load(folder):
    folder = os.path.abspath(folder)
    meta_path = os.path.join(folder, "meta.json")
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    login = meta.get("login") or os.path.basename(os.path.dirname(os.path.dirname(folder)))
    if not os.path.exists(os.path.join(folder, "raw.mp4")):
        raise SystemExit("This clip has no raw.mp4, it can't be re-edited.")
    return folder, meta, login


def reedit_one(cfg, folder, note=None, start=None, end=None, context=None):
    """App button 'Ask Claude to re-edit': one clip, optional instructions and window."""
    require_binary("ffmpeg")
    folder, meta, login = _load(folder)
    pipe = Pipeline(cfg, analyzer.Transcriber(cfg["transcription"]), ClipJudge(cfg))
    if pipe.judge.client is None and not pipe.judge.claude_code:
        raise SystemExit("Claude isn't available (Claude Code not found / not signed in).")
    span = (start, end) if start is not None and end is not None and end > start else None
    log("Claude is re-watching this clip" + (f' (your note: "{note}")' if note else ""), login)
    context = context if context is not None else meta.get("user_context")
    before = json.loads(json.dumps(meta))         # the clip as it was, for the learning lesson
    ok, status = _reedit_with_claude(pipe, cfg, folder, meta, login, note=note, span=span, context=context)
    if not ok:
        reasons = {"claude_limit": "Claude's hourly limit is reached: try again later.",
                   "no_claude": "Claude didn't send a usable answer (it may be busy or your plan's limit is reached): "
                                "try again in a few minutes. The clip wasn't changed.",
                   "raw too short": "This clip's raw.mp4 is too short to re-edit. The clip wasn't changed."}
        raise SystemExit(reasons.get(status) or f"The re-edit didn't finish ({status or 'see the lines above'}). "
                                                 "The clip wasn't changed.")
    log("re-edit done", login)
    try:                                           # learning: what you asked to change, and what changed
        from .learning import reedit_lesson
        with open(os.path.join(folder, "meta.json"), "r", encoding="utf-8") as f:
            after = json.load(f)
        if reedit_lesson(cfg, before, after, note=note, context=context, window=span):
            log("learning: this re-edit was saved as a lesson", "learn")
    except Exception as e:
        log(f"learning: couldn't save this re-edit as a lesson ({str(e)[:100]})", "learn")


def recut_one(cfg, folder, start, end, title=None, highlight=None, emoji=None, focus=None, cam=None,
              keep_cuts=False):
    """App button 'Render my cut': exact start/end from raw.mp4, chosen by hand. No Claude.
    keep_cuts: start/end weren't changed -> keep the clip's jump cuts and callouts (e.g. new title only)."""
    require_binary("ffmpeg")
    folder, meta, login = _load(folder)
    segments = None
    cuts = meta.get("jump_cuts") or []
    if keep_cuts and cuts:
        start = _clip_offset_in_raw(meta)
        segments = [(float(a), float(b)) for a, b in cuts]
        end = start + segments[-1][1]
    transcriber = analyzer.Transcriber(cfg["transcription"])
    pipe = Pipeline(cfg, transcriber, ClipJudge(cfg, enabled=False))
    over = {"title": title, "highlight": highlight, "emoji": emoji, "focus": focus, "cam": cam, "segments": segments}
    log(f"rendering your cut {start:.1f}s - {end:.1f}s of raw.mp4", login)
    _restyle(pipe, transcriber, cfg, dict(cfg["render"]), folder, meta, login, span=(start, end), over=over)
    log("your cut is ready", login)
