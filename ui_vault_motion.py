"""Decorative vault motion. No transfer counts, application callbacks or game IO.

``VaultMotion(..., active=False, color=ACCENT, animations_enabled=True)``
accepts configure(active=..., color=..., state='normal'/'disabled',
animations_enabled=...). Hidden, disabled and reduced-motion widgets schedule
no frames. All positions use monotonic elapsed time rather than frame counts.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

from ui_widgets import (ACCENT, BACKGROUND, ScalarTween, _AnimatedCanvas,
                        _options, _rgb, clamp_unit, mix_color)


CHIP_COLORS = ("#78a9fb", "#ef8e9e", "#b593fb", "#e8c978", "#78a9fb", "#b593fb")
FRAME_MS = 33


def smoothstep(value):
    value = clamp_unit(value)
    return value * value * (3 - 2 * value)


@dataclass(frozen=True)
class ChipPose:
    x: float
    y: float
    angle: float
    scale: float
    opacity: float


def chip_pose(elapsed, index, active=0.0):
    """Stable ambient orbit blended into staggered inward motion while active."""
    elapsed = float(elapsed)
    if not math.isfinite(elapsed):
        raise ValueError("Elapsed time must be finite.")
    if not isinstance(index, int) or not 0 <= index < len(CHIP_COLORS):
        raise ValueError("Unknown decorative chip.")
    elapsed = max(0, elapsed)
    blend = clamp_unit(active)
    angle = index * math.tau / len(CHIP_COLORS) + 0.3 + elapsed * 0.095
    x = math.cos(angle) * (155 + (index % 2) * 23)
    y = math.sin(angle) * 43 + math.sin(elapsed * 0.7 + index) * 3
    phase = (elapsed / 3.8 + index / len(CHIP_COLORS)) % 1
    inward = smoothstep((phase - 0.18) / 0.68)
    # Disappear at the centre before reappearing at the orbit boundary. This is
    # visual activity, never a claim that a particular item was transferred.
    visibility = smoothstep(phase / 0.12) * (1 - smoothstep((phase - 0.83) / 0.17))
    contraction = 1 - blend * inward
    return ChipPose(x * contraction, y * contraction,
                    0.17 * math.sin(elapsed * 0.4 + index),
                    1 - blend * inward * 0.65,
                    1 - blend + blend * visibility)


def rotated_square(x, y, radius, angle):
    points = []
    for dx, dy in ((-radius, -radius), (radius, -radius), (radius, radius), (-radius, radius)):
        points.extend((x + dx * math.cos(angle) - dy * math.sin(angle),
                       y + dx * math.sin(angle) + dy * math.cos(angle)))
    return points


class VaultMotion(_AnimatedCanvas):
    def __init__(self, master, *, width=520, height=150, active=False,
                 color=ACCENT, state="normal", bg=None, animations_enabled=True,
                 **kwargs):
        _rgb(color)
        if state not in {"normal", "disabled"}:
            raise ValueError("Vault state must be normal or disabled.")
        self._active, self._color, self._state = bool(active), color, state
        self._activity = ScalarTween(float(bool(active)))
        self._started_at = time.monotonic()
        self._frame_timer = None
        self._mapped = False
        super().__init__(master, width=width, height=height, takefocus=0,
                         bg=bg, animations_enabled=animations_enabled, **kwargs)
        self.bind("<Map>", self._map_event, add="+")
        self.bind("<Unmap>", self._unmap_event, add="+")
        self.bind("<Configure>", lambda _event: self._draw(), add="+")
        self._draw()

    def _motion_allowed(self):
        return (not self._closed and self._mapped and self.animations_enabled
                and self._state == "normal")

    def _map_event(self, _event=None):
        if self._closed:
            return
        self._mapped = True
        self._draw()
        self._ensure_frame()

    def _unmap_event(self, _event=None):
        self._mapped = False
        self._pause()

    def _pause(self):
        self._cancel(self._frame_timer)
        self._frame_timer = None

    def _ensure_frame(self):
        if self._motion_allowed() and self._frame_timer is None:
            self._frame_timer = self._schedule(FRAME_MS, self._tick)

    def _tick(self):
        self._frame_timer = None
        if not self._motion_allowed():
            return
        self._draw()
        self._ensure_frame()

    def _release_resources(self):
        self._frame_timer = None
        self._mapped = False

    def _draw(self):
        if self._closed:
            return
        width, height = self._dimensions()
        scale = max(0.05, min(width / 520, height / 150))
        cx, cy = width / 2, height / 2 + 3 * scale
        now = time.monotonic()
        moving = self.animations_enabled and self._state == "normal"
        elapsed = now - self._started_at if moving else 0
        activity = self._activity.value(now) if moving else float(self._active)
        bg = self.cget("background")
        accent = self._color if self._state == "normal" else mix_color(bg, self._color, 0.3)
        self.delete("vault")

        def line(points, color, stroke=1, **options):
            converted = []
            for x, y in zip(points[::2], points[1::2]):
                converted.extend((cx + x * scale, cy + y * scale))
            return self.create_line(*converted, fill=color, width=stroke * scale,
                                    tags="vault", **options)

        def oval(bounds, **options):
            l, t, r, b = bounds
            return self.create_oval(cx + l * scale, cy + t * scale,
                                    cx + r * scale, cy + b * scale,
                                    tags="vault", **options)

        def poly(points, **options):
            converted = []
            for x, y in zip(points[::2], points[1::2]):
                converted.extend((cx + x * scale, cy + y * scale))
            return self.create_polygon(*converted, tags="vault", **options)

        # Fine orbital architecture, tiny stars and low-contrast light pools.
        for radius, alpha in ((58, .055), (50, .07), (43, .10)):
            oval((-radius, -radius * .68, radius, radius * .68),
                 fill=mix_color(bg, accent, alpha), outline="")
        for a, b in ((207, 56), (182, 46)):
            oval((-a, -b, a, b), outline=mix_color(bg, accent, .10), width=scale)
        for index in range(18):
            a = index * 2.39996
            x = math.cos(a) * (214 + index % 3 * 6)
            y = math.sin(a) * 56
            radius = .8 + .45 * (1 + math.sin(elapsed * .45 + index))
            oval((x - radius, y - radius, x + radius, y + radius),
                 fill=mix_color(bg, accent, .18), outline="")

        # Animated token gathering remains behind the vault face.
        for index, color in enumerate(CHIP_COLORS):
            pose = chip_pose(elapsed, index, activity)
            if activity > .02 and pose.opacity > .05:
                for trail in (1, 2, 3):
                    previous = chip_pose(max(0, elapsed - .055 * trail), index, activity)
                    radius = (1.7 - .3 * trail) * scale
                    x, y = cx + previous.x * scale, cy + previous.y * scale
                    self.create_oval(x - radius, y - radius, x + radius, y + radius,
                                     fill=mix_color(bg, color, .13 * pose.opacity),
                                     outline="", tags="vault")
            if pose.opacity < .015:
                continue
            color = mix_color(bg, color, pose.opacity * (.82 if self._state == "normal" else .3))
            points = rotated_square(pose.x, pose.y, 9.5 * pose.scale, pose.angle)
            poly(points, fill=mix_color(bg, color, .12), outline=mix_color(bg, color, .55), width=1.2 * scale)
            line((pose.x - 3 * pose.scale, pose.y, pose.x + 3 * pose.scale, pose.y), color, 2)
            line((pose.x, pose.y - 3 * pose.scale, pose.x, pose.y + 3 * pose.scale), color, 2)

        # Machined, chamfered vault: two perspective faces and a branded door.
        oval((-46, 34, 49, 44), fill=mix_color(bg, "#000000", .22), outline="")
        poly((-39, -32, -23, -44, 43, -44, 30, -32),
             fill="#2a2d43", outline=mix_color(bg, accent, .38), width=scale)
        poly((30, -32, 43, -44, 43, 28, 30, 40),
             fill="#171b2a", outline=mix_color(bg, accent, .25), width=scale)
        poly((-39, -24, -31, -32, 22, -32, 30, -24, 30, 32, 22, 40, -31, 40, -39, 32),
             fill="#202535", outline=mix_color(bg, accent, .64), width=1.4 * scale)
        poly((-32, -20, -26, -26, 17, -26, 23, -20, 23, 26, 17, 32, -26, 32, -32, 26),
             fill="#141927", outline="#343d53", width=scale)
        line((-28, -23, 17, -23), mix_color(bg, "#eff1f7", .20))
        line((-34, -16, -34, 23), mix_color(bg, "#eff1f7", .12))
        # Original W geometry; no text label or simulated transfer number.
        line((-22, -9, -15, 16, -5, -2, 5, 16, 13, -9),
             mix_color(bg, accent, .96), 3.4, capstyle="round", joinstyle="round")
        for y in (-13, 20):
            line((26, y, 31, y), mix_color(bg, "#eff1f7", .38), 2.2)
        pulse = .56 + activity * (.12 + .1 * math.sin(elapsed * 2.2))
        oval((-8, 23, -4, 27), fill=mix_color(bg, accent, pulse), outline="")

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, str):
            if cnf in {"active", "color", "state", "animations_enabled"}:
                return (cnf, cnf, cnf.title(), "", self.cget(cnf))
            return super().configure(cnf)
        if cnf is None and not kwargs:
            return super().configure()
        options = _options(cnf, kwargs)
        changed = False
        if "color" in options:
            color = options.pop("color")
            _rgb(color)
            changed |= color != self._color
            self._color = color
        if "state" in options:
            state = options.pop("state")
            if state not in {"normal", "disabled"}:
                raise ValueError("Vault state must be normal or disabled.")
            changed |= state != self._state
            self._state = state
        if "animations_enabled" in options:
            enabled = bool(options.pop("animations_enabled"))
            changed |= enabled != self.animations_enabled
            self.animations_enabled = enabled
        if "active" in options:
            active = bool(options.pop("active"))
            if active != self._active:
                self._active = active
                self._activity.retarget(float(active), time.monotonic(), .55,
                                        enabled=self.animations_enabled)
                changed = True
        if options:
            super().configure(**options)
            changed = True
        if changed:
            if not self._motion_allowed():
                self._pause()
            self._draw()
            self._ensure_frame()

    config = configure

    def cget(self, key):
        attributes = {"active": "_active", "color": "_color", "state": "_state",
                      "animations_enabled": "animations_enabled"}
        return getattr(self, attributes[key]) if key in attributes else super().cget(key)
