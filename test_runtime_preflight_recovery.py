"""Transient pre-input changes are reacquired; fake input is never replayed."""
from unittest.mock import Mock, patch
import unittest

from runtime import SafePreflightChange, PREFLIGHT_ATTEMPTS, LAYOUT_RETRY_SECONDS
from test_runtime import RuntimeTestFixture, cell, frame


class PreflightRecoveryTests(RuntimeTestFixture, unittest.TestCase):
    def automatic(self, frames):
        runner, desktop = self.runner(frames)
        runner.canonical_profile["automatic_layout"] = True
        runner.log = Mock()
        return runner, desktop

    def test_passive_drop_reacquires_entire_baseline_before_one_verified_move(self):
        before = frame([cell(6), cell()], [cell(), cell()])
        dropped = frame([cell(6), cell(2)], [cell(), cell()])
        after = frame([cell(), cell(2)], [cell(6), cell()])
        runner, desktop = self.automatic([before, dropped, dropped.copy(), after])
        self.assertTrue(runner.once(sort=False))
        self.assertEqual(sum(kind == "drag" for kind, _ in desktop.inputs), 1)
        self.assertFalse(runner._transfer_input_issued)
        self.assertEqual(runner._last_inventory_scan.cells[1].tier, 2)

    def test_selected_tier_changed_requires_new_stable_pair_and_new_candidate(self):
        before = frame([cell(6), cell()], [cell(), cell()])
        changed = frame([cell(4), cell()], [cell(), cell()])
        after = frame([cell(), cell()], [cell(4), cell()])
        runner, desktop = self.automatic([before, changed, changed.copy(), after])
        self.assertTrue(runner.once(sort=False))
        self.assertEqual(sum(kind == "drag" for kind, _ in desktop.inputs), 1)
        self.assertTrue(any(message.args[0].startswith("Перенесён T4")
                            for message in runner.log.call_args_list))

    def test_new_destination_occupant_requires_fresh_empty_destination(self):
        before = frame([cell(6), cell()], [cell(), cell()])
        changed = frame([cell(6), cell()], [cell(3), cell()])
        after = frame([cell(), cell()], [cell(3), cell(6)])
        runner, desktop = self.automatic([before, changed, changed.copy(), after])
        self.assertTrue(runner.once(sort=False))
        moves = [args for kind, args in desktop.inputs if kind == "drag"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][2], runner.store.cell_rect(1).center)

    def test_continuous_changes_exhaust_read_budget_without_item_input(self):
        frames = [frame([cell(tier), cell()], [cell(), cell()]) for tier in (6, 4, 3, 5)]
        runner, desktop = self.automatic(frames)
        with self.assertRaises(SafePreflightChange):
            runner.once(sort=False)
        self.assertEqual(PREFLIGHT_ATTEMPTS, 3)
        self.assertEqual(desktop.inputs, [])
        self.assertFalse(runner._transfer_input_issued)

    def test_excluded_or_unknown_replacement_never_uses_stale_selected_item(self):
        for tier in (1, 2, 7):
            with self.subTest(tier=tier):
                before = frame([cell(6), cell()], [cell(), cell()])
                changed = frame([cell(tier), cell()], [cell(), cell()])
                runner, desktop = self.automatic([before, changed])
                self.assertFalse(runner.once(sort=False))
                self.assertEqual(desktop.inputs, [])
                self.assertNotEqual(runner._last_inventory_scan.cells[0].tier, 6)

    def test_stop_during_reacquisition_prevents_next_capture_or_input(self):
        before = frame([cell(6), cell()], [cell(), cell()])
        changed = frame([cell(4), cell()], [cell(), cell()])
        runner, desktop = self.automatic([before, changed])
        def stop(_seconds):
            runner.stop.set()
            runner.check_stop()
        runner.wait = stop
        with self.assertRaises(InterruptedError):
            runner.once(sort=False)
        self.assertEqual(desktop.inputs, [])

    def test_pending_preflight_keeps_monitor_alive_then_honours_stop(self):
        runner, desktop = self.automatic([])
        runner._run_inventory_cycle = Mock(side_effect=[SafePreflightChange("bag changed before input"),
                                                        Mock(outcome="complete")])
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if seconds == 1800:
                runner.stop.set()
            runner.check_stop()
        runner.wait = wait
        with self.assertRaises(InterruptedError):
            runner.monitor(1800)
        self.assertEqual(waits, [LAYOUT_RETRY_SECONDS, 1800])
        self.assertEqual(runner._run_inventory_cycle.call_count, 2)
        self.assertEqual(desktop.inputs, [])

    def test_unverified_issued_transfer_is_still_terminal_after_reacquisition(self):
        before = frame([cell(6), cell()], [cell(), cell()])
        changed = frame([cell(6), cell(2)], [cell(), cell()])
        runner, desktop = self.automatic([before, changed, changed.copy()])
        desktop.capture = Mock(side_effect=[before, changed, changed.copy()])
        runner._park_and_capture = Mock(return_value=changed)
        runner._capture_valid_layout = Mock(side_effect=[before, changed, changed.copy(), changed])
        # Expire the readback budget immediately after the one accepted input.
        with patch("runtime.time.monotonic", side_effect=[0., 5.]):
            with self.assertRaisesRegex(RuntimeError, "Перенос не подтверждён"):
                runner.once(sort=False)
        self.assertEqual(sum(kind == "drag" for kind, _ in desktop.inputs), 1)
        self.assertTrue(runner._transfer_input_issued)


if __name__ == "__main__":
    unittest.main()
