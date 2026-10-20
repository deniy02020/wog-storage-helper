"""Match the physical left item frame against scale-aware native contexts.

This module only reads supplied images/local models. It never captures or sends
input. Tiny native contexts preserve the neighbor pixels involved in resampling;
the existing 14-RMSE and 5-margin requirements remain unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class TierMatch:
    tier: int | None
    rmse: float | None
    margin: float | None
    reason: str


class AdaptiveTierMatcher:
    MIN_SCALE = 0.35
    MAX_SCALE = 1.5
    MAX_RMSE = 14.0
    MIN_MARGIN = 5.0
    _PHASES = (-0.5, -0.25, 0.0, 0.25, 0.5)
    _SMALL_PHASES = (-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0)

    def __init__(self, resource_directory):
        directory = Path(resource_directory)
        document = json.loads((directory / "adaptive_tiers.json").read_text(encoding="utf-8"))
        if (document.get("schema") != 1 or document.get("context_rect") != [0, 0, 8, 35]
                or document.get("badge_rect") != [1, 14, 4, 25]
                or document.get("max_rmse") != self.MAX_RMSE
                or document.get("min_margin") != self.MIN_MARGIN):
            raise ValueError("Unsupported adaptive tier model geometry or thresholds.")
        tiers = document.get("tiers")
        if not isinstance(tiers, dict) or set(tiers) != {str(t) for t in range(1, 7)}:
            raise ValueError("All six tier classes, including both low-tier negatives, are required.")
        backgrounds = document.get("backgrounds")
        if not isinstance(backgrounds, list) or not backgrounds:
            raise ValueError("Native background negatives are required.")
        if any(not isinstance(entries, list) or not entries for entries in tiers.values()):
            raise ValueError("A tier context class is missing.")
        if sum(len(entries) for entries in tiers.values()) + len(backgrounds) > 400:
            raise ValueError("Adaptive context model count exceeds its bound.")
        self.models = {}
        count = 0
        for tier in range(0, 7):
            entries = backgrounds if tier == 0 else tiers[str(tier)]
            if not isinstance(entries, list) or not entries:
                raise ValueError("A tier context class is missing.")
            self.models[tier] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("An adaptive context entry must be an object.")
                name = entry.get("image")
                digest = entry.get("sha256")
                pattern = r"adaptive_tier_background_\d{3}\.png" if tier == 0 else rf"adaptive_tier_T{tier}_\d{{3}}\.png"
                if (not isinstance(name, str) or not re.fullmatch(pattern, name)
                        or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                    raise ValueError("Invalid adaptive tier resource reference.")
                path = directory / name
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError("Adaptive tier context checksum mismatch.")
                with Image.open(path) as image:
                    if image.size != (8, 35):
                        raise ValueError("A native context must be exactly 8x35 pixels.")
                    self.models[tier].append(image.convert("RGB"))
                count += 1
        if count > 400:
            raise ValueError("Adaptive context model count exceeds its bound.")
        self._cache = {}

    def _patches(self, scale, width, height, x, y):
        key = (round(scale, 5), width, height, x, y)
        if key in self._cache:
            return self._cache[key]
        patches = {}
        phases = self._SMALL_PHASES if scale <= 0.5 else self._PHASES
        for tier, contexts in self.models.items():
            values = []
            for context in contexts:
                for phase_x in phases:
                    for phase_y in phases:
                        # Pillow's resize box retains the surrounding context as
                        # filter support. The box samples the physical-pixel grid
                        # at this exact scale, with at most half a pixel of phase
                        # normally, or one physical pixel for Unity's small UI.
                        left, top = (x + phase_x) / scale, (y + phase_y) / scale
                        right, bottom = left + width / scale, top + height / scale
                        if not (0 <= left < right <= 8 and 0 <= top < bottom <= 35):
                            continue
                        for resample in (Image.Resampling.BILINEAR, Image.Resampling.LANCZOS):
                            patch = context.resize((width, height), resample,
                                                   box=(left, top, right, bottom))
                            values.append(np.asarray(patch, dtype=np.float32).reshape(-1))
            if not values:
                raise ValueError("Scale does not fit the verified native context support.")
            patches[tier] = np.stack(values)
        # A changing resize should not retain an unbounded history of models.
        if len(self._cache) >= 8:
            self._cache.clear()
        self._cache[key] = patches
        return patches

    def match(self, cell_image, scale, canonical_size=(65, 65), *, occupied=False) -> TierMatch:
        if type(occupied) is not bool:
            return TierMatch(None, None, None, "occupancy proof must be boolean")
        if (isinstance(scale, bool) or not isinstance(scale, (float, int))
                or not math.isfinite(scale) or not self.MIN_SCALE <= scale <= self.MAX_SCALE):
            return TierMatch(None, None, None, "unsupported or nonfinite scale")
        if (not isinstance(canonical_size, (tuple, list)) or len(canonical_size) != 2
                or any(isinstance(n, bool) or not isinstance(n, int) or n < 35 or n > 100
                       for n in canonical_size)):
            return TierMatch(None, None, None, "unsupported canonical cell dimensions")
        if not isinstance(cell_image, Image.Image):
            return TierMatch(None, None, None, "a physical cell image is required")
        if any(abs(actual - expected * scale) > 1.1
               for actual, expected in zip(cell_image.size, canonical_size)):
            return TierMatch(None, None, None, "physical cell dimensions disagree with scale")
        width, height = max(1, round(3 * scale)), max(1, round(11 * scale))
        x, y = round(scale), round(14 * scale)
        try:
            patches = self._patches(float(scale), width, height, x, y)
        except (ValueError, OSError) as exc:
            return TierMatch(None, None, None, str(exc))
        pixels = cell_image.convert("RGB")
        scores = {tier: float("inf") for tier in range(0, 7)}
        # Raster origin/pitch can differ by one physical pixel after rounding.
        # Search only this tiny frame lane, never any icon or arbitrary location.
        # Unity retains a physical-pixel outline when its tiny UI is redrawn.
        # Accepted 27px native cells put the colored stem at x=2. Include that
        # one-pixel stem only; a two-pixel probe would enter the icon lane.
        offsets_x = (-1, 0, 1, 2) if scale <= 0.5 and width == 1 else (-1, 0, 1)
        for offset_x in offsets_x:
            for offset_y in (-1, 0, 1):
                left, top = x + offset_x, y + offset_y
                if left < 0 or top < 0 or left + width > pixels.width or top + height > pixels.height:
                    continue
                observed = np.asarray(pixels.crop((left, top, left + width, top + height)),
                                      dtype=np.float32).reshape(-1)
                for tier, values in patches.items():
                    delta = values - observed
                    score = float(np.sqrt(np.min(np.mean(delta * delta, axis=1))))
                    scores[tier] = min(scores[tier], score)
        ordered = sorted((score, tier) for tier, score in scores.items())
        score, tier = ordered[0]
        margin = ordered[1][0] - score
        if (occupied and tier == 1 and ordered[1][1] == 0
                and score <= self.MAX_RMSE and scores[0] > score
                and min(scores[t] for t in range(2, 7))-score >= self.MIN_MARGIN):
            # The caller has independently proved a filled FULL cell. A grey
            # T1 frame can resemble the background at small rasters. Only T1
            # may use this separate occupancy proof; all other positive tiers
            # must still lose by the original margin. No transferable tier is
            # admitted by this route, and uncertain/empty cells never call it.
            return TierMatch(1, score, min(scores[t] for t in range(2,7))-score,
                             "occupied grey T1 frame; all higher tiers excluded")
        if not math.isfinite(score) or not math.isfinite(margin):
            return TierMatch(None, None, None, "no valid physical border support")
        if score > self.MAX_RMSE or margin < self.MIN_MARGIN:
            return TierMatch(None, score, margin, "border is unrecognized or ambiguous")
        if tier == 0:
            return TierMatch(None, score, margin, "native background negative")
        return TierMatch(tier, score, margin, "recognized physical item frame")
