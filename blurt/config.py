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
import time
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
LANGUAGE = _get("model", "language", "auto", "BLURT_LANG")


# Input-method engine name (fragment) → Whisper language code. Matched as a
# substring of the lowercased IM name, so e.g. "libpinyin"/"table:cangjie" hit.
_IM_LANG = {
    "hangul": "ko", "korean": "ko", "hanja": "ko",
    "mozc": "ja", "anthy": "ja", "kkc": "ja", "skk": "ja", "japanese": "ja",
    "pinyin": "zh", "shuangpin": "zh", "wubi": "zh", "wbpy": "zh", "wbx": "zh",
    "rime": "zh", "cangjie": "zh", "zhuyin": "zh", "chewing": "zh",
    "chinese": "zh", "sunpinyin": "zh", "erbi": "zh",
    "unikey": "vi", "viqr": "vi", "vietnamese": "vi",
    "thai": "th", "arabic": "ar", "hebrew": "he", "russian": "ru",
}
# XKB layout code → Whisper language. The code is the XX in fcitx5 "keyboard-XX",
# ibus "xkb:XX::…", or a bare layout symbol like "us"/"de(nodeadkeys)"/"us,ru".
_LAYOUT_LANG = {
    "us": "en", "gb": "en", "uk": "en", "ie": "en", "au": "en", "nz": "en",
    "za": "en", "ng": "en", "ph": "en", "ca": "en", "in": "en",
    "fr": "fr", "be": "fr", "ch": "de", "de": "de", "at": "de",
    "es": "es", "latam": "es", "it": "it", "pt": "pt", "br": "pt",
    "ru": "ru", "ua": "uk", "by": "be", "pl": "pl", "cz": "cs", "sk": "sk",
    "nl": "nl", "se": "sv", "no": "no", "dk": "da", "fi": "fi", "is": "is",
    "gr": "el", "tr": "tr", "hu": "hu", "ro": "ro", "bg": "bg", "hr": "hr",
    "rs": "sr", "si": "sl", "ee": "et", "lv": "lv", "lt": "lt", "mk": "mk",
    "al": "sq", "mt": "mt", "jp": "ja", "kr": "ko", "cn": "zh", "tw": "zh",
    "th": "th", "vn": "vi", "ir": "fa", "af": "ps", "il": "he",
    "ara": "ar", "sa": "ar", "eg": "ar", "iq": "ar", "sy": "ar", "ma": "ar",
    "ge": "ka", "am": "hy", "az": "az", "kz": "kk", "uz": "uz",
}


def _layout_to_lang(sym):
    """Map a raw XKB layout symbol (e.g. 'us', 'de(nodeadkeys)', 'us,ru') to a
    Whisper code, or None. Takes the first group and strips any variant."""
    if not sym:
        return None
    code = sym.strip().lower().split(",")[0].split("(")[0].split(":")[0].strip()
    return _LAYOUT_LANG.get(code)


def _im_to_lang(name):
    """Map an input-method name to a Whisper code, or None. Handles IME engines
    (hangul, mozc, libpinyin…) and the layout IMs used by fcitx5/ibus."""
    if not name:
        return None
    n = name.strip().lower()
    if n.startswith("keyboard-"):          # fcitx5 layout IM, e.g. keyboard-de
        return _layout_to_lang(n[len("keyboard-"):])
    if n.startswith("xkb:"):               # ibus layout IM, e.g. xkb:de::ger
        return _layout_to_lang(n[len("xkb:"):])
    for key, lang in _IM_LANG.items():     # engine IM, e.g. hangul, mozc, rime
        if key in n:
            return lang
    return _layout_to_lang(n)              # last resort: a bare layout symbol


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=0.5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _src_fcitx():
    fc = shutil.which("fcitx5-remote") or shutil.which("fcitx-remote")
    return _im_to_lang(_run([fc, "-n"])) if fc else None


def _src_ibus():
    ib = shutil.which("ibus")
    return _im_to_lang(_run([ib, "engine"])) if ib else None


def _src_xkb_tool():
    """Plain XKB layout switchers (no IME) via a small helper, if installed."""
    xs = shutil.which("xkb-switch")
    if xs:
        return _layout_to_lang(_run([xs]))
    xls = shutil.which("xkblayout-state")
    if xls:
        return _layout_to_lang(_run([xls, "print", "%s"]))
    return None


