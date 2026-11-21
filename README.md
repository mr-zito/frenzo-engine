# Frenzo clipping engine

Works with **Twitch, Kick and YouTube Live**. Watches the streamers you choose. When one goes live
playing GTA 6, it records the stream and flags a moment when:

- **chat explodes** (much faster than normal, lots of KEKW / 💀 / "NO WAY"),
- **the streamer reacts**: yells, laughs, or makes a sudden weird noise or scream,
- **a viewer makes a Twitch clip**.

When the same moment triggers several of these, Claude is told, because that usually means a great clip.
For each moment it:

1. takes the 90 seconds around that moment,
2. transcribes what the streamer says (word by word),
3. sends the transcript, chat, loudness and 4 frames to Claude, which scores the moment 0–10 and picks
   the exact start and end, a hook, a title and a caption,
4. renders a 1080×1920 vertical clip with yellow word-by-word captions, the hook, and your @itsfrenzo tag,
5. saves it in `clips/<platform>_<streamer>/<date>/`.

It **does not post anything**. You watch each clip and post the good ones yourself.

---

## The Frenzo app (recommended)

Everything in one black & yellow window instead of the terminal:

```
run_windows.bat app
```
The first time, this installs what's missing, builds **Frenzo.exe** (a real desktop app with its own
window and the Frenzo icon, in `FrenzoApp\Frenzo\`) and puts a **Frenzo** shortcut on your Desktop.
After that, just double-click the shortcut (you can also pin it to the taskbar or Start menu).
If the .exe can't be built on your PC, the shortcut opens the same app in an Edge window instead.
After an update that changes the window, rebuild it with `run_windows.bat build-exe`.

- **Live clipping:** start / stop with one button, see who is being recorded right now, the moments
  being flagged and new clips as they're saved. Add a streamer by pasting their Twitch, Kick or YouTube
  link; remove one with ×; turn on *Speech listener* (hot takes + NPC talk) or *Any game* per streamer.
- **Past streams:** pick a streamer, search their past broadcasts, tick the ones to clip (or paste
  links), choose how many clips per stream, and go.
- **Clips:** every clip with its score and thumbnail. Open one to watch each version (clean, TikTok &
  Instagram, YouTube, X), copy the post text, tick where you posted it, open its folder or delete it.
  The **Edit** tab plays raw.mp4 (a minute before and after) so you can:
  - drag the yellow handles to set your own start and end, change the title, the yellow word, the emoji,
    move the gameplay crop (drag the yellow band on the video), redraw the webcam box (and keep it for
    that streamer's future clips), then **Render my cut**;
  - or write what's wrong ("the title is wrong, it's an NPC not Lucia") and **Re-edit with Claude**.
- **Edit a title:** click the title (or the pencil) on a clip's page, change it, the yellow word or the
  emoji, and save: the clip is re-rendered with the same cut (jump cuts, captions, callouts kept).
  The post text (post title, caption, hashtags) is edited the same way, without re-rendering.
- **Your context:** after watching a clip, write what really happens in your own words. It's saved
  with the clip, and Claude treats it as the truth: *Suggest titles from this* gives 4 title ideas
  (click one to use it), *Re-edit the clip with this* re-cuts the clip and its title from it, and
  later re-edits ("Re-edit all old clips") use it automatically.
- **Recordings:** every live stream is saved in **one-hour parts** (they overlap by a few minutes, so
  nothing is lost when a new part starts). A finished hour is prepared for playback automatically (a
  fast copy to .mp4, same quality). Pick an hour to watch it with an activity graph under the video:
  yellow bars = chat activity, grey line = loudness, yellow marks = clips already made, white dots =
  moments the engine flagged (the list shows which ones it skipped). Drag on the graph (or use
  *Set start* / *Set end*, keys I / O) to select a moment, then **Make clip as is** (your title, exact
  cut) or **Make clip with Claude** (Claude titles it and removes dead air inside your selection).
  Tick two parts next to each other and **Merge** them to cut a moment that crosses the hour change.
  Delete an hour (or a whole stream) with the bin icon. Recordings are **never deleted automatically**
  unless you set *Delete recordings after (days)* in Settings. Each streamer uses about 2-3 GB per
  hour, so delete the hours you've checked: the top of the tab shows the space used and left, and
  warns you when the disk is getting full. Leftover temporary files are cleaned every 2 hours.
- **Post:** on a clip's page, the **Post** tab has a form for each platform (TikTok, Instagram,
  YouTube Shorts, X, Facebook) with the text already written in that platform's format and a character
  counter. Click the button: your browser opens that platform's upload page, the clip's folder opens
  with the right version selected (the one with that platform's handle), and the caption is copied.
  Drag the file onto the page, press Ctrl+V, post. (On X the text is filled in for you.) Then paste the
  post's link and click *Mark as posted*: the link stays with the clip. Websites never accept a file
  automatically from another program, so that one drag is the only manual step; it's free and needs
  no developer accounts.
- **Activity:** everything the engine does, job by job. **Settings:** Claude model, clips per hour,
  minimum score, margins, captions and more (saved to config.yaml).

Closing the window keeps live clipping running in the background: Frenzo stays as an icon next to
the clock (right-click it: Open Frenzo / Quit Frenzo) and pops up a notification for each new clip.
Double-click the shortcut or the icon to get the window back. **Quit Frenzo** (bottom left of the app,
or in the icon's menu) stops everything. The app only runs on your PC
(http://127.0.0.1:8765). Don't also run the engine from a terminal at the same time.

## Step 1: Install the tools (once)

**Windows**
1. Install **Python 3.11 or newer** from python.org/downloads. On the first screen of the installer,
   tick **"Add python.exe to PATH"**.
2. Install **ffmpeg**: open *PowerShell* and run `winget install Gyan.FFmpeg`.
   Close PowerShell and open a new one. Typing `ffmpeg -version` should print a version number.

**Mac**
1. Install Homebrew from brew.sh (copy the command shown on the site into Terminal).
2. In Terminal, run `brew install python ffmpeg`.

## Step 2: Put the engine folder somewhere

Unzip `frenzo-engine.zip`, for example to `Documents/frenzo-engine`.
Make sure you have free disk space: a stream recording takes about **3 GB per hour per streamer**.
Recordings are deleted automatically once their clips are done.

## Step 3: Platform keys (optional)

**No keys are required.** Without them the engine checks who's live itself. With Twitch keys it
also uses the clips viewers make on Twitch as an extra signal. YouTube never needs keys.

### 3a. Twitch

1. Log in at **dev.twitch.tv/console** with a Twitch account. Twitch asks you to turn on
   two-factor authentication first: do it in Twitch → Settings → Security.
2. Click **Register Your Application**:
   - Name: `frenzo-engine-<something>` (it must be unique)
   - OAuth Redirect URL: `http://localhost`
   - Category: *Application Integration*
   - Client type: *Confidential*
