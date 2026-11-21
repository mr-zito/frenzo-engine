"""Per-streamer CAMPAIGN RULES (config.yaml: a "campaign:" block in a streamer's entry).

They change HOW clips are edited and written (length, captions, sound, handle, title, caption, hashtags,
credit), never WHICH moments are found or how they are scored.

    campaign:
      name: "Kai Cenat GTA 6 - $10K"          # optional label
      link: "https://whop.com/..."            # optional, the campaign page
      instructions: |                         # free text, pasted from the campaign's rules
        ...
      hashtags: ["#kaicenat", "#gta6"]        # always in the post text
      mentions: ["@kaicenat"]                 # always in the caption (credit / tag)
      platforms: [tiktok, instagram, youtube, x]   # where this campaign pays
      min_seconds: 0                          # 0 = no rule
      max_seconds: 0
      watermark: true                         # false = no handle on the video for this campaign
      captions: true                          # false = no small captions on the video
      sound_effects: true                     # false = no sus sound
      overlay: {file: assets/overlays/x.png, position: top-right, size: 22, opacity: 1}   # required image on the video
      look_for: |                             # EXTRA content the campaign wants (Claude checks it: campaign_match)
        moments at the 1WIN casino
      chat_keywords: [1win, casino]           # EXTRA live search: chat saying these
      speech_keywords: [1win, casino]         # EXTRA live + past-stream search: the streamer saying these

The extra content searches are ADDED for that streamer only: every existing detector, score and rule stays the same.
"""
import os
import re

PLATFORMS = ["tiktok", "instagram", "youtube", "x", "facebook"]
POSITIONS = ["top-left", "top", "top-right", "center", "bottom-left", "bottom", "bottom-right"]
OVERLAY_DEFAULT = {"file": "", "position": "top-right", "size": 22, "opacity": 1.0}
DEFAULT = {"name": "", "link": "", "instructions": "", "hashtags": [], "mentions": [],
           "platforms": ["tiktok", "instagram", "youtube", "x"], "min_seconds": 0, "max_seconds": 0,
           "watermark": True, "captions": True, "sound_effects": True,
           # an image the campaign requires on every clip (logo, sponsor badge...)
           "overlay": OVERLAY_DEFAULT,
           # EXTRA content this campaign wants: added searches for this streamer, existing detection unchanged
           "look_for": "", "chat_keywords": [], "speech_keywords": []}


def _num(v):
    try:
        v = float(v)
        return max(0.0, v) if v == v else 0.0
    except (TypeError, ValueError):
        return 0.0


def _tags(items, prefix):
    out = []
    for t in items if isinstance(items, (list, tuple)) else re.split(r"[\s,]+", str(items or "")):
        t = str(t).strip()
        if not t:
            continue
        t = t if t.startswith(prefix) else prefix + t.lstrip("#@")
        if len(t) > 1 and t.lower() not in [x.lower() for x in out]:
            out.append(t[:60])
    return out


def normalize(block):
    """Any campaign block (or None) -> a complete dict with the defaults filled in."""
    b = block if isinstance(block, dict) else {}
    out = dict(DEFAULT)
    out["name"] = str(b.get("name") or "").strip()[:120]
    out["link"] = str(b.get("link") or "").strip()[:500]
    out["instructions"] = str(b.get("instructions") or "").strip()[:4000]
    out["hashtags"] = _tags(b.get("hashtags"), "#")
    out["mentions"] = _tags(b.get("mentions"), "@")
    plats = b.get("platforms")
    if isinstance(plats, (list, tuple)):
        out["platforms"] = [p for p in PLATFORMS if p in [str(x).strip().lower() for x in plats]]
    out["min_seconds"] = _num(b.get("min_seconds"))
    out["max_seconds"] = _num(b.get("max_seconds"))
    if out["max_seconds"] and out["min_seconds"] > out["max_seconds"]:
        out["min_seconds"] = out["max_seconds"]
    for k in ("watermark", "captions", "sound_effects"):
        out[k] = b.get(k) is not False
    ov = b.get("overlay") if isinstance(b.get("overlay"), dict) else {}
    pos = str(ov.get("position") or OVERLAY_DEFAULT["position"]).strip().lower()
    out["overlay"] = {"file": str(ov.get("file") or "").strip()[:300],
                      "position": pos if pos in POSITIONS else OVERLAY_DEFAULT["position"],
                      "size": max(5, min(100, int(_num(ov.get("size")) or OVERLAY_DEFAULT["size"]))),
                      "opacity": round(max(0.1, min(1.0, _num(ov.get("opacity")) or 1.0)), 2)}
    out["look_for"] = str(b.get("look_for") or "").strip()[:1500]
    out["chat_keywords"] = _words(b.get("chat_keywords"))
    out["speech_keywords"] = _words(b.get("speech_keywords"))
    return out


