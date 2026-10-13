"""Complete sorted-prefix proof with genuine-sized, synthetic raster crops.

The native game at nominal 65px pitch can expose eight COMPLETE rows with a
three-pixel top inset and one-pixel bottom inset. No game images or input are
used here; recognition and transfer callbacks keep their existing proof roles.
"""
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from core import GridSpec, Rect
from inventory_walk import certify_full_inventory_top, run_inventory_walk
import test_runtime_sorted_batches as fixtures

FULL, PARTIAL = fixtures.FULL, fixtures.PARTIAL
fake_scroller, instance, observation = fixtures.fake_scroller, fixtures.instance, fixtures.observation


RASTER = GridSpec(Rect(250, 103, 575, 619), 8, 5)


class FullTopRasterGeometryTests(unittest.TestCase):
    def proves(self, grid=RASTER, *, scale=1., pitch=65., viewport=FULL.rect,
               offset=0., top=True):
        view = replace(observation([2] * grid.count, grid=grid, offset=offset, top=top),
                       row_pitch_pixels=pitch)
        return certify_full_inventory_top(view, viewport, 8, 5, scale)

    def test_exact_full_grid_and_native_three_pixel_inset_are_complete_top(self):
        self.assertTrue(self.proves(FULL))
        self.assertTrue(self.proves())

    def test_half_scale_rounding_keeps_all_eight_complete_rows(self):
        viewport = Rect(125, 50, 288, 310)
        grid = GridSpec(Rect(125, 52, 288, 309), 8, 5)
        self.assertTrue(self.proves(grid, scale=.5, pitch=32.5, viewport=viewport))

    def test_partial_seven_row_top_never_proves_hidden_beginning(self):
        self.assertFalse(self.proves(PARTIAL))

    def test_unproved_top_or_scrolled_position_cannot_certify(self):
        self.assertFalse(self.proves(top=False))
        self.assertFalse(self.proves(offset=32., top=False))

    def test_large_crops_or_out_of_viewport_rectangles_cannot_drop_cells(self):
        for rect in (Rect(250, 106, 575, 620), Rect(250, 100, 575, 614),
                     Rect(250, 99, 575, 619), Rect(250, 101, 575, 621),
                     Rect(251, 103, 575, 619), Rect(250, 103, 574, 619)):
            with self.subTest(rect=rect):
                self.assertFalse(self.proves(GridSpec(rect, 8, 5)))

    def test_changed_pitch_rows_columns_or_scale_cannot_certify(self):
        for grid, pitch, scale in ((GridSpec(RASTER.rect, 8, 4), 65., 1.),
                                   (GridSpec(RASTER.rect, 9, 5), 65., 1.),
                                   (RASTER, 64.5, 1.), (RASTER, 65., True),
                                   (RASTER, 65., 1.6)):
            with self.subTest(grid=grid, pitch=pitch, scale=scale):
                self.assertFalse(self.proves(grid, pitch=pitch, scale=scale))
        self.assertFalse(self.proves(GridSpec(Rect(250, 105, 575, 615), 8, 5)))


class RuntimeSortedRasterCallbackTests(unittest.TestCase):
    callbacks = fixtures.RuntimeSortedCallbackTests.callbacks
    def test_native_raster_lower_tiers_or_empty_tail_end_without_scroll(self):
        for labels in ([2] * 40, [2, 1], []):
            with self.subTest(labels=labels):
                callbacks = self.callbacks(sorted_after_sort=True)
                view = observation(labels, grid=RASTER)
                self.assertTrue(callbacks["sorted_top"](view))
                self.assertTrue(callbacks["sorted_boundary"](view))
                self.scroller.scroll_down.assert_not_called()

    def test_raster_top_with_selection_hole_waits_for_lowest_selected_tier(self):
        callbacks = self.callbacks(sorted_after_sort=True, selected=(3, 5, 6))
        self.assertFalse(callbacks["sorted_boundary"](observation([4] * 40, grid=RASTER)))
        self.assertTrue(callbacks["sorted_boundary"](
            observation([2] * 35, grid=PARTIAL, offset=32., top=False)))

    def test_raster_unknown_or_known_order_contradiction_still_invalidates_epoch(self):
        for labels in ([None, 2], [5, 4, 5], [2, "empty", 1], [3, 2]):
            with self.subTest(labels=labels):
                callbacks = self.callbacks(sorted_after_sort=True, selected=(3, 6))
                self.assertFalse(callbacks["sorted_boundary"](observation(labels, grid=RASTER)))
                self.assertFalse(callbacks["sorted_boundary"](observation([2] * 40, grid=RASTER)))


