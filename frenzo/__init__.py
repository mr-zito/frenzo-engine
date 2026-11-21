"""Frenzo clipping engine: records live streams, finds hype moments and renders vertical clips."""
__version__ = "0.1.0"

# NVIDIA CUDA 12 libraries installed with pip (nvidia-cublas-cu12, nvidia-cudnn-cu12): make them findable so
# speech-to-text can run on the GPU (nothing happens if they are not installed)
from .gpu import add_cuda_dlls as _add_cuda_dlls
_add_cuda_dlls()

# YouTube downloads need a JavaScript runtime (Deno, pip package "deno") to decode video links: it is installed
# next to this Python, so put that folder on PATH for the downloader (and the processes it starts)
import os as _os, sys as _sys
_scripts = _os.path.dirname(_sys.executable)
_deno = _os.path.join(_scripts, "deno.exe" if _os.name == "nt" else "deno")
if _os.path.exists(_deno) and _scripts not in _os.environ.get("PATH", "").split(_os.pathsep):
    _os.environ["PATH"] = _scripts + _os.pathsep + _os.environ.get("PATH", "")
