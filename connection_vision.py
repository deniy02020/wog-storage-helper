"""Offline, message-specific disconnect recognition on fresh game pixels.

The generic game's Confirm frame is insufficient: both the disconnect title
and its complete body must independently match one authentic game locale.
OCR is lazy and runs only on a plausible complete modal, never on inventories.
This module performs no capture, input, network access, or profile writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
import unicodedata

import cv2
import numpy as np
from PIL import Image, ImageOps

from core import Rect


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
        if not (180 <= w <= 1600 and .45 <= h/w <= .85 and area >= .15*w*h):
            continue
        body = rgb[y:y+h, x:x+w]
        blue = np.all(np.abs(body-(99,131,153)) <= 10, axis=2).astype('uint8')
        _, _, buttons, _ = cv2.connectedComponentsWithStats(blue, 8)
        options = []
        for bx, by, bw, bh, pixels in buttons[1:]:
            if (.35*w <= bw <= .58*w and .05*w <= bh <= .14*w
                    and pixels >= .005*w*w and by >= .65*h
                    and abs(bx+bw/2-w/2) <= .05*w and h-(by+bh) <= .08*w):
                options.append(Rect(x+int(bx),y+int(by),x+int(bx+bw),y+int(by+bh)))
        if len(options) != 1:
            continue
        button = options[0]
        top = button.top-round(w*1.03)
        rect = Rect(x,top,x+w,y+h)
        if not rect.fits(*image.size):
            continue
        heading = Rect(x+round(w*.11),top+round(w*.008),x+round(w*.86),top+round(w*.105))
        label_y = (button.top-round(w*.32),button.top-round(w*.24))
        labels = (Rect(x+round(w*.04),label_y[0],x+round(w*.48),label_y[1]),
                  Rect(x+round(w*.53),label_y[0],x+round(w*.97),label_y[1]))
        found.append((rect,button,heading,labels))
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
