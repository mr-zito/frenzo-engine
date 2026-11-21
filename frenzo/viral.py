"""Editing style learned from viral GTA 6 clip pages (analysed Nov 2026):

- title on the line between webcam and gameplay: sentence case, mentions the game, ONE yellow keyword,
  a colour emoji at the end ("Insane GTA 6 bug 💀", "Bro wants Lucia 😱", "GTA VI has NO Autosave 😳")
- small captions of what the streamer says, 1-3 words at a time, in the middle of the gameplay
- a few bigger coloured "callouts" on the key beats ("*Laughing*", "+$1k", "Gawd~", "PS5 Stick drift")
- jump cuts: dead air removed, first frame already on the action, end on the reaction
- 12-35 s (most around 17 s), no background music, sound effects rare
- swear / sensitive words censored ("dr*nk", "f*cking") to stay advertiser-friendly
"""
import re

from .util import fmt_ts, hex_to_ass

W, H = 1080, 1920

CALLOUT_COLORS = {"yellow": "#FFE135", "green": "#3BE36B", "pink": "#FF6AD5", "red": "#FF3B3B",
                  "orange": "#FF9F1C", "white": "#FFFFFF", "blue": "#4FC3FF", "purple": "#B57BFF"}

_CENSOR = {
    r"fuck\w*": None, r"shit\w*": None, r"bitch\w*": None, r"asshole\w*": None, r"dick\w*": None,
    r"pussy\w*": None, r"cock\w*": None, r"whore\w*": None, r"slut\w*": None, r"drunk\w*": None,
    r"sex\w*": None, r"porn\w*": None, r"cocaine": None, r"weed": None, r"drugs?": None,
    r"kill\w*": None, r"suicide": None, r"nigg\w*": "n****", r"fag\w*": "f**", r"retard\w*": "r*****",
}


def censor(text):
    """dr*nk / f*cking style: replace the first vowel after the first letter with *."""
    def star(m, fixed=None):
        w = m.group(0)
        if fixed:
            return fixed
        for i in range(1, len(w)):
            if w[i].lower() in "aeiou":
                return w[:i] + "*" + w[i + 1:]
        return w[0] + "*" + w[2:]
    for pat, fixed in _CENSOR.items():
        text = re.sub(r"\b" + pat + r"\b", lambda m, f=fixed: star(m, f), text, flags=re.IGNORECASE)
    return text


