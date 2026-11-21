"""Picking moments: scaled chat floor, learned chat delay, hourly cap keeps the best.
Run:  .venv\\Scripts\\python -m unittest tests.test_picking"""
import tempfile
import time
import types
import unittest

from frenzo import chatdelay
from frenzo.config import DEFAULTS, _merge
from frenzo.detector import SpikeDetector, poisson_tail
from frenzo.moments import MomentQueue
from frenzo.replay import ReplayChat


def _chat(rate_per_10s, burst_at=None, burst=0, length=900):
    msgs = []
    step = 10.0 / max(rate_per_10s, 0.01)
    t = 0.0
    while t < length:
        msgs.append((t, "u", "ok"))
        t += step
    if burst_at is not None:
        msgs += [(burst_at + i * (8.0 / burst), f"b{i}", "ok") for i in range(burst)]
    return ReplayChat(msgs)


def _run(det_cfg, chat, length=900):
    d = SpikeDetector(chat, det_cfg)
    d.started = -1e9
    hits, t = [], 70.0
    while t < length:
        h = d.check(t)
        if h:
            hits.append(h)
        t += 5
    return hits


class Floor(unittest.TestCase):
    def test_poisson_tail(self):
        self.assertAlmostEqual(poisson_tail(0, 3), 1.0)
        self.assertLess(poisson_tail(15, 2), 1e-6)
        self.assertGreater(poisson_tail(3, 2), 0.2)
        self.assertLess(poisson_tail(400, 190), 1e-3)          # normal approximation branch
        self.assertGreater(poisson_tail(200, 190), 0.1)

    def test_small_streamer_can_trigger_with_scaled_floor(self):
        chat = _chat(1.0, burst_at=600, burst=9)                    # 1 msg / 10 s, then 9 in 8 s
        fixed = _merge(DEFAULTS["detection"], {"chat_floor": "fixed"})
        scaled = _merge(DEFAULTS["detection"], {"chat_floor": "scaled"})
        self.assertEqual(_run(fixed, chat), [])                     # the old fixed 15 never fires
        hits = _run(scaled, chat)
        self.assertEqual(len(hits), 1)
        self.assertTrue(590 <= hits[0][0] <= 610)

    def test_big_chat_needs_a_real_spike(self):
        calm = _chat(150.0)
        self.assertEqual(_run(_merge(DEFAULTS["detection"], {}), calm), [])


class Delay(unittest.TestCase):
    def test_estimate_finds_the_peak_lag(self):
        curves = [[0] * 26 for _ in range(6)]
        for c in curves:
            c[7], c[8], c[9] = 3, 8, 4
        self.assertEqual(chatdelay.estimate(curves), 8.0)
        self.assertIsNone(chatdelay.estimate(curves[:3]))         # too few reactions

    def test_curve_from_chat(self):
        msgs = [(t, "u", "x") for t in range(0, 100, 5)] + [(52.5 + i * 0.1, "b", "x") for i in range(10)]
        c = chatdelay.curve(ReplayChat(msgs), 46.0)
        self.assertEqual(max(range(len(c)), key=lambda i: c[i]), 6)

    def test_store_and_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"storage": {"output_dir": d}, "detection": {"learned_chat_delay": True}}
            self.assertEqual(chatdelay.delay_for(cfg, "kick:x", "kick"), 8.0)
            c = [0] * 26
            c[11] = 9
            for _ in range(5):
                chatdelay.add_curve(cfg, "kick:x", c)
            self.assertEqual(chatdelay.delay_for(cfg, "kick:x", "kick"), 11.0)
            cfg["detection"]["learned_chat_delay"] = False
            self.assertEqual(chatdelay.delay_for(cfg, "kick:x", "kick"), 8.0)

    def test_detector_anchor_uses_delay(self):
        chat = _chat(1.0, burst_at=600, burst=9)
        d = SpikeDetector(chat, DEFAULTS["detection"])
        d.started, d.delay = -1e9, 10.0
        t, hit = 70.0, None
        while t < 900 and not hit:
            hit = d.check(t)
            t += 5
        self.assertAlmostEqual(hit[0], (t - 5) - 6 - 4)               # 4 s earlier than the 6 s default


class Cap(unittest.TestCase):
    def test_weaker_waiting_moments_make_room(self):
        from frenzo.engine import Session
        with tempfile.TemporaryDirectory() as d:
            cfg = _merge(DEFAULTS, {"storage": {"recordings_dir": d, "output_dir": d},
                                    "clips": {"max_per_hour_per_streamer": 2}})
            info = types.SimpleNamespace(login="kai", display="Kai", url="u", platform="twitch", title="", game="",
                                         user_id=None)
            q = MomentQueue()
            s = Session({"login": "kai", "key": "twitch:kai", "platform": "twitch", "tags": []}, info, object(), cfg, q)
            s.recorder.t0, s.recorder.path = time.time() - 3000, d + "/rec.ts"
            now = time.time()
            for i in range(6):                           # room = max(6, 3 * 2) = 6
                s._queue(now - 2000 + i * 100, "streamer reaction: sudden loud outburst")
            self.assertEqual(q.qsize(), 6)
            s._queue(now - 100, "chat spike: 9.0x faster than normal + gameplay chaos: 4.0x wilder")
            self.assertEqual(q.qsize(), 6)
            self.assertTrue(any("chat spike" in c.source for c in q.waiting()))
            self.assertEqual(s.pending, 6)               # the dropped one was released


if __name__ == "__main__":
    unittest.main()
