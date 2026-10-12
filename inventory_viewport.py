"""Pure recognition of complete inventory rows at an arbitrary scroll phase.

The physical viewport comes from the independently proved inventory header
transform. RGB, regular-pitch and segment guards remain those of measure_grid.
No image dimensions, item labels or cursor contents establish its geometry.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

import numpy as np
from PIL import Image

from auto_layout import GridEvidence, LayoutError, _runs, measure_grid
from core import GridSpec, Rect
from inventory_motion_policy import MAXIMUM_WHEEL_ROW_FRACTION, InventoryMotionUnobservable


class InventoryMotionUnknown(LayoutError):
    """A unique actual sub-row displacement has not been established."""


@dataclass(frozen=True)
class InventoryViewport:
    grid: GridSpec
    viewport: Rect
    row_pitch: float
    phase: float
    identity: str
    evidence: GridEvidence

    def metadata(self):
        return {"rect": list(self.viewport.as_box()), "pitch": self.row_pitch,
                "phase": self.phase, "identity": self.identity}


def _merged_runs(projection, scale):
    merged = []
    for run in _runs(projection >= .78):
        if (merged and run[0] - merged[-1][1] <= max(1, round(scale))
                and run[1] - merged[-1][0] <= math.ceil(10 * scale)):
            merged[-1] = (merged[-1][0], run[1])
        else:
            merged.append(run)
    if any(b - a > math.ceil(10 * scale) for a, b in merged):
        raise LayoutError("Inventory viewport has a broad or occluded separator")
    return merged


def _lattice(runs, length, expected, scale):
    # Clipped first/last separator centers cannot measure physical pitch; the
    # visible part is still checked by the per-segment guards below.
    complete = [(a, b) for a, b in runs if a > 0 and b < length]
    if len(complete) < 3:
        raise LayoutError("Inventory viewport cannot establish complete row pitch")
    centers = np.array([(a + b - 1) / 2 for a, b in complete])
    pitch, offset = np.polyfit(np.arange(len(centers)), centers, 1)
    if (abs(pitch - expected) > max(1.5 * scale, expected * .025)
            or np.max(np.abs(centers - (offset + pitch * np.arange(len(centers))))) > 1.8 * scale):
        raise LayoutError("Inventory viewport pitch contradicts the header transform")
    return float(pitch), float(offset % pitch)


def _visible_segments(mask, xs, ys, columns, scale):
    height, width = mask.shape
    edges = [0] + [int(round((a + b - 1) / 2)) for a, b in ys] + [height]
    for a, b in xs:
        x = (a + b - 1) // 2
        for start, end in zip(edges, edges[1:]):
            if end - start >= max(1, round(3 * scale)) and np.mean(mask[start:end, x]) < .72:
                raise LayoutError("An inventory viewport border segment is occluded")
    for a, b in ys:
        y = (a + b - 1) // 2
        for column in range(columns):
            start, end = width * column // columns, width * (column + 1) // columns
            if np.mean(mask[y, start:end]) < .72:
                raise LayoutError("An inventory viewport row separator is occluded")


def _identity(viewport, columns, scale):
    # Scrolling changes phase and the complete-row crop, not the native
    # viewport geometry. The runner also binds HWND/PID/header/monitor proof.
    return hashlib.sha256(json.dumps([viewport.as_box(), columns, scale],
        separators=(",", ":")).encode("ascii")).hexdigest()


def _raster_coordinate(value: float) -> int:
    # Complete-row input starts at the first WHOLE pixel at/after the measured
    # boundary: nearest rounding could include a fractional preceding cell.
    # Least-squares double noise around an integer uses only1e-7px precision;
    # real fit drift crossing that boundary still changes the EXACT GridSpec.
    return math.ceil(value - 1e-7)


def recognize_inventory_viewport(image: Image.Image, viewport: Rect, rows: int,
                                 columns: int, gap_color: tuple[int, int, int],
                                 scale: float) -> InventoryViewport:
    """Recognize the original full grid, or 7/8 fully visible shifted rows.

    Existing unshifted grids use measure_grid unchanged. A shifted grid needs
    all neutral separator projections and regular pitch of the WHOLE original
    viewport before a complete-row crop is accepted by the same measure_grid.
    Every active complete cell needs the original per-segment RGB proof.
    Partial clipped cells are never input targets; their animated border color
    bleed must not invalidate independently proved complete cells below them.
    Wrong-pitch/full-grid occlusion cannot be discarded by choosing a smaller
    rectangle. The method never captures or sends input.
    """
    if (not isinstance(viewport, Rect) or not viewport.fits(*image.size)
            or isinstance(rows, bool) or not isinstance(rows, int) or rows < 3
            or isinstance(columns, bool) or not isinstance(columns, int) or columns < 2
            or isinstance(scale, bool) or not isinstance(scale, (int, float))
            or not math.isfinite(scale) or not .35 <= scale <= 1.5):
        raise LayoutError("Inventory viewport geometry is unproved or clipped")
    full_grid_error = None
    try:
        evidence = measure_grid(image, viewport, rows, columns, gap_color, scale)
    except LayoutError as error:
        evidence = None
        full_grid_error = error
    if evidence is not None:
        centers = np.array([(a + b - 1) / 2 for a, b in evidence.horizontal_stripes])
        pitch, offset = np.polyfit(np.arange(rows), centers, 1)
        baseline = float((offset + 3 * scale) % pitch)
        # measure_grid's small raster-phase allowance is preserved, but an
        # actually clipped first row is not offered as a complete input cell.
        # The genuine ring-unhovered frame's complete first-row margin gives
        # .857px, and its 1.25x synthetic raster gives1.214px. This is neutral
        # chrome rounding, below the original1.8*scale separator phase bound.
        # It neither relaxes measure_grid nor authorizes an unproved wheel.
        if min(baseline, pitch - baseline) <= max(.75, 1.25 * scale):
            return InventoryViewport(GridSpec(viewport, rows, columns), viewport, float(pitch),
                                     float(offset % pitch), _identity(viewport, columns, scale), evidence)

    array = np.asarray(image.crop(viewport.as_box()).convert("RGB"), dtype=np.float32)
    mask = np.max(np.abs(array - gap_color), axis=2) <= 6
    xs = _merged_runs(np.mean(mask, axis=0), scale)
    xs = [run for run in xs if run[1] > max(2, round(4 * scale))]
    if len(xs) != columns:
        raise LayoutError("Inventory viewport columns are incomplete or occluded")
    centers = np.array([(a + b - 1) / 2 for a, b in xs])
    pitch_x, offset_x = np.polyfit(np.arange(columns), centers, 1)
    expected_x = viewport.width / columns
    if (abs(pitch_x - expected_x) > max(1.5 * scale, expected_x * .025)
            or np.max(np.abs(centers - (offset_x + pitch_x * np.arange(columns)))) > 1.8 * scale
            or abs(centers[-1] - (viewport.width - 3 * scale)) > 6 * scale):
        raise LayoutError("Inventory viewport columns contradict the header transform")
    ys = _merged_runs(np.mean(mask, axis=1), scale)
    if not rows - 1 <= len(ys) <= rows + 1:
        raise LayoutError("Inventory viewport rows are incomplete or occluded")
    pitch, phase = _lattice(ys, viewport.height, viewport.height / rows, scale)
    # Separators precede a row boundary by the original physical 3px neutral
    # tail. This phase inference does not use item identities or frame ratios.
    first = (phase + 3 * scale) % pitch
    # An independently fitted boundary near the original unshifted origin
    # means the original complete first row needs its RAW segment proof. If it
    # failed (e.g. interpolated chrome erased its gutter), dropping that row as
    # supposedly partial would hide an occlusion. Reuse the existing unshifted
    # raster tolerance; genuinely fractional half-row poses remain distinct.
    if (full_grid_error is not None
            and min(first, pitch - first) <= max(.75, 1.25 * scale)):
        raise full_grid_error
    top = _raster_coordinate(first)
    count = min(rows, math.floor((viewport.height - top + .5) / pitch))
    if count not in (rows - 1, rows):
        raise LayoutError("Inventory viewport contains too few complete rows")
    rect = Rect(viewport.left, viewport.top + top, viewport.right,
                viewport.top + top + round(count * pitch))
    if (not rect.fits(*image.size) or rect.bottom > viewport.bottom
            or rect.top < viewport.top):
        raise LayoutError("A partial inventory row cannot be an input grid")
    # All complete input rows retain the original strong per-cell border and
    # separator guards. Native animated item chrome may bleed into a clipped
    # partial cell ABOVE this crop; it is not an input cell and cannot authorize
    # any action. The full-viewport projections/lattice above remain unchanged.
    cropped = measure_grid(image, rect, count, columns, gap_color, scale)
    return InventoryViewport(GridSpec(rect, count, columns), viewport, pitch, phase,
                             _identity(viewport, columns, scale), cropped)


def measure_inventory_motion(before: InventoryViewport, after: InventoryViewport,
                             *, direction: str, maximum_step: float | None = None) -> float:
    """Measure requested-direction sub-row phase after signed RGB calibration.

    Returns positive physical scroll distance in the requested direction; zero
    means no measured movement and NEVER proves an endpoint by itself. The
    caller must previously calibrate a command, check native receipts/focus,
    and obtain two independent clamp confirmations to establish an endpoint.
    Near-half-row phase has two possible directions; initial full-RGB overlap
    must independently establish the real direction before using this measure.
    """
    if (not isinstance(before, InventoryViewport) or not isinstance(after, InventoryViewport)
            or before.identity != after.identity or before.viewport != after.viewport
            or direction not in ("up", "down")):
        raise LayoutError("Inventory motion belongs to a different viewport")
    pitch = before.row_pitch
    if abs(after.row_pitch - pitch) > max(.75, pitch * .0125):
        raise LayoutError("Inventory pitch changed during the wheel readback")
    if maximum_step is None:
        maximum_step = pitch * MAXIMUM_WHEEL_ROW_FRACTION
    if (isinstance(maximum_step, bool) or not isinstance(maximum_step, (int, float))
            or not math.isfinite(maximum_step)
            or not 0 < maximum_step <= pitch * MAXIMUM_WHEEL_ROW_FRACTION):
        raise ValueError("Inventory wheel motion exceeds the proved sub-row policy")
    delta = (after.phase - before.phase) % pitch
    if min(delta, pitch - delta) <= .75:
        return 0.0
    # A viewport phase moves up when its scroll position advances down.
    # Genuine accepted delta31 and delta15 both moved about half a row. Choose
    # the requested-direction modulo phase, not the ambiguous shortest arc.
    # The independently proved command direction is mandatory at calibration.
    advance = (-delta) % pitch if direction == "down" else delta
    if advance > maximum_step + .75:
        raise LayoutError("Inventory wheel motion is opposite or outside the calibrated sub-row bound")
    return float(advance)


def _motion_raster(array: np.ndarray) -> np.ndarray:
    """Normalize fractional native rendering for motion evidence only.

    A native Unity scroll can rerasterize every sprite at a fractional pixel
    phase: the same stationary content then differs from an integer-shifted
    snapshot. Apply the same normalized Gaussian (sigma1.5 PHYSICAL pixels,
    radius5) to every RGB pixel of both full viewports. No pointer, gutter,
    item, border or partial row is masked or omitted. Edge replication retains
    the original viewport size. Header/grid/control/fresh-item proof stays raw.
    """
    sigma = 1.5
    radius = math.ceil(3 * sigma)
    positions = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-positions * positions / (2 * sigma * sigma))
    kernel /= kernel.sum()
    result = array
    for axis in (0, 1):
        pads = [(0, 0)] * 3
        pads[axis] = (radius, radius)
        source = np.pad(result, pads, mode="edge")
        normalized = np.zeros_like(result)
        for index, weight in enumerate(kernel):
            slices = [slice(None)] * 3
            slices[axis] = slice(index, index + array.shape[axis])
            normalized += source[tuple(slices)] * weight
        result = normalized
    return result


def calibrate_inventory_motion(before_image: Image.Image, after_image: Image.Image,
                               before: InventoryViewport, after: InventoryViewport,
                               *, direction: str, pixel_step: bool = False) -> float:
    """Positively calibrate a sub-row command from unique full-RGB overlap.

    Search BOTH signs and large whole-row alternatives, retaining at least
    one row of common content. Repeated/cloned rows are explicitly ambiguous. A
    small cyclic separator-phase change by itself is not a calibration. The
    signed sub-row bound runs FIRST. A complete raw translation proof stays
    sufficient; otherwise full-viewport symmetric Gaussian normalization
    accounts for fractional native sprite rasterization.
    RMSE5 and uniqueness margin5 stay fixed. No pointer or animation mask is
    used, so the caller must first park and settle the image. All other visual
    guards use their original raw pixels and original thresholds.
    By default the legacy API returns the measured fractional phase advance.
    Initial command calibration may request pixel_step to retain the magnitude
    independently proved by RGB translation, rather than a noisy phase fit.
    """
    advance = measure_inventory_motion(before, after, direction=direction)
    if advance <= 0:
        raise LayoutError("No positive inventory motion was observed for calibration")
    if before_image.size != after_image.size or not before.viewport.fits(*before_image.size):
        raise LayoutError("Inventory calibration belongs to another native frame")
    viewport = before.viewport
    if viewport.width * viewport.height > 1_000_000:
        raise LayoutError("Inventory calibration viewport exceeds its bounded model")
    a = np.asarray(before_image.crop(viewport.as_box()).convert("RGB"), dtype=np.float32)
    b = np.asarray(after_image.crop(viewport.as_box()).convert("RGB"), dtype=np.float32)
    maximum = math.floor(viewport.height - before.row_pitch)
    if maximum < 2:
        raise LayoutError("Inventory calibration lacks overlapping complete rows")
    expected = advance if direction == "down" else -advance
    # A fully proved raw translation remains sufficient. Filtering can remove
    # the only distinguishing high-frequency texture from otherwise unique
    # rows, so an inadequate normalized score cannot invalidate raw evidence.
    # Every route independently needs the same whole-range alias proof.
    for normalized in (False, True):
        if normalized:
            a, b = _motion_raster(a), _motion_raster(b)
        matches = []
        for displacement in range(-maximum, maximum + 1):
            if displacement > 0:
                first, second = a[displacement:], b[:-displacement]
            elif displacement < 0:
                first, second = a[:displacement], b[-displacement:]
            else:
                first, second = a, b
            difference = first - second
            error = float(np.sqrt(np.mean(difference * difference)))
            matches.append((error, displacement))
        matches.sort()
        score, displacement = matches[0]
        alternatives = [error for error, shift in matches if abs(shift - displacement) > 2]
        if score <= 5 and min(alternatives, default=math.inf) - score >= 5:
            agreement = .75 if pixel_step else 1.5
            if abs(displacement - expected) <= agreement:
                return float(abs(displacement)) if pixel_step else advance
            # Filtering must never overturn already strong raw evidence of an
            # opposite direction or a true whole-row alternative displacement.
            raise LayoutError("Inventory pixel motion contradicts the measured sub-row phase")
    # The healthy, positive phase passed the geometry/direction/step guards,
    # but neither route proved a unique real displacement. Repeated rows or
    # insufficient texture are an unavailable calibration, not a contradictory
    # observation. Strong unique contradictions above remain fatal LayoutError.
    raise InventoryMotionUnobservable("Inventory wheel calibration lacks unique full-RGB texture evidence")


def prove_inventory_translation(before_image: Image.Image, after_image: Image.Image,
                                before_viewport: InventoryViewport,
                                after_viewport: InventoryViewport,
                                direction: str, *, pixel_step: bool = False) -> float:
    """Keep healthy texture-unavailable motion distinct from fatal evidence.

    InventoryMotionUnobservable intentionally propagates unchanged; the bounded
    calibration seeker may handle it. Geometry/sign/whole-row contradictions
    remain fatal InventoryMotionUnknown and cannot authorize another attempt.
    """
    try:
        return calibrate_inventory_motion(before_image, after_image, before_viewport,
                                          after_viewport, direction=direction, pixel_step=pixel_step)
    except LayoutError as error:
        raise InventoryMotionUnknown(str(error)) from error