class RuntimeSortedRasterWalkTests(unittest.TestCase):
    def test_native_inset_compacts_after_verified_item_and_skips_low_empty_tail(self):
        runner = instance()
        labels = [3, 2]
        scroller = fake_scroller()
        scroller.snapshot.side_effect = lambda: observation(labels, grid=RASTER)
        scroller.reset_to_top.side_effect = lambda: observation(labels, grid=RASTER)
        def once(*, sort):
            self.assertIs(sort, False)
            if 3 in labels:
                labels[labels.index(3)] = "empty"
                return True
            return False
        runner.once.side_effect = once
        runner.sort_inventory.side_effect = lambda: labels.__setitem__(slice(None), [2])
        with patch("inventory_scroller.InventoryScroller", return_value=scroller):
            result = runner._walk_inventory(sorted_after_sort=True)
        self.assertEqual((result.outcome, result.transfers, result.scrolls), ("complete", 1, 0))
        runner.sort_inventory.assert_called_once_with()
        scroller.scroll_down.assert_not_called()
        self.assertEqual(scroller.reset_to_top.call_count, 3)

    def test_final_top_partial_first_row_leaves_completion_unconfirmed_without_scroll(self):
        runner = instance()
        full = observation([2] * 40, grid=RASTER)
        scroller = fake_scroller(full)
        scroller.snapshot.side_effect = [full, observation([2] * 35, grid=PARTIAL)]
        with patch("inventory_scroller.InventoryScroller", return_value=scroller):
            result = runner._walk_inventory(sorted_after_sort=True)
        self.assertEqual((result.outcome, result.transfers, result.scrolls),
                         ("top_unconfirmed", 0, 0))
        runner.sort_inventory.assert_not_called()
        scroller.scroll_down.assert_not_called()
        scroller.reset_to_top.assert_called_once_with()

    def test_unknown_raster_prefix_still_requires_full_walk_and_never_claims_complete(self):
        runner = instance()
        full = observation([None, 2], grid=RASTER)
        bottom = observation([2] * 35, grid=PARTIAL, offset=32., top=False, bottom=True)
        scroller = fake_scroller(full)
        scroller.reset_to_top.side_effect = [full, full]
        scroller.snapshot.side_effect = [full, bottom]
        scroller.scroll_down.side_effect = None
        scroller.scroll_down.return_value = bottom
        with patch("inventory_scroller.InventoryScroller", return_value=scroller):
            result = runner._walk_inventory(sorted_after_sort=True)
        self.assertEqual((result.outcome, result.transfers, result.scrolls), ("unknown", 0, 1))
        runner.sort_inventory.assert_not_called()

    def test_final_top_proof_callback_must_be_strict_boolean_and_propagates_guard_errors(self):
        full = observation([2] * 40, grid=RASTER)
        for value in (None, 1, "yes"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "verified boolean"):
                run_inventory_walk(reset_to_top=lambda: full, inventory_snapshot=lambda: full,
                    transfer_batch=lambda **_: 0, sorted_boundary=lambda _: True,
                    sorted_top=lambda _: value, sorted_epoch=lambda: 1,
                    scroll_down=Mock(side_effect=AssertionError("no wheel")),
                    selected_tiers={3, 4, 5, 6}, check_stop=lambda: None, pause=lambda _: None)
        stop = InterruptedError("F8 at final Top proof")
        with self.assertRaises(InterruptedError) as raised:
            run_inventory_walk(reset_to_top=lambda: full, inventory_snapshot=lambda: full,
                transfer_batch=lambda **_: 0, sorted_boundary=lambda _: True,
                sorted_top=Mock(side_effect=stop), sorted_epoch=lambda: 1,
                scroll_down=Mock(side_effect=AssertionError("no wheel")),
                selected_tiers={3, 4, 5, 6}, check_stop=lambda: None, pause=lambda _: None)
        self.assertIs(raised.exception, stop)


if __name__ == "__main__":
    unittest.main()
