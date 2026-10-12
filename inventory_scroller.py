"""Bounded fine-wheel controller for a positively proved inventory walk.

Native capture, wheel acknowledgement, focus, physical coordinates and activity
guards belong to the supplied callbacks. This module never sends input itself.
Initial calibration requires a unique RGB translation proof as well as the row
lattice measurement: identical tiers or cloned rows cannot bootstrap an end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import Callable

from core import GridScan
from inventory_motion_policy import InventoryMotionUnobservable, MAXIMUM_WHEEL_ROW_FRACTION
from inventory_walk import InventoryEndpointProof, InventoryWalkObservation


class InventoryScrollUnconfirmed(RuntimeError):
    """The whole inventory is unproved; do not report a completed cycle."""


class InventoryCalibrationPending(InventoryScrollUnconfirmed):
    """Healthy guarded samples cannot yet prove the wheel response.

    This is an incomplete check, never an endpoint or a completed inventory.
    A caller may inspect/transfer independently proved visible items and retry
    on its ordinary timer, without treating this limited outcome as a failure
    of the native input or window guards.
    """
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ViewportFrame:
    scan: GridScan
    geometry_identity: str
    phase: float
    row_pitch_pixels: float
    # Root can pass (recognized_viewport, actual_RGB_image) to its pure motion
    # adapters. Payload is not position evidence and is never compared/copied.
    payload: object = field(default=None, compare=False, repr=False)


def _finite(value, *, positive=False):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


class InventoryScroller:
    """Callbacks for ``run_inventory_walk``, with no unbounded input loop.

    ``sample`` returns a fresh guarded, settled frame. ``wheel`` accepts Windows
    deltas (-31 down, +31 up by default), acknowledges exactly that bounded
    wheel action under native guards, and returns its fresh frame. Exceptions
    propagate without another input. Both motion callbacks return a nonnegative
    physical distance IN the requested direction or raise for ambiguous,
    opposite or larger-than-subrow movement. ``calibrate_motion`` must prove a
    unique actual RGB translation, including rejection of whole-row aliases.

    Frame geometry identity describes the native window/physical viewport and
    stable layout, independently of scroll phase and the cropped scan grid.
    This controller preserves only measured offsets, never attempted-wheel
    counts. A transfer invalidates endpoint evidence; reset_to_top establishes
    a new zero. Calibrated response may be reused only under unchanged geometry.
    """
    def __init__(self, *, sample: Callable[[], ViewportFrame],
                 wheel: Callable[[int], ViewportFrame],
                 measure_motion: Callable[..., float],
                 calibrate_motion: Callable[..., float] | None = None,
                 check_stop: Callable[[], None], pause: Callable[[float], None],
                 wheel_delta: int = 31, max_wheel_inputs: int = 128,
                 max_seconds: float = 120., settle_seconds: float = .15,
                 clock: Callable[[], float] = time.monotonic):
        if (isinstance(wheel_delta, bool) or not isinstance(wheel_delta, int)
                or not 1 <= wheel_delta <= 53):
            raise ValueError("Inventory wheel must be a bounded fine delta")
        if (isinstance(max_wheel_inputs, bool) or not isinstance(max_wheel_inputs, int)
                or max_wheel_inputs < 1 or not _finite(max_seconds, positive=True)
                or not _finite(settle_seconds)):
            raise ValueError("Inventory scrolling bounds must be explicit and finite")
        self.sample_callback, self.wheel_callback = sample, wheel
        self.measure_motion, self.calibrate_motion = measure_motion, calibrate_motion
        self.check_stop, self.pause, self.clock = check_stop, pause, clock
        self.wheel_delta = wheel_delta
        self.max_wheel_inputs, self.max_seconds = max_wheel_inputs, max_seconds
        self.settle_seconds = settle_seconds
        self._started_at = clock()
        self._wheel_inputs = 0
        self._geometry = None
        self._pitch = None
        self._frame = None
        self._offset = None
        self._steps = {}
        self._top_proof = self._bottom_proof = None

    @property
    def wheel_inputs(self):
        return self._wheel_inputs

    def begin_cycle(self):
        """Start fresh budgets/position while retaining this live target's calibration.

        Measured response is not serialised or copied to another controller.
        Fresh samples still must match the original geometry and pitch before
        any wheel action; old top/bottom observations never establish a new end.
        """
        self.check_stop()
        self._started_at = self.clock()
        self._wheel_inputs = 0
        self.invalidate()

    def invalidate(self):
        """After a verified transfer/sort, reset position before more scanning."""
        self._frame = None
        self._offset = None
        self._top_proof = self._bottom_proof = None

    def _check(self):
        self.check_stop()
        if self.clock() - self._started_at >= self.max_seconds:
            raise InventoryScrollUnconfirmed("Inventory scrolling reached its bounded limit")

    def _validate(self, frame):
        if not isinstance(frame, ViewportFrame):
            raise InventoryScrollUnconfirmed("Inventory viewport frame is missing")
        scan = frame.scan
        if (not isinstance(scan, GridScan) or len(scan.cells) != scan.grid.count
                or scan.grid.rows < 1 or scan.grid.columns < 1
                or any(cell.index != index or cell.rect != scan.grid.cell_rect(index)
                       for index, cell in enumerate(scan.cells))):
            raise InventoryScrollUnconfirmed("Inventory viewport geometry is incomplete")
        if (not isinstance(frame.geometry_identity, str) or not frame.geometry_identity
                or not _finite(frame.phase) or not _finite(frame.row_pitch_pixels, positive=True)
                or frame.phase >= frame.row_pitch_pixels
                or frame.row_pitch_pixels > scan.grid.rect.height):
            raise InventoryScrollUnconfirmed("Inventory lattice position is unproved")
        if self._geometry is None:
            self._geometry, self._pitch = frame.geometry_identity, frame.row_pitch_pixels
        elif frame.geometry_identity != self._geometry or frame.row_pitch_pixels != self._pitch:
            self.invalidate()
            self._steps.clear()
            raise InventoryScrollUnconfirmed("Inventory geometry changed during scrolling")
        return frame

    def _sample(self):
        self._check()
        frame = self._validate(self.sample_callback())
        self.check_stop()
        return frame

    def _distance(self, before, after, direction, *, calibration=False):
        callback = self.calibrate_motion if calibration else self.measure_motion
        if callback is None:
            raise InventoryScrollUnconfirmed("Unique RGB inventory wheel calibration is unavailable")
        maximum = before.row_pitch_pixels * MAXIMUM_WHEEL_ROW_FRACTION
        distance = callback(before, after, direction=direction, maximum_step=maximum)
        if not _finite(distance) or distance >= maximum:
            raise InventoryScrollUnconfirmed("Inventory wheel motion is not a proved sub-row step")
        return float(distance)

    def _same_position(self, before, after):
        self._validate(after)
        pitch = before.row_pitch_pixels
        difference = abs(after.phase - before.phase)
        difference = min(difference, pitch - difference)
        # The viewport measurer defines <= .75 physical pixel as no motion.
        # Equality within that same limit does not establish a new endpoint.
        return difference <= .75

    def _move(self, before, direction, *, calibration_only=False):
        self._check()
        fresh = self._sample()
        if not self._same_position(before, fresh):
            self.invalidate()
            raise InventoryScrollUnconfirmed("Inventory position changed before the wheel action")
        if self._wheel_inputs >= self.max_wheel_inputs:
            raise InventoryScrollUnconfirmed("Inventory scrolling reached its bounded input limit")
        delta = -self.wheel_delta if direction == "down" else self.wheel_delta
        self._wheel_inputs += 1
        acknowledged = self._validate(self.wheel_callback(delta))
        self.check_stop()
        if acknowledged is fresh:
            raise InventoryScrollUnconfirmed("Inventory wheel did not provide a fresh acknowledged frame")
        self.pause(self.settle_seconds)
        self.check_stop()
        settled = self._sample()
        if settled is acknowledged:
            raise InventoryScrollUnconfirmed("Inventory settling did not provide a fresh frame")
        distance = self._distance(fresh, settled, direction)
        if distance == 0 and not self._same_position(fresh, settled):
            raise InventoryScrollUnconfirmed("No-motion inventory response has a changed lattice phase")
        calibrated = self._steps.get(direction)
        if calibrated is not None and distance > calibrated + .75:
            self.invalidate()
            self._steps.clear()
            raise InventoryScrollUnconfirmed("Inventory wheel response exceeds its calibrated direction step")
        if distance > 0 and direction not in self._steps:
            minimum = max(1., fresh.row_pitch_pixels * .03)
            if distance < minimum:
                # A clipped response too small to calibrate can approach a
                # limit, but it must never supply the endpoint proof itself.
                return settled, distance
            try:
                unique = self._distance(fresh, settled, direction, calibration=True)
            except InventoryMotionUnobservable:
                if not calibration_only:
                    raise
                # The provider has already ruled out strong contradictory
                # RGB motion. This phase response is not an offset, command
                # calibration, or endpoint proof: seek another fresh texture.
                return settled, distance
            if unique < minimum or abs(unique - distance) > .75:
                raise InventoryScrollUnconfirmed("Inventory RGB calibration disagrees with its lattice motion")
            self._steps[direction] = unique
        return settled, distance

    def _bootstrap(self, current):
        """Seek texture without scanning/transferring or claiming position.

        Both signed commands need independent positive RGB calibration. A
        cloned prefix may require more than one wheel, but the seek itself is
        limited to 16 acknowledged inputs / 30 seconds independently of the
        full walk's budget. The last healthy frame is retained only locally.
        """
        if self.calibrate_motion is None:
            raise InventoryScrollUnconfirmed("Unique RGB inventory wheel calibration is unavailable")
        started, initial_inputs = self.clock(), self._wheel_inputs
        direction = "down" if "down" not in self._steps else "up"
        previous_zero = None
        while "down" not in self._steps or "up" not in self._steps:
            self._check()
            if self._wheel_inputs - initial_inputs >= 16 or self.clock() - started >= 30.:
                self.invalidate()
                raise InventoryCalibrationPending("calibration_budget",
                    "Inventory wheel calibration is pending: bounded texture seek was exhausted")
            current, distance = self._move(current, direction, calibration_only=True)
            if self.clock() - started >= 30.:
                self.invalidate()
                raise InventoryCalibrationPending("calibration_budget",
                    "Inventory wheel calibration is pending: bounded texture seek was exhausted")
            if distance == 0:
                if previous_zero is not None and previous_zero != direction:
                    self.invalidate()
                    raise InventoryCalibrationPending("no_observable_motion",
                        "Inventory wheel response cannot be positively calibrated yet: "
                        "fresh acknowledged down/up commands showed no motion")
                previous_zero = direction
                # Seek away from a clamp, without calling that clamp an end.
                direction = "up" if direction == "down" else "down"
            else:
                previous_zero = None
                if "down" not in self._steps and "up" not in self._steps:
                    # A healthy but aliased prefix can hide distinct texture.
                    # Keep seeking in the observed direction; no offset exists.
                    continue
                # Once one command is proved, immediately prove the other in
                # the same area before a reset can hide that distinct texture.
                direction = "down" if "down" not in self._steps else "up"
        return current

    def _proof(self, direction):
        step = self._steps.get("up" if direction == "top" else "down")
        if step is None:
            raise InventoryScrollUnconfirmed("No calibrated wheel response proves this inventory endpoint")
        return InventoryEndpointProof("calibrated_subrow_clamp", direction, self._geometry, 2, step)

    def _observation(self, frame):
        if self._offset is None:
            raise InventoryScrollUnconfirmed("Inventory position has not been reset to a proved top")
        return InventoryWalkObservation(frame.scan, frame.geometry_identity, self._offset,
            frame.row_pitch_pixels, self._top_proof, self._bottom_proof)

    def reset_to_top(self):
        self.invalidate()
        current = self._sample()
        if "down" not in self._steps or "up" not in self._steps:
            current = self._bootstrap(current)
        clamps = 0
        while clamps < 2:
            current, distance = self._move(current, "up")
            if distance > 0:
                clamps = 0
            else:
                # A direction with no observed response cannot prove its end,
                # even when the opposite wheel direction was calibrated.
                if "up" not in self._steps:
                    raise InventoryScrollUnconfirmed("Upward inventory response has not been calibrated")
                clamps += 1
        self._offset = 0.
        self._frame = current
        self._top_proof = self._proof("top")
        return self._observation(current)

    def snapshot(self):
        if self._frame is None or self._offset is None:
            raise InventoryScrollUnconfirmed("Inventory endpoint evidence was invalidated")
        fresh = self._sample()
        if not self._same_position(self._frame, fresh):
            self.invalidate()
            raise InventoryScrollUnconfirmed("Inventory position changed without a proved wheel action")
        self._frame = fresh
        return self._observation(fresh)

    inventory_snapshot = snapshot

    def scroll_down(self, before):
        if (not isinstance(before, InventoryWalkObservation) or self._frame is None
                or self._offset is None or before.geometry_identity != self._geometry
                or before.offset_pixels != self._offset or before.row_pitch_pixels != self._pitch):
            raise InventoryScrollUnconfirmed("Inventory walk supplied a stale pre-scroll observation")
        current, distance = self._move(self._frame, "down")
        if distance == 0:
            if "down" not in self._steps:
                raise InventoryScrollUnconfirmed("Downward inventory response has not been calibrated")
            # The walk policy cannot accept a zero advance without endpoint
            # evidence. Obtain a second independently acknowledged clamp here.
            current, second = self._move(current, "down")
            distance += second
            if second == 0:
                self._bottom_proof = self._proof("bottom")
        if distance > 0:
            self._offset += distance
            self._top_proof = self._bottom_proof = None
        self._frame = current
        return self._observation(current)
