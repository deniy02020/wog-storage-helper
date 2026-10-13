"""Perspective interval wheel and countdown. Pure drawing, no game actions."""
from __future__ import annotations
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import math
import time
import tkinter as tk
from ui_widgets import _AnimatedCanvas, ScalarTween, mix_color
from ui_theme import ACCENT, TEXT, MUTED, GREEN, BORDER

INTERVALS = (60,) + tuple(range(1800, 86401, 1800))

def interval_index(seconds):
    value = float(seconds)
    if not math.isfinite(value):
        raise ValueError("Interval must be finite")
    index = bisect_left(INTERVALS, value)
    if index == 0:
        return 0
    if index == len(INTERVALS):
        return index - 1
    return index - 1 if value - INTERVALS[index - 1] < INTERVALS[index] - value else index

def displayed_interval(actual, index, position):
    if index == interval_index(actual) and abs(index-position) < 1e-6:
        return float(actual)
    return INTERVALS[index]

@dataclass(frozen=True)
class WheelPose:
    y: float
    scale: float
    opacity: float

def wheel_pose(distance, height):
    """Continuous projection of seven values around a virtual cylinder."""
    distance, height = float(distance), float(height)
    if not math.isfinite(distance) or not math.isfinite(height) or height <= 0:
        raise ValueError("Perspective coordinates must be finite")
    angle = min(1.42, max(-1.42, distance*.34))
    depth = max(0, math.cos(angle))
    opacity = max(0, 1-abs(distance)/3.8)**1.5
    return WheelPose(height/2+math.sin(angle)*height*.52,
                     max(.27, depth**3.4), opacity)

def snap_index(position, velocity=0):
    position, velocity = float(position), float(velocity)
    if not math.isfinite(position) or not math.isfinite(velocity):
        raise ValueError("Wheel position and velocity must be finite")
    projected = position+min(3, max(-3, velocity*.14))
    return min(len(INTERVALS)-1, max(0, round(projected)))

