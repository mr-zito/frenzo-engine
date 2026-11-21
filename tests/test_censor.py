"""Tests for the nudity blur.  Run:  .venv\\Scripts\\python -m unittest tests.test_censor"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frenzo import censor as C  # noqa: E402


class Censor(unittest.TestCase):
    def test_mode(self):
        self.assertEqual(C.mode({"clips": {}}), "blur")
        self.assertEqual(C.mode({"clips": {"nudity": "skip"}}), "skip")
        self.assertEqual(C.mode({"clips": {"nudity": "nonsense"}}), "blur")

    def test_detections_become_padded_held_spots(self):
        dets = [(1.0, [(500, 1000, 100, 80)]), (1.25, [(510, 1005, 100, 80)]), (6.0, [(100, 900, 60, 60)])]
        regs, n_det, n_fb = C.regions(dets, [], 10.0, "reel")
        self.assertEqual((n_det, n_fb), (2, 0))
        t0, t1, x, y, w, h = regs[0]
        self.assertEqual((t0, t1), (0.5, 1.75))                       # held 0.5 s before/after
        self.assertTrue(x < 500 and y < 1000 and x + w > 610 and y + h > 1085)   # bigger than the detection
        self.assertTrue(all(v % 2 == 0 for v in (x, y, w, h)))

    def test_failsafe_blurs_gameplay_when_detector_misses(self):
        regs, n_det, n_fb = C.regions([], [(3.0, 5.0)], 20.0, "reel")
        self.assertEqual((n_det, n_fb), (0, 1))
        t0, t1, x, y, w, h = regs[0]
        self.assertEqual((t0, t1), (2.5, 5.5))
        self.assertEqual((x, y, w, h), C.gameplay_box("reel"))
        # detector already covers Claude's seconds: no extra gameplay blur
        dets = [(t / 4, [(400, 1000, 200, 200)]) for t in range(12, 21)]
        regs, n_det, n_fb = C.regions(dets, [(3.0, 5.0)], 20.0, "reel")
        self.assertEqual(n_fb, 0)

    def test_plan_safety(self):
        orig = C.detect
        try:
            C.detect = lambda v: []                                      # detector finds nothing
            regs, info = C.plan("x.mp4", [], 12.0, "reel", chosen=False)
            self.assertIsNone(regs)                                    # automatic clip: skipped, not risked
            regs, info = C.plan("x.mp4", [], 12.0, "reel", chosen=True)
            self.assertEqual(regs, [(0.0, 12.0) + C.gameplay_box("reel")])   # your clip: whole gameplay pixelated
            self.assertTrue(info["whole_clip"])
        finally:
            C.detect = orig

    def test_filter_and_mapping(self):
        g = C.filter_graph([(1.0, 2.0, 10, 20, 100, 100), (3.0, 4.0, 0, 640, 1080, 1280)], "[v0]", "[v0z]")
        self.assertIn("between(t,1.0,2.0)", g)
        self.assertIn("crop=1080:1280:0:640", g)
        self.assertTrue(g.rstrip().endswith("[v0z]"))
        self.assertEqual(C.filter_graph([], "[a]", "[b]"), ";[a]null[b]")
        # window seconds -> clip seconds through jump cuts (here: the clip starts 10 s into the window)
        to_out = lambda t: t - 10 if 10 <= t <= 40 else None
        self.assertEqual(C.map_ranges([[12, 15], [50, 60], ["x"]], to_out), [(2.0, 5.0)])
        self.assertEqual(len(C.regions([(i * 0.1, [(i * 30, 0, 20, 20)]) for i in range(300)], [], 40, "focus")[0]) <= 40, True)


if __name__ == "__main__":
    unittest.main()
