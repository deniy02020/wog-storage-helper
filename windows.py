"""Visible-window screenshots and ordinary Windows mouse input. No game hooks."""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from contextlib import contextmanager
from functools import wraps
import ntpath
import logging
import hashlib
from input_activity import (HELPER_INPUT_TAG, InputObserver, UserActivityInterrupted)
from game_identity import is_game_window


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    executable: str
    process_id: int = 0


@dataclass(frozen=True)
class BackgroundCycle:
    previous_window: int
    was_minimized: bool
    activity_generation: int = 0
    game_window: int = 0
    game_process_id: int = 0


class WindowGeometryChanged(RuntimeError):
    """A fresh client/identity differs; accepted mouse-down must never repeat."""
    def __init__(self, message="Геометрия окна игры изменилась.", *, input_issued=False):
        if not isinstance(input_issued, bool):
            raise TypeError("input_issued must be a boolean")
        super().__init__(message)
        self.input_issued = input_issued


class HitTestMismatch(WindowGeometryChanged):
    """The intended screen point does not currently belong to the game."""


class FocusUnavailable(RuntimeError):
    """Windows declined a bounded activation attempt before any game input."""
    input_issued = False

    def __init__(self, message="Windows не разрешила активировать окно игры. Ожидаю доступный фокус.", *, diagnostics=None):
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})

    @property
    def diagnostic_message(self):
        return "focus_unavailable " + "; ".join(f"{key}={value}" for key, value in self.diagnostics.items())


class GUIThreadInfo(ctypes.Structure):
    """Read-only foreground input-routing state from GetGUIThreadInfo."""
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


