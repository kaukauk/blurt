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
  start      start recording
  stop       stop recording and transcribe
  config     write a default config.toml (if missing) and print its path
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
    elif cmd in ("toggle", "start", "stop"):
        sys.exit(0 if _send(cmd) else 1)
    elif cmd == "config":
        created = C.write_default_config()
        print(("created " if created else "exists  ") + C.CONFIG_FILE)
        if not created:
            print("(edit it, then `systemctl --user restart blurt`)")
    elif cmd in ("version", "--version", "-v"):
        print(f"blurt {__version__}")
    else:
        print(USAGE)
        sys.exit(0 if cmd in ("help", "-h", "--help") else 2)


if __name__ == "__main__":
    main()
