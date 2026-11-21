"""Crowd signals for streams that already ended (past streams / VODs):

- Twitch VOD: viewer clips of that broadcast (Get Clips by broadcaster + the stream's time range, kept tight and
  paginated), placed exactly with vod_offset + duration; and the chat replay (TwitchDownloaderCLI, if installed).
- YouTube: the live-chat replay (yt-dlp --write-subs --sub-langs live_chat) and the "Most replayed" heatmap.
- Kick: no public clip or chat-replay access we can rely on: logged and skipped.

The chat replay runs through the SAME chat-spike and chat-category detectors as live mode."""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

from . import crowd
from .util import log


# ------------------------------------------------------------------------------------------ chat replay
class ReplayChat:
    """Chat messages with stream-second timestamps, with the same reading methods as the live chat listeners,
    so the live detectors can run on it (their 'now' is a stream second)."""

    def __init__(self, messages):
        self.msgs = sorted(messages, key=lambda m: m[0])           # (sec, user, text)
        self._times = [m[0] for m in self.msgs]

    def _range(self, t_from, t_to):
        import bisect
        return bisect.bisect_left(self._times, t_from), bisect.bisect_right(self._times, t_to)

    def snapshot(self, t_from, t_to):
        i, j = self._range(t_from, t_to)
        return self.msgs[i:j]

    def count_between(self, t_from, t_to, weight_fn=None):
        i, j = self._range(t_from, t_to)
        if weight_fn is None:
            return j - i
        return sum(weight_fn(m[2]) for m in self.msgs[i:j])

    def __len__(self):
        return len(self.msgs)

    @property
    def end(self):
        return self._times[-1] if self._times else 0.0


def chat_moments(chat, det, step=5.0):
    """Run the live chat detectors over a replay. Returns [(stream second, why, strength)]."""
    from .detector import BUG, GAMEPLAY, NPC, SPICY, SpikeDetector, SusDetector
    if not len(chat):
        return []
    spikes = SpikeDetector(chat, det)
    spikes.started = -1e9                      # the replay has its whole history: no warm-up in wall time
    words = [("sus", SusDetector(chat, det)), ("spicy", SusDetector(chat, det, pattern=SPICY)),
             ("bug", SusDetector(chat, det, pattern=BUG, min_hits=4)),
             ("npc", SusDetector(chat, det, pattern=NPC, min_hits=4)),
             ("gameplay", SusDetector(chat, det, pattern=GAMEPLAY, min_hits=5))]
    label = {"sus": "chat thinks it's sus", "spicy": "chat is thirsty (spicy moment)", "bug": "chat says bug/glitch/physics",
             "npc": "chat reacting to an NPC / something weird", "gameplay": "chat hyped by the gameplay (chase/stunt/graphics)"}
    out, t = [], chat._times[0] + 60
    while t <= chat.end + step:
        hit = spikes.check(t)
        if hit:
            out.append((hit[0], f"chat replay: {hit[1]:.1f}x faster than normal", 2.0 + hit[1]))
        for name, d in words:
            hit = d.check(t)
            if hit:
                out.append((hit[0], f"chat replay, {label[name]}: {hit[1]} messages", 1.0 + hit[1] / 4))
        t += step
    return out


def _yt_runs(message):
    parts = []
    for r in (message or {}).get("runs") or []:
        if "text" in r:
            parts.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            parts.append((e.get("shortcuts") or [""])[0] or (e.get("emojiId") or ""))
    return "".join(parts)


def parse_youtube_chat(path):
    """yt-dlp's .live_chat.json (one JSON object per line) -> [(sec, user, text)]."""
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            rep = obj.get("replayChatItemAction") or {}
            try:
                sec = int(rep.get("videoOffsetTimeMsec")) / 1000.0
            except (TypeError, ValueError):
                continue
            for act in rep.get("actions") or []:
                item = ((act.get("addChatItemAction") or {}).get("item") or {})
                r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                if not r:
                    continue
                text = _yt_runs(r.get("message"))
                if text:
                    out.append((sec, (r.get("authorName") or {}).get("simpleText", ""), text))
    return out


def youtube_chat(url, work, timeout=3 * 3600):
    """Download a YouTube stream's chat replay. [] if it has none."""
    from .util import youtube_cookies
    cmd = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--skip-download", "--write-subs",
           "--sub-langs", "live_chat", "-o", os.path.join(work, "chat.%(ext)s"), url]
    if youtube_cookies():
        cmd[3:3] = ["--cookies", youtube_cookies()]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        log("chat replay download took too long - using what came", "past")
    for f in os.listdir(work):
        if f.startswith("chat.") and "live_chat" in f:
            return parse_youtube_chat(os.path.join(work, f))
    return []


