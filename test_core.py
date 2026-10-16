"""Behavioral checks with deliberately adversarial recognition and transfer cases."""
from dataclasses import replace
import unittest

from PIL import Image, ImageDraw

from core import (ClassifierConfig, GridSpec, Rect, TemplateSet, choose_transfer,
                  image_rmse, scan_grid, validate_calibration, verify_transfer)


SIZE = (24, 24)
BADGE = Rect(3, 3, 11, 11)
CONFIG = ClassifierConfig(BADGE, max_tier_rmse=8, min_margin=4, max_empty_rmse=4)


def tile(tier=None):
    image = Image.new("RGB", SIZE, (12, 15, 18))
    if tier is not None:
        draw = ImageDraw.Draw(image)
        draw.rectangle((1, 1, 22, 22), fill=(60, 80, 100))
        # Distinct deterministic badges; equipment picture changes independently.
        draw.rectangle((3, 3, 10, 10), fill=(20 + 30 * tier, 250 - 25 * tier, 25 * tier))
        draw.line((14, 13, 20, 20), fill=(250, 250, 250), width=2)
    return image


TEMPLATES = TemplateSet({t: [tile(t).crop(BADGE.as_box())] for t in range(1, 7)}, [tile()])
STORAGE_TEMPLATES = TemplateSet({}, [tile()])
STORAGE_CONFIG = ClassifierConfig(max_empty_rmse=4, min_margin=4)


