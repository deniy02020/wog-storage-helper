"""Panel opener receipts cross the real monitor boundary without OS input.

Synthetic images provide exact observed headers/buttons. The injected desktop
records clicks only; no native game capture, activation or input is performed.
"""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from auto_layout import LayoutError, GridEvidenceError
from input_activity import UserActivityInterrupted
from panels import locate_unique
from runtime import Runner, LayoutChangedDuringOperation, SafeLayoutChange, GRID_WAITING
from test_panels import PanelFixture, PanelDesktop
from test_runtime_lifecycle import EndMonitor
from windows import WindowGeometryChanged, CursorPositionChanged, ForegroundWindowChanged


class PanelInputBoundaryTests(PanelFixture, unittest.TestCase):
    def runner(self, frames):
        desktop = PanelDesktop(frames)
        desktop.active = lambda _handle: True
        runner = Runner(desktop, 7, self.profile, self.directory)
        runner.canonical_profile["automatic_layout"] = True
        runner.log = Mock()
        runner.state_callback = Mock()
        controller = runner.panel_controller

        def headers(image):
            matches = [locate_unique(image, sample) for sample in controller.headers]
            return [SimpleNamespace(rect=rect) if rect is not None else None for rect in matches]

        # These tests concern the coordinator's accepted-input boundary, not
        # multiscale fitting. Exact synthetic RGB proof remains unique/fresh.
        controller.layout = SimpleNamespace(locate_headers=headers,
            resolve=Mock(return_value=copy.deepcopy(self.profile)), validate_current=Mock())
        controller._parking_models = lambda: ()
        controller.locate_control = lambda image, sample, max_error: locate_unique(
            image, sample, max_rmse=max_error)
        runner.wait = lambda _seconds: runner.check_stop()
        runner._run_inventory_cycle_impl = Mock(side_effect=lambda:
            runner._guarded_operation(runner.prepare_panels))
        return runner, desktop, controller

    @staticmethod
    def clicks(desktop):
        return [action for action in desktop.inputs if action[0] == "click"]

    def test_unconfirmed_opener_is_terminal_in_monitor_before_any_two_second_retry(self):
        closed = self.image(storage=False)
        runner, desktop, controller = self.runner([closed] * 4)
        waits = []
        runner.wait = lambda seconds: waits.append(seconds)
        with self.assertRaises(LayoutChangedDuringOperation) as caught:
            runner.monitor(3600)
        self.assertNotIsInstance(caught.exception, SafeLayoutChange)
        self.assertEqual(len(self.clicks(desktop)), 1)
        self.assertTrue(controller.opening_input_pending)
        self.assertTrue(runner._transfer_input_issued)
        runner._run_inventory_cycle_impl.assert_called_once_with()
        self.assertEqual(waits, [.2, .6])
        self.assertFalse(any(call.args[0]["state"] == "waiting"
                             for call in runner.state_callback.call_args_list))

    def test_confirmed_menu_character_and_storage_each_open_once(self):
        closed = self.image(inventory=False, storage=False)
        menu = self.image(inventory=False, storage=False, menu=True)
        inventory = self.image(storage=False)
        runner, desktop, controller = self.runner(
            [closed, closed, menu, menu, inventory, inventory, self.image()])
        runner._guarded_operation(runner.prepare_panels)
        self.assertEqual([action[2] for action in self.clicks(desktop)],
                         [(176, 120), (87, 75), (77, 95)])
        self.assertFalse(controller.opening_input_pending)
        self.assertFalse(runner._transfer_input_issued)

    def test_ambiguous_next_menu_control_does_not_confirm_or_replay_accepted_opener(self):
        closed = self.image(inventory=False, storage=False)
        ambiguous = self.image(inventory=False, storage=False, menu=True)
        ambiguous.paste(self.samples["character"], (125, 65))
        runner, desktop, controller = self.runner([closed, closed, ambiguous])
        waits = []
        runner.wait = lambda seconds: waits.append(seconds)
        with self.assertRaisesRegex(RuntimeError, "неоднозначно"):
            runner.monitor(3600)
        self.assertEqual([action[2] for action in self.clicks(desktop)], [(176, 120)])
        self.assertTrue(controller.opening_input_pending)
        self.assertTrue(runner._transfer_input_issued)
        self.assertEqual(waits, [.6])
        runner._run_inventory_cycle_impl.assert_called_once_with()

    def test_confirmed_open_then_independent_grid_occlusion_reacquires_without_reopening(self):
        closed, opened = self.image(storage=False), self.image()
        runner, desktop, controller = self.runner([closed] * 3 + [opened] * 3)
        controller.layout.resolve.side_effect = [GridEvidenceError("Independent grid occlusion"),
                                                 copy.deepcopy(self.profile)]
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if seconds == 3600:
                raise EndMonitor()
            runner.check_stop()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(3600)
        self.assertEqual(waits, [.2, .6, 2., .2, 3600])
        self.assertEqual(runner._run_inventory_cycle_impl.call_count, 2)
        self.assertEqual(len(self.clicks(desktop)), 1)
        self.assertFalse(controller.opening_input_pending)
        self.assertFalse(runner._transfer_input_issued)
        waits_displayed = [call.args[0]["message"] for call in runner.state_callback.call_args_list
                           if call.args[0]["state"] == "waiting"]
        self.assertEqual(waits_displayed, [GRID_WAITING, "Проверка закончена. Ожидаю следующий цикл."])

    def test_typed_native_rejection_before_input_can_enter_safe_retry(self):
        closed = self.image(storage=False)
        runner, desktop, controller = self.runner([closed] * 3)
        desktop.click = Mock(side_effect=WindowGeometryChanged("Before down", input_issued=False))
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if seconds == 2.:
                raise EndMonitor()
            runner.check_stop()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(3600)
        self.assertEqual(waits, [.2, 2.])
        self.assertEqual(self.clicks(desktop), [])
        self.assertFalse(controller.opening_input_pending)
        self.assertFalse(runner._transfer_input_issued)

    def test_accepted_or_unknown_native_error_retains_boundary_and_never_retries(self):
        for error in (WindowGeometryChanged("After down", input_issued=True),
                      CursorPositionChanged("After down", input_issued=True),
                      ForegroundWindowChanged("After down", input_issued=True),
                      UserActivityInterrupted("After down", input_issued=True),
                      RuntimeError("Unknown native result")):
            with self.subTest(error=type(error).__name__):
                closed = self.image(storage=False)
                runner, desktop, controller = self.runner([closed] * 3)
                original_click = desktop.click
                def click(*args):
                    original_click(*args)
                    raise error
                desktop.click = click
                waits = []
                runner.wait = lambda seconds: waits.append(seconds)
                expected = (LayoutChangedDuringOperation if isinstance(error, WindowGeometryChanged)
                            else type(error))
                with self.assertRaises(expected) as caught:
                    runner.monitor(3600)
                self.assertNotIsInstance(caught.exception, SafeLayoutChange)
                self.assertEqual(len(self.clicks(desktop)), 1)
                self.assertTrue(controller.opening_input_pending)
                self.assertTrue(runner._transfer_input_issued)
                self.assertEqual(waits, [.2])
                runner._run_inventory_cycle_impl.assert_called_once_with()

    def test_pre_input_native_takeover_clears_only_its_rejected_opener_boundary(self):
        closed = self.image(storage=False)
        runner, desktop, controller = self.runner([closed] * 3)
        desktop.click = Mock(side_effect=UserActivityInterrupted("Before down", input_issued=False))
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if seconds == 3600:
                raise EndMonitor()
            runner.check_stop()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(3600)
        self.assertEqual(waits, [.2, 3600])
        self.assertEqual(self.clicks(desktop), [])
        self.assertFalse(controller.opening_input_pending)
        self.assertFalse(runner._transfer_input_issued)
        messages = [call.args[0]["message"] for call in runner.state_callback.call_args_list]
        self.assertFalse(any("Проверка закончена" in message for message in messages))

    def test_rejected_transfer_takeover_preserves_any_earlier_unconfirmed_receipt(self):
        for receipt in ("runner", "panel", "none"):
            with self.subTest(receipt=receipt):
                runner, _, controller = self.runner([])
                runner._transfer_input_issued = receipt == "runner"
                controller._opening_input_pending = receipt == "panel"
                callback = Mock(side_effect=UserActivityInterrupted("Before down", input_issued=False))
                with self.assertRaises(UserActivityInterrupted):
                    runner._issue_transfer(callback)
                self.assertEqual(runner._transfer_input_issued, receipt != "none")

    def test_guard_entry_cannot_erase_or_act_over_an_earlier_pending_receipt(self):
        for receipt in ("runner", "panel"):
            with self.subTest(receipt=receipt):
                runner, desktop, controller = self.runner([])
                runner._transfer_input_issued = receipt == "runner"
                controller._opening_input_pending = receipt == "panel"
                operation = Mock()
                with self.assertRaisesRegex(RuntimeError, "Предыдущий ввод не подтверждён"):
                    runner._guarded_operation(operation)
                operation.assert_not_called()
                self.assertTrue(runner._transfer_input_issued)
                self.assertEqual(desktop.inputs, [])

    def test_idle_cursor_rejection_has_bounded_reacquisition_then_interval_cooldown(self):
        closed = self.image(storage=False)
        runner, desktop, controller = self.runner([closed] * 9)
        desktop.idle_seconds = lambda: 600.
        desktop.click = Mock(side_effect=CursorPositionChanged("Before down", input_issued=False))
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if seconds == 3600:
                raise EndMonitor()
            runner.check_stop()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(3600)
        self.assertEqual(waits, [.2, .3, .2, .3, .2, 3600])
        self.assertEqual(runner._run_inventory_cycle_impl.call_count, 3)
        self.assertEqual(desktop.click.call_count, 3)
        self.assertEqual(self.clicks(desktop), [])
        self.assertFalse(controller.opening_input_pending)
        self.assertFalse(runner._transfer_input_issued)
        states = [call.args[0] for call in runner.state_callback.call_args_list]
        self.assertEqual([state["state"] for state in states if state["state"] != "pending"],
                         ["paused", "paused", "waiting"])
        self.assertFalse(any("Проверка закончена" in state["message"] for state in states))

    def test_recent_activity_cursor_hold_has_no_new_cycle_until_foreground_return(self):
        closed = self.image(storage=False)
        runner, desktop, _ = self.runner([closed] * 3)
        desktop.idle_seconds = lambda: 0.
        desktop.click = Mock(side_effect=CursorPositionChanged("Before down", input_issued=False))
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if waits.count(.3) == 3:
                raise EndMonitor()
            runner.check_stop()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(3600)
        self.assertEqual(waits, [.2, .3, .3, .3])
        runner._run_inventory_cycle_impl.assert_called_once_with()
        self.assertEqual(desktop.click.call_count, 1)
        self.assertEqual(self.clicks(desktop), [])

    def test_cursor_hold_resumes_after_observed_foreground_leave_and_return(self):
        closed, opened = self.image(storage=False), self.image()
        runner, desktop, controller = self.runner([closed] * 6 + [opened])
        active = [True]
        desktop.active = lambda _handle: active[0]
        desktop.idle_seconds = lambda: 0.
        original_click = desktop.click
        attempts = []
        def click(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise CursorPositionChanged("Before down", input_issued=False)
            original_click(*args)
        desktop.click = click
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if seconds == .3:
                active[0] = waits.count(.3) > 1
            if seconds == 3600:
                raise EndMonitor()
            runner.check_stop()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(3600)
        self.assertEqual(waits, [.2, .3, .3, .2, .6, 3600])
        self.assertEqual(runner._run_inventory_cycle_impl.call_count, 2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.clicks(desktop)), 1)
        self.assertFalse(controller.opening_input_pending)

    def test_foreground_rejection_waits_for_return_then_reacquires_fresh_panels(self):
        closed, opened = self.image(storage=False), self.image()
        runner, desktop, controller = self.runner([closed] * 6 + [opened])
        active = [True]
        desktop.active = lambda _handle: active[0]
        desktop.idle_seconds = lambda: 0.
        original_click = desktop.click
        attempts = []
        def click(*args):
            attempts.append(args)
            if len(attempts) == 1:
                active[0] = False
                raise ForegroundWindowChanged("Before down", input_issued=False)
            original_click(*args)
        desktop.click = click
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if seconds == .3:
                active[0] = True
            if seconds == 3600:
                raise EndMonitor()
            runner.check_stop()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(3600)
        self.assertEqual(waits, [.2, .3, .2, .6, 3600])
        self.assertEqual(runner._run_inventory_cycle_impl.call_count, 2)
        self.assertEqual(len(self.clicks(desktop)), 1)
        self.assertFalse(controller.opening_input_pending)

    def test_background_access_rejections_are_bounded_without_two_second_spin(self):
        runner, desktop, _ = self.runner([])
        desktop.active = lambda _handle: False
        desktop.idle_seconds = lambda: 600.
        desktop.begin_background_cycle = Mock(side_effect=CursorPositionChanged("Before down"))
        desktop.end_background_cycle = Mock()
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if seconds == 3600:
                raise EndMonitor()
            runner.check_stop()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(3600)
        self.assertEqual(waits, [.3, .3, 3600])
        self.assertEqual(desktop.begin_background_cycle.call_count, 3)
        desktop.end_background_cycle.assert_not_called()
        runner._run_inventory_cycle_impl.assert_not_called()
        self.assertEqual(desktop.inputs, [])

    def test_stop_or_takeover_interrupts_opener_readback_before_confirmation(self):
        for takeover in (False, True):
            with self.subTest(takeover=takeover):
                closed = self.image(storage=False)
                runner, desktop, controller = self.runner([closed] * 3)
                def wait(seconds):
                    if seconds == .6:
                        if takeover:
                            raise UserActivityInterrupted("User takeover")
                        runner.stop.set()
                    runner.check_stop()
                runner.wait = wait
                expected = UserActivityInterrupted if takeover else InterruptedError
                with self.assertRaises(expected):
                    runner.monitor(3600)
                self.assertEqual(len(self.clicks(desktop)), 1)
                self.assertTrue(controller.opening_input_pending)
                self.assertTrue(runner._transfer_input_issued)
                self.assertEqual(desktop.frames, [])
                runner._run_inventory_cycle_impl.assert_called_once_with()
                self.assertFalse(any(call.args[0]["state"] == "waiting"
                                     for call in runner.state_callback.call_args_list))

    def test_native_or_runner_pending_receipt_makes_takeover_terminal_in_monitor(self):
        for receipt in ("native", "runner", "panel"):
            with self.subTest(receipt=receipt):
                runner, desktop, controller = self.runner([])
                error = UserActivityInterrupted("Takeover before readback")
                if receipt == "native":
                    error.input_issued = True
                elif receipt == "runner":
                    runner._transfer_input_issued = True
                else:
                    controller._opening_input_pending = True
                runner._run_inventory_cycle = Mock(side_effect=error)
                runner.wait = Mock(side_effect=AssertionError("Unproved input cannot schedule a retry"))
                with self.assertRaises(UserActivityInterrupted) as caught:
                    runner.monitor(3600)
                self.assertIs(caught.exception, error)
                runner._run_inventory_cycle.assert_called_once_with()
                runner.wait.assert_not_called()
                self.assertEqual(desktop.inputs, [])
                runner.state_callback.assert_not_called()

    def test_pre_input_takeover_preserves_timer_without_reporting_completed_check(self):
        runner, desktop, _ = self.runner([])
        runner._run_inventory_cycle = Mock(side_effect=UserActivityInterrupted("User takeover"))
        waits = []
        def wait(seconds):
            waits.append(seconds)
            raise EndMonitor()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(3600)
        self.assertEqual(waits, [3600])
        self.assertEqual(desktop.inputs, [])
        states = [call.args[0] for call in runner.state_callback.call_args_list]
        self.assertEqual(states[-1]["state"], "waiting")
        self.assertIn("прервана вводом пользователя", states[-1]["message"])
        self.assertFalse(any("Проверка закончена" in state["message"] for state in states))

    def test_accepted_background_activation_takeover_never_schedules_another_cycle(self):
        runner, desktop, _ = self.runner([])
        desktop.active = lambda _handle: False
        desktop.idle_seconds = lambda: 600.
        error = UserActivityInterrupted("Takeover after activation down")
        error.input_issued = True
        desktop.begin_background_cycle = Mock(side_effect=error)
        desktop.end_background_cycle = Mock()
        runner.wait = Mock(side_effect=AssertionError("Unproved activation cannot schedule a retry"))
        with self.assertRaises(UserActivityInterrupted) as caught:
            runner.monitor(3600)
        self.assertIs(caught.exception, error)
        desktop.begin_background_cycle.assert_called_once()
        desktop.end_background_cycle.assert_not_called()  # Failed begin owns native cleanup.
        runner._run_inventory_cycle_impl.assert_not_called()
        runner.wait.assert_not_called()
        self.assertEqual(desktop.inputs, [])


if __name__ == "__main__":
    unittest.main()
