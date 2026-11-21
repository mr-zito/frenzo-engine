"""Manual cuts from past streams: range parsing, keeping / listing / expiring previews.
Run:  .venv\\Scripts\\python -m unittest tests.test_streams"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from frenzo import crowd, streams


class Ranges(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(streams.parse_ranges(["1:23:45-1:24:30"]), [(5025.0, 5070.0)])
        self.assertEqual(streams.parse_ranges(["5025-5070"]), [(5025.0, 5070.0)])
        self.assertEqual(streams.parse_ranges(["83:20-83:40.5"]), [(5000.0, 5020.5)])
        self.assertEqual(streams.parse_ranges(["600-650,10-20", "30 - 40"]), [(10.0, 20.0), (30.0, 40.0), (600.0, 650.0)])

    def test_bad_ranges(self):
        for bad in (["10-11"], ["50-40"], ["abc"], ["1:2:3:4-1:2:3:5"], ["10-"]):
            with self.assertRaises(ValueError):
                streams.parse_ranges(bad)


class Keep(unittest.TestCase):
    def test_keep_list_and_expire(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"storage": {"output_dir": d}, "past": {"keep_preview_hours": 48}, "render": {}}
            prev = os.path.join(d, "preview.mp4")
            open(prev, "wb").write(b"x" * 100)
            clip = crowd.ViewerClip(id="c", title="LOL", views=9, creator="fan")
            clip.span = (100.0, 130.0)
            found = [(128.0, "viewer clips: 1 clipper(s)", 6, {"clips": [clip], "span": (100, 130)}),
                     (500.0, "most replayed: 0.90 (YouTube heatmap peak)", 6.6, {"signals": {"heatmap": 3.7}})]
            moments = [{"sec": 128.0, "why": "viewer clips: 1 clipper(s)", "score": 5.6}]
            vod = {"id": "youtube_abc", "url": "https://youtube.com/watch?v=abc", "title": "GTA", "author": "Speed",
                   "duration": 3600}
            with mock.patch("frenzo.recordings.loudness_curve", return_value=[-20.0, -18.0]):
                dest = streams.keep(cfg, {"login": "ishowspeed", "key": "youtube:ishowspeed"}, vod, prev, moments, found)
            self.assertTrue(os.path.isfile(os.path.join(dest, "preview.mp4")))
            self.assertFalse(os.path.exists(prev))                    # moved, not copied
            m = streams.load(cfg, "youtube_abc")
            self.assertEqual(m["viewer"][0]["a"], 100.0)
            self.assertEqual(m["heat"][0]["t"], 500.0)
            self.assertEqual(m["moments"][0]["score"], 5.6)
            self.assertEqual([s["vod_id"] for s in streams.list_streams(cfg)], ["youtube_abc"])
            meta_path = os.path.join(dest, "stream.json")
            data = json.load(open(meta_path, encoding="utf-8"))
            data["expires"] = time.time() - 1
            json.dump(data, open(meta_path, "w", encoding="utf-8"))
            self.assertEqual(streams.list_streams(cfg), [])
            self.assertEqual(streams.cleanup(cfg), 1)
            self.assertFalse(os.path.exists(dest))

    def test_keep_off(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"storage": {"output_dir": d}, "past": {"keep_preview_hours": 0}}
            self.assertIsNone(streams.keep(cfg, {}, {"id": "x"}, os.path.join(d, "p.mp4"), [], []))


if __name__ == "__main__":
    unittest.main()
