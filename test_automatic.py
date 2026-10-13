"""Automatic scheduling/filter/input boundaries with fake clocks and pixels."""
import copy
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from helper_config import (default_transfer_filters, migrate_profile, validate_interval,
                           validate_transfer_filters)
from input_activity import ActivityState, HELPER_INPUT_TAG, UserActivityInterrupted
from runtime import Runner, FOCUS_RETRY_SECONDS, FOCUS_WAITING
from windows import FocusUnavailable
from test_runtime import RuntimeTestFixture, cell, frame


class EndTest(InterruptedError):
    pass


class AutomaticDesktop:
    def __init__(self, active=True, idle=0):
        self.is_active, self.idle = active, idle
        self.begin_calls, self.end_calls = [], []
        self.token = object()

    def active(self, handle):
        return self.is_active

    def is_alive(self, handle):
        return True

    def idle_seconds(self):
        return self.idle

    def stop_pressed(self):
        return False

    def begin_background_cycle(self, handle):
        self.begin_calls.append(handle)
        if self.idle < 600:
            return None
        self.is_active = True
        return self.token

    def end_background_cycle(self, handle, token):
        self.end_calls.append((handle, token))
        self.is_active = False
        return True


class AutomaticSchedulerTests(unittest.TestCase):
    def runner(self, desktop):
        result = Runner.__new__(Runner)
        result.desktop, result.handle = desktop, 5
        result.profile = {"supervised_ok": True}
        result.stop = threading.Event()
        result.inv = SimpleNamespace(count=40)
        result.log = Mock()
        result.state_callback = Mock()
        result._run_inventory_cycle = Mock(return_value=False)
        result.handle_resolver = None
        self.now, self.waits = 0.0, []
        def wait(seconds):
            self.waits.append(seconds)
            self.now += seconds
            if self.waits.count(1800.0) == 1:
                raise EndTest()
        result.wait = wait
        return result

    def run_monitor(self, runner):
        with patch("runtime.time.monotonic", side_effect=lambda: self.now):
            with self.assertRaises(EndTest):
                runner.monitor(1800)

    def test_explicit_start_cycle_is_immediate_and_next_cycle_uses_interval(self):
        desktop = AutomaticDesktop(active=True)
        runner = self.runner(desktop)
        runner._run_inventory_cycle.side_effect = lambda **_: self.assertEqual(self.now, 0) or False
        self.run_monitor(runner)
        self.assertEqual(self.waits, [1800.0])
        self.assertEqual(desktop.begin_calls, [])
        self.assertEqual(desktop.end_calls, [])
        deadlines = [call.args[0]["next_check_at"] for call in runner.state_callback.call_args_list]
        self.assertEqual(deadlines, [1800.0])

    def test_user_input_during_activation_skips_cycle_and_keeps_explicit_timer(self):
        desktop = AutomaticDesktop(active=False, idle=600)
        runner = self.runner(desktop)
        desktop.begin_background_cycle = Mock(side_effect=UserActivityInterrupted("user input"))
        self.run_monitor(runner)
        runner._run_inventory_cycle.assert_not_called()
        self.assertEqual(self.waits, [1800.0])
        self.assertEqual(desktop.end_calls, [])  # begin cleans any partial activation.
        runner.log.assert_called_once_with("user input")

    def test_recent_activity_waits_for_manual_game_return_without_activation(self):
        desktop = AutomaticDesktop(active=False, idle=599.9)
        runner = self.runner(desktop)
        base_wait = runner.wait
        def wait(seconds):
            base_wait(seconds)
            if self.waits.count(.3) == 3:
                desktop.is_active = True
        runner.wait = wait
        self.run_monitor(runner)
        self.assertEqual(desktop.begin_calls, [])
        runner._run_inventory_cycle.assert_called_once_with()
        self.assertEqual(self.waits, [.3, .3, .3, 1800.0])

    def test_focus_refusals_retry_shortly_then_run_one_cycle_with_one_cleanup(self):
        desktop = AutomaticDesktop(active=False, idle=600)
        runner = self.runner(desktop)
        acquire = desktop.begin_background_cycle
        def begin(handle):
            if len(desktop.begin_calls) < 2:
                desktop.begin_calls.append(handle)
                raise FocusUnavailable(diagnostics={"foreground_return": False})
            return acquire(handle)
        desktop.begin_background_cycle = begin
        with patch("runtime.logging.info") as journal:
            self.run_monitor(runner)
        self.assertEqual(self.waits, [FOCUS_RETRY_SECONDS, FOCUS_RETRY_SECONDS, 1.0, 1800.0])
        self.assertEqual(desktop.begin_calls, [5, 5, 5])
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])
        runner._run_inventory_cycle.assert_called_once_with()
        states = [call.args[0] for call in runner.state_callback.call_args_list]
        self.assertEqual(states[:2], [
            {"state": "paused", "message": FOCUS_WAITING, "next_check_at": 2.0},
            {"state": "paused", "message": FOCUS_WAITING, "next_check_at": 4.0}])
        self.assertEqual(states[-1]["next_check_at"], 1805.0)
        self.assertEqual(journal.call_count, 2)
        self.assertIn(FOCUS_WAITING, [call.args[0] for call in runner.log.call_args_list])
        self.assertFalse(runner.stop.is_set())

    def test_external_game_return_after_refusal_needs_no_second_activation(self):
        desktop = AutomaticDesktop(active=False, idle=600)
        runner = self.runner(desktop)
        desktop.begin_background_cycle = Mock(side_effect=FocusUnavailable())
        base_wait = runner.wait
        def wait(seconds):
            base_wait(seconds)
            if seconds == FOCUS_RETRY_SECONDS:
                desktop.is_active, desktop.idle = True, 0
        runner.wait = wait
        self.run_monitor(runner)
        desktop.begin_background_cycle.assert_called_once_with(5)
        runner._run_inventory_cycle.assert_called_once_with()
        self.assertEqual(self.waits, [FOCUS_RETRY_SECONDS, 1800.0])
        self.assertEqual(desktop.end_calls, [])

    def test_new_user_input_after_refusal_prevents_automatic_focus_retry(self):
        desktop = AutomaticDesktop(active=False, idle=600)
        runner = self.runner(desktop)
        desktop.begin_background_cycle = Mock(side_effect=FocusUnavailable())
        base_wait = runner.wait
        def wait(seconds):
            base_wait(seconds)
            if seconds == FOCUS_RETRY_SECONDS:
                desktop.idle = 0
            elif seconds == .3:
                runner.stop.set()
                runner.check_stop()
        runner.wait = wait
        with self.assertRaises(InterruptedError):
            runner.monitor(1800)
        desktop.begin_background_cycle.assert_called_once_with(5)
        runner._run_inventory_cycle.assert_not_called()
        self.assertEqual(self.waits, [FOCUS_RETRY_SECONDS, .3])
        self.assertEqual(desktop.end_calls, [])

    def test_f8_during_focus_retry_wait_stops_without_any_cycle_or_cleanup_token(self):
        desktop = AutomaticDesktop(active=False, idle=600)
        runner = self.runner(desktop)
        desktop.begin_background_cycle = Mock(side_effect=FocusUnavailable())
        def wait(seconds):
            self.assertEqual(seconds, FOCUS_RETRY_SECONDS)
            runner.stop.set()
            runner.check_stop()
        runner.wait = wait
        with self.assertRaises(InterruptedError):
            runner.monitor(1800)
        desktop.begin_background_cycle.assert_called_once_with(5)
        runner._run_inventory_cycle.assert_not_called()
        self.assertEqual(desktop.end_calls, [])

    def test_ordinary_activation_error_and_issued_input_are_never_focus_retries(self):
        issued = FocusUnavailable()
        issued.input_issued = True
        for failure in (RuntimeError("Unknown activation failure"), issued):
            with self.subTest(failure=failure):
                desktop = AutomaticDesktop(active=False, idle=600)
                runner = self.runner(desktop)
                desktop.begin_background_cycle = Mock(side_effect=failure)
                with self.assertRaises(type(failure)) as caught:
                    runner.monitor(1800)
                self.assertIs(caught.exception, failure)
                self.assertEqual(self.waits, [])
                runner._run_inventory_cycle.assert_not_called()
                self.assertEqual(desktop.end_calls, [])

    def test_focus_exception_inside_transfer_cycle_is_terminal_and_not_retried(self):
        desktop = AutomaticDesktop(active=False, idle=600)
        runner = self.runner(desktop)
        failure = FocusUnavailable()
        runner._run_inventory_cycle.side_effect = failure
        with self.assertRaises(FocusUnavailable) as caught:
            runner.monitor(1800)
        self.assertIs(caught.exception, failure)
        runner._run_inventory_cycle.assert_called_once_with()
        self.assertEqual(self.waits, [1.0])
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])

    def test_600_second_idle_allows_one_acquisition_cycle_and_cleanup(self):
        desktop = AutomaticDesktop(active=False, idle=600)
        runner = self.runner(desktop)
        runner._run_inventory_cycle.return_value = None
        self.run_monitor(runner)
        self.assertEqual(desktop.begin_calls, [5])
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])
        runner._run_inventory_cycle.assert_called_once_with()
        self.assertEqual(self.waits, [1.0, 1800.0])

    def test_input_during_background_render_cancels_cycle_but_keeps_scheduler(self):
        desktop = AutomaticDesktop(active=False, idle=600)
        runner = self.runner(desktop)
        base_wait = runner.wait
        def wait(seconds):
            if seconds == 1:
                raise UserActivityInterrupted("User input")
            base_wait(seconds)
        runner.wait = wait
        self.run_monitor(runner)
        runner._run_inventory_cycle.assert_not_called()
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])
        self.assertFalse(runner.stop.is_set())
        self.assertEqual(self.waits, [1800.0])

    def test_f8_during_background_render_stops_and_returns_token(self):
        desktop = AutomaticDesktop(active=False, idle=600)
        runner = self.runner(desktop)
        base_wait = runner.wait
        def wait(seconds):
            if seconds == 1:
                runner.stop.set()
                runner.check_stop()
            base_wait(seconds)
        runner.wait = wait
        with self.assertRaises(InterruptedError):
            runner.monitor(1800)
        self.assertTrue(runner.stop.is_set())
        runner._run_inventory_cycle.assert_not_called()
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])

    def test_f8_before_first_cycle_does_not_activate_or_transfer(self):
        desktop=AutomaticDesktop(active=False,idle=1000)
        runner=self.runner(desktop)
        runner.stop.set()
        with self.assertRaises(InterruptedError):
            runner.monitor(1800)
        self.assertEqual(self.waits,[])
        self.assertEqual(desktop.begin_calls,[])
        runner._run_inventory_cycle.assert_not_called()


