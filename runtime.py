"""One-at-a-time transfer runner; Windows access is injected for testing."""
from __future__ import annotations

import json
import copy
import math
import logging
import hashlib
import threading
import time
from dataclasses import replace
from pathlib import Path

from PIL import Image

from core import (Rect, GridSpec, TemplateSet, ClassifierConfig, image_rmse,
                  GridScan, CellState, classify_cell, validate_calibration,
                  choose_transfer, verify_transfer)
from helper_config import migrate_profile, validate_interval
from input_activity import UserActivityInterrupted
from windows import (FocusUnavailable, WindowGeometryChanged, CursorPositionChanged,
                     ForegroundWindowChanged)
from inventory_cycle import run_inventory_cycle
from storage_cycle import find_storage_capacity
from inventory_motion_policy import MAXIMUM_WHEEL_ROW_FRACTION, MAXIMUM_BULK_PULSES


def make_grid(data):
    return GridSpec(Rect(*data["rect"]), int(data["rows"]), int(data["columns"]))


class LayoutChangedDuringOperation(RuntimeError):
    """The current physical layout cannot be used for this operation."""


class SafeLayoutChange(LayoutChangedDuringOperation):
    """A cycle may reacquire visible geometry before transfer input is issued."""


class SafePreflightChange(RuntimeError):
    """Fresh observations changed before any item transfer input was sent."""


LAYOUT_RETRY_SECONDS = 2.0
LAYOUT_READBACK_SECONDS = 4.0
LAYOUT_WAITING = "Ожидаю, пока игровые панели станут видны. Проверка повторится автоматически."
FOCUS_RETRY_SECONDS = 2.0
FOCUS_WAITING = "Ожидаю доступа к игре."
INVENTORY_PENDING = "Полная проверка инвентаря пока не подтверждена. Повторю в следующем цикле."
PREFLIGHT_WAITING = "Сумка обновляется. Повторю распознавание перед переносом."
PREFLIGHT_ATTEMPTS = 3
SAFE_QUICK_RETRY_ATTEMPTS = 3


