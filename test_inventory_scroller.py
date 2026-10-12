"""Typed fine-wheel/scanning callbacks; no actual capture or native input."""
from dataclasses import replace
import unittest

from core import CellState, GridScan, GridSpec, Rect
from input_activity import UserActivityInterrupted
from inventory_motion_policy import InventoryMotionUnobservable
from inventory_scroller import (InventoryCalibrationPending, InventoryScroller,
                                InventoryScrollUnconfirmed, ViewportFrame)
from inventory_walk import run_inventory_walk
from windows import WindowGeometryChanged


def scan(tier=6, *, unknown=False):
    grid = GridSpec(Rect(100, 200, 200, 360), 8, 5)
    cells = tuple(CellState(index, grid.cell_rect(index), None if unknown else tier,
                            None if unknown else False, 0., 20.)
                  for index in range(grid.count))
    return GridScan(grid, cells)


class WheelHarness:
    def __init__(self, *, offset=0., limit=80., step=4.):
        self.offset, self.limit, self.step = offset, limit, step
        self.geometry, self.pitch = "physical HWND+PID+client+viewport", 20.
        self.inputs, self.frames, self.calibrations, self.waits = [], [], [], []
        self.tick = 0.
        self.stop = lambda: None
        self.before_wheel = lambda: None
        self.before_sample = lambda: None
        self.sample_override = None
        self.motion_override = None
        self.calibration_override = None
        self.wheel_error = None
        self.unknown = False

    def sample(self):
        self.before_sample()
        frame = ViewportFrame(scan(unknown=self.unknown), self.geometry,
                              (-self.offset) % self.pitch, self.pitch,
                              payload=(self.offset, len(self.frames)))
        self.frames.append(frame)
        return self.sample_override(frame) if self.sample_override else frame

    def wheel(self, delta):
        self.inputs.append(delta)
        self.before_wheel()
        if self.wheel_error:
            raise self.wheel_error
        self.offset = max(0., min(self.limit, self.offset + (self.step if delta < 0 else -self.step)))
        return self.sample()

    def motion(self, before, after, *, direction, maximum_step):
        if self.motion_override:
            return self.motion_override(before, after, direction, maximum_step)
        distance = after.payload[0] - before.payload[0]
        distance *= 1 if direction == "down" else -1
        if distance < 0 or distance >= maximum_step:
            raise InventoryScrollUnconfirmed("opposite or aliased motion")
        return distance

    def calibrate(self, before, after, *, direction, maximum_step):
        self.calibrations.append((before, after, direction, maximum_step))
        if self.calibration_override:
            return self.calibration_override(before, after, direction, maximum_step)
        # This explicitly supplied fake stands for the separate unique-RGB
        # proof provider. It is not a native image or real calibration result.
        return self.motion(before, after, direction=direction, maximum_step=maximum_step)

    def pause(self, seconds):
        self.waits.append(seconds)
        self.tick += seconds

    def controller(self, **options):
        defaults = dict(sample=self.sample, wheel=self.wheel, measure_motion=self.motion,
                        calibrate_motion=self.calibrate, check_stop=lambda: self.stop(),
                        pause=self.pause, clock=lambda: self.tick)
        defaults.update(options)
        return InventoryScroller(**defaults)