def clean_segments(segments, start, end, min_piece=1.2):
    """Claude's keep-segments (window time) -> sorted, merged, inside [start, end]."""
    segs = []
    for s in segments or []:
        try:
            a, b = float(s[0]), float(s[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        a, b = max(a, start), min(b, end)
        if b - a >= min_piece:
            segs.append([a, b])
    segs.sort()
    merged = []
    for a, b in segs:
        if merged and a - merged[-1][1] < 0.35:      # tiny gaps: don't make a jarring cut
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged[:8] or [[start, end]]


def add_margins(segs, lead, tail, lo, hi, min_setup_before=None):
    """Breathing room so viewers understand the clip: 'lead' s before the first piece, 'tail' s after the
    last one (inside [lo, hi], the recorded window). min_setup_before=(flag_second, s): make sure the clip
    shows at least s seconds before the flagged moment when that moment is inside the first piece."""
    segs = [list(x) for x in segs]
    if not segs:
        return segs
    first = segs[0]
    if min_setup_before:
        flag, need = min_setup_before
        if first[0] <= flag <= first[1] and flag - first[0] < need:
            first[0] = flag - need
    first[0] = max(lo, first[0] - lead)
    segs[-1][1] = min(hi, segs[-1][1] + tail)
    if len(segs) > 1 and segs[0][1] > segs[1][0]:          # margins made pieces overlap: merge
        merged = [segs[0]]
        for a, b in segs[1:]:
            if a <= merged[-1][1] + 0.35:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        segs = merged
    return segs


def mapper(segments):
    """Window time -> output time (None if that moment was cut out)."""
    def f(t):
        out = 0.0
        for a, b in segments:
            if a <= t <= b:
                return out + (t - a)
            out += b - a
        return None
    return f


def _esc(text):
    return re.sub(r"[{}\\]", "", text or "").replace("\n", " ").strip()


def build_ass(words, callouts, dur, rcfg, layout, seam_y):
    """words / callouts already on the OUTPUT timeline. Small captions + coloured callouts."""
    font = rcfg.get("caption_font", "Montserrat ExtraBold")
    white = hex_to_ass("#FFFFFF")
    game_top = seam_y if layout == "reel" else 0
    cap_y = int(game_top + (H - game_top) * (0.40 if layout == "reel" else 0.55))
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Mini,{font},{rcfg.get('caption_size', 50)},{white},{white},&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,4,2,5,80,80,0,1",
        f"Style: Callout,{font},{rcfg.get('callout_size', 64)},{white},{white},&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,5,3,5,70,70,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    busy = []
    for c in callouts:
        t0, t1 = c["at"], min(dur, c["at"] + c.get("dur", 1.6))
        if t1 <= t0:
            continue
        busy.append((t0 - 0.1, t1 + 0.1))
        col = "&H" + hex_to_ass(CALLOUT_COLORS.get(str(c.get("color", "yellow")).lower(), "#FFE135"))[4:]  # \c takes BBGGRR
        out.append(f"Dialogue: 2,{fmt_ts(t0)},{fmt_ts(t1)},Callout,,0,0,0,,"
                   + r"{\pos(540," + str(cap_y - 10) + r")\c" + col + r"&\fscx70\fscy70\t(0,120,\fscx104\fscy104)"
                   + r"\t(120,200,\fscx100\fscy100)}" + _esc(censor(c["text"])))
    if rcfg.get("captions", "mini") in ("mini", True):
        groups, cur = [], []
        for w in words:
            if cur and (len(cur) >= 3 or w["start"] - cur[-1]["end"] > 0.45 or cur[-1]["word"][-1:] in ".?!,"):
                groups.append(cur)
                cur = []
            cur.append(w)
        if cur:
            groups.append(cur)
        for gi, g in enumerate(groups):
            t0 = g[0]["start"]
            t1 = min(g[-1]["end"] + 0.2, groups[gi + 1][0]["start"] if gi + 1 < len(groups) else dur, dur)
            if t1 - t0 < 0.15 or any(a < t1 and t0 < b for a, b in busy):
                continue
            text = censor(" ".join(w["word"] for w in g)).strip(" ,.").lower()
            if text:
                out.append(f"Dialogue: 1,{fmt_ts(t0)},{fmt_ts(t1)},Mini,,0,0,0,,"
                           + r"{\pos(540," + str(cap_y) + r")}" + _esc(text))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- bold captions (caption_style: bold)
FILLERS = {"uh", "uhh", "uhm", "um", "umm", "erm", "er", "ah", "hmm", "mm", "mhm"}


def drop_fillers(words):
    """Remove filler words (uh, um... and 'like' used as a filler: 'like,' or 'like like')."""
    out = []
    for i, w in enumerate(words):
        raw = str(w["word"]).strip()
        core = raw.lower().strip(" ,.!?…\"'")
        if core in FILLERS:
            continue
        nxt = str(words[i + 1]["word"]).strip().lower().strip(" ,.!?") if i + 1 < len(words) else ""
        if core == "like" and (raw.endswith(",") or nxt == "like"):
            continue
        out.append(w)
    return out


def _fit_size(text, base, width=820):
    """Font size so 1-3 ALL CAPS words fit inside the safe width (Montserrat ExtraBold ~0.66 em per char)."""
    return int(max(60, min(base, width / (0.66 * max(1, len(text))))))


def build_ass_bold(words, callouts, dur, rcfg, layout, seam_y, punch_words=None, hook_text=None, hook_end=0.0):
    """1-3 words at a time, ALL CAPS, ~100 px, thick black outline + shadow, on the line between webcam and gameplay
    (under the title), clear of the TikTok / Reels / Shorts buttons (right side) and the bottom 20%.
    The spoken word pops to 115% (karaoke timing); 1-2 punch words in the highlight colour, a bit bigger.
    Fillers removed, slurs and swears masked. hook_text: shown big during the first seconds (0 -> hook_end)."""
    font = rcfg.get("caption_font", "Montserrat ExtraBold")
    base = int(rcfg.get("bold_caption_size", 100))
    white = hex_to_ass("#FFFFFF")
    hl = "&H" + hex_to_ass(rcfg.get("highlight_color", "#FFD60A"))[4:]
    cx = 500                                         # a little left of centre: the right side holds the app buttons
    if layout == "reel":
        cap_y = int(seam_y + 150)                    # just under the title on the seam
    else:
        cap_y = int(H * 0.36)
    cap_y = min(cap_y, int(H * 0.80) - base)         # never in the bottom 20 %
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Big,{font},{base},{white},{white},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,9,4,5,80,180,0,1",
        f"Style: Hook,{font},{base + 10},{white},{white},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,10,4,5,60,160,0,1",
        f"Style: Callout,{font},{rcfg.get('callout_size', 64)},{white},{white},&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,6,3,5,70,170,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    busy = []
    if hook_text and hook_end > 0.3:
        t1 = min(dur, hook_end)
        txt = _esc(censor(hook_text)).upper()
        size = _fit_size(txt, base + 10, 900) if len(txt) <= 26 else _fit_size(txt[: len(txt) // 2 + 4], base + 10, 900)
        out.append(f"Dialogue: 3,{fmt_ts(0)},{fmt_ts(t1)},Hook,,0,0,0,,"
                   + r"{\pos(" + f"{cx},{cap_y}" + r")\fs" + str(size) + r"\fscx90\fscy90\t(0,120,\fscx104\fscy104)"
                   + r"\t(120,200,\fscx100\fscy100)}" + txt.replace(" ", r"\N", 1 if len(txt) > 18 else 0))
        busy.append((0.0, t1))
    for c in callouts:
        t0, t1 = c["at"], min(dur, c["at"] + c.get("dur", 1.6))
        if t1 <= t0:
            continue
        busy.append((t0 - 0.1, t1 + 0.1))
        col = "&H" + hex_to_ass(CALLOUT_COLORS.get(str(c.get("color", "yellow")).lower(), "#FFE135"))[4:]
        out.append(f"Dialogue: 2,{fmt_ts(t0)},{fmt_ts(t1)},Callout,,0,0,0,,"
                   + r"{\pos(" + f"{cx},{cap_y - 150}" + r")\c" + col + r"&\fscx70\fscy70\t(0,120,\fscx104\fscy104)"
                   + r"\t(120,200,\fscx100\fscy100)}" + _esc(censor(c["text"])).upper())
    punch = {p.lower().strip(" .,!?\"'") for p in (punch_words or []) if p}
    words = drop_fillers(words)
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or w["start"] - cur[-1]["end"] > 0.45 or cur[-1]["word"].strip()[-1:] in ".?!,"):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    for gi, g in enumerate(groups):
        t0 = g[0]["start"]
        t1 = min(g[-1]["end"] + 0.15, groups[gi + 1][0]["start"] if gi + 1 < len(groups) else dur, dur)
        if t1 - t0 < 0.15 or any(a < t1 and t0 < b for a, b in busy):
            continue
        toks = [_esc(censor(w["word"].strip())).upper().strip(",") for w in g]
        line_txt = " ".join(toks)
        if not line_txt.strip():
            continue
        size = _fit_size(line_txt, base)
        parts = []
        for w, tok in zip(g, toks):
            s = max(0, int((w["start"] - t0) * 1000))
            e = max(s + 80, int((min(w["end"], t1) - t0) * 1000))
            is_punch = tok.lower().strip(" .,!?\"'") in punch
            scale = 118 if is_punch else 100
            pop = int(round(scale * 1.15))
            col = r"\c" + (hl if is_punch else "&HFFFFFF") + "&"     # explicit colour per word (\r would drop \fs)
            parts.append(r"{" + col + rf"\fscx{scale}\fscy{scale}\t({s},{s + 70},\fscx{pop}\fscy{pop})"
                         + rf"\t({e - 60},{e},\fscx{scale}\fscy{scale})" + r"}" + tok)
        out.append(f"Dialogue: 1,{fmt_ts(t0)},{fmt_ts(t1)},Big,,0,0,0,,"
                   + r"{\pos(" + f"{cx},{cap_y}" + r")\fs" + str(size) + r"}" + " ".join(parts))
    return "\n".join(out) + "\n"
