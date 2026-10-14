"""Typed cursor/focus cancellations exercise fake Win32, never the desktop."""
import ctypes
import unittest
from unittest.mock import patch

from PIL import Image

from input_activity import UserActivityInterrupted
from test_windows import FakeUser32, Input, WindowTestBoundary
import test_windows_scroll
from windows import (BackgroundCycle, CursorPositionChanged,
                     ForegroundWindowChanged, HitTestMismatch,
                     WindowGeometryChanged)


class NativePreflightInterruptionTests(WindowTestBoundary, unittest.TestCase):
    def operation(self, desktop, kind):
        arguments = (FakeUser32.GAME, (20, 30), (500, 400))
        if kind == "drag":
            return desktop.drag(FakeUser32.GAME, (20, 30), (50, 70), (500, 400))
        if kind == "scroll":
            return desktop.scroll(*arguments, -31)
        if kind == "park":
            return desktop.park(*arguments)
        return desktop.click(*arguments, right=kind == "right-click")

    def recorded_desktop(self, *, observer=False):
        return test_windows_scroll.WindowsScrollTests.recorded_desktop(
            self, observer=observer)

    def assert_no_buttons_or_wheel(self, desktop):
        self.assertFalse(any(flags in (2, 4, 8, 16, 0x0800)
                             for flags, *_ in getattr(desktop, "events", [])))
        self.assertEqual(desktop.u.button_events, [])

    def test_cursor_mismatch_before_input_is_typed_for_each_native_operation(self):
        for kind in ("left-click", "right-click", "drag", "scroll", "park"):
            with self.subTest(kind=kind):
                desktop = self.recorded_desktop()
                desktop.u.cursor_offset = (1, 0)
                with self.assertRaises(CursorPositionChanged) as caught:
                    self.operation(desktop, kind)
                self.assertIs(caught.exception.input_issued, False)
                self.assert_no_buttons_or_wheel(desktop)

    def test_foreground_loss_before_input_is_typed_without_any_buttons(self):
        for kind in ("left-click", "right-click", "drag", "scroll", "park"):
            with self.subTest(kind=kind):
                desktop = self.recorded_desktop()
                desktop.u.foreground = FakeUser32.OVERLAY
                with self.assertRaises(ForegroundWindowChanged) as caught:
                    self.operation(desktop, kind)
                self.assertIs(caught.exception.input_issued, False)
                self.assert_no_buttons_or_wheel(desktop)

    def test_focus_loss_during_hit_test_is_typed_before_button_down(self):
        desktop = self.desktop([FakeUser32.CHILD])
        desktop.u.on_hit = lambda: setattr(desktop.u, "foreground", FakeUser32.OVERLAY)
        with self.assertRaises(ForegroundWindowChanged) as caught:
            self.operation(desktop, "left-click")
        self.assertIs(caught.exception.input_issued, False)
        self.assertEqual(desktop.u.button_events, [])

    def test_replaced_or_minimized_game_is_not_misclassified_as_focus_loss(self):
        for reason in ("pid", "executable", "minimized", "hidden"):
            with self.subTest(reason=reason):
                desktop = self.desktop([FakeUser32.CHILD])
                desktop._capture_identities[FakeUser32.GAME] = desktop._require_game_identity(FakeUser32.GAME)
                desktop.u.foreground = FakeUser32.OVERLAY
                if reason == "pid":
                    desktop.u.game_pid += 1
                elif reason == "executable":
                    desktop.k.executable = r"C:\Other\Other.exe"
                elif reason == "minimized":
                    desktop.u.IsIconic = lambda handle: True
                else:
                    desktop.u.IsWindowVisible = lambda handle: False
                with self.assertRaises(WindowGeometryChanged) as caught:
                    self.operation(desktop, "left-click")
                self.assertNotIsInstance(caught.exception, ForegroundWindowChanged)
                self.assertIs(caught.exception.input_issued, False)
                self.assertEqual(desktop.u.button_events, [])

    def test_permanent_overlay_preserves_strict_hit_test_error(self):
        desktop = self.desktop([FakeUser32.OVERLAY])
        with self.assertRaises(HitTestMismatch) as caught:
            self.operation(desktop, "left-click")
        self.assertIs(caught.exception.input_issued, False)
        self.assertEqual(desktop.u.button_events, [])
        self.assertLessEqual(self.clock.now, 1.01)

    def test_accepted_click_or_drag_focus_loss_marks_input_and_releases_once(self):
        for kind, down, up in (("left-click", 2, 4), ("right-click", 8, 16), ("drag", 2, 4)):
            with self.subTest(kind=kind):
                desktop = self.desktop([FakeUser32.CHILD])
                send = desktop.u.SendInput

                def lose_focus(count, pointer, size):
                    result = send(count, pointer, size)
                    if desktop.u.button_events[-1] == down:
                        desktop.u.foreground = FakeUser32.OVERLAY
                    return result

                desktop.u.SendInput = lose_focus
                with self.assertRaises(ForegroundWindowChanged) as caught:
                    self.operation(desktop, kind)
                self.assertIs(caught.exception.input_issued, True)
                self.assertEqual(desktop.u.button_events, [down, up])

    def test_accepted_drag_cursor_drift_marks_input_and_releases_without_replay(self):
        desktop = self.desktop([FakeUser32.CHILD])
        send = desktop.u.SendInput

        def drift_after_down(count, pointer, size):
            result = send(count, pointer, size)
            if desktop.u.button_events[-1] == 2:
                desktop.u.cursor_offset = (1, 0)
            return result

        desktop.u.SendInput = drift_after_down
        with self.assertRaises(CursorPositionChanged) as caught:
            self.operation(desktop, "drag")
        self.assertIs(caught.exception.input_issued, True)
        self.assertEqual(desktop.u.button_events, [2, 4])
        self.assertEqual(desktop.u.cursor_moves, [(120, 230), (150, 270)])

    def test_accepted_wheel_cursor_or_focus_change_cannot_be_replayed(self):
        for reason, exception_type in (("cursor", CursorPositionChanged),
                                       ("focus", ForegroundWindowChanged)):
            with self.subTest(reason=reason):
                desktop = self.recorded_desktop()
                send = desktop.u.SendInput

                def change_after_wheel(count, pointer, size):
                    result = send(count, pointer, size)
                    flags = ctypes.cast(pointer, ctypes.POINTER(Input)).contents.mi.dwFlags
                    if flags == 0x0800:
                        if reason == "cursor":
                            desktop.u.cursor_offset = (1, 0)
                        else:
                            desktop.u.foreground = FakeUser32.OVERLAY
                    return result

                desktop.u.SendInput = change_after_wheel
                with self.assertRaises(exception_type) as caught:
                    self.operation(desktop, "scroll")
                self.assertIs(caught.exception.input_issued, True)
                self.assertEqual([flags for flags, *_ in desktop.events], [0xC001, 0x0800])

    def test_user_activity_or_f8_after_accepted_down_stays_issued_and_releases_once(self):
        for kind in ("left-click", "right-click", "drag"):
            for reason, exception_type in (("activity", UserActivityInterrupted),
                                           ("f8", InterruptedError)):
                with self.subTest(kind=kind, reason=reason):
                    desktop = self.recorded_desktop(observer=True)
                    desktop._background_token = BackgroundCycle(81, False, 0)
                    down, up = (8, 16) if kind == "right-click" else (2, 4)
                    send = desktop.u.SendInput

                    def interrupt(count, pointer, size):
                        result = send(count, pointer, size)
                        flags = ctypes.cast(pointer, ctypes.POINTER(Input)).contents.mi.dwFlags
                        if flags == down:
                            if reason == "activity":
                                desktop.input_observer.record(0)
                            else:
                                desktop.u.stop = True
                        return result

                    desktop.u.SendInput = interrupt
                    with self.assertRaises(exception_type) as caught:
                        self.operation(desktop, kind)
                    self.assertIs(caught.exception.input_issued, True)
                    self.assertEqual([flags for flags, *_ in desktop.events
                                      if flags in (2, 4, 8, 16)], [down, up])

    def test_observer_takeover_before_down_explicitly_proves_no_game_input(self):
        for kind in ("left-click", "right-click", "drag", "scroll", "park"):
            with self.subTest(kind=kind):
                desktop = self.recorded_desktop(observer=True)
                desktop._background_token = BackgroundCycle(81, False, 0)
                desktop.input_observer.record(0)
                with self.assertRaises(UserActivityInterrupted) as caught:
                    self.operation(desktop, kind)
                self.assertIs(caught.exception.input_issued, False)
                self.assertEqual(desktop.events, [])
                self.assertEqual(desktop.u.cursor_moves, [])

    def test_activity_input_certainty_accepts_only_explicit_boolean_values(self):
        for invalid in (None, 0, 1, "false", "true"):
            with self.subTest(value=invalid):
                with self.assertRaises(TypeError):
                    UserActivityInterrupted("takeover", input_issued=invalid)

    def test_capture_focus_loss_before_grab_has_no_screenshot_or_input(self):
        desktop = self.desktop([FakeUser32.CHILD])
        desktop.u.foreground = FakeUser32.OVERLAY
        with patch("PIL.ImageGrab.grab") as grab:
            with self.assertRaises(ForegroundWindowChanged) as caught:
                desktop.capture(FakeUser32.GAME)
        self.assertIs(caught.exception.input_issued, False)
        grab.assert_not_called()
        self.assertEqual(desktop._capture_identities, {})
        self.assertEqual(desktop.u.cursor_moves, [])
        self.assertEqual(desktop.u.button_events, [])

    def test_capture_focus_loss_after_grab_is_typed_only_for_unchanged_client(self):
        desktop = self.desktop([FakeUser32.CHILD])
        frame = Image.new("RGB", (500, 400))

        def lose_focus(**kwargs):
            self.assertEqual(kwargs, {"bbox": (100, 200, 600, 600), "all_screens": True})
            desktop.u.foreground = FakeUser32.OVERLAY
            return frame

        with patch("PIL.ImageGrab.grab", side_effect=lose_focus) as grab:
            with self.assertRaises(ForegroundWindowChanged) as caught:
                desktop.capture(FakeUser32.GAME)
        self.assertIs(caught.exception.input_issued, False)
        grab.assert_called_once()
        self.assertEqual(desktop._capture_identities, {})
        self.assertEqual(desktop.u.cursor_moves, [])
        self.assertEqual(desktop.u.button_events, [])

    def test_capture_simultaneous_focus_and_geometry_or_identity_change_is_not_downgraded(self):
        for reason in ("move", "resize", "pid", "executable", "hidden", "minimized", "frame-size"):
            with self.subTest(reason=reason):
                desktop = self.desktop([FakeUser32.CHILD])

                def change(**kwargs):
                    desktop.u.foreground = FakeUser32.OVERLAY
                    if reason == "move":
                        desktop.u.box[0] += 10
                        desktop.u.box[2] += 10
                    elif reason == "resize":
                        desktop.u.box[2] += 10
                    elif reason == "pid":
                        desktop.u.game_pid += 1
                    elif reason == "executable":
                        desktop.k.executable = r"C:\Other\Other.exe"
                    elif reason == "hidden":
                        desktop.u.IsWindowVisible = lambda handle: False
                    elif reason == "minimized":
                        desktop.u.IsIconic = lambda handle: True
                    return Image.new("RGB", (499, 400) if reason == "frame-size" else (500, 400))

                with patch("PIL.ImageGrab.grab", side_effect=change):
                    with self.assertRaises(WindowGeometryChanged) as caught:
                        desktop.capture(FakeUser32.GAME)
                self.assertNotIsInstance(caught.exception, ForegroundWindowChanged)
                self.assertIs(caught.exception.input_issued, False)
                self.assertEqual(desktop._capture_identities, {})
                self.assertEqual(desktop.u.button_events, [])

    def test_capture_rectangle_change_during_focus_read_stays_geometry_error(self):
        desktop = self.desktop([FakeUser32.CHILD])
        final_focus_read = False
        foreground = desktop.u.GetForegroundWindow

        def focus_read():
            if final_focus_read:
                desktop.u.box[0] += 1
                desktop.u.box[2] += 1
            return foreground()

        def frame(**kwargs):
            nonlocal final_focus_read
            desktop.u.foreground = FakeUser32.OVERLAY
            final_focus_read = True
            return Image.new("RGB", (500, 400))

        desktop.u.GetForegroundWindow = focus_read
        with patch("PIL.ImageGrab.grab", side_effect=frame):
            with self.assertRaises(WindowGeometryChanged) as caught:
                desktop.capture(FakeUser32.GAME)
        self.assertNotIsInstance(caught.exception, ForegroundWindowChanged)
        self.assertIs(caught.exception.input_issued, False)
        self.assertEqual(desktop._capture_identities, {})


if __name__ == "__main__":
    unittest.main()