def _physical_api(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._physical_coordinates():
            return method(self, *args, **kwargs)
    return call


class WindowsDesktop:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Этот помощник запускается в Windows 10/11.")
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u.GetForegroundWindow.restype = wintypes.HWND
        self.u.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.u.SetForegroundWindow.restype = wintypes.BOOL
        self.u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.u.ShowWindow.restype = wintypes.BOOL
        self.u.IsWindow.argtypes = [wintypes.HWND]
        self.u.IsWindowVisible.argtypes = [wintypes.HWND]
        self.u.IsIconic.argtypes = [wintypes.HWND]
        self.u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k.OpenProcess.restype = wintypes.HANDLE
        self.k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.u.GetAsyncKeyState.restype = ctypes.c_short
        self.u.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUIThreadInfo)]
        self.u.GetGUIThreadInfo.restype = wintypes.BOOL
        self.u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.u.WindowFromPoint.argtypes = [wintypes.POINT]
        self.u.WindowFromPoint.restype = wintypes.HWND
        self.u.WindowFromPhysicalPoint.argtypes = [wintypes.POINT]
        self.u.WindowFromPhysicalPoint.restype = wintypes.HWND
        self.u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.u.GetAncestor.restype = wintypes.HWND
        self._configure_dpi_awareness()
        self._capture_identities = {}

        ulong_ptr = ctypes.c_size_t

        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

        class KeyboardInput(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ulong_ptr)]

        class HardwareInput(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput), ("ki", KeyboardInput), ("hi", HardwareInput)]

        class Input(ctypes.Structure):
            _anonymous_ = ("data",)
            _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]

        self.Input, self.MouseInput = Input, MouseInput
        self.u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.u.SendInput.restype = wintypes.UINT
        self.input_observer = None
        self.input_observer_error = ""
        self._background_token = None
        try:
            self.input_observer = InputObserver(self.idle_seconds())
        except RuntimeError as error:
            # Foreground and explicit operations remain available. Automatic
            # foreground acquisition requires a healthy observer, below.
            self.input_observer_error = str(error)

    def _configure_dpi_awareness(self):
        """Use device coordinates even if a host set a different process context."""
        self._per_monitor_fallback = False
        setter = getattr(self.u, "SetProcessDpiAwarenessContext", None)
        if setter is not None:
            setter.argtypes = [ctypes.c_void_p]
            setter.restype = wintypes.BOOL
            for context in (-4, -3):  # PMv2, then Windows 10 1607 PMv1.
                if setter(ctypes.c_void_p(context)):
                    self._per_monitor_fallback = True
                    break
        thread_setter = getattr(self.u, "SetThreadDpiAwarenessContext", None)
        if thread_setter is not None:
            thread_setter.argtypes = [ctypes.c_void_p]
            thread_setter.restype = ctypes.c_void_p
            self.u.GetThreadDpiAwarenessContext.argtypes = []
            self.u.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            self.u.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            self.u.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        elif not self._per_monitor_fallback:
            # The legacy process setter alone is not proof: it can return
            # E_ACCESSDENIED because a system/unaware context already exists.
            try:
                shcore = ctypes.WinDLL("shcore", use_last_error=True)
                shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
                shcore.SetProcessDpiAwareness(2)
                shcore.GetProcessDpiAwareness.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
                awareness = ctypes.c_int(-1)
                self._per_monitor_fallback = (shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness)) == 0
                                              and awareness.value == 2)
            except (AttributeError, OSError):
                pass
        # Validate now, before any capture or input and before constructing Tk.
        with self._physical_coordinates():
            pass

    @contextmanager
    def _physical_coordinates(self):
        setter = getattr(self.u, "SetThreadDpiAwarenessContext", None)
        if setter is None:
            if not getattr(self, "_per_monitor_fallback", False):
                raise RuntimeError("Windows не подтвердила физические координаты мониторов. Ввод отменён.")
            yield
            return
        previous = None
        for context in (-4, -3):
            previous = setter(ctypes.c_void_p(context))
            if previous:
                break
        if not previous:
            raise RuntimeError("Windows не разрешила физические координаты потока. Ввод отменён.")
        try:
            current = self.u.GetThreadDpiAwarenessContext()
            if self.u.GetAwarenessFromDpiAwarenessContext(current) != 2:
                raise RuntimeError("Windows не подтвердила физические координаты потока. Ввод отменён.")
            yield
        finally:
            if not setter(previous):
                raise RuntimeError("Windows не восстановила DPI-контекст потока.")

    def window_info(self, handle):
        """Query ordinary process metadata; reject an HWND recycled mid-query."""
        if not handle or not self.u.IsWindow(handle):
            return None
        before = wintypes.DWORD()
        if not self.u.GetWindowThreadProcessId(handle, ctypes.byref(before)) or not before.value:
            return None
        size = self.u.GetWindowTextLengthW(handle)
        if not size:
            return None
        title = ctypes.create_unicode_buffer(size + 1)
        if not self.u.GetWindowTextW(handle, title, len(title)):
            return None
        process = self.k.OpenProcess(0x1000, False, before.value)
        executable = ""
        if process:
            try:
                buf = ctypes.create_unicode_buffer(32768)
                length = wintypes.DWORD(len(buf))
                if self.k.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(length)):
                    executable = buf.value
            finally:
                self.k.CloseHandle(process)
        after = wintypes.DWORD()
        if (not self.u.IsWindow(handle)
                or not self.u.GetWindowThreadProcessId(handle, ctypes.byref(after))
                or before.value != after.value):
            return None
        return WindowInfo(int(handle), title.value, executable, before.value)

    def _require_game_identity(self, handle, *, captured=True):
        info = self.window_info(handle)
        if not is_game_window(info) or not info.process_id:
            raise WindowGeometryChanged("Окно игры закрыто или заменено другим приложением.")
        identity = (info.process_id, ntpath.normcase(ntpath.normpath(info.executable)))
        previous = getattr(self, "_capture_identities", {}).get(handle)
        if captured and previous is not None and identity != previous:
            raise WindowGeometryChanged("Процесс игры изменился. Требуется новый снимок окна.")
        return identity

    def windows(self):
        result = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def collect(handle, _):
            if not self.u.IsWindowVisible(handle):
                return True
            info = self.window_info(handle)
            if info is not None:
                result.append(info)
            return True

        self.u.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        self.u.EnumWindows(collect, 0)
        return sorted(result, key=lambda w: w.title.lower())

    def is_alive(self, handle):
        return bool(self.u.IsWindow(handle) and is_game_window(self.window_info(handle)))

    def idle_seconds(self):
        observer = getattr(self, "input_observer", None)
        if observer is not None:
            observer.ensure_alive()
            return observer.idle_seconds()
        return self._windows_idle_seconds()

    def _windows_idle_seconds(self):
        """Read only the last input timestamp, never keys or input history."""
        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        info = LastInputInfo(ctypes.sizeof(LastInputInfo), 0)
        self.u.GetLastInputInfo.argtypes = [ctypes.POINTER(LastInputInfo)]
        self.u.GetLastInputInfo.restype = wintypes.BOOL
        if not self.u.GetLastInputInfo(ctypes.byref(info)):
            raise RuntimeError("Windows не сообщила время последнего ввода. Автоматическое открытие отменено.")
        self.k.GetTickCount64.restype = ctypes.c_ulonglong
        now = self.k.GetTickCount64() & 0xffffffff
        return ((now - info.dwTime) & 0xffffffff) / 1000.0

    def _focus_diagnostics(self, handle, identity, was_minimized, show_return, foreground_return):
        result = {"target_hwnd": int(handle), "target_pid": identity[0],
                  "foreground_hwnd": int(self.u.GetForegroundWindow() or 0),
                  "show_command": 9 if was_minimized else 5,
                  "show_return": bool(show_return), "set_foreground_return": bool(foreground_return),
                  "was_iconic": was_minimized, "current_iconic": bool(self.u.IsIconic(handle))}
        result["restored_minimized"] = was_minimized and not result["current_iconic"]
        observer = getattr(self, "input_observer", None)
        result["observer_idle_seconds"] = None
        result["windows_idle_seconds"] = None
        try:
            if observer is not None:
                observer.ensure_alive()
                result["observer_idle_seconds"] = round(observer.idle_seconds(), 3)
        except (AttributeError, RuntimeError, OSError):
            pass
        try:
            result["windows_idle_seconds"] = round(self._windows_idle_seconds(), 3)
        except (AttributeError, RuntimeError, OSError):
            pass
        return result

    def _check_acquisition_available(self):
        """Never acquire focus or move through a held/captured user gesture."""
        # Only current high bits matter. The low 'pressed since last query' bit
        # is unreliable and is never interpreted as a held key/button here.
        mouse_keys = (0x01, 0x02, 0x04, 0x05, 0x06)
        modifier_keys = (0x10, 0x11, 0x12, 0x5B, 0x5C)
        mouse_held = modifier_held = unavailable = False
        capture_active = interactive_mode = foreground_changed = False
        try:
            foreground = int(self.u.GetForegroundWindow() or 0)
            mouse_held = any(self.u.GetAsyncKeyState(key) & 0x8000 for key in mouse_keys)
            modifier_held = any(self.u.GetAsyncKeyState(key) & 0x8000 for key in modifier_keys)
            info = GUIThreadInfo(cbSize=ctypes.sizeof(GUIThreadInfo))
            # Zero requests the current foreground thread; no cross-process
            # hooks, cancellation, ReleaseCapture or synthetic key release.
            available = bool(self.u.GetGUIThreadInfo(0, ctypes.byref(info)))
            current = int(self.u.GetForegroundWindow() or 0)
            foreground_changed = current != foreground
            unavailable = (not available or not foreground
                           or int(info.hwndActive or 0) != foreground)
            capture_active = bool(info.hwndCapture)
            # GUI_INMOVESIZE / INMENUMODE / SYSTEMMENUMODE / POPUPMENUMODE.
            # Caret blinking alone is ordinary foreground activity, not a block.
            interactive_mode = bool(info.flags & 0x1E or info.hwndMoveSize or info.hwndMenuOwner)
            # A press/capture can start during the read-only GUI query.
            mouse_held = mouse_held or any(self.u.GetAsyncKeyState(key) & 0x8000 for key in mouse_keys)
            modifier_held = modifier_held or any(self.u.GetAsyncKeyState(key) & 0x8000 for key in modifier_keys)
        except (AttributeError, OSError, ctypes.ArgumentError):
            unavailable = True
        if mouse_held or modifier_held or capture_active or interactive_mode or unavailable or foreground_changed:
            # Only boolean reasons are recorded, never keys, titles or the
            # capturing application's HWND/content/history.
            raise FocusUnavailable(diagnostics={"acquisition_blocked": True,
                "mouse_held": bool(mouse_held), "modifier_held": bool(modifier_held),
                "capture_active": capture_active, "interactive_mode": interactive_mode,
                "gui_state_unavailable": unavailable, "foreground_changed": foreground_changed})

    def begin_background_cycle(self, handle, *, activation_target=None, activation_guard=None):
        """Automatic foreground cycle, only after ten minutes without input."""
        if self.stop_pressed():
            raise InterruptedError("Остановлено: F8.")
        if not self.is_alive(handle):
            return None
        if self.idle_seconds() < 600:
            return None
        observer = getattr(self, "input_observer", None)
        if observer is None:
            raise RuntimeError(getattr(self, "input_observer_error", "") or "Наблюдение ввода недоступно. Автоматическое открытие отменено.")
        observer.ensure_alive()
        self._check_acquisition_available()
        identity = self._require_game_identity(handle, captured=False)
        token = BackgroundCycle(int(self.u.GetForegroundWindow() or 0), bool(self.u.IsIconic(handle)),
                                observer.snapshot()[0], handle, identity[0])
        self._background_token = token
        activation_input_issued = False
        try:
            try:
                self.activate_for_start(handle)
            except FocusUnavailable as error:
                # A separate, game-only activation gesture may be supplied by
                # the runner. Ordinary transfers never bypass foreground guards.
                if not callable(activation_target) or not callable(activation_guard):
                    raise
                activation_input_issued = self._activate_from_header(handle, token, identity, activation_target, activation_guard, error)
            self.check_background_cycle(token)
        except Exception as error:
            promoted = error
            if activation_input_issued:
                if isinstance(error, WindowGeometryChanged):
                    error.input_issued = True
                elif isinstance(error, (InterruptedError, UserActivityInterrupted)):
                    error.input_issued = True  # Stop/takeover stays sticky and cleanup respects the user.
                else:
                    promoted = WindowGeometryChanged("Активационный клик выполнен; дальнейшая проверка не завершена. Повтор отменён.", input_issued=True)
            if (token.was_minimized and self.is_alive(handle)
                    and self._background_identity_matches(token)
                    and not self.u.IsIconic(handle)
                    and self.u.GetForegroundWindow() == token.previous_window
                    and not self.active(handle)
                    and observer.snapshot()[0] == token.activity_generation):
                # Activation may restore a minimized game before Windows denies focus.
                self.u.ShowWindow(handle, 6)
            else:
                self.end_background_cycle(handle, token)
            self._background_token = None
            if isinstance(error, FocusUnavailable):
                error.diagnostics["cleanup_reminimized"] = (token.was_minimized
                    and self._background_identity_matches(token) and bool(self.u.IsIconic(handle)))
            if promoted is not error:
                raise promoted from error
            raise
        return token

    def _activation_context(self, handle, token, identity, guard, expected_box=None, *, acquisition=True):
        guard()  # Runner cancellation/Stop and its sticky F8 handling.
        self.check_background_cycle(token)
        if self.stop_pressed():
            raise InterruptedError("Остановлено: F8.")
        if (getattr(self, "_background_token", None) is not token
                or token.game_window != handle or self.idle_seconds() < 600
                or self._require_game_identity(handle, captured=False) != identity
                or not self.u.IsWindowVisible(handle) or self.u.IsIconic(handle)):
            raise WindowGeometryChanged("Доказательство фоновой активации больше не актуально.")
        if int(self.u.GetForegroundWindow() or 0) not in (token.previous_window, handle):
            raise WindowGeometryChanged("Активное окно изменилось. Активация отменена.")
        box = self.client_box(handle)
        if expected_box is not None and box != expected_box:
            raise WindowGeometryChanged("Окно изменилось во время проверки активации.")
        if acquisition:
            self._check_acquisition_available()
        return box

    @_physical_api
    def _capture_activation_pixels(self, handle, token, identity, guard, expected_box=None):
        """Private visible-screen proof; never reads a hidden window or game memory."""
        from PIL import ImageGrab
        box = self._activation_context(handle, token, identity, guard, expected_box)
        frame = ImageGrab.grab(bbox=box, all_screens=True).convert("RGB")
        self._activation_context(handle, token, identity, guard, box)
        if frame.size != (box[2] - box[0], box[3] - box[1]):
            raise WindowGeometryChanged("Снимок фоновой активации имеет неверный размер.")
        return frame, box, time.monotonic()

    @staticmethod
    def _validated_activation_target(target, size):
        from panels import ActivationTarget
        from core import Rect
        if target is None:
            return None
        if (not isinstance(target, ActivationTarget) or not isinstance(target.rect, Rect)
                or target.pane not in ("storage", "inventory")
                or any(type(value) is not int for value in target.rect.as_box())
                or not target.rect.fits(*size) or target.rect.width < 4 or target.rect.height < 4):
            raise ValueError("Invalid immutable activation header evidence")
        anchor = getattr(target, "anchor_rect", None)
        proof = getattr(target, "anchor_proof", None)
        if (anchor is None) != (proof is None):
            raise ValueError("Activation anchor geometry and fingerprint must be supplied together")
        if anchor is not None:
            if (not isinstance(anchor, Rect)
                    or any(type(value) is not int for value in anchor.as_box())
                    or not anchor.fits(*size) or anchor.width < 4 or anchor.height < 4
                    or not isinstance(proof, str) or len(proof) != 64
                    or any(value not in "0123456789abcdefABCDEF" for value in proof)):
                raise ValueError("Invalid immutable activation anchor evidence")
        # Exact dataclass comparison on the next fresh frame includes these
        # optional fields. Native input continues to use only target.rect.
        return target

    @_physical_api
    def _activation_point(self, handle, token, identity, guard, box, target, *, cursor=False):
        current = self._activation_context(handle, token, identity, guard, box)
        local = target.rect.center
        point = wintypes.POINT(current[0] + local[0], current[1] + local[1])
        # The visible label may still be click-through. Both native coordinate
        # APIs must agree that the point belongs to this exact supported HWND.
        physical = self.u.WindowFromPhysicalPoint(point)
        logical = self.u.WindowFromPoint(point)
        physical_root = self.u.GetAncestor(physical, 2) if physical else 0
        logical_root = self.u.GetAncestor(logical, 2) if logical else 0
        if physical_root != handle or logical_root != handle:
            raise WindowGeometryChanged("Заголовок игры перекрыт или пропускает клик. Активация отменена.")
        self._activation_context(handle, token, identity, guard, box)
        if cursor:
            self._check_cursor((point.x, point.y))
            self._activation_context(handle, token, identity, guard, box)
        return point.x, point.y

    @_physical_api
    def _activate_from_header(self, handle, token, identity, target_reader, guard, denied):
        """At most one caption-bound neutral click, never an item/transfer click."""
        self._activation_context(handle, token, identity, guard)
        self.last_activation_diagnostics = {"target_hwnd": int(handle), "target_pid": identity[0],
                                            "header_pane": 0, "down_accepted": False,
                                            "up_accepted": False, "focus_verified": False}
        if self.active(handle):
            self.last_activation_diagnostics["focus_verified"] = True
            return False
        first, box, captured_at = self._capture_activation_pixels(handle, token, identity, guard)
        target = self._validated_activation_target(target_reader(first), first.size)
        self._activation_context(handle, token, identity, guard, box)
        if target is None or time.monotonic() - captured_at > 2.0:
            denied.diagnostics["activation_stage"] = 1  # Missing/expired visible header; no input.
            raise denied
        bound_patch = getattr(target, "anchor_rect", None) is not None
        local = target.rect.center
        if not bound_patch:
            # Preserve the original heading-only evidence order.
            self._position_cursor(box[0] + local[0], box[1] + local[1])
        second, _, captured_at = self._capture_activation_pixels(handle, token, identity, guard, box)
        again = self._validated_activation_target(target_reader(second), second.size)
        self._activation_context(handle, token, identity, guard, box)
        if again != target or time.monotonic() - captured_at > 2.0:
            denied.diagnostics["activation_stage"] = 2  # Heading changed/covered; no button.
            raise denied
        if bound_patch:
            # Both caption+blank proofs precede the cursor. A software cursor
            # may cover the blank patch afterward; never mask it or relax RGB.
            # The complete caption must remain pixel-identical at its proved
            # position on one more fresh visible frame after the tagged move.
            anchor_box = target.anchor_rect.as_box()
            caption_hash = hashlib.sha256(second.crop(anchor_box).convert("RGB").tobytes()).digest()
            self._activation_context(handle, token, identity, guard, box)
            self._position_cursor(box[0] + local[0], box[1] + local[1])
            after_move, _, _ = self._capture_activation_pixels(handle, token, identity, guard, box)
            if (hashlib.sha256(after_move.crop(anchor_box).convert("RGB").tobytes()).digest() != caption_hash
                    or time.monotonic() - captured_at > 2.0):
                denied.diagnostics["activation_stage"] = 3  # Caption changed or prior proof expired; no button.
                raise denied
        intended = self._activation_point(handle, token, identity, guard, box, target, cursor=True)
        if self.active(handle):
            self.last_activation_diagnostics["focus_verified"] = True
            return False  # Hover/ordinary OS activation succeeded; no activation button.
        down_sent = up_sent = False
        self.last_activation_diagnostics = {"target_hwnd": int(handle), "target_pid": identity[0],
                                            "header_pane": 2 if target.pane == "storage" else 1,
                                            "screen_x": intended[0], "screen_y": intended[1],
                                            "down_accepted": False, "up_accepted": False, "focus_verified": False}
        try:
            def before_down():
                if time.monotonic() - captured_at > 2.0:
                    raise WindowGeometryChanged("Доказательство активации устарело. Клик отменён.")
                if self._activation_point(handle, token, identity, guard, box, target, cursor=True) != intended:
                    raise WindowGeometryChanged()
                if bound_patch and time.monotonic() - captured_at > 2.0:
                    raise WindowGeometryChanged("Доказательство активации устарело. Клик отменён.")
            self._send_mouse(0x0002, before_down)
            down_sent = True
            self.last_activation_diagnostics["down_accepted"] = True
            try:
                self._activation_context(handle, token, identity, guard, box, acquisition=False)
            finally:
                try:
                    self._send_mouse(0x0004)
                    up_sent = True
                    self.last_activation_diagnostics["up_accepted"] = True
                finally:
                    logging.info("activation_header_input %s", self.last_activation_diagnostics)
            deadline = time.monotonic() + 0.5
            while True:
                self._activation_context(handle, token, identity, guard, box, acquisition=False)
                if self.active(handle):
                    self.last_activation_diagnostics["focus_verified"] = True
                    logging.info("activation_header_confirmed %s", self.last_activation_diagnostics)
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WindowGeometryChanged("Клик активации отправлен, но фокус игры не подтверждён. Повтор отменён.", input_issued=True)
                time.sleep(min(0.02, remaining))
        except InterruptedError:
            raise  # Sticky cancellation/F8 exits the monitor; it is never retried.
        except Exception as error:
            if down_sent:
                terminal = WindowGeometryChanged("Клик активации отправлен, но результат не подтверждён. Повтор отменён.", input_issued=True)
                terminal.diagnostics = {"target_hwnd": int(handle), "target_pid": identity[0],
                                        "foreground_hwnd": int(self.u.GetForegroundWindow() or 0),
                                        "activation_down_accepted": True, "activation_up_accepted": up_sent}
                raise terminal from error
            raise

    def check_background_cycle(self, token=None):
        token = token or getattr(self, "_background_token", None)
        if token is None:
            return
        observer = getattr(self, "input_observer", None)
        if observer is None:
            raise RuntimeError("Наблюдение ввода недоступно. Фоновая проверка отменена.")
        observer.ensure_alive()
        if observer.snapshot()[0] != token.activity_generation:
            raise UserActivityInterrupted("Пауза: ввод пользователя прервал фоновую проверку.")
        if not self._background_identity_matches(token):
            raise WindowGeometryChanged("Окно фоновой проверки было закрыто или заменено.")

    def _background_identity_matches(self, token):
        if not token.game_window:
            return True  # Legacy/injected token has no native identity binding.
        info = self.window_info(token.game_window)
        return bool(is_game_window(info) and info.process_id == token.game_process_id)

    def close(self):
        observer = getattr(self, "input_observer", None)
        if observer is not None:
            observer.close()

    def end_background_cycle(self, handle, token):
        """Hide an automatic idle cycle and return focus unless the user took over."""
        self.last_background_error = ""
        self._background_token = None
        observer = getattr(self, "input_observer", None)
        if observer is not None and observer.snapshot()[0] != token.activity_generation:
            return True  # User took over; do not hide their game or steal focus.
        if not self._background_identity_matches(token):
            return True  # A stale cycle must never hide/activate a replacement HWND.
        if not self.active(handle):
            return True  # User changed focus; leave their choice intact.
        previous = token.previous_window
        if previous == handle or not previous or not self.u.IsWindow(previous):
            self.last_background_error = f"Нет доступного прежнего окна: {previous}."
            return False
        if observer is not None and observer.snapshot()[0] != token.activity_generation:
            return True
        self.u.ShowWindow(handle, 6)  # SW_MINIMIZE: the work mode hides the game after a cycle.
        if observer is not None and observer.snapshot()[0] != token.activity_generation:
            return True
        if self.u.GetForegroundWindow() != previous:
            self.u.SetForegroundWindow(previous)  # One request, no repeated focus stealing.
        deadline = time.monotonic() + 0.5
        while True:
            current = self.u.GetForegroundWindow()
            if current == previous:
                return True
            if observer is not None and observer.snapshot()[0] != token.activity_generation:
                return True
            if self.stop_pressed():
                return True  # No further focus requests while stopping.
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.u.IsWindow(previous):
                self.last_background_error = f"Прежнее окно {previous}; активное окно {int(current or 0)}."
                return False
            time.sleep(min(0.02, remaining))

    def active(self, handle):
        return bool(self.is_alive(handle) and self.u.IsWindowVisible(handle)
                    and not self.u.IsIconic(handle) and self.u.GetForegroundWindow() == handle)

    def activate_for_start(self, handle):
        """Activate once for an explicit action or a guarded automatic idle cycle."""
        self.check_background_cycle()
        if self.stop_pressed():
            raise InterruptedError("Запуск отменён: F8.")
        if not handle or not self.is_alive(handle):
            raise RuntimeError("Выбранное окно игры закрыто. Нажми «Обновить окна» и выбери игру заново.")
        was_minimized = bool(self.u.IsIconic(handle))
        if not self.u.IsWindowVisible(handle):
            raise RuntimeError("Выбранное окно игры скрыто. Открой игру и выбери её окно заново.")
        self.check_background_cycle()
        if self.stop_pressed():
            raise InterruptedError("Запуск отменён: F8.")
        identity = self._require_game_identity(handle, captured=False)
        observer = getattr(self, "input_observer", None)
        previous_generation = observer.snapshot()[0] if observer is not None else None
        previous_window = int(self.u.GetForegroundWindow() or 0) if was_minimized else 0
        # SW_SHOW preserves a normal/maximized window's current size and position.
        # SW_RESTORE is reserved for minimized windows. Neither API is a promise
        # that Windows grants foreground activation; actual focus is checked below.
        self._check_acquisition_available()
        show_return = self.u.ShowWindow(handle, 9 if was_minimized else 5)
        self.check_background_cycle()
        if self.stop_pressed():
            raise InterruptedError("Запуск отменён: F8.")
        if self._require_game_identity(handle, captured=False) != identity:
            raise WindowGeometryChanged("Процесс игры изменился во время активации.")
        foreground_return = self.u.SetForegroundWindow(handle)
        deadline = time.monotonic() + 0.5
        while True:
            self.check_background_cycle()
            if self.stop_pressed():
                raise InterruptedError("Запуск отменён: F8.")
            if self._require_game_identity(handle, captured=False) != identity:
                raise WindowGeometryChanged("Процесс игры изменился во время активации.")
            if self.active(handle):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                diagnostics = self._focus_diagnostics(handle, identity, was_minimized, show_return, foreground_return)
                # Background attempts have their own guarded cleanup in begin.
                # An explicit minimized start must also undo only its own restore,
                # while leaving any newer focus/user/PID choice untouched.
                if (was_minimized and getattr(self, "_background_token", None) is None
                        and previous_window != handle
                        and self.u.GetForegroundWindow() == previous_window
                        and not self.u.IsIconic(handle)
                        and (observer is None or observer.snapshot()[0] == previous_generation)
                        and self._require_game_identity(handle, captured=False) == identity):
                    self.u.ShowWindow(handle, 6)
                diagnostics["cleanup_reminimized"] = was_minimized and bool(self.u.IsIconic(handle))
                raise FocusUnavailable(diagnostics=diagnostics)
            time.sleep(min(0.02, remaining))

    @_physical_api
    def client_box(self, handle):
        identity = self._require_game_identity(handle, captured=False)
        def read():
            if not self.u.IsWindow(handle) or self.u.IsIconic(handle):
                raise WindowGeometryChanged("Окно игры закрыто или свёрнуто.")
            rect, origin = wintypes.RECT(), wintypes.POINT()
            if not self.u.GetClientRect(handle, ctypes.byref(rect)) or not self.u.ClientToScreen(handle, ctypes.byref(origin)):
                raise WindowGeometryChanged("Windows не сообщила актуальные границы окна игры.")
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width < 20 or height < 20:
                raise WindowGeometryChanged("Окно слишком маленькое.")
            return origin.x, origin.y, origin.x + width, origin.y + height
        before, after = read(), read()
        if (before != after or identity != self._require_game_identity(handle, captured=False)
                or self.u.IsIconic(handle)):
            raise WindowGeometryChanged()
        return after

    @_physical_api
    def capture(self, handle):
        from PIL import ImageGrab
        self.check_background_cycle()
        if not self.active(handle):
            raise WindowGeometryChanged("Выбери окно игры и оставь его на переднем плане.")
        identity = self._require_game_identity(handle, captured=False)
        box = self.client_box(handle)
        # all_screens preserves physical coordinates on monitors left of the primary.
        frame = ImageGrab.grab(bbox=box, all_screens=True).convert("RGB")
        self.check_background_cycle()
        if (not self.active(handle) or self.client_box(handle) != box
                or self._require_game_identity(handle, captured=False) != identity
                or frame.size != (box[2] - box[0], box[3] - box[1])):
            raise WindowGeometryChanged("Окно изменилось во время снимка. Снимок отменён.")
        if not hasattr(self, "_capture_identities"):
            self._capture_identities = {}
        self._capture_identities[handle] = identity
        return frame

    def stop_pressed(self):
        return bool(self.u.GetAsyncKeyState(0x77) & 0x8001)  # F8: held or pressed since last poll

    def _send_mouse(self, flags, guard=None, mouse_data=0):
        # Releases must still run after cancellation to avoid a held button.
        if flags not in (0x0004, 0x0010):
            if self.stop_pressed():
                raise InterruptedError("Остановлено: F8.")
            self.check_background_cycle()
            if guard is not None:
                guard()
        event = self.Input(type=0)
        event.mi = self.MouseInput(0, 0, int(mouse_data) & 0xffffffff, flags, 0, HELPER_INPUT_TAG)
        if self.u.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise RuntimeError("Windows не приняла ввод мыши. Помощник остановлен.")

    @_physical_api
    def _point_geometry(self, handle, x, y, expected_size):
        self.check_background_cycle()
        if self.stop_pressed():
            raise InterruptedError("Остановлено: F8.")
        if not self.active(handle):
            raise WindowGeometryChanged("Перенос остановлен: изменилось активное окно.")
        self._require_game_identity(handle)
        box = self.client_box(handle)
        if (box[2] - box[0], box[3] - box[1]) != tuple(expected_size):
            raise WindowGeometryChanged("Размер окна изменился. Требуется новый снимок сеток.")
        if not (0 <= x < expected_size[0] and 0 <= y < expected_size[1]):
            raise RuntimeError("Точка клика находится вне окна игры.")
        return wintypes.POINT(round(box[0] + x), round(box[1] + y))

    @_physical_api
    def _check_point(self, handle, x, y, expected_size):
        point = self._point_geometry(handle, x, y, expected_size)
        box = self.client_box(handle)
        # The window may change between geometry and hit-testing. The fresh
        # rectangle must still describe the same client and physical point.
        if (box[2] - box[0], box[3] - box[1]) != tuple(expected_size):
            raise WindowGeometryChanged("Размер окна изменился. Требуется новый снимок сеток.")
        if (point.x, point.y) != (round(box[0] + x), round(box[1] + y)):
            raise WindowGeometryChanged("Положение окна изменилось. Ввод отменён.")
        target = self.u.WindowFromPoint(point)
        root = self.u.GetAncestor(target, 2) if target else None
        if not target or root != handle:
            dpi_awareness = "unavailable"
            try:
                get_context = self.u.GetThreadDpiAwarenessContext
                get_context.argtypes = []
                get_context.restype = ctypes.c_void_p
                get_awareness = self.u.GetAwarenessFromDpiAwarenessContext
                get_awareness.argtypes = [ctypes.c_void_p]
                get_awareness.restype = ctypes.c_int
                dpi_awareness = get_awareness(get_context())
            except (AttributeError, OSError, ctypes.ArgumentError):
                pass
            physical_target = physical_root = "unavailable"
            try:
                get_physical = self.u.WindowFromPhysicalPoint
                get_physical.argtypes = [wintypes.POINT]
                get_physical.restype = wintypes.HWND
                physical_target = int(get_physical(point) or 0)
                physical_root = int(self.u.GetAncestor(physical_target, 2) or 0) if physical_target else 0
            except (AttributeError, OSError, ctypes.ArgumentError):
                pass
            window_dpi = window_awareness = "unavailable"
            try:
                get_dpi = self.u.GetDpiForWindow
                get_dpi.argtypes = [wintypes.HWND]
                get_dpi.restype = wintypes.UINT
                window_dpi = get_dpi(handle)
            except (AttributeError, OSError, ctypes.ArgumentError):
                pass
            try:
                get_window_context = self.u.GetWindowDpiAwarenessContext
                get_window_context.argtypes = [wintypes.HWND]
                get_window_context.restype = ctypes.c_void_p
                get_awareness = self.u.GetAwarenessFromDpiAwarenessContext
                get_awareness.argtypes = [ctypes.c_void_p]
                get_awareness.restype = ctypes.c_int
                window_awareness = get_awareness(get_window_context(handle))
            except (AttributeError, OSError, ctypes.ArgumentError):
                pass
            classes = {}
            for name, diagnostic_handle in [("target", target), ("root", root), ("selected", handle),
                                            ("physical_target", physical_target), ("physical_root", physical_root)]:
                classes[name] = "unavailable"
                if not diagnostic_handle or isinstance(diagnostic_handle, str):
                    continue
                try:
                    get_class = self.u.GetClassNameW
                    get_class.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
                    get_class.restype = ctypes.c_int
                    buffer = ctypes.create_unicode_buffer(256)
                    classes[name] = buffer.value if get_class(diagnostic_handle, buffer, len(buffer)) else "query_failed"
                except (AttributeError, OSError, ctypes.ArgumentError):
                    pass
            window_rect = window_style = window_exstyle = window_enabled = "unavailable"
            try:
                get_window_rect = self.u.GetWindowRect
                get_window_rect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
                get_window_rect.restype = wintypes.BOOL
                rectangle = wintypes.RECT()
                if get_window_rect(handle, ctypes.byref(rectangle)):
                    window_rect = (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)
                get_long = getattr(self.u, "GetWindowLongPtrW", None) or self.u.GetWindowLongW
                get_long.argtypes = [wintypes.HWND, ctypes.c_int]
                get_long.restype = ctypes.c_ssize_t
                window_style = hex(get_long(handle, -16) & 0xffffffff)
                window_exstyle = hex(get_long(handle, -20) & 0xffffffff)
                is_enabled = self.u.IsWindowEnabled
                is_enabled.argtypes = [wintypes.HWND]
                is_enabled.restype = wintypes.BOOL
                window_enabled = bool(is_enabled(handle))
            except (AttributeError, OSError, ctypes.ArgumentError):
                pass
            region_info = "unavailable"
            region = None
            try:
                gdi = ctypes.WinDLL("gdi32", use_last_error=True)
                gdi.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
                gdi.CreateRectRgn.restype = wintypes.HANDLE
                gdi.GetRgnBox.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.RECT)]
                gdi.GetRgnBox.restype = ctypes.c_int
                gdi.DeleteObject.argtypes = [wintypes.HANDLE]
                gdi.DeleteObject.restype = wintypes.BOOL
                get_region = self.u.GetWindowRgn
                get_region.argtypes = [wintypes.HWND, wintypes.HANDLE]
                get_region.restype = ctypes.c_int
                region = gdi.CreateRectRgn(0, 0, 0, 0)
                if region:
                    kind = get_region(handle, region)
                    region_box = wintypes.RECT()
                    box_kind = gdi.GetRgnBox(region, ctypes.byref(region_box)) if kind else 0
                    region_info = {"kind": kind, "box_kind": box_kind,
                                   "box": (region_box.left, region_box.top, region_box.right, region_box.bottom) if box_kind else None}
            except (AttributeError, OSError, ctypes.ArgumentError):
                pass
            finally:
                if region:
                    gdi.DeleteObject(region)
            raise HitTestMismatch(
                "Место клика перекрыто другим окном. "
                f"box={box}; local_point=({x}, {y}); "
                f"screen_point=({point.x}, {point.y}); "
                f"target_hwnd={int(target or 0)}; root_hwnd={int(root or 0)}; "
                f"selected_hwnd={int(handle)}; thread_dpi_awareness={dpi_awareness}; "
                f"physical_target_hwnd={physical_target}; physical_root_hwnd={physical_root}; "
                f"selected_window_dpi={window_dpi}; selected_window_awareness={window_awareness}; "
                f"classes={classes}; selected_window_rect={window_rect}; "
                f"selected_style={window_style}; selected_exstyle={window_exstyle}; "
                f"selected_enabled={window_enabled}; selected_region={region_info}."
            )
        # Hit testing itself crosses native calls; do not trust its earlier
        # rectangle if the window moved, resized, changed focus or PID meanwhile.
        fresh = self._point_geometry(handle, x, y, expected_size)
        if (point.x, point.y) != (fresh.x, fresh.y):
            raise WindowGeometryChanged("Положение окна изменилось. Ввод отменён.")
        return point.x, point.y

    @_physical_api
    def _check_cursor(self, expected_point):
        actual = wintypes.POINT()
        if not self.u.GetCursorPos(ctypes.byref(actual)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (actual.x, actual.y) != tuple(expected_point):
            raise RuntimeError("Курсор или положение окна изменились. Ввод отменён.")

    def _move_checked(self, handle, point, expected_size, *, tagged=False):
        physical = self._point_geometry(handle, *point, expected_size)
        if tagged:
            self._position_cursor(physical.x, physical.y, tagged=True)
        else:
            self._position_cursor(physical.x, physical.y)
        # Desktop overlays can clear click-through only after the cursor enters
        # opaque UI. No mouse button is sent before strict hit-testing succeeds.
        deadline = time.monotonic() + 1.0
        while True:
            try:
                intended = self._check_point(handle, *point, expected_size)
            except HitTestMismatch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.02, remaining))
                continue
            if intended != (physical.x, physical.y):
                raise WindowGeometryChanged("Положение окна изменилось после перемещения курсора.")
            self._check_cursor(intended)
            return intended

    @_physical_api
    def _position_cursor(self, x, y, *, tagged=False):
        self.check_background_cycle()
        if not tagged and getattr(self, "_background_token", None) is None:
            if not self.u.SetCursorPos(x, y):
                raise ctypes.WinError(ctypes.get_last_error())
            return
        # Tagged absolute SendInput lets the observer distinguish our cursor
        # movement from every physical/unrelated injected movement.
        left, top, width, height = (self.u.GetSystemMetrics(index) for index in (76, 77, 78, 79))
        if width <= 0 or height <= 0 or not (left <= x < left + width and top <= y < top + height):
            raise RuntimeError("Точка курсора выходит за пределы рабочего стола.")
        nx = min(65535, int((x - left + 0.5) * 65536 / width))
        ny = min(65535, int((y - top + 0.5) * 65536 / height))
        event = self.Input(type=0)
        event.mi = self.MouseInput(nx, ny, 0, 0x0001 | 0x8000 | 0x4000, 0, HELPER_INPUT_TAG)
        if self.u.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise RuntimeError("Windows не приняла перемещение мыши.")

    def _button_guard(self, handle, point, expected_size, expected_point, identity):
        intended = self._check_point(handle, *point, expected_size)
        if intended != expected_point or self._require_game_identity(handle) != identity:
            raise WindowGeometryChanged("Окно игры изменилось перед нажатием. Ввод отменён.")
        self._check_cursor(intended)
        fresh = self._point_geometry(handle, *point, expected_size)
        if ((fresh.x, fresh.y) != expected_point or self._require_game_identity(handle) != identity):
            raise WindowGeometryChanged("Окно игры изменилось перед нажатием. Ввод отменён.")

    @_physical_api
    def click(self, handle, point, expected_size, right=False):
        identity = self._require_game_identity(handle)
        positioned = self._move_checked(handle, point, expected_size)
        # Recheck focus and hit-test after cursor movement, before button-down.
        intended = self._check_point(handle, *point, expected_size)
        self._check_cursor(intended)
        down, up = (0x0008, 0x0010) if right else (0x0002, 0x0004)
        self._send_mouse(down, lambda: self._button_guard(handle, point, expected_size, positioned, identity))
        try:
            fresh = self._point_geometry(handle, *point, expected_size)
            if ((fresh.x, fresh.y) != positioned or self._require_game_identity(handle) != identity):
                raise WindowGeometryChanged()
        except WindowGeometryChanged as error:
            error.input_issued = True
            raise
        finally:
            self._send_mouse(up)

    def move(self, handle, point, expected_size):
        self._move_checked(handle, point, expected_size)

    @_physical_api
    def park(self, handle, point, expected_size):
        """Park with tagged absolute movement and fresh guards; never send buttons."""
        if self.stop_pressed():
            raise InterruptedError("Остановлено: F8.")
        self.check_background_cycle()
        observer = getattr(self, "input_observer", None)
        generation = None
        if observer is not None:
            observer.ensure_alive()
            generation = observer.snapshot()[0]
        self._check_acquisition_available()
        identity = self._require_game_identity(handle)
        if observer is not None and observer.snapshot()[0] != generation:
            raise UserActivityInterrupted("Пауза: ввод пользователя прервал перемещение курсора.")
        positioned = self._move_checked(handle, point, expected_size, tagged=True)
        self._check_acquisition_available()
        self._button_guard(handle, point, expected_size, positioned, identity)
        self.check_background_cycle()
        if observer is not None:
            observer.ensure_alive()
            if observer.snapshot()[0] != generation:
                raise UserActivityInterrupted("Пауза: ввод пользователя прервал перемещение курсора.")
        if self.stop_pressed():
            raise InterruptedError("Остановлено: F8.")

    @_physical_api
    def scroll(self, handle, point, expected_size, delta):
        """Issue one guarded tagged wheel pulse; the caller verifies game readback.

        Fine deltas are intentional: Windows permits values below WHEEL_DELTA.
        A successful return confirms OS acceptance, not a Unity scroll result.
        No button is pressed or released and no wheel pulse is retried.
        """
        if isinstance(delta, bool) or not isinstance(delta, int) or not 0 < abs(delta) <= 1200:
            raise ValueError("Прокрутка должна быть целым ненулевым значением от -1200 до 1200.")
        if self.stop_pressed():
            raise InterruptedError("Остановлено: F8.")
        self.check_background_cycle()
        observer = getattr(self, "input_observer", None)
        generation = None
        if observer is not None:
            observer.ensure_alive()
            generation = observer.snapshot()[0]
        self._check_acquisition_available()
        identity = self._require_game_identity(handle)
        if observer is not None and observer.snapshot()[0] != generation:
            raise UserActivityInterrupted("Пауза: ввод пользователя прервал прокрутку.")
        # Scroll opts into the existing tagged absolute movement path even
        # in the foreground. Own movement cannot advance the observer, so
        # generation remains bound before movement without a delivery race.
        # Ordinary click/move/drag callers retain their original defaults.
        positioned = self._move_checked(handle, point, expected_size, tagged=True)

        def guard():
            self._check_acquisition_available()
            self._button_guard(handle, point, expected_size, positioned, identity)
            self.check_background_cycle()
            if observer is not None:
                observer.ensure_alive()
                if observer.snapshot()[0] != generation:
                    raise UserActivityInterrupted("Пауза: ввод пользователя прервал прокрутку.")
            if self.stop_pressed():
                raise InterruptedError("Остановлено: F8.")

        self._send_mouse(0x0800, guard, mouse_data=delta)
        try:
            guard()  # Fresh geometry, focus and takeover checks after acceptance.
        except (WindowGeometryChanged, FocusUnavailable, InterruptedError, UserActivityInterrupted) as error:
            error.input_issued = True
            raise

    @_physical_api
    def drag(self, handle, source, destination, expected_size):
        identity = self._require_game_identity(handle)
        x, y = self._check_point(handle, *source, expected_size)
        box = self.client_box(handle)
        if (x, y) != (round(box[0] + source[0]), round(box[1] + source[1])):
            raise WindowGeometryChanged()
        self._check_point(handle, *destination, expected_size)
        self._position_cursor(x, y)
        intended = self._check_point(handle, *source, expected_size)
        self._check_cursor(intended)
        source_screen = (x, y)
        destination_screen = (round(box[0] + destination[0]), round(box[1] + destination[1]))
        self._send_mouse(0x0002, lambda: self._button_guard(handle, source, expected_size, source_screen, identity))
        try:
            time.sleep(0.12)
            dx, dy = self._check_point(handle, *destination, expected_size)
            if ((dx, dy) != destination_screen
                    or self._require_game_identity(handle) != identity):
                raise WindowGeometryChanged()
            self._position_cursor(dx, dy)
            time.sleep(0.12)
            intended = self._check_point(handle, *destination, expected_size)
            if (intended != destination_screen
                    or self._require_game_identity(handle) != identity):
                raise WindowGeometryChanged()
            self._check_cursor(intended)
        except WindowGeometryChanged as error:
            error.input_issued = True
            raise
        finally:
            self._send_mouse(0x0004)

