"""Locate this user's two panel headers and translate reference geometry."""
import copy
import json
import shutil
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from core import Rect, image_rmse


ASSETS = Path(__file__).parent / "preset_assets"


def locate_header(image, sample):
    if image.width < sample.width or image.height < sample.height:
        raise ValueError("Окно слишком маленькое для сохранённого интерфейса.")
    frame = np.asarray(image.convert("RGB"))
    template = np.asarray(sample.convert("RGB"))
    result = cv2.matchTemplate(frame, template, cv2.TM_SQDIFF_NORMED)
    _, _, point, _ = cv2.minMaxLoc(result)
    rect = Rect(point[0], point[1], point[0] + sample.width, point[1] + sample.height)
    if image_rmse(image.crop(rect.as_box()), sample) > 10:
        raise ValueError("Оба окна должны быть открыты в том же масштабе, что на твоих скриншотах. Заголовок не найден.")
    return rect


def build_profile(image, directory, *, adaptive=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    reference = json.loads((ASSETS / "reference.json").read_text(encoding="utf-8"))
    offsets = {}
    anchors = []
    baseline_size = tuple(reference["reference_size"]) if adaptive else image.size
    for spec in reference["anchors"]:
        sample = Image.open(ASSETS / spec["image"]).convert("RGB")
        original = Rect(*spec["rect"])
        found = original if adaptive else locate_header(image, sample)
        offsets[spec["pane"]] = (found.left - original.left, found.top - original.top)
        anchors.append({"rect": list(found.as_box()), "image": spec["image"]})

    def shifted(coords, pane):
        return list(Rect(*coords).translated(*offsets[pane]).as_box())

    result = {key: reference[key] for key in ["badge", "icon_rect", "tiers", "inventory_empty", "storage_empty"]}
    result["tiers"] = copy.deepcopy(reference["tiers"])
    for pane in ["inventory", "storage"]:
        result[pane] = dict(reference[pane], rect=shifted(reference[pane]["rect"], pane))
        if not Rect(*result[pane]["rect"]).fits(*baseline_size):
            raise ValueError("Сетка выходит за окно: раскрой обе панели полностью.")
    result.update({"size": list(baseline_size), "mode": "right", "anchors": anchors,
                   "interval": 60, "supervised_ok": False})
    if reference.get("panel_controls"):
        result["panel_controls"] = reference["panel_controls"]
    # The user confirmed these colors on equipped gear; keep an explicit note.
    for tier, filenames in reference["provisional_equipped_tiers"].items():
        result["tiers"].setdefault(tier, [])
        for filename in filenames:
            if filename not in result["tiers"][tier]:
                result["tiers"][tier].append(filename)
    result["provisional_tiers"] = [4, 5]
    result["equipment_label_samples"] = list(reference["equipment_label_samples"])
    result["pages"] = [{"id": spec["id"], "button": shifted(spec["button_rect"], "storage"),
                         "marker": shifted(spec["marker_rect"], "storage")}
                       for spec in reference["pages"]]
    result["page_active_image"] = reference["page_active_sample"]
    result["page_inactive_image"] = reference["page_inactive_sample"]
    sort = reference["sort_button"]
    result["sort_button"] = {"rect": shifted(sort["rect"], "inventory"), "image": sort["image"]}
    for filename in ASSETS.glob("*.png"):
        shutil.copyfile(filename, directory / filename.name)
    for name in result["equipment_label_samples"]:
        if not (directory / name).exists():
            raise ValueError("Не найден образец букв Lv: " + name)
    if adaptive:
        from auto_layout import AutoLayout
        result["automatic_layout"] = True
        # Prove the current geometry but retain canonical samples/coordinates.
        # Each run will resolve physical positions afresh, without recalibration.
        AutoLayout(result, directory).resolve(image)
    return result
