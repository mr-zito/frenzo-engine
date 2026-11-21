"""Live mode: watches streamers on Twitch, Kick and YouTube, records them and queues hype moments."""
import datetime as dt
import json
import os
import queue
import threading
import time

from . import campaign
from .ai import ClipJudge
from .analyzer import Transcriber
from .detector import BUG, GAMEPLAY, NPC, SPICY, LoudnessDetector, SpikeDetector, SusDetector
from .chaos import ChaosDetector
from .hottake import HotTakeDetector
from .learning import detection_for, hot_takes_for, keyword_pattern
from .voice import VoiceReactionDetector
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

    def stop(self):
        self._halt.set()

    def run(self):
        # global detection settings + what was learned for this streamer (learned.json, read at start)
        det = detection_for(self.cfg, self.scfg)
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
                if det["use_twitch_clips"] and time.time() - last_poll > det["clip_poll_seconds"]:
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

    def _queue(self, anchor_wall, source):
        for a, cand in self.recent:
            if abs(anchor_wall - a) < 30:
                if source.split(":")[0] not in cand.source:
                    cand.source += f" + {source}"   # several signals at once = stronger moment
                    log(f"same moment also -> {source}", self.tag)
                return
        # RUNNING GAG: 2+ other moments in the last 3 minutes -> one long window covering all of them,
        # so Claude can stitch the best beats; the shorter pending ones it covers are dropped
        ccfg = self.cfg["clips"]
        gag_span = ccfg.get("gag_window", 180)
        earlier = [(a, c) for a, c in self.recent if anchor_wall - gag_span < a < anchor_wall and not c.superseded]
        before, after, gag_note = None, None, ""
        if len(earlier) >= 2 and ccfg.get("running_gags", True):
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
        rec_path, rec_t0 = self.recorder.path, self.recorder.t0
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
            stream_title=self.info.title, on_done=lambda p=rec_path: self._done(p),
        )
        self.recent = [(a, c) for a, c in self.recent if a > anchor_wall - 600] + [(anchor_wall, cand)]
        self.work_q.put(cand)

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
    work_q = queue.Queue()

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
