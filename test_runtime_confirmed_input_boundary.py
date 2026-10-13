"""A confirmed interface action retires only its pending input boundary.

Fake desktops never send OS input. Existing vision tests prove controls and
viewports; these regressions exercise the real sort/page/wheel coordinators
and preserve their cancellation boundary after accepted but unproved input.
"""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from auto_layout import LayoutError
from core import Rect
from inventory_scroller import ViewportFrame
from runtime import LayoutChangedDuringOperation, SafeLayoutChange
from test_runtime import RuntimeTestFixture, cell, frame


class ConfirmedInputBoundaryTests(RuntimeTestFixture, unittest.TestCase):
    def setup_sort(self):
        image = frame([cell(), cell()], [cell(), cell()])
        rect = Rect(24, 0, 36, 8)
        image.crop(rect.as_box()).save(self.directory / "sort.png")
        self.profile.update(automatic_layout=True,
                            sort_button={"rect": list(rect.as_box()), "image": "sort.png"})
        runner, desktop = self.runner([image, image.copy()])
        return runner, desktop, image

    def setup_page(self):
        image = frame([cell(), cell()], [cell(), cell()])
        self.profile["automatic_layout"] = True
        runner, desktop = self.runner([image, image.copy()])
        page = SimpleNamespace(id=2, button=Rect(36, 0, 44, 8))
        first = SimpleNamespace(identity="proved-page-list", selected_id=1, pages=(page,))
        second = SimpleNamespace(identity=first.identity, selected_id=2, pages=(page,))
        # Detector evidence is explicit, not inferred from synthetic cell colors.
        runner.selected_page = Mock(return_value=1)
        runner._discover_storage_pages = Mock(side_effect=[first, second])
        return runner, desktop, image, first, second

    def setup_wheel(self):
        image = frame([cell(), cell()], [cell(), cell()])
        self.profile["automatic_layout"] = True
        runner, desktop = self.runner([])
        viewport = SimpleNamespace(viewport=runner.inv.rect)
        scan = runner.scan_pane(image, "inventory")
        first = ViewportFrame(scan, "same-proved-viewport", 0., 24., (viewport, image))
        second = ViewportFrame(scan, first.geometry_identity, 12., 24., (viewport, image.copy()))
        runner._inventory_viewport_frame = Mock(side_effect=[first, second])
        runner._park_inventory_readback = Mock(return_value=image)
        desktop.scroll = Mock(side_effect=lambda *args: desktop.inputs.append(("scroll", args)))
        return runner, desktop, image, first, second

    @staticmethod
    def actions(desktop):
        return [action for action, _ in desktop.inputs if action != "move"]

    def assert_terminal(self, runner, operation):
        with self.assertRaises(LayoutChangedDuringOperation) as caught:
            runner._guarded_operation(operation)
        self.assertNotIsInstance(caught.exception, SafeLayoutChange)
        self.assertTrue(runner._transfer_input_issued)

    def test_confirmed_sort_allows_later_pre_input_layout_reacquisition(self):
        runner, desktop, _ = self.setup_sort()
        canonical = copy.deepcopy(runner.canonical_profile)
        def operation():
            runner.sort_inventory()
            raise LayoutError("Inventory scroll phase changed after adoption")
        with self.assertRaises(SafeLayoutChange):
            runner._guarded_operation(operation)
        self.assertFalse(runner._transfer_input_issued)
        self.assertEqual(self.actions(desktop), ["click"])
        self.assertEqual(runner.canonical_profile, canonical)

    def test_sort_with_unconfirmed_layout_readback_remains_terminal_after_one_click(self):
        runner, desktop, _ = self.setup_sort()
        runner._park_and_capture = Mock(side_effect=LayoutChangedDuringOperation("Gutter not proved"))
        self.assert_terminal(runner, runner.sort_inventory)
        self.assertEqual(self.actions(desktop), ["click"])
        runner._park_and_capture.assert_called_once_with()

    def test_sort_with_unconfirmed_viewport_readback_cannot_retire_input(self):
        runner, desktop, _ = self.setup_sort()
        runner.profile["inventory_viewport"] = {"rect": list(runner.inv.rect.as_box())}
        runner._park_inventory_readback = Mock(side_effect=LayoutChangedDuringOperation("Rows unsettled"))
        self.assert_terminal(runner, runner.sort_inventory)
        self.assertEqual(self.actions(desktop), ["click"])
        runner._park_inventory_readback.assert_called_once_with()

    def test_confirmed_existing_page_allows_later_pre_input_layout_reacquisition(self):
        runner, desktop, image, first, _ = self.setup_page()
        def operation():
            runner._select_storage_page(image, 2, first.identity)
            raise LayoutError("Fresh inventory observation lost its phase")
        with self.assertRaises(SafeLayoutChange):
            runner._guarded_operation(operation)
        self.assertFalse(runner._transfer_input_issued)
        self.assertEqual(self.actions(desktop), ["click"])
        self.assertEqual(runner._discover_storage_pages.call_count, 2)

    def test_page_with_unconfirmed_readback_remains_terminal_after_one_click(self):
        runner, desktop, image, first, _ = self.setup_page()
        runner._park_and_capture = Mock(side_effect=LayoutChangedDuringOperation("Storage occluded"))
        self.assert_terminal(runner, lambda: runner._select_storage_page(image, 2, first.identity))
        self.assertEqual(self.actions(desktop), ["click"])
        self.assertEqual(runner._discover_storage_pages.call_count, 1)

    def test_page_with_wrong_selected_marker_stops_without_retiring_or_repeating_input(self):
        runner, desktop, image, first, second = self.setup_page()
        second.selected_id = 1
        with self.assertRaisesRegex(RuntimeError, "Переключение страницы"):
            runner._guarded_operation(lambda: runner._select_storage_page(image, 2, first.identity))
        self.assertTrue(runner._transfer_input_issued)
        self.assertEqual(self.actions(desktop), ["click"])

    def test_page_with_changed_page_list_stops_without_retiring_or_repeating_input(self):
        runner, desktop, image, first, second = self.setup_page()
        second.identity = "different-page-list"
        with self.assertRaisesRegex(RuntimeError, "Переключение страницы"):
            runner._guarded_operation(lambda: runner._select_storage_page(image, 2, first.identity))
        self.assertTrue(runner._transfer_input_issued)
        self.assertEqual(self.actions(desktop), ["click"])

    def test_confirmed_wheel_allows_later_pre_input_layout_reacquisition(self):
        runner, desktop, _, _, _ = self.setup_wheel()
        def operation():
            runner._scroll_inventory(-31)
            raise LayoutError("Inventory scroll phase changed after adoption")
        with self.assertRaises(SafeLayoutChange):
            runner._guarded_operation(operation)
        self.assertFalse(runner._transfer_input_issued)
        self.assertEqual(self.actions(desktop), ["scroll"])
        runner._inventory_viewport_frame.assert_called()

    def test_wheel_with_unconfirmed_readback_remains_terminal_after_one_input(self):
        runner, desktop, _, _, _ = self.setup_wheel()
        runner._park_inventory_readback.side_effect = LayoutChangedDuringOperation("Viewport occluded")
        self.assert_terminal(runner, lambda: runner._scroll_inventory(-31))
        self.assertEqual(self.actions(desktop), ["scroll"])
        self.assertEqual(runner._inventory_viewport_frame.call_count, 1)

    def test_wheel_with_changed_geometry_cannot_retire_accepted_input(self):
        runner, desktop, _, first, second = self.setup_wheel()
        changed = ViewportFrame(second.scan, "different-viewport", second.phase,
                                second.row_pitch_pixels, second.payload)
        runner._inventory_viewport_frame.side_effect = [first, changed]
        self.assert_terminal(runner, lambda: runner._scroll_inventory(-31))
        self.assertEqual(self.actions(desktop), ["scroll"])

    def test_cycle_observation_after_confirmed_wheel_is_safe_without_replaying_wheel(self):
        runner, desktop, _, _, _ = self.setup_wheel()
        error = LayoutChangedDuringOperation("Inventory scroll phase changed after adoption")
        def cycle():
            runner._scroll_inventory(-31)
            raise error
        runner._run_inventory_cycle_impl = cycle
        with self.assertRaises(SafeLayoutChange) as caught:
            runner._run_inventory_cycle()
        self.assertIs(caught.exception.__cause__, error)
        self.assertFalse(runner._transfer_input_issued)
        self.assertEqual(self.actions(desktop), ["scroll"])

    def test_cycle_with_unverified_accepted_wheel_remains_terminal_without_replay(self):
        runner, desktop, _, _, _ = self.setup_wheel()
        error = LayoutChangedDuringOperation("Accepted wheel readback failed")
        runner._park_inventory_readback.side_effect = error
        runner._run_inventory_cycle_impl = lambda: runner._scroll_inventory(-31)
        with self.assertRaises(LayoutChangedDuringOperation) as caught:
            runner._run_inventory_cycle()
        self.assertIs(caught.exception, error)
        self.assertNotIsInstance(caught.exception, SafeLayoutChange)
        self.assertTrue(runner._transfer_input_issued)
        self.assertEqual(self.actions(desktop), ["scroll"])


if __name__ == "__main__":
    unittest.main()
