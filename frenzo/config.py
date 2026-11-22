import copy
import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "game": {
        "name": "Grand Theft Auto VI",
        "keywords": ["grand theft auto vi", "grand theft auto 6", "gta vi", "gta 6", "gta6", "gtavi"],
        "only_when_playing": True,
    },
    "poll_seconds": 60,
    "twitch": {"client_id": "", "client_secret": "", "public_api": True},   # public_api: keyless viewer clips
    "kick": {"client_id": "", "client_secret": ""},
    "anthropic": {"api_key": "", "model": "claude-sonnet-5-5", "backend": "auto", "claude_code_model": "haiku",
                  "max_checks_per_hour": 30, "send_frames": True},
    "streamers": [],
    "detection": {
        "chat_window_seconds": 10,
        "spike_multiplier": 3.0,
        "min_messages_in_window": 15,
        "cooldown_seconds": 60,
        "use_twitch_clips": True,
        "clip_poll_seconds": 90,
        "voice_triggers": True,
        "chaos_triggers": True,
        "chaos_ratio": 2.6,
        "hot_take_model": "base",
        "voice_sensitivity": "normal",
        "loudness_triggers": "off",
        "loudness_jump_db": 12,
        # --- viewer clips as evidence (needs Twitch keys) ---
        "clip_evidence": True,          # off = the old behaviour (a clip is just one more signal)
        "clip_open_minutes": 5,         # a moment stays open this long for viewer clips to land on it
        "clip_poll_fast_seconds": 25,   # poll a streamer's clips this often for a while after any signal
        "clip_fast_minutes": 5,
        "clip_lookback_minutes": 10,    # each poll asks for clips made in the last N minutes
        "clip_delay_seconds": 6,        # a live clip ends about this long before it was created
        "clip_align": True,             # download each clip and match its sound against our recording
        "clip_lead_in": 25,             # seconds kept before a viewer clip's span (the setup)
        "clip_tail": 8,
        "clip_keep_bonus": 1,           # strong clip evidence lowers that moment's keep threshold by this
        "clip_strong_clippers": 3,
        "clip_strong_views": 1000,
        "clip_fast_views_per_min": 30,  # a clip gaining views this fast moves its moment to the front
        "clip_bots": ["streamelements", "nightbot", "moobot", "fossabot", "streamlabs", "wizebot", "sery_bot",
                      "soundalerts", "botrixoficial", "kofistreambot"],
        "clip_lessons": True,           # strong viewer clips become "missed" / "confirmed" lessons
        # --- picking moments ---
        "learned_chat_delay": True,     # per-streamer chat delay measured from chat vs voice reactions
        "chat_delay_seconds": 6,        # fallback chat delay
        "chat_floor": "scaled",         # scaled (by the chat's own speed: small streamers can trigger) / fixed
    },
    "queue": {
        "best_first": True,             # edit the best waiting moment first (combined score), not the oldest
        "cap_keeps_best": True,         # hourly cap: a stronger moment replaces a weaker waiting one
    },
    "past": {
        "crowd_signals": True,          # viewer clips, chat replay and YouTube "Most replayed" before the detectors
        "chat_replay": True,
        "chat_replay_max_minutes": 180, # a long stream's chat replay downloads slowly: use what came by then
        "twitch_downloader": "",        # path of TwitchDownloaderCLI.exe (Twitch chat replay); empty = on PATH
        "heatmap": True,
        "keep_preview_hours": 48,       # keep each past stream's preview so you can cut it by hand (0 = delete)
        "piece_lead": 30,               # harvest: seconds downloaded before a viewer clip
        "piece_tail": 10,
    },
    "clips": {
        "min_score": 6,
        "min_length": 12,
        "max_length": 45,
        "running_gags": True,
        "gag_window": 180,
        "max_per_hour_per_streamer": 8,
        "max_per_hour_total": 0,
        "skip_spoilers": True,
        "skip_nudity": True,       # (old setting, replaced by "nudity")
        "nudity": "blur",          # blur (keep the clip, pixelate the nudity) / skip / keep
        "context_before": 70,
        "context_after": 20,
        "lead_in": 1.5,        # extra seconds kept before the setup (so viewers see what's going on)
        "tail": 2.0,           # extra seconds kept after the reaction (so the laugh lands)
        "min_setup": 4.0,      # the clip always shows at least this much before the flagged moment
    },
    "transcription": {"model": "small", "device": "auto", "language": None},
    "render": {
        "layout": "reel",
        "captions": "mini",
        "jump_cuts": True,
        "title_bar": True,
        "title_size": 58,
        "caption_size": 56,
        "callout_size": 64,
        "caption_font": "Montserrat ExtraBold",
        "sound_effects": True,
        "sfx_types": ["sus"],
        "sfx_volume": 0.8,
        "game_zoom": 1.0,
        "heading_size": 72,
        "raw_padding": 60,
        "font": "Arial Black",
        "caption_color": "#FFFFFF",
        "highlight_color": "#FFD60A",
        "handle": "@itsfrenzo",
        "handles": {},
        "tag_image": "assets/clip_tag.png",
        "crf": 18,
        "encoder": "auto",     # auto = NVIDIA GPU (NVENC) when it works, else CPU / gpu / cpu
        "preset": "slow",
        "save_raw": True,
        # --- sound design with the GTA SFX library ---
        "sound_library": True,          # off = the old single "sus" sound
        "sound_library_path": "",       # empty = the installed gta-reel-sound-studio skill's gta_sfx_library
        "sound_check": True,            # run check_reel.py on every clip, auto-fix once
        "sound_safe_mode": True,        # no sounds tagged music / explicit unless the campaign allows them
        "cam_max_upscale": 2.2,         # the webcam is enlarged at most this much (0 = always fill the band)
        "raw_crf": 20,                  # quality of raw.mp4, the source of re-edits (lower = better, bigger)
        "nvenc_cq_offset": 5,           # GPU quality = crf + this (lower = better, bigger files)
        "sound_no_repeat": 3,           # prefer not to reuse sounds from the account's last N clips
        "sound_max_per_clip": 3,        # hard cap: never more sound effects than this in one clip
        "hook_types": ["action", "tension", "quote"],   # + reaction_first / cold_open = the rewind styles
        # YOUR sound list: the only sounds Frenzo uses, when to use each one, and the picture effect that goes
        # with it (effect: zoom / flash / slow_zoom / cam_zoom / freeze_zoom / silence_after / hook_rewind).
        # rare: true = only for a moment that really calls for it. Empty list = the whole library (old way).
        "sound_guide": [
            {"id": "anime-wow", "when": "the character finds a new or rare item", "effect": "freeze_zoom", "rare": True},
            {"id": "awkward-pause", "when": "something awkward happens", "effect": "cam_zoom"},
            {"id": "hells-kitchen-suspense", "when": "build suspense before something weird is about to happen",
             "effect": "slow_zoom"},
            {"id": "bua-wa-wa-wa-wa", "when": "a sexy lady / a sexy moment", "rare": True},
            {"id": "fnaf-2-hallway", "when": "suspense before a little scary moment", "effect": "slow_zoom"},
            {"id": "bass-drop-vine-boom", "when": "right AFTER the impact, to make it funny (the character dies in a "
                                                  "weird way, a weird NPC), the streamer looking at the camera in "
                                                  "silence", "effect": "silence_after"},
            {"id": "boom", "when": "an impact, or a sus moment"},
            {"id": "flashbang-loud", "when": "a big impact, like dying to the police", "effect": "flash"},
            {"id": "vine-boom-slowed", "when": "a dramatic impact or reveal"},
            {"id": "cash-register", "when": "ONLY when getting cash"},
            {"id": "apple-notification", "when": "the character gets a notification or message on the in-game phone"},
            {"id": "rizzz", "when": "a rizz moment"},
            {"id": "sudden-suspense", "when": "a sudden suspense beat", "effect": "slow_zoom"},
            {"id": "whoosh-fast", "when": "a fast transition / jump cut"},
            {"id": "faahhhh", "when": "a funny fail"},
        ],
        "punch_fx": True,               # punch-in / shake / flash on the gameplay band at Claude's cue times
        # --- hook, captions, titles ---
        "hook": True,                   # hook rules for the first 3 seconds
        "caption_style": "bold",        # bold (1-3 words ALL CAPS on the seam, karaoke pop) / mini (old)
        "five_titles": True,
    },
    "storage": {
        "output_dir": "clips",
        "recordings_dir": "recordings",
        "keep_recordings": True,          # recordings are kept for the app's Recordings tab
        "segment_minutes": 60,            # live recordings are split into parts of this length
        "auto_delete_recordings_days": 0, # 0 = never delete recordings automatically
        "quality": "1080p60,1080p,936p60,720p60,best",
    },
    "workers": 1,
    "harvest": {
        "enabled": True,
        "top": 10,
        "min_clippers": 2,
        "min_views": 0,
        "schedule_minutes": 0,          # 0 = off; otherwise harvest each streamer this often
    },
    "learning": {
        "auto": True,          # learn automatically after 5+ new examples (at most every 2 hours)
        "model": "sonnet",     # model for learning runs (falls back to claude_code_model)
    },
}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def placeholder(value):
    return (not value) or str(value).startswith("PASTE_")


