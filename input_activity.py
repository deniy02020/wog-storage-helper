"""Read-only local input observation; no key content or cursor trails are stored."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading
import time

HELPER_INPUT_TAG = 0x574F4748


class UserActivityInterrupted(RuntimeError):
    """A user or another program supplied input during an automatic cycle."""
    def __init__(self, message="Пауза: ввод пользователя прервал проверку.", *, input_issued=False):
        if not isinstance(input_issued, bool):
            raise TypeError("input_issued must be a boolean")
        super().__init__(message)
        # Native accepted-button/wheel boundaries promote this to True. A
        # takeover before that boundary proves no game input was issued.
        self.input_issued = input_issued


class ActivityState:
    def __init__(self, initial_idle=0, clock=time.monotonic):
        self.clock = clock
        self.last_activity = clock() - max(0, initial_idle)
        self.generation = 0
        self._lock = threading.Lock()

    def record(self, extra_info):
        # Only our exact tag is exempt; input injected by other apps counts.
        if int(extra_info) == HELPER_INPUT_TAG:
            return
        with self._lock:
            self.last_activity = self.clock()
            self.generation += 1

    def snapshot(self):
        with self._lock:
            return self.generation, self.last_activity

    def idle_seconds(self):
        return max(0, self.clock() - self.snapshot()[1])


class InputObserver(ActivityState):
    """WH_MOUSE_LL/WH_KEYBOARD_LL callbacks run only in this helper's thread.

    Every callback immediately passes the event on. No game process is opened,
    no input is blocked, and only the last activity time/counter are retained.
    """
    def __init__(self, initial_idle):
        super().__init__(initial_idle)
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._error = None
        self._thread_id = None
        self._thread = threading.Thread(target=self._thread_entry, daemon=True)
        self._thread.start()
        if not self._ready.wait(2) or self._error is not None:
            self.close()
            raise RuntimeError("Наблюдение ввода Windows недоступно. Фоновая проверка отключена.") from self._error

    def _thread_entry(self):
        try:
            self._run()
        except Exception as error:
            self._error = error
        finally:
            self._ready.set()

    def _run(self):
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        class MouseEvent(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]
        class KeyEvent(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]
        u.SetWindowsHookExW.argtypes = (ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD)
        u.SetWindowsHookExW.restype = wintypes.HANDLE
        u.CallNextHookEx.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        u.CallNextHookEx.restype = ctypes.c_ssize_t
        u.UnhookWindowsHookEx.argtypes = (wintypes.HANDLE,)
        u.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        k.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        k.GetModuleHandleW.restype = wintypes.HMODULE
        k.GetCurrentThreadId.restype = wintypes.DWORD
        self._thread_id = k.GetCurrentThreadId()
        self._u = u

        def callback(structure):
            @callback_type
            def receive(code, message, pointer):
                if code >= 0:
                    try:
                        self.record(ctypes.cast(pointer, ctypes.POINTER(structure)).contents.dwExtraInfo)
                    except Exception as error:
                        self._error = error
                return u.CallNextHookEx(None, code, message, pointer)
            return receive
        mouse, keyboard = callback(MouseEvent), callback(KeyEvent)
        hooks = []
        try:
            # Ensure this thread has a message queue before callers can stop it.
            msg = wintypes.MSG()
            u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            for kind, receiver in ((14, mouse), (13, keyboard)):
                if self._closed.is_set():
                    return
                hook = u.SetWindowsHookExW(kind, receiver, k.GetModuleHandleW(None), 0)
                if not hook:
                    raise ctypes.WinError(ctypes.get_last_error())
                hooks.append(hook)
            self._ready.set()
            while True:
                if self._closed.is_set():
                    break
                result = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception as error:
            self._error = error
        finally:
            self._ready.set()
            for hook in hooks:
                u.UnhookWindowsHookEx(hook)

    def ensure_alive(self):
        if self._error is not None or not self._thread.is_alive():
            raise RuntimeError("Наблюдение ввода остановилось. Фоновая проверка отменена.")

    def close(self):
        self._closed.set()
        if self._thread_id is not None and hasattr(self, "_u"):
            self._u.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            self._u.PostThreadMessageW(self._thread_id, 0x12, 0, 0)  # WM_QUIT
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(1)
