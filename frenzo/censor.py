"""Nudity blur: keep clips that show nudity, but pixelate it.

How a clip is censored (after it is rendered once):
  1. a nudity detector (NudeNet, on the CPU) scans the finished clip a few times per second and gives boxes around
     exposed body parts; each box is made bigger and held a little longer, so a missed frame stays covered,
  2. Claude's own answer says WHEN nudity is visible (nudity_at); for those seconds, if the detector didn't cover
     anything, the whole gameplay area is pixelated (the fail-safe: GTA's graphics can fool the detector),
  3. the clip is rendered again with those spots pixelated, on every version.
config.yaml  clips: nudity: blur | skip | keep   (blur is the default)
"""
import os
import shutil
import subprocess
import tempfile

from .util import NO_WINDOW, log

EXPOSED = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "BUTTOCKS_EXPOSED",
           "ANUS_EXPOSED"}
W, H = 1080, 1920
_detector = {}


def mode(cfg):
    """skip / blur / keep (old configs: skip_nudity: false = keep)."""
    m = str(cfg["clips"].get("nudity") or "blur").lower()
    if m not in ("skip", "blur", "keep"):
        m = "blur"
    return m


def gameplay_box(layout):
    """Where the gameplay is in the vertical video (the fail-safe blurs all of it)."""
    from .editor import REEL_CAM_H
    if layout == "reel":
        return (0, REEL_CAM_H, W, H - REEL_CAM_H)
    return (0, 0, W, H)


def _get_detector():
    if "d" not in _detector:
        try:
            from nudenet import NudeDetector
            _detector["d"] = NudeDetector()
        except Exception as e:                       # not installed / model problem: the fail-safe still works
            log(f"nudity detector unavailable ({str(e)[:80]}): using Claude's times only", "censor")
            _detector["d"] = None
    return _detector["d"]


def detect(video, fps=4, threshold=0.25):
    """[(second, [(x, y, w, h), ...]), ...] for every sampled frame of the video with exposed nudity."""
    det = _get_detector()
    if det is None:
        return None
    work = tempfile.mkdtemp(prefix="frenzo_censor_")
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", video, "-vf", f"fps={fps}",
                        "-q:v", "3", os.path.join(work, "f_%05d.jpg")], creationflags=NO_WINDOW, check=True)
        out = []
        for name in sorted(os.listdir(work)):
            t = (int(name[2:7]) - 1) / fps + 0.5 / fps
            boxes = [tuple(int(v) for v in x["box"]) for x in det.detect(os.path.join(work, name))
                     if x.get("class") in EXPOSED and x.get("score", 0) >= threshold]
            if boxes:
                out.append((round(t, 2), boxes))
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _pad(box, k=0.45, minimum=140):
    x, y, w, h = box
    pw, ph = max(w * (1 + 2 * k), minimum), max(h * (1 + 2 * k), minimum)
    cx, cy = x + w / 2, y + h / 2
    x0, y0 = max(0, cx - pw / 2), max(0, cy - ph / 2)
    x1, y1 = min(W, cx + pw / 2), min(H, cy + ph / 2)
    return [x0, y0, x1, y1]


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def regions(dets, claude_ranges, duration, layout, hold=0.5, max_regions=40):
    """Spots to pixelate: [(t0, t1, x, y, w, h)] in the clip's own timeline and pixels."""
    spots = []                                       # [t0, t1, x0, y0, x1, y1]
    for t, boxes in dets or []:
        for b in boxes:
            r = [max(0.0, t - hold), min(duration, t + hold)] + _pad(b)
            for s in spots:                          # same spot a moment later: extend it
                if s[1] >= r[0] - 0.3 and _overlap(s[2:], r[2:]):
                    s[1] = max(s[1], r[1])
                    s[2:] = [min(s[2], r[2]), min(s[3], r[3]), max(s[4], r[4]), max(s[5], r[5])]
                    break
            else:
                spots.append(r)
    detector_spots = len(spots)
    gx, gy, gw, gh = gameplay_box(layout)
    fallback = 0
    for a, b in claude_ranges or []:                 # Claude saw nudity here: if nothing covers it, blur the gameplay
        a, b = max(0.0, a - 0.5), min(duration, b + 0.5)
        if b - a < 0.2:
            continue
        covered = sum(max(0.0, min(b, s[1]) - max(a, s[0])) for s in spots)
        if covered < 0.6 * (b - a):
            spots.append([a, b, gx, gy, gx + gw, gy + gh])
            fallback += 1
    while len(spots) > max_regions:                  # too many: merge the two closest in time
        spots.sort(key=lambda s: s[0])
        i = min(range(len(spots) - 1), key=lambda j: spots[j + 1][0] - spots[j][1])
        a, b = spots[i], spots.pop(i + 1)
        spots[i] = [a[0], max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]), max(a[4], b[4]), max(a[5], b[5])]
    out = []
    for t0, t1, x0, y0, x1, y1 in sorted(spots):
        x, y = int(x0) // 2 * 2, int(y0) // 2 * 2
        w, h = max(16, int(x1 - x) // 2 * 2), max(16, int(y1 - y) // 2 * 2)
        w, h = min(w, W - x), min(h, H - y)
        out.append((round(t0, 2), round(t1, 2), x, y, w, h))
    return out, detector_spots, fallback


def filter_graph(regions_, src, dst):
    """ffmpeg filter text: pixelate each spot (mosaic) only while it's on screen."""
    if not regions_:
        return f";{src}null{dst}"
    g, cur = "", src
    for i, (t0, t1, x, y, w, h) in enumerate(regions_):
        nxt = dst if i == len(regions_) - 1 else f"[cz{i}]"
        block = 48                                    # big mosaic blocks (~4.5% of the video width): nothing readable
        bw, bh = max(2, w // block), max(2, h // block)
        g += (f";{cur}split[cza{i}][czb{i}];[czb{i}]crop={w}:{h}:{x}:{y},scale={bw}:{bh},"
              f"scale={w}:{h}:flags=neighbor[czp{i}];[cza{i}][czp{i}]overlay={x}:{y}:enable='between(t,{t0},{t1})'{nxt}")
        cur = nxt
    return g


def map_ranges(ranges, to_out):
    """Claude's nudity_at (window seconds) -> the clip's timeline (after jump cuts)."""
    out = []
    for r in ranges or []:
        try:
            a, b = float(r[0]), float(r[1])
        except (TypeError, ValueError, IndexError):
            continue
        pts = [to_out(a + (b - a) * k / 10) for k in range(11)]
        pts = [p for p in pts if p is not None]
        if pts:
            out.append((min(pts), max(pts)))
    return out


def plan(clean_video, claude_ranges, duration, layout, chosen):
    """Detect + build the spots. chosen: you picked this clip (never skipped: if nothing is found, the whole
    gameplay is pixelated). Returns (regions, info) or (None, info) when an automatic clip should be skipped."""
    dets = detect(clean_video)
    regs, n_det, n_fb = regions(dets, claude_ranges, duration, layout)
    info = {"detector_spots": n_det, "claude_spots": n_fb, "detector": dets is not None}
    if not regs:
        if not chosen:
            return None, info                       # nothing found anywhere: don't risk it
        gx, gy, gw, gh = gameplay_box(layout)
        regs = [(0.0, round(duration, 2), gx, gy, gw, gh)]
        info["whole_clip"] = True
    return regs, info
