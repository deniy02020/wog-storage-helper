"""Authentic notice resources for release/wrapper packaging; no legal inference.

Only the previously collected texts and manifest are used. No files are written,
downloaded or executed. PyInstaller archives are inspected without launching EXEs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from installer.core import InstallError

MANIFEST = "licenses/manifest.json"
NOTICES = "THIRD_PARTY_NOTICES.txt"
COMPONENTS = {"python", "tcl", "tk", "pyinstaller", "numpy", "pillow", "opencv-python-headless"}


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise InstallError("Invalid authentic licence SHA256.")
    return value


def _relative(value):
    if (not isinstance(value, str) or "\\" in value or ":" in value
            or value.startswith("/") or "//" in value):
        raise InstallError("Invalid authentic licence resource path.")
    path = PurePosixPath(value)
    if (len(path.parts) < 3 or path.parts[0] != "licenses"
            or any(part in {".", ".."} for part in value.split("/"))):
        raise InstallError("Invalid authentic licence resource path.")
    return value


def licensing_files(root: Path) -> dict[str, Path]:
    """Return exact source files after checking the collected manifest and bytes."""
    root = Path(root).absolute().resolve()
    manifest_path, notices_path = root / MANIFEST, root / NOTICES
    for path in (manifest_path, notices_path):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise InstallError("Authentic licence collection is missing or linked: " + path.name)
    if manifest_path.stat().st_size > 1_000_000:
        raise InstallError("Authentic licence manifest exceeds its size bound.")
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (ValueError, UnicodeError) as exc:
        raise InstallError("Invalid authentic licence manifest JSON.") from exc
    if (not isinstance(manifest, dict) or manifest.get("schema") != 1
            or manifest.get("status") != "requested_components_collected"
            or manifest.get("missing") != []):
        raise InstallError("Authentic licence collection is incomplete.")
    components, entries = manifest.get("components"), manifest.get("files")
    if (not isinstance(components, list) or not isinstance(entries, list)
            or not 1 <= len(entries) <= 100):
        raise InstallError("Invalid authentic licence component/file list.")
    declared = {}
    for component in components:
        if (not isinstance(component, dict) or not isinstance(component.get("id"), str)
                or component.get("id") not in COMPONENTS
                or component["id"] in declared or not isinstance(component.get("notice_files"), list)
                or not component["notice_files"]):
            raise InstallError("Invalid authentic licence component declaration.")
        references = [_relative(name) for name in component["notice_files"]]
        if len(set(references)) != len(references):
            raise InstallError("Duplicate authentic licence declaration.")
        declared[component["id"]] = set(references)
    if set(declared) != COMPONENTS:
        raise InstallError("Authentic licences for all bundled component families are required.")
    found = {name: set() for name in declared}
    files, seen, total = {MANIFEST: manifest_path, NOTICES: notices_path}, set(), 0
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("component"), str)
                or entry.get("component") not in declared):
            raise InstallError("Invalid authentic licence file entry.")
        name = _relative(entry.get("path"))
        expected_bytes = entry.get("bytes")
        if (name.casefold() in seen or isinstance(expected_bytes, bool)
                or not isinstance(expected_bytes, int) or not 1 <= expected_bytes <= 5_000_000):
            raise InstallError("Duplicate or invalid authentic licence file size.")
        seen.add(name.casefold())
        path = root / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise InstallError("Authentic licence file is missing or linked: " + name)
        content = path.read_bytes()
        total += len(content)
        if len(content) != expected_bytes or hashlib.sha256(content).hexdigest() != _digest(entry.get("sha256")):
            raise InstallError("Authentic licence checksum/size mismatch: " + name)
        files[name] = path
        found[entry["component"]].add(name)
    if total > 10_000_000 or found != declared:
        raise InstallError("Authentic licence declarations differ from collected files.")
    notices = notices_path.read_bytes()
    if len(notices) > 10_000_000 or hashlib.sha256(notices).hexdigest() != _digest(manifest.get("notice_sha256")):
        raise InstallError("Third-party notices checksum mismatch.")
    return files


def licensing_hashes(root: Path) -> dict[str, str]:
    return {name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in licensing_files(root).items()}


def licensing_datas(root: Path) -> list[tuple[str, str]]:
    """PyInstaller data tuples, retaining each original relative licence path."""
    return [(str(path), str(PurePosixPath(name).parent))
            for name, path in sorted(licensing_files(root).items())]


def verify_wrapper_licenses(executable: Path, expected: dict[str, str]) -> dict:
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    names = {name.replace("\\", "/"): name for name in archive.toc}
    if len(names) != len(archive.toc):
        raise InstallError("Duplicate normalized wrapper archive resources.")
    for name, digest in expected.items():
        if name not in names:
            raise InstallError("Wrapper authentic licence resource is missing: " + name)
        if hashlib.sha256(archive.extract(names[name])).hexdigest() != digest:
            raise InstallError("Wrapper authentic licence checksum mismatch: " + name)
    return {"file_count": len(expected), "sha256": dict(expected)}
