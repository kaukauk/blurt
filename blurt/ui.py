"""
blurt recording overlay.

Two states, driven by lines on stdin from the daemon:
  - "<float>"   -> recording: a volume-responsive bell-shaped equaliser.
  - "T <secs>"  -> transcribing: the window stays up and shows a progress bar.
                   secs > 0 gives a determinate bar (estimated from history);
                   secs <= 0 gives an indeterminate "Transcribing…" animation.
  - stdin EOF   -> finish: fill the bar and close.

Never takes keyboard focus. Drag to move; position is saved. Run via
`python -m blurt.ui`. Placement/dragging rely on the X11 window manager.
"""
import os
import sys
import json
import math
import time
import socket
import random
import subprocess
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
GEAR_X, GEAR_Y, GEAR_R = 34, 24, 8     # settings cog, above the status dot
DEL_X, DEL_Y, DEL_R = 34, 80, 7        # delete/discard, below the status dot
HIT_R = 14
PANEL_BG = (0.07, 0.08, 0.11)


def _send(cmd):
    """Best-effort one-shot command to the daemon over its unix socket."""
    try:
        s = socket.socket(socket.AF_UNIX)
        s.connect(C.SOCKET_PATH)
        s.sendall(cmd.encode())
        s.close()
    except OSError:
        pass


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
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


