"""Pinned offline OCR resources. Network use is an explicit BUILD command only."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

WHEEL_URL = ('https://github.com/simonflueckiger/tesserocr-windows_build/releases/download/'
             'tesserocr-v2.11.0-tesseract-5.5.3/tesserocr-2.11.0-cp313-cp313-win_amd64.whl')
WHEEL_SHA256 = '41958c5b8a030189e5733dc16317705a0fd7910f247fbca09d4c9a5bd152969b'
MODEL_MANIFEST_SHA256 = '555025672c9b890ee093798df27c6e54ca8791a8ae11cba6156e2d86d92cf850'
NATIVE_MANIFEST_SHA256 = 'd80443f68605ae9aaee96891366a112593c777fc51a1edb625d4a7498132b5b4'
MODELS = frozenset('eng rus fra spa ita ukr pol deu por ind tha jpn kor vie chi_sim tur chi_tra'.split())


def read_model(path, language):
    from paths import resource_path
    if language not in MODELS:
        raise ValueError('A single bundled OCR language is required')
    manifest_bytes = resource_path('ocr_models/manifest.json').read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MODEL_MANIFEST_SHA256:
        raise ValueError('Unreviewed OCR model manifest')
    record = next(r for r in json.loads(manifest_bytes)['models'] if r['path'] == language+'.traineddata')
    content = (Path(path)/(language+'.traineddata')).read_bytes()
    if len(content) != record['bytes'] or hashlib.sha256(content).hexdigest() != record['sha256']:
        raise ValueError('Offline reconnect model changed')
    return content


def verify_models(root):
    root = Path(root)
    manifest_bytes = (root/'ocr_models/manifest.json').read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MODEL_MANIFEST_SHA256:
        raise ValueError('Unreviewed OCR model manifest')
    manifest = json.loads(manifest_bytes)
    records = manifest.get('models', [])
    if (manifest.get('schema') != 1 or len(records) != len(MODELS)
            or {r.get('path') for r in records} != {x+'.traineddata' for x in MODELS}):
        raise ValueError('Incomplete offline reconnect language models')
    result = {}
    for record in records:
        name = 'ocr_models/' + record['path']
        content = (root/name).read_bytes()
        if len(content) != record['bytes'] or hashlib.sha256(content).hexdigest() != record['sha256']:
            raise ValueError('Offline reconnect model changed: '+name)
        result[name] = record['sha256']
    return result


def verify_native(root):
    root = Path(root)
    manifest_bytes = (root/'ocr_native_manifest.json').read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != NATIVE_MANIFEST_SHA256:
        raise ValueError('Unreviewed OCR native manifest')
    manifest = json.loads(manifest_bytes)
    records = manifest.get('files', [])
    if manifest.get('wheel_sha256') != WHEEL_SHA256 or len(records) != 11:
        raise ValueError('Unexpected offline OCR native manifest')
    result = {}
    for record in records:
        name = record.get('path')
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.dll'):
            raise ValueError('Unsafe OCR library filename')
        content = (root/'ocr_native'/name).read_bytes()
        if len(content) != record['bytes'] or hashlib.sha256(content).hexdigest() != record['sha256']:
            raise ValueError('Offline OCR library changed: '+name)
        result['ocr_native/'+name] = record['sha256']
    if len(result) != 11 or 'ocr_native/tesseract55.dll' not in result:
        raise ValueError('Duplicate or missing offline OCR library')
    return result


def fetch_native(root, *, wheel_content=None):
    root = Path(root)
    if wheel_content is None:
        with urllib.request.urlopen(WHEEL_URL, timeout=60) as response:
            wheel_content = response.read(10_000_000)
    if hashlib.sha256(wheel_content).hexdigest() != WHEEL_SHA256:
        raise ValueError('The pinned OCR build dependency checksum did not match')
    manifest = json.loads((root/'ocr_native_manifest.json').read_bytes())
    target = root/'ocr_native'
    target.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(wheel_content)) as archive:
        for record in manifest['files']:
            name = record['path']
            if Path(name).name != name or not name.endswith('.dll'):
                raise ValueError('Unsafe OCR library filename')
            content = archive.read('tesserocr/'+name)
            if len(content) != record['bytes'] or hashlib.sha256(content).hexdigest() != record['sha256']:
                raise ValueError('The OCR library checksum did not match')
            (target/name).write_bytes(content)
    return verify_native(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch-native', action='store_true')
    args = parser.parse_args()
    base = Path(__file__).resolve().parent
    if args.fetch_native:
        fetch_native(base)
    print(json.dumps({'models': len(verify_models(base)), 'libraries': len(verify_native(base))}))
