"""Five-row walking on synthetic RGB and callbacks; no desktop/native input."""
from dataclasses import replace
import unittest

import numpy as np
from PIL import Image

from core import CellState, GridScan, GridSpec, Rect
from inventory_motion_policy import InventoryMotionUnobservable
from inventory_scroller import InventoryScroller, InventoryScrollUnconfirmed, ViewportFrame
from inventory_viewport import (InventoryMotionUnknown, inventory_bulk_ready,
    prove_inventory_bulk_translation, prove_inventory_translation,
    recognize_inventory_viewport, measure_inventory_motion)


GAP = (106, 103, 97)


class SyntheticInventory:
    """Explicitly generated independent chrome/texture, never a game capture."""
    def __init__(self, *, clones=False, scale=1.):
        self.scale = scale
        rng = np.random.default_rng(3818783491)
        pitch, width, rows = 65, 325, 32
        array = rng.integers(15, 240, (pitch*rows, width, 3), dtype=np.uint8)
        for row in range(rows):
            pixels = array[row*pitch:(row+1)*pitch]
            if clones and row:
                pixels[:] = array[:pitch]
                continue
            pixels[:4] = (85, 83, 78)
            pixels[pitch-9:pitch-5] = (85, 83, 78)
            pixels[pitch-5:] = GAP
            for column in range(5):
                pixels[:, column*65:column*65+4] = (85, 83, 78)
                pixels[:, (column+1)*65-9:(column+1)*65-5] = (85, 83, 78)
                pixels[:, (column+1)*65-5:(column+1)*65] = GAP
        self.body = Image.fromarray(array).resize((round(width*scale), round(pitch*rows*scale)),
                                                  Image.Resampling.BILINEAR)
        self.rect = Rect(30, 40, 30+round(325*scale), 40+round(520*scale))

    def frame(self, offset):
        image = Image.new("RGB", (500, 700), (17, 21, 29))
        top = round(offset*self.scale)
        image.paste(self.body.crop((0, top, self.rect.width, top+self.rect.height)),
                    (self.rect.left, self.rect.top))
        view = recognize_inventory_viewport(image, self.rect, 8, 5, GAP, self.scale)
        return image, view