class FilterTests(RuntimeTestFixture, unittest.TestCase):
    def test_historical_tier_one_selection_is_disabled_without_enabling_other_tiers(self):
        filters = default_transfer_filters()
        for value in filters.values():
            value["enabled"] = False
        filters["1"]["enabled"] = True
        self.profile["transfer_filters"] = filters
        runner, desktop = self.runner([frame([cell(3), cell(1)], [cell(), cell()])])
        candidate = runner.preview()[3]
        self.assertIsNone(candidate)
        self.assertFalse(runner.filters["1"]["enabled"])
        self.assertFalse(any(rule["enabled"] for rule in runner.filters.values()))
        self.assertEqual(desktop.inputs, [])

    def test_historical_tier_one_and_two_can_never_receive_transfer_input(self):
        self.profile["transfer_filters"] = {"1": {"enabled": True}, "2": {"enabled": True}}
        runner, desktop = self.runner([frame([cell(1), cell(2)], [cell(), cell()])])
        self.assertFalse(runner.once(sort=False))
        self.assertFalse(runner.filters["1"]["enabled"])
        self.assertFalse(runner.filters["2"]["enabled"])
        self.assertEqual(desktop.inputs, [])

    def test_selected_tier_accepts_equipment_and_gems_without_level_features(self):
        for equipment in (False,True):
            with self.subTest(equipment=equipment):
                self.profile["transfer_filters"]={"3":{"enabled":True}}
                image=frame([cell(2),cell(3,equipment=equipment)],[cell(),cell()])
                runner,desktop=self.runner([image])
                self.assertFalse(hasattr(runner,"level_reader"))
                self.assertFalse(hasattr(runner,"gem_matcher"))
                self.assertEqual(runner.preview()[3].source_index,1)
                self.assertEqual(desktop.inputs,[])

    def test_old_level_restriction_is_removed_without_changing_selected_tiers(self):
        self.profile["transfer_filters"]={"3":{"enabled":True,"min_level":999,"max_level":999}}
        image=frame([cell(3,equipment=False),cell(4)],[cell(),cell()])
        runner,desktop=self.runner([image])
        self.assertEqual(runner.preview()[3].tier,3)
        self.assertEqual(runner.profile["transfer_filters"]["3"],{"enabled":True})
        self.assertFalse(runner.profile["transfer_filters"]["4"]["enabled"])
        self.assertEqual(desktop.inputs,[])

    def test_empty_selection_and_unknown_tier_never_select_an_item(self):
        for filters,items in (({},[cell(3),cell(4)]),
                              ({"3":{"enabled":True}},[cell(7),cell(2)])):
            with self.subTest(filters=filters):
                self.profile["transfer_filters"]=filters
                runner,desktop=self.runner([frame(items,[cell(),cell()])])
                self.assertIsNone(runner.preview()[3])
                self.assertEqual(desktop.inputs,[])