def grid_image(tiles, columns=None):
    columns = columns or len(tiles)
    rows = len(tiles) // columns
    image = Image.new("RGB", (columns * SIZE[0], rows * SIZE[1]))
    for index, sample in enumerate(tiles):
        image.paste(sample, ((index % columns) * SIZE[0], (index // columns) * SIZE[1]))
    return image, GridSpec(Rect(0, 0, *image.size), rows, columns)


def scan(tiles, storage=False):
    image, grid = grid_image(tiles)
    return scan_grid(image, grid, STORAGE_TEMPLATES if storage else TEMPLATES,
                     STORAGE_CONFIG if storage else CONFIG)


class GeometryTests(unittest.TestCase):
    def test_grid_has_no_overlaps_or_gaps_and_right_bottom_are_exclusive(self):
        grid = GridSpec(Rect(10, 20, 39, 39), 2, 3)
        coverage = set()
        for i in range(grid.count):
            r = grid.cell_rect(i)
            points = {(x, y) for x in range(r.left, r.right) for y in range(r.top, r.bottom)}
            self.assertFalse(coverage & points)
            coverage |= points
        self.assertEqual(len(coverage), 29 * 19)
        self.assertEqual(grid.cell_rect(5).right, 39)
        self.assertEqual(grid.cell_rect(5).bottom, 39)

    def test_outside_capture_refused(self):
        with self.assertRaises(ValueError):
            scan_grid(Image.new("RGB", SIZE), GridSpec(Rect(-1, 0, 23, 24), 1, 1), TEMPLATES, CONFIG)

    def test_negative_index_refused(self):
        with self.assertRaises(IndexError):
            GridSpec(Rect(0, 0, 24, 24), 1, 1).cell_rect(-1)

    def test_comparison_refuses_silent_scaling(self):
        with self.assertRaises(ValueError):
            image_rmse(tile(), Image.new("RGB", (23, 24)))


class RecognitionTests(unittest.TestCase):
    def test_low_tiers_higher_tiers_and_empty_are_distinguished(self):
        result = scan([tile(t) for t in (None, 1, 2, 3, 4, 5, 6)])
        self.assertEqual([c.label for c in result.cells], ["empty", "T1", "T2", "T3", "T4", "T5", "T6"])
        self.assertEqual(result.empty_count, 1)

    def test_identical_tier_samples_make_ambiguous_item_unmovable(self):
        samples = dict(TEMPLATES.tier_samples)
        samples[2] = samples[3]
        image, grid = grid_image([tile(3)])
        result = scan_grid(image, grid, TemplateSet(samples, [tile()]), CONFIG)
        self.assertIsNone(result.cells[0].tier)
        self.assertIsNone(choose_transfer(result, scan([tile()], True)))

    def test_unknown_badge_is_skipped(self):
        unusual = tile(3)
        ImageDraw.Draw(unusual).rectangle((3, 3, 10, 10), fill=(0, 0, 0))
        result = scan([unusual, tile(4)])
        self.assertEqual(choose_transfer(result, scan([tile()], True)).source_index, 1)

    def test_empty_tier_conflict_is_uncertain(self):
        samples = dict(TEMPLATES.tier_samples)
        samples[3] = [tile().crop(BADGE.as_box())]
        image, grid = grid_image([tile()])
        result = scan_grid(image, grid, TemplateSet(samples, [tile()]), CONFIG)
        self.assertIsNone(result.cells[0].empty)
        self.assertIsNone(result.cells[0].tier)

    def test_storage_uncertainty_band_cannot_be_chosen_or_verify_fill(self):
        base = tile()
        uncertain = Image.new("RGB", SIZE, (18, 21, 24))  # RMSE 6: between 4 and 8.
        result = scan([uncertain], True)
        self.assertIsNone(result.cells[0].empty)
        self.assertIsNone(choose_transfer(scan([tile(3)]), result))

    def test_invariant_badge_can_recognize_different_equipment_picture(self):
        other = tile(3)
        ImageDraw.Draw(other).rectangle((13, 13, 22, 22), fill=(150, 25, 90))
        self.assertEqual(scan([other]).cells[0].tier, 3)


class CalibrationTests(unittest.TestCase):
    def test_complete_calibration_valid(self):
        self.assertEqual(validate_calibration(SIZE, GridSpec(Rect(0, 0, 24, 24), 1, 1), TEMPLATES, CONFIG), [])

    def test_storage_needs_no_tier_labels(self):
        self.assertEqual(validate_calibration(SIZE, GridSpec(Rect(0, 0, 24, 24), 1, 1), STORAGE_TEMPLATES,
                                             STORAGE_CONFIG, require_tiers=False), [])

    def test_t1_t2_negative_samples_are_mandatory(self):
        incomplete = TemplateSet({3: TEMPLATES.tier_samples[3]}, [tile()])
        errors = validate_calibration(SIZE, GridSpec(Rect(0, 0, 24, 24), 1, 1), incomplete, CONFIG)
        self.assertTrue(any("T1 and T2" in e for e in errors))

    def test_badge_and_empty_dimensions_are_validated(self):
        errors = validate_calibration(SIZE, GridSpec(Rect(0, 0, 24, 24), 1, 1),
                                      TemplateSet(TEMPLATES.tier_samples, [Image.new("RGB", (23, 24))]),
                                      ClassifierConfig(Rect(0, 0, 25, 24)))
        self.assertTrue(any("empty sample" in e for e in errors))
        self.assertTrue(any("Badge rectangle" in e for e in errors))


class PlanningAndVerificationTests(unittest.TestCase):
    def test_only_known_t3_plus_and_nonexcluded_sources_selected(self):
        before = scan([tile(1), tile(2), tile(3), tile(4)])
        storage = scan([tile(5), tile(), tile()], True)
        candidate = choose_transfer(before, storage, excluded_inventory={2})
        self.assertEqual((candidate.source_index, candidate.destination_index, candidate.tier), (3, 1, 4))

    def test_full_storage_never_produces_action(self):
        self.assertIsNone(choose_transfer(scan([tile(3)]), scan([tile(4)], True)))

    def test_low_tier_requires_explicit_minimum_and_invalid_minima_fail(self):
        inventory, storage = scan([tile(1), tile(2)]), scan([tile()], True)
        self.assertIsNone(choose_transfer(inventory, storage))
        self.assertEqual(choose_transfer(inventory, storage, min_tier=1).tier, 1)
        self.assertEqual(choose_transfer(inventory, storage, min_tier=2).tier, 2)
        for value in (0, 7, True, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                choose_transfer(inventory, storage, min_tier=value)

    def test_valid_transfer_with_identical_item_compaction(self):
        before = scan([tile(3), tile(3), tile(1)])
        after = scan([tile(3), tile(1), tile()])
        before_storage = scan([tile(), tile()], True)
        after_storage = scan([tile(3), tile()], True)
        candidate = choose_transfer(before, before_storage)
        # The original source still looks exactly the same, yet counts prove removal.
        self.assertEqual(before.cells[0].tier, after.cells[0].tier)
        self.assertTrue(verify_transfer(before, after, before_storage, after_storage, candidate).ok)

    def test_changed_source_alone_is_not_success(self):
        before = scan([tile(3), tile(4)])
        after = scan([tile(4), tile(3)])
        storage = scan([tile()], True)
        candidate = choose_transfer(before, storage)
        self.assertFalse(verify_transfer(before, after, storage, scan([tile(3)], True), candidate).ok)

    def test_concurrent_same_tier_drop_hides_transfer_and_pauses(self):
        before = scan([tile(3), tile()])
        after = scan([tile(3), tile()])
        storage = scan([tile()], True)
        candidate = choose_transfer(before, storage)
        self.assertFalse(verify_transfer(before, after, storage, scan([tile(3)], True), candidate).ok)

    def test_source_vacancy_confirms_transfer_despite_same_tier_drop_elsewhere(self):
        before = scan([tile(3), tile(), tile(1)])
        after = scan([tile(), tile(3), tile(1)])
        storage = scan([tile(4), tile(), tile(5)])
        after_storage = scan([tile(4), tile(3), tile(5)])
        candidate = choose_transfer(before, storage)
        self.assertEqual(before.tier_counts[3], after.tier_counts[3])
        self.assertTrue(verify_transfer(before, after, storage, after_storage, candidate).ok)

    def test_source_vacancy_confirms_transfer_despite_unrelated_tier_recognition_loss(self):
        before = scan([tile(3), tile(3), tile(1)])
        unrecognized = tile(3)
        ImageDraw.Draw(unrecognized).rectangle(BADGE.as_box(), fill=(0, 0, 0))
        after = scan([tile(), unrecognized, tile(1)])
        storage = scan([tile(), tile(5)])
        after_storage = scan([tile(3), tile(5)])
        candidate = choose_transfer(before, storage)
        self.assertIsNone(after.cells[1].tier)
        self.assertIs(after.cells[1].empty, False)
        self.assertEqual(before.tier_counts[3] - after.tier_counts.get(3, 0), 2)
        self.assertTrue(verify_transfer(before, after, storage, after_storage, candidate).ok)

    def test_alternative_proof_needs_confidently_empty_original_source(self):
        before = scan([tile(3), tile(), tile(1)])
        storage = scan([tile(), tile(5)])
        after_storage = scan([tile(3), tile(5)])
        candidate = choose_transfer(before, storage)
        for replacement in (tile(3), tile(4), Image.new("RGB", SIZE, (18, 21, 24))):
            with self.subTest(replacement=replacement.getpixel((0, 0))):
                after = scan([replacement, tile(3), tile(1)])
                self.assertFalse(verify_transfer(before, after, storage, after_storage, candidate).ok)
        # A contradictory manually supplied empty/tier label is not vacancy proof.
        after = scan([tile(), tile(3), tile(1)])
        contradictory = replace(after.cells[0], tier=3)
        after = replace(after, cells=(contradictory, *after.cells[1:]))
        self.assertFalse(verify_transfer(before, after, storage, after_storage, candidate).ok)

    def test_alternative_proof_needs_exact_tier_at_original_destination(self):
        before = scan([tile(3), tile()])
        after = scan([tile(), tile(3)])
        storage = scan([tile(), tile()])
        candidate = choose_transfer(before, storage)
        cases = (scan([tile(3), tile()], True),  # Filled, but tier is unknown.
                 scan([tile(4), tile()]),
                 scan([Image.new("RGB", SIZE, (18, 21, 24)), tile()]),
                 scan([tile(), tile(3)]),
                 storage)
        for after_storage in cases:
            with self.subTest(labels=[cell.label for cell in after_storage.cells]):
                self.assertFalse(verify_transfer(before, after, storage, after_storage, candidate).ok)

    def test_alternative_proof_rejects_other_storage_changes_even_with_same_empty_total(self):
        before = scan([tile(3), tile()])
        after = scan([tile(), tile(3)])
        storage = scan([tile(), tile(4), tile(5), tile()])
        candidate = choose_transfer(before, storage)
        # Occupied-tier changes, swaps and a second fill are not one transfer.
        cases = (scan([tile(3), tile(6), tile(5), tile()]),
                 scan([tile(3), tile(5), tile(4), tile()]),
                 scan([tile(3), tile(4), tile(5), tile(3)]),
                 scan([tile(3), tile(), tile(5), tile(4)]))
        for after_storage in cases:
            with self.subTest(labels=[cell.label for cell in after_storage.cells]):
                self.assertFalse(verify_transfer(before, after, storage, after_storage, candidate).ok)

    def test_alternative_proof_rejects_uncertain_other_storage_occupancy(self):
        before = scan([tile(3), tile()])
        after = scan([tile(), tile(3)])
        storage = scan([tile(), tile(5)])
        after_storage = scan([tile(3), tile(5)])
        candidate = choose_transfer(before, storage)
        uncertain = replace(after_storage.cells[1], tier=None, empty=None)
        after_storage = replace(after_storage, cells=(after_storage.cells[0], uncertain))
        self.assertFalse(verify_transfer(before, after, storage, after_storage, candidate).ok)

    def test_transfer_proofs_reject_missing_or_misaligned_grid_cells(self):
        before = scan([tile(3), tile()])
        after = scan([tile(), tile(3)])
        storage = scan([tile(), tile(5)])
        after_storage = scan([tile(3), tile(5)])
        candidate = choose_transfer(before, storage)
        scans = [before, after, storage, after_storage]
        for index in range(4):
            for broken_cells in (scans[index].cells[:-1],
                                 tuple(reversed(scans[index].cells))):
                changed = list(scans)
                changed[index] = replace(scans[index], cells=broken_cells)
                with self.subTest(scan=index, cells=len(broken_cells)):
                    self.assertFalse(verify_transfer(*changed, candidate).ok)
        moved_grid = replace(after.grid, rect=after.grid.rect.translated(1, 0))
        self.assertFalse(verify_transfer(before, replace(after, grid=moved_grid),
                                         storage, after_storage, candidate).ok)

    def test_transfer_proofs_reject_candidate_from_another_geometry(self):
        before = scan([tile(3), tile()])
        after = scan([tile(), tile(3)])
        storage = scan([tile(), tile(5)])
        after_storage = scan([tile(3), tile(5)])
        candidate = choose_transfer(before, storage)
        for changed in (replace(candidate, source_rect=candidate.source_rect.translated(1, 0)),
                        replace(candidate, destination_rect=candidate.destination_rect.translated(1, 0))):
            self.assertFalse(verify_transfer(before, after, storage, after_storage, changed).ok)

    def test_missing_storage_change_is_not_success(self):
        before = scan([tile(3)])
        after = scan([tile()])
        storage = scan([tile()], True)
        candidate = choose_transfer(before, storage)
        self.assertFalse(verify_transfer(before, after, storage, storage, candidate).ok)

    def test_wrong_destination_or_multiple_storage_changes_pause(self):
        before = scan([tile(3)])
        after = scan([tile()])
        storage = scan([tile(), tile()], True)
        candidate = choose_transfer(before, storage)
        self.assertFalse(verify_transfer(before, after, storage, scan([tile(), tile(3)], True), candidate).ok)
        self.assertFalse(verify_transfer(before, after, storage, scan([tile(3), tile(4)], True), candidate).ok)

    def test_wrong_recognized_destination_tier_is_rejected(self):
        before = scan([tile(3)])
        after = scan([tile()])
        storage = scan([tile()])
        candidate = choose_transfer(before, storage)
        self.assertFalse(verify_transfer(before, after, storage, scan([tile(4)]), candidate).ok)


if __name__ == "__main__":
    unittest.main()