def finalize(cfg):
    env = os.environ.get
    cfg["anthropic"]["api_key"] = env("ANTHROPIC_API_KEY") or cfg["anthropic"]["api_key"]
    cfg["twitch"]["client_id"] = env("TWITCH_CLIENT_ID") or cfg["twitch"]["client_id"]
    cfg["twitch"]["client_secret"] = env("TWITCH_CLIENT_SECRET") or cfg["twitch"]["client_secret"]
    cfg["kick"]["client_id"] = env("KICK_CLIENT_ID") or cfg["kick"]["client_id"]
    cfg["kick"]["client_secret"] = env("KICK_CLIENT_SECRET") or cfg["kick"]["client_secret"]
    streamers = []
    for s in cfg.get("streamers") or []:
        if isinstance(s, str):
            s = {"login": s}
        raw = str(s.get("login", "")).strip()
        if not raw or raw.lstrip("@").upper().startswith("PASTE_"):
            continue
        s["platform"] = str(s.get("platform", "twitch")).strip().lower()
        if s["platform"] not in ("twitch", "kick", "youtube"):
            raise SystemExit(f"ERROR: unknown platform '{s['platform']}' for {raw} (use twitch, kick or youtube).")
        s["raw_login"] = raw
        login = raw.lower()
        if s["platform"] == "youtube":
            login = re.sub(r"^https?://(www\.)?youtube\.com/", "", login).strip("/").replace("/live", "")
            login = login.split("/")[-1].lstrip("@")
        s["login"] = login
        s["key"] = f"{s['platform']}:{login}"
        s.setdefault("tags", [])
        from .campaign import normalize as _campaign          # campaign rules: defaults when missing
        s["campaign"] = _campaign(s.get("campaign"))
        streamers.append(s)
    cfg["streamers"] = streamers
    for key in ("output_dir", "recordings_dir"):
        d = Path(os.path.expandvars(str(cfg["storage"][key]))).expanduser()
        cfg["storage"][key] = str(d if d.is_absolute() else ROOT / d)
    tag = str(cfg["render"].get("tag_image") or "")
    cfg["render"]["tag_image"] = (tag if Path(tag).is_absolute() else str(ROOT / tag)) if tag else ""
    return cfg


def load_config(path="config.yaml", required=True):
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    from .learning import apply_to_config      # learned.json: merged here, never written into config.yaml
    if not p.exists():
        if required:
            raise SystemExit(
                f"ERROR: {p.name} not found. Copy config.example.yaml to config.yaml and fill it in.")
        return apply_to_config(finalize(_merge(DEFAULTS, {})))
    with open(p, "r", encoding="utf-8") as f:
        user = yaml.safe_load(f) or {}
    return apply_to_config(finalize(_merge(DEFAULTS, user)))
