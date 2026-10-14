"""Strict profile alias proof with synthetic paths; no game/native input.

The alias tests explicitly supply Windows virtualization's same-directory
receipt. Distinct and external resolved parents retain real samefile checks.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paths import BASE, bootstrap_new_profile, canonical_profile_directory
from runtime import Runner
from storage_pages import StoragePageDetector


class ProfileDirectoryAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=BASE, prefix=".test-profile-alias-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.selected = self.root / "selected"
        self.alias = self.root / "physical-alias"
        self.external = self.root / "external"
        for directory in (self.selected, self.alias, self.external):
            directory.mkdir()
        self.probe_name = "sample.png"
        for directory in (self.selected, self.alias, self.external):
            (directory / self.probe_name).write_bytes(b"PNG filename fixture; no image decoding")

    def redirect(self, target, *, prove_alias=False, probe_name=None):
        name = probe_name or self.probe_name
        original_resolve, original_samefile = Path.resolve, Path.samefile
        def resolve(path, *args, **kwargs):
            if path == self.selected / name:
                return target
            return original_resolve(path, *args, **kwargs)
        def samefile(path, other):
            if prove_alias and path == self.alias and Path(other) == self.selected:
                return True  # Explicit fake transparent-directory identity receipt.
            return original_samefile(path, other)
        return patch.object(Path, "resolve", resolve), patch.object(Path, "samefile", samefile)

    def test_ordinary_profile_preserves_canonical_contained_directory(self):
        self.assertEqual(canonical_profile_directory(self.selected, self.probe_name),
                         self.selected.resolve())

    def test_proved_transparent_directory_alias_becomes_shared_canonical_root(self):
        resolver, identity = self.redirect(self.alias / self.probe_name, prove_alias=True)
        with resolver, identity:
            self.assertEqual(canonical_profile_directory(self.selected, self.probe_name), self.alias)

    def test_distinct_directory_with_identical_filename_and_bytes_is_rejected(self):
        resolver, identity = self.redirect(self.alias / self.probe_name)
        with resolver, identity, self.assertRaisesRegex(ValueError, "внутри папки профиля"):
            canonical_profile_directory(self.selected, self.probe_name)

    def test_external_symlink_resolution_cannot_adopt_the_targets_directory(self):
        # Explicitly emulate an external symlink's resolved target. Its parent
        # really is a different directory, even with identical probe contents.
        resolver, identity = self.redirect(self.external / self.probe_name)
        with resolver, identity, self.assertRaisesRegex(ValueError, "внутри папки профиля"):
            canonical_profile_directory(self.selected, self.probe_name)

    def test_alias_receipt_cannot_change_the_requested_probe_basename(self):
        renamed = self.alias / "different.png"
        renamed.write_bytes(b"different filename")
        resolver, identity = self.redirect(renamed, prove_alias=True)
        with resolver, identity, self.assertRaises(ValueError):
            canonical_profile_directory(self.selected, self.probe_name)

    def test_absolute_traversal_stream_and_invalid_probe_names_fail_before_filesystem_reads(self):
        names = (None, True, 17, "", "../sample.png", "nested/sample.png", "nested\\sample.png",
                 "C:\\sample.png", "C:sample.png", "\\sample.png", "/sample.png", "sample.png:stream",
                 "sample.jpg", "sample.png.", "sample\x00.png", "sample\n.png", "sample?.png", "CON.png")
        for name in names:
            with self.subTest(name=name), patch.object(Path, "resolve", side_effect=AssertionError(
                    "Invalid probe names must fail before filesystem reads")), self.assertRaises(ValueError):
                canonical_profile_directory(self.selected, name)

    def test_missing_probe_directory_probe_and_directory_instead_of_png_are_rejected(self):
        for directory, name in ((self.selected, "missing.png"),
                                (self.selected / self.probe_name, self.probe_name)):
            with self.subTest(directory=directory, name=name), self.assertRaises(ValueError):
                canonical_profile_directory(directory, name)
        (self.selected / "directory.png").mkdir()
        with self.assertRaises(ValueError):
            canonical_profile_directory(self.selected, "directory.png")

    def test_unavailable_physical_identity_proof_never_permits_an_alias(self):
        resolver, _ = self.redirect(self.alias / self.probe_name)
        with resolver, patch.object(Path, "samefile", side_effect=OSError("Identity unavailable")):
            with self.assertRaises(ValueError) as caught:
                canonical_profile_directory(self.selected, self.probe_name)
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_unavailable_path_resolution_is_rejected(self):
        with patch.object(Path, "resolve", side_effect=OSError("Path unavailable")):
            with self.assertRaises(ValueError) as caught:
                canonical_profile_directory(self.selected, self.probe_name)
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_runner_passes_one_proved_alias_to_panel_layout_label_and_page_readers(self):
        settings = bootstrap_new_profile(self.alias)
        profile = json.loads(settings.read_text(encoding="utf-8"))
        name = profile["inventory_empty"][0]
        resolver, identity = self.redirect(self.alias / name, prove_alias=True, probe_name=name)
        with resolver, identity:
            runner = Runner(None, 1, profile, self.selected)
            pages = StoragePageDetector(runner.canonical_profile, runner.directory)
        self.assertEqual(runner.directory, self.alias)
        self.assertEqual(runner.panel_controller.directory, self.alias)
        self.assertEqual(runner.panel_controller.layout.directory, self.alias)
        self.assertEqual(runner.gear.directory, self.alias)
        self.assertEqual(pages.directory, self.alias)


if __name__ == "__main__":
    unittest.main()
