"""Supported interval selection preserves saved values and the busy-state gate."""
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from ui_time_picker import (TimeWheel, INTERVALS, interval_index, displayed_interval,
                            wheel_pose, snap_index, clock_text, CountdownDial,
                            countdown_font_pixels)
from ui_widgets import ScalarTween


class PickerTests(unittest.TestCase):
    def fake(self, value=1800, state="normal"):
        current = [str(value)]
        obj = SimpleNamespace(_state=state, variable=SimpleNamespace(
            get=lambda: current[0], set=lambda value: current.__setitem__(0, value)), command=Mock())
        obj.step = lambda delta: TimeWheel.step(obj, delta)
        return obj, current

    def test_both_bounds_ignore_repeat_input_and_do_not_save(self):
        for value, delta in ((60, -1), (86400, 1)):
            obj, current = self.fake(value)
            TimeWheel.step(obj, delta)
            self.assertEqual(int(current[0]), value)
            obj.command.assert_not_called()

    def test_keyboard_page_and_home_end_use_minute_then_half_hour_choices(self):
        obj, current = self.fake()
        for key, expected in (("Next", 5400), ("Prior", 1800), ("End", 86400), ("Home", 60), ("Down", 1800)):
            TimeWheel._key(obj, SimpleNamespace(keysym=key))
            self.assertEqual(int(current[0]), expected)
            self.assertIn(int(current[0]), INTERVALS)

    def test_disabled_picker_cannot_write_or_invoke_save_by_keyboard_or_wheel(self):
        obj, current = self.fake(3600, "disabled")
        TimeWheel._key(obj, SimpleNamespace(keysym="End"))
        TimeWheel._wheel(obj, SimpleNamespace(delta=-120))
        self.assertEqual(current, ["3600"])
        obj.command.assert_not_called()

    def test_saved_long_non_step_interval_gets_nearest_visible_choice(self):
        self.assertEqual(interval_index(60), 0)
        self.assertEqual(interval_index(1800.5), 1)
        self.assertEqual(interval_index(14400), 8)
        self.assertEqual(interval_index(86400), len(INTERVALS) - 1)
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                interval_index(value)

    def test_settled_label_keeps_saved_45_minutes_and_navigation_selects_real_steps(self):
        index = interval_index(2700)
        self.assertEqual(displayed_interval(2700, index, index), 2700)
        self.assertEqual(displayed_interval(2700, index, index - .5), INTERVALS[index])
        obj, current = self.fake(2700)
        TimeWheel._key(obj, SimpleNamespace(keysym="Home"))
        self.assertEqual(current, ["60"])
        obj.command.assert_called_once()

    def test_non_step_value_moves_to_adjacent_half_hour_in_each_direction(self):
        for delta, expected in ((1, "3600"), (-1, "1800"), (2, "5400")):
            obj, current = self.fake(2700)
            TimeWheel.step(obj, delta)
            self.assertEqual(current, [expected])
            obj.command.assert_called_once()

    def test_only_one_minute_choice_is_added_before_existing_half_hour_intervals(self):
        self.assertEqual(INTERVALS[:3], (60, 1800, 3600))
        self.assertEqual(INTERVALS[1:], tuple(range(1800, 86401, 1800)))
        for value, delta, expected in ((60, 1, "1800"), (1800, -1, "60"),
                                       (900, -1, "60"), (900, 1, "1800")):
            with self.subTest(value=value, delta=delta):
                obj, current = self.fake(value)
                TimeWheel.step(obj, delta)
                self.assertEqual(current, [expected])
                obj.command.assert_called_once()

    def test_saved_custom_short_interval_is_displayed_until_the_user_selects_a_choice(self):
        index = interval_index(900)
        self.assertEqual(index, 0)
        self.assertEqual(displayed_interval(900, index, index), 900)
        self.assertEqual(displayed_interval(900, index, index + .5), 60)


