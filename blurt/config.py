"""
Configuration, path, and environment helpers for blurt.

Everything that varies between machines is resolved here: where the socket and
config live, which compute device to use, and how to type text into the focused
window (X11 vs Wayland). All values can be overridden with BLURT_* env vars.
"""

import os
import sys
import shutil
import subprocess

APP = "blurt"

# --- paths ------------------------------------------------------------------
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{os.getuid()}"
SOCKET_PATH = os.path.join(RUNTIME_DIR, f"{APP}.sock")

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), APP)
UI_STATE_FILE = os.path.join(CONFIG_DIR, "ui.json")

# --- model / transcription --------------------------------------------------
MODEL_NAME = os.environ.get("BLURT_MODEL", "large-v3-turbo")
LANGUAGE = os.environ.get("BLURT_LANG", "en")
# Bias toward correct punctuation/capitalization (context only; never typed out).
PROMPT = os.environ.get(
    "BLURT_PROMPT",
    "Hello. Here is the dictation, written with correct punctuation, "
    "capitalization, commas, periods, and question marks?")

SAMPLE_RATE = 16000
CHANNELS = 1
MIN_SECONDS = 0.3

# --- features ---------------------------------------------------------------
SHOW_UI = os.environ.get("BLURT_UI", "1") != "0"
ENABLE_VAD = os.environ.get("BLURT_VAD", "1") != "0"
VAD_WINDOW = 1536  # samples (multiple of 512) fed to Silero each tick
# Mouse button that toggles recording (9 = forward on most mice). 0 disables.
MOUSE_BUTTON = int(os.environ.get("BLURT_MOUSE_BUTTON", "9"))

# Python used to launch the GTK overlay (must have python-gobject/cairo). The
# daemon's own interpreter normally has them too; override only for odd setups.
UI_PYTHON = os.environ.get("BLURT_UI_PYTHON", sys.executable)


def session_type():
    return os.environ.get("XDG_SESSION_TYPE", "").lower()


def is_x11():
    return session_type() == "x11" or (
        "DISPLAY" in os.environ and "WAYLAND_DISPLAY" not in os.environ)


def resolve_device():
    """Return (device, compute_type), auto-detecting CUDA when not forced."""
    device = os.environ.get("BLURT_DEVICE", "auto")
    compute = os.environ.get("BLURT_COMPUTE", "auto")
    if device == "auto":
        device = "cpu"
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                device = "cuda"
        except Exception:
            pass
    if compute == "auto":
        # int8 is fast and accurate on both CPU and (Turing+) GPUs; on GPU we
        # keep sensitive ops in fp16 for a touch more precision.
        compute = "int8_float16" if device == "cuda" else "int8"
    return device, compute


# --- typing backend (X11 / Wayland) -----------------------------------------
def _have(cmd):
    return shutil.which(cmd) is not None


def pick_typer():
    """Choose how to type text into the focused window for this session."""
    forced = os.environ.get("BLURT_TYPER")
    order = ([forced] if forced else
             (["xdotool", "wtype", "ydotool"] if is_x11()
              else ["wtype", "ydotool", "xdotool"]))
    for tool in order:
        if tool and _have(tool):
            return tool
    return None


TYPER = pick_typer()


def type_text(text):
    """Type `text` into whatever window currently has focus."""
    if not text or not TYPER:
        if not TYPER:
            print("[blurt] no typing tool found (install xdotool or wtype)",
                  file=sys.stderr)
        return
    try:
        if TYPER == "xdotool":
            cmd = ["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", text]
        elif TYPER == "wtype":
            cmd = ["wtype", text]
        elif TYPER == "ydotool":
            cmd = ["ydotool", "type", text]
        else:
            return
        subprocess.run(cmd, check=False)
    except Exception as e:
        print(f"[blurt] typing via {TYPER} failed: {e}", file=sys.stderr)


def notify(title, body="", icon="audio-input-microphone"):
    try:
        subprocess.Popen(
            ["notify-send", "-a", APP, "-i", icon, "-t", "1500", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
