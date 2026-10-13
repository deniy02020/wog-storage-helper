"""Incomplete healthy scans keep the timer alive without claiming completion.

The returned walk/cycle evidence and every native callback below are explicit
test doubles. These are coordinator regressions, not real endpoint proofs or
native game input tests.
"""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auto_layout import LayoutError
from input_activity import UserActivityInterrupted
from inventory_cycle import InventoryCycleResult
from inventory_scroller import InventoryScrollUnconfirmed
from inventory_walk import InventoryWalkResult
from runtime import INVENTORY_PENDING, LayoutChangedDuringOperation, SafeLayoutChange
from storage_cycle import StorageFull, STORAGE_FULL_MESSAGE
from test_runtime import RuntimeTestFixture
from windows import HitTestMismatch, WindowGeometryChanged


class HealthyIncompleteOutcomeTests(RuntimeTestFixture, unittest.TestCase):
    def automatic_runner(self, result=None, error=None):
        runner, desktop = self.runner([])
        # Only the automatic coordinator branch is under test. No synthetic
        # grid or supplied return value is passed off as native vision proof.
        runner.canonical_profile["automatic_layout"] = True
        runner.canonical_inv = replace(runner.canonical_inv, rows=8)
        runner.panel_controller = SimpleNamespace(layout=object())
        runner.prepare_panels = Mock()
        runner.sort_inventory = Mock()
        runner._walk_inventory = Mock(return_value=result, side_effect=error)
        runner.log = Mock()
        runner.state_callback = Mock()
        desktop.scroll = Mock(side_effect=AssertionError("no native wheel in coordinator tests"))
        return runner, desktop

    def assert_pending(self, result, *, transfers, scrolls=0, resets=0):
        self.assertIsInstance(result, InventoryWalkResult)
        self.assertEqual((result.outcome, result.transfers, result.scrolls, result.resets),
                         ("pending", transfers, scrolls, resets))
        self.assertFalse(result.complete)

    def assert_no_completion_log(self, runner):
        self.assertFalse(any("Проверка закончена" in call.args[0]
                             for call in runner.log.call_args_list))

    def test_healthy_walk_incomplete_results_preserve_verified_progress_and_wait(self):
        for outcome in ("unknown", "bounded", "top_unconfirmed", "endpoint_unconfirmed"):
            with self.subTest(outcome=outcome):
                walked = InventoryWalkResult(outcome, 4, 12, 3)
                runner, desktop = self.automatic_runner(walked)

                result = runner._run_inventory_cycle()

                self.assert_pending(result, transfers=4, scrolls=12, resets=3)
                runner.prepare_panels.assert_called_once_with()
                runner.sort_inventory.assert_called_once_with()
                runner._walk_inventory.assert_called_once_with(sorted_after_sort=True)
                self.assert_no_completion_log(runner)
                self.assertIn(INVENTORY_PENDING, [call.args[0] for call in runner.log.call_args_list])
                self.assertEqual(desktop.inputs, [])
                desktop.scroll.assert_not_called()

    def test_complete_walk_result_is_preserved_without_pending_log(self):
        walked = InventoryWalkResult("complete", 4, 12, 3)
        runner, desktop = self.automatic_runner(walked)

        self.assertIs(runner._run_inventory_cycle(), walked)

        self.assertFalse(any(call.args[0] == INVENTORY_PENDING for call in runner.log.call_args_list))
        self.assertEqual(desktop.inputs, [])

    def test_thrown_terminal_walk_errors_do_not_become_pending_or_start_another_walk(self):
        errors = (StorageFull(STORAGE_FULL_MESSAGE),
                  WindowGeometryChanged("accepted input; geometry changed", input_issued=True),
                  HitTestMismatch("foreign overlay", input_issued=True),
                  LayoutChangedDuringOperation("layout changed after input"),
                  RuntimeError("unverified item transfer"),
                  InterruptedError("F8"), UserActivityInterrupted("takeover"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                runner, desktop = self.automatic_runner(error=error)
                if isinstance(error, LayoutChangedDuringOperation):
                    def walk_with_outstanding_input(**kwargs):
                        runner._transfer_input_issued = True
                        raise error
                    runner._walk_inventory.side_effect = walk_with_outstanding_input

                with self.assertRaises(type(error)) as raised:
                    runner._run_inventory_cycle()

                self.assertIs(raised.exception, error)
                runner._walk_inventory.assert_called_once_with(sorted_after_sort=True)
                self.assertFalse(any(call.args[0] == INVENTORY_PENDING for call in runner.log.call_args_list))
                self.assertEqual(desktop.inputs, [])

    def test_phase_change_without_outstanding_input_becomes_safe_layout_wait(self):
        error = LayoutChangedDuringOperation("rows settled after confirmed interface action")
        runner, desktop = self.automatic_runner(error=error)

        with self.assertRaises(SafeLayoutChange) as raised:
            runner._run_inventory_cycle()

        self.assertIs(raised.exception.__cause__, error)
        self.assertFalse(runner._transfer_input_issued)
        runner._walk_inventory.assert_called_once_with(sorted_after_sort=True)
        self.assertEqual(desktop.inputs, [])

    def test_thrown_vision_or_scroll_guard_still_has_original_diagnostic_cause(self):
        for error in (LayoutError("occluded separator"),
                      InventoryScrollUnconfirmed("wrong signed wheel motion")):
            with self.subTest(error=type(error).__name__):
                runner, desktop = self.automatic_runner(error=error)

                with self.assertRaises(RuntimeError) as raised:
                    runner._run_inventory_cycle()

                self.assertIs(raised.exception.__cause__, error)
                runner._walk_inventory.assert_called_once_with(sorted_after_sort=True)
                self.assertFalse(any(call.args[0] == INVENTORY_PENDING for call in runner.log.call_args_list))
                self.assertEqual(desktop.inputs, [])

    def test_legacy_healthy_incomplete_results_keep_verified_transfer_count_without_walk(self):
        for outcome in ("unknown", "hidden_unconfirmed", "bounded"):
            with self.subTest(outcome=outcome):
                runner, desktop = self.runner([])
                runner.log = Mock()
                runner._walk_inventory = Mock(side_effect=AssertionError("no invented legacy viewport"))
                visible = InventoryCycleResult(outcome, 3, 2)

                with patch("runtime.run_inventory_cycle", return_value=visible):
                    result = runner._run_inventory_cycle()

                self.assert_pending(result, transfers=3)
                runner._walk_inventory.assert_not_called()
                self.assert_no_completion_log(runner)
                self.assertEqual(desktop.inputs, [])

    def test_legacy_safe_fallback_incomplete_sums_visible_and_walk_verified_transfers(self):
        for initial_outcome in ("unknown", "hidden_unconfirmed"):
            for walk_outcome in ("unknown", "bounded", "top_unconfirmed", "endpoint_unconfirmed"):
                with self.subTest(initial=initial_outcome, walk=walk_outcome):
                    runner, desktop = self.runner([])
                    runner.profile["inventory_viewport"] = {"rect": [0, 16, 48, 40]}
                    desktop.scroll = Mock(side_effect=AssertionError("no native wheel in coordinator tests"))
                    runner.log = Mock()
                    runner._walk_inventory = Mock(return_value=InventoryWalkResult(walk_outcome, 2, 7, 1))

                    with patch("runtime.run_inventory_cycle",
                               return_value=InventoryCycleResult(initial_outcome, 3, 2)):
                        result = runner._run_inventory_cycle()

                    self.assert_pending(result, transfers=5, scrolls=7, resets=1)
                    runner._walk_inventory.assert_called_once_with()
                    self.assert_no_completion_log(runner)
                    self.assertEqual(desktop.inputs, [])
                    desktop.scroll.assert_not_called()

    def test_failed_legacy_fallback_never_turns_verified_progress_into_pending(self):
        error = RuntimeError("transfer issued but not verified")
        runner, desktop = self.runner([])
        runner.profile["inventory_viewport"] = {"rect": [0, 16, 48, 40]}
        desktop.scroll = Mock()
        runner.log = Mock()
        runner._walk_inventory = Mock(side_effect=error)

        with patch("runtime.run_inventory_cycle", return_value=InventoryCycleResult("unknown", 3, 2)):
            with self.assertRaises(RuntimeError) as raised:
                runner._run_inventory_cycle()

        self.assertIs(raised.exception, error)
        runner._walk_inventory.assert_called_once_with()
        self.assertFalse(any(call.args[0] == INVENTORY_PENDING for call in runner.log.call_args_list))
        self.assertEqual(desktop.inputs, [])

    def test_complete_legacy_result_is_preserved(self):
        runner, desktop = self.runner([])
        runner.log = Mock()
        visible = InventoryCycleResult("complete", 3, 2)

        with patch("runtime.run_inventory_cycle", return_value=visible):
            self.assertIs(runner._run_inventory_cycle(), visible)

        self.assertFalse(any(call.args[0] == INVENTORY_PENDING for call in runner.log.call_args_list))
        self.assertEqual(desktop.inputs, [])

    def test_pending_automatic_scan_does_not_stop_timer_and_next_cycle_can_complete(self):
        runner, desktop = self.automatic_runner()
        runner._walk_inventory.side_effect = [InventoryWalkResult("unknown", 4, 12, 3),
                                             InventoryWalkResult("complete", 1, 2, 1)]
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                runner.stop.set()
            runner.check_stop()
        runner.wait = wait

        with patch("runtime.time.monotonic", return_value=1000.), self.assertRaises(InterruptedError):
            runner.monitor(1800)

        self.assertEqual(waits, [1800, 1800])
        self.assertEqual(runner._walk_inventory.call_count, 2)
        states = [call.args[0] for call in runner.state_callback.call_args_list]
        self.assertEqual(states[0], {"state": "waiting", "message": INVENTORY_PENDING,
                                    "next_check_at": 2800.})
        self.assertIn("Проверка закончена", states[1]["message"])
        self.assertTrue(runner.stop.is_set())
        self.assertEqual(desktop.inputs, [])


if __name__ == "__main__":
    unittest.main()