class Overlay(Gtk.Window):
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

        # recording state
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

        # transcribing state
        self.mode = "recording"      # "recording" | "transcribing"
        self.est = None
        self.t_trans = 0.0
        self.prog = 0.0
        self.finishing = False
        self._quit_scheduled = False
        self._save_id = 0

        self._gear_hover = False
        self._del_hover = False
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("draw", self.on_draw)
        self.connect("destroy", Gtk.main_quit)
        self.connect("button-press-event", self.on_button)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("configure-event", self.on_configure)
        GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP, self.on_stdin)
        GLib.timeout_add(16, self.on_tick)
        self.show_all()

    def _monitor_geometry(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        return monitor.get_geometry()

    # ---- dragging + persistence ----------------------------------------
    @staticmethod
    def _hit(x, y, cx, cy):
        return (x - cx) ** 2 + (y - cy) ** 2 <= HIT_R ** 2

    def on_motion(self, _w, ev):
        gear = self._hit(ev.x, ev.y, GEAR_X, GEAR_Y)
        dele = self.mode == "recording" and self._hit(ev.x, ev.y, DEL_X, DEL_Y)
        if gear != self._gear_hover or dele != self._del_hover:
            self._gear_hover, self._del_hover = gear, dele
            self.queue_draw()
        return False

    def on_button(self, _w, ev):
        if ev.button == 1:
            if self._hit(ev.x, ev.y, GEAR_X, GEAR_Y):
                self._open_settings()
                return True
            if self.mode == "recording" and self._hit(ev.x, ev.y, DEL_X, DEL_Y):
                _send("cancel")          # discard the recording
                return True
            self.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
        return True

    def _open_settings(self):
        _send("stop")                    # stop recording when opening settings
        try:
            env = dict(os.environ)
            pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
            subprocess.Popen([sys.executable, "-m", "blurt.settings"], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[blurt-ui] could not open settings: {e}", file=sys.stderr)

    def on_configure(self, _w, _ev):
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(400, self._persist_position)
        return False

    def _persist_position(self):
        self._save_id = 0
        save_position(*self.get_position())
        return False

    # ---- input ---------------------------------------------------------
    def on_stdin(self, _fd, cond):
        if cond & GLib.IO_HUP:
            return self._on_eof()
        line = sys.stdin.readline()
        if not line:
            return self._on_eof()
        line = line.strip()
        if line.startswith("T"):
            parts = line.split()
            try:
                secs = float(parts[1]) if len(parts) > 1 else -1.0
            except ValueError:
                secs = -1.0
            self.mode = "transcribing"
            self.est = secs if secs > 0 else None
            self.t_trans = time.monotonic()
            self.prog = 0.0
        else:
            try:
                self.target = max(0.0, min(1.0, float(line)))
            except ValueError:
                pass
        return True

    def _on_eof(self):
        # Daemon closed the pipe. If transcribing, complete the bar then quit;
        # otherwise quit immediately.
        if self.mode == "transcribing":
            self.finishing = True
            return False   # stop watching stdin; the tick loop finishes the bar
        Gtk.main_quit()
        return False

    # ---- animation -----------------------------------------------------
    def on_tick(self):
        self.phase += 0.22
        self.dot = (self.dot + 0.05) % 1.0
        if self.mode == "recording":
            self._tick_recording()
        else:
            self._tick_transcribing()
        self.queue_draw()
        return True

    def _tick_recording(self):
        self.cur += (self.target - self.cur) * 0.5
        for i in range(N_BARS):
            wobble = 0.93 + 0.07 * math.sin(self.phase * self.bfreq[i] + self.bphase[i])
            idle = 0.04 * self.env[i] * (0.6 + 0.4 * math.sin(
                self.phase * 0.5 * self.bfreq[i] + self.bphase[i]))
            desired = max(idle, self.cur * self.env[i] * wobble)
            self.vel[i] += (desired - self.levels[i]) * 0.55
            self.vel[i] *= 0.55
            self.levels[i] = max(0.0, min(1.0, self.levels[i] + self.vel[i]))

    def _tick_transcribing(self):
        if self.finishing:
            self.prog += (1.0 - self.prog) * 0.35
            if self.prog >= 0.995 and not self._quit_scheduled:
                self._quit_scheduled = True
                GLib.timeout_add(140, Gtk.main_quit)
        elif self.est:
            elapsed = time.monotonic() - self.t_trans
            self.prog = min(0.92, elapsed / self.est)

    # ---- drawing -------------------------------------------------------
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

        if self.mode == "recording":
            self._draw_recording(cr)
            self._draw_delete(cr)
        else:
            self._draw_transcribing(cr)
        self._draw_gear(cr)
        return False

    def _draw_delete(self, cr):
        cx, cy = DEL_X, DEL_Y
        col = (1.0, 0.46, 0.48) if self._del_hover else (0.80, 0.40, 0.43)
        a = 0.98 if self._del_hover else 0.7
        cr.save()
        cr.set_source_rgba(*col, a)
        cr.set_line_width(1.6)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.move_to(cx - 6, cy - 4)                 # lid
        cr.line_to(cx + 6, cy - 4)
        cr.stroke()
        cr.move_to(cx - 2, cy - 4)                 # handle
        cr.line_to(cx - 2, cy - 6)
        cr.line_to(cx + 2, cy - 6)
        cr.line_to(cx + 2, cy - 4)
        cr.stroke()
        cr.move_to(cx - 4.5, cy - 2.5)             # can body (tapered)
        cr.line_to(cx - 3.6, cy + 6)
        cr.line_to(cx + 3.6, cy + 6)
        cr.line_to(cx + 4.5, cy - 2.5)
        cr.stroke()
        for dx in (-1.6, 1.6):                     # ribs
            cr.move_to(cx + dx, cy)
            cr.line_to(cx + dx * 0.7, cy + 4.5)
            cr.stroke()
        cr.restore()

    def _draw_gear(self, cr):
        cx, cy, r = GEAR_X, GEAR_Y, GEAR_R
        a = 0.95 if self._gear_hover else 0.5
        col = (0.92, 0.93, 0.96) if self._gear_hover else (0.62, 0.64, 0.72)
        cr.save()
        cr.set_source_rgba(*col, a)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(r * 0.5)          # 8 spoke "teeth"
        for i in range(8):
            ang = (i / 8.0) * 2 * math.pi
            cr.move_to(cx + math.cos(ang) * r * 0.55, cy + math.sin(ang) * r * 0.55)
            cr.line_to(cx + math.cos(ang) * r * 1.05, cy + math.sin(ang) * r * 1.05)
        cr.stroke()
        cr.arc(cx, cy, r * 0.62, 0, 2 * math.pi)   # body ring
        cr.set_line_width(r * 0.55)
        cr.stroke()
        cr.set_source_rgba(*PANEL_BG, 0.92)        # hub hole
        cr.arc(cx, cy, r * 0.30, 0, 2 * math.pi)
        cr.fill()
        cr.restore()

    def _draw_dot(self, cr, color):
        pulse = 0.5 + 0.5 * math.sin(self.dot * 2 * math.pi)
        cr.arc(34, H / 2, 7, 0, 2 * math.pi)
        cr.set_source_rgba(*color, 0.55 + 0.45 * pulse)
        cr.fill()

    def _draw_recording(self, cr):
        self._draw_dot(cr, (0.96, 0.27, 0.34))
        area_x0, area_x1 = 58, W - 26
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

    def _draw_transcribing(self, cr):
        self._draw_dot(cr, (1.0, 0.72, 0.22))
        tx0, tx1 = 58, W - 26
        tw = tx1 - tx0
        th = 9
        ty = H / 2 - th / 2
        # track
        rounded_rect(cr, tx0, ty, tw, th, th / 2)
        cr.set_source_rgba(1, 1, 1, 0.10)
        cr.fill()

        def fill_grad():
            g = cairo.LinearGradient(tx0, 0, tx1, 0)
            g.add_color_stop_rgba(0, *ACCENT_TOP, 0.95)
            g.add_color_stop_rgba(1, *ACCENT_BOT, 0.95)
            return g

        if self.est or self.finishing:
            w = max(th, tw * max(0.0, min(1.0, self.prog)))
            rounded_rect(cr, tx0, ty, w, th, th / 2)
            cr.set_source(fill_grad())
            cr.fill()
        else:
            # indeterminate marquee
            seg = tw * 0.3
            travel = tw + seg
            pos = (self.phase * 14) % travel - seg
            x0 = max(tx0, tx0 + pos)
            x1 = min(tx1, tx0 + pos + seg)
            if x1 > x0:
                rounded_rect(cr, x0, ty, x1 - x0, th, th / 2)
                cr.set_source(fill_grad())
                cr.fill()


def main():
    Overlay()
    Gtk.main()


if __name__ == "__main__":
    main()
