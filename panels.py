"""Verified panel opening and automatic geometry adaptation in memory.

Only the injected desktop performs input. No profile files are written here.
"""
from __future__ import annotations

import copy
import math
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from core import Rect, image_rmse


CALIBRATION_REQUIRED = "Не удалось подтвердить положение игровых панелей. Текущая проверка отменена."


@dataclass(frozen=True)
class ActivationTarget:
    """A fully observed static panel heading; no saved click coordinates."""
    rect: Rect
    pane: str
    anchor_rect: Rect | None = None
    anchor_proof: str | None = None

    @property
    def point(self):
        return self.rect.center


def locate_unique(image, sample, *, max_rmse=10, min_margin=5):
    """Return an exact-scale match, rejecting a second separate plausible match."""
    if image.width < sample.width or image.height < sample.height:
        return None
    frame = np.asarray(image.convert("RGB"), dtype=np.float32)
    template = np.asarray(sample.convert("RGB"), dtype=np.float32)
    # Unnormalised squared error orders matches by the same RGB RMSE guard.
    errors = cv2.matchTemplate(frame, template, cv2.TM_SQDIFF)
    _, _, point, _ = cv2.minMaxLoc(errors)
    x, y = point
    rect = Rect(x, y, x + sample.width, y + sample.height)
    best = image_rmse(image.crop(rect.as_box()), sample)
    if best > max_rmse:
        return None
    # Adjacent placements of the same icon are one match, not duplicate icons.
    distinct = errors.copy()
    left, right = max(0, x - sample.width + 1), min(errors.shape[1], x + sample.width)
    top, bottom = max(0, y - sample.height + 1), min(errors.shape[0], y + sample.height)
    distinct[top:bottom, left:right] = np.inf
    if np.isfinite(distinct).any():
        _, _, second, _ = cv2.minMaxLoc(distinct)
        other = image.crop((second[0], second[1], second[0] + sample.width,
                            second[1] + sample.height))
        if image_rmse(other, sample) - best < min_margin:
            raise RuntimeError("Элемент интерфейса найден неоднозначно. Клик отменён.")
    return rect


