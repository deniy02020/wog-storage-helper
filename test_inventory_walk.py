"""Walk policy with a fake viewport; never native wheel/capture/input."""
from dataclasses import replace
from itertools import combinations
import unittest

from core import CellState, GridScan, GridSpec, Rect
from input_activity import UserActivityInterrupted
from inventory_walk import (InventoryEndpointProof, InventoryWalkObservation,
                            certify_full_inventory_top, certify_sorted_prefix,
                            run_inventory_walk)
from windows import WindowGeometryChanged


def scan(labels, *, grid=None):
    grid = grid or GridSpec(Rect(100, 200, 140, 360), 8, 2)
    cells = []
    for index, tier in enumerate(labels):
        empty = True if tier == "empty" else None if tier == "ambiguous" else False
        number = None if tier in ("empty", "unknown", "ambiguous") else tier
        cells.append(CellState(index, grid.cell_rect(index), number, empty, 0, 20))
    return GridScan(grid, tuple(cells))


class WalkHarness:
    def __init__(self, content, selected=(3, 4), *, step_rows=3, compact=False):
        self.content = list(content)
        self.selected = set(selected)
        self.step = step_rows * 2
        self.compact = compact
        self.first = 0
        self.events, self.transferred, self.waits = [], [], []
        self.geometry = "HWND+PID+client+two validated headers"
        self.prove_top = self.prove_bottom = True
        self.force_no_motion = False
        self.before_reset = lambda: None
        self.before_transfer = lambda: None
        self.before_scroll = lambda: None
        self.before_snapshot = lambda: None
        self.check_stop = lambda: None
        self.scroll_override = None
        self.transfer_override = None

    def observation(self):
        labels = self.content[self.first:self.first + 16]
        labels += ["empty"] * (16 - len(labels))
        top = (InventoryEndpointProof("scrollbar", "top", self.geometry, 2)
               if self.first == 0 and self.prove_top else None)
        bottom = (InventoryEndpointProof("scrollbar", "bottom", self.geometry, 2)
                  if self.first >= max(0, len(self.content) - 16) and self.prove_bottom else None)
        return InventoryWalkObservation(scan(labels), self.geometry, self.first / 2 * 20,
                                        20, top, bottom)

    def reset(self):
        self.before_reset()
        self.events.append("reset")
        self.first = 0
        return self.observation()

    def snapshot(self):
        self.before_snapshot()
        self.events.append("snapshot")
        return self.observation()

    def transfer(self, *, sort):
        self.before_transfer()
        self.events.append(("transfer", sort))
        if self.transfer_override is not None:
            return self.transfer_override
        for index in range(self.first, min(self.first + 16, len(self.content))):
            tier = self.content[index]
            if tier in self.selected:
                self.transferred.append(tier)
                if self.compact:
                    self.content.pop(index)
                    self.content.append("empty")
                else:
                    self.content[index] = "empty"
                return True
        return False

    def scroll(self, before):
        self.before_scroll()
        self.events.append("scroll")
        if self.scroll_override is not None:
            return self.scroll_override(before)
        if not self.force_no_motion:
            self.first = min(self.first + self.step, max(0, len(self.content) - 16))
        return self.observation()

    def run(self, **kwargs):
        return run_inventory_walk(reset_to_top=self.reset,
            inventory_snapshot=self.snapshot, transfer=self.transfer,
            scroll_down=self.scroll, selected_tiers=self.selected,
            check_stop=lambda: self.check_stop(), pause=self.waits.append, **kwargs)


