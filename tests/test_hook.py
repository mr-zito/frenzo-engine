"""Hook, titles, captions and pacing.  Run:  .venv\\Scripts\\python -m unittest tests.test_hook"""
import tempfile
import unittest

from frenzo import hook, titles, viral


class Titles(unittest.TestCase):
    def test_spoiler_check(self):
        self.assertTrue(any("spoils" in p for p in titles.problems("Streamer crashes into ocean after chase")))
        self.assertTrue(any("spoils" in p for p in titles.problems("JasonTheWeen's car gets crushed into a cube")))
        self.assertTrue(any("spoils" in p for p in titles.problems("Kai gets arrested during the heist")))
        self.assertEqual(titles.problems("Bro thought the cops were AFK"), [])
        self.assertEqual(titles.problems('Bro said "I won\'t crash" again'), [])          # quoted: the joke

    def test_banned_and_length(self):
        self.assertTrue(any("banned" in p for p in titles.problems("Funny moment in GTA 6 today")))
        self.assertTrue(any("banned" in p for p in titles.problems("Kai reacts to the new GTA 6")))
        self.assertTrue(any("words" in p for p in titles.problems("Wow")))
        self.assertTrue(any("words" in p for p in titles.problems("one two three four five six seven eight nine")))

    def test_pick_skips_bad_candidates_and_keeps_all_five(self):
        d = {"titles": [{"text": "Kai crashes the plane into the sea", "format": "summary"},
                        {"text": "Bro said he could land it", "format": "ironic quote"},
                        {"text": "POV: Kai flies the plane", "format": "POV"},
                        {"text": "Hilarious GTA 6 plane", "format": "x"},
                        {"text": "He had ONE job", "format": "one job"}], "title_pick": 0, "title_why": "x"}
        t, rep = titles.pick(d)
        self.assertEqual(t, "Bro said he could land it")
        self.assertEqual(len(rep["candidates"]), 5)
        self.assertEqual(rep["claude_pick"], 0)
        self.assertEqual(rep["picked"], 1)
        self.assertIn("note", rep)

    def test_bank_prefers_owner_titles(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"storage": {"output_dir": d}}
            self.assertTrue(titles.add(cfg, "Nobody told Kai about autosave", "edited"))
            self.assertFalse(titles.add(cfg, "Kai dies", "edited"))              # fails the checks: not added
            ex = titles.examples(cfg, n=25)
            self.assertEqual(ex[0]["good"], "Nobody told Kai about autosave")
            self.assertLessEqual(len(ex), 25)
            self.assertIn('GOOD "Rockstar did NOT test this"', titles.prompt_block(cfg))


