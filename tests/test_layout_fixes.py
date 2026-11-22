import unittest

from frenzo import editor, titles


class DoubledWords(unittest.TestCase):
    def test_small_word(self):
        self.assertEqual(titles.drop_doubled("GTA 6 NPC tries to to rob Lacy"), "GTA 6 NPC tries to rob Lacy")

    def test_quotes_kept(self):
        self.assertEqual(titles.drop_doubled("'almost almost'"), "'almost almost'")
        self.assertEqual(titles.drop_doubled("no no no"), "no no no")


CROP_W = (1080 / 1920) * editor.W / editor.REEL_GAME_H


def lim(cam, fx=0.4):
    return editor.game_x_limits(cam, (1920, 1080), 1.0, editor.REEL_GAME_H, fx)


class AvoidCam(unittest.TestCase):
    def test_left_webcam_frame_pushes_right(self):      # Lacy: the crop sits inside his webcam overlay
        lo, hi = lim({"x": 0.02, "y": 0.05, "w": 0.19, "h": 0.2, "how": "Claude + face scan, webcam frame"})
        self.assertIsNone(hi)
        self.assertGreater(lo, 0.21)
        self.assertLessEqual(lo, max(0.0, 0.4 - CROP_W / 2) + editor.MAX_CAM_SHIFT + 1e-9)

    def test_right_cam_keeps_left(self):
        lo, hi = lim({"x": 0.78, "y": 0.0, "w": 0.2, "h": 0.25, "how": "set by hand in the app"}, fx=0.6)
        self.assertEqual(lo, 0.0)
        self.assertLessEqual(hi + CROP_W, 0.78)

    def test_around_the_face_not_moved(self):           # KreekCraft: that box is wider than his real webcam
        self.assertEqual(lim({"x": 0.0, "y": 0.12, "w": 0.35, "h": 0.37, "how": "Claude + face scan, around the face"},
                             fx=0.495), (0.0, None))

    def test_real_frame_used(self):
        lo, _ = lim({"x": 0.0, "y": 0.1, "w": 0.15, "h": 0.3, "how": "Claude + webcam frame",
                     "frame": {"x": 0.0, "y": 0.0, "w": 0.22, "h": 0.5}}, fx=0.3)
        self.assertAlmostEqual(lo, min(0.23, 0.3 - CROP_W / 2 + editor.MAX_CAM_SHIFT), places=3)

    def test_no_shift_when_already_clear(self):
        lo, _ = lim({"x": 0.0, "y": 0.0, "w": 0.1, "h": 0.2, "how": "set by hand in the app"}, fx=0.6)
        self.assertLessEqual(lo, 0.6 - CROP_W / 2)

    def test_off_keeps_old_expr(self):
        self.assertEqual(editor.game_x_expr(0.4, 0.0, None), "clip(iw*0.4000-ow/2,0,iw-ow)")


class ReeditNotes(unittest.TestCase):
    def test_sound_words(self):
        from frenzo import redo
        self.assertFalse(redo.note_about("recenter the the gameplay", redo.SOUND_WORDS))
        self.assertFalse(redo.note_about("make it shorter", redo.SOUND_WORDS))
        self.assertTrue(redo.note_about("remove the faahh sound effects used", redo.SOUND_WORDS))
        self.assertTrue(redo.note_about("add the bua-wa-wa-wa-wa at 3.5s", redo.SOUND_WORDS))
        self.assertTrue(redo.note_about("put cash register at 4s", redo.SOUND_WORDS, ["cash-register"]))

    def test_framing_words(self):
        from frenzo import redo
        self.assertTrue(redo.note_about("recenter the the gameplay", redo.FRAMING_WORDS))
        self.assertFalse(redo.note_about("remove the faah", redo.FRAMING_WORDS))

    def test_cues_in_window(self):
        from frenzo import redo
        meta = {"duration": 20, "jump_cuts": [[0, 10], [15, 25]],
                "sound": {"cues": [{"sfx": "boom", "t": 0.4}, {"sfx": "faahhhh", "t": 12.0}]}}
        got = redo._cues_in_window(meta, 60.0, 0.0)
        self.assertEqual([c["t"] for c in got], [60.4, 77.0])


if __name__ == "__main__":
    unittest.main()