def _src_kde():
    """KDE Plasma's native keyboard-layout switching, via its D-Bus service."""
    qd = shutil.which("qdbus") or shutil.which("qdbus6") or shutil.which("qdbus-qt5")
    if not qd:
        return None
    try:
        idx = int(_run([qd, "org.kde.keyboard", "/Layouts", "getLayout"]).strip())
        codes = [ln.split()[0] for ln in
                 _run([qd, "org.kde.keyboard", "/Layouts", "getLayoutsList"]).splitlines()
                 if ln.strip()]
        return _layout_to_lang(codes[idx]) if 0 <= idx < len(codes) else None
    except (ValueError, IndexError):
        return None


def keyboard_language():
    """Language of the currently active keyboard/input method, or None.

    Works across the common Linux setups — fcitx5, ibus (incl. GNOME's plain
    layout switches as xkb:XX), KDE Plasma layouts, and xkb-switch — so dictation
    comes out in whatever language your keyboard is set to. None → auto-detect.
    """
    for src in (_src_fcitx, _src_ibus, _src_xkb_tool, _src_kde):
        lang = src()
        if lang:
            return lang
    return None


def whisper_language():
    """Language to pass to Whisper, or None to auto-detect each utterance.

    "auto" (or empty) → None: Whisper detects the spoken language and writes it
    back in that same language. "keyboard" → follow the focused window's input
    method (fcitx5/ibus), falling back to auto-detect if it can't be read.
    Otherwise pin a code, e.g. "ko"/"en". Needs a multilingual model
    (large-v3-turbo, small, …); the `.en` models are English-only and ignore this.
    """
    lang = str(LANGUAGE).strip().lower()
    if lang == "keyboard":
        return keyboard_language()   # None → Whisper auto-detects this utterance
    return None if lang in ("", "auto") else lang
# Empty by default: large Whisper models punctuate/capitalize natively, and an
# English prompt biases output toward English. Set this only to bias vocabulary
# (proper nouns, jargon), e.g. "Kaustubh, GameBench, CTranslate2.".
PROMPT = _get("model", "prompt", "", "BLURT_PROMPT")
BEAM_SIZE = int(_get("model", "beam_size", 5, "BLURT_BEAM", int))
# Whisper's internal Silero VAD trims audio to "speech" regions before
# transcribing — it strips silence and cuts end-of-clip hallucinations. On by
# default; set false if it ever drops trailing/soft words.
VAD_FILTER = _as_bool(_get("model", "vad_filter", True, "BLURT_VAD_FILTER"))

SAMPLE_RATE = 16000
CHANNELS = 1
MIN_SECONDS = float(_get("model", "min_seconds", 0.3, "BLURT_MIN_SECONDS", float))

# --- input ------------------------------------------------------------------
# mode: "toggle" (press to start, press/stop-key to stop) or "hold" (push-to-talk)
MODE = str(_get("input", "mode", "toggle", "BLURT_MODE")).lower()
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
# Pause after switching the IME off before typing, so the app has applied it.
IME_SETTLE = float(_get("input", "ime_settle", 0.15, "BLURT_IME_SETTLE", float))
# Pause after setting the clipboard before sending the paste keystroke.
PASTE_SETTLE = float(_get("input", "paste_settle", 0.05, "BLURT_PASTE_SETTLE", float))
# Per-keystroke delay (ms) for the xdotool *type* fallback (non-paste path).
TYPE_DELAY = int(_get("input", "type_delay", 12, "BLURT_TYPE_DELAY", int))

# --- keybinds ---------------------------------------------------------------
# Each action maps to a list of triggers; a trigger is a key spec ("space",
# "alt+backspace", "ctrl+grave") or a mouse button ("button8"). Add as many as
# you like — every one is grabbed (input is consumed). Edit via `blurt settings`.
#   behavior: "start" = begin recording | "plain" = stop + transcribe
#             "submit" = stop + transcribe + Enter | "cancel" = stop + discard
#   scope:    "global" = fires any time | "recording" = only while recording
KEYBIND_SPEC = [
    ("start",  "Start recording",          "start",  "global"),
    ("stop",   "Stop (transcribe)",        "plain",  "recording"),
    ("submit", "Submit (stop + Enter)",    "submit", "recording"),
    ("cancel", "Delete (cancel & discard)", "cancel", "recording"),
]
KEYBIND_ACTIONS = [s[0] for s in KEYBIND_SPEC]


