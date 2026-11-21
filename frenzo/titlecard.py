"""The title on the line between the webcam and the gameplay, like viral clip pages do it:
short sentence-case text, ONE keyword in yellow, a coloured emoji at the end, white with a black outline.
Example: "Insane GTA 6 bug 💀" with "GTA 6" in yellow.
Drawn with Pillow because the subtitle renderer can't draw colour emoji."""
import os
import re

from PIL import Image, ImageDraw, ImageFont

from .config import ROOT

FONTS = ROOT / "assets" / "fonts"
YELLOW = (255, 225, 53, 255)
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+")


def _font(size, weight="ExtraBold"):
    for p in (FONTS / f"Montserrat-{weight}.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def _emoji_img(emoji, height):
    for p in (FONTS / "NotoColorEmoji.ttf", "C:/Windows/Fonts/seguiemj.ttf"):
        if not os.path.exists(p):
            continue
        try:
            size = 109 if "Noto" in str(p) else height
            f = ImageFont.truetype(str(p), size)
            box = f.getbbox(emoji)
            img = Image.new("RGBA", (box[2] + 10, box[3] + 10), (0, 0, 0, 0))
            ImageDraw.Draw(img).text((5 - box[0], 5 - box[1]), emoji, font=f, embedded_color=True)
            bbox = img.getbbox()
            if not bbox:
                continue
            img = img.crop(bbox)
            return img.resize((max(1, round(img.width * height / img.height)), height), Image.LANCZOS)
        except Exception:
            continue
    return None


def _tokens(text, highlight):
    """Split text into (word, is_highlighted)."""
    text = text.strip()
    if highlight and highlight.lower() in text.lower():
        i = text.lower().index(highlight.lower())
        parts = [(text[:i], False), (text[i:i + len(highlight)], True), (text[i + len(highlight):], False)]
    else:
        parts = [(text, False)]
    out = []
    for chunk, hl in parts:
        for w in chunk.split():
            out.append((w, hl))
    return out


def render_title(text, highlight, emoji, out_path, max_width=1000, size=58):
    """Write a transparent PNG of the title. Returns (width, height) or None if there's no text."""
    text = EMOJI_RE.sub("", text or "").strip()
    emoji = "".join(EMOJI_RE.findall(emoji or "")) or None
    if not text:
        return None
    for s in range(size, 30, -4):
        font = _font(s)
        space = font.getlength(" ")
        stroke = max(3, s // 11)
        emo = _emoji_img(emoji, int(s * 1.05)) if emoji else None
        words = _tokens(text, highlight)
        lines, cur, cur_w = [], [], 0.0
        for w, hl in words:
            ww = font.getlength(w)
            if cur and cur_w + space + ww > max_width:
                lines.append(cur)
                cur, cur_w = [], 0.0
            cur.append((w, hl, ww))
            cur_w += (space if len(cur) > 1 else 0) + ww
        if cur:
            lines.append(cur)
        widths = [sum(x[2] for x in l) + space * (len(l) - 1) for l in lines]
        if emo:
            widths[-1] += space * 0.6 + emo.width
        if len(lines) <= 2 and max(widths) <= max_width:
            break
    asc, desc = font.getmetrics()
    line_h = asc + desc
    W = int(max(widths) + stroke * 2 + 8)
    H = int(line_h * len(lines) + (len(lines) - 1) * 6 + stroke * 2 + 8)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d, ds = ImageDraw.Draw(img), ImageDraw.Draw(shadow)
    y = stroke + 4
    for li, line in enumerate(lines):
        x = (W - widths[li]) / 2
        for w, hl, ww in line:
            ds.text((x + 3, y + 4), w, font=font, fill=(0, 0, 0, 150), stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
            d.text((x, y), w, font=font, fill=YELLOW if hl else WHITE, stroke_width=stroke, stroke_fill=BLACK)
            x += ww + space
        if emo and li == len(lines) - 1:
            img.alpha_composite(emo, (int(x - space + space * 0.6), int(y + (asc - emo.height) / 2 + desc * 0.35)))
        y += line_h + 6
    final = Image.alpha_composite(shadow, img)
    final.save(out_path)
    return final.size
