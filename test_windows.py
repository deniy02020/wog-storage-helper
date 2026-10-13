"""Mouse safety tests with fake user32 only; never capture or control the desktop."""
import ctypes
from contextlib import nullcontext
from ctypes import wintypes
import unittest
from unittest.mock import patch

import windows
from windows import WindowsDesktop


class MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", MouseInput)]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class FakeUser32:
    GAME, CHILD, OVERLAY, OVERLAY_ROOT = 23, 24, 81, 82

    def __init__(self, targets):
        self.targets = list(targets)
        self.hit_calls = 0
        self.box = [100, 200, 600, 600]
        self.foreground = self.GAME
        self.stop = False
        self.cursor = (0, 0)
        self.cursor_moves = []
        self.button_events = []
        self.on_move = lambda: None
        self.on_hit = lambda: None
        self.cursor_offset = (0, 0)
        self.game_pid = 1001
        self.game_title = "War of Genesis: Idle Loot"
        self.on_pid = lambda: None
        self.held_keys = set()
        self.gui_capture = 0
        self.gui_flags = 0
        self.gui_menu_owner = self.gui_move_size = 0
        self.gui_available = True
        self.on_gui_query = lambda: None

    def IsWindow(self, handle):
        return handle == self.GAME

    def IsWindowVisible(self, handle):
        return handle == self.GAME

    def IsIconic(self, handle):
        return False

    def GetForegroundWindow(self):
        return self.foreground

    def GetWindowThreadProcessId(self, handle, pointer):
        self.on_pid()
        pointer._obj.value = self.game_pid if handle == self.GAME else 9000
        return 10

    def GetWindowTextLengthW(self, handle):
        return len(self.game_title) if handle == self.GAME else 8

    def GetWindowTextW(self, handle, buffer, size):
        buffer.value = self.game_title if handle == self.GAME else "Explorer"
        return len(buffer.value)

    def GetClientRect(self, handle, pointer):
        rectangle = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
        rectangle.left = rectangle.top = 0
        rectangle.right = self.box[2] - self.box[0]
        rectangle.bottom = self.box[3] - self.box[1]
        return True

    def ClientToScreen(self, handle, pointer):
        point = ctypes.cast(pointer, ctypes.POINTER(wintypes.POINT)).contents
        point.x, point.y = self.box[:2]
        return True

    def GetAsyncKeyState(self, key):
        return 0x8000 if (key == 0x77 and self.stop) or key in self.held_keys else 0

    def GetGUIThreadInfo(self, thread_id, pointer):
        if thread_id != 0:
            raise AssertionError("Only the current foreground thread is queried")
        info = pointer._obj
        if info.cbSize != ctypes.sizeof(type(info)):
            raise AssertionError("GUITHREADINFO.cbSize must match its structure")
        info.hwndActive = self.foreground
        info.hwndCapture = self.gui_capture
        info.hwndMenuOwner = self.gui_menu_owner
        info.hwndMoveSize = self.gui_move_size
        info.flags = self.gui_flags
        self.on_gui_query()
        return self.gui_available

    def SetCursorPos(self, x, y):
        self.cursor = (x, y)
        self.cursor_moves.append(self.cursor)
        self.on_move()
        return True

    def GetCursorPos(self, pointer):
        point = ctypes.cast(pointer, ctypes.POINTER(wintypes.POINT)).contents
        point.x = self.cursor[0] + self.cursor_offset[0]
        point.y = self.cursor[1] + self.cursor_offset[1]
        return True

    def WindowFromPoint(self, point):
        index = min(self.hit_calls, len(self.targets) - 1)
        self.hit_calls += 1
        target = self.targets[index]
        self.on_hit()
        return target

    def GetAncestor(self, target, kind):
        return self.GAME if target in (self.GAME, self.CHILD) else self.OVERLAY_ROOT

    def SendInput(self, count, pointer, size):
        event = ctypes.cast(pointer, ctypes.POINTER(Input)).contents
        self.button_events.append(event.mi.dwFlags)
        return count


