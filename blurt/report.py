"""
Error logging and one-click bug reporting for blurt.

Nothing is ever sent automatically. We log locally (rotating file + journal) and,
on demand, build a *pre-filled* GitHub issue URL the user opens in their browser
and submits themselves. Home paths / usernames are redacted from anything shown.
"""

import os
import sys
import time
import getpass
import platform
import subprocess
import urllib.parse

from . import config as C

_HOME = os.path.expanduser("~")
try:
    _USER = getpass.getuser()
except Exception:
    _USER = os.environ.get("USER", "")


def redact(text):
    if not text:
        return text
    text = text.replace(_HOME, "~")
    if _USER:
        text = text.replace("/home/" + _USER, "~").replace(_USER, "user")
    return text


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=4).stdout.strip()
    except Exception:
        return ""


def _os_name():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return platform.platform()


def _gpu():
    name = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    return name.splitlines()[0] if name else "none / CPU"


def diagnostics():
    from . import __version__
    device, compute = C.resolve_device()
    rows = {
        "blurt": __version__,
        "python": platform.python_version(),
        "OS": _os_name(),
        "session": C.session_type() or "?",
        "device/compute": f"{device} / {compute}",
        "model": C.MODEL_NAME,
        "GPU": _gpu(),
        "typer": C.TYPER or "none",
    }
    return "\n".join(f"- {k}: {v}" for k, v in rows.items())


def log_exception(header, body):
    """Append a timestamped, redacted entry to the rotating log file."""
    try:
        os.makedirs(C.CACHE_DIR, exist_ok=True)
        # crude size-based rotation: keep the file under ~1 MB
        if os.path.exists(C.LOG_FILE) and os.path.getsize(C.LOG_FILE) > 1_000_000:
            os.replace(C.LOG_FILE, C.LOG_FILE + ".1")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(C.LOG_FILE, "a") as f:
            f.write(f"\n===== {stamp}  {header} =====\n{redact(body)}\n")
    except Exception as e:
        print(f"[blurt] could not write log: {e}", file=sys.stderr)


def recent_log(max_chars=2500):
    try:
        with open(C.LOG_FILE) as f:
            data = f.read()
        return redact(data[-max_chars:])
    except Exception:
        return ""


def issue_body(error_text="", what=""):
    err = redact(error_text or recent_log()).strip()
    if len(err) > 2500:
        err = "…(truncated; see ~/.cache/blurt/blurt.log)\n" + err[-2500:]
    return (
        f"### What happened\n{what or '_Describe what you were doing._'}\n\n"
        f"### Error\n```\n{err or 'no traceback captured'}\n```\n\n"
        f"### Diagnostics\n{diagnostics()}\n\n"
        f"_Full local log: `~/.cache/blurt/blurt.log`_\n"
    )


def issue_url(title, body):
    base = C.REPO_URL.rstrip("/") + "/issues/new"
    # keep the URL comfortably under common limits
    q = urllib.parse.urlencode({"title": title[:120], "body": body[:5500],
                                "labels": "bug"})
    return base + "?" + q


def open_browser(url):
    for opener in (["xdg-open", url], ["gio", "open", url]):
        try:
            subprocess.Popen(opener, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    try:
        import webbrowser
        return webbrowser.open(url)
    except Exception:
        return False


def write_report(text):
    try:
        os.makedirs(C.CACHE_DIR, exist_ok=True)
        path = os.path.join(C.CACHE_DIR, "last-report.txt")
        with open(path, "w") as f:
            f.write(text)
        return path
    except Exception:
        return None
