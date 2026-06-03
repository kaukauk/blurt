"""
blurt daemon: loads Whisper once and keeps it resident, records on demand,
transcribes, and types the result into the focused window.

Control:
  - `blurt toggle` (bind it to a hotkey in your desktop) starts/stops recording.
  - On X11 only: a configurable mouse button (default 9 = forward) toggles
    recording, and pressing Space while recording stops it. Both are grabbed so
    they don't leak to the focused window. These are skipped on Wayland, where
    you drive blurt purely through the `toggle` command bound to a compositor
    shortcut.
"""

import os
import sys
import time
import socket
import threading
import subprocess

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

from . import config as C

# X11 input grabbing is optional (absent/no-op on Wayland or headless).
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
            n = x.size
            w = self._vadbuf.size
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


class SpaceStopper:
    """Active X key-grab on Space (X11 only); swallows the press and stops."""

    MODS = (0,)
    def __init__(self, on_press):
        self._on_press = on_press
        self._disp = Display()
        self._root = self._disp.screen().root
        self._kc = self._disp.keysym_to_keycode(XK.XK_space)
        self.MODS = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)
        self._want = False
        self._grabbed = False
        threading.Thread(target=self._loop, daemon=True).start()

    def start(self):
        self._want = True

    def stop(self):
        self._want = False

    def _grab(self):
        for m in self.MODS:
            try:
                self._root.grab_key(self._kc, m, True, X.GrabModeAsync, X.GrabModeAsync)
            except Exception:
                pass
        self._disp.flush()
        self._grabbed = True

    def _ungrab(self):
        for m in self.MODS:
            try:
                self._root.ungrab_key(self._kc, m)
            except Exception:
                pass
        self._disp.flush()
        self._grabbed = False

    def _loop(self):
        fd = self._disp.fileno()
        while True:
            if self._want and not self._grabbed:
                self._grab()
            elif not self._want and self._grabbed:
                self._ungrab()
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                continue
            for _ in range(self._disp.pending_events()):
                ev = self._disp.next_event()
                if (self._grabbed and ev.type == X.KeyPress
                        and ev.detail == self._kc):
                    try:
                        self._on_press()
                    except Exception as e:
                        print(f"[blurt] space handler error: {e}", file=sys.stderr)


class ButtonToggle:
    """Permanent X button-grab (X11 only); each click calls on_press (debounced)."""

    DEBOUNCE = 0.4
    def __init__(self, button, on_press):
        self._button = button
        self._on_press = on_press
        self._disp = Display()
        self._root = self._disp.screen().root
        self._last = 0.0
        mods = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)
        for m in mods:
            try:
                self._root.grab_button(button, m, False, X.ButtonPressMask,
                                       X.GrabModeAsync, X.GrabModeAsync, X.NONE, X.NONE)
            except Exception as e:
                print(f"[blurt] grab_button failed: {e}", file=sys.stderr)
        self._disp.flush()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        fd = self._disp.fileno()
        while True:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                continue
            for _ in range(self._disp.pending_events()):
                ev = self._disp.next_event()
                if ev.type == X.ButtonPress and ev.detail == self._button:
                    now = time.time()
                    if now - self._last < self.DEBOUNCE:
                        continue
                    self._last = now
                    try:
                        self._on_press()
                    except Exception as e:
                        print(f"[blurt] button handler error: {e}", file=sys.stderr)


class Daemon:
    def __init__(self):
        device, compute = C.resolve_device()
        print(f"[blurt] loading '{C.MODEL_NAME}' on {device} ({compute}) ...",
              flush=True)
        t0 = time.time()
        self.model = WhisperModel(C.MODEL_NAME, device=device, compute_type=compute)
        print(f"[blurt] model ready in {time.time() - t0:.1f}s", flush=True)

        self.recorder = Recorder()
        self.busy = threading.Lock()
        self._ctl = threading.Lock()
        self._rec_start = 0.0
        self._ui = None

        # X11-only input grabs.
        self._stopper = None
        self._mouse = None
        if _XOK:
            try:
                self._stopper = SpaceStopper(self._on_space)
            except Exception as e:
                print(f"[blurt] Space grab unavailable: {e}", file=sys.stderr)
            if C.MOUSE_BUTTON > 0:
                try:
                    self._mouse = ButtonToggle(C.MOUSE_BUTTON, self.toggle)
                    print(f"[blurt] mouse button {C.MOUSE_BUTTON} toggles recording",
                          flush=True)
                except Exception as e:
                    print(f"[blurt] mouse bind failed: {e}", file=sys.stderr)
        else:
            print("[blurt] no X11 grabs (Wayland/headless): bind `blurt toggle` "
                  "to a shortcut in your desktop settings", flush=True)

        # Voice-activity model for gating the visualiser.
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
        C.notify("blurt ready", "Dictation is running")
        print("[blurt] ready", flush=True)

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
        p = self.recorder.speech
        return max(0.0, min(1.0, (p - 0.25) / 0.20))

    # --- Space-to-stop ----------------------------------------------------
    def _on_space(self):
        if not self.recorder.recording:
            return
        if time.time() - self._rec_start < 0.4:
            return
        print("[blurt] space -> stop", flush=True)
        self.stop_recording()

    # --- overlay ----------------------------------------------------------
    def _start_ui(self):
        if not C.SHOW_UI:
            return
        try:
            env = dict(os.environ)
            pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
            self._ui = subprocess.Popen(
                [C.UI_PYTHON, "-m", "blurt.ui"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            threading.Thread(target=self._ui_sender, args=(self._ui,),
                             daemon=True).start()
        except Exception as e:
            print(f"[blurt] UI launch failed: {e}", file=sys.stderr)
            self._ui = None

    def _ui_sender(self, proc):
        try:
            while proc.poll() is None and self.recorder.recording:
                lvl = self.recorder.level * self._gate()
                proc.stdin.write(f"{lvl:.4f}\n".encode())
                proc.stdin.flush()
                time.sleep(1 / 60)
        except (BrokenPipeError, OSError):
            pass

    def _stop_ui(self):
        proc, self._ui = self._ui, None
        if proc is None:
            return
        for fn in (proc.stdin.close, proc.terminate):
            try:
                fn()
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
            if self._stopper:
                self._stopper.start()
            self._start_ui()
            C.notify("🎙️ Recording…", "Say something")
            print("[blurt] recording started", flush=True)

    def stop_recording(self):
        with self._ctl:
            if not self.recorder.recording:
                return
            if self._stopper:
                self._stopper.stop()
            self._stop_ui()
            audio = self.recorder.stop()
            print(f"[blurt] recording stopped ({len(audio)/C.SAMPLE_RATE:.1f}s)",
                  flush=True)
            threading.Thread(target=self._transcribe, args=(audio,),
                             daemon=True).start()

    def toggle(self):
        if self.recorder.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def _transcribe(self, audio):
        with self.busy:
            if audio.size < C.SAMPLE_RATE * C.MIN_SECONDS:
                C.notify("Too short", "No audio captured")
                return
            C.notify("✍️ Transcribing…")
            t0 = time.time()
            segments, _ = self.model.transcribe(
                audio, language=C.LANGUAGE, beam_size=5, vad_filter=True,
                condition_on_previous_text=False, without_timestamps=True,
                initial_prompt=C.PROMPT)
            text = "".join(s.text for s in segments).strip()
            print(f"[blurt] transcribed in {time.time()-t0:.2f}s: {text!r}",
                  flush=True)
            if text:
                C.type_text(text)
            else:
                C.notify("No speech detected")

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
                    "toggle": self.toggle}
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
    Daemon().serve()


if __name__ == "__main__":
    main()
