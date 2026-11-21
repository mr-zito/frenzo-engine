"""Voice reaction detector: notices when the streamer yells, laughs or makes a sudden weird noise.

Everything is measured against the streamer's OWN normal voice (learned during the first minutes),
so a naturally loud streamer does not trigger it constantly.

Per 40 ms frame we measure:
  - loudness in the voice band (250-3500 Hz, so deep game rumbles and explosions count less)
  - pitch (how high the voice is) and whether the sound is voiced
Then we look for three patterns:
  - YELL:     clearly louder AND higher-pitched than normal talking for at least ~0.5 s
  - LAUGH:    a regular "ha-ha-ha" rhythm: 5+ bursts, 3-6 per second, with deep gaps between them
  - OUTBURST: a very sudden very loud sound (scream, weird noise, smashed desk...)
"""
import collections
import subprocess

import numpy as np

RATE = 16000
HOP = 640                 # 40 ms frames
FPS = RATE // HOP         # 25 frames per second
SENSITIVITY = {"low": 1.3, "normal": 1.0, "high": 0.75}


def frame_features(audio):
    n = len(audio) // HOP
    if n == 0:
        z = np.zeros(0)
        return z, z, z.astype(bool)
    frames = audio[: n * HOP].reshape(n, HOP).astype(np.float64)
    frames = frames - frames.mean(axis=1, keepdims=True)
    spec = np.abs(np.fft.rfft(frames * np.hanning(HOP), n=1024, axis=1)) ** 2
    freqs = np.fft.rfftfreq(1024, 1 / RATE)
    band = (freqs >= 250) & (freqs <= 3500)
    db = 10 * np.log10(spec[:, band].sum(axis=1) + 1e-10)
    # pitch by normalised autocorrelation
    f = np.fft.rfft(frames, n=2 * HOP, axis=1)
    pw = np.abs(f) ** 2
    fq = np.fft.rfftfreq(2 * HOP, 1 / RATE)
    pw[:, (fq < 85) | (fq > 2500)] = 0          # ignore hum/rumble and hiss when finding pitch
    ac = np.fft.irfft(pw, axis=1)[:, :HOP]
    ac = ac / (ac[:, :1] + 1e-12)
    lo, hi = RATE // 1000, RATE // 70
    seg = ac[:, lo:hi]
    best = seg.argmax(axis=1)
    lag = best + lo
    voiced = (seg.max(axis=1) > 0.5) & (best > 0) & (best < seg.shape[1] - 1) & (db > db.max() - 45)
    f0 = np.where(voiced, RATE / lag, 0.0)
    return db, f0, voiced


