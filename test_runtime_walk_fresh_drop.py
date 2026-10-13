"""A fresh passive drop invalidates only a positively observed empty decision.

Real Runner capture, tier classification, candidate selection and transfer
verification run against supplied RGB frames and a fake desktop. Endpoint
observations/scroller are explicit policy test doubles, not native proofs.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from input_activity import UserActivityInterrupted
from inventory_walk import InventoryEndpointProof, InventoryWalkObservation
from runtime import LayoutChangedDuringOperation, SafePreflightChange
from test_runtime import RuntimeTestFixture, cell, frame
from windows import FocusUnavailable, WindowGeometryChanged


class FreshDropWalkTests(RuntimeTestFixture, unittest.TestCase):
    def observation(self, runner, image):
        inventory, _ = runner.scans(image)
        geometry = "explicit synthetic stable viewport"
        top = InventoryEndpointProof("scrollbar", "top", geometry, 2)
        return InventoryWalkObservation(inventory, geometry, 0., 24., top_proof=top)

    def walk(self, runner, initial):
        self.scroller = SimpleNamespace(
            reset_to_top=Mock(return_value=self.observation(runner, initial)),
            snapshot=Mock(side_effect=lambda: self.observation(runner, runner._capture_valid_layout())),
            scroll_down=Mock(side_effect=AssertionError("no wheel after stale exhaustion or unverified move")),
            invalidate=Mock(), begin_cycle=Mock())
        with patch("inventory_scroller.InventoryScroller", return_value=self.scroller):
            return runner._walk_inventory()

    def test_new_selected_loot_after_real_candidate_free_once_is_safe_preflight_wait(self):
        before = frame([cell(2), cell()], [cell(), cell()])
        new_loot = frame([cell(2), cell(3)], [cell(), cell()])
        runner, desktop = self.runner([before, new_loot])
        runner.canonical_profile["automatic_layout"] = True
        runner.once = Mock(wraps=runner.once)

        with self.assertRaises(SafePreflightChange):
            self.walk(runner, before)

        runner.once.assert_called_once_with(sort=False)
        self.assertEqual([c.label for c in runner._candidate_free_scan.cells], ["T2", "empty"])
        self.assertFalse(runner._transfer_input_issued)
        self.assertEqual(desktop.inputs, [])
        self.scroller.snapshot.assert_called_once_with()
        self.scroller.scroll_down.assert_not_called()
        self.scroller.reset_to_top.assert_called_once_with()

    def test_false_transfer_without_positive_candidate_free_scan_remains_terminal(self):
        before = frame([cell(2), cell()], [cell(), cell()])
        fresh = frame([cell(2), cell(3)], [cell(), cell()])
        runner, desktop = self.runner([fresh])
        runner.once = Mock(return_value=False)

        with self.assertRaisesRegex(RuntimeError, "selected visible item remains") as raised:
            self.walk(runner, before)

        self.assertNotIsInstance(raised.exception, SafePreflightChange)
        runner.once.assert_called_once_with(sort=False)
        self.assertEqual(desktop.inputs, [])
        self.scroller.scroll_down.assert_not_called()

    def test_real_unverified_transfer_still_has_one_drag_without_snapshot_or_replay(self):
        selected = frame([cell(3), cell()], [cell(), cell()])
        runner, desktop = self.runner([selected, selected.copy(), selected.copy()])
        runner.once = Mock(wraps=runner.once)

        with self.assertRaisesRegex(RuntimeError, "Перенос не подтверждён") as raised:
            self.walk(runner, selected)

        self.assertNotIsInstance(raised.exception, SafePreflightChange)
        runner.once.assert_called_once_with(sort=False)
        self.assertEqual(sum(kind == "drag" for kind, _ in desktop.inputs), 1)
        self.assertTrue(runner._transfer_input_issued)
        self.assertIsNone(runner._candidate_free_scan)
        self.scroller.snapshot.assert_not_called()
        self.scroller.scroll_down.assert_not_called()
        self.scroller.reset_to_top.assert_called_once_with()

    def test_fresh_native_or_activity_guard_failure_cannot_become_passive_drop_wait(self):
        before = frame([cell(2), cell()], [cell(), cell()])
        errors = (WindowGeometryChanged("native target changed", input_issued=False),
                  FocusUnavailable(), UserActivityInterrupted("new real input"), InterruptedError("F8"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                runner, desktop = self.runner([before])
                capture = desktop.capture
                count = 0
                def capture_then_guard(handle):
                    nonlocal count
                    count += 1
                    if count == 2:
                        raise error
                    return capture(handle)
                desktop.capture = capture_then_guard
                runner.once = Mock(wraps=runner.once)

                with self.assertRaises(type(error)) as raised:
                    self.walk(runner, before)

                self.assertIs(raised.exception, error)
                self.assertNotIsInstance(raised.exception, SafePreflightChange)
                runner.once.assert_called_once_with(sort=False)
                self.assertEqual(desktop.inputs, [])
                self.scroller.scroll_down.assert_not_called()

    def test_fresh_wrong_frame_size_blocks_without_passive_drop_recovery(self):
        before = frame([cell(2), cell()], [cell(), cell()])
        resized = frame([cell(2), cell(3)], [cell(), cell()]).resize((97, 64))
        runner, desktop = self.runner([before, resized])
        runner.canonical_profile["automatic_layout"] = True
        runner.once = Mock(wraps=runner.once)

        with self.assertRaises(LayoutChangedDuringOperation) as raised:
            self.walk(runner, before)

        self.assertNotIsInstance(raised.exception, SafePreflightChange)
        runner.once.assert_called_once_with(sort=False)
        self.assertEqual(desktop.inputs, [])
        self.scroller.scroll_down.assert_not_called()


if __name__ == "__main__":
    unittest.main()