class InventoryWalkTests(unittest.TestCase):
    def test_full_excluded_t6_prefix_does_not_hide_lower_selected_t3_t4(self):
        # More than the eight visible rows; successive full-T6 semantic scans
        # are identical. Actual proved position, not labels, advances the walk.
        content = [6] * 32 + [3, 6, 4, 2, 1] + [6] * 25
        harness = WalkHarness(content)
        result = harness.run()
        self.assertTrue(result.complete)
        self.assertEqual(harness.transferred, [3, 4])
        self.assertEqual(result.transfers, 2)
        self.assertGreater(result.scrolls, 8)
        self.assertEqual(harness.first, 0)
        self.assertTrue(all(event[1] is False for event in harness.events if isinstance(event, tuple)))
        self.assertEqual(harness.content.count(1), 1)
        self.assertEqual(harness.content.count(2), 1)
        self.assertEqual(harness.content.count(6), content.count(6))

    def test_identical_clone_rows_continue_until_positive_bottom_proof(self):
        harness = WalkHarness([6] * 80)
        result = harness.run()
        self.assertTrue(result.complete)
        self.assertEqual(result.transfers, 0)
        self.assertEqual(result.scrolls, 11)
        self.assertEqual(result.resets, 2)
        self.assertEqual(harness.first, 0)

    def test_no_motion_and_identical_items_without_endpoint_never_complete(self):
        harness = WalkHarness([6] * 40)
        harness.force_no_motion = True
        result = harness.run()
        self.assertEqual(result.outcome, "endpoint_unconfirmed")
        self.assertFalse(result.complete)
        self.assertEqual(result.scrolls, 1)
        self.assertEqual(result.resets, 1)

    def test_unconfirmed_initial_top_sends_no_transfer_or_scroll(self):
        harness = WalkHarness([6] * 20)
        harness.prove_top = False
        self.assertEqual(harness.run().outcome, "top_unconfirmed")
        self.assertEqual(harness.events, ["reset"])

    def test_unknown_or_ambiguous_cell_never_means_finished(self):
        for label in ("unknown", "ambiguous"):
            with self.subTest(label=label):
                harness = WalkHarness([label] + [6] * 15)
                result = harness.run()
                self.assertEqual(result.outcome, "unknown")
                self.assertFalse(result.complete)
                self.assertNotIn("scroll", harness.events)
                self.assertEqual(result.resets, 2)

    def test_unknown_prefix_does_not_hide_later_known_selected_items(self):
        harness = WalkHarness(["unknown"] + [6] * 24 + [3, 4] + [6] * 20)
        result = harness.run()
        self.assertEqual(harness.transferred, [3, 4])
        self.assertEqual(result.transfers, 2)
        self.assertGreater(result.scrolls, 5)
        self.assertEqual(result.outcome, "unknown")
        self.assertFalse(result.complete)
        self.assertEqual(harness.content[0], "unknown")
        self.assertEqual(harness.first, 0)

    def test_ambiguous_occupancy_is_not_transferred_and_does_not_block_later_selected(self):
        harness = WalkHarness(["ambiguous"] + [6] * 24 + [3] + [6] * 20)
        result = harness.run()
        self.assertEqual(harness.transferred, [3])
        self.assertEqual(harness.content[0], "ambiguous")
        self.assertEqual(result.outcome, "unknown")

    def test_verified_progress_starts_a_new_pass_without_stale_unknown_evidence(self):
        harness = WalkHarness(["unknown"] + [6] * 24 + [3] + [6] * 20)
        original = harness.transfer
        def transfer(*, sort):
            moved = original(sort=sort)
            if moved:
                harness.content[0] = 6  # New fresh top after verified progress.
            return moved
        harness.transfer = transfer
        result = harness.run()
        self.assertEqual(harness.transferred, [3])
        self.assertTrue(result.complete)
        self.assertGreater(result.scrolls, 5)

    def test_unresolved_unknown_during_middle_pass_prevents_complete_even_if_top_known(self):
        harness = WalkHarness([6] * 20 + ["unknown"] + [6] * 30)
        result = harness.run()
        self.assertGreater(result.scrolls, 0)
        self.assertEqual(harness.transferred, [])
        self.assertEqual(result.outcome, "unknown")
        self.assertFalse(result.complete)
        self.assertEqual(harness.first, 0)

    def test_selected_visible_item_without_transfer_is_an_error(self):
        harness = WalkHarness([3] + [6] * 15)
        harness.transfer_override = False
        with self.assertRaisesRegex(RuntimeError, "selected"):
            harness.run()
        self.assertNotIn("scroll", harness.events)

    def test_verified_transfer_restarts_top_to_recheck_compacted_prefix(self):
        harness = WalkHarness([6] * 18 + [3, 4, 3] + [6] * 17, compact=True)
        result = harness.run()
        self.assertTrue(result.complete)
        self.assertEqual(harness.transferred, [3, 4, 3])
        self.assertEqual(result.resets, result.transfers + 2)
        self.assertEqual(harness.content.count(3), 0)
        self.assertEqual(harness.content.count(4), 0)

    def test_new_selected_drop_on_final_reset_restarts_walk(self):
        harness = WalkHarness([6] * 16)
        resets = 0
        def drop():
            nonlocal resets
            resets += 1
            if resets == 2:
                harness.content[0] = 3
        harness.before_reset = drop
        result = harness.run()
        self.assertTrue(result.complete)
        self.assertEqual(harness.transferred, [3])
        self.assertEqual(result.resets, 4)

    def test_stop_takeover_stale_geometry_unverified_transfer_are_never_retried(self):
        errors = (InterruptedError("Stop"), UserActivityInterrupted("real input"),
                  WindowGeometryChanged("client changed", input_issued=True),
                  RuntimeError("Transfer was issued but not verified"))
        for error in errors:
            for callback in ("before_transfer", "before_scroll"):
                with self.subTest(error=type(error).__name__, callback=callback):
                    harness = WalkHarness([6] * 40)
                    def fail():
                        raise error
                    setattr(harness, callback, fail)
                    with self.assertRaises(type(error)) as caught:
                        harness.run()
                    self.assertIs(caught.exception, error)
                    self.assertEqual(harness.events.count("reset"), 1)

    def test_position_or_layout_changes_during_fresh_scan_abort_before_wheel(self):
        for change in ("geometry", "position"):
            harness = WalkHarness([6] * 40)
            def alter():
                if change == "geometry": harness.geometry = "another HWND/client"
                else: harness.first = 2
            harness.before_snapshot = alter
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                harness.run()
            self.assertNotIn("scroll", harness.events)

    def test_big_forward_jump_or_backwards_motion_cannot_skip_uninspected_rows(self):
        for offset in (160, -20):
            harness = WalkHarness([6] * 40)
            harness.scroll_override = lambda before: replace(before, offset_pixels=offset)
            with self.subTest(offset=offset), self.assertRaises((RuntimeError, ValueError)):
                harness.run()
            self.assertEqual(harness.events.count("scroll"), 1)

    def test_clone_alias_endpoint_and_unconfirmed_clamp_are_rejected(self):
        for kind, confirmations, step in (("same_pixels", 2, None),
                                          ("calibrated_subrow_clamp", 1, 5),
                                          ("calibrated_subrow_clamp", 2, 20),
                                          ("calibrated_subrow_clamp", 2, 40),
                                          ("calibrated_subrow_clamp", 2, 0)):
            harness = WalkHarness([6] * 16)
            normal = harness.observation()
            bad = InventoryEndpointProof(kind, "bottom", harness.geometry, confirmations, step)
            harness.snapshot = lambda: replace(normal, bottom_proof=bad)
            with self.subTest(kind=kind, confirmations=confirmations, step=step), self.assertRaises(ValueError):
                harness.run()
            self.assertEqual(harness.events.count("reset"), 1)

    def test_bounded_calibrated_subrow_clamp_is_explicit_endpoint_evidence(self):
        harness = WalkHarness([6] * 16)
        normal = harness.observation()
        proof = InventoryEndpointProof("calibrated_subrow_clamp", "bottom",
                                       harness.geometry, 2, 5)
        harness.snapshot = lambda: replace(normal, bottom_proof=proof)
        result = harness.run()
        self.assertTrue(result.complete)
        self.assertEqual(result.scrolls, 0)
        self.assertEqual(result.resets, 2)

    def test_continually_arriving_items_and_viewports_are_bounded(self):
        harness = WalkHarness([6] * 1000)
        result = harness.run(max_scrolls=12)
        self.assertEqual(result.outcome, "bounded")
        self.assertEqual(result.scrolls, 12)  # More than the visible row count.
        self.assertFalse(result.complete)
        harness = WalkHarness([3] * 100)
        result = harness.run(max_transfers=3)
        self.assertEqual(result.outcome, "bounded")
        self.assertEqual(result.transfers, 3)
        self.assertEqual(harness.transferred, [3, 3, 3])

    def test_duration_and_reset_limits_send_no_more_inputs(self):
        harness = WalkHarness([6] * 40)
        ticks = iter((0, 0, 2))
        result = harness.run(max_seconds=1, clock=lambda: next(ticks))
        self.assertEqual(result.outcome, "bounded")
        self.assertEqual(harness.events, ["reset"])
        harness = WalkHarness([6] * 16)
        result = harness.run(max_resets=1)
        self.assertEqual(result.outcome, "bounded")
        self.assertEqual(result.resets, 1)

    def test_non_boolean_transfer_result_and_partial_grid_reject(self):
        harness = WalkHarness([6] * 16)
        harness.transfer_override = 1
        with self.assertRaises(ValueError):
            harness.run()
        harness = WalkHarness([6] * 16)
        observation = harness.observation()
        partial = GridScan(observation.scan.grid, observation.scan.cells[:-1])
        harness.snapshot = lambda: replace(observation, scan=partial)
        with self.assertRaises(ValueError):
            harness.run()