class ConfigAndActivityTests(unittest.TestCase):
    def test_migration_preserves_calibration_and_user_filters_independently(self):
        profile = {"interval": 30, "include_gems": False, "supervised_ok": True,
                   "inventory": {"rect": [1, 2, 3, 4]}}
        original = copy.deepcopy(profile)
        result = migrate_profile(profile)
        self.assertEqual(result["interval"], 60)
        self.assertTrue(result["include_gems"])
        self.assertFalse(result["supervised_ok"])
        self.assertEqual(result["inventory"], profile["inventory"])
        result["inventory"]["rect"][0] = 900
        self.assertEqual(profile, original)
        self.assertEqual([t for t, r in result["transfer_filters"].items() if r["enabled"]], ["3", "4", "5", "6"])

    def test_interval_limits_reject_invalid_before_any_input(self):
        for value in (60, 1800, 3600, 86400, "60", "1800,5"):
            self.assertGreaterEqual(validate_interval(value), 60)
        for value in (True, None, "nan", "inf", 59.9, 86401):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_interval(value)

    def test_minute_option_does_not_change_default_or_saved_long_intervals(self):
        self.assertEqual(migrate_profile({})["interval"], 1800)
        for interval in (60, 900, 1800, 2700, 14400):
            with self.subTest(interval=interval):
                profile = {"interval": interval}
                self.assertEqual(migrate_profile(profile)["interval"], interval)
                self.assertEqual(profile, {"interval": interval})

    def test_filters_reject_invalid_tier_or_selection_and_drop_historical_level_fields(self):
        for value in ({"7":{"enabled":True}}, {"3":{"enabled":1}},
                      {"3":{}},{"3":{"enabled":True,"unexpected":0}}):
            with self.subTest(value=value),self.assertRaises(ValueError):
                validate_transfer_filters(value)
        selected=validate_transfer_filters({"3":{"enabled":True,"min_level":50,"max_level":20}})
        self.assertEqual(selected["3"],{"enabled":True})
        self.assertTrue(all(set(rule)=={"enabled"} for rule in selected.values()))

    def test_removing_effective_range_invalidates_proof_only_once(self):
        for enabled,low,high,invalidated in ((True,20,None,True),(True,None,50,True),
                                            (False,20,50,False),(True,None,None,False)):
            with self.subTest(enabled=enabled,low=low,high=high):
                profile={"supervised_ok":True,"include_gems":True,"interval":3600,
                         "transfer_filters":{"4":{"enabled":enabled,"min_level":low,"max_level":high}}}
                original=copy.deepcopy(profile)
                migrated=migrate_profile(profile)
                self.assertEqual(migrated["supervised_ok"],not invalidated)
                self.assertEqual(migrated["transfer_filters"]["4"],{"enabled":enabled})
                self.assertEqual(profile,original)
                migrated["supervised_ok"]=True
                self.assertTrue(migrate_profile(migrated)["supervised_ok"])

    def test_activity_tracks_all_other_input_but_never_own_tag(self):
        now = [1000.0]
        state = ActivityState(600, clock=lambda: now[0])
        self.assertEqual(state.idle_seconds(), 600)
        state.record(HELPER_INPUT_TAG)
        self.assertEqual(state.snapshot(), (0, 400.0))
        for other_tag in (0, 99, HELPER_INPUT_TAG + 1):
            now[0] += 1
            state.record(other_tag)
            self.assertEqual(state.idle_seconds(), 0)
        self.assertEqual(state.snapshot()[0], 3)



if __name__ == "__main__":
    unittest.main()
