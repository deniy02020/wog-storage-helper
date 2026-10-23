"""Explicit launch/focus checks without constructing Tk or touching Windows."""
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app
from runtime import Runner
from test_windows import FakeUser32, WindowTestBoundary
from windows import FocusUnavailable


class FakeFocusUser32(FakeUser32):
    def __init__(self):
        super().__init__([self.CHILD])
        self.exists = True
        self.visible = True
        self.minimized = False
        self.foreground = self.OVERLAY
        self.allow_activation = True
        self.activation_return = True
        self.activation_calls = []
        self.restore_calls = []
        self.maximized = False
        self.show_activates = False
        self.show_return = True
        self.on_show = lambda _command: None

    def IsWindow(self, handle):
        return handle == self.GAME and self.exists

    def IsWindowVisible(self, handle):
        return handle == self.GAME and self.visible

    def IsIconic(self, handle):
        return self.minimized

    def ShowWindow(self, handle, command):
        self.restore_calls.append((handle, command))
        if command == 9:
            self.minimized = self.maximized = False
        elif command == 6:
            self.minimized = True
        elif command != 5:
            raise AssertionError(f"Unexpected ShowWindow command: {command}")
        if self.show_activates and command in (5, 9):
            self.foreground = handle
        self.on_show(command)
        return self.show_return

    def SetForegroundWindow(self, handle):
        self.activation_calls.append(handle)
        if self.allow_activation:
            self.foreground = handle
        return self.activation_return


class FakeThread:
    created = 0

    def __init__(self, *, target, daemon):
        type(self).created += 1
        self.target = target

    def start(self):
        self.target()


