"""
blurt error pop-up. Reads one JSON line on stdin:
    {"summary": "...", "url": "https://github.com/.../issues/new?...", "log": "/path"}
and shows a small always-on-top dialog so the user never needs the terminal.
"Report on GitHub" opens the pre-filled issue in their browser.

Run with the system Python (it has GTK): `python -m blurt.errordialog`.
"""
import sys
import json
import subprocess
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk  # noqa: E402


def _open(target):
    if not target:
        return
    try:
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        try:
            import webbrowser
            webbrowser.open(target)
        except Exception:
            pass


class ErrorDialog(Gtk.Window):
    def __init__(self, summary, url, logpath):
        super().__init__(title="blurt error")
        self._url = url
        self._log = logpath
        self.set_keep_above(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_resizable(False)
        self.set_border_width(18)
        self.set_default_size(440, -1)
        self.connect("destroy", Gtk.main_quit)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(box)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = Gtk.Image.new_from_icon_name("dialog-error", Gtk.IconSize.DIALOG)
        head.pack_start(icon, False, False, 0)
        title = Gtk.Label()
        title.set_markup("<b>blurt hit an error</b>")
        title.set_xalign(0)
        head.pack_start(title, False, False, 0)
        box.pack_start(head, False, False, 0)

        msg = Gtk.Label(label=(summary or "Something went wrong.")
                        + "\n\nYou can file a pre-filled bug report — it opens in "
                        "your browser; just review and submit.")
        msg.set_line_wrap(True)
        msg.set_xalign(0)
        msg.set_max_width_chars(52)
        box.pack_start(msg, False, False, 0)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        dismiss = Gtk.Button(label="Dismiss")
        dismiss.connect("clicked", lambda *_: self.destroy())
        log_btn = Gtk.Button(label="Open log")
        log_btn.connect("clicked", lambda *_: _open(self._log))
        report = Gtk.Button(label="Report on GitHub")
        report.get_style_context().add_class("suggested-action")
        report.connect("clicked", self._report)
        for b in (dismiss, log_btn, report):
            btns.pack_start(b, False, False, 0)
        box.pack_start(btns, False, False, 0)

        self.show_all()

    def _report(self, *_):
        _open(self._url)
        self.destroy()


def main():
    try:
        data = json.loads(sys.stdin.readline() or "{}")
    except Exception:
        data = {}
    ErrorDialog(data.get("summary", ""), data.get("url", ""), data.get("log", ""))
    Gtk.main()


if __name__ == "__main__":
    main()
