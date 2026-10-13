"""Allowlisted local source export. No Git, network, installation or game input.

Only exported copies are sanitized. Original application code, resources and
private evidence are never rewritten. Every output is created exclusively.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

from PIL import Image


KIT = Path(__file__).resolve().parent
ROOT = KIT.parents[1]
SENTINEL = "private-source-withheld.png"
MANIFEST = "SOURCE_CONTENT_MANIFEST.json"
GENERATED = {MANIFEST, "SOURCE_SHA256SUMS.txt", "PUBLIC_TEST_RESULT.json"}
MODULES = tuple(("adaptive_tiers app app_startup auto_layout autocalibration core dashboard game_identity "
    "gearlabel helper_config i18n input_activity instance inventory_cycle inventory_motion_policy "
    "inventory_samples inventory_scroller inventory_viewport inventory_walk inventory_halo package_release panels "
    "paths runtime release_identity session_logging windows_signing storage_cycle storage_pages supplemental_tiers ui_brand ui_filters ui_journal "
    "ui_theme ui_time_picker ui_vault_motion ui_widgets windows").split())
INSTALLER_MODULES = tuple(("__init__ build core launcher license_resources uninstall_launcher "
    "verify_real_payload windows_integration wizard").split())
TESTS = tuple(("test_core test_inventory_cycle test_inventory_scroller test_inventory_walk "
    "test_inventory_bulk_scroll test_windows_scroll_burst test_windows_scroll test_windows "
    "test_time_picker test_app_timer test_app_startup test_automatic test_runtime_retry_budget "
    "test_runtime_lifecycle test_panels test_inventory_halo test_native_halo_tiers installer.test_build "
    "test_storage_cycle test_i18n test_ui_journal test_runtime_walk_session test_auto_waiting_i18n "
    "test_runtime_sorted_batches test_runtime_sorted_top_raster test_runtime_inventory_bulk test_runtime test_session_logging test_runtime_preflight_recovery "
    "test_runtime_incomplete_outcomes test_runtime_walk_fresh_drop test_runtime_confirmed_input_boundary test_release_identity "
    "test_windows_signing installer.test_signing_build ci.test_pipeline").split())
PUBLIC_TESTS = TESTS + ("test_public_resources",)
FILES = {*(name + ".py" for name in MODULES), *(name.replace(".", "/") + ".py" for name in TESTS),
    *("installer/" + name + ".py" for name in INSTALLER_MODULES),
    "installer/Setup.spec", "installer/Uninstaller.spec", "installer/strings_ru.json",
    "installer/installer_locales.json", "WoGStorageHelper.spec", "locales.json", "LICENSE", "AUTHORS.md",
    "requirements.txt", "requirements-lock.txt", "requirements-build-lock.txt", "build.bat",
    "build_installer.bat", "install.bat", "run.bat", "run-exe.bat", "selftest.bat",
    "preset_assets/reference.json", "preset_assets/adaptive_tiers.json",
    "preset_assets/adaptive_ui.json", "preset_assets/header_parking.json", "preset_assets/inventory_halos.json",
    "ci/__init__.py", "ci/windows_build.py", "ci/resume_signing.py", "ci/signpath.py", "ci/actions-pins.json",
    ".github/workflows/windows-build.yml", ".github/workflows/signing-resume.yml"}
TEMPLATES = {"README.md", "ATTRIBUTION.md", ".gitignore", ".gitattributes", "docs/STEAM_RELEASE.md",
    "docs/STATE.md", "docs/CODE_SIGNING.md", "docs/SIGNPATH_APPLICATION_DRAFT.md", ".github/SECURITY.md",
    "preset_assets/CALIBRATION.md", "installer/README.md", "test_public_resources.py"}
JSON_MODELS = {"preset_assets/reference.json", "preset_assets/adaptive_tiers.json",
    "preset_assets/adaptive_ui.json", "preset_assets/header_parking.json", "preset_assets/inventory_halos.json"}
FORBIDDEN_PARTS = {".git", ".venv", "verification", "diagnostics", "profile", "data", "logs",
    "__pycache__", "build", "dist", "releases", "website", "website-starter"}
PRIVATE_TEXT = re.compile(r"(?i)(?:[A-Z]:[/\\]Users[/\\]|[A-Z]:[/\\]Projects[/\\]|"
    r"verification[/\\](?:native|factory|readback|calibration)[^\s\"']*|calibration-capture-\d{10,}|tooltip-cell-\d+-\d{10,}|"
    r"readback-20\d{6}T|factory-native-beta\d|codex-clipboard-[a-f0-9]{8}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})")


def digest(content):
    return hashlib.sha256(content).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def policy():
    document = json.loads((KIT / "source-policy.json").read_bytes())
    if document.get("schema") != 1 or document.get("author") != "deniy":
        raise ValueError("Unexpected reviewed source policy")
    records = {record["path"]: record for record in document["images"]}
    if len(records) != 827:
        raise ValueError("Reviewed image allowlist must contain 825 matching crops and 2 original icons")
    return document, records


def safe_relative(name):
    if not isinstance(name, str) or "\\" in name or ":" in name or name.startswith("/"):
        raise ValueError("Unsafe source name")
    path = PurePosixPath(name)
    if not path.parts or any(part in {"", ".", ".."} for part in name.split("/")):
        raise ValueError("Unsafe source path components")
    if {part.casefold() for part in path.parts} & FORBIDDEN_PARTS:
        raise ValueError("Private or build path is forbidden: " + name)
    if path.suffix.casefold() in {".bak", ".log", ".stderr", ".exe", ".dll", ".pyc", ".pyd", ".zip"}:
        raise ValueError("Binary, log or backup is forbidden: " + name)
    return path


def output_path(value):
    path = Path(value).resolve()
    if path == KIT or not path.is_relative_to(KIT):
        raise ValueError("Source-kit outputs must be inside the dedicated source-kit directory")
    return path


def sanitize_model(value, trail=(), changes=None):
    changes = [] if changes is None else changes
    if isinstance(value, list):
        return [sanitize_model(child, trail + (str(index),), changes) for index, child in enumerate(value)]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, child in value.items():
        position = trail + (key,)
        if key in {"source_image", "native_phase_source_image"} and isinstance(child, str):
            result[key] = SENTINEL
            if child != SENTINEL:
                changes.append(".".join(position))
        elif not trail and key == "source" and isinstance(child, str):
            result[key] = "Private original reference evidence withheld from this source snapshot."
            changes.append("source")
        elif key == "game_tier_source" and isinstance(child, str):
            result[key] = "Private native tooltip evidence; original diagnostic filename withheld."
            changes.append(".".join(position))
        else:
            result[key] = sanitize_model(child, position, changes)
    return result


def exported_bytes(name, source):
    original = source.read_bytes()
    transformations = []
    if name in JSON_MODELS:
        value = json.loads(original)
        exported = json_bytes(sanitize_model(value, changes=transformations))
    elif name == "storage_pages.py":
        text = original.decode("utf-8")
        lines = text.splitlines(keepends=True)
        clean = []
        for line in lines:
            if line.lstrip().startswith("#") and "verification/" in line:
                newline = "\r\n" if line.endswith("\r\n") else "\n"
                clean.append("# Private original native reference filename withheld." + newline)
                transformations.append("comment-only private reference filename")
            else:
                clean.append(line)
        exported = "".join(clean).encode("utf-8")
        if ast.dump(ast.parse(original), include_attributes=False) != ast.dump(ast.parse(exported), include_attributes=False):
            raise ValueError("Comment sanitation changed production semantics")
    else:
        exported = original
    return original, exported, transformations


def inspect_image(content, name, record):
    from io import BytesIO
    if len(content) != record["bytes"] or digest(content) != record["sha256"]:
        raise ValueError("Unreviewed image bytes: " + name)
    with Image.open(BytesIO(content)) as image:
        if image.format != record["format"] or list(image.size) != record["size"]:
            raise ValueError("Unreviewed image geometry: " + name)
        if image.format == "PNG" and image.info:
            raise ValueError("PNG metadata is not part of the reviewed source policy")
        if image.format == "ICO" and sorted([list(size) for size in image.ico.sizes()]) != record["ico_sizes"]:
            raise ValueError("Unexpected icon frames")
        image.load()
        if ("mode" in record and image.mode != record["mode"]
                or "rgb_sha256" in record and digest(image.convert("RGB").tobytes()) != record["rgb_sha256"]):
            raise ValueError("Unreviewed image pixel mode or fingerprint: " + name)


def inspect_text(content, name):
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Unreviewed non-image binary: " + name) from error
    if PRIVATE_TEXT.search(text):
        raise ValueError("Private diagnostic path or credential marker in: " + name)


def licensing_paths():
    # A script started by filename has only its own directory on sys.path.
    # Resolve this known local read-only validator from the checkout explicitly.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from installer import license_resources
    if Path(license_resources.__file__).resolve() != ROOT / "installer" / "license_resources.py":
        raise ValueError("Licence validator was imported outside the source checkout")
    return license_resources.licensing_files(ROOT)


def stage(label):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", label):
        raise ValueError("Use a short lowercase source-stage label")
    destination = output_path(KIT / label)
    destination.mkdir(exist_ok=False)
    document, images = policy()
    # Import only the known local manifest validator. It verifies bytes and
    # never launches the installer, reads profiles or accesses the game.
    licenses = licensing_paths()
    selected = FILES | set(licenses) | set(images)
    records = []
    for name in sorted(selected):
        safe_relative(name)
        source = ROOT / name
        if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(ROOT):
            raise ValueError("Missing or linked allowlisted source: " + name)
        original, content, transformations = exported_bytes(name, source)
        if name in images:
            inspect_image(content, name, images[name])
        else:
            inspect_text(content, name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
        records.append({"path": name, "bytes": len(content), "sha256": digest(content),
                        "original_source_sha256": digest(original), "sanitized_fields": transformations})
    extra_files = {"release-kit/source-kit/prepare_source.py": KIT / "prepare_source.py",
                   "release-kit/source-kit/source-policy.json": KIT / "source-policy.json"}
    extra_files.update({name: KIT / "templates" / name for name in TEMPLATES})
    extra_files.update({"release-kit/source-kit/templates/" + name: KIT / "templates" / name
                        for name in TEMPLATES})
    for name, source in sorted(extra_files.items()):
        safe_relative(name)
        content = source.read_bytes()
        inspect_text(content, name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
        records.append({"path": name, "bytes": len(content), "sha256": digest(content),
                        "origin": "sanitized public source-kit template"})
    manifest = {"schema": 1, "version": document["version"], "author": "deniy",
        "created_utc": datetime.now(timezone.utc).isoformat(), "status": "reviewable local source snapshot",
        "repository_url": "https://github.com/deniy02020/wog-storage-helper", "public_tests": list(PUBLIC_TESTS),
        "privacy": "No profiles, logs, diagnostics, full game screenshots, binaries or credentials. Original private image filenames are replaced in exported provenance only; source hashes and geometry remain.",
        "code_semantics": "Production Python is byte-exact except one private-source comment; its AST is verified identical. Recognition PNG bytes are unchanged.",
        "images": list(images.values()), "files": sorted(records, key=lambda record: record["path"])}
    with (destination / MANIFEST).open("xb") as stream:
        stream.write(json_bytes(manifest))
    sums = "".join(record["sha256"] + "  " + record["path"] + "\n" for record in manifest["files"])
    with (destination / "SOURCE_SHA256SUMS.txt").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(sums)
    # Never emit a snapshot assembled while another agent edited source files.
    for record in records:
        if "original_source_sha256" in record and digest((ROOT / record["path"]).read_bytes()) != record["original_source_sha256"]:
            raise ValueError("Source changed during staging: " + record["path"])
    return verify_tree(destination)


def verify_tree(tree):
    tree = output_path(tree)
    manifest = json.loads((tree / MANIFEST).read_bytes())
    records = {record["path"]: record for record in manifest["files"]}
    if len(records) != len(manifest["files"]):
        raise ValueError("Duplicate content manifest names")
    allowed = FILES | set(licensing_paths()) | set(policy()[1]) | TEMPLATES | {
        "release-kit/source-kit/prepare_source.py", "release-kit/source-kit/source-policy.json"}
    allowed |= {"release-kit/source-kit/templates/" + name for name in TEMPLATES}
    if set(records) != allowed:
        raise ValueError("Content manifest differs from the explicit source allowlist")
    expected = set(records) | {MANIFEST, "SOURCE_SHA256SUMS.txt"}
    if (tree / "PUBLIC_TEST_RESULT.json").exists():
        expected.add("PUBLIC_TEST_RESULT.json")
    actual = {path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_file()}
    if actual != expected or any(path.is_symlink() for path in tree.rglob("*")):
        raise ValueError("Unexpected, missing or linked exported file")
    _, approved_images = policy()
    if {record["path"]: record for record in manifest["images"]} != approved_images:
        raise ValueError("Exported image list differs from reviewed policy")
    for name in sorted(expected):
        safe_relative(name)
        content = (tree / name).read_bytes()
        if name in records:
            record = records[name]
            if len(content) != record["bytes"] or digest(content) != record["sha256"]:
                raise ValueError("Exported checksum mismatch: " + name)
        if name in approved_images:
            inspect_image(content, name, approved_images[name])
        else:
            inspect_text(content, name)
    sums = "".join(record["sha256"] + "  " + record["path"] + "\n" for record in manifest["files"])
    if (tree / "SOURCE_SHA256SUMS.txt").read_text(encoding="utf-8") != sums:
        raise ValueError("Source checksum index differs from manifest")
    return {"tree": str(tree), "files": len(expected), "images": len(approved_images),
            "manifest_sha256": digest((tree / MANIFEST).read_bytes()), "leak_gate": "passed"}


def public_tests(tree):
    before = verify_tree(tree)
    tree = Path(before["tree"])
    result_path = tree / "PUBLIC_TEST_RESULT.json"
    if result_path.exists():
        raise ValueError("Public test report already exists")
    private_temp = output_path(KIT / (tree.name + "-test-tmp"))
    private_temp.mkdir(exist_ok=False)
    # The interpreter/dependencies are local, but every imported product/test
    # module must belong to the exported tree, never the development checkout.
    driver = """import json,pathlib,tempfile,time,unittest,sys
