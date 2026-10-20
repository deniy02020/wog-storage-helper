"""App launch timing with synchronous fake workers; no Tk or native input."""
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import app
from dashboard import ProductApp
from runtime import Runner, FOCUS_WAITING
from windows import FocusUnavailable


class SynchronousThread:
    created = 0

    def __init__(self, *, target, daemon):
        type(self).created += 1
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()

    def is_alive(self):
        return False


class AppTimerTests(unittest.TestCase):
    def setUp(self):
        SynchronousThread.created = 0
        self.runner = SimpleNamespace(state=Mock(), wait=Mock(), monitor=Mock(),
                                      enable_connection_monitor=Mock(), close_connection_monitor=Mock(),
                                      once=Mock(return_value=True), prepare_panels=Mock(),
                                      inspect_storage_pages=Mock(return_value=[]))
        self.timeline = Mock()
        for name in ("state", "wait", "monitor", "once"):
            self.timeline.attach_mock(getattr(self.runner, name), name)
        self.state = SimpleNamespace(
            interval=SimpleNamespace(get=lambda: "1800"),
            profile={"supervised_ok": True, "focus_mode": "background_cycle"}, _runtime_state="idle",
            save=Mock(), make_runner=Mock(return_value=self.runner),
            actions=[Mock(), Mock()], window_choice=Mock(),
            prepare_game_window=Mock(), iconify=Mock(), log=Mock(),
            events=queue.Queue(),
        )
        self.thread_patch = patch.object(app.threading, "Thread", SynchronousThread)
        self.time_patch = patch.object(app.time, "monotonic", return_value=1000.0)
        self.thread_patch.start()
        self.time_patch.start()
        self.addCleanup(self.thread_patch.stop)
        self.addCleanup(self.time_patch.stop)

    def events(self):
        result = []
        while not self.state.events.empty():
            result.append(self.state.events.get_nowait())
        return result

    def assert_product_finished_state(self, expected):
        # The worker consumed the F8 edge. The product must use the sticky
        # cancellation event, without relying on another keyboard observation.
        self.state.desktop = SimpleNamespace(stop_pressed=Mock(return_value=False))
        self.state._verified_result = None
        self.state._panels_result = None
        self.state._last_worker_message = ""
        self.state._dashboard_ready = False
        self.state.set_runtime_state = Mock()
        self.state.deiconify = Mock()
        self.state.after = Mock()
        self.state.poll = Mock()
        self.state.log.side_effect = lambda message: setattr(self.state, "_last_worker_message", message)
        ProductApp.poll(self.state)
        self.assertEqual(self.state.set_runtime_state.call_args.args[0], expected)
        self.state.desktop.stop_pressed.assert_not_called()
        self.state.deiconify.assert_called_once_with()

    def test_explicit_continuous_start_prepares_once_and_starts_after_render_delay(self):
        for interval in (60, 1800, 1800.5, 3600, 14400, 86400):
            with self.subTest(interval=interval):
                self.timeline.reset_mock()
                self.state.iconify.reset_mock()
                self.state.prepare_game_window.reset_mock()
                self.state.interval.get = lambda: str(interval)
                app.App.start(self.state, True)
                self.assertEqual(self.timeline.mock_calls, [call.wait(1.0), call.monitor(float(interval))])
                self.state.prepare_game_window.assert_called_once_with(hide_helper=False)
                self.state.iconify.assert_not_called()
                self.assertEqual(self.events(), [("finished", None)])
                self.assertEqual(self.state.profile["interval"], float(interval))

    def test_default_explicit_start_prepares_once_then_runs_immediate_cycle(self):
        self.state.profile.pop("focus_mode")
        app.App.start(self.state, True)
        self.state.prepare_game_window.assert_called_once_with(hide_helper=False)
        self.state.iconify.assert_not_called()
        self.assertEqual(self.timeline.mock_calls, [call.wait(1.0), call.monitor(1800.0)])
        self.assertEqual(self.events(), [("finished", None)])

    def test_continuous_focus_refusal_still_starts_scheduler_and_keeps_helper_visible(self):
        self.state.prepare_game_window.side_effect = FocusUnavailable(diagnostics={"foreground_return": False})
        with patch.object(app.logging, "info") as journal:
            app.App.start(self.state, True)
        self.state.prepare_game_window.assert_called_once_with(hide_helper=False)
        self.state.iconify.assert_not_called()
        self.assertEqual(self.timeline.mock_calls, [
            call.state("paused", FOCUS_WAITING), call.wait(1.0), call.monitor(1800.0)])
        self.assertEqual(SynchronousThread.created, 1)
        self.state.log.assert_called_once_with(FOCUS_WAITING)
        journal.assert_called_once()
        self.assertFalse(self.state.cancel.is_set())
        self.assertEqual(self.events(), [("finished", None)])
        for control in self.state.actions:
            control.configure.assert_called_once_with(state="disabled")
        self.state.window_choice.configure.assert_called_once_with(state="disabled")
        self.runner.once.assert_not_called()

    def test_private_single_focus_refusal_restores_controls_without_worker_or_verification(self):
        self.state.prepare_game_window.side_effect = FocusUnavailable()
        with self.assertRaises(FocusUnavailable):
            app.App.start(self.state, False)
        self.state.prepare_game_window.assert_called_once_with(hide_helper=True)
        self.assertEqual(SynchronousThread.created, 0)
        for control in self.state.actions:
            control.configure.assert_called_with(state="normal")
        self.state.window_choice.configure.assert_called_with(state="readonly")
        self.runner.wait.assert_not_called()
        self.runner.monitor.assert_not_called()
        self.runner.once.assert_not_called()
        self.assertEqual(self.events(), [])

    def test_focus_pending_scheduler_still_reports_f8_as_stopped_without_verification(self):
        self.state.prepare_game_window.side_effect = FocusUnavailable()
        self.runner.monitor.side_effect = InterruptedError("Остановлено (F8).")
        app.App.start(self.state, True)
        self.assertTrue(self.state.cancel.is_set())
        self.assertEqual(self.events(), [("log", "Остановлено (F8)."), ("finished", None)])
        self.runner.once.assert_not_called()

    def test_unknown_explicit_activation_failure_still_does_not_start_scheduler(self):
        self.state.prepare_game_window.side_effect = RuntimeError("Unknown activation failure")
        with self.assertRaisesRegex(RuntimeError, "Unknown activation failure"):
            app.App.start(self.state, True)
        self.assertEqual(SynchronousThread.created, 0)
        self.runner.monitor.assert_not_called()
        self.assertEqual(self.events(), [])

    def test_legacy_wait_metadata_does_not_delay_first_cycle_to_interval(self):
        self.state.profile["focus_mode"] = "wait"
        self.state.interval.get = lambda: "3600"
        app.App.start(self.state, True)
        self.state.prepare_game_window.assert_called_once_with(hide_helper=False)
        self.assertEqual(self.timeline.mock_calls, [call.wait(1.0), call.monitor(3600.0)])
        self.assertEqual(self.events(), [("finished", None)])

    def test_single_in_background_mode_still_prepares_game_waits_five_and_verifies(self):
        self.state.profile["supervised_ok"] = False
        app.App.start(self.state, False)
        self.state.prepare_game_window.assert_called_once_with(hide_helper=True)
        self.state.iconify.assert_not_called()
        self.assertEqual(self.timeline.mock_calls, [call.wait(5), call.once()])
        self.assertEqual(self.events(), [("verified", True), ("finished", None)])
        # Only the UI's later handling of the verification event can set proof.
        self.assertFalse(self.state.profile["supervised_ok"])

    def test_explicit_continuous_start_accepts_factory_profile_without_single_proof(self):
        self.state.profile["supervised_ok"] = False
        app.App.start(self.state, True)
        self.state.iconify.assert_not_called()
        self.state.prepare_game_window.assert_called_once_with(hide_helper=False)
        self.assertEqual(SynchronousThread.created, 1)
        self.assertEqual(self.timeline.mock_calls, [call.wait(1.0), call.monitor(1800.0)])
        self.assertEqual(self.events(), [("finished", None)])
        self.assertFalse(self.state.profile["supervised_ok"])

    def test_f8_during_initial_render_wait_stops_before_monitor_or_transfer(self):
        self.runner.wait.side_effect = InterruptedError("Остановлено (F8).")
        app.App.start(self.state, True)
        self.runner.wait.assert_called_once_with(1.0)
        self.runner.monitor.assert_not_called()
        self.runner.once.assert_not_called()
        self.state.prepare_game_window.assert_called_once_with(hide_helper=False)
        self.assertTrue(self.state.cancel.is_set())
        self.assertEqual(self.events(), [("log", "Остановлено (F8)."), ("finished", None)])

    def test_user_leaving_game_during_first_render_wait_cannot_be_reactivated_or_clicked(self):
        desktop = SimpleNamespace(is_active=False, stop_pressed=lambda: False,
                                  idle_seconds=lambda: 0, check_background_cycle=lambda: None,
                                  begin_background_cycle=Mock(), end_background_cycle=Mock())
        desktop.active = lambda handle: desktop.is_active
        engine = Runner.__new__(Runner)
        engine.desktop, engine.handle = desktop, 5
        engine.profile = {"supervised_ok": True}
        engine.inv = SimpleNamespace(count=40)
        engine.once, engine.log, engine.state_callback = Mock(), Mock(), Mock()
        engine.handle_resolver = None
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if seconds == 1.0:
                desktop.is_active = False  # User chose another application after Start.
            elif seconds == .3:
                raise InterruptedError("Остановлено (F8).")
            else:
                self.fail("No interval wait or input is due before the first safe cycle")
        engine.wait = wait
        def make_runner():
            engine.stop = self.state.cancel
            return engine
        self.state.make_runner = make_runner
        self.state.prepare_game_window.side_effect = lambda **kwargs: setattr(desktop, "is_active", True)
        app.App.start(self.state, True)
        self.state.prepare_game_window.assert_called_once_with(hide_helper=False)
        self.assertEqual(waits, [1.0, .3])
        engine.once.assert_not_called()
        desktop.begin_background_cycle.assert_not_called()
        desktop.end_background_cycle.assert_not_called()
        self.assertTrue(self.state.cancel.is_set())
        self.assertNotIn(("verified", True), self.events())

    def test_monitor_error_still_queues_finished_once(self):
        self.runner.monitor.side_effect = RuntimeError("Панель не найдена.")
        app.App.start(self.state, True)
        self.runner.wait.assert_called_once_with(1.0)
        self.assertFalse(self.state.cancel.is_set())
        self.assertEqual(self.events(), [("log", "Панель не найдена."), ("finished", None)])

    def test_worker_f8_during_single_or_monitor_marks_product_stopped_without_proof(self):
        for continuous, method in ((False, "once"), (True, "monitor")):
            with self.subTest(method=method):
                getattr(self.runner, method).side_effect = InterruptedError("Остановлено (F8).")
                app.App.start(self.state, continuous)
                self.assertTrue(self.state.cancel.is_set())
                self.assertEqual(list(self.state.events.queue), [
                    ("log", "Остановлено (F8)."), ("finished", None)])
                self.assert_product_finished_state("stopped")
                getattr(self.runner, method).side_effect = None
                self.state.deiconify.reset_mock()
                self.state.set_runtime_state.reset_mock()

    def test_panel_worker_f8_marks_product_stopped_at_each_stage(self):
        for inspect_pages, method in ((False, "wait"), (False, "prepare_panels"),
                                      (True, "inspect_storage_pages")):
            with self.subTest(method=method):
                getattr(self.runner, method).side_effect = InterruptedError("Остановлено (F8).")
                app.App.open_panels(self.state, inspect_pages)
                self.assertTrue(self.state.cancel.is_set())
                self.assertEqual(list(self.state.events.queue), [
                    ("log", "Остановлено (F8)."), ("panels_ready", False), ("finished", None)])
                self.assert_product_finished_state("stopped")
                getattr(self.runner, method).side_effect = None
                self.state.deiconify.reset_mock()
                self.state.set_runtime_state.reset_mock()

    def test_f8_before_panel_worker_is_reported_stopped_without_any_panel_action(self):
        self.state.prepare_game_window.side_effect = InterruptedError("Остановлено (F8).")
        app.App.open_panels(self.state)
        self.assertTrue(self.state.cancel.is_set())
        self.assertEqual(SynchronousThread.created, 0)
        self.runner.wait.assert_not_called()
        self.runner.prepare_panels.assert_not_called()
        self.runner.inspect_storage_pages.assert_not_called()
        self.assertEqual(list(self.state.events.queue), [
            ("log", "Остановлено (F8)."), ("finished", None)])
        self.state.worker = None
        self.assert_product_finished_state("stopped")

    def test_runtime_failure_keeps_product_error_and_never_emits_verification(self):
        for panels in (False, True):
            with self.subTest(panels=panels):
                method = self.runner.prepare_panels if panels else self.runner.once
                method.side_effect = RuntimeError("Панель не найдена.")
                if panels:
                    app.App.open_panels(self.state)
                else:
                    app.App.start(self.state, False)
                self.assertFalse(self.state.cancel.is_set())
                expected = [("log", "Панель не найдена.")]
                if panels:
                    expected.append(("panels_ready", False))
                expected.append(("finished", None))
                self.assertEqual(list(self.state.events.queue), expected)
                self.assert_product_finished_state("error")
                method.side_effect = None
                self.state.deiconify.reset_mock()
                self.state.set_runtime_state.reset_mock()

    def test_invalid_interval_cannot_minimize_activate_or_create_worker(self):
        for interval in ("59.9", "86401", "nan", "inf"):
            with self.subTest(interval=interval):
                self.state.interval.get = lambda: interval
                with self.assertRaisesRegex(ValueError, "Интервал"):
                    app.App.start(self.state, True)
                self.state.iconify.assert_not_called()
                self.state.prepare_game_window.assert_not_called()
                self.state.make_runner.assert_not_called()
                self.assertEqual(SynchronousThread.created, 0)


if __name__ == "__main__":
    unittest.main()
