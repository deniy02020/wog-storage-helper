"""Safe recognition retries are bounded independently of diagnostic wording.

All cycle outcomes, activity, window replacement and token receipts here are
supplied fakes. No Windows capture, activation, mouse or game input is issued.
"""
import unittest
from unittest.mock import Mock, patch

from inventory_walk import InventoryWalkResult
from runtime import (LAYOUT_RETRY_SECONDS, LAYOUT_WAITING, PREFLIGHT_WAITING,
                     LayoutChangedDuringOperation, SafeLayoutChange, SafePreflightChange)
from test_runtime import RuntimeTestFixture
from test_runtime_lifecycle import BackgroundDesktop, EndMonitor, LifecycleDesktop
from windows import WindowGeometryChanged


class RuntimeRetryBudgetTests(RuntimeTestFixture, unittest.TestCase):
    def monitor_runner(self, errors, *, desktop=None):
        runner, fake = self.runner([])
        runner.desktop = desktop or fake
        runner.log = Mock()
        runner.state_callback = Mock()
        runner._run_inventory_cycle = Mock(side_effect=errors)
        return runner, runner.desktop

    def schedule(self, runner, *, limit):
        waits, tick = [], [1000.]
        def wait(seconds):
            waits.append(seconds)
            tick[0] += seconds
            if len(waits) == limit:
                raise EndMonitor()
            runner.check_stop()
        runner.wait = wait
        return waits, tick

    def states(self, runner):
        return [call.args[0] for call in runner.state_callback.call_args_list]

    def assert_no_completion(self, runner):
        self.assertFalse(any("Проверка закончена" in state["message"] for state in self.states(runner)))
        self.assertFalse(any("Перенесён" in call.args[0] for call in runner.log.call_args_list))

    def test_unchanged_failure_has_only_two_quick_waits_then_preserves_exhaustion(self):
        runner, desktop = self.monitor_runner(SafeLayoutChange("same occluded row lattice"))
        waits, tick = self.schedule(runner, limit=5)

        with patch("runtime.time.monotonic", side_effect=lambda: tick[0]), self.assertRaises(EndMonitor):
            runner.monitor(1800)

        self.assertEqual(waits, [LAYOUT_RETRY_SECONDS, LAYOUT_RETRY_SECONDS, 1800, 1800, 1800])
        self.assertEqual(runner._run_inventory_cycle.call_count, 5)
        states = self.states(runner)
        self.assertTrue(all(state["state"] == "waiting" and state["message"] == LAYOUT_WAITING
                            for state in states))
        self.assertEqual([state["next_check_at"] for state in states],
                         [1002., 1004., 2804., 4604., 6404.])
        self.assert_no_completion(runner)
        self.assertEqual(desktop.inputs, [])

    def test_alternating_safe_error_classes_and_texts_share_one_retry_budget(self):
        errors = [SafeLayoutChange("rows occluded"), SafePreflightChange("new loot"),
                  SafeLayoutChange("columns occluded"), SafePreflightChange("gem animation")]
        runner, desktop = self.monitor_runner(errors)
        waits, tick = self.schedule(runner, limit=4)

        with patch("runtime.time.monotonic", side_effect=lambda: tick[0]), self.assertRaises(EndMonitor):
            runner.monitor(3600)

        self.assertEqual(waits, [2., 2., 3600, 3600])
        self.assertEqual([state["message"] for state in self.states(runner)],
                         [LAYOUT_WAITING, PREFLIGHT_WAITING, LAYOUT_WAITING, PREFLIGHT_WAITING])
        self.assert_no_completion(runner)
        self.assertEqual(desktop.inputs, [])

    def test_healthy_result_resets_budget_for_a_later_failure_streak(self):
        for healthy_outcome in ("complete", "pending"):
            with self.subTest(healthy_outcome=healthy_outcome):
                errors = [SafeLayoutChange("A"), SafePreflightChange("B"), SafeLayoutChange("C"),
                          InventoryWalkResult(healthy_outcome, 1, 2, 1),
                          SafePreflightChange("D"), SafeLayoutChange("E"), SafePreflightChange("F")]
                runner, desktop = self.monitor_runner(errors)
                waits, tick = self.schedule(runner, limit=7)

                with patch("runtime.time.monotonic", side_effect=lambda: tick[0]), self.assertRaises(EndMonitor):
                    runner.monitor(1800)

                self.assertEqual(waits, [2., 2., 1800, 1800, 2., 2., 1800])
                self.assertEqual(runner._run_inventory_cycle.call_count, 7)
                states = self.states(runner)
                self.assertTrue(all("Проверка закончена" not in state["message"]
                                    for index, state in enumerate(states) if index != 3))
                self.assertEqual(desktop.inputs, [])

    def test_replacement_game_window_gets_a_new_budget_after_old_window_exhaustion(self):
        desktop = LifecycleDesktop(active=True)
        runner, _ = self.monitor_runner(SafeLayoutChange("same missing rows"), desktop=desktop)
        runner.handle_resolver = Mock(return_value=9)
        waits, tick = [], [1000.]
        replaced = False
        def wait(seconds):
            nonlocal replaced
            waits.append(seconds)
            tick[0] += seconds
            if seconds == 1800 and not replaced:
                desktop.alive = False
                desktop.is_active = False
                replaced = True
            elif seconds == .3:
                self.assertEqual(runner.handle, 9)
                desktop.is_active = True
            elif len(waits) == 7:
                raise EndMonitor()
            runner.check_stop()
        runner.wait = wait

        with patch("runtime.time.monotonic", side_effect=lambda: tick[0]), self.assertRaises(EndMonitor):
            runner.monitor(1800)

        self.assertEqual(waits, [2., 2., 1800, .3, 2., 2., 1800])
        runner.handle_resolver.assert_called_once_with()
        self.assertEqual(runner.handle, 9)
        self.assertEqual(runner._run_inventory_cycle.call_count, 6)
        self.assert_no_completion(runner)

    def test_background_tokens_are_restored_before_short_and_exhausted_interval_waits(self):
        desktop = BackgroundDesktop(idle=True)
        runner, _ = self.monitor_runner(SafeLayoutChange("bad visible lattice"), desktop=desktop)
        waits, scheduled, tick = [], [], [1000.]
        def wait(seconds):
            waits.append(seconds)
            tick[0] += seconds
            if seconds != 1.:
                scheduled.append(seconds)
                self.assertEqual(len(desktop.end_calls), len(scheduled))
                self.assertEqual(len(desktop.begin_calls), len(scheduled))
                self.assertFalse(desktop.is_active)
                self.assertEqual(desktop.end_calls[-1], (5, desktop.token))
                if len(scheduled) == 4:
                    raise EndMonitor()
            runner.check_stop()
        runner.wait = wait

        with patch("runtime.time.monotonic", side_effect=lambda: tick[0]), self.assertRaises(EndMonitor):
            runner.monitor(1800)

        self.assertEqual(scheduled, [2., 2., 1800, 1800])
        self.assertEqual(waits, [1., 2., 1., 2., 1., 1800, 1., 1800])
        self.assertEqual(desktop.begin_calls, [5] * 4)
        self.assertEqual(desktop.end_calls, [(5, desktop.token)] * 4)
        self.assert_no_completion(runner)

    def test_f8_interrupts_the_first_exhausted_interval_without_another_cycle(self):
        runner, desktop = self.monitor_runner(SafePreflightChange("continually updating bag"))
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if seconds == 1800:
                runner.stop.set()
            runner.check_stop()
        runner.wait = wait

        with self.assertRaises(InterruptedError):
            runner.monitor(1800)

        self.assertEqual(waits, [2., 2., 1800])
        self.assertEqual(runner._run_inventory_cycle.call_count, 3)
        self.assertTrue(runner.stop.is_set())
        self.assertEqual(desktop.inputs, [])

    def test_error_after_accepted_item_input_never_enters_safe_retry_budget(self):
        errors = (WindowGeometryChanged("accepted item input; geometry changed", input_issued=True),
                  LayoutChangedDuringOperation("accepted item readback layout unavailable"),
                  RuntimeError("accepted item transfer was not verified"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                runner, desktop = self.monitor_runner(error)
                runner._transfer_input_issued = True
                runner.wait = Mock(side_effect=AssertionError("no retry or interval after uncertain input"))

                with self.assertRaises(type(error)) as raised:
                    runner.monitor(1800)

                self.assertIs(raised.exception, error)
                runner._run_inventory_cycle.assert_called_once_with()
                runner.wait.assert_not_called()
                self.assertEqual(self.states(runner), [])
                self.assertEqual(desktop.inputs, [])


if __name__ == "__main__":
    unittest.main()
