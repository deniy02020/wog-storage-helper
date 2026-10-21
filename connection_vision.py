"""Offline, message-specific disconnect recognition on fresh game pixels.

The generic game's Confirm frame is insufficient: both the disconnect title
and its complete body must independently match one authentic game locale.
OCR is lazy and runs only on a plausible complete modal, never on inventories.
This module performs no capture, input, network access, or profile writes.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
from difflib import SequenceMatcher
import hashlib
import json
import unicodedata

import cv2
import numpy as np
from PIL import Image, ImageOps

from core import Rect

VISUAL_REFERENCE_SHA256 = '805597eb4b73196be2760295b213160df3d23ec3c69f77887a6cd102409a7c29'


def text_ink(image, kind):
    """Complete foreground glyph mask; a blank background is never a match."""
    gray = np.asarray(ImageOps.grayscale(image))
    if kind in ('reward_last_login','reward_duration'):
        return (gray > 135).astype('uint8')
    return (gray < 170 if kind == 'body' else gray > 175).astype('uint8')


def full_text_similarity(reference, actual):
    """Symmetric full-glyph overlap with only one reference-pixel alignment slack."""
    expected_ink, observed_ink = int(reference.sum()), int(actual.sum())
    if (expected_ink < 50 or observed_ink < .80 * expected_ink
            or observed_ink > 1.25 * expected_ink):
        return 0.0
    padded = np.pad(actual, 1)
    h, w = reference.shape
    return max(2 * int(np.sum(reference & padded[y:y+h, x:x+w])) /
               (expected_ink + observed_ink) for y in range(3) for x in range(3))


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if c.isalpha() or c.isdigit())


def text_matches(observed, expected, confidence):
    a, b = normalized(observed), normalized(expected)
    return (confidence >= 70 and len(a) >= .88 * len(b)
            and len(a) <= 1.12 * len(b)
            and SequenceMatcher(None, a, b, autojunk=False).ratio() >= .90)


@dataclass(frozen=True)
class DisconnectProof:
    rect: Rect
    button: Rect
    locale: str
    image_size: tuple[int, int]
    pixels_sha256: str

    @property
    def point(self):
        return self.button.center


def modal_candidates(image):
    """Independent body, dark title bar and paired blue/brown button geometry."""
    rgb = np.asarray(image.convert('RGB'), dtype=np.int16)
    mask = np.all(np.abs(rgb - (214, 210, 198)) <= 7, axis=2).astype('uint8')
    count, _labels, stats, _centres = cv2.connectedComponentsWithStats(mask, 8)
    results = []
    for x, y, w, h, area in stats[1:]:
        x, y, w, h, area = map(int, (x, y, w, h, area))
        if not (155 <= w <= 1600 and .50 <= h / w <= 1.15 and area >= w * h * .45):
            continue
        title_height = round(w * .139)
        rect = Rect(x, y-title_height, x+w, y+h)
        if not rect.fits(*image.size):
            continue
        title = rgb[rect.top+3:y-3, x+3:x+w-3]
        if title.size == 0 or np.mean(np.mean(title, axis=2) < 105) < .60:
            continue
        buttons = []
        body = rgb[y:y+h, x:x+w]
        for colour in ((137, 121, 121), (99, 131, 153)):
            bm = np.all(np.abs(body - colour) <= 10, axis=2).astype('uint8')
            n, _, bs, _ = cv2.connectedComponentsWithStats(bm, 8)
            options = []
            for bx, by, bw, bh, pixels in bs[1:]:
                if (.65*h <= by < .96*h and .15*w <= bw <= .49*w
                        and .035*w <= bh <= .20*w and pixels >= .005*w*w):
                    options.append(Rect(x+int(bx), y+int(by), x+int(bx+bw), y+int(by+bh)))
            if len(options) != 1:
                break
            buttons.append(options[0])
        if len(buttons) != 2:
            continue
        left, right = buttons
        if not (left.right < right.left and left.center[0] < x+w*.5 < right.center[0]
                and abs(left.center[1]-right.center[1]) <= .025*w):
            continue
        heading = Rect(x+round(w*.025), rect.top+round(w*.015), x+round(w*.87), y-2)
        message = Rect(x+round(w*.025), y+round(w*.025), x+w-round(w*.025),
                       min(left.top, right.top)-round(w*.035))
        if min(message.width, message.height, heading.height) <= 0:
            continue
        results.append((rect, right, heading, message))
    return tuple(results)


def reward_candidates(image):
    """Reward footer, centred blue button, title and two independent labels."""
    rgb = np.asarray(image.convert('RGB'), dtype=np.int16)
    mask = np.all(np.abs(rgb - (214, 210, 198)) <= 7, axis=2).astype('uint8')
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    found = []
    for x, y, w, h, area in stats[1:]:
        x, y, w, h, area = map(int, (x,y,w,h,area))
        # At small window sizes the grey footer separates from the thin paper
        # border around the portrait. Prove its centred button independently;
        # the complete reward title and both labels remain mandatory below.
        if not (180 <= w <= 1600 and .15 <= h/w <= .85 and area >= .15*w*h):
            continue
        body = rgb[y:y+h, x:x+w]
        blue = np.all(np.abs(body-(99,131,153)) <= 10, axis=2).astype('uint8')
        # Tiny CJK button glyphs can split the blue fill into two components.
        # Rejoin only within this footer, before proving centred button bounds.
        blue = cv2.morphologyEx(blue,cv2.MORPH_CLOSE,
            np.ones((3,max(3,round(w*.08))),dtype='uint8'))
        _, _, buttons, _ = cv2.connectedComponentsWithStats(blue, 8)
        options = []
        for bx, by, bw, bh, pixels in buttons[1:]:
            if (.35*w <= bw <= .58*w and .05*w <= bh <= .14*w
                    and pixels >= .005*w*w and by >= .20*h
                    and abs(bx+bw/2-w/2) <= .05*w and h-(by+bh) <= .08*w):
                options.append(Rect(x+int(bx),y+int(by),x+int(bx+bw),y+int(by+bh)))
        if len(options) != 1:
            continue
        button = options[0]
        top = button.top-round(w*1.03)
        rect = Rect(x,top,x+w,y+h)
        if not rect.fits(*image.size):
            continue
        heading = Rect(x+round(w*.11),top+round(w*.008),x+round(w*.86),top+round(w*.13))
        label_y = (button.top-round(w*.32),button.top-round(w*.24))
        labels = (Rect(x+round(w*.04),label_y[0],x+round(w*.48),label_y[1]),
                  Rect(x+round(w*.53),label_y[0],x+round(w*.97),label_y[1]))
        found.append((rect,button,heading,labels))
    return tuple(found)


def session_candidates(image):
    """Single wide blue button in a complete game error dialog, never a proof alone."""
    rgb = np.asarray(image.convert('RGB'), dtype=np.int16)
    mask = np.all(np.abs(rgb-(214,210,198)) <= 7,axis=2).astype('uint8')
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask,8)
    found = []
    for x,y,w,h,area in stats[1:]:
        x,y,w,h,area = map(int,(x,y,w,h,area))
        if not (155 <= w <= 1600 and .50 <= h/w <= 1.15 and area >= .45*w*h):
            continue
        rect = Rect(x,y-round(w*.139),x+w,y+h)
        if not rect.fits(*image.size):
            continue
        title = rgb[rect.top+3:y-3,x+3:x+w-3]
        if not title.size or np.mean(np.mean(title,axis=2)<105) < .60:
            continue
        body = rgb[y:y+h,x:x+w]
        blue = np.all(np.abs(body-(99,131,153)) <= 10,axis=2).astype('uint8')
        blue = cv2.morphologyEx(blue,cv2.MORPH_CLOSE,
            np.ones((3,max(3,round(w*.08))),dtype='uint8'))
        _, _, buttons, _ = cv2.connectedComponentsWithStats(blue,8)
        options = []
        for bx,by,bw,bh,pixels in buttons[1:]:
            if (.65*h <= by < .96*h and .70*w <= bw <= .96*w
                    and .035*w <= bh <= .20*w and pixels >= .01*w*w
                    and abs(bx+bw/2-w/2) <= .04*w):
                options.append(Rect(x+int(bx),y+int(by),x+int(bx+bw),y+int(by+bh)))
        if len(options) != 1:
            continue
        button = options[0]
        heading = Rect(x+round(w*.025),rect.top+round(w*.015),x+round(w*.87),y-2)
        message = Rect(x+round(w*.025),y+round(w*.025),x+w-round(w*.025),button.top-round(w*.035))
        if min(message.width,message.height,heading.height) > 0:
            found.append((rect,button,heading,message))
    return tuple(found)


class DisconnectRecognizer:
    def __init__(self, *, read_text=None):
        from paths import resource_path
        self.root = resource_path('ocr_models')
        self.messages = json.loads(resource_path('disconnect_messages.json').read_bytes())['messages']
        self.read_text = read_text or self._read_text
        self._apis = {}
        self._verified = set()
        self._last_locale = None
        self._visual_references = None

    def _visual_locale(self, image, heading, message):
        return self._visual_match(image,{'title':heading,'body':message},'disconnect')

    def _visual_match(self, image, bounds_by_role, phase):
        """Prove BOTH complete fixed texts when tiny glyphs defeat general OCR.

        References are pinned text-only features from an authentic game warning.
        Geometry alone or a title without its full body never authorizes input.
        The runtime still requires two identical fresh complete-dialog proofs.
        """
        if self._visual_references is None:
            from paths import resource_path
            content = resource_path('disconnect_visual.json').read_bytes()
            if hashlib.sha256(content).hexdigest() != VISUAL_REFERENCE_SHA256:
                raise ValueError('Unreviewed disconnect text references')
            document = json.loads(content)
            if document.get('schema') != 2:
                raise ValueError('Invalid disconnect text reference schema')
            references = []
            for record in document['references']:
                locale = record['locale']
                reference_phase = record['kind']
                if reference_phase not in ('disconnect','reward'):
                    raise ValueError('Invalid disconnect reference role')
                roles = (('title','body') if reference_phase=='disconnect' else
                         ('reward_title','reward_last_login','reward_duration'))
                if set(record['regions']) != set(roles):
                    raise ValueError('Incomplete whole-message visual reference')
                if any(record[k] != self.messages[locale][k] for k in roles):
                    raise ValueError('Disconnect reference/catalog text mismatch')
                regions = {}
                for kind in roles:
                    field = record['regions'][kind]
                    w, h = field['width'], field['height']
                    if not (10 <= w <= 1600 and 5 <= h <= 1000):
                        raise ValueError('Invalid disconnect reference dimensions')
                    data = base64.b64decode(field['bits'], validate=True)
                    if len(data) != (w*h+7)//8:
                        raise ValueError('Invalid disconnect text feature length')
                    pixels = np.unpackbits(np.frombuffer(data, dtype='uint8'))[:w*h].reshape(h,w)
                    if (int(pixels.sum()) != field['ink']
                            or not .01 <= field['ink']/(w*h) <= .40):
                        raise ValueError('Invalid disconnect text feature ink')
                    regions[kind] = pixels
                references.append((reference_phase, locale, regions))
            self._visual_references = references
        matches = []
        for reference_phase, locale, regions in self._visual_references:
            if reference_phase != phase:
                continue
            scores = []
            for kind, bounds in bounds_by_role.items():
                reference = regions[kind]
                h, w = reference.shape
                if abs(bounds.width/bounds.height - w/h) > .08 * w/h:
                    break
                # Body-background antialiasing can move its measured bottom by
                # one pixel. Preserve glyph proportions instead of stretching
                # text vertically to that noisy edge. Never discard extra ink.
                crop = image.crop(bounds.as_box())
                target = crop.resize((w,round(crop.height*w/crop.width)),Image.Resampling.LANCZOS)
                ink = text_ink(target,kind)
                if ink.shape[0] > h and np.any(ink[h:]):
                    break
                aligned = np.zeros_like(reference)
                aligned[:min(h,ink.shape[0])] = ink[:h]
                scores.append(full_text_similarity(reference, aligned))
            if len(scores) == len(regions) and min(scores) >= .92:
                matches.append(locale)
        return matches[0] if len(matches) == 1 else None

    def close(self):
        for api in self._apis.values():
            api.End()
        self._apis.clear()

    def _read_text(self, image, language):
        from ocr_native_api import TextAPI
        if language not in self._verified:
            manifest = json.loads((self.root / 'manifest.json').read_bytes())
            record = next(x for x in manifest['models'] if x['path'] == language+'.traineddata')
            content = (self.root / record['path']).read_bytes()
            if len(content) != record['bytes'] or hashlib.sha256(content).hexdigest() != record['sha256']:
                raise RuntimeError('Bundled reconnect recognition model is missing or changed')
            self._verified.add(language)
        if language not in self._apis:
            # Bound persistent native OCR memory to two languages. Most sessions
            # need just one; another game locale is always recognized afresh.
            if len(self._apis) >= 2:
                self._apis.pop(next(iter(self._apis))).End()
            self._apis[language] = TextAPI(path=str(self.root), language=language)
        api = self._apis[language]
        scale = max(1, min(4, 900 / max(1, image.width)))
        gray = ImageOps.grayscale(image)
        api.SetImage(gray.resize((round(gray.width*scale), round(gray.height*scale)), Image.Resampling.LANCZOS))
        text = api.GetUTF8Text()
        confidence = api.MeanTextConf()
        api.Clear()
        return text, confidence

    def detect(self, image, check_stop=lambda: None):
        matches = []
        candidates = modal_candidates(image)
        for rect, button, heading, message in candidates:
            visual_locale = self._visual_locale(image,heading,message)
            if visual_locale is not None:
                matches.append(DisconnectProof(rect,button,visual_locale,image.size,
                    hashlib.sha256(image.crop(rect.as_box()).tobytes()).hexdigest()))
                self._last_locale = visual_locale
                continue
            order = list(self.messages)
            if self._last_locale in order:
                order.remove(self._last_locale)
                order.insert(0, self._last_locale)
            for locale in order:
                check_stop()
                model = self.messages[locale]
                title, title_conf = self.read_text(image.crop(heading.as_box()), model['ocr'])
                if not text_matches(title, model['title'], title_conf):
                    continue
                body, body_conf = self.read_text(image.crop(message.as_box()), model['ocr'])
                if not text_matches(body, model['body'], body_conf):
                    continue
                matches.append(DisconnectProof(rect, button, locale, image.size,
                    hashlib.sha256(image.crop(rect.as_box()).tobytes()).hexdigest()))
                self._last_locale = locale
                break
        return matches[0] if len(matches) == 1 else None

    def detect_reward(self, image, check_stop=lambda: None):
        matches = []
        order = list(self.messages)
        if self._last_locale in order:
            order.remove(self._last_locale)
            order.insert(0,self._last_locale)
        for rect,button,heading,labels in reward_candidates(image):
            visual_locale = self._visual_match(image,
                dict(zip(('reward_title','reward_last_login','reward_duration'),(heading,*labels))), 'reward')
            if visual_locale is not None:
                fingerprint = hashlib.sha256(b''.join(image.crop(r.as_box()).tobytes()
                                                      for r in (heading,*labels,button))).hexdigest()
                matches.append(DisconnectProof(rect,button,visual_locale,image.size,fingerprint))
                self._last_locale = visual_locale
                continue
            for locale in order:
                check_stop()
                model = self.messages[locale]
                text,confidence = self.read_text(image.crop(heading.as_box()),model['ocr'])
                if not text_matches(text,model['reward_title'],confidence):
                    continue
                good = True
                for region,key in zip(labels,('reward_last_login','reward_duration')):
                    text,confidence = self.read_text(image.crop(region.as_box()),model['ocr'])
                    if not text_matches(text,model[key],confidence):
                        good = False
                        break
                if not good:
                    continue
                fingerprint = hashlib.sha256(b''.join(image.crop(r.as_box()).tobytes()
                                                      for r in (heading,*labels,button))).hexdigest()
                matches.append(DisconnectProof(rect,button,locale,image.size,fingerprint))
                self._last_locale = locale
                break
        return matches[0] if len(matches) == 1 else None

    def detect_session(self, image, check_stop=lambda: None):
        """Only the complete authentic expired-session message permits a restart."""
        matches = []
        order = list(self.messages)
        if self._last_locale in order:
            order.remove(self._last_locale)
            order.insert(0,self._last_locale)
        for rect,button,heading,message in session_candidates(image):
            for locale in order:
                check_stop()
                model = self.messages[locale]
                title,confidence = self.read_text(image.crop(heading.as_box()),model['ocr'])
                if not text_matches(title,model['session_title'],confidence):
                    continue
                body,confidence = self.read_text(image.crop(message.as_box()),model['ocr'])
                if not text_matches(body,model['session_body'],confidence):
                    continue
                matches.append(DisconnectProof(rect,button,locale,image.size,
                    hashlib.sha256(image.crop(rect.as_box()).tobytes()).hexdigest()))
                self._last_locale = locale
                break
        return matches[0] if len(matches) == 1 else None
