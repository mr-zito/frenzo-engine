"""Turns a moment into a 1080x1920 vertical clip.

Layouts:
  reel    (default) streamer webcam on top, a heading explaining the moment, gameplay below zoomed
          on the action. No subtitles. Webcam is found automatically (or set with 'facecam').
  focus   no webcam found: full-height gameplay crop on the action, heading at the top.
  stacked / blur  the older styles (with word-by-word subtitles if 'captions' is on).
"""
import os
import re

from .util import fmt_ts, hex_to_ass, run

W, H = 1080, 1920
REEL_CAM_H = 640                  # webcam band (top third)
REEL_GAME_H = H - REEL_CAM_H      # gameplay band
OLD_CAM_H = 700


def _esc(text):
    return re.sub(r"[{}\\]", "", text or "").replace("\n", " ").strip()


def group_words(words, max_words=3, gap=0.5):
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["start"] - cur[-1]["end"] > gap
                    or cur[-1]["word"][-1:] in ".?!,"):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build_ass(words, clip_start, clip_dur, decision, rcfg, use_text_tag, layout):
    """words: dicts with start/end relative to the analysis window."""
    white = hex_to_ass(rcfg["caption_color"])
    yellow = hex_to_ass(rcfg["highlight_color"])
    black = "&H000C0B0B"
    font = rcfg["font"]
    reel = layout in ("reel", "focus")
    head_y = REEL_CAM_H + 26 if layout == "reel" else 250
    tag_align, tag_v = (2, 330) if reel else (8, 190)
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Heading,{font},{rcfg.get('heading_size', 64)},{white},{white},{black},&H64000000,-1,0,0,0,100,100,0,0,1,6,2,8,70,70,{head_y},1",
        f"Style: Caption,{font},86,{white},{white},{black},&H96000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,560,1",
        f"Style: Hook,{font},68,{black},{black},{yellow},{yellow},-1,0,0,0,100,100,0,0,3,20,0,8,90,90,330,1",
        f"Style: Tag,{font},40,&H30{yellow[4:]},&H30{yellow[4:]},&H60000000,&H96000000,-1,0,0,0,100,100,0,0,1,3,1,{tag_align},60,60,{tag_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if use_text_tag and rcfg.get("handle"):
        out.append(f"Dialogue: 1,{fmt_ts(0)},{fmt_ts(clip_dur)},Tag,,0,0,0,,{_esc(rcfg['handle'])}")
    heading = _esc(decision.get("heading") or decision.get("hook_text") or "")
    if reel:
        if heading:
            out.append(f"Dialogue: 2,{fmt_ts(0)},{fmt_ts(clip_dur)},Heading,,0,0,0,,"
                       + r"{\fad(150,0)}" + heading.upper())
    elif decision.get("hook_text"):
        out.append(f"Dialogue: 2,{fmt_ts(0)},{fmt_ts(min(2.8, clip_dur))},Hook,,0,0,0,,"
                   r"{\fad(0,250)}" + _esc(decision["hook_text"]).upper())
    if not rcfg.get("captions", False):
        return "\n".join(out) + "\n"
    clip_words = []
    for w in words:
        s, e = w["start"] - clip_start, w["end"] - clip_start
        if e <= 0 or s >= clip_dur:
            continue
        clip_words.append({"start": max(0.0, s), "end": min(clip_dur, e), "word": w["word"]})
    groups = group_words(clip_words)
    for gi, g in enumerate(groups):
        next_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else clip_dur
        g_end = min(g[-1]["end"] + 0.25, next_start)
        tokens = [_esc(w["word"].upper()) for w in g]
        for i, w in enumerate(g):
            start = w["start"] if i else max(0.0, w["start"] - 0.05)
            end = g[i + 1]["start"] if i + 1 < len(g) else g_end
            if end <= start:
                continue
            parts = [(r"{\c&H" + yellow[4:] + r"&\fscx108\fscy108}" + tok + r"{\r}") if j == i else tok
                     for j, tok in enumerate(tokens)]
            pop = r"{\fscx92\fscy92\t(0,90,\fscx100\fscy100)}" if i == 0 else ""
            out.append(f"Dialogue: 0,{fmt_ts(start)},{fmt_ts(end)},Caption,,0,0,0,,{pop}{' '.join(parts)}")
    return "\n".join(out) + "\n"


