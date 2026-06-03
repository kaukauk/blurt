"""
Configuration, path, and environment helpers for blurt.

Settings come from (highest priority first):
  1. BLURT_* environment variables
  2. ~/.config/blurt/config.toml
  3. built-in defaults

Anything machine-specific (paths, compute device, typing backend) is resolved
here so the rest of the code stays portable.
"""

import os
import sys
import shutil
import subprocess

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

APP = "blurt"

# --- paths ------------------------------------------------------------------
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{os.getuid()}"
SOCKET_PATH = os.path.join(RUNTIME_DIR, f"{APP}.sock")

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), APP)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.toml")
UI_STATE_FILE = os.path.join(CONFIG_DIR, "ui.json")
TIMING_FILE = os.path.join(CONFIG_DIR, "timing.json")

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), APP)
LOG_FILE = os.path.join(CACHE_DIR, "blurt.log")


def _load_file():
    if tomllib is None:
        return {}
    try:
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[blurt] bad config.toml ({e}); using defaults", file=sys.stderr)
        return {}


_FILE = _load_file()


def _get(section, key, default, env=None, cast=str):
    """Resolve a setting: env var > config.toml > default."""
    if env and env in os.environ:
        try:
            return cast(os.environ[env])
        except ValueError:
            pass
    val = _FILE.get(section, {}).get(key)
    if val is not None:
        return val
    return default


def _as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


# --- model / transcription --------------------------------------------------
# "auto" picks a model by device (see resolve_model): the big, accurate model on
# a CUDA GPU, a smaller/faster one on CPU. Or set an explicit faster-whisper
# model (e.g. "large-v3-turbo", "small.en") to force it on both.
MODEL_NAME = _get("model", "name", "auto", "BLURT_MODEL")
# Models used when name = "auto". large-v3-turbo is great on GPU but ~2.5x slower
# than realtime on CPU, so CPU falls back to a much faster English model.
GPU_MODEL = _get("model", "gpu_name", "large-v3-turbo", "BLURT_GPU_MODEL")
CPU_MODEL = _get("model", "cpu_name", "small.en", "BLURT_CPU_MODEL")
_DEVICE = _get("model", "device", "auto", "BLURT_DEVICE")
_COMPUTE = _get("model", "compute", "auto", "BLURT_COMPUTE")
LANGUAGE = _get("model", "language", "en", "BLURT_LANG")
PROMPT = _get(
    "model", "prompt",
    "Hello. Here is the dictation, written with correct punctuation, "
    "capitalization, commas, periods, and question marks?", "BLURT_PROMPT")
BEAM_SIZE = int(_get("model", "beam_size", 5, "BLURT_BEAM", int))

SAMPLE_RATE = 16000
CHANNELS = 1
MIN_SECONDS = float(_get("model", "min_seconds", 0.3, "BLURT_MIN_SECONDS", float))

# --- input ------------------------------------------------------------------
# mode: "toggle" (press to start, press/stop-key to stop) or "hold" (push-to-talk)
MODE = str(_get("input", "mode", "toggle", "BLURT_MODE")).lower()
# Mouse button that toggles plain dictation: start, or stop + transcribe + type
# (no Enter). Default 8 = mouse "back"; 0 disables.
TOGGLE_BUTTON = int(_get("input", "toggle_button", 8, "BLURT_TOGGLE_BUTTON", int))
# Mouse button that toggles "submit" dictation: start, or stop + transcribe +
# type, then press Enter. Default 9 = mouse "forward"; 0 disables.
SUBMIT_BUTTON = int(_get("input", "submit_button", 9, "BLURT_SUBMIT_BUTTON", int))
# Optional global trigger key the daemon grabs directly, e.g. "ctrl+grave".
# Empty by default: most desktops reserve common combos (Alt+Space is the XFCE
# window menu), so binding `blurt toggle` to a key in your DE is more reliable.
# Setting this is required for "hold" mode on a key (a DE binding can't do hold).
TRIGGER_KEY = _get("input", "key", "", "BLURT_KEY")
# Key that stops while recording in toggle mode (no modifiers). "" disables.
STOP_KEY = _get("input", "stop_key", "space", "BLURT_STOP_KEY")
# Key that stops + transcribes + presses Enter ("submit") while recording in
# toggle mode. Default "enter"; "" disables.
SUBMIT_KEY = _get("input", "submit_key", "enter", "BLURT_SUBMIT_KEY")
# Key that cancels + discards the recording (no transcription, nothing typed)
# while recording in toggle mode. Default "alt+backspace" (Alt+Delete is
# reserved by some desktops, e.g. XFCE); "" disables.
CANCEL_KEY = _get("input", "cancel_key", "alt+backspace", "BLURT_CANCEL_KEY")
# Seconds to wait after typing before pressing Enter (lets the app catch up).
SUBMIT_DELAY = float(_get("input", "submit_delay", 0.6, "BLURT_SUBMIT_DELAY", float))

# --- ui / features ----------------------------------------------------------
SHOW_UI = _as_bool(_get("ui", "enabled", True, "BLURT_UI"))
ENABLE_VAD = _as_bool(_get("ui", "vad", True, "BLURT_VAD"))
UI_PROGRESS = _as_bool(_get("ui", "progress", True, "BLURT_PROGRESS"))
# Desktop notifications are off by default — the overlay already shows state.
NOTIFY = _as_bool(_get("ui", "notifications", False, "BLURT_NOTIFY"))
# Also copy each transcription to the clipboard (so it's not lost if no field
# is focused).
CLIPBOARD = _as_bool(_get("output", "clipboard", True, "BLURT_CLIPBOARD"))
VAD_WINDOW = 1536

