"""Bounded installation transactions, independent of Tk and live Windows input."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from typing import Callable
from uuid import uuid4
from zipfile import ZipFile

PRODUCT_DIRECTORY = "WoGStorageHelper"
PRODUCT_ID = "wog-storage-helper"
MARKER = ".wog-storage-helper-install.json"
EXECUTABLE = "WoGStorageHelper.exe"
UNINSTALLER = "Uninstall.exe"
MAX_FILES = 10000
MAX_BYTES = 1024 ** 3
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


class InstallError(RuntimeError):
    pass


class InstallCancelled(InstallError):
    pass


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def default_parent(environ=None) -> Path:
    value = (os.environ if environ is None else environ).get("LOCALAPPDATA", "")
    if not value or not Path(value).is_absolute():
        raise InstallError("LOCALAPPDATA is unavailable or invalid.")
    return Path(value) / "Programs"


def safe_relative(name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name:
        raise InstallError("Invalid installation file path.")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(p in {"", ".", ".."} for p in name.split("/")):
        raise InstallError("Installation path escapes its directory.")
    for part in pure.parts:
        if (any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or part.endswith((".", " ")) or RESERVED.match(part)):
            raise InstallError("Invalid Windows installation filename.")
    return Path(*pure.parts)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def validate_target(target: Path, parent: Path, profile: Path | None = None) -> Path:
    target, parent = Path(target).absolute(), Path(parent).absolute()
    if parent == Path(parent.anchor) or target != parent / PRODUCT_DIRECTORY:
        raise InstallError("Only the dedicated Helper child directory may be managed.")
    if _is_link(parent) or _is_link(target):
        raise InstallError("Linked installation directories are not supported.")
    canonical = target.resolve()
    if canonical.parent != parent.resolve() or canonical.name != PRODUCT_DIRECTORY:
        raise InstallError("Installation directory resolves outside its parent.")
    if profile is not None:
        protected = Path(profile).resolve()
        if (canonical == protected or canonical.is_relative_to(protected)
                or protected.is_relative_to(canonical)):
            raise InstallError("The user profile must remain separate from the application.")
    return canonical


def _owned_record(target: Path) -> dict:
    marker = target / MARKER
    if not marker.is_file() or _is_link(marker):
        raise InstallError("Existing directory is not owned by this installer.")
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
        if record.get("product") != PRODUCT_ID or record.get("schema") != 1:
            raise ValueError("Wrong product record")
        files = record["files"]
        if not isinstance(files, dict) or EXECUTABLE not in files:
            raise ValueError("Missing owned file manifest")
        for name, digest in files.items():
            safe_relative(name)
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("Invalid hash")
        return record
    except (ValueError, KeyError, TypeError) as exc:
        raise InstallError("Invalid installation ownership record.") from exc


def payload_files(payload: Path, expected_sha256: str | None = None):
    if expected_sha256 and file_hash(payload) != expected_sha256:
        raise InstallError("Installer payload checksum mismatch.")
    names, result, total = set(), [], 0
    with ZipFile(payload) as archive:
        for entry in archive.infolist():
            # ZipInfo normalizes Windows backslashes and truncates NUL characters.
            # Validate the original central-directory spelling before trusting it.
            path = safe_relative(entry.orig_filename.rstrip("/"))
            if path.parts[0] != PRODUCT_DIRECTORY:
                raise InstallError("Unexpected payload root directory.")
            if entry.is_dir():
                continue
            if len(path.parts) < 2 or entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                raise InstallError("Encrypted, linked or invalid payload entry.")
            relative = Path(*path.parts[1:]).as_posix()
            if relative.casefold() in names or relative.casefold() in {MARKER.casefold(), UNINSTALLER.casefold()}:
                raise InstallError("Duplicate or reserved installation payload filename.")
            names.add(relative.casefold())
            total += entry.file_size
            result.append((entry.filename, relative, entry.file_size))
        if len(result) > MAX_FILES or total > MAX_BYTES:
            raise InstallError("Payload exceeds bounded installation size.")
        if EXECUTABLE.casefold() not in names or not any(n.startswith("_internal/") for n in names):
            raise InstallError("The complete onedir application is missing.")
    return result, total


@dataclass
class IntegrationReceipt:
    state: dict
    undo: Callable[[], None]


@dataclass(frozen=True)
class InstallResult:
    target: Path
    file_count: int
    upgraded: bool


def _remove_transaction_directory(path: Path, parent: Path):
    if (path.parent.resolve() != parent.resolve() or _is_link(path)
            or not path.name.startswith((".wog-stage-", ".wog-backup-", ".wog-remove-"))):
        raise InstallError("Unsafe transaction cleanup directory.")
    if path.exists():
        shutil.rmtree(path)


def install(payload: Path, target: Path, parent: Path, *, profile: Path | None = None,
            expected_sha256: str | None = None, uninstaller: Path | None = None,
            progress=lambda _fraction: None, cancelled=lambda: False,
            integrate=lambda _target, _old: IntegrationReceipt({}, lambda: None)) -> InstallResult:
    target = validate_target(target, parent, profile)
    parent = target.parent
    entries, total = payload_files(Path(payload), expected_sha256)
    if target.exists() and not target.is_dir():
        raise InstallError("Installation destination is a file.")
    previous = _owned_record(target) if target.exists() else None
    parent.mkdir(parents=True, exist_ok=True)
    extra_bytes = Path(uninstaller).stat().st_size if uninstaller is not None else 0
    if shutil.disk_usage(parent).free < total + extra_bytes + 20 * 1024 ** 2:
        raise InstallError("Not enough free space for safe installation.")
    stage = parent / f".wog-stage-{uuid4().hex}"
    backup = parent / f".wog-backup-{uuid4().hex}"
    stage.mkdir()
    receipt, published, backed_up = None, False, False
    hashes = {}
    try:
        completed = 0
        with ZipFile(payload) as archive:
            for source_name, relative, _size in entries:
                destination = stage / safe_relative(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with archive.open(source_name) as source, destination.open("xb") as output:
                    while block := source.read(1024 * 1024):
                        if cancelled():
                            raise InstallCancelled("Installation cancelled.")
                        output.write(block)
                        digest.update(block)
                        completed += len(block)
                        progress(min(0.85, completed / max(1, total) * 0.85))
                hashes[relative] = digest.hexdigest()
        if uninstaller is not None:
            shutil.copyfile(uninstaller, stage / UNINSTALLER)
            hashes[UNINSTALLER] = file_hash(stage / UNINSTALLER)
        # Preserve files that were never managed by us (including portable data).
        if previous:
            for source in target.rglob("*"):
                if _is_link(source):
                    raise InstallError("Existing installation contains a linked entry.")
                if not source.is_file():
                    continue
                relative = source.relative_to(target).as_posix()
                if relative in previous["files"] or relative == MARKER:
                    continue
                destination = stage / safe_relative(relative)
                if destination.exists():
                    raise InstallError("A new program file would overwrite unmanaged user data.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        if cancelled():
            raise InstallCancelled("Installation cancelled.")
        if previous:
            target.rename(backup)
            backed_up = True
        stage.rename(target)
        published = True
        receipt = integrate(target, previous)
        record = {"schema": 1, "product": PRODUCT_ID, "files": hashes, "integration": receipt.state}
        (target / MARKER).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        progress(1.0)
    except Exception as failure:
        integration_error = None
        if receipt is not None:
            try:
                receipt.undo()
            except Exception as exc:
                integration_error = exc
        if published:
            target.rename(stage)
        if backed_up:
            backup.rename(target)
        _remove_transaction_directory(stage, parent)
        if integration_error is not None:
            raise InstallError("Program restored, but external integration rollback failed.") from failure
        raise
    if backed_up:
        _remove_transaction_directory(backup, parent)
    return InstallResult(target, len(hashes), previous is not None)


@dataclass(frozen=True)
class UninstallResult:
    target: Path
    retained: tuple[str, ...]
    deferred: dict[str, str]


def uninstall(target: Path, parent: Path, *, profile: Path | None = None,
              defer_uninstaller=False, progress=lambda _fraction: None,
              integrate=lambda _target, _old: IntegrationReceipt({}, lambda: None)) -> UninstallResult:
    target = validate_target(target, parent, profile)
    record = _owned_record(target)
    for path in target.rglob("*"):
        if _is_link(path):
            raise InstallError("Linked installation entries cannot be uninstalled.")
    trash = target.parent / f".wog-remove-{uuid4().hex}"
    trash.mkdir()
    moved, retained, deferred, receipt = [], [], {}, None
    try:
        for index, (relative, expected) in enumerate(record["files"].items()):
            source = target / safe_relative(relative)
            if not source.exists():
                continue
            if not source.is_file() or file_hash(source) != expected:
                retained.append(relative)
                continue
            if relative == UNINSTALLER and defer_uninstaller:
                deferred[relative] = expected
                continue
            destination = trash / safe_relative(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            moved.append((source, destination))
            progress((index + 1) / max(1, len(record["files"])) * 0.85)
        if deferred:
            deferred[MARKER] = file_hash(target / MARKER)
        else:
            (target / MARKER).rename(trash / MARKER)
            moved.append((target / MARKER, trash / MARKER))
        receipt = integrate(target, record)
    except Exception:
        if receipt is not None:
            receipt.undo()
        for source, destination in reversed(moved):
            source.parent.mkdir(parents=True, exist_ok=True)
            destination.rename(source)
        _remove_transaction_directory(trash, target.parent)
        raise
    _remove_transaction_directory(trash, target.parent)
    for directory in sorted((p for p in target.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        target.rmdir()
    except OSError:
        pass
    progress(1.0)
    return UninstallResult(target, tuple(retained), deferred)