class SortedPrefixTests(unittest.TestCase):
    def certificate(self, labels, selected=(3, 5, 6), **kwargs):
        return certify_sorted_prefix(scan(labels), selected, **kwargs)

    def test_empty_suffix_and_strictly_lower_known_tier_are_boundaries(self):
        self.assertTrue(self.certificate([4] * 10 + [2] * 6))
        self.assertTrue(self.certificate([4] * 10 + ["empty"] * 6))
        self.assertTrue(self.certificate(["empty"] * 16))
        self.assertFalse(self.certificate([6] * 16, selected=(5,)))

    def test_t4_does_not_hide_selected_t3_when_only_higher_tiers_are_visible(self):
        for selected in ((3,), (3, 5, 6)):
            self.assertFalse(self.certificate([4] * 16, selected=selected))
            self.assertTrue(self.certificate([4] * 16, selected=selected,
                                             require_boundary=False))
        self.assertTrue(self.certificate([4] * 16, selected=(5, 6)))

    def test_validation_mode_still_requires_known_descending_compacted_no_selected_prefix(self):
        for labels in ([4] * 14 + [5, 4], [4, "empty", 4] + ["empty"] * 13,
                       [4] * 15 + [3], [4] * 15 + ["unknown"],
                       [4] * 15 + ["ambiguous"], [4] * 15 + [0],
                       [4] * 15 + [7], [4] * 15 + [True]):
            for require_boundary in (True, False):
                with self.subTest(labels=labels, boundary=require_boundary):
                    self.assertFalse(self.certificate(labels, require_boundary=require_boundary))

    def test_empty_hole_is_never_a_compacted_suffix(self):
        self.assertFalse(self.certificate([4, "empty", 2] + ["empty"] * 13))

    def test_selected_lower_tier_cannot_be_skipped_even_with_a_later_sentinel(self):
        self.assertFalse(self.certificate([4] * 8 + [3] * 4 + [2] * 4))

    def test_partial_geometry_malformed_cell_and_invalid_selection_fail_closed(self):
        normal = scan([2] * 16)
        for bad in (GridScan(normal.grid, normal.cells[:-1]),
                    GridScan(normal.grid, (replace(normal.cells[0], index=1),) + normal.cells[1:]),
                    GridScan(normal.grid, (replace(normal.cells[0], empty=True),) + normal.cells[1:]),
                    GridScan(normal.grid, (replace(normal.cells[0], rect=Rect(0, 0, 1, 1)),)
                             + normal.cells[1:]), None):
            with self.subTest(scan=bad):
                self.assertFalse(certify_sorted_prefix(bad, (3,)))
        for selected in ((), (True,), (1, True), (0,), (7,), (3.0,), ("3",), None):
            with self.subTest(selected=selected):
                self.assertFalse(certify_sorted_prefix(normal, selected))
        self.assertFalse(certify_sorted_prefix(normal, (3,), require_boundary=1))


