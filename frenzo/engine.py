"""Live mode: watches streamers on Twitch, Kick and YouTube, records them and queues hype moments."""
import datetime as dt
import json
import dataclasses
import os
import shutil
import tempfile
import threading
import time

from . import campaign, crowd, scoring
from .ai import ClipJudge
from .analyzer import Transcriber
from .detector import BUG, GAMEPLAY, NPC, SPICY, LoudnessDetector, SpikeDetector, SusDetector
from .chaos import ChaosDetector
from .hottake import HotTakeDetector
from .learning import detection_for, hot_takes_for, keyword_pattern
from .voice import VoiceReactionDetector
from .moments import MomentQueue
from .pipeline import Candidate, Pipeline
from .platforms import PLATFORMS
from .recorder import Recorder
from .util import log, require_binary


class Session(threading.Thread):
    def __init__(self, scfg, info, platform, cfg, work_q):
        super().__init__(daemon=True)
        self.scfg, self.info, self.platform, self.cfg, self.work_q = scfg, info, platform, cfg, work_q
        self.tag = scfg["key"]
        self.recorder = Recorder(info.login, info.url, cfg["storage"]["recordings_dir"],
                                 cfg["storage"]["quality"], info.platform,
                                 segment_minutes=cfg["storage"].get("segment_minutes", 60),
                                 display=info.display, title=info.title, game=info.game)
        self.refs = {}              # recording part -> moments still being clipped from it
        self.chat = None
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self.pending = 0
        self.closed = False
        self.recent = []
        self.started = time.time()
        self.flagged = 0
        self.last_moment = ""
        self.det = detection_for(cfg, scfg)
        self.watcher = None         # viewer clips (Twitch keys needed)
        self.crowd_moments = []     # moments with viewer clips, until their lessons are saved

    def stop(self):
        self._halt.set()

    def run(self):
        # global detection settings + what was learned for this streamer (learned.json, read at start)
        det = self.det
        hot_on = hot_takes_for(self.scfg)
        learned = self.cfg.get("learned") or {}
        chat_kw = keyword_pattern(learned.get("chat_keywords") or [])
        speech_kw = keyword_pattern(learned.get("speech_keywords") or [])
        if self.scfg.get("learned"):
            log("using learned settings: " + ", ".join(f"{k} {v}" for k, v in self.scfg["learned"].items()), self.tag)
        self.recorder.start()
        try:
            self.chat = self.platform.chat(self.info, self.scfg)
        except Exception as e:
            log(f"chat unavailable ({e})", self.tag)
        if self.chat:
            self.chat.start()
        spikes = SpikeDetector(self.chat, det) if self.chat else None
        sus = SusDetector(self.chat, det) if self.chat else None
        spicy = SusDetector(self.chat, det, pattern=SPICY) if self.chat else None
        bugchat = SusDetector(self.chat, det, pattern=BUG, min_hits=4) if self.chat else None
        npcchat = SusDetector(self.chat, det, pattern=NPC, min_hits=4) if self.chat else None
        playchat = SusDetector(self.chat, det, pattern=GAMEPLAY, min_hits=5) if self.chat else None
        learnchat = SusDetector(self.chat, det, pattern=chat_kw, min_hits=4) if self.chat and chat_kw else None
        # campaign: EXTRA searches for the content this streamer's campaign wants (the ones above stay as they are)
        camp = campaign.of(self.scfg)
        camp_chat, camp_speech = campaign.chat_pattern(camp), campaign.speech_pattern(camp)
        campchat = SusDetector(self.chat, det, pattern=camp_chat, min_hits=3) if self.chat and camp_chat else None

        def camp_listener():
            return HotTakeDetector(self.recorder, self.cfg, only=camp_speech) if camp_speech is not None else None
        campsp = camp_listener()
        if campchat or campsp:
            log("also looking for the campaign's content: " + campaign.looks_for(camp)[:150], self.tag)
        mode = det["loudness_triggers"]

        def rec_detectors():
            """Detectors that read the recording itself; rebuilt when the engine moves to a new hourly part."""
            return (ChaosDetector(self.recorder, det) if det.get("chaos_triggers", True) else None,
                    HotTakeDetector(self.recorder, self.cfg, learned=speech_kw) if hot_on else None,
                    LoudnessDetector(self.recorder, det) if (mode == "always" or (mode == "when_no_chat" and not self.chat)) else None,
                    VoiceReactionDetector(self.recorder, det) if det["voice_triggers"] else None)
        chaos, hot, loud, voice = rec_detectors()
        gen = self.recorder.gen
        if hot:
            log("speech listener on (hot takes + NPC moments)", self.tag)
        last_chat, last_save = time.time(), time.time()
        started_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        seen, last_poll = set(), time.time()
        if det["use_twitch_clips"] and det.get("clip_evidence", True) and hasattr(self.platform, "clips_raw") \
                and self.info.user_id:
            self.watcher = ClipWatcher(self, det)
            log("viewer clips: watching them as evidence (moments stay open 5 min, exact position by sound)", self.tag)
        try:
            while not self._halt.wait(5):
                if not self.recorder.alive():
                    log("recording stopped (stream ended or connection lost)", self.tag)
                    break
                if self.recorder.t0 is None:
                    continue
                if self.recorder.gen != gen:            # moved to the next hourly part
                    gen = self.recorder.gen
                    chaos, hot, loud, voice = rec_detectors()
                    campsp = camp_listener()
                now = time.time()
                if self.chat:                           # chat activity, for the Recordings tab
                    self.recorder.add_chat(now, self.chat.count_between(last_chat, now))
                    last_chat = now
                if now - last_save > 30:
                    self.recorder.save_all()
                    last_save = now
                if spikes:
                    hit = spikes.check()
                    if hit:
                        self._queue(hit[0], f"chat spike: {hit[1]:.1f}x faster than normal")
                if sus:
                    hit = sus.check()
                    if hit:
                        self._queue(hit[0], f"chat thinks it's sus: {hit[1]} sus/ayo messages")
                if spicy:
                    hit = spicy.check()
                    if hit:
                        self._queue(hit[0], f"chat is thirsty (spicy moment): {hit[1]} 🥵/gyat messages")
                if bugchat:
                    hit = bugchat.check()
                    if hit:
                        self._queue(hit[0], f"chat says bug/glitch/physics: {hit[1]} messages")
                if npcchat:
                    hit = npcchat.check()
                    if hit:
                        self._queue(hit[0], f"chat reacting to an NPC / something weird: {hit[1]} messages")
                if playchat:
                    hit = playchat.check()
                    if hit:
                        self._queue(hit[0], f"chat hyped by the gameplay (chase/stunt/graphics): {hit[1]} messages")
                if learnchat:
                    hit = learnchat.check()
                    if hit:
                        self._queue(hit[0], f"chat said something the owner's clips often had: {hit[1]} messages")
                if campchat:
                    hit = campchat.check()
                    if hit:
                        self._queue(hit[0], f"campaign content: chat talked about what the campaign wants: {hit[1]} messages")
                if campsp:
                    hit = campsp.check(time.time())
                    if hit:
                        self._queue(hit[0], f"campaign content: streamer said \"{hit[1][:90]}\"")
                if chaos:
                    hit = chaos.check(time.time())
                    if hit:
                        self._queue(hit[0], f"gameplay chaos: picture went {hit[1]:.1f}x wilder than normal (bug/physics?)")
                if hot:
                    hit = hot.check(time.time())
                    if hit:
                        if hit[2] == "npc":
                            self._queue(hit[0], f"streamer talking about an NPC / something weird: \"{hit[1][:90]}\"")
                        elif hit[2] == "learned":
                            self._queue(hit[0], f"streamer said something the owner's clips often had: \"{hit[1][:90]}\"")
                        else:
                            self._queue(hit[0], f"hot take: \"{hit[1][:90]}\"")
                if voice:
                    hit = voice.check(time.time())
                    if hit:
                        self._queue(hit[0], f"streamer reaction: {hit[1]}")
                if loud:
                    hit = loud.check()
                    if hit:
                        self._queue(hit[0], f"sudden loudness jump of {hit[1]:.0f} dB")
                if self.watcher:
                    self.watcher.tick(time.time())
                elif det["use_twitch_clips"] and time.time() - last_poll > det["clip_poll_seconds"]:
                    last_poll = time.time()
                    try:
                        for cid, created, title in self.platform.new_clips(self.info, started_iso):
                            if cid in seen:
                                continue
                            seen.add(cid)
                            if created > self.recorder.t0 + 20:
                                self._queue(created, f"a viewer made a clip titled '{title}'")
                    except Exception as e:
                        log(f"could not check viewer clips ({e})", self.tag)
        finally:
            self.recorder.stop()
            if self.chat:
                self.chat.stop()
            with self._lock:
                self.closed = True
            self._cleanup()

    def _queue(self, anchor_wall, source, clip=None, part=None, span=None):
        if clip is None:
            if self.watcher:
                self.watcher.boost()            # something happened: look for viewer clips more often for a while
            for a, cand in self.recent:
                if abs(anchor_wall - a) < 30:
                    if source.split(":")[0] not in cand.source:
                        cand.source += f" + {source}"   # several signals at once = stronger moment
                        cand.priority = scoring.priority(cand)
                        log(f"same moment also -> {source}", self.tag)
                    return
        # RUNNING GAG: 2+ other moments in the last 3 minutes -> one long window covering all of them,
        # so Claude can stitch the best beats; the shorter pending ones it covers are dropped
        ccfg = self.cfg["clips"]
        gag_span = ccfg.get("gag_window", 180)
        earlier = [(a, c) for a, c in self.recent if anchor_wall - gag_span < a < anchor_wall and not c.superseded]
        before, after, gag_note = None, None, ""
        if len(earlier) >= 2 and ccfg.get("running_gags", True) and clip is None:
            before = min(gag_span, anchor_wall - min(a for a, _ in earlier) + 15)
            after = ccfg["context_after"] + 5
            for a, c in earlier:
                if not c.started:
                    c.superseded = True
            gag_note = f" [running gag: {len(earlier) + 1} beats in the last {before:.0f}s: " + \
                "; ".join(c.source.split(" [")[0][:60] for _, c in earlier) + "]"
        with self._lock:
            self.pending += 1
        self.flagged += 1
        self.last_moment = source.split(" [")[0][:120]
        log(f"moment flagged -> {source}" + (" (running gag: long window)" if gag_note else ""), self.tag)
        rec_path, rec_t0 = (part.path, part.t0) if part is not None else (self.recorder.path, self.recorder.t0)
        with self._lock:
            self.refs[rec_path] = self.refs.get(rec_path, 0) + 1
        self.recorder.add_moment(rec_path, anchor_wall - rec_t0, source.split(" [")[0])
        cand = Candidate(
            login=self.info.login, display_name=self.info.display, rec_path=rec_path,
            anchor=anchor_wall - rec_t0, source=source + gag_note,
            context_before=before, context_after=after,
            # wait until the raw version's extra minute after the clip is recorded too
            ready_at=anchor_wall + self.cfg["clips"]["context_after"] + self.cfg["render"].get("raw_padding", 60) + 8,
            chat=self.chat, t0=rec_t0, streamer_cfg=self.scfg,
            game=self.info.game or self.cfg["game"]["name"], platform=self.info.platform,
            stream_title=self.info.title, flagged_wall=time.time(),
        )
        done_cb = lambda p=rec_path: self._done(p)

        def on_done(c=cand):
            if getattr(c, "late_clip", False) and not c.folder:    # rejected while viewer clips arrived
                self._rejudge(c)
            done_cb()
        cand.on_done = on_done
        if clip is not None:
            cand.clips, cand.clip_span = [clip], span
            if crowd.strong(crowd.ClipMoment(0, 0, [clip]), self.cfg):
                cand.keep_bonus = int(self.det.get("clip_keep_bonus", 1))
            self._track(cand)
        cand.priority = scoring.priority(cand)
        self.recent = [(a, c) for a, c in self.recent if a > anchor_wall - 600] + [(anchor_wall, cand)]
        self.work_q.put(cand)

    # ------------------------------------------------------------------ viewer clips (crowd evidence)
    def add_clip(self, vc, part):
        """A viewer clip placed on our recording (vc.span = wall times): attach it to an open moment whose
        window it overlaps, or make a new moment from it. Clip evidence ignores the cooldowns."""
        open_for = float(self.det.get("clip_open_minutes", 5)) * 60
        now = time.time()
        target = None
        with self._lock:
            for a, cand in reversed(self.recent):
                if now - (cand.flagged_wall or a) > open_for or cand.superseded:
                    continue
                before = cand.context_before if cand.context_before is not None else self.cfg["clips"]["context_before"]
                after = cand.context_after if cand.context_after is not None else self.cfg["clips"]["context_after"]
                if vc.span[0] <= a + after and vc.span[1] >= a - before:
                    target = cand
                    break
        if target is not None:
            self._attach(target, vc)
            return
        a, b = vc.span[0] - part.t0, vc.span[1] - part.t0
        self._queue(vc.span[1] - 2, f"viewer clip: '{vc.title[:60]}'" + (" (featured)" if vc.featured else ""),
                    clip=vc, part=part, span=(a, b))

    def _attach(self, cand, vc):
        if any(c.id == vc.id for c in cand.clips):
            return
        cand.clips.append(vc)
        if cand not in self.crowd_moments:
            self._track(cand)
        m = crowd.ClipMoment(0, 0, cand.clips)
        if not cand.started and vc.span and cand.t0:
            a, b = vc.span[0] - cand.t0, vc.span[1] - cand.t0
            cand.clip_span = (min(a, cand.clip_span[0]), max(b, cand.clip_span[1])) if cand.clip_span else (a, b)
        if crowd.strong(m, self.cfg):
            cand.keep_bonus = int(self.det.get("clip_keep_bonus", 1))
        if "viewer clip" not in cand.source:
            cand.source += " + viewer clips"
        cand.priority = scoring.priority(cand)
        log(f"viewer clip lands on a moment: {m.clippers} clipper(s){', featured' if m.featured else ''}"
            f" - '{vc.title[:50]}'", self.tag)
        if cand.done and not cand.folder and not cand.manual:
            self._rejudge(cand)
        elif cand.started and not cand.done:
            cand.late_clip = True           # being judged right now: looked at again when it's finished

    def _rejudge(self, cand):
        """Claude rejected this moment, then viewers clipped it: judge it again with the clip evidence."""
        if getattr(cand, "rejudged", False) or cand.status not in ("rejected", "") or not os.path.exists(cand.rec_path):
            return
        cand.rejudged = True
        log("Claude passed on this moment but viewers clipped it: judging it again with the clip evidence", self.tag)
        with self._lock:
            self.pending += 1
            self.refs[cand.rec_path] = self.refs.get(cand.rec_path, 0) + 1
        again = dataclasses.replace(cand, started=False, done=False, judged=None, folder=None, status="",
                                    learn_log=None, superseded=False, ready_at=time.time(),
                                    source=cand.source + " [judged again: viewers clipped it]",
                                    on_done=lambda p=cand.rec_path: self._done(p))
        again.rejudged = True
        cand.second = again
        self._track(again)
        self.work_q.put(again)

    def clip_views(self, vc):
        """Fresh view count for a clip: a clip gaining views fast moves its moment to the front of the queue."""
        fast = float(self.det.get("clip_fast_views_per_min", 30))
        for cand in list(self.crowd_moments):
            if cand.started or not any(c.id == vc.id for c in cand.clips):
                continue
            v = crowd.velocity(vc.views_hist)
            cand.priority = scoring.priority(cand) + (5.0 if v >= fast else 0.0)
            if v >= fast:
                log(f"viewer clip gaining {v:.0f} views/min: its moment moves to the front", self.tag)

    def _track(self, cand):
        with self._lock:
            if cand not in self.crowd_moments:
                self.crowd_moments.append(cand)

    def crowd_lessons(self, final):
        """After the last view check, strong viewer clips become lessons: 'missed' when Frenzo made no clip,
        'confirmed' when it did. final(clip_id) says whether a clip's view checks are finished."""
        if not self.det.get("clip_lessons", True):
            return
        from .learning import add_example
        keep = []
        for cand in list(self.crowd_moments):
            second = getattr(cand, "second", None)
            if getattr(cand, "lesson_done", False) or getattr(cand, "rejudged", False) and second is None and \
                    cand.source.endswith("viewers clipped it]"):
                continue                        # second judgings are counted through the first one
            if not cand.clips or not cand.done or (second is not None and not second.done) or \
                    not all(final(c.id) for c in cand.clips):
                keep.append(cand)
                continue
            cand.lesson_done = True
            m = crowd.ClipMoment(0, 0, cand.clips)
            if not crowd.strong(m, self.cfg):
                continue
            folder = cand.folder or (second.folder if second is not None else None)
            d = (second.judged if second is not None and second.judged else cand.judged) or {}
            data = {"login": self.info.login, "platform": self.info.platform, "source": "viewer_clips",
                    "recording": os.path.splitext(os.path.basename(cand.rec_path))[0],
                    "sec": round(cand.anchor, 1), "clippers": m.clippers, "featured": m.featured, "views": m.views,
                    "viewer_titles": m.titles[:5], "flagged_by": cand.source[:300],
                    "engine_score": d.get("viral_score"), "engine_reason": str(d.get("reason") or "")[:200],
                    "what_happens": str(d.get("what_happens") or "")[:300]}
            try:
                if folder:
                    add_example(self.cfg, "confirmed", dict(data, clip=os.path.basename(folder)))
                    log(f"lesson: viewers confirmed a Frenzo pick ({m.clippers} clippers)", self.tag)
                else:
                    data["verdict"] = ("never flagged before the viewer clip" if cand.source.startswith("viewer clip")
                                       else f"flagged but scored {d.get('viral_score')}" if d.get("viral_score") is not None
                                       else "flagged, not judged")
                    add_example(self.cfg, "missed", data)
                    log(f"lesson: viewers clipped a moment Frenzo didn't keep ({m.clippers} clippers)", self.tag)
            except Exception as e:
                log(f"couldn't save the lesson ({str(e)[:80]})", self.tag)
        with self._lock:
            self.crowd_moments = keep[-200:]

    def _done(self, path=None):
        with self._lock:
            self.pending -= 1
            if path in self.refs:
                self.refs[path] -= 1
                if self.refs[path] <= 0:
                    del self.refs[path]
        self._cleanup()

    def in_use(self):
        """Recording files the engine still writes or reads (the app must not convert or delete them)."""
        with self._lock:
            busy = set(self.refs)
        if not self.closed:
            busy |= {p.path for p in [self.recorder.active, self.recorder.next] + self.recorder.retiring if p}
        return busy

    def _cleanup(self):
        # recordings are kept: the app's Recordings tab shows them (convert, browse, cut, merge, delete)
        with self._lock:
            if not (self.closed and self.pending <= 0):
                return
            self.pending = 10 ** 9  # only once



