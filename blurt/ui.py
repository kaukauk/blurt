"""
blurt recording overlay: a translucent, rounded, always-on-top pill with a
volume-responsive bouncing equaliser (bell-shaped). Reads newline-separated
levels (0..1) on stdin from the daemon. Never takes keyboard focus. Drag it to
move; the position is saved. Run as `python -m blurt.ui`.

Window placement and dragging rely on the X11 window manager. Under Wayland the
bell still renders, but the compositor controls placement and dragging may be
unavailable — that's a Wayland limitation, not a bug.
"""
import os
import sys
import json
import math
import random
import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

from . import config as C  # noqa: E402

W, H = 440, 104
N_BARS = 40
BAR_W = 5.0
PAD_L = 116
PAD_R = 22
ACCENT_TOP = (0.20, 0.92, 0.82)
ACCENT_BOT = (0.29, 0.55, 1.00)
DEFAULT_Y_FRAC = 0.25


def load_position():
    try:
        with open(C.UI_STATE_FILE) as f:
            d = json.load(f)
        return int(d["x"]), int(d["y"])
    except Exception:
        return None


def save_position(x, y):
    try:
        os.makedirs(C.CONFIG_DIR, exist_ok=True)
        tmp = C.UI_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"x": int(x), "y": int(y)}, f)
        os.replace(tmp, C.UI_STATE_FILE)
    except Exception as e:
        print(f"[blurt-ui] save_position failed: {e}", file=sys.stderr)


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
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_app_paintable(True)

        screen = self.get_screen()
        vis = screen.get_rgba_visual()
        if vis is not None:
            self.set_visual(vis)
        self.set_default_size(W, H)

        saved = load_position()
        if saved is not None:
            self.move(*saved)
        else:
            mon = self._monitor_geometry()
            self.move(mon.x + (mon.width - W) // 2,
                      mon.y + int(mon.height * DEFAULT_Y_FRAC))

        self.levels = [0.0] * N_BARS
        self.vel = [0.0] * N_BARS
        center = (N_BARS - 1) / 2.0
        sigma = N_BARS / 4.5
        self.env = [math.exp(-((i - center) ** 2) / (2 * sigma ** 2))
                    for i in range(N_BARS)]
        rng = random.Random(1234)
        self.bphase = [rng.uniform(0, 2 * math.pi) for _ in range(N_BARS)]
        self.bfreq = [rng.uniform(0.6, 1.5) for _ in range(N_BARS)]
        self.cur = 0.0
        self.target = 0.0
        self.phase = 0.0
        self.dot = 0.0
        self._save_id = 0

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

    def on_button(self, _w, ev):
        if ev.button == 1:
            self.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
        return True

    def on_configure(self, _w, _ev):
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(400, self._persist_position)
        return False

    def _persist_position(self):
        self._save_id = 0
        save_position(*self.get_position())
        return False

    def on_stdin(self, _fd, cond):
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
        self.cur += (self.target - self.cur) * 0.5
        self.phase += 0.22
        for i in range(N_BARS):
            wobble = 0.93 + 0.07 * math.sin(self.phase * self.bfreq[i] + self.bphase[i])
            idle = 0.04 * self.env[i] * (0.6 + 0.4 * math.sin(
                self.phase * 0.5 * self.bfreq[i] + self.bphase[i]))
            desired = max(idle, self.cur * self.env[i] * wobble)
            self.vel[i] += (desired - self.levels[i]) * 0.55
            self.vel[i] *= 0.55
            self.levels[i] = max(0.0, min(1.0, self.levels[i] + self.vel[i]))
        self.dot = (self.dot + 0.05) % 1.0
        self.queue_draw()
        return True

    def on_draw(self, _w, cr):
        cr.set_operator(1)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(2)

        rounded_rect(cr, 1, 1, W - 2, H - 2, (H - 2) / 2)
        cr.set_source_rgba(0.07, 0.08, 0.11, 0.86)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.08)
        cr.set_line_width(1.2)
        cr.stroke()

        dot_y = H / 2
        pulse = 0.5 + 0.5 * math.sin(self.dot * 2 * math.pi)
        cr.arc(34, dot_y, 7, 0, 2 * math.pi)
        cr.set_source_rgba(0.96, 0.27, 0.34, 0.55 + 0.45 * pulse)
        cr.fill()

        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(15)
        cr.set_source_rgba(0.92, 0.94, 0.98, 0.92)
        cr.move_to(50, H / 2 + 5)
        cr.show_text("Listening")

        area_x0, area_x1 = PAD_L, W - PAD_R
        step = (area_x1 - area_x0) / N_BARS
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


def main():
    Waveform()
    Gtk.main()


if __name__ == "__main__":
    main()