def _norm_triggers(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [str(x).strip() for x in v if str(x).strip()]


def _legacy_keybinds():
    """Build the keybind map from the old scalar settings (back-compat)."""
    kb = {a: [] for a in KEYBIND_ACTIONS}
    if TRIGGER_KEY:
        kb["start"].append(str(TRIGGER_KEY))
    if STOP_KEY:
        kb["stop"].append(str(STOP_KEY))
    if SUBMIT_KEY:
        kb["submit"].append(str(SUBMIT_KEY))
    if CANCEL_KEY:
        kb["cancel"].append(str(CANCEL_KEY))
    return kb


def keybinds():
    """Effective action -> [triggers]. A [keybinds] section overrides per action;
    actions it omits fall back to the legacy scalar settings."""
    kb = _legacy_keybinds()
    sec = _FILE.get("keybinds")
    if isinstance(sec, dict):
        for a in KEYBIND_ACTIONS:
            if a in sec:
                kb[a] = _norm_triggers(sec[a])
    return kb


# --- writing / reloading config ---------------------------------------------
def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _dump_toml(data):
    lines, tables = [], []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{k} = {_toml_value(v)}")
    for name, tbl in tables:
        lines.append("")
        lines.append(f"[{name}]")
        for kk, vv in tbl.items():
            lines.append(f"{kk} = {_toml_value(vv)}")
    return "\n".join(lines) + "\n"


def save_config(updates):
    """Merge `updates` (section -> dict of key->value) into config.toml.

    Round-trips the parsed file so unknown sections/keys are preserved (inline
    comments are not). `blurt settings` is the intended writer.
    """
    data = _load_file()
    for section, vals in updates.items():
        if isinstance(vals, dict):
            cur = data.get(section)
            data[section] = {**(cur if isinstance(cur, dict) else {}), **vals}
        else:
            data[section] = vals
    os.makedirs(CONFIG_DIR, exist_ok=True)
    header = ("# blurt configuration — managed by `blurt settings`.\n"
              "# BLURT_* environment variables still override these.\n\n")
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(header + _dump_toml(data))
    os.replace(tmp, CONFIG_FILE)


def reload():
    """Re-read config.toml and refresh the trigger/runtime globals (not the model)."""
    global _FILE, MODE, SUBMIT_DELAY, LANGUAGE
    global TRIGGER_KEY, STOP_KEY, SUBMIT_KEY, CANCEL_KEY, CLIPBOARD
    _FILE = _load_file()
    MODE = str(_get("input", "mode", "toggle", "BLURT_MODE")).lower()
    LANGUAGE = _get("model", "language", "auto", "BLURT_LANG")
    SUBMIT_DELAY = float(_get("input", "submit_delay", 0.6, "BLURT_SUBMIT_DELAY", float))
    CLIPBOARD = _as_bool(_get("output", "clipboard", True, "BLURT_CLIPBOARD"))
    TRIGGER_KEY = _get("input", "key", "", "BLURT_KEY")
    STOP_KEY = _get("input", "stop_key", "space", "BLURT_STOP_KEY")
    SUBMIT_KEY = _get("input", "submit_key", "enter", "BLURT_SUBMIT_KEY")
    CANCEL_KEY = _get("input", "cancel_key", "alt+backspace", "BLURT_CANCEL_KEY")

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


def _ime_suspend():
    """If an input method is actively composing (e.g. fcitx5 Hangul), turn it
    off so synthetic keystrokes aren't re-composed into jamo. Returns a token
    to pass to _ime_resume(), or None if nothing was changed."""
    fc = shutil.which("fcitx5-remote") or shutil.which("fcitx-remote")
    if not fc:
        return None
    try:
        state = subprocess.run([fc], capture_output=True, text=True,
                               timeout=0.5).stdout.strip()
        if state == "2":                 # 2 = an engine is active/composing
            subprocess.run([fc, "-c"], timeout=0.5)   # deactivate (passthrough)
            time.sleep(IME_SETTLE)       # let fcitx5 tell the app before we type
            print("[blurt] IME suspended for injection", flush=True)
            return fc
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _ime_resume(token):
    if token:
        try:
            subprocess.run([token, "-o"], timeout=0.5)  # reactivate the engine
        except (OSError, subprocess.SubprocessError):
            pass


def type_text(text):
    if not text:
        return
    if not TYPER:
        print("[blurt] no typing tool found (install xdotool or wtype)",
              file=sys.stderr)
        return
    ime = _ime_suspend()
    try:
        # On X11, paste rather than synthesise keystrokes: xdotool reorders/drops
        # non-ASCII (it remaps keycodes per char) and the IME re-composes them.
        if TYPER == "xdotool" and CLIP_TOOL and _inject_paste(text):
            return
        if TYPER == "xdotool":
            cmd = ["xdotool", "type", "--clearmodifiers",
                   "--delay", str(TYPE_DELAY), "--", text]
        elif TYPER == "wtype":
            cmd = ["wtype", text]
        elif TYPER == "ydotool":
            cmd = ["ydotool", "type", text]
        else:
            return
        subprocess.run(cmd, check=False)
    except Exception as e:
        print(f"[blurt] typing via {TYPER} failed: {e}", file=sys.stderr)
    finally:
        _ime_resume(ime)


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


def _clip_set(text):
    """Write text to the clipboard, ignoring the CLIPBOARD preference."""
    if not CLIP_TOOL:
        return False
    cmd = {"xclip": ["xclip", "-selection", "clipboard"],
           "xsel": ["xsel", "--clipboard", "--input"],
           "wl-copy": ["wl-copy"]}[CLIP_TOOL]
    try:
        subprocess.run(cmd, input=text.encode(), check=False)
        return True
    except Exception as e:
        print(f"[blurt] clipboard via {CLIP_TOOL} failed: {e}", file=sys.stderr)
        return False


def _clip_get():
    """Read the current clipboard text (best-effort), or None."""
    if not CLIP_TOOL:
        return None
    cmd = {"xclip": ["xclip", "-selection", "clipboard", "-o"],
           "xsel": ["xsel", "--clipboard", "--output"],
           "wl-copy": ["wl-paste", "-n"]}[CLIP_TOOL]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=0.5)
        return out.stdout.decode(errors="replace")
    except Exception:
        return None


