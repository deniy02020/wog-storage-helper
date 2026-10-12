"""Bundled read-only resources and separate, persistent user profiles."""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence
from helper_config import default_transfer_filters

BASE = Path(__file__).resolve().parent


def resource_path(relative: str | Path) -> Path:
    """Resolve a bundled resource; PyInstaller supplies a bundled __file__."""
    path = Path(relative)
    if path.is_absolute() or path.drive or path.root or ".." in path.parts:
        raise ValueError("Ресурс должен находиться внутри папки программы.")
    return BASE / path


def _profile_path(value: str) -> Path:
    if not value or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("WOG_HELPER_PROFILE должен содержать полный путь к папке.")
    path = Path(value).expanduser()
    if not path.is_absolute() or any(c in '<>"|?*' for c in value):
        raise ValueError("WOG_HELPER_PROFILE должен содержать допустимый полный путь.")
    if hasattr(os.path, "isreserved") and os.path.isreserved(str(path)):
        raise ValueError("Путь профиля содержит зарезервированное имя Windows.")
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ValueError("Путь профиля указывает на файл, а не папку.")
    return path


def get_profile_dir(argv: Sequence[str] | None = None, *,
                    environ: Mapping[str, str] | None = None,
                    frozen: bool | None = None,
                    executable: str | Path | None = None) -> Path:
    """Explicit override, portable executable data, frozen AppData, or dev profile."""
    env = os.environ if environ is None else environ
    if "WOG_HELPER_PROFILE" in env:
        return _profile_path(env["WOG_HELPER_PROFILE"])
    args = sys.argv[1:] if argv is None else argv
    if "--portable" in args:
        return Path(executable or sys.executable).resolve().parent / "data"
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        local = env.get("LOCALAPPDATA", "")
        if not local:
            raise ValueError("Windows не сообщил папку LOCALAPPDATA для профиля.")
        return _profile_path(local) / "WoGStorageHelper"
    return BASE / "profile"


def _asset_path(directory: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ValueError("Имя образца должно быть простым именем PNG.")
    if "/" in name or "\\" in name or Path(name).suffix.lower() != ".png":
        raise ValueError("Образцы профиля должны быть локальными PNG.")
    path = (directory / name).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise ValueError(f"Не найден обязательный образец: {name}")
    return path


def _images_from_controls(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "image" and isinstance(child, str):
                yield child
            else:
                yield from _images_from_controls(child)
    elif isinstance(value, list):
        for child in value:
            yield from _images_from_controls(child)


def bootstrap_new_profile(profile_dir: str | Path | None = None, *,
                          assets_dir: str | Path | None = None) -> Path:
    """Create a baseline profile once, preserving every existing user file.

    Reference coordinates describe its source image. The runner resolves live
    panel geometry; reference frame dimensions are never a UI scale factor.
    """
    directory = Path(profile_dir) if profile_dir is not None else get_profile_dir()
    settings = directory / "settings.json"
    if settings.exists():
        if not settings.is_file():
            raise ValueError("settings.json должен быть файлом.")
        return settings
    assets = (Path(assets_dir) if assets_dir is not None
              else resource_path("preset_assets")).resolve()
    reference = json.loads((assets / "reference.json").read_text(encoding="utf-8"))
    if not isinstance(reference, dict):
        raise ValueError("Некорректный справочный профиль.")
    tiers = copy.deepcopy(reference["tiers"])
    for tier, names in reference.get("provisional_equipped_tiers", {}).items():
        tiers.setdefault(tier, [])
        for name in names:
            if name not in tiers[tier]:
                tiers[tier].append(name)
    profile = {
        "mode": "right", "include_gems": True, "interval": 1800.0,
        "scheduler_mode": "automatic", "config_version": 3, "automatic_layout": True,
        "transfer_filters": default_transfer_filters(),
        "supervised_ok": False, "size": copy.deepcopy(reference["reference_size"]),
        "tiers": tiers,
        "inventory": copy.deepcopy(reference["inventory"]),
        "storage": copy.deepcopy(reference["storage"]),
        "badge": copy.deepcopy(reference["badge"]),
        "inventory_empty": copy.deepcopy(reference["inventory_empty"]),
        "storage_empty": copy.deepcopy(reference["storage_empty"]),
        "equipment_label_samples": copy.deepcopy(reference["equipment_label_samples"]),
        "anchors": [{"rect": copy.deepcopy(a["rect"]), "image": a["image"]}
                    for a in reference["anchors"]],
        "pages": [{"id": p["id"], "button": copy.deepcopy(p["button_rect"]),
                   "marker": copy.deepcopy(p["marker_rect"])}
                  for p in reference.get("pages", [])],
        "page_active_image": reference.get("active_page_image", reference.get("page_active_sample")),
        "page_inactive_image": reference.get("inactive_page_image", reference.get("page_inactive_sample")),
    }
    for key in ("icon_rect", "sort_button", "panel_controls", "tier_color_map",
                "tier_color_map_source", "equipment_label_thresholds",
                "equipment_label_provenance", "native_frame_phase_provenance"):
        if key in reference:
            profile[key] = copy.deepcopy(reference[key])
    profile["provisional_tiers"] = [int(t) for t in reference.get("provisional_equipped_tiers", {})]
    required = {n for names in tiers.values() for n in names}
    for key in ("inventory_empty", "storage_empty", "equipment_label_samples"):
        required.update(profile[key])
    required.update(a["image"] for a in profile["anchors"])
    required.update(n for n in (profile["page_active_image"], profile["page_inactive_image"]) if n)
    required.update(_images_from_controls(profile.get("sort_button", {})))
    required.update(_images_from_controls(profile.get("panel_controls", {})))
    # Validate all sources before touching a new profile; reference data is not code.
    sources = {name: _asset_path(assets, name) for name in required}
    encoded = json.dumps(profile, ensure_ascii=False, indent=2) + "\n"
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        target = directory / name
        if target.exists() and not target.is_file():
            raise ValueError(f"Образец профиля должен быть файлом: {name}")
        content = source.read_bytes()
        try:
            with target.open("xb") as output:
                output.write(content)
        except FileExistsError:
            pass
    try:
        with settings.open("x", encoding="utf-8", newline="\n") as output:
            output.write(encoded)
    except FileExistsError:
        pass
    return settings