def _words(items):
    """Plain words / short phrases (always escaped before use: never a regex)."""
    from .learning import clean_keywords
    if isinstance(items, str):
        items = [x for x in re.split(r"[,\n]+", items)]
    return clean_keywords([str(x) for x in items or []])


def has_rules(camp):
    """True when the streamer has any campaign rule at all (anything different from the defaults)."""
    if not camp:
        return False
    return any(camp.get(k) != DEFAULT[k] for k in DEFAULT)


def of(scfg):
    """The normalized campaign of a streamer config (None if it has no rules)."""
    camp = normalize((scfg or {}).get("campaign"))
    return camp if has_rules(camp) else None


def for_login(cfg, login, platform=None):
    for s in cfg.get("streamers") or []:
        if s.get("login") == login and (platform is None or s.get("platform") == platform):
            return of(s)
    return None


def _length_text(camp):
    mn, mx = camp.get("min_seconds") or 0, camp.get("max_seconds") or 0
    if mn and mx:
        return f"between {mn:g} and {mx:g} seconds"
    if mx:
        return f"at most {mx:g} seconds"
    if mn:
        return f"at least {mn:g} seconds"
    return ""


def prompt_section(camp):
    """The rules for Claude's prompt (editing and writing only)."""
    if not camp:
        return ""
    parts = []
    if camp.get("instructions"):
        parts.append(camp["instructions"].strip())
    if camp.get("hashtags"):
        parts.append("Required hashtags: " + " ".join(camp["hashtags"]))
    if camp.get("mentions"):
        parts.append("Required mentions/credit: " + " ".join(camp["mentions"]))
    if _length_text(camp):
        parts.append("Length: " + _length_text(camp))
    if not camp.get("captions", True):
        parts.append("No small captions on the video (they are turned off for this campaign).")
    if not camp.get("sound_effects", True):
        parts.append("No sound effects: sfx must be [].")
    if overlay_path(camp):
        parts.append(f"The campaign's image is placed {camp['overlay']['position'].replace('-', ' ')} on the video: "
                     "don't put callouts there.")
    wants = looks_for(camp)
    if not parts and not wants:
        return ""
    name = f" ({camp['name']})" if camp.get("name") else ""
    out = ""
    if parts:
        out += (f"\n\nCAMPAIGN RULES for this streamer{name} (must be followed in the EDIT and the TEXT):\n"
                + "\n".join(parts)
                + "\nThese rules only change HOW you edit and write: start/end, segments, title_bar, highlight, callouts, "
                  "title, caption, hashtags. They never change keep or viral_score: score the moment exactly as you would "
                  "without them. If a rule conflicts with the general style, the rule wins.")
    if wants:
        out += ("\n\nEXTRA CONTENT THIS CAMPAIGN WANTS (an extra check, on top of everything above): " + wants
                + "\nAdd two keys to your JSON: campaign_match (true if this moment clearly shows or talks about that "
                  "content, else false) and campaign_note (one short sentence: what you saw or heard, or why not). "
                  "This is only reported: it never changes keep or viral_score. When it's true and the clip is kept, "
                  "make sure the cut includes that content.")
    return out


def looks_for(camp):
    """Text of the extra content the campaign wants (free text + the words to watch for)."""
    if not camp:
        return ""
    bits = []
    if camp.get("look_for"):
        bits.append(camp["look_for"].strip())
    if camp.get("speech_keywords"):
        bits.append("the streamer says: " + ", ".join(f'"{w}"' for w in camp["speech_keywords"]))
    if camp.get("chat_keywords"):
        bits.append("chat says: " + ", ".join(f'"{w}"' for w in camp["chat_keywords"]))
    return " | ".join(bits)


def chat_pattern(camp):
    from .learning import keyword_pattern
    return keyword_pattern((camp or {}).get("chat_keywords") or [])


def speech_pattern(camp):
    from .learning import keyword_pattern
    return keyword_pattern((camp or {}).get("speech_keywords") or [])


def overlay_path(camp):
    """Absolute path of the campaign's overlay image, or None."""
    f = ((camp or {}).get("overlay") or {}).get("file") or ""
    if not f:
        return None
    from .config import ROOT
    p = f if os.path.isabs(f) else str(ROOT / f)
    return p if os.path.isfile(p) else None


# ------------------------------------------------------------------------------------------- length
def length_bounds(clips_cfg, camp):
    """(min, max) clip length: the campaign's rule replaces the global one when set."""
    mn, mx = float(clips_cfg["min_length"]), float(clips_cfg["max_length"])
    if camp:
        if camp.get("min_seconds"):
            mn = float(camp["min_seconds"])
        if camp.get("max_seconds"):
            mx = float(camp["max_seconds"])
    return min(mn, mx), mx


