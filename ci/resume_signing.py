"""Promote completed, origin-bound signing requests in isolated CI stages."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import zipfile

from ci.signpath import fetch, github_origin
from ci.windows_build import ROOT, VERSION, copy_source, run, sha, workspace, write_json

UNSIGNED_FILES = {"WoGStorageHelper.exe", "WoGStorageHelperUninstall.exe", "WoGStorageHelperSetup.exe",
                  "unsigned-portable.zip", "unsigned-onedir.zip"}
SETUP_FILES = {"signed-inner-portable.zip", "WoGStorageHelperUninstall.exe", "WoGStorageHelperSetup.exe", "setup_metadata.json"}


def hash_records(document, expected, directory):
    if not isinstance(document, dict) or set(document) != expected:
        raise ValueError("Every expected artifact must have one exact hash record")
    for name, record in document.items():
        if (not isinstance(record, dict) or set(record) != {"bytes", "sha256"}
                or isinstance(record.get("bytes"), bool) or not isinstance(record.get("bytes"), int)
                or not 1 <= record["bytes"] <= 500_000_000
                or not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])):
            raise ValueError("Invalid artifact size or SHA256 record")
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != record["bytes"] or sha(path) != record["sha256"]:
            raise ValueError("Artifact changed before signed-stage assembly")


def validate_unsigned(directory, receipt):
    directory = Path(directory)
    document = json.loads((directory / "unsigned-build.json").read_bytes())
    if (document.get("version") != VERSION or document.get("signing_state") != "unsigned"
            or document.get("release_eligible") is not False
            or document.get("source_manifest_sha256") != sha(ROOT / "SOURCE_CONTENT_MANIFEST.json")
            or any(document.get(key) != receipt.get(key) for key in ("repository", "source_sha", "run_id"))):
        raise ValueError("Unsigned artifact metadata is not bound to the submitted source run")
    hash_records(document.get("artifacts"), UNSIGNED_FILES, directory)
    if {path.name for path in directory.iterdir()} != UNSIGNED_FILES | {"unsigned-build.json"}:
        raise ValueError("Unsigned artifact directory has unexpected or missing entries")
    return document


def extract_onedir(archive, destination):
    seen = set()
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("Unsigned onedir CRC mismatch")
        for entry in bundle.infolist():
            name = entry.filename
            if (entry.is_dir() or "\\" in name or ":" in name or name.startswith("/")
                    or any(part in {"", ".", ".."} for part in name.split("/"))
                    or name.casefold() in seen or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("Unsafe onedir artifact path")
            path = (destination / name).resolve()
            if not path.is_relative_to(destination.resolve()):
                raise ValueError("Onedir extraction escaped the isolated stage")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(bundle.read(entry))
            seen.add(name.casefold())


def prepare_setup(unsigned, receipt_path, output):
    from windows_signing import verify_owned_executable
    document = json.loads(Path(receipt_path).read_bytes())
    if set(document.get("requests", {})) != {"app", "uninstaller"}:
        raise ValueError("Both submitted inner-component request IDs are required")
    validate_unsigned(unsigned, document)
    output = workspace(output)
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    copy_source(source)
    signed = output / "signed"
    app = signed / "WoGStorageHelper.exe"
    uninstaller = signed / "WoGStorageHelperUninstall.exe"
    fetch(document, "app", app)
    fetch(document, "uninstaller", uninstaller)
    app_facts = verify_owned_executable(app, role="app", version=VERSION)
    uninstall_facts = verify_owned_executable(uninstaller, role="uninstaller", version=VERSION,
                                               expected_signer=app_facts["signer_thumbprint"])
    bundle = source / "dist/WoGStorageHelper"
    bundle.mkdir(parents=True)
    extract_onedir(Path(unsigned) / "unsigned-onedir.zip", bundle)
    # The input unsigned bundle was independently hash-checked above. Only the
    # origin-verified signed app replaces its own PE before portable packaging.
    shutil.copyfile(app, bundle / app.name)
    if sha(bundle / app.name) != app_facts["sha256"]:
        raise ValueError("Signed app changed before portable packaging")
    run(["package_release.py", "--create"], source)
    payloads = list((source / "releases").glob("*.zip"))
    if len(payloads) != 1:
        raise ValueError("Expected one signed-inner portable payload")
    wrappers = output / "wrappers"
    run(["-m", "installer.build", "--payload", str(payloads[0]), "--signed-uninstaller", str(uninstaller),
         "--require-signed-inputs", "--defer-setup-signing", "--version", VERSION,
         "--output-root", str(wrappers)], source)
    artifacts = output / "artifacts"
    artifacts.mkdir()
    for original, name in ((payloads[0], "signed-inner-portable.zip"), (uninstaller, uninstaller.name),
                           (wrappers / "dist/setup/WoGStorageHelperSetup.exe", "WoGStorageHelperSetup.exe"),
                           (wrappers / "dist/setup/setup_metadata.json", "setup_metadata.json")):
        shutil.copyfile(original, artifacts / name)
    result = {"schema": 1, "version": VERSION, "state": "awaiting_setup_signature", "release_eligible": False,
              **github_origin(os.environ), "source_run_id": document["run_id"],
              "source_manifest_sha256": sha(ROOT / "SOURCE_CONTENT_MANIFEST.json"),
              "inner_signatures": {"app": app_facts, "uninstaller": uninstall_facts},
              "files": {path.name: {"bytes": path.stat().st_size, "sha256": sha(path)}
                        for path in artifacts.iterdir() if path.is_file()}}
    write_json(artifacts / "setup-stage.json", result)
    return result


def finalize_setup(stage, receipt_path, output):
    from windows_signing import finalize_setup as finalize
    stage = Path(stage)
    facts = json.loads((stage / "setup-stage.json").read_bytes())
    receipt = json.loads(Path(receipt_path).read_bytes())
    if (facts.get("state") != "awaiting_setup_signature" or facts.get("release_eligible") is not False
            or facts.get("version") != VERSION or facts.get("source_manifest_sha256") != sha(ROOT / "SOURCE_CONTENT_MANIFEST.json")
            or any(facts.get(key) != receipt.get(key) for key in ("repository", "source_sha", "run_id"))):
        raise ValueError("Setup stage is not bound to the submitted request's source and run")
    hash_records(facts.get("files"), SETUP_FILES, stage)
    if {path.name for path in stage.iterdir()} != SETUP_FILES | {"setup-stage.json"}:
        raise ValueError("Setup stage has unexpected or missing entries")
    output = workspace(output)
    output.mkdir(parents=True, exist_ok=False)
    setup = output / "WoGStorageHelperSetup.exe"
    fetch(receipt, "setup", setup)
    result = finalize(setup, metadata=stage / "setup_metadata.json", payload=stage / "signed-inner-portable.zip",
                      uninstaller=stage / "WoGStorageHelperUninstall.exe", version=VERSION,
                      expected_signer=facts["inner_signatures"]["app"]["signer_thumbprint"],
                      output=output / "final_setup_metadata.json")
    shutil.copyfile(stage / "signed-inner-portable.zip", output / "signed-portable.zip")
    write_json(output / "final-signing-state.json", {"state": "verified_signed_not_published", "version": VERSION,
        **github_origin(os.environ), "setup_sha256": result["setup_sha256"],
        "portable_sha256": sha(output / "signed-portable.zip"), "published": False})
    return {"state": "verified_signed_not_published", "setup_sha256": result["setup_sha256"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    command = sub.add_parser("prepare-setup")
    command.add_argument("--unsigned", type=Path, required=True)
    command = sub.add_parser("finalize-setup")
    command.add_argument("--stage", type=Path, required=True)
    for item in sub.choices.values():
        item.add_argument("--receipt", type=Path, required=True)
        item.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    result = prepare_setup(args.unsigned, args.receipt, args.output_root) if args.phase == "prepare-setup" else finalize_setup(args.stage, args.receipt, args.output_root)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
