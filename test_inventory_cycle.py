"""Inventory passes are tested without native input, timing waits, or game data."""
from collections import deque
import unittest

from core import CellState, GridScan, GridSpec, Rect
from inventory_cycle import run_inventory_cycle


def scan(*labels):
    grid = GridSpec(Rect(0, 0, 20 * len(labels), 20), 1, len(labels))
    cells = []
    for index, label in enumerate(labels):
        if label == "empty":
            empty, tier = True, None
        elif label == "unknown":
            empty, tier = False, None
        elif label == "ambiguous":
            empty, tier = None, None
        else:
            empty, tier = False, label
        cells.append(CellState(index, grid.cell_rect(index), tier, empty, 0, 20))
    return GridScan(grid, tuple(cells))


class CycleHarness:
    def __init__(self, steps):
        self.steps = deque(steps)
        self.sort_flags = []
        self.waits = []
        self.current = None
        self.stop = lambda: None

    def transfer(self, *, sort):
        self.sort_flags.append(sort)
        step = self.steps.popleft()
        if isinstance(step, BaseException):
            raise step
        moved, self.current = step
        return moved

    def run(self, **options):
        return run_inventory_cycle(
            transfer=self.transfer, inventory_snapshot=lambda: self.current,
            selected_tiers={3, 4, 5, 6}, check_stop=lambda: self.stop(),
            pause=self.waits.append, sort_available=True, **options)


class InventoryCycleTests(unittest.TestCase):
    def test_hidden_rows_are_exposed_by_repeated_sort_after_visible_drain(self):
        exhausted = scan(2, "empty")
        harness = CycleHarness([
            (True, scan(3, 2)), (True, exhausted), (False, exhausted),
            (True, scan(5, 2)), (True, exhausted), (False, exhausted),
            (False, exhausted), (False, exhausted),
        ])
        result = harness.run()
        self.assertTrue(result.complete)
        self.assertEqual(result.transfers, 4)  # More than the two visible cells.
        self.assertEqual(result.sorts, 4)
        self.assertEqual(harness.sort_flags,
                         [True, False, False, True, False, False, True, True])
        self.assertFalse(harness.steps)

    def test_identical_tiers_in_new_batches_do_not_trigger_stall(self):
        emptied = scan("empty")
        harness = CycleHarness([
            (True, emptied), (False, emptied),
            (True, emptied), (False, emptied),
            (True, emptied), (False, emptied),
            (False, emptied), (False, emptied),
        ])
        result = harness.run()
        self.assertEqual(result.transfers, 3)
        self.assertTrue(result.complete)

    def test_completion_uses_semantics_instead_of_cell_error_animation(self):
        first = scan(1, "empty")
        second = GridScan(first.grid, tuple(
            CellState(cell.index, cell.rect, cell.tier, cell.empty, 9, 7, "changed pixels")
            for cell in first.cells))
        harness = CycleHarness([(False, first), (False, second)])
        self.assertTrue(harness.run().complete)

    def test_unknown_occupied_cell_never_means_complete(self):
        observation = scan("unknown", "empty")
        harness = CycleHarness([(False, observation), (False, observation)])
        result = harness.run()
        self.assertEqual(result.outcome, "unknown")
        self.assertFalse(result.complete)
        self.assertEqual(harness.sort_flags, [True, True])

    def test_ambiguous_occupancy_never_means_complete(self):
        observation = scan("ambiguous", "empty")
        harness = CycleHarness([(False, observation), (False, observation)])
        self.assertEqual(harness.run().outcome, "unknown")

    def test_full_disabled_grid_cannot_prove_hidden_tiers_are_absent(self):
        observation = scan(1, 2)
        harness = CycleHarness([(False, observation), (False, observation)])
        result = harness.run()
        self.assertEqual(result.outcome, "hidden_unconfirmed")
        self.assertFalse(result.complete)

    def test_selected_item_without_transfer_is_blocking_not_success(self):
        harness = CycleHarness([(False, scan(3, "empty"))])
        with self.assertRaisesRegex(RuntimeError, "selected"):
            harness.run()
        self.assertEqual(harness.sort_flags, [True])

    def test_native_stop_takeover_and_unverified_transfer_errors_are_not_retried(self):
        for error in (InterruptedError("F8"), RuntimeError("unverified transfer"),
                      RuntimeError("storage full"), RuntimeError("focus lost")):
            with self.subTest(error=error):
                harness = CycleHarness([error, (False, scan("empty"))])
                with self.assertRaises(type(error)) as caught:
                    harness.run()
                self.assertIs(caught.exception, error)
                self.assertEqual(harness.sort_flags, [True])
                self.assertEqual(harness.waits, [])

    def test_stop_after_no_move_prevents_confirmation_sort(self):
        harness = CycleHarness([(False, scan("empty")), (False, scan("empty"))])
        checks = 0
        def stop_after_transfer():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise InterruptedError("Stop during readback")
        harness.stop = stop_after_transfer
        with self.assertRaises(InterruptedError):
            harness.run()
        self.assertEqual(harness.sort_flags, [True])
        self.assertEqual(len(harness.steps), 1)

    def test_continually_arriving_loot_stops_at_verified_transfer_limit(self):
        harness = CycleHarness([(True, scan("empty"))] * 4)
        result = harness.run(max_transfers=3)
        self.assertEqual(result.outcome, "bounded")
        self.assertEqual(result.transfers, 3)
        self.assertFalse(result.complete)
        self.assertEqual(len(harness.sort_flags), 3)

    def test_changing_no_progress_grid_is_bounded_by_sort_limit(self):
        harness = CycleHarness([(False, scan(1, "empty")), (False, scan(2, "empty")),
                                (False, scan(1, "empty"))])
        result = harness.run(max_sorts=2)
        self.assertEqual(result.outcome, "bounded")
        self.assertEqual(harness.sort_flags, [True, True])

    def test_cycle_duration_limit_sends_no_additional_input(self):
        times = iter((0, 0, 2))
        harness = CycleHarness([(True, scan("empty")), (True, scan("empty"))])
        result = harness.run(max_seconds=1, clock=lambda: next(times))
        self.assertEqual(result.outcome, "bounded")
        self.assertEqual(harness.sort_flags, [True])

    def test_missing_sort_never_claims_hidden_completeness(self):
        observation = scan(2, "empty")
        harness = CycleHarness([(False, observation)])
        result = run_inventory_cycle(
            transfer=harness.transfer, inventory_snapshot=lambda: harness.current,
            selected_tiers={3}, check_stop=lambda: None, pause=harness.waits.append,
            sort_available=False)
        self.assertEqual(result.outcome, "sort_unavailable")
        self.assertFalse(result.complete)
        self.assertEqual(harness.sort_flags, [False])

    def test_partial_inventory_observation_is_rejected(self):
        observation = scan("empty", "empty")
        malformed = GridScan(observation.grid, observation.cells[:-1])
        harness = CycleHarness([(False, malformed)])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            harness.run()

    def test_non_boolean_transfer_confirmation_is_rejected(self):
        harness = CycleHarness([(1, scan("empty"))])
        with self.assertRaisesRegex(ValueError, "confirmed"):
            harness.run()


if __name__ == "__main__":
    unittest.main()
