"""Read-only native badge variants for the exact bundled canonical geometry."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from PIL import Image

from core import Rect
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
            or not set(tiers) <= required | {"1"}
            or any(not isinstance(entries, list) or not entries for entries in tiers.values())
            or ("1" in tiers and len(tiers["1"]) > 4)
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
            if (not isinstance(name, str)
                    or not re.fullmatch(rf"tier_{tier}_native_shifted_index_\d{{2}}{phase_suffix}_20261012\.png", name)
                    or name in names or not isinstance(digest, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                raise ValueError("Invalid supplemental tier resource reference")
            names.add(name)
            path = (directory / name).resolve()
            if not path.is_relative_to(directory) or not path.is_file():
                raise ValueError("Missing or unsafe supplemental native tier resource")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Supplemental native tier checksum mismatch")
            with Image.open(path) as image:
                if image.format != "PNG" or image.size != (3, 11):
                    raise ValueError("A supplemental badge must be an authentic 3x11 PNG")
                result[int(tier)].append(image.convert("RGB"))
    return result