class ExplicitStartFocusTests(WindowTestBoundary, unittest.TestCase):
    def focus_desktop(self):
        desktop = self.desktop([FakeUser32.CHILD])
        desktop.u = FakeFocusUser32()
        return desktop

    def test_dead_selected_window_prevents_activation_and_mouse_input(self):
        desktop = self.focus_desktop()
        desktop.u.exists = False
        with self.assertRaises(RuntimeError):
            desktop.activate_for_start(FakeUser32.GAME)
        self.assertEqual(desktop.u.activation_calls, [])
        self.assertEqual(desktop.u.button_events, [])

    def test_invisible_window_prevents_activation_and_mouse_input(self):
        desktop = self.focus_desktop()
        desktop.u.visible = False
        with self.assertRaises(RuntimeError):
            desktop.activate_for_start(FakeUser32.GAME)
        self.assertEqual(desktop.u.activation_calls, [])
        self.assertEqual(desktop.u.button_events, [])

    def test_windows_refusing_focus_prevents_mouse_input_without_retry(self):
        desktop = self.focus_desktop()
        desktop.u.allow_activation = False
        desktop.u.activation_return = False
        with self.assertRaises(FocusUnavailable) as caught:
            desktop.activate_for_start(FakeUser32.GAME)
        self.assertIs(caught.exception.input_issued, False)
        self.assertEqual(desktop.u.restore_calls, [(FakeUser32.GAME, 5)])
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        self.assertEqual(desktop.u.button_events, [])

    def test_normal_and_maximized_show_preserves_geometry_and_never_uses_restore(self):
        for maximized in (False, True):
            with self.subTest(maximized=maximized):
                desktop = self.focus_desktop()
                desktop.u.maximized = maximized
                initial_box = list(desktop.u.box)
                desktop.activate_for_start(FakeUser32.GAME)
                self.assertEqual(desktop.u.restore_calls, [(FakeUser32.GAME, 5)])
                self.assertEqual(desktop.u.box, initial_box)
                self.assertEqual(desktop.u.maximized, maximized)
                self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
                self.assertEqual(desktop.u.button_events, [])

    def test_show_activation_is_verified_even_if_setforeground_returns_false(self):
        desktop = self.focus_desktop()
        desktop.u.show_activates = True
        desktop.u.show_return = False  # ShowWindow returns the previous visibility.
        desktop.u.allow_activation = False
        desktop.u.activation_return = False
        desktop.activate_for_start(FakeUser32.GAME)
        self.assertTrue(desktop.active(FakeUser32.GAME))
        self.assertEqual(desktop.u.restore_calls, [(FakeUser32.GAME, 5)])
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        self.assertEqual(desktop.u.button_events, [])

    def test_f8_during_show_cancels_before_setforeground_without_input(self):
        desktop = self.focus_desktop()
        desktop.u.on_show = lambda _command: setattr(desktop.u, "stop", True)
        with self.assertRaises(InterruptedError):
            desktop.activate_for_start(FakeUser32.GAME)
        self.assertEqual(desktop.u.activation_calls, [])
        self.assertEqual(desktop.u.button_events, [])

    def test_denied_explicit_minimized_start_restores_only_prior_minimized_state(self):
        desktop = self.focus_desktop()
        desktop.u.minimized = True
        desktop.u.allow_activation = False
        with self.assertRaises(FocusUnavailable):
            desktop.activate_for_start(FakeUser32.GAME)
        self.assertTrue(desktop.u.minimized)
        self.assertEqual(desktop.u.restore_calls, [(FakeUser32.GAME, 9), (FakeUser32.GAME, 6)])
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        self.assertEqual(desktop.u.button_events, [])

    def test_asynchronous_foreground_change_is_awaited_without_activation_retry(self):
        desktop = self.focus_desktop()
        desktop.u.allow_activation = False
        def delayed_foreground():
            if self.clock.now >= 0.04:
                desktop.u.foreground = FakeUser32.GAME
            return desktop.u.foreground
        # Additional read-only checks must not advance an asynchronous fake.
        desktop.u.GetForegroundWindow = Mock(side_effect=delayed_foreground)
        desktop.activate_for_start(FakeUser32.GAME)
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        self.assertGreaterEqual(self.clock.now, 0.04)
        self.assertEqual(desktop.u.button_events, [])

    def test_f8_during_foreground_wait_cancels_without_activation_retry(self):
        desktop = self.focus_desktop()
        desktop.u.allow_activation = False

        def pending_foreground():
            if desktop.u.activation_calls:
                desktop.u.stop = True
            return FakeUser32.OVERLAY

        desktop.u.GetForegroundWindow = Mock(side_effect=pending_foreground)
        with self.assertRaises(InterruptedError):
            desktop.activate_for_start(FakeUser32.GAME)
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        self.assertEqual(desktop.u.button_events, [])

    def test_success_requires_actual_foreground_even_when_windows_returns_true(self):
        desktop = self.focus_desktop()
        desktop.u.allow_activation = False
        with self.assertRaises(RuntimeError):
            desktop.activate_for_start(FakeUser32.GAME)
        self.assertEqual(desktop.u.button_events, [])

    def test_already_foreground_window_can_succeed_with_false_api_return(self):
        desktop = self.focus_desktop()
        desktop.u.foreground = FakeUser32.GAME
        desktop.u.activation_return = False
        desktop.activate_for_start(FakeUser32.GAME)
        self.assertTrue(desktop.active(FakeUser32.GAME))
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        self.assertEqual(desktop.u.button_events, [])

    def test_explicit_start_restores_minimized_game_then_activates_once(self):
        desktop = self.focus_desktop()
        desktop.u.minimized = True
        desktop.activate_for_start(FakeUser32.GAME)
        self.assertEqual(desktop.u.restore_calls, [(FakeUser32.GAME, 9)])
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        self.assertTrue(desktop.active(FakeUser32.GAME))
        self.assertEqual(desktop.u.button_events, [])

    def test_f8_prevents_explicit_activation_and_all_mouse_input(self):
        desktop = self.focus_desktop()
        desktop.u.stop = True
        with self.assertRaises((RuntimeError, InterruptedError)):
            desktop.activate_for_start(FakeUser32.GAME)
        self.assertEqual(desktop.u.activation_calls, [])
        self.assertEqual(desktop.u.cursor_moves, [])
        self.assertEqual(desktop.u.button_events, [])

    def dummy_app(self, desktop):
        runner = SimpleNamespace(wait=Mock(), once=Mock(return_value=True), monitor=Mock(), state=Mock(),
                                 enable_connection_monitor=Mock(), close_connection_monitor=Mock())
        controls = [Mock(), Mock()]
        state = SimpleNamespace(
            interval=SimpleNamespace(get=lambda: "1800"),
            profile={"supervised_ok": True}, save=Mock(),
            make_runner=lambda: runner, desktop=desktop, handle=FakeUser32.GAME,
            cancel=threading.Event(), log=Mock(), actions=controls,
            window_choice=Mock(), iconify=Mock(), deiconify=Mock(),
            events=queue.Queue(), worker=None,
        )
        state.prepare_game_window = lambda **kwargs: app.App.prepare_game_window(state, **kwargs)
        return state, runner

    def test_failed_focus_never_starts_worker_and_restores_helper_controls(self):
        for failure in ("dead", "invisible", "refused"):
            with self.subTest(failure=failure):
                desktop = self.focus_desktop()
                if failure == "dead":
                    desktop.u.exists = False
                elif failure == "invisible":
                    desktop.u.visible = False
                else:
                    desktop.u.allow_activation = False
                state, runner = self.dummy_app(desktop)
                FakeThread.created = 0
                with patch.object(app.threading, "Thread", FakeThread):
                    with self.assertRaises(RuntimeError):
                        app.App.start(state, False)
                self.assertEqual(FakeThread.created, 0)
                runner.once.assert_not_called()
                runner.monitor.assert_not_called()
                self.assertEqual(desktop.u.button_events, [])
                state.deiconify.assert_called_once()
                for control in state.actions:
                    control.configure.assert_called_with(state="normal")
                state.window_choice.configure.assert_called_with(state="readonly")

    def test_explicit_single_start_activates_once_before_running_transfer(self):
        desktop = self.focus_desktop()
        state, runner = self.dummy_app(desktop)
        runner.once.side_effect = lambda: desktop.active(FakeUser32.GAME)
        with patch.object(app.threading, "Thread", FakeThread):
            app.App.start(state, False)
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        runner.once.assert_called_once()
        runner.monitor.assert_not_called()
        self.assertIn(("verified", True), list(state.events.queue))

    def test_f8_during_explicit_preparation_is_sticky_before_worker_creation(self):
        for continuous in (False, True):
            with self.subTest(continuous=continuous):
                desktop = self.focus_desktop()
                desktop.u.stop = True
                state, runner = self.dummy_app(desktop)
                FakeThread.created = 0
                with patch.object(app.threading, "Thread", FakeThread):
                    app.App.start(state, continuous)
                self.assertTrue(state.cancel.is_set())
                self.assertEqual(FakeThread.created, 0)
                self.assertIsNone(state.worker)
                self.assertEqual(desktop.u.activation_calls, [])
                self.assertEqual(desktop.u.button_events, [])
                runner.once.assert_not_called()
                runner.monitor.assert_not_called()
                events = list(state.events.queue)
                self.assertEqual([kind for kind, _ in events], ["log", "finished"])
                self.assertNotIn("verified", [kind for kind, _ in events])
                for control in state.actions:
                    control.configure.assert_called_with(state="normal")
                state.window_choice.configure.assert_called_with(state="readonly")

    def test_f8_edge_consumed_during_foreground_wait_still_cancels_start(self):
        desktop = self.focus_desktop()
        desktop.u.allow_activation = False
        pressed = iter((False, False, False, False, True, False))
        desktop.stop_pressed = lambda: next(pressed, False)
        state, runner = self.dummy_app(desktop)
        FakeThread.created = 0
        with patch.object(app.threading, "Thread", FakeThread):
            app.App.start(state, False)
        self.assertTrue(state.cancel.is_set())
        self.assertEqual(FakeThread.created, 0)
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        self.assertEqual(desktop.u.button_events, [])
        runner.once.assert_not_called()

    def test_explicit_start_activates_game_before_minimizing_helper(self):
        desktop = self.focus_desktop()
        state, runner = self.dummy_app(desktop)
        original_activation = desktop.u.SetForegroundWindow

        def activate(handle):
            state.iconify.assert_not_called()
            return original_activation(handle)

        desktop.u.SetForegroundWindow = activate
        with patch.object(app.threading, "Thread", FakeThread):
            app.App.start(state, False)
        state.iconify.assert_called_once()
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])

    def test_explicit_start_without_single_proof_activates_once_then_runs_monitor(self):
        desktop = self.focus_desktop()
        state, runner = self.dummy_app(desktop)
        state.profile["supervised_ok"] = False
        with patch.object(app.threading, "Thread", FakeThread):
            app.App.start(state, True)
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        state.iconify.assert_not_called()
        state.deiconify.assert_not_called()
        runner.once.assert_not_called()
        runner.monitor.assert_called_once_with(1800.0)
        self.assertFalse(state.profile["supervised_ok"])

    def test_private_prepare_defaults_to_hide_but_continuous_option_does_not(self):
        for hide, expected in ((True, True), (False, False)):
            with self.subTest(hide_helper=hide):
                desktop = self.focus_desktop()
                state, _ = self.dummy_app(desktop)
                if hide:
                    app.App.prepare_game_window(state)
                else:
                    app.App.prepare_game_window(state, hide_helper=False)
                self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
                self.assertEqual(state.iconify.called, expected)
                state.deiconify.assert_not_called()

    def test_visible_continuous_prepare_failure_does_not_restore_or_minimize_helper(self):
        desktop = self.focus_desktop()
        desktop.u.allow_activation = False
        state, _ = self.dummy_app(desktop)
        with self.assertRaises(RuntimeError):
            app.App.prepare_game_window(state, hide_helper=False)
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        state.iconify.assert_not_called()
        state.deiconify.assert_not_called()

    def test_actual_continuous_prepare_focus_refusal_starts_monitor_without_hide_or_reactivation(self):
        desktop = self.focus_desktop()
        desktop.u.allow_activation = False
        state, runner = self.dummy_app(desktop)
        with patch.object(app.threading, "Thread", FakeThread):
            app.App.start(state, True)
        self.assertEqual(desktop.u.activation_calls, [FakeUser32.GAME])
        state.iconify.assert_not_called()
        state.deiconify.assert_not_called()
        runner.monitor.assert_called_once_with(1800.0)
        runner.once.assert_not_called()
        self.assertEqual(desktop.u.button_events, [])
        self.assertFalse(state.cancel.is_set())
        self.assertEqual(list(state.events.queue), [("finished", None)])
        self.assertEqual(state.log.call_args.args[0], app.FOCUS_WAITING)
        for control in state.actions:
            control.configure.assert_called_once_with(state="disabled")


