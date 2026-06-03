"""
blurt daemon: loads Whisper once and keeps it resident, records on demand,
transcribes, and types the result into the focused window.

Triggers (X11): an optional global key (default off), two mouse buttons —
button 8 toggles plain dictation (stop + transcribe) and button 9 toggles
"submit" dictation (stop + transcribe, then press Enter) — and, in toggle mode
while recording, a stop-key (Space), a submit-key (Enter), and a cancel chord
(Alt+Delete, which discards the recording). Mode is "toggle" (press start /
press stop) or "hold" (push-to-talk). All grabs are swallowed so they don't leak
to the focused window. On Wayland the grabs are skipped; drive blurt with
`blurt toggle` / `blurt submit` / `blurt cancel` bound to compositor shortcuts.
"""

import os
import sys
import time
import json
import socket
import threading
import traceback
import subprocess

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

from . import config as C
from . import report


_last_popup = 0.0


def report_error(summary, tb_text):
    """Log an error locally and (throttled) pop up a one-click report dialog."""
    global _last_popup
    report.log_exception(summary, tb_text)
    print(f"[blurt] ERROR: {summary}", file=sys.stderr)
    if not C.ERROR_POPUP:
        return
    now = time.time()
    if now - _last_popup < 20:   # don't spam dialogs
        return
    _last_popup = now
    try:
        title = f"blurt error: {summary}"
        url = report.issue_url(title, report.issue_body(error_text=tb_text))
        payload = json.dumps({"summary": summary, "url": url, "log": C.LOG_FILE})
        env = dict(os.environ)
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
        p = subprocess.Popen([C.UI_PYTHON, "-m", "blurt.errordialog"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env)
        p.stdin.write((payload + "\n").encode())
        p.stdin.flush()
        p.stdin.close()
    except Exception as e:
        print(f"[blurt] could not show error popup: {e}", file=sys.stderr)


def _install_excepthooks():
    def main_hook(exc_type, exc, tb):
        report_error(f"{exc_type.__name__}: {exc}",
                     "".join(traceback.format_exception(exc_type, exc, tb)))

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        report_error(f"{args.exc_type.__name__}: {args.exc_value}",
                     "".join(traceback.format_exception(
                         args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = main_hook
    threading.excepthook = thread_hook

_XOK = False
if C.is_x11():
    try:
        import select
        from Xlib import X, XK
        from Xlib.display import Display
        _XOK = True
    except Exception as e:
        print(f"[blurt] Xlib unavailable, hotkey grabs disabled: {e}",
              file=sys.stderr)

_LOCK_COMBOS = (0,)
if _XOK:
    _LOCK_COMBOS = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)


class Recorder:
    """Continuous input stream that buffers audio only while `recording`."""

    def __init__(self):
        self._frames = []
        self._lock = threading.Lock()
        self.recording = False
        self.level = 0.0
        self.speech = 0.0
        self._vadbuf = np.zeros(C.VAD_WINDOW, dtype=np.float32)
        self.stream = sd.InputStream(
            samplerate=C.SAMPLE_RATE, channels=C.CHANNELS,
            dtype="float32", callback=self._callback, blocksize=0)
        self.stream.start()

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[blurt] audio status: {status}", file=sys.stderr)
        if not self.recording:
            return
        x = indata[:, 0] if indata.ndim > 1 else indata
        with self._lock:
            self._frames.append(indata.copy())
            n, w = x.size, self._vadbuf.size
            if n >= w:
                self._vadbuf = x[-w:].astype(np.float32).copy()
            elif n > 0:
                self._vadbuf = np.concatenate([self._vadbuf[n:], x]).astype(np.float32)
        if x.size >= 4:
            hp = np.diff(x)
            hp = 0.25 * hp[:-2] + 0.5 * hp[1:-1] + 0.25 * hp[2:]
            rms = float(np.sqrt(np.mean(np.square(hp))))
        else:
            rms = 0.0
        level = (rms ** 0.6) * 17.0
        self.level = 0.0 if level < 0.05 else min(1.0, level)

    def vad_window(self):
        with self._lock:
            return self._vadbuf.copy()

    def start(self):
        with self._lock:
            self._frames = []
            self._vadbuf[:] = 0.0
        self.speech = 0.0
        self.recording = True

    def stop(self):
        self.recording = False
        with self._lock:
            frames = self._frames
            self._frames = []
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames, axis=0).reshape(-1)


# --- X11 input triggers ------------------------------------------------------
def _parse_keyspec(disp, spec):
    """'alt+space' -> (modmask, keycode), or None if unparseable."""
    modmap = {"shift": X.ShiftMask, "ctrl": X.ControlMask, "control": X.ControlMask,
              "alt": X.Mod1Mask, "mod1": X.Mod1Mask, "super": X.Mod4Mask,
              "meta": X.Mod4Mask, "win": X.Mod4Mask, "hyper": X.Mod4Mask}
    keyalias = {"enter": "Return", "return": "Return", "esc": "Escape",
                "del": "Delete", "ins": "Insert", "pgup": "Prior", "pgdn": "Next",
                "backspace": "BackSpace", "bksp": "BackSpace", "tab": "Tab"}
    parts = [p for p in str(spec).replace(" ", "").split("+") if p]
    if not parts:
        return None
    *mods, key = parts
    mask = 0
    for m in mods:
        ml = m.lower()
        if ml not in modmap:
            print(f"[blurt] unknown modifier {m!r} in key {spec!r}", file=sys.stderr)
        mask |= modmap.get(ml, 0)
    # Keep the key token's case (X keysyms like Page_Up, BackSpace are case-
    # sensitive), but also try aliases / lower / capitalised forms.
    kl = key.lower()
    key = keyalias.get(kl, key)
    ks = (XK.string_to_keysym(key) or XK.string_to_keysym(kl)
          or XK.string_to_keysym(key.capitalize()))
    if not ks:
        print(f"[blurt] unknown key {key!r} in {spec!r}", file=sys.stderr)
        return None
    kc = disp.keysym_to_keycode(ks)
    if not kc:
        return None
    return mask, kc


def _safe(fn):
    try:
        fn()
    except Exception as e:
        print(f"[blurt] input handler error: {e}", file=sys.stderr)


def _parse_trigger(disp, spec):
    """Classify a bind: ('button', N) | ('key', mask, kc) | None."""
    s = str(spec).strip().lower()
    for prefix in ("button", "mouse", "btn"):
        if s.startswith(prefix) and s[len(prefix):].isdigit():
            return ("button", int(s[len(prefix):]))
    pk = _parse_keyspec(disp, spec)
    if pk:
        return ("key", pk[0], pk[1])
    return None


class KeyTrigger:
    """Persistent X key-grab; toggle on press, or hold-to-talk (press/release)."""

    def __init__(self, mask, kc, mode, on_start, on_stop, on_toggle, label="key"):
        self._mask = mask
        self._kc = kc
        self._mode = mode
        self._on_start, self._on_stop, self._on_toggle = on_start, on_stop, on_toggle
        self._alive = True
        self._disp = Display()
        self._root = self._disp.screen().root
        self._down = False
        self._release_at = None
        self._errs = 0
        self._disp.set_error_handler(self._on_xerror)
        for e in _LOCK_COMBOS:
            self._root.grab_key(kc, mask | e, True, X.GrabModeAsync, X.GrabModeAsync)
        self._disp.sync()
        if self._errs:
            print(f"[blurt] could not grab {label!r} (reserved by your desktop?); "
                  f"bind `blurt toggle` to a key in your DE instead", file=sys.stderr)
        threading.Thread(target=self._loop, daemon=True).start()

    def _on_xerror(self, *_a):
        self._errs += 1

    def close(self):
        self._alive = False

    def _teardown(self):
        try:
            for e in _LOCK_COMBOS:
                try:
                    self._root.ungrab_key(self._kc, self._mask | e)
                except Exception:
                    pass
            self._disp.sync()
            self._disp.close()
        except Exception:
            pass

    def _loop(self):
        fd = self._disp.fileno()
        while self._alive:
            r, _, _ = select.select([fd], [], [], 0.02)
            # Deferred release (hold mode): fire stop unless cancelled by an
            # auto-repeat KeyPress within the window.
            if (self._mode == "hold" and self._release_at is not None
                    and time.time() - self._release_at >= 0.03):
                self._release_at = None
                if self._down:
                    self._down = False
                    _safe(self._on_stop)
            if not r:
                continue
            for _ in range(self._disp.pending_events()):
                ev = self._disp.next_event()
                if getattr(ev, "detail", None) != self._kc:
                    continue
                if ev.type == X.KeyPress:
                    if self._mode == "toggle":
                        _safe(self._on_toggle)
                    elif self._release_at is not None:
                        self._release_at = None        # auto-repeat: stay down
                    elif not self._down:
                        self._down = True
                        _safe(self._on_start)
                elif ev.type == X.KeyRelease and self._mode == "hold":
                    self._release_at = time.time()
        self._teardown()


class ButtonTrigger:
    """Persistent X mouse-button grab; toggle on click, or hold-to-talk."""

    DEBOUNCE = 0.35

    def __init__(self, button, mode, on_start, on_stop, on_toggle, label=None):
        self._button = button
        self._mode = mode
        self._on_start, self._on_stop, self._on_toggle = on_start, on_stop, on_toggle
        self._alive = True
        self._disp = Display()
        self._root = self._disp.screen().root
        self._last = 0.0
        self._errs = 0
        self._disp.set_error_handler(lambda *_a: self._bump())
        mask = X.ButtonPressMask | X.ButtonReleaseMask
        for e in _LOCK_COMBOS:
            self._root.grab_button(button, e, False, mask,
                                   X.GrabModeAsync, X.GrabModeAsync, X.NONE, X.NONE)
        self._disp.sync()
        if self._errs:
            print(f"[blurt] could not grab mouse button {button} "
                  f"({label or 'in use'}?)", file=sys.stderr)
        threading.Thread(target=self._loop, daemon=True).start()

    def _bump(self):
        self._errs += 1

    def close(self):
        self._alive = False

    def _teardown(self):
        try:
            for e in _LOCK_COMBOS:
                try:
                    self._root.ungrab_button(self._button, e)
                except Exception:
                    pass
            self._disp.sync()
            self._disp.close()
        except Exception:
            pass

    def _loop(self):
        fd = self._disp.fileno()
        while self._alive:
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                continue
            for _ in range(self._disp.pending_events()):
                ev = self._disp.next_event()
                if getattr(ev, "detail", None) != self._button:
                    continue
                if ev.type == X.ButtonPress:
                    if self._mode == "toggle":
                        now = time.time()
                        if now - self._last < self.DEBOUNCE:
                            continue
                        self._last = now
                        _safe(self._on_toggle)
                    else:
                        _safe(self._on_start)
                elif ev.type == X.ButtonRelease and self._mode == "hold":
                    _safe(self._on_stop)
        self._teardown()


class _WhileRecording:
    """Base: an X grab that is active only while recording (toggle mode).

    start()/stop() arm/disarm the grab; close() tears it down for reload.
    Subclasses implement _grab(on) and _matches(ev) + fire on a matching press.
    """

    def __init__(self, label="key"):
        self._label = label
        self._errs = 0
        self._warned = False
        self._alive = True
        self._want = False
        self._grabbed = False
        self._disp = Display()
        self._disp.set_error_handler(lambda *_a: self._bump())
        self._root = self._disp.screen().root
        threading.Thread(target=self._loop, daemon=True).start()

    def _bump(self):
        self._errs += 1

    def start(self):
        self._want = True

    def stop(self):
        self._want = False

    def close(self):
        self._alive = False

    def _set_grab(self, on):
        before = self._errs
        self._grab(on)
        self._disp.sync()
        self._grabbed = on
        if on and self._errs > before and not self._warned:
            self._warned = True
            print(f"[blurt] could not grab {self._label!r} (reserved by your "
                  f"desktop?); pick a different bind in settings", file=sys.stderr)

    def _loop(self):
        fd = self._disp.fileno()
        while self._alive:
            if self._want and not self._grabbed:
                self._set_grab(True)
            elif not self._want and self._grabbed:
                self._set_grab(False)
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                continue
            for _ in range(self._disp.pending_events()):
                ev = self._disp.next_event()
                if self._grabbed and self._matches(ev):
                    try:
                        self._on_fire()
                    except Exception as e:
                        print(f"[blurt] bind error: {e}", file=sys.stderr)
        if self._grabbed:
            self._set_grab(False)
        try:
            self._disp.close()
        except Exception:
            pass


class StopKey(_WhileRecording):
    """Key-grab active only while recording; press (with optional modifier) fires."""

    def __init__(self, mask, kc, on_fire, label="key"):
        self._mask, self._kc, self._cb = mask, kc, on_fire
        super().__init__(label)

    def _grab(self, on):
        for e in _LOCK_COMBOS:
            try:
                if on:
                    self._root.grab_key(self._kc, self._mask | e, True,
                                        X.GrabModeAsync, X.GrabModeAsync)
                else:
                    self._root.ungrab_key(self._kc, self._mask | e)
            except Exception:
                pass

    def _matches(self, ev):
        return ev.type == X.KeyPress and getattr(ev, "detail", None) == self._kc

    def _on_fire(self):
        self._cb()


class RecordingButton(_WhileRecording):
    """Mouse-button grab active only while recording; press fires (debounced)."""

    DEBOUNCE = 0.35

    def __init__(self, button, on_fire, label="button"):
        self._button, self._cb = button, on_fire
        self._last = 0.0
        super().__init__(label)

    def _grab(self, on):
        mask = X.ButtonPressMask | X.ButtonReleaseMask
        for e in _LOCK_COMBOS:
            try:
                if on:
                    self._root.grab_button(self._button, e, False, mask,
                                           X.GrabModeAsync, X.GrabModeAsync,
                                           X.NONE, X.NONE)
                else:
                    self._root.ungrab_button(self._button, e)
            except Exception:
                pass

    def _matches(self, ev):
        if ev.type != X.ButtonPress or getattr(ev, "detail", None) != self._button:
            return False
        now = time.time()
        if now - self._last < self.DEBOUNCE:
            return False
        self._last = now
        return True

    def _on_fire(self):
        self._cb()


class Daemon:
    def __init__(self):
        device, compute = C.resolve_device()
        model_name = C.resolve_model(device)
        print(f"[blurt] loading '{model_name}' on {device} ({compute}) ...",
              flush=True)
        t0 = time.time()
        self.model = WhisperModel(model_name, device=device, compute_type=compute)
        print(f"[blurt] model ready in {time.time() - t0:.1f}s", flush=True)

        self.recorder = Recorder()
        self.busy = threading.Lock()
        self._ctl = threading.Lock()
        self._ui_lock = threading.Lock()
        self._rec_start = 0.0
        self._ui = None
        self._ui_phase = None     # "recording" | "transcribing" | None
        self._global_triggers = []  # always-active grabs (toggle/submit buttons+keys)
        self._rec_triggers = []     # grabs active only while recording (stop/submit/cancel)
        self._reload_lock = threading.Lock()
        self._timing = self._load_timing()

        if _XOK:
            self._setup_triggers()
        else:
            print("[blurt] no X11 grabs (Wayland/headless): bind `blurt toggle` "
                  "to a shortcut in your desktop settings", flush=True)

        self._vad = None
        if C.ENABLE_VAD and C.SHOW_UI:
            try:
                from faster_whisper.vad import get_vad_model
                self._vad = get_vad_model()
                threading.Thread(target=self._vad_loop, daemon=True).start()
            except Exception as e:
                print(f"[blurt] VAD unavailable ({e})", file=sys.stderr)

        try:
            list(self.model.transcribe(np.zeros(C.SAMPLE_RATE, dtype=np.float32),
                                       language=C.LANGUAGE)[0])
        except Exception:
            pass
        C.notify("blurt ready", f"{C.MODE} mode — dictation is running")
        print(f"[blurt] ready ({C.MODE} mode)", flush=True)

    def _action_cb(self, behavior):
        return {
            "start":  self.start_recording,
            "plain":  self.stop_recording,
            "submit": lambda: self.stop_recording(submit=True),
            "cancel": self.cancel_recording,
        }[behavior]

    def _setup_triggers(self):
        """Build one grab per physical key/button, dispatching by recording state.

        A trigger bound to a global ("start") action AND a while-recording action
        (stop/submit/delete) becomes a single always-grabbed dispatcher: idle
        press starts, recording press runs the while-recording action. A trigger
        bound only to a while-recording action is grabbed only while recording, so
        common keys (Space, Enter) still work normally when idle.
        """
        meta = {a: (beh, scope) for a, _l, beh, scope in C.KEYBIND_SPEC}
        kb = C.keybinds()

        if C.MODE == "hold":          # push-to-talk: only the global start binds
            disp = Display()
            for spec in kb.get("start", []):
                p = _parse_trigger(disp, spec)
                if not p:
                    print(f"[blurt] skipping unparseable bind {spec!r}", file=sys.stderr)
                    continue
                if p[0] == "button":
                    t = ButtonTrigger(p[1], "hold", self.start_recording,
                                      self.stop_recording, None, label=spec)
                else:
                    t = KeyTrigger(p[1], p[2], "hold", self.start_recording,
                                   self.stop_recording, None, label=spec)
                self._global_triggers.append(t)
                print(f"[blurt] {spec} -> hold (push-to-talk)", flush=True)
            disp.close()
            return

        # toggle mode: group binds by physical trigger.
        by_trigger = {}   # spec -> {"idle": cb|None, "rec": cb|None}
        for action, specs in kb.items():
            beh, scope = meta[action]
            cb = self._action_cb(beh)
            for spec in specs:
                slot = by_trigger.setdefault(spec, {"idle": None, "rec": None})
                key = "idle" if scope == "global" else "rec"
                if slot[key] is None:
                    slot[key] = cb

        disp = Display()
        for spec, slot in by_trigger.items():
            p = _parse_trigger(disp, spec)
            if not p:
                print(f"[blurt] skipping unparseable bind {spec!r}", file=sys.stderr)
                continue
            idle_cb, rec_cb = slot["idle"], slot["rec"]

            def dispatch(idle_cb=idle_cb, rec_cb=rec_cb):
                if self.recorder.recording:
                    if rec_cb:
                        rec_cb()
                elif idle_cb:
                    idle_cb()

            if idle_cb is not None:
                # must be grabbed even when idle to catch the start press
                if p[0] == "button":
                    t = ButtonTrigger(p[1], "toggle", None, None, dispatch, label=spec)
                else:
                    t = KeyTrigger(p[1], p[2], "toggle", None, None, dispatch, label=spec)
                self._global_triggers.append(t)
            else:
                # only fires while recording → dynamic grab (idle key passes through)
                if p[0] == "button":
                    t = RecordingButton(p[1], rec_cb, label=spec)
                else:
                    t = StopKey(p[1], p[2], rec_cb, label=spec)
                self._rec_triggers.append(t)
            roles = "+".join(r for r in ("start" if idle_cb else "",
                                         "act" if rec_cb else "") if r)
            print(f"[blurt] bind {spec} ({roles})", flush=True)
        disp.close()

    def reload(self):
        """Re-read config and rebuild input triggers without reloading the model."""
        with self._reload_lock:
            for t in self._global_triggers + self._rec_triggers:
                try:
                    t.close()
                except Exception:
                    pass
            self._global_triggers, self._rec_triggers = [], []
            time.sleep(0.3)   # let old grabs release before re-grabbing
            C.reload()
            if _XOK:
                self._setup_triggers()
                if self.recorder.recording:
                    for t in self._rec_triggers:
                        t.start()
            print(f"[blurt] config reloaded ({C.MODE} mode)", flush=True)

    # --- timing history / estimate ----------------------------------------
    def _load_timing(self):
        try:
            with open(C.TIMING_FILE) as f:
                return [tuple(p) for p in json.load(f)][-30:]
        except Exception:
            return []

    def _save_timing(self):
        try:
            os.makedirs(C.CONFIG_DIR, exist_ok=True)
            with open(C.TIMING_FILE, "w") as f:
                json.dump(self._timing[-30:], f)
        except Exception:
            pass

    def estimate_time(self, duration):
        """Estimate transcription seconds from history (linear fit), or None."""
        pts = self._timing
        if len(pts) < 3:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        n = len(xs)
        sx, sy = sum(xs), sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, ys))
        denom = n * sxx - sx * sx
        if abs(denom) < 1e-9:
            return max(0.2, sy / n)
        a = (n * sxy - sx * sy) / denom
        b = (sy - a * sx) / n
        return float(min(30.0, max(0.2, a * duration + b)))

    # --- VAD gate ---------------------------------------------------------
    def _vad_loop(self):
        while True:
            if not self.recorder.recording:
                self.recorder.speech = 0.0
                time.sleep(0.05)
                continue
            try:
                out = np.asarray(self._vad(self.recorder.vad_window())).reshape(-1)
                p = float(out.max()) if out.size else 0.0
            except Exception:
                p = 0.0
            cur = self.recorder.speech
            a = 0.6 if p > cur else 0.18
            self.recorder.speech = cur + (p - cur) * a
            time.sleep(0.03)

    def _gate(self):
        if self._vad is None:
            return 1.0
        return max(0.0, min(1.0, (self.recorder.speech - 0.25) / 0.20))

    # --- overlay ----------------------------------------------------------
    def _start_ui(self):
        if not C.SHOW_UI:
            return
        try:
            env = dict(os.environ)
            pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
            self._ui_phase = "recording"
            self._ui = subprocess.Popen(
                [C.UI_PYTHON, "-m", "blurt.ui"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            threading.Thread(target=self._ui_sender, args=(self._ui,),
                             daemon=True).start()
        except Exception as e:
            print(f"[blurt] UI launch failed: {e}", file=sys.stderr)
            self._ui = None

    def _ui_write(self, line):
        with self._ui_lock:
            if self._ui is None or self._ui.poll() is not None:
                return
            try:
                self._ui.stdin.write(line.encode())
                self._ui.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def _ui_sender(self, proc):
        while proc.poll() is None:
            with self._ui_lock:
                if self._ui_phase != "recording" or not self.recorder.recording:
                    break
                try:
                    proc.stdin.write(f"{self.recorder.level * self._gate():.4f}\n".encode())
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    break
            time.sleep(1 / 60)

    def _stop_ui(self):
        with self._ui_lock:
            proc, self._ui = self._ui, None
            self._ui_phase = None
        if proc is None:
            return
        for fn in (proc.stdin.close, proc.terminate):
            try:
                fn()
            except Exception:
                pass

    def _finish_ui(self):
        """Close stdin so the overlay finishes its progress bar and quits."""
        with self._ui_lock:
            proc, self._ui = self._ui, None
            self._ui_phase = None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass

    # --- control ----------------------------------------------------------
    def start_recording(self):
        with self._ctl:
            if self.recorder.recording:
                return
            self.recorder.level = 0.0
            self.recorder.start()
            self._rec_start = time.time()
            for t in self._rec_triggers:
                t.start()
            self._start_ui()
            C.notify("🎙️ Recording…", "Say something")
            print("[blurt] recording started", flush=True)

    def stop_recording(self, submit=False):
        with self._ctl:
            if not self.recorder.recording:
                return
            for t in self._rec_triggers:
                t.stop()
            audio = self.recorder.stop()
            dur = len(audio) / C.SAMPLE_RATE
            print(f"[blurt] recording stopped ({dur:.1f}s)", flush=True)
            progress = bool(C.SHOW_UI and C.UI_PROGRESS and self._ui
                            and audio.size >= C.SAMPLE_RATE * C.MIN_SECONDS)
            if progress:
                with self._ui_lock:
                    self._ui_phase = "transcribing"
                est = self.estimate_time(dur)
                self._ui_write(f"T {est if est is not None else -1:.3f}\n")
            else:
                self._stop_ui()
            threading.Thread(target=self._transcribe,
                             args=(audio, dur, progress, submit),
                             daemon=True).start()

    def toggle(self):
        if self.recorder.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def submit(self):
        """Like toggle, but on stop also presses Enter after transcribing."""
        if self.recorder.recording:
            self.stop_recording(submit=True)
        else:
            self.start_recording()

    def cancel_toggle(self):
        """For a global-scope cancel bind: start if idle, else cancel + discard."""
        if self.recorder.recording:
            self.cancel_recording()
        else:
            self.start_recording()

    def cancel_recording(self):
        """Stop recording and discard the audio — nothing is transcribed/typed."""
        with self._ctl:
            if not self.recorder.recording:
                return
            for t in self._rec_triggers:
                t.stop()
            self.recorder.stop()   # clears the buffer; we ignore the audio
            self._stop_ui()
            C.notify("Cancelled", "Recording discarded")
            print("[blurt] recording cancelled (discarded)", flush=True)

    def _transcribe(self, audio, duration, progress, submit=False):
        with self.busy:
            if audio.size < C.SAMPLE_RATE * C.MIN_SECONDS:
                if not progress:
                    C.notify("Too short", "No audio captured")
                self._finish_ui()
                return
            try:
                if not progress:
                    C.notify("✍️ Transcribing…")
                t0 = time.time()
                segments, _ = self.model.transcribe(
                    audio, language=C.LANGUAGE, beam_size=C.BEAM_SIZE,
                    vad_filter=True, condition_on_previous_text=False,
                    without_timestamps=True, initial_prompt=C.PROMPT)
                text = "".join(s.text for s in segments).strip()
                dt = time.time() - t0
                print(f"[blurt] transcribed in {dt:.2f}s: {text!r}", flush=True)
                self._timing.append((round(duration, 2), round(dt, 3)))
                self._save_timing()
                if text:
                    C.copy_clipboard(text)  # never lost if nothing is focused
                    C.type_text(text)
                    if submit:
                        time.sleep(C.SUBMIT_DELAY)  # let the app settle, then send
                        C.press_enter()
                elif not progress:
                    C.notify("No speech detected")
            except Exception:
                report_error("transcription failed", traceback.format_exc())
            finally:
                self._finish_ui()

    # --- socket server ----------------------------------------------------
    def serve(self):
        if os.path.exists(C.SOCKET_PATH):
            os.unlink(C.SOCKET_PATH)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(C.SOCKET_PATH)
        os.chmod(C.SOCKET_PATH, 0o600)
        srv.listen(8)
        print(f"[blurt] listening on {C.SOCKET_PATH}", flush=True)
        handlers = {"start": self.start_recording, "stop": self.stop_recording,
                    "toggle": self.toggle, "submit": self.submit,
                    "cancel": self.cancel_recording, "reload": self.reload}
        while True:
            conn, _ = srv.accept()
            try:
                data = conn.recv(64).decode("utf-8", "ignore").strip()
                if data in handlers:
                    handlers[data]()
                    reply = b"ok"
                elif data == "ping":
                    reply = b"pong"
                else:
                    reply = b"unknown"
                try:
                    conn.sendall(reply)
                except (BrokenPipeError, OSError):
                    pass
            except Exception as e:
                print(f"[blurt] conn error: {e}", file=sys.stderr)
            finally:
                conn.close()


def main():
    _install_excepthooks()
    Daemon().serve()


if __name__ == "__main__":
    main()
