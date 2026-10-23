"""Authenticated narrow T4/T5/T6 frame patterns prove animated grid gutters.

This is an alternate pixel proof, not a color exemption. Every accepted glow
requires the same model's complete frame pattern AND gutter evidence on the
current frame. Only individually proved gutter pixels contribute evidence.
Item icons, labels, centers and arbitrary pink pixels never establish a grid.
"""
from __future__ import annotations

import functools
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import re

import numpy as np
from PIL import Image

from core import Rect


FULL_OFFSETS = {"frame_top": [6, 0, 43, 3], "frame_bottom_left": [0, 51, 4, 60],
    "frame_bottom_right": [56, 51, 60, 60], "frame_left": [0, 12, 3, 48],
    "frame_right": [57, 12, 60, 48], "halo_top": [-20, -4, 64, 0],
    "halo_bottom": [-20, 60, 64, 64], "halo_left": [-4, -20, 0, 80],
    "halo_right": [60, -20, 64, 80]}
CAP_OFFSETS = {"frame_top": [6, 0, 43, 3], "halo_top": [-20, -4, 64, 0],
    "halo_left": [-4, -12, 0, 3], "halo_right": [60, -12, 64, 3]}
BOTTOM_CAP_OFFSETS = {key: FULL_OFFSETS[key] for key in ("frame_bottom_left", "frame_bottom_right",
                                                      "halo_bottom", "halo_left", "halo_right")}
MAX_PATCH_RMSE = 10.0


def _frame_pattern_matches(sample, actual):
    """Preserve spatial contrast under bounded RGB lighting/contrast changes.

    Neighbour glow is rendered over the opaque frame too. A uniform overlay
    cannot pass: both images must retain the reference's textured contrast and
    every pixel participates in the same RMSE10 spatial-pattern comparison.
    """
    offset = (actual-sample).mean(axis=(0, 1))
    reference = sample-sample.mean(axis=(0, 1), keepdims=True)
    observed = actual-actual.mean(axis=(0, 1), keepdims=True)
    energy = float(np.sum(reference*reference))
    contrast = float(np.sum(reference*observed))/max(energy, 1.)
    return (np.max(np.abs(offset)) <= 40
            and .65 <= contrast <= 1.5
            and np.mean(np.std(sample, axis=(0, 1))) >= 8
            and np.mean(np.std(actual, axis=(0, 1))) >= 8
            and float(np.sqrt(np.mean((observed-reference*contrast)**2))) <= MAX_PATCH_RMSE)


def _frame_rail_matches(sample, actual):
    """A vertical metal rail retains its cross-section under a glow gradient.

    Remove only the row-wise illumination shared by its three columns. Every
    cross-section and RGB channel still participates. Flat/covered rails have
    no surviving cross-section contrast and cannot supply geometry evidence.
    """
    reference = sample-sample.mean(axis=1, keepdims=True)
    observed = actual-actual.mean(axis=1, keepdims=True)
    shift = actual.mean(axis=1)-sample.mean(axis=1)
    gain = np.sum(reference*observed, axis=(0, 1))/np.maximum(np.sum(reference*reference, axis=(0, 1)), 1.)
    return (np.max(np.abs(shift)) <= 80
            and bool(np.all((gain >= .25) & (gain <= 1.5))) and float(np.sqrt(np.mean(reference**2))) >= 8
            and float(np.sqrt(np.mean(observed**2))) >= 8
            and float(np.sqrt(np.mean((observed-reference*gain)**2))) <= MAX_PATCH_RMSE)


def _halo_chroma_pixels(sample, actual, gap_color):
    """Glow brightness/saturation may pulse while an opaque frame stays put.

    This alternate proof is only used AFTER that same cue's whole opaque frame
    and both side-rail patterns match. It contributes individual four-pixel gutter
    pixels, never frame/center pixels. Dark overlays and unrelated hues fail.
    """
    background = np.asarray(gap_color, dtype=np.float32)
    source, observed = sample-background, actual-background
    source_chroma = source-source.mean(axis=2, keepdims=True)
    observed_chroma = observed-observed.mean(axis=2, keepdims=True)
    energy = np.sum(source_chroma*source_chroma, axis=2)
    gain = np.sum(source_chroma*observed_chroma, axis=2)/np.maximum(energy, 1.)
    residual = np.max(np.abs(observed_chroma-source_chroma*gain[..., None]), axis=2)
    return ((energy >= 144) & (gain >= .25) & (gain <= 4)
            & (residual <= 6) & (observed.min(axis=2) >= -6))


