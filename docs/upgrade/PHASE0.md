# Phase 0: map of Frenzo before the upgrade

The baseline is git commit `c1fbe5e` (the folder had no git repository, so I created one; recordings, logs,
`.venv`, the built app and `youtube_cookies.txt` are git-ignored).

## Detectors (live)

| Signal | File | Notes |
|---|---|---|
| Chat spike | `frenzo/detector.py` `SpikeDetector` | ≥15 weighted msgs / 10 s and ≥3× the 5-min median; anchor = now − 0.6 × window (6 s) |
| Chat categories | `frenzo/detector.py` `SusDetector` with the `SUS`, `SPICY`, `BUG`, `NPC`, `GAMEPLAY` regexes; learned and campaign keywords | each category is its own detector in `engine.Session.run` |
| Twitch clips | `frenzo/platforms.py` `TwitchPlatform.new_clips` → `twitch.TwitchAPI.clips_since` (Helix), polled every 90 s in `engine.py` | **Off in practice: `config.yaml` has no Twitch keys** (`PASTE_TWITCH_CLIENT_ID`), so the platform runs keyless and `new_clips` returns nothing |
| Streamer reaction | `frenzo/voice.py` `VoiceReactionDetector` / `scan_file` | yell / laugh / outburst |
| Gameplay chaos | `frenzo/chaos.py` `ChaosDetector` / `ChaosAnalyzer` | motion ≥2.6× baseline for ≥0.5 s |
| Speech listener | `frenzo/hottake.py` `HotTakeDetector` | hot takes, NPC talk, learned and campaign phrases |
| Loudness (fallback) | `frenzo/detector.py` `LoudnessDetector` | off in your config |

## From detections to moments (`frenzo/engine.py`)
- Each detector has its own 60 s cooldown (`cooldown_seconds`).
- `Session._queue`: a signal within 30 s of a recent moment is appended to it ("same moment also ->").
- Running gag: 2+ moments within `gag_window` (180 s) become one long window, and the earlier pending ones are marked `superseded`.
- The window is `context_before` 70 s / `context_after` 20 s in `pipeline.Pipeline._process`.
- Hourly cap: `Pipeline._rate_ok` counts **saved** clips in the last hour (10/streamer). It is checked when a moment is processed, first come first served. One worker handles a FIFO queue.
- **Right now the live queue holds 99 waiting moments** (Clix's 24/7 marathon flags faster than one worker judges). Phase 3 changes the queue to the best-first order.

## Past streams (`frenzo/vods.py`)
`list_past_streams` (yt-dlp) → `clip_past_stream`: low-res preview → `scan_file` (voice) + `chaos_scan` + `take_scan` → sort by strength, merge within 60 s → per moment download a 1080p `--download-sections` piece → `_seg_start` (loudness-envelope cross-correlation against the preview, which I'll reuse for clip alignment) → `Pipeline.process`. The preview is deleted when the job ends, so nothing is kept for cutting by hand.

## Claude judging (`frenzo/ai.py`)
`SYSTEM` prompt + `TOOL` schema (`what_happens, keep, viral_score, category, start, end, title_bar, highlight, emoji, heading, segments, callouts, hook_text, sfx[type=sus], focus_x, title, caption, hashtags, spoiler, nudity, nudity_at, reason`). It runs through the Claude Code CLI with your plan (Haiku), with frames saved as files. The whole prompt is sent as the user message (no system-prompt caching today). Learned notes come from `learning.prompt_notes` and campaign rules from `campaign.prompt_section`.

## Editor
- `pipeline.py` maps Claude's times through jump cuts with `viral.mapper`, adds margins, and handles the facecam (`_facecam`, Claude + face scan + memory in `.webcams_v2.json`) and nudity.
- `editor.render`: webcam band 640 px on top, a 1280 px gameplay crop around `focus_x`, ASS subtitles via libass (`viral.build_ass`: lowercase "mini" captions 50 px mid-gameplay plus callouts), the title PNG on the seam (`titlecard.py`), the sus sound (`sfx.py`, built-in synth or `assets/sfx/sus`), loudnorm −14. **One encode writes the 4 versions** (clean plus one per handle group) on NVENC, plus `raw.mp4` (±60 s).
- `campaign.caption_txt` writes `caption.txt`.

## Learning (`frenzo/learning.py`)
- Lesson kinds: missed (your own cut, measured), deleted, retitled, timing, reedited.
- After 5 new lessons, Sonnet writes `editor_notes`, `title_notes`, keywords, `lead_in`/`tail`, and per-streamer `spike_multiplier`, `min_messages_in_window`, `chaos_ratio`, `voice_sensitivity`, `min_score`, `hot_takes`, clamped by `LIMITS` and `MAX_STEP`.
- **Per-streamer threshold tuning already exists** (Phase 1.7 only adds the new lesson types).

## Review UI and cutting by hand
The Frenzo app (`frenzo/app/server.py` + `static/`) already has Live, Past streams, Clips (re-edit with Claude, recut, retitle, delete, posted marks), Recordings (player, loudness and chat curve, detected moments, in/out cut → `frenzo cutrec`, which saves a "missed" lesson), Learning and Settings tabs. Past streams can't be cut by hand today, because their preview is deleted.

## Already there (won't rebuild)
- Chat categories; running-gag merge; "same moment also"; the per-streamer learned thresholds and minimum score.
- Audio alignment by loudness-envelope correlation (`vods._seg_start`).
- ASS/libass captions and callouts; jump-cut time mapping (`viral.mapper`); `raw.mp4`.
- Manual cuts from live recordings, with lessons; the review UI with a timeline.
- Two job lanes (past streams vs. quick jobs).

## Conflicts and issues found
1. Twitch keys are missing, so the viewer-clip signal is off and Phase 1 / 2 (Twitch) / 9 need keys.
2. Live backlog of 99 moments, processed FIFO (Phase 3).
3. The single `sfx` "sus" field and the synth sounds (replaced in Phase 4).
4. Captions are lowercase 50 px mid-gameplay (replaced in Phase 5).
5. The past-stream preview is deleted, so there's nothing to cut from (Phase 6).
