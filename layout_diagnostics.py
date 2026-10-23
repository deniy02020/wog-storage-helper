"""Two bounded local grid crops from an existing frame; never capture or input.

No desktop, portrait, character name or chat is saved. Files stay in the local
profile and are overwritten; this module never transmits them anywhere.
"""
import json
import logging
from pathlib import Path
from auto_layout import affine_rect, LayoutError
from core import Rect


def save_grid_failure(directory, layout, image):
    try:
        matches = layout.locate_headers(image)
        crops, metadata = {}, {}
        for index, pane in enumerate(('inventory', 'storage')):
            match = matches[index]
            if match is None:
                continue
            rect = affine_rect(Rect(*layout.profile[pane]['rect']),
                Rect(*layout.profile['anchors'][index]['rect']), match)
            if not rect.fits(*image.size) or max(rect.width, rect.height) > 1000:
                continue
            bounds = Rect(max(0, rect.left-8), max(0, rect.top-8),
                          min(image.width, rect.right+8), min(image.height, rect.bottom+8))
            crops[pane] = image.crop(bounds.as_box()).convert('RGB')
            metadata[pane] = {'rect': list(rect.as_box()), 'crop': list(bounds.as_box()), 'scale': match.scale}
        if not crops:
            return
        target = Path(directory) / 'diagnostics'
        target.mkdir(exist_ok=True)
        for pane, crop in crops.items():
            crop.save(target / (pane + '-grid.png'), format='PNG')
        (target / 'grid.json').write_text(json.dumps(metadata), encoding='utf-8')
    except (OSError, ValueError, LayoutError):
        logging.info('Local grid diagnostic could not be saved')