class InventoryWalkSortedTopRecoveryTests(unittest.TestCase):
    """An independently complete sorted Top can replace an older uncertain view."""

    @staticmethod
    def full_top(observation):
        return certify_full_inventory_top(observation, Rect(100, 200, 140, 360), 8, 2, 1.)

    @staticmethod
    def recovering_harness(fresh_labels, *, selected=(3, 4, 5, 6), epoch=2):
        harness = WalkHarness(["unknown"] + [2] * 15, selected=selected)
        generation = [1]
        def fresh_sort():
            harness.content[:] = fresh_labels
            generation[0] = epoch
        harness.before_snapshot = fresh_sort
        return harness, lambda: generation[0]

    def test_verified_forty_move_batch_then_fresh_sorted_full_top_skips_lower_tail(self):
        # Reproduce the installed cycle: all 40 visible moves were independently
        # verified by the batch adapter, but its post-transfer read had unknown
        # occupancy before the next guarded Sort compacted T2/T1 into full Top.
        # Callbacks here are policy doubles; no native input is performed.
        grid = GridSpec(Rect(890, 588, 1215, 1108), 8, 5)
        geometry = "guarded native-shaped Top"
        proof = InventoryEndpointProof("scrollbar", "top", geometry, 2)
        def top(labels):
            return InventoryWalkObservation(scan(labels, grid=grid), geometry, 0, 65, proof)
        initial = top([4] + [3] * 39)
        stale = top(["ambiguous"] + ["empty"] * 39)
        compacted = top([2] * 26 + [1] * 14)
        current, epoch, resets, batches = [initial], [1], [0], [0]
        top_checks, boundary_checks = [], []
        def reset():
            resets[0] += 1
            current[0] = initial if resets[0] == 1 else stale if resets[0] == 2 else compacted
            return current[0]
        def batch(*, sort, max_transfers):
            self.assertIs(sort, False)
            batches[0] += 1
            if batches[0] == 1:
                self.assertEqual(max_transfers, 512)
                return 40
            self.assertEqual(max_transfers, 472)
            current[0], epoch[0] = compacted, 2
            return 0
        def full_top(view):
            top_checks.append(view)
            return certify_full_inventory_top(view, grid.rect, 8, 5, 1.)
        def boundary(view):
            boundary_checks.append(view)
            self.assertEqual(epoch[0], 2)
            return certify_sorted_prefix(view.scan, (3, 4, 5, 6))
        def forbidden_scroll(view):
            self.fail("Fresh full sorted T2/T1 Top must not scroll its lower/empty tail")
        result = run_inventory_walk(reset_to_top=reset, inventory_snapshot=lambda: current[0],
            transfer_batch=batch, sorted_boundary=boundary, sorted_epoch=lambda: epoch[0],
            sorted_top=full_top, scroll_down=forbidden_scroll, selected_tiers=(3, 4, 5, 6),
            check_stop=lambda: None, pause=lambda _: None)
        self.assertEqual((result.outcome, result.transfers, result.scrolls, result.resets),
                         ("complete", 40, 0, 2))
        self.assertEqual(boundary_checks, [compacted])
        self.assertEqual(top_checks, [compacted, compacted])

    def test_current_complete_known_sorted_top_can_resolve_older_unknown_in_same_epoch(self):
        harness, epoch = self.recovering_harness([2] * 10 + [1] * 6 + ["empty"] * 100,
                                                 epoch=1)
        harness.prove_bottom = False
        result = harness.run(sorted_epoch=epoch, sorted_top=self.full_top,
            sorted_boundary=lambda view: certify_sorted_prefix(view.scan, harness.selected))
        self.assertEqual((result.outcome, result.scrolls, result.resets), ("complete", 0, 1))

    def test_missing_full_top_or_positive_sort_epoch_keeps_historical_unknown(self):
        for gate in ("missing_top", "partial_top", "missing_epoch", "zero_epoch"):
            harness, epoch = self.recovering_harness([2] * 16, epoch=0 if gate == "zero_epoch" else 2)
            def forbidden(view):
                self.fail("Incomplete Top/Sort evidence cannot replace historical uncertainty")
            kwargs = dict(sorted_boundary=forbidden)
            if gate != "missing_epoch":
                kwargs["sorted_epoch"] = epoch
            if gate != "missing_top":
                kwargs["sorted_top"] = (lambda view: False) if gate == "partial_top" else self.full_top
            # A zero epoch cannot move backwards from the initial generation.
            if gate == "zero_epoch":
                kwargs["sorted_epoch"] = lambda: 0
            with self.subTest(gate=gate):
                self.assertEqual(harness.run(**kwargs).outcome, "unknown")
                self.assertNotIn("scroll", harness.events)

    def test_current_unknown_bad_order_hole_or_higher_only_never_replaces_unknown(self):
        for labels in (["unknown"] + [2] * 15, [2, 4] + [2] * 14,
                       [2, "empty", 2] + ["empty"] * 13, [4] * 16):
            harness, epoch = self.recovering_harness(labels, selected=(3, 5, 6))
            def forbidden(view):
                self.fail("Current prefix still lacks a known compact lower boundary")
            with self.subTest(labels=labels):
                self.assertEqual(harness.run(sorted_epoch=epoch, sorted_top=forbidden,
                                            sorted_boundary=forbidden).outcome, "unknown")

    def test_tier_selection_gap_still_walks_past_t4_and_transfers_hidden_t3(self):
        harness = WalkHarness(["unknown"] + [4] * 23 + [3] + [2] * 15 + ["empty"] * 30,
                              selected=(3, 5, 6), compact=True)
        epoch = [1]
        def fresh_sort():
            harness.content[0] = 4
            epoch[0] = 2
        harness.before_snapshot = fresh_sort
        result = harness.run(sorted_epoch=lambda: epoch[0], sorted_top=self.full_top,
            sorted_boundary=lambda view: certify_sorted_prefix(view.scan, harness.selected))
        self.assertTrue(result.complete)
        self.assertEqual(harness.transferred, [3])
        self.assertGreater(result.scrolls, 0)

    def test_mixed_selected_batches_walk_t4_gap_then_stop_at_t2_without_lower_tail(self):
        harness = WalkHarness([6, 6, 5, 5] + [4] * 32 + [3, 3] + [2] * 16
                              + [1] * 20 + ["empty"] * 100,
                              selected=(3, 5, 6), compact=True)
        epoch, certified_moves, boundaries, batch_calls = [1], [0], [], []
        def sorted_reset():
            if len(harness.transferred) > certified_moves[0]:
                certified_moves[0] = len(harness.transferred)
                epoch[0] += 1  # Guarded Sort of the remaining compacted fake bag.
        harness.before_reset = sorted_reset
        def boundary(view):
            accepted = certify_sorted_prefix(view.scan, harness.selected)
            boundaries.append((len(harness.transferred), view, accepted))
            return accepted
        result = harness.run(sorted_epoch=lambda: epoch[0], sorted_top=self.full_top,
            sorted_boundary=boundary,
            transfer_batch=InventoryWalkPerformanceTests.draining_batch(harness, batch_calls))
        self.assertEqual((result.outcome, result.transfers), ("complete", 6))
        self.assertEqual(harness.transferred, [6, 6, 5, 5, 3, 3])
        self.assertGreater(result.scrolls, 0)
        self.assertTrue(any(moved == 4 and not accepted and
                            all(cell.tier == 4 for cell in view.scan.cells)
                            for moved, view, accepted in boundaries))
        moved, last_view, accepted = boundaries[-1]
        self.assertEqual(moved, 6)
        self.assertTrue(accepted)
        self.assertGreater(last_view.offset_pixels, 0)
        self.assertTrue(any(cell.tier == 2 for cell in last_view.scan.cells))
        self.assertFalse(any(cell.tier == 1 or cell.empty for cell in last_view.scan.cells))
        self.assertEqual(harness.events[-1], "reset")
        self.assertEqual(harness.first, 0)

    def test_all_fifteen_tier_subsets_drain_selected_items_without_scrolling_empty_tail(self):
        content = sum(([tier] * 20 for tier in (6, 5, 4, 3)), [])
        content += [2] * 16 + [1] * 16 + ["empty"] * 100
        subsets = [tiers for count in range(1, 5) for tiers in combinations((3, 4, 5, 6), count)]
        self.assertEqual(len(subsets), 15)
        for selected in subsets:
            with self.subTest(selected=selected):
                harness = WalkHarness(content, selected=selected, compact=True)
                harness.prove_bottom = False
                epoch, compacted_moves, accepted, batches = [1], [0], [], []
                def fresh_sort():
                    if len(harness.transferred) > compacted_moves[0]:
                        compacted_moves[0] = len(harness.transferred)
                        epoch[0] += 1
                harness.before_reset = fresh_sort
                def boundary(view):
                    certified = certify_sorted_prefix(view.scan, selected)
                    if certified:
                        accepted.append((len(harness.events), view))
                    return certified
                result = harness.run(sorted_epoch=lambda: epoch[0], sorted_top=self.full_top,
                    sorted_boundary=boundary,
                    transfer_batch=InventoryWalkPerformanceTests.draining_batch(harness, batches))
                # Expected moves are the selected original items, in game order;
                # the oracle does not use a completion/prefix implementation.
                expected = [tier for tier in content if tier in selected]
                self.assertEqual(harness.transferred, expected)
                self.assertEqual((result.outcome, result.transfers), ("complete", 20 * len(selected)))
                for tier in (1, 2, 3, 4, 5, 6):
                    self.assertEqual(harness.content.count(tier),
                                     0 if tier in selected else content.count(tier))
                self.assertEqual(len(accepted), 1)
                event_index, view = accepted[0]
                self.assertTrue(any(cell.tier < min(selected) for cell in view.scan.cells))
                self.assertTrue(all(cell.empty is False for cell in view.scan.cells))
                self.assertEqual(harness.events[event_index:],
                                 ["snapshot"] if view.top_proof is not None else ["reset"])
                self.assertEqual(harness.first, 0)

    def test_scrolled_known_boundary_cannot_replace_unknown_from_earlier_view(self):
        harness = WalkHarness(["unknown"] + [6] * 19 + [2] * 20, selected=(3,))
        def recognize_after_scroll():
            harness.content[0] = 6
        harness.before_scroll = recognize_after_scroll
        def forbidden(view):
            self.fail("A scrolled prefix cannot certify the complete beginning")
        result = harness.run(sorted_epoch=lambda: 1, sorted_top=forbidden,
                             sorted_boundary=forbidden)
        self.assertEqual(result.outcome, "unknown")
        self.assertGreater(result.scrolls, 0)

    def test_complete_top_alone_cannot_replace_the_fresh_sort_boundary_callback(self):
        harness, epoch = self.recovering_harness([2] * 16)
        calls = []
        def no_fresh_sort(view):
            calls.append(view)
            return False
        result = harness.run(sorted_epoch=epoch, sorted_top=self.full_top,
                             sorted_boundary=no_fresh_sort)
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(len(calls), 1)

    def test_recovery_current_full_top_callback_retains_stop_type_and_time_guards(self):
        for value in (1, None, "complete"):
            harness, epoch = self.recovering_harness([2] * 16)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                harness.run(sorted_epoch=epoch, sorted_top=lambda view: value,
                            sorted_boundary=lambda view: True)
            self.assertEqual(harness.events.count("reset"), 1)
            self.assertNotIn("scroll", harness.events)
        for error in (InterruptedError("Stop"), UserActivityInterrupted("takeover"),
                      WindowGeometryChanged("layout changed", input_issued=True)):
            harness, epoch = self.recovering_harness([2] * 16)
            def fail(view):
                raise error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)) as caught:
                harness.run(sorted_epoch=epoch, sorted_top=fail,
                            sorted_boundary=lambda view: True)
            self.assertIs(caught.exception, error)
            self.assertEqual(harness.events.count("reset"), 1)
            self.assertNotIn("scroll", harness.events)
        harness, epoch = self.recovering_harness([2] * 16)
        now = [0.]
        def expire(view):
            now[0] = 2.
            return True
        def forbidden(view):
            self.fail("Timeout must prevent the Sort callback and final reset")
        result = harness.run(sorted_epoch=epoch, sorted_top=expire, sorted_boundary=forbidden,
                             max_seconds=1., clock=lambda: now[0])
        self.assertEqual((result.outcome, result.resets, result.scrolls), ("bounded", 1, 0))

    def test_recovery_still_requires_independent_final_known_positive_top(self):
        for changed in ("unknown", "top_unconfirmed"):
            harness, epoch = self.recovering_harness([2] * 16)
            snapshots = [0]
            initial_snapshot = harness.before_snapshot
            def change_final():
                snapshots[0] += 1
                if snapshots[0] == 1:
                    initial_snapshot()
                else:
                    if changed == "unknown":
                        harness.content[0] = "unknown"
                    else:
                        harness.prove_top = False
            harness.before_snapshot = change_final
            with self.subTest(changed=changed):
                result = harness.run(sorted_epoch=epoch, sorted_top=self.full_top,
                    sorted_boundary=lambda view: certify_sorted_prefix(view.scan, harness.selected))
                self.assertEqual((result.outcome, result.scrolls, result.resets), (changed, 0, 1))


