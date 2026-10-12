"""Storage fullness is a twice-confirmed capacity fact, independent of tiers."""
import unittest

from core import CellState, GridScan, GridSpec, Rect
from storage_cycle import (STORAGE_FULL_MESSAGE, StorageCapacityUnknown, StorageFull,
                           find_storage_capacity)


def observation(*occupancy):
    grid = GridSpec(Rect(0, 0, 20 * len(occupancy), 20), 1, len(occupancy))
    # Storage needs occupancy, not item-tier recognition: tier=None is valid
    # when an occupied cell clearly differs from every empty template.
    cells = tuple(CellState(index, grid.cell_rect(index), None, empty, 0, None)
                  for index, empty in enumerate(occupancy))
    return object(), object(), GridScan(grid, cells), object()


class StorageCycleTests(unittest.TestCase):
    def test_two_four_and_six_pages_are_freshly_inspected_in_two_complete_sweeps(self):
        for count in (2, 4, 6):
            with self.subTest(count=count):
                inspected, checks = [], []
                def inspect(page_id):
                    inspected.append(page_id)
                    return observation(False, False)
                with self.assertRaises(StorageFull) as caught:
                    find_storage_capacity(range(1, count + 1), inspect_page=inspect,
                                          check_stop=lambda: checks.append(len(inspected)))
                self.assertEqual(str(caught.exception), STORAGE_FULL_MESSAGE)
                self.assertEqual(inspected, list(range(1, count + 1)) * 2)
                self.assertGreaterEqual(len(checks), count * 2)

    def test_free_slot_in_first_sweep_returns_that_exact_observation_without_further_input(self):
        calls = []
        available = observation(False, True)
        def inspect(page_id):
            calls.append(page_id)
            return available if page_id == 3 else observation(False, False)
        result = find_storage_capacity(range(1, 7), inspect_page=inspect, check_stop=lambda: None)
        self.assertIs(result, available)
        self.assertEqual(calls, [1, 2, 3])

    def test_free_slot_revealed_in_second_sweep_does_not_raise_full(self):
        calls = []
        available = observation(True, False)
        def inspect(page_id):
            calls.append(page_id)
            return available if len(calls) == 6 else observation(False, False)
        result = find_storage_capacity((1, 2, 3, 4), inspect_page=inspect, check_stop=lambda: None)
        self.assertIs(result, available)
        self.assertEqual(calls, [1, 2, 3, 4, 1, 2])

    def test_unknown_occupancy_in_either_sweep_never_means_full(self):
        for unknown_at in (1, 4):
            with self.subTest(unknown_at=unknown_at):
                calls = []
                def inspect(page_id):
                    calls.append(page_id)
                    return observation(None, False) if len(calls) == unknown_at else observation(False, False)
                with self.assertRaises(StorageCapacityUnknown):
                    find_storage_capacity((1, 2), inspect_page=inspect, check_stop=lambda: None)
                self.assertEqual(calls, [1, 2, 1, 2])

    def test_unknown_cells_do_not_hide_an_independently_confirmed_empty_slot(self):
        available = observation(None, True)
        calls = []
        def inspect(page_id):
            calls.append(page_id)
            return available
        result = find_storage_capacity((1, 2), inspect_page=inspect, check_stop=lambda: None)
        self.assertIs(result, available)
        self.assertEqual(calls, [1])

    def test_partial_grid_cannot_establish_fullness(self):
        full = observation(False, False)
        incomplete = GridScan(full[2].grid, full[2].cells[:1])
        calls = []
        def inspect(page_id):
            calls.append(page_id)
            return full[0], full[1], incomplete, full[3]
        with self.assertRaises(StorageCapacityUnknown):
            find_storage_capacity((1, 2), inspect_page=inspect, check_stop=lambda: None)
        self.assertEqual(calls, [1])

    def test_stop_before_next_page_prevents_all_further_inspections(self):
        calls = []
        def check_stop():
            if calls:
                raise InterruptedError("F8")
        def inspect(page_id):
            calls.append(page_id)
            return observation(False, False)
        with self.assertRaisesRegex(InterruptedError, "F8"):
            find_storage_capacity((1, 2), inspect_page=inspect, check_stop=check_stop)
        self.assertEqual(calls, [1])

    def test_unverified_page_input_and_focus_errors_never_repeat_the_page(self):
        for error in (RuntimeError("page switch unverified"), RuntimeError("takeover"),
                      InterruptedError("Stop")):
            with self.subTest(error=error):
                calls = []
                def inspect(page_id):
                    calls.append(page_id)
                    if page_id == 2:
                        raise error
                    return observation(False, False)
                with self.assertRaises(type(error)) as caught:
                    find_storage_capacity((1, 2, 3, 4), inspect_page=inspect, check_stop=lambda: None)
                self.assertIs(caught.exception, error)
                self.assertEqual(calls, [1, 2])

    def test_invalid_or_purchase_like_page_identifiers_send_no_input(self):
        for ids in ((), (1, 1), (1, 3), (0, 1), (True, 2), ("1", "2"), range(1, 8)):
            with self.subTest(ids=ids):
                calls = []
                with self.assertRaises(ValueError):
                    find_storage_capacity(ids, inspect_page=calls.append, check_stop=lambda: None)
                self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