class VoiceAnalyzer:
    """Feed it consecutive chunks of audio; it returns reactions it finds."""

    def __init__(self, sensitivity="normal", cooldown=60, history_seconds=300):
        self.k = SENSITIVITY.get(sensitivity, 1.0)
        self.cooldown = cooldown * FPS
        self.hist = collections.deque(maxlen=history_seconds * FPS)  # (idx, db, f0, voiced)
        self.last_idx = -1
        self.last_trigger = -10 ** 9

    def _baseline(self):
        talk = [(d, f) for _, d, f, v in self.hist if v]
        if len(talk) < 12 * FPS:          # need ~12 s of talking to know the streamer's voice
            return None
        dbs = np.array([d for d, _ in talk])
        f0s = np.array([f for _, f in talk])
        return float(np.median(dbs)), float(np.median(f0s)), float(np.percentile(dbs, 90))

    def feed(self, start_idx, audio):
        """start_idx = frame index (seconds*25) of the first sample. Returns list of (second, label, strength)."""
        db, f0, voiced = frame_features(audio)
        idx = np.arange(start_idx, start_idx + len(db))
        new = idx > self.last_idx
        base = self._baseline()
        events = []
        if base is not None and new.any():
            events = self._find(idx, db, f0, voiced, new, base)
        for i in np.nonzero(new)[0]:
            self.hist.append((int(idx[i]), float(db[i]), float(f0[i]), bool(voiced[i])))
        if len(idx):
            self.last_idx = max(self.last_idx, int(idx[-1]))
        return events

    def _find(self, idx, db, f0, voiced, new, base):
        base_db, base_f0, loud_db = base
        k = self.k
        found = []
        n = len(db)

        # YELL: louder + higher than normal talking
        yell = voiced & (db > base_db + 8 * k) & (f0 > base_f0 * (1 + 0.3 * k))
        # OUTBURST: very loud, voiced or not
        burst = db > max(base_db + 14 * k, loud_db + 6 * k)
        win = 30  # 1.2 s
        for i in range(0, max(1, n - win + 1), 5):
            sl = slice(i, i + win)
            if not new[sl].any():
                continue
            if yell[sl].sum() >= 12:
                found.append((idx[i + win // 2], "yelling", float(db[sl][yell[sl]].mean() - base_db)))
            elif burst[sl].sum() >= 6:
                found.append((idx[i + win // 2], "sudden loud outburst", float(db[sl].max() - base_db)))

        # LAUGH: regular bursts 3-6 per second with deep gaps
        sm = db
        peaks = [i for i in range(2, n - 2)
                 if sm[i] == sm[i - 2:i + 3].max() and sm[i] > base_db + 4 * k]
        run = []
        for p in peaks:
            if run and not (4 <= p - run[-1] <= 9 and sm[run[-1]:p + 1].min() < min(sm[run[-1]], sm[p]) - 6 * k):
                found += self._laugh(run, idx, sm, f0, voiced, base_f0)
                run = []
            run.append(p)
        found += self._laugh(run, idx, sm, f0, voiced, base_f0)

        out = []
        rank = {"laughing": 0, "yelling": 1, "sudden loud outburst": 2}
        for i, label, strength in sorted(found, key=lambda x: (rank[x[1]], -x[2])):
            if not new[int(i - idx[0])]:
                continue
            if i - self.last_trigger > self.cooldown and all(abs(i - j) > self.cooldown for j, _, _ in out):
                out.append((i, label, strength))
        if out:
            self.last_trigger = max(i for i, _, _ in out)
        return [(i / FPS, label, s) for i, label, s in out]

    def _laugh(self, run, idx, sm, f0, voiced, base_f0):
        if len(run) < 5:
            return []
        gaps = np.diff(run)
        if gaps.std() / gaps.mean() > 0.3:              # laughs are rhythmic, speech isn't
            return []
        pf = [f0[q] for p in run for q in (p - 1, p, p + 1) if 0 <= q < len(f0) and voiced[q]]
        if len(pf) < len(run) or np.median(pf) < base_f0 * 1.1:
            return []
        mid = run[len(run) // 2]
        return [(idx[mid], "laughing", float(len(run) * 2))]


class VoiceReactionDetector:
    """Live version: every few seconds, analyses the newest audio of the recording."""

    def __init__(self, recorder, cfg, every=8):
        self.recorder = recorder
        self.every = every
        self.an = VoiceAnalyzer(cfg.get("voice_sensitivity", "normal"), cfg["cooldown_seconds"])
        self.last_check = 0.0

    def check(self, now):
        from .analyzer import extract_audio
        rec = self.recorder.recorded_seconds()
        if now - self.last_check < self.every or rec < 20:
            return None
        self.last_check = now
        start = max(0.0, rec - 14)
        try:
            audio = extract_audio(self.recorder.path, start, 12)
        except Exception:
            return None
        events = self.an.feed(int(round(start * FPS)), audio)
        if not events:
            return None
        sec, label, strength = max(events, key=lambda e: e[2])
        return self.recorder.t0 + sec, label


def scan_file(path, sensitivity="normal", cooldown=60, chunk_seconds=12):
    """Test mode: run the analyzer over a whole video file. Returns [(second, label, strength)]."""
    an = VoiceAnalyzer(sensitivity, cooldown)
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-vn",
         "-af", "aresample=async=1:first_pts=0", "-ac", "1",
         "-ar", str(RATE), "-f", "s16le", "-"], stdout=subprocess.PIPE)
    events, pos = [], 0
    size = chunk_seconds * RATE * 2
    while True:
        raw = proc.stdout.read(size)
        if not raw:
            break
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        events += an.feed(pos // HOP, audio)
        pos += len(audio)
    proc.wait()
    return events
