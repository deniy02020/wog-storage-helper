"""Read-only proof of the currently accessible storage page strip.

The caller must first validate the adopted full headers and item grids. Page
coordinates then come only from that storage header transform, never from the
desktop or from the number of pages saved in an old user profile. A neutral
marker alone is insufficient: each tile must contain its actual numeral.

1--4 masks below are derived from genuine native game glyphs. 5/6 are bounded
stroke prototypes, not claims that an account with those pages was observed.
No OCR service, desktop input, capture, settings write or font dependency is
used here. Uncertain evidence raises StoragePagesUnknown; it is never "full".
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from auto_layout import ScaleMatch, affine_rect, control_error
from core import Rect


class StoragePagesUnknown(RuntimeError):
    """Accessible page count or selection is not positively established."""


@dataclass(frozen=True)
class PageEvidence:
    id: int
    button: Rect
    marker: Rect
    active: bool
    digit_score: float
    marker_error: float


@dataclass(frozen=True)
class StoragePages:
    pages: tuple[PageEvidence, ...]
    selected_id: int
    canonical_pages: tuple[dict, ...]
    identity: str

    def page(self, page_id: int) -> PageEvidence:
        return next(page for page in self.pages if page.id == page_id)


# Genuine glyph silhouettes: the unmodified native capture
# Private original native reference filename withheld.
# SHA256 a86dd524eecdaa1caf54f058ecc9302c9d6704b1cde98d03d5fac9ab13145efd.
# These are tiny semantic masks, not full game screenshots. Coordinates are
# documented in test_storage_pages.py. 5/6 are deliberately marked synthetic.
_GLYPHS = {
    1: ("...###", ".#####", "######", "######", "#..###") + ("...###",) * 10,
    2: ("..######...", ".########..", "##########.", "###....###.", "###....###.",
        ".......###.", "......####.", ".....####..", "....####...", "...####....",
        "..####.....", ".####......", "##########.", "###########", "###########"),
    3: ("..######..", ".########.", "####..####", "###....###", ".......###",
        "......####", "...######.", "...######.", ".....#####", ".......###",
        "##.....###", "###....###", "##########", ".########.", "...####..."),
    4: ("......###..", ".....#####.", "....######.", "....######.", "...#######.",
        "...#######.", "..###..###.", ".###...###.", ".###...###.", "####..####.",
        "###########", "###########", ".#########.", "......####.", "......####."),
    # Hand-specified numeral stroke prototypes. Native 5/6 evidence remains a
    # separate release-verification task; a mismatching font is rejected.
    5: (".#########", ".#########", ".###......", "###.......", "###.......",
        "########..", "#########.", ".....#####", ".......###", ".......###",
        ".......###", "###....###", "##########", ".########.", "..######.."),
    6: ("...####...", ".########.", ".###..###.", "###...###.", "###.......",
        "#######...", "########..", "####..###.", "###...###.", "###...####",
        "###....###", "###...###.", ".###..###.", ".#######..", "..#####..."),
}


def _tight_mask(mask: np.ndarray) -> np.ndarray:
    yy, xx = np.nonzero(mask)
    if not len(xx):
        raise StoragePagesUnknown("Номер страницы хранилища не виден.")
    return mask[yy.min():yy.max() + 1, xx.min():xx.max() + 1]


def _normalized(mask: np.ndarray) -> np.ndarray:
    return cv2.resize(_tight_mask(mask).astype(np.float32), (24, 36),
                      interpolation=cv2.INTER_LINEAR)


@lru_cache(maxsize=1)
def _glyph_models():
    return {digit: _normalized(np.array([[pixel == "#" for pixel in row]
                                        for row in rows], dtype=bool))
            for digit, rows in _GLYPHS.items()}


def _digit(mask: np.ndarray, expected: int) -> float:
    """A single positively identified numeral, with an ambiguity margin."""
    tight = _tight_mask(mask)
    normalized = _normalized(tight)
    contours, hierarchy = cv2.findContours(tight.astype(np.uint8), cv2.RETR_CCOMP,
                                           cv2.CHAIN_APPROX_SIMPLE)
    lower_bowl = hierarchy is not None and any(
        hierarchy[0][index][3] >= 0 and
        cv2.boundingRect(contour)[1] + cv2.boundingRect(contour)[3] / 2
        >= tight.shape[0] * .45 for index, contour in enumerate(contours))
    scores = {}
    for digit, model in _glyph_models().items():
        # Soft Dice measures the whole glyph; blank space is not positive
        # matching evidence and cannot conceal an extra cursor stroke.
        scores[digit] = float(2 * np.minimum(normalized, model).sum()
                              / (normalized.sum() + model.sum()))
        # Bounding proportions keep the narrow 1 distinct from a blob.
        aspect = tight.shape[1] / tight.shape[0]
        target = len(_GLYPHS[digit][0]) / len(_GLYPHS[digit])
        if not target * .62 <= aspect <= target * 1.38:
            scores[digit] = 0.0
        if digit == 6 and not lower_bowl:
            scores[digit] = 0.0
    ordered = sorted(scores, key=scores.get, reverse=True)
    score = scores[expected]
    if ordered[0] != expected or score < .77 or score - scores[ordered[1]] < .055:
        raise StoragePagesUnknown("Номер страницы хранилища не подтверждён.")
    if expected == 6:
        # A 6 has a closed lower bowl. A C, G, 5 or a cursor with a similar
        # outline must not become an accessible sixth page.
        if not lower_bowl:
            raise StoragePagesUnknown("Цифра шестой страницы неоднозначна.")
    return score


def canonical_page_slots(profile: dict) -> tuple[dict, ...]:
    """Extend canonical strip geometry to six slots, without claiming access."""
    pages = profile.get("pages")
    if not isinstance(pages, list) or not 2 <= len(pages) <= 6:
        raise ValueError("Нужна каноническая геометрия полосы страниц.")
    if [p.get("id") for p in pages] != list(range(1, len(pages) + 1)):
        raise ValueError("Номера канонических страниц должны идти подряд.")
    for page in pages:
        if (isinstance(page["id"], bool) or any(
                len(page.get(key, [])) != 4 or any(
                    isinstance(v, bool) or not isinstance(v, int) for v in page[key])
                for key in ("button", "marker"))):
            raise ValueError("Некорректная каноническая страница.")
    buttons = [Rect(*page["button"]) for page in pages]
    base, base_marker = buttons[0], Rect(*pages[0]["marker"])
    pitch = (buttons[-1].left - base.left) / (len(buttons) - 1)
    if (not 20 <= base.width <= 80 or not 20 <= base.height <= 65 or
            not base.width <= pitch <= base.width + 8 or
            any(abs(button.left - base.left - index * pitch) > 2 or
                abs(button.width - base.width) > 1 or button.top != base.top or
                button.bottom != base.bottom for index, button in enumerate(buttons))):
        raise ValueError("Каноническая полоса страниц неоднозначна.")
    for page, button in zip(pages, buttons):
        marker = Rect(*page["marker"])
        if (marker.width != base_marker.width or marker.height != base_marker.height or
                marker.left - button.left != base_marker.left - base.left or
                marker.top - button.top != base_marker.top - base.top or
                not button.left < marker.left < marker.right < button.right or
                not button.top < marker.top < marker.bottom < button.bottom):
            raise ValueError("Ориентир страницы выходит за её кнопку.")
    result = copy.deepcopy(pages)
    for index in range(len(result), 6):
        dx = round(pitch * index)
        result.append({"id": index + 1, "button": list(base.translated(dx, 0).as_box()),
                       "marker": list(base_marker.translated(dx, 0).as_box())})
    return tuple(result)


class StoragePageDetector:
    """Prove 2--6 current pages using a freshly validated storage transform."""
    def __init__(self, canonical_profile: dict, directory: str | Path):
        self.profile = copy.deepcopy(canonical_profile)
        self.slots = canonical_page_slots(self.profile)
        self.directory = Path(directory).resolve()
        self.active = self._image(self.profile["page_active_image"])
        self.inactive = self._image(self.profile["page_inactive_image"])
        self.anchor = Rect(*self.profile["anchors"][1]["rect"])

    def _image(self, name):
        path = (self.directory / name).resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError("Образец страницы должен находиться в папке профиля.")
        with Image.open(path) as image:
            return image.convert("RGB")

    def _glyph(self, image, button, scale, active):
        # The numeral ROI excludes chrome and the strict independent marker.
        inset_x = max(1, round(button.width * 12 / 49))
        inset_y = max(1, round(button.height * 5 / 37))
        roi = Rect(button.left + inset_x, button.top + inset_y,
                   button.right - inset_x, button.bottom - inset_y)
        if not roi.fits(*image.size):
            raise StoragePagesUnknown("Кнопка страницы видна не полностью.")
        rgb = np.asarray(image.crop(roi.as_box()), dtype=np.float32)
        background = np.median(np.asarray(self.active if active else self.inactive,
                                          dtype=np.float32).reshape(-1, 3), axis=0)
        distance = rgb.mean(axis=2) - background.mean()
        # Direction is part of the proof: selected pages use a dark numeral,
        # all other accessible pages a light numeral on the neutral button.
        signed = -distance if active else distance
        mask = signed >= 35
        yy, xx = np.nonzero(mask)
        if not len(xx):
            raise StoragePagesUnknown("Номер страницы хранилища не виден.")
        width, height = int(xx.max() - xx.min() + 1), int(yy.max() - yy.min() + 1)
        effective_scale = button.height / 37
        if (not max(3, 11 * effective_scale) <= height <= 19 * effective_scale + 1 or
                not max(1, 3 * effective_scale) <= width <= 14 * effective_scale + 1 or
                abs((xx.min() + xx.max()) / 2 - (roi.width - 1) / 2) > 4 * effective_scale + 1 or
                abs((yy.min() + yy.max()) / 2 - (roi.height - 1) / 2) > 4 * effective_scale + 1):
            raise StoragePagesUnknown("Символ кнопки страницы не совпал с цифрой.")
        components, _labels, stats, _centers = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        if components != 2 or stats[1, cv2.CC_STAT_AREA] < 3:
            raise StoragePagesUnknown("Номер страницы скрыт или неоднозначен.")
        return mask

    @staticmethod
    def _is_plus(image, button):
        """Positive cross on a dark purchase tile; never an input target."""
        inset = max(1, round(button.height * 4 / 37))
        roi = Rect(button.left + inset, button.top + inset,
                   button.right - inset, button.bottom - inset)
        if not roi.fits(*image.size):
            return False
        rgb = np.asarray(image.crop(roi.as_box()), dtype=np.float32)
        gray = rgb.mean(axis=2)
        background = float(np.median(gray))
        if background > 65:
            return False
        mask = gray > max(95, background + 60)
        try:
            tight = _tight_mask(mask)
        except StoragePagesUnknown:
            return False
        height, width = tight.shape
        if (height < roi.height * .6 or width < roi.width * .48 or
                not .65 <= width / height <= 1.45):
            return False
        # A cross contains both long arms through the middle, with clear
        # corners. No text, disabled padlock or generic dark tile is enough.
        n = cv2.resize(tight.astype(np.float32), (24, 24), interpolation=cv2.INTER_LINEAR)
        # At the genuine 696px raster an arm is one physical pixel wide and
        # one antialiased pixel does not exceed the foreground threshold.
        # This semantic terminator can only remove a target, never authorize
        # a page input; the accessible-tile RGB bounds remain 10/10 above.
        return (float(n[8:16, :].mean(axis=1).max()) >= .70 and
                float(n[:, 8:16].mean(axis=0).max()) >= .80 and
                max(float(n[a:a + 7, b:b + 7].mean()) for a in (0, 17) for b in (0, 17)) < .32)

    def discover(self, image: Image.Image, adapted_profile: dict) -> StoragePages:
        if image.size != tuple(adapted_profile.get("size", ())):
            raise StoragePagesUnknown("Геометрия окна изменилась при проверке страниц.")
        scale = adapted_profile.get("layout_scales", {}).get("storage", 1.0)
        phase = tuple(adapted_profile.get("layout_anchor_phases", {}).get("storage", (0, 0)))
        if (isinstance(scale, bool) or not isinstance(scale, (int, float)) or
                not math.isfinite(scale) or not .35 <= scale <= 1.5):
            raise StoragePagesUnknown("Масштаб полосы страниц не подтверждён.")
        anchor = Rect(*adapted_profile["anchors"][1]["rect"])
        match = ScaleMatch(anchor, scale, 0, 0)
        pages, canonical = [], []
        terminator = False
        for spec in self.slots:
            button = affine_rect(Rect(*spec["button"]), self.anchor, match)
            marker = affine_rect(Rect(*spec["marker"]), self.anchor, match)
            if not button.fits(*image.size) or not marker.fits(*image.size):
                raise StoragePagesUnknown("Полоса страниц видна не полностью.")
            on = control_error(image, marker, self.active, Rect(*spec["marker"]),
                               self.anchor, anchor, scale, phase)
            off = control_error(image, marker, self.inactive, Rect(*spec["marker"]),
                                self.anchor, anchor, scale, phase)
            active = on <= 10 and off - on >= 10
            inactive = off <= 10 and on - off >= 10
            if not (active or inactive):
                if len(pages) >= 2 and self._is_plus(image, button):
                    terminator = True
                    break
                raise StoragePagesUnknown("Доступная страница или кнопка «+» не подтверждена.")
            digit_score = _digit(self._glyph(image, button, scale, active), spec["id"])
            pages.append(PageEvidence(spec["id"], button, marker, active, digit_score,
                                      on if active else off))
            canonical.append(copy.deepcopy(spec))
        if not 2 <= len(pages) <= 6 or (len(pages) < 6 and not terminator):
            raise StoragePagesUnknown("Число доступных страниц не подтверждено.")
        selected = [page.id for page in pages if page.active]
        if len(selected) != 1:
            raise StoragePagesUnknown("Выбранная страница хранилища неоднозначна.")
        identity = hashlib.sha256(json.dumps(
            [[page.id, page.button.as_box(), page.marker.as_box()] for page in pages],
            separators=(",", ":")).encode("ascii")).hexdigest()
        return StoragePages(tuple(pages), selected[0], tuple(canonical), identity)
