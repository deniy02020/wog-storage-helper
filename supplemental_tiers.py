"""Read-only native badge variants for the exact bundled canonical geometry."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from PIL import Image

from core import GridSpec, Rect
from paths import resource_path


CANONICAL_BADGE = (1, 14, 4, 25)
CANONICAL_CELL_SIZE = (65, 65)


def load_supplemental_tiers(canonical_badge, canonical_cell_size) -> dict[int, list[Image.Image]]:
    """Load authentic RGB variants without changing thresholds or user profiles.

    A different canonical shape has no compatible supplemental model. Resource
    names, SHA256 and dimensions are checked before pixels enter recognition;
    full screenshots and private diagnostics are never read by this loader.
    """
    badge = canonical_badge.as_box() if isinstance(canonical_badge, Rect) else canonical_badge
    if (not isinstance(badge, (tuple, list)) or len(badge) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in badge)
            or tuple(badge) != CANONICAL_BADGE
            or not isinstance(canonical_cell_size, (tuple, list)) or len(canonical_cell_size) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in canonical_cell_size)
            or tuple(canonical_cell_size) != CANONICAL_CELL_SIZE):
        return {}
    directory = resource_path("preset_assets").resolve()
    document = json.loads((directory / "reference.json").read_text(encoding="utf-8"))
    model = document.get("supplemental_native_tiers")
    if (not isinstance(model, dict) or model.get("schema") != 1
            or model.get("canonical_badge") != list(CANONICAL_BADGE)
            or model.get("canonical_cell_size") != list(CANONICAL_CELL_SIZE)):
        raise ValueError("Unsupported supplemental native tier geometry")
    tiers = model.get("tiers")
    required = {"3", "5", "6"}
    if (not isinstance(tiers, dict) or not required <= set(tiers)
            or not set(tiers) <= required | {"1", "4"}
            or any(not isinstance(entries, list) or not entries for entries in tiers.values())
            or ("1" in tiers and len(tiers["1"]) > 4)
            or ("4" in tiers and len(tiers["4"]) > 8)
            or sum(len(entries) for entries in tiers.values()) > 64):
        raise ValueError("Invalid supplemental native tier classes")
    result = {}
    names = set()
    for tier, entries in tiers.items():
        result[int(tier)] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Supplemental tier entries must be objects")
            name, digest = entry.get("image"), entry.get("sha256")
            phase_suffix = r"(?:_phase_\d{2})?" if tier == "1" else ""
            pattern = (r"tier_4_native_halo_index_24_phase_\d{2}_20261013\.png" if tier == "4" else
                       rf"tier_{tier}_native_shifted_index_\d{{2}}{phase_suffix}_20261012\.png")
            if (not isinstance(name, str)
                    or not re.fullmatch(pattern, name)
                    or name in names or not isinstance(digest, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                raise ValueError("Invalid supplemental tier resource reference")
            if tier == "4":
                _validate_halo_provenance(entry)
            names.add(name)
            path = (directory / name).resolve()
            if not path.is_relative_to(directory) or not path.is_file():
                raise ValueError("Missing or unsafe supplemental native tier resource")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Supplemental native tier checksum mismatch")
            with Image.open(path) as image:
                if image.format != "PNG" or image.size != (3, 11):
                    raise ValueError("A supplemental badge must be an authentic 3x11 PNG")
                if tier == "4" and (image.mode != "RGB" or
                        hashlib.sha256(image.tobytes()).hexdigest() != entry["rgb_sha256"]):
                    raise ValueError("Native halo badge RGB pixels do not match their provenance")
                result[int(tier)].append(image.convert("RGB"))
    return result


def _validate_halo_provenance(entry):
    """Accept only detached complete-cell variants from the approved T4 item.

    The loader checks declared geometry and hashes without ever opening the
    private source capture. Runtime recognition continues to use the same
    independent occupancy and 14-RMSE / 5-margin tier thresholds.
    """
    phase, size, grid, box = (entry.get(key) for key in
                              ("native_phase", "source_image_size", "source_grid", "source_cell_rect"))
    hashes = [entry.get(key) for key in ("rgb_sha256", "source_file_sha256", "source_rgb_sha256")]
    if (isinstance(phase, bool) or not isinstance(phase, int) or not 0 <= phase < 40
            or entry.get("image") != f"tier_4_native_halo_index_24_phase_{phase:02d}_20261013.png"
            or entry.get("source_image") not in {f"frame-{phase:02d}.png", "private-source-withheld.png"}
            or entry.get("inventory_cell_index") != 24
            or entry.get("source_cell_size") != [65, 65]
            or not isinstance(size, list) or len(size) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000
                   for value in size)
            or not isinstance(grid, dict) or grid.get("rows") != 8 or grid.get("columns") != 5
            or not isinstance(grid.get("rect"), list) or len(grid["rect"]) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in grid["rect"])
            or not isinstance(box, list) or len(box) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in box)
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes)):
        raise ValueError("Invalid native halo badge provenance")
    source_grid = GridSpec(Rect(*grid["rect"]), 8, 5)
    source_cell = source_grid.cell_rect(24)
    if (not source_grid.rect.fits(*size) or source_cell.as_box() != tuple(box)
            or (source_cell.width, source_cell.height) != CANONICAL_CELL_SIZE):
        raise ValueError("A native halo badge must come from one complete canonical cell")
