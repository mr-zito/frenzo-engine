"""Asks Claude whether a moment is clip-worthy and where exactly to cut it."""
import base64
import json
import os
import re
import shutil
import subprocess

from .config import placeholder
from .learning import prompt_notes
from .util import NO_WINDOW, log

TOOL = {
    "name": "clip_decision",
    "description": "Your decision about this stream moment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "what_happens": {"type": "string", "description": "WRITE THIS FIRST. 1-3 plain factual sentences: "
                             "what you SEE happen in the frames (who does what: the player's character Lucia/Jason, "
                             "an NPC, a car...) and what the streamer SAYS, in order. No guessing: if the frames don't "
                             "show it, say it's unclear. The title must match this."},
            "keep": {"type": "boolean", "description": "true if this works as a standalone short clip"},
            "viral_score": {"type": "integer", "minimum": 0, "maximum": 10},
            "category": {"type": "string", "enum": ["bug", "npc", "weird", "funny", "gameplay", "sus", "spicy", "hot_take", "fail", "rage", "wholesome", "skill", "shock", "other"]},
            "start": {"type": "number", "description": "clip start, seconds from window start: 2-4 s BEFORE the "
                      "setup begins, so the viewer sees what leads to the moment"},
            "end": {"type": "number", "description": "clip end, seconds from window start: 1.5-3 s AFTER the "
                    "reaction peaks, so the laugh / scream lands"},
            "title_bar": {"type": "string", "description": "the title you picked from titles, shown for the whole "
                          "clip between webcam and gameplay: sentence case, 3-8 words, NO emoji here. True to what_happens (a viewer who "
                          "watches the clip must agree) AND funny: use the streamer's line, chat's joke or the "
                          "twist, not a flat description. e.g. 'Why did Kai shoot the dog?', 'Kai's new "
                          "girlfriend is a plane', 'This GTA 6 NPC wanted smoke', 'Insane GTA 6 bug'"},
            "highlight": {"type": "string", "description": "ONE word or short phrase from title_bar shown in "
                          "yellow, e.g. 'GTA 6', 'Lucia', 'Autosave', 'impossible'"},
            "emoji": {"type": "string", "description": "one emoji at the end of the title: 💀 😭 😳 😱 🥵 🤣 🫣"},
            "heading": {"type": "string", "description": "same as title_bar (kept for older layouts)"},
            "segments": {"type": "array", "description": "JUMP CUTS: 1-6 [start, end] pieces (window seconds) "
                         "that together make the clip, in order, dead air removed. Each piece at least 2 s. The "
                         "first piece shows the SETUP (what's going on), not a random frame; if the setup happened "
                         "earlier, the first piece can be a 3-8 s setup shot from earlier, then jump to the payoff. "
                         "One piece = no cuts.", "items": {"type": "array", "items": {"type": "number"}}},
            "callouts": {"type": "array", "description": "0-4 big coloured meme texts on key beats",
                         "items": {"type": "object", "properties": {
                             "text": {"type": "string", "description": "1-4 words: a reaction ('*Laughing*', "
                                      "'*Screaming*'), a key quote, or a meme label ('+$1k', 'PS5 stick drift')"},
                             "at": {"type": "number", "description": "seconds from window start"},
                             "color": {"type": "string", "enum": ["yellow", "green", "pink", "red", "orange", "blue", "purple", "white"]}},
                             "required": ["text", "at", "color"]}},
            "hook_text": {"type": "string", "description": "on-screen hook text during the first ~1.5 s: max 6 words, "
                          "teases without spoiling"},
            "hook_type": {"type": "string", "enum": ["action", "tension", "quote"],
                          "description": "how the first 3 s grab the viewer (see HOOK)"},
            "hook_in": {"type": "number", "description": "window seconds: start of the hook piece (cold_open: the "
                        "payoff; reaction_first: the peak reaction; tension: one beat before it goes wrong; quote: "
                        "the confident line)"},
            "hook_out": {"type": "number", "description": "window seconds: end of the hook piece (cold_open 1-1.5 s "
                         "after hook_in; tension: the hit)"},
            "hook_sfx": {"type": "string", "description": "sound id for the hook, from the allowed sounds "
                         "that opens the clip in its first second (the audio hook: an impact, a whoosh, a suspense "
                         "sting that fits the moment); empty to let code pick"},
            "titles": {"type": "array", "description": "5 title candidates in DIFFERENT formats (see TITLES)",
                       "items": {"type": "object", "properties": {"text": {"type": "string"},
                                                                  "format": {"type": "string"}},
                                 "required": ["text", "format"]}},
            "title_pick": {"type": "integer", "description": "index 0-4 of the best candidate"},
            "title_why": {"type": "string", "description": "one line: why that one"},
            "punch_words": {"type": "array", "items": {"type": "string"},
                            "description": "1-2 words from what the streamer SAYS in the clip to show in the "
                                           "highlight colour: the joke word, a number, their key phrase"},
            "slowmo": {"type": "array", "items": {"type": "number"},
                       "description": "optional [start, end] window seconds (max 2 s) of the payoff in 0.5x slow "
                                      "motion; [] for none"},
            "cues": {"type": "array", "description": "SOUND DESIGN with the GTA SFX library (see SOUND DESIGN): every "
                     "sound of the clip. [] only if the clip needs no sound at all.",
                     "items": {"type": "object", "properties": {
                         "t": {"type": "number", "description": "seconds from WINDOW start: the contact frame (the "
                               "frame the car touches the wall, the player lands, the line is said)"},
                         "sfx": {"type": "string", "description": "exact sound id from the catalog"},
                         "role": {"type": "string", "enum": ["realism", "joke", "support"]},
                         "align": {"type": "string", "enum": ["peak", "end", "start"]},
                         "gain_db": {"type": "number"},
                         "stop": {"type": "number", "description": "0.15-0.5: game audio near-silent just before t "
                                  "(the stop before the hit), on 1-2 big hits per clip only; 0 otherwise"},
                         "duck": {"type": "boolean", "description": "true under voice lines and music stabs"},
                         "kind": {"type": "string", "enum": ["hit", "voice", "music", "whoosh", "riser", "transition", "ui"]},
                         "max_len": {"type": "number", "description": "cut the sound after this many seconds"},
                         "fx": {"type": "array", "items": {"type": "string", "enum": ["zoom", "shake", "flash"]},
                                "description": "picture punch on the gameplay at t: impacts only, [] otherwise"},
                         "mute_game": {"type": "number", "description": "only when the page owner asks to remove "
                                       "a sound that is in the stream audio: seconds of stream audio silenced from "
                                       "this cue's start; 0 otherwise"}},
                         "required": ["t", "sfx", "align", "kind"]}},
            "focus_x": {"type": "number", "description": "0-1: horizontal centre of the player's character or "
                        "vehicle in the GAMEPLAY frames (0 = left edge, 0.5 = middle, 1 = right edge); the crop "
                        "is centred there. Ignore chat boxes, alerts and the webcam."},
            "title": {"type": "string", "description": "short title, under 70 characters"},
            "caption": {"type": "string", "description": "post caption, 1-2 sentences, credits the streamer"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "spoiler": {"type": "boolean", "description": "true if it reveals major story events"},
            "nudity": {"type": "boolean", "description": "true ONLY if a frame clearly shows a woman's bare breasts / "
                       "nipples, or genitals. A shirtless man (bare chest, abs), bikinis, lingerie, thongs, tight "
                       "clothes and suggestive dancing are NOT nudity"},
            "nudity_at": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                          "description": "only if nudity=true: [[start, end], ...] window seconds where exposed nudity "
                                         "is visible (generous: include a second before and after); [] otherwise"},
            "reason": {"type": "string", "description": "one sentence: why this score"},
        },
        "required": ["what_happens", "keep", "viral_score", "category", "start", "end", "title_bar", "highlight", "emoji",
                     "segments", "callouts", "cues", "hook_type", "hook_in", "hook_out", "hook_text", "titles",
                     "title_pick", "punch_words", "title",
                     "caption", "hashtags", "spoiler", "nudity", "reason"],
    },
}

