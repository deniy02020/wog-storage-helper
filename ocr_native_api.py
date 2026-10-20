"""Small Tesseract C API binding for local image text only; no desktop access."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

_library = None
_dll_directory = None


def library():
    global _library, _dll_directory
    if _library is not None:
        return _library
    from paths import resource_path
    from ocr_resources import verify_native
    verify_native(resource_path('.'))
    root = resource_path('ocr_native')
    _dll_directory = os.add_dll_directory(str(root))
    lib = ctypes.CDLL(str(root/'tesseract55.dll'))
    pointer, integer, text = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    signatures = {
        'TessBaseAPICreate': (pointer, []),
        'TessBaseAPIInit5': (integer, [pointer, pointer, integer, text, integer,
                                     pointer, integer, pointer, pointer, ctypes.c_size_t, integer]),
        'TessBaseAPISetPageSegMode': (None, [pointer, integer]),
        'TessBaseAPISetImage': (None, [pointer, pointer, integer, integer, integer, integer]),
        'TessBaseAPIGetUTF8Text': (pointer, [pointer]),
        'TessBaseAPIMeanTextConf': (integer, [pointer]),
        'TessBaseAPIClear': (None, [pointer]),
        'TessBaseAPIDelete': (None, [pointer]),
        'TessDeleteText': (None, [pointer]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, arguments
    _library = lib
    return lib


class TextAPI:
    def __init__(self, *, path, language):
        self.lib = library()
        self.handle = self.lib.TessBaseAPICreate()
        self.pixels = None
        self.model = None
        if not self.handle:
            raise RuntimeError('Could not create offline reconnect recognizer')
        if not language.isascii() or not language.replace('_', '').isalpha():
            self.End()
            raise ValueError('A single bundled OCR language is required')
        # Python opens Unicode paths with Windows' wide API. Supplying model
        # bytes avoids Tesseract's narrow fopen path, including for usernames
        # and installation directories containing Cyrillic, CJK or Thai text.
        try:
            from ocr_resources import read_model
            content = read_model(path,language)
            self.model = ctypes.create_string_buffer(content)
        except BaseException:
            self.End()
            raise
        if self.lib.TessBaseAPIInit5(self.handle, self.model, len(content), language.encode('ascii'),
                                    1, None, 0, None, None, 0, 0):
            self.End()
            raise RuntimeError('Could not load offline reconnect language model')
        self.lib.TessBaseAPISetPageSegMode(self.handle, 6)  # One uniform text block.

    def SetImage(self, image):
        gray = image.convert('L')
        self.pixels = ctypes.create_string_buffer(gray.tobytes())
        self.lib.TessBaseAPISetImage(self.handle, self.pixels, gray.width, gray.height, 1, gray.width)

    def GetUTF8Text(self):
        text = self.lib.TessBaseAPIGetUTF8Text(self.handle)
        if not text:
            return ''
        try:
            return ctypes.string_at(text).decode('utf-8')
        finally:
            self.lib.TessDeleteText(text)

    def MeanTextConf(self):
        return self.lib.TessBaseAPIMeanTextConf(self.handle)

    def Clear(self):
        self.lib.TessBaseAPIClear(self.handle)
        self.pixels = None

    def End(self):
        if self.handle:
            self.lib.TessBaseAPIDelete(self.handle)
            self.handle = None
            self.pixels = None
            self.model = None
