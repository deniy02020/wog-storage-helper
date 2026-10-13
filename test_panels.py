"""Synthetic images and an injected desktop; no native capture or input."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np
from PIL import Image

from core import Rect
from panels import PanelController, locate_unique
from test_runtime import cell


class PanelDesktop:
    def __init__(self, frames):
        self.frames = list(frames)
        self.inputs = []

    def capture(self, handle):
        return self.frames.pop(0).copy()

    def click(self, handle, point, size):
        self.inputs.append(("click", handle, point, size))

    def move(self, handle, point, size):
        self.inputs.append(("move", handle, point, size))

    def stop_pressed(self):
        return False


class PanelFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.samples = {}
        random = np.random.default_rng(316)
        for name, size in (("inventory_header", (16, 8)), ("storage_header", (16, 8)),
                           ("storage_open", (14, 10)), ("menu", (12, 10)), ("character", (14, 10))):
            sample = Image.fromarray(random.integers(15, 240, (size[1], size[0], 3), dtype=np.uint8))
            sample.save(self.directory / (name + ".png"))
            self.samples[name] = sample
        cell().save(self.directory / "empty.png")
        for tier in range(1, 7):
            cell(tier).crop((3, 3, 11, 11)).save(self.directory / f"T{tier}.png")
        self.profile = {
            "size": [200, 140], "mode": "right", "supervised_ok": True,
            "include_gems": True, "interval": 30,
            "inventory": {"rect": [20, 30, 68, 54], "rows": 1, "columns": 2},
            "storage": {"rect": [110, 30, 158, 54], "rows": 1, "columns": 2},
            "badge": [3, 3, 11, 11],
            "tiers": {str(t): [f"T{t}.png"] for t in range(1, 7)},
            "inventory_empty": ["empty.png"], "storage_empty": ["empty.png"],
            "anchors": [{"rect": [10, 10, 26, 18], "image": "inventory_header.png"},
                        {"rect": [100, 10, 116, 18], "image": "storage_header.png"}],
            "panel_controls": {"inventory_open": [{"image": "menu.png"}, {"image": "character.png"}],
                               "storage_open": {"image": "storage_open.png"}},
        }

    def tearDown(self):
        self.temp.cleanup()

    def image(self, *, inventory=True, storage=True, inv_offset=(0, 0), store_offset=(0, 0),
              menu=False, size=(200, 140), inv_items=None, store_items=None):
        image = Image.new("RGB", size, (28, 32, 36))
        for present, name, point, offset, items in (
            (inventory, "inventory", (10, 10), inv_offset, inv_items),
            (storage, "storage", (100, 10), store_offset, store_items)):
            if present:
                image.paste(self.samples[name + "_header"], (point[0] + offset[0], point[1] + offset[1]))
                rect = Rect(*self.profile[name]["rect"]).translated(*offset)
                for index, tile in enumerate(items or [cell(), cell()]):
                    image.paste(tile, (rect.left + 24 * index, rect.top))
        if inventory:
            image.paste(self.samples["storage_open"], (70 + inv_offset[0], 90 + inv_offset[1]))
        image.paste(self.samples["menu"], (170, 115))
        if menu:
            image.paste(self.samples["character"], (80, 70))
        return image

    def controller(self, frames, stop=lambda: None):
        desktop = PanelDesktop(frames)
        controller = PanelController(desktop, 7, self.profile, self.directory, stop, lambda _: None)
        return controller, desktop


class PanelControllerTests(PanelFixture, unittest.TestCase):
    def test_automatic_prepare_clears_grid_tooltip_with_both_headers_visible(self):
        from auto_layout import LayoutError
        clear = self.image()
        occluded = clear.copy()
        occluded.paste((0, 0, 0), self.profile["inventory"]["rect"])
        controller, desktop = self.controller([occluded, clear])

        def locate(image):
            return [SimpleNamespace(rect=locate_unique(image, sample))
                    for sample in controller.headers]

        def resolve(image):
            if image.crop(self.profile["inventory"]["rect"]).tobytes() != clear.crop(
                    self.profile["inventory"]["rect"]).tobytes():
                raise LayoutError("Grid is occluded")
            return copy.deepcopy(self.profile)

        controller.layout = SimpleNamespace(locate_headers=locate, resolve=Mock(side_effect=resolve))
        original = copy.deepcopy(self.profile)
        image, adopted = controller.prepare()
        self.assertEqual(image.tobytes(), clear.tobytes())
        self.assertEqual(adopted, original)
        self.assertEqual(desktop.inputs, [("move", 7, (18, 14), clear.size)])
        controller.layout.resolve.assert_called_once()
        self.assertEqual(self.profile, original)

    def test_automatic_prepare_never_parks_without_a_unique_visible_header(self):
        from auto_layout import LayoutError
        controller, desktop = self.controller([self.image()])
        controller.layout = SimpleNamespace(locate_headers=Mock(side_effect=LayoutError("Ambiguous header")),
                                            resolve=Mock())
        with self.assertRaisesRegex(LayoutError, "Ambiguous"):
            controller.prepare()
        self.assertEqual(desktop.inputs, [])
        controller.layout.resolve.assert_not_called()

    def test_automatic_grid_occlusion_persists_after_park_and_never_clicks_an_item(self):
        from auto_layout import LayoutError
        image = self.image()
        controller, desktop = self.controller([image, image])
        controller.layout = SimpleNamespace(
            locate_headers=lambda frame: [SimpleNamespace(rect=locate_unique(frame, sample))
                                          for sample in controller.headers],
            resolve=Mock(side_effect=LayoutError("Grid remains occluded")))
        with self.assertRaisesRegex(LayoutError, "remains occluded"):
            controller.prepare()
        self.assertEqual(desktop.inputs, [("move", 7, (18, 14), image.size)])

    def test_open_panels_translate_independently_without_input_or_file_changes(self):
        self.profile["sort_button"] = {"rect": [70, 60, 84, 70], "image": "storage_open.png"}
        self.profile["pages"] = [{"id": 1, "button": [110, 65, 120, 75], "marker": [111, 66, 115, 70]}]
        original = copy.deepcopy(self.profile)
        image = self.image(inv_offset=(4, 3), store_offset=(6, 5))
        controller, desktop = self.controller([image])
        _, translated = controller.prepare()
        self.assertEqual(translated["inventory"]["rect"], [24, 33, 72, 57])
        self.assertEqual(translated["storage"]["rect"], [116, 35, 164, 59])
        self.assertEqual(translated["sort_button"]["rect"], [74, 63, 88, 73])
        self.assertEqual(translated["pages"][0]["button"], [116, 70, 126, 80])
        self.assertEqual(translated["size"], [200, 140])
        for key in ("tiers", "include_gems", "supervised_ok", "interval", "badge"):
            self.assertEqual(translated[key], original[key])
        self.assertEqual(self.profile, original)
        self.assertEqual(desktop.inputs, [])

    def test_resized_client_with_unchanged_headers_rejected_before_opening(self):
        original = copy.deepcopy(self.profile)
        for panels_open in (True, False):
            with self.subTest(panels_open=panels_open):
                image = self.image(inventory=panels_open, storage=panels_open,
                                   size=(210, 150))
                controller, desktop = self.controller([image])
                with self.assertRaisesRegex(RuntimeError, "Текущая проверка отменена"):
                    controller.prepare()
                self.assertEqual(desktop.inputs, [])
                self.assertEqual(controller.profile, original)
                self.assertEqual(self.profile, original)

    def test_supplied_resized_frame_cannot_bypass_native_size_guard(self):
        controller, desktop = self.controller([])
        with self.assertRaisesRegex(RuntimeError, "Текущая проверка отменена"):
            controller.prepare(self.image(size=(210, 150)))
        self.assertEqual(desktop.inputs, [])

    def test_resize_between_button_confirmation_frames_cancels_before_click(self):
        closed = self.image(inventory=False, storage=False)
        resized = self.image(inventory=False, storage=False, size=(210, 150))
        controller, desktop = self.controller([closed, resized])
        with self.assertRaisesRegex(RuntimeError, "Текущая проверка отменена"):
            controller.prepare()
        self.assertEqual(desktop.inputs, [])

    def test_closed_storage_opens_once_and_requires_both_headers(self):
        closed, opened = self.image(storage=False), self.image()
        controller, desktop = self.controller([closed, closed, closed, opened])
        controller.prepare()
        clicks = [action for action in desktop.inputs if action[0] == "click"]
        self.assertEqual(clicks, [("click", 7, (77, 95), (200, 140))])

    def test_inventory_menu_then_character_sequence_each_once(self):
        closed = self.image(inventory=False, storage=False)
        menu = self.image(inventory=False, storage=False, menu=True)
        inv = self.image(storage=False)
        controller, desktop = self.controller([closed, closed, menu, menu, inv, inv, self.image()])
        controller.prepare()
        clicks = [action[2] for action in desktop.inputs if action[0] == "click"]
        self.assertEqual(clicks, [(176, 120), (87, 75), (77, 95)])

    def test_menu_only_stops_without_repeating_toggle_or_transfer(self):
        self.profile["panel_controls"]["inventory_open"] = [{"image": "menu.png"}]
        closed = self.image(inventory=False, storage=False)
        menu = self.image(inventory=False, storage=False, menu=True)
        controller, desktop = self.controller([closed, closed, menu])
        with self.assertRaisesRegex(RuntimeError, "не подтвердилось"):
            controller.prepare()
        self.assertEqual(desktop.inputs, [("click", 7, (176, 120), (200, 140))])

    def test_already_open_menu_skips_toggle_and_opens_character(self):
        menu = self.image(inventory=False, storage=False, menu=True)
        inv = self.image(storage=False)
        controller, desktop = self.controller([menu, menu, inv, inv, self.image()])
        controller.prepare()
        self.assertEqual([action[2] for action in desktop.inputs if action[0] == "click"],
                         [(87, 75), (77, 95)])

    def test_missing_inventory_control_cannot_click(self):
        self.profile["panel_controls"].pop("inventory_open")
        controller, desktop = self.controller([self.image(inventory=False, storage=False)])
        with self.assertRaisesRegex(RuntimeError, "подтверждённых"):
            controller.prepare()
        self.assertEqual(desktop.inputs, [])

    def test_failed_storage_open_does_not_retry(self):
        closed = self.image(storage=False)
        controller, desktop = self.controller([closed] * 4)
        with self.assertRaisesRegex(RuntimeError, "не подтвердилось"):
            controller.prepare()
        self.assertEqual(sum(action[0] == "click" for action in desktop.inputs), 1)

    def test_tooltip_header_restores_before_any_toggle(self):
        closed = self.image(storage=False)
        controller, desktop = self.controller([closed, self.image()])
        controller.prepare()
        self.assertTrue(desktop.inputs)
        self.assertFalse(any(action[0] == "click" for action in desktop.inputs))

    def test_duplicate_header_is_ambiguous_and_never_clicks(self):
        image = self.image()
        image.paste(self.samples["inventory_header"], (20, 70))
        controller, desktop = self.controller([image])
        with self.assertRaisesRegex(RuntimeError, "неоднозначно"):
            controller.prepare()
        self.assertEqual(desktop.inputs, [])

    def test_duplicate_button_is_ambiguous_and_never_clicks(self):
        closed = self.image(storage=False)
        closed.paste(self.samples["storage_open"], (140, 80))
        controller, desktop = self.controller([closed, closed])
        with self.assertRaisesRegex(RuntimeError, "неоднозначно"):
            controller.prepare()
        self.assertFalse(any(action[0] == "click" for action in desktop.inputs))

    def test_moved_button_between_frames_cancels_before_click(self):
        closed = self.image(storage=False)
        moved = self.image(storage=False, inv_offset=(2, 0))
        controller, desktop = self.controller([closed, closed, moved])
        with self.assertRaisesRegex(RuntimeError, "изменилась"):
            controller.prepare()
        self.assertFalse(any(action[0] == "click" for action in desktop.inputs))

    def test_clipped_or_overlapping_grid_stops_before_input(self):
        for offset in ((75, 0), (-70, 0)):
            with self.subTest(offset=offset):
                controller, desktop = self.controller([self.image(store_offset=offset)])
                with self.assertRaises(RuntimeError):
                    controller.prepare()
                self.assertEqual(desktop.inputs, [])

    def test_stop_prevents_even_initial_capture(self):
        def stopped():
            raise InterruptedError("F8")
        controller, desktop = self.controller([], stopped)
        with self.assertRaises(InterruptedError):
            controller.prepare()
        self.assertEqual(desktop.inputs, [])

    def test_untrusted_asset_path_rejected_without_input(self):
        self.profile["panel_controls"]["storage_open"] = {"image": "../escape.png"}
        desktop = PanelDesktop([])
        with self.assertRaisesRegex(ValueError, "внутри"):
            PanelController(desktop, 7, self.profile, self.directory, lambda: None, lambda _: None)
        self.assertEqual(desktop.inputs, [])

    def test_control_threshold_allows_proven_phase_on_initial_and_fresh_frames(self):
        self.profile["panel_controls"]["inventory_open"] = [{"image": "menu.png", "max_rmse": 20}]
        closed = self.image(inventory=False, storage=False)
        shifted = Image.fromarray(np.asarray(self.samples["menu"], dtype=np.uint8) + 15)
        closed.paste(shifted, (170, 115))
        controller, desktop = self.controller([closed, closed, self.image()])
        controller.prepare()
        self.assertEqual(desktop.inputs, [("click", 7, (176, 120), (200, 140))])

    def test_already_open_character_step_uses_its_own_threshold(self):
        self.profile["panel_controls"]["inventory_open"][1]["max_rmse"] = 20
        menu = self.image(inventory=False, storage=False, menu=True)
        shifted = Image.fromarray(np.asarray(self.samples["character"], dtype=np.uint8) + 15)
        menu.paste(shifted, (80, 70))
        inv = self.image(storage=False)
        controller, desktop = self.controller([menu, menu, inv, inv, self.image()])
        controller.prepare()
        self.assertEqual([action[2] for action in desktop.inputs if action[0] == "click"],
                         [(87, 75), (77, 95)])

    def test_control_threshold_default_remains_ten(self):
        self.profile["panel_controls"]["inventory_open"] = [{"image": "menu.png"}]
        closed = self.image(inventory=False, storage=False)
        shifted = Image.fromarray(np.asarray(self.samples["menu"], dtype=np.uint8) + 15)
        closed.paste(shifted, (170, 115))
        controller, desktop = self.controller([closed])
        with self.assertRaisesRegex(RuntimeError, "не найдена"):
            controller.prepare()
        self.assertEqual(desktop.inputs, [])

    def test_invalid_control_threshold_rejected_before_input(self):
        for value in (True, False, None, "20", float("nan"), float("inf"), -1, 20.01, [], {}):
            with self.subTest(value=value):
                self.profile["panel_controls"]["storage_open"] = {"image": "storage_open.png", "max_rmse": value}
                desktop = PanelDesktop([])
                with self.assertRaisesRegex(ValueError, "числом от 0 до 20"):
                    PanelController(desktop, 7, self.profile, self.directory, lambda: None, lambda _: None)
                self.assertEqual(desktop.inputs, [])

    def test_threshold_twenty_does_not_relax_header_or_duplicate_guard(self):
        self.profile["panel_controls"]["storage_open"] = {"image": "storage_open.png", "max_rmse": 20}
        image = self.image()
        shifted_header = Image.fromarray(np.asarray(self.samples["inventory_header"], dtype=np.uint8) + 15)
        image.paste(shifted_header, (10, 10))
        controller, desktop = self.controller([])
        self.assertIsNone(controller.locate(image)[0])
        self.assertEqual(desktop.inputs, [])
        closed = self.image(storage=False)
        shifted_button = Image.fromarray(np.asarray(self.samples["storage_open"], dtype=np.uint8) + 15)
        closed.paste(shifted_button, (70, 90))
        closed.paste(shifted_button, (140, 80))
        controller, desktop = self.controller([closed, closed])
        with self.assertRaisesRegex(RuntimeError, "неоднозначно"):
            controller.prepare()
        self.assertFalse(any(action[0] == "click" for action in desktop.inputs))


class NativeMenuThresholdTests(unittest.TestCase):
    def test_saved_closed_menu_phase_is_unique_at_twenty_and_rejected_at_ten(self):
        directory = Path(__file__).parent / "profile"
        source = directory / "closed-panels-native-20261011.png"
        sample_path = directory / "panel_menu_open.png"
        if not source.exists() or not sample_path.exists():
            self.skipTest("Local native menu calibration snapshots are unavailable")
        with Image.open(source) as native, Image.open(sample_path) as sample:
            self.assertIsNone(locate_unique(native, sample, max_rmse=10))
            self.assertEqual(locate_unique(native, sample, max_rmse=20),
                             Rect(1165, 1591, 1195, 1625))


if __name__ == "__main__":
    unittest.main()
