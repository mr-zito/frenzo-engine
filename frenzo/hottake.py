"""Speech listener (hot takes + NPC talk): for chosen streamers, keeps transcribing what they say (small, fast speech model)
and flags strong opinions about the game, the kind of moment that goes viral with no action at all
("ohnePixel's honest thoughts on GTA 6"). Turn it on per streamer with  hot_takes: true  in config.yaml.
"""
import re
import threading

from .analyzer import Transcriber, extract_audio
from .util import log

# phrases that start a take (English + French)
TAKE = re.compile(
    r"(\bgta (6|six|vi)\b.{0,25}\b(is|was|feels|looks|has|got|ain'?t|isn'?t)\b"
    r"|\bthis game\b.{0,15}\b(is|was|feels|looks|ain'?t|isn'?t)\b"
    r"|\b(honestly|ngl|not gonna lie|hot take|unpopular opinion|i swear|i('m| am) telling you|real talk)\b"
    r"|\brockstar\b.{0,20}\b(cooked|did|made|is|are|really|fumbled|nailed)\b"
    r"|\b(overrated|underrated|mid|masterpiece|best game|worst game|game of the year|goty|10 out of 10|"
    r"ten out of ten|peak|trash|garbage|too real|next gen|no way this is a game)\b"
    r"|\b(franchement|honnêtement|ce jeu est|gta (6|vi) (est|c'est)|rockstar (a|ont))\b)",
    re.IGNORECASE)

# the streamer talking about a funny / weird NPC or something strange in the game
NPC_TALK = re.compile(
    r"(\b(this|that|the) (npc|guy|lady|dude|cop|man|woman|old man|granny)\b.{0,30}\b(is|just|did|tweaking|crazy|weird|wild|"
    r"tripping|bugging|chasing|following|staring|crying|dancing|naked|drunk|high)\b"
    r"|\bwhat is (he|she|bro|blud|this guy|this lady|this npc) doing\b|\bwhy (is|did|does) (he|she|bro|this guy|this lady)\b"
    r"|\bnpcs? (are|is|in this game)\b|\bthe ai (is|in this)\b|\bbro is tweaking\b|\bhe'?s tweaking\b|\bshe'?s tweaking\b"
    r"|\bthat'?s (so )?(weird|cursed|creepy)\b|\bwhat the (hell|heck) (was|is) that\b"
    r"|\b(il fait quoi|elle fait quoi|c'est quoi ce pnj|ce pnj|les pnj)\b)",
    re.IGNORECASE)

_shared = {}
_lock = threading.Lock()


def _transcriber(cfg):
    with _lock:
        if "t" not in _shared:
            tcfg = dict(cfg["transcription"], model=cfg["detection"].get("hot_take_model", "base"))
            _shared["t"] = Transcriber(tcfg)
        return _shared["t"]


def find_takes(words, min_words=8, pattern=None):
    """words with start/end (seconds) -> list of (second, snippet, strength) for opinion phrases
    (or for NPC talk with pattern=NPC_TALK)."""
    pattern = pattern or TAKE
    if len(words) < min_words:
        return []
    text, pos = "", []
    for w in words:
        pos.append((len(text), w["start"]))
        text += w["word"] + " "
    out = []
    for m in pattern.finditer(text):
        at = next((t for p, t in reversed(pos) if p <= m.start()), words[0]["start"])
        snippet = text[max(0, m.start() - 40): m.end() + 60].strip()
        talk = sum(1 for w in words if at - 3 <= w["start"] <= at + 12)   # keeps talking = real take
        out.append((at, snippet, talk + 4))
    return out


class HotTakeDetector:
    def __init__(self, recorder, cfg, every=20, cooldown=90, learned=None, only=None):
        self.recorder, self.cfg = recorder, cfg
        self.learned = learned          # phrases learned from your clips (escaped keywords), or None
        self.only = only                # a campaign's phrases: this listener looks for nothing else (kind "campaign")
        self.every, self.cooldown = every, cooldown
        self.last_check = 0.0
        self.last_trigger = -1e9
        self.done_until = 0.0

    def check(self, now):
        rec = self.recorder.recorded_seconds()
        if now - self.last_check < self.every or rec < 40:
            return None
        self.last_check = now
        start = max(self.done_until - 3, rec - 26)      # small overlap so sentences aren't split
        try:
            audio = extract_audio(self.recorder.path, start, rec - 2 - start)
            words, _ = _transcriber(self.cfg).transcribe(audio)
        except Exception as e:
            log(f"hot-take listener paused ({str(e)[:80]})", "hottake")
            return None
        self.done_until = rec - 2
        if self.only is not None:
            takes = [(start + t, snip, s, "campaign") for t, snip, s in find_takes(words, min_words=2, pattern=self.only)]
            takes = [x for x in takes if x[0] - self.last_trigger > self.cooldown]
            if not takes:
                return None
            sec, snippet, _, kind = max(takes, key=lambda x: x[2])
            self.last_trigger = sec
            return self.recorder.t0 + sec, snippet, kind
        takes = [(start + t, snip, s + 2, "npc") for t, snip, s in find_takes(words, min_words=4, pattern=NPC_TALK)]
        takes = [x for x in takes if x[2] >= 8]
        takes += [(start + t, snip, s, "take") for t, snip, s in find_takes(words) if s >= 14]
        if self.learned is not None:
            takes += [(start + t, snip, s + 2, "learned")
                      for t, snip, s in find_takes(words, min_words=4, pattern=self.learned) if s + 2 >= 8]
        takes = [x for x in takes if x[0] - self.last_trigger > self.cooldown]
        if not takes:
            return None
        sec, snippet, _, kind = max(takes, key=lambda x: x[2])
        self.last_trigger = sec
        return self.recorder.t0 + sec, snippet, kind