def twitch_chat(vod_id, work, exe=None, timeout=3 * 3600):
    """Twitch chat replay with TwitchDownloaderCLI (if it is installed). [] otherwise."""
    exe = exe or shutil.which("TwitchDownloaderCLI") or shutil.which("TwitchDownloaderCLI.exe")
    if not exe:
        log("Twitch chat replay needs TwitchDownloaderCLI (not installed): skipped", "past")
        return []
    out = os.path.join(work, "chat.json")
    try:
        subprocess.run([exe, "chatdownload", "--id", str(vod_id), "-o", out, "--collision", "Overwrite"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"Twitch chat replay failed ({str(e)[:100]})", "past")
        return []
    msgs = []
    for c in data.get("comments") or []:
        try:
            msgs.append((float(c["content_offset_seconds"]), (c.get("commenter") or {}).get("display_name", ""),
                         (c.get("message") or {}).get("body", "")))
        except (KeyError, TypeError, ValueError):
            continue
    return msgs


# ------------------------------------------------------------------------------- YouTube "Most replayed"
def heatmap_peaks(heatmap, top=12, min_value=0.35, min_gap=60):
    """yt-dlp's heatmap ([{start_time, end_time, value 0-1}]) -> [(second, value)] local peaks, strongest first.
    The 'value' is relative: 1.0 = the most replayed part of this video."""
    pts = [(float(h["start_time"]), float(h["end_time"]), float(h.get("value") or 0)) for h in heatmap or []
           if "start_time" in h and "end_time" in h]
    if len(pts) < 3:
        return []
    vals = [v for _, _, v in pts]
    peaks = []
    for i, (a, b, v) in enumerate(pts):
        left = vals[i - 1] if i > 0 else -1
        right = vals[i + 1] if i + 1 < len(vals) else -1
        if v >= min_value and v >= left and v >= right:
            peaks.append(((a + b) / 2, v))
    peaks.sort(key=lambda p: -p[1])
    out = []
    for sec, v in peaks:
        if all(abs(sec - s) >= min_gap for s, _ in out):
            out.append((sec, v))
        if len(out) >= top:
            break
    return out


# ------------------------------------------------------------------------------------- Twitch VOD clips
_DUR = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def twitch_duration(text):
    m = _DUR.fullmatch(str(text or ""))
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def twitch_vod_id(url):
    m = re.search(r"twitch\.tv/videos/(\d+)", url or "")
    return m.group(1) if m else None


def vod_clips(api, vod_id, chunk_hours=2.0, bots=crowd.DEFAULT_BOTS):
    """Every viewer clip of a Twitch VOD, placed with vod_offset. Queried by broadcaster + the stream's time range
    in tight chunks (very wide ranges can miss clips), paginated, then kept only if video_id matches."""
    v = api.video(vod_id)
    if not v:
        return [], None
    start = crowd.parse_ts(v["created_at"])
    dur = twitch_duration(v.get("duration"))
    out, seen = [], set()
    t = start
    while t < start + dur + 600:
        t2 = min(t + chunk_hours * 3600, start + dur + 600)
        for raw in api.clips_between(v["user_id"], crowd.iso(t), crowd.iso(t2)):
            c = crowd.from_helix(raw)
            if c.id in seen or c.video_id != str(vod_id) or c.vod_offset is None:
                continue
            seen.add(c.id)
            if crowd.ignored(c, bots, v["user_id"]):
                continue
            c.span = crowd.vod_span(c)
            out.append(c)
        t = t2
    return out, v


def gather(cfg, scfg, vod, work, api=None, info=None):
    """All crowd signals of one past stream: [(second, why, strength, extras)] with extras = {"clips": [...],
    "span": (a, b), "signals": {...}}, plus a short summary for the log/report."""
    pc = cfg.get("past") or {}
    det = cfg["detection"]
    url = vod["url"]
    found, summary = [], {}
    if not pc.get("crowd_signals", True):
        return found, summary, None
    # viewer clips (Twitch)
    vid = twitch_vod_id(url)
    if vid and api is not None:
        try:
            clips, _ = vod_clips(api, vid, bots=det.get("clip_bots") or crowd.DEFAULT_BOTS)
            moments = crowd.group(clips)
            summary["viewer_clips"] = len(clips)
            summary["clip_moments"] = len(moments)
            for m in moments:
                found.append((m.end - 2, f"viewer clips: {m.clippers} clipper(s), {m.views} views"
                              + (", featured" if m.featured else ""), 5 + m.strength(),
                              {"clips": m.clips, "span": (m.start, m.end)}))
        except Exception as e:
            log(f"viewer clips of this VOD unavailable ({str(e)[:100]})", scfg.get("key", ""))
    elif vid:
        log("viewer clips of Twitch VODs need Twitch keys in config.yaml: skipped", scfg.get("key", ""))
    # YouTube heatmap
    if ("youtu" in url) and pc.get("heatmap", True):
        peaks = heatmap_peaks((info or {}).get("heatmap"))
        summary["heatmap_peaks"] = len(peaks)
        for sec, v in peaks:
            found.append((sec, f"most replayed: {v:.2f} (YouTube heatmap peak)", 3 + 4 * v,
                          {"signals": {"heatmap": round(1.0 + 3.0 * v, 2)}}))
        if not (info or {}).get("heatmap"):
            log("no 'Most replayed' heatmap for this video yet", scfg.get("key", ""))
    # chat replay
    chat = None
    if pc.get("chat_replay", True):
        msgs = []
        if "youtu" in url:
            log("downloading the chat replay...", scfg.get("key", ""))
            msgs = youtube_chat(url, work, timeout=float(pc.get("chat_replay_max_minutes", 180)) * 60)
        elif vid:
            msgs = twitch_chat(vid, work, exe=pc.get("twitch_downloader") or None,
                               timeout=float(pc.get("chat_replay_max_minutes", 180)) * 60)
        elif "kick.com" in url:
            log("Kick chat replay isn't reachable: skipped", scfg.get("key", ""))
        summary["chat_messages"] = len(msgs)
        if msgs:
            chat = ReplayChat(msgs)
            for sec, why, strength in chat_moments(chat, det):
                found.append((sec, why, strength, {}))
            summary["chat_moments"] = sum(1 for f in found if f[1].startswith("chat replay"))
        elif not vid or "youtu" in url:
            log("no chat replay for this stream", scfg.get("key", ""))
    return found, summary, chat


def as_cand(why, clips=None, signals=None):
    """Something scoring.priority can read, before a real Candidate exists."""
    return SimpleNamespace(source=why, clips=list(clips or []), signals=dict(signals or {}))


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")
