"""Release input gates checked on synthetic ZIPs, with no compiler invocation."""
import ast
import hashlib
import json
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from PIL import Image

from installer.build import INSTALLER, verify_release_payload
from installer.core import EXECUTABLE, PRODUCT_DIRECTORY, InstallError
from installer.license_resources import licensing_files, licensing_hashes


class BuildInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix=".test-installer-build-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = {EXECUTABLE: b"verified executable fixture", "_internal/required.dll": b"dependency",
                      "_internal/assets/wog_storage_helper.png": b"original png",
                      "_internal/assets/wog_storage_helper.ico": b"original icon",
                      "_internal/locales.json": b"{}", "README.md": b"readme", "ATTRIBUTION.md": b"attribution",
                      "docs/STATE.md": b"actual state", "docs/STEAM_RELEASE.md": b"unresolved release gates",
                      "preset_assets/CALIBRATION.md": b"observations",
                      "_internal/preset_assets/reference.json": json.dumps({"tiers": {"3": ["tier.png"]}}).encode(),
                      "_internal/preset_assets/tier.png": b"native tier sample"}
        project = Path(__file__).resolve().parents[1]
        for name, source in licensing_files(project).items():
            self.files["_internal/" + name] = source.read_bytes()
        self.files["THIRD_PARTY_NOTICES.txt"] = (project / "THIRD_PARTY_NOTICES.txt").read_bytes()
        # The adaptive gate always receives the complete real bundled model;
        # missing-resource tests cannot pass via a fake optional model bypass.
        assets = Path(__file__).resolve().parents[1] / "preset_assets"
        self.adaptive = json.loads((assets / "adaptive_tiers.json").read_text())
        self.files["_internal/preset_assets/adaptive_tiers.json"] = (assets / "adaptive_tiers.json").read_bytes()
        groups = list(self.adaptive["tiers"].values()) + [self.adaptive["backgrounds"]]
        self.adaptive_names = {entry["image"] for entries in groups for entry in entries}
        for name in self.adaptive_names:
            self.files["_internal/preset_assets/" + name] = (assets / name).read_bytes()
        self.ui = json.loads((assets / "adaptive_ui.json").read_text())
        self.files["_internal/preset_assets/adaptive_ui.json"] = (assets / "adaptive_ui.json").read_bytes()
        self.ui_names = {entry["image"] for entries in (self.ui["headers"], self.ui["controls"]) for entry in entries}
        canonical_names = {entry["canonical_image"] for entries in (self.ui["headers"], self.ui["controls"]) for entry in entries}
        for name in self.ui_names | canonical_names:
            self.files["_internal/preset_assets/" + name] = (assets / name).read_bytes()
        self.parking = json.loads((assets / "header_parking.json").read_text())
        self.files["_internal/preset_assets/header_parking.json"] = (assets / "header_parking.json").read_bytes()
        self.parking_names = {entry["image"] for entry in self.parking["records"]}
        for name in self.parking_names:
            self.files["_internal/preset_assets/"+name] = (assets / name).read_bytes()
        self.halo = json.loads((assets / "inventory_halos.json").read_text(encoding="utf-8"))
        self.files["_internal/preset_assets/inventory_halos.json"] = (assets / "inventory_halos.json").read_bytes()
        self.halo_names = {entry["image"] for entry in self.halo["records"]}
        for name in self.halo_names:
            self.files["_internal/preset_assets/"+name] = (assets / name).read_bytes()
    def payload(self, missing=(), extra=None):
        path = self.root / "payload.zip"
        with ZipFile(path, "w") as archive:
            for name, content in (self.files | (extra or {})).items():
                if name not in missing:
                    archive.writestr(PRODUCT_DIRECTORY + "/" + name, content)
        return path

    def test_full_payload_facts_use_actual_exe_and_reference_asset(self):
        result = verify_release_payload(self.payload())
        self.assertEqual(result["exe_sha256"], hashlib.sha256(self.files[EXECUTABLE]).hexdigest())
        self.assertEqual(result["referenced_samples"], 1 + len(self.adaptive_names | self.ui_names | self.parking_names | self.halo_names))
        self.assertEqual(result["adaptive_context_samples"], len(self.adaptive_names))
        self.assertEqual(result["adaptive_ui_samples"], len(self.ui_names))
        self.assertEqual(result["headerparking_sample_count"],6)
        self.assertEqual(result["headerparking_model_sha256"],hashlib.sha256(
            self.files["_internal/preset_assets/header_parking.json"]).hexdigest())
        self.assertEqual(result["inventoryhalo_sample_count"], len(self.halo_names))
        self.assertEqual(result["inventoryhalo_model_sha256"], hashlib.sha256(
            self.files["_internal/preset_assets/inventory_halos.json"]).hexdigest())
        self.assertEqual(result["file_count"], len(self.files))
        self.assertEqual(result["license_sha256"], licensing_hashes(Path(__file__).resolve().parents[1]))

    def test_every_authentic_licence_and_both_notice_copies_are_required(self):
        for name in ("THIRD_PARTY_NOTICES.txt", "_internal/THIRD_PARTY_NOTICES.txt",
                     "_internal/licenses/manifest.json", "_internal/licenses/python/LICENSE.txt"):
            with self.subTest(name=name), self.assertRaises(InstallError):
                verify_release_payload(self.payload(missing=[name]))

    def test_tampered_licence_manifest_text_and_notice_copies_block_build(self):
        for name in ("THIRD_PARTY_NOTICES.txt", "_internal/THIRD_PARTY_NOTICES.txt",
                     "_internal/licenses/manifest.json", "_internal/licenses/python/LICENSE.txt"):
            changed = bytes([self.files[name][0] ^ 1]) + self.files[name][1:]
            with self.subTest(name=name), self.assertRaisesRegex(InstallError, "checksum"):
                verify_release_payload(self.payload(extra={name:changed}))

    def test_missing_native_reference_sample_blocks_build(self):
        with self.assertRaisesRegex(InstallError, "referenced calibration asset"):
            verify_release_payload(self.payload(missing=["_internal/preset_assets/tier.png"]))

    def test_tiny_native_image_required_but_its_private_provenance_frame_is_not(self):
        reference = {"tiers": {"3": ["tier.png"]}, "native_bag_tier_samples": [
            {"image": "native_tier5_phase.png", "source_image": "synthetic-private-source-frame.png",
             "source_file": "tooltip-cell-private.png", "source": {"image": "private-source.png"}}
        ]}
        files = {"_internal/preset_assets/reference.json": json.dumps(reference).encode(),
                 "_internal/preset_assets/native_tier5_phase.png": b"tiny actual frame sample"}
        result = verify_release_payload(self.payload(extra=files))
        self.assertEqual(result["referenced_samples"], 2 + len(self.adaptive_names | self.ui_names | self.parking_names | self.halo_names))
        with self.assertRaisesRegex(InstallError, "referenced calibration asset"):
            verify_release_payload(self.payload(extra=files, missing=["_internal/preset_assets/native_tier5_phase.png"]))

    def test_declared_image_basename_is_checked_even_inside_native_metadata(self):
        for invalid in ("../private.png", "folder/sample.png", "folder\\sample.png", "frame.txt", 123):
            reference = {"tiers": {"3": ["tier.png"]}, "native_bag_tier_samples": [{"image": invalid}]}
            with self.assertRaisesRegex(InstallError, "Invalid declared calibration image"):
                verify_release_payload(self.payload(extra={"_internal/preset_assets/reference.json": json.dumps(reference).encode()}))

    def test_missing_own_icon_or_final_docs_blocks_build(self):
        for name in ("_internal/assets/wog_storage_helper.ico", "docs/STATE.md", "_internal/locales.json"):
            with self.assertRaisesRegex(InstallError, "incomplete"):
                verify_release_payload(self.payload(missing=[name]))

    def test_adaptive_model_and_every_tiny_referenced_image_are_required(self):
        with self.assertRaisesRegex(InstallError, "incomplete"):
            verify_release_payload(self.payload(missing=["_internal/preset_assets/adaptive_tiers.json"]))
        sample = sorted(self.adaptive_names)[0]
        with self.assertRaisesRegex(InstallError, "referenced adaptive context asset"):
            verify_release_payload(self.payload(missing=["_internal/preset_assets/" + sample]))

    def test_adaptive_context_hash_and_dimensions_are_verified(self):
        sample = sorted(self.adaptive_names)[0]
        with self.assertRaisesRegex(InstallError, "checksum"):
            verify_release_payload(self.payload(extra={"_internal/preset_assets/" + sample: b"corrupt resource"}))
        changed = json.loads(json.dumps(self.adaptive))
        entry = changed["backgrounds"][0]
        invalid = self.files["_internal/assets/wog_storage_helper.png"]
        entry["sha256"] = hashlib.sha256(invalid).hexdigest()
        with self.assertRaisesRegex(InstallError, "image geometry"):
            verify_release_payload(self.payload(extra={
                "_internal/preset_assets/adaptive_tiers.json": json.dumps(changed).encode(),
                "_internal/preset_assets/" + entry["image"]: invalid}))

    def test_adaptive_private_sources_are_provenance_and_never_required_resources(self):
        changed = json.loads(json.dumps(self.adaptive))
        changed["tiers"]["3"][0]["source_image"] = "never-bundle-private-frame.png"
        changed["backgrounds"][0]["source_file"] = "private-background-full-frame.png"
        result = verify_release_payload(self.payload(extra={
            "_internal/preset_assets/adaptive_tiers.json": json.dumps(changed).encode()}))
        self.assertEqual(result["adaptive_context_samples"], len(self.adaptive_names))

    def test_adaptive_missing_negative_class_and_unsafe_image_reference_block_build(self):
        changed = json.loads(json.dumps(self.adaptive))
        del changed["tiers"]["2"]
        with self.assertRaisesRegex(InstallError, "six adaptive tier classes"):
            verify_release_payload(self.payload(extra={"_internal/preset_assets/adaptive_tiers.json": json.dumps(changed).encode()}))

    def test_ui_manifest_and_each_declared_small_native_resource_are_required(self):
        with self.assertRaisesRegex(InstallError, "incomplete"):
            verify_release_payload(self.payload(missing=["_internal/preset_assets/adaptive_ui.json"]))
        sample = sorted(self.ui_names)[0]
        with self.assertRaisesRegex(InstallError, "referenced adaptive UI asset"):
            verify_release_payload(self.payload(missing=["_internal/preset_assets/" + sample]))

    def test_ui_private_frame_provenance_is_not_a_required_resource(self):
        changed = json.loads(json.dumps(self.ui))
        changed["headers"][0]["source_image"] = "private-full-frame-do-not-ship.png"
        changed["controls"][0]["source_file"] = "another-private-full-frame.png"
        result = verify_release_payload(self.payload(extra={"_internal/preset_assets/adaptive_ui.json": json.dumps(changed).encode()}))
        self.assertEqual(result["adaptive_ui_samples"], len(self.ui_names))

    def test_ui_sample_hash_and_source_dimensions_are_verified(self):
        sample = sorted(self.ui_names)[0]
        with self.assertRaisesRegex(InstallError, "checksum"):
            verify_release_payload(self.payload(extra={"_internal/preset_assets/" + sample: b"corrupt UI PNG"}))
        changed = json.loads(json.dumps(self.ui))
        changed["headers"][0]["source_rect"][2] += 1
        with self.assertRaisesRegex(InstallError, "PNG geometry mismatch"):
            verify_release_payload(self.payload(extra={"_internal/preset_assets/adaptive_ui.json": json.dumps(changed).encode()}))

    def test_ui_bounds_scale_and_canonical_fingerprint_are_checked(self):
        for field, value, message in (
                ("source_rect", [0, 0, 301, 10], "tiny-crop bounds"),
                ("canonical_scale", 0.2, "scale or pane"),
                ("canonical_scale", float("nan"), "scale or pane"),
                ("canonical_pixels_sha256", "0" * 64, "pixel fingerprint"),
                ("image", "../private.png", "adaptive UI image")):
            changed = json.loads(json.dumps(self.ui))
            changed["headers"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(InstallError, message):
                verify_release_payload(self.payload(extra={"_internal/preset_assets/adaptive_ui.json": json.dumps(changed).encode()}))
        changed = json.loads(json.dumps(self.adaptive))
        changed["tiers"]["3"][0]["image"] = "../native-private.png"
        with self.assertRaisesRegex(InstallError, "declared adaptive image"):
            verify_release_payload(self.payload(extra={"_internal/preset_assets/adaptive_tiers.json": json.dumps(changed).encode()}))

    def test_live_profile_files_block_build_while_stock_dependency_data_is_kept(self):
        with self.assertRaisesRegex(InstallError, "live or development data"):
            verify_release_payload(self.payload(extra={"profile/settings.json": b"private"}))
        result = verify_release_payload(self.payload(extra={"_internal/cv2/data/__init__.py": b"stock dependency",
                                                           "_internal/_tk_data/images/logoLarge.gif": b"stock tcl resource"}))
        self.assertEqual(result["file_count"], len(self.files) + 2)

    def test_header_parking_json_and_all_six_actual_tiny_samples_are_required(self):
        with self.assertRaisesRegex(InstallError,"incomplete"):
            verify_release_payload(self.payload(missing=["_internal/preset_assets/header_parking.json"]))
        for name in sorted(self.parking_names):
            with self.subTest(name=name),self.assertRaisesRegex(InstallError,"referenced header parking asset"):
                verify_release_payload(self.payload(missing=["_internal/preset_assets/"+name]))

    def test_header_parking_corruption_or_inaccurate_geometry_blocks_build(self):
        for name in sorted(self.parking_names):
            with self.subTest(name=name),self.assertRaisesRegex(InstallError,"checksum"):
                verify_release_payload(self.payload(extra={"_internal/preset_assets/"+name:b"corrupt sample"}))
        changed = json.loads(json.dumps(self.parking))
        changed["records"][0]["source_rect"][2] += 1
        with self.assertRaisesRegex(InstallError,"geometry mismatch"):
            verify_release_payload(self.payload(extra={"_internal/preset_assets/header_parking.json":json.dumps(changed).encode()}))

    def test_header_parking_rejects_duplicate_unsafe_and_invalid_model_records(self):
        for field,value,message in (("image","../private-frame.png","image"),
                                    ("canonical_scale",True,"scale"),
                                    ("canonical_pixels_sha256","0"*64,"pixel fingerprint"),
                                    ("source_rect",[0,0,1000,600],"tiny-crop bounds")):
            changed = json.loads(json.dumps(self.parking))
            changed["records"][0][field] = value
            with self.subTest(field=field),self.assertRaisesRegex(InstallError,message):
                verify_release_payload(self.payload(extra={"_internal/preset_assets/header_parking.json":json.dumps(changed).encode()}))
        changed = json.loads(json.dumps(self.parking))
        changed["records"][1] = dict(changed["records"][0])
        with self.assertRaisesRegex(InstallError,"duplicate identity"):
            verify_release_payload(self.payload(extra={"_internal/preset_assets/header_parking.json":json.dumps(changed).encode()}))
        with self.assertRaisesRegex(InstallError,"model JSON"):
            verify_release_payload(self.payload(extra={"_internal/preset_assets/header_parking.json":b"{broken JSON"}))

    def test_header_parking_native_source_frames_are_provenance_never_bundled_or_required(self):
        changed = json.loads(json.dumps(self.parking))
        for record in changed["records"]:
            record["source_image"] = "private-failure-frame-do-not-bundle.png"
        result = verify_release_payload(self.payload(extra={"_internal/preset_assets/header_parking.json":json.dumps(changed).encode()}))
        self.assertEqual(result["headerparking_sample_count"],6)
        self.assertTrue(all(not name.endswith("private-failure-frame-do-not-bundle.png") for name in self.files))

    def test_inventory_halo_manifest_and_first_last_resources_are_required(self):
        with self.assertRaisesRegex(InstallError, "incomplete"):
            verify_release_payload(self.payload(missing=["_internal/preset_assets/inventory_halos.json"]))
        for name in (min(self.halo_names), max(self.halo_names)):
            with self.subTest(name=name), self.assertRaisesRegex(InstallError, "inventory halo"):
                verify_release_payload(self.payload(missing=["_internal/preset_assets/"+name]))

    def test_inventory_halo_corruption_and_inaccurate_geometry_block_build(self):
        for name in (min(self.halo_names), max(self.halo_names)):
            with self.subTest(name=name), self.assertRaisesRegex(InstallError, "checksum"):
                verify_release_payload(self.payload(extra={"_internal/preset_assets/"+name: b"corrupt halo lane"}))
        changed = json.loads(json.dumps(self.halo))
        changed["records"][0]["source_rect"][2] += 1
        with self.assertRaisesRegex(InstallError, "dimensions or pixels"):
            verify_release_payload(self.payload(extra={
                "_internal/preset_assets/inventory_halos.json": json.dumps(changed).encode()}))

    def test_inventory_halo_rejects_unsafe_duplicate_and_changed_proof_geometry(self):
        for field, value, message in (("image", "../private.png", "filename"),
                                      ("pixels_sha256", "0"*64, "pixels")):
            changed = json.loads(json.dumps(self.halo))
            changed["records"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(InstallError, message):
                verify_release_payload(self.payload(extra={
                    "_internal/preset_assets/inventory_halos.json": json.dumps(changed).encode()}))
        changed = json.loads(json.dumps(self.halo))
        changed["records"][1] = dict(changed["records"][0])
        with self.assertRaisesRegex(InstallError, "filename"):
            verify_release_payload(self.payload(extra={
                "_internal/preset_assets/inventory_halos.json": json.dumps(changed).encode()}))
        changed = json.loads(json.dumps(self.halo))
        changed["full_offsets"]["frame_top"][2] += 1
        with self.assertRaisesRegex(InstallError, "proof geometry"):
            verify_release_payload(self.payload(extra={
                "_internal/preset_assets/inventory_halos.json": json.dumps(changed).encode()}))
        for malformed in (b"{broken JSON", b"[]"):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(InstallError, "inventory halo"):
                verify_release_payload(self.payload(extra={
                    "_internal/preset_assets/inventory_halos.json": malformed}))

    def test_inventory_halo_png_dimensions_remain_guarded_after_valid_file_hash(self):
        changed = json.loads(json.dumps(self.halo))
        entry = changed["records"][0]
        stream = BytesIO()
        Image.new("RGB", (8, 8), (25, 30, 35)).save(stream, format="PNG")
        content = stream.getvalue()
        entry["sha256"] = hashlib.sha256(content).hexdigest()
        with self.assertRaisesRegex(InstallError, "dimensions or pixels"):
            verify_release_payload(self.payload(extra={
                "_internal/preset_assets/inventory_halos.json": json.dumps(changed).encode(),
                "_internal/preset_assets/"+entry["image"]: content}))

    def test_inventory_halo_private_provenance_frames_are_not_runtime_resources(self):
        changed = json.loads(json.dumps(self.halo))
        for entry in changed["records"]:
            entry["source_image"] = "private-source-withheld.png"
        for model in changed.get("supplemental_models", []):
            model["source_image"] = "private-source-withheld.png"
        result = verify_release_payload(self.payload(extra={
            "_internal/preset_assets/inventory_halos.json": json.dumps(changed).encode()}))
        self.assertEqual(result["inventoryhalo_sample_count"], len(self.halo_names))
        self.assertFalse(any(name.endswith("private-source-withheld.png") for name in self.files))

    def test_setup_spec_contains_complete_payload_and_separate_small_uninstaller(self):
        setup = (INSTALLER / "Setup.spec").read_text()
        uninstaller = (INSTALLER / "Uninstaller.spec").read_text()
        ast.parse(setup)
        ast.parse(uninstaller)
        self.assertIn("payload.zip", setup)
        self.assertIn("uninstaller.exe", setup)
        self.assertNotIn("payload.zip", uninstaller)
        for source in (setup, uninstaller):
            self.assertIn("installer_locales.json", source)
            self.assertIn("locales.json", source)
            self.assertIn("uac_admin=False", source)
            self.assertIn("console=False", source)
            self.assertIn("wog_storage_helper.ico", source)
            self.assertIn("licensing_datas(project)", source)

    def test_both_specs_collect_each_authentic_resource_without_freezing_or_execution(self):
        expected = licensing_files(Path(__file__).resolve().parents[1])
        for name in ("Setup.spec", "Uninstaller.spec"):
            captures = []
            def analysis(_scripts, **kwargs):
                captures.append(kwargs["datas"])
                return type("AnalysisFixture", (), {"pure":None,"scripts":[],"binaries":[],"datas":[]})()
            namespace = {"SPECPATH":str(INSTALLER), "Analysis":analysis,
                         "PYZ":lambda *_args:None, "EXE":lambda *_args, **_kwargs:None}
            exec(compile((INSTALLER / name).read_text(), name, "exec"), namespace)
            delivered = {str(Path(destination) / Path(source).name).replace("\\", "/"):Path(source)
                         for source, destination in captures[0]}
            for relative, original in expected.items():
                with self.subTest(spec=name, resource=relative):
                    self.assertEqual(delivered[relative].read_bytes(), original.read_bytes())


if __name__ == "__main__":
    unittest.main()
