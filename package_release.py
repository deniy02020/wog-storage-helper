"""Create and verify a local onedir release after final build/docs are ready.

No application launch, installation, rebuild, upload or publication is performed.
The archive is created exclusively; previous releases are never overwritten.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile

from PIL import Image
from installer.license_resources import licensing_hashes
from installer.build import verify_header_parking_resources


ROOT = Path(__file__).resolve().parent
DOCUMENTS = ("README.md", "LICENSE", "AUTHORS.md", "ATTRIBUTION.md", "docs/STATE.md",
             "docs/STEAM_RELEASE.md", "preset_assets/CALIBRATION.md", "THIRD_PARTY_NOTICES.txt")
BRAND_FILES = ("wog_storage_helper.png", "wog_storage_helper.ico")
FORBIDDEN_PARTS = {"profile", "data", "logs", "tests", "test", ".venv", "__pycache__"}
STOCK_TK_IMAGES = {"_internal/_tk_data/images/logoLarge.gif": (354, 520)}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_sha256(archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name) as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def control_images(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "image" and isinstance(child, str):
                yield child
            else:
                yield from control_images(child)
    elif isinstance(value, list):
        for child in value:
            yield from control_images(child)


def required_samples(reference: dict) -> set[str]:
    names = set()
    for field in ("tiers", "provisional_equipped_tiers"):
        for samples in reference.get(field, {}).values():
            names.update(samples)
    for field in ("inventory_empty", "storage_empty", "equipment_label_samples"):
        names.update(reference[field])
    names.update(anchor["image"] for anchor in reference["anchors"])
    for field in ("active_page_image", "inactive_page_image",
                  "page_active_sample", "page_inactive_sample"):
        if reference.get(field):
            names.add(reference[field])
    names.update(control_images(reference.get("sort_button", {})))
    names.update(control_images(reference.get("panel_controls", {})))
    for name in names:
        if (not isinstance(name, str) or Path(name).name != name or
                "/" in name or "\\" in name or Path(name).suffix.lower() != ".png"):
            raise ValueError(f"Invalid local calibration sample name: {name!r}")
    return names


def check_payload_name(relative: str):
    path = Path(relative)
    parts = {part.casefold() for part in path.parts}
    forbidden = parts & FORBIDDEN_PARTS
    if relative == "_internal/cv2/data/__init__.py":
        forbidden.discard("data")  # Stock package module, not the Helper's user data directory.
    if forbidden:
        raise ValueError(f"Private/development payload found: {relative}")
    name = path.name.casefold()
    if (name.startswith("test_") or name.endswith((".log", ".bak", ".stderr", ".stdout"))
            or name.startswith(("tooltip-cell-", "calibration-capture-", "ring-unhovered-"))):
        raise ValueError(f"Diagnostic/development payload found: {relative}")


def create_release() -> dict:
    bundle = ROOT / "dist" / "WoGStorageHelper"
    releases = ROOT / "releases"
    executable = bundle / "WoGStorageHelper.exe"
    if not executable.is_file():
        raise FileNotFoundError("Build dist/WoGStorageHelper/WoGStorageHelper.exe first.")
    now = datetime.now(timezone.utc)
    basename = f"WoGStorageHelper-{now:%Y-%m-%d}-beta-automatic-{now:%H%M%SZ}"
    releases.mkdir(exist_ok=True)
    target = releases / f"{basename}.zip"
    partial = releases / f"{basename}.partial.zip"
    metadata_path = releases / f"{basename}.metadata.json"
    if any(path.exists() for path in (target, partial, metadata_path)):
        raise FileExistsError("Release timestamp already exists; nothing will be overwritten.")
    prior_archives = {path.name: sha256(path) for path in releases.glob("*.zip")}

    files = {}
    largest_sample = {"dimensions": [0, 0], "path": ""}
    image_suffixes = {".png", ".ico", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
    for source in sorted(bundle.rglob("*")):
        if source.is_symlink() or not source.resolve().is_relative_to(bundle.resolve()):
            raise ValueError(f"External bundle link found: {source}")
        if not source.is_file():
            continue
        relative = source.relative_to(bundle).as_posix()
        check_payload_name(relative)
        if source.suffix.lower() in image_suffixes:
            with Image.open(source) as sample:
                width, height = sample.size
            if ((width > 512 or height > 512)
                    and STOCK_TK_IMAGES.get(relative) != (width, height)):
                raise ValueError(f"Full-size image must not be shipped: {relative} ({width}x{height})")
            if relative.startswith("_internal/preset_assets/"):
                old = largest_sample["dimensions"]
                if width * height > old[0] * old[1]:
                    largest_sample = {"dimensions": [width, height], "path": relative}
        files[relative] = source
    original_count = len(files)
    for relative in DOCUMENTS:
        source = ROOT / relative
        if not source.is_file():
            raise FileNotFoundError(f"Required release document missing: {relative}")
        files[relative] = source

    authentic_licenses = licensing_hashes(ROOT)
    for name, expected in authentic_licenses.items():
        relative = "_internal/" + name
        if relative not in files or sha256(files[relative]) != expected:
            raise ValueError("Built authentic licence resource missing or stale: " + name)

    reference_relative = "_internal/preset_assets/reference.json"
    reference_path = files[reference_relative]
    if sha256(reference_path) != sha256(ROOT / "preset_assets" / "reference.json"):
        raise ValueError("Built reference differs from the final source reference; rebuild first.")
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    samples = required_samples(reference)
    adaptive_relative = "_internal/preset_assets/adaptive_tiers.json"
    adaptive_path = files.get(adaptive_relative)
    if adaptive_path is None or sha256(adaptive_path) != sha256(ROOT / "preset_assets/adaptive_tiers.json"):
        raise ValueError("Built adaptive tier model missing or stale; rebuild first.")
    adaptive = json.loads(adaptive_path.read_text(encoding="utf-8"))
    adaptive_samples = set(control_images(adaptive))
    if adaptive.get("schema") != 1 or not adaptive_samples:
        raise ValueError("Invalid adaptive tier model.")
    for name in adaptive_samples:
        if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name or Path(name).suffix.lower() != ".png":
            raise ValueError(f"Invalid adaptive sample name: {name!r}")
    samples.update(adaptive_samples)
    ui_relative = "_internal/preset_assets/adaptive_ui.json"
    ui_path = files.get(ui_relative)
    if ui_path is None or sha256(ui_path) != sha256(ROOT / "preset_assets/adaptive_ui.json"):
        raise ValueError("Built adaptive UI model missing or stale; rebuild first.")
    ui = json.loads(ui_path.read_text(encoding="utf-8"))
    ui_samples = set(control_images(ui))
    if ui.get("schema") != 1 or not ui_samples:
        raise ValueError("Invalid adaptive UI model.")
    for name in ui_samples:
        if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name or Path(name).suffix.lower() != ".png":
            raise ValueError(f"Invalid adaptive UI sample name: {name!r}")
    samples.update(ui_samples)
    parking_relative = "_internal/preset_assets/header_parking.json"
    parking_path = files.get(parking_relative)
    if parking_path is None or sha256(parking_path) != sha256(ROOT / "preset_assets/header_parking.json"):
        raise ValueError("Built header parking model missing or stale; rebuild first.")
    parking = json.loads(parking_path.read_text(encoding="utf-8"))
    parking_samples = verify_header_parking_resources(parking,
        lambda name:files[f"_internal/preset_assets/{name}"].read_bytes())
    samples.update(parking_samples)
    for records in list(adaptive["tiers"].values()) + [adaptive["backgrounds"], ui["headers"], ui["controls"]]:
        for record in records:
            expected = record.get("sha256", record.get("sample_sha256"))
            if not isinstance(expected, str) or sha256(ROOT / "preset_assets" / record["image"]) != expected:
                raise ValueError(f"Adaptive resource checksum differs from manifest: {record['image']}")
    for relative in ("_internal/locales.json",):
        source = ROOT / relative.removeprefix("_internal/")
        if relative not in files or sha256(files[relative]) != sha256(source):
            raise ValueError(f"Built resource missing or stale: {relative}")
    for name in samples:
        relative = f"_internal/preset_assets/{name}"
        if relative not in files:
            raise FileNotFoundError(f"Referenced sample missing from onedir: {relative}")
        if sha256(files[relative]) != sha256(ROOT / "preset_assets" / name):
            raise ValueError(f"Built calibration sample differs from source: {name}")
    for name in BRAND_FILES:
        relative = f"_internal/assets/{name}"
        if relative not in files or sha256(files[relative]) != sha256(ROOT / "assets" / name):
            raise ValueError(f"Built own icon missing or stale: {name}")
    with Image.open(files["_internal/assets/wog_storage_helper.png"]) as icon:
        if icon.size != (256, 256):
            raise ValueError("Brand PNG must be 256x256.")
    with Image.open(files["_internal/assets/wog_storage_helper.ico"]) as icon:
        expected = {(n, n) for n in (16, 24, 32, 48, 64, 128, 256)}
        if icon.ico.sizes() != expected:
            raise ValueError("Brand ICO must contain all seven requested resolutions.")

    source_hashes = {relative: sha256(source) for relative, source in files.items()}
    with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative, source in sorted(files.items()):
            archive.write(source, f"WoGStorageHelper/{relative}")
    with zipfile.ZipFile(partial) as archive:
        entries = archive.namelist()
        if len(entries) != len(set(entries)) or len(entries) != len(files):
            raise ValueError("Duplicate/missing archive entries.")
        broken = archive.testzip()
        if broken is not None:
            raise ValueError(f"CRC check failed: {broken}")
        for relative, expected_hash in source_hashes.items():
            if archive_sha256(archive, f"WoGStorageHelper/{relative}") != expected_hash:
                raise ValueError(f"Archived bytes differ from source: {relative}")
        uncompressed_bytes = sum(info.file_size for info in archive.infolist())
    for relative, source in files.items():
        if sha256(source) != source_hashes[relative]:
            raise ValueError(f"Source changed during packaging: {relative}")
    for name, expected_hash in prior_archives.items():
        if sha256(releases / name) != expected_hash:
            raise ValueError(f"Previous release changed: {name}")
    # Exclusive creation above and rename without replacement preserve old files.
    partial.rename(target)
    asset_hashes = {relative: digest for relative, digest in source_hashes.items()
                    if relative.startswith(("_internal/assets/", "_internal/preset_assets/"))}
    metadata = {
        "artifact": target.name, "created_at_utc": now.isoformat(), "channel": "local-beta-automatic",
        "sha256": sha256(target), "size_bytes": target.stat().st_size,
        "bundle_directory": "WoGStorageHelper", "launch_path": "WoGStorageHelper/WoGStorageHelper.exe",
        "bundle_file_count": original_count, "archive_file_count": len(files),
        "bundle_uncompressed_bytes": uncompressed_bytes,
        "executable_sha256": source_hashes["WoGStorageHelper.exe"],
        "documentation": {relative: source_hashes[relative] for relative in DOCUMENTS},
        "authentic_license_resources": len(authentic_licenses), "license_sha256": authentic_licenses,
        "application_assets": {name: source_hashes[f"_internal/assets/{name}"] for name in BRAND_FILES},
        "asset_sha256": asset_hashes, "calibration_reference_sha256": source_hashes[reference_relative],
        "calibration_tier_sample_counts": {tier: len(names) for tier, names in reference["tiers"].items()},
        "adaptive_tier_model_sha256": source_hashes[adaptive_relative],
        "adaptive_context_sample_count": len(adaptive_samples),
        "adaptive_ui_model_sha256": source_hashes[ui_relative],
        "adaptive_ui_sample_count": len(ui_samples),
        "headerparking_model_sha256": source_hashes[parking_relative],
        "headerparking_sample_count": len(parking_samples),
        "referenced_calibration_sample_count": len(samples), "largest_calibration_asset": largest_sample,
        "zip_integrity": "All entries CRC-checked and SHA256-checked against stable source bytes.",
        "leak_check": "No live profiles/user data/logs/full-size game images/tests/virtual environment; own icon, small calibration assets and stock Tcl/Tk/OpenCV resources included.",
        "prior_zip_count_preserved": len(prior_archives), "prior_archive_sha256": prior_archives,
        "scope": "Local tier-only autoscale beta ZIP from the full rebuilt onedir with release documents and authentic licence resources. No publication or upload.",
    }
    with metadata_path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(metadata, output, ensure_ascii=False, indent=2)
        output.write("\n")
    if json.loads(metadata_path.read_text(encoding="utf-8")) != metadata:
        raise ValueError("Metadata JSON roundtrip failed.")
    return {"archive": str(target), "metadata": str(metadata_path),
            "sha256": metadata["sha256"], "size_bytes": metadata["size_bytes"],
            "executable_sha256": metadata["executable_sha256"],
            "entries": len(files), "referenced_samples": len(samples),
            "prior_zip_count_preserved": len(prior_archives)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", action="store_true", help="Create ZIP after final build/docs approval")
    arguments = parser.parse_args()
    if not arguments.create:
        parser.error("Pass --create only after the final build and documents are ready.")
    print(json.dumps(create_release(), ensure_ascii=False, indent=2))
