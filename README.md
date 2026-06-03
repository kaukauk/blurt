# linux-stt — local push-to-toggle dictation

Fast, accurate, fully-local speech-to-text for Linux/X11. Press **Alt+Space**
to start dictating, press **Space** to stop — the transcript is typed straight
into whatever window has focus. Both keystrokes are swallowed, so neither the
starting Alt+Space nor the stopping Space leaves a stray character behind.

## What it uses

- **Model:** OpenAI Whisper `large-v3-turbo` (809M params) via
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2).
- **Acceleration:** NVIDIA GPU, `float16` (your GTX 1660 SUPER, CUDA + bundled
  cuDNN/cuBLAS wheels — no system CUDA install needed).
- **Audio:** default microphone via PortAudio (`sounddevice`), 16 kHz mono.
- **Typing:** `xdotool type` into the focused window (X11).
- **Overlay:** a translucent, rounded, always-on-top pill (`ui_waveform.py`,
  GTK3 + Cairo) appears while recording, showing a **volume-responsive bouncing
  equaliser** of what the mic hears. It never takes keyboard focus, so your text
  field stays the typing target. Defaults to the upper-middle of the screen;
  **click-drag it anywhere and the position is saved** to
  `~/.config/linux-stt/ui.json`. The daemon streams live mic levels to it at
  60 fps over a pipe.
- **Hotkeys:**
  - **Alt+Space = start.** Bound as an XFCE keyboard shortcut to `<Alt>space`.
    Because XFCE grabs the combo, **no stray space is inserted**. (XFCE's default
    Alt+Space "window menu" was disabled to free the combo.)
  - **Space = stop.** While recording, the daemon holds an **active X key-grab**
    on Space (via `python-xlib`), so the press is delivered to the daemon and
    **swallowed** — it never reaches the focused window. A 0.4 s debounce ignores
    a stray Space right after start. (Verified: with the grab active, a focused
    test window receives no Space; without it, the window receives it.)
- **Lifecycle:** a `systemd --user` service starts the daemon at login and keeps
  the model resident in VRAM, so each dictation starts instantly.

Accuracy/speed sanity check (JFK clip, 11 s): transcribed verbatim in ~5 s the
first time, and ~0.4× real-time after warm-up. Short dictation snippets feel
near-instant.

## How it works

```
Alt+Space ──(XFCE shortcut)──▶ stt-toggle start ──(unix socket)──▶ stt_daemon.py
                                                                       │
                                  start mic recording ◀────────────────┤
   Space ──(X key-grab inside daemon, swallowed)──▶ stop → transcribe → xdotool type
```

## Files

| File | Purpose |
|------|---------|
| `stt_daemon.py`   | Long-running daemon: loads the model, records, transcribes, types. |
| `run-daemon.sh`   | Launcher; puts the bundled NVIDIA libs on `LD_LIBRARY_PATH`. |
| `stt-toggle`      | Tiny client that sends a command (`start`/`stop`/`toggle`) to the daemon. Alt+Space runs `stt-toggle start`. |
| `ui_waveform.py`  | The recording overlay (bouncing EQ). Run by the daemon with the system Python (GTK3/Cairo). Drag to move; position saved to `~/.config/linux-stt/ui.json`. Disable with `STT_UI=0`. |
| `.venv/`          | Python 3.12 virtualenv with all dependencies. |
| `~/.config/systemd/user/linux-stt.service` | Autostart + supervision. |

## Managing it

```bash
systemctl --user status  linux-stt      # is it running?
systemctl --user restart linux-stt      # restart (e.g. after editing config)
systemctl --user stop    linux-stt      # stop
systemctl --user disable linux-stt      # don't start on login
journalctl --user -u linux-stt -f       # live logs
```

The Alt+Space binding lives in XFCE settings
(`Settings → Keyboard → Application Shortcuts`, or
`xfconf-query -c xfce4-keyboard-shortcuts -p '/commands/custom/<Alt>space'`).
The Space-to-stop is handled inside the daemon, not by XFCE.

## Tuning

Environment variables read at startup (set them in the `[Service]` section of the
unit file as `Environment=...`, then `systemctl --user daemon-reload && restart`):

- `STT_MODEL`   (default `large-v3-turbo`) — e.g. `small.en`, `medium.en`, `distil-large-v3`.
- `STT_COMPUTE` (default `float16`) — `int8_float16` saves VRAM, `float32` for CPU.
- `STT_DEVICE`  (default `cuda`) — set `cpu` to run without the GPU.
- `STT_LANG`    (default `en`).

Transcription quality knobs (`beam_size`, `vad_filter`) are in `stt_daemon.py`.
`vad_filter=True` is what suppresses Whisper's "Thank you." hallucination on
silence.

## Notes / limitations

- **X11 only** (uses `xdotool`). On Wayland you'd swap in `ydotool`/`wtype`.
- First run after a reboot reloads the model from disk cache (~1–2 s).
- Recording uses the system **default** input device; change it in
  `pavucontrol`/your audio settings.
