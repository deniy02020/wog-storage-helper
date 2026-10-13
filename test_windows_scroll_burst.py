"""Bounded native burst gates on fake Win32; never drive the real desktop."""
import ctypes
import unittest

from input_activity import HELPER_INPUT_TAG, UserActivityInterrupted
from test_windows import FakeUser32, Input
from test_windows_scroll import WindowsScrollTests
from windows import FocusUnavailable, WindowGeometryChanged


class WindowsBurstTests(WindowsScrollTests):
    def burst(self, desktop, delta=-31, count=10):
        return desktop.scroll_burst(FakeUser32.GAME, (20, 30), (500, 400), delta, count)

    def test_ten_fine_pulses_keep_full_guards_tags_and_render_gaps(self):
        desktop = self.recorded_desktop(observer=True)
        start = self.clock.now
        self.burst(desktop)
        events = self.wheel_events(desktop)
        self.assertEqual(len(events), 10)
        self.assertTrue(all(event == (0x0800, (-31)&0xffffffff, HELPER_INPUT_TAG) for event in events))
        self.assertEqual(desktop.input_observer.snapshot()[0], 0)
        self.assertAlmostEqual(self.clock.now-start, .225)

    def test_invalid_burst_cannot_move_or_send_any_input(self):
        for delta, count in ((120, 10), (0, 10), (True, 10), (-31, 1), (-31, 11),
                             (-31, True), (-31, 2.5)):
            with self.subTest(delta=delta, count=count):
                desktop = self.recorded_desktop()
                with self.assertRaises(ValueError):
                    self.burst(desktop, delta, count)
                self.assertEqual(desktop.events, [])

    def test_f8_or_takeover_between_pulses_marks_prior_input_and_stops_immediately(self):
        for change in ("f8", "activity", "move", "pid", "exe", "focus", "overlay", "modifier"):
            with self.subTest(change=change):
                desktop = self.recorded_desktop(observer=True)
                send = desktop.u.SendInput
                def mutate(count, pointer, size):
                    receipt = send(count, pointer, size)
                    event = ctypes.cast(pointer, ctypes.POINTER(Input)).contents.mi
                    if event.dwFlags != 0x0800 or len(self.wheel_events(desktop)) != 2:
                        return receipt
                    if change == "f8": desktop.u.stop = True
                    elif change == "activity": desktop.input_observer.record(0)
                    elif change == "move":
                        desktop.u.box[0] += 1
                        desktop.u.box[2] += 1
                    elif change == "pid": desktop.u.game_pid += 1
                    elif change == "exe": desktop.k.executable = r"C:\Other\Other.exe"
                    elif change == "focus": desktop.u.foreground = FakeUser32.OVERLAY
                    elif change == "overlay": desktop.u.targets = [FakeUser32.OVERLAY]
                    else: desktop.u.held_keys.add(0x11)
                    return receipt
                desktop.u.SendInput = mutate
                with self.assertRaises((InterruptedError, UserActivityInterrupted, WindowGeometryChanged,
                                        FocusUnavailable)) as caught:
                    self.burst(desktop)
                self.assertTrue(caught.exception.input_issued)
                self.assertEqual(len(self.wheel_events(desktop)), 2)

    def test_denied_later_packet_is_not_repeated_or_followed_by_cleanup(self):
        desktop = self.recorded_desktop()
        send = desktop.u.SendInput
        def deny(count, pointer, size):
            result = send(count, pointer, size)
            if desktop.events[-1][0] == 0x0800 and len(self.wheel_events(desktop)) == 3:
                return 0
            return result
        desktop.u.SendInput = deny
        with self.assertRaisesRegex(RuntimeError, "Windows") as caught:
            self.burst(desktop)
        self.assertTrue(caught.exception.input_issued)
        self.assertEqual(len(self.wheel_events(desktop)), 3)
        self.assertFalse(any(flags in (0x02, 0x04, 0x08, 0x10) for flags, *_ in desktop.events))

    def test_f8_before_burst_does_not_even_move_pointer(self):
        desktop = self.recorded_desktop()
        desktop.u.stop = True
        with self.assertRaises(InterruptedError):
            self.burst(desktop)
        self.assertEqual(desktop.events, [])


if __name__ == "__main__":
    unittest.main()