class PerspectiveMotionTests(unittest.TestCase):
    def test_centre_is_largest_and_neighbours_recede_symmetrically(self):
        centre = wheel_pose(0, 300)
        self.assertEqual((centre.y, centre.scale, centre.opacity), (150, 1, 1))
        previous = centre
        for distance in (1, 2, 3):
            upper, lower = wheel_pose(-distance, 300), wheel_pose(distance, 300)
            self.assertAlmostEqual(upper.y + lower.y, 300)
            self.assertEqual(upper.scale, lower.scale)
            self.assertEqual(upper.opacity, lower.opacity)
            self.assertLess(lower.scale, previous.scale)
            self.assertLess(lower.opacity, previous.opacity)
            previous = lower

    def test_fractional_motion_remains_continuous_through_selection_plane(self):
        before, at, after = [wheel_pose(value, 300) for value in (-.001, 0, .001)]
        self.assertLess(abs(before.y-at.y), .1)
        self.assertLess(abs(after.y-at.y), .1)
        self.assertLess(abs(before.scale-after.scale), .0001)
        self.assertLess(abs(before.opacity-after.opacity), .0001)

    def test_inertia_is_bounded_and_snaps_only_to_supported_values(self):
        for position, velocity in ((-100, -20), (1000, 20), (3.4, 5), (3.4, -5)):
            index = snap_index(position, velocity)
            self.assertIn(INTERVALS[index], INTERVALS)
            self.assertGreaterEqual(index, 0)
            self.assertLess(index, len(INTERVALS))
        self.assertGreater(snap_index(8, 10), snap_index(8, -10))
        with self.assertRaises(ValueError):
            snap_index(float("nan"))

    def test_clock_uses_the_real_deadline_or_a_dash_without_inventing_time(self):
        for value, expected in ((None, "—:—"), (0, "00:00"), (-1, "00:00"),
                                (29.1, "00:30"), (3599, "59:59"), (3600, "01:00:00"),
                                (86400, "24:00:00")):
            self.assertEqual(clock_text(value), expected)
        with self.assertRaises(ValueError):
            clock_text(float("inf"))

    def test_clock_is_prominent_but_long_hour_values_fit_within_the_dial(self):
        self.assertEqual(countdown_font_pixels("29:59", 540, 438), 74)
        hours = countdown_font_pixels("24:00:00", 540, 438)
        self.assertGreaterEqual(hours, 60)
        self.assertLess(hours, 74)
        available = min(540,438)*.68-18
        self.assertLessEqual(hours*8*.58, available)
        self.assertLess(countdown_font_pixels("24:00:00", 360, 300), hours)

    def selection(self, value=2700, target=1, pending=False, state="normal"):
        current = [str(value)]
        obj = SimpleNamespace(_state=state, _snap_timer=None, _pending_selection=pending,
            variable=SimpleNamespace(get=lambda: current[0], set=lambda v: current.__setitem__(0,v)),
            _position=ScalarTween(target), animations_enabled=False, _cancel=Mock(),
            _ensure_frame=Mock(), event_generate=Mock(), command=Mock())
        return obj, current

    def test_start_flush_preserves_saved_non_step_time_without_a_pending_gesture(self):
        obj, current = self.selection()
        self.assertEqual(TimeWheel.commit_pending(obj), 2700)
        self.assertEqual(current, ["2700"])
        obj.command.assert_not_called()
        obj.event_generate.assert_not_called()

    def test_one_gesture_commits_once_and_disabled_picker_never_commits(self):
        obj, current = self.selection(target=3.7, pending=True)
        TimeWheel.commit_pending(obj, notify=True)
        TimeWheel.commit_pending(obj, notify=True)
        self.assertEqual(current, ["7200"])
        obj.command.assert_called_once()
        obj.event_generate.assert_called_once()
        obj, current = self.selection(target=3.7, pending=True, state="disabled")
        TimeWheel.commit_pending(obj, notify=True)
        self.assertEqual(current, ["2700"])
        obj.command.assert_not_called()

    def test_pending_gesture_can_select_one_minute_before_start(self):
        obj, current = self.selection(value=14400, target=0, pending=True)
        self.assertEqual(TimeWheel.commit_pending(obj), 60)
        self.assertEqual(current, ["60"])
        self.assertEqual(clock_text(60), "01:00")
        obj.event_generate.assert_called_once()

    def test_hidden_wheel_and_countdown_schedule_no_frames(self):
        wheel = SimpleNamespace(_mapped=False, _state="normal", animations_enabled=True,
            _closed=False, _frame=None, _position=ScalarTween(0), _schedule=Mock())
        TimeWheel._ensure_frame(wheel)
        wheel._schedule.assert_not_called()
        dial = SimpleNamespace(_mapped=False, animations_enabled=True, _active=True,
            _closed=False, _frame=None, _schedule=Mock())
        CountdownDial._ensure_frame(dial)
        dial._schedule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
