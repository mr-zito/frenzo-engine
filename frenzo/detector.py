"""Finds hype moments from chat speed."""
import re
import statistics
import time

HYPE = re.compile(
    r"(KEKW|LUL|OMEGALUL|LMAO+|LMFAO|ICANT|💀|😂|🤣|\bW+\b|POG|PogU|Pog|CLIP IT|CLIPPED|NO WAY|WTF|HOLY|"
    r"\bAHA+|\bHAHA+|\bxD+|\bmdr+|\bptdr+)",
    re.IGNORECASE,
)


def hype_weight(text):
    return 1.6 if HYPE.search(text) else 1.0


SUS = re.compile(r"(\bsus\b|\bsussy\b|\bayo+\b|🤨|😳|📸|\bcaught\b|\bbro what\b)", re.IGNORECASE)
SPICY = re.compile(r"(🥵|🤤|😍|🍑|\bgya+t+\b|\bthirsty\b|\brizz\b|\btwerk)", re.IGNORECASE)
BUG = re.compile(r"(\bbug+(ed|gy|s)?\b|\bglitch\w*|\bphysics\b|\bragdoll\w*|\bwhat is this game\b|\bnext gen\b|\brockstar\b|\b10/10\b|\bgame of the year\b|\bgoty\b)", re.IGNORECASE)
# funny / weird NPCs: chat talking about the NPC or confused by what someone in the game just did
NPC = re.compile(r"(\bnpcs?\b|\bthe ai\b|\bpedestrian|\bthat (guy|lady|dude|man|woman|cop)\b|\bthis (guy|lady|dude|cop)\b"
                 r"|\bwhat is (he|she|bro|blud|this guy) doing\b|\bwhy (is|did|does) (he|she|bro|blud)\b|\bcursed\b"
                 r"|\bweird\w*\b|\bbro is (tweaking|crazy|wild)\b|\bhe'?s tweaking\b|\bshe'?s tweaking\b|\bbro what\b"
                 r"|\bwhat the hell\b|\b(c'est quoi|il fait quoi|elle fait quoi)\b)", re.IGNORECASE)
# engaging gameplay: chat hyped by a chase, a stunt, a clean shootout or how real the game looks
GAMEPLAY = re.compile(r"(\bclean\b|\bcinematic\b|\bnasty\b|\bcracked\b|\bgoated\b|\bmovie\b|\bgraphics\b|\bso real\b"
                      r"|\brealistic\b|\blooks real\b|\bthis game is (crazy|insane|real|unreal)\b|\binsane (chase|drive|driving|jump|stunt|shot)\b"
                      r"|\bwhat a (chase|jump|shot|escape|save)\b|\bstunt\b|\bescape\w*\b|\bgetaway\b|\bchase\b)", re.IGNORECASE)


class SusDetector:
    """Flags moments where chat spams sus / ayo / 🤨 / 😳 even if chat isn't faster than usual."""

    def __init__(self, chat, cfg, window=15, min_hits=5, pattern=None):
        self.chat, self.window, self.min_hits = chat, window, min_hits
        self.pattern = pattern or SUS
        self.cooldown = cfg["cooldown_seconds"]
        self.last = 0.0

    def check(self, now=None):
        now = now or time.time()
        msgs = self.chat.snapshot(now - self.window, now)
        hits = sum(1 for _, _, text in msgs if self.pattern.search(text))
        if hits >= self.min_hits and hits >= 0.15 * max(1, len(msgs)) and now - self.last > self.cooldown:
            self.last = now
            return now - self.window * 0.6, hits
        return None


class SpikeDetector:
    def __init__(self, chat, cfg):
        self.chat = chat
        self.window = cfg["chat_window_seconds"]
        self.mult = cfg["spike_multiplier"]
        self.min_msgs = cfg["min_messages_in_window"]
        self.cooldown = cfg["cooldown_seconds"]
        self.history = []  # (time, score)
        self.last_trigger = 0.0
        self.started = time.time()

    def check(self, now=None):
        """Return (anchor_wall_time, ratio) when chat spikes, else None."""
        now = now or time.time()
        score = self.chat.count_between(now - self.window, now, hype_weight)
        self.history = [(t, s) for t, s in self.history if t > now - 300]
        baseline_vals = [s for t, s in self.history if t < now - self.window]
        self.history.append((now, score))
        if now - self.started < 60 or len(baseline_vals) < 6:
            return None  # still learning this chat's normal speed
        baseline = max(statistics.median(baseline_vals), 1.0)
        ratio = score / baseline
        if score >= self.min_msgs and ratio >= self.mult and now - self.last_trigger > self.cooldown:
            self.last_trigger = now
            # Chat reacts a few seconds after the moment happens
            return now - self.window * 0.6, ratio
        return None


class LoudnessDetector:
    """Fallback when there is no chat: flags sudden jumps in stream loudness (screams, laughs, explosions)."""

    def __init__(self, recorder, cfg, every=20):
        self.recorder = recorder
        self.jump = cfg["loudness_jump_db"]
        self.cooldown = cfg["cooldown_seconds"]
        self.every = every
        self.db = {}            # second-in-recording -> loudness
        self.last_check = 0.0
        self.last_trigger = -1e9

    def check(self, now=None):
        from .analyzer import extract_audio
        import numpy as np
        now = now or time.time()
        rec = self.recorder.recorded_seconds()
        if now - self.last_check < self.every or rec < 90:
            return None
        self.last_check = now
        start = int(max(0, rec - 35))
        rate = 4000
        try:
            audio = extract_audio(self.recorder.path, start, 30, rate=rate)
        except Exception:
            return None
        n = len(audio) // rate
        if n < 5:
            return None
        rms = np.sqrt((audio[: n * rate].reshape(n, rate) ** 2).mean(axis=1)) + 1e-9
        for i, v in enumerate(20 * np.log10(rms)):
            self.db[start + i] = float(v)
        cutoff = start - 300
        self.db = {k: v for k, v in self.db.items() if k >= cutoff}
        history = [v for k, v in self.db.items() if k < start]
        if len(history) < 60:
            return None
        baseline = statistics.median(history)
        best_sec, best = None, -1e9
        for s in range(start, start + n - 2):
            avg = (self.db[s] + self.db[s + 1] + self.db[s + 2]) / 3
            if avg > best:
                best_sec, best = s, avg
        if best - baseline >= self.jump and best_sec - self.last_trigger > self.cooldown:
            self.last_trigger = best_sec
            return self.recorder.t0 + best_sec + 1, best - baseline
        return None
