"""Records a live stream with streamlink, split into hourly parts.

Every `segment_minutes` a new part starts (a second streamlink, so nothing is lost). The engine moves
to the new part once it has a few minutes of video (so every moment still has its context in one
file), and the old part keeps recording a little longer, then stops. Parts overlap by a few minutes;
merging two parts in the app removes the overlap.

Next to each part, a small .json file keeps what the Frenzo app shows in its Recordings tab: when it
started, the chat activity, and the moments the engine flagged in it.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .util import log

BUCKET = 5          # seconds per chat-activity bucket


class _Part:
    def __init__(self, rec, index):
        self.rec = rec
        self.index = index
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.stem = f"{rec.platform}_{rec.login}_{stamp}"
        self.path = str(Path(rec.dir) / f"{self.stem}.ts")
        self.side = str(Path(rec.dir) / f"{self.stem}.json")
        self.proc = None
        self.logf = None
        self.t0 = None              # wall time of the first video bytes
        self.ended = None
        self.chat = []              # messages per BUCKET seconds, from t0
        self.moments = []           # [{"t": seconds in this part, "why": ...}]
        self.lock = threading.Lock()

    def start(self):
        r = self.rec
        cmd = [sys.executable, "-m", "streamlink", "--retry-open", "3", "--stream-segment-threads", "2",
               "-o", self.path, r.url, r.quality]
        if r.platform == "twitch":
            cmd.insert(3, "--twitch-low-latency")
        self.logf = open(self.path + ".log", "w", encoding="utf-8")
        self.proc = subprocess.Popen(cmd, stdout=self.logf, stderr=subprocess.STDOUT)
        threading.Thread(target=self._wait_first_bytes, daemon=True).start()

    def _wait_first_bytes(self):
        while self.proc and self.proc.poll() is None:
            if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
                self.t0 = time.time()
                self.rec._on_first_bytes(self)
                self.save()
                return
            time.sleep(0.25)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":
                # the venv's python.exe starts the real streamlink as a CHILD process: end both, or the child
                # keeps recording on its own after Frenzo stops (it filled the disk with orphan recordings)
                from .util import NO_WINDOW
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.logf:
            try:
                self.logf.close()
            except OSError:
                pass
        if self.ended is None:
            self.ended = time.time()
        if self.t0 is None:                     # never got any video: nothing to keep
            for p in (self.path, self.path + ".log", self.side):
                try:
                    os.remove(p)
                except OSError:
                    pass
        else:
            self.save()

    def add_chat(self, wall, n):
        if self.t0 is None or wall < self.t0 or (self.ended and wall > self.ended):
            return
        i = int((wall - self.t0) // BUCKET)
        with self.lock:
            if i >= len(self.chat):
                self.chat.extend([0] * (i + 1 - len(self.chat)))
            self.chat[i] += n

    def save(self):
        r = self.rec
        with self.lock:
            data = {"stem": self.stem, "file": os.path.basename(self.path), "platform": r.platform,
                    "login": r.login, "display": r.display, "title": r.title, "game": r.game,
                    "session": r.session, "part": self.index, "t0": self.t0, "ended": self.ended,
                    "chat": {"bucket": BUCKET, "counts": list(self.chat)}, "moments": list(self.moments)}
        try:
            old = {}
            if os.path.exists(self.side):
                with open(self.side, "r", encoding="utf-8") as f:
                    old = json.load(f)
            old.update(data)            # keep what the app added (duration, loudness...)
            tmp = self.side + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(old, f)
            os.replace(tmp, self.side)
        except (OSError, ValueError):
            pass


class Recorder:
    def __init__(self, login, url, recordings_dir, quality, platform="twitch", segment_minutes=60,
                 display=None, title="", game="", on_retired=None):
        self.login, self.url, self.platform, self.quality = login, url, platform, quality
        self.dir = str(recordings_dir)
        Path(self.dir).mkdir(parents=True, exist_ok=True)
        self.display, self.title, self.game = display or login, title, game
        self.session = f"{platform}_{login}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.segment = max(0, float(segment_minutes or 0)) * 60    # 0 = one file for the whole stream
        self.switch_after = 300     # new part becomes the engine's part after 5 min (moments keep their context)
        self.linger = 150           # old part keeps recording 2.5 min more (moments near the switch keep theirs)
        self.on_retired = on_retired
        self.lock = threading.Lock()
        self.active = _Part(self, 0)
        self.next = None
        self.retiring = []
        self.done = []
        self.path = self.active.path
        self.t0 = None              # the engine's current part: path + t0 always change together
        self.gen = 0                # +1 every time the engine moves to a new part
        self._halt = threading.Event()
        self._next_try = 0.0

    # ---- engine-facing API (same as before) ----
    def start(self):
        self.active.start()
        log(f"recording -> {os.path.basename(self.active.path)}" +
            (f" (new part every {self.segment / 60:.0f} min)" if self.segment else ""), f"{self.platform}:{self.login}")
        if self.segment:
            threading.Thread(target=self._rotate_loop, daemon=True).start()

    def alive(self):
        return self.active is not None and self.active.alive()

    def recorded_seconds(self):
        return 0.0 if self.t0 is None else time.time() - self.t0

    def stop(self):
        self._halt.set()
        for p in [self.next] + self.retiring + [self.active]:
            if p:
                p.stop()
        self.done += [p for p in self.retiring + [self.active] if p and p.t0]
        self.retiring = []
        self.next = None

    def delete_files(self):
        for part in self.all_parts():
            for p in (part.path, part.path + ".log", part.side):
                try:
                    os.remove(p)
                except OSError:
                    pass

    # ---- hourly parts ----
    def all_parts(self):
        return [p for p in self.done + self.retiring + [self.active, self.next] if p]

    def part_paths(self):
        return [p.path for p in self.all_parts()]

    def _on_first_bytes(self, part):
        with self.lock:
            if part is self.active and self.t0 is None:
                self.path, self.t0 = part.path, part.t0

    def add_chat(self, wall, n):
        for p in [self.active, self.next] + self.retiring:
            if p:
                p.add_chat(wall, n)

    def add_moment(self, path, sec, why):
        for p in self.all_parts():
            if p.path == path:
                with p.lock:
                    p.moments.append({"t": round(sec, 1), "why": why[:200]})
                p.save()
                return

    def save_all(self):
        for p in [self.active, self.next] + self.retiring:
            if p and p.t0:
                p.save()

    def _rotate_loop(self):
        while not self._halt.wait(5):
            now = time.time()
            act = self.active
            if act.t0 and act.alive() and self.next is None and now >= self._next_try \
                    and now - act.t0 >= self.segment:
                self.next = _Part(self, act.index + 1)
                self.next.start()
                log(f"starting part {act.index + 2} -> {os.path.basename(self.next.path)}", f"{self.platform}:{self.login}")
            nx = self.next
            if nx:
                if not nx.alive():
                    log("the new part couldn't start, trying again in 2 min", f"{self.platform}:{self.login}")
                    nx.stop()
                    self.next, self._next_try = None, now + 120
                elif nx.t0 and now - nx.t0 >= self.switch_after:
                    with self.lock:
                        self.active, self.next = nx, None
                        self.path, self.t0 = nx.path, nx.t0
                        self.gen += 1
                    act.retire_at = now + self.linger
                    self.retiring.append(act)
            for old in list(self.retiring):
                if now >= getattr(old, "retire_at", 0):
                    old.stop()
                    self.retiring.remove(old)
                    self.done.append(old)
                    log(f"part {old.index + 1} finished: {os.path.basename(old.path)}", f"{self.platform}:{self.login}")
                    if self.on_retired:
                        try:
                            self.on_retired(old.path)
                        except Exception:
                            pass