def handle_groups(rcfg):
    """{"tiktok": "@a", "instagram": "@a", "x": "@b"} -> [("tiktok+instagram", "@a"), ("x", "@b")]"""
    groups = {}
    for platform, handle in (rcfg.get("handles") or {}).items():
        handle = str(handle or "").strip()
        if handle:
            handle = handle if handle.startswith("@") else "@" + handle
            groups.setdefault(handle, []).append(str(platform).lower())
    return [("+".join(p), h) for h, p in groups.items()]


def tag_ass(handle, layout, rcfg):
    yellow = hex_to_ass(rcfg["highlight_color"])
    align, margin_v = (2, 330) if layout in ("reel", "focus") else (8, 190)
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # yellow text on a dark rounded-looking box, like the brand kit's handle tag
        f"Style: Tag,{rcfg['font']},40,{yellow},{yellow},&H38000000,&H38000000,-1,0,0,0,100,100,1,0,3,12,0,"
        f"{align},60,60,{margin_v},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 0,{fmt_ts(0)},{fmt_ts(36000)},Tag,,0,0,0,,{_esc(handle)}",
    ]) + "\n"


def _windows(ranges):
    if not ranges:
        return "0"
    return "min(1," + "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges) + ")"


def punch_filter(fx_events, w, h, hook_zoom=False, band="game"):
    """Picture effects on one band (the gameplay band, or the webcam band with band="cam"), at the sound cues:
    zoom = punch-in that eases out, shake, flash, slow_zoom = slow zoom-in while a suspense sound plays,
    zoom_hold = zoomed in during a pause (anime-wow), cam_zoom = zoom on the webcam (awkward / staring at camera).
    hook_zoom = frame 0 starts at 1.1x and eases to 1.0 in half a second. Events: (t, [fx], seconds).
    Returns '' when there is nothing to do, else ',filter,filter...'."""
    zt, shakes, flashes = [], [], []
    for ev in fx_events or []:
        t, fx = float(ev[0]), ev[1]
        d = max(0.4, min(4.0, float(ev[2]))) if len(ev) > 2 and ev[2] else 1.0
        if band == "cam":
            if "cam_zoom" in fx:                      # ease in over 0.2 s, hold while the sound plays
                zt.append(f"0.18*min(1,(t-{t:.3f})/0.2)*between(t,{t:.3f},{t + d:.3f})")
            continue
        if "slow_zoom" in fx:                         # ramps up to 1.18x over the suspense sound
            zt.append(f"0.18*min(1,max(0,(t-{t - d:.3f})/{d:.3f}))*between(t,{t - d:.3f},{t + 0.4:.3f})")
        if "zoom_hold" in fx:
            zt.append(f"0.3*min(1,(t-{t:.3f})/0.15)*between(t,{t:.3f},{t + d:.3f})")
        if "zoom" in fx:
            zt.append(f"0.12*between(t,{t:.3f},{t + 0.6:.3f})*exp(-(t-{t:.3f})*7)")
        if "shake" in fx:
            shakes.append((t, t + 0.35))
        if "flash" in fx:
            flashes.append((t, t + 0.06))
    if hook_zoom and band == "game":
        zt.append("0.1*max(0,1-t/0.5)")
    if not zt and not shakes and not flashes:
        return ""
    S = _windows(shakes)
    if shakes:
        zt.append(f"0.06*{S}")                       # room around the picture for the shake
    Z = "+".join(zt) or "0"
    out = (f",scale=w='trunc({w}*(1+{Z})/2)*2':h='trunc({h}*(1+{Z})/2)*2':eval=frame:flags=bicubic,"
           f"crop={w}:{h}:x='(iw-{w})/2+20*sin(t*83)*{S}':y='(ih-{h})/2+20*cos(t*71)*{S}'")
    if flashes:
        out += f",drawbox=x=0:y=0:w=iw:h=ih:color=white@0.7:t=fill:enable='{_windows(flashes)}'"
    return out


