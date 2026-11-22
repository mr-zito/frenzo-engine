"""Audio, transcript and frame extraction for a candidate moment."""
import threading

import numpy as np

from .util import log, run

RATE = 16000


def extract_audio(rec, start, dur, rate=RATE):
    raw = run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
         "-i", rec, "-vn", "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", str(rate),
         "-f", "s16le", "-"],
        capture=True,
    )
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def loudness_per_second(audio, rate=RATE):
    n = len(audio) // rate
    if n == 0:
        return []
    a = audio[: n * rate].reshape(n, rate)
    rms = np.sqrt((a ** 2).mean(axis=1)) + 1e-9
    return [round(float(x), 1) for x in 20 * np.log10(rms)]


def grab_frames(rec, start, dur, fractions=(0.1, 0.3, 0.5, 0.65, 0.78, 0.9), width=640, times=None):
    """Frames at fractions of the window, or at exact window-relative seconds if 'times' is given."""
    if times is not None:
        fractions = [t / dur for t in times if 0 <= t <= dur]
    frames = []
    for frac in fractions:
        t = start + dur * frac
        try:
            jpg = run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", rec,
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5",
                 "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                capture=True,
            )
        except RuntimeError:
            continue
        if jpg:
            frames.append((round(dur * frac, 1), jpg))
    return frames


def _cuda_libs_missing():
    """Windows: an NVIDIA GPU can be present without the CUDA libraries (cuBLAS). Using it then can hang."""
    import os
    if os.name != "nt":
        return False
    import ctypes
    try:
        ctypes.WinDLL("cublas64_12.dll")
        return False
    except OSError:
        return True


# lines Whisper is known to invent on silence or music (never real streamer speech in a GTA clip)
HALLUCINATIONS = ("thanks for watching", "thank you for watching", "please subscribe", "subscribe to my channel",
                  "amara.org", "subtitles by", "transcribed by", "see you in the next video", "like and subscribe")


class Transcriber:
    """Speech-to-text with word timings (faster-whisper). Loaded once, shared by workers."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.model = None
        self.lock = threading.Lock()
        self.available = True

    def _load(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            log("faster-whisper is not installed: clips will have no captions", "whisper")
            self.available = False
            return
        device = self.cfg.get("device", "auto")
        auto = device == "auto"
        if auto:
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"
            if device == "cuda" and _cuda_libs_missing():
                device = "cpu"   # trying CUDA without its libraries can hang the worker: don't even try
        compute = "float16" if device == "cuda" else "int8"
        log(f"loading speech model '{self.cfg['model']}' on {device} (first time downloads it)", "whisper")
        try:
            self.model = WhisperModel(self.cfg["model"], device=device, compute_type=compute)
            if auto and device == "cuda":
                # a GPU can be present without cuBLAS/cuDNN; that only fails on first use, so test it now
                segs, _ = self.model.transcribe(np.zeros(RATE, dtype=np.float32), language="en", beam_size=1)
                list(segs)
        except Exception as e:
            if not (auto and device == "cuda"):
                raise
            log(f"GPU not usable ({str(e)[:100]}): using the CPU instead", "whisper")
            self.model = WhisperModel(self.cfg["model"], device="cpu", compute_type="int8")

    def transcribe(self, audio):
        with self.lock:
            if self.model is None and self.available:
                self._load()
            if not self.available or len(audio) < RATE:
                return [], None
            # no voice-activity filter by default: it threw away a streamer YELLING over loud game music (0 words
            # in 39 s of Kai). Instead, segments that look like silence hallucinations are dropped below.
            segments, info = self.model.transcribe(
                audio,
                language=self.cfg.get("language"),
                word_timestamps=True,
                vad_filter=bool(self.cfg.get("vad", False)),
                beam_size=int(self.cfg.get("beam_size", 5)),
                condition_on_previous_text=False,
            )
            words = []
            for seg in segments:
                text = (seg.text or "").strip().lower()
                if (seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.7) or any(
                        h in text for h in HALLUCINATIONS):
                    continue                             # whisper "hearing" words in silence / music
                for w in seg.words or []:
                    text = w.word.strip()
                    if text:
                        words.append({"start": float(w.start), "end": float(w.end), "word": text})
            return words, getattr(info, "language", None)


def transcript_lines(words, gap=0.8):
    """Group words into timestamped lines for the AI prompt."""
    lines, cur, cur_start, last_end = [], [], None, None
    for w in words:
        if cur and (w["start"] - last_end > gap or len(cur) >= 14):
            lines.append(f"[{cur_start:5.1f}s] {' '.join(cur)}")
            cur = []
        if not cur:
            cur_start = w["start"]
        cur.append(w["word"])
        last_end = w["end"]
    if cur:
        lines.append(f"[{cur_start:5.1f}s] {' '.join(cur)}")
    return lines