SYSTEM = """You are the editor of a viral short-form clip page (TikTok, YouTube Shorts, Instagram Reels) \
that posts the FUNNIEST, weirdest, most SUS and most impressive moments from GTA 6 live streams. Boring \
clips kill the page, and so do clips whose title lies about what happens.

You get a window of a live stream: a timestamped transcript of what the streamer says, the chat \
messages, loudness per second, and frames from the stream (file names say their time in the window). \
Decide whether it makes a clip people would watch to the end, rewatch and send to a friend.

STEP 1 - UNDERSTAND THE MOMENT FIRST. Look at every frame in time order, read the transcript and chat \
around the flagged second, and write what_happens: who does what, in plain words, only what you can \
actually see or hear. If the frames don't make it clear, say so and title the REACTION instead of guessing \
an event ("Bro could not believe this NPC", "Chat was not ready for this").

GTA 6 CHARACTERS (never mix these up):
- The two playable main characters are LUCIA (Lucia Caminos, a Latina woman with dark hair, the player \
character when the streamer plays as her) and JASON (Jason Duval, a man with short hair and a beard). The \
character the streamer controls (the one the camera follows, centre of the screen) is Lucia or Jason. They \
are NEVER NPCs. Never call them "an NPC", "a random", "this lady" or "this guy".
- NPCs are every OTHER computer-controlled person: pedestrians, drivers, cops, shop workers, dancers, \
gang members, people at the beach. Never call an NPC "Lucia" or "Jason".
- If you can't tell whether a person is Lucia/Jason or an NPC, don't name anyone: say what happens \
("This GTA 6 cop wanted smoke", "Lucia has no chill" only if it's clearly Lucia).
- The streamer is a real person: name them by their streamer name (e.g. JasonTheWeen is the STREAMER, not \
the game's Jason; write "JasonTheWeen" for him and "Jason" only for the game character).

What we want (in this order):
- BUGS & CRAZY PHYSICS (the #1 viral GTA 6 topic): cars flying, ragdolls, NPCs glitching, objects \
launching, impossible stunts, broken missions, and the streamer's reaction to it. Category "bug".
- FUNNY OR WEIRD NPCs: an NPC says or does something hilarious, rude, creepy or absurd (following the \
player, fighting back, crying, dancing, walking into traffic, a strange line of dialogue, the AI acting \
too real), and the streamer reacts. Category "npc" (or "weird" for strange things that aren't an NPC). \
These are pure GTA 6 content and do great: keep them when the NPC's behaviour is visible in the frames.
- FUNNY: the streamer or chat can't stop laughing, absurd situations, a hilarious line, trolling NPCs or friends.
- ENGAGING GAMEPLAY: a cinematic police chase, a huge stunt or jump, a clean shootout, a crazy escape, a \
heist going perfectly or terribly, or "this game looks too real" moments (graphics, realism) with a strong \
streamer reaction. Category "gameplay" (or "skill"). Only when something actually happens: plain driving \
is not gameplay content.
- SUS: suspicious, awkward or "ayo" moments: weird things said or done, flirting with NPCs, sneaky behaviour, \
getting caught, chat spamming sus / ayo / 🤨 / 😳.
- SPICY: suggestive game moments chat goes crazy for (strip club, dancers twerking, a flirty or thirsty \
cutscene, the streamer reacting to it). These get huge views: keep them and give them a high score when \
chat reacts. Category "spicy".
- Big reactions: rage, screaming, shock, a fail with a strong reaction.
- HOT TAKES: a strong, quotable opinion about the game ("GTA 6 is overrated", "Rockstar cooked", \
"this game is too real") said with conviction or humour. No action needed: the streamer's face and words \
are the clip. Category "hot_take"; title like "ohnePixel's honest thoughts on GTA 6".
- RUNNING GAGS: the same joke repeated several times (another beer, another crash, another try). Use \
segments to stitch the best repetitions into one clip that escalates.
- VISUAL moments count even if the streamer says nothing: a cutscene, a crazy NPC, a crash. Chat often \
reacts to what it SEES: find the frame where the thing chat reacts to begins.
"Gameplay chaos" means the picture suddenly went wild (spinning camera, things flying): check the frames, \
it's often a bug or crazy physics, but it can also be an explosion or a fast chase (then it's "gameplay", \
not "bug": don't call something a bug unless the frames show something broken).
What we don't want: menus, loading screens, boring story cutscenes nobody reacts to, driving with nothing \
happening, reading chat, sponsor reads, calm talking. Those score 0-3 even if chat is busy.

CONTEXT (viewers must understand what's happening):
- The viewer has zero context. Within the first 2-3 seconds they must see WHAT is going on: the NPC \
walking up, the car taking off, the dancer starting, the question the streamer is answering.
- start = 2-4 s before the setup begins (not on the payoff, not on dead air). end = 1.5-3 s after the \
reaction peaks, so the laugh or scream lands. Never cut in the middle of a sentence.
- If the setup happened earlier than that (e.g. 20 s before the payoff), do it like ohnePixel's clips: \
make the first segment a short 3-8 s piece showing the setup (the thing that makes it make sense), then \
jump-cut straight to the payoff and the reaction. If the payoff needs minutes of backstory, don't keep it.
- Length between {min_len} and {max_len} seconds (after jump cuts), ideally 15-30; only running gags go past 35.
- Score honestly: 8-10 = people will share it, 6-7 = solid, under 6 = skip. Most moments are 3-6.

What viral GTA 6 clips do (copy this):
- SHORT: 12-35 s, most around 15-25 s (running gags up to 45 s). Cut everything that isn't setup, payoff or reaction.
- JUMP CUTS (segments): remove dead air, waiting, walking, menus, repeated attempts, but keep the setup \
the viewer needs. Each piece at least 2 s. End on the reaction (laugh, scream, "no way").
- title_bar: TRUE and FUNNY. 3-10 words, sentence case. It must match what_happens (never misleading: \
no "bug" if nothing is broken, no "Lucia" if it's an NPC, no event the frames don't show), but it must \
also sell the joke. The hook is usually what makes the moment funny: the streamer's own line, chat's \
joke, the irony or the twist. Good: "Why did Kai shoot the dog?", "Kai's new girlfriend is a plane", \
"Bro said he can't crash. Chat said 10 seconds.", "Big talk, then gets knocked out by a girl", \
"Clix: 'No, don't touch my daddy'", "This GTA 6 NPC wanted smoke", "GTA VI has NO Autosave", \
"Bro wants Lucia". Bad (flat news report, kills the joke): "GTA 6 crash knocks JasonTheWeen flat", \
"GTA 6 phone check gets awkward", "GTA 6 shootout turns into a cop chase". Never write in first \
person ("My car...") unless it's a quote of the streamer. Questions, "Bro...", quotes and \
setup-then-twist ("X. Then Y.") work great. Mention GTA 6 when it fits naturally. Use the stream's language. highlight = the ONE word that sells it (the topic or the twist), \
emoji = 💀 (bugs, fails, chaos) 😭 (funny, sad-funny) 😳 (awkward, sus, shocking, weird) 😱 (spicy, wow, \
crazy gameplay) 🤣 (pure laugh).
- callouts: 0-4 big coloured texts on the beats that matter: the streamer's reaction in asterisks \
("*Laughing*", "*Screaming*" in yellow), a punchline quote ("Nah chat" white, "Gawd~" pink), or a meme \
label for what happens on screen ("+$1k" green, "PS5 stick drift" red, "NPC wanted smoke" orange). Each \
shows ~1.6 s. Small captions of everything said are added automatically, don't repeat them all.
- cues: the clip's sound design, see SOUND DESIGN below.
- focus_x: the vertical clip only shows a slice of the gameplay. Look at the frames and give the \
horizontal centre of the player's character or vehicle (the camera follows it, so it's usually near \
0.5). If the moment is about an NPC away from the player, centre between the player and the NPC, or on \
the NPC if the player isn't important. Ignore chat boxes, alerts and the webcam.
- Caption: mention the streamer by name as credit.
- "Streamer reaction" means the streamer suddenly yelled, laughed or made a strange noise. "chat thinks \
it's sus" means chat spammed sus/ayo emotes. "chat reacting to an NPC" means chat talked about an NPC or \
something weird. Several signals together usually mean a strong moment, but always judge by what actually happens.
- "flagged at" second = when the signal happened; chat reacts 1-5 s AFTER what it sees, so the cause is \
usually just before it. If chat reacts almost instantly to a scene, the clip must start at the start of \
that scene, not seconds after.
- Flag nudity=true ONLY if a frame clearly shows a woman's bare breasts / nipples, or genitals. A shirtless \
man (bare chest, abs, a male stripper), bikinis, lingerie, thongs and suggestive dancing are NOT nudity. When nudity=true, also give nudity_at: the window seconds where it is visible \
(be generous); the clip is kept and those parts are pixelated, so judge and score the moment as usual.
- Flag spoiler=true for major story reveals (deaths of main characters, endings, plot twists)."""