def copy_clipboard(text):
    """Put text on the system clipboard (best-effort), honoring CLIPBOARD."""
    if not (CLIPBOARD and text and CLIP_TOOL):
        return
    _clip_set(text)


_TERMINAL_HINTS = (
    "term", "konsole", "kitty", "alacritty", "xterm", "rxvt", "tilix",
    "terminator", "wezterm", "ghostty", "foot", "yakuake", "guake",
    "st-256color", "cool-retro-term",
)


def _active_window_is_terminal():
    """True if the focused X11 window looks like a terminal (pastes with
    Ctrl+Shift+V rather than Ctrl+V)."""
    try:
        wid = subprocess.run(["xdotool", "getactivewindow"], capture_output=True,
                             text=True, timeout=0.5).stdout.strip()
        if not wid:
            return False
        cls = subprocess.run(["xdotool", "getwindowclassname", wid],
                             capture_output=True, text=True,
                             timeout=0.5).stdout.strip().lower()
        return any(h in cls for h in _TERMINAL_HINTS)
    except (OSError, subprocess.SubprocessError):
        return False


def _inject_paste(text):
    """Inject text via the clipboard + a paste keystroke. Atomic — avoids the
    per-character keymap remapping xdotool needs for non-ASCII (which drops and
    reorders CJK), and bypasses IME composition entirely. Returns True on
    success."""
    keep = bool(CLIPBOARD)
    saved = None if keep else _clip_get()
    if not _clip_set(text):
        return False
    time.sleep(PASTE_SETTLE)            # let the clipboard manager register it
    combo = "ctrl+shift+v" if _active_window_is_terminal() else "ctrl+v"
    subprocess.run(["xdotool", "key", "--clearmodifiers", combo], check=False)
    if not keep:                        # restore the user's clipboard
        time.sleep(0.25)                # but only after the app has read ours
        _clip_set(saved or "")
    return True


DEFAULT_CONFIG = """\
# blurt configuration. Environment variables (BLURT_*) override these.

[model]
name = "auto"             # "auto" picks by device (below); or force one, e.g. "large-v3-turbo", "small.en"
gpu_name = "large-v3-turbo"  # model used when a CUDA GPU is available (accurate)
cpu_name = "small.en"        # model used on CPU (much faster than large-v3 there)
device = "auto"           # "auto" | "cuda" | "cpu"
compute = "auto"          # "auto" | "int8" | "int8_float16" | "float16" | "float32"
language = "auto"         # "auto" detects per utterance; "keyboard" follows your active keyboard layout / IM (fcitx5, ibus, KDE, xkb-switch); or pin "en","ko",… (multilingual model only)
beam_size = 5

[input]
mode = "toggle"           # "toggle" (press start / press stop) or "hold" (push-to-talk)
# Keybinds are best edited with `blurt settings`. These scalars seed the
# defaults; the [keybinds] section (written by the settings window) overrides
# them and supports multiple binds per action.
# key = "ctrl+grave"      # a key that STARTS recording (also `blurt toggle` via your DE)
stop_key = "space"        # while recording: stop + transcribe
submit_key = "enter"      # while recording: stop + transcribe, then press Enter
cancel_key = "alt+backspace" # while recording: cancel + discard (nothing typed)
submit_delay = 0.6        # seconds to wait after typing before pressing Enter
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
