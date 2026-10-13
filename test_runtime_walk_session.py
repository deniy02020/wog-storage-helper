"""Runtime session/cache boundaries; no screenshots or native input are used.

The walk policy is replaced at its entry point. Real Scroller construction and
begin_cycle remain exercised; calibration sentinels below stand only for prior
session state, never for a native movement or endpoint proof.
"""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from auto_layout import LayoutError
from core import CellState, GridScan, GridSpec, Rect
from input_activity import UserActivityInterrupted
from inventory_scroller import InventoryScroller, InventoryScrollUnconfirmed
from inventory_walk import (InventoryEndpointProof, InventoryWalkObservation,
                            InventoryWalkResult, run_inventory_walk)
from runtime import Runner
from storage_cycle import StorageFull, STORAGE_FULL_MESSAGE
from windows import FocusUnavailable, HitTestMismatch, WindowGeometryChanged, WindowInfo


def runner():
    instance = Runner.__new__(Runner)
    instance.handle = 5
    instance.desktop = SimpleNamespace(window_info=Mock(return_value=WindowInfo(
        5, "War of Genesis: Idle Loot", r"C:\Games\Genesis.exe", 1001)))
    instance.profile = {
        "size": [1000, 800],
        "anchors": [{"rect": [10, 10, 200, 40]}, {"rect": [600, 10, 780, 40]}],
        "layout_scales": {"inventory": 1.0, "storage": 1.0},
        "inventory_viewport": {"rect": [250, 100, 575, 620], "phase": 0.0},
        "inventory": {"rect": [250, 100, 575, 620], "rows": 8, "columns": 5},
    }
    instance.filters = {str(tier): {"enabled": tier in (1, 2, 3, 5)}
                        for tier in range(1, 7)}
    instance.check_stop = Mock()
    instance.wait = Mock()
    instance.log = Mock()
    # These callbacks must remain unused when testing the coordinator boundary.
    instance._inventory_viewport_frame = Mock(side_effect=AssertionError("unexpected capture"))
    instance._scroll_inventory = Mock(side_effect=AssertionError("unexpected wheel input"))
    instance.once = Mock(side_effect=AssertionError("unexpected transfer input"))
    instance.sort_inventory = Mock(side_effect=AssertionError("unexpected sort input"))
    return instance


