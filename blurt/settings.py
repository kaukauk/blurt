"""
blurt settings window (GTK).

Edit every keybind, add unlimited alternates per action, and tweak general
options. Clicking a bind (or "Add") listens for the next key/mouse press and
assigns it. Saving writes config.toml and tells the running daemon to reload —
no restart, no model reload. Runs on the system Python (needs gi):
`python -m blurt.settings`.
"""
import os
import sys
import socket
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

from . import config as C  # noqa: E402

_MOD_KEYVALS = {
    Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
    Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R,
    Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R,
    Gdk.KEY_ISO_Level3_Shift, Gdk.KEY_Caps_Lock, Gdk.KEY_Num_Lock,
}

_NICE = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super",
         "backspace": "Backspace", "enter": "Enter", "return": "Enter",
         "space": "Space", "delete": "Delete", "escape": "Esc", "tab": "Tab"}


def pretty(spec):
    s = str(spec)
    low = s.lower()
    for p in ("button", "mouse", "btn"):
        if low.startswith(p) and low[len(p):].isdigit():
            return f"Mouse {low[len(p):]}"
    out = []
    for part in s.split("+"):
        pl = part.lower()
        out.append(_NICE.get(pl, part.upper() if len(part) == 1 else part))
    return " + ".join(out)


def key_event_to_spec(ev):
    """Build a parseable bind spec from a Gdk key-press event, or None."""
    if ev.keyval in _MOD_KEYVALS:
        return None
    name = Gdk.keyval_name(Gdk.keyval_to_lower(ev.keyval))
    if not name:
        return None
    s, mods = ev.state, []
    if s & Gdk.ModifierType.CONTROL_MASK:
        mods.append("ctrl")
    if s & Gdk.ModifierType.MOD1_MASK:
        mods.append("alt")
    if (s & Gdk.ModifierType.SUPER_MASK) or (s & Gdk.ModifierType.MOD4_MASK):
        mods.append("super")
    if s & Gdk.ModifierType.SHIFT_MASK:
        mods.append("shift")
    return "+".join(mods + [name])


def send_reload():
    try:
        s = socket.socket(socket.AF_UNIX)
        s.connect(C.SOCKET_PATH)
        s.sendall(b"reload")
        s.close()
        return True
    except OSError:
        return False


