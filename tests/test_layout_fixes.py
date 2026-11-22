import unittest

from frenzo import editor, titles


class DoubledWords(unittest.TestCase):
    def test_small_word(self):
        self.assertEqual(titles.drop_doubled("GTA 6 NPC tries to to rob Lacy"), "GTA 6 NPC tries to rob Lacy")

    def test_quotes_kept(self):
        self.assertEqual(titles.drop_doubled("'almost almost'"), "'almost almost'")
        self.assertEqual(titles.drop_doubled("no no no"), "no no no")


class AvoidCam(unittest.TestCase):
    def test_left_cam_pushes_right(self):
        lo, hi = editor.game_x_limits({"x": 0.02, "y": 0.05, "w": 0.19, "h": 0.2}, (1920, 1080), 1.0, editor.REEL_GAME_H)
        self.assertIsNone(hi)
        self.assertGreater(lo, 0.21)

    def test_right_cam_keeps_left(self):
        lo, hi = editor.game_x_limits({"x": 0.78, "y": 0.0, "w": 0.2, "h": 0.25}, (1920, 1080), 1.0, editor.REEL_GAME_H)
        self.assertEqual(lo, 0.0)
        crop_w = (1080 / 1920) * editor.W / editor.REEL_GAME_H
        self.assertLessEqual(hi + crop_w, 0.78)

    def test_off_keeps_old_expr(self):
        self.assertEqual(editor.game_x_expr(0.4, 0.0, None), "clip(iw*0.4000-ow/2,0,iw-ow)")


if __name__ == "__main__":
    unittest.main()