class FinalSortedTopReadbackTests(unittest.TestCase):
    """Final readback may capture again, but never repeat reset/wheel/Sort input."""

    @staticmethod
    def setup_reads(labels):
        harness = WalkHarness([2] * 16, selected=(3,))
        epoch, calls = [1], []
        known = harness.observation()
        frames = [replace(known, scan=scan(values)) for values in labels]
        def snapshot():
            harness.events.append("snapshot")
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                return known  # Fresh zero-transfer certificate observation.
            index = len(calls) - 2
            return frames[index] if index < len(frames) else harness.observation()
        harness.snapshot = snapshot
        return harness, epoch, calls

    @staticmethod
    def run_walk(harness, epoch, **kwargs):
        return harness.run(sorted_epoch=lambda: epoch[0],
            sorted_top=InventoryWalkSortedTopRecoveryTests.full_top,
            sorted_boundary=lambda view: certify_sorted_prefix(view.scan, harness.selected), **kwargs)

    def test_transient_unknown_then_fresh_known_top_completes_without_extra_reset_or_down(self):
        harness, epoch, calls = self.setup_reads([["unknown"] + [2] * 15, [2] * 16])
        result = self.run_walk(harness, epoch)
        self.assertEqual((result.outcome, result.transfers, result.scrolls, result.resets),
                         ("complete", 0, 0, 1))
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(harness.waits, [.12])
        self.assertEqual(harness.events,
                         ["reset", ("transfer", False), "snapshot", "snapshot", "snapshot"])

    def test_persistent_unknown_uses_only_three_fresh_final_observations_never_old_known(self):
        unknown = ["ambiguous"] + [2] * 15
        harness, epoch, calls = self.setup_reads([unknown, unknown, unknown])
        result = self.run_walk(harness, epoch)
        self.assertEqual((result.outcome, result.scrolls, result.resets), ("unknown", 0, 1))
        self.assertEqual(calls, [1, 2, 3, 4])  # One certificate +three final captures.
        self.assertEqual(harness.waits, [.12, .12])
        self.assertEqual(harness.events.count("reset"), 1)

    def test_retry_revalidates_geometry_pitch_position_absolute_and_complete_top(self):
        for change in ("geometry", "pitch", "position", "missing_top", "partial_top"):
            harness, epoch, calls = self.setup_reads([["unknown"] + [2] * 15, [2] * 16])
            ordinary = harness.snapshot
            def changed_read():
                view = ordinary()
                if len(calls) != 3:
                    return view
                if change == "geometry":
                    return replace(view, geometry_identity="another guarded window")
                if change == "pitch":
                    return replace(view, row_pitch_pixels=21.)
                if change == "position":
                    return replace(view, offset_pixels=20.)
                if change == "missing_top":
                    return replace(view, top_proof=None)
                partial = GridSpec(Rect(100, 220, 140, 360), 7, 2)
                return replace(view, scan=scan([2] * 14, grid=partial))
            harness.snapshot = changed_read
            with self.subTest(change=change):
                if change in ("geometry", "pitch", "position"):
                    with self.assertRaises((RuntimeError, ValueError)):
                        self.run_walk(harness, epoch)
                else:
                    self.assertEqual(self.run_walk(harness, epoch).outcome, "top_unconfirmed")
                self.assertEqual(calls, [1, 2, 3])
                self.assertEqual(harness.events.count("reset"), 1)
                self.assertNotIn("scroll", harness.events)

    def test_retry_cannot_accept_a_changed_or_backward_sort_generation(self):
        for changed in (0, 2):
            harness, epoch, calls = self.setup_reads([["unknown"] + [2] * 15, [2] * 16])
            ordinary = harness.snapshot
            def changed_epoch():
                view = ordinary()
                if len(calls) == 3:
                    epoch[0] = changed
                return view
            harness.snapshot = changed_epoch
            with self.subTest(epoch=changed), self.assertRaises((RuntimeError, ValueError)):
                self.run_walk(harness, epoch)
            self.assertEqual(calls, [1, 2, 3])
            self.assertNotIn("scroll", harness.events)

    def test_retry_time_limit_is_checked_after_pause_and_after_capture(self):
        for expire_at in ("pause", "capture"):
            harness, epoch, calls = self.setup_reads([["unknown"] + [2] * 15, [2] * 16])
            now = [0.]
            ordinary = harness.snapshot
            def capture():
                view = ordinary()
                if expire_at == "capture" and len(calls) == 3:
                    now[0] = 2.
                return view
            harness.snapshot = capture
            def pause(seconds):
                harness.waits.append(seconds)
                if expire_at == "pause":
                    now[0] = 2.
            # Supply the same harmless fake pause through the public policy API.
            with self.subTest(expire_at=expire_at):
                result = run_inventory_walk(reset_to_top=harness.reset,
                    inventory_snapshot=harness.snapshot, transfer=harness.transfer,
                    scroll_down=harness.scroll, selected_tiers=harness.selected,
                    sorted_epoch=lambda: epoch[0], sorted_top=InventoryWalkSortedTopRecoveryTests.full_top,
                    sorted_boundary=lambda view: certify_sorted_prefix(view.scan, harness.selected),
                    check_stop=lambda: None, pause=pause, clock=lambda: now[0], max_seconds=1.)
                self.assertEqual((result.outcome, result.scrolls, result.resets), ("bounded", 0, 1))
                self.assertEqual(len(calls), 2 if expire_at == "pause" else 3)

    def test_retry_propagates_f8_takeover_and_native_preflight_errors_without_input(self):
        from runtime import SafePreflightChange
        for error in (InterruptedError("F8"), UserActivityInterrupted("takeover"),
                      WindowGeometryChanged("layout changed", input_issued=True),
                      SafePreflightChange("a new selected drop invalidated exhaustion")):
            harness, epoch, calls = self.setup_reads([["unknown"] + [2] * 15, [2] * 16])
            ordinary = harness.snapshot
            def fail():
                if len(calls) == 2:
                    raise error
                return ordinary()
            harness.snapshot = fail
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)) as caught:
                self.run_walk(harness, epoch)
            self.assertIs(caught.exception, error)
            self.assertEqual(calls, [1, 2])
            self.assertEqual(harness.events.count("reset"), 1)
            self.assertNotIn("scroll", harness.events)

    def test_new_selected_retry_observation_restarts_verified_transfer_walk(self):
        harness, epoch, calls = self.setup_reads([["unknown"] + [2] * 15])
        ordinary = harness.snapshot
        def drop():
            if len(calls) == 2:
                harness.content[0] = 3
            return ordinary()
        harness.snapshot = drop
        result = self.run_walk(harness, epoch)
        self.assertTrue(result.complete)
        self.assertEqual(harness.transferred, [3])
        self.assertEqual(result.transfers, 1)
        self.assertEqual(result.scrolls, 0)

    def test_scrolled_gap_certificate_keeps_one_final_reset_then_pure_unknown_recheck(self):
        harness = WalkHarness([4] * 24 + [2] * 16 + [1] * 16 + ["empty"] * 40, selected=(3,))
        harness.prove_bottom = False
        resets, certified_events = [0], []
        def unknown_after_reset():
            resets[0] += 1
            if resets[0] == 2:
                harness.content[0] = "unknown"
        harness.before_reset = unknown_after_reset
        def restore_known():
            if resets[0] == 2:
                harness.content[0] = 4
        harness.before_snapshot = restore_known
        def boundary(view):
            accepted = certify_sorted_prefix(view.scan, (3,))
            if accepted:
                self.assertGreater(view.offset_pixels, 0)
                certified_events.append(len(harness.events))
            return accepted
        result = harness.run(sorted_epoch=lambda: 1,
            sorted_top=InventoryWalkSortedTopRecoveryTests.full_top, sorted_boundary=boundary)
        self.assertTrue(result.complete)
        self.assertEqual(result.resets, 2)
        self.assertGreater(result.scrolls, 0)
        self.assertEqual(harness.events[certified_events[0]:], ["reset", "snapshot"])
        self.assertEqual(harness.waits[-1], .12)

    def test_retry_full_top_result_requires_strict_boolean(self):
        for bad in (None, 1, "yes"):
            harness, epoch, calls = self.setup_reads([["unknown"] + [2] * 15, [2] * 16])
            proofs = [0]
            def full_top(view):
                proofs[0] += 1
                return bad if proofs[0] == 3 else InventoryWalkSortedTopRecoveryTests.full_top(view)
            with self.subTest(value=bad), self.assertRaisesRegex(ValueError, "verified boolean"):
                harness.run(sorted_epoch=lambda: epoch[0], sorted_top=full_top,
                    sorted_boundary=lambda view: certify_sorted_prefix(view.scan, harness.selected))
            self.assertEqual(calls, [1, 2, 3])
            self.assertNotIn("scroll", harness.events)


