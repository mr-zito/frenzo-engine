"""The GTA SFX library (your gta-reel-sound-studio skill): finding it, the catalog for Claude's prompt, checking
the sound ids Claude picks, hints from chat categories, safe mode and the per-account "recently used" list.

The sounds are NOT copied into Frenzo: render.sound_library_path points at the library folder (empty = the
installed skill's gta_sfx_library)."""
import difflib
import json
import os
import re
import threading
from pathlib import Path

SKILL_NAME = "gta-reel-sound-studio"
_cache = {}
_lock = threading.Lock()

# Frenzo's chat categories / moment kinds -> library tags to look at first
HINTS = {
    "bug": ["fail", "glitch", "impact", "chaos"],
    "npc": ["awkward", "funny", "voice"],
    "weird": ["awkward", "funny", "suspense"],
    "sus": ["awkward", "rizz", "funny"],
    "spicy": ["rizz", "funny"],
    "hype": ["win", "hype", "sigma"],
    "gameplay": ["win", "hype", "impact", "whoosh"],
    "skill": ["win", "sigma", "hype"],
    "fail": ["fail", "impact", "sad"],
    "funny": ["funny", "fail", "voice"],
    "rage": ["impact", "fail", "chaos"],
    "shock": ["impact", "suspense", "flash"],
    "hot_take": ["voice", "sigma", "notification"],
    "wholesome": ["win", "chill"],
}
SOURCE_HINTS = [(re.compile(r"bug/glitch|gameplay chaos"), "bug"), (re.compile(r"NPC"), "npc"),
                (re.compile(r"sus"), "sus"), (re.compile(r"thirsty|spicy"), "spicy"),
                (re.compile(r"hyped by the gameplay|chat spike"), "hype"), (re.compile(r"laugh"), "funny"),
                (re.compile(r"yell|outburst"), "rage"), (re.compile(r"hot take"), "hot_take")]


def find_library(rcfg=None):
    """The gta_sfx_library folder: render.sound_library_path, else the installed skill, else the cloned repo."""
    cand = []
    p = str((rcfg or {}).get("sound_library_path") or "").strip()
    if p:
        cand.append(Path(os.path.expandvars(p)).expanduser())
    home = Path.home()
    cand += [home / ".claude" / "skills" / SKILL_NAME / "gta_sfx_library",
             home / "Documents" / SKILL_NAME / "skills" / SKILL_NAME / "gta_sfx_library"]
    for c in cand:
        if (c / "library.json").is_file():
            return c
    return None


def scripts_dir(lib):
    """The skill's scripts folder (render_reel.py, check_reel.py) next to the library."""
    s = Path(lib).parent / "scripts"
    return s if (s / "check_reel.py").is_file() else None


def skill_md(lib):
    p = Path(lib).parent / "SKILL.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def load(lib):
    lib = Path(lib)
    with _lock:
        if lib not in _cache:
            entries = json.loads((lib / "library.json").read_text(encoding="utf-8"))
            _cache[lib] = {e["id"]: e for e in entries}
        return _cache[lib]


def allowed(entry, allow_music=False, allow_explicit=False):
    tags = set(entry.get("tags") or [])
    if "music" in tags and not allow_music:
        return False
    if "explicit" in tags and not allow_explicit:
        return False
    return True


def catalog(lib, allow_music=False, allow_explicit=False):
    """One line per sound: id | tags | duration | peak_s (only the sounds safe mode allows)."""
    lines = []
    for sid, e in sorted(load(lib).items()):
        if allowed(e, allow_music, allow_explicit):
            lines.append(f"{sid} | {' '.join(e.get('tags') or [])} | {float(e.get('duration') or 0):.2f}s"
                         f" | peak {float(e.get('peak_s') or 0):.2f}s")
    return "\n".join(lines)


def rules(lib):
    """Sections 1 and 2 of SKILL.md (moment -> sound map, placement and mix rules), copyright notes kept short."""
    md = skill_md(lib)
    m = re.search(r"## 1\. Moment.*?(?=\n## 3\.)", md, re.S)
    if not m:
        return ""
    text = m.group(0)
    text = re.sub(r"\n### Copyright: say it once.*", "", text, flags=re.S)       # handled by safe mode in code
    return text.strip()


