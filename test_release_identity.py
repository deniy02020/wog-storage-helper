"""Actual Windows version-resource serialization, without compiling or signing."""
import os
import unittest
from unittest.mock import patch

from release_identity import DEFAULT_RELEASE_VERSION, EXECUTABLES, display_version, expected_strings, numeric_version, pe_version_info, release_version


class ReleaseIdentityTests(unittest.TestCase):
    def test_local_default_is_stable_without_changing_explicit_old_versions(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(release_version(), "1.4.0")
        self.assertEqual(DEFAULT_RELEASE_VERSION, "1.4.0")
        self.assertEqual(release_version("0.2.0-beta.6"), "0.2.0-beta.6")
        self.assertEqual(release_version("0.2.0-beta.7"), "0.2.0-beta.7")

    def test_app_display_uses_source_version_and_preserves_patch_and_beta_identity(self):
        with patch.dict(os.environ, {"WOG_HELPER_BUILD_VERSION": "0.2.0-beta.6"}):
            self.assertEqual(display_version(), "1.4")
        for version, expected in (("1.0.0", "1.0"), ("1.1.0", "1.1"), ("1.2.0", "1.2"),
                                  ("1.1.2", "1.1.2"), ("0.2.0-beta.9", "0.2.0-beta.9")):
            with self.subTest(version=version):
                self.assertEqual(display_version(version), expected)

    def test_beta_and_stable_binary_versions(self):
        self.assertEqual(numeric_version(release_version("0.2.0-beta.7")), (0, 2, 0, 7))
        self.assertEqual(numeric_version(release_version("1.2.3")), (1, 2, 3, 0))

    def test_invalid_or_oversized_versions_are_rejected(self):
        for value in ("local", "1.2", "0.2.0-beta.0", "0.2.0-beta.7\n", "0.2.0;echo bad", "65536.0.0", "1.2.3-beta.65536", None):
            with self.subTest(value=value):
                if value is None:
                    with patch.dict(os.environ, {"WOG_HELPER_BUILD_VERSION": "untrusted"}):
                        with self.assertRaises(ValueError):
                            release_version()
                else:
                    with self.assertRaises(ValueError):
                        release_version(value)

    def test_explicit_build_version_overrides_environment(self):
        with patch.dict(os.environ, {"WOG_HELPER_BUILD_VERSION": "0.2.0-beta.6"}):
            self.assertEqual(release_version(), "0.2.0-beta.6")
            self.assertEqual(release_version("0.2.0-beta.7"), "0.2.0-beta.7")

    def test_all_three_owned_resources_roundtrip_real_pe_structure(self):
        from PyInstaller.utils.win32.versioninfo import VSVersionInfo
        for role, (filename, _description) in EXECUTABLES.items():
            with self.subTest(role=role):
                resource = pe_version_info(role, "0.2.0-beta.8")
                parsed = VSVersionInfo()
                parsed.fromRaw(resource.toRaw())
                delivered = {entry.name: entry.val for entry in parsed.kids[0].kids[0].kids}
                self.assertEqual(delivered, expected_strings(role, "0.2.0-beta.8"))
                self.assertEqual(delivered["OriginalFilename"], filename)
                self.assertEqual(parsed.ffi.fileVersionLS, 8)
                self.assertEqual(parsed.ffi.fileFlags, 2)

    def test_stable_owned_resources_match_app_identity_and_remove_prerelease_flag(self):
        from PyInstaller.utils.win32.versioninfo import VSVersionInfo
        for role in EXECUTABLES:
            with self.subTest(role=role), patch.dict(os.environ, {}, clear=True):
                parsed = VSVersionInfo()
                parsed.fromRaw(pe_version_info(role).toRaw())
                delivered = {entry.name: entry.val for entry in parsed.kids[0].kids[0].kids}
                self.assertEqual(delivered["FileVersion"], DEFAULT_RELEASE_VERSION)
                self.assertEqual(delivered["ProductVersion"], DEFAULT_RELEASE_VERSION)
                self.assertEqual((parsed.ffi.fileVersionMS, parsed.ffi.fileVersionLS), (65540, 0))
                self.assertEqual(parsed.ffi.fileFlags, 0)

    def test_unknown_product_role_is_rejected(self):
        with self.assertRaises(ValueError):
            pe_version_info("third_party", "0.2.0-beta.7")


if __name__ == "__main__":
    unittest.main()