class ClipWatcher:
    """Viewer Twitch clips as evidence for one live session (its Helix calls run in a background thread).

    - polls every clip_poll_seconds (90 s), every clip_poll_fast_seconds (25 s) for 5 min after any other signal;
    - each new clip is placed on our recording (estimate, then exact by matching its sound) and handed to the
      session, which attaches it to an open moment or makes a new moment from it;
    - known clips are re-checked at about 5, 15 and 30 minutes (100 ids per call): fast-growing clips move
      their moment to the front of the editing queue; after the last check strong clips become lessons."""
    CHECKS = (300, 900, 1800)

    def __init__(self, sess, det):
        self.s, self.det = sess, det
        self.base = float(det.get("clip_poll_seconds", 90))
        self.fast = float(det.get("clip_poll_fast_seconds", 25))
        self.fast_until, self.last, self.busy = 0.0, 0.0, False
        self.clips = {}             # id -> ViewerClip
        self.checked = {}           # id -> how many re-checks done
        self.bots = det.get("clip_bots") or crowd.DEFAULT_BOTS
        self.delay = float(det.get("clip_delay_seconds", 6))
        self.started = time.time()

    def boost(self):
        self.fast_until = time.time() + float(self.det.get("clip_fast_minutes", 5)) * 60

    def tick(self, now):
        every = self.fast if now < self.fast_until else self.base
        if self.busy or now - self.last < every:
            return
        self.last, self.busy = now, True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self._poll()
            self._recheck()
        except Exception as e:
            log(f"could not check viewer clips ({str(e)[:120]})", self.s.tag)
        finally:
            self.busy = False

    def _poll(self):
        since = crowd.iso(time.time() - float(self.det.get("clip_lookback_minutes", 10)) * 60)
        for raw in self.s.platform.clips_raw(self.s.info, since):
            vc = crowd.from_helix(raw)
            if vc.id in self.clips:
                continue
            self.clips[vc.id] = vc
            if crowd.ignored(vc, self.bots, self.s.info.user_id):
                continue
            if vc.created < self.started + 20:
                continue                    # made before we started recording
            try:
                self._place(vc)
            except Exception as e:
                log(f"viewer clip '{vc.title[:40]}' couldn't be placed ({str(e)[:100]})", self.s.tag)

    def _place(self, vc):
        wa, wb = crowd.live_span(vc, self.delay)
        lead = float(self.det.get("clip_lead_in", 25))
        rec = self.s.recorder
        part = rec.part_for(wa - lead, wb) or rec.part_for(wa, wb)
        if not part:
            log(f"viewer clip '{vc.title[:40]}' is outside our recording - skipped", self.s.tag)
            return
        if self.det.get("clip_align", True):
            work = tempfile.mkdtemp(prefix="frenzo_clip_")
            try:
                path = crowd.download_clip(vc.url, work)
                found = crowd.locate(path, part.path, wa - part.t0, search=120, min_len=5.0, piece_max=90)
                if found is not None:
                    wa, wb, vc.aligned = part.t0 + found, part.t0 + found + vc.duration, True
            except Exception as e:
                log(f"couldn't match the clip's sound ({str(e)[:80]}): using the estimated time", self.s.tag)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        vc.span = (wa, wb)
        self.s.add_clip(vc, part)

    def _recheck(self):
        now = time.time()
        due = [cid for cid, vc in self.clips.items() if vc.span and self.checked.get(cid, 0) < len(self.CHECKS)
               and now - vc.created >= self.CHECKS[self.checked.get(cid, 0)]]
        if due:
            for raw in self.s.platform.clips_by_ids(due):
                vc = self.clips.get(str(raw.get("id")))
                if vc:
                    vc.views = int(raw.get("view_count") or vc.views)
                    vc.featured = vc.featured or bool(raw.get("is_featured"))
                    vc.views_hist.append((now, vc.views))
            for cid in due:
                self.checked[cid] = self.checked.get(cid, 0) + 1
                self.s.clip_views(self.clips[cid])

        def final(cid):
            vc = self.clips.get(cid)
            return vc is None or self.checked.get(cid, 0) >= len(self.CHECKS) or now - vc.created > self.CHECKS[-1] + 600
        self.s.crowd_lessons(final)