def clock_text(seconds):
    if seconds is None:
        return "—:—"
    seconds = float(seconds)
    if not math.isfinite(seconds):
        raise ValueError("Countdown must be finite")
    whole = max(0, math.ceil(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"

class TimeWheel(_AnimatedCanvas):
    def __init__(self, master, *, variable, formatter, command=None, width=500,
                 height=300, state="normal", **kwargs):
        if state not in ("normal", "disabled"):
            raise ValueError("Picker state must be normal or disabled")
        self.variable, self.formatter, self.command = variable, formatter, command
        self._state = state
        self._frame = self._snap_timer = self._trace = None
        self._focused = self._dragging = self._mapped = False
        self._position = ScalarTween(interval_index(variable.get()))
        self._velocity = 0.0
        self._pending_selection = False
        super().__init__(master, width=width, height=height, takefocus=int(state != "disabled"),
                         cursor="arrow" if state == "disabled" else "hand2", **kwargs)
        self._trace = variable.trace_add("write", self._changed)
        self.bind("<Map>", self._on_map, add="+")
        self.bind("<Unmap>", self._on_unmap, add="+")
        self.bind("<Configure>", lambda _: self._draw())
        self.bind("<FocusIn>", lambda _: self._focus(True))
        self.bind("<FocusOut>", lambda _: self._focus(False))
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda _: self.scroll(-1))
        self.bind("<Button-5>", lambda _: self.scroll(1))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        for key in ("Up", "Down", "Left", "Right", "Home", "End", "Prior", "Next", "Return"):
            self.bind(f"<KeyPress-{key}>", self._key)
        self._draw()

    def _release_resources(self):
        if self._trace:
            try:
                self.variable.trace_remove("write", self._trace)
            except tk.TclError:
                pass
            self._trace = None
        self._mapped = self._dragging = False
        self._frame = self._snap_timer = None

    def _on_map(self, _event=None):
        self._mapped = True
        self._draw()
        self._ensure_frame()

    def _pause(self):
        self._cancel(self._frame)
        self._cancel(self._snap_timer)
        self._frame = self._snap_timer = None

    def _on_unmap(self, _event=None):
        self._mapped = self._dragging = False
        self._pause()

    def _focus(self, focused):
        self._focused = focused
        self._draw()

    def _changed(self, *_):
        target = interval_index(self.variable.get())
        if target != self._position.target:
            self._position.retarget(target, time.monotonic(), .36,
                                    enabled=self.animations_enabled and self._mapped)
        self._draw()
        self._ensure_frame()

    def _ensure_frame(self):
        if (self._mapped and self._state != "disabled" and self.animations_enabled
                and not self._closed and self._frame is None
                and not self._position.finished(time.monotonic())):
            self._frame = self._schedule(16, self._tick)

    def _tick(self):
        self._frame = None
        if not self._mapped or self._state == "disabled":
            return
        self._draw()
        self._ensure_frame()

    def _draw(self):
        if self._closed:
            return
        width, height = self._dimensions()
        self.delete("visual")
        centre = height/2
        disabled = self._state == "disabled"
        bg = self.cget("background")
        for x, direction in ((width*.08, 1), (width*.92, -1)):
            self.create_line(x, centre, x+direction*16, centre, width=2,
                             fill=ACCENT if self._focused and not disabled else BORDER, tags="visual")
        position = self._position.value(time.monotonic())
        entries = range(max(0, math.floor(position)-4),
                        min(len(INTERVALS), math.ceil(position)+5))
        for index in sorted(entries, key=lambda i: -abs(i-position)):
            pose = wheel_pose(index-position, height)
            if pose.opacity <= .005:
                continue
            seconds = displayed_interval(self.variable.get(), index, position)
            color = mix_color(bg, MUTED if disabled else TEXT, pose.opacity)
            size = max(12, round(44*pose.scale))
            self.create_text(width/2, pose.y, text=self.formatter(seconds), fill=color,
                             font=("Segoe UI", -size, "bold" if pose.scale > .65 else "normal"), tags="visual")
        if self._focused and not disabled:
            self._shape((width*.12, centre-35, width*.88, centre+35), 35,
                        fill="", outline=mix_color(bg, ACCENT, .4), width=1, tags="visual")

    def step(self, delta):
        if self._state == "disabled":
            return "break"
        actual = float(self.variable.get())
        selected = interval_index(actual)
        target = (bisect_right(INTERVALS, actual)+delta-1 if delta > 0 else
                  bisect_left(INTERVALS, actual)+delta if delta < 0 else selected)
        target = min(len(INTERVALS)-1, max(0, target))
        if INTERVALS[target] != actual:
            self.variable.set(str(INTERVALS[target]))
            if callable(self.command):
                self.command()
        return "break"

    def scroll(self, delta):
        if self._state == "disabled":
            return "break"
        target = min(len(INTERVALS)-1, max(0, self._position.target+delta))
        self._pending_selection = True
        self._position.retarget(target, time.monotonic(), .20, enabled=self.animations_enabled)
        self._cancel(self._snap_timer)
        self._snap_timer = self._schedule(140, self._snap)
        self._draw()
        self._ensure_frame()
        return "break"

    def _wheel(self, event):
        if self._state == "disabled":
            return "break"
        return self.scroll(-event.delta/120) if event.delta else "break"

    def commit_pending(self, *, notify=False):
        self._cancel(self._snap_timer)
        self._snap_timer = None
        if self._state == "disabled":
            return float(self.variable.get())
        if not self._pending_selection:
            return float(self.variable.get())
        self._pending_selection = False
        target = snap_index(self._position.target)
        seconds = INTERVALS[target]
        changed = float(self.variable.get()) != seconds
        if changed:
            self.variable.set(str(seconds))
        if self._position.target != target:
            self._position.retarget(target, time.monotonic(), .28, enabled=self.animations_enabled)
        self._ensure_frame()
        if changed and notify and callable(self.command):
            self.command()
        if changed:
            self.event_generate("<<IntervalChanged>>", when="tail")
        return seconds

    def _snap(self):
        self._snap_timer = None
        self.commit_pending(notify=True)

    def _press(self, event):
        if self._state == "disabled":
            return "break"
        self.focus_set()
        self._pause()
        now = time.monotonic()
        self._dragging = True
        self._drag_y, self._drag_position = event.y, self._position.value(now)
        self._last_drag_time, self._last_drag_position = now, self._drag_position
        self._velocity = 0.0
        self._position.retarget(self._drag_position, now, 0)

    def _drag(self, event):
        if self._state == "disabled" or not self._dragging:
            return "break"
        _, height = self._dimensions()
        pitch = height*.52*math.sin(.34)
        target = min(len(INTERVALS)-1, max(0, self._drag_position-(event.y-self._drag_y)/pitch))
        self._pending_selection = True
        now = time.monotonic()
        dt = now-self._last_drag_time
        if dt > .001:
            speed = min(20, max(-20, (target-self._last_drag_position)/dt))
            self._velocity = .4*self._velocity+.6*speed
        self._last_drag_time, self._last_drag_position = now, target
        self._position.retarget(target, now, 0)
        self._draw()

    def _release(self, event):
        if self._state == "disabled" or not self._dragging:
            return "break"
        self._dragging = False
        self._pending_selection = True
        if abs(event.y-self._drag_y) < 4:
            _, height = self._dimensions()
            relative = min(.98, max(-.98, (event.y-height/2)/(height*.52)))
            target = snap_index(self._drag_position+math.asin(relative)/.34)
        else:
            velocity = self._velocity if time.monotonic()-self._last_drag_time < .15 else 0
            target = snap_index(self._position.target, velocity)
        self._position.retarget(target, time.monotonic(), .44, enabled=self.animations_enabled)
        self.commit_pending(notify=True)

    def _key(self, event):
        if event.keysym == "Return" and hasattr(self, "commit_pending"):
            self.commit_pending(notify=True)
            return "break"
        selected = interval_index(self.variable.get())
        delta = {"Up":-1, "Left":-1, "Down":1, "Right":1, "Prior":-2, "Next":2,
                 "Home":-selected, "End":len(INTERVALS)-1-selected}.get(event.keysym, 0)
        return self.step(delta)

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, str):
            return (cnf,cnf,cnf.title(),"",self.cget(cnf)) if cnf == "state" else super().configure(cnf)
        if cnf is None and not kwargs:
            return super().configure()
        options = dict(cnf or {}); options.update(kwargs)
        if "state" in options:
            state = options.pop("state")
            if state not in ("normal","disabled"):
                raise ValueError("Picker state must be normal or disabled")
            if state != self._state:
                self._state = state
                super().configure(takefocus=int(state != "disabled"), cursor="arrow" if state == "disabled" else "hand2")
                if state == "disabled":
                    self._dragging = False
                    self._pending_selection = False
                    self._pause()
                    self._position.retarget(interval_index(self.variable.get()), time.monotonic(), 0)
                self._draw()
        if "animations_enabled" in options:
            self.animations_enabled = bool(options.pop("animations_enabled"))
            if not self.animations_enabled:
                self._position.retarget(self._position.target, time.monotonic(), 0)
                self._cancel(self._frame); self._frame = None
                self._draw()
        if options:
            super().configure(**options)
    config = configure

    def cget(self, key):
        return self._state if key == "state" else super().cget(key)