class RuntimeWalkSessionTests(unittest.TestCase):
    def setUp(self):
        self.runner = runner()
        self.result = InventoryWalkResult("complete", 0, 0, 0)
        self.walk_patch = patch("inventory_walk.run_inventory_walk", return_value=self.result)
        self.walk = self.walk_patch.start()
        self.addCleanup(self.walk_patch.stop)

    def tearDown(self):
        self.runner._inventory_viewport_frame.assert_not_called()
        self.runner._scroll_inventory.assert_not_called()
        self.runner.once.assert_not_called()
        self.runner.sort_inventory.assert_not_called()

    def controller(self):
        self.assertIs(self.runner._walk_inventory(), self.result)
        return self.runner._inventory_scroll_session[1]

    def test_same_native_identity_reuses_controller_with_fresh_budget_and_position(self):
        first = self.controller()
        self.assertIsInstance(first, InventoryScroller)
        self.assertEqual(first.wheel_delta, 31)
        self.assertEqual((first.max_wheel_inputs, first.max_seconds), (512, 900))
        # Synthetic state tests the session handoff, not RGB calibration itself.
        first._geometry, first._pitch = "previously-bound viewport", 64.4
        first._steps = {"up": 31.4, "down": 31.8}
        first._wheel_inputs, first._started_at = 512, -10000.0
        first._frame, first._offset = object(), 120.0
        first._top_proof, first._bottom_proof = object(), object()
        first.clock = lambda: 2000.0
        with patch.object(first, "begin_cycle", wraps=first.begin_cycle) as begin:
            self.assertIs(self.controller(), first)
            begin.assert_called_once_with()
        self.assertEqual((first.wheel_inputs, first._started_at), (0, 2000.0))
        self.assertEqual((first._geometry, first._pitch), ("previously-bound viewport", 64.4))
        self.assertEqual(first._steps, {"up": 31.4, "down": 31.8})
        self.assertIsNone(first._frame)
        self.assertIsNone(first._offset)
        self.assertIsNone(first._top_proof)
        self.assertIsNone(first._bottom_proof)
        self.runner.check_stop.assert_called_once_with()
        self.runner.desktop.window_info.assert_any_call(5)
        self.assertEqual(self.walk.call_args.kwargs["selected_tiers"], {3, 5})
        observed = object()
        with patch.object(first, "snapshot", return_value=observed) as snapshot:
            self.assertIs(self.walk.call_args.kwargs["inventory_snapshot"](), observed)
        snapshot.assert_called_once_with()

    def test_changed_native_handle_pid_or_executable_discards_calibration(self):
        variants = ("handle", "pid", "executable")
        for change in variants:
            with self.subTest(change=change):
                self.runner = runner()
                first = self.controller()
                first._steps = {"down": 31.8}
                info = self.runner.desktop.window_info.return_value
                if change == "handle":
                    self.runner.handle = 6
                elif change == "pid":
                    self.runner.desktop.window_info.return_value = WindowInfo(
                        info.handle, info.title, info.executable, 1002)
                else:
                    self.runner.desktop.window_info.return_value = WindowInfo(
                        info.handle, info.title, r"D:\Steam\Genesis.exe", info.process_id)
                second = self.controller()
                self.assertIsNot(second, first)
                self.assertEqual(second._steps, {})

    def test_changed_physical_layout_discards_controller(self):
        for change in ("size", "anchor", "pane_scale", "viewport"):
            with self.subTest(change=change):
                self.runner = runner()
                first = self.controller()
                first._steps = {"down": 31.8}
                if change == "size":
                    self.runner.profile["size"][0] += 1
                elif change == "anchor":
                    self.runner.profile["anchors"][0]["rect"][0] += 1
                elif change == "pane_scale":
                    self.runner.profile["layout_scales"]["inventory"] = 0.5
                else:
                    self.runner.profile["inventory_viewport"]["rect"][0] += 1
                second = self.controller()
                self.assertIsNot(second, first)
                self.assertEqual(second._steps, {})

    def test_scroll_phase_and_complete_row_scan_do_not_change_physical_binding(self):
        first = self.controller()
        binding = copy.deepcopy(self.runner._layout_binding())
        self.runner.profile["inventory_viewport"]["phase"] = 31.8
        self.runner.profile["inventory"] = {
            "rect": [250, 132, 575, 584], "rows": 7, "columns": 5}
        self.assertEqual(self.runner._layout_binding(), binding)
        self.assertIs(self.controller(), first)

    def test_different_previous_wheel_delta_cannot_borrow_calibration(self):
        first = self.controller()
        context, _ = self.runner._inventory_scroll_session
        first._steps = {"down": 31.8}
        self.runner._inventory_scroll_session = (context[:-1] + (15,), first)
        second = self.controller()
        self.assertIsNot(second, first)
        self.assertEqual(second._steps, {})
        self.assertEqual(second.wheel_delta, 31)

    def test_missing_native_identity_never_reuses_a_controller(self):
        for identity in ("no_reader", "noncallable", "no_window", "pid_unknown"):
            with self.subTest(identity=identity):
                self.runner = runner()
                if identity == "no_reader":
                    self.runner.desktop = SimpleNamespace()
                elif identity == "noncallable":
                    self.runner.desktop.window_info = None
                elif identity == "no_window":
                    self.runner.desktop.window_info.return_value = None
                else:
                    self.runner.desktop.window_info.return_value = WindowInfo(
                        5, "War of Genesis: Idle Loot", r"C:\Games\Genesis.exe", 0)
                first = self.controller()
                first._steps = {"down": 31.8}
                second = self.controller()
                self.assertIsNot(second, first)
                self.assertEqual(second._steps, {})
                self.assertIsNone(self.runner._inventory_scroll_session[0][1])

    def test_stop_at_new_cycle_prevents_cached_walk_and_preserves_calibration(self):
        first = self.controller()
        first._steps = {"down": 31.8}
        stop = InterruptedError("F8")
        self.runner.check_stop.side_effect = stop
        self.walk.reset_mock()
        with self.assertRaises(InterruptedError) as raised:
            self.runner._walk_inventory()
        self.assertIs(raised.exception, stop)
        self.walk.assert_not_called()
        self.assertEqual(first._steps, {"down": 31.8})

    def test_native_identity_guard_exception_prevents_walk_and_new_controller(self):
        guard = WindowGeometryChanged("PID changed during identity capture", input_issued=True)
        self.runner.desktop.window_info.side_effect = guard
        with self.assertRaises(WindowGeometryChanged) as raised:
            self.runner._walk_inventory()
        self.assertIs(raised.exception, guard)
        self.walk.assert_not_called()
        self.assertFalse(hasattr(self.runner, "_inventory_scroll_session"))