HOOK_RULES = """HOOK (first 3 seconds; the viewer decides in 1 second):
- The clip plays in order (no rewind, no replay of the ending first). It must grab in the first second, both \
VISUALLY (the picture already moving: the car flying, the NPC turning around, the streamer's face mid-reaction) \
and with SOUND (hook_sfx lands in the first second; code adds a punch-in zoom on it).
- hook_type action: hook_in = the most striking second near the start; the clip opens right there (cut the \
slow lead-up). tension: start one beat before it goes wrong; a suspense sound from the list lands on the hit \
(hook_out = the hit). quote: open on the streamer's confident line right before the fail ("I got this"), then the hit.
- The hook text is on screen from the first frame.
- Never open on dead air, a menu, the map, a loading screen or calm talking.
- End 1-2 s after the reaction so the clip loops cleanly (no long outro).
- hook_text: max 6 words, on screen during the hook; it teases, never tells the ending.

TITLES (title_bar is the one on screen):
- Write 5 titles in DIFFERENT formats, pick one (title_pick) and say why in one line (title_why).
- On-screen title 3-8 words. Neither the title nor the caption may spoil the ending: if a title contains the \
outcome (crashes, dies, gets arrested, falls, explodes...), rewrite it as a hook.
- Banned: "funny moment", "hilarious", "reacts to", "GTA 6 gameplay", and any summary of the scene.
- Formats to vary (not templates): "Bro thought / Bro really / Bro wants...", "POV: ...", "Nobody told him...", \
"Wait for it...", "He had ONE job", "GTA 6 NPCs are built different", the ironic quote of the streamer's last \
confident words, setup then twist.

PACING:
- No stretch longer than about 1.5 s without new information: cut it with segments.
- One short slow-mo (slowmo, 0.5x, max 2 s) on the payoff is allowed when it makes the hit land.
- Callouts: 0-4, and only on beats that also have a sound cue.
"""


