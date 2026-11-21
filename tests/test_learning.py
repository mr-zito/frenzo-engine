"""Tests for the learning safety limits and keyword escaping.  Run:  .venv\\Scripts\\python -m unittest tests.test_learning"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frenzo import learning as L  # noqa: E402


def cfg(tmp):
    return {"storage": {"output_dir": tmp, "recordings_dir": tmp},
            "detection": {"spike_multiplier": 3.0, "min_messages_in_window": 15, "voice_sensitivity": "normal",
                          "chaos_ratio": 2.6},
            "clips": {"min_score": 6, "lead_in": 1.5, "tail": 2.0},
            "streamers": [{"login": "kaicenat", "key": "twitch:kaicenat", "hot_takes": True},
                          {"login": "lacy", "key": "twitch:lacy"}],
            "anthropic": {"claude_code_model": "haiku"}, "learning": {"auto": True}}


class Limits(unittest.TestCase):
    def test_step_is_at_most_25_percent(self):
        self.assertEqual(L.clamp_step("spike_multiplier", 3.0, 1.0), 2.25)      # wanted 1.0, moves 25% max
        self.assertEqual(L.clamp_step("spike_multiplier", 3.0, 9.0), 3.75)
        self.assertEqual(L.clamp_step("chaos_ratio", 2.6, 2.4), 2.4)             # small change passes as is

    def test_absolute_bounds(self):
        self.assertEqual(L.clamp_step("spike_multiplier", 2.0, 1.0), 1.8)        # 25% would be 1.5: floor 1.8
        self.assertEqual(L.clamp_step("chaos_ratio", 4.0, 5.0), 4.0)
        self.assertEqual(L.clamp_step("min_messages_in_window", 6, 1), 5)
        self.assertEqual(L.clamp_step("min_messages_in_window", 40, 100), 40)
        self.assertEqual(L.clamp_step("lead_in", 5.0, 9), 6.0)
        self.assertEqual(L.clamp_step("tail", 0.0, -3), 0.0)

    def test_min_score_moves_one_point(self):
        self.assertEqual(L.clamp_step("min_score", 6, 2), 5)
        self.assertEqual(L.clamp_step("min_score", 6, 10), 7)
        self.assertEqual(L.clamp_step("min_score", 4, 1), 4)
        self.assertEqual(L.clamp_step("min_score", 9, 12), 9)
        self.assertIsInstance(L.clamp_step("min_score", 6, 5.4), int)

    def test_bad_values_are_ignored(self):
        self.assertEqual(L.clamp_step("chaos_ratio", 2.6, "abc"), 2.6)
        self.assertEqual(L.clamp_step("chaos_ratio", 2.6, float("nan")), 2.6)

    def test_sanitize_whatever_claude_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            ans = {"streamers": {"kaicenat": {"spike_multiplier": 0.1, "min_messages_in_window": 2, "chaos_ratio": 99,
                                              "min_score": 1, "voice_sensitivity": "high", "hot_takes": False},
                                 "twitch:lacy": {"voice_sensitivity": "low", "min_score": 9},
                                 "notastreamer": {"min_score": 4}},
                   "lead_in": 50, "tail": -5, "editor_notes": ["n"] * 30, "title_notes": ["t"] * 10,
                   "chat_keywords": ["a" * 41, "ok", "ok", "x"], "summary": "s"}
            out = L.sanitize(ans, {}, c)
            k = out["streamers"]["kaicenat"]
            self.assertEqual(k["spike_multiplier"], 2.25)
            self.assertEqual(k["min_messages_in_window"], 11)
            self.assertEqual(k["chaos_ratio"], 3.25)
            self.assertEqual(k["min_score"], 5)
            self.assertEqual(k["voice_sensitivity"], "high")
            self.assertIs(k["hot_takes"], False)
            self.assertEqual(out["streamers"]["lacy"], {"voice_sensitivity": "low", "min_score": 7})
            self.assertNotIn("notastreamer", out["streamers"])
            self.assertEqual(out["lead_in"], 1.88)
            self.assertEqual(out["tail"], 1.5)
            self.assertEqual(len(out["editor_notes"]), 12)
            self.assertEqual(len(out["title_notes"]), 6)
            self.assertEqual(out["chat_keywords"], ["ok"])

    def test_voice_moves_one_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            out = L.sanitize({"streamers": {"lacy": {"voice_sensitivity": "high"}}},
                             {"streamers": {"lacy": {"voice_sensitivity": "low"}}}, c)
            self.assertEqual(out["streamers"]["lacy"]["voice_sensitivity"], "normal")

    def test_step_is_from_the_current_learned_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            out = L.sanitize({"streamers": {"lacy": {"spike_multiplier": 1.0}}},
                             {"streamers": {"lacy": {"spike_multiplier": 2.4}}}, c)
            self.assertEqual(out["streamers"]["lacy"]["spike_multiplier"], 1.8)


class Keywords(unittest.TestCase):
    def test_regex_characters_are_escaped(self):
        p = L.keyword_pattern(["what.*", "(ayo)", "a+b", "[wtf]"])
        self.assertTrue(p.search("bro said what.* lol"))
        self.assertFalse(p.search("bro said whatever"))           # ".*" is not a wildcard
        self.assertTrue(p.search("(ayo)"))
        self.assertFalse(p.search("ayo"))
        self.assertTrue(p.search("a+b"))
        self.assertFalse(p.search("aab"))
        self.assertFalse(p.search("w"))

    def test_whole_words_and_case(self):
        p = L.keyword_pattern(["no shot", "GYAT"])
        self.assertTrue(p.search("NO SHOT bro"))
        self.assertTrue(p.search("gyat"))
        self.assertFalse(p.search("gyattt"))
        self.assertFalse(p.search("noshot"))

    def test_emoji_and_limits(self):
        p = L.keyword_pattern(["💀💀"])
        self.assertTrue(p.search("lol 💀💀 lol"))
        self.assertIsNone(L.keyword_pattern([]))
        self.assertIsNone(L.keyword_pattern(["x", "a" * 41, 5, None, "\x00bad"]))
        self.assertEqual(len(L.clean_keywords([f"word{i}" for i in range(50)])), 20)
        self.assertEqual(L.clean_keywords(["  lots   of   space  ", "LOTS OF SPACE"]), ["lots of space"])

    def test_pattern_cannot_be_abused(self):
        evil = "(a+)+$"
        p = L.keyword_pattern([evil])
        self.assertEqual(p.pattern.count(re.escape(evil)), 1)
        self.assertFalse(p.search("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab"))


class Files(unittest.TestCase):
    def test_save_undo_reset_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            L._save_learned(c, {"editor_notes": ["one"]})
            L._save_learned(c, {"editor_notes": ["two"]})
            self.assertEqual(L.load_learned(c)["editor_notes"], ["two"])
            L.reset(c)
            self.assertTrue(L.load_learned(c).get("reset"))
            self.assertTrue(L.undo(c))
            self.assertEqual(L.load_learned(c)["editor_notes"], ["two"])
            self.assertTrue(L.undo(c))
            self.assertEqual(L.load_learned(c)["editor_notes"], ["one"])
            self.assertFalse(L.undo(c))
            self.assertEqual(L.load_learned(c), {})

    def test_examples_from_the_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            meta = {"login": "marlon", "title_bar": "Old title", "category": "bug", "viral_score": 6, "reason": "r",
                    "flagged_by": "gameplay chaos", "what_happens": "w", "duration": 20, "recording": "twitch_x_1.ts"}
            L.bad_pick(c, meta)
            self.assertIsNone(L.title_fix(c, meta, "Old title"))           # unchanged title: no example
            L.title_fix(c, dict(meta, user_context="ctx"), "New title", "idea")
            self.assertIsNone(L.timing_fix(c, meta, 10.0, 30.0, 10.2, 30.3))  # < 0.5 s: no example
            L.timing_fix(c, meta, 10.0, 30.0, 7.0, 31.0)
            exs = L.examples(c)
            self.assertEqual([e["type"] for e in exs], ["deleted", "retitled", "timing"])
            self.assertEqual(exs[0]["recording"], "twitch_x_1")
            self.assertEqual((exs[1]["old"], exs[1]["new"], exs[1]["source"], exs[1]["context"]),
                             ("Old title", "New title", "idea", "ctx"))
            self.assertEqual((exs[2]["added_before"], exs[2]["added_after"]), (3.0, 1.0))
            self.assertEqual(L.new_since_last_run(c), 3)
            self.assertFalse(L.auto_due(c))                                   # needs 5 new examples
            for _ in range(2):
                L.bad_pick(c, meta)
            self.assertTrue(L.auto_due(c))
            c["learning"]["auto"] = False
            self.assertFalse(L.auto_due(c))
            self.assertEqual(L.remove_examples(c, lambda e: e["type"] == "deleted"), 3)
            summary = L.build_summary(c, L.examples(c), [], {})
            self.assertIn("TITLE FIXES", summary)
            self.assertIn("TIMING FIXES", summary)
            self.assertLess(len(L.build_summary(c, L.examples(c) * 500, [], {})), 14100)   # prompt size is capped

    def test_reedit_lesson(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            before = {"login": "kaicenat", "title_bar": "Old", "category": "bug", "viral_score": 6, "duration": 40.0,
                      "start_in_recording": 100.0, "recording": "twitch_kaicenat_1.ts", "flagged_by": "chaos"}
            after = dict(before, title_bar="Kai's new title", duration=30.0, start_in_recording=97.0,
                         what_happens="Kai crashes")
            self.assertIsNone(L.reedit_lesson(c, before, dict(before)))         # nothing asked, nothing changed
            ex = L.reedit_lesson(c, before, after, note="start earlier, mention Kai", context="he crashes")
            self.assertEqual(ex["type"], "reedited")
            self.assertEqual((ex["old_title"], ex["new_title"]), ("Old", "Kai's new title"))
            self.assertEqual((ex["added_before"], ex["added_after"]), (3.0, -13.0))
            self.assertTrue(ex["by_engine"])
            s = L.build_summary(c, L.examples(c), [], {})
            self.assertIn("RE-EDITS THE OWNER ASKED CLAUDE FOR", s)
            self.assertIn("start earlier, mention Kai", s)
            self.assertEqual(L.overview(c)["counts"]["reedited"], 1)

    def test_apply_and_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            L._write_json(os.path.join(L.ldir(c), "learned.json"), {
                "streamers": {"lacy": {"min_score": 5, "chaos_ratio": 2.2, "hot_takes": True}},
                "lead_in": 3.0, "editor_notes": ["Keep NPC moments"], "title_notes": ["Use the streamer's own words"]})
            L.apply_to_config(c)
            lacy = c["streamers"][1]
            self.assertEqual(L.min_score_for(c, lacy), 5)
            self.assertEqual(L.min_score_for(c, c["streamers"][0]), 6)
            self.assertEqual(L.detection_for(c, lacy)["chaos_ratio"], 2.2)
            self.assertEqual(L.detection_for(c, lacy)["spike_multiplier"], 3.0)
            self.assertTrue(L.hot_takes_for(lacy))
            self.assertEqual(L.clip_value(c, "lead_in"), 3.0)
            self.assertEqual(L.clip_value(c, "tail"), 2.0)
            self.assertEqual(c["clips"]["lead_in"], 1.5)           # config values are never overwritten
            notes = L.prompt_notes(c)
            self.assertIn("WHAT THE PAGE OWNER LIKES", notes)
            self.assertIn("Keep NPC moments", notes)
            self.assertIn("Use the streamer's own words", notes)


if __name__ == "__main__":
    unittest.main()
