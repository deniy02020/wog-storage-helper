"""Tiny shipped T4 variants, old-profile adoption, and conservative negatives.

These tests open only shipped small resources and supplied synthetic cells.
They do not need private screenshots or issue any native capture/game input.
Actual native-frame replay is kept separately in private verification material.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from adaptive_tiers import AdaptiveTierMatcher
from core import ClassifierConfig, Rect, TemplateSet, classify_cell, image_rmse
from paths import bootstrap_new_profile
from runtime import Runner
from supplemental_tiers import load_supplemental_tiers

BASE = Path(__file__).parent
ASSETS = BASE / "preset_assets"
BADGE = Rect(1, 14, 4, 25)


class NativeHaloTierResourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = json.loads((ASSETS / "reference.json").read_text(encoding="utf-8"))
        cls.adaptive = json.loads((ASSETS / "adaptive_tiers.json").read_text(encoding="utf-8"))
        cls.entries = cls.reference["supplemental_native_tiers"]["tiers"]["4"]

    def temporary_directory(self):
        directory = tempfile.TemporaryDirectory(dir=BASE, prefix=".test-halo-tiers-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def copied_model(self):
        target = self.temporary_directory()
        reference = copy.deepcopy(self.reference)
        for entries in reference["supplemental_native_tiers"]["tiers"].values():
            for entry in entries:
                shutil.copyfile(ASSETS / entry["image"], target / entry["image"])
        return target, reference

    def load_model(self, directory, reference):
        (directory / "reference.json").write_text(json.dumps(reference), encoding="utf-8")
        with patch("supplemental_tiers.resource_path", return_value=directory):
            return load_supplemental_tiers(BADGE, (65, 65))

    def test_five_badges_and_contexts_share_exact_rgb_pixels_without_new_thresholds(self):
        self.assertEqual(len(self.entries), 5)
        contexts = {entry["native_phase"]: entry for entry in self.adaptive["tiers"]["4"]
                    if "native_phase" in entry}
        self.assertEqual(set(contexts), {entry["native_phase"] for entry in self.entries})
        self.assertEqual((AdaptiveTierMatcher.MAX_RMSE, AdaptiveTierMatcher.MIN_MARGIN), (14, 5))
        config = ClassifierConfig(BADGE)
        self.assertEqual((config.max_tier_rmse, config.min_margin, config.max_empty_rmse), (14, 5, 12))
        for entry in self.entries:
            with self.subTest(phase=entry["native_phase"]):
                context = contexts[entry["native_phase"]]
                with Image.open(ASSETS / entry["image"]) as badge, Image.open(ASSETS / context["image"]) as image:
                    self.assertEqual((badge.mode, badge.size), ("RGB", (3, 11)))
                    self.assertEqual((image.mode, image.size), ("RGB", (8, 35)))
                    self.assertEqual(hashlib.sha256(badge.tobytes()).hexdigest(), entry["rgb_sha256"])
                    self.assertEqual(hashlib.sha256(image.tobytes()).hexdigest(), context["rgb_sha256"])
                    self.assertEqual(image.crop(BADGE.as_box()).tobytes(), badge.tobytes())
                self.assertIn("User-confirmed", entry["tier_basis"])
                self.assertFalse(entry["tooltip_tier_confirmed_for_this_sample"])

    def test_redacting_private_source_names_keeps_public_model_usable(self):
        target, reference = self.copied_model()
        for entries in reference["supplemental_native_tiers"]["tiers"].values():
            for entry in entries:
                entry["source_image"] = "private-source-withheld.png"
        self.assertEqual(len(self.load_model(target, reference)[4]), 5)

    def test_invalid_halo_phase_geometry_or_pixel_hash_cannot_load(self):
        for failure in ("phase", "index", "cell", "grid", "source_hash", "rgb_hash", "count"):
            with self.subTest(failure=failure):
                target, reference = self.copied_model()
                entries = reference["supplemental_native_tiers"]["tiers"]["4"]
                entry = entries[0]
                if failure == "phase": entry["native_phase"] = True
                elif failure == "index": entry["inventory_cell_index"] = 25
                elif failure == "cell": entry["source_cell_rect"][1] += 1
                elif failure == "grid": entry["source_grid"]["rows"] = 7
                elif failure == "source_hash": entry["source_rgb_sha256"] = "invalid"
                elif failure == "rgb_hash": entry["rgb_sha256"] = "0" * 64
                else: entries *= 2
                with self.assertRaises(ValueError):
                    self.load_model(target, reference)

    def test_existing_t1_t2_badges_remain_separate_from_every_t4_halo_variant(self):
        supplements = load_supplemental_tiers(BADGE, (65, 65))
        for tier in ("1", "2"):
            for name in self.reference["tiers"][tier]:
                with Image.open(ASSETS / name) as source:
                    badge = source.convert("RGB")
                with self.subTest(tier=tier, image=name):
                    self.assertGreater(min(image_rmse(badge, variant) for variant in supplements[4]), 19)

    def test_old_automatic_profile_loads_new_t4_samples_without_settings_write(self):
        directory = self.temporary_directory()
        settings = bootstrap_new_profile(directory)
        profile = json.loads(settings.read_text(encoding="utf-8"))
        names = {entry["image"] for entry in self.entries}
        profile["tiers"]["4"] = [name for name in profile["tiers"]["4"] if name not in names]
        profile["interval"] = 14400
        original = copy.deepcopy(profile)
        saved = settings.read_bytes()
        desktop = SimpleNamespace(stop_pressed=lambda: False,
            capture=Mock(side_effect=AssertionError("no native capture")),
            click=Mock(side_effect=AssertionError("no native input")))
        runner = Runner(desktop, 7, profile, directory)

        self.assertEqual(len(runner.inv_templates.tier_samples[4]), len(profile["tiers"]["4"]) + 5)
        self.assertEqual(runner.profile["interval"], 14400)
        self.assertEqual(profile, original)
        self.assertEqual(settings.read_bytes(), saved)
        self.assertFalse(runner.filters["1"]["enabled"])
        self.assertFalse(runner.filters["2"]["enabled"])
        desktop.capture.assert_not_called()
        desktop.click.assert_not_called()

    def test_neutral_or_unrelated_synthetic_occluders_do_not_become_t4(self):
        tiers = {int(tier): [Image.open(ASSETS / name).convert("RGB") for name in names]
                 for tier, names in self.reference["tiers"].items()}
        empties = [Image.open(ASSETS / name).convert("RGB") for name in self.reference["inventory_empty"]]
        templates = TemplateSet(tiers, empties)
        for color in ((0, 0, 0), (255, 255, 255), (255, 0, 255), (40, 40, 40)):
            with self.subTest(color=color):
                state = classify_cell(Image.new("RGB", (65, 65), color), 0, Rect(0, 0, 65, 65),
                                      templates, ClassifierConfig(BADGE))
                self.assertIsNone(state.tier)


if __name__ == "__main__":
    unittest.main()
