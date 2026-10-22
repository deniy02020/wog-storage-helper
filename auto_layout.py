"""Pure, bounded visual layout adaptation; never captures, inputs or writes.

Scale is established by a visible header and independently checked against
the repeated neutral separators of *each* item grid. RGB template tolerances
remain those of the fixed-scale recognizer. A resized saved frame is a test
of interpolation, not evidence of a real game UI scale being supported.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from PIL import Image

from core import Rect, GridSpec, image_rmse


DEFAULT_SCALES = tuple(round(0.35 + i * 0.025, 4) for i in range(47))


class LayoutError(RuntimeError):
    """Insufficient or contradictory visible evidence; no layout is adopted."""


class GridEvidenceError(LayoutError):
    """Both panel headers were proved, but their item grid was not."""


@dataclass(frozen=True)
class ScaleMatch:
    rect: Rect
    scale: float
    rmse: float
    margin: float
    # Physical-pixel sampling phase, independent of translation/scale.
    phase: tuple[float, float] = (0.0, 0.0)
    variant_id: str | None = None
    # A full-source resize and a fractional phase box can differ even with
    # phase zero, when nominal scaled dimensions round to physical pixels.
    renderer: str = "resize"


@dataclass(frozen=True)
class GridEvidence:
    pitch_x: float
    pitch_y: float
    vertical_stripes: tuple[tuple[int, int], ...]
    horizontal_stripes: tuple[tuple[int, int], ...]


def _scales(values: Iterable[float]) -> tuple[float, ...]:
    values = tuple(values)
    if not values or len(values) > 200 or any(
            isinstance(s, bool) or not isinstance(s, (int, float))
            or not math.isfinite(s) or not 0.35 <= s <= 1.50 for s in values):
        raise ValueError("Scales must be finite values in the bounded range 0.35..1.50")
    return tuple(sorted(set(values)))


def scaled_sample(sample: Image.Image, scale: float) -> Image.Image:
    size = (max(1, round(sample.width * scale)), max(1, round(sample.height * scale)))
    return sample.convert("RGB") if size == sample.size else sample.convert("RGB").resize(
        size, Image.Resampling.BILINEAR)


def adapted_sample(sample, scale, *, physical_size=None, phase=(0.0, 0.0)):
    """Render a canonical sample at an explicitly proved scale/sampling phase.

    ``physical_size`` covers separately rounded affine rectangle endpoints.
    This helper is also suitable for small sort buttons and page markers.
    """
    _scales((scale,))
    if len(phase) != 2 or any(not math.isfinite(p) or abs(p) > 1.0 for p in phase):
        raise ValueError("Sampling phase must be bounded to one physical pixel")
    if phase == (0.0, 0.0) and physical_size is None:
        return scaled_sample(sample, scale)
    width, height = physical_size or (max(1, round(sample.width * scale)), max(1, round(sample.height * scale)))
    if (width < 1 or height < 1 or abs(width - sample.width * scale) > 1.1
            or abs(height - sample.height * scale) > 1.1):
        raise ValueError("Physical sample dimensions contradict the proved scale")
    # PIL's bilinear reducer includes antialiasing. A plain cv2.remap only
    # interpolates four pixels and is not equivalent at 50% game UI scale.
    padding = 8
    padded = Image.fromarray(np.pad(np.asarray(sample.convert("RGB")),
                                   ((padding, padding), (padding, padding), (0, 0)), mode="edge"))
    left, top = padding - phase[0] / scale, padding - phase[1] / scale
    box = (left, top, left + width / scale, top + height / scale)
    return padded.resize((width, height), Image.Resampling.BILINEAR, box=box)


def _phase_sample(sample, scale, phase):
    return adapted_sample(sample, scale, phase=phase)


def _header_sample(sample, scale, physical_size, phase, renderer):
    """Repeat exactly the raster hypothesis whose RGB score was accepted."""
    if renderer == "resize":
        rendered = scaled_sample(sample, scale)
        if phase != (0.0, 0.0) or rendered.size != physical_size:
            raise ValueError("Resize rendering contradicts adopted phase or dimensions")
        return rendered
    if renderer == "phasebox":
        return adapted_sample(sample, scale, physical_size=physical_size, phase=phase)
    raise ValueError("Unknown header renderer")


def control_error(image, physical_rect, sample, canonical_rect, canonical_anchor,
                  adapted_anchor, scale, anchor_phase=(0.0, 0.0)):
    """Strict forward-rendered RGB error at a header-affine control rectangle.

    An endpoint rounded a pixel differently is handled in the sample render,
    never by moving the click rectangle or widening a recognition threshold.
    """
    for rect in (physical_rect, canonical_rect, canonical_anchor, adapted_anchor):
        if not isinstance(rect, Rect):
            raise TypeError("Control geometry must use Rect")
    if sample.size != (canonical_rect.width, canonical_rect.height):
        raise LayoutError("Control sample dimensions differ from canonical geometry")
    expected = affine_rect(canonical_rect, canonical_anchor, ScaleMatch(adapted_anchor, scale, 0, 0))
    if expected != physical_rect or not physical_rect.fits(*image.size):
        raise LayoutError("Control geometry differs from its proved header transform")
    delta_x = (canonical_rect.left - canonical_anchor.left) * scale
    delta_y = (canonical_rect.top - canonical_anchor.top) * scale
    phase = (anchor_phase[0] + delta_x - round(delta_x),
             anchor_phase[1] + delta_y - round(delta_y))
    rendered = adapted_sample(sample, scale, physical_size=(physical_rect.width, physical_rect.height), phase=phase)
    score = image_rmse(image.crop(physical_rect.as_box()), rendered)
    fingerprint = hashlib.sha256(sample.convert("RGB").tobytes()).hexdigest()
    for spec in load_ui_variants():
        if spec["kind"] != "controls" or spec["canonical_pixels_sha256"] != fingerprint:
            continue
        relative_scale = scale / spec["canonical_scale"]
        if not 0.35 <= relative_scale <= 1.50:
            continue
        source, source_anchor = Rect(*spec["source_rect"]), Rect(*spec["source_anchor_rect"])
        variant_phase = (anchor_phase[0] + (source.left - source_anchor.left) * relative_scale - round(delta_x),
                         anchor_phase[1] + (source.top - source_anchor.top) * relative_scale - round(delta_y))
        if any(abs(p) > 1 for p in variant_phase):
            continue
        try:
            rendered = adapted_sample(spec["sample"], relative_scale,
                                      physical_size=(physical_rect.width, physical_rect.height), phase=variant_phase)
        except ValueError:
            continue
        score = min(score, image_rmse(image.crop(physical_rect.as_box()), rendered))
    return score


def _same_object(a: Rect, b: Rect) -> bool:
    return (abs(a.center[0] - b.center[0]) < min(a.width, b.width) * 0.5
            and abs(a.center[1] - b.center[1]) < min(a.height, b.height) * 0.5)


def _match_template_family(image: Image.Image, sample: Image.Image, *,
                     scales=DEFAULT_SCALES, max_rmse=10, min_margin=5) -> ScaleMatch | None:
    """Coarse grayscale search, local RGB refinement, spatial uniqueness guard.

    Nearby scales at one location are one object. Distinct plausible objects
    must still be separated by the original RGB margin. No frame-size ratio
    participates in the scale estimate.
    """
    scales = _scales(scales)
    if min(sample.size) < 3 or not 0 <= max_rmse <= 20 or min_margin <= 0:
        raise ValueError("Invalid template or matching thresholds")
    image = image.convert("RGB")
    reduction = min(1.0, 640 / max(image.size))
    coarse_size = tuple(max(1, round(n * reduction)) for n in image.size)
    frame = np.asarray(image.resize(coarse_size, Image.Resampling.BILINEAR).convert("L"),
                       dtype=np.float32)
    proposals = []
    for scale in scales:
        size = tuple(max(1, round(n * scale * reduction)) for n in sample.size)
        if size[0] > frame.shape[1] or size[1] > frame.shape[0]:
            continue
        template = np.asarray(sample.convert("L").resize(size, Image.Resampling.BILINEAR),
                              dtype=np.float32)
        errors = cv2.matchTemplate(frame, template, cv2.TM_SQDIFF)
        for _ in range(3):
            score, _, point, _ = cv2.minMaxLoc(errors)
            if not math.isfinite(score):
                break
            x, y = point
            full = Rect(round(x / reduction), round(y / reduction),
                        round(x / reduction) + round(sample.width * scale),
                        round(y / reduction) + round(sample.height * scale))
            proposals.append((score / template.size, scale, full))
            errors[max(0, y - size[1] + 1):y + size[1],
                   max(0, x - size[0] + 1):x + size[0]] = np.inf
    # Retain distinct locations so an exact duplicate is never hidden by the
    # many scale hypotheses of the first object.
    locations = []
    for _, scale, rect in sorted(proposals, key=lambda p: p[0]):
        if not any(_same_object(rect, other[1]) for other in locations):
            locations.append((scale, rect))
        if len(locations) == 8:
            break
    matches = []
    full_frame = np.asarray(image, dtype=np.float32)
    for initial_scale, initial_rect in locations:
        # An exact native-variant scale may be inserted between regular coarse
        # steps. Its tiny gap must not shrink every local refinement interval.
        step = max((b - a for a, b in zip(scales, scales[1:])), default=0)
        fine_scales = {initial_scale}
        if step:
            fine_scales.update(s for s in scales if abs(s - initial_scale) <= step + 1e-8)
            fine_scales.update(round(initial_scale + step * i / 10, 5) for i in range(-10, 11)
                               if scales[0] <= initial_scale + step * i / 10 <= scales[-1])
        best = None
        local_matches = []
        for scale in sorted(fine_scales):
            template_image = scaled_sample(sample, scale)
            tw, th = template_image.size
            # Scale changes are pinned at the candidate center, then a small
            # translation is searched at full resolution.
            cx, cy = initial_rect.center
            pad = math.ceil(3 / reduction)
            left, top = max(0, cx - tw // 2 - pad), max(0, cy - th // 2 - pad)
            right, bottom = min(image.width, cx + tw - tw // 2 + pad), min(image.height, cy + th - th // 2 + pad)
            if right - left < tw or bottom - top < th:
                continue
            errors = cv2.matchTemplate(full_frame[top:bottom, left:right],
                                       np.asarray(template_image, dtype=np.float32), cv2.TM_SQDIFF)
            _, _, point, _ = cv2.minMaxLoc(errors)
            x, y = left + point[0], top + point[1]
            rect = Rect(x, y, x + tw, y + th)
            score = image_rmse(image.crop(rect.as_box()), template_image)
            # Prefer exact canonical scale when pixel dimensions/scores tie.
            rank = (score, abs(scale - 1.0))
            if best is None or rank < best[0]:
                best = (rank, scale, rect, (0.0, 0.0), "resize")
            local_matches.append((rank, scale, rect))
        # Scaling a whole panel can place the saved header at a fractional
        # physical pixel. Test bounded sampling phases rather than increasing
        # the RGB error threshold. This is still one spatial object.
        if best is not None and 0.1 < best[0][0] <= max_rmse + 15:
            for _, scale, candidate_rect in sorted(local_matches, key=lambda value: value[0]):
                dimensions = {(round(sample.width * scale), round(sample.height * scale))}
                if scale <= 0.6:
                    dimensions.update((w, h) for w in (math.floor(sample.width * scale), math.ceil(sample.width * scale))
                                      for h in (math.floor(sample.height * scale), math.ceil(sample.height * scale)))
                for px in (-0.5, -0.25, 0.0, 0.25, 0.5):
                    for py in (-0.5, -0.25, 0.0, 0.25, 0.5):
                        for size in dimensions:
                            template_image = adapted_sample(sample, scale, physical_size=size, phase=(px, py))
                            tw, th = template_image.size
                            left, top = max(0, candidate_rect.left - 1), max(0, candidate_rect.top - 1)
                            right, bottom = min(image.width, left + tw + 2), min(image.height, top + th + 2)
                            if right - left < tw or bottom - top < th:
                                continue
                            errors = cv2.matchTemplate(full_frame[top:bottom, left:right],
                                                       np.asarray(template_image, dtype=np.float32), cv2.TM_SQDIFF)
                            _, _, point, _ = cv2.minMaxLoc(errors)
                            x, y = left + point[0], top + point[1]
                            rect = Rect(x, y, x + tw, y + th)
                            score = image_rmse(image.crop(rect.as_box()), template_image)
                            rank = (score, abs(scale - 1.0))
                            if rank < best[0]:
                                best = (rank, scale, rect, (px, py), "phasebox")
        if best is not None:
            matches.append((best[0][0], best[1], best[2], best[3], best[4]))
    if not matches:
        return None
    matches.sort(key=lambda value: value[0])
    best, scale, rect, phase, renderer = matches[0]
    if best > max_rmse:
        return None
    alternatives = [score for score, _, other, _, _ in matches[1:] if not _same_object(rect, other)]
    margin = min(alternatives, default=math.inf) - best
    # Coarse proposals bound work, not uniqueness. A final complete RGB scan
    # at the proved scale/phase cannot miss a second object of that rendering
    # merely because many grayscale decoys exhausted the proposal budget.
    final_template = _header_sample(sample, scale, (rect.width, rect.height), phase, renderer)
    exact_errors = cv2.matchTemplate(full_frame, np.asarray(final_template, dtype=np.float32), cv2.TM_SQDIFF)
    x, y = rect.left, rect.top
    exact_errors[max(0, y - rect.height // 2):min(exact_errors.shape[0], y + rect.height // 2 + 1),
                 max(0, x - rect.width // 2):min(exact_errors.shape[1], x + rect.width // 2 + 1)] = np.inf
    if np.isfinite(exact_errors).any():
        _, _, other, _ = cv2.minMaxLoc(exact_errors)
        crop = image.crop((other[0], other[1], other[0] + rect.width, other[1] + rect.height))
        margin = min(margin, image_rmse(crop, final_template) - best)
    if margin < min_margin:
        raise LayoutError("Interface element is visually ambiguous")
    return ScaleMatch(rect, scale, best, margin, phase, renderer=renderer)


def _variant_assets(directory=None):
    if directory is not None:
        return Path(directory).resolve()
    from paths import resource_path
    return resource_path("preset_assets").resolve()


@lru_cache(maxsize=4)
def _load_ui_variants(directory, modified, size):
    directory = Path(directory)
    manifest = json.loads((directory / "adaptive_ui.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != 1:
        raise LayoutError("Unsupported UI raster variant manifest")
    result = []
    identifiers = set()
    for kind in ("headers", "controls"):
        records = manifest.get(kind, [])
        if not isinstance(records, list) or len(records) > 32:
            raise LayoutError("UI variant manifest exceeds its bounded sample count")
        for record in records:
            if not isinstance(record, dict):
                raise LayoutError("UI variant record must be an object")
            spec = dict(record)
            identifier, name = spec.get("id"), spec.get("image")
            if (not isinstance(identifier, str) or not identifier or identifier in identifiers
                    or not isinstance(name, str) or Path(name).name != name or "/" in name
                    or "\\" in name or Path(name).suffix.lower() != ".png"):
                raise LayoutError("UI variant identity or filename is invalid")
            path = (directory / name).resolve()
            if not path.is_relative_to(directory) or not path.is_file() or path.stat().st_size > 100_000:
                raise LayoutError("UI variant sample is missing or outside its directory")
            if hashlib.sha256(path.read_bytes()).hexdigest() != spec.get("sample_sha256"):
                raise LayoutError("UI variant sample hash differs from its provenance")
            scale = spec.get("canonical_scale")
            _scales((scale,))
            if spec.get("pane") not in ("inventory", "storage"):
                raise LayoutError("UI variant must name its panel")
            for key in ("source_rect", "source_anchor_rect"):
                value = spec.get(key)
                if (not isinstance(value, list) or len(value) != 4
                        or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in value)
                        or Rect(*value).width < 1 or Rect(*value).height < 1):
                    raise LayoutError("UI variant source rectangle is invalid")
            with Image.open(path) as image:
                if (image.size != (Rect(*spec["source_rect"]).width, Rect(*spec["source_rect"]).height)
                        or image.width > 300 or image.height > 100):
                    raise LayoutError("UI variant must remain a small exact source crop")
                spec["sample"] = image.convert("RGB")
            if (not isinstance(spec.get("canonical_pixels_sha256"), str)
                    or len(spec["canonical_pixels_sha256"]) != 64):
                raise LayoutError("UI variant canonical pixel fingerprint is missing")
            spec["kind"] = kind
            result.append(spec)
            identifiers.add(identifier)
    return tuple(result)


def load_ui_variants(directory=None):
    """Read bounded, hash-checked builtin tiny assets, never private captures."""
    directory = _variant_assets(directory)
    path = directory / "adaptive_ui.json"
    if not path.exists():
        return ()
    info = path.stat()
    if info.st_size > 64_000:
        raise LayoutError("UI variant manifest is too large")
    return _load_ui_variants(str(directory), info.st_mtime_ns, info.st_size)


def match_multiscale(image: Image.Image, sample: Image.Image, *,
                     scales=DEFAULT_SCALES, max_rmse=10, min_margin=5) -> ScaleMatch | None:
    """Match canonical rendering and approved genuine raster variants.

    The returned scale always relates to canonical geometry. ``variant_id``
    records which exact builtin raster must be checked on the next frame.
    No variant is selected by client size or manually supplied coordinates.
    """
    scales = _scales(scales)
    matches = []
    match = _match_template_family(image, sample, scales=scales,
                                   max_rmse=max_rmse, min_margin=min_margin)
    if match is not None:
        matches.append(match)
    fingerprint = hashlib.sha256(sample.convert("RGB").tobytes()).hexdigest()
    for spec in load_ui_variants():
        if spec["canonical_pixels_sha256"] != fingerprint:
            continue
        base = spec["canonical_scale"]
        relative_scales = set(s / base for s in scales if 0.35 <= s / base <= 1.50)
        if scales[0] <= base <= scales[-1]:
            relative_scales.add(1.0)
        if not relative_scales:
            continue
        found = _match_template_family(image, spec["sample"], scales=relative_scales,
                                       max_rmse=max_rmse, min_margin=min_margin)
        if found is not None:
            actual_scale = found.scale * base
            matches.append(ScaleMatch(found.rect, actual_scale, found.rmse, found.margin,
                                      found.phase, spec["id"], found.renderer))
    if not matches:
        return None
    matches.sort(key=lambda m: (m.rmse, m.variant_id is not None))
    best = matches[0]
    others = [m.rmse for m in matches[1:] if not _same_object(best.rect, m.rect)]
    margin = min(best.margin, min(others, default=math.inf) - best.rmse)
    if margin < min_margin:
        raise LayoutError("Different approved raster variants match distinct objects")
    return ScaleMatch(best.rect, best.scale, best.rmse, margin, best.phase, best.variant_id, best.renderer)


def _runs(mask) -> list[tuple[int, int]]:
    indices = np.flatnonzero(mask)
    return [(int(run[0]), int(run[-1]) + 1) for run in
            np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1) if len(run)]


def _gap_color(samples: list[Image.Image]) -> tuple[int, int, int]:
    """Derive the bright neutral separator from the real saved empty cells."""
    colors = []
    for sample in samples:
        array = np.asarray(sample.convert("RGB"))
        values, counts = np.unique(array.reshape(-1, 3), axis=0, return_counts=True)
        for color, count in zip(values, counts):
            if count >= array.shape[0] * array.shape[1] * 0.04 and int(color.max()) - int(color.min()) <= 20:
                colors.append((float(np.mean(color)), tuple(int(c) for c in color)))
    if not colors:
        raise LayoutError("Empty-cell samples do not establish a neutral grid separator")
    return max(colors)[1]


def measure_grid(image: Image.Image, rect: Rect, rows: int, columns: int,
                 gap_color: tuple[int, int, int], scale: float, *, allow_inventory_halo=False) -> GridEvidence:
    """Require every repeated separator in both axes, regular pitch and phase.

    Gap evidence is independent of the header. Uniform panels, wrong-pitch
    grids and common occlusion fail even if a header still matches exactly.
    The small phase allowance covers the fractional raster pitch visible in
    the existing native captures; it is not a classifier tolerance change.
    """
    if rows < 2 or columns < 2 or not rect.fits(*image.size):
        raise LayoutError("Grid is cropped or cannot establish repeated pitch")
    array = np.asarray(image.crop(rect.as_box()).convert("RGB"), dtype=np.float32)
    mask = np.max(np.abs(array - gap_color), axis=2) <= 6
    try:
        return _measure_grid_mask(mask, rect, rows, columns, scale)
    except LayoutError:
        if not allow_inventory_halo:
            raise
        from inventory_halo import prove_inventory_halo_mask
        proved = prove_inventory_halo_mask(image, rect, rows, columns, gap_color, scale, mask)
        return _measure_grid_mask(proved, rect, rows, columns, scale)


def _measure_grid_mask(mask, rect, rows, columns, scale):
    """Unchanged full lattice and EVERY segment check for either pixel proof."""
    axes = []
    for projection, count, length in ((np.mean(mask, axis=0), columns, rect.width),
                                      (np.mean(mask, axis=1), rows, rect.height)):
        runs = _runs(projection >= 0.78)
        merged = []
        for run in runs:
            # A single antialiased pixel can split one separator; it cannot
            # merge two different cell boundaries at the supported pitches.
            if (merged and run[0] - merged[-1][1] <= max(1, round(scale))
                    and run[1] - merged[-1][0] <= math.ceil(10 * scale)):
                merged[-1] = (merged[-1][0], run[1])
            else:
                merged.append(run)
        runs = merged
        # A one-pixel leading separator belongs to the preceding cell. Broad
        # neutral rectangles are not repeated cell borders.
        runs = [run for run in runs if run[1] > max(2, round(4 * scale))]
        if len(runs) != count or any(b - a > math.ceil(10 * scale) for a, b in runs):
            raise LayoutError("Visible grid separators are incomplete or occluded")
        centers = np.array([(a + b - 1) / 2 for a, b in runs])
        slope, offset = np.polyfit(np.arange(count), centers, 1)
        expected = length / count
        if (abs(slope - expected) > max(1.5 * scale, expected * 0.025)
                or np.max(np.abs(centers - (offset + slope * np.arange(count)))) > 1.8 * scale
                or abs(centers[-1] - (length - 3 * scale)) > 6 * scale):
            raise LayoutError("Visible cell pitch contradicts header geometry")
        axes.append((runs, float(slope)))
    xs, ys = axes[0][0], axes[1][0]
    # Validate each stripe segment, so a popup hiding one row or column cannot
    # be averaged away by the rest of a long border.
    for a, b in xs:
        # An item halo may cover one edge pixel of a separator at any native
        # scale while its neighbouring pixel remains independently visible.
        # Require ONE common observed line through EVERY segment, never pick a
        # different pixel per cell or manufacture separator evidence.
        lines = range(a, b)
        if not any(all(np.mean(mask[rect.height*r//rows:rect.height*(r+1)//rows, x]) >= .72
                       for r in range(rows)) for x in lines):
            raise LayoutError("A cell-border segment is occluded")
    for a, b in ys:
        lines = range(a, b)
        if not any(all(np.mean(mask[y, rect.width*c//columns:rect.width*(c+1)//columns]) >= .72
                       for c in range(columns)) for y in lines):
            raise LayoutError("A cell-border segment is occluded")
    return GridEvidence(axes[0][1], axes[1][1], tuple(xs), tuple(ys))


def affine_rect(rect: Rect, original_anchor: Rect, match: ScaleMatch) -> Rect:
    def x(value):
        return match.rect.left + round((value - original_anchor.left) * match.scale)
    def y(value):
        return match.rect.top + round((value - original_anchor.top) * match.scale)
    return Rect(x(rect.left), y(rect.top), x(rect.right), y(rect.bottom))


class AutoLayout:
    """Adapt independent panel geometry in memory after visual evidence."""
    def __init__(self, profile, directory, *, scales=DEFAULT_SCALES):
        self.profile = copy.deepcopy(profile)
        self.directory = Path(directory).resolve()
        self.scales = _scales(scales)
        if len(profile.get("anchors", [])) != 2:
            raise ValueError("Two independent panel headers are required")
        self.headers = [self._image(a["image"]) for a in profile["anchors"]]
        self.variants = {spec["id"]: spec for spec in load_ui_variants()}
        self.gaps = {pane: _gap_color([self._image(name) for name in profile[pane + "_empty"]])
                     for pane in ("inventory", "storage")}

    def _image(self, name):
        path = (self.directory / name).resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError("Sample must stay inside its directory")
        with Image.open(path) as image:
            return image.convert("RGB")

    def locate_headers(self, image):
        from neutral_panels import locate, supports
        found = []
        for pane, sample in zip(("inventory", "storage"), self.headers):
            match = match_multiscale(image, sample, scales=self.scales)
            if match is None and supports(sample, pane):
                match = locate(image, sample, pane, self.scales)
            found.append(match)
        return found

    def resolve(self, image):
        found = self.locate_headers(image)
        if any(match is None for match in found):
            raise LayoutError("Both complete, unique panel headers are required")
        result = copy.deepcopy(self.profile)
        result["size"] = list(image.size)
        result["automatic_layout"] = True
        result["layout_scales"] = {}
        result["layout_anchor_phases"] = {}
        result["layout_anchor_variants"] = {}
        result["layout_anchor_renderers"] = {}
        for index, pane in enumerate(("inventory", "storage")):
            match = found[index]
            anchor = Rect(*self.profile["anchors"][index]["rect"])
            result["anchors"][index]["rect"] = list(match.rect.as_box())
            result["layout_scales"][pane] = match.scale
            result["layout_anchor_phases"][pane] = list(match.phase)
            result["layout_anchor_variants"][pane] = match.variant_id
            result["layout_anchor_renderers"][pane] = match.renderer
            grid = result[pane]
            grid["rect"] = list(affine_rect(Rect(*self.profile[pane]["rect"]), anchor, match).as_box())
            try:
                if pane == "inventory" and self.profile[pane]["rows"] == 8:
                    from inventory_viewport import recognize_inventory_viewport
                    viewport = recognize_inventory_viewport(image, Rect(*grid["rect"]),
                        grid["rows"], grid["columns"], self.gaps[pane], match.scale)
                    grid["rect"] = list(viewport.grid.rect.as_box())
                    grid["rows"] = viewport.grid.rows
                    result["inventory_viewport"] = viewport.metadata()
                else:
                    measure_grid(image, Rect(*grid["rect"]), grid["rows"], grid["columns"], self.gaps[pane], match.scale)
            except LayoutError as error:
                # Preserve the independently proved header stage without
                # changing any grid tolerance or accepting a failed layout.
                raise GridEvidenceError(str(error)) from error
            keys = ("sort_button", "transfer_button") if pane == "inventory" else ()
            for key in keys:
                if result.get(key):
                    result[key]["rect"] = list(affine_rect(Rect(*self.profile[key]["rect"]), anchor, match).as_box())
            if pane == "storage":
                for page in result.get("pages", []):
                    for key in ("button", "marker"):
                        page[key] = list(affine_rect(Rect(*page[key]), anchor, match).as_box())
        self._geometry(result, image.size)
        return result

    def resolve_inventory_scroll(self, image, adapted_profile):
        """Resolve content phase at proved header positions, then revalidate.

        Deliberate sort/wheel readback cannot silently move or rescale either
        pane. Avoid a global multiscale search: the whole original viewport
        comes from the already adopted inventory header transform, and both
        complete headers plus the storage grid are checked on this same frame.
        Controls and safe parking retain their original physical coordinates.
        """
        if image.size != tuple(adapted_profile.get("size", ())):
            raise LayoutError("Client geometry changed during inventory scroll readback")
        if self.profile["inventory"]["rows"] != 8:
            raise LayoutError("Inventory scroll readback needs canonical viewport evidence")
        result = copy.deepcopy(adapted_profile)
        metadata = result.get("inventory_viewport")
        if not isinstance(metadata, dict):
            raise LayoutError("Inventory scroll readback lacks its original viewport")
        scale = result.get("layout_scales", {}).get("inventory")
        _scales((scale,))
        anchor = Rect(*result["anchors"][0]["rect"])
        original = affine_rect(Rect(*self.profile["inventory"]["rect"]),
            Rect(*self.profile["anchors"][0]["rect"]), ScaleMatch(anchor, scale, 0, 0))
        if metadata.get("rect") != list(original.as_box()):
            raise LayoutError("Inventory scroll viewport differs from its fixed header transform")
        from inventory_viewport import recognize_inventory_viewport
        viewport = recognize_inventory_viewport(image, original, self.profile["inventory"]["rows"],
            self.profile["inventory"]["columns"], self.gaps["inventory"], scale)
        result["inventory"] = {**result["inventory"], "rect": list(viewport.grid.rect.as_box()),
                               "rows": viewport.grid.rows, "columns": viewport.grid.columns}
        result["inventory_viewport"] = viewport.metadata()
        self.validate_current(image, result)
        return result

    @staticmethod
    def _geometry(profile, size):
        grids = [Rect(*profile[pane]["rect"]) for pane in ("inventory", "storage")]
        rects = grids + [Rect(*a["rect"]) for a in profile["anchors"]]
        viewport = profile.get("inventory_viewport")
        if viewport is not None:
            if not isinstance(viewport, dict) or set(viewport) != {"rect", "pitch", "phase", "identity"}:
                raise LayoutError("Invalid inventory viewport provenance")
            original_viewport = Rect(*viewport["rect"])
            if not (original_viewport.left == grids[0].left and original_viewport.right == grids[0].right
                    and original_viewport.top <= grids[0].top < grids[0].bottom <= original_viewport.bottom):
                raise LayoutError("Complete inventory rows leave their proved viewport")
            rects.append(original_viewport)
            grids[0] = original_viewport
        rects += [Rect(*profile[k]["rect"]) for k in ("sort_button", "transfer_button") if profile.get(k)]
        rects += [Rect(*p[k]) for p in profile.get("pages", []) for k in ("button", "marker")]
        if any(not rect.fits(*size) for rect in rects):
            raise LayoutError("Panel geometry is not completely inside the frame")
        def overlap(a, b):
            return a.left < b.right and a.right > b.left and a.top < b.bottom and a.bottom > b.top
        if overlap(*grids) or any(overlap(Rect(*a["rect"]), grid) for a in profile["anchors"] for grid in grids):
            raise LayoutError("Panels overlap the item grids")

    def validate_current(self, image, adapted_profile):
        """Fast validation of adopted positions/scales; never silently re-adopt."""
        if image.size != tuple(adapted_profile["size"]):
            raise LayoutError("Client geometry changed during the operation")
        self._geometry(adapted_profile, image.size)
        for index, pane in enumerate(("inventory", "storage")):
            scale = adapted_profile.get("layout_scales", {}).get(pane, 1.0)
            _scales((scale,))
            rect = Rect(*adapted_profile["anchors"][index]["rect"])
            phase = tuple(adapted_profile.get("layout_anchor_phases", {}).get(pane, (0.0, 0.0)))
            if len(phase) != 2 or any(not isinstance(p, (int, float)) or not math.isfinite(p) or abs(p) > 0.5 for p in phase):
                raise LayoutError("Invalid adopted sampling phase")
            reference_sample = self.headers[index]
            # Old in-memory/adapted profiles used fractional-box validation.
            # New resolutions always record their accepted renderer explicitly.
            renderer = adapted_profile.get("layout_anchor_renderers", {}).get(pane, "phasebox")
            if renderer not in ("resize", "phasebox", "neutral"):
                raise LayoutError("Invalid adopted header renderer")
            render_scale = scale
            identifier = adapted_profile.get("layout_anchor_variants", {}).get(pane)
            if renderer == "neutral":
                from neutral_panels import validate
                validate(image, reference_sample, pane, rect, scale, phase, identifier)
            elif identifier is not None:
                spec = self.variants.get(identifier)
                fingerprint = hashlib.sha256(reference_sample.convert("RGB").tobytes()).hexdigest()
                if (spec is None or spec["kind"] != "headers" or spec["pane"] != pane
                        or spec["canonical_pixels_sha256"] != fingerprint):
                    raise LayoutError("Adopted header variant is not an approved canonical match")
                reference_sample = spec["sample"]
                render_scale = scale / spec["canonical_scale"]
            if renderer != "neutral":
                try:
                    sample = _header_sample(reference_sample, render_scale,
                                            (rect.width, rect.height), phase, renderer)
                except ValueError as error:
                    raise LayoutError("Adopted header dimensions contradict its scale") from error
                if image_rmse(image.crop(rect.as_box()), sample) > 10:
                    raise LayoutError("Adopted header changed or is occluded")
            grid = adapted_profile[pane]
            if pane == "inventory" and self.profile[pane]["rows"] == 8:
                metadata = adapted_profile.get("inventory_viewport")
                if metadata is None:
                    # Historical unshifted in-memory profiles remain valid;
                    # changed row counts need explicit new viewport evidence.
                    if grid["rows"] != self.profile[pane]["rows"]:
                        raise LayoutError("Shifted inventory rows lack viewport provenance")
                    measure_grid(image, Rect(*grid["rect"]), grid["rows"], grid["columns"], self.gaps[pane], scale)
                    continue
                original = affine_rect(Rect(*self.profile[pane]["rect"]),
                    Rect(*self.profile["anchors"][index]["rect"]), ScaleMatch(rect, scale, 0, 0))
                if metadata["rect"] != list(original.as_box()):
                    raise LayoutError("Inventory viewport differs from its header transform")
                from inventory_viewport import recognize_inventory_viewport
                viewport = recognize_inventory_viewport(image, original, self.profile[pane]["rows"],
                    self.profile[pane]["columns"], self.gaps[pane], scale)
                if (viewport.grid != GridSpec(Rect(*grid["rect"]), grid["rows"], grid["columns"])
                        or viewport.identity != metadata.get("identity")):
                    raise LayoutError("Inventory scroll phase changed after adoption")
                pitch, phase = metadata.get("pitch"), metadata.get("phase")
                if (isinstance(pitch, bool) or not isinstance(pitch, (int, float))
                        or not math.isfinite(pitch) or pitch <= 0
                        or isinstance(phase, bool) or not isinstance(phase, (int, float))
                        or not math.isfinite(phase) or not 0 <= phase < pitch):
                    raise LayoutError("Inventory scroll metadata is unproved")
                # Sub-pixel separator fit varies with native antialiasing. The
                # actual complete input grid/viewport/identity remain EXACT.
                # Bounds match the separate lattice-motion readback precision;
                # whole RGB/segment/header/storage guards above are unchanged.
                phase_difference = abs((phase - viewport.phase + viewport.row_pitch / 2)
                                       % viewport.row_pitch - viewport.row_pitch / 2)
                if (abs(pitch - viewport.row_pitch) > max(.75, viewport.row_pitch * .0125)
                        or phase_difference > .75):
                    raise LayoutError("Inventory scroll phase changed after adoption")
            else:
                measure_grid(image, Rect(*grid["rect"]), grid["rows"], grid["columns"], self.gaps[pane], scale)