class CaptureDialog(Gtk.Dialog):
    """Modal that grabs all input and resolves to one key/mouse spec."""

    def __init__(self, parent):
        super().__init__(title="Press a key…", transient_for=parent, modal=True)
        self.set_default_size(340, 130)
        self.spec = None
        self._seat = None
        lbl = Gtk.Label()
        lbl.set_markup("<b>Press a key or mouse button</b>\n"
                       "<span foreground='#888'>to bind it. Esc cancels.</span>")
        lbl.set_justify(Gtk.Justification.CENTER)
        lbl.set_margin_top(22)
        lbl.set_margin_bottom(22)
        self.get_content_area().add(lbl)
        self.add_events(Gdk.EventMask.KEY_PRESS_MASK
                        | Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("key-press-event", self._on_key)
        self.connect("button-press-event", self._on_button)
        self.connect("map-event", self._on_map)
        self.show_all()

    def _on_map(self, *_):
        self._seat = self.get_display().get_default_seat()
        self._seat.grab(self.get_window(), Gdk.SeatCapabilities.ALL,
                        False, None, None, None)
        return False

    def _finish(self, spec):
        self.spec = spec
        if self._seat:
            self._seat.ungrab()
        self.response(Gtk.ResponseType.OK if spec else Gtk.ResponseType.CANCEL)

    def _on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self._finish(None)
        else:
            spec = key_event_to_spec(ev)
            if spec:
                self._finish(spec)
        return True

    def _on_button(self, _w, ev):
        self._finish(f"button{int(ev.button)}")
        return True


class ActionRow(Gtk.Box):
    """One bindable action: header + a row of bind chips + an Add button."""

    def __init__(self, win, action, label, scope):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.win = win
        self.action = action
        self.binds = list(win.binds.get(action, []))
        hint = ("any time" if scope == "global"
                else "while recording")
        head = Gtk.Label()
        head.set_xalign(0)
        head.set_markup(f"<b>{GLib.markup_escape_text(label)}</b>  "
                        f"<span size='small' foreground='#8a8a8a'>· {hint}</span>")
        self.pack_start(head, False, False, 0)
        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_max_children_per_line(10)
        self.flow.set_column_spacing(6)
        self.flow.set_row_spacing(6)
        self.pack_start(self.flow, False, False, 0)
        self._refresh()

    def _refresh(self):
        for c in self.flow.get_children():
            self.flow.remove(c)
        for spec in self.binds:
            self.flow.add(self._chip(spec))
        add = Gtk.Button(label="＋ Add")
        add.connect("clicked", lambda *_: self._add())
        self.flow.add(add)
        self.flow.show_all()

    def _chip(self, spec):
        box = Gtk.Box(spacing=0)
        box.get_style_context().add_class("linked")
        name = Gtk.Button(label=pretty(spec))
        name.set_tooltip_text(f"{spec} — click to rebind")
        name.connect("clicked", lambda *_: self._replace(spec))
        rm = Gtk.Button(label="✕")
        rm.set_tooltip_text("Remove")
        rm.connect("clicked", lambda *_: self._remove(spec))
        box.pack_start(name, False, False, 0)
        box.pack_start(rm, False, False, 0)
        return box

    def _capture(self):
        dlg = CaptureDialog(self.win)
        resp = dlg.run()
        spec = dlg.spec
        dlg.destroy()
        return spec if resp == Gtk.ResponseType.OK else None

    def _add(self):
        spec = self._capture()
        if spec and spec not in self.binds:
            self.binds.append(spec)
        self._refresh()
        self.win.mark_dirty()

    def _replace(self, old):
        spec = self._capture()
        if spec and old in self.binds:
            self.binds[self.binds.index(old)] = spec
        self._refresh()
        self.win.mark_dirty()

    def _remove(self, spec):
        if spec in self.binds:
            self.binds.remove(spec)
        self._refresh()
        self.win.mark_dirty()


class Settings(Gtk.Window):
    def __init__(self):
        super().__init__(title="blurt settings")
        self.set_default_size(460, 600)
        self.set_border_width(14)
        self.connect("destroy", Gtk.main_quit)
        self.binds = C.keybinds()

        self._apply_css()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(outer)

        title = Gtk.Label()
        title.set_markup("<span size='large' weight='bold'>blurt settings</span>")
        title.set_xalign(0)
        outer.pack_start(title, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.pack_start(scroll, True, True, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        scroll.add(body)

        body.pack_start(self._section_label("Keybinds"), False, False, 0)
        body.pack_start(Gtk.Label(
            label="Add as many alternates as you like — each one is grabbed and "
                  "consumes the input.", xalign=0, wrap=True), False, False, 0)
        self.rows = []
        for action, label, _behavior, scope in C.KEYBIND_SPEC:
            row = ActionRow(self, action, label, scope)
            self.rows.append(row)
            body.pack_start(row, False, False, 0)

        body.pack_start(Gtk.Separator(), False, False, 4)
        body.pack_start(self._section_label("General"), False, False, 0)
        body.pack_start(self._general(), False, False, 0)

        # footer
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status = Gtk.Label(label="", xalign=0)
        self.status.get_style_context().add_class("dim-label")
        foot.pack_start(self.status, True, True, 0)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.destroy())
        self.save_btn = Gtk.Button(label="Save & Apply")
        self.save_btn.get_style_context().add_class("suggested-action")
        self.save_btn.connect("clicked", self._save)
        foot.pack_start(close, False, False, 0)
        foot.pack_start(self.save_btn, False, False, 0)
        outer.pack_start(foot, False, False, 0)

        self.show_all()

    def _apply_css(self):
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            flowboxchild { padding: 0; }
            button { padding: 2px 8px; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _section_label(self, text):
        lbl = Gtk.Label()
        lbl.set_markup(f"<b>{text}</b>")
        lbl.set_xalign(0)
        return lbl

    def _general(self):
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)

        grid.attach(Gtk.Label(label="Mode", xalign=0), 0, 0, 1, 1)
        self.mode = Gtk.ComboBoxText()
        for m in ("toggle", "hold"):
            self.mode.append_text(m)
        self.mode.set_active(0 if str(C.MODE).lower() != "hold" else 1)
        self.mode.set_tooltip_text("toggle: press to start/stop. "
                                   "hold: push-to-talk (key/button held).")
        grid.attach(self.mode, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Submit delay (s)", xalign=0), 0, 1, 1, 1)
        self.delay = Gtk.SpinButton.new_with_range(0.0, 3.0, 0.1)
        self.delay.set_value(float(C.SUBMIT_DELAY))
        self.delay.set_tooltip_text("Pause after typing before pressing Enter "
                                    "on a submit bind.")
        grid.attach(self.delay, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="Copy to clipboard", xalign=0), 0, 2, 1, 1)
        self.clipboard = Gtk.Switch()
        self.clipboard.set_active(bool(C.CLIPBOARD))
        self.clipboard.set_halign(Gtk.Align.START)
        grid.attach(self.clipboard, 1, 2, 1, 1)

        for w in (self.mode, self.delay):
            w.connect("changed" if isinstance(w, Gtk.ComboBoxText)
                      else "value-changed", lambda *_: self.mark_dirty())
        self.clipboard.connect("notify::active", lambda *_: self.mark_dirty())
        return grid

    def mark_dirty(self):
        self.status.set_text("Unsaved changes")

    def _save(self, *_):
        updates = {
            "keybinds": {r.action: r.binds for r in self.rows},
            "input": {"mode": self.mode.get_active_text(),
                      "submit_delay": round(self.delay.get_value(), 2)},
            "output": {"clipboard": self.clipboard.get_active()},
        }
        try:
            C.save_config(updates)
        except Exception as e:
            self.status.set_text(f"Save failed: {e}")
            return
        applied = send_reload()
        self.status.set_text("Saved — applied to the running daemon."
                             if applied else
                             "Saved. Daemon not running; will apply on next start.")


def main():
    Settings()
    Gtk.main()


if __name__ == "__main__":
    main()
