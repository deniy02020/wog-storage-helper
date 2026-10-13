"""Guarded wheel pulses use fake Win32 only; never capture or control Windows."""
import ctypes
import unittest
from unittest.mock import Mock

from input_activity import ActivityState, HELPER_INPUT_TAG, UserActivityInterrupted
from test_windows import FakeUser32, Input, WindowTestBoundary
from windows import BackgroundCycle, FocusUnavailable, WindowGeometryChanged


class WindowsScrollTests(WindowTestBoundary, unittest.TestCase):
    def recorded_desktop(self, targets=None, *, background=False, observer=False):
        desktop = self.desktop(targets or [FakeUser32.CHILD])
        desktop.events = []
        if background or observer:
            desktop.input_observer = ActivityState(600, clock=self.clock.monotonic)
            desktop.input_observer.ensure_alive = Mock()
        if background:
            desktop._background_token = BackgroundCycle(81, False, 0)
        metrics = {76: -1920, 77: -600, 78: 5760, 79: 2160}
        desktop.u.GetSystemMetrics = lambda index: metrics[index]
        def send(count, pointer, size):
            event = ctypes.cast(pointer, ctypes.POINTER(Input)).contents.mi
            desktop.events.append((event.dwFlags, event.mouseData, event.dwExtraInfo))
            if hasattr(desktop, "input_observer"):
                desktop.input_observer.record(event.dwExtraInfo)
            if event.dwFlags & 0x8000:
                desktop.u.cursor = (int(event.dx * metrics[78] / 65536) + metrics[76],
                                    int(event.dy * metrics[79] / 65536) + metrics[77])
                desktop.u.on_move()
            return count
        desktop.u.SendInput = send
        return desktop

    def scroll(self, desktop, delta=-53):
        return desktop.scroll(FakeUser32.GAME, (20, 30), (500, 400), delta)

    def wheel_events(self, desktop):
        return [event for event in desktop.events if event[0] == 0x0800]

    def test_fine_positive_and_negative_deltas_issue_one_tagged_signed_pulse(self):
        for delta in (-1200, -120, -53, -31, 31, 53, 120, 1200):
            with self.subTest(delta=delta):
                desktop = self.recorded_desktop()
                self.scroll(desktop, delta)
                self.assertEqual([event[0] for event in desktop.events], [0xC001, 0x0800])
                self.assertEqual(self.wheel_events(desktop), [(0x0800, delta & 0xffffffff, HELPER_INPUT_TAG)])
                self.assertTrue(all(event[2] == HELPER_INPUT_TAG for event in desktop.events))
                self.assertEqual(ctypes.c_int32(self.wheel_events(desktop)[0][1]).value, delta)
                self.assertEqual(desktop.u.cursor, (120, 230))
                self.assertEqual(desktop.u.cursor_moves, [])

    def test_invalid_delta_cannot_move_cursor_or_issue_any_input(self):
        for delta in (True, False, 0, 1.0, -31.0, "53", None, 1201, -1201):
            with self.subTest(delta=delta):
                desktop = self.recorded_desktop()
                with self.assertRaises(ValueError):
                    self.scroll(desktop, delta)
                self.assertEqual(desktop.events, [])
                self.assertEqual(desktop.u.cursor_moves, [])

    def test_permanent_or_new_overlay_receives_no_wheel(self):
        for targets in ([FakeUser32.OVERLAY], [FakeUser32.CHILD, FakeUser32.OVERLAY]):
            with self.subTest(targets=targets):
                desktop = self.recorded_desktop(targets)
                with self.assertRaises(WindowGeometryChanged) as caught:
                    self.scroll(desktop)
                self.assertFalse(caught.exception.input_issued)
                self.assertEqual(self.wheel_events(desktop), [])

    def test_f8_before_move_or_during_hover_sends_no_wheel(self):
        for during in (False, True):
            with self.subTest(during=during):
                desktop = self.recorded_desktop([FakeUser32.OVERLAY])
                if during:
                    desktop.u.on_hit = lambda: setattr(desktop.u, "stop", True)
                else:
                    desktop.u.stop = True
                with self.assertRaises(InterruptedError):
                    self.scroll(desktop)
                self.assertEqual(self.wheel_events(desktop), [])
                if not during:
                    self.assertEqual(desktop.events, [])
                    self.assertEqual(desktop.u.cursor_moves, [])

    def test_held_modifiers_buttons_and_gui_capture_prevent_even_cursor_move(self):
        cases = [("key", key) for key in (0x01, 0x02, 0x04, 0x05, 0x06,
                                          0x10, 0x11, 0x12, 0x5B, 0x5C)]
        cases += [("capture", 99), ("menu", 4), ("move_size", 99), ("unavailable", False)]
        for kind, value in cases:
            with self.subTest(kind=kind, value=value):
                desktop = self.recorded_desktop()
                if kind == "key":
                    desktop.u.held_keys.add(value)
                elif kind == "capture":
                    desktop.u.gui_capture = value
                elif kind == "menu":
                    desktop.u.gui_flags = value
                elif kind == "move_size":
                    desktop.u.gui_move_size = value
                else:
                    desktop.u.gui_available = value
                with self.assertRaises(FocusUnavailable):
                    self.scroll(desktop)
                self.assertEqual(desktop.events, [])
                self.assertEqual(desktop.u.cursor_moves, [])

    def test_new_modifier_during_positioning_blocks_final_wheel_guard(self):
        desktop = self.recorded_desktop()
        desktop.u.on_move = lambda: desktop.u.held_keys.add(0x11)
        with self.assertRaises(FocusUnavailable):
            self.scroll(desktop)
        self.assertEqual(self.wheel_events(desktop), [])

    def test_lost_focus_geometry_pid_or_executable_after_move_prevents_wheel(self):
        for change in ("focus", "resize", "move", "pid", "exe"):
            with self.subTest(change=change):
                desktop = self.recorded_desktop()
                def changed():
                    if change == "focus":
                        desktop.u.foreground = FakeUser32.OVERLAY
                    elif change == "resize":
                        desktop.u.box[2] -= 1
                    elif change == "move":
                        desktop.u.box[0] += 1
                        desktop.u.box[2] += 1
                    elif change == "pid":
                        desktop.u.game_pid += 1
                    else:
                        desktop.k.executable = r"C:\Other\Other.exe"
                desktop.u.on_move = changed
                with self.assertRaises(WindowGeometryChanged) as caught:
                    self.scroll(desktop)
                self.assertFalse(caught.exception.input_issued)
                self.assertEqual(self.wheel_events(desktop), [])

    def test_cursor_takeover_and_outside_client_point_send_no_wheel(self):
        desktop = self.recorded_desktop()
        desktop.u.cursor_offset = (1, 0)
        with self.assertRaises(RuntimeError):
            self.scroll(desktop)
        self.assertEqual(self.wheel_events(desktop), [])
        desktop = self.recorded_desktop()
        with self.assertRaises(RuntimeError):
            desktop.scroll(FakeUser32.GAME, (500, 30), (500, 400), -53)
        self.assertEqual(desktop.events, [])
        self.assertEqual(desktop.u.cursor_moves, [])

    def test_accepted_wheel_then_geometry_or_identity_change_is_terminal(self):
        for change in ("resize", "move", "pid", "exe", "focus", "overlay"):
            with self.subTest(change=change):
                desktop = self.recorded_desktop()
                send = desktop.u.SendInput
                def mutate(count, pointer, size):
                    result = send(count, pointer, size)
                    if desktop.events[-1][0] != 0x0800:
                        return result
                    if change == "resize":
                        desktop.u.box[2] -= 1
                    elif change == "move":
                        desktop.u.box[0] += 1
                        desktop.u.box[2] += 1
                    elif change == "pid":
                        desktop.u.game_pid += 1
                    elif change == "exe":
                        desktop.k.executable = r"C:\Other\Other.exe"
                    elif change == "focus":
                        desktop.u.foreground = FakeUser32.OVERLAY
                    else:
                        desktop.u.targets = [FakeUser32.OVERLAY]
                    return result
                desktop.u.SendInput = mutate
                with self.assertRaises((WindowGeometryChanged, FocusUnavailable)) as caught:
                    self.scroll(desktop)
                self.assertTrue(caught.exception.input_issued)
                self.assertEqual([event[0] for event in desktop.events], [0xC001, 0x0800])
                self.assertEqual(len(self.wheel_events(desktop)), 1)

    def test_sendinput_denial_is_not_retried_or_followed_by_button_release(self):
        desktop = self.recorded_desktop()
        accepted = desktop.u.SendInput
        def denied(count, pointer, size):
            receipt = accepted(count, pointer, size)
            return receipt - 1 if desktop.events[-1][0] == 0x0800 else receipt
        desktop.u.SendInput = denied
        with self.assertRaisesRegex(RuntimeError, "Windows"):
            self.scroll(desktop)
        self.assertEqual([event[0] for event in desktop.events], [0xC001, 0x0800])

    def test_background_move_and_wheel_keep_idle_generation_and_are_both_tagged(self):
        desktop = self.recorded_desktop(background=True)
        self.scroll(desktop)
        self.assertEqual([event[0] for event in desktop.events], [0xC001, 0x0800])
        self.assertTrue(all(event[2] == HELPER_INPUT_TAG for event in desktop.events))
        self.assertEqual(desktop.input_observer.snapshot()[0], 0)
        self.assertEqual(desktop.input_observer.idle_seconds(), 600)
        self.assertEqual(desktop.u.cursor_moves, [])

    def test_background_takeover_before_or_after_pulse_stops_without_repeat(self):
        for after in (False, True):
            with self.subTest(after=after):
                desktop = self.recorded_desktop(background=True)
                if after:
                    send = desktop.u.SendInput
                    def takeover(count, pointer, size):
                        result = send(count, pointer, size)
                        if desktop.events[-1][0] == 0x0800:
                            desktop.input_observer.record(0)
                        return result
                    desktop.u.SendInput = takeover
                else:
                    desktop.u.on_hit = lambda: desktop.input_observer.record(0)
                with self.assertRaises(UserActivityInterrupted) as caught:
                    self.scroll(desktop)
                self.assertEqual(len(self.wheel_events(desktop)), 1 if after else 0)
                if after:
                    self.assertTrue(caught.exception.input_issued)

    def test_dead_background_observer_prevents_movement_and_wheel(self):
        desktop = self.recorded_desktop(background=True)
        desktop.input_observer.ensure_alive.side_effect = RuntimeError("observer dead")
        with self.assertRaisesRegex(RuntimeError, "observer dead"):
            self.scroll(desktop)
        self.assertEqual(desktop.events, [])
        self.assertEqual(desktop.u.cursor_moves, [])

    def test_foreground_own_movement_is_tagged_without_advancing_generation(self):
        desktop = self.recorded_desktop(observer=True)
        desktop.u.on_move = lambda: desktop.input_observer.record(HELPER_INPUT_TAG)
        self.scroll(desktop)
        self.assertEqual(desktop.input_observer.snapshot()[0], 0)
        self.assertEqual(desktop.u.cursor_moves, [])
        self.assertEqual(len(self.wheel_events(desktop)), 1)

    def test_foreground_user_input_during_tagged_movement_blocks_wheel(self):
        desktop = self.recorded_desktop(observer=True)
        desktop.u.on_move = lambda: desktop.input_observer.record(0)
        with self.assertRaises(UserActivityInterrupted):
            self.scroll(desktop)
        self.assertEqual([event[0] for event in desktop.events], [0xC001])

    def test_foreground_user_input_during_initial_gui_query_blocks_all_movement(self):
        desktop = self.recorded_desktop(observer=True)
        desktop.u.on_gui_query = lambda: desktop.input_observer.record(0)
        with self.assertRaises(UserActivityInterrupted):
            self.scroll(desktop)
        self.assertEqual(desktop.events, [])
        self.assertEqual(desktop.u.cursor_moves, [])

    def test_foreground_new_input_after_positioning_prevents_pulse(self):
        desktop = self.recorded_desktop(observer=True)
        gui_queries = 0
        def takeover():
            nonlocal gui_queries
            gui_queries += 1
            if gui_queries == 2:
                desktop.input_observer.record(0)
        desktop.u.on_gui_query = takeover
        with self.assertRaises(UserActivityInterrupted):
            self.scroll(desktop)
        self.assertEqual(self.wheel_events(desktop), [])

    def test_negative_monitor_origin_uses_fresh_physical_game_coordinates(self):
        desktop = self.recorded_desktop()
        desktop.u.box = [-1900, -500, -1400, -100]
        self.scroll(desktop, -31)
        self.assertEqual(desktop.u.cursor, (-1880, -470))
        self.assertEqual(self.wheel_events(desktop), [(0x0800, (-31) & 0xffffffff, HELPER_INPUT_TAG)])