class BulkRGBProofTests(unittest.TestCase):
    def pair(self, first=0, second=325, **options):
        scene = SyntheticInventory(**options)
        return scene.frame(first), scene.frame(second)

    def prove(self, before, after, direction="down", **options):
        return prove_inventory_bulk_translation(before[0], after[0], before[1], after[1], direction, **options)

    def test_five_rows_are_measured_from_rgb_with_three_whole_rows_retained(self):
        before, after = self.pair()
        self.assertTrue(inventory_bulk_ready(*before, direction="down"))
        self.assertEqual(self.prove(before, after), 325.)
        self.assertEqual(self.prove(after, before, "up"), 325.)
        # A whole-row phase cannot supply legacy sub-row motion calibration.
        self.assertEqual(measure_inventory_motion(before[1], after[1], direction="down"), 0.)

    def test_shifted_seven_row_crops_still_prove_two_whole_retained_rows(self):
        before, after = self.pair(32, 352)
        self.assertEqual((before[1].grid.rows, after[1].grid.rows), (7, 7))
        self.assertEqual(self.prove(before, after), 320.)
        self.assertEqual(self.prove(after, before, "up"), 320.)

    def fractional_pitch_pair(self):
        # Explicit generated65px chrome rerasterized to64.5px pitch, leaving
        # neutral padding in the full520px viewport. Equal input-row division
        # drifts by several pixels, while the actual observed lattice is valid.
        scene = SyntheticInventory()
        scene.body = scene.body.resize((325,2064),Image.Resampling.BILINEAR)
        return scene.frame(285),scene.frame(0)

    def test_fractional_chrome_padding_does_not_replace_actual_whole_row_overlap(self):
        before,after = self.fractional_pitch_pair()
        self.assertEqual((before[1].grid.rows,after[1].grid.rows),(7,8))
        self.assertAlmostEqual(before[1].row_pitch,64.5)
        self.assertEqual(self.prove(before,after,"up"),285.)
        self.assertEqual(self.prove(after,before,"down"),285.)

    def test_unique_rgb_cannot_excuse_wrong_or_missing_actual_separator_evidence(self):
        before,after = self.fractional_pitch_pair()
        evidence = after[1].evidence
        mutations = (replace(evidence,horizontal_stripes=tuple((a+4,b+4)
                       for a,b in evidence.horizontal_stripes)),
                     replace(evidence,horizontal_stripes=evidence.horizontal_stripes[1:]))
        for changed in mutations:
            with self.subTest(changed=changed),self.assertRaises(InventoryMotionUnknown):
                self.prove(before,(after[0],replace(after[1],evidence=changed)),"up")

    def test_scaled_window_uses_same_physical_rgb_and_complete_overlap_policy(self):
        for scale in (.50, .7075, 1.):
            with self.subTest(scale=scale):
                before, after = self.pair(scale=scale)
                self.assertTrue(inventory_bulk_ready(*before, direction="down"))
                self.assertEqual(self.prove(before, after), round(325*scale))

    def test_repeated_rows_cannot_grant_preflight_or_alias_a_whole_row_jump(self):
        before, after = self.pair(clones=True)
        self.assertFalse(inventory_bulk_ready(*before, direction="down"))
        with self.assertRaises(InventoryMotionUnobservable):
            self.prove(before, after)

    def test_opposite_and_six_row_unique_moves_remain_terminal(self):
        before, after = self.pair()
        with self.assertRaises(InventoryMotionUnknown):
            self.prove(before, after, "up")
        before, after = self.pair(second=390)
        with self.assertRaises(InventoryMotionUnknown):
            self.prove(before, after)

    def test_matching_pixels_need_raw_phase_and_at_least_two_complete_rows(self):
        before, after = self.pair()
        wrong_phase = (after[0], replace(after[1], phase=(after[1].phase+4) % after[1].row_pitch))
        with self.assertRaises(InventoryMotionUnknown):
            self.prove(before, wrong_phase)
        # Header-bound viewport itself is unchanged, but cropping away another
        # genuine input row cannot falsely grant the five-row overlap.
        grid = after[1].grid
        crop = GridSpec(Rect(grid.rect.left, grid.rect.top+65, grid.rect.right, grid.rect.bottom), 7, 5)
        clipped = (after[0], replace(after[1], grid=crop))
        with self.assertRaises(InventoryMotionUnknown):
            self.prove(before, clipped, minimum_overlap_rows=3)

    def test_new_identity_size_pitch_and_unsafe_bounds_never_authorize_bulk(self):
        before, after = self.pair()
        for changed in (replace(after[1], identity="new HWND/UID/header"),
                        replace(after[1], row_pitch=after[1].row_pitch+2),
                        replace(after[1], viewport=Rect(31, 40, 356, 560))):
            with self.subTest(changed=changed):
                with self.assertRaises(InventoryMotionUnknown):
                    self.prove(before, (after[0], changed))
        with self.assertRaises(InventoryMotionUnknown):
            self.prove(before, (after[0].crop((0, 0, 490, 700)), after[1]))
        for options in ({"maximum_rows": 6}, {"maximum_rows": True},
                        {"minimum_overlap_rows": 1}, {"minimum_overlap_rows": 4}):
            with self.assertRaises(InventoryMotionUnknown):
                self.prove(before, after, **options)

    def test_zero_unique_rgb_is_only_a_distance_not_an_endpoint(self):
        before, after = self.pair(second=0)
        self.assertEqual(self.prove(before, after), 0.)

    def test_corrupted_overlap_is_not_hidden_by_tier_labels_or_normalization(self):
        before, after = self.pair()
        altered = after[0].copy()
        pixels = np.asarray(altered).copy()
        pixels[after[1].viewport.top:after[1].viewport.top+195, after[1].viewport.left:after[1].viewport.right] = (220, 3, 80)
        altered = Image.fromarray(pixels)
        with self.assertRaises(InventoryMotionUnobservable):
            self.prove(before, (altered, after[1]))


class BulkHarness:
    """Typed callbacks with an explicit fake offset; not native observation."""
    def __init__(self, *, limit=2000., step=32.):
        self.offset, self.limit, self.step = 0., limit, step
        self.tick, self.inputs, self.bursts = 0., [], []
        self.ready = True
        self.stop = lambda: None
        self.bulk_error = self.motion_error = None
        self.distance_override = None
        self.geometry = "same physical HWND/UID/header/client"
        self.frame_number = 0

    def sample(self):
        self.frame_number += 1
        grid = GridSpec(Rect(30, 40, 355, 560), 8, 5)
        scan = GridScan(grid, tuple(CellState(i, grid.cell_rect(i), 6, False, 0., 20.) for i in range(grid.count)))
        return ViewportFrame(scan, self.geometry, (-self.offset) % 65., 65., (self.offset, self.frame_number))

    def wheel(self, delta):
        self.inputs.append(delta)
        self.offset = max(0., min(self.limit, self.offset + (self.step if delta < 0 else -self.step)))
        return self.sample()

    def burst(self, delta, count):
        self.bursts.append((delta, count))
        if self.bulk_error:
            raise self.bulk_error
        self.offset = max(0., min(self.limit, self.offset + count*(self.step if delta < 0 else -self.step)))
        return self.sample()

    def motion(self, before, after, *, direction, maximum_step):
        value = (after.payload[0]-before.payload[0])*(1 if direction == "down" else -1)
        if not 0 <= value < maximum_step:
            raise InventoryScrollUnconfirmed("Fake fine movement exceeds its bound")
        return value

    def bulk_motion(self, before, after, *, direction, **_bounds):
        if self.motion_error:
            raise self.motion_error
        if self.distance_override is not None:
            return self.distance_override
        return (after.payload[0]-before.payload[0])*(1 if direction == "down" else -1)

    def pause(self, seconds):
        self.tick += seconds

    def controller(self, **options):
        values = dict(sample=self.sample, wheel=self.wheel, measure_motion=self.motion,
            calibrate_motion=self.motion, bulk_wheel=self.burst,
            bulk_ready=lambda *_args, **_options: self.ready, bulk_motion=self.bulk_motion,
            check_stop=lambda: self.stop(), pause=self.pause, clock=lambda: self.tick)
        values.update(options)
        return InventoryScroller(**values)


