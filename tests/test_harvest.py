"""Clip harvest: ranges, GTA VI filter, skipping moments Frenzo already clipped, the processed-clip cache.
Run:  .venv\\Scripts\\python -m unittest tests.test_harvest"""
import json
import os
import tempfile
import unittest

from frenzo import harvest
from frenzo.config import DEFAULTS, _merge

GTA = "1867279146"


def _raw(cid, creator, offset, views=10, game=GTA, video="99", created="2026-11-21T03:00:00Z", dur=30):
    return {"id": cid, "url": f"https://clips.twitch.tv/{cid}", "creator_id": creator, "creator_name": f"u{creator}",
            "title": f"clip {cid}", "created_at": created, "duration": dur, "view_count": views, "is_featured": False,
            "video_id": video, "vod_offset": offset, "game_id": game, "broadcaster_id": "555"}


class FakeApi:
    def __init__(self, clips):
        self.clips = clips

    def users(self, logins):
        return {l: "555" for l in logins}

    def game_id(self, name):
        return GTA

    def clips_between(self, uid, a, b):
        return self.clips


class Harvest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _merge(DEFAULTS, {"storage": {"output_dir": self.tmp.name, "recordings_dir": self.tmp.name}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_ranges(self):
        self.assertEqual(harvest.parse_since("24h"), 86400)
        self.assertEqual(harvest.parse_since("90m"), 5400)
        self.assertEqual(harvest.parse_since("2d"), 172800)
        with self.assertRaises(ValueError):
            harvest.parse_since("yesterday")
        a = harvest.parse_when("2026-11-21T10:00")
        self.assertEqual(harvest.parse_when("2026-11-21 10:00"), a)
        with self.assertRaises(ValueError):
            harvest.parse_when("21/11/2026")

    def test_gta_filter_grouping_and_minimums(self):
        api = FakeApi([_raw("a", "1", 100), _raw("b", "2", 110), _raw("c", "3", 112),        # one moment, 3 clippers
                       _raw("d", "4", 900),                                                   # alone: under min 2
                       _raw("e", "5", 2000, game="27471"),                                    # Minecraft
                       _raw("f", "555", 2500)])                                               # the broadcaster
        moments, stats = harvest.find_moments(self.cfg, api, "kai", 0, 1e10, min_clippers=2)
        self.assertEqual(stats["clips_found"], 6)
        self.assertEqual(stats["not_gta"], 1)
        self.assertEqual(stats["moments"], 2)
        self.assertEqual(stats["below_minimums"], 1)
        self.assertEqual(len(moments), 1)
        self.assertEqual(moments[0].clippers, 3)
        self.assertEqual((moments[0].timeline, moments[0].vod_id), ("vod", "99"))

    def test_skips_moments_frenzo_already_clipped(self):
        d = os.path.join(self.tmp.name, "kai", "2026-11-21", "x")
        os.makedirs(d)
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"vod_url": "https://www.twitch.tv/videos/99", "vod_start": 95.0, "duration": 25}, f)
        api = FakeApi([_raw("a", "1", 100), _raw("b", "2", 110), _raw("g", "6", 3000), _raw("h", "7", 3005)])
        moments, stats = harvest.find_moments(self.cfg, api, "kai", 0, 1e10, min_clippers=2)
        self.assertEqual(stats["skipped_already_clipped"], 1)
        self.assertEqual([m.key for m in moments], ["g"])

    def test_live_clip_time_from_recording_name(self):
        d = os.path.join(self.tmp.name, "kai", "2026-11-21", "y")
        os.makedirs(d)
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"recording": "twitch_kai_20261121_030000.ts", "start_in_recording": 120.0, "duration": 30}, f)
        wall, vod = harvest.clipped_spans(self.cfg, "kai")
        import datetime as dt
        t0 = dt.datetime(2026, 11, 21, 3, 0, 0).timestamp()
        self.assertEqual(wall, [(t0 + 120, t0 + 150)])

    def test_processed_cache(self):
        self.assertEqual(harvest.done_ids(self.cfg), set())
        harvest.mark_done(self.cfg, ["a", "b"])
        harvest.mark_done(self.cfg, ["b", "c"])
        self.assertEqual(harvest.done_ids(self.cfg), {"a", "b", "c"})
        api = FakeApi([_raw("a", "1", 100), _raw("b", "2", 110), _raw("x", "3", 500), _raw("y", "4", 505)])
        moments, stats = harvest.find_moments(self.cfg, api, "kai", 0, 1e10, min_clippers=2)
        self.assertEqual(stats["harvested_before"], 2)
        self.assertEqual([m.key for m in moments], ["x"])


if __name__ == "__main__":
    unittest.main()
