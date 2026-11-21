"""Sound design with the GTA SFX library: cue mapping through jump cuts, id checks, safe mode, alignment,
auto-fix, the recently-used list.  Run:  .venv\\Scripts\\python -m unittest tests.test_sound"""
import json
import os
import tempfile
import unittest

from frenzo import soundlib, soundpass, viral
from frenzo.pipeline import map_cues


def _lib(d):
    entries = [
        {"id": "vine-boom-slowed", "file": "sfx/vine-boom-slowed.flac", "duration": 2.0, "peak_s": 0.3, "tags": ["impact", "funny"]},
        {"id": "metal-pipe-falling", "file": "sfx/metal-pipe-falling.flac", "duration": 3.0, "peak_s": 0.5, "tags": ["impact", "fail"]},
        {"id": "riser-air", "file": "sfx/riser-air.flac", "duration": 2.5, "peak_s": 2.4, "tags": ["buildup", "realistic"]},
        {"id": "gangnam-style", "file": "sfx/gangnam-style.flac", "duration": 4.0, "peak_s": 1.0, "tags": ["music", "funny"]},
        {"id": "bad-word", "file": "sfx/bad-word.flac", "duration": 1.0, "peak_s": 0.2, "tags": ["voice", "explicit"]},
    ]
    with open(os.path.join(d, "library.json"), "w", encoding="utf-8") as f:
        json.dump(entries, f)
    soundlib._cache.clear()
    return d


class Sound(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = _lib(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cues_mapped_through_jump_cuts(self):
        to_out = viral.mapper([[10.0, 18.0], [25.0, 35.0]])        # window seconds kept: 8 s then 10 s
        cues = [{"t": 10.5, "sfx": "vine-boom-slowed", "align": "peak", "kind": "hit", "fx": ["zoom", "bogus"]},
                {"t": 20.0, "sfx": "metal-pipe-falling", "kind": "hit"},                  # in the cut: dropped
                {"t": 30.0, "sfx": "metal-pipe-falling", "align": "peak", "kind": "hit", "stop": 2},
                {"t": 30.0, "sfx": "metal-pipe-falling", "align": "peak", "kind": "hit"}]   # duplicate
        out = map_cues(cues, to_out, 18.0, self.lib, None, {"sound_safe_mode": True})
        self.assertEqual([(c["t"], c["sfx"]) for c in out], [(0.5, "vine-boom-slowed"), (13.0, "metal-pipe-falling")])
        self.assertEqual(out[0]["fx"], ["zoom"])
        self.assertEqual(out[1]["stop"], 0.6)                  # clamped

    def test_unknown_ids_fixed_or_dropped_and_safe_mode(self):
        self.assertEqual(soundlib.fix_id("vine boom slowed", self.lib), "vine-boom-slowed")
        self.assertEqual(soundlib.fix_id("metal-pipe-falin", self.lib), "metal-pipe-falling")
        self.assertIsNone(soundlib.fix_id("not-a-sound", self.lib))
        self.assertIsNone(soundlib.fix_id("gangnam-style", self.lib))                  # music: safe mode
        self.assertEqual(soundlib.fix_id("gangnam-style", self.lib, allow_music=True), "gangnam-style")
        self.assertIsNone(soundlib.fix_id("bad-word", self.lib))
        cat = soundlib.catalog(self.lib)
        self.assertNotIn("gangnam-style", cat)
        self.assertIn("vine-boom-slowed | impact funny | 2.00s | peak 0.30s", cat)
        to_out = viral.mapper([[0, 10]])
        out = map_cues([{"t": 1, "sfx": "gangnam-style"}], to_out, 10, self.lib, {"allow_music": True}, {})
        self.assertEqual(out[0]["sfx"], "gangnam-style")

    def test_alignment_math(self):
        rows = soundpass.placed([{"t": 5.0, "sfx": "vine-boom-slowed", "align": "peak"},
                                 {"t": 5.0, "sfx": "riser-air", "align": "end", "kind": "riser"},
                                 {"t": 0.1, "sfx": "metal-pipe-falling", "align": "peak", "max_len": 1.5},
                                 {"t": 8.0, "sfx": "metal-pipe-falling", "align": "start", "nudge": -0.04}], self.lib)
        by = {(r["sfx"], r["align"]): r for r in rows}
        self.assertAlmostEqual(by[("vine-boom-slowed", "peak")]["starts"], 4.7)
        self.assertAlmostEqual(by[("riser-air", "end")]["starts"], 2.5)
        early = by[("metal-pipe-falling", "peak")]                     # would start before 0: head trimmed
        self.assertEqual((early["starts"], early["head"]), (0.0, 0.4))
        self.assertAlmostEqual(early["len"], 1.1)
        self.assertAlmostEqual(by[("metal-pipe-falling", "start")]["starts"], 7.96)

    def test_autofix(self):
        cues = [{"t": 1.0, "sfx": "a", "align": "peak", "kind": "hit"},
                {"t": 3.0, "sfx": "b", "align": "peak", "kind": "hit"},
                {"t": 6.0, "sfx": "c", "align": "start", "kind": "hit", "gain_db": 2},
                {"t": 9.0, "sfx": "d", "align": "peak", "kind": "hit"}]
        qa = {"cues": [{"t": 1.0, "verdict": "masked"}, {"t": 3.0, "verdict": "off sync", "onset_ms": 40},
                       {"t": 6.0, "verdict": "masked"}, {"t": 9.0, "verdict": "off sync", "onset_ms": 400}]}
        new, changes = soundpass.autofix(cues, qa)
        self.assertEqual((new[0]["gain_db"], new[0]["stop"]), (4.0, 0.25))
        self.assertEqual(new[1]["nudge"], -0.04)
        self.assertEqual(new[2]["gain_db"], 4.0)                    # capped
        self.assertEqual(new[3]["align"], "start")
        self.assertEqual(len(changes), 4)
        self.assertEqual(soundpass.failing({"cues": [{"t": 1, "sfx": "a", "verdict": "ok"}], "lufs": -14.3,
                                            "true_peak_dbfs": -1.5}), [])
        self.assertTrue(soundpass.failing({"cues": [], "lufs": -11.0, "true_peak_dbfs": -0.2}))

    def test_recently_used_per_account(self):
        cfg = {"storage": {"output_dir": self.tmp.name}}
        rcfg = {"handles": {"tiktok": "@its_frenzo"}, "sound_no_repeat": 2}
        soundlib.remember(cfg, rcfg, ["a", "b"])
        soundlib.remember(cfg, rcfg, ["c"])
        soundlib.remember(cfg, rcfg, ["d", "a"])
        self.assertEqual(soundlib.recent(cfg, rcfg), ["c", "d", "a"])     # last 2 posts only
        self.assertEqual(soundlib.recent(cfg, {"handles": {"tiktok": "@other"}}), [])

    def test_hints_from_chat_categories(self):
        tags = soundlib.hints_for("chat says bug/glitch/physics: 6 messages")
        self.assertEqual(tags[:3], ["fail", "glitch", "impact"])
        self.assertIn("rizz", soundlib.hints_for("chat thinks it's sus: 7 sus/ayo messages"))

    def test_real_library_rules_and_catalog(self):
        lib = soundlib.find_library({})
        if lib is None:
            self.skipTest("sound library not installed")
        soundlib._cache.clear()
        r = soundlib.rules(lib)
        self.assertIn("Moment", r)
        self.assertIn("Placement and mix rules", r)
        self.assertNotIn("Copyright", r)
        self.assertGreater(len(soundlib.catalog(lib).splitlines()), 150)


if __name__ == "__main__":
    unittest.main()
