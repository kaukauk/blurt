"""
One-command GPU acceleration for blurt.

The packaged `python-ctranslate2` is built CPU-only, so an AUR install
transcribes on CPU even on an NVIDIA box. `blurt gpu` sets up a private venv
with the PyPI `ctranslate2` wheel (which is built with CUDA and pulls the
bundled cuBLAS/cuDNN libraries), installs blurt's runtime deps into it, and
writes a systemd **user drop-in** so the daemon runs under that venv with the
big model. The GTK overlay/dialogs keep running on the system Python (they need
`gi`). `blurt gpu --disable` removes the drop-in and reverts to the packaged
CPU setup.
"""

import os
import sys
import glob
import shutil
import subprocess

GPU_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "blurt", "gpu")
VENV_PY = os.path.join(GPU_DIR, "bin", "python")
DROPIN_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "systemd", "user", "blurt.service.d")
DROPIN = os.path.join(DROPIN_DIR, "gpu.conf")

# Daemon runtime deps for the venv. The overlay/dialogs run on system Python,
# so gi/cairo/gtk are intentionally absent here. ctranslate2 4.x needs the CUDA
# 12 cuBLAS + cuDNN runtime libraries, which it does NOT pull automatically — we
# install the matching nvidia-*-cu12 wheels and point LD_LIBRARY_PATH at them.
PKGS = ["ctranslate2", "faster-whisper", "sounddevice", "numpy", "python-xlib",
        "nvidia-cublas-cu12", "nvidia-cudnn-cu12"]


def _pkg_parent():
    """Directory that contains the `blurt` package (so the venv can import it)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ui_python():
    """A system Python that can import gi, for the overlay/error dialog."""
    for p in ("/usr/bin/python3", "/usr/bin/python"):
        if os.path.exists(p) and subprocess.run(
                [p, "-c", "import gi"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL).returncode == 0:
            return p
    return sys.executable


def _ld_library_path():
    """Colon-joined paths to the venv's bundled NVIDIA .so dirs."""
    libs = glob.glob(os.path.join(
        GPU_DIR, "lib", "python*", "site-packages", "nvidia", "*", "lib"))
    return os.pathsep.join(sorted(libs))


_GPU_TEST = (
    "import numpy as np\n"
    "from faster_whisper import WhisperModel\n"
    "m = WhisperModel('tiny', device='cuda', compute_type='int8_float16')\n"
    "list(m.transcribe(np.zeros(16000, dtype='float32'), language='en')[0])\n"
    "print('GPU_OK')\n")


def _gpu_works(ld):
    """Actually load a model on CUDA and encode — the path that needs cuBLAS.

    Returns (ok, message). A bare get_cuda_device_count() is not enough: it
    passes even when libcublas is missing, which then fails at encode time.
    """
    env = dict(os.environ)
    if ld:
        env["LD_LIBRARY_PATH"] = ld + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    env["PYTHONPATH"] = _pkg_parent() + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([VENV_PY, "-c", _GPU_TEST],
                       capture_output=True, text=True, env=env)
    if "GPU_OK" in r.stdout:
        return True, ""
    tail = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
    return False, tail


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args]).returncode == 0


def enable():
    py = sys.executable
    print(f"[blurt] creating GPU venv at {GPU_DIR}", flush=True)
    os.makedirs(os.path.dirname(GPU_DIR), exist_ok=True)
    if not os.path.isfile(VENV_PY):
        subprocess.run([py, "-m", "venv", GPU_DIR], check=True)
    pip = os.path.join(GPU_DIR, "bin", "pip")
    print("[blurt] installing CUDA ctranslate2 + faster-whisper "
          "(downloads ~1 GB, can take a few minutes)…", flush=True)
    subprocess.run([pip, "install", "--upgrade", "pip", "wheel"], check=True)
    subprocess.run([pip, "install", "--upgrade", *PKGS], check=True)

    ld = _ld_library_path()
    print("[blurt] verifying GPU transcription works…", flush=True)
    ok, msg = _gpu_works(ld)
    if not ok:
        print(f"[blurt] ERROR: GPU test failed ({msg}).\n"
              f"        Your existing setup is unchanged. The venv is at "
              f"{GPU_DIR}\n        (remove with `blurt gpu --disable --purge`).",
              file=sys.stderr)
        return 1
    print("[blurt] GPU transcription verified.", flush=True)

    os.makedirs(DROPIN_DIR, exist_ok=True)
    with open(DROPIN, "w") as f:
        f.write(
            "# Written by `blurt gpu`. Remove with `blurt gpu --disable`.\n"
            "[Service]\n"
            "ExecStart=\n"
            f"ExecStart={VENV_PY} -m blurt daemon\n"
            f"Environment=PYTHONPATH={_pkg_parent()}\n"
            f"Environment=BLURT_UI_PYTHON={_ui_python()}\n"
            "Environment=BLURT_MODEL=large-v3-turbo\n"
            f"Environment=LD_LIBRARY_PATH={ld}\n")
    print(f"[blurt] wrote systemd drop-in {DROPIN}", flush=True)

    _systemctl("daemon-reload")
    if _systemctl("restart", "blurt.service"):
        print("[blurt] GPU enabled — daemon restarted on large-v3-turbo (CUDA).")
    else:
        print("[blurt] GPU configured. Restart the daemon to apply: "
              "`systemctl --user restart blurt.service`")
    return 0


def disable(purge=False):
    if os.path.exists(DROPIN):
        os.remove(DROPIN)
        print(f"[blurt] removed {DROPIN}")
    else:
        print("[blurt] GPU drop-in was not present.")
    if purge and os.path.isdir(GPU_DIR):
        shutil.rmtree(GPU_DIR, ignore_errors=True)
        print(f"[blurt] deleted venv {GPU_DIR}")
    _systemctl("daemon-reload")
    if _systemctl("restart", "blurt.service"):
        print("[blurt] reverted to the packaged (CPU) setup.")
    else:
        print("[blurt] drop-in removed. Restart the daemon to apply: "
              "`systemctl --user restart blurt.service`")
    return 0


def main(argv):
    if argv and argv[0] in ("--disable", "disable", "off"):
        return disable(purge=("--purge" in argv))
    return enable()
