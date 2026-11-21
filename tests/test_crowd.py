"""Viewer clips as evidence: dedup + clipper counting, live span estimate, audio alignment, the live
session attaching clips to open moments, and the best-first queue."""
import time
import types

import numpy as np

from frenzo import crowd, scoring
from frenzo.moments import MomentQueue


def _clip(cid, creator, a=None, b=None, views=10, featured=False, created=1000.0, duration=30.0):
    c = crowd.ViewerClip(id=cid, creator_id=creator, creator=f"user{creator}", title=f"clip {cid}",
                         created=created, duration=duration, views=views, featured=featured)
    if a is not None:
        c.span = (a, b)
    return c


def test_from_helix_parses_live_clip():
    c = crowd.from_helix({"id": "AbC", "url": "https://clips.twitch.tv/AbC", "creator_id": "7", "creator_name": "Bob",
                          "title": "LMAO", "created_at": "2026-11-21T18:00:00Z", "duration": 28.4, "view_count": 51,
                          "is_featured": False, "video_id": "", "vod_offset": None, "game_id": "1"})
    assert c.id == "AbC" and c.creator == "Bob" and c.views == 51 and c.vod_offset is None
    assert abs(c.duration - 28.4) < 1e-9 and c.created > 0


def test_overlapping_clips_are_one_moment_and_clippers_are_distinct():
    clips = [_clip("a", "1", 100, 130), _clip("b", "2", 110, 140), _clip("c", "1", 112, 138),   # same person twice
             _clip("d", "3", 400, 430)]
    moments = crowd.group(clips)
    assert len(moments) == 2
    first = moments[0]
    assert {c.id for c in first.clips} == {"a", "b", "c"}
    assert first.clippers == 2                       # creator 1 made two clips: counts once
    assert (first.start, first.end) == (100, 140)
    assert moments[1].clippers == 1


def test_bots_and_broadcaster_are_ignored():
    bot = crowd.ViewerClip(id="x", creator="Nightbot", creator_id="9")
    own = crowd.ViewerClip(id="y", creator="kaicenat", creator_id="555")
    viewer = crowd.ViewerClip(id="z", creator="fan", creator_id="1")
    assert crowd.ignored(bot)
    assert crowd.ignored(own, broadcaster_id="555")
    assert not crowd.ignored(viewer, broadcaster_id="555")


def test_featured_and_strength():
    weak = crowd.ClipMoment(0, 1, [_clip("a", "1")])
    feat = crowd.ClipMoment(0, 1, [_clip("b", "1", featured=True)])
    many = crowd.ClipMoment(0, 1, [_clip(str(i), str(i)) for i in range(3)])
    cfg = {"detection": {}}
    assert not crowd.strong(weak, cfg)
    assert crowd.strong(feat, cfg) and crowd.strong(many, cfg)
    assert feat.strength() > weak.strength() and many.strength() > weak.strength()


def test_live_span_estimate():
    c = _clip("a", "1", created=1000.0, duration=30.0)
    a, b = crowd.live_span(c, delay=6)
    assert (a, b) == (964.0, 994.0)


def test_velocity():
    assert crowd.velocity([(0, 0)]) == 0.0
    assert crowd.velocity([(0, 100), (120, 400)]) == 150.0