def _halo_raster(array):
    """Symmetric sigma1.5 physical-pixel raster normalization, without masks.

    Every RGB pixel of BOTH matched tiny patches participates. Full cues may
    use this only after their two opposite opaque side rails independently
    pass RAW RMSE10 on the SAME cue. Headers, raw lattice, grid and item proof
    retain their original pixels/thresholds. No clipped cap gets this route.
    """
    positions = np.arange(-5, 6, dtype=np.float32)
    kernel = np.exp(-positions*positions/(2*1.5*1.5))
    kernel /= kernel.sum()
    result = array
    for axis in (array.ndim-3, array.ndim-2):
        pads = [(0, 0)]*array.ndim
        pads[axis] = (5, 5)
        source = np.pad(result, pads, mode="edge")
        normalized = np.zeros_like(result)
        for index, weight in enumerate(kernel):
            slices = [slice(None)]*array.ndim
            slices[axis] = slice(index, index+array.shape[axis])
            normalized += source[tuple(slices)]*weight
        result = normalized
    return result


class InventoryHaloModels:
    def __init__(self, directory):
        directory = Path(directory).resolve()
        manifest = directory / "inventory_halos.json"
        if manifest.stat().st_size > 1024 * 1024:
            raise ValueError("Inventory halo manifest exceeds its bounded size")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self._initialize(data, lambda name: (directory / name).read_bytes())

    def _initialize(self, data, read_resource):
        if (not isinstance(data, dict) or type(data.get("schema")) is not int or data.get("schema") not in (1,2) or data.get("tier") != 4
                or data.get("body_size") != [60, 60]
                or data.get("max_patch_rmse") != MAX_PATCH_RMSE
                or data.get("full_offsets") != FULL_OFFSETS
                or data.get("cap_offsets") != CAP_OFFSETS
                or data.get("bottom_cap_offsets") != BOTTOM_CAP_OFFSETS):
            raise ValueError("Inventory halo proof geometry or thresholds differ")
        generalized = data["schema"] == 2
        supplemental = data.get("supplemental_models", [])
        if generalized:
            if (data.get("supplemental_tiers") != [5,6]
                    or any(type(tier) is not int for tier in data["supplemental_tiers"])
                    or not isinstance(supplemental,list) or not 1<=len(supplemental)<=128):
                raise ValueError("Generalized halo tiers or supplemental models differ")
        elif "supplemental_tiers" in data or "supplemental_models" in data:
            raise ValueError("Legacy halo schema cannot declare supplemental tiers")
        records, models = data.get("records"), data.get("models")
        if (not isinstance(records, list) or not 1 <= len(records) <= (1200 if generalized else 600)
                or not isinstance(models, list) or not 1 <= len(models) <= 128):
            raise ValueError("Inventory halo sample count is unbounded")
        self.samples = {}
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Inventory halo sample must be an object")
            name = record.get("image")
            if (not isinstance(name, str) or not re.fullmatch(r"inventory_halo_T[456]_\d{3}\.png" if generalized else r"inventory_halo_T4_\d{3}\.png", name)
                    or name in self.samples):
                raise ValueError("Inventory halo sample filename is invalid")
            content = read_resource(name)
            if (not isinstance(content, bytes) or len(content) > 20000
                    or hashlib.sha256(content).hexdigest() != record.get("sha256")):
                raise ValueError("Inventory halo sample checksum differs")
            with Image.open(BytesIO(content)) as source:
                if source.format != "PNG":
                    raise ValueError("Inventory halo sample must contain PNG pixels")
                sample = source.convert("RGB")
            rect = Rect(*record.get("source_rect", ()))
            if (sample.size != tuple(record.get("dimensions", ()))
                    or sample.size != (rect.width, rect.height)
                    or min(sample.size) < 1 or max(sample.size) > 100
                    or sample.width * sample.height > 500
                    or hashlib.sha256(sample.tobytes()).hexdigest() != record.get("pixels_sha256")):
                raise ValueError("Inventory halo lane dimensions or pixels differ")
            self.samples[name] = sample
        self.models = {"full": [], "cap": [], "bottom_cap": []}
        tiers_seen=set()
        by_name={record["image"]:record for record in records}
        for model in [*models,*supplemental]:
            if not isinstance(model, dict):
                raise ValueError("Inventory halo model must be an object")
            extra = "tier" in model
            tier=model.get("tier",4)
            if (not extra and model not in models) or (extra and (model in models or type(tier) is not int or tier not in (5,6))):
                raise ValueError("Halo model tier does not fit its declared catalog")
            if extra:
                tiers_seen.add(tier)
                proof=model.get("source_classification")
                body=model.get("source_body_rect")
                if (model.get("kind")!="full" or not isinstance(proof,dict) or proof.get("tier")!=tier
                        or type(proof.get("tier")) is not int
                        or any(type(proof.get(key)) not in (int,float) or not math.isfinite(proof[key]) for key in ("rmse","margin"))
                        or not 0<=proof["rmse"]<=14 or proof["margin"]<5
                        or not isinstance(body,list) or len(body)!=4 or any(type(v) is not int for v in body)
                        or body[2]-body[0]!=60 or body[3]-body[1]!=60
                        or not isinstance(model.get("source_image"),str)
                        or not isinstance(model.get("source_sha256"),str)
                        or not re.fullmatch(r"[a-f0-9]{64}",model["source_sha256"])):
                    raise ValueError("Supplemental halo lacks genuine complete-frame tier proof")
            kind, patches = model.get("kind"), model.get("patches")
            offsets = {"full": FULL_OFFSETS, "cap": CAP_OFFSETS,
                       "bottom_cap": BOTTOM_CAP_OFFSETS}.get(kind)
            if (offsets is None or isinstance(model.get("canonical_scale"), bool)
                    or model.get("canonical_scale") != 1.0
                    or not isinstance(patches, dict) or set(patches) != set(offsets)):
                raise ValueError("Inventory halo model has unproved lanes")
            selected = {}
            for label, name in patches.items():
                if not isinstance(name,str) or not name.startswith(f"inventory_halo_T{tier}_"):
                    raise ValueError("Halo patch tier differs from its same-cue model")
                sample = self.samples.get(name)
                rect = Rect(*offsets[label])
                if sample is None or sample.size != (rect.width, rect.height):
                    raise ValueError("Inventory halo lane does not fit its model")
                if extra:
                    record=by_name[name]
                    expected=rect.translated(body[0],body[1])
                    if (record.get("source_image")!=model["source_image"]
                            or record.get("source_sha256")!=model["source_sha256"]
                            or tuple(record.get("source_rect",()))!=expected.as_box()):
                        raise ValueError("Supplemental halo patches mix source cues or body geometry")
                selected[label] = sample
            self.models[kind].append(selected)
        if not all(self.models.values()):
            raise ValueError("Both full and clipped-cap halo proofs are required")
        if generalized and tiers_seen!={5,6}:
            raise ValueError("Both declared supplemental halo tiers are required")
        self.native_models = []
        native = data.get('native_models', [])
        if (not isinstance(native, list) or len(native)>32 or (native and not generalized)):
            raise ValueError('Invalid native halo model collection')
        for model in native:
            if not isinstance(model,dict):
                raise ValueError('Native halo model must be an object')
            scale,tier=model.get('canonical_scale'),model.get('tier')
            proof=model.get('source_classification')
            body,cell=model.get('source_body_rect'),model.get('source_cell_rect')
            if (type(scale) not in (int,float) or not math.isfinite(scale) or not .35<=scale<=1.5
                    or type(tier) is not int or tier not in (4,5,6) or model.get('kind')!='full'
                    or not isinstance(proof,dict) or type(proof.get('tier')) is not int or proof['tier']!=tier
                    or any(type(proof.get(k)) not in (int,float) or not math.isfinite(proof[k]) for k in ('rmse','margin'))
                    or not 0<=proof['rmse']<=14 or proof['margin']<5
                    or not isinstance(body,list) or len(body)!=4 or any(type(v) is not int for v in body)
                    or not isinstance(cell,list) or len(cell)!=4 or any(type(v) is not int for v in cell)
                    or not isinstance(model.get('source_image'),str)
                    or not re.fullmatch(r'[a-f0-9]{64}',str(model.get('source_sha256','')))):
                raise ValueError('Native halo lacks independent complete-cell tier evidence')
            body,cell=Rect(*body),Rect(*cell)
            if (body.width!=round(60*scale) or body.height!=round(60*scale)
                    or any(abs(v-65*scale)>1.1 for v in (cell.width,cell.height))
                    or not cell.left<=body.left<body.right<=cell.right
                    or not cell.top<=body.top<body.bottom<=cell.bottom):
                raise ValueError('Native halo body contradicts its full cell')
            patches=model.get('patches')
            if not isinstance(patches,dict) or set(patches)!=set(FULL_OFFSETS):
                raise ValueError('Native halo requires every complete frame and gutter lane')
            selected={}
            for label,name in patches.items():
                if not isinstance(name,str) or not name.startswith(f'inventory_halo_T{tier}_'):
                    raise ValueError('Native halo patch tier differs')
                sample=self.samples.get(name)
                relative=Rect(*(round(v*scale) for v in FULL_OFFSETS[label]))
                expected=relative.translated(body.left,body.top)
                record=by_name.get(name,{})
                if (sample is None or sample.size!=(relative.width,relative.height)
                        or record.get('source_image')!=model['source_image']
                        or record.get('source_sha256')!=model['source_sha256']
                        or tuple(record.get('source_rect',()))!=expected.as_box()):
                    raise ValueError('Native halo patches mix sources or pixel geometry')
                selected[label]=(relative,np.asarray(sample,dtype=np.float32))
            self.native_models.append((round(scale,6),selected))
        self._rendered = {}
        self._normalized = {}

    def rendered(self, kind, scale):
        key = kind, round(scale, 6)
        if key not in self._rendered:
            offsets = {"full": FULL_OFFSETS, "cap": CAP_OFFSETS,
                       "bottom_cap": BOTTOM_CAP_OFFSETS}[kind]
            values = {}
            for label, box in offsets.items():
                rect = Rect(*(round(value * scale) for value in box))
                if min(rect.width, rect.height) < 1:
                    raise ValueError("Inventory halo lanes are below physical pixel resolution")
                arrays = [np.asarray(model[label].resize((rect.width, rect.height),
                    Image.Resampling.BILINEAR), dtype=np.float32) for model in self.models[kind]]
                values[label] = (rect, np.stack(arrays))
            if len(self._rendered) >= 12:
                self._rendered.clear()
                self._normalized.clear()
            self._rendered[key] = values
        return self._rendered[key]

    def match(self, image_array, viewport, kind, origin, scale, *, gap_color=None):
        # Native rasters require both side-rail patterns before any gutter
        # contribution. Each cue independently proves
        # its entire frame; parts from different cues never prove a frame.
        # Only individually matched gutter lanes can contribute evidence.
        if kind=='full':
            native_lanes=[]
            for native_index,(native_scale,patches) in enumerate(getattr(self,'native_models',())):
                if round(scale,6)!=native_scale:
                    continue
                frame_patches=sorted(((label,value) for label,value in patches.items()
                    if not label.startswith('halo_')),
                    key=lambda pair:pair[0] not in ('frame_left','frame_right'))
                for label,(relative,sample) in frame_patches:
                    target=relative.translated(*origin)
                    if not (viewport.left<=target.left<target.right<=viewport.right
                            and viewport.top<=target.top<target.bottom<=viewport.bottom
                            and target.fits(image_array.shape[1],image_array.shape[0])):
                        break
                    actual=image_array[target.top:target.bottom,target.left:target.right]
                    if (float(np.sqrt(np.mean((sample-actual)**2)))>MAX_PATCH_RMSE
                            and not _frame_pattern_matches(sample, actual)):
                        if label in ('frame_left','frame_right'):
                            if _frame_rail_matches(sample, actual):
                                continue
                            break
                        key=('native',native_index,label)
                        normalized=self._normalized.get(key)
                        if normalized is None:
                            normalized=_halo_raster(sample)
                            if len(self._normalized)>=256:self._normalized.clear()
                            self._normalized[key]=normalized
                        observed_normalized = _halo_raster(actual)
                        difference = observed_normalized-normalized
                        if float(np.sqrt(np.mean(difference**2)))>MAX_PATCH_RMSE:
                            if not _frame_pattern_matches(normalized, observed_normalized):
                                break
                else:
                    # A neighboring item may change ONE glow lane. As with
                    # clipped caps, contribute only independently matched raw
                    # lanes on the SAME fully proved opaque frame. A missing
                    # lane stays unproved and must pass the final grid check.
                    lanes=[]
                    for label,(relative,sample) in patches.items():
                        if not label.startswith('halo_'):
                            continue
                        target=relative.translated(*origin)
                        # Overhangs include neighbouring icons and NEW badges.
                        # They cannot be authenticated by this item's frame.
                        # Prove only the lane alongside this same opaque body;
                        # the unchanged whole-grid check still needs corners
                        # and neighbours to supply their own pixel evidence.
                        body = round(60*scale)
                        own = (Rect(origin[0], target.top, origin[0]+body, target.bottom)
                               if label in ('halo_top', 'halo_bottom') else
                               Rect(target.left, origin[1], target.right, origin[1]+body))
                        clipped=Rect(max(target.left,viewport.left),max(target.top,viewport.top),
                                     min(target.right,viewport.right),min(target.bottom,viewport.bottom))
                        clipped=Rect(max(clipped.left,own.left),max(clipped.top,own.top),
                                     min(clipped.right,own.right),min(clipped.bottom,own.bottom))
                        if clipped.width<=0 or clipped.height<=0:
                            continue
                        sample=sample[clipped.top-target.top:clipped.bottom-target.top,
                                      clipped.left-target.left:clipped.right-target.left]
                        target=clipped
                        if not target.fits(image_array.shape[1],image_array.shape[0]):
                            continue
                        actual=image_array[target.top:target.bottom,target.left:target.right]
                        accepted=float(np.sqrt(np.mean((sample-actual)**2)))<=MAX_PATCH_RMSE
                        if not accepted:
                            key=('native',native_index,label,
                                 clipped.top-(origin[1]+relative.top),clipped.bottom-(origin[1]+relative.top),
                                 clipped.left-(origin[0]+relative.left),clipped.right-(origin[0]+relative.left))
                            normalized=self._normalized.get(key)
                            if normalized is None:
                                normalized=_halo_raster(sample)
                                if len(self._normalized)>=256:self._normalized.clear()
                                self._normalized[key]=normalized
                            accepted=float(np.sqrt(np.mean((normalized-_halo_raster(actual))**2)))<=MAX_PATCH_RMSE
                        if accepted:
                            lanes.append(target)
                        elif gap_color is not None:
                            from auto_layout import _runs
                            visible = _halo_chroma_pixels(sample, actual, gap_color)
                            # A shared gutter can contain the neighbour's glow
                            # in the reference. The opposite edge of THIS SAME
                            # authenticated item supplies its radial hue too.
                            opposite = {'halo_left': 'halo_right', 'halo_right': 'halo_left',
                                        'halo_top': 'halo_bottom', 'halo_bottom': 'halo_top'}[label]
                            alternate = patches[opposite][1]
                            alternate = np.flip(alternate, axis=1 if label in ('halo_left', 'halo_right') else 0)
                            full_target = relative.translated(*origin)
                            alternate = alternate[target.top-full_target.top:target.bottom-full_target.top,
                                                  target.left-full_target.left:target.right-full_target.left]
                            visible |= _halo_chroma_pixels(alternate, actual, gap_color)
                            for y, line in enumerate(visible):
                                lanes.extend(Rect(target.left+a, target.top+y,
                                                  target.left+b, target.top+y+1)
                                             for a, b in _runs(line))
                    native_lanes.extend(lanes)
            if native_lanes:
                return list(dict.fromkeys(native_lanes))
        rendered = self.rendered(kind, scale)
        surviving = np.ones(len(self.models[kind]), dtype=bool)
        halo_rects = []
        observed = []
        raw_rails = {}
        raw_errors = {}
        for label, (relative, samples) in rendered.items():
            target = relative.translated(*origin)
            if label.startswith("halo_"):
                clipped = Rect(max(target.left, viewport.left), max(target.top, viewport.top),
                               min(target.right, viewport.right), min(target.bottom, viewport.bottom))
                if clipped.width <= 0 or clipped.height <= 0:
                    continue  # No gutter outside this viewport can establish proof.
                samples = samples[:, clipped.top-target.top:clipped.bottom-target.top,
                                  clipped.left-target.left:clipped.right-target.left]
                target = clipped
                halo_rects.append(target)
            if not target.fits(image_array.shape[1], image_array.shape[0]):
                return None
            actual = image_array[target.top:target.bottom, target.left:target.right]
            errors = np.sqrt(np.mean((samples - actual) ** 2, axis=(1, 2, 3)))
            raw_errors[label] = errors
            surviving &= errors <= MAX_PATCH_RMSE
            observed.append((label, samples, actual, target))
            if label in ("frame_left", "frame_right"):
                raw_rails[label] = errors <= MAX_PATCH_RMSE
        if halo_rects and surviving.any():
            return halo_rects  # Complete original raw proof remains sufficient.
        if kind == "cap" and halo_rects:
            # A clipped lower item can illuminate one separator while another
            # side contains different preceding-row pixels. Authenticate the
            # WHOLE visible top rail and top halo on one existing cue, then
            # contribute only halo lanes independently matched on THAT cue.
            # A failed side remains raw evidence, never an invented repair.
            targets = {label: target for label, _, _, target in observed}
            rail, top = targets.get("frame_top"), targets.get("halo_top")
            expected_top = rendered["halo_top"][0].translated(*origin)
            if (rail is None or top is None or rail.left < viewport.left
                    or rail.right > viewport.right or rail.top < viewport.top
                    or rail.bottom > viewport.bottom
                    or top.top != expected_top.top or top.bottom != expected_top.bottom):
                return None
            base = ((raw_errors["frame_top"] <= MAX_PATCH_RMSE)
                    & (raw_errors["halo_top"] <= MAX_PATCH_RMSE))
            if not base.any():
                return None
            halo_labels = [label for label in targets if label.startswith("halo_")]
            support = sum((raw_errors[label] <= MAX_PATCH_RMSE).astype(int)
                          for label in halo_labels)
            support[~base] = -1
            chosen = int(np.argmax(support))  # Stable catalogue-order tie break.
            return [targets[label] for label in halo_labels
                    if raw_errors[label][chosen] <= MAX_PATCH_RMSE]
        if kind != "full" or not halo_rects or len(raw_rails) != 2:
            return None
        surviving = raw_rails["frame_left"] & raw_rails["frame_right"]
        if not surviving.any():
            return None  # Cheap raw bilateral veto before ANY normalization.
        for label, samples, actual, target in observed:
            relative = rendered[label][0]
            full_target = relative.translated(*origin)
            clipping = (target.top-full_target.top, target.bottom-full_target.top,
                        target.left-full_target.left, target.right-full_target.left)
            key = kind, round(scale, 6), label, clipping
            normalized = self._normalized.get(key)
            if normalized is None:
                normalized = _halo_raster(samples)
                if len(self._normalized) >= 256:
                    self._normalized.clear()
                self._normalized[key] = normalized
            errors = np.sqrt(np.mean((normalized-_halo_raster(actual))**2, axis=(1, 2, 3)))
            surviving &= errors <= MAX_PATCH_RMSE
            if not surviving.any():
                return None
        return halo_rects if surviving.any() else None


