# Frenzo: Automated Highlight Detection and Short-Form Video Production for Live Streams

Frenzo is an end-to-end system that monitors live gaming broadcasts on Twitch, Kick and YouTube, detects
noteworthy moments in real time from several independent signals, and turns each one into a
publication-ready vertical video (1080×1920) with captions, titles, framing and sound design. A large
language model (Claude) evaluates each candidate moment and chooses the cut and the title. The pipeline
works on live streams, archived broadcasts and local recordings, and a desktop application lets a person
review and adjust every result.

The project was designed, implemented and tested by Zitouni Mohamed Rayen.

---

## Highlights

- **Multi-signal event detection**: chat-rate spikes, speech-based reaction detection (shouting,
  laughter, sudden outbursts), computer-vision motion analysis of the gameplay, live transcription of the
  streamer's speech, and community signals (viewer-created clips). When signals agree on the same moment,
  it is ranked higher.
- **LLM-assisted editorial decisions**: for each candidate moment the system builds a multimodal context
  (word-level transcript, chat excerpt, loudness profile and sampled video frames). Claude scores the
  moment, sets precise in/out points and proposes several titles. Rule-based validators check every
  answer (title length, banned phrasing, words that give away the ending) before it is accepted.
- **Automated video production with FFmpeg**: a split layout with the webcam on top and the gameplay
  below, with automatic webcam and face localisation. Word-by-word animated captions, title cards, jump
  cuts, zoom and shake effects, and branding overlays.
- **Audio post-production**: integration with an external tagged library of 260+ sound effects, placed
  frame-accurately on impact moments, with ducking, two-pass loudness normalisation to −14 LUFS, and an automatic check that each
  sound is in sync and audible.
- **Content-safety screening**: automatic detection and blurring of sensitive frames, plus a warning
  flag on clips that may break platform guidelines.
- **Human-in-the-loop workflow**: a desktop application to review clips, adjust the cut, the framing and
  the title by hand, or ask the model for a re-edit in natural language ("recenter the gameplay",
  "the title should mention the NPC"). Manual edits never change the sound design.
- **Continuous improvement**: the system learns from the reviewer's corrections (deleted clips,
  rewritten titles, re-timed cuts) and from how published clips perform, and feeds this back into future
  prompts and scoring.
- **Several sources**: live recording in overlapping one-hour segments, processing of archived
  broadcasts, and harvesting of community-made clips, with checks that the same moment is never
  clipped twice.

---

## Architecture

```
            ┌──────────────── Sources ────────────────┐
            │ Live streams · Archived broadcasts ·    │
            │ Community clips · Local recordings      │
            └───────────────────┬─────────────────────┘
                                │
                   ┌────────────▼────────────┐
                   │  Signal detectors       │  chat spikes · voice reactions ·
                   │  (real time / offline)  │  motion "chaos" · speech topics ·
                   └────────────┬────────────┘  viewer clips
                                │ candidate moments
                   ┌────────────▼────────────┐
                   │  Fusion & ranking       │  multi-signal agreement, campaign rules
                   └────────────┬────────────┘
                                │ top candidates
                   ┌────────────▼────────────┐
                   │  LLM judgement          │  transcript + frames + chat → score,
                   │  (Claude)               │  cut points, titles, sound cues
                   └────────────┬────────────┘
                                │ validated edit decision
                   ┌────────────▼────────────┐
                   │  Rendering (FFmpeg)     │  layout, captions, effects, SFX,
                   │                         │  loudness, safety screening
                   └────────────┬────────────┘
                                │
                   ┌────────────▼────────────┐
                   │  Review app (Flask)     │  manual edits, re-edits, feedback
                   └────────────┬────────────┘     → learning loop
                                ▼
                     Platform-specific exports
```

---

## Technology stack

| Area | Technologies |
|---|---|
| Language | Python 3.11 |
| AI / ML | Anthropic Claude (multimodal judgement and editing), faster-whisper (GPU speech-to-text), NudeNet (content screening) |
| Computer vision | OpenCV, NumPy (motion analysis, webcam and face localisation) |
| Media | FFmpeg (filter graphs, loudness normalisation), Streamlink and yt-dlp (stream capture) |
| Platforms | Twitch Helix API and chat (IRC over WebSocket), Kick, YouTube |
| Application | Flask REST API, vanilla JavaScript front end, pywebview desktop shell, system-tray integration |
| Quality | `unittest` suite (114 tests), configuration flags for every behaviour |

---

## Project structure

```
frenzo/
  engine.py, streams.py, recorder.py   live monitoring and segmented recording
  detector.py, chat.py, crowd.py       chat and community signals
  voice.py, hottake.py, chaos.py       audio, speech and motion detectors
  moments.py, scoring.py, viral.py     candidate fusion and ranking
  ai.py, pipeline.py                   LLM judgement and the clip-production pipeline
  editor.py, layout.py, titlecard.py   rendering, framing and on-screen text
  sfx.py, soundlib.py, soundpass.py    sound design and audio checks
  censor.py                            content-safety screening
  titles.py, hook.py                   title generation and validation
  vods.py, harvest.py, replay.py       archived broadcasts and community clips
  redo.py                              manual re-cuts and natural-language re-edits
  learning.py                          feedback loop from reviewer corrections and results
  app/                                 desktop review application (Flask + JS)
tests/                                 unit tests
config.example.yaml                    documented configuration template
```

---

## Getting started

### Requirements
- Python 3.11, FFmpeg on the `PATH`
- An NVIDIA GPU is recommended for transcription (it also runs on CPU)
- Claude Code or an Anthropic API key, for the editorial step (optional: the detectors and renderer
  also work without it)

### Installation
```bash
python -m venv .venv
.venv/Scripts/activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
cp config.example.yaml config.yaml   # then fill in streamers and API keys
python -m frenzo check               # checks that dependencies and configuration are ready
```

### Usage
```bash
python -m frenzo app                 # desktop review application
python -m frenzo run                 # monitor live streams and produce clips
python -m frenzo vods --url <link>   # process an archived broadcast
python -m frenzo harvest <channel>   # edit the best community clips of a channel
python -m frenzo test <video.mp4>    # run the full pipeline on a local file
```

### Tests
```bash
python -m unittest discover tests
```

---

## Engineering notes

- **Robustness**: validated LLM answers with deterministic fallbacks, overlap-aware deduplication of
  moments across sources, segmented recordings that overlap so no moment is lost at a boundary,
  and safe file writes (write to a temporary file, then rename).
- **Configurability**: every behaviour sits behind a documented configuration flag, so a new
  feature can be turned off without touching the code.
- **Separation of concerns**: detectors, ranking, LLM decision, rendering and review are separate
  modules that can be tested on their own.
- **No automatic publishing**: the system prepares content for publication, and a person always
  makes the final decision.