class FakeKernel32:
    def __init__(self):
        self.executable = r"C:\Games\Genesis.exe"
        self.opened = []
        self.closed = []
        self.on_query = lambda: None
        self.query_ok = True

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return 100

    def QueryFullProcessImageNameW(self, process, flags, buffer, pointer):
        self.on_query()
        buffer.value = self.executable
        pointer._obj.value = len(buffer.value)
        return self.query_ok

    def CloseHandle(self, process):
        self.closed.append(process)
        return True


class WindowTestBoundary:
    def setUp(self):
        self.clock = FakeClock()
        # Diagnostic code may try to load optional DLLs. Stub that boundary too.
        self.patches = [
            patch.object(windows.time, "monotonic", self.clock.monotonic),
            patch.object(windows.time, "sleep", self.clock.sleep),
            patch.object(windows.ctypes, "WinDLL", create=True,
                         side_effect=AttributeError("No real DLLs in these tests")),
            patch.object(WindowsDesktop, "_physical_coordinates", side_effect=nullcontext),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def desktop(self, targets):
        desktop = WindowsDesktop.__new__(WindowsDesktop)
        desktop.u = FakeUser32(targets)
        desktop.k = FakeKernel32()
        desktop._capture_identities = {}
        desktop.Input, desktop.MouseInput = Input, MouseInput
        return desktop


class WindowsInputSafetyTests(WindowTestBoundary, unittest.TestCase):
    def click(self, desktop, *, right=False):
        desktop.click(FakeUser32.GAME, (20, 30), (500, 400), right=right)

    def test_transient_click_through_then_game_receives_exactly_one_click(self):
        for right, expected in ((False, [0x0002, 0x0004]),
                                (True, [0x0008, 0x0010])):
            with self.subTest(right=right):
                desktop = self.desktop([FakeUser32.OVERLAY, FakeUser32.OVERLAY,
                                        FakeUser32.CHILD])
                self.click(desktop, right=right)
                self.assertEqual(desktop.u.cursor_moves, [(120, 230)])
                self.assertEqual(desktop.u.button_events, expected)

    def test_permanent_overlay_never_receives_button_input(self):
        desktop = self.desktop([FakeUser32.OVERLAY])
        with self.assertRaises(RuntimeError):
            self.click(desktop)
        self.assertEqual(desktop.u.button_events, [])
        self.assertLessEqual(self.clock.now, 1.01)

    def test_delayed_hover_after_half_second_sends_one_click_only_when_validated(self):
        desktop = self.desktop([FakeUser32.OVERLAY])

        def delayed_target(point):
            return FakeUser32.CHILD if self.clock.now >= 0.5 else FakeUser32.OVERLAY

        desktop.u.WindowFromPoint = delayed_target
        self.click(desktop)

        self.assertGreaterEqual(self.clock.now, 0.5)
        self.assertLessEqual(self.clock.now, 1.1)
        self.assertEqual(desktop.u.cursor_moves, [(120, 230)])
        self.assertEqual(desktop.u.button_events, [0x0002, 0x0004])

    def test_f8_during_extended_hover_wait_stops_before_mouse_down(self):
        desktop = self.desktop([FakeUser32.OVERLAY])

        def press_f8_after_half_second():
            if self.clock.now >= 0.5:
                desktop.u.stop = True

        desktop.u.on_hit = press_f8_after_half_second
        with self.assertRaises(InterruptedError):
            self.click(desktop)

        self.assertGreaterEqual(self.clock.now, 0.5)
        self.assertLessEqual(self.clock.now, 0.55)
        self.assertEqual(desktop.u.button_events, [])

    def test_no_window_at_point_never_receives_button_input(self):
        desktop = self.desktop([0])
        with self.assertRaises(RuntimeError):
            self.click(desktop)
        self.assertEqual(desktop.u.button_events, [])

    def test_f8_before_movement_prevents_all_input(self):
        desktop = self.desktop([FakeUser32.CHILD])
        desktop.u.stop = True
        with self.assertRaises(InterruptedError):
            self.click(desktop)
        self.assertEqual(desktop.u.cursor_moves, [])
        self.assertEqual(desktop.u.button_events, [])

    def test_f8_during_hover_wait_prevents_button_input(self):
        desktop = self.desktop([FakeUser32.OVERLAY, FakeUser32.CHILD])
        desktop.u.on_hit = lambda: setattr(desktop.u, "stop", True)
        with self.assertRaises(InterruptedError):
            self.click(desktop)
        self.assertEqual(desktop.u.button_events, [])

    def test_losing_focus_during_hover_wait_prevents_button_input(self):
        desktop = self.desktop([FakeUser32.OVERLAY, FakeUser32.CHILD])
        desktop.u.on_hit = lambda: setattr(desktop.u, "foreground", FakeUser32.OVERLAY)
        with self.assertRaises(RuntimeError):
            self.click(desktop)
        self.assertEqual(desktop.u.button_events, [])

    def test_cursor_moved_by_user_prevents_button_input(self):
        desktop = self.desktop([FakeUser32.CHILD])
        desktop.u.cursor_offset = (1, 0)
        with self.assertRaises(RuntimeError):
            self.click(desktop)
        self.assertEqual(desktop.u.button_events, [])

    def test_window_moved_after_cursor_positioning_prevents_button_input(self):
        desktop = self.desktop([FakeUser32.CHILD])

        def move_window():
            desktop.u.box[0] += 10
            desktop.u.box[2] += 10

        desktop.u.on_move = move_window
        with self.assertRaises(RuntimeError):
            self.click(desktop)
        self.assertEqual(desktop.u.button_events, [])

    def test_window_resized_after_cursor_positioning_prevents_button_input(self):
        desktop = self.desktop([FakeUser32.CHILD])
        desktop.u.on_move = lambda: desktop.u.box.__setitem__(2, 599)
        with self.assertRaises(RuntimeError):
            self.click(desktop)
        self.assertEqual(desktop.u.button_events, [])

    def test_second_client_rect_read_in_final_hit_test_cancels_changed_geometry(self):
        # Mutate one of the paired rectangle reads after cursor positioning.
        for change in ("resize", "move"):
            with self.subTest(change=change):
                desktop = self.desktop([FakeUser32.CHILD])
                original = desktop.u.GetClientRect
                reads = []

                def rectangle(handle, pointer):
                    reads.append(handle)
                    if desktop.u.cursor_moves and len(reads) == 4:
                        if change == "resize":
                            desktop.u.box[2] -= 1
                        else:
                            desktop.u.box[0] += 10
                            desktop.u.box[2] += 10
                    return original(handle, pointer)

                desktop.u.GetClientRect = rectangle
                with self.assertRaises(RuntimeError):
                    self.click(desktop)
                self.assertEqual(len(reads), 4)
                self.assertEqual(desktop.u.cursor_moves, [(120, 230)])
                self.assertEqual(desktop.u.hit_calls, 0)
                self.assertEqual(desktop.u.button_events, [])

    def test_overlay_appearing_after_hover_validation_prevents_button_input(self):
        desktop = self.desktop([FakeUser32.CHILD, FakeUser32.OVERLAY])
        with self.assertRaises(RuntimeError):
            self.click(desktop)
        self.assertEqual(desktop.u.button_events, [])

    def test_outside_client_point_prevents_all_input(self):
        desktop = self.desktop([FakeUser32.CHILD])
        with self.assertRaises(RuntimeError):
            desktop.click(FakeUser32.GAME, (500, 30), (500, 400))
        self.assertEqual(desktop.u.cursor_moves, [])
        self.assertEqual(desktop.u.button_events, [])

    def test_move_waits_for_game_hit_test_without_sending_buttons(self):
        desktop = self.desktop([FakeUser32.OVERLAY, FakeUser32.CHILD])
        desktop.move(FakeUser32.GAME, (20, 30), (500, 400))
        self.assertEqual(desktop.u.cursor, (120, 230))
        self.assertEqual(desktop.u.button_events, [])
        self.assertGreaterEqual(desktop.u.hit_calls, 2)


if __name__ == "__main__":
    unittest.main()
