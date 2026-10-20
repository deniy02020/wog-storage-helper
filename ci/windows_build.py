"""Isolated CI builds. Unsigned output is evidence, never a public release.

All generated files live in a fresh beta8-preparation workspace. Existing Beta6
and Beta7 bundles, profiles and release archives are never read or overwritten.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
# Running this file directly puts only ci/ on the initial module path. Resolve
# the known adjacent product source before importing its shared build identity.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release_identity import DEFAULT_RELEASE_VERSION

VERSION = DEFAULT_RELEASE_VERSION


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, document):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def relative(name):
    if not isinstance(name, str) or "\\" in name or ":" in name or name.startswith("/"):
        raise ValueError("Unsafe CI source path")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in name.split("/")):
        raise ValueError("Unsafe CI source path components")
    if set(part.casefold() for part in path.parts) & {".git", ".venv", "profile", "data", "verification", "logs", "dist", "build", "releases", "__pycache__"}:
        raise ValueError("Private or generated source directory")
    return path


def workspace(path):
    path = Path(path).resolve()
    base = (ROOT / ".ci-work").resolve()
    if (not path.is_relative_to(base) or path == base
            or path.relative_to(base).parts[0] != "beta8-preparation"):
        raise ValueError("CI output must be a fresh .ci-work/beta8-preparation workspace")
    return path


def copy_source(destination):
    manifest = json.loads((ROOT / "SOURCE_CONTENT_MANIFEST.json").read_bytes())
    copied = set()
    for record in manifest["files"]:
        name = record["path"]
        relative(name)
        if name.casefold() in copied:
            raise ValueError("Duplicate case-insensitive source entry")
        source = ROOT / name
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(ROOT):
            raise ValueError("Missing or linked CI source")
        content = source.read_bytes()
        if len(content) != record["bytes"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise ValueError("Source manifest differs from checkout: " + name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
        copied.add(name.casefold())
    return manifest


def run(command, source, *, version=VERSION):
    subprocess.run([sys.executable, "-B", *command], cwd=source,
                   env=dict(os.environ, WOG_HELPER_BUILD_VERSION=version, PYTHONDONTWRITEBYTECODE="1"), check=True)


def zip_tree(directory, target):
    with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(Path(directory).rglob("*")):
            if path.is_symlink():
                raise ValueError("Linked build output")
            if path.is_file():
                bundle.write(path, path.relative_to(directory).as_posix())
    with zipfile.ZipFile(target) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("CI build archive CRC mismatch")


def load_source_module(source, filename, name):
    spec = importlib.util.spec_from_file_location(name, source / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unsigned(output):
    output = workspace(output)
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    manifest = copy_source(source)
    artifacts = output / "artifacts"
    artifacts.mkdir()
    run(["ocr_resources.py", "--fetch-native"], source)
    run(["-m", "PyInstaller", "--noconfirm", "--clean", "WoGStorageHelper.spec"], source)
    app = source / "dist/WoGStorageHelper/WoGStorageHelper.exe"
    wrappers = output / "wrappers"
    run(["-m", "installer.build", "--stage-uninstaller", "--version", VERSION,
         "--output-root", str(wrappers)], source)
    uninstaller = wrappers / "dist/uninstaller/WoGStorageHelperUninstall.exe"
    # Package from the isolated source tree only. This preserves all _internal
    # dependencies and licences rather than treating a lone EXE as portable.
    run(["package_release.py", "--create"], source)
    payloads = list((source / "releases").glob("*.zip"))
    if len(payloads) != 1:
        raise ValueError("Expected one isolated unsigned portable payload")
    run(["-m", "installer.build", "--payload", str(payloads[0]), "--version", VERSION,
         "--output-root", str(output / "unsigned-setup")], source)
    setup = output / "unsigned-setup/dist/setup/WoGStorageHelperSetup.exe"
    outputs = {"app": app, "uninstaller": uninstaller, "setup": setup, "portable": payloads[0]}
    for role, original in outputs.items():
        name = {"portable": "unsigned-portable.zip"}.get(role, original.name)
        shutil.copyfile(original, artifacts / name)
    zip_tree(source / "dist/WoGStorageHelper", artifacts / "unsigned-onedir.zip")
    result = {"schema": 1, "version": VERSION, "signing_state": "unsigned",
              "release_eligible": False, "source_sha": os.environ.get("GITHUB_SHA"),
              "repository": os.environ.get("GITHUB_REPOSITORY"), "run_id": os.environ.get("GITHUB_RUN_ID"),
              "source_manifest_sha256": sha(ROOT / "SOURCE_CONTENT_MANIFEST.json"),
              "public_test_count": json.loads((ROOT / "PUBLIC_TEST_RESULT.json").read_bytes())["tests"],
              "artifacts": {path.name: {"sha256": sha(path), "bytes": path.stat().st_size}
                            for path in artifacts.iterdir() if path.is_file()}}
    write_json(artifacts / "unsigned-build.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(unsigned(args.output_root), indent=2))


if __name__ == "__main__":
    main()
