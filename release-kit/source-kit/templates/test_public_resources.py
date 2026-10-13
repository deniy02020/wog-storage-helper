"""Read-only checks of the tiny delivered resource set, without game captures.

These checks prove resource parsing and constructor compatibility, not native
window recognition. The desktop rejects every operation, including capture.
"""
import copy
import json
from pathlib import Path
import unittest

from adaptive_tiers import AdaptiveTierMatcher
from auto_layout import load_ui_variants
from core import Rect
from installer.build import referenced_sample_names, verify_header_parking_resources
from inventory_samples import load_inventory_empty_samples
from inventory_halo import verify_inventory_halo_resources
from package_release import DOCUMENTS
from runtime import Runner
from supplemental_tiers import load_supplemental_tiers


ASSETS = Path(__file__).resolve().parent / "preset_assets"


class NoDesktopOperations:
    def __getattr__(self, name):
        raise AssertionError("Read-only construction attempted a desktop operation: " + name)


class PublicResourceTests(unittest.TestCase):
    def test_source_snapshot_contains_build_documents_and_own_licence(self):
        root = ASSETS.parent
        self.assertIn("LICENSE", DOCUMENTS)
        self.assertIn("AUTHORS.md", DOCUMENTS)
        for name in DOCUMENTS:
            self.assertTrue((root / name).is_file(), name)
        self.assertIn("MIT License", (root / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("deniy", (root / "AUTHORS.md").read_text(encoding="utf-8"))

    def test_reference_resource_closure_is_delivered(self):
        reference = json.loads((ASSETS / "reference.json").read_bytes())
        names = referenced_sample_names(reference)
        self.assertGreater(len(names), 100)
        for name in names:
            self.assertEqual(Path(name).name, name)
            self.assertTrue((ASSETS / name).is_file(), name)

    def test_sanitized_header_provenance_keeps_all_resource_guards_valid(self):
        model = json.loads((ASSETS / "header_parking.json").read_bytes())
        self.assertTrue(all(entry["source_image"] == "private-source-withheld.png"
                            for entry in model["records"]))
        names = verify_header_parking_resources(model, lambda name: (ASSETS / name).read_bytes())
        self.assertEqual(len(names), 6)

    def test_adaptive_and_supplemental_models_load_from_export(self):
        matcher = AdaptiveTierMatcher(ASSETS)
        self.assertEqual(set(matcher.models), set(range(7)))
        self.assertTrue(load_ui_variants(ASSETS))
        tiers = load_supplemental_tiers(Rect(1, 14, 4, 25), (65, 65))
        self.assertEqual(set(tiers), {1, 3, 4, 5, 6})
        self.assertTrue(all(samples and all(sample.size == (3, 11) for sample in samples)
                            for samples in tiers.values()))
        empties = load_inventory_empty_samples((65, 65))
        self.assertEqual(len(empties), 1)
        self.assertEqual(empties[0].size, (65, 65))
        self.assertEqual(load_inventory_empty_samples((64, 65)), [])

    def test_inventory_halo_model_keeps_tiny_resource_proofs_after_provenance_redaction(self):
        model = json.loads((ASSETS / "inventory_halos.json").read_bytes())
        self.assertTrue(all(record["source_image"] == "private-source-withheld.png"
                            for record in model["records"]))
        names = verify_inventory_halo_resources(model, lambda name: (ASSETS / name).read_bytes())
        self.assertEqual(names, {record["image"] for record in model["records"]})
        self.assertEqual(len(names), 532)
        self.assertEqual(len(model["models"]), 128)
        self.assertTrue(all(min(record["dimensions"]) <= 4
                            and max(record["dimensions"]) <= 100
                            and record["dimensions"][0]*record["dimensions"][1] <= 400
                            for record in model["records"]))

    def test_automatic_runner_constructs_without_any_desktop_operation(self):
        reference = json.loads((ASSETS / "reference.json").read_bytes())
        profile = copy.deepcopy(reference)
        profile.update(size=reference["reference_size"], automatic_layout=True, mode="right")
        profile["pages"] = [{"id": page["id"], "button": page["button_rect"],
                             "marker": page["marker_rect"]} for page in reference["pages"]]
        profile["page_active_image"] = reference["page_active_sample"]
        profile["page_inactive_image"] = reference["page_inactive_sample"]
        profile["equipment_label_samples"] = reference["equipment_label_samples"]
        for tier, names in reference["provisional_equipped_tiers"].items():
            profile["tiers"].setdefault(tier, []).extend(
                name for name in names if name not in profile["tiers"].get(tier, []))
        runner = Runner(NoDesktopOperations(), 1, profile, ASSETS)
        self.assertIsNotNone(runner.panel_controller)
        self.assertTrue(runner.include_gems)
        self.assertEqual({tier for tier, spec in runner.filters.items() if spec["enabled"]},
                         {"3", "4", "5", "6"})
        self.assertFalse(runner.filters["1"]["enabled"])
        self.assertFalse(runner.filters["2"]["enabled"])


if __name__ == "__main__":
    unittest.main()