def fix_id(sid, lib, allow_music=False, allow_explicit=False, only=None):
    """A sound id Claude wrote -> the exact library id (close typos fixed), or None if unknown / not allowed.
    only: the owner's sound list (ids outside it are refused)."""
    lib_ids = load(lib)
    sid = str(sid or "").strip().lower().replace(" ", "-").replace("_", "-")
    if sid.endswith(".flac"):
        sid = sid[:-5]
    ok = {k for k, e in lib_ids.items() if allowed(e, allow_music, allow_explicit) and (not only or k in only)}
    if sid in ok:
        return sid
    near = difflib.get_close_matches(sid, list(ok), n=1, cutoff=0.82)
    return near[0] if near else None


def hints_for(source, category=None):
    """Library tags suggested by why the moment was flagged (chat categories etc.)."""
    kinds = [k for pat, k in SOURCE_HINTS if pat.search(source or "")]
    if category:
        kinds.insert(0, category)
    tags = []
    for k in kinds:
        for t in HINTS.get(k, []):
            if t not in tags:
                tags.append(t)
    return tags


# ---------------------------------------------------------------------------- recently used, per account
def _hist_path(cfg):
    return os.path.join(cfg["storage"]["output_dir"], ".sound_history.json")


def account_key(rcfg):
    """The account the clips are posted to: the first handle (e.g. @its_frenzo)."""
    hs = [str(h) for h in (rcfg.get("handles") or {}).values() if h]
    return (hs[0] if hs else str(rcfg.get("handle") or "default")).lower()


def recent(cfg, rcfg, n=None):
    n = int(n if n is not None else rcfg.get("sound_no_repeat", 15) or 0)
    if n <= 0:
        return []
    try:
        with open(_hist_path(cfg), "r", encoding="utf-8") as f:
            posts = json.load(f).get(account_key(rcfg)) or []
    except (OSError, ValueError):
        return []
    out = []
    for post in posts[-n:]:
        for s in post:
            if s not in out:
                out.append(s)
    return out


def remember(cfg, rcfg, sounds):
    if not sounds:
        return
    with _lock:
        path = _hist_path(cfg)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        key = account_key(rcfg)
        data[key] = (data.get(key) or [])[-49:] + [list(dict.fromkeys(sounds))]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(path + ".tmp", path)
        except OSError:
            pass


# ------------------------------------------------------------------------- your sound guide (render.sound_guide)
EFFECT_FX = {"zoom": ["zoom"], "flash": ["flash"], "slow_zoom": ["slow_zoom"], "cam_zoom": ["cam_zoom"],
             "freeze_zoom": ["zoom_hold"], "silence_after": ["cam_zoom"], "hook_rewind": []}


def guide(rcfg, lib=None):
    """{id: entry} of the owner's sound list (only ids that exist in the library), in their order. {} = no guide."""
    entries = [g for g in (rcfg.get("sound_guide") or []) if isinstance(g, dict) and g.get("id")]
    if not entries:
        return {}
    known = load(lib) if lib is not None else None
    return {g["id"]: g for g in entries if known is None or g["id"] in known}


def guide_prompt(rcfg, lib):
    """The owner's sound list for Claude: id | when | length | effect (the effect is added by code)."""
    entries = load(lib)
    lines = []
    for sid, g in guide(rcfg, lib).items():
        e = entries[sid]
        eff = {"freeze_zoom": "picture pauses + zooms on the item while it plays", "cam_zoom": "zoom on the webcam",
               "slow_zoom": "slow zoom-in on what's about to happen", "flash": "white flash",
               "silence_after": "game goes silent for a second, zoom on the webcam",
               "hook_rewind": "rewind from the webcam-only reaction to the start"}.get(g.get("effect") or "", "")
        lines.append(f"- {sid} ({float(e.get('duration') or 0):.1f}s): {g.get('when', '')}"
                     + (" [RARE: only when the moment really calls for it]" if g.get("rare") else "")
                     + (f" -> {eff}" if eff else ""))
    return "\n".join(lines)
