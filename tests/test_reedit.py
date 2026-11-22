"""Re-edit with a note: Claude sees the current clip in both timelines, and the automatic rules don't undo its answer."""
import unittest

from frenzo import viral
from frenzo.pipeline import _norm_title
from frenzo.redo import _out_to_src, _owner_block


class OutToSrc(unittest.TestCase):
    def test_jump_cut(self):
        pieces, speeds = [(0.0, 11.3), (12.7, 18.0)], [1.0, 1.0]
        self.assertAlmostEqual(_out_to_src(pieces, speeds, 5.0), 5.0)
        self.assertAlmostEqual(_out_to_src(pieces, speeds, 12.0), 13.4)     # 0.7 s into the second piece

    def test_slow_piece(self):
        pieces, speeds = [(0.0, 10.0), (10.0, 11.0), (11.0, 20.0)], [1.0, 0.5, 1.0]
        self.assertAlmostEqual(_out_to_src(pieces, speeds, 11.0), 10.5)     # half-speed second piece lasts 2 s
        self.assertAlmostEqual(_out_to_src(pieces, speeds, 13.0), 12.0)


class OwnerBlock(unittest.TestCase):
    META = {"duration": 16.6, "jump_cuts": [[0.0, 11.3], [12.7, 18.0]],
            "sound": {"cues": [{"sfx": "faahhhh", "t": 0.7}, {"sfx": "boom", "t": 12.0}]}}

    def test_both_timelines(self):
        txt = _owner_block(self.META, offset=76.0, lo=0.0, win_len=185.0)
        self.assertIn("clip 0.0-11.3s = window 76.0-87.3s", txt)
        self.assertIn("clip 11.3-16.6s = window 88.7-94.0s", txt)
        self.assertIn("faahhhh at clip 0.7s = window 76.7s", txt)
        self.assertIn("boom at clip 12.0s = window 89.4s", txt)
        self.assertIn("COMPLETE list of sounds", txt)

    def test_window_starts_later(self):
        txt = _owner_block(self.META, offset=76.0, lo=50.0, win_len=100.0)
        self.assertIn("clip 0.0-11.3s = window 26.0-37.3s", txt)

    def test_no_sounds(self):
        txt = _owner_block({"duration": 20.0}, offset=10.0, lo=0.0, win_len=60.0)
        self.assertIn("Its sounds now: none", txt)


class KeptLength(unittest.TestCase):
    def test_cut_out_middle_does_not_count(self):
        # Claude: 76.5-92 and 124-140 (31 s kept) inside a 63.5 s span: the 45 s limit must not cut the second piece
        segs = [[76.5, 92.0], [124.0, 140.0]]
        kept = sum(b - a for a, b in viral.clean_segments(segs, 76.5, 140.0))
        self.assertLess(kept, 45)
        span_max = 45 + max(0.0, (140.0 - 76.5) - kept) if kept <= 45 else 45
        self.assertGreaterEqual(span_max, 140.0 - 76.5)


class Titles(unittest.TestCase):
    def test_same_title(self):
        self.assertEqual(_norm_title("Kai did NOT come here for this"), _norm_title("kai did not come here for this!"))
        self.assertNotEqual(_norm_title("Kai gets into his first race"), _norm_title("They cheating bro"))


class OwnerSounds(unittest.TestCase):
    def setUp(self):
        from frenzo import soundlib
        from frenzo.config import DEFAULTS
        self.rcfg = dict(DEFAULTS["render"])
        self.lib = soundlib.find_library(self.rcfg)
        if self.lib is None:
            self.skipTest("sound library not installed")

    def cues(self):
        return [{"t": 5.0, "sfx": "faahhhh", "align": "peak", "kind": "hit", "mute_game": 1.5},
                {"t": 9.0, "sfx": "boom", "align": "peak", "kind": "hit"},
                {"t": 14.0, "sfx": "boom", "align": "peak", "kind": "hit"},
                {"t": 20.0, "sfx": "bass-drop-vine-boom", "align": "peak", "kind": "hit"}]

    def test_normal_rules(self):
        from frenzo.pipeline import map_cues
        out = map_cues(self.cues(), lambda t: t, 30.0, self.lib, None, dict(self.rcfg, sound_max_per_clip=3))
        self.assertEqual([c["sfx"] for c in out].count("boom"), 1)          # never twice
        self.assertLessEqual(len(out), 3)

    def test_owner_note(self):
        from frenzo import soundpass
        from frenzo.pipeline import map_cues
        out = map_cues(self.cues(), lambda t: t, 30.0, self.lib, None, dict(self.rcfg, sound_max_per_clip=0),
                       repeat=True)
        self.assertEqual([c["sfx"] for c in out], ["faahhhh", "boom", "boom", "bass-drop-vine-boom"])
        self.assertEqual(out[0]["mute_game"], 1.5)
        rows = soundpass.placed(out, self.lib)
        self.assertEqual(rows[0]["mute_game"], 1.5)


if __name__ == "__main__":
    unittest.main()


class NudgeKept(unittest.TestCase):
    def test_map_cues_keeps_nudge(self):
        from frenzo import pipeline, soundlib
        from frenzo.config import load_config
        rcfg = dict(load_config("config.yaml")["render"])
        lib = soundlib.find_library(rcfg)
        if lib is None:
            self.skipTest("no sound library")
        sid = next(iter(soundlib.guide(rcfg, lib) or soundlib.load(lib)))
        out = pipeline.map_cues([{"t": 2.0, "sfx": sid, "nudge": 0.1}], lambda t: t, 10.0, lib, None, rcfg, repeat=True)
        self.assertEqual(out[0].get("nudge"), 0.1)