class ScrollerEndpointTests(unittest.TestCase):
    def test_initial_downward_unique_calibration_then_two_up_clamps_prove_top(self):
        harness = WheelHarness()
        controller = harness.controller()
        view = controller.reset_to_top()
        self.assertEqual(harness.inputs, [-31, 31, 31, 31])
        self.assertEqual(view.offset_pixels, 0.)
        self.assertEqual(view.top_proof.kind, "calibrated_subrow_clamp")
        self.assertEqual((view.top_proof.direction, view.top_proof.confirmations), ("top", 2))
        self.assertEqual(view.top_proof.calibrated_step_pixels, 4.)
        self.assertIsNone(view.bottom_proof)
        self.assertEqual([call[2] for call in harness.calibrations], ["down", "up"])

    def test_starting_at_bottom_calibrates_up_before_resetting_absolute_zero(self):
        harness = WheelHarness(offset=12., limit=12.)
        controller = harness.controller()
        top = controller.reset_to_top()
        self.assertEqual(harness.offset, 0.)
        self.assertEqual(top.offset_pixels, 0.)
        self.assertEqual(harness.inputs, [-31, 31, -31, 31, 31, 31, 31, 31])
        self.assertEqual([call[2] for call in harness.calibrations], ["up", "down"])
        next_view = controller.scroll_down(top)
        self.assertEqual(next_view.offset_pixels, 4.)
        self.assertEqual([call[2] for call in harness.calibrations], ["up", "down"])

    def test_bottom_rgb_can_calibrate_both_directions_before_returning_to_cloned_top_rows(self):
        harness = WheelHarness(offset=12., limit=12.)
        def only_distinct_tail(before, after, direction, maximum):
            if min(before.payload[0], after.payload[0]) < 8.:
                raise InventoryScrollUnconfirmed("cloned top rows have no unique RGB calibration")
            return harness.motion(before, after, direction=direction, maximum_step=maximum)
        harness.calibration_override = only_distinct_tail
        controller = harness.controller()
        top = controller.reset_to_top()
        self.assertEqual(top.offset_pixels, 0.)
        view = controller.scroll_down(top)
        self.assertEqual(view.offset_pixels, 4.)
        self.assertEqual(len(harness.calibrations), 2)

    def test_overlapping_small_steps_continue_through_identical_tier_rows_until_positive_bottom(self):
        harness = WheelHarness(limit=24.)
        controller = harness.controller()
        view = controller.reset_to_top()
        offsets = []
        while view.bottom_proof is None:
            view = controller.scroll_down(view)
            offsets.append(view.offset_pixels)
        self.assertEqual(offsets, [4., 8., 12., 16., 20., 24., 24.])
        self.assertEqual(harness.inputs[-2:], [-31, -31])
        self.assertEqual(view.bottom_proof.confirmations, 2)
        self.assertEqual(view.bottom_proof.direction, "bottom")
        self.assertIsNone(view.top_proof)

    def test_acknowledged_unchanged_short_inventory_does_not_bootstrap_endpoints(self):
        harness = WheelHarness(limit=0.)
        controller = harness.controller()
        with self.assertRaisesRegex(InventoryCalibrationPending, "positively calibrated") as caught:
            controller.reset_to_top()
        self.assertEqual(caught.exception.reason, "no_observable_motion")
        self.assertEqual(harness.inputs, [-31, 31])
        self.assertEqual(harness.calibrations, [])
        self.assertIsNone(controller._top_proof)
        self.assertIsNone(controller._bottom_proof)

    def test_missing_unique_rgb_calibration_sends_no_wheel(self):
        harness = WheelHarness()
        controller = harness.controller(calibrate_motion=None)
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "RGB"):
            controller.reset_to_top()
        self.assertEqual(harness.inputs, [])

    def test_clone_alias_rejected_by_rgb_provider_stops_without_reverse_retry(self):
        harness = WheelHarness()
        error = InventoryScrollUnconfirmed("whole-row clone aliases")
        def ambiguous(*_args):
            raise error
        harness.calibration_override = ambiguous
        controller = harness.controller()
        with self.assertRaises(InventoryScrollUnconfirmed) as caught:
            controller.reset_to_top()
        self.assertIs(caught.exception, error)
        self.assertEqual(harness.inputs, [-31])

    def test_phase_and_unique_rgb_disagreement_never_supply_calibration(self):
        harness = WheelHarness()
        harness.calibration_override = lambda *_args: 7.
        controller = harness.controller()
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "disagrees"):
            controller.reset_to_top()
        self.assertEqual(harness.inputs, [-31])
        self.assertEqual(controller._steps, {})

    def test_snapshots_retain_endpoint_proof_only_at_same_phase_and_geometry(self):
        harness = WheelHarness()
        controller = harness.controller()
        top = controller.reset_to_top()
        fresh = controller.snapshot()
        self.assertEqual(fresh.top_proof, top.top_proof)
        before = len(harness.inputs)
        harness.offset = 4.
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "position changed"):
            controller.snapshot()
        self.assertIsNone(controller._top_proof)
        self.assertEqual(len(harness.inputs), before)

    def test_invalidation_after_transfer_requires_a_new_top_proof_before_snapshot(self):
        harness = WheelHarness(offset=12.)
        controller = harness.controller()
        controller.reset_to_top()
        controller.invalidate()
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "invalidated"):
            controller.snapshot()
        restored = controller.reset_to_top()
        self.assertEqual(restored.offset_pixels, 0.)
        self.assertIsNotNone(restored.top_proof)

    def test_walk_policy_integrates_with_actual_measured_offsets_and_final_reset(self):
        harness = WheelHarness(limit=24.)
        controller = harness.controller()
        result = run_inventory_walk(reset_to_top=controller.reset_to_top,
            inventory_snapshot=controller.snapshot, transfer=lambda **_: False,
            scroll_down=controller.scroll_down, selected_tiers={3},
            check_stop=lambda: harness.stop(), pause=harness.pause,
            clock=lambda: harness.tick)
        self.assertTrue(result.complete)
        self.assertEqual(result.transfers, 0)
        self.assertEqual(harness.offset, 0.)
        self.assertEqual(result.scrolls, 7)


