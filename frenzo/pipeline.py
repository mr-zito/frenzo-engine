"""One moment in -> zero or one finished clip out."""
import csv
import datetime as dt
import json
import os
import re
import shutil
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import analyzer, campaign, censor, crowd, editor
from . import layout as layout_mod
from . import hook as hook_mod
from . import scoring, titlecard, titles, viral
from . import sfx as sfx_mod
from .learning import clip_value, min_score_for
from .util import log, slugify

_csv_lock = threading.Lock()


@dataclass
class Candidate:
    login: str
    display_name: str
    rec_path: str
    anchor: float                 # seconds into the recording where the moment peaks
    source: str                   # why it was flagged
    ready_at: float = 0.0         # wall time when enough video after the anchor is recorded
    chat: object = None           # ChatListener (live mode) or None
    t0: Optional[float] = None    # wall time of recording start (live mode)
    streamer_cfg: dict = field(default_factory=dict)
    game: str = ""
    platform: str = "twitch"
    context_before: Optional[float] = None   # longer window for running gags
    context_after: Optional[float] = None
    superseded: bool = False                 # covered by a running-gag window: skip
    started: bool = False
    stream_title: str = ""
    on_done: object = None        # callback after processing
    out_folder: Optional[str] = None   # re-edit an existing clip folder (fix command)
    keep_meta: Optional[dict] = None   # that clip's old meta.json
    status: str = ""                   # why it stopped (fix command reads it)
    manual: bool = False               # cut by hand in the app: always make the clip
    forced: Optional[dict] = None      # decision given by hand (no Claude)
    fallback: Optional[dict] = None    # decision to use if Claude can't answer
    user_context: str = ""             # what happens, written by you
    owner_note: str = ""               # re-edit: what you asked to change -> your note beats the automatic rules
    keep_cues: Optional[list] = None   # re-edit note that isn't about sound: the clip's sounds (window time), kept
    keep_framing: bool = False         # re-edit note about the framing: Claude's focus_x is used as it is
    learn_log: Optional[tuple] = None  # (decision, note, window start) for the learning decision log
    # crowd evidence (viewer clips, heatmap, chat replay) and the editing queue
    clips: list = field(default_factory=list)   # crowd.ViewerClip objects that landed on this moment
    clip_span: Optional[tuple] = None  # (a, b) seconds in the recording the viewer clips cover: the window's core
    priority: float = 0.0              # combined score: the editing queue takes the best moment first
    keep_bonus: int = 0                # strong crowd evidence lowers the keep threshold by this much
    evidence: str = ""                 # extra crowd lines for Claude (heatmap peak, chat replay...)
    signals: dict = field(default_factory=dict)  # signal name -> strength (z-score like), for the combined score
    flagged_wall: float = 0.0          # wall time it was flagged (speed log)
    judged: Optional[dict] = None      # Claude's answer, once judged
    folder: Optional[str] = None       # the finished clip, once saved
    done: bool = False


def _nudity_raw(d, win_start, clip_abs, raw_offset, redo):
    """Claude's nudity times (window seconds) -> seconds in raw.mp4, so later re-renders can pixelate them."""
    if not d.get("nudity"):
        return None
    raw_start = 0.0 if redo else clip_abs - raw_offset     # where raw.mp4 starts in the source
    out = []
    for r in d.get("nudity_at") or []:
        try:
            out.append([round(win_start + float(r[0]) - raw_start, 2), round(win_start + float(r[1]) - raw_start, 2)])
        except (TypeError, ValueError, IndexError):
            pass
    return out or None


def _norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()


def map_cues(cues, to_out, out_dur, lib, camp, rcfg, repeat=False):
    """Claude's cues (window seconds) -> the final timeline through the jump cuts. Cues in cut-out pieces are dropped,
    unknown sound ids are fixed (close typo) or dropped, safe mode is enforced.
    repeat: the same sound may play more than once (you asked for it in a re-edit note)."""
    from . import soundlib
    safe = rcfg.get("sound_safe_mode", True)
    music = not safe or bool((camp or {}).get("allow_music"))
    explicit = not safe or bool((camp or {}).get("allow_explicit"))
    guide = soundlib.guide(rcfg, lib)              # your sound list: only these, each with its own effect
    entries = soundlib.load(lib)
    out, seen = [], set()
    for cu in cues or []:
        if not isinstance(cu, dict):
            continue
        try:
            t = to_out(float(cu.get("t")))
        except (TypeError, ValueError):
            continue
        sid = soundlib.fix_id(cu.get("sfx"), lib, music, explicit, only=set(guide) or None)
        if t is None or sid is None or not 0 <= t < out_dur:
            continue
        key = sid if guide and not repeat else (sid, round(t, 1))   # with your list: never the same sound twice
        if key in seen:
            continue
        seen.add(key)
        fx = [f for f in (cu.get("fx") or []) if f in ("zoom", "shake", "flash")]
        mute_after = 0.0
        if guide:
            eff = guide[sid].get("effect") or ""
            fx = list(soundlib.EFFECT_FX.get(eff, []))
            if eff == "silence_after":
                mute_after = 1.2
        kind = cu.get("kind") if cu.get("kind") in ("hit", "voice", "music", "whoosh", "riser", "transition", "ui") \
            else "hit"
        try:
            gain = max(-30.0, min(6.0, float(cu.get("gain_db") or 0)))
            stop = max(0.0, min(0.6, float(cu.get("stop") or 0)))
            max_len = max(0.2, min(8.0, float(cu.get("max_len") or 8.0)))
        except (TypeError, ValueError):
            gain, stop, max_len = 0.0, 0.0, 8.0
        try:                                       # silence a sound in the stream audio (this sound replaces it)
            mute_game = max(0.0, min(8.0, float(cu.get("mute_game") or 0)))
        except (TypeError, ValueError):
            mute_game = 0.0
        row = {"t": round(t, 3), "sfx": sid, "role": cu.get("role") or "", "align": cu.get("align") or "start",
               "gain_db": gain, "stop": stop, "duck": bool(cu.get("duck", kind in ("voice", "music"))),
               "kind": kind, "max_len": max_len, "fx": fx, "mute_after": mute_after,
               "dur": round(min(max_len, float(entries[sid].get("duration") or 1.0)), 3)}
        if mute_game:
            row["mute_game"] = round(mute_game, 2)
        try:                                       # the sound check's earlier timing fix stays with the sound
            if cu.get("nudge"):
                row["nudge"] = round(max(-0.5, min(0.5, float(cu["nudge"]))), 3)
        except (TypeError, ValueError):
            pass
        out.append(row)
    cap = int(rcfg.get("sound_max_per_clip", 0) or 0) if guide else 0
    if cap and len(out) > cap:                      # Claude lists the most important first: keep those
        out = out[:cap]
    return sorted(out, key=lambda x: x["t"])


