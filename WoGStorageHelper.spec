# Build on Windows using the pinned project environment; no live user profile.
from pathlib import Path
from release_identity import pe_version_info
from ocr_resources import verify_models, verify_native

root = Path(SPECPATH).resolve()
verify_models(root)
native_resources = verify_native(root)
binaries = [(str(root/relative), 'ocr_native') for relative in sorted(native_resources)]
assets = root / "preset_assets"
datas = [(str(path), "preset_assets") for path in sorted(assets.glob("*.png"))]
datas.append((str(assets / "reference.json"), "preset_assets"))
datas.append((str(assets / "adaptive_tiers.json"), "preset_assets"))
datas.append((str(assets / "adaptive_ui.json"), "preset_assets"))
datas.append((str(assets / "header_parking.json"), "preset_assets"))
datas.append((str(assets / "neutral_panels.json"), "preset_assets"))
datas.append((str(assets / "inventory_halos.json"), "preset_assets"))
datas.append((str(root / "locales.json"), "."))
datas.extend([(str(root/'disconnect_messages.json'), '.'),
              (str(root/'ocr_native_manifest.json'), '.')])
datas.extend((str(path), 'ocr_models') for path in sorted((root/'ocr_models').iterdir()) if path.is_file())
datas.append((str(root / "docs" / "STEAM_RELEASE.md"), "docs"))
datas.extend((str(root / "assets" / name), "assets") for name in
             ("wog_storage_helper.png", "wog_storage_helper.ico"))
licenses = [p for p in root.glob("LICENSE*") if p.is_file()]
datas.extend((str(path), ".") for path in licenses)
datas.append((str(root / "THIRD_PARTY_NOTICES.txt"), "."))
datas.extend((str(path), str(Path("licenses") / path.relative_to(root / "licenses").parent))
             for path in sorted((root / "licenses").rglob("*")) if path.is_file())

a = Analysis(
    [str(root / "dashboard.py")], pathex=[str(root)], binaries=binaries, datas=datas,
    hiddenimports=["PIL.ImageTk", "PIL.ImageGrab", "tkinter"],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="WoGStorageHelper",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, uac_admin=False,
    icon=str(root / "assets" / "wog_storage_helper.ico"),
    version=pe_version_info("app"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="WoGStorageHelper")