class Hook(unittest.TestCase):
    def test_hook_piece_lengths(self):
        self.assertEqual(hook.hook_piece({"hook_type": "cold_open", "hook_in": 30, "hook_out": 35}, 90),
                         ("action", (30.0, 35.0)))                     # rewind styles are off by default
        self.assertEqual(hook.hook_piece({"hook_type": "cold_open", "hook_in": 30, "hook_out": 35}, 90,
                                         allowed=hook.TYPES), ("cold_open", (30.0, 31.5)))
        self.assertEqual(hook.hook_piece({"hook_type": "reaction_first", "hook_in": 40, "hook_out": 41.5}, 90, allowed=hook.TYPES)[1],
                         (40.0, 41.5))
        self.assertEqual(hook.hook_piece({"hook_type": "nonsense", "hook_in": "x"}, 90), ("action", None))

    def test_mapper_with_hook_offset_and_slowmo(self):
        segs, speeds = hook.apply_slowmo([[10, 20], [25, 30]], [15, 16])
        self.assertEqual(segs, [[10, 15], [15, 16], [16, 20], [25, 30]])
        self.assertEqual(speeds, [1.0, 0.5, 1.0, 1.0])
        f = hook.mapper(segs, speeds, offset=1.5)          # a 1.5 s hook piece plays first
        self.assertAlmostEqual(f(10), 1.5)
        self.assertAlmostEqual(f(15.5), 1.5 + 5 + 1.0)    # half a second of slow-mo = 1 s on screen
        self.assertAlmostEqual(f(17), 1.5 + 5 + 2 + 1)
        self.assertIsNone(f(22))                           # cut out
        self.assertAlmostEqual(f(25), 1.5 + 5 + 2 + 4)

    def test_slowmo_limited(self):
        segs, speeds = hook.apply_slowmo([[0, 10]], [2, 9])
        self.assertEqual(segs[1], [2, 4.0])                # at most 2 s
        self.assertEqual(hook.apply_slowmo([[0, 10]], [5, 5.1])[1], [1.0])   # too short: ignored

    def test_dead_air_and_leading_quiet(self):
        loud = [-20.0] * 30
        for s in range(8, 12):
            loud[s] = -45.0                                 # 4 s of near silence
        words = [{"start": 2.0, "end": 2.5, "word": "yo"}]
        out = hook.trim_dead_air([[0, 20]], words, loud, protect=18)
        self.assertEqual(len(out), 2)
        self.assertLess(out[0][1], 9.5)
        self.assertGreater(out[1][0], 10.5)
        self.assertEqual(hook.trim_dead_air([[0, 20]], words, loud, protect=10), [[0, 20]])   # payoff protected
        quiet = [-50.0, -50.0, -20.0] + [-20.0] * 20
        self.assertEqual(hook.trim_leading_quiet([[0, 15]], quiet, protect=10), [[2.0, 15]])

    def test_opening_sound_added(self):
        lib_ids = {"whoosh-fast", "record-scratch", "tape-rewind"}

        def fix(s, lib):
            return s if s in lib_ids else None
        cues, notes = hook.ensure_opening_sound([{"t": 5.0, "sfx": "x"}], "cold_open", 1.4, None, fix)
        self.assertEqual([c["t"] for c in cues], [0.4, 1.4, 5.0])
        self.assertEqual(cues[0]["fx"], ["zoom"])                        # the audio hook punches the picture too
        self.assertEqual(cues[1]["sfx"], "record-scratch")
        self.assertEqual(len(notes), 2)
        cues, notes = hook.ensure_opening_sound([{"t": 0.3, "sfx": "x"}], "quote", 0, None, fix)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["fx"], ["zoom"])                        # Claude's own opening sound gets the punch
        cues, _ = hook.ensure_opening_sound([], "action", 0, None, fix, prefer="tape-rewind")
        self.assertEqual(cues[0]["sfx"], "whoosh-fast")                  # not an impact/whoosh: not an opener
        cues, notes = hook.ensure_opening_sound([], "action", 0, None, fix, add=False)
        self.assertEqual(cues, [])                                       # opens loud already: nothing added
        self.assertIn("loud", notes[0])
        cues, _ = hook.ensure_opening_sound([{"t": 9.0, "sfx": "whoosh-fast"}], "action", 0, None,
                                            lambda s, lib: s if s in {"whoosh-fast", "boom"} else None)
        self.assertEqual([c["sfx"] for c in cues], ["boom", "whoosh-fast"])   # never the same sound twice (impact first)


class Captions(unittest.TestCase):
    def test_fillers_removed(self):
        ws = [{"word": w, "start": i, "end": i + 0.5} for i, w in enumerate(["uh", "bro", "like,", "um", "what", "like", "like", "it"])]
        self.assertEqual([w["word"] for w in viral.drop_fillers(ws)], ["bro", "what", "like", "it"])

    def test_bold_captions_caps_punch_and_safe_zone(self):
        ws = [{"word": "no", "start": 2.0, "end": 2.3}, {"word": "way,", "start": 2.3, "end": 2.6},
              {"word": "fucking", "start": 3.0, "end": 3.4}, {"word": "idiot", "start": 3.4, "end": 3.8}]
        ass = viral.build_ass_bold(ws, [], 6, {}, "reel", 640, punch_words=["way"], hook_text="Wait for it", hook_end=1.5)
        ev = ass.split("[Events]")[1]
        self.assertIn("WAIT FOR IT", ev)
        self.assertIn("NO", ev)
        self.assertIn("F*CKING", ev)                            # masked
        self.assertIn(r"\c&H0AD6FF&\fscx118", ev)               # punch word: highlight colour, bigger
        self.assertIn(r"\t(0,70,\fscx115\fscy115)", ev)         # the spoken word pops to 115 %
        self.assertIn(r"\pos(500,790)", ev)                     # on the seam, left of the app buttons
        self.assertNotIn(r"{\r}", ev)


if __name__ == "__main__":
    unittest.main()