class InventoryWalkPerformanceTests(unittest.TestCase):
    @staticmethod
    def draining_batch(harness, calls):
        def batch(*, sort, max_transfers):
            calls.append((sort, max_transfers))
            moves = 0
            while moves < max_transfers and harness.transfer(sort=sort):
                moves += 1
            return moves
        return batch

    def test_batch_counts_all_verified_moves_and_resets_only_once_after_batch(self):
        harness = WalkHarness([3, 4, 3, 4, 3, 4] + [6] * 10)
        calls = []
        result = harness.run(transfer_batch=self.draining_batch(harness, calls))
        self.assertTrue(result.complete)
        self.assertEqual(result.transfers, 6)
        self.assertEqual(harness.transferred, [3, 4, 3, 4, 3, 4])
        self.assertEqual(result.resets, 3)  # Initial, one batch, final proof.
        self.assertEqual(calls, [(False, 512), (False, 506)])
        self.assertEqual(harness.waits.count(.4), 1)

    def test_batch_takes_precedence_over_supplied_legacy_callback(self):
        harness = WalkHarness([6] * 16)
        def forbidden(*, sort):
            self.fail("The supplied batch must take precedence")
        harness.transfer = forbidden
        calls = []
        def batch(*, sort, max_transfers):
            calls.append((sort, max_transfers))
            return 0
        result = harness.run(transfer_batch=batch)
        self.assertTrue(result.complete)
        self.assertEqual(calls, [(False, 512)])

    def test_batch_can_be_the_only_transfer_callback(self):
        harness = WalkHarness([6] * 16)
        result = run_inventory_walk(reset_to_top=harness.reset,
            inventory_snapshot=harness.snapshot, transfer_batch=lambda **kwargs: 0,
            scroll_down=harness.scroll, selected_tiers=(3,), check_stop=lambda: None,
            pause=harness.waits.append)
        self.assertTrue(result.complete)

    def test_batch_budget_is_exact_and_exhaustion_sends_no_reset_or_snapshot(self):
        harness = WalkHarness([3] * 16)
        calls = []
        result = harness.run(max_transfers=5,
                             transfer_batch=self.draining_batch(harness, calls))
        self.assertEqual(result.outcome, "bounded")
        self.assertEqual(result.transfers, 5)
        self.assertEqual(result.resets, 1)
        self.assertEqual(calls, [(False, 5)])
        self.assertNotIn("snapshot", harness.events)
        self.assertNotIn("scroll", harness.events)

    def test_malformed_or_over_budget_batch_counts_abort_without_followup_input(self):
        for count in (True, False, -1, 1.0, "1", None, 4):
            harness = WalkHarness([6] * 16)
            with self.subTest(count=count), self.assertRaises(ValueError):
                harness.run(max_transfers=3, transfer_batch=lambda **kwargs: count)
            self.assertEqual(harness.events, ["reset"])

    def test_batch_stop_takeover_and_unverified_move_are_not_retried(self):
        for error in (InterruptedError("Stop"), UserActivityInterrupted("takeover"),
                      WindowGeometryChanged("moved", input_issued=True),
                      RuntimeError("issued but not verified")):
            harness = WalkHarness([3] * 16)
            calls = []
            def batch(**kwargs):
                calls.append(kwargs)
                raise error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)) as caught:
                harness.run(transfer_batch=batch)
            self.assertIs(caught.exception, error)
            self.assertEqual(len(calls), 1)
            self.assertEqual(harness.events, ["reset"])

    def test_stop_after_batch_return_prevents_reset_and_scan(self):
        harness = WalkHarness([6] * 16)
        def batch(**kwargs):
            harness.check_stop = lambda: (_ for _ in ()).throw(InterruptedError("Stop"))
            return 0
        with self.assertRaises(InterruptedError):
            harness.run(transfer_batch=batch)
        self.assertEqual(harness.events, ["reset"])

    def test_expired_batch_return_zero_or_moves_never_scans_or_resets(self):
        for count in (0, 2):
            harness = WalkHarness([6] * 16)
            now = [0.]
            def batch(**kwargs):
                now[0] = 2.
                return count
            result = harness.run(transfer_batch=batch, clock=lambda: now[0], max_seconds=1)
            with self.subTest(count=count):
                self.assertEqual(result.outcome, "bounded")
                self.assertEqual(result.transfers, count)
                self.assertEqual(harness.events, ["reset"])

    def test_top_boundary_skips_empty_tail_only_after_fresh_zero_move_scan(self):
        harness = WalkHarness([2] * 8 + ["empty"] * 200)
        harness.prove_bottom = False  # Completion must use the new certificate.
        calls = []
        def boundary(observation):
            self.assertEqual(harness.events[-1], "snapshot")
            self.assertEqual(observation.offset_pixels, 0)
            self.assertIsNotNone(observation.top_proof)
            calls.append(observation)
            return certify_sorted_prefix(observation.scan, harness.selected)
        result = harness.run(sorted_boundary=boundary)
        self.assertTrue(result.complete)
        self.assertEqual(result.resets, 2)
        self.assertEqual(result.scrolls, 0)
        self.assertEqual(len(calls), 1)

    def test_higher_only_prefix_walks_to_scrolled_lower_sentinel_then_resets_once(self):
        harness = WalkHarness([6] * 32 + [4] * 10 + [2] * 8 + ["empty"] * 200,
                              selected=(3,))
        harness.prove_bottom = False
        calls = []
        def boundary(observation):
            self.assertEqual(harness.events[-1], "snapshot")
            calls.append((observation.offset_pixels,
                          certify_sorted_prefix(observation.scan, harness.selected)))
            return calls[-1][1]
        result = harness.run(sorted_boundary=boundary)
        self.assertTrue(result.complete)
        self.assertGreater(result.scrolls, 0)
        self.assertLess(result.scrolls, 10)
        self.assertTrue(all(not answer for _, answer in calls[:-1]))
        self.assertGreater(calls[-1][0], 0)
        self.assertEqual(result.resets, 2)
        self.assertEqual(harness.first, 0)

    def test_unselected_t4_boundary_does_not_skip_later_selected_t3(self):
        harness = WalkHarness([6] * 32 + [4] * 16 + [3] * 2 + [2] * 10,
                              selected=(3,))
        result = harness.run(sorted_boundary=lambda obs: certify_sorted_prefix(obs.scan, (3,)))
        self.assertTrue(result.complete)
        self.assertEqual(harness.transferred, [3, 3])
        self.assertGreater(result.scrolls, 5)

    def test_external_true_cannot_override_unknown_bad_order_hole_or_higher_only(self):
        harness = WalkHarness([4] * 16, selected=(3,))
        with self.assertRaisesRegex(ValueError, "contradicts"):
            harness.run(sorted_boundary=lambda obs: True)
        self.assertEqual(harness.events.count("reset"), 1)
        self.assertNotIn("scroll", harness.events)
        for labels in ([2, 4] + [2] * 14, [2, "empty", 2] + ["empty"] * 13):
            harness = WalkHarness(labels, selected=(3,))
            def forbidden(obs):
                self.fail("A known ordering/hole contradiction must block shortcuts")
            with self.subTest(labels=labels):
                self.assertTrue(harness.run(sorted_boundary=forbidden).complete)
                # Independent ordinary bottom proof still checks a fresh Top.
                self.assertEqual(harness.events.count("reset"), 2)
        harness = WalkHarness(["unknown"] + [2] * 15)
        def forbidden(obs):
            self.fail("An unknown pass must never request a completion shortcut")
        self.assertEqual(harness.run(sorted_boundary=forbidden).outcome, "unknown")

    def test_unknown_seen_in_initial_reset_cannot_be_omitted_from_fresh_snapshot(self):
        harness = WalkHarness(["unknown"] + [2] * 15)
        harness.before_snapshot = lambda: harness.content.__setitem__(0, 2)
        def forbidden(obs):
            self.fail("Previously unknown reset evidence must block shortcut")
        result = harness.run(sorted_boundary=forbidden)
        self.assertEqual(result.outcome, "unknown")

    def test_unknown_seen_in_scroll_blocks_later_known_sorted_boundary(self):
        harness = WalkHarness([6] * 20 + ["unknown"] + [2] * 10 + ["empty"] * 30,
                              selected=(3,))
        calls = []
        def snapshot():
            harness.content[20] = 2  # Recognized again only after the scroll observation.
        def boundary(obs):
            calls.append(obs.offset_pixels)
            return False
        harness.before_snapshot = snapshot
        # Keep unknown until the first scrolled view has actually included it.
        ordinary_snapshot = harness.snapshot
        def fresh():
            if harness.first == 0:
                harness.events.append("snapshot")
                return harness.observation()
            return ordinary_snapshot()
        harness.snapshot = fresh
        result = harness.run(sorted_boundary=boundary)
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(calls, [0])

    def test_boundary_final_reset_new_selected_item_restarts_and_unknown_blocks_completion(self):
        for new_item in (3, "unknown"):
            harness = WalkHarness([2] * 16, selected=(3,))
            resets = [0]
            def arrive():
                resets[0] += 1
                if resets[0] == 2:
                    harness.content[0] = new_item
            harness.before_reset = arrive
            result = harness.run(sorted_boundary=lambda obs: certify_sorted_prefix(obs.scan, (3,)))
            with self.subTest(new_item=new_item):
                if new_item == 3:
                    self.assertTrue(result.complete)
                    self.assertEqual(result.transfers, 1)
                else:
                    self.assertEqual(result.outcome, "unknown")

    def test_boundary_exception_stop_timeout_or_unproved_top_never_completes(self):
        harness = WalkHarness([2] * 16)
        error = RuntimeError("Sort epoch became stale")
        def fail(obs):
            raise error
        with self.assertRaises(RuntimeError) as caught:
            harness.run(sorted_boundary=fail)
        self.assertIs(caught.exception, error)
        self.assertEqual(harness.events.count("reset"), 1)
        harness = WalkHarness([2] * 16)
        now = [0.]
        def expire(obs):
            now[0] = 2.
            return True
        self.assertEqual(harness.run(sorted_boundary=expire, clock=lambda: now[0],
                                     max_seconds=1).outcome, "bounded")
        self.assertEqual(harness.events.count("reset"), 1)
        harness = WalkHarness([2] * 16)
        def remove_top(obs):
            harness.prove_top = False
            return True
        self.assertEqual(harness.run(sorted_boundary=remove_top).outcome, "top_unconfirmed")
        harness = WalkHarness([2] * 16)
        def stop(obs):
            harness.check_stop = lambda: (_ for _ in ()).throw(InterruptedError("Stop"))
            return True
        with self.assertRaises(InterruptedError):
            harness.run(sorted_boundary=stop)
        self.assertEqual(harness.events.count("reset"), 1)

    def test_no_sorted_callback_without_initial_top_or_after_bad_overlap(self):
        harness = WalkHarness([2] * 40)
        harness.prove_top = False
        def forbidden(obs):
            self.fail("Unproved Top must not reach callback")
        self.assertEqual(harness.run(sorted_boundary=forbidden).outcome, "top_unconfirmed")
        harness = WalkHarness([6] * 40, selected=(3,))
        calls = []
        harness.scroll_override = lambda obs: replace(obs, offset_pixels=160,
                                                      scan=scan([2] * 16), top_proof=None)
        with self.assertRaisesRegex(RuntimeError, "skipped"):
            harness.run(sorted_boundary=lambda obs: calls.append(obs.offset_pixels) or False)
        self.assertEqual(calls, [0])

    def test_final_top_readback_duration_is_bounded_before_completion(self):
        harness = WalkHarness([2] * 16)
        now = [0.]
        resets = [0]
        def expire_final():
            resets[0] += 1
            if resets[0] == 2:
                now[0] = 2.
        harness.before_reset = expire_final
        result = harness.run(sorted_boundary=lambda obs: True, clock=lambda: now[0], max_seconds=1)
        self.assertEqual(result.outcome, "bounded")
        self.assertFalse(result.complete)

    def test_boundary_never_uses_pre_batch_or_stale_position_observation(self):
        harness = WalkHarness([3] + [2] * 15, selected=(3,))
        calls = []
        batch_calls = []
        epoch = [1]
        def fresh_sort_at_reset():
            if harness.transferred:
                # Synthetic guarded Sort epoch; no native input is asserted.
                harness.content[:] = [2] * 15 + ["empty"]
                epoch[0] = 2
        harness.before_reset = fresh_sort_at_reset
        def boundary(obs):
            calls.append(obs)
            self.assertEqual(harness.events[-1], "snapshot")
            self.assertEqual(harness.transferred, [3])
            return False
        self.assertTrue(harness.run(transfer_batch=self.draining_batch(harness, batch_calls),
                                    sorted_boundary=boundary, sorted_epoch=lambda: epoch[0]).complete)
        self.assertEqual(len(calls), 1)
        harness = WalkHarness([2] * 16)
        harness.before_snapshot = lambda: setattr(harness, "first", 2)
        def forbidden(obs):
            self.fail("A position change must reject before boundary callback")
        with self.assertRaisesRegex(RuntimeError, "position"):
            harness.run(sorted_boundary=forbidden)
        self.assertEqual(harness.events.count("reset"), 1)

    def test_non_boolean_sorted_callback_is_rejected_and_missing_transfer_callback_is_invalid(self):
        for value in (1, None, "complete"):
            harness = WalkHarness([2] * 16)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                harness.run(sorted_boundary=lambda obs: value)
            self.assertEqual(harness.events.count("reset"), 1)
        harness = WalkHarness([2] * 16)
        with self.assertRaisesRegex(ValueError, "transfer callback"):
            run_inventory_walk(reset_to_top=harness.reset,
                inventory_snapshot=harness.snapshot, scroll_down=harness.scroll,
                selected_tiers=(3,), check_stop=lambda: None, pause=harness.waits.append)
        self.assertEqual(harness.events, [])


if __name__ == "__main__":
    unittest.main()
