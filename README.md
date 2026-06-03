# blurt

**Fast, local, push-to-toggle speech-to-text dictation for Linux.** Press a key
(or your mouse's forward button), speak, press again — your words are typed
straight into whatever window has focus. Everything runs locally with OpenAI
Whisper; nothing is sent to the cloud.

- 🎤 **Toggle to dictate** — start/stop with a hotkey, the mouse forward button,
  or `blurt toggle` bound to any shortcut.
- ⚡ **Whisper** via faster-whisper/CTranslate2 with **int8** quantization. On a
  **CUDA GPU** it runs `large-v3-turbo` faster than realtime; on **CPU** it picks
  a smaller, much faster English model automatically (see *GPU vs CPU* below).
- 🧠 **Accurate, punctuated English** out of the box.
- 📊 **Live overlay** — a translucent, draggable bell-shaped equaliser that
  reacts to your **voice** (Silero VAD gating ignores music/noise).
- 🖥️ **Picks the right model for your hardware** — `model.name = "auto"` uses
  the big model on a GPU and a fast one on CPU; override either in the config.
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
(e.g. *Settings → Keyboard → Shortcuts*). On **X11** the daemon also grabs two
**mouse buttons** automatically: **button 8 (back)** toggles plain dictation and
**button 9 (forward)** toggles *submit* dictation (it presses **Enter** after
typing — handy for chat boxes and search bars). While recording you can also
press **Space** to stop, **Enter** to stop + submit, or **Alt+Backspace** to
cancel and discard — no extra config.

First run downloads the model (~1.5 GB) to `~/.cache/huggingface`.

## Usage

```
blurt toggle     # start, or stop+transcribe if already recording
blurt submit     # like toggle, but also presses Enter after transcribing
blurt cancel     # stop recording and discard it (nothing transcribed)
blurt settings   # open the settings window (edit keybinds, mode, …)
blurt gpu        # set up GPU acceleration; `blurt gpu --disable` to revert
blurt start      # start recording
blurt stop       # stop and transcribe
blurt config     # write/locate ~/.config/blurt/config.toml
blurt report     # open a pre-filled GitHub bug report in your browser
blurt daemon     # run the daemon in the foreground (debug)
blurt version
```

## Errors & bug reports

blurt is built to never need the terminal. If something goes wrong it logs the
full traceback locally to `~/.cache/blurt/blurt.log` (rotating) and shows a
**pop-up** with a one-click **“Report on GitHub”** button that opens a
*pre-filled* issue in your browser — you just review and submit. Nothing is ever
sent automatically, and home paths/usernames are redacted. You can also trigger
it any time with `blurt report`, or disable the pop-up with `[report] popup =
false`.

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
toggle_button = 8         # mouse button: start, or stop + transcribe (no Enter); 0 disables
submit_button = 9         # mouse button: start, or stop + transcribe, then Enter; 0 disables
stop_key = "space"        # key that stops while recording (toggle mode); "" disables
submit_key = "enter"      # while recording: stop, transcribe, then press Enter; "" disables
cancel_key = "alt+backspace" # while recording: cancel + discard (nothing typed); "" disables
submit_delay = 0.6        # seconds to wait after typing before pressing Enter
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

## Settings

Run **`blurt settings`** (or click the ⚙ on the overlay while recording) to open
a window where you can:

- **Rebind everything** — start/stop, submit, stop, cancel. Click a bind (or
  **Add**) and blurt listens for the next key or mouse button and assigns it.
- **Add unlimited alternates** per action — every one is grabbed and consumes
  the input, just like the defaults.
- Switch **toggle ↔ hold** mode, tune the **submit delay**, and toggle clipboard.

Saving writes `~/.config/blurt/config.toml` and tells the running daemon to
reload instantly — no restart, no model reload.

## GPU vs CPU

blurt runs on **CPU by default** when installed from the AUR. The packaged
`ctranslate2` is built **without CUDA**, so even with an NVIDIA GPU the AUR
install transcribes on CPU. To keep that usable, `model.name = "auto"` loads a
small, fast English model on CPU (`small.en`) instead of `large-v3-turbo`, which
is ~2.5× slower than realtime on CPU.

**Want GPU speed?** Run **`blurt gpu`**. It creates a private venv with the PyPI
`ctranslate2` wheel (which is built with CUDA + bundles cuBLAS/cuDNN), verifies a
real GPU transcription works, and switches the daemon to it running
`large-v3-turbo` — faster than realtime on a modest GPU (e.g. a GTX 1660). The
overlay/dialogs keep using the system Python. Revert any time with
`blurt gpu --disable` (add `--purge` to also delete the venv).

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
