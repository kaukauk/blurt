#!/usr/bin/env python3
"""
Local push-to-toggle speech-to-text daemon.

Loads Whisper large-v3-turbo on the GPU (faster-whisper / CTranslate2) once at
startup and keeps it resident. Control:
  - Alt+Space (XFCE shortcut -> `stt-toggle start`) begins recording. XFCE
    consumes the combo so no stray space is inserted.
  - Space stops recording: while recording, the daemon holds an X key-grab on
    Space, so the press is delivered to us and swallowed (it does NOT reach the
    focused window). On that press we transcribe and type into the focused window.
"""

import os
import sys
import socket
import threading
import subprocess
import time
import select

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from Xlib import X, XK
from Xlib.display import Display

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL_NAME   = os.environ.get("STT_MODEL", "large-v3-turbo")
DEVICE       = os.environ.get("STT_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("STT_COMPUTE", "float16")
LANGUAGE     = os.environ.get("STT_LANG", "en")
SAMPLE_RATE  = 16000
CHANNELS     = 1
MIN_SECONDS  = 0.3   # ignore accidental ultra-short taps

# Bias the model toward correct punctuation & capitalization. This text is only
# context (it never appears in the output) but it primes Whisper's writing style,
# which turbo otherwise tends to drop on short dictation.
PUNCT_PROMPT = os.environ.get(
    "STT_PROMPT",
    "Hello. Here is the dictation, written with correct punctuation, "
    "capitalization, commas, periods, and question marks?")

RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
SOCKET_PATH = os.path.join(RUNTIME_DIR, "stt-daemon.sock")

HERE = os.path.dirname(os.path.abspath(__file__))
UI_SCRIPT = os.path.join(HERE, "ui_waveform.py")
# The overlay uses GTK3/Cairo from the system Python (the venv has neither).
UI_PYTHON = os.environ.get("STT_UI_PYTHON", "/usr/bin/python3")
SHOW_UI = os.environ.get("STT_UI", "1") != "0"


def notify(title, body="", icon="audio-input-microphone"):
    """Best-effort desktop notification; never crash the daemon over it."""
    try:
        subprocess.Popen(
            ["notify-send", "-a", "linux-stt", "-i", icon, "-t", "1500", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def type_text(text):
    """Type text into the currently focused window via xdotool (X11)."""
    if not text:
        return
    try:
        subprocess.run(
            ["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", text],
            check=False,
        )
    except Exception as e:
        print(f"[stt] xdotool type failed: {e}", file=sys.stderr)


class Recorder:
    """Continuous input stream that buffers audio only while `recording`."""

    def __init__(self):
        self._frames = []
        self._lock = threading.Lock()
        self.recording = False
        self.level = 0.0          # latest mic level (0..1) for the visualiser
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=self._callback,
            blocksize=0,
        )
        self.stream.start()

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[stt] audio status: {status}", file=sys.stderr)
        if self.recording:
            with self._lock:
                self._frames.append(indata.copy())
            # Perceptual level for the waveform: RMS, gained and soft-clipped.
            rms = float(np.sqrt(np.mean(np.square(indata))))
            self.level = min(1.0, (rms ** 0.6) * 6.5)

    def start(self):
        with self._lock:
            self._frames = []
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
    """
    Holds an active X key-grab on the Space key while armed. Because the grab is
    active, the Space press is delivered to this client and is NOT passed on to
    the focused window -- so the stop key is swallowed. Calls `on_press` (in the
    grab thread) for each swallowed Space.

    All Xlib calls happen on the single internal loop thread; start()/stop() only
    flip a desired-state flag, keeping the X connection single-threaded and safe.
    """

    # Grab plain Space across the harmless lock modifiers so NumLock/CapsLock
    # don't defeat it. We deliberately avoid Alt (Mod1) so we never collide with
    # XFCE's Alt+Space grab (which would raise BadAccess).
    MODS = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)

    def __init__(self, on_press):
        self._on_press = on_press
        self._disp = Display()
        self._root = self._disp.screen().root
        self._kc = self._disp.keysym_to_keycode(XK.XK_space)
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
                self._root.grab_key(self._kc, m, True,
                                    X.GrabModeAsync, X.GrabModeAsync)
            except Exception as e:
                print(f"[stt] grab_key failed: {e}", file=sys.stderr)
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
            # Reconcile desired vs actual grab state (only this thread touches X).
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
                        print(f"[stt] on_press error: {e}", file=sys.stderr)


class Daemon:
    def __init__(self):
        print(f"[stt] loading model '{MODEL_NAME}' on {DEVICE} ({COMPUTE_TYPE}) ...",
              flush=True)
        t0 = time.time()
        self.model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
        print(f"[stt] model ready in {time.time() - t0:.1f}s", flush=True)
        self.recorder = Recorder()
        self.busy = threading.Lock()
        self._ctl = threading.Lock()     # serialize start/stop transitions
        self._rec_start = 0.0
        self._stopper = SpaceStopper(self._on_space)  # swallows Space to stop
        self._ui = None                               # waveform overlay process
        # Warm up CUDA kernels so the first real transcription isn't slow.
        try:
            list(self.model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32),
                                       language=LANGUAGE)[0])
        except Exception:
            pass
        notify("Speech-to-text ready", "Press Alt+Space to dictate")
        print("[stt] ready", flush=True)

    # --- Space-to-stop (swallowed via X grab) -----------------------------
    def _on_space(self):
        # Called from the grabber thread for each swallowed Space press.
        if not self.recorder.recording:
            return
        # Ignore a stray Space right after start (e.g. the Alt+Space combo).
        if time.time() - self._rec_start < 0.4:
            return
        print("[stt] space pressed -> stop", flush=True)
        self.stop_recording()

    # --- waveform overlay -------------------------------------------------
    def _start_ui(self):
        if not SHOW_UI:
            return
        try:
            self._ui = subprocess.Popen(
                [UI_PYTHON, UI_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            threading.Thread(target=self._ui_sender, args=(self._ui,),
                             daemon=True).start()
        except Exception as e:
            print(f"[stt] UI launch failed: {e}", file=sys.stderr)
            self._ui = None

    def _ui_sender(self, proc):
        """Stream the live mic level to the overlay at ~60 Hz."""
        try:
            while proc.poll() is None and self.recorder.recording:
                proc.stdin.write(f"{self.recorder.level:.4f}\n".encode())
                proc.stdin.flush()
                time.sleep(1 / 60)
        except (BrokenPipeError, OSError):
            pass

    def _stop_ui(self):
        proc, self._ui = self._ui, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
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
            self._stopper.start()   # begin swallowing Space
            self._start_ui()        # show the waveform overlay
            notify("🎙️ Recording…", "Press Space to stop")
            print("[stt] recording started", flush=True)

    def stop_recording(self):
        with self._ctl:
            if not self.recorder.recording:
                return
            self._stopper.stop()    # release the Space grab
            self._stop_ui()         # hide the waveform overlay
            audio = self.recorder.stop()
            print(f"[stt] recording stopped ({len(audio)/SAMPLE_RATE:.1f}s)", flush=True)
            threading.Thread(target=self._transcribe, args=(audio,), daemon=True).start()

    def toggle(self):
        if self.recorder.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def _transcribe(self, audio):
        with self.busy:
            if audio.size < SAMPLE_RATE * MIN_SECONDS:
                notify("Too short", "No audio captured")
                return
            notify("✍️ Transcribing…")
            t0 = time.time()
            segments, _ = self.model.transcribe(
                audio,
                language=LANGUAGE,
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
                initial_prompt=PUNCT_PROMPT,
            )
            text = "".join(s.text for s in segments).strip()
            dt = time.time() - t0
            print(f"[stt] transcribed in {dt:.2f}s: {text!r}", flush=True)
            if text:
                type_text(text)
            else:
                notify("No speech detected")

    def serve(self):
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        srv.listen(8)
        print(f"[stt] listening on {SOCKET_PATH}", flush=True)
        while True:
            conn, _ = srv.accept()
            try:
                data = conn.recv(64).decode("utf-8", "ignore").strip()
                if data == "start":
                    self.start_recording()
                    reply = b"ok"
                elif data == "stop":
                    self.stop_recording()
                    reply = b"ok"
                elif data == "toggle":
                    self.toggle()
                    reply = b"ok"
                elif data == "ping":
                    reply = b"pong"
                else:
                    reply = b"unknown"
                # Client may have already closed; replying is best-effort.
                try:
                    conn.sendall(reply)
                except (BrokenPipeError, OSError):
                    pass
            except Exception as e:
                print(f"[stt] conn error: {e}", file=sys.stderr)
            finally:
                conn.close()


if __name__ == "__main__":
    Daemon().serve()
