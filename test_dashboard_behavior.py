"""Dashboard behavior contracts with fake widgets/events; no Tk or game input."""
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app
import dashboard
from i18n import tr, format_duration, set_locale
from helper_config import default_transfer_filters
from dashboard import ProductApp


class FakeControl:
    def __init__(self):
        self.options = {}
        self.packed = True

    def configure(self, **options):
        self.options.update(options)

    def pack(self, **options):
        self.packed = True

    def pack_forget(self):
        self.packed = False

    def winfo_manager(self):
        return "pack" if self.packed else ""


class DashboardBehaviorTests(unittest.TestCase):
    def setUp(self):
        set_locale("ru")
        self.state = SimpleNamespace(
            worker=SimpleNamespace(is_alive=Mock(return_value=False)),
            desktop=SimpleNamespace(stop_pressed=Mock(return_value=False)),
            cancel=threading.Event(), events=queue.Queue(),
            actions=[FakeControl(), FakeControl()], window_choice=FakeControl(),
            profile={"supervised_ok": False, "interval": 1800, "include_gems": True},
            save=Mock(), deiconify=Mock(), after=Mock(), poll=Mock(),
            _dashboard_ready=False, _verified_result=None, _panels_result=None,
            _runtime_state="idle", _runtime_message="", _next_check_at=None,
            _confirmed_count=0, _last_worker_message="", _journal=None,
            _profile_problem="", handle=123,
            primary=FakeControl(), language_selector=FakeControl(),
            interval_choice=FakeControl(), _main_actions=[], _activity_state="work",
            countdown_text=Mock(), activity_text=Mock(),
            count_text=Mock(), run_title=Mock(), run_detail=Mock(),
            status_orbit=FakeControl(), cycle_progress=FakeControl(), game_dot=Mock(),
            detail_label=FakeControl(), stats_label=FakeControl(), stats_text=Mock(),
            _advanced_open=False,
        )
        self.state.vault_motion = self.state.status_orbit
        self.state._main_actions = [self.state.interval_choice, self.state.language_selector]
        self.state.set_runtime_state = lambda *args: ProductApp.set_runtime_state(self.state, *args)
        self.state.log = lambda message: ProductApp.log(self.state, message)
        self.state._update_dashboard = lambda: ProductApp._update_dashboard(self.state)
        self.log_patch = patch.object(app.App, "log")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def poll(self, *events):
        for event in events:
            self.state.events.put(event)
        ProductApp.poll(self.state)

    def test_state_events_update_deadline_without_granting_transfer_proof(self):
        self.poll(("state", {"state": "waiting", "message": "Ожидание", "next_check_at": 130.0}))
        self.assertEqual(self.state._runtime_state, "waiting")
        self.assertEqual(self.state._next_check_at, 130.0)
        self.assertFalse(self.state.profile["supervised_ok"])
        self.state.save.assert_not_called()

    def test_verified_false_finishes_without_granting_proof(self):
        self.poll(("verified", False), ("finished", None))
        self.assertFalse(self.state.profile["supervised_ok"])
        self.state.save.assert_not_called()
        self.assertEqual(self.state._runtime_state, "finished")
        self.assertIn("пока нет", self.state._runtime_message)

    def test_verified_true_persists_proof_once_and_restores_controls(self):
        self.poll(("verified", True), ("finished", None))
        self.assertTrue(self.state.profile["supervised_ok"])
        self.state.save.assert_called_once_with(invalidate=False)
        self.assertEqual(self.state._runtime_state, "finished")
        for control in self.state.actions:
            self.assertEqual(control.options["state"], "normal")
        self.assertEqual(self.state.window_choice.options["state"], "readonly")
        self.state.deiconify.assert_called_once_with()

    def test_sticky_cancel_takes_precedence_over_prior_results(self):
        for event in (("verified", False), ("panels_ready", False), ("panels_ready", True)):
            with self.subTest(event=event):
                self.state.cancel.set()
                self.poll(event, ("finished", None))
                self.assertEqual(self.state._runtime_state, "stopped")
                self.assertIsNone(self.state._next_check_at)
                self.state.save.assert_not_called()

    def test_opening_panels_never_grants_transfer_proof(self):
        self.poll(("panels_ready", True), ("finished", None))
        self.assertFalse(self.state.profile["supervised_ok"])
        self.state.save.assert_not_called()
        self.assertEqual(self.state._runtime_state, "finished")

    def test_worker_failure_remains_error_with_original_message(self):
        self.poll(("log", "Кнопка не найдена."), ("panels_ready", False), ("finished", None))
        self.assertFalse(self.state.cancel.is_set())
        self.assertEqual(self.state._runtime_state, "error")
        self.assertEqual(self.state._runtime_message, "Кнопка не найдена.")
        self.state.save.assert_not_called()

    def test_confirmed_full_storage_stops_deadline_and_survives_finished_event(self):
        from storage_cycle import STORAGE_FULL_MESSAGE
        self.state._next_check_at = 99999
        self.poll(("state", {"state": "storage_full", "message": STORAGE_FULL_MESSAGE,
                             "next_check_at": 99999}), ("finished", None))
        self.assertEqual(self.state._runtime_state, "storage_full")
        self.assertIsNone(self.state._next_check_at)
        self.assertTrue(self.state.profile["storage_full"])
        self.state.save.assert_called_once_with(invalidate=False)
        self.state._update_dashboard()
        self.assertEqual(self.state.primary.options["text"], tr("storage_space_available"))
        self.state.run_title.set.assert_called_with(tr("storage_full_title"))
        self.assertTrue(self.state.detail_label.packed)
        self.state.after.assert_called_once()  # UI polling only, no worker restart.

    def test_space_freed_button_requires_explicit_user_action_to_restart(self):
        self.state._runtime_state = "storage_full"
        self.state.safe = Mock(side_effect=lambda callback: callback())
        self.state.start = Mock()
        self.state._update_dashboard()
        self.state.start.assert_not_called()
        ProductApp.toggle_run(self.state)
        self.state.start.assert_called_once_with(True)

    def test_counter_counts_only_confirmed_transfer_records_for_enabled_low_and_high_tiers(self):
        for tier in range(1, 7):
            ProductApp.log(self.state, f"Перенесён T{tier}: сумка 1 → хранилище 2.")
        for message in ("Кандидат: T6", "Перенесён T7: сумка 1 → хранилище 2.",
                        "Перенесён T3: сумка 1 → хранилище 2. Не подтверждён.",
                        "Проверка страниц завершена.", "Один перенос подтверждён."):
            ProductApp.log(self.state, message)
        self.assertEqual(self.state._confirmed_count, 6)
        self.state.save.assert_not_called()

    def test_busy_safety_gate_rejects_settings_commands_without_showing_dialog(self):
        self.state.worker.is_alive.return_value = True
        command = Mock()
        self.state._record_error = Mock()
        with patch.object(app.messagebox, "showerror") as dialog:
            ProductApp.safe(self.state, command)
        command.assert_not_called()
        self.state._record_error.assert_not_called()
        dialog.assert_not_called()

    def test_busy_primary_requests_stop_instead_of_starting_another_worker(self):
        self.state.worker.is_alive.return_value = True
        self.state.stop = self.state.cancel.set
        self.state.safe = Mock()
        ProductApp.toggle_run(self.state)
        self.assertTrue(self.state.cancel.is_set())
        self.assertEqual(self.state._runtime_state, "stopping")
        self.state.safe.assert_not_called()

    def test_launch_controls_obey_game_readiness_without_a_manual_proof_gate(self):
        for busy, handle, problem, proof, primary in (
            (False, 123, "", False, "normal"),
            (False, 123, "", True, "normal"),
            (False, None, "", True, "disabled"),
            (False, 123, "Некорректный профиль", True, "disabled"),
            (True, None, "Некорректный профиль", False, "normal"),
        ):
            with self.subTest(busy=busy, handle=handle, problem=problem, proof=proof):
                self.state.worker.is_alive.return_value = busy
                self.state.handle = handle
                self.state._profile_problem = problem
                self.state.profile["supervised_ok"] = proof
                ProductApp._update_dashboard(self.state)
                self.assertEqual(self.state.primary.options["state"], primary)
                self.assertEqual(self.state.language_selector.options["state"],
                                 "disabled" if busy else "readonly")
                if busy:
                    self.assertEqual(self.state.primary.options["text"], "Остановить")
                else:
                    self.assertTrue(self.state.primary.options["text"].startswith("Запустить"))

    def test_countdown_repaints_do_not_extend_monotonic_deadline(self):
        self.state.worker.is_alive.return_value = True
        ProductApp.set_runtime_state(self.state, "waiting", "Ожидание", 130.0)
        for now, remaining in ((100.0, 30), (101.3, 29), (131.0, 0)):
            with self.subTest(now=now), patch.object(dashboard.time, "monotonic", return_value=now):
                ProductApp._update_dashboard(self.state)
                self.assertEqual(self.state._next_check_at, 130.0)
                self.assertEqual(tr("interval.next", time=format_duration(remaining, countdown=True)),
                                 self.state.run_detail.set.call_args.args[0])
                self.assertGreaterEqual(self.state.cycle_progress.options["value"], 0)
                self.assertLessEqual(self.state.cycle_progress.options["value"], 100)
        ProductApp.set_runtime_state(self.state, "paused", "Пауза")
        self.assertIsNone(self.state._next_check_at)

    def test_grid_wait_explanation_stays_visible_beside_countdown_in_every_locale(self):
        from i18n import SUPPORTED_LOCALES
        from runtime import GRID_WAITING
        self.addCleanup(set_locale, "ru")
        self.state.worker.is_alive.return_value = True
        ProductApp.set_runtime_state(self.state, "waiting", GRID_WAITING, 130.)
        for locale in SUPPORTED_LOCALES:
            with self.subTest(locale=locale), \
                    patch.object(dashboard.time, "monotonic", return_value=100.):
                set_locale(locale)
                self.state.detail_label.packed = False
                ProductApp._update_dashboard(self.state)
                self.state.run_title.set.assert_called_with(tr("state.paused"))
                self.state.run_detail.set.assert_called_with(tr("state.auto_grid_waiting"))
                self.state.countdown_text.set.assert_called_with(
                    tr("interval.next", time=format_duration(30, countdown=True)))
                self.assertTrue(self.state.detail_label.packed)
                self.assertEqual(self.state._next_check_at, 130.)
        self.state.save.assert_not_called()

    def test_visual_refresh_never_activates_game_or_writes_profile(self):
        self.state.desktop.activate_for_start = Mock()
        self.state.desktop.click = Mock()
        self.state.desktop.capture = Mock()
        self.state.worker.is_alive.return_value = True
        self.state._dashboard_ready = True
        self.poll(("state", {"state": "waiting", "message": "Ожидание", "next_check_at": 130.0}))
        self.assertFalse(self.state.status_orbit.options["active"])
        self.poll(("state", {"state": "scanning", "message": "Проверка", "next_check_at": None}))
        self.assertTrue(self.state.status_orbit.options["active"])
        self.state._advanced_open = True
        ProductApp._update_dashboard(self.state)
        self.assertFalse(self.state.status_orbit.options["active"])
        self.assertIsNone(self.state._next_check_at)
        self.state.save.assert_not_called()
        self.state.desktop.activate_for_start.assert_not_called()
        self.state.desktop.click.assert_not_called()
        self.state.desktop.capture.assert_not_called()

    def test_panel_preparation_does_not_claim_items_are_being_checked(self):
        self.state.worker.is_alive.return_value = True
        ProductApp.set_runtime_state(self.state, "pending", "Проверяю и открываю панели игры.")
        ProductApp._update_dashboard(self.state)
        self.state.run_title.set.assert_called_with(tr("state.opening"))
        self.assertTrue(self.state.detail_label.packed)
        self.state.save.assert_not_called()

    def test_unfinished_retry_keeps_its_reason_beside_the_real_countdown(self):
        from runtime import FOCUS_WAITING, LAYOUT_WAITING, PREFLIGHT_WAITING, INVENTORY_PENDING
        from i18n import translate_message
        self.state.worker.is_alive.return_value = True
        for message in (FOCUS_WAITING, LAYOUT_WAITING, PREFLIGHT_WAITING, INVENTORY_PENDING,
                        "Фоновая проверка прервана вводом пользователя. Следующая проверка по таймеру."):
            with self.subTest(message=message), patch.object(dashboard.time, "monotonic", return_value=100):
                ProductApp.set_runtime_state(self.state, "waiting", message, 130.0)
                ProductApp._update_dashboard(self.state)
                self.state.run_title.set.assert_called_with(tr("state.paused"))
                self.state.run_detail.set.assert_called_with(translate_message(message))
                self.assertTrue(self.state.detail_label.packed)
                self.state.countdown_text.set.assert_called_with(
                    tr("interval.next", time=format_duration(30, countdown=True)))
                self.assertFalse(self.state.status_orbit.options["active"])
        self.state.save.assert_not_called()

    def test_successful_wait_remains_minimal_and_hides_old_failure_detail(self):
        self.state.worker.is_alive.return_value = True
        ProductApp.set_runtime_state(self.state, "paused", "Ожидаю доступа к игре.")
        ProductApp._update_dashboard(self.state)
        self.assertTrue(self.state.detail_label.packed)
        with patch.object(dashboard.time, "monotonic", return_value=100):
            ProductApp.set_runtime_state(self.state, "waiting", "Проверка закончена. Ожидаю следующий цикл.", 130.0)
            ProductApp._update_dashboard(self.state)
        self.assertFalse(self.state.detail_label.packed)
        self.state.run_title.set.assert_called_with(tr("state.waiting"))
        self.state.save.assert_not_called()

    def test_terminal_failure_points_to_journal_without_exposing_native_diagnostics(self):
        ProductApp.set_runtime_state(self.state, "error", "Курсор или положение окна изменились. Ввод отменён.")
        ProductApp._update_dashboard(self.state)
        self.state.run_detail.set.assert_called_with(tr("detail.see_journal"))
        self.assertTrue(self.state.detail_label.packed)
        self.assertIsNone(self.state._next_check_at)

    def test_busy_scene_hides_every_setting_and_stop_reveals_them(self):
        self.state.idle_scene, self.state.active_scene = FakeControl(), FakeControl()
        self.state.countdown_dial = FakeControl()
        self.state.worker.is_alive.return_value = True
        self.state._runtime_state, self.state._next_check_at = "waiting", 130.0
        with patch.object(dashboard.time, "monotonic", return_value=100):
            ProductApp._update_dashboard(self.state)
        self.assertFalse(self.state.idle_scene.options["revealed"])
        self.assertTrue(self.state.active_scene.options["revealed"])
        self.assertEqual(self.state.countdown_dial.options["seconds"], 30)
        self.assertEqual(self.state.primary.options["text"], tr("action.stop"))
        self.assertTrue(all(control.options["state"] == "disabled" for control in self.state._main_actions))
        self.state.worker.is_alive.return_value = False
        ProductApp._update_dashboard(self.state)
        self.assertTrue(self.state.idle_scene.options["revealed"])
        self.assertFalse(self.state.active_scene.options["revealed"])
        self.assertIsNone(self.state.countdown_dial.options["seconds"])
        self.assertEqual(self.state.primary.options["text"], tr("action.start"))
        self.state.save.assert_not_called()

    def test_interval_change_preserves_proof_and_gems_always_remain_included(self):
        with tempfile.TemporaryDirectory(prefix=".test-dashboard-contract-", dir=Path(__file__).parent) as directory:
            self.state.profile_file = Path(directory) / "settings.json"
            self.state.profile["supervised_ok"] = True
            self.state.mode = SimpleNamespace(get=lambda: "right")
            self.state.interval = SimpleNamespace(get=lambda: "1800,5", set=Mock())
            self.state.include_gems = SimpleNamespace(get=lambda: False, set=Mock())
            self.state.update_info = Mock()
            self.state.save = lambda invalidate=True: app.App.save(self.state, invalidate)
            ProductApp.save_interval(self.state)
            saved = json.loads(self.state.profile_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["interval"], 1800.5)
            self.assertTrue(saved["supervised_ok"])
            app.App.include_gems_changed(self.state)
            saved = json.loads(self.state.profile_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["interval"], 1800.5)
            self.assertTrue(saved["include_gems"])
            self.assertTrue(saved["supervised_ok"])

    def test_interval_presets_save_user_choice_without_resetting_proof_or_filters(self):
        with tempfile.TemporaryDirectory(prefix=".test-dashboard-presets-", dir=Path(__file__).parent) as directory:
            current = ["1800.5"]
            self.state.profile_file = Path(directory) / "settings.json"
            self.state.profile.update(supervised_ok=True, focus_mode="background_cycle")
            self.state.mode = SimpleNamespace(get=lambda: "right")
            self.state.interval = SimpleNamespace(get=lambda: current[0],
                                                 set=lambda value: current.__setitem__(0, value))
            self.state.update_info = Mock()
            self.state.save = lambda invalidate=True: app.App.save(self.state, invalidate)
            self.state.save_interval = lambda: ProductApp.save_interval(self.state)
            for seconds in (1800, 3600, 86400):
                with self.subTest(seconds=seconds):
                    ProductApp.choose_interval(self.state, seconds)
                    saved = json.loads(self.state.profile_file.read_text(encoding="utf-8"))
                    self.assertEqual(saved["interval"], seconds)
                    self.assertTrue(saved["supervised_ok"])
                    self.assertTrue(saved["include_gems"])
                    self.assertEqual(saved["focus_mode"], "background_cycle")


if __name__ == "__main__":
    unittest.main()