class PanelController:
    def __init__(self, desktop, handle, profile, directory, check_stop, wait):
        self.desktop, self.handle = desktop, handle
        self.profile = copy.deepcopy(profile)
        self.directory = Path(directory).resolve()
        self.check_stop, self.wait = check_stop, wait
        # An accepted opener is not safe to repeat until fresh pixels prove
        # its result. Runner reads this boundary independently of grid fitting.
        self._opening_input_pending = False
        self.frame_guard = None
        controls = profile.get("panel_controls")
        if not isinstance(controls, dict):
            raise ValueError("Настройки открытия панелей должны быть объектом.")
        self.controls = {}
        for key in ("inventory_open", "storage_open"):
            spec = controls.get(key)
            if spec is not None:
                steps = spec if isinstance(spec, list) else [spec]
                if not 1 <= len(steps) <= (2 if key == "inventory_open" else 1):
                    raise ValueError("Нужна одна кнопка хранилища или не более двух шагов открытия инвентаря.")
                samples = []
                for step in steps:
                    name = step if isinstance(step, str) else step.get("image") if isinstance(step, dict) else None
                    if not isinstance(name, str) or not name:
                        raise ValueError("Не указан образец кнопки открытия панели.")
                    max_error = step.get("max_rmse", 10) if isinstance(step, dict) else 10
                    if (isinstance(max_error, bool) or not isinstance(max_error, (int, float))
                            or not math.isfinite(max_error) or not 0 <= max_error <= 20):
                        raise ValueError("Порог кнопки открытия должен быть конечным числом от 0 до 20.")
                    samples.append((self._image(name), float(max_error)))
                self.controls[key] = samples
        anchors = profile.get("anchors", [])
        if len(anchors) != 2:
            raise ValueError("Для автоматического открытия нужны два отдельных заголовка панелей.")
        self.headers = [self._image(a["image"]) for a in anchors]
        self.layout = None
        if profile.get("automatic_layout") is True:
            from auto_layout import AutoLayout
            self.layout = AutoLayout(self.profile, self.directory)
        self._parking_specs = None

    def _parking_models(self):
        if self._parking_specs is not None:
            return self._parking_specs
        from paths import resource_path
        assets = resource_path("preset_assets").resolve()
        fingerprints = [hashlib.sha256(sample.tobytes()).hexdigest() for sample in self.headers]
        known = []
        for pane in ("inventory", "storage"):
            with Image.open(assets / (pane + "_header.png")) as sample:
                known.append(hashlib.sha256(sample.convert("RGB").tobytes()).hexdigest())
        if not any(a == b for a, b in zip(fingerprints, known)):
            self._parking_specs = ()  # Fixed/legacy custom profiles keep their old flow.
            return self._parking_specs
        path = assets / "header_parking.json"
        if not path.is_file() or path.stat().st_size > 64000:
            raise RuntimeError("Встроенные образцы безопасной парковки недоступны.")
        model = json.loads(path.read_text(encoding="utf-8"))
        records = model.get("records")
        if model.get("schema") != 1 or not isinstance(records, list) or len(records) != 6:
            raise RuntimeError("Некорректная модель безопасной парковки.")
        offsets = {"inventory": (378, 6, 418, 34), "storage": (334, 5, 364, 33)}
        result, identifiers = [], set()
        for record in records:
            spec = dict(record)
            pane, name, identifier = spec.get("pane"), spec.get("image"), spec.get("id")
            if (pane not in offsets or not isinstance(name, str) or Path(name).name != name
                    or "/" in name or "\\" in name or not name.endswith(".png")
                    or not isinstance(identifier, str) or identifier in identifiers):
                raise RuntimeError("Некорректный образец безопасной парковки.")
            scale = spec.get("canonical_scale")
            if (isinstance(scale, bool) or not isinstance(scale, (int, float))
                    or not math.isfinite(scale) or not .35 <= scale <= 1.5):
                raise RuntimeError("Некорректный масштаб безопасной парковки.")
            anchor, rect = Rect(*spec["source_anchor_rect"]), Rect(*spec["source_rect"])
            expected = Rect(anchor.left + round(offsets[pane][0] * scale),
                            anchor.top + round(offsets[pane][1] * scale),
                            anchor.left + round(offsets[pane][2] * scale),
                            anchor.top + round(offsets[pane][3] * scale))
            index = 0 if pane == "inventory" else 1
            if (rect != expected or spec.get("canonical_pixels_sha256") != known[index]
                    or min(rect.width, rect.height) < 1 or max(rect.width, rect.height) > 150):
                raise RuntimeError("Парковка выходит за доказанный неподвижный участок.")
            sample_path = assets / name
            if (not sample_path.is_file() or sample_path.stat().st_size > 100000
                    or hashlib.sha256(sample_path.read_bytes()).hexdigest() != spec.get("sample_sha256")):
                raise RuntimeError("Образец безопасной парковки изменён или отсутствует.")
            with Image.open(sample_path) as sample:
                if sample.size != (rect.width, rect.height):
                    raise RuntimeError("Размер образца безопасной парковки не совпал.")
                spec["sample"] = sample.convert("RGB")
            result.append(spec)
            identifiers.add(identifier)
        self._parking_specs = tuple(result)
        return self._parking_specs

    def _blank_target(self, image, matches):
        from auto_layout import ScaleMatch, affine_rect, control_error
        specs = self._parking_models()
        fingerprints = [hashlib.sha256(sample.tobytes()).hexdigest() for sample in self.headers]
        for index, pane in ((1, "storage"), (0, "inventory")):
            match = matches[index]
            if match is None:
                continue
            choices = []
            for spec in specs:
                if spec["pane"] != pane or spec["canonical_pixels_sha256"] != fingerprints[index]:
                    continue
                scale = match.scale / spec["canonical_scale"]
                if not .35 <= scale <= 1.5:
                    continue
                source, anchor = Rect(*spec["source_rect"]), Rect(*spec["source_anchor_rect"])
                rect = affine_rect(source, anchor, ScaleMatch(match.rect, scale, 0, 0))
                if not rect.fits(*image.size):
                    continue
                # The rendered pointer/glow is up to 75 physical pixels wide.
                # Never park its centre within 40 pixels of a caption anchor.
                x, y = rect.center
                if any(other is not None and other.rect.left - 40 <= x < other.rect.right + 40
                       and other.rect.top - 40 <= y < other.rect.bottom + 40 for other in matches):
                    continue
                try:
                    error = control_error(image, rect, spec["sample"], source, anchor,
                                          match.rect, scale, anchor_phase=match.phase)
                except ValueError:
                    continue
                if error <= 10:
                    choices.append((error, spec["id"], rect))
            if choices:
                _, identifier, rect = min(choices, key=lambda value: value[0])
                target = ActivationTarget(rect, pane, match.rect, fingerprints[index])
                return target, identifier
        return None, None

    def parking_point(self, profile):
        """Use adopted proof for harmless moves, never recheck an occupied patch."""
        spec = profile.get("layout_parking")
        if not isinstance(spec, dict):
            self._not_ready("Безопасное место парковки не подтверждено.")
        model = next((record for record in self._parking_models() if record["id"] == spec.get("id")), None)
        if model is None:
            self._not_ready("Безопасное место парковки не подтверждено.")
        from auto_layout import ScaleMatch, affine_rect
        pane = model["pane"]
        index = 0 if pane == "inventory" else 1
        anchor = Rect(*profile["anchors"][index]["rect"])
        scale = profile["layout_scales"][pane] / model["canonical_scale"]
        expected = affine_rect(Rect(*model["source_rect"]), Rect(*model["source_anchor_rect"]),
                               ScaleMatch(anchor, scale, 0, 0))
        if (spec.get("pane") != pane or spec.get("anchor_rect") != list(anchor.as_box())
                or spec.get("anchor_proof") != model["canonical_pixels_sha256"]
                or spec.get("rect") != list(expected.as_box()) or not expected.fits(*profile["size"])):
            self._not_ready("Безопасное место парковки изменилось.")
        return self._portrait_hover_point(Rect(*profile['anchors'][0]['rect']),
                                          profile['layout_scales']['inventory'], profile['size'])

    @staticmethod
    def _portrait_hover_point(anchor, scale, size):
        # A harmless hover in Character's portrait, well left of all item
        # cells. Unlike the narrow header bar at small scales, this keeps
        # Unity's fixed-size software-pointer glow away from grid separators.
        # This point is NEVER an activation or mouse-button target.
        point = (anchor.left + round(80*scale), anchor.top + round(170*scale))
        if not (0 <= point[0] < size[0] and 0 <= point[1] < size[1]):
            raise RuntimeError('Portrait hover point leaves the current client')
        return point

    def _image(self, name):
        path = (self.directory / name).resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError("Образец должен быть внутри папки профиля.")
        with Image.open(path) as image:
            return image.convert("RGB")

    def capture(self):
        self.check_stop()
        image = self.desktop.capture(self.handle)
        if self.frame_guard is not None:
            self.frame_guard(image)
        self.check_size(image)
        return image

    def check_size(self, image):
        # Header offsets establish panel translation, not cell pitch. The
        # reference screenshot may be cropped; only the calibrated native
        # client size is meaningful. Never infer scale from frame ratios.
        if self.layout is None and image.size != tuple(self.profile["size"]):
            raise RuntimeError(CALIBRATION_REQUIRED)

    def check_layout(self, image, profile=None):
        """Check adopted physical geometry without adopting a fresh transform."""
        self.check_stop()
        if self.frame_guard is not None:
            self.frame_guard(image)
        if self.layout is not None:
            self.layout.validate_current(image, profile or self.profile)
        else:
            self.check_size(image)

    def locate(self, image):
        self.check_stop()
        if self.layout is not None:
            return [match.rect if match is not None else None
                    for match in self.layout.locate_headers(image)]
        return [locate_unique(image, sample) for sample in self.headers]

    def activation_target(self, image):
        """Pure visible-pixel evidence for the separate native activation guard.

        This neither opens panels nor relaxes foreground checks for transfers.
        Native code must bind and revalidate the evidence on two fresh frames.
        """
        if self.layout is not None:
            from auto_layout import LayoutError
            try:
                matches = self.layout.locate_headers(image)
                if self._parking_models():
                    target = self._blank_target(image, matches)[0]
                    self._activation_pixels = None if target is None else (
                        image.size, target, image.crop(target.anchor_rect.as_box()).tobytes(),
                        image.crop(target.rect.as_box()).tobytes())
                    return target
                found = [match.rect if match is not None else None for match in matches]
            except LayoutError:
                return None
        else:
            if image.size != tuple(self.profile["size"]):
                return None
            try:
                found = [locate_unique(image, sample) for sample in self.headers]
            except RuntimeError:
                # locate_unique's only RuntimeError is its explicit duplicate
                # match guard. Ambiguity supplies no activation coordinate.
                return None
        for pane, rect in (("storage", found[1]), ("inventory", found[0])):
            if rect is not None and rect.fits(*image.size):
                return ActivationTarget(rect, pane)
        return None

    def recheck_activation_target(self, image, target):
        """Fresh exact caption AND blank pixels; no repeat global scale search."""
        cached = getattr(self, '_activation_pixels', None)
        if (cached is None or cached[:2] != (image.size, target)
                or target.anchor_rect is None):
            return None
        if (image.crop(target.anchor_rect.as_box()).tobytes() != cached[2]
                or image.crop(target.rect.as_box()).tobytes() != cached[3]):
            return None
        return target

    def locate_control(self, image, sample, max_error):
        if self.layout is not None:
            from auto_layout import match_multiscale
            from neutral_panels import locate_storage_opener
            neutral = locate_storage_opener(image, sample, max_error)
            if neutral is not NotImplemented:
                return neutral
            match = match_multiscale(image, sample, max_rmse=max_error)
            return match.rect if match is not None else None
        return locate_unique(image, sample, max_rmse=max_error)

    def _not_ready(self, message):
        # A missing visible control/header can become available on a later
        # fresh attempt. Configuration errors retain their ordinary exception.
        if self.layout is not None:
            from auto_layout import LayoutError
            raise LayoutError(message)
        raise RuntimeError(message)

    @property
    def opening_input_pending(self):
        return self._opening_input_pending

    def _open(self, key, image, step, *, next_step=None):
        if self.opening_input_pending:
            raise RuntimeError("Открытие игровой панели не подтвердилось. Повторного клика не будет.")
        if step is None:
            raise RuntimeError("Панель закрыта или перекрыта; нет подтверждённого образца её открытия.")
        sample, max_error = step
        rect = self.locate_control(image, sample, max_error)
        if rect is None:
            self._not_ready("Кнопка открытия панели не найдена. Клик отменён.")
        # A second frame must still contain the same complete button immediately
        # before the strict native click. Window size changes cancel this attempt.
        fresh = self.capture()
        if fresh.size != image.size:
            self._not_ready("Кнопка открытия панели изменилась. Клик отменён.")
        headers = self.locate(fresh)
        if headers[0 if key == "inventory_open" else 1] is not None:
            return fresh
        if (key == "inventory_open" and next_step is not None
                and self.locate_control(fresh, next_step[0], next_step[1]) is not None):
            # A menu can finish appearing between the initial and fresh frame.
            # Its proved next control avoids toggling that menu closed again.
            self.check_stop()
            return fresh
        if key == "storage_open" and headers[0] is None:
            self._not_ready("Инвентарь перекрыт или закрыт. Клик хранилища отменён.")
        if self.locate_control(fresh, sample, max_error) != rect:
            self._not_ready("Кнопка открытия панели изменилась. Клик отменён.")
        self.check_stop()
        self._opening_input_pending = True
        try:
            self.desktop.click(self.handle, rect.center, fresh.size)
        except Exception as error:
            from windows import WindowGeometryChanged, FocusUnavailable
            from input_activity import UserActivityInterrupted
            # Only these typed native guards guarantee no accepted down.
            # A generic exception can follow input and must retain the boundary.
            if (isinstance(error, (WindowGeometryChanged, FocusUnavailable, UserActivityInterrupted))
                    and error.input_issued is False):
                self._opening_input_pending = False
            raise
        self.wait(0.6)
        opened = self.capture()
        headers = self.locate(opened)
        confirmed = headers[0 if key == "inventory_open" else 1] is not None
        if not confirmed and key == "inventory_open" and next_step is not None:
            confirmed = self.locate_control(opened, next_step[0], next_step[1]) is not None
        self.check_stop()
        if not confirmed:
            self._not_ready("Открытие инвентаря не подтвердилось. Повторного клика не будет."
                            if key == "inventory_open" else
                            "Открытие хранилища не подтвердилось. Повторного клика не будет.")
        self._opening_input_pending = False
        return opened

    def prepare(self, image=None):
        self.check_stop()
        if self.opening_input_pending:
            raise RuntimeError("Открытие игровой панели не подтвердилось. Повторного клика не будет.")
        image = self.capture() if image is None else image
        if self.frame_guard is not None:
            self.frame_guard(image)
        self.check_size(image)
        found = self.locate(image)
        parking, parking_id = None, None
        # Clear hover tooltips on a proven static header before deciding a panel
        # is closed. A missing/ambiguous header never supplies a cursor target.
        if (any(rect is not None for rect in found)
                and (self.layout is not None or any(rect is None for rect in found))):
            static = next(rect for rect in found if rect is not None)
            if self.layout is not None and self._parking_models():
                parking, parking_id = self._blank_target(image, self.layout.locate_headers(image))
                if parking is None:
                    self._not_ready("Безопасное место парковки закрыто или перекрыто.")
                static = parking.rect
            self.check_stop()
            # Unity's software pointer may ignore SetCursorPos-only movement.
            # The native park path sends a tagged movement and rechecks user
            # activity/focus/geometry, so stale hover cannot cover the opener
            # result. Minimal offline desktops retain their move-only boundary.
            point = static.center
            if self.layout is not None and self._parking_models():
                inventory = self.layout.locate_headers(image)[0]
                if inventory is not None:
                    point = self._portrait_hover_point(inventory.rect, inventory.scale, image.size)
            mover = getattr(self.desktop, "park", self.desktop.move)
            mover(self.handle, point, image.size)
            self.wait(0.2)
            image = self.capture()
            found = self.locate(image)
        if found[0] is None:
            steps = self.controls.get("inventory_open")
            if not steps:
                raise RuntimeError("Инвентарь закрыт или перекрыт; нет подтверждённых кнопок открытия.")
            # A menu may already be open from a previous explicit request. Its
            # proven Character button avoids toggling that menu closed again.
            start = (1 if len(steps) == 2 and
                     self.locate_control(image, steps[1][0], steps[1][1]) is not None else 0)
            for index in range(start, len(steps)):
                next_step = steps[index + 1] if index + 1 < len(steps) else None
                image = self._open("inventory_open", image, steps[index], next_step=next_step)
                found = self.locate(image)
                if found[0] is not None:
                    break
            if found[0] is None:
                self._not_ready("Открытие инвентаря не подтвердилось. Повторного клика не будет.")
        if found[1] is None:
            steps = self.controls.get("storage_open")
            image = self._open("storage_open", image, steps[0] if steps else None)
            found = self.locate(image)
            if any(rect is None for rect in found):
                self._not_ready("Открытие хранилища не подтвердилось. Повторного клика не будет.")
        if self.layout is None:
            profile = self.translated_profile(image.size, found)
        elif not self._parking_models():
            profile = self.layout.resolve(image)
        else:
            from auto_layout import LayoutError
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    profile = self.layout.resolve(image)
                    break
                except LayoutError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if getattr(self.desktop, 'local_layout_diagnostics', False) is True:
                            from layout_diagnostics import save_grid_failure
                            save_grid_failure(self.directory, self.layout, image)
                        raise
                    self.wait(min(.1, remaining))
                    image = self.capture()  # Foreground/geometry/F8 remain strict.
        if self.layout is not None and self._parking_models():
            if parking is None:
                parking, parking_id = self._blank_target(image, self.layout.locate_headers(image))
            if parking is None:
                self._not_ready("Безопасное место парковки не подтверждено.")
            index = 0 if parking.pane == "inventory" else 1
            if profile["anchors"][index]["rect"] != list(parking.anchor_rect.as_box()):
                self._not_ready("Ориентир безопасной парковки изменился.")
            profile["layout_parking"] = {"id": parking_id, "pane": parking.pane,
                "rect": list(parking.rect.as_box()), "anchor_rect": list(parking.anchor_rect.as_box()),
                "anchor_proof": parking.anchor_proof}
        return image, profile

    def translated_profile(self, size, found):
        if tuple(size) != tuple(self.profile["size"]):
            raise RuntimeError(CALIBRATION_REQUIRED)
        result = copy.deepcopy(self.profile)
        offsets = []
        for spec, rect in zip(result["anchors"], found):
            original = Rect(*spec["rect"])
            offsets.append((rect.left - original.left, rect.top - original.top))
            spec["rect"] = list(rect.as_box())
        def shifted(coords, index):
            return list(Rect(*coords).translated(*offsets[index]).as_box())
        for index, name in enumerate(("inventory", "storage")):
            result[name]["rect"] = shifted(result[name]["rect"], index)
        for key in ("sort_button", "transfer_button"):
            if result.get(key):
                result[key]["rect"] = shifted(result[key]["rect"], 0)
        for page in result.get("pages", []):
            for key in ("button", "marker"):
                page[key] = shifted(page[key], 1)
        result["size"] = list(size)
        rects = [result[name]["rect"] for name in ("inventory", "storage")]
        rects += [anchor["rect"] for anchor in result["anchors"]]
        rects += [result[key]["rect"] for key in ("sort_button", "transfer_button") if result.get(key)]
        rects += [page[key] for page in result.get("pages", []) for key in ("button", "marker")]
        if any(not Rect(*coords).fits(*size) for coords in rects):
            raise RuntimeError("Панели видны не полностью или изменился масштаб. Клики отменены.")
        def overlaps(a, b):
            return a.left < b.right and a.right > b.left and a.top < b.bottom and a.bottom > b.top
        grids = [Rect(*result[name]["rect"]) for name in ("inventory", "storage")]
        if overlaps(*grids) or any(overlaps(Rect(*a["rect"]), grid)
                                  for a in result["anchors"] for grid in grids):
            raise RuntimeError("Панели перекрывают сетку предметов. Клики отменены.")
        return result