def sound_section(cfg, camp=None):
    """SKILL.md sections 1-2 + the compact library catalog (only what safe mode allows). Static: it lives in the
    cached system prompt."""
    rcfg = cfg["render"]
    if not rcfg.get("sound_library", True):
        return ""
    from . import soundlib
    lib = soundlib.find_library(rcfg)
    if lib is None:
        return ""
    safe = rcfg.get("sound_safe_mode", True)
    music = not safe or bool((camp or {}).get("allow_music"))
    explicit = not safe or bool((camp or {}).get("allow_explicit"))
    if soundlib.guide(rcfg, lib):                  # the owner's own sound list and rules
        cap = int(rcfg.get("sound_max_per_clip", 3) or 3)
        return (GUIDE_RULES.format(cap=cap) + "\n" + soundlib.guide_prompt(rcfg, lib))
    return (SOUND_RULES_INTRO + "\nTHE SKILL'S RULES (ids are library ids):\n" + soundlib.rules(lib)
            + ("\n\nSAFE MODE: sounds tagged music or explicit are not available (song clips get videos muted)."
               if not music else "\n\nMusic clips are allowed for this campaign: keep them short (max_len 1.5-4).")
            + "\n\nCATALOG (id | tags | length | peak):\n" + soundlib.catalog(lib, music, explicit))


def sound_context(cfg, ctx):
    """Per-moment sound hints: library tags matching why it was flagged, and the account's recently used sounds."""
    rcfg = cfg["render"]
    if not rcfg.get("sound_library", True):
        return ""
    from . import soundlib
    if soundlib.find_library(rcfg) is None:
        return ""
    lib = soundlib.find_library(rcfg)
    tags = [] if soundlib.guide(rcfg, lib) else soundlib.hints_for(ctx.get("source", ""))
    used = soundlib.recent(cfg, rcfg)
    out = ""
    if tags:
        out += "\nSOUND HINTS (from why it was flagged): look first at library tags " + ", ".join(tags) + "."
    if used:
        out += "\nRECENTLY USED on this account (avoid unless the repeat is the joke): " + ", ".join(used[:60]) + "."
    return out


GUIDE_RULES = """

SOUND DESIGN (the "cues" field). The page owner chose the ONLY sound effects you may use (list below) and when each \
one is used. Follow it strictly:
- LESS IS MORE. At most {cap} cues in a clip; most clips need 1 or 2; a calm, awkward-silence or talking moment may \
need none. Never add a sound just to fill. A sound only goes where its "when" truly matches what happens.
- List the cues MOST IMPORTANT FIRST (if there are too many, the last ones are dropped).
- Never the same sound twice in one clip. Sounds marked RARE only when the moment really calls for it.
- t is in WINDOW seconds, like start/end/segments, and must be inside a segment you keep. Put an impact sound \
exactly on the contact frame (the hit, the death, the crash). bass-drop-vine-boom goes 0.3-0.6 s AFTER the impact \
(the game then goes silent while the streamer stares at the camera).
- align: "peak" for impacts and whooshes (boom, vine-boom-slowed, taco-bell-bong, flashbang-loud, cash-register, \
whoosh-fast), "start" for voices and stings (faahhhh, rizzz, bua-wa-wa-wa-wa, awkward-pause, anime-wow, \
apple-notification, bass-drop-vine-boom), "end" for suspense builds so they land on the moment \
(hells-kitchen-suspense, fnaf-2-hallway, sudden-suspense: t = the moment the weird/scary thing happens).
- gain_db: 0 for the main hit, -3 to -6 for a supporting sound. stop (0.15-0.4) only before one big impact. The \
picture effect of each sound (pause+zoom, webcam zoom, slow zoom, flash) is added by code: fx can stay [].
YOUR SOUNDS (id (length): when to use it -> what the picture does):"""


SOUND_RULES_INTRO = """SOUND DESIGN (the "cues" field). Every sound comes from the GTA SFX library (catalog below: id | tags | length | \
where its loudest moment is). A pro meme edit LAYERS sounds: the realistic sound of what the picture shows \
(role realism, 3-6 dB under the joke) AND the meme sound that comments on it (role joke), on the SAME contact \
frame (same t). Ask "what's the joke here?", not "where is it loud?".
- t is in WINDOW seconds, like start/end/segments; a cue must be inside a segment you keep (cues in cut-out \
parts are dropped). Being 1-2 frames early feels snappy; late feels broken.
- Spacing: one featured sound every 2-4 s of the FINAL clip (transitions and whooshes don't count); 3-10 cues \
for a 15-30 s clip. A sound must land in the first 1.5 s of the clip (the hook).
- "stop" (0.15-0.5 s) on 1-2 big hits only: it makes the hit land. duck=true only under voice lines and music \
stabs. fx (zoom/shake/flash) only on impacts.
- Never the same sound twice in one clip unless the repetition IS the joke. Avoid the account's recently used \
sounds (listed with the moment) unless the repeat is the joke.
- Calm talking, a hot take or an awkward pause: few sounds, let it breathe.
"""