class BulkControllerTests(unittest.TestCase):
    def test_calibrated_same_pulses_make_one_five_row_burst_and_rgb_sets_offset(self):
        fake = BulkHarness()
        control = fake.controller()
        top = control.reset_to_top()
        fine_before = len(fake.inputs)
        view = control.scroll_down(top)
        self.assertEqual(fake.bursts[-1], (-31, 10))
        self.assertEqual(len(fake.inputs), fine_before)
        self.assertEqual(view.offset_pixels, 320.)
        self.assertIsNone(view.bottom_proof)
        self.assertIsNone(view.top_proof)

    def test_ambiguous_preflight_keeps_fine_path_without_bulk_input(self):
        fake = BulkHarness()
        fake.ready = False
        control = fake.controller()
        top = control.reset_to_top()
        view = control.scroll_down(top)
        self.assertEqual(fake.bursts, [])
        self.assertEqual(view.offset_pixels, 32.)

    def test_actual_measured_partial_clamp_does_not_use_attempted_pulse_count(self):
        fake = BulkHarness(limit=90.)
        control = fake.controller()
        top = control.reset_to_top()
        view = control.scroll_down(top)
        self.assertEqual(view.offset_pixels, 90.)
        self.assertIsNone(view.bottom_proof)
        view = control.scroll_down(view)
        self.assertEqual(view.offset_pixels, 90.)
        self.assertEqual(view.bottom_proof.kind, "calibrated_subrow_clamp")
        self.assertEqual(fake.inputs[-2:], [-31, -31])

    def test_accepted_bulk_unknown_distance_invalidates_position_without_repeat(self):
        fake = BulkHarness()
        control = fake.controller()
        top = control.reset_to_top()
        fine_before, bursts_before = len(fake.inputs), len(fake.bursts)
        fake.motion_error = InventoryMotionUnobservable("cloned after-frame")
        with self.assertRaisesRegex(InventoryScrollUnconfirmed, "accepted"):
            control.scroll_down(top)
        self.assertEqual(len(fake.bursts), bursts_before+1)
        self.assertEqual(len(fake.inputs), fine_before)
        self.assertIsNone(control._frame)
        self.assertIsNone(control._offset)

    def test_takeover_f8_or_geometry_error_after_burst_never_gets_replayed(self):
        for error in (InterruptedError("F8"), RuntimeError("user takeover"),
                      InventoryScrollUnconfirmed("geometry changed")):
            with self.subTest(error=error):
                fake = BulkHarness()
                control = fake.controller()
                top = control.reset_to_top()
                fake.bulk_error = error
                fine_before, bursts_before = len(fake.inputs), len(fake.bursts)
                with self.assertRaises(type(error)) as caught:
                    control.scroll_down(top)
                self.assertIs(caught.exception, error)
                self.assertEqual(len(fake.bursts), bursts_before+1)
                self.assertEqual(len(fake.inputs), fine_before)
                self.assertIsNone(control._offset)

    def test_unproved_oversized_or_nonfinite_bulk_distance_is_terminal(self):
        for distance in (326.6, float("nan"), True, -1):
            with self.subTest(distance=distance):
                fake = BulkHarness()
                control = fake.controller()
                top = control.reset_to_top()
                fake.distance_override = distance
                with self.assertRaises(InventoryScrollUnconfirmed):
                    control.scroll_down(top)
                self.assertIsNone(control._offset)

    def test_incomplete_bulk_callbacks_and_pulse_budget_cannot_expand_input(self):
        fake = BulkHarness()
        with self.assertRaises(ValueError):
            fake.controller(bulk_motion=None)
        control = fake.controller(max_wheel_inputs=5, bulk_ready=lambda *_a, **_k: False)
        top = control.reset_to_top()
        fake.ready = True
        view = control.scroll_down(top)
        self.assertEqual(view.offset_pixels, 32.)
        self.assertEqual(control.wheel_inputs, 5)


if __name__ == "__main__":
    unittest.main()
