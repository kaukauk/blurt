"""blurt command-line entry point."""

import sys
import socket

from . import config as C
from . import __version__

USAGE = """\
blurt — local push-to-toggle speech-to-text

usage: blurt <command>

commands:
  daemon     run the dictation daemon (loads the model, stays resident)
  toggle     start recording, or stop+transcribe if already recording
  submit     like toggle, but also presses Enter after transcribing
  cancel     stop recording and discard it (nothing transcribed)
  start      start recording
  stop       stop recording and transcribe
  settings   open the settings window (keybinds, mode, …)
  config     write a default config.toml (if missing) and print its path
  gpu        enable GPU acceleration (CUDA venv); `gpu --disable` to revert
  report     open a pre-filled GitHub bug report in your browser
  ui         run the waveform overlay standalone (debug)
  version    print version

Bind `blurt toggle` to a hotkey in your desktop settings. On X11 the daemon also
grabs a mouse button (default: forward) and Space-to-stop automatically.
"""


def _send(cmd):
    try:
        s = socket.socket(socket.AF_UNIX)
        s.connect(C.SOCKET_PATH)
        s.sendall(cmd.encode())
        s.close()
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        C.notify("blurt is not running", "Start the blurt service first")
        print("blurt daemon is not running (no socket). "
              "Start it: `systemctl --user start blurt` or `blurt daemon`.",
              file=sys.stderr)
        return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "help"

    if cmd == "daemon":
        from . import daemon
        daemon.main()
    elif cmd == "ui":
        from . import ui
        ui.main()
    elif cmd in ("toggle", "submit", "cancel", "start", "stop"):
        sys.exit(0 if _send(cmd) else 1)
    elif cmd == "settings":
        import os
        import subprocess
        env = dict(os.environ)
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
        # Launch on a Python with gi (the overlay's UI_PYTHON), not necessarily us.
        sys.exit(subprocess.run([C.UI_PYTHON, "-m", "blurt.settings"], env=env).returncode)
    elif cmd == "gpu":
        from . import gpu
        sys.exit(gpu.main(argv[1:]))
    elif cmd == "config":
        created = C.write_default_config()
        print(("created " if created else "exists  ") + C.CONFIG_FILE)
        if not created:
            print("(edit it, then `systemctl --user restart blurt`)")
    elif cmd == "report":
        from . import report
        body = report.issue_body()
        path = report.write_report(body)
        print("Opening a pre-filled GitHub issue in your browser — review and submit.")
        if path:
            print("Diagnostics also saved to", path)
        if not report.open_browser(report.issue_url("blurt: bug report", body)):
            print("Could not open a browser; the report is in the file above.")
    elif cmd in ("version", "--version", "-v"):
        print(f"blurt {__version__}")
    else:
        print(USAGE)
        sys.exit(0 if cmd in ("help", "-h", "--help") else 2)


if __name__ == "__main__":
    main()
