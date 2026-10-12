"""Small dark Tk widgets. Animation never performs application or game actions.

All controls accept ``animations_enabled=False`` for reduced motion. Their
callbacks use the Tk event loop and are cancelled when the widget is destroyed.
Canvas controls remain ordinary Tk widgets: pack/grid, bind and focus_set work.
"""
from __future__ import annotations

import math
import time
import tkinter as tk


BACKGROUND = "#10131d"
SURFACE = "#191e2c"
ACCENT = "#9b8afb"
TEXT = "#eff1f7"
MUTED = "#9199ad"
FONT = ("Segoe UI", 10)


def clamp_unit(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Animation progress must be finite.")
    return min(1.0, max(0.0, value))


def ease_out_cubic(value: float) -> float:
    return 1.0 - (1.0 - clamp_unit(value)) ** 3


def _rgb(color: str) -> tuple[int, int, int]:
    if not isinstance(color, str) or len(color) != 7 or not color.startswith("#"):
        raise ValueError("Use a #rrggbb color.")
    try:
        return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
    except ValueError:
        raise ValueError("Use a #rrggbb color.") from None


def mix_color(start: str, end: str, progress: float) -> str:
    """Interpolate sRGB channels without overshooting either endpoint."""
    fraction = clamp_unit(progress)
    channels = (round(a + (b - a) * fraction) for a, b in zip(_rgb(start), _rgb(end)))
    return "#" + "".join(f"{channel:02x}" for channel in channels)


class ScalarTween:
    """Time-based transition that can be retargeted smoothly mid-animation."""
    def __init__(self, value: float):
        self.start = self.target = float(value)
        if not math.isfinite(self.start):
            raise ValueError("Tween value must be finite.")
        self.started_at = 0.0
        self.duration = 0.0

    def value(self, now: float) -> float:
        if self.duration <= 0:
            return self.target
        fraction = ease_out_cubic((now - self.started_at) / self.duration)
        return self.start + (self.target - self.start) * fraction

    def finished(self, now: float) -> bool:
        return self.duration <= 0 or now >= self.started_at + self.duration

    def retarget(self, target: float, now: float, duration: float = 0.18,
                 *, enabled: bool = True):
        target, now, duration = float(target), float(now), float(duration)
        if not all(math.isfinite(v) for v in (target, now, duration)) or duration < 0:
            raise ValueError("Tween values must be finite and duration nonnegative.")
        self.start = self.value(now)
        self.target = target
        self.started_at = now
        self.duration = duration if enabled else 0.0


def _background(master, explicit):
    if explicit is not None:
        return explicit
    try:
        return master.cget("background")
    except tk.TclError:
        return BACKGROUND


def _rounded_points(left, top, right, bottom, radius):
    radius = max(0, min(radius, (right - left) / 2, (bottom - top) / 2))
    points = []
    # Real circular corners, including a semicircular cap at half-height.
    # A sparse spline polygon produces visibly squared corners on wide pills.
    for cx, cy, first in ((right-radius, top+radius, -90),
                          (right-radius, bottom-radius, 0),
                          (left+radius, bottom-radius, 90),
                          (left+radius, top+radius, 180)):
        for step in range(13):
            angle = math.radians(first + step * 90 / 12)
            points.extend((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return tuple(points)


def _options(cnf, kwargs):
    result = dict(cnf or {})
    result.update(kwargs)
    return result


class _AnimatedCanvas(tk.Canvas):
    def __init__(self, master, *, animations_enabled=True, bg=None, **kwargs):
        self.animations_enabled = bool(animations_enabled)
        self._closed = False
        self._timers = set()
        super().__init__(master, background=_background(master, bg),
                         highlightthickness=0, borderwidth=0, **kwargs)
        self.bind("<Destroy>", self._destroy_event, add="+")

    def _schedule(self, delay, callback):
        if self._closed:
            return None
        token = None

        def run():
            self._timers.discard(token)
            if not self._closed:
                callback()

        token = self.after(delay, run)
        self._timers.add(token)
        return token

    def _cancel(self, token):
        if token is not None:
            self._timers.discard(token)
            try:
                self.after_cancel(token)
            except tk.TclError:
                pass

    def _release_resources(self):
        pass

    def _cleanup(self):
        if self._closed:
            return
        self._closed = True
        for token in tuple(self._timers):
            self._cancel(token)
        self._release_resources()

    def _destroy_event(self, event):
        if event.widget is self:
            self._cleanup()

    def destroy(self):
        self._cleanup()
        super().destroy()

    def _dimensions(self):
        width, height = self.winfo_width(), self.winfo_height()
        return (float(self.cget("width")) if width <= 1 else width,
                float(self.cget("height")) if height <= 1 else height)

    def _inside(self, event):
        width, height = self._dimensions()
        return 0 <= event.x < width and 0 <= event.y < height

    def _shape(self, bounds, radius, **kwargs):
        return self.create_polygon(_rounded_points(*bounds, radius),
                                   smooth=False, **kwargs)


class AnimatedReveal(_AnimatedCanvas):
    """Animated scene that fully leaves the pack layout when collapsed."""
    def __init__(self, master, *, height, revealed=True, width=540, **kwargs):
        self.body_height = int(height)
        self._revealed = bool(revealed)
        self._reveal = ScalarTween(float(bool(revealed)))
        self._frame = None
        self._mapped = False
        self._pack_options = None
        self._pack_previous = self._pack_next = None
        self._layout_hidden = False
        super().__init__(master, width=width, height=height if revealed else 1, takefocus=0, **kwargs)
        self.body = tk.Frame(self, background=self.cget("background"), borderwidth=0)
        self._window = self.create_window(0, 0, anchor="nw", window=self.body,
                                          width=width, height=self.body_height)
        self.bind("<Configure>", lambda _: self._draw(), add="+")
        self.bind("<Map>", self._map, add="+")
        self.bind("<Unmap>", self._unmap, add="+")

    def pack(self, cnf=None, **kwargs):
        super().pack_configure(cnf or {}, **kwargs)
        self._layout_hidden = False
        self._draw()

    def _conceal_layout(self):
        # Native Tk child windows can expose pixels beyond a one-pixel Canvas.
        # Hide the window item as well as removing the Canvas from the layout.
        self.itemconfigure(self._window, state="hidden")
        if self.winfo_manager() == "pack":
            self._pack_options = dict(self.pack_info())
            siblings = self.master.pack_slaves()
            index = siblings.index(self)
            self._pack_previous = siblings[index-1] if index else None
            self._pack_next = siblings[index+1] if index+1 < len(siblings) else None
            self.pack_forget()
            self._layout_hidden = True
        self._mapped = False
        self._cancel(self._frame)
        self._frame = None

    def _restore_layout(self):
        self.itemconfigure(self._window, state="normal")
        if self._layout_hidden and self._pack_options is not None:
            options = dict(self._pack_options)
            for widget, direction in ((self._pack_next, "before"),
                                      (self._pack_previous, "after")):
                if widget is not None and widget.winfo_exists() and widget.winfo_manager() == "pack":
                    options[direction] = widget
                    break
            super().pack_configure(**options)
            self._layout_hidden = False

    def _parent_mapped(self):
        return bool(self.master.winfo_ismapped())

    def _map(self, _event=None):
        self._mapped = True
        self._tick()

    def _unmap(self, _event=None):
        self._mapped = False
        self._cancel(self._frame)
        self._frame = None

    def _draw(self):
        if self._closed:
            return
        progress = self._reveal.value(time.monotonic())
        requested = max(1, round(self.body_height * progress))
        if int(float(super().cget("height"))) != requested:
            super().configure(height=requested)
        width, _ = self._dimensions()
        self.itemconfigure(self._window, width=width)
        self.coords(self._window, 0, -18 * (1-progress))
        if not self._revealed and progress == 0:
            self._conceal_layout()

    def _tick(self):
        self._frame = None
        self._draw()
        if self._mapped and not self._reveal.finished(time.monotonic()):
            self._frame = self._schedule(16, self._tick)

    def configure(self, cnf=None, **kwargs):
        if cnf is None and not kwargs:
            return super().configure()
        options = _options(cnf, kwargs)
        if "revealed" in options:
            value = bool(options.pop("revealed"))
            if value != self._revealed:
                self._revealed = value
                if value:
                    self._restore_layout()
                self._reveal.retarget(float(value), time.monotonic(), .30,
                                      enabled=self.animations_enabled and
                                      (self._mapped or self._parent_mapped()))
                self._cancel(self._frame)
                self._tick()
        if "animations_enabled" in options:
            self.animations_enabled = bool(options.pop("animations_enabled"))
            if not self.animations_enabled:
                self._reveal.retarget(float(self._revealed), time.monotonic(), 0)
                self._cancel(self._frame); self._frame = None
                self._draw()
        if options:
            super().configure(**options)

    config = configure


class RoundedCard(_AnimatedCanvas):
    """Rounded surface with ``body``/``content`` Frame for normal child widgets.

    ``bg`` colors the inner surface; ``background`` colors the outer corners.
    With no explicit height, the card follows its body's requested height.
    """
    def __init__(self, master, *, bg=SURFACE, background=None, radius=18,
                 padding=18, width=400, height=None, **kwargs):
        self.surface = bg
        self.radius = radius
        self.padding = padding
        self._auto_height = height is None
        self._layout_timer = None
        super().__init__(master, bg=background, width=width,
                         height=height or 2 * padding + 1, takefocus=0, **kwargs)
        self._panel = self._shape((0, 0, width, height or 2 * padding + 1), radius,
                                  fill=bg, outline="#252b3c", width=1)
        self.body = tk.Frame(self, background=bg, borderwidth=0, highlightthickness=0)
        self.content = self.body
        self._window = self.create_window(padding, padding, anchor="nw", window=self.body)
        self.bind("<Configure>", self._layout, add="+")
        self.body.bind("<Configure>", self._request_layout, add="+")
        self._request_layout()

    def _request_layout(self, _event=None):
        if self._layout_timer is None and not self._closed:
            self._layout_timer = self._schedule(0, self._fit)

    def _fit(self):
        self._layout_timer = None
        if self._auto_height:
            requested = self.body.winfo_reqheight() + 2 * self.padding
            if int(float(self.cget("height"))) != requested:
                super().configure(height=requested)
        self._layout()

    def _layout(self, _event=None):
        if self._closed:
            return
        width, height = self._dimensions()
        self.coords(self._panel, *_rounded_points(1, 1, width - 1, height - 1, self.radius))
        self.coords(self._window, self.padding, self.padding)
        self.itemconfigure(self._window, width=max(1, width - 2 * self.padding))
        if not self._auto_height:
            self.itemconfigure(self._window, height=max(1, height - 2 * self.padding))


class AnimatedButton(_AnimatedCanvas):
    """Keyboard-accessible button; ``invoke`` is inert while disabled."""
    def __init__(self, master, *, text="", command=None, state="normal",
                 width=180, height=46, variant="primary", font=("Segoe UI", 10, "bold"),
                 radius=24, bg=None, selected=False, animations_enabled=True, **kwargs):
        if variant not in {"primary", "secondary", "ghost"}:
            raise ValueError("Unknown button variant.")
        self._text, self._command = text, command
        self._state = "normal"
        self._selected = bool(selected)
        self._variant, self._font, self.radius = variant, font, radius
        self._hover = self._pressed = self._key_pressed = self._focused = False
        self._visual_timer = None
        self._phase = ScalarTween(1)
        self._press_motion = ScalarTween(0)
        super().__init__(master, bg=bg, animations_enabled=animations_enabled,
                         width=width, height=height, takefocus=1, cursor="hand2", **kwargs)
        self._colors = self._target_colors()
        self._origin_colors = self._colors
        self._destination_colors = self._colors
        for event, handler in (("<Enter>", self._enter), ("<Leave>", self._leave),
                               ("<ButtonPress-1>", self._mouse_down),
                               ("<ButtonRelease-1>", self._mouse_up),
                               ("<FocusIn>", self._focus_in), ("<FocusOut>", self._focus_out),
                               ("<KeyPress-space>", self._key_down), ("<KeyPress-Return>", self._key_down),
                               ("<KeyRelease-space>", self._key_up), ("<KeyRelease-Return>", self._key_up)):
            self.bind(event, handler)
        self.bind("<Configure>", lambda _event: self._draw())
        self.configure(state=state)

    def _target_colors(self):
        if self._state == "disabled":
            return "#262c3c", "#707b92"
        fills = {
            "primary": (ACCENT, "#b0a0ff", "#8271de"),
            "secondary": (("#353047", "#423950", "#29253e") if self._selected else
                          ("#252c3e", "#323b50", "#202637")),
            "ghost": (("#2b2639", "#353047", "#29253e") if self._selected else
                      (self.cget("background"), "#252c3e", "#202637")),
        }[self._variant]
        return fills[2 if self._pressed else 1 if self._hover else 0], (
            "#111522" if self._variant == "primary" else TEXT)

    def _draw(self):
        if self._closed:
            return
        width, height = self._dimensions()
        self.delete("visual")
        press = self._press_motion.value(time.monotonic())
        inset = 1 + press * 2
        self._shape((inset, 1 + press, width - inset, height - 1 - press), self.radius,
                    fill=self._colors[0],
                    outline=ACCENT if self._selected and self._state != "disabled" else "",
                    width=1, tags="visual")
        if self._focused and self._state != "disabled":
            self._shape((4, 4, width - 4, height - 4), max(1, self.radius - 3),
                        fill="", outline="#d2c9ff", width=1.5, tags="visual")
        self.create_text(width / 2, height / 2 + press, text=self._text,
                         fill=self._colors[1], font=self._font, tags="visual")

    def _animate(self):
        destination = self._target_colors()
        if hasattr(self, "_press_motion") and self._press_motion.target != float(self._pressed):
            self._press_motion.retarget(float(self._pressed), time.monotonic(), .12,
                                        enabled=self.animations_enabled)
        if not self.animations_enabled:
            self._cancel(self._visual_timer)
            self._visual_timer = None
            self._colors = self._destination_colors = destination
            self._draw()
            return
        if destination == self._destination_colors:
            self._draw()
            return
        self._cancel(self._visual_timer)
        self._visual_timer = None
        self._origin_colors = self._colors
        self._destination_colors = destination
        if self._colors == self._destination_colors:
            self._draw()
            return
        self._phase = ScalarTween(0)
        self._phase.retarget(1, time.monotonic(), 0.15, enabled=self.animations_enabled)
        self._tick()

    def _tick(self):
        self._visual_timer = None
        now = time.monotonic()
        fraction = self._phase.value(now)
        self._colors = tuple(mix_color(a, b, fraction)
                             for a, b in zip(self._origin_colors, self._destination_colors))
        self._draw()
        if not self._phase.finished(now) or not self._press_motion.finished(now):
            self._visual_timer = self._schedule(16, self._tick)

    def _enter(self, _event):
        self._hover = True
        self._animate()

    def _leave(self, _event):
        self._hover = self._pressed = False
        self._animate()

    def _mouse_down(self, _event):
        if self._state != "disabled":
            self.focus_set()
            self._pressed = True
            self._animate()

    def _mouse_up(self, event):
        armed = self._pressed and self._inside(event)
        self._pressed = False
        self._animate()
        if armed:
            self.invoke()

    def _key_down(self, _event):
        if self._state != "disabled" and not self._key_pressed:
            self._key_pressed = self._pressed = True
            self._animate()
        return "break"

    def _key_up(self, _event):
        armed = self._key_pressed
        self._key_pressed = self._pressed = False
        self._animate()
        if armed:
            self.invoke()
        return "break"

    def _focus_in(self, _event):
        self._focused = True
        self._draw()

    def _focus_out(self, _event):
        self._focused = self._pressed = self._key_pressed = False
        self._animate()

    def invoke(self):
        if self._state != "disabled" and callable(self._command):
            return self._command()

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, str):
            if cnf in {"text", "command", "state", "font", "variant", "selected", "animations_enabled"}:
                return (cnf, cnf, cnf.title(), "", self.cget(cnf))
            return super().configure(cnf)
        if cnf is None and not kwargs:
            return super().configure()
        options = _options(cnf, kwargs)
        if "state" in options:
            state = options.pop("state")
            if state not in {"normal", "disabled"}:
                raise ValueError("Button state must be normal or disabled.")
            self._state = state
            if state == "disabled":
                self._pressed = self._key_pressed = False
            super().configure(takefocus=int(state != "disabled"),
                              cursor="arrow" if state == "disabled" else "hand2")
        for name, attribute in (("text", "_text"), ("command", "_command"), ("font", "_font")):
            if name in options:
                setattr(self, attribute, options.pop(name))
        if "variant" in options:
            variant = options.pop("variant")
            if variant not in {"primary", "secondary", "ghost"}:
                raise ValueError("Unknown button variant.")
            self._variant = variant
        if "selected" in options:
            self._selected = bool(options.pop("selected"))
        if "animations_enabled" in options:
            self.animations_enabled = bool(options.pop("animations_enabled"))
        if options:
            super().configure(**options)
        self._animate()

    config = configure

    def cget(self, key):
        attributes = {"text": "_text", "command": "_command", "state": "_state",
                      "font": "_font", "variant": "_variant", "selected": "_selected",
                      "animations_enabled": "animations_enabled"}
        return getattr(self, attributes[key]) if key in attributes else super().cget(key)


class AnimatedSwitch(_AnimatedCanvas):
    """BooleanVar-backed switch. Programmatic changes animate without commands."""
    def __init__(self, master, *, variable=None, command=None, text="", state="normal",
                 enabled=None, width=None, height=30, bg=None, animations_enabled=True, **kwargs):
        self.variable = variable if variable is not None else tk.BooleanVar(master, value=False)
        self._command, self._text = command, text
        self._state = "normal"
        self._focused = self._pressed = False
        self._trace = None
        self._visual_timer = None
        self._position = ScalarTween(float(bool(self.variable.get())))
        super().__init__(master, bg=bg, animations_enabled=animations_enabled,
                         width=width or (280 if text else 54), height=height,
                         takefocus=1, cursor="hand2", **kwargs)
        self._watch_variable()
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<FocusIn>", lambda _event: self._focus(True))
        self.bind("<FocusOut>", lambda _event: self._focus(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        for key in ("space", "Return"):
            self.bind(f"<KeyPress-{key}>", self._press)
            self.bind(f"<KeyRelease-{key}>", self._release)
        if enabled is not None:
            state = "normal" if enabled else "disabled"
        self.configure(state=state)

    def _watch_variable(self):
        self._trace = self.variable.trace_add("write", self._variable_changed)

    def _release_resources(self):
        if self._trace is not None:
            try:
                self.variable.trace_remove("write", self._trace)
            except tk.TclError:
                pass
            self._trace = None

    def _variable_changed(self, *_args):
        target = float(bool(self.variable.get()))
        now = time.monotonic()
        if target == self._position.target and (self.animations_enabled or self._position.finished(now)):
            self._draw()
            return
        self._position.retarget(target, now, enabled=self.animations_enabled)
        self._cancel(self._visual_timer)
        self._tick()

    def _tick(self):
        self._visual_timer = None
        now = time.monotonic()
        self._draw()
        if not self._position.finished(now):
            self._visual_timer = self._schedule(16, self._tick)

    def _draw(self):
        if self._closed:
            return
        width, height = self._dimensions()
        self.delete("visual")
        track_width, track_height = min(48, width - 6), min(26, height - 4)
        left, top = 3, (height - track_height) / 2
        position = clamp_unit(self._position.value(time.monotonic()))
        fill = mix_color("#343c50", ACCENT, position)
        disabled = self._state == "disabled"
        if disabled:
            fill = mix_color(fill, self.cget("background"), 0.55)
        self._shape((left, top, left + track_width, top + track_height), track_height / 2,
                    fill=fill, outline="#d2c9ff" if self._focused and not disabled else "", tags="visual")
        radius = max(1, track_height / 2 - 3)
        x = left + track_height / 2 + position * (track_width - track_height)
        y = height / 2
        self.create_oval(x - radius, y - radius, x + radius, y + radius,
                         fill=MUTED if disabled else TEXT, outline="", tags="visual")
        if self._text:
            self.create_text(left + track_width + 12, y, text=self._text, anchor="w",
                             fill=MUTED if disabled else TEXT, font=FONT, tags="visual")

    def _focus(self, focused):
        self._focused = focused
        if not focused:
            self._pressed = False
        self._draw()

    def _press(self, _event):
        if self._state != "disabled":
            self.focus_set()
            self._pressed = True
        return "break"

    def _release(self, event):
        armed = self._pressed
        self._pressed = False
        if armed and (getattr(event, "keysym", "") in {"space", "Return"} or self._inside(event)):
            self.invoke()
        return "break"

    def invoke(self):
        if self._state == "disabled":
            return
        self.variable.set(not bool(self.variable.get()))
        if callable(self._command):
            return self._command()

    def get(self):
        return bool(self.variable.get())

    def set(self, value):
        self.variable.set(bool(value))

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, str):
            if cnf in {"state", "variable", "command", "text", "enabled", "animations_enabled"}:
                return (cnf, cnf, cnf.title(), "", self.cget(cnf))
            return super().configure(cnf)
        if cnf is None and not kwargs:
            return super().configure()
        options = _options(cnf, kwargs)
        if "enabled" in options:
            options["state"] = "normal" if options.pop("enabled") else "disabled"
        if "state" in options:
            state = options.pop("state")
            if state not in {"normal", "disabled"}:
                raise ValueError("Switch state must be normal or disabled.")
            self._state = state
            if state == "disabled":
                self._pressed = False
            super().configure(takefocus=int(state != "disabled"), cursor="arrow" if state == "disabled" else "hand2")
        if "variable" in options:
            self._release_resources()
            self.variable = options.pop("variable")
            self._watch_variable()
        for name, attribute in (("text", "_text"), ("command", "_command")):
            if name in options:
                setattr(self, attribute, options.pop(name))
        if "animations_enabled" in options:
            self.animations_enabled = bool(options.pop("animations_enabled"))
        if options:
            super().configure(**options)
        self._variable_changed()

    config = configure

    def cget(self, key):
        if key == "enabled":
            return self._state != "disabled"
        attributes = {"state": "_state", "variable": "variable", "command": "_command",
                      "text": "_text", "animations_enabled": "animations_enabled"}
        return getattr(self, attributes[key]) if key in attributes else super().cget(key)


class StatusOrbit(_AnimatedCanvas):
    """Subtle status orbit; inactive/unmapped/reduced-motion states have no timer."""
    def __init__(self, master, *, active=False, color=ACCENT, size=28, bg=None,
                 animations_enabled=True, **kwargs):
        _rgb(color)
        self._active, self._color = bool(active), color
        self._orbit_timer = None
        self._started_at = time.monotonic()
        super().__init__(master, bg=bg, animations_enabled=animations_enabled,
                         width=size, height=size, takefocus=0, **kwargs)
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Map>", lambda _event: self._restart())
        self.bind("<Unmap>", lambda _event: self._pause())
        self._restart()

    def _pause(self):
        self._cancel(self._orbit_timer)
        self._orbit_timer = None

    def _restart(self):
        self._pause()
        self._tick()

    def _tick(self):
        self._orbit_timer = None
        self._draw()
        if self._active and self.animations_enabled and self.winfo_ismapped():
            self._orbit_timer = self._schedule(40, self._tick)

    def _draw(self):
        if self._closed:
            return
        width, height = self._dimensions()
        x, y = width / 2, height / 2
        radius = max(2, min(width, height) / 2 - 4)
        phase = ((time.monotonic() - self._started_at) / 2.8) % 1 if self._active and self.animations_enabled else 0
        self.delete("visual")
        self.create_oval(x - radius, y - radius, x + radius, y + radius,
                         outline=mix_color(self._color, self.cget("background"), 0.78), width=1.5, tags="visual")
        if self._active:
            self.create_arc(x - radius, y - radius, x + radius, y + radius,
                            start=90 - 360 * phase, extent=85, style="arc", outline=self._color,
                            width=2, tags="visual")
        dot = 3.0 + (0.65 * math.sin(phase * 2 * math.pi) if self._active else 0)
        self.create_oval(x - dot, y - dot, x + dot, y + dot,
                         fill=self._color, outline="", tags="visual")

    def start(self):
        self.configure(active=True)

    def stop(self):
        self.configure(active=False)

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, str):
            if cnf in {"active", "color", "animations_enabled"}:
                return (cnf, cnf, cnf.title(), "", self.cget(cnf))
            return super().configure(cnf)
        if cnf is None and not kwargs:
            return super().configure()
        options = _options(cnf, kwargs)
        if "active" in options:
            self._active = bool(options.pop("active"))
        if "color" in options:
            color = options.pop("color")
            _rgb(color)
            self._color = color
        if "animations_enabled" in options:
            self.animations_enabled = bool(options.pop("animations_enabled"))
        if options:
            super().configure(**options)
        self._restart()

    config = configure

    def cget(self, key):
        attributes = {"active": "_active", "color": "_color", "animations_enabled": "animations_enabled"}
        return getattr(self, attributes[key]) if key in attributes else super().cget(key)


class SegmentedControl(_AnimatedCanvas):
    """StringVar selector, supporting readonly/normal/disabled and arrow keys."""
    def __init__(self, master, *, values, variable=None, textvariable=None, command=None,
                 state="readonly", width=536, height=44, bg=None,
                 animations_enabled=True, **kwargs):
        self._values = tuple(values)
        if not self._values or not all(isinstance(v, str) for v in self._values):
            raise ValueError("Segment values must be nonempty strings.")
        self.variable = variable if variable is not None else textvariable
        if self.variable is None:
            self.variable = tk.StringVar(master, value=self._values[0])
        self._command, self._state = command, "readonly"
        self._focused = False
        self._pressed_index = None
        self._visual_timer = self._trace = None
        value = self.variable.get()
        self._position = ScalarTween(self._values.index(value) if value in self._values else 0)
        super().__init__(master, bg=bg, animations_enabled=animations_enabled,
                         width=width, height=height, takefocus=1, cursor="hand2", **kwargs)
        self._trace = self.variable.trace_add("write", self._variable_changed)
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<FocusIn>", lambda _event: self._focus(True))
        self.bind("<FocusOut>", lambda _event: self._focus(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        for key in ("Left", "Right", "Home", "End", "space", "Return"):
            self.bind(f"<KeyPress-{key}>", self._key)
        self.configure(state=state)

    def _release_resources(self):
        if self._trace is not None:
            try:
                self.variable.trace_remove("write", self._trace)
            except tk.TclError:
                pass
            self._trace = None

    def _variable_changed(self, *_args):
        value = self.variable.get()
        if value not in self._values:
            self._cancel(self._visual_timer)
            self._visual_timer = None
            self._draw()
            return
        target = self._values.index(value)
        now = time.monotonic()
        if target == self._position.target and (self.animations_enabled or self._position.finished(now)):
            self._draw()
            return
        self._position.retarget(target, now, 0.20, enabled=self.animations_enabled)
        self._cancel(self._visual_timer)
        self._tick()

    def _tick(self):
        self._visual_timer = None
        now = time.monotonic()
        self._draw()
        if not self._position.finished(now):
            self._visual_timer = self._schedule(16, self._tick)

    def _draw(self):
        if self._closed:
            return
        width, height = self._dimensions()
        self.delete("visual")
        disabled = self._state == "disabled"
        self._shape((1, 1, width - 1, height - 1), 12, fill="#111622",
                    outline=ACCENT if self._focused and not disabled else "#2b3245", width=1, tags="visual")
        segment_width = (width - 8) / len(self._values)
        value = self.variable.get()
        if value in self._values:
            position = min(len(self._values) - 1, max(0, self._position.value(time.monotonic())))
            left = 4 + segment_width * position
            self._shape((left, 4, left + segment_width, height - 4), 9,
                        fill="#252a3c" if disabled else "#353047", outline="", tags="visual")
        for index, label in enumerate(self._values):
            selected = label == value
            self.create_text(4 + segment_width * (index + 0.5), height / 2,
                             text=label, fill=MUTED if disabled or not selected else TEXT,
                             font=("Segoe UI", 10, "bold" if selected else "normal"), tags="visual")

    def _focus(self, focused):
        self._focused = focused
        if not focused:
            self._pressed_index = None
        self._draw()

    def _index_at(self, event):
        if not self._inside(event):
            return None
        width, _height = self._dimensions()
        return min(len(self._values) - 1, int(event.x * len(self._values) / width))

    def _press(self, event):
        if self._state != "disabled":
            self.focus_set()
            self._pressed_index = self._index_at(event)

    def _release(self, event):
        index = self._index_at(event)
        if index is not None and index == self._pressed_index:
            self.invoke(index)
        self._pressed_index = None

    def _key(self, event):
        if self._state == "disabled":
            return "break"
        value = self.variable.get()
        index = self._values.index(value) if value in self._values else 0
        if event.keysym == "Left":
            index = (index - 1) % len(self._values)
        elif event.keysym == "Right":
            index = (index + 1) % len(self._values)
        elif event.keysym == "Home":
            index = 0
        elif event.keysym == "End":
            index = len(self._values) - 1
        self.invoke(index)
        return "break"

    def invoke(self, index=None):
        if self._state == "disabled":
            return
        if index is None:
            current = self.variable.get()
            index = self._values.index(current) if current in self._values else 0
        value = self._values[index]
        changed = self.variable.get() != value
        self.variable.set(value)
        if changed:
            self.event_generate("<<ComboboxSelected>>")
            if callable(self._command):
                return self._command()

    def get(self):
        return self.variable.get()

    def set(self, value):
        if value not in self._values:
            raise ValueError("Unknown segment value.")
        self.variable.set(value)

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, str):
            if cnf in {"state", "variable", "textvariable", "command", "values", "animations_enabled"}:
                return (cnf, cnf, cnf.title(), "", self.cget(cnf))
            return super().configure(cnf)
        if cnf is None and not kwargs:
            return super().configure()
        options = _options(cnf, kwargs)
        if "state" in options:
            state = options.pop("state")
            if state not in {"normal", "readonly", "disabled"}:
                raise ValueError("Segment state must be readonly, normal or disabled.")
            self._state = state
            if state == "disabled":
                self._pressed_index = None
            super().configure(takefocus=int(state != "disabled"), cursor="arrow" if state == "disabled" else "hand2")
        if "values" in options:
            values = tuple(options.pop("values"))
            if not values or not all(isinstance(v, str) for v in values):
                raise ValueError("Segment values must be nonempty strings.")
            self._values = values
        if "variable" in options or "textvariable" in options:
            self._release_resources()
            self.variable = options.pop("variable", options.pop("textvariable", None))
            self._trace = self.variable.trace_add("write", self._variable_changed)
        if "command" in options:
            self._command = options.pop("command")
        if "animations_enabled" in options:
            self.animations_enabled = bool(options.pop("animations_enabled"))
        if options:
            super().configure(**options)
        self._variable_changed()

    config = configure

    def cget(self, key):
        attributes = {"state": "_state", "variable": "variable", "textvariable": "variable",
                      "command": "_command", "values": "_values", "animations_enabled": "animations_enabled"}
        return getattr(self, attributes[key]) if key in attributes else super().cget(key)


ActivityRing = StatusOrbit
