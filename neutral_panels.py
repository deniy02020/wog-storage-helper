"""Read-only paired icon recognition, independent of translated captions.

Coordinates are derived from a unique visible icon and independently checked
against a second distant icon. AutoLayout still proves both complete grids.
"""
from functools import lru_cache
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
from PIL import Image
from core import Rect, image_rmse

OFFSETS = {'inventory': ((585, 1, 703, 40), (0, 0, 33, 41)),
           'storage': ((-1, 595, 159, 625), (379, 5, 405, 33))}


@lru_cache(maxsize=1)
def load_models():
    from paths import resource_path
    assets = resource_path('preset_assets')
    path = assets / 'neutral_panels.json'
    if not path.is_file() or path.stat().st_size > 32000:
        raise ValueError('Missing bounded neutral panel model')
    model = json.loads(path.read_bytes())
    return verify_model(model, lambda name: (assets/name).read_bytes())


def verify_model(model, read_resource):
    """Shared strict resource validation for source, installer and runtime."""
    records = model.get('records')
    if model.get('schema') != 1 or not isinstance(records, list) or len(records) != 6:
        raise ValueError('Invalid neutral panel model')
    result, ids = [], set()
    for record in records:
        spec = dict(record)
        pane, scale, identifier = spec['pane'], spec['canonical_scale'], spec['id']
        if (pane not in ('inventory', 'storage') or not isinstance(identifier, str)
                or identifier in ids or isinstance(scale, bool) or not isinstance(scale, (int, float))
                or not math.isfinite(scale) or not .35 <= scale <= 1.5):
            raise ValueError('Invalid neutral panel identity')
        anchor = Rect(*spec['source_anchor_rect'])
        with Image.open(BytesIO(read_resource(pane + '_header.png'))) as original:
            fingerprint = hashlib.sha256(original.convert('RGB').tobytes()).hexdigest()
            dimensions = original.size
        if spec['canonical_pixels_sha256'] != fingerprint or len(spec['patches']) != 2:
            raise ValueError('Neutral panel model does not belong to the stock geometry')
        if any(abs(actual - expected*scale) > 1.1 for actual, expected in zip((anchor.width, anchor.height), dimensions)):
            raise ValueError('Neutral anchor contradicts its canonical dimensions')
        patches = []
        for patch, offset in zip(spec['patches'], OFFSETS[pane]):
            item = dict(patch)
            name = item['image']
            if (not isinstance(name, str) or Path(name).name != name or '/' in name
                    or '\\' in name or not name.startswith('neutral_') or not name.endswith('.png')):
                raise ValueError('Unsafe neutral panel crop')
            rect = Rect(*item['source_rect'])
            expected = Rect(anchor.left+round(offset[0]*scale), anchor.top+round(offset[1]*scale),
                            anchor.left+round(offset[2]*scale), anchor.top+round(offset[3]*scale))
            if rect != expected:
                raise ValueError('Neutral icon geometry differs from its reviewed offsets')
            if min(rect.width, rect.height) < 3 or rect.width > 240 or rect.height > 80:
                raise ValueError('Unbounded neutral panel crop')
            content = read_resource(name)
            if len(content) > 32000 or hashlib.sha256(content).hexdigest() != item['sample_sha256']:
                raise ValueError('Neutral panel crop changed')
            with Image.open(BytesIO(content)) as image:
                if image.size != (rect.width, rect.height):
                    raise ValueError('Neutral panel dimensions changed')
                item['sample'] = image.convert('RGB')
            patches.append(item)
        spec['patches'] = patches
        ids.add(identifier)
        result.append(spec)
    return tuple(result)


def supports(sample, pane):
    fingerprint = hashlib.sha256(sample.convert('RGB').tobytes()).hexdigest()
    return any(r['pane'] == pane and r['canonical_pixels_sha256'] == fingerprint for r in load_models())


