"""Local setup builder. Never installs dependencies, launches, or publishes anything."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from io import BytesIO
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from zipfile import ZipFile

from installer.core import EXECUTABLE, PRODUCT_DIRECTORY, InstallError, file_hash, payload_files
from installer.license_resources import licensing_hashes, verify_wrapper_licenses
from release_identity import release_version

PROJECT = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT / "installer"


def referenced_sample_names(reference: dict) -> set[str]:
    """Only resource references, never diagnostic-source/provenance filenames."""
    if not isinstance(reference, dict):
        raise InstallError("Invalid bundled calibration reference.")
    samples = set()
    def add(name):
        if (not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name
                or Path(name).suffix.lower() != ".png"):
            raise InstallError("Invalid declared calibration image basename: " + repr(name))
        samples.add(name)
    for field in ("tiers", "provisional_equipped_tiers"):
        groups = reference.get(field, {})
        if not isinstance(groups, dict):
            raise InstallError("Invalid calibration sample group: " + field)
        for names in groups.values():
            if not isinstance(names, list):
                raise InstallError("Calibration samples must be lists: " + field)
            for name in names:
                add(name)
    for field in ("inventory_empty", "storage_empty", "equipment_label_samples"):
        names = reference.get(field, [])
        if not isinstance(names, list):
            raise InstallError("Calibration samples must be lists: " + field)
        for name in names:
            add(name)
    for field in ("active_page_image", "inactive_page_image", "page_active_sample", "page_inactive_sample"):
        if reference.get(field):
            add(reference[field])
    def images(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"source", "source_file", "source_image"}:
                    continue
                if key == "image":
                    add(child)
                else:
                    images(child)
        elif isinstance(value, list):
            for child in value:
                images(child)
    images(reference)
    return samples


def verify_adaptive_resources(document, archive, names) -> set[str]:
    """Validate only model images; native source frames are provenance data."""
    from PIL import Image
    if (not isinstance(document, dict) or document.get("schema") != 1
            or document.get("context_rect") != [0, 0, 8, 35]
            or document.get("badge_rect") != [1, 14, 4, 25]
            or document.get("max_rmse") != 14.0 or document.get("min_margin") != 5.0):
        raise InstallError("Invalid adaptive tier model schema, geometry or thresholds.")
    tiers, backgrounds = document.get("tiers"), document.get("backgrounds")
    if (not isinstance(tiers, dict) or set(tiers) != {str(t) for t in range(1, 7)}
            or not isinstance(backgrounds, list) or not backgrounds
            or any(not isinstance(entries, list) or not entries for entries in tiers.values())):
        raise InstallError("All six adaptive tier classes and background negatives are required.")
    if sum(len(entries) for entries in tiers.values()) + len(backgrounds) > 400:
        raise InstallError("Adaptive model resources exceed their count bound.")
    samples = set()
    for label, entries in list(tiers.items()) + [("background", backgrounds)]:
        for entry in entries:
            if not isinstance(entry, dict):
                raise InstallError("Invalid adaptive model resource entry.")
            name, digest = entry.get("image"), entry.get("sha256")
            pattern = r"adaptive_tier_background_\d{3}\.png" if label == "background" else rf"adaptive_tier_T{label}_\d{{3}}\.png"
            if (not isinstance(name, str) or not re.fullmatch(pattern, name)
                    or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or name in samples):
                raise InstallError("Invalid declared adaptive image/hash: " + repr(name))
            relative = "_internal/preset_assets/" + name
            if relative not in names:
                raise InstallError("A referenced adaptive context asset is missing: " + name)
            content = archive.read(PRODUCT_DIRECTORY + "/" + relative)
            if hashlib.sha256(content).hexdigest() != digest:
                raise InstallError("Adaptive context checksum mismatch: " + name)
            try:
                with Image.open(BytesIO(content)) as image:
                    if image.format != "PNG" or image.size != (8, 35):
                        raise ValueError("Not a native 8x35 PNG context")
                    image.load()
            except (OSError, ValueError) as exc:
                raise InstallError("Invalid adaptive context image geometry: " + name) from exc
            samples.add(name)
    return samples


def verify_ui_resources(document, archive, names) -> set[str]:
    """Check small native raster variants and their delivered canonical links."""
    from PIL import Image
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise InstallError("Invalid adaptive UI variant manifest schema.")
    samples, identifiers = set(), set()
    for group in ("headers", "controls"):
        entries = document.get(group)
        if not isinstance(entries, list) or not entries or len(entries) > 32:
            raise InstallError("Adaptive UI variant lists must be nonempty and bounded.")
        for entry in entries:
            if not isinstance(entry, dict):
                raise InstallError("Invalid adaptive UI variant record.")
            identifier, name = entry.get("id"), entry.get("image")
            if (not isinstance(identifier, str) or not identifier or identifier in identifiers
                    or not isinstance(name, str) or Path(name).name != name
                    or "/" in name or "\\" in name or Path(name).suffix.lower() != ".png"
                    or name in samples):
                raise InstallError("Invalid adaptive UI image or duplicate variant identity.")
            scale = entry.get("canonical_scale")
            if (isinstance(scale, bool) or not isinstance(scale, (int, float))
                    or not math.isfinite(scale) or not 0.35 <= scale <= 1.5
                    or entry.get("pane") not in {"inventory", "storage"}):
                raise InstallError("Invalid adaptive UI scale or pane.")
            dimensions = {}
            for field in ("source_rect", "source_anchor_rect"):
                rect = entry.get(field)
                if (not isinstance(rect, list) or len(rect) != 4
                        or any(isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 32768 for n in rect)
                        or not 1 <= rect[2] - rect[0] <= 300
                        or not 1 <= rect[3] - rect[1] <= 100):
                    raise InstallError("Adaptive UI source geometry is invalid or exceeds its tiny-crop bounds.")
                dimensions[field] = (rect[2] - rect[0], rect[3] - rect[1])
            digest = entry.get("sample_sha256")
            fingerprint = entry.get("canonical_pixels_sha256")
            if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                   for value in (digest, fingerprint)):
                raise InstallError("Invalid adaptive UI image hash or canonical pixel fingerprint.")
            relative = "_internal/preset_assets/" + name
            if relative not in names:
                raise InstallError("A referenced adaptive UI asset is missing: " + name)
            content = archive.read(PRODUCT_DIRECTORY + "/" + relative)
            if len(content) > 100_000 or hashlib.sha256(content).hexdigest() != digest:
                raise InstallError("Adaptive UI sample checksum or byte bound mismatch: " + name)
            try:
                with Image.open(BytesIO(content)) as image:
                    if image.format != "PNG" or image.size != dimensions["source_rect"]:
                        raise ValueError("Source rectangle does not describe the PNG dimensions")
                    image.load()
            except (OSError, ValueError) as exc:
                raise InstallError("Adaptive UI PNG geometry mismatch: " + name) from exc
            # canonical_image names an already delivered calibration resource.
            # source_image/source_sha256 are private frame provenance only.
            canonical = entry.get("canonical_image")
            if (not isinstance(canonical, str) or Path(canonical).name != canonical
                    or "/" in canonical or "\\" in canonical or Path(canonical).suffix.lower() != ".png"
                    or "_internal/preset_assets/" + canonical not in names):
                raise InstallError("Adaptive UI canonical calibration image is missing or invalid.")
            try:
                with Image.open(BytesIO(archive.read(PRODUCT_DIRECTORY + "/_internal/preset_assets/" + canonical))) as image:
                    actual = hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()
            except (OSError, ValueError) as exc:
                raise InstallError("Adaptive UI canonical calibration image is invalid.") from exc
            if actual != fingerprint:
                raise InstallError("Adaptive UI canonical pixel fingerprint differs from its delivered sample.")
            samples.add(name)
            identifiers.add(identifier)
    return samples


def verify_header_parking_resources(document, read_resource) -> set[str]:
    """Check delivered tiny parking crops; full native sources remain provenance."""
    from PIL import Image
    if (not isinstance(document, dict) or document.get("schema") != 1
            or not isinstance(document.get("records"), list) or len(document["records"]) != 6):
        raise InstallError("Invalid header parking model schema or six-record resource set.")
    samples, identifiers, pane_scales = set(), set(), set()
    for entry in document["records"]:
        if not isinstance(entry, dict):
            raise InstallError("Invalid header parking model record.")
        name, identifier, pane = entry.get("image"), entry.get("id"), entry.get("pane")
        if (not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name
                or not re.fullmatch(r"(?:inventory|storage)_header_parking_native_(?:1670|696|1182)_20261012\.png", name)
                or name in samples or not isinstance(identifier, str) or not identifier or identifier in identifiers
                or pane not in {"inventory", "storage"} or not name.startswith(pane+"_")):
            raise InstallError("Invalid header parking image, pane or duplicate identity.")
        scale = entry.get("canonical_scale")
        if (isinstance(scale, bool) or not isinstance(scale, (int, float))
                or not math.isfinite(scale) or not 0.35 <= scale <= 1.5 or (pane,scale) in pane_scales):
            raise InstallError("Invalid header parking scale or duplicate pane-scale.")
        dimensions = {}
        for field, limits in (("source_rect",(64,40)),("source_anchor_rect",(300,100))):
            rect = entry.get(field)
            if (not isinstance(rect, list) or len(rect) != 4
                    or any(isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 32768 for n in rect)
                    or not 1 <= rect[2]-rect[0] <= limits[0] or not 1 <= rect[3]-rect[1] <= limits[1]):
                raise InstallError("Header parking source geometry exceeds its tiny-crop bounds.")
            dimensions[field] = (rect[2]-rect[0],rect[3]-rect[1])
        for field in ("sample_sha256","canonical_pixels_sha256","source_sha256"):
            if not isinstance(entry.get(field),str) or not re.fullmatch(r"[a-f0-9]{64}",entry[field]):
                raise InstallError("Invalid header parking resource/provenance hash.")
        source = entry.get("source_image")
        if (not isinstance(source,str) or Path(source).name != source or "/" in source or "\\" in source
                or Path(source).suffix.lower() != ".png"):
            raise InstallError("Invalid header parking private source basename.")
        try:
            content = read_resource(name)
        except (KeyError,FileNotFoundError) as error:
            raise InstallError("A referenced header parking asset is missing: "+name) from error
        if len(content) > 100_000 or hashlib.sha256(content).hexdigest() != entry["sample_sha256"]:
            raise InstallError("Header parking sample checksum or byte bound mismatch: "+name)
        try:
            with Image.open(BytesIO(content)) as image:
                if image.format != "PNG" or image.size != dimensions["source_rect"]:
                    raise ValueError("Source rectangle does not describe the PNG dimensions")
                image.load()
            # Fingerprint the delivered caption; never read source_image.
            with Image.open(BytesIO(read_resource(pane+"_header.png"))) as image:
                canonical = hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()
        except (KeyError,FileNotFoundError,OSError,ValueError) as error:
            raise InstallError("Header parking PNG or canonical caption geometry mismatch: "+name) from error
        if canonical != entry["canonical_pixels_sha256"]:
            raise InstallError("Header parking canonical pixel fingerprint differs from its delivered caption.")
        samples.add(name)
        identifiers.add(identifier)
        pane_scales.add((pane,scale))
    if {pane for pane,_scale in pane_scales} != {"inventory","storage"} or any(
            sum(pane == expected for pane,_scale in pane_scales) != 3 for expected in ("inventory","storage")):
        raise InstallError("Header parking model must include three native sizes for each panel.")
    return samples


def verify_release_payload(payload: Path):
    entries, size = payload_files(payload)
    names = {relative for _source, relative, _bytes in entries}
    required = {EXECUTABLE, "_internal/assets/wog_storage_helper.png",
                "_internal/assets/wog_storage_helper.ico", "_internal/locales.json",
                "_internal/preset_assets/reference.json", "_internal/preset_assets/adaptive_tiers.json",
                "_internal/preset_assets/adaptive_ui.json",
                "_internal/preset_assets/header_parking.json",
                "README.md", "ATTRIBUTION.md",
                "THIRD_PARTY_NOTICES.txt", "_internal/THIRD_PARTY_NOTICES.txt",
                "_internal/licenses/manifest.json",
                "docs/STATE.md", "docs/STEAM_RELEASE.md", "preset_assets/CALIBRATION.md"}
    if missing := required - names:
        raise InstallError("The final release is incomplete: " + ", ".join(sorted(missing)))
    for relative in names:
        pieces = Path(relative).parts
        # Dependency directories are part of the complete onedir. Only user/runtime
        # data and development sources outside _internal are excluded here.
        if pieces[0] != "_internal" and (pieces[0].casefold() in {"profile", "data", "logs", ".venv", "tests"}
                                         or relative.casefold().endswith((".log", ".py"))):
            raise InstallError("The release includes live or development data: " + relative)
    with ZipFile(payload) as archive:
        if bad := archive.testzip():
            raise InstallError("Release archive CRC mismatch: " + bad)
        source = archive.read(f"{PRODUCT_DIRECTORY}/{EXECUTABLE}")
        executable_hash = hashlib.sha256(source).hexdigest()
        reference = json.loads(archive.read(f"{PRODUCT_DIRECTORY}/_internal/preset_assets/reference.json"))
        samples = referenced_sample_names(reference)
        for sample in samples:
            if Path(sample).name != sample or f"_internal/preset_assets/{sample}" not in names:
                raise InstallError("A referenced calibration asset is missing: " + sample)
        adaptive = json.loads(archive.read(f"{PRODUCT_DIRECTORY}/_internal/preset_assets/adaptive_tiers.json"))
        adaptive_samples = verify_adaptive_resources(adaptive, archive, names)
        ui = json.loads(archive.read(f"{PRODUCT_DIRECTORY}/_internal/preset_assets/adaptive_ui.json"))
        ui_samples = verify_ui_resources(ui, archive, names)
        parking_bytes = archive.read(f"{PRODUCT_DIRECTORY}/_internal/preset_assets/header_parking.json")
        try:
            parking = json.loads(parking_bytes)
        except (ValueError,UnicodeDecodeError) as error:
            raise InstallError("Invalid header parking model JSON.") from error
        parking_samples = verify_header_parking_resources(parking,
            lambda name:archive.read(f"{PRODUCT_DIRECTORY}/_internal/preset_assets/{name}"))
        authentic = licensing_hashes(PROJECT)
        for name, digest in authentic.items():
            relative = "_internal/" + name
            if relative not in names:
                raise InstallError("An authentic licence resource is missing: " + name)
            if hashlib.sha256(archive.read(PRODUCT_DIRECTORY + "/" + relative)).hexdigest() != digest:
                raise InstallError("Authentic licence checksum mismatch: " + name)
        if hashlib.sha256(archive.read(PRODUCT_DIRECTORY + "/THIRD_PARTY_NOTICES.txt")).hexdigest() != authentic["THIRD_PARTY_NOTICES.txt"]:
            raise InstallError("Release third-party notices checksum mismatch.")
    return {"file_count": len(entries), "uncompressed_bytes": size, "exe_sha256": executable_hash,
            "referenced_samples": len(samples | adaptive_samples | ui_samples | parking_samples),
            "adaptive_context_samples": len(adaptive_samples), "adaptive_ui_samples": len(ui_samples),
            "headerparking_model_sha256":hashlib.sha256(parking_bytes).hexdigest(),
            "headerparking_sample_count":len(parking_samples),
            "authentic_license_resources": len(authentic), "license_sha256": authentic}


def _freeze(name: str, output: str, *, version: str, output_root: Path):
    environment = os.environ.copy()
    environment["WOG_HELPER_BUILD_VERSION"] = version
    environment["WOG_HELPER_INSTALLER_RESOURCES"] = str(output_root / "resources")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(output_root / "dist" / output),
                    "--workpath", str(output_root / "build" / output), str(INSTALLER / name)],
                   cwd=PROJECT, env=environment, check=True)


def _require_compiler():
    import PyInstaller
    if PyInstaller.__version__ != "6.22.3":
        raise InstallError("This local build requires the locked PyInstaller 6.22.3 environment.")


def build_uninstaller(version: str | None = None, *, output_root: Path | None = None):
    """Build only an unsigned own wrapper for a later explicit signing request."""
    _require_compiler()
    version = release_version(version)
    destination = Path(output_root).absolute() if output_root is not None else INSTALLER
    _freeze("Uninstaller.spec", "uninstaller", version=version, output_root=destination)
    uninstaller = destination / "dist/uninstaller/WoGStorageHelperUninstall.exe"
    licenses = verify_wrapper_licenses(uninstaller, licensing_hashes(PROJECT))
    result = {"schema": 1, "version": version, "signing_state": "unsigned",
              "uninstaller": str(uninstaller), "uninstaller_sha256": file_hash(uninstaller),
              "uninstaller_bytes": uninstaller.stat().st_size, "uninstaller_licenses": licenses}
    (uninstaller.parent / "uninstaller_build_metadata.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def build(payload: Path, version: str | None = None, *, signer=None,
          signed_uninstaller: Path | None = None, require_signed_inputs=False,
          defer_setup_signing=False, output_root: Path | None = None,
          verifier=None, payload_verifier=None):
    """Build unsigned by default, or enforce signatures before every embedded hash.

    A signer callback is explicit build code, not a command imported from a manifest.
    The verifier always checks the returned bytes independently of that callback.
    """
    from i18n import validate_catalog, _catalog
    from windows_signing import verify_owned_executable, verify_signed_payload
    _require_compiler()
    version = release_version(version)
    if not isinstance(require_signed_inputs, bool) or not isinstance(defer_setup_signing, bool):
        raise InstallError("Signing gate flags must be explicit booleans.")
    require_signed_inputs = require_signed_inputs or signer is not None
    if signed_uninstaller is not None and not require_signed_inputs:
        raise InstallError("A pre-signed uninstaller requires the signed-input release gate.")
    if defer_setup_signing and (not require_signed_inputs or signer is not None):
        raise InstallError("Deferred cloud Setup signing requires verified signed inputs and no local signer.")
    if require_signed_inputs and signer is None and signed_uninstaller is None:
        raise InstallError("Signed builds require a verified signed uninstaller or an explicit signer.")
    verifier = verifier or verify_owned_executable
    payload_verifier = payload_verifier or verify_signed_payload
    destination = Path(output_root).absolute() if output_root is not None else INSTALLER
    # Reading/hash verification is sufficient for an explicitly selected source.
    # Strict Windows final-path queries can need privileges beyond file reads.
    payload = Path(payload).absolute()
    if not payload.is_file():
        raise FileNotFoundError(payload)
    stable_payload_hash = file_hash(payload)
    facts = verify_release_payload(payload)
    signatures = {}
    if require_signed_inputs:
        signatures["app"] = payload_verifier(payload, version=version)
        if signatures["app"]["sha256"] != facts["exe_sha256"]:
            raise InstallError("The app bytes changed between payload and signature audits.")
    validate_catalog({"messages": _catalog()})
    resources = destination / "resources"
    resources.mkdir(parents=True, exist_ok=True)
    target_payload = resources / "payload.zip"
    if payload != target_payload:
        shutil.copyfile(payload, target_payload)
    digest = file_hash(target_payload)
    if digest != stable_payload_hash or file_hash(payload) != stable_payload_hash:
        raise InstallError("The verified payload changed before embedding.")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    metadata = {"schema": 1, "version": version,
                "built_at_utc": stamp, "payload_name": payload.name, "sha256": digest, **facts}
    (resources / "payload_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if signed_uninstaller is None:
        _freeze("Uninstaller.spec", "uninstaller", version=version, output_root=destination)
        uninstaller = destination / "dist/uninstaller/WoGStorageHelperUninstall.exe"
        if signer is not None:
            signer(uninstaller)
    else:
        uninstaller = Path(signed_uninstaller).resolve(strict=True)
    if require_signed_inputs:
        signatures["uninstaller"] = verifier(uninstaller, role="uninstaller", version=version,
                                              expected_signer=signatures["app"]["signer_thumbprint"])
    metadata["uninstaller_licenses"] = verify_wrapper_licenses(uninstaller, facts["license_sha256"])
    shutil.copyfile(uninstaller, resources / "uninstaller.exe")
    metadata["uninstaller_sha256"] = file_hash(resources / "uninstaller.exe")
    if (file_hash(uninstaller) != metadata["uninstaller_sha256"] or (require_signed_inputs
            and signatures["uninstaller"]["sha256"] != metadata["uninstaller_sha256"])):
        raise InstallError("The verified uninstaller changed before embedding.")
    (resources / "payload_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    _freeze("Setup.spec", "setup", version=version, output_root=destination)
    setup = destination / "dist/setup/WoGStorageHelperSetup.exe"
    if signer is not None:
        signer(setup)
    if require_signed_inputs and not defer_setup_signing:
        signatures["setup"] = verifier(setup, role="setup", version=version,
                                         expected_signer=signatures["app"]["signer_thumbprint"])
    metadata["setup_licenses"] = verify_wrapper_licenses(setup, facts["license_sha256"])
    result = {**metadata, "setup": str(setup), "setup_sha256": file_hash(setup),
              "setup_bytes": setup.stat().st_size, "uninstaller_bytes": uninstaller.stat().st_size,
              "signatures": signatures, "signing_state": ("awaiting_setup_signature" if defer_setup_signing
                 else "signed" if require_signed_inputs else "unsigned")}
    if "setup" in signatures and signatures["setup"]["sha256"] != result["setup_sha256"]:
        raise InstallError("The signed Setup changed before its final hash was recorded.")
    (destination / "dist/setup/setup_metadata.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build local branded setup from the final verified full-onedir ZIP.")
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--stage-uninstaller", action="store_true")
    parser.add_argument("--signed-uninstaller", type=Path)
    parser.add_argument("--require-signed-inputs", action="store_true")
    parser.add_argument("--defer-setup-signing", action="store_true")
    parser.add_argument("--signtool", type=Path)
    parser.add_argument("--certificate-thumbprint")
    parser.add_argument("--timestamp-url")
    args = parser.parse_args(argv)
    options = (args.signtool, args.certificate_thumbprint, args.timestamp_url)
    if any(value is not None for value in options) and not all(value is not None for value in options):
        parser.error("Local signing requires --signtool, --certificate-thumbprint and --timestamp-url together.")
    if args.stage_uninstaller:
        if args.payload is not None or args.signed_uninstaller is not None or any(options) or args.require_signed_inputs or args.defer_setup_signing:
            parser.error("--stage-uninstaller accepts only --version and --output-root.")
        print(json.dumps(build_uninstaller(args.version, output_root=args.output_root), indent=2))
        return
    if args.payload is None:
        parser.error("--payload is required unless --stage-uninstaller is selected.")
    signer = None
    if all(value is not None for value in options):
        from windows_signing import CertificateStoreSigner
        signer = CertificateStoreSigner(signtool=args.signtool, thumbprint=args.certificate_thumbprint,
                                         timestamp_url=args.timestamp_url)
    build(args.payload, args.version, signer=signer, signed_uninstaller=args.signed_uninstaller,
          require_signed_inputs=args.require_signed_inputs, defer_setup_signing=args.defer_setup_signing,
          output_root=args.output_root)


if __name__ == "__main__":
    main()
