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