def countdown_font_pixels(label: str, width: float, height: float) -> int:
    """Prominent clock that remains inside the dial for long hour values."""
    diameter = min(float(width), float(height)) * .68 - 18
    return max(28, min(74, int(diameter / (max(1, len(label)) * .58))))


class CountdownDial(_AnimatedCanvas):
    """Supplied seconds only. A dash indicates no scheduled deadline."""
    def __init__(self, master, *, width=540, height=420, color=ACCENT, **kwargs):
        self._seconds, self._active, self._color = None, False, color
        self._mapped, self._frame = False, None
        self._started = time.monotonic()
        super().__init__(master, width=width, height=height, takefocus=0, **kwargs)
        self.bind("<Map>", self._map, add="+")
        self.bind("<Unmap>", self._unmap, add="+")
        self.bind("<Configure>", lambda _: self._draw(), add="+")
        self._draw()

    def _map(self, _event=None):
        self._mapped = True
        self._draw(); self._ensure_frame()

    def _unmap(self, _event=None):
        self._mapped = False
        self._cancel(self._frame); self._frame = None

    def _ensure_frame(self):
        if self._mapped and self.animations_enabled and self._active and not self._closed and self._frame is None:
            self._frame = self._schedule(33,self._tick)

    def _tick(self):
        self._frame = None
        if self._mapped and self._active:
            self._draw(); self._ensure_frame()

    def _draw(self):
        if self._closed:
            return
        width,height = self._dimensions()
        cx,cy,radius = width/2,height/2,min(width,height)*.34
        bg = self.cget("background")
        self.delete("dial")
        self.create_oval(cx-radius,cy-radius,cx+radius,cy+radius,
                         outline=mix_color(bg,self._color,.17),width=1,tags="dial")
        for index in range(60):
            angle = index*math.tau/60-math.pi/2
            inner = radius-(11 if index%5 == 0 else 5)
            self.create_line(cx+math.cos(angle)*inner,cy+math.sin(angle)*inner,
                             cx+math.cos(angle)*radius,cy+math.sin(angle)*radius,
                             fill=mix_color(bg,self._color,.48 if index%5 == 0 else .16),
                             width=1.3 if index%5 == 0 else 1,tags="dial")
        phase = (time.monotonic()-self._started)*.20 if self.animations_enabled else -.6
        for offset,alpha in ((0,.85),(-.08,.5),(-.16,.2)):
            angle = phase+offset-math.pi/2
            x,y = cx+math.cos(angle)*radius,cy+math.sin(angle)*radius
            self.create_oval(x-2,y-2,x+2,y+2,fill=mix_color(bg,GREEN,alpha),outline="",tags="dial")
        label = clock_text(self._seconds)
        self.create_text(cx,cy,text=label,fill=TEXT,
                         font=("Segoe UI",-countdown_font_pixels(label,width,height),"bold"),tags="dial")

    def configure(self, cnf=None, **kwargs):
        if cnf is None and not kwargs:
            return super().configure()
        options = dict(cnf or {}); options.update(kwargs); changed = False
        for name in ("seconds","active","color","animations_enabled"):
            if name in options:
                value = options.pop(name)
                if name == "seconds":
                    clock_text(value)
                elif name in ("active","animations_enabled"):
                    value = bool(value)
                attr = "animations_enabled" if name == "animations_enabled" else "_"+name
                if getattr(self,attr) != value:
                    setattr(self,attr,value); changed = True
        if options:
            super().configure(**options); changed = True
        if not self._active or not self.animations_enabled:
            self._cancel(self._frame); self._frame = None
        if changed:
            self._draw()
        self._ensure_frame()
    config = configure