class Runner:
    def __init__(self, desktop, handle, profile, directory, stop_event=None, log=None,
                 state_callback=None, handle_resolver=None):
        self.desktop, self.handle = desktop, handle
        self.profile = migrate_profile(profile)
        profile = self.profile
        from paths import canonical_profile_directory
        probe_name = next(iter(profile.get("inventory_empty", [])), None)
        if probe_name is None:
            probe_name = next((name for names in profile.get("tiers", {}).values()
                               for name in names), None)
        self.directory = canonical_profile_directory(directory, probe_name)
        self.stop = stop_event or threading.Event()
        self.log = log or (lambda message: None)
        self.state_callback = state_callback or (lambda payload: None)
        self.handle_resolver = handle_resolver
        self.panel_controller = None
        self.include_gems = True
        self.filters = profile["transfer_filters"]
        self.size = tuple(profile["size"])
        self.inv = make_grid(profile["inventory"])
        self.store = make_grid(profile["storage"])
        # Saved calibration and all recognition samples remain canonical. Only
        # a resolved physical layout is adopted in memory at a cycle boundary.
        self.canonical_profile = copy.deepcopy(profile)
        self.canonical_inv, self.canonical_store = self.inv, self.store
        self.layout_scales = {"inventory": 1.0, "storage": 1.0}
        self.adaptive_tier_matcher = None
        self.badge = Rect(*profile["badge"])
        self.inv_config = ClassifierConfig(self.badge)
        self.store_config = ClassifierConfig()
        self.inv_templates = TemplateSet(
            {int(key): [self._image(p) for p in paths]
             for key, paths in profile.get("tiers", {}).items()},
            [self._image(p) for p in profile.get("inventory_empty", [])])
        if profile.get("automatic_layout") is True:
            from supplemental_tiers import load_supplemental_tiers
            from inventory_samples import load_inventory_empty_samples
            first_cell = self.canonical_inv.cell_rect(0)
            supplements = load_supplemental_tiers(self.badge, (first_cell.width, first_cell.height))
            for tier, samples in supplements.items():
                targets = self.inv_templates.tier_samples.setdefault(tier, [])
                pixels = {sample.tobytes() for sample in targets}
                targets.extend(sample for sample in samples if sample.tobytes() not in pixels)
            empty_pixels = {sample.tobytes() for sample in self.inv_templates.empty_samples}
            for sample in load_inventory_empty_samples((first_cell.width, first_cell.height)):
                pixels = sample.tobytes()
                if pixels not in empty_pixels:
                    self.inv_templates.empty_samples.append(sample)
                    empty_pixels.add(pixels)
        self.store_templates = TemplateSet({}, [self._image(p) for p in profile.get("storage_empty", [])])
        self.anchors = [(Rect(*a["rect"]), self._image(a["image"]))
                        for a in profile.get("anchors", [])]
        self.gear = None  # Diagnostic glyph matching remains available.
        if profile.get("equipment_label_samples"):
            from gearlabel import EquipmentLabelMatcher
            self.gear = EquipmentLabelMatcher(self.directory, profile["equipment_label_samples"])
        errors = validate_calibration(self.size, self.inv, self.inv_templates, self.inv_config)
        errors += validate_calibration(self.size, self.store, self.store_templates, self.store_config, require_tiers=False)
        if len(self.anchors) < 2:
            errors.append("Нужны отдельные неподвижные ориентиры инвентаря и хранилища.")
        for rect, sample in self.anchors:
            if not rect.fits(*self.size) or sample.size != (rect.width, rect.height):
                errors.append("Некорректный ориентир окна.")
            for grid in [self.inv, self.store]:
                g = grid.rect
                if rect.left < g.right and rect.right > g.left and rect.top < g.bottom and rect.bottom > g.top:
                    errors.append("Ориентир должен находиться вне сеток предметов.")
        for page in self.profile.get("pages", []):
            if not Rect(*page["button"]).fits(*self.size) or not Rect(*page["marker"]).fits(*self.size):
                errors.append("Кнопка страницы выходит за пределы окна.")
        sort = self.profile.get("sort_button")
        if sort and not Rect(*sort["rect"]).fits(*self.size):
            errors.append("Кнопка сортировки выходит за пределы окна.")
        if self.profile.get("mode") not in {"right", "drag", "button"}:
            errors.append("Выбери способ переноса.")
        if self.profile.get("mode") == "button":
            button = self.profile.get("transfer_button")
            if not button or not Rect(*button["rect"]).fits(*self.size):
                errors.append("Запомни кнопку переноса после выбора вещи.")
        if errors:
            raise ValueError("\n".join(errors))
        if profile.get("panel_controls") is not None:
            from panels import PanelController
            self.panel_controller = PanelController(desktop, handle, profile, self.directory,
                                                    self.check_stop, self.wait)

    def state(self, name, message, next_check_at=None):
        callback = getattr(self, "state_callback", None)
        if callback is not None:
            callback({"state": name, "message": message,
                      "next_check_at": next_check_at})

    def prepare_panels(self):
        """Re-anchor a cycle in memory; never rewrite calibration or trust flags."""
        if self.panel_controller is None:
            image = self.capture()
            self.check_anchors(image)
            return image
        self.state("pending", "Проверяю и открываю панели игры.")
        self.panel_controller.handle = self.handle
        # Resolve the current wait method at call time (also useful for fake tests).
        self.panel_controller.wait = self.wait
        image, profile = self._layout_guard(self.panel_controller.prepare)
        self._adopt_layout(image, profile)
        return image

    def _layout_guard(self, callback, *args, **kwargs):
        """Keep technical vision diagnostics out of visible action errors."""
        try:
            return callback(*args, **kwargs)
        except self._layout_error_type() as error:
            if self.canonical_profile.get("automatic_layout") is not True:
                raise
            from panels import CALIBRATION_REQUIRED
            self.log("Диагностика геометрии: " + str(error))
            raise LayoutChangedDuringOperation(CALIBRATION_REQUIRED) from error

    def _adopt_layout(self, image, profile):
        """Adopt only physical geometry, preserving the canonical calibration."""
        inv, store = make_grid(profile["inventory"]), make_grid(profile["storage"])
        scales = profile.get("layout_scales", {"inventory": 1.0, "storage": 1.0})
        if (not isinstance(scales, dict) or set(scales) != {"inventory", "storage"}
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or value <= 0 for value in scales.values())):
            raise RuntimeError("Масштаб панелей не подтверждён. Клики отменены.")
        if tuple(profile["size"]) != image.size:
            raise RuntimeError("Размер обнаруженного интерфейса не совпал со снимком. Клики отменены.")
        for pane, physical, canonical in (("inventory", inv, self.canonical_inv),
                                           ("storage", store, self.canonical_store)):
            viewport = profile.get("inventory_viewport") if pane == "inventory" else None
            shifted = (self.canonical_profile.get("automatic_layout") is True
                       and isinstance(viewport, dict)
                       and physical.rows in (canonical.rows - 1, canonical.rows)
                       and physical.rect != Rect(*viewport.get("rect", physical.rect.as_box())))
            if ((physical.columns != canonical.columns or
                 (physical.rows != canonical.rows and not shifted))
                    or not physical.rect.fits(*image.size)
                    or any(min(physical.cell_rect(i).width, physical.cell_rect(i).height) < 1
                           for i in range(physical.count))):
                raise RuntimeError("Границы сетки не подтверждены. Клики отменены.")
            # A pane-specific UI scale comes from observed headers AND grids,
            # never from the whole screenshot/client dimensions.
            expected_height = canonical.rect.height * scales[pane]
            if shifted:
                pitch = viewport.get("pitch")
                if (isinstance(pitch, bool) or not isinstance(pitch, (int, float))
                        or not math.isfinite(pitch) or pitch <= 0
                        or abs(pitch - expected_height / canonical.rows) > 1.5 * scales[pane]):
                    raise RuntimeError("Шаг сетки не совпал с масштабом панели. Клики отменены.")
                expected_height = pitch * physical.rows
            if (abs(physical.rect.width - canonical.rect.width * scales[pane]) > 2
                    or abs(physical.rect.height - expected_height) > 2):
                raise RuntimeError("Шаг сетки не совпал с масштабом панели. Клики отменены.")
        if len(profile.get("anchors", [])) != len(self.anchors):
            raise RuntimeError("Открытие панелей не подтвердилось. Клики отменены.")
        anchors = [(Rect(*a["rect"]), sample)
                   for a, (_, sample) in zip(profile["anchors"], self.anchors)]
        checker = getattr(self.panel_controller, "check_layout", None)
        if checker is None and (image.size != self.size or any(value != 1 for value in scales.values())):
            raise RuntimeError("Масштаб панелей не подтверждён. Клики отменены.")
        # AutoLayout verifies proved sampling phase AND independent grid pitch.
        # A second unphased resize comparison would discard that evidence.
        automatic = self.canonical_profile.get("automatic_layout") is True
        if any(not rect.fits(*image.size) for rect, _ in anchors):
            raise RuntimeError("Открытие панелей не подтвердилось. Клики отменены.")
        if not (automatic and checker is not None):
            if any(self.sample_error(image, rect, sample) > 10 for rect, sample in anchors):
                raise RuntimeError("Открытие панелей не подтвердилось. Клики отменены.")
        if checker is not None:
            self._layout_guard(checker, image, profile)
        self.profile, self.size = copy.deepcopy(profile), image.size
        self.inv, self.store, self.anchors = inv, store, anchors
        self.layout_scales = dict(scales)

    def _image(self, name):
        path = (self.directory / name).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            raise ValueError("Образец должен быть внутри папки профиля.")
        return Image.open(path).convert("RGB")

    def check_stop(self):
        # A supplied-image preview may intentionally have no desktop. It still
        # honours explicit cancellation; native runners retain every desktop
        # stop/activity guard before capture or input.
        if self.stop.is_set() or (self.desktop is not None and self.desktop.stop_pressed()):
            raise InterruptedError("Остановлено (F8 / кнопка Стоп).")
        checker = getattr(self.desktop, "check_background_cycle", None)
        if checker is not None:
            checker()

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_stop()
            self.stop.wait(min(0.1, max(0, deadline - time.monotonic())))

    def capture(self):
        self.check_stop()
        image = self.desktop.capture(self.handle)
        if image.size != self.size:
            from panels import CALIBRATION_REQUIRED
            if self.canonical_profile.get("automatic_layout") is True:
                raise LayoutChangedDuringOperation(CALIBRATION_REQUIRED)
            raise RuntimeError(CALIBRATION_REQUIRED)
        return image

    def check_anchors(self, image):
        checker = getattr(self.panel_controller, "check_layout", None)
        if self.canonical_profile.get("automatic_layout") is True and checker is not None:
            self._layout_guard(checker, image, self.profile)
            return
        for rect, sample in self.anchors:
            if self.sample_error(image, rect, sample) > 10:
                raise RuntimeError("Интерфейс изменился или перекрыт: ориентир не совпал.")
        if checker is not None:
            self._layout_guard(checker, image, self.profile)

    def _capture_valid_layout(self, *, deadline=None):
        """Read through item glints, retaining the exact adopted layout."""
        if deadline is None:
            deadline = time.monotonic() + LAYOUT_READBACK_SECONDS
        while True:
            image = self.capture()  # Native focus/PID/size/F8 failures propagate.
            try:
                self.check_anchors(image)
                return image
            except LayoutChangedDuringOperation:
                if self.canonical_profile.get("automatic_layout") is not True:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                self.wait(min(.1, remaining))

    def _park_pointer(self, point):
        # Tagged movement also updates the rendered pointer. SetCursorPos alone
        # can leave a hover effect covering the inventory readback.
        park = getattr(self.desktop, "park", None)
        (park if callable(park) else self.desktop.move)(self.handle, point, self.size)

    def _park_and_capture(self, index=0, *, legacy_wait=.2):
        controller = self.panel_controller
        point = self.anchors[index][0].center
        settle = False
        if (controller is not None and getattr(controller, "layout", None) is not None
                and callable(getattr(controller, "parking_point", None))
                and self.profile.get("layout_parking") is not None):
            point = self._layout_guard(controller.parking_point, self.profile)
            settle = True
        self._park_pointer(point)
        if not settle:
            self.wait(legacy_wait)
        # Only visual readback is retried. A delayed software cursor can still
        # cross a grid after the one harmless move; no button is repeated.
        deadline = time.monotonic() + LAYOUT_READBACK_SECONDS
        while True:
            frame = self.capture()  # Native size/focus/PID/F8 guards are never caught.
            try:
                self.check_anchors(frame)
                return frame
            except LayoutChangedDuringOperation:
                if not settle:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                self.wait(min(.1, remaining))

    def sample_error(self, image, rect, sample, *, pane=None, canonical_rect=None):
        """Compare a physical control/anchor to its canonical saved sample."""
        if not rect.fits(*image.size):
            raise RuntimeError("Элемент интерфейса выходит за окно. Клики отменены.")
        if self.canonical_profile.get("automatic_layout") is True and pane is not None:
            from auto_layout import control_error
            index = 0 if pane == "inventory" else 1
            return self._layout_guard(control_error,
                image, rect, sample, Rect(*canonical_rect),
                Rect(*self.canonical_profile["anchors"][index]["rect"]),
                self.anchors[index][0], self.layout_scales[pane],
                anchor_phase=tuple(self.profile.get("layout_anchor_phases", {}).get(pane, (0.0, 0.0))))
        physical_sample = sample
        if sample.size != (rect.width, rect.height):
            physical_sample = sample.resize((rect.width, rect.height), Image.Resampling.BILINEAR)
        return image_rmse(image.crop(rect.as_box()), physical_sample)

    def scan_pane(self, image, pane):
        physical, canonical, templates, config = (
            (self.inv, self.canonical_inv, self.inv_templates, self.inv_config)
            if pane == "inventory" else
            (self.store, self.canonical_store, self.store_templates, self.store_config))
        if not physical.rect.fits(*image.size):
            raise RuntimeError("Границы сетки не подтверждены. Клики отменены.")
        cells = []
        scale = self.layout_scales[pane]
        adaptive = (pane == "inventory" and self.canonical_profile.get("automatic_layout") is True
                    and scale != 1.0)
        if adaptive and self.adaptive_tier_matcher is None:
            from adaptive_tiers import AdaptiveTierMatcher
            from paths import resource_path
            self.adaptive_tier_matcher = AdaptiveTierMatcher(resource_path("preset_assets"))
        for index in range(physical.count):
            rect, reference = physical.cell_rect(index), canonical.cell_rect(index)
            actual_crop = image.crop(rect.as_box())
            crop = actual_crop
            # Normalise the FULL cell before slicing the narrow tier badge.
            # Its physical rect is retained for input and transfer verification.
            size = reference.width, reference.height
            if crop.size != size:
                crop = crop.resize(size, Image.Resampling.BILINEAR)
            if not adaptive:
                cells.append(classify_cell(crop, index, rect, templates, config))
                continue
            # Occupancy remains an independent full-cell comparison. Scaled
            # border context cannot turn an empty/uncertain tile into an item.
            occupancy = classify_cell(crop, index, rect, TemplateSet({}, templates.empty_samples), config)
            if occupancy.empty is not False:
                cells.append(occupancy)
                continue
            match = self.adaptive_tier_matcher.match(actual_crop, scale, canonical_size=size)
            tier, error, margin = match.tier, match.rmse, match.margin
            confident = (isinstance(tier, int) and not isinstance(tier, bool)
                         and tier in templates.tier_samples
                         and isinstance(error, (int, float)) and not isinstance(error, bool)
                         and math.isfinite(error) and 0 <= error <= config.max_tier_rmse
                         and isinstance(margin, (int, float)) and not isinstance(margin, bool)
                         and math.isfinite(margin) and margin >= config.min_margin)
            cells.append(CellState(index, rect, tier if confident else None, False,
                                   error, margin, match.reason))
        return GridScan(physical, tuple(cells))

    def scans(self, image):
        self.check_anchors(image)
        return self.scan_pane(image, "inventory"), self.scan_pane(image, "storage")

    def find_candidate(self, image, inv, store):
        if not store.empty_count:
            return None
        excluded = set()
        for cell in inv.cells:
            rule = self.filters.get(str(cell.tier))
            if cell.empty is not False or rule is None or not rule["enabled"]:
                excluded.add(cell.index)
                continue
            break
        return choose_transfer(inv, store, min_tier=3, excluded_inventory=excluded)

    def preview(self, image=None):
        layout = getattr(self.panel_controller, "layout", None)
        if layout is not None:
            self.check_stop()
            image = image if image is not None else self.desktop.capture(self.handle)
            profile = self._layout_guard(layout.resolve, image)
            self.check_stop()
            self._adopt_layout(image, profile)
        else:
            image = image if image is not None else self.capture()
        inv, store = self.scans(image)
        return image, inv, store, self.find_candidate(image, inv, store)

    def once(self, sort=True):
        return self._guarded_operation(self._once, sort)

    def _guarded_operation(self, callback, *args):
        self._transfer_input_issued = (getattr(self, "_transfer_input_issued", False)
                                       or self._panel_open_input_pending())
        if self._transfer_input_issued:
            # A fresh Start creates a new runner. Re-entering this runner must
            # never discard an earlier accepted/ambiguous receipt or replay it.
            raise RuntimeError("Предыдущий ввод не подтверждён. Повторного ввода не будет.")
        try:
            return callback(*args)
        except (LayoutChangedDuringOperation, self._layout_error_type(), WindowGeometryChanged) as error:
            if self.canonical_profile.get("automatic_layout") is True:
                from panels import CALIBRATION_REQUIRED
                # Technical vision details belong to the journal. The visible
                # cancellation message is the same localised text for single
                # and scheduled actions, including an unverified issued input.
                if not isinstance(error, LayoutChangedDuringOperation):
                    self.log("Диагностика геометрии: " + str(error))
                if isinstance(error, WindowGeometryChanged) and error.input_issued:
                    self._transfer_input_issued = True
                if self._panel_open_input_pending():
                    self._transfer_input_issued = True
                if not self._transfer_input_issued:
                    if isinstance(error, (CursorPositionChanged, ForegroundWindowChanged)):
                        raise  # Access deferral is distinct from layout fitting.
                    raise SafeLayoutChange(CALIBRATION_REQUIRED) from error
                raise LayoutChangedDuringOperation(CALIBRATION_REQUIRED) from error
            raise
        finally:
            # Panel controls own their per-step readback. An unknown native
            # error, Stop or failed recognition cannot erase an unproved click.
            if self._panel_open_input_pending():
                self._transfer_input_issued = True

    def _panel_open_input_pending(self):
        controller = getattr(self, "panel_controller", None)
        return getattr(controller, "opening_input_pending", False) is True

    @staticmethod
    def _layout_error_type():
        from auto_layout import LayoutError
        return LayoutError

    def _issue_transfer(self, callback, *args, **kwargs):
        previous_input = getattr(self, "_transfer_input_issued", False)
        # An unknown error can follow a sent down, so assume input was issued.
        # Only the native typed guard can prove this call sent no down at all.
        self._transfer_input_issued = True
        try:
            return callback(*args, **kwargs)
        except (WindowGeometryChanged, UserActivityInterrupted) as error:
            self._transfer_input_issued = (previous_input or self._panel_open_input_pending()
                                           or error.input_issued)
            raise

    def _verify_transfer_readback(self, image, inv, after_inv, store, after_store, candidate):
        """Read the destination tier only when an unrelated drop hides counts.

        Storage occupancy keeps its independent empty-cell classifier. This
        optional second proof additionally authenticates the destination badge
        using the existing inventory tier samples and unchanged score limits;
        it never turns an unknown badge into a successful transfer.
        """
        verification = verify_transfer(inv, after_inv, store, after_store, candidate)
        if (verification.ok or self.canonical_profile.get("automatic_layout") is not True
                or verification.reason != "inventory tier count did not decrease by exactly one"):
            return verification, after_store
        source = after_inv.cells[candidate.source_index]
        destination = after_store.cells[candidate.destination_index]
        if (source.empty is not True or source.tier is not None
                or destination.empty is not False or destination.tier is not None):
            return verification, after_store
        reference = self.canonical_inv.cell_rect(candidate.source_index)
        size = reference.width, reference.height
        actual_crop = image.crop(destination.rect.as_box())
        crop = actual_crop
        if crop.size != size:
            crop = crop.resize(size, Image.Resampling.BILINEAR)
        empty_samples = [sample if sample.size == size else
                         sample.resize(size, Image.Resampling.BILINEAR)
                         for sample in self.store_templates.empty_samples]
        evidence = classify_cell(crop, destination.index, destination.rect,
            TemplateSet(self.inv_templates.tier_samples, empty_samples), self.inv_config)
        if evidence.empty is not False:
            return verification, after_store
        if evidence.tier is None:
            # Storage uses a slightly larger item raster than the inventory.
            # Its physical border matcher handles that pane's OWN canonical
            # size and measured scale. Conflicting recognized tiers are never
            # replaced by whichever matcher happens to agree with a candidate.
            if getattr(self, "adaptive_tier_matcher", None) is None:
                from adaptive_tiers import AdaptiveTierMatcher
                from paths import resource_path
                self.adaptive_tier_matcher = AdaptiveTierMatcher(resource_path("preset_assets"))
            store_reference = self.canonical_store.cell_rect(destination.index)
            match = self.adaptive_tier_matcher.match(actual_crop, self.layout_scales["storage"],
                canonical_size=(store_reference.width, store_reference.height))
            if (isinstance(match.tier, int) and not isinstance(match.tier, bool)
                    and match.tier in self.inv_templates.tier_samples
                    and isinstance(match.rmse, (int, float)) and not isinstance(match.rmse, bool)
                    and math.isfinite(match.rmse) and 0 <= match.rmse <= self.inv_config.max_tier_rmse
                    and isinstance(match.margin, (int, float)) and not isinstance(match.margin, bool)
                    and math.isfinite(match.margin) and match.margin >= self.inv_config.min_margin):
                evidence = replace(evidence, tier=match.tier, rmse=match.rmse,
                                   margin=match.margin, reason=match.reason)
        if evidence.tier != candidate.tier:
            return verification, after_store
        cells = list(after_store.cells)
        cells[destination.index] = replace(destination, tier=evidence.tier,
            rmse=evidence.rmse, margin=evidence.margin, reason=evidence.reason)
        enriched = replace(after_store, cells=tuple(cells))
        return verify_transfer(inv, after_inv, store, enriched, candidate), enriched

    @staticmethod
    def _log_unverified_transfer(inv, after_inv, store, after_store, candidate, verification):
        """One bounded local audit after readback expires; never retry input."""
        def score(value):
            return value if isinstance(value, (int, float)) and math.isfinite(value) else None
        def pane(scan):
            return {"rect": scan.grid.rect.as_box(), "rows": scan.grid.rows,
                    "columns": scan.grid.columns, "tier_counts": scan.tier_counts,
                    "empty_count": scan.empty_count, "unknown_count": scan.unknown_count,
                    "cells": [{"index": cell.index, "tier": cell.tier, "empty": cell.empty,
                               "rmse": score(cell.rmse), "margin": score(cell.margin),
                               "reason": cell.reason} for cell in scan.cells]}
        audit = {"reason": verification.reason,
                 "candidate": {"tier": candidate.tier, "source_index": candidate.source_index,
                               "destination_index": candidate.destination_index,
                               "source_rect": candidate.source_rect.as_box(),
                               "destination_rect": candidate.destination_rect.as_box()},
                 "before": {"inventory": pane(inv), "storage": pane(store)},
                 "after": {"inventory": pane(after_inv), "storage": pane(after_store)}}
        logging.info("transfer_readback_unverified %s", json.dumps(audit, ensure_ascii=False,
                                                                  separators=(",", ":"), allow_nan=False))

    def _once(self, sort):
        self._candidate_free_scan = None
        if sort:
            self.state("working", "Сортирую оставшиеся предметы")
            if self.panel_controller is not None:
                self.prepare_panels()
            self.sort_inventory()
        self.state("working", "Распознаю предметы и проверяю хранилище.")
        selection = self._fresh_transfer_selection()
        if selection is None:
            self._candidate_free_scan = self._last_inventory_scan
            self.log("Предметов, подходящих выбранным тирам, нет.")
            return False
        candidate, before, inv, store = selection
        self.check_stop()
        mode = self.profile["mode"]
        source, destination = candidate.source_rect.center, candidate.destination_rect.center
        # Once the transfer call starts, even an exception may follow a sent
        # mouse-down. A timer must never retry that unverified item.
        if mode == "right":
            self._issue_transfer(self.desktop.click, self.handle, source, self.size, right=True)
        elif mode == "drag":
            self._issue_transfer(self.desktop.drag, self.handle, source, destination, self.size)
        else:
            self._issue_transfer(self.desktop.click, self.handle, source, self.size)
            self.wait(0.4)
            button = self.profile["transfer_button"]
            rect = Rect(*button["rect"])
            frame = self.capture()
            self.check_anchors(frame)
            if self.sample_error(frame, rect, self._image(button["image"]), pane="inventory",
                                 canonical_rect=self.canonical_profile["transfer_button"]["rect"]) > 10:
                raise RuntimeError("Кнопка переноса не совпала. Второй клик отменён.")
            self._issue_transfer(self.desktop.click, self.handle, rect.center, self.size)
        self.wait(0.9)
        after = self._park_and_capture(legacy_wait=.3)
        after_inv, after_store = self.scans(after)
        verification, verified_store = self._verify_transfer_readback(
            after, inv, after_inv, store, after_store, candidate)
        if not verification.ok and self.canonical_profile.get("automatic_layout") is True:
            # A click is never repeated. Only its guarded readback can settle.
            deadline = time.monotonic() + LAYOUT_READBACK_SECONDS
            while not verification.ok and time.monotonic() < deadline:
                self.wait(min(.1, deadline - time.monotonic()))
                after = self._capture_valid_layout(deadline=deadline)
                after_inv, after_store = self.scans(after)
                verification, verified_store = self._verify_transfer_readback(
                    after, inv, after_inv, store, after_store, candidate)
        if not verification.ok:
            self._log_unverified_transfer(inv, after_inv, store, verified_store, candidate, verification)
            raise RuntimeError("Перенос не подтверждён; повторных кликов не будет. " + verification.reason)
        self._transfer_input_issued = False
        self._last_inventory_scan = after_inv
        self.log(f"Перенесён T{candidate.tier}: сумка {candidate.source_index + 1} → хранилище {candidate.destination_index + 1}.")
        self.state("working", "Перенос подтверждён. Проверяю следующую вещь.")
        return True

    def _fresh_transfer_selection(self):
        """Discard stale decisions, never retry an issued item input.

        A passive drop or a classifier transition can change an unrelated cell.
        Automatic mode reacquires the whole baseline and requires a new matching
        pair under the ordinary geometry/focus/activity guards before acting.
        """
        automatic = self.canonical_profile.get("automatic_layout") is True
        before = self._capture_valid_layout()
        inv, store = self.scans(before)
        for attempt in range(PREFLIGHT_ATTEMPTS if automatic else 1):
            self._last_inventory_scan = inv
            eligible = any(cell.empty is False and cell.tier in (3, 4, 5, 6)
                           and self.filters.get(str(cell.tier), {}).get("enabled")
                           for cell in inv.cells)
            if not eligible:
                return None
            candidate = self.find_candidate(before, inv, store)
            if not store.empty_count:
                before, inv, store, candidate = self.next_storage_page(before)
                self._last_inventory_scan = inv
            if candidate is None:
                return None
            fresh = self._capture_valid_layout()
            inv2, store2 = self.scans(fresh)
            reason = None
            if [c.label for c in inv.cells] != [c.label for c in inv2.cells]:
                reason = "Состав сумки изменился во время распознавания. Перенос отменён."
            elif [c.empty for c in store.cells] != [c.empty for c in store2.cells]:
                reason = "Хранилище изменилось во время распознавания. Перенос отменён."
            else:
                current = self.find_candidate(fresh, inv2, store2)
                if (current is None or current.source_index != candidate.source_index
                        or current.destination_index != candidate.destination_index
                        or current.tier != candidate.tier):
                    reason = "Выбранная вещь изменилась. Перенос отменён."
                else:
                    return current, fresh, inv2, store2
            if not automatic:
                raise RuntimeError(reason)
            self.log("Диагностика повторного распознавания до переноса: " + reason)
            if attempt + 1 == PREFLIGHT_ATTEMPTS:
                raise SafePreflightChange(reason)
            self.check_stop()
            self.wait(.12)
            before, inv, store = fresh, inv2, store2

    def sort_inventory(self):
        spec = self.profile.get("sort_button")
        if not spec:
            return
        rect = Rect(*spec["rect"])
        frame = self._capture_valid_layout()
        self.check_anchors(frame)
        if self.sample_error(frame, rect, self._image(spec["image"]), pane="inventory",
                             canonical_rect=self.canonical_profile["sort_button"]["rect"]) > 10:
            raise RuntimeError("Кнопка сортировки не совпала с образцом. Клик отменён.")
        self._issue_transfer(self.desktop.click, self.handle, rect.center, self.size)
        self.wait(0.6)
        if self.profile.get("inventory_viewport") is not None:
            self._park_inventory_readback()
        else:
            self._park_and_capture()
        # The interface action has completed its guarded readback. A later
        # observation can be reacquired; an uncertain item input cannot.
        self._transfer_input_issued = False

    def _layout_binding(self, profile=None):
        profile = self.profile if profile is None else profile
        return (tuple(profile["size"]), tuple(tuple(anchor["rect"]) for anchor in profile["anchors"]),
                tuple(sorted(profile.get("layout_scales", {}).items())),
                tuple(profile.get("inventory_viewport", {}).get("rect", ())))

    def _park_inventory_readback(self):
        """Resolve a deliberate content scroll/sort without accepting window movement."""
        controller = self.panel_controller
        if controller is None or getattr(controller, "layout", None) is None:
            raise RuntimeError("Проверка инвентаря не завершена. Работа остановлена.")
        binding = self._layout_binding()
        parking = copy.deepcopy(self.profile.get("layout_parking"))
        point = (self._layout_guard(controller.parking_point, self.profile)
                 if parking is not None else self.anchors[0][0].center)
        self._park_pointer(point)
        self.wait(.2)
        deadline = time.monotonic() + LAYOUT_READBACK_SECONDS
        stable_profile = None
        stable_since = None
        stable_phase = None
        while True:
            image = self.capture()
            try:
                profile = self._layout_guard(controller.layout.resolve_inventory_scroll, image, self.profile)
                if self._layout_binding(profile) != binding:
                    raise LayoutChangedDuringOperation("Положение или масштаб игры изменились во время ввода.")
                if parking is not None:
                    profile["layout_parking"] = parking
                pose = (tuple(profile["inventory"]["rect"]), profile["inventory"]["rows"])
                phase = profile["inventory_viewport"]["phase"]
                pitch = profile["inventory_viewport"]["pitch"]
                phase_changed = (stable_phase is not None and
                    abs((phase - stable_phase + pitch / 2) % pitch - pitch / 2) > .75)
                if pose != stable_profile or phase_changed:
                    stable_profile, stable_since = pose, time.monotonic()
                    stable_phase = phase
                if time.monotonic() - stable_since < .3:
                    if time.monotonic() >= deadline:
                        raise LayoutChangedDuringOperation("Положение строк инвентаря ещё не стабильно.")
                    self.wait(.1)
                    continue
                self._adopt_layout(image, profile)
                return image
            except LayoutChangedDuringOperation:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                self.wait(min(.1, remaining))

    def _inventory_viewport_frame(self, image=None):
        from inventory_scroller import ViewportFrame
        from inventory_viewport import recognize_inventory_viewport
        image = self._capture_valid_layout() if image is None else image
        self.check_anchors(image)
        spec = self.profile.get("inventory_viewport")
        layout = getattr(self.panel_controller, "layout", None)
        if not isinstance(spec, dict) or layout is None:
            raise RuntimeError("Проверка инвентаря не завершена. Работа остановлена.")
        viewport = recognize_inventory_viewport(image, Rect(*spec["rect"]),
            self.canonical_inv.rows, self.canonical_inv.columns,
            layout.gaps["inventory"], self.layout_scales["inventory"])
        if viewport.grid != self.inv:
            raise LayoutChangedDuringOperation("Положение строк инвентаря изменилось без подтверждённой прокрутки.")
        identity = hashlib.sha256(json.dumps([self.handle, self._layout_binding(), viewport.identity],
                                              separators=(",", ":")).encode("ascii")).hexdigest()
        scan = self.scan_pane(image, "inventory")
        # The nominal header-bound pitch is stable across fitted raster phases.
        pitch = Rect(*spec["rect"]).height / self.canonical_inv.rows
        return ViewportFrame(scan=scan, geometry_identity=identity, phase=viewport.phase % pitch,
                             row_pitch_pixels=pitch, payload=(viewport, image))

    def _scroll_inventory(self, delta):
        before = self._inventory_viewport_frame()
        self.check_stop()
        self._issue_transfer(self.desktop.scroll, self.handle,
                             before.payload[0].viewport.center, self.size, delta)
        self.wait(.25)
        image = self._park_inventory_readback()
        after = self._inventory_viewport_frame(image)
        if after.geometry_identity != before.geometry_identity:
            raise LayoutChangedDuringOperation("Положение или масштаб игры изменились во время прокрутки.")
        self._transfer_input_issued = False
        return after

    def _scroll_inventory_bulk(self, delta, count):
        if (isinstance(delta, bool) or not isinstance(delta, int) or not 1 <= abs(delta) <= 53
                or isinstance(count, bool) or not isinstance(count, int)
                or not 2 <= count <= MAXIMUM_BULK_PULSES):
            raise ValueError("Bulk inventory input needs two to ten identical fine pulses")
        burst = getattr(self.desktop, "scroll_burst", None)
        if not callable(burst):
            raise RuntimeError("Guarded native inventory burst is unavailable")
        before = self._inventory_viewport_frame()
        self.check_stop()
        self._issue_transfer(burst, self.handle,
                             before.payload[0].viewport.center, self.size, delta, count)
        self.wait(.25)
        image = self._park_inventory_readback()
        after = self._inventory_viewport_frame(image)
        if after.geometry_identity != before.geometry_identity:
            raise LayoutChangedDuringOperation("Положение или масштаб игры изменились во время прокрутки.")
        self._inventory_bulk_receipt = (after.geometry_identity, delta, count)
        self._transfer_input_issued = False
        logging.info("Inventory bulk input: delta=%s pulses=%s guarded_geometry_readback=True", delta, count)
        # A matching geometry readback is not a displacement or endpoint.
        # The controller separately proves unique RGB overlap before walking.
        return after

    def _walk_inventory(self, *, sorted_after_sort=False):
        from inventory_scroller import InventoryScroller
        from inventory_viewport import (measure_inventory_motion, prove_inventory_translation,
                                        inventory_bulk_ready, prove_inventory_bulk_translation)
        from inventory_walk import (run_inventory_walk, certify_sorted_prefix,
                                    certify_full_inventory_top, InventoryWalkObservation,
                                    _validate_observation, _endpoint)
        def motion(before, after, *, direction, maximum_step):
            maximum_step = min(maximum_step, before.payload[0].row_pitch * MAXIMUM_WHEEL_ROW_FRACTION,
                               after.payload[0].row_pitch * MAXIMUM_WHEEL_ROW_FRACTION)
            return measure_inventory_motion(before.payload[0], after.payload[0],
                                            direction=direction, maximum_step=maximum_step)
        def calibrate(before, after, *, direction, maximum_step):
            return prove_inventory_translation(before.payload[1], after.payload[1],
                                               before.payload[0], after.payload[0], direction, pixel_step=True)
        def bulk_ready(frame, *, direction, maximum_rows, minimum_overlap_rows):
            return inventory_bulk_ready(frame.payload[1], frame.payload[0], direction=direction,
                maximum_rows=maximum_rows, minimum_overlap_rows=minimum_overlap_rows)
        def bulk_motion(before, after, *, direction, maximum_rows, minimum_overlap_rows):
            distance = prove_inventory_bulk_translation(before.payload[1], after.payload[1],
                before.payload[0], after.payload[0], direction,
                maximum_rows=maximum_rows, minimum_overlap_rows=minimum_overlap_rows)
            receipt = getattr(self, "_inventory_bulk_receipt", None)
            pulses = (receipt[2] if receipt is not None and receipt[0] == after.geometry_identity else None)
            logging.info("Inventory bulk proof: direction=%s pixels=%.3f rows=%.3f pulses=%s overlap_min=%s",
                direction, distance, distance / before.row_pitch_pixels, pulses, minimum_overlap_rows)
            return distance
        info_reader = getattr(self.desktop, "window_info", None)
        info = info_reader(self.handle) if callable(info_reader) else None
        identity = ((info.process_id, info.executable)
                    if info is not None and info.process_id else None)
        context = (self.handle, identity, self._layout_binding(), 31)
        previous = getattr(self, "_inventory_scroll_session", None)
        if identity is not None and previous is not None and previous[0] == context:
            scroller = previous[1]
            scroller.begin_cycle()
        else:
            has_bulk = callable(getattr(self.desktop, "scroll_burst", None))
            scroller = InventoryScroller(sample=self._inventory_viewport_frame, wheel=self._scroll_inventory,
                measure_motion=motion, calibrate_motion=calibrate, check_stop=self.check_stop, pause=self.wait,
                bulk_wheel=self._scroll_inventory_bulk if has_bulk else None,
                bulk_ready=bulk_ready if has_bulk else None, bulk_motion=bulk_motion if has_bulk else None,
                wheel_delta=31, max_wheel_inputs=512, max_seconds=900)
            self._inventory_scroll_session = (context, scroller)
        unsorted_transfers = 0
        sorted_since_transfer = (sorted_after_sort is True
                                 and bool(self.profile.get("sort_button")))
        sort_epoch = 1 if sorted_since_transfer else 0
        full_sorted_top = False
        sorted_audit_state = None
        batch_deadline = time.monotonic() + 900
        selected = {int(tier) for tier, rule in self.filters.items()
                    if rule.get("enabled") and int(tier) >= 3}

        def compact_at_top():
            nonlocal unsorted_transfers, sorted_since_transfer, full_sorted_top, sort_epoch
            if not self.profile.get("sort_button"):
                return False
            self.sort_inventory()
            unsorted_transfers = 0
            scroller.invalidate()
            top = _validate_observation(scroller.reset_to_top())
            if not _endpoint(top, "top"):
                raise RuntimeError("После сортировки начало инвентаря не подтверждено.")
            sorted_since_transfer = True
            full_sorted_top = False
            sort_epoch += 1
            return True

        def transfer(**kwargs):
            nonlocal unsorted_transfers, sorted_since_transfer, full_sorted_top
            moved = self.once(**kwargs)
            if not moved and unsorted_transfers and scroller.snapshot().offset_pixels == 0:
                # Compact hidden items into the drained top viewport before
                # continuing the complete walk. A false result still owns a
                # fresh scan of the same proved top position.
                if compact_at_top():
                    moved = self.once(sort=False)
            if moved:
                unsorted_transfers += 1
                sorted_since_transfer = False
                full_sorted_top = False
                scroller.invalidate()
            return moved

        def transfer_batch(*, sort=False, max_transfers):
            nonlocal unsorted_transfers, sorted_since_transfer, full_sorted_top
            if (sort is not False or isinstance(max_transfers, bool)
                    or not isinstance(max_transfers, int) or max_transfers < 1):
                raise ValueError("A visible batch needs a positive remaining transfer budget")
            count = 0
            compacted = False
            while count < max_transfers and time.monotonic() < batch_deadline:
                self.check_stop()
                # Every item retains fresh native guards and independent
                # transfer verification. Reacquire Top once after this batch.
                moved = self.once(sort=False)
                if not isinstance(moved, bool):
                    raise ValueError("A batch item must have a verified boolean outcome")
                if moved:
                    count += 1
                    unsorted_transfers += 1
                    sorted_since_transfer = False
                    full_sorted_top = False
                    continue
                if (count == 0 and unsorted_transfers and not compacted
                        and scroller.snapshot().offset_pixels == 0):
                    compacted = True
                    if compact_at_top():
                        continue
                break
            if count:
                scroller.invalidate()
            return count

        def sorted_top(observation):
            viewport = self.profile.get("inventory_viewport", {}).get("rect")
            if not isinstance(viewport, list) or len(viewport) != 4:
                return False
            scale = self.profile.get("layout_scales", {}).get("inventory", 1.)
            return certify_full_inventory_top(observation, Rect(*viewport),
                self.canonical_inv.rows, self.canonical_inv.columns, scale)

        def audit_sorted_top(observation):
            nonlocal sorted_audit_state
            if (not isinstance(observation, InventoryWalkObservation)
                    or observation.offset_pixels != 0):
                return
            state = (sort_epoch, sorted_since_transfer, sorted_top(observation),
                certify_sorted_prefix(observation.scan, selected, require_boundary=False),
                certify_sorted_prefix(observation.scan, selected), observation.scan.unknown_count,
                observation.scan.grid.rows, observation.scan.grid.rect.as_box(),
                tuple(self.profile.get("inventory_viewport", {}).get("rect", ())),
                tuple("empty" if cell.empty is True else cell.tier
                      for cell in observation.scan.cells))
            if state != sorted_audit_state:
                sorted_audit_state = state
                # Private native diagnostics distinguish a raster crop from
                # unknown tiers/order. They do not add user-facing events.
                logging.info("Inventory sorted-prefix audit: epoch=%s fresh_sort=%s full_top=%s "
                    "known_order=%s boundary=%s unknown=%s rows=%s crop=%s viewport=%s labels=%s", *state)

        def sorted_boundary(observation):
            nonlocal full_sorted_top, sorted_since_transfer
            audit_sorted_top(observation)
            if not sorted_since_transfer:
                return False
            if not certify_sorted_prefix(observation.scan, selected, require_boundary=False):
                sorted_since_transfer = False
                full_sorted_top = False
                return False
            if sorted_top(observation):
                # A partial first row cannot certify the sorted prefix. A
                # bounded eight-row raster inset retains every complete row.
                # A lower tier later can end only the same overlapping pass.
                full_sorted_top = True
            return full_sorted_top and certify_sorted_prefix(observation.scan, selected)

        def fresh_snapshot():
            observation = scroller.snapshot()
            audit_sorted_top(observation)
            candidate_free = getattr(self, "_candidate_free_scan", None)
            if (candidate_free is not None and not getattr(self, "_transfer_input_issued", False)
                    and any(cell.empty is False and cell.tier in selected
                            for cell in observation.scan.cells)):
                # Loot can arrive after the independently observed empty pass.
                # Keep the walk's strict false-transfer invariant; discard this
                # stale exhaustion decision and start a new guarded cycle.
                raise SafePreflightChange("Состав сумки изменился во время распознавания. Перенос отменён.")
            return observation

        return run_inventory_walk(reset_to_top=scroller.reset_to_top,
            inventory_snapshot=fresh_snapshot, transfer=transfer, transfer_batch=transfer_batch,
            sorted_boundary=sorted_boundary, sorted_epoch=lambda: sort_epoch, sorted_top=sorted_top,
            scroll_down=scroller.scroll_down,
            selected_tiers=selected,
            check_stop=self.check_stop, pause=self.wait)

    def selected_page(self, image):
        if self.canonical_profile.get("automatic_layout") is True:
            return self._discover_storage_pages(image).selected_id
        pages = self.profile.get("pages", [])
        if not pages:
            return None
        active = self._image(self.profile["page_active_image"])
        inactive = self._image(self.profile["page_inactive_image"])
        matches = []
        for page in pages:
            rect = Rect(*page["marker"])
            reference = self.canonical_page(page["id"])["marker"]
            on = self.sample_error(image, rect, active, pane="storage", canonical_rect=reference)
            off = self.sample_error(image, rect, inactive, pane="storage", canonical_rect=reference)
            if on <= 10 and off - on >= 10:
                matches.append(page["id"])
        if len(matches) != 1:
            raise RuntimeError("Не удалось однозначно определить выбранную страницу хранилища.")
        return matches[0]

    def canonical_page(self, page_id):
        pages = getattr(self, "_canonical_storage_pages", self.canonical_profile.get("pages", []))
        return next(page for page in pages if page["id"] == page_id)

    def _discover_storage_pages(self, image):
        if self.canonical_profile.get("automatic_layout") is not True:
            return None
        from storage_pages import StoragePageDetector
        detector = getattr(self, "_storage_page_detector", None)
        if detector is None:
            detector = self._storage_page_detector = StoragePageDetector(self.canonical_profile, self.directory)
        proof = detector.discover(image, self.profile)
        self._canonical_storage_pages = proof.canonical_pages
        self.profile["pages"] = [{"id": page.id, "button": list(page.button.as_box()),
                                  "marker": list(page.marker.as_box())} for page in proof.pages]
        return proof

    def _select_storage_page(self, image, page_id, expected_identity=None):
        expected_selected = self.selected_page(image)
        fresh = self.capture()
        self.check_anchors(fresh)
        proof = self._discover_storage_pages(fresh)
        if proof is not None:
            if proof.identity != expected_identity or proof.selected_id != expected_selected:
                raise LayoutChangedDuringOperation("Страницы хранилища изменились. Текущая проверка отменена.")
            page = next((page for page in proof.pages if page.id == page_id), None)
            if page is None:
                raise RuntimeError("Кнопка существующей страницы не подтверждена. Клик отменён.")
            button = page.button
        else:
            if self.selected_page(fresh) != expected_selected:
                raise RuntimeError("Кнопка существующей страницы не подтверждена. Клик отменён.")
            page = next(page for page in self.profile.get("pages", []) if page["id"] == page_id)
            button = Rect(*page["button"])
            if expected_selected != page_id:
                rect = Rect(*page["marker"])
                reference = self.canonical_page(page_id)["marker"]
                off = self.sample_error(fresh, rect, self._image(self.profile["page_inactive_image"]),
                                        pane="storage", canonical_rect=reference)
                on = self.sample_error(fresh, rect, self._image(self.profile["page_active_image"]),
                                       pane="storage", canonical_rect=reference)
                if off > 10 or on - off < 10:
                    raise RuntimeError("Кнопка существующей страницы не подтверждена. Клик отменён.")
        if expected_selected != page_id:
            self.check_stop()
            self._issue_transfer(self.desktop.click, self.handle, button.center, self.size)
            self.wait(.4)
            fresh = self._park_and_capture(1)
            self.check_anchors(fresh)
            if proof is not None:
                updated = self._discover_storage_pages(fresh)
                if updated.identity != expected_identity or updated.selected_id != page_id:
                    raise RuntimeError("Переключение страницы хранилища не подтвердилось.")
            elif self.selected_page(fresh) != page_id:
                raise RuntimeError("Переключение страницы хранилища не подтвердилось.")
            self._transfer_input_issued = False
        return fresh

    def next_storage_page(self, image):
        proof = self._discover_storage_pages(image)
        pages = tuple(page["id"] for page in self.profile.get("pages", []))
        if not pages:
            raise RuntimeError("Нет распознанных свободных ячеек или подтверждённых страниц хранилища. Работа остановлена.")
        identity = proof.identity if proof is not None else None
        def inspect(page_id):
            nonlocal image
            image = self._select_storage_page(image, page_id, identity)
            inv, store = self.scans(image)
            if store.empty_count:
                self.log(f"Выбрана страница хранилища {page_id}.")
            return image, inv, store, self.find_candidate(image, inv, store)
        return find_storage_capacity(pages, inspect_page=inspect, check_stop=self.check_stop)

    def _run_inventory_cycle(self):
        from auto_layout import LayoutError
        from inventory_scroller import InventoryCalibrationPending, InventoryScrollUnconfirmed
        try:
            return self._run_inventory_cycle_impl()
        except LayoutChangedDuringOperation as error:
            # Observation after a completed, confirmed interface action may
            # become stale while Unity finishes scrolling. Reacquire next
            # cycle only when no issued input is still awaiting verification.
            if (self.canonical_profile.get("automatic_layout") is True
                    and not getattr(self, "_transfer_input_issued", False)):
                raise SafeLayoutChange(str(error)) from error
            raise
        except InventoryCalibrationPending as error:
            self.log("Диагностика калибровки инвентаря: " + error.reason)
            result = run_inventory_cycle(transfer=self.once,
                inventory_snapshot=lambda: self._last_inventory_scan,
                selected_tiers={int(tier) for tier, rule in self.filters.items()
                                if rule.get("enabled") and int(tier) >= 3},
                check_stop=self.check_stop, pause=self.wait,
                sort_available=bool(self.profile.get("sort_button")))
            from inventory_walk import InventoryWalkResult
            self.log(INVENTORY_PENDING)
            # This is useful verified progress without an invented endpoint.
            return InventoryWalkResult("pending", result.transfers, 0, 0)
        except (LayoutError, InventoryScrollUnconfirmed) as error:
            self.log("Диагностика обхода инвентаря: " + str(error))
            raise RuntimeError("Проверка инвентаря не завершена. Работа остановлена.") from error

    def _run_inventory_cycle_impl(self):
        if (self.canonical_profile.get("automatic_layout") is True
                and self.canonical_inv.rows == 8
                and getattr(self.panel_controller, "layout", None) is not None
                and callable(getattr(self.desktop, "scroll", None))):
            sorted_after_sort = False
            def prepare():
                nonlocal sorted_after_sort
                sorted_after_sort = False
                self.prepare_panels()
                if self.profile.get("sort_button"):
                    self.sort_inventory()
                    sorted_after_sort = True
            self._guarded_operation(prepare)
            # A game's Sort may preserve its scroll position. Establish the
            # top before the first transfer and prove the whole inventory.
            walked = self._walk_inventory(sorted_after_sort=sorted_after_sort)
            if not walked.complete:
                self.log("Диагностика обхода инвентаря: " + walked.outcome)
                return self._inventory_pending(walked)
            return walked
        result = run_inventory_cycle(transfer=self.once,
            inventory_snapshot=lambda: self._last_inventory_scan,
            selected_tiers={int(tier) for tier, rule in self.filters.items()
                            if rule.get("enabled") and int(tier) >= 3},
            check_stop=self.check_stop, pause=self.wait,
            sort_available=bool(self.profile.get("sort_button")))
        if not result.complete:
            self.log("Диагностика завершения инвентаря: " + result.outcome)
            if (result.outcome in ("unknown", "hidden_unconfirmed")
                    and self.profile.get("inventory_viewport") is not None
                    and callable(getattr(self.desktop, "scroll", None))):
                self.state("working", "Распознаю предметы и проверяю хранилище.")
                walked = self._walk_inventory()
                if walked.complete:
                    from dataclasses import replace
                    return replace(walked, transfers=result.transfers + walked.transfers)
                self.log("Диагностика обхода инвентаря: " + walked.outcome)
                return self._inventory_pending(walked, prior_transfers=result.transfers)
            return self._inventory_pending(result)
        return result

    def _inventory_pending(self, result, *, prior_transfers=0):
        from inventory_walk import InventoryWalkResult
        from inventory_cycle import InventoryCycleResult
        allowed = ({"unknown", "bounded", "top_unconfirmed", "endpoint_unconfirmed"}
                   if isinstance(result, InventoryWalkResult) else
                   {"unknown", "hidden_unconfirmed", "bounded", "sort_unavailable"}
                   if isinstance(result, InventoryCycleResult) else set())
        if result.outcome not in allowed:
            raise RuntimeError("Проверка инвентаря не завершена. Работа остановлена.")
        self.log(INVENTORY_PENDING)
        return InventoryWalkResult("pending", prior_transfers + result.transfers,
                                   getattr(result, "scrolls", 0), getattr(result, "resets", 0))

    def inspect_storage_pages(self):
        """Audit configured existing pages without sorting, transfer or trust writes."""
        pages = self.profile.get("pages", [])
        if not isinstance(pages, list) or not pages:
            raise ValueError("Сначала настрой существующие страницы хранилища.")
        ids = [page.get("id") for page in pages]
        if (any(isinstance(id_, bool) or not isinstance(id_, int) or id_ < 1 for id_ in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("Страницы хранилища должны иметь разные положительные номера.")
        self.prepare_panels()
        pages = self.profile["pages"]
        for page in pages:
            button, marker = Rect(*page["button"]), Rect(*page["marker"])
            if (not button.fits(*self.size) or not marker.fits(*self.size)
                    or not (button.left <= marker.left < marker.right <= button.right
                            and button.top <= marker.top < marker.bottom <= button.bottom)):
                raise ValueError("Маркер существующей страницы должен быть внутри её кнопки.")
        active = self._image(self.profile["page_active_image"])
        inactive = self._image(self.profile["page_inactive_image"])
        image = self.capture()
        self.check_anchors(image)
        original = self.selected_page(image)
        original_page = next(page for page in pages if page["id"] == original)
        touched = False

        def select(page):
            nonlocal touched
            fresh = self.capture()
            self.check_anchors(fresh)
            current = self.selected_page(fresh)
            if current == page["id"]:
                return fresh
            rect = Rect(*page["marker"])
            reference = self.canonical_page(page["id"])["marker"]
            off = self.sample_error(fresh, rect, inactive, pane="storage", canonical_rect=reference)
            on = self.sample_error(fresh, rect, active, pane="storage", canonical_rect=reference)
            if off > 10 or on - off < 10:
                raise RuntimeError("Кнопка существующей страницы не подтверждена. Клик отменён.")
            self.check_stop()
            touched = True
            self.desktop.click(self.handle, Rect(*page["button"]).center, self.size)
            self.wait(0.4)
            fresh = self._park_and_capture(1)
            self.check_anchors(fresh)
            if self.selected_page(fresh) != page["id"]:
                raise RuntimeError("Переключение страницы хранилища не подтвердилось.")
            return fresh

        audit_error = None
        try:
            result = []
            for page in pages:
                self.state("working", f"Проверяю страницу хранилища {page['id']}.")
                image = select(page)
                scan = self.scan_pane(image, "storage")
                result.append({"id": page["id"], "free": scan.empty_count,
                               "unknown": sum(cell.empty is None for cell in scan.cells)})
            return result
        except BaseException as error:
            audit_error = error
            raise
        finally:
            # F8 or a user focus change always wins over restoration. Never activate
            # the game or attempt a repeated click after a failed page switch.
            if (touched and not self.stop.is_set() and not self.desktop.stop_pressed()
                    and self.desktop.active(self.handle)):
                try:
                    select(original_page)
                except Exception as error:
                    if audit_error is None:
                        raise
                    self.log("Исходная страница не восстановлена: " + str(error))

    def monitor(self, interval):
        interval = validate_interval(interval)
        paused = False
        safe_failure_streak = 0
        access_hold = False
        access_needs_return = False
        while True:
            self.check_stop()
            token = None
            if access_hold:
                active_now = self.desktop.active(self.handle)
                if not active_now:
                    access_needs_return = False
                elif access_needs_return:
                    idle_provider = getattr(self.desktop, "idle_seconds", None)
                    if idle_provider is None or idle_provider() < 600:
                        # Leave the player's active game alone until its next
                        # foreground return or the existing idle eligibility.
                        self.wait(.3)
                        continue
                if active_now:
                    access_hold = False
            if not self.desktop.active(self.handle):
                # A live background game never triggers the window resolver.
                is_alive = getattr(type(self.desktop), "is_alive", None)
                resolver = getattr(self, "handle_resolver", None)
                alive = is_alive is None or is_alive(self.desktop, self.handle)
                if not alive:
                    if not paused:
                        self.log("Пауза: окно игры закрыто; ожидаю перезапуск.")
                        self.state("paused", "Игра закрыта. Ожидаю её перезапуск.")
                        paused = True
                    if resolver is not None:
                        self.check_stop()
                        replacement = resolver()
                        if replacement is not None:
                            if isinstance(replacement, bool) or not isinstance(replacement, int) or replacement <= 0:
                                raise RuntimeError("Новое окно игры не определено однозначно.")
                            if is_alive(self.desktop, replacement):
                                self.handle = replacement
                                safe_failure_streak = 0
                                self.state("paused", "Игра снова найдена. Ожидаю следующую проверку.")
                    self.wait(0.3)
                    continue
                idle = getattr(self.desktop, "idle_seconds", None)
                if idle is not None and idle() >= 600:
                    if not callable(getattr(self.desktop, "begin_background_cycle", None)) or not callable(getattr(self.desktop, "end_background_cycle", None)):
                        raise ValueError("Автоматическое открытие игры недоступно в этом интерфейсе Windows.")
                    self.check_stop()
                    try:
                        controller = getattr(self, "panel_controller", None)
                        if controller is None:
                            token = self.desktop.begin_background_cycle(self.handle)
                        else:
                            # Only the separate native idle-activation path may
                            # use this pure header evidence. Ordinary transfer
                            # capture/click still require actual foreground.
                            token = self.desktop.begin_background_cycle(self.handle,
                                activation_target=controller.activation_target,
                                activation_guard=self.check_stop)
                    except (FocusUnavailable, WindowGeometryChanged) as error:
                        # Failed begin owns its token cleanup. Retry only when
                        # native guards explicitly rule out accepted button-down;
                        # no generic transfer failure may enter this retry path.
                        if error.input_issued is not False:
                            raise
                        logging.info(getattr(error, "diagnostic_message", str(error)))
                        if not paused:
                            self.log(FOCUS_WAITING)
                        paused = True
                        if isinstance(error, (CursorPositionChanged, ForegroundWindowChanged)):
                            safe_failure_streak += 1
                            access_hold = True
                            access_needs_return = (isinstance(error, CursorPositionChanged)
                                                   and self.desktop.active(self.handle))
                            exhausted = safe_failure_streak >= SAFE_QUICK_RETRY_ATTEMPTS
                            self.state("waiting" if exhausted else "paused", FOCUS_WAITING,
                                       time.monotonic() + interval if exhausted else None)
                            self.wait(interval if exhausted else .3)
                            continue
                        self.state("paused", FOCUS_WAITING,
                                   time.monotonic() + FOCUS_RETRY_SECONDS)
                        self.wait(FOCUS_RETRY_SECONDS)
                        continue
                    except UserActivityInterrupted as error:
                        # begin_background_cycle cleans its own partial activation;
                        # an accepted unproved activation cannot be replayed.
                        if getattr(error, "input_issued", False) is True:
                            raise
                        self.log(str(error))
                        self.state("waiting", "Фоновая проверка прервана вводом пользователя. Следующая проверка по таймеру.",
                                   time.monotonic() + interval)
                        self.wait(interval)
                        continue
                    if token is None:
                        if not paused:
                            self.log("Пауза: ожидаю возвращение в игру или 10 минут без ввода.")
                            self.state("paused", "Вернитесь в игру; после 10 минут без ввода Helper сможет открыть её сам.")
                            paused = True
                        self.wait(0.3)
                        continue
                else:
                    if not paused:
                        self.log("Пауза: ожидаю возвращение в игру или 10 минут без ввода.")
                        self.state("paused", "Вернитесь в игру; после 10 минут без ввода Helper сможет открыть её сам.")
                        paused = True
                    self.wait(0.3)
                    continue
            cycle_error = None
            layout_pending = False
            inventory_pending = False
            preflight_pending = False
            activity_pending = False
            access_pending = False
            try:
                self.check_stop()
                access_hold = False  # Foreground/idle activation passed the normal gate.
                if token is not None:
                    is_alive = getattr(type(self.desktop), "is_alive", None)
                    if not self.desktop.active(self.handle) or (is_alive is not None and not is_alive(self.desktop, self.handle)):
                        raise RuntimeError("Окно игры не стало активным. Фоновая проверка отменена.")
                    # A restored/minimised Unity window may be foreground before
                    # its panels are rendered. Stop/focus checks still apply.
                    self.wait(1.0)
                    self.check_stop()
                    if not self.desktop.active(self.handle) or (is_alive is not None and not is_alive(self.desktop, self.handle)):
                        raise RuntimeError("Окно игры потеряло фокус во время отрисовки. Фоновая проверка отменена.")
                # Rescan after each verified move; compaction never reuses old positions.
                result = self._run_inventory_cycle()
                inventory_pending = getattr(result, "outcome", None) == "pending"
                safe_failure_streak = 0
                if paused:
                    self.log("Окно игры активно: продолжаю.")
                    paused = False
            except (CursorPositionChanged, ForegroundWindowChanged) as error:
                cycle_error = error
                if (error.input_issued or getattr(self, "_transfer_input_issued", False)
                        or self._panel_open_input_pending()):
                    raise
                safe_failure_streak += 1
                access_pending = True
                access_hold = True
                access_needs_return = (isinstance(error, CursorPositionChanged)
                                       and self.desktop.active(self.handle))
                if not paused:
                    self.log(FOCUS_WAITING)
                paused = True
            except SafeLayoutChange as error:
                cycle_error = error
                layout_pending = True
                safe_failure_streak += 1
                if not paused:
                    self.log(LAYOUT_WAITING)
                paused = True
            except SafePreflightChange as error:
                cycle_error = error
                preflight_pending = True
                safe_failure_streak += 1
                self.log(PREFLIGHT_WAITING)
            except UserActivityInterrupted as error:
                cycle_error = error
                if (getattr(error, "input_issued", False) is True
                        or getattr(self, "_transfer_input_issued", False)
                        or self._panel_open_input_pending()):
                    raise
                activity_pending = True
                self.log(str(error))
                self.state("paused", "Фоновая проверка прервана вводом пользователя. Следующая проверка по таймеру.")
            except BaseException as error:
                cycle_error = error
                raise
            finally:
                if token is not None:
                    try:
                        restored = self.desktop.end_background_cycle(self.handle, token)
                        if restored is False:
                            self.log("Windows не разрешила активировать прежнее окно после проверки.")
                            detail = getattr(self.desktop, "last_background_error", "")
                            if isinstance(detail, str) and detail:
                                self.log(detail)
                    except Exception as error:
                        if cycle_error is None:
                            raise
                        self.log("Не удалось восстановить прежнее окно: " + str(error))
            if access_pending:
                # Already-idle cursor mismatches share the bounded failure
                # budget; a persistent mismatch never creates another short
                # countdown loop. Exhaustion survives the interval cooldown.
                exhausted = safe_failure_streak >= SAFE_QUICK_RETRY_ATTEMPTS
                delay = interval if exhausted else .3
                self.state("waiting" if exhausted else "paused", FOCUS_WAITING,
                           time.monotonic() + interval if exhausted else None)
                self.wait(delay)
                continue
            quick_retry = ((layout_pending or preflight_pending)
                           and safe_failure_streak < SAFE_QUICK_RETRY_ATTEMPTS)
            delay = LAYOUT_RETRY_SECONDS if quick_retry else interval
            self.state("waiting", LAYOUT_WAITING if layout_pending else
                       PREFLIGHT_WAITING if preflight_pending else
                       INVENTORY_PENDING if inventory_pending else
                       "Фоновая проверка прервана вводом пользователя. Следующая проверка по таймеру."
                       if activity_pending else
                       "Проверка закончена. Ожидаю следующий цикл.",
                       time.monotonic() + delay)
            self.wait(delay)
