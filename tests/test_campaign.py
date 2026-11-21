"""Tests for per-streamer campaign rules.  Run:  .venv\\Scripts\\python -m unittest tests.test_campaign"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frenzo import campaign as C  # noqa: E402

KAI = {"name": "Kai Cenat GTA 6 - $10K", "instructions": "Title must mention Kai's name, max 40 seconds, always credit @kaicenat",
       "hashtags": ["kaicenat", "#gta6"], "mentions": ["kaicenat"], "max_seconds": 40}


class Rules(unittest.TestCase):
    def test_defaults_when_missing(self):
        d = C.normalize(None)
        self.assertEqual(d, C.DEFAULT)
        self.assertFalse(C.has_rules(d))
        self.assertIsNone(C.of({"login": "x"}))

    def test_normalize(self):
        d = C.normalize(dict(KAI, platforms=["TikTok", "x", "myspace"], min_seconds=60, watermark=False))
        self.assertEqual(d["hashtags"], ["#kaicenat", "#gta6"])
        self.assertEqual(d["mentions"], ["@kaicenat"])
        self.assertEqual(d["platforms"], ["tiktok", "x"])
        self.assertEqual(d["min_seconds"], 40.0)          # min can't be above max
        self.assertFalse(d["watermark"])
        self.assertTrue(d["captions"])
        self.assertTrue(C.has_rules(d))

    def test_post_text(self):
        camp = C.normalize(KAI)
        tags, cap = C.apply_text("#gta6 #funny", "Kai loses it", camp)
        self.assertEqual(tags, "#gta6 #funny #kaicenat")             # no duplicates
        self.assertEqual(cap, "Kai loses it\n\nCredit: @kaicenat")
        tags2, cap2 = C.apply_text(tags, cap, camp)                   # applying twice changes nothing
        self.assertEqual((tags2, cap2), (tags, cap))
        _, cap3 = C.apply_text("", "big W @KaiCenat", camp)          # already credited (any case)
        self.assertEqual(cap3, "big W @KaiCenat")
        _, cap4 = C.apply_text("", "x", C.normalize(dict(KAI, instructions="Always put 'via @kaicenat'")))
        self.assertTrue(cap4.endswith("Via: @kaicenat"))

    def test_meta(self):
        meta = {"hashtags": "#gta6", "caption": "c"}
        self.assertTrue(C.apply_to_meta(meta, C.normalize(KAI)))
        self.assertEqual(meta["campaign"], "Kai Cenat GTA 6 - $10K")
        self.assertFalse(C.apply_to_meta(meta, C.normalize(KAI)))
        self.assertFalse(C.apply_to_meta(meta, None))

    def test_length(self):
        clips = {"min_length": 12, "max_length": 45}
        self.assertEqual(C.length_bounds(clips, None), (12.0, 45.0))
        self.assertEqual(C.length_bounds(clips, C.normalize(KAI)), (12.0, 40.0))
        self.assertEqual(C.length_bounds(clips, C.normalize(dict(KAI, max_seconds=10))), (10.0, 10.0))
        segs = C.fit_segments([[0, 20], [25, 50]], 0, 60, 12, 40)
        self.assertAlmostEqual(sum(b - a for a, b in segs), 40)
        self.assertEqual(segs[0], [0.0, 20.0])                        # the setup is kept
        segs = C.fit_segments([[10, 15]], 0, 60, 12, 40)              # too short: extended
        self.assertAlmostEqual(sum(b - a for a, b in segs), 12)
        segs = C.fit_segments([[50, 58]], 0, 60, 12, 40)              # can't extend the end enough: start earlier
        self.assertEqual(segs, [[48.0, 60.0]])

    def test_render_and_files(self):
        camp = C.normalize(dict(KAI, captions=False, sound_effects=False, watermark=False))
        r = C.render_cfg({"captions": "mini", "sound_effects": True, "layout": "reel"}, camp)
        self.assertEqual((r["captions"], r["sound_effects"], r["layout"]), (False, False, "reel"))
        names = ["clip_clean.mp4", "clip_tiktok+instagram.mp4", "clip_x.mp4", "clip_youtube.mp4"]
        self.assertEqual(C.post_file(names, "tiktok", None), "clip_tiktok+instagram.mp4")
        self.assertEqual(C.post_file(names, "facebook", None), "clip_clean.mp4")
        self.assertEqual(C.post_file(names, "tiktok", camp), "clip_clean.mp4")
        txt = C.caption_txt({"title": "t", "caption": "c", "hashtags": "#a"}, names, 60, camp)
        self.assertIn("clip_clean.mp4  ->  every platform", txt)
        self.assertNotIn("clip_x.mp4  ->", txt)

    def test_prompt(self):
        p = C.prompt_section(C.normalize(KAI))
        self.assertIn("CAMPAIGN RULES for this streamer (Kai Cenat GTA 6 - $10K)", p)
        self.assertIn("Title must mention Kai's name", p)
        self.assertIn("Required hashtags: #kaicenat #gta6", p)
        self.assertIn("Required mentions/credit: @kaicenat", p)
        self.assertIn("Length: at most 40 seconds", p)
        self.assertIn("They never change keep or viral_score", p)
        self.assertEqual(C.prompt_section(None), "")


class Extras(unittest.TestCase):
    def test_overlay_and_look_for_normalize(self):
        d = C.normalize({"overlay": {"file": "assets/overlays/a.png", "position": "LEFT-ish", "size": 500, "opacity": 0},
                         "look_for": "the 1WIN casino", "chat_keywords": "1win, casino ,(a+)+$", "speech_keywords": ["1win"]})
        self.assertEqual(d["overlay"], {"file": "assets/overlays/a.png", "position": "top-right", "size": 100, "opacity": 1.0})
        self.assertEqual(d["chat_keywords"], ["1win", "casino", "(a+)+$"])
        self.assertTrue(C.has_rules(d))
        self.assertEqual(C.normalize({})["overlay"], C.OVERLAY_DEFAULT)

    def test_patterns_are_escaped_and_extra(self):
        camp = C.normalize({"chat_keywords": ["1win", "a.b"], "speech_keywords": ["use code frenzo"]})
        self.assertTrue(C.chat_pattern(camp).search("chat: 1WIN casino lol"))
        self.assertFalse(C.chat_pattern(camp).search("axb"))
        self.assertTrue(C.speech_pattern(camp).search("guys use code frenzo now"))
        self.assertIsNone(C.chat_pattern(C.normalize({})))
        self.assertIsNone(C.speech_pattern(None))

    def test_prompt_has_extra_check_but_keeps_scoring(self):
        p = C.prompt_section(C.normalize({"look_for": "moments at the casino", "speech_keywords": ["1win"]}))
        self.assertIn("EXTRA CONTENT THIS CAMPAIGN WANTS", p)
        self.assertIn("moments at the casino", p)
        self.assertIn('"1win"', p)
        self.assertIn("campaign_match", p)
        self.assertIn("never changes keep or viral_score", p)
        self.assertNotIn("CAMPAIGN RULES for this streamer", p)      # no other rules set: only the extra check

    def test_overlay_render_cfg(self):
        import tempfile
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            png = os.path.join(tmp, "o.png")
            Image.new("RGBA", (64, 32), (255, 0, 255, 255)).save(png)
            camp = C.normalize({"overlay": {"file": png, "position": "bottom-left", "size": 30}})
            r = C.render_cfg({"captions": "mini"}, camp)
            self.assertEqual(r["campaign_overlay"]["path"], png)
            self.assertEqual(r["campaign_overlay"]["position"], "bottom-left")
            missing = C.normalize({"overlay": {"file": os.path.join(tmp, "gone.png")}})
            self.assertNotIn("campaign_overlay", C.render_cfg({}, missing))   # missing file: rendered without it

    def test_past_stream_scan_keeps_regular_moments(self):
        from frenzo import hottake, offline, vods, analyzer
        words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate(
            "honestly gta 6 is the best game ever made bro and use code frenzo at 1win right now".split())]

        class T:
            def transcribe(self, audio):
                return words, "en"
        saved = (hottake._transcriber, offline.duration, analyzer.extract_audio)
        hottake._transcriber, offline.duration, analyzer.extract_audio = (lambda cfg: T()), (lambda p: 60.0), (lambda *a, **k: None)
        try:
            pat = C.speech_pattern(C.normalize({"speech_keywords": ["1win"]}))
            regular = vods.take_scan("x", {}, chunk=60)
            both = vods.take_scan("x", {}, chunk=60, extra=pat)
            only = vods.take_scan("x", {}, chunk=60, extra=pat, only_extra=True)
        finally:
            hottake._transcriber, offline.duration, analyzer.extract_audio = saved
        self.assertTrue(regular and all(k != "campaign" for *_, k in regular))
        self.assertEqual([x for x in both if x[3] != "campaign"], regular)      # regular moments unchanged
        self.assertEqual([x[3] for x in both if x[3] == "campaign"], ["campaign"])
        self.assertEqual([x[3] for x in only], ["campaign"])


if __name__ == "__main__":
    unittest.main()