def freeze_cue(cues, rcfg, lib, segs):
    """anime-wow ("freeze_zoom" in your sound list): the picture pauses on that frame while the sound plays.
    Returns (window second, seconds held) or None. Uses Claude's window-time cue, before the timeline is built."""
    from . import soundlib
    guide = soundlib.guide(rcfg, lib)
    for cu in cues or []:
        if not isinstance(cu, dict):
            continue
        sid = soundlib.fix_id(cu.get("sfx"), lib, only=set(guide) or None) if guide else None
        if sid and (guide[sid].get("effect") == "freeze_zoom"):
            try:
                t = float(cu.get("t"))
            except (TypeError, ValueError):
                return None
            if any(a + 0.3 <= t <= b - 0.3 for a, b in segs):
                return t, min(1.8, float(soundlib.load(lib)[sid].get("duration") or 1.5))
    return None


def sound_check(folder, files, cues, rows, lib, game_wav, out_dur, rcfg, login, fix=True):
    """check_reel.py on the clean clip; one automatic fix (mix again, put the sound into every version with
    -c:v copy); still failing -> needs review (the report stays in sound_qa/).
    fix=False (your own renders: new framing, longer cut, new title): only checked, never changed again; the
    clip's cues already carry the fix from when it was made, so it sounds exactly as before."""
    from . import soundlib, soundpass
    clip = os.path.join(folder, files[0])
    soundpass.write_cues_json(clip, rows, out_dur)
    entries = soundlib.load(lib)
    meta = {"library": str(lib), "cues": cues, "autofix": [], "needs_review": False,
            "music": [cu["sfx"] for cu in cues if "music" in (entries.get(cu["sfx"], {}).get("tags") or [])]}
    if not rcfg.get("sound_check", True):
        return meta
    qa_dir = os.path.join(folder, "sound_qa")
    qa = soundpass.check(clip, lib, qa_dir)
    bad = soundpass.failing(qa)
    if bad and fix:
        new_cues, changes = soundpass.autofix(cues, qa)
        log("sound check: " + "; ".join(bad[:4]) + (" -> fixing: " + "; ".join(changes[:4]) if changes else ""), login)
        if changes:
            rows = soundpass.placed(new_cues, lib)
            mixed = soundpass.mix(game_wav, rows, os.path.join(folder, "_mix.wav"), out_dur)
            for f in files:
                soundpass.remux(os.path.join(folder, f), mixed)
            soundpass.write_cues_json(clip, rows, out_dur)
            meta["cues"], meta["autofix"] = new_cues, changes
            qa = soundpass.check(clip, lib, qa_dir)
            bad = soundpass.failing(qa)
    if qa:
        meta["qa"] = {"lufs": qa.get("lufs"), "true_peak_dbfs": qa.get("true_peak_dbfs"),
                      "cues_on_sync": sum(1 for r in qa.get("cues") or [] if str(r.get("verdict", "")).startswith("ok")),
                      "cues": len(qa.get("cues") or []),
                      "masked": [r["t"] for r in qa.get("cues") or [] if r.get("verdict") == "masked"],
                      "off_sync": [r["t"] for r in qa.get("cues") or [] if r.get("verdict") == "off sync"],
                      "issues": qa.get("issues") or []}
    if bad:
        meta["needs_review"] = True
        log(("sound check still failing after one fix" if fix else "sound check (your render: sounds kept exactly as "
             "they were)") + ": clip marked NEEDS REVIEW (" + "; ".join(bad[:3]) + ")", login)
    return meta