def fit_segments(segs, lo, hi, mn, mx):
    """Keep the total length of the kept pieces between mn and mx: too long -> trimmed from the end (the
    first pieces hold the setup), too short -> the last piece is extended (then the first one, earlier)."""
    segs = [[float(a), float(b)] for a, b in segs if b > a]
    if not segs:
        return segs
    total = sum(b - a for a, b in segs)
    if mx and total > mx:
        out, left = [], mx
        for a, b in segs:
            if left <= 0.05:
                break
            take = min(b - a, left)
            out.append([a, a + take])
            left -= take
        segs = out
    total = sum(b - a for a, b in segs)
    if mn and total < mn:
        need = mn - total
        grow = min(need, max(0.0, hi - segs[-1][1]))
        segs[-1][1] += grow
        need -= grow
        if need > 0:
            segs[0][0] = max(lo, segs[0][0] - need)
    return segs


# ---------------------------------------------------------------------------------------- post text
CREDIT_WORD = re.compile(r"\b(credit|credits|via|cc|tag|tags|featuring|ft\.?|shoutout|by)\s*:?\s*@", re.I)


def _credit_word(instructions):
    m = CREDIT_WORD.search(instructions or "")
    if not m:
        return "Credit:"
    w = m.group(1)
    return (w[:1].upper() + w[1:].lower()).rstrip(".") + ":"


def apply_text(hashtags, caption, camp):
    """Required hashtags always in the hashtags, required mentions always in the caption. Returns both."""
    if not camp:
        return hashtags, caption
    tags = [t for t in re.split(r"\s+", str(hashtags or "").strip()) if t]
    low = [t.lower() for t in tags]
    for t in camp.get("hashtags") or []:
        if t.lower() not in low:
            tags.append(t)
            low.append(t.lower())
    caption = str(caption or "").strip()
    missing = [m for m in camp.get("mentions") or [] if not re.search(r"(?<![\w@])" + re.escape(m) + r"(?!\w)", caption, re.I)]
    if missing:
        caption = (caption + "\n\n" if caption else "") + f"{_credit_word(camp.get('instructions'))} " + " ".join(missing)
    return " ".join(tags), caption


def apply_to_meta(meta, camp):
    """meta.json fields: hashtags, caption, campaign name. Returns True if something changed."""
    if not camp:
        return False
    before = (meta.get("hashtags"), meta.get("caption"), meta.get("campaign"))
    meta["hashtags"], meta["caption"] = apply_text(meta.get("hashtags"), meta.get("caption"), camp)
    meta["campaign"] = camp.get("name") or "campaign rules"
    return before != (meta.get("hashtags"), meta.get("caption"), meta.get("campaign"))


# ------------------------------------------------------------------------------------------ rendering
def render_cfg(rcfg, camp):
    """Render settings for this streamer: captions / sus sound turned off when the campaign says so."""
    if not camp:
        return rcfg
    rcfg = dict(rcfg)
    if not camp.get("captions", True):
        rcfg["captions"] = False
    if not camp.get("sound_effects", True):
        rcfg["sound_effects"] = False
    path = overlay_path(camp)
    if path:
        rcfg["campaign_overlay"] = dict(camp["overlay"], path=path)
    return rcfg


def post_file(names, platform, camp):
    """Which file to post on a platform: the handle version, or the clean one when the campaign forbids
    your handle on the video (watermark: false)."""
    names = list(names)
    if camp and not camp.get("watermark", True):
        for f in ("clip_clean.mp4", "clip.mp4"):
            if f in names:
                return f
    for f in names:
        if platform in f[5:-4].split("+"):
            return f
    for f in ("clip_clean.mp4", "clip.mp4"):
        if f in names:
            return f
    return names[0] if names else None


def caption_txt(meta, files, raw_offset=None, camp=None):
    """Text of caption.txt (post text + which file to post where)."""
    files = list(files)
    head = f"{meta.get('title', '') or ''}\n\n{meta.get('caption', '') or ''}\n\n{meta.get('hashtags', '') or ''}\n\n"
    if raw_offset is not None:
        head += f"(raw.mp4: the clip starts at {float(raw_offset):.0f}s)\n\n"
    if camp and camp.get("name"):
        head += f"Campaign: {camp['name']}" + (f" ({camp['link']})" if camp.get("link") else "") + "\n\n"
    if camp and not camp.get("watermark", True):
        clean = post_file(files, "tiktok", camp)
        plats = ", ".join(camp.get("platforms") or PLATFORMS)
        return head + ("Which file to post where:\n"
                       f"  {clean}  ->  every platform ({plats}): this campaign doesn't allow your handle on the video\n")
    return head + "Which file to post where:\n" + "".join(
        f"  {fn}  ->  " + ("no handle (Facebook / campaigns without watermark)" if fn == "clip_clean.mp4"
                           else fn[5:-4].replace("+", " & ")) + "\n" for fn in files)


def write_caption_txt(folder, meta, files=None, raw_offset=None, camp=None):
    if files is None:
        files = sorted(f for f in os.listdir(folder) if f.startswith("clip") and f.endswith(".mp4"))
    with open(os.path.join(folder, "caption.txt"), "w", encoding="utf-8") as f:
        f.write(caption_txt(meta, files, raw_offset, camp))