def video_filter(layout, cam, focus_x, zoom=1.0, src="[0:v]", fx_events=None, hook_zoom=False, cam_only_until=0.0):
    """cam_only_until: during the first N seconds only the webcam is shown, full screen (the reaction-first hook:
    the viewer sees the streamer react, not what happened)."""
    fx = min(max(focus_x, 0.0), 1.0)
    if layout == "reel" and cam:
        cam = {k: float(cam[k]) for k in ("x", "y", "w", "h")}
        gh = REEL_GAME_H
        crop_cam = f"crop=w=iw*{cam['w']:.4f}:h=ih*{cam['h']:.4f}:x=iw*{cam['x']:.4f}:y=ih*{cam['y']:.4f}"
        full = ""
        if cam_only_until and cam_only_until > 0.2:
            full = (f"[s3]{crop_cam},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},"
                    f"unsharp=5:5:0.7:5:5:0,setsar=1[camfull];")
        out = (
            f"{src}split={3 if full else 2}[s1][s2]{'[s3]' if full else ''};"
            f"[s1]{crop_cam},"
            f"scale={W}:{REEL_CAM_H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{REEL_CAM_H},"
            f"unsharp=5:5:0.7:5:5:0,setsar=1{punch_filter(fx_events, W, REEL_CAM_H, band='cam')}[cam];"
            f"[s2]crop=w=ih/{zoom}*{W}/{gh}:h=ih/{zoom}:x='clip(iw*{fx:.4f}-ow/2,0,iw-ow)':y=(ih-oh)/2,"
            f"scale={W}:{gh}:flags=lanczos,unsharp=5:5:0.5:5:5:0,setsar=1{punch_filter(fx_events, W, gh, hook_zoom)}[game];"
            + full
            + ("[cam][game]vstack=inputs=2[vst];[vst][camfull]overlay=0:0:enable='lt(t," + f"{cam_only_until:.3f}" + ")'[v0]"
               if full else "[cam][game]vstack=inputs=2[v0]")
        )
        return out
    if layout in ("focus", "reel"):
        evs = [(e[0], ["zoom_hold" if f == "cam_zoom" else f for f in e[1]]) + tuple(e[2:]) for e in fx_events or []]
        return (f"{src}crop=w=ih*{W}/{H}:h=ih:x='clip(iw*{fx:.4f}-ow/2,0,iw-ow)':y=0,"
                f"scale={W}:{H}:flags=lanczos,unsharp=5:5:0.5:5:5:0,setsar=1{punch_filter(evs, W, H, hook_zoom)}[v0]")
    if layout == "stacked" and cam:
        return (
            f"{src}split=2[s1][s2];"
            f"[s1]crop=w=iw*{cam['w']}:h=ih*{cam['h']}:x=iw*{cam['x']}:y=ih*{cam['y']},"
            f"scale={W}:{OLD_CAM_H}:force_original_aspect_ratio=increase,crop={W}:{OLD_CAM_H},setsar=1[cam];"
            f"[s2]crop=w=ih*{W}/{H - OLD_CAM_H}:h=ih:x=(iw-ow)/2:y=0,scale={W}:{H - OLD_CAM_H},setsar=1[game];"
            "[cam][game]vstack=inputs=2[v0]"
        )
    return (
        f"{src}split=2[s1][s2];"
        f"[s1]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        "boxblur=24:2,eq=brightness=-0.10[bg];"
        f"[s2]scale={W}:-2,setsar=1[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2-80[v0]"
    )


def _fontsdir(out_dir):
    """Relative path from the clip folder to the bundled fonts (no drive letter, so ffmpeg is happy)."""
    from .config import ROOT
    try:
        rel = os.path.relpath(str(ROOT / "assets" / "fonts"), out_dir).replace("\\", "/")
        return "" if ":" in rel else f":fontsdir={rel}"
    except ValueError:
        return ""


