"""Install/update/rollback/uninstall the real payload only in a new workspace fixture.

No GUI, shortcuts, registry, app launch, or live AppData reads/writes occur here.
The retained synthetic profile and report make the exact check reviewable.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
from uuid import uuid4
from zipfile import ZipFile

from installer.build import INSTALLER, verify_release_payload
from installer.core import (EXECUTABLE, MARKER, PRODUCT_DIRECTORY, UNINSTALLER,
                            InstallError, file_hash, install, payload_files, uninstall)


BETA6_REQUIRED_PRODUCT_MODULES = frozenset({
    "inventory_cycle", "storage_cycle", "storage_pages", "inventory_walk",
    "inventory_scroller", "inventory_viewport", "inventory_motion_policy",
    "supplemental_tiers", "inventory_samples", "ui_journal",
})
PRIVATE_PARTS = frozenset({"verification", "diagnostics", "readback", "diagnostic-frames",
                           "profile", "data", "logs", "tests", "test", ".venv", "__pycache__"})
PRIVATE_PREFIXES = ("native_beta6_debug", "native-inventory-", "native-after-",
                    "native-background-", "private-failure-frame-", "readback-",
                    "calibration-capture-", "tooltip-cell-", "ring-unhovered-")
STOCK_IMAGES = {"_internal/_tk_data/images/logoLarge.gif": (354, 520)}


def verify_payload_privacy(payload: Path):
    """Audit names/images before installation, including inside _internal.

    This does not execute or extract the app. Small private readback crops are
    rejected by location/name as well as full screenshots by their dimensions.
    Provenance strings inside the calibration JSON are data, not bundled paths.
    """
    from PIL import Image
    entries, _total = payload_files(payload)
    image_count = 0
    with ZipFile(payload) as archive:
        for source, relative, _size in entries:
            path = Path(relative)
            parts = {part.casefold() for part in path.parts}
            forbidden = parts & PRIVATE_PARTS
            if relative == "_internal/cv2/data/__init__.py":
                forbidden.discard("data")  # Stock dependency module, not a profile directory.
            if (forbidden or path.name.casefold().startswith(PRIVATE_PREFIXES)
                    or any(part.startswith(("factory-native-", "native-idle-probe-", "native-layout-")) for part in parts)
                    or path.name.casefold().startswith("test_")):
                raise InstallError("Private/development release resource: " + relative)
            if path.suffix.lower() in {".png", ".ico", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}:
                try:
                    with Image.open(BytesIO(archive.read(source))) as image:
                        dimensions = image.size
                except (OSError, ValueError) as error:
                    raise InstallError("Invalid release image: " + relative) from error
                if ((dimensions[0] > 512 or dimensions[1] > 512)
                        and STOCK_IMAGES.get(relative) != dimensions):
                    raise InstallError("Full-size private image in release: " + relative)
                image_count += 1
    return {"files_checked": len(entries), "images_checked": image_count,
            "private_resources": [], "native_execution": False}


def verify_product_modules(executable: Path, *, reader_factory=None):
    """Inspect the product PYZ module names without executing frozen code."""
    if reader_factory is None:
        from PyInstaller.archive.readers import CArchiveReader
        reader_factory = CArchiveReader
    archive = reader_factory(str(executable))
    pyz_names = [name for name in archive.toc if name.replace("\\", "/").lower().endswith(".pyz")]
    if len(pyz_names) != 1:
        raise InstallError("Product must contain one unambiguous PYZ archive")
    pyz = archive.open_embedded_archive(pyz_names[0])
    modules = set(pyz.toc)
    private = sorted(name for name in modules if name.split(".", 1)[0] in PRIVATE_PARTS
                     or any(part.startswith(("test_", "native_beta6_debug")) for part in name.split(".")))
    if private:
        raise InstallError("Private/development product modules: " + ", ".join(private))
    if missing := BETA6_REQUIRED_PRODUCT_MODULES - modules:
        raise InstallError("Missing beta6 product modules: " + ", ".join(sorted(missing)))
    return {"required_modules": sorted(BETA6_REQUIRED_PRODUCT_MODULES),
            "pyz_modules": len(modules), "private_probe_modules": [], "native_execution": False}


def verify_payload_product_modules(payload: Path):
    """Temporarily materialize only executable bytes for the read-only reader."""
    with ZipFile(payload) as archive:
        content = archive.read(PRODUCT_DIRECTORY + "/" + EXECUTABLE)
    with tempfile.TemporaryDirectory(prefix=".test-product-audit-", dir=INSTALLER) as directory:
        executable = Path(directory) / EXECUTABLE
        executable.write_bytes(content)
        return verify_product_modules(executable)


def check(payload: Path, uninstaller: Path, *, require_beta6_modules=False):
    payload, uninstaller = Path(payload).absolute(), Path(uninstaller).absolute()
    if not payload.is_file() or not uninstaller.is_file():
        raise FileNotFoundError("The verified payload/uninstaller input is missing.")
    facts = verify_release_payload(payload)
    privacy = verify_payload_privacy(payload)
    # Opt in for the new product, so historical beta5 verification remains a
    # separate record rather than being judged by modules that did not exist.
    modules = verify_payload_product_modules(payload) if require_beta6_modules else None
    payload_hash, uninstaller_hash = file_hash(payload), file_hash(uninstaller)
    # Keep this workspace-only root short: stock NumPy license paths otherwise
    # exceed MAX_PATH inside the deliberately deep project checkout in source
    # Python. Do not change Windows settings or the production target guards.
    root = INSTALLER / ".it"
    # Never reuse or clear an old verification directory.
    fixture = root / uuid4().hex[:8]
    if fixture.resolve().parent != root.resolve() or fixture.exists():
        raise RuntimeError("Unsafe or existing verification fixture.")
    parent = fixture / "P"
    target = parent / PRODUCT_DIRECTORY
    profile = fixture / "synthetic-profile" / PRODUCT_DIRECTORY
    profile.mkdir(parents=True)
    settings = profile / "settings.json"
    settings.write_bytes(b'{"fixture_only":true,"user_override":"must-survive"}\n')
    profile_hash = file_hash(settings)

    def unchanged():
        if file_hash(settings) != profile_hash:
            raise AssertionError("Synthetic user profile was changed.")
        if list(parent.glob(".wog-*")):
            raise AssertionError("Transaction stage/backup was left behind.")

    def installed_hashes():
        record = json.loads((target / MARKER).read_text(encoding="utf-8"))
        for name, expected in record["files"].items():
            if file_hash(target / name) != expected:
                raise AssertionError("Installed file SHA mismatch: " + name)
        if record["files"][EXECUTABLE] != facts["exe_sha256"] or record["files"][UNINSTALLER] != uninstaller_hash:
            raise AssertionError("Installed executable/uninstaller differs from the final source.")
        for name, digest in facts["license_sha256"].items():
            if record["files"].get("_internal/" + name) != digest:
                raise AssertionError("Installed authentic licence differs from the final source: " + name)
        if record["files"].get("THIRD_PARTY_NOTICES.txt") != facts["license_sha256"]["THIRD_PARTY_NOTICES.txt"]:
            raise AssertionError("Installed release notices differ from the final source.")
        return record

    print("Real payload: fresh isolated install", flush=True)
    first = install(payload, target, parent, profile=profile, expected_sha256=payload_hash, uninstaller=uninstaller)
    original_record = installed_hashes()
    unchanged()
    personal = target / "synthetic-personal-notes.txt"
    personal.write_bytes(b"Unmanaged personal file must survive update and uninstall.\n")
    personal_hash = file_hash(personal)

    print("Real payload: failed update restores exact owned program", flush=True)
    def refuse(_target, _previous):
        raise OSError("Intentional isolated fixture failure after publish")
    try:
        install(payload, target, parent, profile=profile, expected_sha256=payload_hash,
                uninstaller=uninstaller, integrate=refuse)
    except OSError as exc:
        if str(exc) != "Intentional isolated fixture failure after publish":
            raise
    else:
        raise AssertionError("The simulated failed update unexpectedly succeeded.")
    if installed_hashes() != original_record or file_hash(personal) != personal_hash:
        raise AssertionError("The update rollback changed prior program or personal files.")
    unchanged()

    print("Real payload: successful isolated update", flush=True)
    second = install(payload, target, parent, profile=profile, expected_sha256=payload_hash, uninstaller=uninstaller)
    record = installed_hashes()
    if not second.upgraded or file_hash(personal) != personal_hash:
        raise AssertionError("The update did not preserve its unmanaged data.")
    unchanged()

    print("Real payload: uninstall preserving synthetic profile and personal file", flush=True)
    result = uninstall(target, parent, profile=profile)
    for name in record["files"]:
        if (target / name).exists():
            raise AssertionError("Owned file remains after uninstall: " + name)
    if (target / MARKER).exists() or result.deferred or result.retained or file_hash(personal) != personal_hash:
        raise AssertionError("Unexpected uninstall ownership/preservation result.")
    unchanged()
    report = {"schema": 1, "verified_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "fixture": str(fixture), "payload": payload.name, "payload_sha256": payload_hash,
              "exe_sha256": facts["exe_sha256"], "uninstaller_sha256": uninstaller_hash,
              "authentic_license_resources": facts["authentic_license_resources"],
              "license_sha256": facts["license_sha256"],
              "installed_owned_files": first.file_count, "fresh_install": True,
              "failed_update_rollback": True, "successful_update": True, "uninstall_owned_files_removed": True,
              "synthetic_profile_sha256": profile_hash, "synthetic_profile_preserved": True,
              "unmanaged_file_preserved": True, "os_integrations": False, "live_appdata_accessed": False,
              "application_launched": False, "privacy_audit": privacy}
    if modules is not None:
        report["product_module_audit"] = modules
    (fixture / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    output = INSTALLER / "dist" / "setup" / "integration_metadata.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify the real payload only in a new installer workspace fixture.")
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--uninstaller", required=True, type=Path)
    parser.add_argument("--require-beta6-product-modules", action="store_true",
                        help="Inspect the product PYZ for all new beta6 modules and reject private probes.")
    args = parser.parse_args(argv)
    check(args.payload, args.uninstaller, require_beta6_modules=args.require_beta6_product_modules)


if __name__ == "__main__":
    main()