def test_audio_cross_correlation_finds_the_offset():
    rng = np.random.default_rng(3)
    rate = 8000
    full = rng.standard_normal(rate * 120).astype(np.float32) * np.repeat(rng.uniform(0.05, 1.0, 120 * 10), rate // 10)
    start = 47.3
    piece = full[int(start * rate): int((start + 25) * rate)]
    i, corr = crowd.best_offset(crowd.envelope(piece, rate), crowd.envelope(full, rate))
    assert i is not None and corr > 0.9
    assert abs(i / 10 - start) <= 0.1


def test_alignment_refuses_unrelated_audio():
    rng = np.random.default_rng(4)
    full = rng.standard_normal(8000 * 60).astype(np.float32) * np.repeat(rng.uniform(0.05, 1, 600), 800)
    other = rng.standard_normal(8000 * 20).astype(np.float32) * np.repeat(rng.uniform(0.05, 1, 200), 800)
    i, corr = crowd.best_offset(crowd.envelope(other), crowd.envelope(full))
    assert i is None and corr < 0.6


def test_queue_takes_best_ready_moment_first():
    q = MomentQueue()
    now = time.time()
    weak = types.SimpleNamespace(priority=1.0, ready_at=now - 1)
    strong = types.SimpleNamespace(priority=6.0, ready_at=now - 1)
    later = types.SimpleNamespace(priority=99.0, ready_at=now + 3600)     # best, but not recorded yet
    for c in (weak, later, strong):
        q.put(c)
    assert q.get() is strong
    assert q.get() is weak
    assert q.get() is later


def test_queue_fifo_when_best_first_off():
    q = MomentQueue(best_first=False)
    a, b = types.SimpleNamespace(priority=1), types.SimpleNamespace(priority=9)
    q.put(a)
    q.put(b)
    assert q.get() is a


def test_combined_score_rewards_agreeing_signals_and_clips():
    one = types.SimpleNamespace(source="chat spike: 4.0x faster than normal", clips=[], signals={})
    two = types.SimpleNamespace(source="chat spike: 4.0x faster than normal + streamer reaction: laughing",
                                clips=[], signals={})
    crowdy = types.SimpleNamespace(source="chat spike: 4.0x faster than normal", signals={},
                                   clips=[_clip(str(i), str(i)) for i in range(4)])
    assert scoring.priority(two) > scoring.priority(one) + 2      # second signal + agreement bonus
    assert scoring.priority(crowdy) > scoring.priority(two)
    assert "chat_spike" in scoring.describe(one)


# ---------------------------------------------------------------------------- live session with fake parts
def _session(tmp_path):
    from frenzo.config import DEFAULTS, _merge
    from frenzo.engine import Session
    cfg = _merge(DEFAULTS, {"storage": {"recordings_dir": str(tmp_path), "output_dir": str(tmp_path)}})
    info = types.SimpleNamespace(login="kai", display="Kai", url="u", platform="twitch", title="GTA 6", game="GTA",
                                 user_id="555")
    scfg = {"login": "kai", "key": "twitch:kai", "platform": "twitch", "tags": []}
    q = MomentQueue()
    s = Session(scfg, info, object(), cfg, q)
    return s, q


def test_clip_creates_moment_then_overlapping_clip_attaches(tmp_path):
    s, q = _session(tmp_path)
    part = types.SimpleNamespace(path=str(tmp_path / "rec.ts"), t0=time.time() - 600)
    s.recorder.t0, s.recorder.path = part.t0, part.path
    now = time.time()
    c1 = _clip("a", "1", now - 90, now - 60, created=now - 50)
    s.add_clip(c1, part)
    assert q.qsize() == 1
    cand = q.waiting()[0]
    assert cand.clip_span == (c1.span[0] - part.t0, c1.span[1] - part.t0)
    assert cand.source.startswith("viewer clip")
    for i, creator in enumerate(("2", "3")):
        s.add_clip(_clip(f"b{i}", creator, now - 85, now - 58, created=now - 40), part)
    assert q.qsize() == 1                       # same moment: no new one
    assert len(cand.clips) == 3 and cand.keep_bonus == 1
    assert cand.priority > 4


def test_clip_far_away_makes_a_second_moment(tmp_path):
    s, q = _session(tmp_path)
    part = types.SimpleNamespace(path=str(tmp_path / "rec.ts"), t0=time.time() - 900)
    s.recorder.t0, s.recorder.path = part.t0, part.path
    now = time.time()
    s.add_clip(_clip("a", "1", now - 400, now - 370, created=now - 360), part)
    s.add_clip(_clip("b", "2", now - 90, now - 60, created=now - 50), part)
    assert q.qsize() == 2


def test_detector_moment_gets_clip_evidence_and_rejected_moment_is_judged_again(tmp_path):
    s, q = _session(tmp_path)
    rec = tmp_path / "rec.ts"
    rec.write_bytes(b"x")
    part = types.SimpleNamespace(path=str(rec), t0=time.time() - 600)
    s.recorder.t0, s.recorder.path = part.t0, part.path
    now = time.time()
    s._queue(now - 70, "chat spike: 5.0x faster than normal")
    cand = q.get()
    cand.started, cand.done, cand.status, cand.judged = True, True, "rejected", {"viral_score": 5}
    s.add_clip(_clip("a", "1", now - 95, now - 66, created=now - 60), part)
    assert len(cand.clips) == 1
    assert q.qsize() == 1                       # judged again with the clip evidence
    again = q.get()
    assert again is not cand and again.clips and "judged again" in again.source


# ---- run with unittest like the other tests:  .venv\Scripts\python -m unittest tests.test_crowd
import inspect as _inspect
import pathlib as _pathlib
import tempfile as _tempfile
import unittest as _unittest


class Crowd(_unittest.TestCase):
    pass


def _wrap(fn):
    def method(self):
        if "tmp_path" in _inspect.signature(fn).parameters:
            with _tempfile.TemporaryDirectory() as d:
                fn(_pathlib.Path(d))
        else:
            fn()
    return method


for _name, _fn in list(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        setattr(Crowd, _name, _wrap(_fn))

if __name__ == "__main__":
    _unittest.main()
