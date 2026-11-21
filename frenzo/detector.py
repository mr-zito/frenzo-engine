"""Finds hype moments from chat speed."""
import math
import re
import statistics
import time

DEFAULT_DELAY = 6.0         # chat reacts ~6 s after what it sees (replaced by the learned per-streamer delay)


def poisson_tail(k, lam):
    """P(X >= k) for X ~ Poisson(lam): how surprising k messages are when lam is this chat's normal count."""
    lam = max(lam, 1e-6)
    if k <= 0:
        return 1.0
    if lam > 60:                                   # normal approximation (with continuity correction)
        z = (k - 0.5 - lam) / math.sqrt(lam)
        return 0.5 * math.erfc(z / math.sqrt(2))
    term = math.exp(-lam)
    cdf = term
    for i in range(1, int(k)):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)

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
        self.last = -1e18
        self.delay = DEFAULT_DELAY          # learned chat delay (set by the engine)

    def check(self, now=None):
        now = now or time.time()
        msgs = self.chat.snapshot(now - self.window, now)
        hits = sum(1 for _, _, text in msgs if self.pattern.search(text))
        if hits >= self.min_hits and hits >= 0.15 * max(1, len(msgs)) and now - self.last > self.cooldown:
            self.last = now
            return now - self.window * 0.6 - (self.delay - DEFAULT_DELAY), hits
        return None


class SpikeDetector:
    def __init__(self, chat, cfg):
        self.chat = chat
        self.window = cfg["chat_window_seconds"]
        self.mult = cfg["spike_multiplier"]
        self.min_msgs = cfg["min_messages_in_window"]
        self.cooldown = cfg["cooldown_seconds"]
        # "scaled": instead of a fixed 15 messages, the burst must be very unlikely for THIS chat's normal speed
        # (Poisson test), so small streamers can trigger and big chats still need a real spike
        self.floor = cfg.get("chat_floor", "scaled")
        self.history = []  # (time, weighted score, message count)
        self.last_trigger = -1e18
        self.started = time.time()
        self.delay = DEFAULT_DELAY          # learned chat delay (set by the engine)

    def check(self, now=None):
        """Return (anchor_wall_time, ratio) when chat spikes, else None."""
        now = now or time.time()
        score = self.chat.count_between(now - self.window, now, hype_weight)
        count = self.chat.count_between(now - self.window, now)
        self.history = [h for h in self.history if h[0] > now - 300]
        older = [h for h in self.history if h[0] < now - self.window]
        self.history.append((now, score, count))
        if now - self.started < 60 or len(older) < 6:
            return None  # still learning this chat's normal speed
        baseline = max(statistics.median([h[1] for h in older]), 1.0)
        ratio = score / baseline
        if self.floor == "fixed":
            enough = score >= self.min_msgs
        else:
            normal = statistics.mean([h[2] for h in older])
            enough = count >= 5 and poisson_tail(count, max(normal, 0.5)) < 1e-3
        if enough and ratio >= self.mult and now - self.last_trigger > self.cooldown:
            self.last_trigger = now
            # Chat reacts a few seconds after the moment happens (learned per streamer)
            return now - self.window * 0.6 - (self.delay - DEFAULT_DELAY), ratio
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