class Pipeline:
    def __init__(self, cfg, transcriber, judge):
        self.cfg = cfg
        self.transcriber = transcriber
        self.judge = judge
        self.saved = {}  # login -> [wall times of saved clips]

    def _rate_ok(self, login):
        now = time.time()
        for k in list(self.saved):
            self.saved[k] = [t for t in self.saved[k] if t > now - 3600]
        total = sum(len(v) for v in self.saved.values())
        cap_total = int(self.cfg["clips"].get("max_per_hour_total") or 0)
        if cap_total and total >= cap_total:
            return False
        return len(self.saved.get(login, [])) < self.cfg["clips"]["max_per_hour_per_streamer"]

    def process(self, c: Candidate):
        folder = None
        try:
            folder = self._process(c)
            c.folder = folder
            return folder
        except Exception as e:
            log(f"moment at {c.anchor:.0f}s failed: {e}", c.login)
            return None
        finally:
            # learning: remember what the engine decided about this moment (never for your own cuts or re-edits)
            if c.learn_log is not None and not c.manual and c.out_folder is None:
                from .learning import log_decision
                d, note, win_start = c.learn_log
                log_decision(self.cfg, c, d, bool(folder), win_start, note)
            c.done = True
            if c.on_done:
                c.on_done()

    def _process(self, c: Candidate):
        ccfg, rcfg = self.cfg["clips"], self.cfg["render"]
        camp = campaign.of(c.streamer_cfg)              # campaign rules: editing and writing only
        rcfg = campaign.render_cfg(rcfg, camp)
        redo = c.out_folder is not None
        if redo:
            rcfg = dict(rcfg, save_raw=False)          # raw.mp4 is the source here: keep it as it is
        # a re-edit with your note: Claude's cut and sounds follow it, so the automatic rules (dead-air trim, open on
        # the action, margins, auto sounds, sound cap, title swap, 45 s limit) don't change its answer afterwards
        obey = redo and bool((c.owner_note or "").strip()) and rcfg.get("reedit_obey_note", True)
        if obey and c.keep_framing:                # "recenter" etc.: the webcam guard doesn't move Claude's framing
            rcfg = dict(rcfg, game_avoid_cam=False)
        if not redo and not c.manual and not self._rate_ok(c.login):
            log("hourly clip limit reached (see max_per_hour in config), skipping moment", c.login)
            c.learn_log = (None, "not judged: hourly clip limit reached", None)
            return None
        wait = c.ready_at - time.time()
        if wait > 0:
            time.sleep(wait)
        if c.superseded:
            log("moment merged into a running-gag clip, skipping on its own", c.login)
            return None
        c.started = True

        before = c.context_before if c.context_before is not None else ccfg["context_before"]
        after = c.context_after if c.context_after is not None else ccfg["context_after"]
        if c.clip_span and c.context_before is None:
            # viewer clips: their span is the core of the window, plus a lead-in for the setup
            det = self.cfg["detection"]
            before = max(10.0, c.anchor - (c.clip_span[0] - float(det.get("clip_lead_in", 25))))
            after = max(5.0, c.clip_span[1] + float(det.get("clip_tail", 8)) - c.anchor)
        win_start = max(0.0, c.anchor - before)
        win_dur = c.anchor + after - win_start
        log(f"analysing {win_dur:.0f}s window around {c.anchor:.0f}s ({c.source[:240]})", c.login)

        audio = analyzer.extract_audio(c.rec_path, win_start, win_dur)
        if len(audio) < analyzer.RATE * 5:
            log("not enough recorded audio for this moment yet, skipping", c.login)
            c.learn_log = (None, "not judged: not enough recorded audio", win_start)
            return None
        win_dur = min(win_dur, len(audio) / analyzer.RATE)
        loud = analyzer.loudness_per_second(audio)
        words, lang = self.transcriber.transcribe(audio)
        rel = c.anchor - win_start          # where the signal (chat spike, reaction...) happened in the window
        # dense frames around the flagged second, so Claude sees the setup, the payoff and the reaction
        # (a title written from 2-3 frames is a guess; most "wrong titles" came from that)
        times = [win_dur * 0.12, win_dur * 0.35] + [rel + o for o in (-22, -16, -12, -9, -6, -4, -2, 0, 2, 4, 7, 10)]
        if win_dur > 120:          # running gag: also look at the earlier beats
            times += [win_dur * f for f in (0.22, 0.5, 0.62, 0.75)]
        frames = analyzer.grab_frames(c.rec_path, win_start, win_dur, width=768,
                                      times=sorted({round(max(0.5, min(win_dur - 0.5, t)), 1) for t in times}))

        chat_lines = []
        if c.chat is not None and c.t0 is not None:
            for t, user, text in c.chat.snapshot(c.t0 + win_start, c.t0 + win_start + win_dur):
                chat_lines.append(f"[{t - c.t0 - win_start:5.1f}s] {user}: {text[:120]}")

        ctx = {
            "streamer": c.display_name, "game": c.game, "source": c.source, "window": win_dur,
            "platform": c.platform, "stream_title": c.stream_title, "flag_at": rel,
            "transcript": analyzer.transcript_lines(words), "chat_lines": chat_lines,
            "loudness": loud, "frames": frames, "tags": c.streamer_cfg.get("tags", []),
            "scfg": c.streamer_cfg,                  # so ai.py can read the streamer's campaign rules
            "crowd": crowd.prompt_text(c.clips) + (c.evidence or ""),
            "score_line": scoring.describe(c),
        }
        d = dict(c.forced) if c.forced else self.judge.decide(ctx)
        c.learn_log = (d, None, win_start)
        c.judged = d
        if obey and c.keep_cues is not None and isinstance(d, dict) and not str(d.get("reason", "")).startswith("skipped:"):
            d = dict(d, cues=[dict(cu) for cu in c.keep_cues])   # your note isn't about sound: the sounds stay
            c.judged = d
            log(f"re-edit: your note doesn't mention sounds, so the clip keeps its {len(c.keep_cues)} sound(s)", c.login)
        if c.manual and not c.forced:
            reason = str(d.get("reason", ""))
            if reason.startswith("skipped:"):
                c.status = "claude_limit"
                return None
            if reason.startswith("backup rule") or "start" not in d or "end" not in d:
                log("Claude didn't answer: making the clip as you cut it", c.login)
                d = dict(c.fallback)
            d["keep"] = True
        if c.manual and c.forced is None and d.get("category") != "manual":
            d.setdefault("viral_score", 0)
        score = int(d.get("viral_score", 0) or 0)
        log(f"score {score}/10 - {d.get('reason', '')}", c.login)
        if d.get("what_happens"):
            log(f"what happens: {str(d['what_happens'])[:160]}", c.login)
        if redo:
            # re-edit: the clip was already chosen; only use a real answer from Claude
            reason = str(d.get("reason", ""))
            if reason.startswith("skipped:"):
                c.status = "claude_limit"
                return None
            if reason.startswith("backup rule") or "start" not in d or "end" not in d:
                c.status = "no_claude"
                return None
        elif not c.manual:
            need = min_score_for(self.cfg, c.streamer_cfg) - int(c.keep_bonus or 0)
            if score < need or not (d.get("keep") or c.keep_bonus):
                c.status = "rejected"
                return None
            if c.keep_bonus and not d.get("keep"):
                log(f"kept on the viewers' word: {score}/10 with strong clip evidence (threshold {need})", c.login)
        nudity_mode, chosen = censor.mode(self.cfg), c.manual or redo
        blur = bool(d.get("nudity")) and (nudity_mode == "blur" or (chosen and nudity_mode == "skip"))
        if chosen:
            # you chose this clip (your own cut, or a re-edit you asked for): never thrown away, only flagged
            if d.get("nudity"):
                log("note: Claude says this shows nudity: " + ("it will be pixelated" if blur else "kept as is (nudity: keep)"),
                    c.login)
            if d.get("spoiler"):
                log("note: Claude marked this a story spoiler; kept because you chose this clip", c.login)
        elif d.get("nudity") and nudity_mode == "skip":
            log("skipped: shows nudity (platforms remove these clips; set nudity: blur to keep them pixelated)", c.login)
            c.status = "nudity"
            return None
        elif d.get("spoiler") and ccfg["skip_spoilers"]:
            log("skipped: story spoiler", c.login)
            c.status = "spoiler"
            return None

        min_len, max_len = campaign.length_bounds(ccfg, camp)
        if obey:                                   # your note sets the length (a campaign's max still applies)
            max_len = float(camp["max_seconds"]) if camp and camp.get("max_seconds") else max(max_len, 180.0)
        s0, e0 = float(d["start"]), float(d["end"])
        # the length limit counts the seconds that are KEPT: pieces Claude cut out in the middle don't count
        kept0 = viral.clean_segments(d.get("segments"), s0, e0) if rcfg.get("jump_cuts", True) else [[s0, e0]]
        kept_len = sum(b2 - a2 for a2, b2 in kept0)
        span_max = max_len + max(0.0, (e0 - s0) - kept_len) if kept_len <= max_len else max_len
        start, end = self._fix_bounds(s0, e0, win_dur, min_len, span_max)
        clip_dur = end - start

        clip_abs = win_start + start
        layout = rcfg["layout"]
        facecam = c.streamer_cfg.get("facecam")
        if layout in ("auto", "reel"):
            if not facecam:
                facecam = self._facecam(c, clip_abs, clip_dur)
            layout = "reel" if facecam else "focus"
        game_crop_w = (editor.W / editor.REEL_GAME_H) * (1 / rcfg.get("game_zoom", 1.0)) * 9 / 16
        if layout == "focus":
            game_crop_w = (editor.W / editor.H) * 9 / 16
        focus_x = d.get("focus_x")
        try:
            focus_x = float(focus_x)
            if not 0 <= focus_x <= 1:
                raise ValueError
        except (TypeError, ValueError):
            focus_x = layout_mod.action_focus(c.rec_path, clip_abs, clip_dur, game_crop_w, facecam)

        # ---- jump cuts: Claude's keep-segments (window time), mapped to the output timeline ----
        segs = viral.clean_segments(d.get("segments"), start, end) if rcfg.get("jump_cuts", True) else [[start, end]]
        if sum(b2 - a2 for a2, b2 in segs) < ccfg["min_length"] * 0.6:   # Claude cut too much: keep the whole span
            segs = [[start, end]]
        if sum(b2 - a2 for a2, b2 in segs) > max_len:                  # too long: trimmed from the end
            segs = campaign.fit_segments(segs, 0.0, win_dur, 0, max_len)
        # ---- hook rules (first 3 s) and pacing: Claude chooses, code enforces ----
        hook_on = bool(rcfg.get("hook", True)) and not c.forced and layout in ("reel", "focus")
        ht, hp = hook_mod.hook_piece(d, win_dur, tuple(rcfg.get("hook_types") or hook_mod.DEFAULT_TYPES)) \
            if hook_on else (None, None)
        lead, tail = float(clip_value(self.cfg, "lead_in", 1.5)), float(clip_value(self.cfg, "tail", 2.0))
        if hook_on:                        # open on the action, end 1-2 s after the reaction (clean loop)
            lead, tail = min(lead, 0.5), max(1.0, min(tail, 2.0))
        if obey:                           # your note: Claude's start and end exactly
            lead, tail = 0.0, 0.0
        # ---- context margins: a little before the setup and after the reaction, so the clip makes sense ----
        segs = viral.add_margins(segs, lead, tail, 0.0, win_dur,
                                 min_setup_before=(rel, float(ccfg.get("min_setup", 4.0)))
                                 if c.source and not obey else None)
        hook_notes = []
        if hook_on and not obey:
            if rcfg.get("trim_dead_air", True):
                before_n = sum(b2 - a2 for a2, b2 in segs)
                segs = hook_mod.trim_dead_air(segs, words, loud, protect=rel)
                cut = before_n - sum(b2 - a2 for a2, b2 in segs)
                if cut > 0.5:
                    hook_notes.append(f"cut {cut:.1f}s of dead air")
            if ht not in ("cold_open", "reaction_first"):
                a0 = segs[0][0]
                # "action" hook: open right on the strongest second Claude picked (hook_in), within 6 s
                if ht == "action" and hp and segs[0][0] < hp[0] < min(segs[0][1] - 3, segs[0][0] + 6):
                    segs[0][0] = hp[0]
                segs = hook_mod.trim_leading_quiet(segs, loud, protect=rel)
                if segs[0][0] > a0:
                    hook_notes.append(f"opens {segs[0][0] - a0:.1f}s later, on the action")
        if camp and (camp.get("min_seconds") or camp.get("max_seconds")):
            if c.manual and c.forced:              # your own cut "as is": kept exactly as you chose it
                total = sum(b2 - a2 for a2, b2 in segs)
                if (camp.get("max_seconds") and total > camp["max_seconds"]) or total < (camp.get("min_seconds") or 0):
                    log(f"note: your cut is {total:.0f}s, outside the campaign's length rule", c.login)
            else:                                  # campaign length rule: never shorter than min, never longer than max
                segs = campaign.fit_segments(segs, 0.0, win_dur, min_len, max_len)
        speeds = [1.0] * len(segs)
        frz = None
        if rcfg.get("sound_effects", True) and rcfg.get("sound_library", True) and rcfg.get("punch_fx", True):
            from . import soundlib
            _lib = soundlib.find_library(rcfg)
            frz = freeze_cue(d.get("cues"), rcfg, _lib, segs) if _lib is not None else None
        if frz:                                    # pause on one frame (anime-wow): a near-still piece held frz[1] s
            t_f, hold = frz
            new, sp_new = [], []
            for (a2, b2) in segs:
                if a2 + 0.3 <= t_f <= b2 - 0.3:
                    new += [[a2, t_f], [t_f, t_f + 0.04], [t_f + 0.04, b2]]
                    sp_new += [1.0, 0.04 / hold, 1.0]
                else:
                    new.append([a2, b2])
                    sp_new.append(1.0)
            segs, speeds = new, sp_new
            hook_notes.append(f"picture pauses {hold:.1f}s at {t_f:.1f}s (anime-wow)")
        if hook_on and d.get("slowmo") and not frz:
            segs, speeds = hook_mod.apply_slowmo(segs, d.get("slowmo"))
            if 0.5 in speeds:
                hook_notes.append("0.5x slow-mo on the payoff")
        pieces, piece_speeds, hook_len = [list(s) for s in segs], list(speeds), 0.0
        if hook_on and ht in ("cold_open", "reaction_first") and hp:
            pieces, piece_speeds = [list(hp)] + pieces, [1.0] + piece_speeds      # the hook piece plays first
            hook_len = hp[1] - hp[0]
        start, end = min(p[0] for p in pieces), max(p[1] for p in pieces)
        clip_abs = win_start + start
        to_out = hook_mod.mapper(segs, speeds, offset=hook_len)     # times map onto the main (not the hook) copy
        out_dur = hook_len + sum((b2 - a2) / sp for (a2, b2), sp in zip(segs, speeds))
        seg_rel = [(a2 - start, b2 - start) for a2, b2 in pieces]
        title_rep = None
        old_t = (c.keep_meta or {}).get("title_bar") or (c.keep_meta or {}).get("heading") or ""
        kept_old_title = redo and old_t and _norm_title(d.get("title_bar")) == _norm_title(old_t)
        if kept_old_title:
            log("title: Claude kept the clip's title", c.login)
        if rcfg.get("five_titles", True) and d.get("titles") and not c.forced and not kept_old_title:
            picked, title_rep = titles.pick(d)
            if picked:
                d["title_bar"] = picked
                if d.get("highlight") and str(d["highlight"]).lower() not in picked.lower():
                    d["highlight"] = None
                if title_rep.get("note"):
                    log("title: " + title_rep["note"], c.login)
        for k in ("title_bar", "heading"):
            if d.get(k) and rcfg.get("title_fix_doubles", True):
                fixed = titles.drop_doubled(d[k])
                if fixed != d[k]:
                    log(f"title: doubled word removed ({d[k]!r} -> {fixed!r})", c.login)
                    d[k] = fixed

        sfx_list, cues_out, fx_events, lib = [], [], [], None
        if rcfg.get("sound_effects", True) and rcfg.get("sound_library", True):
            from . import soundlib
            lib = soundlib.find_library(rcfg)
            if lib is None:
                log("sound library not found (render.sound_library_path): using the old sus sound", c.login)
        if lib is not None:
            rc_cap = rcfg
            if obey:                                  # the sounds you asked for: no cap, no automatic extras
                rc_cap = dict(rcfg, sound_max_per_clip=0)
            elif hook_on and rcfg.get("sound_max_per_clip"):   # one of the sounds is kept for the hook
                rc_cap = dict(rcfg, sound_max_per_clip=max(1, int(rcfg["sound_max_per_clip"]) - (2 if hook_len else 1)))
            cues_out = map_cues(d.get("cues"), to_out, out_dur, lib, camp, rc_cap, repeat=obey)
            from . import soundlib as _sl
            _g = _sl.guide(rcfg, lib)
            # the rewind sound belongs to the hook only (on the cut back to the start), never mid-clip
            cues_out = [cu for cu in cues_out if (_g.get(cu["sfx"]) or {}).get("effect") != "hook_rewind"
                        or (hook_len and abs(cu["t"] - hook_len) < 0.6)]
            if hook_on and not obey:
                from . import soundlib
                safe = rcfg.get("sound_safe_mode", True)

                _only = set(soundlib.guide(rcfg, lib)) or None      # sounds added by code: your list too

                def fid(s, lb):
                    return soundlib.fix_id(s, lb, not safe or bool((camp or {}).get("allow_music")),
                                           not safe or bool((camp or {}).get("allow_explicit")), only=_only)
                pref = fid(d.get("hook_sfx"), lib) if d.get("hook_sfx") else None
                if ht == "tension" and hp:          # the riser / suspense sound ends on the hit
                    t_hit = to_out(hp[1])
                    riser = next((s for s in ([pref] if pref else []) + ["riser-air", "sudden-suspense",
                                                                           "hells-kitchen-suspense"] if fid(s, lib)), None)
                    riser = fid(riser, lib) if riser else None
                    if t_hit is not None and riser and not any(cu["align"] == "end" and abs(cu["t"] - t_hit) < 0.3
                                                               for cu in cues_out):
                        cues_out.append({"t": round(t_hit, 3), "sfx": riser, "role": "support", "align": "end",
                                         "gain_db": -6.0, "stop": 0.0, "duck": False, "kind": "riser", "max_len": 3.0,
                                         "fx": [], "auto": True})
                        hook_notes.append(f"riser {riser} ends on the hit at {t_hit:.2f}s")
                elif pref and hook_len and not any(abs(cu["t"] - hook_len) < 0.25 for cu in cues_out):
                    cues_out.append({"t": round(hook_len, 3), "sfx": pref, "role": "support", "align": "start",
                                     "gain_db": -3.0, "stop": 0.0, "duck": False, "kind": "transition", "max_len": 1.5,
                                     "fx": [], "auto": True})
                cap = int(rcfg.get("sound_max_per_clip", 0) or 0) if soundlib.guide(rcfg, lib) else 0
                # opens loud already (a yell, a crash)? then the picture + that sound are the hook: add nothing
                a0 = int(pieces[0][0]) if pieces else 0
                opens_loud = bool(loud) and max(loud[a0:a0 + 2] or [-99]) >= statistics.median(loud) + 4
                cues_out, notes = hook_mod.ensure_opening_sound(cues_out, ht, hook_len, lib, fid, cap=0, prefer=pref,
                                                                add=not opens_loud)
                hook_notes += notes
                for cu in cues_out:                 # sounds added by code get their effect from your list too
                    g = soundlib.guide(rcfg, lib).get(cu["sfx"]) if cu.get("auto") else None
                    if g:
                        keep = ["zoom"] if "zoom" in (cu.get("fx") or []) else []   # the hook's punch-in stays
                        cu["fx"] = list(dict.fromkeys(list(soundlib.EFFECT_FX.get(g.get("effect") or "", [])) + keep))
                        cu.setdefault("dur", float(soundlib.load(lib)[cu["sfx"]].get("duration") or 1.0))
            fx_events = [(cu["t"], cu["fx"], cu.get("dur") or 1.0) for cu in cues_out if cu.get("fx")]
            if frz:                                 # zoom on the item during the pause
                t_out = to_out(frz[0])
                if t_out is not None:
                    fx_events.append((t_out, ["zoom_hold"], frz[1]))
        elif rcfg.get("sound_effects", True):
            allowed = [t.lower() for t in rcfg.get("sfx_types", ["sus"])]
            for e in (d.get("sfx") or [])[:1]:
                if str(e.get("type", "")).lower() not in allowed:
                    continue
                try:
                    at = to_out(float(e.get("at")))
                except (TypeError, ValueError, AttributeError):
                    continue
                path = sfx_mod.pick(e.get("type"))
                if path and at is not None and 0 <= at < out_dur:
                    sfx_list.append((path, at))

        now = dt.datetime.now()
        if redo:
            folder = c.out_folder
            for old in os.listdir(folder):
                if old.startswith("clip") and (old.endswith(".mp4") or old.endswith(".cues.json")) \
                        or old in ("thumb.jpg", "captions.ass"):
                    os.remove(os.path.join(folder, old))
            shutil.rmtree(os.path.join(folder, "sound_qa"), ignore_errors=True)
        else:
            folder = os.path.join(
                self.cfg["storage"]["output_dir"], slugify(c.login), now.strftime("%Y-%m-%d"),
                f"{now.strftime('%H%M%S')}_s{score}_{slugify(d.get('title_bar') or d.get('heading') or d.get('title'))}",
            )
        os.makedirs(folder, exist_ok=True)
        title_png = None
        if layout in ("reel", "focus"):
            # viral style: title on the seam + small captions + coloured callouts
            title_text = d.get("title_bar") or d.get("heading") or ""
            if title_text and rcfg.get("title_bar", True):
                title_png = os.path.join(folder, "title.png")
                if not titlecard.render_title(viral.censor(title_text), d.get("highlight"), d.get("emoji"),
                                              title_png, size=rcfg.get("title_size", 58)):
                    title_png = None
            words_out = []
            for w in words:
                t0, t1 = to_out(w["start"]), to_out(w["end"])
                if t0 is not None:
                    words_out.append({"start": t0, "end": t1 if t1 is not None else t0 + 0.3, "word": w["word"]})
            callouts = []
            for co in (d.get("callouts") or [])[:5]:
                try:
                    t = to_out(float(co.get("at")))
                except (TypeError, ValueError, AttributeError):
                    continue
                if t is not None and str(co.get("text", "")).strip():
                    callouts.append({"text": str(co["text"])[:40], "at": t, "color": co.get("color", "yellow")})
            if cues_out:                          # callouts only on beats that also have a sound
                callouts = [co for co in callouts if any(abs(cu["t"] - co["at"]) <= 0.5 for cu in cues_out)]
            if rcfg.get("caption_style", "bold") == "bold":
                ass = viral.build_ass_bold(words_out, callouts, out_dur, rcfg, layout, editor.REEL_CAM_H,
                                           punch_words=d.get("punch_words") if isinstance(d.get("punch_words"), list) else None,
                                           hook_text=d.get("hook_text") if hook_on else None,
                                           hook_end=max(hook_len, 1.5) if hook_on else 0.0,
                                           title=d.get("title_bar"))
            else:
                ass = viral.build_ass(words_out, callouts, out_dur, rcfg, layout, editor.REEL_CAM_H)
        else:
            use_text_tag = not editor.handle_groups(rcfg) and not (rcfg.get("tag_image") and os.path.exists(rcfg["tag_image"]))
            ass = editor.build_ass(words, start, out_dur, d, rcfg, use_text_tag, layout)
            callouts = []
        mixed, game_wav, sound_rows = None, None, []
        if lib is not None and cues_out:
            # sound pass: game audio with the jump cuts + the library cues, -14 LUFS (the picture isn't touched)
            from . import soundpass
            try:
                game_wav = soundpass.cut_game_audio(c.rec_path, clip_abs, seg_rel, os.path.join(folder, "_game.wav"),
                                                    speeds=piece_speeds)
                sound_rows = soundpass.placed(cues_out, lib)
                mixed = soundpass.mix(game_wav, sound_rows, os.path.join(folder, "_mix.wav"), out_dur)
            except Exception as e:
                log(f"sound pass failed ({str(e)[:120]}): game audio only", c.login)
                mixed, sound_rows = None, []
        files, raw_offset = editor.render(c.rec_path, clip_abs, out_dur, folder, ass, layout, facecam,
                                          focus_x, sfx_list, rcfg, rcfg.get("raw_padding", 60),
                                          segments=seg_rel, title_png=title_png, mixed_audio=mixed,
                                          fx_events=fx_events, hook_zoom=hook_on, speeds=piece_speeds,
                                          cam_only_until=hook_len if layout == "reel" else 0.0)
        censored, censor_regs = None, None
        if blur:                                    # nudity: find it in the finished clip, then render again pixelated
            ranges = censor.map_ranges(d.get("nudity_at"), to_out)
            if not ranges and chosen:
                ranges = [(0.0, out_dur)]           # Claude didn't say when: treat the whole clip as unsafe
            regs, info = censor.plan(os.path.join(folder, files[0]), ranges, out_dur, layout, chosen) \
                if (ranges or chosen) else (None, {})
            if regs is None:
                log("skipped: shows nudity and it couldn't be located safely to pixelate it", c.login)
                if not redo:
                    shutil.rmtree(folder, ignore_errors=True)
                c.status = "nudity"
                return None
            files, _ = editor.render(c.rec_path, clip_abs, out_dur, folder, ass, layout, facecam, focus_x, sfx_list,
                                     dict(rcfg, censor_regions=regs, save_raw=False), rcfg.get("raw_padding", 60),
                                     segments=seg_rel, title_png=title_png, mixed_audio=mixed, fx_events=fx_events,
                                     hook_zoom=hook_on, speeds=piece_speeds,
                                     cam_only_until=hook_len if layout == "reel" else 0.0)
            censored = dict(info, spots=len(regs))
            censor_regs = regs
            log(f"nudity pixelated: {len(regs)} spot(s) (detector {info.get('detector_spots', 0)}, "
                f"Claude's times {info.get('claude_spots', 0)}" + (", whole gameplay" if info.get("whole_clip") else "") + ")",
                c.login)
        if title_png and os.path.exists(title_png):
            os.remove(title_png)
        sound_meta = None
        if mixed:
            sound_meta = sound_check(folder, files, cues_out, sound_rows, lib, game_wav, out_dur, rcfg, c.login)
            from . import soundlib
            soundlib.remember(self.cfg, rcfg, [cu["sfx"] for cu in cues_out])
        for tmp in ("_game.wav", "_mix.wav"):
            try:
                os.remove(os.path.join(folder, tmp))
            except OSError:
                pass
        clip_dur = out_dur

        tags = " ".join(dict.fromkeys(
            [t if t.startswith("#") else f"#{t}" for t in d.get("hashtags", []) + ctx["tags"]]))
        meta = {
            "streamer": c.display_name, "login": c.login, "platform": c.platform,
            "stream_title": c.stream_title, "game": c.game,
            "title": d.get("title"), "caption": d.get("caption"), "hashtags": tags,
            "heading": d.get("heading"), "title_bar": d.get("title_bar"), "highlight": d.get("highlight"),
            "emoji": d.get("emoji"), "category": d.get("category"),
            "jump_cuts": [[round(a2, 2), round(b2, 2)] for a2, b2 in seg_rel],
            "piece_speeds": piece_speeds if any(sp != 1.0 for sp in piece_speeds) else None,
            "callouts": [{"text": co["text"], "at": round(co["at"], 2), "color": co["color"]} for co in callouts],
            "sound_effects": [{"file": os.path.basename(p), "at": round(t, 2)} for p, t in sfx_list],
            "sound": sound_meta, "needs_review": bool(sound_meta and sound_meta.get("needs_review")) or None,
            "hook": {"type": ht, "in": hp[0] if hp else None, "out": hp[1] if hp else None, "length": round(hook_len, 2),
                     "text": d.get("hook_text"), "sfx": d.get("hook_sfx"), "notes": hook_notes} if hook_on else None,
            "titles": title_rep, "punch_words": d.get("punch_words") or None,
            "slow_motion": [list(p) for p, sp in zip(seg_rel, piece_speeds) if sp != 1.0] or None,
            "what_happens": d.get("what_happens"),
            "user_context": c.user_context or None, "cut_by_hand": c.manual or None,
            "viral_score": score, "reason": d.get("reason"),
            "spoiler": d.get("spoiler"), "nudity": bool(d.get("nudity")) or None, "censored": censored,
            # warning only: platforms may remove it for sexual content (strip club, lap dance, twerking...)
            "risky": {"why": str(d.get("risky_why") or "sexual content")[:160]}
            if d.get("risky") and rcfg.get("risky_warning", True) else None,
            "censor_regions": [list(r) for r in censor_regs] if censor_regs else None,
            "nudity_raw": _nudity_raw(d, win_start, clip_abs, raw_offset, redo),
            "flagged_by": c.source, "language": lang,
            "viewer_clips": [vc.as_dict() for vc in c.clips] or None,
            "combined_score": scoring.priority(c) or None, "score_breakdown": scoring.breakdown(c) or None,
            "recording": os.path.basename(c.rec_path),
            "start_in_recording": round(win_start + start, 2), "duration": round(clip_dur, 2),
            "files": {f: (dict((g, h) for g, h in editor.handle_groups(rcfg)).get(f[5:-4], "no handle"))
                      for f in files},
            "layout": layout, "facecam": facecam, "focus_x": round(focus_x, 3),
            "raw_clip": "raw.mp4 has 1 min before and after the clip; the clip starts at "
                        f"{raw_offset:.1f}s in raw.mp4",
            "created": now.isoformat(timespec="seconds"),
            "seconds_flag_to_clip": round(time.time() - c.flagged_wall, 1) if c.flagged_wall and not redo else None,
            # live clips: where the clip starts in real time (harvest uses it to skip moments already clipped)
            "start_wall": round(c.t0 + win_start + start, 2) if isinstance(c.t0, (int, float)) and c.t0 > 1e9 else None,
            "posted": {"tiktok": False, "youtube": False, "instagram": False, "x": False},
        }
        if redo:
            old = c.keep_meta or {}
            off_old = old.get("_offset_in_raw", 0.0)
            for k in ("created", "posted", "recording", "stream_title", "platform", "game", "flagged_by", "user_context"):
                if k in old:
                    meta[k] = old[k]
            if isinstance(old.get("start_in_recording"), (int, float)):
                meta["start_in_recording"] = round(old["start_in_recording"] - off_old + win_start + start, 2)
            raw_offset = win_start + start
            meta["raw_clip"] = f"raw.mp4 has the context around the clip; the clip starts at {raw_offset:.1f}s in raw.mp4"
            meta["old_title"] = old.get("title_bar") or old.get("heading")
            meta["redo_version"] = old.get("_redo_version")
            meta["re_edited"] = now.isoformat(timespec="seconds")
            from .redo import keep_user_edits          # posted marks / your context edited meanwhile
            keep_user_edits(os.path.join(folder, "meta.json"), meta, ("posted", "user_context"))
        campaign.apply_to_meta(meta, camp)          # required hashtags + credit, campaign name
        if campaign.looks_for(camp) and "campaign_match" in d:    # Claude's extra check: the campaign's content
            meta["campaign_match"] = bool(d.get("campaign_match"))
            meta["campaign_note"] = str(d.get("campaign_note") or "")[:300]
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        with open(os.path.join(folder, "caption.txt"), "w", encoding="utf-8") as f:
            f.write(campaign.caption_txt(meta, files, raw_offset, camp))
            if sound_meta and sound_meta.get("music"):
                f.write("\n\nNOTE: this clip uses short music clips (" + ", ".join(sound_meta["music"])
                        + "): TikTok / Instagram / YouTube may mute or demonetise it.\n")
        if redo:
            log(f"RE-EDITED ({clip_dur:.0f}s): '{meta['old_title']}' -> '{d.get('title_bar')}'", c.login)
            return folder
        self._log_csv(meta, folder)
        self.saved.setdefault(c.login, []).append(time.time())
        speed = f", {time.time() - c.flagged_wall:.0f}s after the flag" if c.flagged_wall else ""
        log(f"SAVED clip ({clip_dur:.0f}s, score {score}{speed}) -> {folder}", c.login)
        return folder

    def _facecam(self, c, start, dur):
        """Webcam position, found once per recording (it doesn't move).
        Claude looks at 3 frames and says where the real webcam is (or that there is none); the face scan
        and the webcam's own frame make the crop exact. Remembered per streamer as a last resort."""
        cache = self.__dict__.setdefault("_cams", {})
        key = c.rec_path if c.platform != "test" else c.login
        if key in cache:
            return cache[key]
        memory_path = os.path.join(self.cfg["storage"]["output_dir"], ".webcams_v2.json")
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                memory = json.load(f)
        except (OSError, ValueError):
            memory = {}
        scan_start, scan_dur = max(0.0, start - 20), dur + 40
        locate = None
        if hasattr(self.judge, "locate_webcam") and (self.judge.client is not None or self.judge.claude_code):
            def locate():
                frames = analyzer.grab_frames(c.rec_path, scan_start, scan_dur, fractions=(0.2, 0.5, 0.8), width=1280)
                return self.judge.locate_webcam([j for _, j in frames])
        cam, claude_said_none = None, False
        try:
            answer = {}
            if locate:
                real_locate = locate
                def locate():
                    answer["v"] = real_locate()
                    return answer["v"]
            cam = layout_mod.detect_facecam(c.rec_path, scan_start, scan_dur, locate=locate)
            claude_said_none = isinstance(answer.get("v"), dict) and answer["v"].get("found") is False
        except Exception as e:
            log(f"webcam detection failed ({e})", c.login)
        if not cam and not claude_said_none and memory.get(c.login):
            cam = dict(memory[c.login], how="remembered from an earlier stream")
        if cam:
            log(f"webcam found ({cam.get('how', '')})", c.login)
            cam = {k: v for k, v in cam.items() if k in ("x", "y", "w", "h", "how")}
            if "remembered" not in cam.get("how", ""):
                memory[c.login] = cam
                try:
                    os.makedirs(os.path.dirname(memory_path), exist_ok=True)
                    with open(memory_path, "w", encoding="utf-8") as f:
                        json.dump(memory, f, indent=1)
                except OSError:
                    pass
        else:
            log("no webcam on this stream" + (" (Claude checked)" if claude_said_none else "")
                + ": using full-screen gameplay layout", c.login)
        cache[key] = cam
        return cam

    def _fix_bounds(self, start, end, win_dur, mn=None, mx=None):
        ccfg = self.cfg["clips"]
        mn = ccfg["min_length"] if mn is None else mn
        mx = ccfg["max_length"] if mx is None else mx
        start = max(0.0, min(start, win_dur - 1))
        end = max(start + 1, min(end, win_dur))
        if end - start > mx:
            end = start + mx
        if end - start < mn:
            end = min(win_dur, start + mn)
            start = max(0.0, end - mn)
        return start, end

    def _log_csv(self, meta, folder):
        path = os.path.join(self.cfg["storage"]["output_dir"], "clips_log.csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _csv_lock:
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["created", "platform", "streamer", "score", "duration", "title", "folder"])
                w.writerow([meta["created"], meta["platform"], meta["streamer"], meta["viral_score"],
                            meta["duration"], meta["title"], folder])