@functools.lru_cache(maxsize=1)
def default_models():
    from paths import resource_path
    return InventoryHaloModels(resource_path("preset_assets"))


def verify_inventory_halo_resources(document, read_resource) -> set[str]:
    """Authenticate bundled model geometry and every declared tiny PNG lane."""
    model = InventoryHaloModels.__new__(InventoryHaloModels)
    model._initialize(document, read_resource)
    return set(model.samples)


def _column_candidates(runs, width, columns, scale):
    """Suggest missing positions from three raw columns; never add mask pixels.

    These positions only select frame/halo patches to authenticate below.
    The final original whole-grid and every-segment tests remain mandatory.
    """
    from auto_layout import LayoutError
    observed=[run for run in runs if run[1]>max(2,round(4*scale))]
    if not 3<=len(observed)<columns or any(b-a>math.ceil(10*scale) for a,b in observed):
        raise LayoutError('Inventory halo lacks bounded raw column candidates')
    centers=np.array([(a+b-1)/2 for a,b in observed])
    expected=width/columns
    indices=np.rint((centers+3*scale)/expected).astype(int)-1
    if (np.any(indices<0) or np.any(indices>=columns)
            or np.any(np.diff(indices)<1) or np.any(np.diff(indices)>3)):
        raise LayoutError('Inventory halo raw column indices are ambiguous')
    pitch,offset=np.polyfit(indices,centers,1)
    if (abs(pitch-expected)>max(1.5*scale,expected*.025)
            or np.max(abs(centers-(offset+pitch*indices)))>1.8*scale
            or abs(offset+pitch*(columns-1)-(width-3*scale))>6*scale):
        raise LayoutError('Inventory halo raw column candidates contradict the header')
    known=dict(zip(indices,observed))
    return [known[index] if index in known else
            (round(offset+pitch*index),round(offset+pitch*index)+1)
            for index in range(columns)]


