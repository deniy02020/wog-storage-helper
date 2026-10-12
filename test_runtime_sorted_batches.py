"""Runtime batching and fresh Sort certificates with synthetic native boundaries.

No screenshots, mouse or wheel input is issued.  The ordinary ``once`` callback
stands for independently verified native transfers; tests retain each callback
invocation rather than treating a batch count as transfer verification.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import CellState, GridScan, GridSpec, Rect
from input_activity import UserActivityInterrupted
from inventory_walk import (InventoryEndpointProof, InventoryWalkObservation, InventoryWalkResult,
                            run_inventory_walk)
from runtime import Runner
from storage_cycle import StorageFull, STORAGE_FULL_MESSAGE
from windows import FocusUnavailable, HitTestMismatch, WindowGeometryChanged, WindowInfo


FULL = GridSpec(Rect(250, 100, 575, 620), 8, 5)
PARTIAL = GridSpec(Rect(250, 132, 575, 584), 7, 5)
GEOMETRY = "synthetic fixed native viewport"


def observation(labels=(), *, grid=FULL, offset=0., top=True, bottom=False):
    labels = list(labels) + ["empty"] * (grid.count - len(labels))
    if len(labels) != grid.count:
        raise ValueError("Synthetic scan must fit its viewport")
    cells = tuple(CellState(index, grid.cell_rect(index),
                            label if isinstance(label, int) else None,
                            True if label == "empty" else (None if label is None else False),
                            0., 20.) for index, label in enumerate(labels))
    proof = lambda direction: InventoryEndpointProof("scrollbar", direction, GEOMETRY, 2)
    return InventoryWalkObservation(GridScan(grid, cells), GEOMETRY, offset, 65.,
                                    proof("top") if top else None,
                                    proof("bottom") if bottom else None)


def instance(selected=(3, 4, 5, 6)):
    runner = Runner.__new__(Runner)
    runner.handle = 5
    runner.desktop = SimpleNamespace(window_info=Mock(return_value=WindowInfo(
        5, "War of Genesis: Idle Loot", r"C:\Games\Genesis.exe", 1001)))
    runner.profile = {
        "size": [1000, 800],
        "anchors": [{"rect": [10, 10, 200, 40]}, {"rect": [600, 10, 780, 40]}],
        "layout_scales": {"inventory": 1.0, "storage": 1.0},
        "inventory_viewport": {"rect": list(FULL.rect.as_box()), "phase": 0.},
        "inventory": {"rect": list(FULL.rect.as_box()), "rows": 8, "columns": 5},
    }
    runner.canonical_inv = FULL
    runner.filters = {str(tier): {"enabled": tier in selected} for tier in range(1, 7)}
    runner.check_stop = Mock()
    runner.wait = Mock()
    runner.log = Mock()
    runner.once = Mock(return_value=False)
    runner.sort_inventory = Mock()
    runner._inventory_viewport_frame = Mock(side_effect=AssertionError("unexpected native capture"))
    runner._scroll_inventory = Mock(side_effect=AssertionError("unexpected native wheel"))
    return runner


def fake_scroller(view=None):
    return SimpleNamespace(
        reset_to_top=Mock(return_value=view or observation()),
        snapshot=Mock(return_value=view or observation()),
        scroll_down=Mock(side_effect=AssertionError("unexpected wheel while draining visible batch")),
        invalidate=Mock(), begin_cycle=Mock())


class RuntimeSortedCallbackTests(unittest.TestCase):
    def callbacks(self, *, sorted_after_sort=False, selected=(3, 4, 5, 6), view=None):
        self.runner = instance(selected)
        self.scroller = fake_scroller(view)
        result = InventoryWalkResult("complete", 0, 0, 0)
        with patch("inventory_scroller.InventoryScroller", return_value=self.scroller), \
                patch("inventory_walk.run_inventory_walk", return_value=result) as walk:
            self.assertIs(self.runner._walk_inventory(sorted_after_sort=sorted_after_sort), result)
        return walk.call_args.kwargs

    def test_visible_batch_keeps_each_independent_once_and_invalidates_only_once(self):
        callbacks = self.callbacks()
        self.runner.once.side_effect = [True, True, True, False]
        self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=20), 3)
        self.assertEqual(self.runner.once.call_count, 4)
        self.assertTrue(all(call.kwargs == {"sort": False} for call in self.runner.once.call_args_list))
        self.scroller.invalidate.assert_called_once_with()
        self.scroller.reset_to_top.assert_not_called()
        self.scroller.scroll_down.assert_not_called()
        self.runner.sort_inventory.assert_not_called()
        self.runner.wait.assert_not_called()
        self.runner.check_stop.assert_has_calls([unittest.mock.call()] * 4)

    def test_remaining_budget_does_not_probe_an_extra_item(self):
        callbacks = self.callbacks()
        self.runner.once.return_value = True
        self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=2), 2)
        self.assertEqual(self.runner.once.call_count, 2)
        self.scroller.invalidate.assert_called_once_with()
        self.scroller.reset_to_top.assert_not_called()

    def test_time_limit_stops_before_next_item_after_verified_progress(self):
        with patch("runtime.time.monotonic", side_effect=[0., 899., 900.]):
            callbacks = self.callbacks()
            self.runner.once.return_value = True
            self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=20), 1)
        self.runner.once.assert_called_once_with(sort=False)
        self.scroller.invalidate.assert_called_once_with()
        self.scroller.reset_to_top.assert_not_called()

    def test_expired_batch_does_not_issue_input(self):
        with patch("runtime.time.monotonic", side_effect=[0., 900.]):
            callbacks = self.callbacks()
            self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=20), 0)
        self.runner.once.assert_not_called()
        self.scroller.invalidate.assert_not_called()
        self.scroller.reset_to_top.assert_not_called()

    def test_invalid_batch_arguments_are_rejected_before_input(self):
        for kwargs in ({"sort": True, "max_transfers": 1}, {"sort": 0, "max_transfers": 1},
                       {"max_transfers": True}, {"max_transfers": 0},
                       {"max_transfers": -1}, {"max_transfers": 1.5}):
            with self.subTest(kwargs=kwargs):
                callbacks = self.callbacks()
                with self.assertRaises(ValueError):
                    callbacks["transfer_batch"](**kwargs)
                self.runner.once.assert_not_called()
                self.scroller.invalidate.assert_not_called()

    def test_truthy_unverified_transfer_result_is_not_counted_or_retried(self):
        for value in (1, "verified", None):
            with self.subTest(value=value):
                callbacks = self.callbacks()
                self.runner.once.return_value = value
                with self.assertRaises(ValueError):
                    callbacks["transfer_batch"](sort=False, max_transfers=3)
                self.runner.once.assert_called_once_with(sort=False)
                self.scroller.invalidate.assert_not_called()
                self.scroller.reset_to_top.assert_not_called()

    def test_native_f8_full_and_unverified_errors_never_trigger_a_second_attempt(self):
        failures = (InterruptedError("F8"), StorageFull(STORAGE_FULL_MESSAGE),
                    WindowGeometryChanged("accepted input changed geometry", input_issued=True),
                    HitTestMismatch("another app beneath pointer", input_issued=True),
                    FocusUnavailable(), UserActivityInterrupted("physical mouse moved"),
                    RuntimeError("accepted transfer was not independently verified"))
        for error in failures:
            with self.subTest(error=type(error).__name__):
                callbacks = self.callbacks()
                self.runner.once.side_effect = error
                with self.assertRaises(type(error)) as raised:
                    callbacks["transfer_batch"](sort=False, max_transfers=4)
                self.assertIs(raised.exception, error)
                self.runner.once.assert_called_once_with(sort=False)
                self.scroller.invalidate.assert_not_called()
                self.scroller.reset_to_top.assert_not_called()
                self.runner.sort_inventory.assert_not_called()

    def test_stop_between_verified_items_prevents_next_item_and_cleanup_input(self):
        callbacks = self.callbacks()
        error = InterruptedError("F8 after first verified item")
        self.runner.check_stop.side_effect = [None, error]
        self.runner.once.return_value = True
        with self.assertRaises(InterruptedError) as raised:
            callbacks["transfer_batch"](sort=False, max_transfers=5)
        self.assertIs(raised.exception, error)
        self.runner.once.assert_called_once_with(sort=False)
        self.scroller.reset_to_top.assert_not_called()
        self.scroller.scroll_down.assert_not_called()
        self.runner.sort_inventory.assert_not_called()

    def test_failure_after_one_verified_item_propagates_without_retry_or_compaction(self):
        for error in (StorageFull(STORAGE_FULL_MESSAGE),
                      WindowGeometryChanged("accepted second item changed geometry", input_issued=True),
                      RuntimeError("second transfer has no independent readback proof")):
            with self.subTest(error=type(error).__name__):
                callbacks = self.callbacks()
                self.runner.once.side_effect = [True, error]
                with self.assertRaises(type(error)) as raised:
                    callbacks["transfer_batch"](sort=False, max_transfers=5)
                self.assertIs(raised.exception, error)
                self.assertEqual(self.runner.once.call_count, 2)
                self.scroller.reset_to_top.assert_not_called()
                self.scroller.scroll_down.assert_not_called()
                self.runner.sort_inventory.assert_not_called()

    def test_no_eligible_item_after_progress_compacts_once_and_rescans_top(self):
        callbacks = self.callbacks()
        self.runner.once.side_effect = [True, False, False, False]
        self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=10), 1)
        self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=9), 0)
        self.assertEqual(self.runner.once.call_count, 4)
        self.runner.sort_inventory.assert_called_once_with()
        self.scroller.reset_to_top.assert_called_once_with()
        self.assertEqual(self.scroller.invalidate.call_count, 2)
        self.assertTrue(callbacks["sorted_boundary"](observation([2, 1])))

    def test_no_progress_at_top_does_not_sort_again(self):
        callbacks = self.callbacks(sorted_after_sort=True)
        self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=10), 0)
        self.runner.sort_inventory.assert_not_called()
        self.scroller.reset_to_top.assert_not_called()
        self.scroller.invalidate.assert_not_called()

    def test_progress_away_from_top_does_not_sort_or_reset_during_batch(self):
        callbacks = self.callbacks(view=observation([4] * 40, offset=32., top=False))
        self.runner.once.side_effect = [True, False, False]
        self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=10), 1)
        self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=9), 0)
        self.runner.sort_inventory.assert_not_called()
        self.scroller.reset_to_top.assert_not_called()
        self.scroller.invalidate.assert_called_once_with()

    def test_full_sorted_top_with_lower_tiers_ends_default_selection(self):
        callbacks = self.callbacks(sorted_after_sort=True)
        self.assertTrue(callbacks["sorted_boundary"](observation([2] * 40)))
        self.assertTrue(callbacks["sorted_boundary"](observation()))
        self.runner.once.assert_not_called()

    def test_selection_hole_does_not_stop_at_tier_four(self):
        callbacks = self.callbacks(sorted_after_sort=True, selected=(3, 5, 6))
        self.assertFalse(callbacks["sorted_boundary"](observation([4] * 40)))
        self.assertTrue(callbacks["sorted_boundary"](
            observation([2] * 35, grid=PARTIAL, offset=32., top=False)))
        self.assertEqual(callbacks["selected_tiers"], {3, 5, 6})

    def test_hidden_lower_selected_tier_behind_full_higher_tiers_keeps_walk(self):
        callbacks = self.callbacks(sorted_after_sort=True, selected=(3,))
        self.assertFalse(callbacks["sorted_boundary"](observation([6] * 40)))
        self.assertFalse(callbacks["sorted_boundary"](
            observation([5] * 35, grid=PARTIAL, offset=32., top=False)))

    def test_labels_without_fresh_sort_epoch_cannot_certify_tail(self):
        callbacks = self.callbacks()
        self.assertFalse(callbacks["sorted_boundary"](observation([2] * 40)))
        self.assertFalse(callbacks["sorted_boundary"](observation()))

    def test_sorted_flag_is_strict_boolean_and_not_truthy(self):
        callbacks = self.callbacks(sorted_after_sort=1)
        self.assertFalse(callbacks["sorted_boundary"](observation()))

    def test_any_verified_item_invalidates_sorted_certificate(self):
        callbacks = self.callbacks(sorted_after_sort=True)
        self.assertTrue(callbacks["sorted_boundary"](observation([2] * 40)))
        self.runner.once.return_value = True
        self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=1), 1)
        self.assertFalse(callbacks["sorted_boundary"](observation()))

    def test_partial_top_or_missing_top_proof_cannot_certify_unseen_first_row(self):
        for view in (observation([2] * 35, grid=PARTIAL),
                     observation([2] * 40, top=False)):
            with self.subTest(grid=view.scan.grid, top=view.top_proof):
                callbacks = self.callbacks(sorted_after_sort=True)
                self.assertFalse(callbacks["sorted_boundary"](view))
                self.assertFalse(callbacks["sorted_boundary"](
                    observation([2] * 35, grid=PARTIAL, offset=32., top=False)))

    def test_unknown_bad_order_internal_empty_or_selected_item_invalidates_sort_epoch(self):
        for labels in ([None, 2], [4, 5, 2], [2, "empty", 1], [3, 2]):
            with self.subTest(labels=labels):
                callbacks = self.callbacks(sorted_after_sort=True, selected=(3, 6))
                self.assertFalse(callbacks["sorted_boundary"](observation(labels)))
                self.assertFalse(callbacks["sorted_boundary"](observation([2] * 40)))

    def test_bad_order_at_first_top_cannot_be_repaired_by_later_lower_tier_view(self):
        callbacks = self.callbacks(sorted_after_sort=True, selected=(3, 6))
        self.assertFalse(callbacks["sorted_boundary"](observation([5, 4, 5])))
        self.assertFalse(callbacks["sorted_boundary"](
            observation([2] * 35, grid=PARTIAL, offset=32., top=False)))
        self.runner.once.assert_not_called()
        self.runner.sort_inventory.assert_not_called()

    def test_unknown_scrolled_prefix_invalidates_prior_full_sorted_top(self):
        callbacks = self.callbacks(sorted_after_sort=True, selected=(3,))
        self.assertFalse(callbacks["sorted_boundary"](observation([6] * 40)))
        self.assertFalse(callbacks["sorted_boundary"](
            observation([None, 4], grid=PARTIAL, offset=32., top=False)))
        self.assertFalse(callbacks["sorted_boundary"](
            observation([2] * 35, grid=PARTIAL, offset=64., top=False)))

    def test_legacy_t1_t2_enabled_flags_stay_out_of_runtime_selected_set(self):
        callbacks = self.callbacks(sorted_after_sort=True, selected=(1, 2, 3, 5))
        self.assertEqual(callbacks["selected_tiers"], {3, 5})
        self.assertTrue(callbacks["sorted_boundary"](observation([2] * 40)))

    def test_sort_generation_advances_only_after_guarded_compaction_and_proved_top(self):
        for initially_sorted in (False, True):
            with self.subTest(initially_sorted=initially_sorted):
                callbacks = self.callbacks(sorted_after_sort=initially_sorted)
                initial = int(initially_sorted)
                self.assertEqual(callbacks["sorted_epoch"](), initial)
                self.runner.once.side_effect = [True, False, False, False]
                self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=5), 1)
                self.assertEqual(callbacks["sorted_epoch"](), initial)
                self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=4), 0)
                self.assertEqual(callbacks["sorted_epoch"](), initial + 1)
                self.scroller.snapshot()
                self.assertEqual(callbacks["sorted_epoch"](), initial + 1)

    def test_unproved_compaction_top_cannot_advance_sort_generation(self):
        callbacks = self.callbacks(sorted_after_sort=True)
        self.runner.once.side_effect = [True, False, False]
        self.assertEqual(callbacks["transfer_batch"](sort=False, max_transfers=5), 1)
        self.scroller.reset_to_top.return_value = observation(top=False)
        with self.assertRaisesRegex(RuntimeError, "начало инвентаря не подтверждено"):
            callbacks["transfer_batch"](sort=False, max_transfers=4)
        self.assertEqual(callbacks["sorted_epoch"](), 1)
        self.assertEqual(self.runner.once.call_count, 3)
        self.runner.sort_inventory.assert_called_once_with()


class RuntimeSortedWalkIntegrationTests(unittest.TestCase):
    def test_actual_policy_resets_once_after_batch_and_compacts_once_before_shortcut(self):
        runner = instance()
        labels = [6, 5]
        scroller = fake_scroller()
        scroller.snapshot.side_effect = lambda: observation(labels)
        scroller.reset_to_top.side_effect = lambda: observation(labels)
        events = []
        def once(*, sort):
            self.assertIs(sort, False)
            events.append("verified item" if labels else "fresh no candidate")
            if labels:
                labels.pop(0)
                return True
            return False
        runner.once.side_effect = once
        with patch("inventory_scroller.InventoryScroller", return_value=scroller):
            result = runner._walk_inventory(sorted_after_sort=True)
        self.assertTrue(result.complete)
        self.assertEqual((result.transfers, result.scrolls), (2, 0))
        self.assertEqual(events, ["verified item", "verified item", "fresh no candidate",
                                  "fresh no candidate", "fresh no candidate"])
        # Initial Top, one post-batch Top, one guarded compaction Top, final Top.
        self.assertEqual(scroller.reset_to_top.call_count, 4)
        self.assertEqual(result.resets, 3)  # Compaction owns its additional reset.
        self.assertEqual(scroller.invalidate.call_count, 2)
        runner.sort_inventory.assert_called_once_with()
        scroller.scroll_down.assert_not_called()
        runner._scroll_inventory.assert_not_called()

    def test_fresh_compaction_epoch_clears_post_transfer_holes_without_scrolling_tail(self):
        runner = instance()
        labels = [3, 2]
        scroller = fake_scroller()
        scroller.snapshot.side_effect = lambda: observation(labels)
        scroller.reset_to_top.side_effect = lambda: observation(labels)
        def once(*, sort):
            self.assertIs(sort, False)
            if 3 in labels:
                labels[labels.index(3)] = "empty"
                return True
            return False
        def compact():
            labels[:] = [2]
        runner.once.side_effect = once
        runner.sort_inventory.side_effect = compact
        with patch("inventory_scroller.InventoryScroller", return_value=scroller):
            result = runner._walk_inventory(sorted_after_sort=True)
        self.assertTrue(result.complete)
        self.assertEqual((result.transfers, result.scrolls, result.resets), (1, 0, 3))
        self.assertEqual(scroller.reset_to_top.call_count, 4)
        runner.sort_inventory.assert_called_once_with()
        scroller.scroll_down.assert_not_called()

    def assert_contradiction_keeps_ordinary_bottom_walk(self, initial, *, scrolled=None):
        runner = instance((3, 6))
        valid_top = observation([5] + [4] * 39)
        lower = observation([2] * 35, grid=PARTIAL, offset=32., top=False)
        bottom = observation([2] * 35, grid=PARTIAL, offset=64., top=False, bottom=True)
        scroller = fake_scroller()
        scroller.reset_to_top.side_effect = [initial, initial]
        scroller.snapshot.side_effect = [valid_top, lower, bottom]
        scroller.scroll_down.side_effect = [scrolled or lower, bottom]
        with patch("inventory_scroller.InventoryScroller", return_value=scroller):
            result = runner._walk_inventory(sorted_after_sort=True)
        self.assertTrue(result.complete)  # Independent full-bottom proof remains valid.
        self.assertEqual((result.transfers, result.scrolls, result.resets), (0, 2, 2))
        self.assertEqual(scroller.scroll_down.call_count, 2)
        runner.sort_inventory.assert_not_called()

    def test_initial_bad_order_cannot_disappear_in_later_candidate_free_snapshot(self):
        self.assert_contradiction_keeps_ordinary_bottom_walk(
            observation([5, 4, 5] + [4] * 37))

    def test_initial_empty_hole_cannot_disappear_in_later_candidate_free_snapshot(self):
        self.assert_contradiction_keeps_ordinary_bottom_walk(
            observation([5, "empty", 4] + [4] * 37))

    def test_scrolled_bad_order_cannot_disappear_before_sorted_boundary_callback(self):
        self.assert_contradiction_keeps_ordinary_bottom_walk(
            observation([5] + [4] * 39),
            scrolled=observation([4, 2, 4] + [2] * 32, grid=PARTIAL, offset=32., top=False))

    def test_shortcut_final_top_must_still_have_known_compacted_descending_order(self):
        for labels in ([5, 4, 5] + [4] * 37, [5, "empty", 4] + [4] * 37):
            with self.subTest(labels=labels):
                runner = instance((3, 6))
                known_lower = observation([2] * 40)
                contradiction = observation(labels)
                bottom = observation([2] * 35, grid=PARTIAL, offset=32., top=False, bottom=True)
                scroller = fake_scroller()
                scroller.reset_to_top.side_effect = [known_lower, contradiction, contradiction]
                scroller.snapshot.side_effect = [known_lower, contradiction, bottom]
                scroller.scroll_down.side_effect = [bottom]
                with patch("inventory_scroller.InventoryScroller", return_value=scroller):
                    result = runner._walk_inventory(sorted_after_sort=True)
                self.assertTrue(result.complete)
                self.assertEqual((result.transfers, result.scrolls, result.resets), (0, 1, 3))
                scroller.scroll_down.assert_called_once()


class SortedEpochPolicyTests(unittest.TestCase):
    def run_policy(self, epoch, *, initial=None, fresh=None, down=None, final=None, boundary=True):
        initial = initial or observation([2] * 40)
        fresh = fresh or initial
        final = final or fresh
        self.reset = Mock(side_effect=[initial, final])
        self.snapshot = Mock(side_effect=[fresh, down] if down is not None else [fresh])
        self.scroll = Mock(side_effect=[down] if down is not None else AssertionError("no wheel"))
        self.transfer = Mock(return_value=0)
        self.boundary = Mock(return_value=boundary)
        return run_inventory_walk(reset_to_top=self.reset, inventory_snapshot=self.snapshot,
            transfer_batch=self.transfer, sorted_boundary=self.boundary, sorted_epoch=epoch,
            scroll_down=self.scroll, selected_tiers={3, 6},
            check_stop=lambda: None, pause=lambda _: None)

    def test_invalid_initial_epoch_is_rejected_before_any_reset_or_transfer(self):
        for epoch in (True, -1, 1.5, "1", None):
            with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                self.run_policy(lambda: epoch)
            self.reset.assert_not_called()
            self.transfer.assert_not_called()
            self.scroll.assert_not_called()

    def test_backward_epoch_is_rejected_before_boundary_or_next_input(self):
        with self.assertRaisesRegex(ValueError, "backwards"):
            self.run_policy(Mock(side_effect=[2, 2, 1]))
        self.transfer.assert_called_once_with(sort=False, max_transfers=512)
        self.boundary.assert_not_called()
        self.scroll.assert_not_called()

    def test_advanced_epoch_without_positive_top_cannot_clear_prefix_evidence(self):
        initial = observation([4] * 40)
        down = observation([2] * 35, grid=PARTIAL, offset=32., top=False)
        with self.assertRaisesRegex(ValueError, "positively proved Top"):
            self.run_policy(Mock(side_effect=[1, 1, 1, 2]), initial=initial, down=down, boundary=False)
        self.scroll.assert_called_once()
        self.transfer.assert_called_once()

    def test_new_sort_epoch_does_not_silently_clear_prior_unknown_observation(self):
        initial = observation([None, 2])
        fresh = observation([2] * 40)
        down = observation([2] * 35, grid=PARTIAL, offset=32., top=False, bottom=True)
        result = self.run_policy(Mock(side_effect=[1, 1, 2, 2, 2, 2]),
                                 initial=initial, fresh=fresh, down=down, final=fresh)
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(result.scrolls, 1)
        self.boundary.assert_not_called()

    def test_fresh_proved_top_epoch_can_clear_known_order_contradiction(self):
        result = self.run_policy(Mock(side_effect=[1, 1, 2, 2]),
                                 initial=observation([5, 4, 5] + [4] * 37),
                                 fresh=observation([2] * 40))
        self.assertTrue(result.complete)
        self.assertEqual((result.transfers, result.scrolls, result.resets), (0, 0, 2))
        self.boundary.assert_called_once()
        self.scroll.assert_not_called()

    def test_zero_epoch_never_claims_a_fresh_sort_shortcut(self):
        result = self.run_policy(lambda: 0, initial=observation([2] * 40, bottom=True))
        self.assertTrue(result.complete)  # Ordinary bottom proof is independent.
        self.boundary.assert_not_called()
        self.scroll.assert_not_called()


class RuntimeSortedWalkOtherIntegrationTests(unittest.TestCase):

    def test_sorted_empty_top_skips_wheel_but_requires_fresh_final_top(self):
        runner = instance()
        scroller = fake_scroller(observation())
        with patch("inventory_scroller.InventoryScroller", return_value=scroller):
            result = runner._walk_inventory(sorted_after_sort=True)
        self.assertTrue(result.complete)
        self.assertEqual((result.transfers, result.scrolls, result.resets), (0, 0, 2))
        self.assertEqual(scroller.reset_to_top.call_count, 2)
        runner.once.assert_called_once_with(sort=False)
        runner.sort_inventory.assert_not_called()
        scroller.scroll_down.assert_not_called()

    def test_unknown_top_cannot_skip_wheel_even_after_sort(self):
        runner = instance()
        scroller = fake_scroller(observation([None, 2]))
        error = RuntimeError("ordinary guarded wheel reached")
        scroller.scroll_down.side_effect = error
        with patch("inventory_scroller.InventoryScroller", return_value=scroller), \
                self.assertRaises(RuntimeError) as raised:
            runner._walk_inventory(sorted_after_sort=True)
        self.assertIs(raised.exception, error)
        scroller.scroll_down.assert_called_once()
        scroller.reset_to_top.assert_called_once_with()
        runner.once.assert_called_once_with(sort=False)

    def test_selection_hole_reaches_tier_three_before_lower_tier_certificate(self):
        runner = instance((3, 5, 6))
        views = [observation([4] * 40),
                 observation([3] * 35, grid=PARTIAL, offset=32., top=False),
                 observation([2] * 40)]
        current = [views[0]]
        transferred = [False]
        scroller = fake_scroller()
        scroller.snapshot.side_effect = lambda: current[0]
        def reset():
            current[0] = views[2] if transferred[0] else views[0]
            return current[0]
        def scroll(before):
            self.assertIs(before, views[0])
            current[0] = views[1]
            return current[0]
        def once(*, sort):
            self.assertIs(sort, False)
            if current[0] is views[1] and not transferred[0]:
                transferred[0] = True
                current[0] = views[2]
                return True
            return False
        scroller.reset_to_top.side_effect = reset
        scroller.scroll_down.side_effect = scroll
        runner.once.side_effect = once
        with patch("inventory_scroller.InventoryScroller", return_value=scroller):
            result = runner._walk_inventory(sorted_after_sort=True)
        self.assertTrue(result.complete)
        self.assertEqual((result.transfers, result.scrolls), (1, 1))
        scroller.scroll_down.assert_called_once()
        runner.sort_inventory.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