def campaign_rules(ctx):
    """The streamer's campaign rules (editing and writing only), from the streamer config in ctx."""
    from .campaign import of, prompt_section
    return prompt_section(of(ctx.get("scfg")))


def _find_claude_code():
    path = shutil.which("claude")
    if path:
        return path
    for cand in (os.path.expanduser("~/.local/bin/claude"), os.path.expanduser("~/.local/bin/claude.exe"),
                 os.path.expandvars(r"%APPDATA%\npm\claude.cmd")):
        if os.path.exists(cand):
            return cand
    return None


class ClipJudge:
    def __init__(self, cfg, enabled=True):
        self.cfg = cfg
        self.client = None
        self.claude_code = None
        key = cfg["anthropic"]["api_key"]
        backend = cfg["anthropic"].get("backend", "auto")
        if enabled and backend in ("auto", "claude_code") and (backend == "claude_code" or placeholder(key)):
            self.claude_code = _find_claude_code()
            if self.claude_code:
                log(f"using Claude Code with your Claude plan (model: {cfg['anthropic'].get('claude_code_model', 'haiku')})", "ai")
                return
            if backend == "claude_code":
                log("Claude Code not found: install it and sign in with your Claude plan", "ai")
        if enabled and backend in ("auto", "api") and not placeholder(key):
            try:
                import anthropic
                self.client = anthropic.Anthropic(api_key=key)
            except ImportError:
                log("the 'anthropic' package is missing: using the simple loudness rule", "ai")
        elif enabled:
            log("no Anthropic API key: using the simple loudness rule instead of Claude", "ai")

    LOCATE = ("These are frames from a livestream of a video game. Find the STREAMER'S WEBCAM: the real person "
              "filmed by their camera, usually a box in a corner or along an edge, or a cut-out person (green "
              "screen) on top of the gameplay. It is NOT the player's game character (in third-person games the "
              "character stands in the middle of the screen), NOT an NPC, NOT a tattoo, poster or cutscene face. "
              "A real webcam looks like real-life video (a real room, headset, microphone, real skin and hair). "
              "Reply with ONLY one JSON object: {\"found\": true/false, \"x\": ..., \"y\": ..., \"w\": ..., "
              "\"h\": ..., \"player_x\": ...} where x,y,w,h is a box around the person's HEAD (top-left corner, "
              "width, height) as fractions 0-1 of the image (found=false if there is no real person on screen), "
              "and player_x is the horizontal centre (0-1) of the player's game character or vehicle, ignoring "
              "chat boxes, alerts and the webcam.")

    def locate_webcam(self, jpgs):
        """Ask Claude where the streamer's webcam is. Returns head box fractions or None."""
        jpgs = [j for j in jpgs if j][:3]
        if not jpgs or (self.client is None and not self.claude_code):
            return None
        text = None
        try:
            if self.claude_code:
                import tempfile
                work = tempfile.mkdtemp(prefix="frenzo_cam_")
                try:
                    names = []
                    for i, j in enumerate(jpgs):
                        names.append(f"stream_frame_{i}.jpg")
                        with open(os.path.join(work, names[-1]), "wb") as f:
                            f.write(j)
                    env = dict(os.environ)
                    env.pop("ANTHROPIC_API_KEY", None)
                    prompt = f"Read these images with the Read tool: {', '.join(names)}. " + self.LOCATE
                    res = subprocess.run(
                        [self.claude_code, "-p", "--output-format", "json", "--model",
                         self.cfg["anthropic"].get("claude_code_model", "haiku"), "--max-turns", "4",
                         "--allowedTools", "Read"],
                        input=prompt.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=180, env=env, cwd=work)
                    out = res.stdout.decode("utf-8", "replace")
                    try:
                        text = json.loads(out).get("result", out)
                    except ValueError:
                        text = out
                finally:
                    shutil.rmtree(work, ignore_errors=True)
            else:
                content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.b64encode(j).decode()}} for j in jpgs]
                content.append({"type": "text", "text": self.LOCATE})
                msg = self.client.messages.create(model=self.cfg["anthropic"]["model"], max_tokens=200,
                                                  messages=[{"role": "user", "content": content}])
                text = "".join(getattr(b, "text", "") for b in msg.content)
            m = re.search(r"\{.*?\}", text or "", re.S)
            if not m:
                return None
            d = json.loads(m.group(0))
            px = d.get("player_x")
            self.last_player_x = float(px) if isinstance(px, (int, float)) and 0 <= px <= 1 else None
            if d.get("found") is False:
                return {"found": False}
            if not d.get("found"):
                return None
            box = {k: float(d[k]) for k in ("x", "y", "w", "h")}
            if not (0 <= box["x"] < 1 and 0 <= box["y"] < 1 and 0 < box["w"] <= 0.5 and 0 < box["h"] <= 0.7):
                return None
            return box
        except Exception as e:
            log(f"Claude couldn't locate the webcam ({str(e)[:100]})", "ai")
            return None

    def _prompt_text(self, ctx):
        chat = ctx["chat_lines"][-180:]
        loud = ", ".join(f"{i}s:{db:.0f}" for i, db in enumerate(ctx["loudness"]) if i % 2 == 0)
        return (
            f"Streamer: {ctx['streamer']} (on {ctx.get('platform', 'twitch')})\nGame: {ctx['game']}\n"
            f"Stream title: {ctx.get('stream_title') or '(unknown)'}\n"
            f"Why this window was flagged: {ctx['source']} (signal at {ctx.get('flag_at', 0):.0f}s of the window)\n"
            f"Window length: {ctx['window']:.0f} s (all times below are seconds from window start)\n\n"
            "TRANSCRIPT:\n" + ("\n".join(ctx["transcript"]) or "(no speech detected)") + "\n\n"
            "CHAT:\n" + ("\n".join(chat) or "(no chat data)") + "\n\n"
            f"LOUDNESS (dBFS, every 2 s): {loud}\n\n"
            f"Hashtags to always include: {' '.join(ctx['tags']) or '(none)'}"
            + ctx.get("crowd", "") + ctx.get("score_line", "") + sound_context(self.cfg, ctx) + campaign_rules(ctx)
        )

    def system_text(self, ctx=None):
        """The static instructions (cached by Claude: identical for every moment of the same streamer setup)."""
        clips = self.cfg["clips"]
        from .campaign import of
        camp = of((ctx or {}).get("scfg"))
        extra = ""
        if self.cfg["render"].get("hook", True) or self.cfg["render"].get("five_titles", True):
            from .titles import prompt_block
            extra = HOOK_RULES + "\nTITLE BANK (good hooks this page likes; vary the format, never copy):\n" \
                + prompt_block(self.cfg)
        return SYSTEM.format(min_len=clips["min_length"], max_len=clips["max_length"]) + prompt_notes(self.cfg) \
            + extra + sound_section(self.cfg, camp)

    def _system_file(self, text):
        """Write the static instructions once per content (Claude Code reads them with --system-prompt-file)."""
        import hashlib
        import tempfile
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        path = os.path.join(tempfile.gettempdir(), f"frenzo_system_{h}.txt")
        if not os.path.exists(path):
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(path + ".tmp", path)
        return path

    def _decide_claude_code(self, ctx):
        system = self.system_text(ctx)
        fields = ", ".join(TOOL["input_schema"]["properties"])
        import tempfile
        work = tempfile.mkdtemp(prefix="frenzo_ai_")
        frame_note = ""
        if self.cfg["anthropic"].get("send_frames", True) and ctx["frames"]:
            names = []
            for i, (t, jpg) in enumerate(ctx["frames"]):
                name = f"frame_{i:02d}_at_{t:.0f}s.jpg"
                with open(os.path.join(work, name), "wb") as f:
                    f.write(jpg)
                names.append(name)
            frame_note = ("\n\nFIRST look at these stream frames with the Read tool (all in the current folder, "
                          "read them all in one go): " + ", ".join(names) + "\n")
        prompt = ("Reply with ONLY one JSON object (no markdown, no other text) with these keys: "
                  + fields + ". Types: keep/spoiler boolean, viral_score integer 0-10, start/end/focus_x numbers, "
                  "hashtags a list of strings, cues a list of objects (t, sfx, role, align, gain_db, stop, duck, kind, "
                  "max_len, fx), titles a list of 5 {\"text\", \"format\"}, title_pick integer, punch_words a list "
                  "of strings, slowmo [start, end] or [], hook_in/hook_out numbers, the rest strings. Fill what_happens FIRST (from the frames + transcript), then write "
                  "title_bar so it matches it." + frame_note + "\n\n" + self._prompt_text(ctx))
        sys_file = self._system_file(system)
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)   # make sure your Claude plan is used, not paid API credits
        model = self.cfg["anthropic"].get("claude_code_model", "haiku")
        try:
            res = subprocess.run(
                [self.claude_code, "-p", "--output-format", "json", "--model", model,
                 "--max-turns", "6" if frame_note else "1", "--allowedTools", "Read",
                 "--system-prompt-file", sys_file],
                input=prompt.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=240, env=env, cwd=work,
            )
            out = res.stdout.decode("utf-8", "replace")
            text = out
            try:
                wrapper = json.loads(out)
                if wrapper.get("is_error"):
                    raise RuntimeError(str(wrapper.get("result", ""))[:150])
                text = wrapper.get("result", out)
                cost = wrapper.get("total_cost_usd")
            except ValueError:
                cost = None
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                raise RuntimeError((text or res.stderr.decode("utf-8", "replace"))[:150])
            d = json.loads(m.group(0))
            if "viral_score" not in d:
                raise RuntimeError("answer had no score")
            if isinstance(cost, (int, float)):
                d["_cost_usd"] = round(float(cost), 5)
            return d
        except Exception as e:
            log(f"Claude Code didn't answer ({str(e)[:150]}); using the chat backup rule for this moment", "ai")
            return heuristic(ctx, self.cfg)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def decide(self, ctx):
        if self.claude_code:
            import time
            cap = int(self.cfg["anthropic"].get("max_checks_per_hour") or 0)
            now = time.time()
            self._calls = [t for t in getattr(self, "_calls", []) if t > now - 3600]
            if cap and len(self._calls) >= cap:
                return {"keep": False, "viral_score": 0, "start": 0, "end": 0, "hook_text": "", "title": "",
                        "caption": "", "hashtags": [], "spoiler": False,
                        "reason": f"skipped: {cap} Claude checks this hour already (protects your plan's limits)"}
            self._calls.append(now)
            return self._decide_claude_code(ctx)
        if self.client is None:
            return heuristic(ctx, self.cfg)
        clips = self.cfg["clips"]
        content = []
        for t, jpg in ctx["frames"]:
            content.append({"type": "text", "text": f"Frame at {t}s:"})
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": base64.b64encode(jpg).decode()},
            })
        chat = ctx["chat_lines"][-180:]
        loud = ", ".join(f"{i}s:{db:.0f}" for i, db in enumerate(ctx["loudness"]) if i % 2 == 0)
        content.append({"type": "text", "text": (
            f"Streamer: {ctx['streamer']} (on {ctx.get('platform', 'twitch')})\nGame: {ctx['game']}\n"
            f"Stream title: {ctx.get('stream_title') or '(unknown)'}\n"
            f"Why this window was flagged: {ctx['source']} (signal at {ctx.get('flag_at', 0):.0f}s of the window)\n"
            f"Window length: {ctx['window']:.0f} s (all times below are seconds from window start)\n\n"
            "TRANSCRIPT:\n" + ("\n".join(ctx["transcript"]) or "(no speech detected)") + "\n\n"
            "CHAT:\n" + ("\n".join(chat) or "(no chat data)") + "\n\n"
            f"LOUDNESS (dBFS, every 2 s): {loud}\n\n"
            f"Hashtags to always include: {' '.join(ctx['tags']) or '(none)'}"
            + ctx.get("crowd", "") + ctx.get("score_line", "") + sound_context(self.cfg, ctx) + campaign_rules(ctx)
        )})
        try:
            msg = self.client.messages.create(
                model=self.cfg["anthropic"]["model"],
                max_tokens=2500,
                system=[{"type": "text", "text": self.system_text(ctx), "cache_control": {"type": "ephemeral"}}],
                tools=[TOOL],
                tool_choice={"type": "tool", "name": "clip_decision"},
                messages=[{"role": "user", "content": content}],
            )
        except Exception as e:
            log(f"Claude request failed ({e}); using the loudness rule for this moment", "ai")
            return heuristic(ctx, self.cfg)
        for block in msg.content:
            if getattr(block, "type", "") == "tool_use":
                return dict(block.input)
        return heuristic(ctx, self.cfg)