_closing = []      # ended sessions whose moments are still being clipped


def _write_status(cfg, sessions, by_platform, work_q, state="running"):
    """Small status file the Frenzo app reads (who is being recorded right now)."""
    path = os.path.join(cfg["storage"]["output_dir"], ".live_status.json")
    rec = {}
    for key, sess in sessions.items():
        info = sess.info
        rec[key] = {"title": info.title, "game": info.game, "platform": info.platform, "url": info.url,
                    "display": info.display, "since": sess.started, "flagged": sess.flagged,
                    "pending": max(0, sess.pending) if sess.pending < 10 ** 8 else 0,
                    "last_moment": sess.last_moment}
    in_use = set()
    for sess in list(sessions.values()) + list(_closing):
        in_use |= sess.in_use()
    data = {"state": state, "updated": time.time(), "pid": os.getpid(),
            "watching": [s["key"] for g in by_platform.values() for s in g],
            "recording": rec, "queued": work_q.qsize(), "in_use": sorted(in_use)}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def stop_leftover_recordings(rec_dir):
    """Only one live engine runs at a time: any streamlink still writing into the recordings folder when it starts
    was left behind by an earlier session (it would keep filling the disk). Windows only."""
    if os.name != "nt":
        return 0
    import subprocess
    from .util import NO_WINDOW
    folder = os.path.normcase(os.path.abspath(rec_dir))
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match "
          "'-m streamlink' } | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=60, creationflags=NO_WINDOW).stdout
    except Exception:
        return 0
    killed = 0
    for line in out.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit() and folder in os.path.normcase(cmd) and int(pid) != os.getpid():
            subprocess.run(["taskkill", "/T", "/F", "/PID", pid.strip()], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            killed += 1
    return killed


def run_live(cfg):
    require_binary("ffmpeg")
    try:
        import streamlink  # noqa: F401
    except ImportError:
        raise SystemExit("ERROR: streamlink is not installed. Run: pip install -r requirements.txt")
    streamers = cfg["streamers"]
    if not streamers:
        raise SystemExit("ERROR: add at least one streamer to the 'streamers' list in config.yaml.")
    n = stop_leftover_recordings(cfg["storage"]["recordings_dir"])
    if n:
        log(f"stopped {n} recording(s) left running by an earlier session")

    by_platform = {}
    for s in streamers:
        by_platform.setdefault(s["platform"], []).append(s)
    platforms = {}
    for name, group in by_platform.items():
        platforms[name] = PLATFORMS[name](cfg)
        platforms[name].setup(group)

    pipeline = Pipeline(cfg, Transcriber(cfg["transcription"]), ClipJudge(cfg))
    work_q = MomentQueue(best_first=(cfg.get("queue") or {}).get("best_first", True))

    def worker():
        while True:
            pipeline.process(work_q.get())

    for _ in range(max(1, int(cfg.get("workers", 1)))):
        threading.Thread(target=worker, daemon=True).start()

    summary = ", ".join(f"{len(g)} on {p}" for p, g in by_platform.items())
    log(f"watching {summary} for '{cfg['game']['name']}'. Press Ctrl+C to stop.")
    sessions, last_status = {}, None
    stop_file = os.environ.get("FRENZO_STOP_FILE")
    if stop_file and os.path.exists(stop_file):
        os.remove(stop_file)
    try:
        while True:
            for name, group in by_platform.items():
                try:
                    live = platforms[name].check(group)
                except Exception as e:
                    log(f"{name} check failed ({e}); will retry")
                    continue
                for s in group:
                    info = live.get(s["login"])
                    key = s["key"]
                    if info and key not in sessions:
                        log(f"LIVE: {info.title[:70] or info.game}", key)
                        sess = Session(s, info, platforms[name], cfg, work_q)
                        sessions[key] = sess
                        sess.start()
                    elif not info and key in sessions:
                        log("offline or not playing the game - stopping", key)
                        gone = sessions.pop(key)
                        gone.stop()
                        _closing.append(gone)
            for key, sess in list(sessions.items()):
                if not sess.is_alive():
                    _closing.append(sessions.pop(key))
            status = sorted(sessions)
            if status != last_status:
                log("recording now: " + (", ".join(status) or "nobody (waiting for streams)"))
                last_status = status
            # wait until the next live check, keeping the app's status fresh and listening for "stop"
            wait_until = time.time() + cfg["poll_seconds"]
            while time.time() < wait_until:
                for key, sess in list(sessions.items()):
                    if not sess.is_alive():
                        _closing.append(sessions.pop(key))
                _closing[:] = [x for x in _closing if x.is_alive() or 0 < x.pending < 10 ** 8]
                _write_status(cfg, sessions, by_platform, work_q)
                if stop_file and os.path.exists(stop_file):
                    raise KeyboardInterrupt
                time.sleep(3)
    except KeyboardInterrupt:
        log("stopping... (finishing recordings)")
        _write_status(cfg, sessions, by_platform, work_q, state="stopping")
        for sess in sessions.values():
            sess.stop()
        for sess in sessions.values():
            sess.join(timeout=15)
        for sess in list(sessions.values()) + list(_closing):   # make sure no recording keeps running on its own
            try:
                sess.recorder.stop()
            except Exception:
                pass
        log(f"queued moments not processed: {work_q.qsize()}")
    finally:
        _write_status(cfg, {}, by_platform, work_q, state="stopped")
        if stop_file and os.path.exists(stop_file):
            try:
                os.remove(stop_file)
            except OSError:
                pass
