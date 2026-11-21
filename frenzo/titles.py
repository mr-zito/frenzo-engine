"""Titles: 5 candidates from Claude, checked by code (3-8 words, no spoiler of the outcome, no banned phrases),
and the title bank (examples of good hooks: the seed list + your own edited titles and posted clips)."""
import json
import os
import re
import threading

BANNED = [r"\bfunny moments?\b", r"\bhilarious\b", r"\breacts? to\b", r"\bgta ?(6|vi) gameplay\b"]
# words that give away how it ends (the payoff should be watched, not read)
SPOILERS = [r"\bcrash(es|ed)?\b", r"\bdies\b", r"\bdied\b", r"\bdead\b", r"\bkilled\b", r"\bgets? (arrested|busted|caught|killed|wasted)\b",
            r"\barrested\b", r"\bbusted\b", r"\bwasted\b", r"\bfalls?\b", r"\bfell\b", r"\bexplodes?\b", r"\bexploded\b",
            r"\bblows? up\b", r"\bdrowns?\b", r"\bdrowned\b", r"\bflips?\b", r"\bflipped\b", r"\bgets? hit\b",
            r"\bends up\b", r"\bturns into\b", r"\bfails?\b", r"\bfailed\b", r"\bcrush(es|ed)?\b", r"\bsmash(es|ed)\b",
            r"\bdestroy(s|ed)\b", r"\bsinks?\b", r"\bsank\b", r"\blaunch(es|ed)\b", r"\bruns? over\b", r"\bran over\b",
            r"\bgets? (launched|smashed|destroyed|run over|robbed|shot)\b", r"\bloses\b", r"\blost it all\b"]
_lock = threading.Lock()
SEED = os.path.join(os.path.dirname(__file__), "title_bank.json")


def words(text):
    return [w for w in re.findall(r"[\w'’$%+-]+", text or "") if w.strip("'’")]


def problems(title, what_happens=""):
    """What's wrong with an on-screen title ([] = fine)."""
    out = []
    n = len(words(title))
    if n < 3 or n > 8:
        out.append(f"{n} words (want 3-8)")
    low = (title or "").lower()
    for pat in BANNED:
        if re.search(pat, low):
            out.append("banned phrase: " + re.search(pat, low).group(0))
    for pat in SPOILERS:
        m = re.search(pat, low)
        if m and not _quoted(title, m.start()):
            out.append("spoils the ending: " + m.group(0))
            break
    return out


def _quoted(text, pos):
    """A spoiler word inside the streamer's quoted words is fine ('Bro said "I won't crash"')."""
    before = text[:pos]
    return before.count('"') % 2 == 1 or before.count("“") > before.count("”")


def pick(d):
    """Claude's 5 candidates -> (title, check report). Claude's pick if it passes, else the first one that passes,
    else Claude's pick (flagged). Candidates are kept for A/B tests."""
    cands = []
    for t in d.get("titles") or []:
        text = (t.get("text") if isinstance(t, dict) else str(t or "")).strip()
        if len(text) > 2 and text[0] == text[-1] == '"' and text.count('"') == 2:
            text = text[1:-1]                      # the whole title wrapped in quotes
        if text:
            cands.append({"text": text[:90], "format": (t.get("format") if isinstance(t, dict) else "") or "",
                          "problems": problems(text)})
    if not cands:
        tb = str(d.get("title_bar") or "").strip()
        return tb, {"candidates": [], "problems": problems(tb) if tb else []}
    try:
        i = int(d.get("title_pick") or 0)
        if not 0 <= i < len(cands):
            i = 0
    except (TypeError, ValueError):
        i = 0
    chosen = i if not cands[i]["problems"] else next((k for k, c in enumerate(cands) if not c["problems"]), i)
    rep = {"candidates": cands, "claude_pick": i, "picked": chosen, "why": str(d.get("title_why") or "")[:200],
           "problems": cands[chosen]["problems"]}
    if chosen != i:
        rep["note"] = f"Claude's pick had problems ({'; '.join(cands[i]['problems'])}): used candidate {chosen + 1}"
    return cands[chosen]["text"], rep


# ------------------------------------------------------------------------------------------- title bank
def _user_bank(cfg):
    return os.path.join(cfg["storage"]["output_dir"], ".learning", "title_bank.json")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def add(cfg, title, source, note=""):
    """Your edited titles ('edited') and posted / best-performing clips ('posted', 'performer') join the bank."""
    title = (title or "").strip()
    if not title or problems(title):
        return False
    with _lock:
        path = _user_bank(cfg)
        bank = [b for b in _read(path) if b.get("good", "").lower() != title.lower()]
        bank.append({"good": title, "source": source, "note": note[:120]})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(bank[-300:], f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)
    return True


def examples(cfg, n=25):
    """The 20-30 best for the prompt: your performers and edits first (newest first), then the seed list."""
    rank = {"performer": 0, "edited": 1, "posted": 2}
    mine = sorted(enumerate(_read(_user_bank(cfg))), key=lambda x: (rank.get(x[1].get("source"), 3), -x[0]))
    out = [b for _, b in mine][: max(0, n - 10)]
    for s in _read(SEED):
        if len(out) >= n:
            break
        if all(s.get("good", "").lower() != o.get("good", "").lower() for o in out):
            out.append(s)
    return out


def prompt_block(cfg, n=25):
    ex = examples(cfg, n)
    lines = []
    for e in ex:
        bad = f'BAD "{e["bad"]}" -> ' if e.get("bad") else ""
        lines.append(f'- {bad}GOOD "{e["good"]}"' + (f" ({e['source']})" if e.get("source") in ("edited", "performer", "posted") else ""))
    return "\n".join(lines)


def refresh_from_clips(cfg, limit=400):
    """Titles of clips you marked posted (and any with view stats) join the bank (called by learning runs)."""
    import glob
    root = cfg["storage"]["output_dir"]
    metas = sorted(glob.glob(os.path.join(root, "*", "*", "*", "meta.json")), key=os.path.getmtime)[-limit:]
    added = 0
    for m in metas:
        try:
            with open(m, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        posted = any((meta.get("posted") or {}).values())
        views = meta.get("views") if isinstance(meta.get("views"), (int, float)) else 0
        if posted or views:
            added += add(cfg, meta.get("title_bar") or "", "performer" if views >= 10000 else "posted")
    return added