TITLE_PROMPT = """You write the on-screen title of a short viral GTA 6 clip (TikTok / Reels / Shorts).

Streamer: {streamer}
What happens (written by the page owner, who watched it: this is TRUE): {context}
What the streamer says in the clip: {said}
Current title: {current}

Write 5 different title options in different formats. Each must be TRUE to what happens AND funny: use the streamer's own \
line, chat's joke, the irony or the twist, not a flat description. 3-10 words, sentence case, no emoji \
in the title. Good: "Why did Kai shoot the dog?", "Kai's new girlfriend is a plane", "Bro said he can't \
crash. Chat said 10 seconds.", "Big talk, then gets knocked out by a girl", "This GTA 6 NPC wanted smoke". \
Bad: "GTA 6 crash knocks JasonTheWeen flat" (flat news report). Lucia and Jason are the playable GTA 6 \
characters, never NPCs; JasonTheWeen is the streamer, not the game's Jason. Mention GTA 6 when natural.
For each: highlight = the ONE word or short phrase of the title shown in yellow, emoji = one of \
💀 😭 😳 😱 🤣 🥵 🫣.
Reply with ONLY a JSON list: [{{"title": "...", "highlight": "...", "emoji": "..."}}, ...]"""


def suggest_titles(cfg, streamer, context, said="", current="", scfg=None):
    """4 title ideas from the owner's own description of the clip. Text only: uses very little of the plan.
    scfg: the streamer's config, so the titles follow its campaign rules."""
    prompt = TITLE_PROMPT.format(streamer=streamer, context=context.strip()[:800],
                                 said=(said or "(unknown)")[:1200], current=current or "(none)")
    notes = prompt_notes(cfg) + campaign_rules({"scfg": scfg})
    if notes:
        prompt = prompt.replace("\nReply with ONLY a JSON list", notes + "\nReply with ONLY a JSON list")
    judge = ClipJudge(cfg)
    text = None
    if judge.claude_code:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        res = subprocess.run([judge.claude_code, "-p", "--output-format", "json", "--model",
                              cfg["anthropic"].get("claude_code_model", "haiku"), "--max-turns", "1"],
                             input=prompt.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=120, env=env, creationflags=NO_WINDOW)
        out = res.stdout.decode("utf-8", "replace")
        try:
            wrapper = json.loads(out)
            if wrapper.get("is_error"):
                raise RuntimeError(str(wrapper.get("result", ""))[:150])
            text = wrapper.get("result", out)
        except ValueError:
            text = out
    elif judge.client is not None:
        msg = judge.client.messages.create(model=cfg["anthropic"]["model"], max_tokens=600,
                                           messages=[{"role": "user", "content": prompt}])
        text = "".join(getattr(b, "text", "") for b in msg.content)
    else:
        raise RuntimeError("Claude isn't available on this PC (Claude Code not found).")
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        raise RuntimeError("Claude didn't send title ideas, try again.")
    out = []
    for o in json.loads(m.group(0)):
        if isinstance(o, dict) and str(o.get("title", "")).strip():
            out.append({"title": str(o["title"]).strip()[:90], "highlight": str(o.get("highlight") or "").strip()[:40],
                        "emoji": str(o.get("emoji") or "").strip()[:4]})
    return out[:5]