def render(rec, start, dur, out_dir, ass_text, layout, cam, focus_x, sfx, rcfg, raw_pad=60,
           segments=None, title_png=None, seam_y=None, mixed_audio=None, fx_events=None, hook_zoom=False,
           speeds=None, cam_only_until=0.0):
    """start: where the clip starts in the recording. segments: jump-cut pieces as (a, b) seconds
    relative to 'start' (None = one piece of length dur). sfx: (file, seconds in the OUTPUT).
    mixed_audio: the finished sound (sound pass) to use instead of the game audio + sfx.
    fx_events: [(output second, ["zoom", "shake", "flash"])] on the gameplay band. speeds: per piece (0.5 = slow-mo)."""
    os.makedirs(out_dir, exist_ok=True)
    segments = [(float(a), float(b)) for a, b in (segments or [(0.0, dur)])]
    # speeds: 0.5 = slow motion; much lower = a freeze (a pause on one frame while a sound plays)
    speeds = [max(0.005, min(1.0, float(s or 1.0))) for s in (speeds or [1.0] * len(segments))]
    dur = sum((b - a) / sp for (a, b), sp in zip(segments, speeds))
    span = max(b for _, b in segments)             # a hook piece can come first and be later in the source
    fonts = _fontsdir(out_dir)
    with open(os.path.join(out_dir, "captions.ass"), "w", encoding="utf-8") as f:
        f.write(ass_text)
    handles = handle_groups(rcfg)
    base = "clip_clean.mp4" if handles else "clip.mp4"
    tag = rcfg.get("tag_image") or ""
    use_png = not handles and bool(tag) and os.path.exists(tag)
    inputs = ["-ss", f"{start:.2f}", "-t", f"{span + 0.1:.2f}", "-i", rec]
    # jump cuts: keep only the chosen pieces, glued back to back (sound gaps filled first, so it stays in sync)
    k = len(segments)
    pre = "[0:v]fps=60," if rcfg.get("output_fps") == 60 else "[0:v]"
    graph = f"{pre}split={k}" + "".join(f"[vs{i}]" for i in range(k))
    graph += f";[0:a]aresample=48000:async=1:first_pts=0,asplit={k}" + "".join(f"[as{i}]" for i in range(k))
    for i, (a, b) in enumerate(segments):
        sp = speeds[i]
        vslow = f",setpts=PTS/{sp:.3f}" if abs(sp - 1) > 1e-3 else ""
        piece = (b - a) / sp
        aslow = ("" if abs(sp - 1) <= 1e-3 else f",atempo={sp:.3f}" if sp >= 0.5 else
                 f",volume=0,apad=whole_dur={piece:.3f},atrim=0:{piece:.3f}")         # freeze: silent
        graph += (f";[vs{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS{vslow}[vp{i}]"
                  f";[as{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS{aslow}[ap{i}]")
    graph += ";" + "".join(f"[vp{i}][ap{i}]" for i in range(k)) + f"concat=n={k}:v=1:a=1[vcat][acat]"
    graph += ";" + video_filter(layout, cam, focus_x, rcfg.get("game_zoom", 1.0), src="[vcat]",
                                fx_events=fx_events if rcfg.get("punch_fx", True) else None, hook_zoom=hook_zoom,
                                cam_only_until=cam_only_until)
    v0 = "[v0]"
    if rcfg.get("censor_regions"):                 # nudity: pixelated spots (under the captions and the title)
        from .censor import filter_graph
        graph += filter_graph(rcfg["censor_regions"], "[v0]", "[v0z]")
        v0 = "[v0z]"
    graph += f";{v0}subtitles=captions.ass{fonts}[v1]"
    idx = 1
    if title_png and os.path.exists(title_png):
        inputs += ["-loop", "1", "-i", title_png]
        sy = seam_y if seam_y is not None else (REEL_CAM_H if layout == "reel" else 230)
        graph += f";[{idx}:v]format=rgba[ttl];[v1][ttl]overlay=(W-w)/2:{sy}-h/2:shortest=1[v1c]"
        idx += 1
        v1 = "[v1c]"
    else:
        v1 = "[v1]"
    ov = rcfg.get("campaign_overlay")              # the campaign's required image, on every version
    if ov and ov.get("path") and os.path.exists(ov["path"]):
        inputs += ["-loop", "1", "-i", ov["path"]]
        m = 40
        pos = {"top-left": (f"{m}", f"{m}"), "top": ("(W-w)/2", f"{m}"), "top-right": (f"W-w-{m}", f"{m}"),
               "center": ("(W-w)/2", "(H-h)/2"), "bottom-left": (f"{m}", f"H-h-{m}"),
               "bottom": ("(W-w)/2", f"H-h-{m}"), "bottom-right": (f"W-w-{m}", f"H-h-{m}")}
        x, y = pos.get(ov.get("position"), pos["top-right"])
        width = max(16, int(W * float(ov.get("size", 22)) / 100) // 2 * 2)
        alpha = max(0.1, min(1.0, float(ov.get("opacity", 1.0))))
        graph += (f";[{idx}:v]scale={width}:-2,format=rgba,colorchannelmixer=aa={alpha:.2f}[cov]"
                  f";{v1}[cov]overlay={x}:{y}:shortest=1[v1o]")
        idx += 1
        v1 = "[v1o]"
    if use_png:
        inputs += ["-i", tag]
        y = f"H-h-330" if layout in ("reel", "focus") else "150"
        graph += f";[{idx}:v]scale=380:-1,format=rgba,colorchannelmixer=aa=0.85[tag];{v1}[tag]overlay=(W-w)/2:{y}[vout]"
        idx += 1
    else:
        graph += f";{v1}null[vout]"
    # async=1 + first_pts=0: fill audio gaps (skipped ads, dropped segments) with silence so the
    # sound stays locked to the picture instead of sliding earlier after every gap
    if mixed_audio:                                  # the sound pass already made the final sound
        inputs += ["-i", mixed_audio]
        graph += f";[acat]anullsink;[{idx}:a]aresample=48000,apad=whole_dur={dur:.3f}[main]"
        idx += 1
        sfx = []
    else:
        graph += ";[acat]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[main]"
    mix = ["[main]"]
    vol = float(rcfg.get("sfx_volume", 0.8))
    for path, at in sfx or []:
        if not path or at < 0 or at > dur:
            continue
        inputs += ["-i", path]
        ms = int(at * 1000)
        graph += f";[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}:all=1,volume={vol}[sfx{idx}]"
        mix.append(f"[sfx{idx}]")
        idx += 1
    if len(mix) > 1:
        graph += (f";{''.join(mix)}amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0,"
                  "alimiter=limit=0.95[aout]")
    else:
        graph += ";[main]anull[aout]"
    # ONE encode for every version: the clean clip and each handle version are made from the same
    # decoded frames (no re-encoding an already-compressed clip, which blurred the handle versions)
    outputs = [(base, None)] + [(f"clip_{suffix}.mp4", handle) for suffix, handle in handles]
    tag_files = []
    n = len(outputs)
    graph += f";[vout]split={n}" + "".join(f"[vo{i}]" for i in range(n))
    graph += f";[aout]asplit={n}" + "".join(f"[ao{i}]" for i in range(n))
    from .gpu import use_gpu_encoder, video_args
    gpu = use_gpu_encoder(rcfg)                     # NVENC on the GPU when it works, else libx264 on the CPU

    def enc(on_gpu):
        return video_args(rcfg, on_gpu) + ["-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-t", f"{dur:.2f}",
                                           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    labels = []
    for i, (name, handle) in enumerate(outputs):
        label = f"[vo{i}]"
        if handle:
            ass_name = f"tag_{i}.ass"
            with open(os.path.join(out_dir, ass_name), "w", encoding="utf-8") as f:
                f.write(tag_ass(handle, layout, rcfg))
            tag_files.append(ass_name)
            graph += f";[vo{i}]subtitles={ass_name}{fonts}[vt{i}]"
            label = f"[vt{i}]"
        labels.append((label, i, name))

    def encode(on_gpu, which):
        """which: indexes of the outputs to write in this ffmpeg run (the others go to a null sink)."""
        maps, g = [], graph
        for label, i, name in labels:
            if i in which:
                maps += ["-map", label, "-map", f"[ao{i}]"] + enc(on_gpu) + [name]
            else:
                g += f";{label}nullsink;[ao{i}]anullsink"
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + inputs + ["-filter_complex", g] + maps,
            cwd=out_dir)

    # the clean master and the MAIN platform version first (ready to post sooner), the other versions after
    order = [list(range(len(labels)))]
    if rcfg.get("main_first", True) and len(labels) > 2:
        order = [[0, 1], list(range(2, len(labels)))]
    for which in order:
        try:
            encode(gpu, which)
        except RuntimeError as e:
            if not gpu:
                raise
            from .util import log
            log(f"GPU encoding failed ({str(e).splitlines()[-1][:100]}): encoding on the CPU instead", "render")
            gpu = False
            encode(False, which)
    for t in tag_files:
        os.remove(os.path.join(out_dir, t))
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "1.5", "-i", base,
         "-frames:v", "1", "-q:v", "2", "thumb.jpg"], cwd=out_dir)
    files = [name for name, _ in outputs]
    if rcfg.get("save_raw"):
        raw_start = max(0.0, start - raw_pad)
        raw_dur = span + (start - raw_start) + raw_pad
        # re-encoded (not copied) so it starts exactly on time and keeps sound in sync

        def raw_encode(on_gpu):
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{raw_start:.2f}",
                 "-t", f"{raw_dur:.2f}", "-i", rec, "-map", "0:v:0", "-map", "0:a:0?",
                 "-af", "aresample=48000:async=1:first_pts=0", "-fps_mode", "cfr"]
                + video_args(rcfg, on_gpu, crf=27 if on_gpu else 23, preset="veryfast")   # raw.mp4: editing source
                + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "raw.mp4"],
                cwd=out_dir)
        try:
            raw_encode(gpu)
        except RuntimeError:
            if not gpu:
                raise
            raw_encode(False)
        return files, start - raw_start
    return files, 0.0
