"""Past-stream crowd signals: chat replay through the live detectors, YouTube chat parsing, the heatmap,
Twitch VOD clips, and merging crowd + detector moments.  Run:  .venv\\Scripts\\python -m unittest tests.test_replay"""
import json
import os
import random
import tempfile
import unittest

from frenzo import replay
from frenzo.config import DEFAULTS


class Replay(unittest.TestCase):
    def test_replay_chat_reads_like_live_chat(self):
        ch = replay.ReplayChat([(5.0, "a", "hi"), (1.0, "b", "yo"), (9.0, "c", "LMAO")])
        self.assertEqual([m[0] for m in ch.snapshot(0, 6)], [1.0, 5.0])
        self.assertEqual(ch.count_between(0, 10), 3)
        self.assertEqual(ch.end, 9.0)

    def test_chat_spike_found_in_replay(self):
        rnd = random.Random(1)
        msgs = [(t + rnd.random(), "u", "ok") for t in range(0, 1200, 2)]          # calm: 1 msg / 2 s
        msgs += [(700 + i * 0.1, f"u{i}", "KEKW NO WAY") for i in range(120)]          # burst at 700-712 s
        hits = replay.chat_moments(replay.ReplayChat(msgs), DEFAULTS["detection"])
        spikes = [h for h in hits if h[1].startswith("chat replay:")]
        self.assertTrue(spikes)
        self.assertTrue(690 <= spikes[0][0] <= 715, spikes[0])

    def test_parse_youtube_chat(self):
        line = {"replayChatItemAction": {"videoOffsetTimeMsec": "61500", "actions": [{"addChatItemAction": {"item": {
            "liveChatTextMessageRenderer": {"message": {"runs": [{"text": "bro "}, {"emoji": {"shortcuts": [":skull:"]}}]},
                                            "authorName": {"simpleText": "@fan"}}}}}]}}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "chat.live_chat.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n" + "garbage\n")
            self.assertEqual(replay.parse_youtube_chat(p), [(61.5, "@fan", "bro :skull:")])

    def test_heatmap_peaks(self):
        hm = [{"start_time": i * 10, "end_time": i * 10 + 10, "value": v} for i, v in
              enumerate([0.1, 0.2, 0.9, 0.3, 0.2, 0.25, 0.2, 1.0, 0.4, 0.1])]
        peaks = replay.heatmap_peaks(hm, min_gap=20)
        self.assertEqual([p[0] for p in peaks], [75.0, 25.0])
        self.assertEqual(replay.heatmap_peaks(None), [])

    def test_twitch_duration_and_id(self):
        self.assertEqual(replay.twitch_duration("3h2m1s"), 10921)
        self.assertEqual(replay.twitch_duration("45s"), 45)
        self.assertEqual(replay.twitch_vod_id("https://www.twitch.tv/videos/2601234567?t=1h"), "2601234567")
        self.assertIsNone(replay.twitch_vod_id("https://youtube.com/watch?v=x"))

    def test_vod_clips_keeps_this_vod_only_and_places_them(self):
        class Api:
            calls = []

            def video(self, vid):
                return {"id": vid, "user_id": "555", "created_at": "2026-11-20T10:00:00Z", "duration": "3h0m0s"}

            def clips_between(self, uid, a, b):
                self.calls.append((a, b))
                if len(self.calls) > 1:
                    return []
                base = {"created_at": "2026-11-20T11:00:00Z", "duration": 30, "view_count": 5, "game_id": "1"}
                return [dict(base, id="c1", creator_id="1", video_id="99", vod_offset=600),
                        dict(base, id="c2", creator_id="2", video_id="98", vod_offset=100),       # another VOD
                        dict(base, id="c3", creator_id="555", video_id="99", vod_offset=610),     # broadcaster
                        dict(base, id="c4", creator_id="3", video_id="99", vod_offset=None)]      # not placed yet
        api = Api()
        clips, v = replay.vod_clips(api, "99", chunk_hours=2)
        self.assertEqual([c.id for c in clips], ["c1"])
        self.assertEqual(clips[0].span, (600.0, 630.0))
        self.assertEqual(len(api.calls), 2)                    # 3 h stream in 2 h chunks (tight ranges)

    def test_merge_puts_crowd_and_detectors_together(self):
        from frenzo.vods import _merge_moments
        found = [(100.0, "most replayed: 0.90 (YouTube heatmap peak)", 6.6, {"signals": {"heatmap": 3.7}}),
                 (120.0, "streamer reaction: laughing", 3, {}),
                 (900.0, "gameplay chaos: 3.0x wilder than normal (bug/physics?)", 12, {})]
        ms = _merge_moments(found)
        self.assertEqual(len(ms), 2)
        self.assertIn("streamer reaction", ms[0]["why"])
        self.assertTrue(ms[0]["crowd"])
        self.assertGreater(ms[0]["score"], ms[1]["score"])


if __name__ == "__main__":
    unittest.main()
