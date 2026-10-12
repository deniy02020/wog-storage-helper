"""One Helper per Windows session, independent of profile or portable mode."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

MUTEX_NAME = "Local\\WoGStorageHelper"
ERROR_ALREADY_EXISTS = 183


def _load_kernel32():
    if os.name != "nt":
        raise OSError("WoG Storage Helper поддерживает только Windows.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


class SingleInstance:
    def __init__(self, kernel32=None, get_last_error=None):
        self._kernel = kernel32
        self._error = get_last_error or ctypes.get_last_error
        self._handle = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        kernel = self._kernel or _load_kernel32()
        self._kernel = kernel
        # Do not mistake a stale thread-local error for an existing mutex.
        if hasattr(ctypes, "set_last_error"):
            ctypes.set_last_error(0)
        handle = kernel.CreateMutexW(None, False, MUTEX_NAME)
        error = self._error()
        if not handle:
            raise OSError(error, "Не удалось проверить уже запущенный Helper.")
        if error == ERROR_ALREADY_EXISTS:
            kernel.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def close(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            if not self._kernel.CloseHandle(handle):
                raise OSError(self._error(), "Не удалось освободить дескриптор Helper.")

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("WoG Storage Helper уже запущен.")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
