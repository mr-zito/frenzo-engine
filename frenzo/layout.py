"""Finds where the streamer's webcam is and where the action is in the gameplay."""
import subprocess

import numpy as np

from .util import log

CAM_ASPECT = 1080 / 640       # shape of the webcam band at the top of the reel
GAME_ASPECT = 1080 / 1280     # shape of the gameplay band under it


def _probe_size(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode()
    first = next(l for l in out.splitlines() if "," in l)
    w, h = (int(x) for x in first.strip().split(",")[:2])
    return w, h


def _gray_frames(path, start, dur, fps, width):
    """Grayscale frames as a numpy array (n, h, width)."""
    w, h = _probe_size(path)
    height = int(round(width * h / w / 2)) * 2
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
         "-i", path, "-vf", f"fps={fps},scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    n = len(raw) // (width * height)
    if n == 0:
        return np.zeros((0, height, width), np.uint8), (w, h)
    return np.frombuffer(raw[: n * width * height], np.uint8).reshape(n, height, width), (w, h)


def face_clusters(path, start, dur):
    """Faces that stay in the same place across ~20 frames. Returns (clusters, frames, W, H).
    Each cluster: {"box": (x, y, w, h) in pixels of the full frame, "share": fraction of frames}."""
    import cv2
    frames, (W, H) = _gray_frames(path, start, dur, fps=20 / max(dur, 1), width=1280)
    if len(frames) == 0:
        return [], frames, W, H
    sx, sy = W / frames.shape[2], H / frames.shape[1]
    front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    dets = []
    for i, f in enumerate(frames):
        f = cv2.equalizeHist(f)
        found = list(front.detectMultiScale(f, 1.08, 5, minSize=(22, 22)))
        found += list(profile.detectMultiScale(f, 1.08, 5, minSize=(22, 22)))
        found += [(f.shape[1] - x - w, y, w, h) for (x, y, w, h)
                  in profile.detectMultiScale(cv2.flip(f, 1), 1.08, 5, minSize=(22, 22))]
        dets += [(i, x * sx, y * sy, w * sx, h * sy) for (x, y, w, h) in found]
    clusters, used = [], set()
    for k, (_, x, y, w, h) in sorted(enumerate(dets), key=lambda e: -e[1][3]):
        if k in used:
            continue
        cx, cy = x + w / 2, y + h / 2
        members = [j for j, d in enumerate(dets) if j not in used
                   and abs(d[1] + d[3] / 2 - cx) < w * 0.6 and abs(d[2] + d[4] / 2 - cy) < h * 0.6]
        used.update(members)
        group = [dets[j] for j in members]
        share = len({d[0] for d in group}) / len(frames)
        box = tuple(float(np.median([d[i] for d in group])) for i in (1, 2, 3, 4))
        clusters.append({"box": box, "share": share})
    clusters.sort(key=lambda c: -c["share"])
    return clusters, frames, W, H


def in_character_zone(box, W, H):
    """Third-person games keep the player character around the middle of the screen."""
    cx, cy = (box[0] + box[2] / 2) / W, (box[1] + box[3] / 2) / H
    return 0.28 < cx < 0.72 and 0.2 < cy < 0.9


def overlay_box(frames, face, W, H):
    """Find the webcam overlay's own frame around a face. Its border is a straight edge that stays in
    place in every frame, with MOVING gameplay on the outside and the streamer's (mostly still) room on
    the inside. Static edges inside the room (furniture, windows) don't have gameplay moving behind
    them, so they're skipped. Returns (x, y, w, h) in pixels, or None."""
    import cv2
    if len(frames) < 4:
        return None
    sx, sy = frames.shape[2] / W, frames.shape[1] / H
    persist = np.mean([cv2.Canny(f, 40, 120) > 0 for f in frames], axis=0)
    persist = np.maximum.reduce([np.roll(np.roll(persist, dy, 0), dx, 1) for dy in (-1, 0, 1) for dx in (-1, 0, 1)])
    motion = frames.astype(np.float32).std(axis=0)
    h, w = persist.shape
    fx, fy, fw, fh = face[0] * sx, face[1] * sy, face[2] * sx, face[3] * sy
    cx, cy = fx + fw / 2, fy + fh / 2
    y0, y1 = int(max(0, cy - fh * 0.8)), int(min(h, cy + fh * 1.2))
    x0, x1 = int(max(0, cx - fw * 0.8)), int(min(w, cx + fw * 0.8))
    gap, band = 3, 10

    def best(candidates, strength, out_in):
        scored = []
        for v in candidates:
            if strength(v) > 0.55:
                o, i = out_in(v)
                if o > i + 4:
                    scored.append((o - i, v))
        return max(scored)[1] if scored else None

    def col_str(x):
        return float((persist[y0:y1, x] > 0.6).mean())

    def row_str(y):
        return float((persist[y, x0:x1] > 0.6).mean())

    def lr(sign):
        def f(x):
            out = motion[y0:y1, max(0, x + sign * (gap + band)):max(0, x + sign * gap)] if sign < 0 else \
                motion[y0:y1, min(w, x + gap):min(w, x + gap + band)]
            inn = motion[y0:y1, min(w, x + gap):min(w, x + gap + band)] if sign < 0 else \
                motion[y0:y1, max(0, x - gap - band):max(0, x - gap)]
            return (float(out.mean()) if out.size else 0.0), (float(inn.mean()) if inn.size else 0.0)
        return f

    def tb(sign):
        def f(y):
            out = motion[max(0, y - gap - band):max(0, y - gap), x0:x1] if sign < 0 else \
                motion[min(h, y + gap):min(h, y + gap + band), x0:x1]
            inn = motion[min(h, y + gap):min(h, y + gap + band), x0:x1] if sign < 0 else \
                motion[max(0, y - gap - band):max(0, y - gap), x0:x1]
            return (float(out.mean()) if out.size else 0.0), (float(inn.mean()) if inn.size else 0.0)
        return f

    reach_x, reach_y = fw * 8, fh * 7
    left = best(range(int(fx - fw * 0.3), max(gap + band, int(cx - reach_x)), -1), col_str, lr(-1))
    right = best(range(int(fx + fw * 1.3), min(w - gap - band, int(cx + reach_x))), col_str, lr(1))
    top = best(range(int(fy - fh * 0.3), max(gap + band, int(cy - reach_y)), -1), row_str, tb(-1))
    bottom = best(range(int(fy + fh * 1.5), min(h - gap - band, int(cy + reach_y))), row_str, tb(1))
    # a side with no border found: the webcam touches the screen edge if the face is close to it
    left = left if left is not None else (0 if cx < reach_x else None)
    right = right if right is not None else (w - 1 if w - cx < reach_x else None)
    top = top if top is not None else (0 if cy < reach_y else None)
    bottom = bottom if bottom is not None else (h - 1 if h - cy < reach_y else None)
    if None in (left, right, top, bottom):
        return None
    bw, bh = right - left, bottom - top
    if not (fw * 1.8 <= bw <= fw * 14 and fh * 1.6 <= bh <= fh * 10 and bw * bh <= 0.35 * w * h):
        return None
    return ((left + 2) / sx, (top + 2) / sy, (bw - 4) / sx, (bh - 4) / sy)


def cam_from_face(face, frames, W, H):
    """Webcam crop (fractions) around a face: exactly inside the webcam's own frame when it can be found,
    otherwise a box around the head and shoulders. The person is centred horizontally."""
    ov = overlay_box(frames, face, W, H)
    fx, fy, fw, fh = face
    if ov:
        ox, oy, ow, oh = ov
        oh_t = min(oh, fh * 3.0)          # viral clips crop tight: the face fills about half the band
        ow_t = min(ow, oh_t * CAM_ASPECT)
        cw, ch = ow_t, oh_t
        ox2 = min(max(ox, fx + fw / 2 - cw / 2), ox + ow - cw)
        oy2 = min(max(oy, fy + fh / 2 - ch * 0.42), oy + oh - ch)
        ox, oy, ow, oh = ox2, oy2, cw, ch
        if ow / oh > CAM_ASPECT:          # overlay wider than the reel band: trim the sides around the face
            cw = oh * CAM_ASPECT
        else:                             # taller: trim top/bottom, keep the head in the upper middle
            ch = ow / CAM_ASPECT
        x = min(max(ox, fx + fw / 2 - cw / 2), ox + ow - cw)
        y = min(max(oy, fy + fh / 2 - ch * 0.42), oy + oh - ch)
        return {"x": x / W, "y": y / H, "w": cw / W, "h": ch / H, "how": "webcam frame"}
    cam = head_box_to_cam(fx / W, fy / H, fw / W, fh / H, W, H)
    cam["how"] = "around the face"
    return cam


def detect_facecam(path, start, dur, locate=None):
    """Return the webcam crop {x,y,w,h} as fractions of the frame, or None.

    locate: optional function(jpeg frames) -> head box fractions / {"found": False} / None (unknown),
    i.e. Claude looking at the stream. When Claude answers, its answer decides; the face scan is then
    only used to make the position precise. Without Claude, faces in the middle of the screen (where the
    player character stands) are ignored.
    """
    try:
        import cv2  # noqa: F401
    except ImportError:
        log("opencv not installed: can't find the webcam automatically", "layout")
        return None
    clusters, frames, W, H = face_clusters(path, start, dur)
    if locate:
        head = locate()
        if isinstance(head, dict) and head.get("found") is False:
            return None
        if head:
            hx, hy, hw, hh = head["x"] * W, head["y"] * H, head["w"] * W, head["h"] * H
            hcx, hcy = hx + hw / 2, hy + hh / 2
            near = [c for c in clusters if c["share"] >= 0.15
                    and abs(c["box"][0] + c["box"][2] / 2 - hcx) < max(hw, c["box"][2]) * 1.5
                    and abs(c["box"][1] + c["box"][3] / 2 - hcy) < max(hh, c["box"][3]) * 1.5]
            face = near[0]["box"] if near else (hx, hy, hw, hh)
            cam = cam_from_face(face, frames, W, H)
            cam["how"] = "Claude + " + ("face scan, " if near else "") + cam["how"]
            return cam
    # no Claude: trust only faces that stay put AND are not where the player character stands
    good = [c for c in clusters if c["share"] >= 0.25 and not in_character_zone(c["box"], W, H)
            and c["box"][2] <= W * 0.22]
    for c in good:
        cam = cam_from_face(c["box"], frames, W, H)
        if cam["how"] == "webcam frame":      # without Claude, only trust a face inside a real webcam frame
            cam["how"] = "face scan, webcam frame"
            return cam
    return None


def head_box_to_cam(fx, fy, fw, fh, W=1920, H=1080):
    """Head box (fractions) -> webcam crop (fractions) with the reel's webcam shape."""
    fx, fy, fw, fh = fx * W, fy * H, fw * W, fh * H
    ch = min(H, fh * 2.5)
    cw = min(W, ch * CAM_ASPECT)
    ch = cw / CAM_ASPECT
    cx = fx + fw / 2
    cy = fy + fh / 2 + ch * 0.12          # a bit lower, so shoulders show and the head isn't cut
    x = min(max(0.0, cx - cw / 2), W - cw)
    y = min(max(0.0, cy - ch / 2), H - ch)
    return {"x": x / W, "y": y / H, "w": cw / W, "h": ch / H}


def action_focus(path, start, dur, crop_w_frac, cam=None):
    """Horizontal centre (0-1) of the gameplay crop.

    In third-person games the camera follows the player's character or vehicle, which therefore stays
    near the middle of the screen. Stream overlays (chat boxes, alerts, donation pop-ups) change a lot
    and used to pull the crop off the character, so they are masked out, and the result is kept close
    to the centre unless the action is clearly elsewhere."""
    import cv2
    frames, _ = _gray_frames(path, start, dur, fps=2, width=192)
    if len(frames) < 3:
        return 0.5
    f = frames.astype(np.float32)
    motion = np.abs(np.diff(f, axis=0)).mean(axis=0)
    h, w = motion.shape
    motion[: int(h * 0.12)] = 0
    motion[int(h * 0.88):] = 0
    # overlays: areas full of sharp edges that stay put (text boxes, alert frames) -> ignore them
    persist = np.mean([cv2.Canny(x, 60, 160) > 0 for x in frames], axis=0)
    overlay = cv2.dilate((persist > 0.5).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    motion[overlay] = 0
    if cam:
        x0, y0 = int(cam["x"] * w), int(cam["y"] * h)
        motion[max(0, y0 - 2): y0 + int(cam["h"] * h) + 2, max(0, x0 - 2): x0 + int(cam["w"] * w) + 2] = 0
    cols = motion.sum(axis=0)
    xs = (np.arange(w) + 0.5) / w
    cols = cols * np.exp(-((xs - 0.5) / 0.18) ** 2)            # the character is near the middle
    if cols.sum() <= 0:
        return 0.5
    centroid = float((cols * xs).sum() / cols.sum())
    return float(min(0.62, max(0.38, 0.5 + 0.6 * (centroid - 0.5))))