class WindowsParkTests(WindowTestBoundary, unittest.TestCase):
    recorded_desktop = WindowsScrollTests.recorded_desktop

    def park(self, desktop):
        desktop.park(FakeUser32.GAME, (20, 30), (500, 400))

    def test_foreground_and_background_parking_send_one_tagged_move_only(self):
        for background in (False, True):
            with self.subTest(background=background):
                desktop = self.recorded_desktop(background=background, observer=True)
                self.park(desktop)
                self.assertEqual([event[0] for event in desktop.events], [0xC001])
                self.assertEqual(desktop.events[0][1:], (0, HELPER_INPUT_TAG))
                self.assertEqual(desktop.u.cursor, (120, 230))
                self.assertEqual(desktop.u.cursor_moves, [])
                self.assertEqual(desktop.input_observer.snapshot()[0], 0)
                self.assertEqual(desktop.input_observer.idle_seconds(), 600)

    def test_legacy_foreground_move_retains_setcursorpos_and_no_tagged_sendinput(self):
        desktop = self.recorded_desktop(observer=True)
        desktop.move(FakeUser32.GAME, (20, 30), (500, 400))
        self.assertEqual(desktop.events, [])
        self.assertEqual(desktop.u.cursor_moves, [(120, 230)])

    def test_f8_before_or_during_park_sends_no_button_or_wheel(self):
        for during in (False, True):
            with self.subTest(during=during):
                desktop = self.recorded_desktop()
                if during:
                    desktop.u.on_move = lambda: setattr(desktop.u, "stop", True)
                else:
                    desktop.u.stop = True
                with self.assertRaises(InterruptedError):
                    self.park(desktop)
                self.assertEqual([event[0] for event in desktop.events], [0xC001] if during else [])

    def test_current_modifiers_or_gui_capture_block_parking_before_movement(self):
        for captured in (False, True):
            with self.subTest(captured=captured):
                desktop = self.recorded_desktop()
                if captured:
                    desktop.u.gui_capture = FakeUser32.CHILD
                else:
                    desktop.u.held_keys.add(0x11)
                with self.assertRaises(FocusUnavailable):
                    self.park(desktop)
                self.assertEqual(desktop.events, [])

    def test_real_user_takeover_during_tagged_park_is_cancelled(self):
        for background in (False, True):
            with self.subTest(background=background):
                desktop = self.recorded_desktop(background=background, observer=True)
                desktop.u.on_move = lambda: desktop.input_observer.record(0)
                with self.assertRaises(UserActivityInterrupted):
                    self.park(desktop)
                self.assertEqual([event[0] for event in desktop.events], [0xC001])

    def test_new_overlay_or_geometry_identity_change_after_park_is_rejected(self):
        for change in ("overlay", "move", "resize", "pid", "focus"):
            with self.subTest(change=change):
                desktop = self.recorded_desktop()
                def changed():
                    if change == "overlay":
                        desktop.u.targets = [FakeUser32.OVERLAY]
                    elif change == "move":
                        desktop.u.box[0] += 1
                        desktop.u.box[2] += 1
                    elif change == "resize":
                        desktop.u.box[2] -= 1
                    elif change == "pid":
                        desktop.u.game_pid += 1
                    else:
                        desktop.u.foreground = FakeUser32.OVERLAY
                desktop.u.on_move = changed
                with self.assertRaises((WindowGeometryChanged, FocusUnavailable)):
                    self.park(desktop)
                self.assertEqual([event[0] for event in desktop.events], [0xC001])

    def test_park_move_sendinput_denial_is_not_repeated(self):
        desktop = self.recorded_desktop()
        send = desktop.u.SendInput
        desktop.u.SendInput = lambda *args: send(*args) - 1
        with self.assertRaisesRegex(RuntimeError, "Windows"):
            self.park(desktop)
        self.assertEqual([event[0] for event in desktop.events], [0xC001])

    def test_parking_on_negative_monitor_uses_physical_virtual_desktop_coordinates(self):
        desktop = self.recorded_desktop()
        desktop.u.box = [-1900, -500, -1400, -100]
        self.park(desktop)
        self.assertEqual(desktop.u.cursor, (-1880, -470))
        self.assertEqual([event[0] for event in desktop.events], [0xC001])


if __name__ == "__main__":
    unittest.main()
