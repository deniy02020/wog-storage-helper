"""Synthetic CI boundaries only: no network, compiler, signing or game input."""
from io import BytesIO
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ci import signpath
from ci.resume_signing import UNSIGNED_FILES, hash_records
from ci.windows_build import VERSION, relative, workspace
from release_identity import DEFAULT_RELEASE_VERSION


def environment():
    return {"SIGNPATH_ORGANIZATION_ID": "12345678-1234-1234-1234-123456789abc",
            "SIGNPATH_PROJECT_SLUG": "synthetic-project", "SIGNPATH_SIGNING_POLICY_SLUG": "synthetic-policy",
            "SIGNPATH_APP_CONFIGURATION": "synthetic-app", "SIGNPATH_UNINSTALLER_CONFIGURATION": "synthetic-remove",
            "SIGNPATH_SETUP_CONFIGURATION": "synthetic-setup", "SIGNPATH_API_TOKEN": "inert-test-token",
            "SIGNPATH_APP_REQUEST": "11111111-1111-1111-1111-111111111111",
            "SIGNPATH_UNINSTALLER_REQUEST": "22222222-2222-2222-2222-222222222222",
            "GITHUB_REPOSITORY": "example/synthetic", "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123",
            "GITHUB_REF": "refs/heads/main"}


def completed(receipt):
    return {"status": "Completed", "isFinalStatus": True, "projectSlug": receipt["project_slug"],
            "signingPolicySlug": receipt["signing_policy_slug"], "artifactConfigurationSlug": receipt["configurations"]["app"],
            "origin": {"repositoryData": {"url": "https://github.com/" + receipt["repository"],
                                          "commitId": receipt["source_sha"], "branchName": "main"},
                       "buildData": {"url": "https://github.com/" + receipt["repository"] + "/actions/runs/123"}}}


class PipelineTests(unittest.TestCase):
    def test_help_works_by_direct_script_from_external_cwd_and_by_module(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory(prefix=".test-ci-cli-", dir=root) as external:
            cases = (([sys.executable, "-B", str(root / "ci/windows_build.py"), "--help"], external),
                     ([sys.executable, "-B", "-m", "ci.windows_build", "--help"], root))
            for command, directory in cases:
                with self.subTest(command=command[2]):
                    result = subprocess.run(command, cwd=directory, env=env,
                        capture_output=True, text=True, encoding="utf-8", timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("--output-root", result.stdout)
                    self.assertEqual(list(Path(external).iterdir()), [])

    def test_missing_configuration_stops_without_request_or_secret_output(self):
        for key in environment():
            if key.startswith("SIGNPATH_") and "REQUEST" not in key:
                env = environment()
                env.pop(key)
                with self.subTest(key=key), self.assertRaises(ValueError):
                    signpath.config(env)

    def test_receipt_is_pending_and_never_serializes_token(self):
        env = environment()
        document = signpath.receipt(env)
        self.assertEqual(document["state"], "submitted_not_signed")
        self.assertEqual(document["version"], VERSION)
        self.assertEqual(VERSION, DEFAULT_RELEASE_VERSION)
        self.assertNotIn(env["SIGNPATH_API_TOKEN"], json.dumps(document))
        self.assertEqual(set(document["requests"]), {"app", "uninstaller"})

    def test_token_controls_whitespace_nonascii_are_rejected_without_echo(self):
        for token in ("a\nb", "a\rb", "a b", "\ta", "é", ""):
            env = dict(environment(), SIGNPATH_API_TOKEN=token)
            with self.subTest(token=repr(token)), self.assertRaises(ValueError) as caught:
                signpath.config(env)
            if token:
                self.assertNotIn(token, str(caught.exception))

    def test_checkout_override_and_nonmain_context_are_rejected(self):
        for update in ({"SOURCE_CHECKOUT_SHA": "b" * 40}, {"GITHUB_REF": "refs/heads/feature"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                signpath.github_origin(dict(environment(), **update))

    def test_pending_or_failed_status_never_downloads_or_writes_binary(self):
        env = environment()
        document = signpath.receipt(env)
        for status in ("WaitingForApproval", "InProgress", "Failed", "Denied", "Canceled"):
            calls = []
            def getter(url, **kwargs):
                calls.append((url, kwargs))
                return dict(completed(document), status=status, isFinalStatus=status in {"Failed", "Denied", "Canceled"})
            with tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary) / "WoGStorageHelper.exe"
                with self.subTest(status=status), self.assertRaises(ValueError):
                    signpath.fetch(document, "app", target, environ=env, getter=getter)
                self.assertFalse(target.exists())
                self.assertEqual(len(calls), 1)

    def test_wrong_project_policy_configuration_or_origin_stops_before_binary(self):
        document = signpath.receipt(environment())
        for key in ("projectSlug", "signingPolicySlug", "artifactConfigurationSlug"):
            data = dict(completed(document), **{key: "wrong"})
            with self.subTest(key=key), self.assertRaises(ValueError):
                signpath.verify_request_data(data, document, "app")
        for key in ("url", "commitId", "branchName"):
            data = copy.deepcopy(completed(document))
            data["origin"]["repositoryData"][key] = "wrong"
            with self.subTest(key=key), self.assertRaises(ValueError):
                signpath.verify_request_data(data, document, "app")
        data = copy.deepcopy(completed(document))
        data["origin"]["buildData"]["url"] += "4"
        with self.assertRaises(ValueError):
            signpath.verify_request_data(data, document, "app")

    def test_missing_or_extra_artifact_hash_records_are_rejected(self):
        for records in ({}, {name: {} for name in UNSIGNED_FILES}, {"unexpected.exe": {}}):
            with self.subTest(records=records), self.assertRaises(ValueError):
                hash_records(records, UNSIGNED_FILES, Path("synthetic-unopened"))

    def test_build_workspace_cannot_touch_old_dist_or_parent_project(self):
        for name in ("dist", ".ci-work", "../other", ".ci-work/wrong-name", ".ci-work/beta7-preparation",
                     ".ci-work/beta7-preparation/beta8-preparation"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                workspace(Path(name))
        self.assertEqual(workspace(Path(".ci-work/beta8-preparation")).name, "beta8-preparation")

    def test_private_source_names_and_traversal_are_rejected(self):
        for name in ("profile/settings.json", ".git/config", "verification/frame.png", "../app.py", "data/x", "a\\x"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                relative(name)

    def test_all_external_actions_are_pinned_to_verified_full_commits(self):
        root = Path(__file__).resolve().parents[1]
        pins = json.loads((root / "ci/actions-pins.json").read_bytes())["actions"]
        for workflow in (root / ".github/workflows").glob("*.yml"):
            for action, revision in re.findall(r"uses:\s*([^@\s]+)@([^\s#]+)", workflow.read_text(encoding="utf-8")):
                with self.subTest(action=action, revision=revision):
                    self.assertRegex(revision, r"^[a-f0-9]{40}$")
                    self.assertEqual(revision, pins[action]["sha"])

    def test_signed_download_can_never_promote_sibling_executables(self):
        env = environment()
        document = signpath.receipt(env)
        stream = BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("WoGStorageHelper.exe", b"synthetic-not-executable")
            archive.writestr("unexpected.exe", b"synthetic")
        def getter(url, **kwargs):
            return stream.getvalue() if kwargs.get("binary") else completed(document)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "WoGStorageHelper.exe"
            with self.assertRaises(ValueError):
                signpath.fetch(document, "app", target, environ=env, getter=getter)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
