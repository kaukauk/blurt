#!/usr/bin/env python3
"""
Recording overlay: a translucent, rounded, always-on-top pill showing a live
waveform of what the microphone is hearing.

Run with the SYSTEM python (it has GTK3 + Cairo). Audio levels arrive as
newline-separated floats (0..1) on stdin, one per frame, produced by the daemon.
The window never takes keyboard focus, so the user's text field stays the typing
target. Closing stdin (or SIGTERM) makes it exit.
"""
import os
import sys
import math
import random
import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

W, H = 440, 104
N_BARS = 40
BAR_W = 5.0
PAD_L = 116          # space reserved for the dot + label
PAD_R = 22
ACCENT_TOP = (0.20, 0.92, 0.82)   # teal  #34ebd1
ACCENT_BOT = (0.29, 0.55, 1.00)   # blue  #4a8cff

# Persisted window position (saved when the user drags the overlay).
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "linux-stt")
CONFIG_FILE = os.path.join(CONFIG_DIR, "ui.json")
# Default: horizontally centred, ~1/4 down from the top (upper-middle).
DEFAULT_Y_FRAC = 0.25


def load_position():
    try:
        import json
        with open(CONFIG_FILE) as f:
            d = json.load(f)
        return int(d["x"]), int(d["y"])
    except Exception:
        return None


def save_position(x, y):
    try:
        import json
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"x": int(x), "y": int(y)}, f)
        os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        print(f"[ui] save_position failed: {e}", file=sys.stderr)


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


class Waveform(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        # UTILITY keeps the window above and out of the taskbar while still
        # letting the WM move it on begin_move_drag (NOTIFICATION can be
        # click-through under some WMs). Focus is still refused below.
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_app_paintable(True)

        screen = self.get_screen()
        vis = screen.get_rgba_visual()
        if vis is not None:
            self.set_visual(vis)
        self.set_default_size(W, H)

        # Restore the saved position, else default to upper-middle.
        saved = load_position()
        if saved is not None:
            self.move(*saved)
        else:
            mon = self._monitor_geometry()
            x = mon.x + (mon.width - W) // 2
            y = mon.y + int(mon.height * DEFAULT_Y_FRAC)
            self.move(x, y)

        self.levels = [0.0] * N_BARS   # current bar heights (0..1)
        self.vel = [0.0] * N_BARS      # per-bar velocity (for springy bounce)
        # Static envelope: bars are tallest in the middle, shorter at the edges.
        self.env = [0.45 + 0.55 * math.sin(math.pi * (i + 0.5) / N_BARS)
                    for i in range(N_BARS)]
        # Each bar gets an INDEPENDENT oscillator (random phase + frequency) so
        # there is no phase that marches across the bars -> no left/right "flow".
        rng = random.Random(1234)
        self.bphase = [rng.uniform(0, 2 * math.pi) for _ in range(N_BARS)]
        self.bfreq = [rng.uniform(0.6, 1.5) for _ in range(N_BARS)]
        self.cur = 0.0
        self.target = 0.0
        self.phase = 0.0
        self.dot = 0.0
        self._save_id = 0     # debounce handle for persisting position

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("draw", self.on_draw)
        self.connect("destroy", Gtk.main_quit)
        self.connect("button-press-event", self.on_button)
        self.connect("configure-event", self.on_configure)
        GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP, self.on_stdin)
        GLib.timeout_add(16, self.on_tick)
        self.show_all()

    def _monitor_geometry(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        return monitor.get_geometry()

    # ---- dragging + position persistence -------------------------------
    def on_button(self, _w, ev):
        # Click anywhere on the pill and drag to reposition (WM-driven move).
        if ev.button == 1:
            self.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
        return True

    def on_configure(self, _w, _ev):
        # Window moved/resized -> persist the new position (debounced).
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(400, self._persist_position)
        return False

    def _persist_position(self):
        self._save_id = 0
        x, y = self.get_position()
        save_position(x, y)
        return False

    # ---- input ---------------------------------------------------------
    def on_stdin(self, fd, cond):
        if cond & GLib.IO_HUP:
            Gtk.main_quit()
            return False
        line = sys.stdin.readline()
        if not line:
            Gtk.main_quit()
            return False
        try:
            self.target = max(0.0, min(1.0, float(line.strip())))
        except ValueError:
            pass
        return True

    def on_tick(self):
        # Bars bounce in place to the *current* volume (no scrolling).
        self.cur += (self.target - self.cur) * 0.5
        self.phase += 0.22
        for i in range(N_BARS):
            # Independent per-bar oscillators (random phase/freq) -> bars jitter
            # in place with no wave marching across them (no left/right flow).
            wobble = 0.78 + 0.22 * math.sin(self.phase * self.bfreq[i] + self.bphase[i])
            idle = 0.05 * (0.5 + 0.5 * math.sin(
                self.phase * 0.5 * self.bfreq[i] + self.bphase[i]))
            desired = max(idle, self.cur * self.env[i] * wobble)
            # Spring toward the target: snappy attack, softer settle => bounce.
            self.vel[i] += (desired - self.levels[i]) * 0.55
            self.vel[i] *= 0.55
            self.levels[i] = max(0.0, min(1.0, self.levels[i] + self.vel[i]))
        self.dot = (self.dot + 0.05) % 1.0
        self.queue_draw()
        return True

    # ---- drawing -------------------------------------------------------
    def on_draw(self, _w, cr):
        cr.set_operator(1)  # CLEAR
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(2)  # OVER

        # Panel.
        rounded_rect(cr, 1, 1, W - 2, H - 2, (H - 2) / 2)
        cr.set_source_rgba(0.07, 0.08, 0.11, 0.86)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.08)
        cr.set_line_width(1.2)
        cr.stroke()

        # Pulsing record dot.
        dot_x, dot_y = 34, H / 2
        pulse = 0.5 + 0.5 * math.sin(self.dot * 2 * math.pi)
        cr.arc(dot_x, dot_y, 7, 0, 2 * math.pi)
        cr.set_source_rgba(0.96, 0.27, 0.34, 0.55 + 0.45 * pulse)
        cr.fill()

        # Label.
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(15)
        cr.set_source_rgba(0.92, 0.94, 0.98, 0.92)
        cr.move_to(50, H / 2 + 5)
        cr.show_text("Listening")

        # Waveform: mirrored bars with a vertical teal->blue gradient.
        area_x0 = PAD_L
        area_x1 = W - PAD_R
        span = area_x1 - area_x0
        step = span / N_BARS
        mid = H / 2
        max_h = (H - 30) / 2

        for i, lv in enumerate(self.levels):
            h = max(2.0, lv * max_h)
            x = area_x0 + i * step + (step - BAR_W) / 2

            grad = cairo.LinearGradient(0, mid - h, 0, mid + h)
            grad.add_color_stop_rgba(0, *ACCENT_TOP, 0.95)
            grad.add_color_stop_rgba(1, *ACCENT_BOT, 0.95)
            cr.set_source(grad)
            rounded_rect(cr, x, mid - h, BAR_W, 2 * h, BAR_W / 2)
            cr.fill()
        return False


if __name__ == "__main__":
    Waveform()
    Gtk.main()