class ScrollerBootstrapTests(unittest.TestCase):
    def test_cloned_prefix_seeks_distinct_texture_then_proves_both_directions(self):
        harness = WheelHarness(limit=40.)
        controller = harness.controller()
        seen_unpositioned = []
        def prove_distinct(before, after, direction, maximum):
            seen_unpositioned.append((controller._offset, controller._top_proof,
                                      controller._bottom_proof))
            if min(before.payload[0], after.payload[0]) < 12.:
                raise InventoryMotionUnobservable("healthy cloned prefix")
            return harness.motion(before, after, direction=direction, maximum_step=maximum)
        harness.calibration_override = prove_distinct
        top = controller.reset_to_top()
        self.assertEqual(harness.inputs[:5], [-31, -31, -31, -31, 31])
        self.assertEqual([c[2] for c in harness.calibrations], ["down"] * 4 + ["up"])
        self.assertEqual(seen_unpositioned, [(None, None, None)] * 5)
        self.assertEqual(controller._steps, {"down": 4., "up": 4.})
        self.assertEqual(harness.offset, 0.)
        self.assertEqual(top.offset_pixels, 0.)
        self.assertIsNotNone(top.top_proof)
        self.assertIsNone(top.bottom_proof)

    def test_aliased_seek_exhaustion_is_pending_after_at_most_sixteen_wheels(self):
        harness = WheelHarness(limit=400.)
        harness.calibration_override = lambda *_args: (_ for _ in ()).throw(
            InventoryMotionUnobservable("cloned rows"))
        controller = harness.controller()
        with self.assertRaises(InventoryCalibrationPending) as caught:
            controller.reset_to_top()
        self.assertEqual(caught.exception.reason, "calibration_budget")
        self.assertEqual(harness.inputs, [-31] * 16)
        self.assertEqual(controller._steps, {})
        self.assertIsNone(controller._offset)
        self.assertIsNone(controller._frame)
        self.assertIsNone(controller._top_proof)
        self.assertIsNone(controller._bottom_proof)
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "invalidated"):
            controller.snapshot()
        self.assertEqual(len(harness.inputs), 16)

    def test_seek_time_budget_stops_before_any_further_wheel(self):
        harness = WheelHarness(limit=400.)
        harness.calibration_override = lambda *_args: (_ for _ in ()).throw(
            InventoryMotionUnobservable("texture unavailable"))
        def slow_settle(_seconds):
            harness.tick += 10.
        controller = harness.controller(pause=slow_settle)
        with self.assertRaises(InventoryCalibrationPending) as caught:
            controller.reset_to_top()
        self.assertEqual(caught.exception.reason, "calibration_budget")
        self.assertEqual(harness.inputs, [-31] * 3)
        self.assertEqual(harness.tick, 30.)
        self.assertIsNone(controller._offset)
        self.assertIsNone(controller._top_proof)

    def test_one_proved_command_never_proves_the_other_aliased_direction(self):
        harness = WheelHarness(limit=400.)
        def direction_proof(before, after, direction, maximum):
            if direction == "up":
                raise InventoryMotionUnobservable("upward texture unavailable")
            return harness.motion(before, after, direction=direction, maximum_step=maximum)
        harness.calibration_override = direction_proof
        controller = harness.controller()
        with self.assertRaises(InventoryCalibrationPending):
            controller.reset_to_top()
        self.assertEqual(controller._steps, {"down": 4.})
        self.assertLessEqual(len(harness.inputs), 16)
        self.assertIsNone(controller._offset)
        self.assertIsNone(controller._top_proof)
        self.assertIsNone(controller._bottom_proof)

    def test_pending_short_can_retry_later_with_new_positive_live_evidence(self):
        harness = WheelHarness(limit=0.)
        controller = harness.controller()
        with self.assertRaises(InventoryCalibrationPending):
            controller.reset_to_top()
        harness.limit = 12.
        harness.tick += 1800.
        controller.begin_cycle()
        top = controller.reset_to_top()
        self.assertEqual(controller._steps, {"down": 4., "up": 4.})
        self.assertIsNotNone(top.top_proof)
        self.assertEqual(controller.wheel_inputs, 4)

    def test_unobservable_from_motion_or_native_callbacks_is_not_retried(self):
        for source in ("measure", "wheel"):
            with self.subTest(source=source):
                harness = WheelHarness()
                failure = InventoryMotionUnobservable("outside RGB calibration provider")
                if source == "measure":
                    harness.motion_override = lambda *_args: (_ for _ in ()).throw(failure)
                else:
                    harness.wheel_error = failure
                controller = harness.controller()
                with self.assertRaises(InventoryMotionUnobservable) as caught:
                    controller.reset_to_top()
                self.assertIs(caught.exception, failure)
                self.assertEqual(harness.inputs, [-31])

    def test_unsafe_evidence_after_healthy_alias_propagates_without_next_probe(self):
        for failure in (InterruptedError("F8"), UserActivityInterrupted("mouse takeover"),
                        WindowGeometryChanged("changed target", input_issued=True),
                        InventoryScrollUnconfirmed("strong contrary full RGB motion"),
                        RuntimeError("unverified native receipt")):
            with self.subTest(failure=type(failure).__name__):
                harness = WheelHarness(limit=400.)
                def calibration(*_args):
                    if len(harness.calibrations) == 1:
                        raise InventoryMotionUnobservable("healthy alias")
                    raise failure
                harness.calibration_override = calibration
                controller = harness.controller()
                with self.assertRaises(type(failure)) as caught:
                    controller.reset_to_top()
                self.assertIs(caught.exception, failure)
                self.assertEqual(harness.inputs, [-31, -31])
                self.assertIsNone(controller._offset)
                self.assertIsNone(controller._top_proof)

    def test_geometry_change_during_seek_is_fatal_before_next_wheel(self):
        harness = WheelHarness(limit=400.)
        def first_alias(*_args):
            harness.geometry = "different physical viewport"
            raise InventoryMotionUnobservable("healthy previous texture")
        harness.calibration_override = first_alias
        controller = harness.controller()
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "geometry changed") as caught:
            controller.reset_to_top()
        self.assertNotIsInstance(caught.exception, InventoryCalibrationPending)
        self.assertEqual(harness.inputs, [-31])
        self.assertEqual(controller._steps, {})


