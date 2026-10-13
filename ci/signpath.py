"""Strict, resumable SignPath request receipts and downloads.

Submission is performed only by the pinned GitHub Action. This module never
creates requests or installs certificates; pending approval produces no output
binary. Tokens are read from the environment and are never serialized/logged.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
import uuid
import zipfile

from release_identity import DEFAULT_RELEASE_VERSION

CONFIGURATION_KEYS = {"app": "SIGNPATH_APP_CONFIGURATION", "uninstaller": "SIGNPATH_UNINSTALLER_CONFIGURATION",
                      "setup": "SIGNPATH_SETUP_CONFIGURATION"}
FILENAMES = {"app": "WoGStorageHelper.exe", "uninstaller": "WoGStorageHelperUninstall.exe",
             "setup": "WoGStorageHelperSetup.exe"}
API = "https://app.signpath.io/Api/v1/"


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9-]{36}", value):
        raise ValueError("A real SignPath UUID is required")
    return str(uuid.UUID(value))


def slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value):
        raise ValueError("A configured SignPath slug is required")
    return value


def config(environ=None, *, require_token=True):
    env = os.environ if environ is None else environ
    result = {"organization_id": identifier(env.get("SIGNPATH_ORGANIZATION_ID")),
              "project_slug": slug(env.get("SIGNPATH_PROJECT_SLUG")),
              "signing_policy_slug": slug(env.get("SIGNPATH_SIGNING_POLICY_SLUG")),
              "configurations": {role: slug(env.get(key)) for role, key in CONFIGURATION_KEYS.items()}}
    token = env.get("SIGNPATH_API_TOKEN")
    if require_token and (not isinstance(token, str) or not 1 <= len(token) <= 4096
                          or any(not 33 <= ord(character) <= 126 for character in token)):
        raise ValueError("A SignPath API token must be configured as an environment secret")
    return result


def github_origin(env):
    repository, commit, run_id = env.get("GITHUB_REPOSITORY"), env.get("GITHUB_SHA"), env.get("GITHUB_RUN_ID")
    if env.get("SOURCE_CHECKOUT_SHA", commit) != commit:
        raise ValueError("Main advanced since submission; the checkout must equal the actual workflow source commit")
    if (not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit)
            or not isinstance(run_id, str) or not re.fullmatch(r"[1-9][0-9]*", run_id)):
        raise ValueError("Actual GitHub repository, commit and run ID are required")
    if env.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Only an explicit main-branch signing workflow is supported")
    return {"repository": repository, "source_sha": commit, "run_id": run_id, "branch": "main"}


def receipt(environ=None):
    env = os.environ if environ is None else environ
    settings = config(env)
    roles = ("setup",) if env.get("SIGNPATH_SETUP_REQUEST") else ("app", "uninstaller")
    requests = {role: identifier(env["SIGNPATH_" + role.upper() + "_REQUEST"])
                for role in roles if env.get("SIGNPATH_" + role.upper() + "_REQUEST")}
    if not requests:
        raise ValueError("No actual submitted request ID is available")
    return {"schema": 1, "state": "submitted_not_signed", "version": DEFAULT_RELEASE_VERSION,
            **settings, **github_origin(env), "requests": requests}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        raise ValueError("SignPath authenticated requests cannot redirect")


def get(endpoint, *, token, binary=False):
    if not endpoint.startswith(API):
        raise ValueError("Refusing to send credentials to a noncanonical API endpoint")
    request = urllib.request.Request(endpoint, headers={"Authorization": "Bearer " + token,
                                                       "Accept": "application/octet-stream" if binary else "application/json"})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            content = response.read(100_000_001 if binary else 1_000_001)
    except urllib.error.HTTPError as error:
        raise ValueError("SignPath read failed with HTTP status " + str(error.code)) from None
    except urllib.error.URLError:
        raise ValueError("SignPath read failed; no binary was promoted") from None
    if len(content) > (100_000_000 if binary else 1_000_000):
        raise ValueError("SignPath response exceeded its bound")
    return content if binary else json.loads(content)


def verify_request_data(data, document, role):
    if data.get("status") != "Completed" or data.get("isFinalStatus") is not True:
        raise ValueError("Signing is pending or unsuccessful; resume only after approval and completion")
    if (data.get("projectSlug") != document["project_slug"]
            or data.get("signingPolicySlug") != document["signing_policy_slug"]
            or data.get("artifactConfigurationSlug") != document["configurations"][role]):
        raise ValueError("Signing request project, policy or artifact configuration mismatch")
    origin = data.get("origin", {})
    repository = origin.get("repositoryData", {})
    expected_repository = "https://github.com/" + document["repository"]
    if (repository.get("url", "").removesuffix(".git").rstrip("/") != expected_repository
            or repository.get("commitId") != document["source_sha"] or repository.get("branchName") != document["branch"]):
        raise ValueError("Signing request lacks the exact trusted GitHub source origin")
    url = origin.get("buildData", {}).get("url", "")
    expected = expected_repository + "/actions/runs/" + document["run_id"]
    if url != expected and not url.startswith(expected + "/"):
        raise ValueError("Signing request belongs to a different GitHub run")


def fetch(document, role, output, *, environ=None, getter=get):
    env = os.environ if environ is None else environ
    configured = config(env)
    if any(document.get(key) != value for key, value in configured.items()):
        raise ValueError("Receipt differs from the approved signing configuration")
    current = github_origin(env)
    if document.get("repository") != current["repository"] or document.get("source_sha") != current["source_sha"]:
        raise ValueError("Receipt differs from the actual checked-out repository and source commit")
    request_id = identifier(document["requests"][role])
    prefix = API + configured["organization_id"] + "/SigningRequests/" + request_id
    token = env["SIGNPATH_API_TOKEN"]
    data = getter(prefix, token=token)
    verify_request_data(data, document, role)
    content = getter(prefix + "/SignedArtifact", token=token, binary=True)
    # Default GitHub upload artifacts are ZIPs. Extract exactly one owned PE;
    # no server-selected file names, scripts or neighbouring DLLs are trusted.
    from io import BytesIO
    with zipfile.ZipFile(BytesIO(content)) as archive:
        entries = archive.infolist()
        expected = FILENAMES[role]
        if len(entries) != 1 or entries[0].filename != expected or entries[0].file_size > 100_000_000:
            raise ValueError("Signed artifact must contain exactly the requested owned executable")
        if archive.testzip() is not None:
            raise ValueError("Signed artifact CRC mismatch")
        executable = archive.read(expected)
    output = Path(output)
    if output.name != FILENAMES[role] or output.exists():
        raise ValueError("Use a new destination with the exact owned executable name")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        stream.write(executable)
    return {"role": role, "state": "downloaded_requires_windows_verification",
            "sha256": hashlib.sha256(executable).hexdigest(), "request_id": request_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config")
    command = sub.add_parser("receipt")
    command.add_argument("--output", type=Path, required=True)
    command = sub.add_parser("fetch")
    command.add_argument("--receipt", type=Path, required=True)
    command.add_argument("--role", choices=tuple(FILENAMES), required=True)
    command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "config":
        result = config()
    elif args.command == "receipt":
        result = receipt()
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    else:
        result = fetch(json.loads(args.receipt.read_bytes()), args.role, args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