3. Click **Manage** on your new app. Copy the **Client ID**, then click **New Secret** and copy the
   **Client Secret**.

### 3b. Kick

1. Log in at **kick.com**, then open **kick.com/settings/developer** (if Kick asks you to turn on
   two-factor authentication first, do it in your Kick security settings).
2. Create an app: any name (e.g. `frenzo-engine`), redirect URL `http://localhost`. Tick the
   *read channel information* permission if asked.
3. Copy the **Client ID** and **Client Secret**.

## Step 4: Claude (two options)

**Option A: your Claude plan (Pro or higher), no extra cost.** Install Claude Code and sign in with
your Claude account. The engine finds it automatically and uses it to judge moments. This uses your
plan's normal usage limits (shared with your Claude chats), so the engine uses the small *haiku*
model and judges at most 30 moments per hour (`max_checks_per_hour`). If you hit your plan's limit,
moments fall back to the simple loudness rule until it resets.

**Option B: a paid API key** (no effect on your plan's limits):

1. Go to **console.anthropic.com**, create an account, and add a few dollars of credit under *Billing*.
2. Go to **API Keys → Create Key** and copy the key (it starts with `sk-ant-`).

Each moment is one request with a little text and 4 small images. After the first day, check
*Usage* in the console to see what it really costs you.

## Step 5: Fill in config.yaml

Run the engine once (Step 6). It creates `config.yaml` and stops. Open `config.yaml` with Notepad
(Windows) or TextEdit (Mac) and fill in:

- the `twitch` and/or `kick` keys (Step 3). Leave the ones you don't use as they are.
- `api_key` (Step 4)
- `streamers`: the streamers whose **clipping campaigns you joined**, one block each, with their
  platform and name. Delete the example blocks you don't need.
  ```yaml
  streamers:
    - platform: twitch          # from twitch.tv/streamerone
      login: "streamerone"
      tags: ["#gta6", "#gtavi"]
    - platform: kick            # from kick.com/streamertwo
      login: "streamertwo"
      chatroom_id: 12345678
      tags: ["#gta6"]
    - platform: youtube         # from youtube.com/@streamerthree
      login: "@streamerthree"
      tags: ["#gta6", "#shorts"]
  ```
- **Kick `chatroom_id`** (needed to read Kick chat): open
  `https://kick.com/api/v2/channels/<their kick name>` in your browser, press Ctrl+F (Cmd+F on Mac),
  search `"chatroom":{"id":` and copy the number right after it. It never changes, so you only do this
  once per streamer. Without it, the engine tries to find it itself, and if it can't, it uses sudden
  loudness instead of chat to spot moments (works, but less precise).
- **How the game is detected:** Twitch and Kick use the stream's category. YouTube has no game
  category, so the engine records a YouTube stream only if its **title** contains one of the
  `game.keywords` ("GTA 6", "GTA VI", ...). For a YouTuber who never puts the game in the title, add
  `any_game: true` to their block.
- **facecam** (optional, but clips look much better with it): where the webcam sits on that
  streamer's screen, as fractions of the full picture.
  `x` = distance from the left edge, `y` = distance from the top, `w` = webcam width, `h` = webcam height.
  Example: a webcam in the top-right corner covering a quarter of the width and about a third of the
  height is `{x: 0.75, y: 0.0, w: 0.25, h: 0.33}`. Take a screenshot of their stream to estimate it.
  With a facecam, clips are "webcam on top, gameplay below". Without one, the full picture sits in the
  middle with a blurred copy behind it.

**Your handles:** each clip is made once per handle, set under `handles:` in config.yaml
(TikTok/Instagram @its_frenzo, YouTube @itsfrenzo, X @its_frenzohq), plus a clean version with
no handle. If you change a handle on a platform, change it there too.

## Step 6: Run it

**Windows:** double-click `run_windows.bat`. It finds Python (including Anaconda) and installs
ffmpeg by itself if it's missing.
**Mac:** right-click `run_mac.command` → Open. The first time, run `chmod +x run_mac.command` in
Terminal from this folder if it won't open.

The first run installs everything it needs (a few minutes). The first clip also downloads the
speech model (about 500 MB for `small`).

**Check your setup** by opening a terminal in this folder and running:
```
run_windows.bat check        (Windows)
./run_mac.command check      (Mac)
```
Every line should say `OK`.

## Step 7: Test it on a past stream (recommended)

Download 30 minutes of a past GTA 6 stream (a Twitch VOD link looks like `twitch.tv/videos/123456789`;
a YouTube video link `youtube.com/watch?v=...` works too):
```
.venv\Scripts\python -m streamlink --hls-duration 30:00 -o vod.ts https://www.twitch.tv/videos/123456789 best
run_windows.bat test vod.ts --streamer StreamerName
```
(On Mac use `.venv/bin/python` and `./run_mac.command`.) The engine listens to the whole video,
lists the streamer reactions it hears (yelling, laughing, outbursts), takes the 3 strongest, asks
Claude about them, and saves the ones that score high enough. `--top 10` tries more moments. Use `--at 754` to test a
moment you know (754 seconds in), or `--no-ai` to test without Claude.

## Step 8: Leave it running

Double-click the run file and leave the window open. The engine checks every minute who is live.
Your PC must stay on and awake (turn off sleep mode while it runs). Press **Ctrl+C** to stop.

What you'll see in the window:
```
[21:04:12] [twitch:streamerone] LIVE: GTA 6 DAY 2 - BECOMING A COP
[21:19:40] [twitch:streamerone] moment flagged -> chat spike: 4.2x faster than normal
[21:20:31] [streamerone] score 8/10 - cop chase ends with the car flying off the pier
[21:20:58] [streamerone] SAVED clip (31s, score 8) -> clips/twitch_streamerone/2026-11-19/212031_s8_...
```

## Clip streams that already ended (past broadcasts)

```
run_windows.bat vods                 (clips past streams from the last 36 hours)
run_windows.bat vods --hours 12 --top 3
run_windows.bat vods --any-game      (also streams whose title doesn't mention GTA)
run_windows.bat vods --url https://www.twitch.tv/videos/123456789 --streamer lacy
```
For each past stream of your streamers (Twitch past broadcasts, Kick videos, YouTube past live streams) it
downloads only the audio, finds the strongest streamer reactions, downloads just ~90 seconds of
video around each one, and makes the clips. They're saved in the same per-streamer folders.
Streams already done are remembered, so running it again only picks up new ones.
Past streams count as GTA if the title **or the stream's category** mentions it.
You can run it in a second window while the live engine is running.

## The clip style (tuned on viral GTA 6 clip pages)

- **top third:** the streamer's webcam, cropped tight on the face (found automatically),
- **title on the line between webcam and gameplay**, written by Claude in sentence case with ONE
  keyword in yellow and an emoji at the end: "Insane **GTA 6** bug 💀", "Bro wants **Lucia** 😱",
- **small captions** of what the streamer says (1-3 words at a time) in the middle of the gameplay,
- **coloured callouts** on the key beats: "*Laughing*", "+$1k" (green), "Gawd~" (pink), "PS5 stick drift" (red),
- **context first:** the clip opens on the setup (with 1.5 s of margin) so viewers understand what's
  happening, and keeps 2 s after the reaction. If the setup happened earlier, the clip opens with a short
  setup shot and jump-cuts to the payoff, like ohnePixel's clips (`lead_in`, `tail`, `min_setup` in config),
- **jump cuts:** dead air removed, every piece at least 2 s, the clip ends on the reaction,
- **12-35 s** (most 15-25 s), no background music, the sus sound only on sus/spicy moments,
- **swear/sensitive words censored** (dr\*nk, f\*cking) to stay advertiser-friendly,
- the gameplay centred on the player's character or vehicle.

Turn parts off in config.yaml under `render:` (`captions: false`, `jump_cuts: false`, `title_bar: false`).
Fonts (Montserrat, Noto Color Emoji) are bundled in `assets/fonts` (free SIL Open Font License).

### Your own sound effects
The engine comes with its own original "sus" sound. To use your favourite meme sus sound instead,
download it (free sources: Pixabay sound effects, Freesound, Myinstants) and drop the .mp3/.wav file
into `assets/sfx/sus/`. If the folder has several files, one is picked at random.
(Other effects exist but are off: add them to `sfx_types` in config.yaml if you ever want them.)
Only use sounds you're allowed to use: check each sound's license.

**Spicy moments are kept, nudity isn't:** clips showing exposed nudity are skipped
(`skip_nudity: true`) because TikTok, Instagram and YouTube remove them and can restrict the account.
Suggestive dancing, bikinis and flirting are kept.

## Picture quality

Streams are recorded in **1080p60**, the webcam and gameplay are enlarged with a high-quality
(lanczos) resize plus light sharpening, and clips are encoded at very high quality (`crf: 18`,
`preset: slow`). All handle versions come from one single encode, so they're exactly as sharp as the
clean one. If your PC struggles to keep up (clips piling up late), set `preset: "medium"`. If your
internet struggles with 10 streams, set `quality: "936p60,720p60,best"`.

## Sound always in sync

Twitch removes ad breaks from recordings, which leaves short gaps in the sound. The engine fills
those gaps with silence at the exact right place, so the voice stays matched with the webcam and
the gameplay (tested: 0.01 s offset), in both clip.mp4 and raw.mp4.

## How moments are picked

Signals that flag a moment (each stream is compared with its own normal level):
- **Gameplay chaos (bugs & crazy physics, the #1 viral GTA 6 topic):** the picture suddenly goes
  much wilder than usual for at least half a second (car flying, ragdolls, spinning camera). Menu or
  map cuts are ignored. Works with no chat at all.
- **Chat:** speed spikes; sus / ayo / 🤨; thirsty 🥵 / gyat; **bug / glitch / physics** spam;
  **NPC / weird** ("bro the npc", "what is he doing", "cursed"); **gameplay hype** ("clean",
  "that chase was a movie", "graphics", "looks real").
- **Streamer reactions:** yelling, laughing, sudden screams.
- **Hot takes and NPC talk** (streamers with `hot_takes: true`): the engine keeps listening and flags
  strong opinions about the game ("honestly GTA 6 is…", "Rockstar cooked", "overrated") and the streamer
  talking about an NPC ("this guy is tweaking", "what is she doing", "NPCs in this game…"), in English or French.
  On by default for Kai Cenat, Lacy, Marlon and JasonTheWeen. It uses some PC power per streamer.
- **Viewer clips** (with Twitch keys).

**Running gags:** when 3+ moments happen within 3 minutes, they become ONE long window so Claude can
stitch the best repetitions into a clip that escalates (like "another beer… one more… one more").

Claude then looks at 14 frames around the moment, the transcript and chat. Bugs and physics come
first, then funny/weird NPCs, funny moments, engaging gameplay (chases, stunts, "too real" moments),
sus, spicy, big reactions and hot takes. It keeps only clips that would work as a 15-30 s short.

**Accurate titles:** Claude first writes down what actually happens (`what_happens` in meta.json),
then writes a title that matches it. It knows GTA 6's two main characters, **Lucia** and **Jason**, are
the player characters and never NPCs, never calls an NPC Lucia or Jason, and never mixes up the
streamer JasonTheWeen with the game's Jason. If the picture doesn't make it clear, it titles the
reaction instead of guessing.

**Past streams** (`vods`) download a tiny low-resolution copy first (~300 MB for 4 h) to listen for
reactions, watch for gameplay chaos and (for hot_takes streamers) hot takes, then download only the
moments in full quality.

## Fix clips made by an older version

```
run_windows.bat fix                       (all clips)
run_windows.bat fix --streamer jasontheween
```
Claude re-watches every clip in its `raw.mp4` (a minute of context each side) and re-cuts it with the
current engine: a correct title (the old one is kept in meta.json as `old_title`), the setup shown at
the start, the reaction kept at the end, jump cuts, webcam, focus, one version per handle. The folder is
renamed to the new title; "posted" flags are kept. Without Claude, clips keep their title and only get
the new style and margins. If Claude's hourly limit is reached it stops: run it again later, finished
clips are skipped.

## What's in each clip folder

| File | What it is |
|---|---|
| `clip_tiktok+instagram.mp4` | The clip with **@its_frenzo**: post on TikTok and Instagram |
| `clip_youtube.mp4` | The clip with **@itsfrenzo**: post on YouTube Shorts |
| `clip_x.mp4` | The clip with **@its_frenzohq**: post on X |
| `clip_clean.mp4` | The clip with no handle: Facebook, or campaigns that don't allow watermarks |
| `caption.txt` | Title, caption and hashtags to copy and paste |
| `thumb.jpg` | A preview frame |
| `raw.mp4` | The uncut horizontal piece **with 1 extra minute before and after**, so you see the full context and can pick your own cut. `caption.txt` says at which second the clip starts inside it |
| `meta.json` | Score, Claude's reason, exact timing |
| `captions.ass` | The caption file (you can ignore it) |

`clips/clips_log.csv` lists every clip; open it in Excel or Google Sheets.
Folder names start with the score (`_s8_`), so you can post the best ones first.

## How the streamer-reaction detector works

Every 8 seconds the engine listens to the newest audio of each stream. During the first minutes it
learns that streamer's **normal talking voice** (how loud, how high). Then it flags:

- **Yelling**: clearly louder *and* higher-pitched than their normal voice for at least half a second.
- **Laughing**: a regular "ha-ha-ha" rhythm (5+ bursts, 3–6 per second).
- **Sudden outbursts**: a very loud sudden sound (scream, weird noise, banging the desk).

It focuses on the voice frequencies, so game explosions and engine rumble count much less.
Because it compares to each streamer's own voice, a naturally loud streamer doesn't trigger it all
the time. Claude still checks every flagged moment, so a false alarm costs one Claude request and
never becomes a clip unless it's actually good.

## Settings worth tuning (config.yaml)

- **Too many voice-reaction moments?** `voice_sensitivity: "low"`. Missing reactions? `"high"`.
  To turn it off: `voice_triggers: false`.
- **Too many weak clips?** Raise `min_score` to 7 and/or `spike_multiplier` to 4.
- **Missing moments in small chats?** Lower `min_messages_in_window` to 8.
- **Captions wrong or slow?** `transcription.model: "medium"` is more accurate. With an NVIDIA
  graphics card, set `device: "cuda"` (much faster). Set `language: "en"` if all streams are in English.
- **Several streamers at once on a strong PC:** `workers: 2`.

## Good to know

- **Always watch a clip before posting.** Claude is good, but it isn't perfect.
- Only add streamers whose clipping campaign you joined, and follow each campaign's rules
  (required tags, length, credit).
- Twitch ad breaks are skipped in the recording. Right after an ad, a clip's timing can be off by a
  few seconds; the 90-second window usually absorbs it.
- Captions can't show emoji (the caption font has none), so Claude is told not to use them in hooks.
- If a streamer switches to another game, recording stops until they play GTA 6 again.
- Kick and YouTube chat are read the same way their websites read them (no official chat feed
  exists for this). If one of those platforms changes its site, chat may stop working until the
  engine is updated; the loudness fallback keeps finding moments in the meantime.
- YouTube live streams have a longer delay than Twitch, so moments there may take a little longer to
  appear.

## Troubleshooting

| Message | Fix |
|---|---|
| `'ffmpeg' was not found` | Redo Step 1, then open a **new** terminal window |
| `Twitch login failed` | Client ID/secret copied wrong, or the secret was regenerated: create a new one |
| `Twitch user 'x' not found` | Check the spelling of `login` in config.yaml |
| `Kick login failed` | Kick Client ID/secret copied wrong; create a new secret |
| Kick streamer: `using loudness to find moments instead` | Add their `chatroom_id` (Step 5) |
| A YouTube streamer is live but not recorded | Their title doesn't mention GTA: add `any_game: true` |
| `chat disconnected` (Kick/YouTube) keeps repeating | Update the engine's tools: `.venv\Scripts\python -m pip install -U yt-dlp streamlink websocket-client` |
| `no Anthropic API key` | Fill `api_key`, or add credit in the Anthropic console |
| Clips have no captions | Run the check; `faster-whisper` must say OK |
| Webcam band shows the wrong area | Adjust that streamer's `facecam` numbers |