class ScrollerGuardTests(unittest.TestCase):
    def test_geometry_or_pitch_change_stop_before_another_input(self):
        for mutation in (lambda h: setattr(h, "geometry", "new HWND client"),
                         lambda h: setattr(h, "pitch", 21.)):
            harness = WheelHarness()
            controller = harness.controller()
            view = controller.reset_to_top()
            count = len(harness.inputs)
            mutation(harness)
            with self.assertRaises(InventoryScrollUnconfirmed):
                controller.scroll_down(view)
            self.assertEqual(len(harness.inputs), count)

    def test_unknown_item_tiers_allow_guarded_calibration_and_scrolling_but_not_complete(self):
        harness = WheelHarness(limit=12.)
        harness.unknown = True
        controller = harness.controller()
        top = controller.reset_to_top()
        self.assertGreater(top.scan.unknown_count, 0)
        following = controller.scroll_down(top)
        self.assertEqual(following.offset_pixels, 4.)
        self.assertGreater(following.scan.unknown_count, 0)
        self.assertIsNone(following.bottom_proof)

    def test_unknown_item_walk_reaches_proved_bottom_then_returns_unknown(self):
        harness = WheelHarness(limit=12.)
        harness.unknown = True
        controller = harness.controller()
        result = run_inventory_walk(reset_to_top=controller.reset_to_top,
            inventory_snapshot=controller.snapshot, transfer=lambda **_: False,
            scroll_down=controller.scroll_down, selected_tiers={3},
            check_stop=lambda: harness.stop(), pause=harness.pause,
            clock=lambda: harness.tick)
        self.assertEqual(result.outcome, "unknown")
        self.assertFalse(result.complete)
        self.assertEqual(result.transfers, 0)
        self.assertEqual(result.scrolls, 4)
        self.assertEqual(harness.offset, 0.)

    def test_invalid_or_whole_row_motion_is_terminal_after_one_input(self):
        for bad in (float("nan"), float("inf"), -1., True, 12., 20.):
            with self.subTest(distance=bad):
                harness = WheelHarness()
                harness.motion_override = lambda *_args, result=bad: result
                controller = harness.controller()
                with self.assertRaises(InventoryScrollUnconfirmed):
                    controller.reset_to_top()
                self.assertEqual(harness.inputs, [-31])

    def test_fresh_acknowledgement_cannot_reuse_the_preinput_frame(self):
        harness = WheelHarness()
        controller = harness.controller(wheel=lambda _delta: harness.frames[-1])
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "fresh acknowledged"):
            controller.reset_to_top()
        self.assertEqual(controller.wheel_inputs, 1)

    def test_stop_takeover_and_native_input_exceptions_propagate_without_cleanup_wheel(self):
        errors = (InterruptedError("F8"), UserActivityInterrupted("real mouse input"),
                  WindowGeometryChanged("geometry", input_issued=True), RuntimeError("unverified wheel"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                harness = WheelHarness()
                harness.wheel_error = error
                controller = harness.controller()
                with self.assertRaises(type(error)) as caught:
                    controller.reset_to_top()
                self.assertIs(caught.exception, error)
                self.assertEqual(harness.inputs, [-31])

    def test_stop_during_settling_prevents_another_capture_or_input(self):
        harness = WheelHarness()
        error = InterruptedError("Stop during wheel settling")
        def pause(_seconds):
            harness.stop = lambda: (_ for _ in ()).throw(error)
        controller = harness.controller(pause=pause)
        with self.assertRaises(InterruptedError) as caught:
            controller.reset_to_top()
        self.assertIs(caught.exception, error)
        self.assertEqual(harness.inputs, [-31])

    def test_total_input_and_elapsed_time_bounds_cannot_loop_indefinitely(self):
        harness = WheelHarness(offset=80., limit=80.)
        controller = harness.controller(max_wheel_inputs=4)
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "input limit"):
            controller.reset_to_top()
        self.assertEqual(len(harness.inputs), 4)
        harness = WheelHarness(offset=80., limit=80.)
        controller = harness.controller(max_seconds=.3)
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "bounded limit"):
            controller.reset_to_top()
        self.assertLessEqual(len(harness.inputs), 2)

    def test_stale_walk_position_or_unproved_snapshot_never_issues_wheel(self):
        harness = WheelHarness()
        controller = harness.controller()
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "invalidated"):
            controller.snapshot()
        top = controller.reset_to_top()
        count = len(harness.inputs)
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "stale"):
            controller.scroll_down(replace(top, offset_pixels=999.))
        self.assertEqual(len(harness.inputs), count)

    def test_zero_claim_with_changed_phase_is_not_a_clamp(self):
        harness = WheelHarness()
        harness.motion_override = lambda *_args: 0.
        controller = harness.controller()
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "changed lattice"):
            controller.reset_to_top()
        self.assertEqual(harness.inputs, [-31])

    def test_invalid_constructor_bounds_are_rejected_before_callbacks(self):
        for options in ({"wheel_delta": 120}, {"wheel_delta": True}, {"wheel_delta": 0},
                        {"max_wheel_inputs": 0}, {"max_seconds": float("inf")},
                        {"settle_seconds": -1}):
            harness = WheelHarness()
            with self.subTest(options=options), self.assertRaises(ValueError):
                harness.controller(**options)
            self.assertEqual(harness.inputs, [])


