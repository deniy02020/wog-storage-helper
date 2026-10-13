"""Guarded runtime wiring for bounded native inventory bursts; no OS input."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from inventory_motion_policy import InventoryMotionUnobservable, MAXIMUM_WHEEL_ROW_FRACTION
from inventory_scroller import ViewportFrame
from inventory_walk import InventoryWalkResult
from input_activity import UserActivityInterrupted
from runtime import Runner, LayoutChangedDuringOperation
import test_runtime_sorted_batches as fixtures
from windows import WindowGeometryChanged


def burst_runner():
    runner = Runner.__new__(Runner)
    runner.handle, runner.size = 7, (1000, 800)
    runner.profile = {}
    runner.canonical_profile = {"automatic_layout": True}
    runner.check_stop, runner.wait, runner.log = Mock(), Mock(), Mock()
    runner.desktop = SimpleNamespace(scroll_burst=Mock())
    scan = fixtures.observation().scan
    viewport = SimpleNamespace(viewport=scan.grid.rect)
    first = ViewportFrame(scan, fixtures.GEOMETRY, 0., 65., (viewport, object()))
    second = replace(first, phase=32., payload=(viewport, object()))
    runner._inventory_viewport_frame = Mock(side_effect=[first, second])
    runner._park_inventory_readback = Mock(return_value=second.payload[1])
    return runner, first, second


class RuntimeNativeBulkBoundaryTests(unittest.TestCase):
    def test_one_guarded_native_burst_reacquires_geometry_and_records_only_pulses(self):
        runner, first, second = burst_runner()
        runner.desktop.scroll_burst.side_effect = lambda *_: self.assertTrue(runner._transfer_input_issued)
        with patch("runtime.logging.info") as log:
            self.assertIs(runner._scroll_inventory_bulk(-31, 10), second)
        runner.desktop.scroll_burst.assert_called_once_with(
            7, first.payload[0].viewport.center, runner.size, -31, 10)
        runner.wait.assert_called_once_with(.25)
        runner._park_inventory_readback.assert_called_once_with()
        self.assertEqual(runner._inventory_viewport_frame.call_args_list,
                         [unittest.mock.call(), unittest.mock.call(second.payload[1])])
        self.assertFalse(runner._transfer_input_issued)
        self.assertEqual(runner._inventory_bulk_receipt, (fixtures.GEOMETRY, -31, 10))
        self.assertNotIn("proof:", log.call_args.args[0])

    def test_invalid_burst_bounds_or_missing_native_capability_send_no_input(self):
        for delta, count in ((True, 2), (0, 2), (54, 2), (-31, True),
                             (-31, 1), (-31, 11), (-31, 2.5)):
            with self.subTest(delta=delta, count=count):
                runner, _, _ = burst_runner()
                with self.assertRaises(ValueError):
                    runner._scroll_inventory_bulk(delta, count)
                runner.desktop.scroll_burst.assert_not_called()
                runner._inventory_viewport_frame.assert_not_called()
        runner, _, _ = burst_runner()
        del runner.desktop.scroll_burst
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            runner._scroll_inventory_bulk(-31, 2)
        runner._inventory_viewport_frame.assert_not_called()

    def test_f8_or_takeover_before_action_sends_no_burst(self):
        for error in (InterruptedError("F8"), UserActivityInterrupted("physical mouse")):
            with self.subTest(error=type(error).__name__):
                runner, _, _ = burst_runner()
                runner.check_stop.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    runner._scroll_inventory_bulk(-31, 10)
                self.assertIs(caught.exception, error)
                runner.desktop.scroll_burst.assert_not_called()
                runner._park_inventory_readback.assert_not_called()

    def test_native_typed_unissued_or_partly_issued_burst_boundary_stays_exact(self):
        for previous, issued in ((False, False), (False, True), (True, False)):
            with self.subTest(previous=previous, issued=issued):
                runner, _, _ = burst_runner()
                runner._transfer_input_issued = previous
                error = WindowGeometryChanged("native guard", input_issued=issued)
                runner.desktop.scroll_burst.side_effect = error
                with self.assertRaises(WindowGeometryChanged) as caught:
                    runner._scroll_inventory_bulk(-31, 10)
                self.assertIs(caught.exception, error)
                self.assertEqual(runner._transfer_input_issued, previous or issued)
                runner.desktop.scroll_burst.assert_called_once()
                runner._park_inventory_readback.assert_not_called()

    def test_accepted_burst_unconfirmed_readback_or_changed_geometry_is_never_repeated(self):
        for reason in ("readback", "geometry", "takeover"):
            with self.subTest(reason=reason):
                runner, first, second = burst_runner()
                if reason == "geometry":
                    runner._inventory_viewport_frame.side_effect = [
                        first, replace(second, geometry_identity="other native layout")]
                    error_type = LayoutChangedDuringOperation
                else:
                    error = (LayoutChangedDuringOperation("unsettled rows") if reason == "readback"
                             else UserActivityInterrupted("physical input after burst"))
                    runner._park_inventory_readback.side_effect = error
                    error_type = type(error)
                runner._run_inventory_cycle_impl = lambda: runner._scroll_inventory_bulk(-31, 10)
                with self.assertRaises(error_type):
                    runner._run_inventory_cycle()
                self.assertTrue(runner._transfer_input_issued)
                runner.desktop.scroll_burst.assert_called_once()
                self.assertFalse(hasattr(runner, "_inventory_bulk_receipt"))


class RuntimeBulkConstructorWiringTests(unittest.TestCase):
    def controller(self, runner):
        result = InventoryWalkResult("complete", 0, 0, 0)
        with patch("inventory_walk.run_inventory_walk", return_value=result):
            self.assertIs(runner._walk_inventory(sorted_after_sort=True), result)
        return runner._inventory_scroll_session[1]

    def test_missing_native_burst_preserves_fine_scroll_policy_and_original_calibration(self):
        runner = fixtures.instance()
        controller = self.controller(runner)
        self.assertIsNone(controller.bulk_wheel)
        self.assertIsNone(controller.bulk_ready)
        self.assertIsNone(controller.bulk_motion)
        self.assertEqual(controller.wheel_delta, 31)
        self.assertEqual(MAXIMUM_WHEEL_ROW_FRACTION, .60)
        controller._steps = {"up": 32., "down": 32.}
        self.assertIs(self.controller(runner), controller)
        self.assertEqual(controller._steps, {"up": 32., "down": 32.})
        runner._scroll_inventory.assert_not_called()

    def test_all_three_callbacks_use_exact_native_rgb_payload_and_guarded_burst(self):
        runner = fixtures.instance()
        runner.desktop.scroll_burst = Mock()
        runner._scroll_inventory_bulk = Mock()
        before = ViewportFrame(fixtures.observation().scan, fixtures.GEOMETRY, 0., 65.,
                               (object(), object()))
        after = replace(before, phase=32., payload=(object(), object()))
        runner._inventory_bulk_receipt = (after.geometry_identity, -31, 10)
        with patch("inventory_viewport.inventory_bulk_ready", return_value=True) as ready, \
                patch("inventory_viewport.prove_inventory_bulk_translation", return_value=320.) as proof:
            controller = self.controller(runner)
            self.assertIs(controller.bulk_wheel, runner._scroll_inventory_bulk)
            self.assertTrue(controller.bulk_ready(before, direction="down", maximum_rows=5,
                                                  minimum_overlap_rows=2))
            ready.assert_called_once_with(before.payload[1], before.payload[0], direction="down",
                                           maximum_rows=5, minimum_overlap_rows=2)
            with patch("runtime.logging.info") as log:
                distance = controller.bulk_motion(before, after, direction="down", maximum_rows=5,
                                                  minimum_overlap_rows=2)
            self.assertEqual(distance, 320.)
            proof.assert_called_once_with(before.payload[1], after.payload[1],
                before.payload[0], after.payload[0], "down", maximum_rows=5, minimum_overlap_rows=2)
            self.assertEqual(log.call_args.args[1:], ("down", 320., 320. / 65., 10, 2))
            controller.bulk_wheel(-31, 10)
            runner._scroll_inventory_bulk.assert_called_once_with(-31, 10)

    def test_ambiguous_after_burst_rgb_proof_propagates_without_false_pixels_log(self):
        runner = fixtures.instance()
        runner.desktop.scroll_burst = Mock()
        before = ViewportFrame(fixtures.observation().scan, fixtures.GEOMETRY, 0., 65.,
                               (object(), object()))
        after = replace(before, phase=32., payload=(object(), object()))
        error = InventoryMotionUnobservable("clone or changed overlap")
        with patch("inventory_viewport.prove_inventory_bulk_translation", side_effect=error) as proof:
            controller = self.controller(runner)
            with patch("runtime.logging.info") as log, self.assertRaises(InventoryMotionUnobservable) as caught:
                controller.bulk_motion(before, after, direction="down", maximum_rows=5,
                                       minimum_overlap_rows=2)
            self.assertIs(caught.exception, error)
            log.assert_not_called()
            proof.assert_called_once()
            runner.desktop.scroll_burst.assert_not_called()
            runner._scroll_inventory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
