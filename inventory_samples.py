"""Authentic full-cell empty variants for the bundled automatic geometry."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from PIL import Image

from core import GridSpec, Rect
from paths import resource_path


CANONICAL_CELL_SIZE = (65, 65)


def load_inventory_empty_samples(canonical_cell_size) -> list[Image.Image]:
    """Load detached, checked native empties without reading private frames.

    The source screenshot is provenance only; distribution contains only the
    exact65x65 RGB cell. Other/manual canonical geometries have no compatible
    model. No user settings or image-recognition thresholds are modified.
    """
    if (not isinstance(canonical_cell_size, (tuple, list)) or len(canonical_cell_size) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in canonical_cell_size)
            or tuple(canonical_cell_size) != CANONICAL_CELL_SIZE):
        return []
    directory = resource_path("preset_assets").resolve()
    document = json.loads((directory / "reference.json").read_text(encoding="utf-8"))
    model = document.get("supplemental_native_inventory_empty")
    if (not isinstance(model, dict) or model.get("schema") != 1
            or model.get("canonical_cell_size") != list(CANONICAL_CELL_SIZE)):
        raise ValueError("Unsupported supplemental inventory-empty geometry")
    entries = model.get("samples")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
        raise ValueError("Invalid inventory-empty sample count")
    samples, names = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Inventory-empty entries must be objects")
        name, digest, pixels_digest = entry.get("image"), entry.get("sha256"), entry.get("rgb_sha256")
        if (not isinstance(name, str)
                or not re.fullmatch(r"inventory_empty_native_complete_index_\d{2}_20261012\.png", name)
                or name in names
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                       for value in (digest, pixels_digest, entry.get("source_file_sha256"),
                                     entry.get("source_rgb_sha256")))):
            raise ValueError("Invalid inventory-empty resource reference")
        names.add(name)
        source_size, grid, index, box = (entry.get(key) for key in
            ("source_client_size", "source_grid", "source_cell_index", "source_cell_rect"))
        if (not isinstance(source_size, list) or len(source_size) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000
                       for value in source_size)
                or not isinstance(grid, dict) or grid.get("rows") != 8 or grid.get("columns") != 5
                or not isinstance(grid.get("rect"), list) or len(grid["rect"]) != 4
                or any(isinstance(value, bool) or not isinstance(value, int) for value in grid["rect"])
                or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 40
                or not isinstance(box, list) or len(box) != 4
                or any(isinstance(value, bool) or not isinstance(value, int) for value in box)):
            raise ValueError("Inventory-empty source geometry is unproved")
        source_grid = GridSpec(Rect(*grid["rect"]), 8, 5)
        source_cell = source_grid.cell_rect(index)
        if (name != f"inventory_empty_native_complete_index_{index:02d}_20261012.png"
                or not source_grid.rect.fits(*source_size) or source_cell.as_box() != tuple(box)
                or (source_cell.width, source_cell.height) != CANONICAL_CELL_SIZE):
            raise ValueError("An inventory-empty sample must be one complete native cell")
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise ValueError("Missing or unsafe inventory-empty resource")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Inventory-empty resource checksum mismatch")
        with Image.open(path) as image:
            if image.format != "PNG" or image.mode != "RGB" or image.size != CANONICAL_CELL_SIZE:
                raise ValueError("A supplemental inventory empty must be an authentic65x65 RGB PNG")
            if hashlib.sha256(image.tobytes()).hexdigest() != pixels_digest:
                raise ValueError("Inventory-empty RGB checksum mismatch")
            samples.append(image.copy())
    return samples