class EndMonitor(InterruptedError):
    pass


class PauseOnlyDesktop:
    """Any attempt to activate/capture/click during AFK pause fails the test."""
    def __init__(self, states):
        self.states = iter(states)
        self.active_calls = 0

    def active(self, handle):
        self.active_calls += 1
        return next(self.states)

    def stop_pressed(self):
        return False

    def is_alive(self, handle):
        return True

    def idle_seconds(self):
        return 599.0

    def check_background_cycle(self):
        pass

    def __getattr__(self, name):
        raise AssertionError(f"Unexpected desktop operation while paused: {name}")


class AfkFocusPauseTests(unittest.TestCase):
    def runner(self, states):
        runner = Runner.__new__(Runner)
        runner.desktop = PauseOnlyDesktop(states)
        runner.handle = FakeUser32.GAME
        runner.profile = {"supervised_ok": True}
        runner.stop = threading.Event()
        runner.log = Mock()
        runner._run_inventory_cycle = Mock(return_value=False)
        runner.inv = SimpleNamespace(count=1)
        return runner

    def test_afk_lost_focus_stays_paused_without_recapturing_focus(self):
        runner = self.runner([False, False, False])
        waits = []

        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                raise EndMonitor()

        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        self.assertEqual(waits, [0.3, 0.3, 0.3])
        runner._run_inventory_cycle.assert_not_called()
        runner.log.assert_called_once_with("Пауза: ожидаю возвращение в игру или 10 минут без ввода.")

    def test_afk_resumes_only_after_foreground_returns_externally(self):
        runner = self.runner([False, False, True])
        waits = []

        def wait(seconds):
            waits.append(seconds)
            if seconds == 1800 and len(waits) > 1:
                raise EndMonitor()

        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        self.assertEqual(waits, [0.3, 0.3, 1800.0])
        runner._run_inventory_cycle.assert_called_once()
        self.assertEqual(runner.desktop.active_calls, 3)
        self.assertEqual([call.args[0] for call in runner.log.call_args_list],
                         ["Пауза: ожидаю возвращение в игру или 10 минут без ввода.",
                          "Окно игры активно: продолжаю."])


if __name__ == "__main__":
    unittest.main()