class RuntimeWalkFailureBoundaryTests(unittest.TestCase):
    def test_unproved_recognition_is_translated_with_raw_diagnostic_and_original_cause(self):
        for error in (LayoutError("partial row gaps missing"),
                      InventoryScrollUnconfirmed("same rows cannot prove bottom")):
            with self.subTest(error=type(error).__name__):
                instance = runner()
                instance._run_inventory_cycle_impl = Mock(side_effect=error)
                with self.assertRaises(RuntimeError) as raised:
                    instance._run_inventory_cycle()
                self.assertEqual(str(raised.exception),
                                 "Проверка инвентаря не завершена. Работа остановлена.")
                self.assertIs(raised.exception.__cause__, error)
                instance.log.assert_called_once_with("Диагностика обхода инвентаря: " + str(error))
                instance._run_inventory_cycle_impl.assert_called_once_with()
                instance._scroll_inventory.assert_not_called()
                instance.once.assert_not_called()

    def test_storage_full_and_native_guards_propagate_without_retry_or_relabeling(self):
        errors = (StorageFull(STORAGE_FULL_MESSAGE),
                  WindowGeometryChanged("accepted input; geometry changed", input_issued=True),
                  HitTestMismatch("another app under pointer", input_issued=True),
                  FocusUnavailable(), InterruptedError("F8"),
                  UserActivityInterrupted("user input during cycle"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                instance = runner()
                instance._run_inventory_cycle_impl = Mock(side_effect=error)
                with self.assertRaises(type(error)) as raised:
                    instance._run_inventory_cycle()
                self.assertIs(raised.exception, error)
                self.assertIsNone(raised.exception.__cause__)
                instance.log.assert_not_called()
                instance._run_inventory_cycle_impl.assert_called_once_with()
                instance._scroll_inventory.assert_not_called()
                instance.once.assert_not_called()

    def test_successful_result_is_forwarded_unchanged(self):
        instance = runner()
        result = InventoryWalkResult("complete", 4, 15, 5)
        instance._run_inventory_cycle_impl = Mock(return_value=result)
        self.assertIs(instance._run_inventory_cycle(), result)
        instance._run_inventory_cycle_impl.assert_called_once_with()
        instance.log.assert_not_called()


class SharedWheelEndpointPolicyTests(unittest.TestCase):
    def walk(self, step):
        # Explicit synthetic endpoint proofs exercise the consumer's bound;
        # their existence does not assert actual native endpoint evidence.
        grid = GridSpec(Rect(100, 200, 425, 652), 7, 5)
        cells = tuple(CellState(index, grid.cell_rect(index), 6, False, 0., 20.)
                      for index in range(grid.count))
        geometry = "synthetic stable viewport"
        top = InventoryEndpointProof("calibrated_subrow_clamp", "top", geometry, 2, step)
        bottom = InventoryEndpointProof("calibrated_subrow_clamp", "bottom", geometry, 2, step)
        observation = InventoryWalkObservation(GridScan(grid, cells), geometry, 0., 64.4, top, bottom)
        self.scroll = Mock(side_effect=AssertionError("already proved bottom must not scroll"))
        return run_inventory_walk(reset_to_top=lambda: observation,
            inventory_snapshot=lambda: observation, transfer=lambda **kwargs: False,
            scroll_down=self.scroll, selected_tiers={3}, check_stop=lambda: None,
            pause=lambda seconds: None)

    def test_half_row_calibration_is_accepted_at_both_endpoint_consumers(self):
        result = self.walk(31.8)
        self.assertTrue(result.complete)
        self.assertEqual((result.transfers, result.scrolls, result.resets), (0, 0, 2))
        self.scroll.assert_not_called()

    def test_sixty_percent_and_whole_row_aliases_cannot_establish_endpoints(self):
        for step in (64.4 * .60, 64.4):
            with self.subTest(step=step), self.assertRaises(ValueError):
                self.walk(step)
            self.scroll.assert_not_called()


if __name__ == "__main__":
    unittest.main()