tempfile.tempdir=sys.argv[1]
root=pathlib.Path.cwd().resolve()
started=time.monotonic()
suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[2:])
result=unittest.TextTestRunner(verbosity=1).run(suite)
escaped=[]
for name,module in tuple(sys.modules.items()):
    if name.split('.')[0] in {value.split('.')[0] for value in sys.argv[2:]}|set(%r)|{'installer','ci'}:
        filename=getattr(module,'__file__',None)
        if filename and not pathlib.Path(filename).resolve().is_relative_to(root):escaped.append(name)
report={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
 'skips':len(result.skipped),'exit_code':0 if result.wasSuccessful() and not escaped else 1,
 'elapsed_seconds':round(time.monotonic()-started,3),'imported_outside_export':escaped,'native_input':False}
print('PUBLIC_RESULT='+json.dumps(report),flush=True)
raise SystemExit(report['exit_code'])
""" % list(MODULES)
    env = dict(os.environ, PYTHONPATH=str(tree), PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.run([sys.executable, "-B", "-c", driver, str(private_temp), *PUBLIC_TESTS],
                             cwd=tree, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
    raw_log = process.stdout + process.stderr
    (private_temp / "public-test-private-log.txt").write_text(raw_log, encoding="utf-8")
    match = re.search(r"^PUBLIC_RESULT=(.+)$", process.stdout, re.MULTILINE)
    if match is None:
        raise ValueError("Exported tests produced no result; inspect private local test log")
    result = json.loads(match.group(1))
    result.update({"scope": "Only the allowlisted synthetic public test subset, run from exported source.",
                   "full_private_corpus": "The historical 913-test local development corpus includes withheld private fixtures and is not reproduced by this snapshot.",
                   "manifest_sha256": before["manifest_sha256"]})
    if process.returncode != result["exit_code"] or result["exit_code"] != 0:
        raise ValueError("Exported public tests failed; no publishable test result created")
    with result_path.open("xb") as stream:
        stream.write(json_bytes(result))
    verify_tree(tree)
    return result


def archive(tree, label):
    report = verify_tree(tree)
    tree = Path(report["tree"])
    tests = json.loads((tree / "PUBLIC_TEST_RESULT.json").read_bytes())
    if (tests.get("exit_code") != 0 or tests.get("imported_outside_export") != []
            or tests.get("failures") != 0 or tests.get("errors") != 0
            or tests.get("native_input") is not False
            or tests.get("manifest_sha256") != report["manifest_sha256"]):
        raise ValueError("Exported source must have passed isolated public tests")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,100}\.zip", label):
        raise ValueError("Use a simple source ZIP basename")
    target = output_path(KIT / label)
    files = sorted(path for path in tree.rglob("*") if path.is_file())
    hashes = {path.relative_to(tree).as_posix(): digest(path.read_bytes()) for path in files}
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for path in files:
            output.write(path, "WoGStorageHelper-source/" + path.relative_to(tree).as_posix())
    with zipfile.ZipFile(target) as output:
        if output.testzip() is not None or len(output.namelist()) != len(hashes):
            raise ValueError("Source ZIP CRC/count mismatch")
        for name, expected in hashes.items():
            if digest(output.read("WoGStorageHelper-source/" + name)) != expected:
                raise ValueError("Source ZIP byte mismatch")
    verify_tree(tree)
    metadata = {"artifact": target.name, "sha256": digest(target.read_bytes()),
        "bytes": target.stat().st_size, "files": len(hashes), "public_tests": tests,
        "manifest_sha256": report["manifest_sha256"], "published": False, "repository_url": None}
    with target.with_suffix(".json").open("xb") as stream:
        stream.write(json_bytes(metadata))
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stage").add_argument("label")
    for command in ("verify", "test", "archive"):
        item = sub.add_parser(command)
        item.add_argument("tree", type=Path)
        if command == "archive":
            item.add_argument("label")
    args = parser.parse_args()
    result = stage(args.label) if args.command == "stage" else verify_tree(args.tree) if args.command == "verify" else public_tests(args.tree) if args.command == "test" else archive(args.tree, args.label)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