class ScrollerCycleReuseTests(unittest.TestCase):
    def walk(self, harness, controller):
        return run_inventory_walk(reset_to_top=controller.reset_to_top,
            inventory_snapshot=controller.snapshot, transfer=lambda **_: False,
            scroll_down=controller.scroll_down, selected_tiers={3},
            check_stop=lambda: harness.stop(), pause=harness.pause,
            clock=lambda: harness.tick)

    def test_same_live_controller_proves_full_first_cycle_and_short_second_cycle(self):
        harness = WheelHarness(limit=12.)
        controller = harness.controller(max_wheel_inputs=16)
        controller.begin_cycle()
        first = self.walk(harness, controller)
        self.assertTrue(first.complete)
        self.assertEqual(first.scrolls, 4)
        self.assertEqual(controller.wheel_inputs, 14)
        calibrations = len(harness.calibrations)
        previous_top = controller._top_proof
        proof_steps = dict(controller._steps)
        old_inputs = len(harness.inputs)
        # A later cycle can have a bag shorter than one viewport. It cannot
        # generate a new positive calibration; only this live calibration is
        # retained, while all endpoint/position evidence and budgets are fresh.
        harness.limit = harness.offset = 0.
        harness.tick += 1800.
        controller.begin_cycle()
        self.assertEqual(controller.wheel_inputs, 0)
        self.assertEqual(controller._steps, proof_steps)
        self.assertIsNone(controller._frame)
        self.assertIsNone(controller._offset)
        self.assertIsNone(controller._top_proof)
        self.assertIsNone(controller._bottom_proof)
        second = self.walk(harness, controller)
        self.assertTrue(second.complete)
        self.assertEqual(second.scrolls, 1)
        self.assertEqual(controller.wheel_inputs, 6)
        self.assertEqual(harness.inputs[old_inputs:], [31, 31, -31, -31, 31, 31])
        self.assertEqual(len(harness.calibrations), calibrations)
        self.assertIsNot(controller._top_proof, previous_top)

    def test_begin_cycle_revokes_old_position_and_endpoints_before_any_input(self):
        harness = WheelHarness(limit=12.)
        controller = harness.controller()
        old = controller.reset_to_top()
        inputs = len(harness.inputs)
        geometry, pitch, steps = controller._geometry, controller._pitch, dict(controller._steps)
        controller.begin_cycle()
        self.assertEqual((controller._geometry, controller._pitch), (geometry, pitch))
        self.assertEqual(controller._steps, steps)
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "invalidated"):
            controller.snapshot()
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "stale"):
            controller.scroll_down(old)
        self.assertEqual(len(harness.inputs), inputs)

    def test_geometry_or_pitch_change_discards_retained_calibration_before_wheel(self):
        for change in ("geometry", "pitch"):
            with self.subTest(change=change):
                harness = WheelHarness(limit=12.)
                controller = harness.controller()
                controller.reset_to_top()
                identity, pitch = controller._geometry, controller._pitch
                inputs = len(harness.inputs)
                if change == "geometry":
                    harness.geometry = "a different native target"
                else:
                    harness.pitch = 21.
                controller.begin_cycle()
                with self.assertRaisesRegex(InventoryScrollUnconfirmed, "geometry changed"):
                    controller.reset_to_top()
                self.assertEqual(len(harness.inputs), inputs)
                self.assertEqual(controller._steps, {})
                self.assertIsNone(controller._top_proof)
                self.assertIsNone(controller._bottom_proof)
                self.assertEqual((controller._geometry, controller._pitch), (identity, pitch))

    def test_another_controller_cannot_borrow_calibration_for_first_ever_short_bag(self):
        harness = WheelHarness(limit=12.)
        first = harness.controller()
        first.reset_to_top()
        self.assertTrue(first._steps)
        harness.limit = harness.offset = 0.
        second = harness.controller()
        second.begin_cycle()
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "positively calibrated"):
            second.reset_to_top()
        self.assertEqual(second._steps, {})
        self.assertIsNone(second._top_proof)
        self.assertIsNone(second._bottom_proof)

    def test_stop_wins_before_budget_reset_or_retained_proof_changes(self):
        harness = WheelHarness()
        controller = harness.controller()
        controller.reset_to_top()
        previous = (controller._started_at, controller.wheel_inputs,
                    controller._frame, controller._top_proof, dict(controller._steps))
        inputs = len(harness.inputs)
        failure = InterruptedError("F8 before next cycle")
        harness.stop = lambda: (_ for _ in ()).throw(failure)
        with self.assertRaises(InterruptedError) as caught:
            controller.begin_cycle()
        self.assertIs(caught.exception, failure)
        self.assertEqual((controller._started_at, controller.wheel_inputs,
                          controller._frame, controller._top_proof, controller._steps), previous)
        self.assertEqual(len(harness.inputs), inputs)


