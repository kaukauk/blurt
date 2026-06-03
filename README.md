# blurt

**Fast, local, push-to-toggle speech-to-text dictation for Linux.** Press a key
(or your mouse's forward button), speak, press again — your words are typed
straight into whatever window has focus. Everything runs locally with OpenAI
Whisper; nothing is sent to the cloud.

- 🎤 **Toggle to dictate** — start/stop with a hotkey, the mouse forward button,
  or `blurt toggle` bound to any shortcut.
- ⚡ **Fast** — Whisper `large-v3-turbo` via faster-whisper/CTranslate2, with
  automatic **int8** quantization (≈4× faster than fp16 on GTX 16xx/20xx GPUs).
- 🧠 **Accurate, punctuated English** out of the box.
- 📊 **Live overlay** — a translucent, draggable bell-shaped equaliser that
  reacts to your **voice** (Silero VAD gating ignores music/noise).
- 🖥️ **GPU optional** — uses CUDA automatically if present, falls back to CPU.
- ⌨️ Types into the focused window via `xdotool` (X11) or `wtype`/`ydotool`
  (Wayland).

## Install (Arch / AUR)

```bash
# with an AUR helper
yay -S blurt        # or: paru -S blurt

# enable it at login and bind a hotkey
systemctl --user enable --now blurt.service
```

Then bind **`blurt toggle`** to a keyboard shortcut in your desktop settings
(e.g. *Settings → Keyboard → Shortcuts*). On **X11** the daemon also grabs the
**mouse forward button** (toggle) and **Space** (stop while recording)
automatically — no extra config.

First run downloads the model (~1.5 GB) to `~/.cache/huggingface`.

## Usage

```
blurt toggle     # start, or stop+transcribe if already recording
blurt start      # start recording
blurt stop       # stop and transcribe
blurt daemon     # run the daemon in the foreground (debug)
blurt version
```

## Configuration

Run `blurt config` to create `~/.config/blurt/config.toml`, then edit it and
`systemctl --user restart blurt`. Every setting also has a `BLURT_*` env override.

```toml
[model]
name = "large-v3-turbo"   # any faster-whisper model: small.en, medium.en, …
device = "auto"           # auto | cuda | cpu
compute = "auto"          # auto | int8 | int8_float16 | float16 | float32
language = "en"
beam_size = 5

[input]
mode = "toggle"           # "toggle" (press start / press stop) or "hold" (push-to-talk)
mouse_button = 9          # mouse button to trigger (9 = forward); 0 disables
stop_key = "space"        # key that stops while recording (toggle mode); "" disables
# key = "ctrl+grave"      # optional key blurt grabs directly (see note below)
# typer = "xdotool"       # force xdotool | wtype | ydotool

[ui]
enabled = true            # show the waveform overlay
vad = true                # only react to voice (Silero VAD), not music
progress = true           # keep the window up with a progress bar while transcribing
notifications = false     # desktop popups (off — the overlay shows state)

[output]
clipboard = true          # also copy each transcription to the clipboard
```

**Start key.** Bind **`blurt toggle`** to a shortcut in your desktop settings —
that's the portable way to get a global start/stop key on any DE. blurt can also
grab a key directly via `input.key`, but most desktops reserve common combos
(e.g. XFCE owns Alt+Space), so a DE binding is more reliable. `input.key` *is*
required for **hold** mode on a key (a DE binding only sends one event, so it
can't do push-to-talk; use the mouse button or `input.key` for hold).

**Clipboard.** Every transcription is also copied to the clipboard (needs
`xclip`/`xsel` on X11 or `wl-clipboard` on Wayland), so nothing is lost if no
text field is focused — just paste.

The overlay position is saved to `~/.config/blurt/ui.json` when you drag it.

## X11 vs Wayland

- **X11 (recommended):** everything works — global hotkey grabs, mouse-button
  toggle, Space-to-stop (all swallowed so they don't leak to your app), and the
  draggable always-on-top overlay.
- **Wayland:** transcription and typing work (via `wtype`/`ydotool`), but
  **global hotkeys, button-grabbing, and Space-to-stop are compositor-specific**
  and not grabbed by blurt. Drive it by binding `blurt toggle` to a compositor
  shortcut. The overlay renders, but placement/dragging is limited by the
  compositor.

## How it works

```
hotkey / mouse button ──▶ blurt toggle ──(unix socket)──▶ blurt daemon
                                                              │
                              record mic ◀─────────────────────┤
   stop ──▶ Whisper transcribe ──▶ type into focused window (xdotool/wtype)
            overlay shows a VAD-gated bell of your voice while recording
```

## Build / run from source

```bash
git clone https://github.com/kaukauk/blurt && cd blurt
makepkg -si          # build & install the package
# or run in place:
python -m blurt daemon
```

Dependencies: `python-faster-whisper`, `python-sounddevice`, `python-numpy`,
`python-xlib`, `python-gobject`, `python-cairo`, `gtk3`, `libnotify`, and one of
`xdotool` / `wtype` / `ydotool`. Optional: `cuda` + `cudnn` for GPU.

## License

MIT — see [LICENSE](LICENSE).