def prove_inventory_halo_mask(image, rect, rows, columns, gap_color, scale, mask, *, allow_shifted=False):
    """Return only model-supported gutter evidence, or reject the whole grid.

    Lattice candidates require original neutral RGB in at least three columns
    on every observed band and three whole row segments at every column. One
    skipped integer band can suggest a candidate only; authenticated frame and
    halo lanes must then restore the unchanged ALL-row/per-segment proof. A
    matched halo cannot invent a complete row, a cropped cell or an endpoint.
    """
    from auto_layout import LayoutError, _runs
    if rows not in (7, 8) or columns != 5:
        raise LayoutError("Inventory halo proof requires its bounded inventory lattice")
    def column_lattice(runs):
        runs = [run for run in runs if run[1] > max(2, round(4*scale))]
        if len(runs) != columns or any(b-a > math.ceil(10*scale) for a, b in runs):
            raise LayoutError("Inventory halo lacks independent column lattice evidence")
        centers = np.array([(a+b-1)/2 for a,b in runs])
        pitch, offset = np.polyfit(np.arange(columns), centers, 1)
        expected = rect.width/columns
        if (abs(pitch-expected) > max(1.5*scale, expected*.025)
                or np.max(abs(centers-(offset+pitch*np.arange(columns)))) > 1.8*scale
                or abs(centers[-1]-(rect.width-3*scale)) > 6*scale):
            raise LayoutError("Inventory halo column lattice contradicts header geometry")
        return runs

    try:
        xs = column_lattice(_runs(mask.mean(axis=0) >= .78))
    except LayoutError:
        # Several authenticated halos can trim, rather than remove, a long
        # projection run. A run count alone cannot establish its midpoint.
        # Keep .78 and every pitch/phase bound: at least three untouched row
        # segments independently establish ALL column positions before any
        # matched halo contributes pixels. Final full-grid proof is unchanged.
        x_support = np.stack([mask[rect.height*r//rows:rect.height*(r+1)//rows].mean(axis=0)
                              for r in range(rows)])
        observed = _runs((x_support >= .78).sum(axis=0) >= 3)
        try:
            xs = column_lattice(observed)
        except LayoutError:
            # No invented separator pixels: a missing column still has to be
            # restored by complete same-cue frame AND gutter evidence below.
            xs = _column_candidates(observed,rect.width,columns,scale)
    support = np.stack([mask[:, rect.width*c//columns:rect.width*(c+1)//columns].mean(axis=1)
                        for c in range(columns)])
    from inventory_viewport import _lattice, _merged_runs
    # The existing bounded1px raster-fragment merge suggests a band only;
    # no pixel in that gap becomes separator evidence until exact cue proof.
    ys = _merged_runs(((support >= .78).sum(axis=0) >= 3).astype(float),scale)
    valid_count = rows-2 <= len(ys) <= rows+1 if allow_shifted else rows-1 <= len(ys) <= rows
    if not valid_count or any(b-a > math.ceil(10*scale) for a, b in ys):
        raise LayoutError("Inventory halo lacks every independently observed row")
    complete = [(a, b) for a, b in ys if a > 0 and b < rect.height]
    if len(complete) < 3:
        raise LayoutError("Inventory halo cannot establish complete row pitch")
    expected = rect.height / rows
    pitch, offset = _lattice(ys, rect.height, expected, scale, candidate_gaps=True)
    if (not allow_shifted and abs((ys[-1][0]+ys[-1][1]-1)/2-(rect.height-3*scale)) > 6*scale):
        raise LayoutError("Inventory halo lattice contradicts header geometry")
    first = (offset+3*scale) % pitch
    if min(first, pitch-first) <= max(.75, 1.25*scale):
        first = 0.
    candidates = set()
    # Every final grid check uses the original whole-projection midpoint.
    # Its raster midpoint can differ1px from independently supported bands.
    # Inspect both actual observations before choosing authenticated cues.
    candidate_bands=set(ys)|set(_runs(mask.mean(axis=1)>=.78))
    candidate_ys={(a+b-1)//2 for a,b in candidate_bands}
    # A partial authentic repair may shift the final band midpoint1px, or
    # restore the one skipped integer band already supported by raw lattice.
    # The fitted positions propose nearby frame cues ONLY. They do not add
    # mask pixels, count rows or excuse a single unsupported final segment.
    for index in range(-1,rows+2):
        center=round(offset+index*pitch)
        candidate_ys.update(y for y in (center-1,center,center+1) if 0<=y<rect.height)
    for y in sorted(candidate_ys):
        row = round((y+3*scale-first)/pitch)-1 if allow_shifted else min(rows-1, int(y / expected))
        for col in range(columns):
            if mask[y, rect.width*col//columns:rect.width*(col+1)//columns].mean() < .72:
                for candidate_row in (row, row+1):
                    for candidate_col in (col, col+1):
                        if candidate_col < columns and -1 <= candidate_row <= rows:
                            candidates.add((candidate_row, candidate_col))
    for col, (a, b) in enumerate(xs):
        x = (a+b-1)//2
        for nominal_row in range(rows):
            row = math.floor(((nominal_row+.5)*expected-first)/pitch) if allow_shifted else nominal_row
            if mask[rect.height*nominal_row//rows:rect.height*(nominal_row+1)//rows, x].mean() < .72:
                for candidate_row in (row, row+1):
                    for candidate_col in (col, col+1):
                        if candidate_col < columns and -1 <= candidate_row <= rows:
                            candidates.add((candidate_row, candidate_col))
    if not candidates:
        raise LayoutError("No complete cell requires an authenticated halo proof")
    models = default_models()
    pixels = np.asarray(image.convert("RGB"), dtype=np.float32)
    proved = mask.copy()
    radius = max(2, math.ceil(5*scale))
    matched = False
    for row, col in sorted(candidates):
        base_y = first+row*pitch if allow_shifted else row*expected
        kind = ("bottom_cap" if base_y < 0 else
                "cap" if base_y+60*scale > rect.height else "full")
        base = (rect.left + round(col*rect.width/columns), rect.top + round(base_y))
        best = None
        offsets = sorted(range(-radius, radius+1), key=lambda value: (abs(value), value))
        for dy in offsets:
            for dx in offsets:
                origin = base[0]+dx, base[1]+dy
                if kind == "cap":
                    # A cap is a CLIPPED cell, never part of the subsequently
                    # proved complete-row input crop. Its top rail must show.
                    if not (origin[1] <= rect.bottom+radius and origin[1]+60*scale > rect.bottom):
                        continue
                elif kind == "bottom_cap":
                    if not (origin[1] < rect.top and origin[1]+51*scale >= rect.top):
                        continue
                elif origin[1]+round(60*scale) > rect.bottom+1:
                    continue
                lanes = models.match(pixels, rect, kind, origin, scale, gap_color=gap_color)
                if lanes is not None:
                    best = lanes
                    break
            if best is not None:
                break
        if best is not None:
            matched = True
            for lane in best:
                proved[lane.top-rect.top:lane.bottom-rect.top,
                       lane.left-rect.left:lane.right-rect.left] = True
    if not matched:
        raise LayoutError("Missing or occluded gutters have no authenticated tier frame/halo proof")
    return proved