class CalibratedHalfRowMotionTests(unittest.TestCase):
    def test_half_row_and_slightly_larger_observed_steps_need_both_rgb_direction_proofs(self):
        for step in (10., 10.2, 11.9):
            with self.subTest(step=step):
                harness = WheelHarness(limit=step * 3, step=step)
                controller = harness.controller()
                top = controller.reset_to_top()
                self.assertEqual(top.offset_pixels, 0.)
                self.assertEqual([call[2] for call in harness.calibrations], ["down", "up"])
                self.assertEqual([call[3] for call in harness.calibrations], [12., 12.])
                following = controller.scroll_down(top)
                self.assertAlmostEqual(following.offset_pixels, step)
                self.assertIsNone(following.bottom_proof)

    def test_later_response_above_same_direction_calibration_is_terminal(self):
        harness = WheelHarness(step=10.2)
        controller = harness.controller()
        top = controller.reset_to_top()
        inputs = len(harness.inputs)
        harness.step = 11.0  # Still <60% of20px, but >10.2+.75.
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "calibrated direction step"):
            controller.scroll_down(top)
        self.assertEqual(len(harness.inputs), inputs + 1)
        self.assertEqual(controller._steps, {})
        self.assertIsNone(controller._top_proof)
        self.assertIsNone(controller._bottom_proof)

    def test_smaller_clipped_steps_can_reach_bottom_but_do_not_replace_calibration(self):
        harness = WheelHarness(limit=25., step=10.2)
        controller = harness.controller()
        view = controller.reset_to_top()
        calibration = dict(controller._steps)
        for _ in range(3):
            view = controller.scroll_down(view)
        self.assertEqual(view.offset_pixels, 25.)
        self.assertIsNone(view.bottom_proof)
        view = controller.scroll_down(view)
        self.assertEqual(view.bottom_proof.confirmations, 2)
        self.assertEqual(controller._steps, calibration)

    def test_up_and_down_keep_independent_calibrated_step_bounds(self):
        harness = WheelHarness(limit=60.)
        def asymmetric_wheel(delta):
            harness.inputs.append(delta)
            step = 10.2 if delta < 0 else 6.0
            harness.offset = max(0., min(harness.limit, harness.offset + (step if delta < 0 else -step)))
            return harness.sample()
        controller = harness.controller(wheel=asymmetric_wheel)
        top = controller.reset_to_top()
        self.assertAlmostEqual(controller._steps["down"], 10.2)
        self.assertAlmostEqual(controller._steps["up"], 6.)
        following = controller.scroll_down(top)
        self.assertAlmostEqual(following.offset_pixels, 10.2)
        self.assertEqual(len(harness.calibrations), 2)

    def test_initial_half_row_without_unique_rgb_evidence_cannot_bootstrap(self):
        harness = WheelHarness(step=10.2)
        error = InventoryScrollUnconfirmed("whole-row clone alias in unique RGB proof")
        harness.calibration_override = lambda *_args: (_ for _ in ()).throw(error)
        controller = harness.controller()
        with self.assertRaises(InventoryScrollUnconfirmed) as caught:
            controller.reset_to_top()
        self.assertIs(caught.exception, error)
        self.assertEqual(harness.inputs, [-31])
        self.assertEqual(controller._steps, {})

    def test_new_cycle_retains_half_row_calibration_only_until_response_changes(self):
        harness = WheelHarness(step=10.2)
        controller = harness.controller()
        controller.reset_to_top()
        controller.begin_cycle()
        top = controller.reset_to_top()
        self.assertEqual(len(harness.calibrations), 2)
        count = len(harness.inputs)
        harness.step = 11.0
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "calibrated direction step"):
            controller.scroll_down(top)
        self.assertEqual(len(harness.inputs), count + 1)
        self.assertEqual(controller._steps, {})


if __name__ == "__main__":
    unittest.main()
