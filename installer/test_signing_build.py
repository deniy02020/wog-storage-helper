"""Signing order tests use inert files and fake compiler/trust boundaries only."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from installer import build as builder
from installer.core import InstallError, file_hash

VERSION = "0.2.0-beta.7"
SIGNER = "A" * 40


class SigningBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix=".test-signing-build-", dir=builder.PROJECT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = self.root / "release.zip"
        self.payload.write_bytes(b"inert zip fixture")
        self.output = self.root / "wrapper-output"
        self.events = []
        output = patch("builtins.print")
        output.start()
        self.addCleanup(output.stop)
        self.facts = {"exe_sha256": hashlib.sha256(b"app").hexdigest(), "license_sha256": {}}
        for target, replacement in (
            ("_require_compiler", lambda: None),
            ("verify_release_payload", lambda _path: self.facts),
            ("verify_wrapper_licenses", lambda *_args: {}),
            ("_freeze", self.freeze),
        ):
            active = patch.object(builder, target, replacement)
            active.start()
            self.addCleanup(active.stop)

    def freeze(self, name, output, *, version, output_root):
        self.assertEqual(version, VERSION)
        self.assertEqual(output_root, self.output)
        self.events.append("freeze_" + output)
        name = "WoGStorageHelperUninstall.exe" if output == "uninstaller" else "WoGStorageHelperSetup.exe"
        path = output_root / "dist" / output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(output.encode())
        if output == "setup" and (output_root / "resources/payload_metadata.json").is_file():
            self.embedded = json.loads((output_root / "resources/payload_metadata.json").read_text())
            self.embedded_uninstaller = (output_root / "resources/uninstaller.exe").read_bytes()

    def signer(self, path):
        role = "uninstaller" if "Uninstall" in path.name else "setup"
        self.events.append("sign_" + role)
        path.write_bytes(path.read_bytes() + b"-signed")

    def verifier(self, path, *, role, version, expected_signer=None):
        self.events.append("verify_" + role)
        self.assertEqual(version, VERSION)
        self.assertEqual(expected_signer, SIGNER)
        self.assertTrue(path.read_bytes().endswith(b"-signed"))
        return {"sha256": file_hash(path), "signer_thumbprint": SIGNER}

    def payload_verifier(self, path, *, version):
        self.events.append("verify_app")
        self.assertEqual(path, self.payload)
        self.assertEqual(version, VERSION)
        return {"sha256": self.facts["exe_sha256"], "signer_thumbprint": SIGNER}

    def call(self, **kwargs):
        return builder.build(self.payload, VERSION, output_root=self.output, verifier=self.verifier,
                             payload_verifier=self.payload_verifier, **kwargs)

    def test_unsigned_default_preserves_working_build_without_trust_calls(self):
        result = self.call()
        self.assertEqual(self.events, ["freeze_uninstaller", "freeze_setup"])
        self.assertEqual(result["signing_state"], "unsigned")
        self.assertEqual(result["signatures"], {})

    def test_local_signing_happens_before_embedding_and_final_hashes(self):
        result = self.call(signer=self.signer)
        self.assertEqual(self.events, ["verify_app", "freeze_uninstaller", "sign_uninstaller", "verify_uninstaller",
                                       "freeze_setup", "sign_setup", "verify_setup"])
        self.assertEqual(self.embedded_uninstaller, b"uninstaller-signed")
        self.assertEqual(self.embedded["uninstaller_sha256"], hashlib.sha256(self.embedded_uninstaller).hexdigest())
        self.assertEqual(result["setup_sha256"], hashlib.sha256(b"setup-signed").hexdigest())
        self.assertEqual(result["signing_state"], "signed")

    def test_returned_cloud_signed_uninstaller_is_not_recompiled(self):
        remove = self.root / "returned-uninstaller.exe"
        remove.write_bytes(b"cloud-signed")
        result = self.call(signed_uninstaller=remove, require_signed_inputs=True, defer_setup_signing=True)
        self.assertEqual(self.events, ["verify_app", "verify_uninstaller", "freeze_setup"])
        self.assertEqual(self.embedded_uninstaller, b"cloud-signed")
        self.assertEqual(result["signing_state"], "awaiting_setup_signature")
        self.assertNotIn("setup", result["signatures"])

    def test_rejected_uninstaller_signature_never_reaches_setup_compilation(self):
        def reject(*_args, **_kwargs):
            raise InstallError("untrusted")
        with self.assertRaises(InstallError):
            builder.build(self.payload, VERSION, output_root=self.output, signer=self.signer,
                          verifier=reject, payload_verifier=self.payload_verifier)
        self.assertNotIn("freeze_setup", self.events)
        self.assertFalse((self.output / "resources/uninstaller.exe").exists())

    def test_rejected_setup_signature_produces_no_final_metadata(self):
        def reject_setup(path, **kwargs):
            if kwargs["role"] == "setup":
                raise InstallError("untrusted setup")
            return self.verifier(path, **kwargs)
        with self.assertRaises(InstallError):
            builder.build(self.payload, VERSION, output_root=self.output, signer=self.signer,
                          verifier=reject_setup, payload_verifier=self.payload_verifier)
        self.assertFalse((self.output / "dist/setup/setup_metadata.json").exists())

    def test_verified_payload_change_is_rejected_before_compilation(self):
        def change_payload(path, *, version):
            report = self.payload_verifier(path, version=version)
            path.write_bytes(b"replaced payload")
            return report
        with self.assertRaises(InstallError):
            builder.build(self.payload, VERSION, output_root=self.output, signer=self.signer,
                          verifier=self.verifier, payload_verifier=change_payload)
        self.assertFalse(any(event.startswith("freeze_") for event in self.events))

    def test_invalid_signing_modes_fail_before_any_build_or_copy(self):
        for options in ({"defer_setup_signing": True}, {"signed_uninstaller": self.payload},
                        {"require_signed_inputs": True}, {"require_signed_inputs": 1},
                        {"signer": self.signer, "defer_setup_signing": True}):
            with self.subTest(options=options), self.assertRaises(InstallError):
                self.call(**options)
        self.assertEqual(self.events, [])

    def test_uninstaller_staging_has_no_payload_or_setup_step(self):
        result = builder.build_uninstaller(VERSION, output_root=self.output)
        self.assertEqual(self.events, ["freeze_uninstaller"])
        self.assertEqual(result["signing_state"], "unsigned")
        self.assertTrue((self.output / "dist/uninstaller/uninstaller_build_metadata.json").is_file())
        self.assertFalse((self.output / "resources").exists())


if __name__ == "__main__":
    unittest.main()