def validate(image, sample, pane, rect, scale, phase, identifier):
    from auto_layout import LayoutError, ScaleMatch, affine_rect, control_error
    spec = next((r for r in load_models() if r['id'] == identifier and r['pane'] == pane), None)
    if spec is None or hashlib.sha256(sample.convert('RGB').tobytes()).hexdigest() != spec['canonical_pixels_sha256']:
        raise LayoutError('Unknown neutral panel evidence')
    relative = scale / spec['canonical_scale']
    source_anchor = Rect(*spec['source_anchor_rect'])
    if (rect.width, rect.height) != (round(source_anchor.width*relative), round(source_anchor.height*relative)):
        raise LayoutError('Neutral panel anchor dimensions changed')
    for patch in spec['patches']:
        source = Rect(*patch['source_rect'])
        physical = affine_rect(source, source_anchor, ScaleMatch(rect, relative, 0, 0))
        try:
            valid = physical.fits(*image.size) and control_error(image, physical, patch['sample'],
                source, source_anchor, rect, relative, phase) <= 10
        except ValueError:
            valid = False
        if not valid:
            raise LayoutError('Neutral panel icons changed or are occluded')


def locate(image, sample, pane, scales):
    from auto_layout import LayoutError, ScaleMatch, _match_template_family, _same_object
    candidates = []
    fingerprint = hashlib.sha256(sample.convert('RGB').tobytes()).hexdigest()
    for spec in load_models():
        if spec['pane'] != pane or spec['canonical_pixels_sha256'] != fingerprint:
            continue
        relative_scales = tuple(sorted({1.0, *(s/spec['canonical_scale'] for s in scales
                                                if .35 <= s/spec['canonical_scale'] <= 1.5)}))
        primary = spec['patches'][0]
        found = _match_template_family(image, primary['sample'], scales=relative_scales)
        if found is None:
            continue
        source, anchor = Rect(*primary['source_rect']), Rect(*spec['source_anchor_rect'])
        delta = ((source.left-anchor.left)*found.scale, (source.top-anchor.top)*found.scale)
        left, top = found.rect.left-round(delta[0]), found.rect.top-round(delta[1])
        rect = Rect(left, top, left+round(anchor.width*found.scale), top+round(anchor.height*found.scale))
        phase = tuple(found.phase[i] + round(delta[i])-delta[i] for i in range(2))
        if any(abs(p) > .5 for p in phase):
            continue
        scale = found.scale*spec['canonical_scale']
        if not .35 <= scale <= 1.5 or not rect.fits(*image.size):
            continue
        try:
            validate(image, sample, pane, rect, scale, phase, spec['id'])
        except LayoutError:
            continue
        candidates.append(ScaleMatch(rect, scale, found.rmse, found.margin, phase, spec['id'], 'neutral'))
    candidates.sort(key=lambda found: found.rmse)
    if candidates and any(not _same_object(candidates[0].rect, other.rect) for other in candidates[1:]):
        raise LayoutError('Multiple independently proved neutral panels')
    return candidates[0] if candidates else None


def locate_storage_opener(image, sample, max_error):
    """Match the house icon only for the exact built-in storage button family."""
    from paths import resource_path
    from auto_layout import LayoutError, _match_template_family, _same_object, DEFAULT_SCALES, load_ui_variants
    with Image.open(resource_path('preset_assets/panel_storage_open.png')) as stock:
        stock = stock.convert('RGB')
    fingerprint = hashlib.sha256(stock.tobytes()).hexdigest()
    if hashlib.sha256(sample.convert('RGB').tobytes()).hexdigest() != fingerprint:
        return NotImplemented
    variants = [(stock, 1.0)] + [(r['sample'], r['canonical_scale']) for r in load_ui_variants()
        if r['kind'] == 'controls' and r['canonical_pixels_sha256'] == fingerprint]
    matches = []
    for button, native_scale in variants:
        # Left padding + house, stopping before the translated caption.
        icon = button.crop((0, 0, round(29*native_scale), button.height))
        scales = tuple(sorted({1.0, *(s/native_scale for s in DEFAULT_SCALES if .35 <= s/native_scale <= 1.5)}))
        found = _match_template_family(image, icon, scales=scales, max_rmse=max_error)
        if found is not None and .35 <= found.scale*native_scale <= 1.5:
            matches.append(found)
    matches.sort(key=lambda found: found.rmse)
    if matches and any(not _same_object(matches[0].rect, m.rect) for m in matches[1:]):
        raise LayoutError('Storage house icon is ambiguous')
    return matches[0].rect if matches else None