# --- error reporting --------------------------------------------------------
REPO_URL = _get("report", "repo", "https://github.com/kaukauk/blurt", "BLURT_REPO")
# Show a GUI pop-up on error with a one-click "Report on GitHub" button.
ERROR_POPUP = _as_bool(_get("report", "popup", True, "BLURT_ERROR_POPUP"))

UI_PYTHON = os.environ.get("BLURT_UI_PYTHON", sys.executable)


# --- session / backends -----------------------------------------------------
def session_type():
    return os.environ.get("XDG_SESSION_TYPE", "").lower()


def is_x11():
    return session_type() == "x11" or (
        "DISPLAY" in os.environ and "WAYLAND_DISPLAY" not in os.environ)


def resolve_device():
    """Return (device, compute_type), auto-detecting CUDA when not forced."""
    device, compute = _DEVICE, _COMPUTE
    if device == "auto":
        device = "cpu"
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                device = "cuda"
        except Exception:
            pass
    if compute == "auto":
        compute = "int8_float16" if device == "cuda" else "int8"
    return device, compute


def resolve_model(device):
    """Model to load: honour an explicit name, else pick one for the device."""
    if str(MODEL_NAME).lower() != "auto":
        return MODEL_NAME
    return GPU_MODEL if device == "cuda" else CPU_MODEL


def _have(cmd):
    return shutil.which(cmd) is not None


def pick_typer():
    forced = os.environ.get("BLURT_TYPER") or _FILE.get("input", {}).get("typer")
    order = ([forced] if forced else
             (["xdotool", "wtype", "ydotool"] if is_x11()
              else ["wtype", "ydotool", "xdotool"]))
    for tool in order:
        if tool and _have(tool):
            return tool
    return None


TYPER = pick_typer()


def type_text(text):
    if not text:
        return
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


def press_enter():
    """Press the Enter/Return key in the focused window (best-effort)."""
    if not TYPER:
        return
    try:
        if TYPER == "xdotool":
            cmd = ["xdotool", "key", "--clearmodifiers", "Return"]
        elif TYPER == "wtype":
            cmd = ["wtype", "-k", "Return"]
        elif TYPER == "ydotool":
            cmd = ["ydotool", "key", "28:1", "28:0"]  # 28 = KEY_ENTER
        else:
            return
        subprocess.run(cmd, check=False)
    except Exception as e:
        print(f"[blurt] press Enter via {TYPER} failed: {e}", file=sys.stderr)


def notify(title, body="", icon="audio-input-microphone"):
    if not NOTIFY:
        return
    try:
        subprocess.Popen(
            ["notify-send", "-a", APP, "-i", icon, "-t", "1500", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _pick_clipboard():
    if is_x11():
        order = ["xclip", "xsel", "wl-copy"]
    else:
        order = ["wl-copy", "xclip", "xsel"]
    for tool in order:
        if _have(tool):
            return tool
    return None


CLIP_TOOL = _pick_clipboard()


def copy_clipboard(text):
    """Put text on the system clipboard (best-effort)."""
    if not (CLIPBOARD and text and CLIP_TOOL):
        return
    cmd = {"xclip": ["xclip", "-selection", "clipboard"],
           "xsel": ["xsel", "--clipboard", "--input"],
           "wl-copy": ["wl-copy"]}[CLIP_TOOL]
    try:
        subprocess.run(cmd, input=text.encode(), check=False)
    except Exception as e:
        print(f"[blurt] clipboard via {CLIP_TOOL} failed: {e}", file=sys.stderr)


DEFAULT_CONFIG = """\
# blurt configuration. Environment variables (BLURT_*) override these.

[model]
name = "auto"             # "auto" picks by device (below); or force one, e.g. "large-v3-turbo", "small.en"
gpu_name = "large-v3-turbo"  # model used when a CUDA GPU is available (accurate)
cpu_name = "small.en"        # model used on CPU (much faster than large-v3 there)
device = "auto"           # "auto" | "cuda" | "cpu"
compute = "auto"          # "auto" | "int8" | "int8_float16" | "float16" | "float32"
language = "en"
beam_size = 5

[input]
mode = "toggle"           # "toggle" (press start / press stop) or "hold" (push-to-talk)
toggle_button = 8         # mouse button: start, or stop + transcribe (no Enter); 0 disables
submit_button = 9         # mouse button: start, or stop + transcribe, then Enter; 0 disables
stop_key = "space"        # key that stops while recording in toggle mode; "" to disable
submit_key = "enter"      # while recording: stop, transcribe, then press Enter; "" to disable
cancel_key = "alt+backspace" # while recording: cancel + discard (nothing typed); "" to disable
submit_delay = 0.6        # seconds to wait after typing before pressing Enter
# key = "ctrl+grave"      # optional: have blurt grab a key directly. Most desktops
#                         # reserve combos like Alt+Space, so prefer binding
#                         # `blurt toggle` to a hotkey in your DE. Required for
#                         # "hold" mode on a key (a DE binding can't do hold).
# typer = "xdotool"       # force a typing backend: xdotool | wtype | ydotool

[ui]
enabled = true            # show the waveform overlay
vad = true                # only react to voice (Silero VAD), not music
progress = true           # keep the window up during transcription with a progress bar
notifications = false     # desktop popups; off because the overlay shows state

[output]
clipboard = true          # also copy each transcription to the clipboard

[report]
popup = true              # on error, show a pop-up with a one-click GitHub report
# repo = "https://github.com/kaukauk/blurt"   # where reports are filed
"""


def write_default_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        return False
    with open(CONFIG_FILE, "w") as f:
        f.write(DEFAULT_CONFIG)
    return True
