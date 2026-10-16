"""Platform-neutral visual recognition and transfer planning.

This module never controls a mouse or keyboard.  It intentionally recognizes
only user-calibrated samples at fixed, window-relative grid positions.  Scores
are RGB root-mean-square differences on the 0..255 scale, not probabilities.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Collection, Mapping, Optional, Sequence

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def as_box(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom

    def translated(self, x: int, y: int) -> "Rect":
        return Rect(self.left + x, self.top + y, self.right + x, self.bottom + y)

    def fits(self, width: int, height: int) -> bool:
        return (0 <= self.left < self.right <= width
                and 0 <= self.top < self.bottom <= height)


@dataclass(frozen=True)
class GridSpec:
    rect: Rect
    rows: int
    columns: int

    @property
    def count(self) -> int:
        return self.rows * self.columns

    def cell_rect(self, index: int) -> Rect:
        if self.rows < 1 or self.columns < 1:
            raise ValueError("Grid must have positive rows and columns")
        if not 0 <= index < self.count:
            raise IndexError("Cell index is outside the grid")
        row, column = divmod(index, self.columns)
        # Half-open coordinates cover the full grid without accumulating error.
        return Rect(
            self.rect.left + self.rect.width * column // self.columns,
            self.rect.top + self.rect.height * row // self.rows,
            self.rect.left + self.rect.width * (column + 1) // self.columns,
            self.rect.top + self.rect.height * (row + 1) // self.rows,
        )


@dataclass(frozen=True)
class TemplateSet:
    tier_samples: Mapping[int, Sequence[Image.Image]]
    empty_samples: Sequence[Image.Image]


@dataclass(frozen=True)
class ClassifierConfig:
    badge_rect: Optional[Rect] = None
    max_tier_rmse: float = 14.0
    min_margin: float = 5.0
    max_empty_rmse: float = 12.0


@dataclass(frozen=True)
class CellState:
    index: int
    rect: Rect
    tier: Optional[int]
    # None means the occupancy itself is uncertain, not a confirmed filled cell.
    empty: Optional[bool]
    rmse: Optional[float]
    margin: Optional[float]
    reason: str = ""

    @property
    def label(self) -> Optional[str]:
        if self.empty is True:
            return "empty"
        return f"T{self.tier}" if self.tier is not None else None


@dataclass(frozen=True)
class GridScan:
    grid: GridSpec
    cells: tuple[CellState, ...]

    @property
    def tier_counts(self) -> dict[int, int]:
        return dict(Counter(c.tier for c in self.cells if c.tier is not None))

    @property
    def empty_count(self) -> int:
        return sum(c.empty is True for c in self.cells)

    @property
    def unknown_count(self) -> int:
        return sum(c.tier is None and c.empty is not True for c in self.cells)


@dataclass(frozen=True)
class TransferCandidate:
    source_index: int
    destination_index: int
    tier: int
    source_rect: Rect
    destination_rect: Rect


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    reason: str


def _rgb(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("RGB"), dtype=np.float32)


def image_rmse(first: Image.Image, second: Image.Image) -> float:
    """Compare identically sized images. No resizing or tolerance is implicit."""
    if first.size != second.size or min(first.size) < 1:
        raise ValueError("Samples must have the same nonzero dimensions")
    difference = _rgb(first) - _rgb(second)
    return float(np.sqrt(np.mean(difference * difference)))


def _best_rmse(image: Image.Image, samples: Sequence[Image.Image]) -> Optional[float]:
    scores = [image_rmse(image, sample) for sample in samples
              if sample.size == image.size and min(sample.size) > 0]
    return min(scores) if scores else None


def validate_calibration(
    image_size: tuple[int, int],
    grid: GridSpec,
    templates: TemplateSet,
    config: ClassifierConfig,
    *,
    require_tiers: bool = True,
) -> list[str]:
    """Return actionable calibration errors. A caller must not run with errors."""
    errors: list[str] = []
    if grid.rows < 1 or grid.columns < 1 or grid.count > 1000:
        errors.append("Grid rows/columns must be positive and contain at most 1000 cells")
        return errors
    if not grid.rect.fits(*image_size):
        errors.append("Grid must fit inside the captured game client area")
        return errors
    for name, value in (("max_tier_rmse", config.max_tier_rmse),
                        ("min_margin", config.min_margin),
                        ("max_empty_rmse", config.max_empty_rmse)):
        if not np.isfinite(value) or not 0 < value < 255:
            errors.append(f"{name} must be finite and between 0 and 255")
    if config.max_empty_rmse + config.min_margin >= 255:
        errors.append("Empty threshold plus margin must be below 255")
    cell_sizes = {(grid.cell_rect(i).width, grid.cell_rect(i).height)
                  for i in range(grid.count)}
    if not templates.empty_samples:
        errors.append("At least one full-cell empty sample is required")
    for size in cell_sizes:
        if not any(sample.size == size for sample in templates.empty_samples):
            errors.append(f"An empty sample with cell dimensions {size[0]}x{size[1]} is required")
    if require_tiers:
        if not templates.tier_samples.get(1) or not templates.tier_samples.get(2):
            errors.append("T1 and T2 negative tier samples are required")
        if not any(t >= 3 and samples for t, samples in templates.tier_samples.items()):
            errors.append("At least one allowed T3+ tier sample is required")
    if templates.tier_samples:
        badge = config.badge_rect
        if badge is None:
            errors.append("A badge rectangle is required for tier recognition")
        else:
            if any(not badge.fits(*size) for size in cell_sizes):
                errors.append("Badge rectangle must fit inside every grid cell")
            for tier, samples in templates.tier_samples.items():
                if not isinstance(tier, int) or isinstance(tier, bool) or not 1 <= tier <= 6:
                    errors.append("Supported equipment tier labels are integers T1 through T6")
                if not samples:
                    errors.append(f"Tier T{tier} has no samples")
                if any(sample.size != (badge.width, badge.height) for sample in samples):
                    errors.append(f"Tier T{tier} sample dimensions differ from the badge rectangle")
    return errors


def classify_cell(
    image: Image.Image,
    index: int,
    rect: Rect,
    templates: TemplateSet,
    config: ClassifierConfig,
) -> CellState:
    empty_score = _best_rmse(image, templates.empty_samples)
    if empty_score is None:
        return CellState(index, rect, None, None, None, None, "no matching empty sample dimensions")

    scores: list[tuple[float, int]] = []
    if templates.tier_samples and config.badge_rect is not None:
        badge = image.crop(config.badge_rect.as_box())
        for tier, samples in templates.tier_samples.items():
            score = _best_rmse(badge, samples)
            if score is not None:
                scores.append((score, tier))
    scores.sort()
    best_score = scores[0][0] if scores else None
    best_tier = scores[0][1] if scores else None
    margin = scores[1][0] - scores[0][0] if len(scores) > 1 else None
    tier_confident = (best_score is not None
                      and best_score <= config.max_tier_rmse
                      and margin is not None and margin >= config.min_margin)

    if empty_score <= config.max_empty_rmse:
        if tier_confident:
            return CellState(index, rect, None, None, best_score, margin,
                             "empty and tier samples conflict")
        return CellState(index, rect, None, True, empty_score, None, "empty")
    # An occupied cell must clearly differ from every empty template.
    if empty_score < config.max_empty_rmse + config.min_margin:
        return CellState(index, rect, None, None, empty_score, margin,
                         "occupancy is ambiguous")
    if not tier_confident:
        return CellState(index, rect, None, False, best_score, margin,
                         "tier is unrecognized or ambiguous")
    return CellState(index, rect, best_tier, False, best_score, margin, "recognized")


def scan_grid(
    image: Image.Image,
    grid: GridSpec,
    templates: TemplateSet,
    config: ClassifierConfig,
) -> GridScan:
    if grid.rows < 1 or grid.columns < 1 or not grid.rect.fits(*image.size):
        raise ValueError("Invalid grid or grid outside image")
    cells = tuple(classify_cell(image.crop(rect.as_box()), index, rect, templates, config)
                  for index in range(grid.count)
                  for rect in (grid.cell_rect(index),))
    return GridScan(grid, cells)


def choose_transfer(
    inventory: GridScan,
    storage: GridScan,
    min_tier: int = 3,
    excluded_inventory: Collection[int] = (),
) -> Optional[TransferCandidate]:
    if isinstance(min_tier, bool) or not isinstance(min_tier, int) or not 1 <= min_tier <= 6:
        raise ValueError("Minimum item tier must be T1 through T6")
    destination = next((cell for cell in storage.cells if cell.empty is True), None)
    if destination is None:
        return None
    source = next((cell for cell in inventory.cells
                   if cell.index not in excluded_inventory
                   and cell.empty is False
                   and cell.tier is not None and min_tier <= cell.tier <= 6), None)
    if source is None:
        return None
    return TransferCandidate(source.index, destination.index, source.tier,
                             source.rect, destination.rect)


def verify_transfer(
    before_inventory: GridScan,
    after_inventory: GridScan,
    before_storage: GridScan,
    after_storage: GridScan,
    candidate: TransferCandidate,
) -> VerificationResult:
    """Require independent evidence in both panels; do not retry on failure.

    Counts retain the compaction proof when an identical item replaces the
    source. A concurrent drop or unrelated recognition change can obscure that
    count; then require the original source to be confidently empty and the
    exact tier to fill only the original destination. An occupied replacement
    source cannot use this alternative proof.
    """
    if (before_inventory.grid != after_inventory.grid
            or before_storage.grid != after_storage.grid):
        return VerificationResult(False, "grid geometry changed")
    for scan in (before_inventory, after_inventory, before_storage, after_storage):
        if (scan.grid.rows < 1 or scan.grid.columns < 1
                or len(scan.cells) != scan.grid.count):
            return VerificationResult(False, "grid cell count is invalid")
        if any(cell.index != index or cell.rect != scan.grid.cell_rect(index)
               for index, cell in enumerate(scan.cells)):
            return VerificationResult(False, "grid cell geometry changed")
    if not (0 <= candidate.source_index < len(before_inventory.cells)
            and 0 <= candidate.destination_index < len(before_storage.cells)):
        return VerificationResult(False, "candidate is outside the recorded grids")
    source = before_inventory.cells[candidate.source_index]
    before_dest = before_storage.cells[candidate.destination_index]
    after_dest = after_storage.cells[candidate.destination_index]
    if (source.tier != candidate.tier or source.empty is not False
            or before_dest.empty is not True
            or source.rect != candidate.source_rect
            or before_dest.rect != candidate.destination_rect):
        return VerificationResult(False, "candidate no longer matches its before state")
    decrease = (before_inventory.tier_counts.get(candidate.tier, 0)
                - after_inventory.tier_counts.get(candidate.tier, 0))
    if decrease != 1:
        after_source = after_inventory.cells[candidate.source_index]
        other_storage_unchanged = all(
            (first.label, first.empty) == (second.label, second.empty)
            for index, (first, second) in enumerate(zip(before_storage.cells, after_storage.cells))
            if index != candidate.destination_index)
        if (after_source.empty is True and after_source.tier is None
                and after_dest.empty is False and after_dest.tier == candidate.tier
                and other_storage_unchanged):
            return VerificationResult(True,
                "source vacancy and unchanged storage independently confirm transfer")
        return VerificationResult(False, "inventory tier count did not decrease by exactly one")
    if after_dest.empty is not False:
        return VerificationResult(False, "destination is not confidently filled")
    if after_dest.tier is not None and after_dest.tier != candidate.tier:
        return VerificationResult(False, "destination shows a different tier")
    if before_storage.empty_count - after_storage.empty_count != 1:
        return VerificationResult(False, "storage empty count did not decrease by exactly one")
    return VerificationResult(True, "inventory and storage independently confirm transfer")
