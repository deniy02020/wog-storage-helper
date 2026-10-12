"""Authenticode failures and asynchronous release finalization with inert fixtures."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from installer.core import InstallError, file_hash
from release_identity import expected_strings
import windows_signing as signing

VERSION = "0.2.0-beta.7"
SIGNER = "A" * 40


class WindowsSigningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix=".test-signatures-", dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.exe = self.root / "app.exe"
        self.exe.write_bytes(b"inert signed fixture")
        self.document = {**expected_strings("app", VERSION), "status": "Valid", "signer_thumbprint": SIGNER,
                         "signer_subject": "test issuer", "timestamp_thumbprint": "B" * 40,
                         "eku": [signing.CODE_SIGNING_OID]}

    def verify(self, document=None, **kwargs):
        return signing.verify_owned_executable(self.exe, role="app", version=VERSION,
                 query=lambda _path: self.document if document is None else document, **kwargs)

    def test_trusted_timestamped_owned_identity_returns_actual_hash(self):
        result = self.verify(expected_signer=SIGNER.lower())
        self.assertEqual(result["sha256"], file_hash(self.exe))
        self.assertEqual(result["signer_thumbprint"], SIGNER)

    def test_unsigned_invalid_expired_and_untrusted_are_rejected(self):
        for status in ("NotSigned", "HashMismatch", "NotTrusted", "UnknownError", "Valid "):
            with self.subTest(status=status), self.assertRaises(InstallError):
                self.verify({**self.document, "status": status})

    def test_missing_timestamp_eku_or_thumbprint_is_rejected(self):
        for changes in ({"timestamp_thumbprint": None}, {"timestamp_thumbprint": "0"},
                        {"eku": []}, {"eku": None}, {"signer_thumbprint": "bad"}):
            with self.subTest(changes=changes), self.assertRaises(InstallError):
                self.verify({**self.document, **changes})

    def test_wrong_metadata_or_release_or_signer_is_rejected(self):
        for field in expected_strings("app", VERSION):
            with self.subTest(field=field), self.assertRaises(InstallError):
                self.verify({**self.document, field: "wrong"})
        with self.assertRaises(InstallError):
            self.verify(expected_signer="C" * 40)

    def test_mutation_during_windows_trust_query_is_rejected(self):
        def change(path):
            path.write_bytes(b"different bytes")
            return self.document
        with self.assertRaises(InstallError):
            signing.verify_owned_executable(self.exe, role="app", version=VERSION, query=change)

    def test_payload_app_only_is_extracted_and_never_launched(self):
        payload = self.root / "payload.zip"
        with ZipFile(payload, "w") as archive:
            archive.writestr("WoGStorageHelper/WoGStorageHelper.exe", b"app")
            archive.writestr("WoGStorageHelper/untrusted.exe", b"not extracted")
        def check(path, **kwargs):
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertEqual(path.read_bytes(), b"app")
            self.assertEqual(kwargs, {"role": "app", "version": VERSION, "expected_signer": None})
            return {"sha256": file_hash(path)}
        result = signing.verify_signed_payload(payload, version=VERSION, verifier=check)
        self.assertEqual(result["sha256"], hashlib.sha256(b"app").hexdigest())

    def test_local_signer_rejects_untrusted_tool_or_credential_timestamp(self):
        tool = self.root / "signtool.exe"
        tool.write_bytes(b"inert SDK fixture")
        with self.assertRaises(InstallError):
            signing.CertificateStoreSigner(signtool=tool, thumbprint=SIGNER, timestamp_url="https://timestamp.example",
                                            query=lambda _path: {"status": "NotSigned"})
        for url in ("http://timestamp.example", "https://user:password@timestamp.example", "https://timestamp.example/#fragment"):
            with self.subTest(url=url), self.assertRaises(InstallError):
                signing.CertificateStoreSigner(signtool=tool, thumbprint=SIGNER, timestamp_url=url)

    def test_verified_local_tool_change_prevents_sign_command(self):
        tool = self.root / "signtool.exe"
        tool.write_bytes(b"inert SDK fixture")
        signer = signing.CertificateStoreSigner(signtool=tool, thumbprint=SIGNER, timestamp_url="https://timestamp.example",
                     query=lambda _path: {"status": "Valid", "signer_subject": "CN=Microsoft Corporation, O=Microsoft Corporation",
                                          "eku": [signing.CODE_SIGNING_OID]})
        tool.write_bytes(b"replaced")
        with patch.object(signing.subprocess, "run") as command, self.assertRaises(InstallError):
            signer(self.exe)
        command.assert_not_called()

    def test_tool_publisher_substring_is_not_a_microsoft_identity(self):
        tool = self.root / "signtool.exe"
        tool.write_bytes(b"inert SDK fixture")
        for subject in ("CN=Someone Microsoft Corporation, O=Attacker", "CN=Microsoft Corporation, O=Attacker"):
            with self.subTest(subject=subject), self.assertRaises(InstallError):
                signing.CertificateStoreSigner(signtool=tool, thumbprint=SIGNER, timestamp_url="https://timestamp.example",
                    query=lambda _path: {"status": "Valid", "signer_subject": subject, "eku": [signing.CODE_SIGNING_OID]})

    def test_payload_duplicate_app_is_rejected_before_any_trust_query(self):
        payload = self.root / "duplicate.zip"
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(payload, "w") as archive:
                archive.writestr("WoGStorageHelper/WoGStorageHelper.exe", b"first")
                archive.writestr("WoGStorageHelper/WoGStorageHelper.exe", b"second")
        with patch.object(signing, "verify_owned_executable") as verify, self.assertRaises(InstallError):
            signing.verify_signed_payload(payload, version=VERSION, verifier=verify)
        verify.assert_not_called()

    def test_payload_change_during_app_verification_is_rejected(self):
        payload = self.root / "payload.zip"
        with ZipFile(payload, "w") as archive:
            archive.writestr("WoGStorageHelper/WoGStorageHelper.exe", b"app")
        def change(_path, **_kwargs):
            payload.write_bytes(b"changed zip")
            return {"sha256": "not used"}
        with self.assertRaises(InstallError):
            signing.verify_signed_payload(payload, version=VERSION, verifier=change)


class FinalizeSigningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix=".test-finalize-signatures-", dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = self.root / "payload.zip"
        self.payload.write_bytes(b"exact ZIP bytes")
        self.remove = self.root / "uninstaller.exe"
        self.remove.write_bytes(b"exact signed uninstaller")
        self.setup = self.root / "setup.exe"
        self.setup.write_bytes(b"returned signed Setup")
        self.metadata = self.root / "pending.json"
        self.output = self.root / "final.json"
        self.original = {"version": VERSION, "signing_state": "awaiting_setup_signature", "sha256": file_hash(self.payload),
                         "exe_sha256": "D" * 64, "uninstaller_sha256": file_hash(self.remove), "license_sha256": {}}
        self.metadata.write_text(json.dumps(self.original))
        self.resources = {"installer/resources/payload_metadata.json": json.dumps(self.original).encode(),
                          "installer/resources/payload.zip": self.payload.read_bytes(),
                          "installer/resources/uninstaller.exe": self.remove.read_bytes()}
        active = patch("installer.license_resources.verify_wrapper_licenses", return_value={})
        active.start()
        self.addCleanup(active.stop)

    def verifier(self, path, *, role, version, expected_signer):
        self.assertEqual(expected_signer, SIGNER)
        self.assertEqual(version, VERSION)
        return {"sha256": file_hash(path), "signer_thumbprint": SIGNER, "role": role}

    def payload_verifier(self, _path, *, version):
        self.assertEqual(version, VERSION)
        return {"sha256": self.original["exe_sha256"], "signer_thumbprint": SIGNER}

    def archive(self, _path):
        resources = self.resources
        return type("InertArchive", (), {"toc": resources,
                    "extract": lambda _self, name: resources[name]})()

    def finalize(self, **kwargs):
        return signing.finalize_setup(self.setup, metadata=self.metadata, payload=self.payload,
                  uninstaller=self.remove, version=VERSION, output=self.output, verifier=self.verifier,
                  payload_verifier=kwargs.pop("payload_verifier", self.payload_verifier),
                  archive_factory=self.archive, **kwargs)

    def test_returned_signature_records_final_bytes_and_exact_embedded_inputs(self):
        result = self.finalize()
        self.assertEqual(result["signing_state"], "signed")
        self.assertEqual(result["setup_sha256"], file_hash(self.setup))
        self.assertEqual(json.loads(self.output.read_text()), result)

    def test_changed_embedded_zip_or_uninstaller_is_rejected(self):
        for name in ("installer/resources/payload.zip", "installer/resources/uninstaller.exe"):
            previous = self.resources[name]
            self.resources[name] = b"different"
            with self.subTest(name=name), self.assertRaises(InstallError):
                self.finalize()
            self.resources[name] = previous
            self.assertFalse(self.output.exists())

    def test_changed_external_input_or_pending_metadata_is_rejected(self):
        self.payload.write_bytes(b"wrong ZIP")
        with self.assertRaises(InstallError):
            self.finalize()
        self.assertFalse(self.output.exists())
        self.original["signing_state"] = "unsigned"
        self.metadata.write_text(json.dumps(self.original))
        with self.assertRaises(InstallError):
            self.finalize()

    def test_final_output_is_exclusive_and_previous_report_is_preserved(self):
        self.output.write_text("previous release")
        with self.assertRaises(FileExistsError):
            self.finalize()
        self.assertEqual(self.output.read_text(), "previous release")

    def test_expected_signer_pin_is_enforced_during_finalization(self):
        observed = []
        def pinned_payload(path, **kwargs):
            observed.append(kwargs)
            return self.payload_verifier(path, version=kwargs["version"])
        with self.assertRaises(InstallError):
            self.finalize(expected_signer="C" * 40, payload_verifier=pinned_payload)
        self.assertEqual(observed, [{"version": VERSION, "expected_signer": "C" * 40}])
        self.assertFalse(self.output.exists())
        result = self.finalize(expected_signer=SIGNER.lower(), payload_verifier=pinned_payload)
        self.assertEqual(result["signing_state"], "signed")


class SigningCliTests(unittest.TestCase):
    def test_expected_pin_mismatch_stops_before_local_signing_or_trust_query(self):
        with patch.object(signing, "query_windows_trust") as query, patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
            signing.main(["--sign", "app.exe", "--role", "app", "--version", VERSION,
                          "--signtool", "signtool.exe", "--certificate-thumbprint", SIGNER,
                          "--timestamp-url", "https://timestamp.example", "--expected-signer", "C" * 40])
        self.assertEqual(error.exception.code, 2)
        query.assert_not_called()

    def test_finalize_cli_forwards_expected_signer_pin(self):
        with patch.object(signing, "finalize_setup", return_value={}) as finalize, patch("builtins.print"):
            signing.main(["--finalize-setup", "setup.exe", "--metadata", "pending.json",
                          "--payload", "payload.zip", "--uninstaller", "remove.exe", "--output", "final.json",
                          "--version", VERSION, "--expected-signer", SIGNER])
        self.assertEqual(finalize.call_args.kwargs["expected_signer"], SIGNER)

    def test_incompatible_mode_options_are_rejected(self):
        cases = (["--verify", "app.exe", "--role", "app", "--signtool", "tool.exe"],
                 ["--finalize-setup", "setup.exe", "--role", "setup"])
        for arguments in cases:
            with self.subTest(arguments=arguments), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
                signing.main([*arguments, "--version", VERSION])
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
