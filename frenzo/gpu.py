"""Using the NVIDIA GPU next to the CPU.

- Speech-to-text (faster-whisper) runs on the GPU when NVIDIA's CUDA 12 libraries are there. They are installed
  inside Frenzo's own Python environment (pip packages nvidia-cublas-cu12 + nvidia-cudnn-cu12), so this module adds
  their folders to the DLL search path of every Frenzo process. Without them, everything stays on the CPU.
- Video encoding uses the GPU's NVENC encoder (h264_nvenc) when ffmpeg has it and it works; otherwise libx264 (CPU).
  config.yaml  render: encoder: auto | gpu | cpu   (auto = NVENC when it works)
"""
import glob
import os
import site
import subprocess
import sys
import sysconfig

_state = {}


def _site_dirs():
    dirs = set()
    try:
        dirs.update(site.getsitepackages())
    except Exception:
        pass
    for key in ("purelib", "platlib"):
        try:
            dirs.add(sysconfig.get_paths()[key])
        except Exception:
            pass
    dirs.add(os.path.join(sys.prefix, "Lib", "site-packages"))
    return [d for d in dirs if d and os.path.isdir(d)]


def add_cuda_dlls():
    """Make the CUDA 12 DLLs from the nvidia-* pip packages findable (Windows). Safe to call many times."""
    if "dlls" in _state:
        return _state["dlls"]
    found = []
    if os.name == "nt":
        for sp in _site_dirs():
            found += [d for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")) if os.path.isdir(d)]
        for d in found:
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
        if found:
            os.environ["PATH"] = os.pathsep.join(found) + os.pathsep + os.environ.get("PATH", "")
    _state["dlls"] = found
    return found


def nvenc_ok():
    """True if ffmpeg can really encode with NVENC on this PC (checked once per process)."""
    if "nvenc" not in _state:
        ok = False
        try:
            from .util import NO_WINDOW
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                "color=black:s=320x240:d=0.3", "-c:v", "h264_nvenc", "-f", "null", "-"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30, creationflags=NO_WINDOW)
            ok = r.returncode == 0
        except Exception:
            ok = False
        _state["nvenc"] = ok
    return _state["nvenc"]


def use_gpu_encoder(rcfg):
    mode = str(rcfg.get("encoder") or "auto").lower()
    return mode != "cpu" and nvenc_ok()


def video_args(rcfg, gpu, crf=None, preset=None):
    """ffmpeg video encoder options: NVENC (GPU) or libx264 (CPU), same quality target."""
    crf = int(rcfg.get("crf", 18) if crf is None else crf)
    if gpu:
        # constant-quality VBR, tuned for quality (the platforms re-compress, so the upload must be clean):
        # cq = crf + 5, slower preset p6, 2-pass inside a frame (qres), 32-frame look-ahead, spatial + temporal AQ,
        # B-frames as references. Measured on this PC (RTX 4060, 15 s of 1080x1920 60 fps): SSIM 0.9972 vs 0.9960 for
        # the old p4 / cq crf+9, 15.5 vs 10 Mbps, 5 s vs 3 s to encode. render.nvenc_cq_offset changes the +5.
        off = int(rcfg.get("nvenc_cq_offset", 5))
        return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-multipass", "qres", "-rc", "vbr",
                "-cq", str(crf + off), "-b:v", "0", "-maxrate", "40M", "-bufsize", "80M", "-rc-lookahead", "32",
                "-spatial-aq", "1", "-temporal-aq", "1", "-aq-strength", "8", "-bf", "3", "-b_ref_mode", "middle",
                "-profile:v", "high"]
    return ["-c:v", "libx264", "-preset", preset or rcfg.get("preset", "veryfast"), "-crf", str(crf),
            "-profile:v", "high", "-x264-params", "aq-mode=3"]