CHAT_LINE = re.compile(r"^\[\s*([\d.]+)s\]\s*[^:]*:\s*(.*)$")


def _chat_curve(ctx):
    """Chat activity per second of the window (hype messages count extra)."""
    from .detector import hype_weight
    n = max(1, int(ctx["window"]) + 1)
    curve = [0.0] * n
    for line in ctx["chat_lines"]:
        m = CHAT_LINE.match(line)
        if m:
            i = min(n - 1, max(0, int(float(m.group(1)))))
            curve[i] += hype_weight(m.group(2))
    return curve


def heuristic(ctx, cfg):
    """Backup when Claude isn't available: read the stream CHAT (and loudness) to pick and cut the moment."""
    clips = cfg["clips"]
    loud = ctx["loudness"]
    name = ctx["streamer"]
    src = ctx.get("source", "")
    length = min(max(26, clips["min_length"]), clips["max_length"])
    skip = {"keep": False, "viral_score": 0, "start": 0, "end": 0, "hook_text": "", "title": "",
            "caption": "", "hashtags": [], "spoiler": False}
    if not loud:
        return dict(skip, reason="no audio")
    n = len(loud)
    curve = _chat_curve(ctx)[:n] + [0.0] * max(0, n - len(_chat_curve(ctx)))
    chat_total = sum(curve)

    if chat_total >= 10:
        # chat reacts ~3-6 s after the moment: smooth over 6 s and look a few seconds earlier
        smooth = [sum(curve[max(0, i - 3):i + 3]) for i in range(n)]
        peak_chat = max(range(n), key=lambda i: smooth[i])
        moment = max(0, peak_chat - 4)
        normal = sorted(smooth)[len(smooth) // 2] or 0.5
        ratio = smooth[peak_chat] / normal
        score = 8 if ratio >= 5 else 7 if ratio >= 3.5 else 6 if ratio >= 2.2 else 4
        why = f"chat peaked {ratio:.1f}x above normal at {moment}s"
    else:
        moment = max(range(n), key=lambda i: loud[i])
        quiet = sorted(loud)[n // 2]
        jump = loud[moment] - quiet
        score = 7 if jump >= 18 else 6 if jump >= 12 else 4
        why = f"no chat data; loudest point at {moment}s ({jump:.0f} dB above normal)"
    flag_at = int(max(0, min(n - 1, ctx.get("flag_at", moment))))
    m_chaos = re.search(r"gameplay chaos: (?:picture went )?([\d.]+)x", src)
    if m_chaos:                                  # bugs / physics: trust the picture, not chat or loudness
        moment = flag_at
        r = float(m_chaos.group(1))
        score = max(score, 7 if r >= 3.5 else 6)
        why = f"gameplay went {r:.1f}x wilder than normal at {moment}s"
    if "hot take" in src:
        moment = flag_at
        score = max(score, 6)
        why = "the streamer gave a strong opinion"
    if "streamer reaction" in src and score >= 4:
        score = min(9, score + 1)
        why += " + streamer reaction"

    spicy = "thirsty" in src or _spicy_count(ctx) >= 4
    sus = not spicy and ("sus" in src or _sus_count(ctx) >= 4)
    bug = "bug/glitch" in src                    # chat SAYS it's a bug; picture chaos alone isn't proof
    npc = "NPC" in src
    play = "hyped by the gameplay" in src
    chaos_only = bool(m_chaos) and not bug
    take = "hot take" in src
    start = max(0, min(moment - length * 0.6, n - length))
    end = min(n, start + length)
    if take:
        heading, category, sfx = f"{name.upper()}'S HONEST THOUGHTS", "hot_take", []
        start = max(0, min(moment - 3, n - length))
        end = min(n, start + length)
    elif bug:
        heading, category, sfx = "INSANE GTA 6 BUG", "bug", []
    elif npc:
        heading, category, sfx = "THIS NPC IS SOMETHING ELSE", "npc", []
    elif play:
        heading, category, sfx = "GTA 6 GAMEPLAY GOT CRAZY", "gameplay", []
    elif chaos_only:
        heading, category, sfx = "THINGS GOT OUT OF HAND", "weird", []
    elif spicy:
        heading, category, sfx = "CHAT WAS NOT READY FOR THIS", "spicy", [{"type": "sus", "at": moment}]
        score = min(9, score + 1)
    elif sus:
        heading, category, sfx = f"{name} WAS ACTING MAD SUS HERE", "sus", [{"type": "sus", "at": moment}]
    elif "laugh" in src:
        heading, category, sfx = f"{name} COULD NOT STOP LAUGHING", "funny", []
    elif "yell" in src or "outburst" in src:
        heading, category, sfx = f"{name} COMPLETELY LOST IT", "rage", []
    elif chat_total >= 10:
        heading, category, sfx = "CHAT WENT CRAZY AT THIS", "other", []
    else:
        heading, category, sfx = "WAIT FOR IT", "other", []
    title = f"{name} " + ("acting sus" if sus else "can't stop laughing" if "laugh" in src
                          else "loses it" if ("yell" in src or "outburst" in src) else "moment that broke chat")
    _g = (ctx.get("game", "") + " " + ctx.get("stream_title", "")).lower()
    game = "GTA 6" if ("gta" in _g or "grand theft auto" in _g) else ""
    tb, hl, emo = {
        "bug": (f"Insane {game} bug" if game else "Insane game bug", game or "Insane", "💀"),
        "npc": (f"This {game} NPC is something else" if game else "This NPC is something else", "NPC", "💀"),
        "gameplay": (f"{game} gameplay got crazy" if game else f"{name} went crazy", "crazy", "😱"),
        "weird": (f"Things got out of hand on {game}" if game else "Things got out of hand", "out of hand", "💀"),
        "hot_take": (f"{name}'s honest thoughts on {game}" if game else f"{name}'s honest thoughts", "honest", "😳"),
        "spicy": (f"{name} was not ready for this", name, "😱"),
        "sus": (f"{name} is acting sus on {game}" if game else f"{name} is acting sus", "sus", "😳"),
        "funny": (f"{name} can't stop laughing", "laughing", "😭"),
        "rage": (f"{name} lost it on {game}" if game else f"{name} lost it", "lost it", "💀"),
    }.get(category, (f"Chat went crazy at this {game} moment".replace("  ", " "), game or "crazy", "💀"))
    calls = [{"text": "*Laughing*", "at": moment, "color": "yellow"}] if category == "funny" else \
        [{"text": "*Screaming*", "at": moment, "color": "yellow"}] if category == "rage" else []
    return {
        "keep": score >= clips["min_score"], "viral_score": score, "category": category,
        "start": start, "end": end, "heading": heading, "hook_text": heading, "sfx": sfx,
        "title_bar": tb, "highlight": hl, "emoji": emo, "segments": [[start, end]], "callouts": calls,
        "title": title[:70], "caption": f"{name} on stream 💀 (clip picked automatically, no AI)",
        "hashtags": list(ctx["tags"]), "spoiler": False,
        "what_happens": f"(backup rule, nobody looked at the picture) {why}",
        "reason": f"backup rule (no Claude): {why}",
    }


SUS_WORDS = re.compile(r"(\bsus\b|\bsussy\b|\bayo+\b|🤨|😳|📸|\bcaught\b|\bbro what\b|\bnah\b|\bhuh\b|\?\?\?)",
                       re.IGNORECASE)


SPICY_WORDS = re.compile(r"(🥵|🤤|😍|🍑|\bgya+t+\b|\bthirsty\b|\brizz\b|\bmommy\b|\bstrip\b|\btwerk)", re.IGNORECASE)


def _spicy_count(ctx):
    return sum(1 for line in ctx["chat_lines"] if SPICY_WORDS.search(line.split(":", 1)[-1]))


def _sus_count(ctx):
    return sum(1 for line in ctx["chat_lines"] if SUS_WORDS.search(line.split(":", 1)[-1]))
