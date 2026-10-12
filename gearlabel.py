"""Equipment category guard using user-calibrated 'Lv' text, never its number.

This recognizer does not infer tier from a wearable level. It searches only the
bottom band of a cell for the calibrated letter pixels and their dark outline.
Unknown text styles remain unrecognized. Native antialiasing changes RGB values
when an item moves between cells; both RGB error and correlation are required.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import cv2
import numpy as np
from PIL import Image


@dataclass(frozen=True)
class LabelMatch:
    is_equipment: bool
    similarity: Optional[float]
    rmse: Optional[float]
    position: Optional[tuple[int, int]]
    reason: str


class EquipmentLabelMatcher:
    def __init__(self, directory: str | Path, sample_names: Sequence[str], *,
                 min_similarity: float = 0.94, max_rmse: float = 40.0):
        self.directory = Path(directory).resolve()
        if not sample_names:
            raise ValueError("Нужны образцы букв Lv на экипировке.")
        if not 0.94 <= min_similarity <= 1 or not 0 < max_rmse <= 40:
            raise ValueError("Порог распознавания Lv должен оставаться строгим.")
        self.min_similarity, self.max_rmse = min_similarity, max_rmse
        self.samples = []
        for name in sample_names:
            path = (self.directory / name).resolve()
            if not path.is_relative_to(self.directory):
                raise ValueError("Образец Lv должен находиться внутри папки профиля.")
            image = Image.open(path).convert("RGB")
            if not 8 <= image.width <= 64 or not 8 <= image.height <= 40:
                raise ValueError("Образец должен содержать только буквы Lv размером 8–64 × 8–40 пикселей.")
            rgb = np.asarray(image)
            red, green, blue = (rgb[:, :, c].astype(np.float32) for c in range(3))
            yellow = ((red > 140) & (green > 100) & (blue < 150)
                      & (red > blue * 1.3) & (green > blue * 1.3))
            reddish = (red > 130) & (red > green * 1.4) & (red > blue * 1.4)
            letters = yellow | reddish
            expanded = cv2.dilate(letters.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
            outline = expanded & (rgb.max(axis=2) < 95)
            if letters.sum() < 20 or outline.sum() < 15:
                raise ValueError("В образце Lv недостаточно цветных букв и тёмной обводки.")
            mask = ((letters | outline) * 255).astype(np.uint8)
            self.samples.append((rgb, mask))

    def match(self, cell: Image.Image) -> LabelMatch:
        # Fractional grid pitch moves lower-row labels a few pixels upward.
        # Searching the lower half keeps the complete glyph in the search.
        start = int(cell.height * 0.50)
        band = np.asarray(cell.convert("RGB"))[start:, :]
        best = None
        for template, mask in self.samples:
            height, width = template.shape[:2]
            if height > band.shape[0] or width > band.shape[1]:
                continue
            # Choose the minimum masked RGB error, then independently require
            # normalized correlation at that position; correlation alone can
            # mistake similarly colored gems for letter pixels.
            errors = cv2.matchTemplate(band, template, cv2.TM_SQDIFF, mask=mask)
            finite_errors = np.where(np.isfinite(errors), errors, np.inf)
            minimum, _, location, _ = cv2.minMaxLoc(finite_errors)
            if not np.isfinite(minimum):
                continue
            x, y = location
            region = band[y:y + height, x:x + width]
            first = template[mask > 0].astype(np.float32).reshape(-1)
            second = region[mask > 0].astype(np.float32).reshape(-1)
            rmse = float(np.sqrt(np.mean((first - second) ** 2)))
            norm = float(np.linalg.norm(first) * np.linalg.norm(second))
            similarity = float(np.dot(first, second) / norm) if norm else 0.0
            result = LabelMatch(rmse <= self.max_rmse and similarity >= self.min_similarity,
                                similarity, rmse, (x, y + start), "recognized Lv label")
            if best is None or rmse < best.rmse:
                best = result
        if best is None:
            return LabelMatch(False, None, None, None, "no label sample fits the cell")
        if not best.is_equipment:
            return LabelMatch(False, best.similarity, best.rmse, best.position,
                              "Lv label is absent or differs from calibrated samples")
        return best
