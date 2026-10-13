"""Panel, page, interval and restart boundaries with fake desktops only."""
import copy
import threading
import unittest
from unittest.mock import Mock

from PIL import Image, ImageDraw

from runtime import Runner, SafeLayoutChange, LAYOUT_RETRY_SECONDS, LAYOUT_WAITING
from storage_cycle import StorageFull, STORAGE_FULL_MESSAGE
from test_panels import PanelFixture, PanelDesktop
from test_runtime import RuntimeTestFixture, cell, frame


class EndMonitor(InterruptedError):
    pass


class LifecycleDesktop:
    def __init__(self, alive=True, active=False):
        self.alive = alive
        self.is_active = active
        self.active_handles = []
        self.alive_handles = []

    def active(self, handle):
        self.active_handles.append(handle)
        return self.is_active

    def is_alive(self, handle):
        self.alive_handles.append(handle)
        return self.alive or handle == 9

    def stop_pressed(self):
        return False


class BackgroundDesktop(LifecycleDesktop):
    def __init__(self, *, idle=True, active=False, alive=True):
        super().__init__(alive=alive, active=active)
        self.idle = idle
        self.begin_calls = []
        self.end_calls = []
        self.token = object()
        self.restore_success = True

    def idle_seconds(self):
        return 600.0 if self.idle else 599.0

    def begin_background_cycle(self, handle):
        self.begin_calls.append(handle)
        if not self.idle:
            return None
        self.is_active = True
        return self.token

    def end_background_cycle(self, handle, token):
        self.end_calls.append((handle, token))
        self.is_active = False
        return self.restore_success


class RuntimeLifecycleTests(RuntimeTestFixture, unittest.TestCase):
    def test_pending_background_geometry_returns_token_before_short_retry(self):
        desktop = BackgroundDesktop(idle=True)
        runner = self.monitor_runner(desktop)
        runner.profile["supervised_ok"] = False
        runner._run_inventory_cycle = Mock(side_effect=SafeLayoutChange("Temporary overlay"))
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if seconds == LAYOUT_RETRY_SECONDS:
                self.assertEqual(desktop.end_calls, [(5, desktop.token)])
                raise EndMonitor()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(3600)
        self.assertEqual(waits, [1.0, LAYOUT_RETRY_SECONDS])
        self.assertEqual(desktop.begin_calls, [5])
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])
        runner._run_inventory_cycle.assert_called_once_with()
        state = runner.state_callback.call_args.args[0]
        self.assertEqual((state["state"], state["message"]), ("waiting", LAYOUT_WAITING))
        self.assertFalse(runner.profile["supervised_ok"])

    def test_full_page_switches_then_performs_one_verified_transfer(self):
        self.add_pages()
        before = self.page_frame(1, [cell(3), cell()], [cell(4), cell(5)])
        empty = self.page_frame(2, [cell(3), cell()], [cell(), cell()])
        after = self.page_frame(2, [cell(), cell()], [cell(3), cell()])
        # Page 1 gets its own fresh capacity scan; page 2 then needs a fresh
        # pre-click page-1 frame, followed by selection and transfer readback.
        runner, desktop = self.runner([before, before, before, empty, empty, after])
        self.assertTrue(runner.once())
        actions = [(action, args) for action, args in desktop.inputs if action != "move"]
        self.assertEqual(actions[0], ("click", (5, (40, 4), (96, 64))))
        self.assertEqual(actions[1], ("drag", (5, (12, 28), (60, 28), (96, 64))))
        self.assertEqual(len(actions), 2)

    def test_all_configured_pages_full_two_sweeps_stop_without_transfer(self):
        self.add_pages()
        before = self.page_frame(1, [cell(3), cell()], [cell(4), cell(5)])
        second = self.page_frame(2, [cell(3), cell()], [cell(4), cell(5)])
        # Fresh scan of already selected page 1, then pre/post selection for
        # page 2, page 1, page 2. No stale occupancy is reused between sweeps.
        runner, desktop = self.runner([before, before, before, second,
                                       second, before, before, second])
        with self.assertRaises(StorageFull) as caught:
            runner.once()
        self.assertEqual(str(caught.exception), STORAGE_FULL_MESSAGE)
        clicks = [args[1] for action, args in desktop.inputs if action == "click"]
        self.assertEqual(clicks, [(40, 4), (28, 4), (40, 4)])
        self.assertEqual(desktop.frames, [])
        self.assertFalse(any(action == "drag" for action, _ in desktop.inputs))

    def audit_runner(self, frames):
        runner, desktop = self.runner(frames)
        runner.sort_inventory = Mock(side_effect=AssertionError("audit must never sort"))
        runner.once = Mock(side_effect=AssertionError("audit must never transfer"))
        return runner, desktop

    def test_page_audit_counts_full_empty_and_uncertain_then_restores_original(self):
        self.add_pages()
        full = self.page_frame(1, [cell(3), cell()], [cell(4), cell(5)])
        uncertain = Image.new("RGB", (24, 24), (26, 29, 32))
        empty = self.page_frame(2, [cell(3), cell()], [cell(), uncertain])
        # prepare, initial original, page1 fresh, page2 fresh+after,
        # then original restore fresh+after. No sorting/transfer frames exist.
        runner, desktop = self.audit_runner([full, full, full, full, empty, empty, full])
        original = copy.deepcopy(runner.profile)
        self.assertEqual(runner.inspect_storage_pages(), [
            {"id": 1, "free": 0, "unknown": 0}, {"id": 2, "free": 1, "unknown": 1}])
        clicks = [args for action, args in desktop.inputs if action == "click"]
        self.assertEqual(clicks, [(5, (40, 4), (96, 64)), (5, (28, 4), (96, 64))])
        self.assertFalse(any(action == "drag" for action, _ in desktop.inputs))
        runner.once.assert_not_called()
        runner.sort_inventory.assert_not_called()
        self.assertEqual(runner.profile, original)

    def test_page_audit_can_start_on_second_page_and_restores_it(self):
        self.add_pages()
        first = self.page_frame(1, [cell(3), cell()], [cell(), cell()])
        second = self.page_frame(2, [cell(3), cell()], [cell(4), cell(5)])
        runner, desktop = self.audit_runner([second, second, second, first, first, second, second])
        self.assertEqual(runner.inspect_storage_pages(), [
            {"id": 1, "free": 2, "unknown": 0}, {"id": 2, "free": 0, "unknown": 0}])
        self.assertEqual([args[1] for action, args in desktop.inputs if action == "click"],
                         [(28, 4), (40, 4)])

    def test_page_audit_wrong_marker_stops_after_one_attempt_without_retry(self):
        self.add_pages()
        first = self.page_frame(1, [cell(3), cell()], [cell(), cell()])
        runner, desktop = self.audit_runner([first] * 6)
        with self.assertRaisesRegex(RuntimeError, "не подтвердилось"):
            runner.inspect_storage_pages()
        self.assertEqual(sum(action == "click" for action, _ in desktop.inputs), 1)
        runner.once.assert_not_called()

    def test_page_audit_unknown_target_marker_cancels_before_any_click(self):
        self.add_pages()
        first = self.page_frame(1, [cell(3), cell()], [cell(), cell()])
        changed = first.copy()
        ImageDraw.Draw(changed).rectangle((38, 0, 39, 7), fill="white")
        runner, desktop = self.audit_runner([first, first, first, changed])
        with self.assertRaisesRegex(RuntimeError, "не подтверждена"):
            runner.inspect_storage_pages()
        self.assertEqual(desktop.inputs, [])

    def test_page_audit_f8_skips_restore_and_all_further_input(self):
        self.add_pages()
        first = self.page_frame(1, [cell(3), cell()], [cell(), cell()])
        runner, desktop = self.audit_runner([first] * 4)
        def stop_after_click(seconds):
            runner.stop.set()
            runner.check_stop()
        runner.wait = stop_after_click
        with self.assertRaises(InterruptedError):
            runner.inspect_storage_pages()
        self.assertEqual([action for action, _ in desktop.inputs], ["click"])

    def test_page_audit_focus_change_cannot_restore_or_activate(self):
        self.add_pages()
        first = self.page_frame(1, [cell(3), cell()], [cell(), cell()])
        runner, desktop = self.audit_runner([first] * 4)
        def lose_focus_after_click(seconds):
            desktop.is_active = False
            raise RuntimeError("not foreground")
        runner.wait = lose_focus_after_click
        with self.assertRaisesRegex(RuntimeError, "foreground"):
            runner.inspect_storage_pages()
        self.assertEqual([action for action, _ in desktop.inputs], ["click"])

    def test_page_audit_restoration_must_verify_original_marker(self):
        self.add_pages()
        first = self.page_frame(1, [cell(3), cell()], [cell(), cell()])
        second = self.page_frame(2, [cell(3), cell()], [cell(), cell()])
        runner, desktop = self.audit_runner([first, first, first, first, second, second, second])
        with self.assertRaisesRegex(RuntimeError, "не подтвердилось"):
            runner.inspect_storage_pages()
        self.assertEqual(sum(action == "click" for action, _ in desktop.inputs), 2)

    def test_page_audit_invalid_or_duplicate_ids_never_start_panel_preparation(self):
        self.add_pages()
        runner, desktop = self.audit_runner([])
        runner.prepare_panels = Mock()
        for ids in ((1, "+"), (1, 1), (1, False), (1, 0)):
            with self.subTest(ids=ids):
                for page, id_ in zip(runner.profile["pages"], ids):
                    page["id"] = id_
                with self.assertRaisesRegex(ValueError, "номера"):
                    runner.inspect_storage_pages()
                runner.prepare_panels.assert_not_called()
        self.assertEqual(desktop.inputs, [])

    def add_pages(self):
        Image.new("RGB", (2, 8), (30, 230, 80)).save(self.directory / "page_active.png")
        Image.new("RGB", (2, 8), (100, 100, 100)).save(self.directory / "page_inactive.png")
        self.profile.update({"page_active_image": "page_active.png", "page_inactive_image": "page_inactive.png",
                             "pages": [{"id": 1, "button": [24, 0, 32, 8], "marker": [26, 0, 28, 8]},
                                       {"id": 2, "button": [36, 0, 44, 8], "marker": [38, 0, 40, 8]}]})

    def page_frame(self, selected, inventory, storage):
        image = frame(inventory, storage)
        draw = ImageDraw.Draw(image)
        for page in self.profile["pages"]:
            left, top, right, bottom = page["marker"]
            draw.rectangle((left, top, right - 1, bottom - 1),
                           fill=(30, 230, 80) if page["id"] == selected else (100, 100, 100))
        return image

    def monitor_runner(self, desktop):
        runner, _ = self.runner([])
        runner.profile["supervised_ok"] = True
        runner.desktop = desktop
        runner.log = Mock()
        runner.state_callback = Mock()
        return runner

    def test_monitor_runs_one_complete_inventory_cycle_per_interval_and_reports_deadline(self):
        runner = self.monitor_runner(LifecycleDesktop(active=True))
        runner._run_inventory_cycle = Mock(side_effect=[None, None])
        cycles = []
        def wait(seconds):
            if seconds == 1800:
                cycles.append(seconds)
                if len(cycles) == 2:
                    raise EndMonitor()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        self.assertEqual([call.kwargs for call in runner._run_inventory_cycle.call_args_list],
                         [{}, {}])
        waiting = [call.args[0] for call in runner.state_callback.call_args_list
                   if call.args[0]["state"] == "waiting"]
        self.assertEqual(len(waiting), 2)
        self.assertTrue(all(isinstance(event["next_check_at"], float) for event in waiting))

    def test_live_background_game_never_resolves_or_acts(self):
        runner = self.monitor_runner(LifecycleDesktop(alive=True))
        runner.handle_resolver = Mock(return_value=9)
        runner._run_inventory_cycle = Mock()
        runner.wait = Mock(side_effect=EndMonitor())
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        runner.handle_resolver.assert_not_called()
        runner._run_inventory_cycle.assert_not_called()
        self.assertEqual(runner.handle, 5)

    def test_dead_game_reacquires_only_then_waits_for_external_foreground(self):
        desktop = LifecycleDesktop(alive=False)
        runner = self.monitor_runner(desktop)
        runner.handle_resolver = Mock(return_value=9)
        runner._run_inventory_cycle = Mock(return_value=False)
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                desktop.is_active = True
            if seconds == 1800 and len(waits) > 1:
                raise EndMonitor()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        runner.handle_resolver.assert_called_once_with()
        self.assertEqual(runner.handle, 9)
        self.assertEqual(desktop.active_handles, [5, 9, 9])
        self.assertEqual(waits, [0.3, 0.3, 1800.0])
        runner._run_inventory_cycle.assert_called_once_with()

    def test_ambiguous_reacquisition_result_cannot_start_input(self):
        runner = self.monitor_runner(LifecycleDesktop(alive=False))
        runner.handle_resolver = Mock(return_value=[9, 10])
        runner._run_inventory_cycle = Mock()
        with self.assertRaisesRegex(RuntimeError, "однозначно"):
            runner.monitor(1800)
        runner._run_inventory_cycle.assert_not_called()
        self.assertEqual(runner.handle, 5)

    def test_f8_cancels_before_dead_game_resolution(self):
        runner = self.monitor_runner(LifecycleDesktop(alive=False))
        runner.stop = threading.Event()
        runner.stop.set()
        runner.handle_resolver = Mock(return_value=9)
        with self.assertRaises(InterruptedError):
            runner.monitor(1800)
        runner.handle_resolver.assert_not_called()

    def test_legacy_focus_mode_does_not_override_automatic_policy(self):
        for value in ("wait", "background_cycle", "obsolete"):
            with self.subTest(value=value):
                self.profile["focus_mode"] = value
                runner = self.monitor_runner(LifecycleDesktop(active=True))
                runner._run_inventory_cycle = Mock(return_value=False)
                runner.wait = Mock(side_effect=EndMonitor())
                with self.assertRaises(EndMonitor):
                    runner.monitor(1800)
                runner._run_inventory_cycle.assert_called_once_with()
        self.profile.pop("focus_mode")

    def test_background_activation_once_per_cycle_and_restore_before_interval(self):
        desktop = BackgroundDesktop()
        runner = self.monitor_runner(desktop)
        runner.profile["focus_mode"] = "background_cycle"
        runner._run_inventory_cycle = Mock(side_effect=[None, None])
        deadlines = []
        def wait(seconds):
            if seconds == 1800:
                deadlines.append(seconds)
                self.assertEqual(len(desktop.end_calls), len(deadlines))
                self.assertFalse(desktop.is_active)
                if len(deadlines) == 2:
                    raise EndMonitor()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        self.assertEqual(desktop.begin_calls, [5, 5])
        self.assertEqual(desktop.end_calls, [(5, desktop.token), (5, desktop.token)])
        self.assertEqual([call.kwargs for call in runner._run_inventory_cycle.call_args_list],
                         [{}, {}])

    def test_background_idle_rejection_never_starts_cycle_or_cleanup(self):
        desktop = BackgroundDesktop(idle=False)
        runner = self.monitor_runner(desktop)
        runner.profile["focus_mode"] = "background_cycle"
        runner._run_inventory_cycle = Mock()
        runner.wait = Mock(side_effect=EndMonitor())
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        self.assertEqual(desktop.begin_calls, [])
        self.assertEqual(desktop.end_calls, [])
        runner._run_inventory_cycle.assert_not_called()

    def test_background_error_restores_token_without_retry(self):
        desktop = BackgroundDesktop()
        runner = self.monitor_runner(desktop)
        runner.profile["focus_mode"] = "background_cycle"
        runner._run_inventory_cycle = Mock(side_effect=RuntimeError("transfer verification failed"))
        with self.assertRaisesRegex(RuntimeError, "verification"):
            runner.monitor(1800)
        self.assertEqual(desktop.begin_calls, [5])
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])
        runner._run_inventory_cycle.assert_called_once_with()

    def test_background_f8_after_activation_still_restores_token(self):
        desktop = BackgroundDesktop()
        runner = self.monitor_runner(desktop)
        runner.profile["focus_mode"] = "background_cycle"
        runner._run_inventory_cycle = Mock()
        begin = desktop.begin_background_cycle
        def stop_after_begin(handle):
            token = begin(handle)
            runner.stop.set()
            return token
        desktop.begin_background_cycle = stop_after_begin
        with self.assertRaises(InterruptedError):
            runner.monitor(1800)
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])
        runner._run_inventory_cycle.assert_not_called()

    def test_background_does_not_restore_game_already_active_by_user(self):
        desktop = BackgroundDesktop(active=True)
        runner = self.monitor_runner(desktop)
        runner.profile["focus_mode"] = "background_cycle"
        runner._run_inventory_cycle = Mock(return_value=False)
        runner.wait = Mock(side_effect=EndMonitor())
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        self.assertEqual(desktop.begin_calls, [])
        self.assertEqual(desktop.end_calls, [])
        self.assertTrue(desktop.is_active)

    def test_recent_user_activity_never_uses_background_activation(self):
        desktop = BackgroundDesktop(idle=False)
        runner = self.monitor_runner(desktop)
        runner._run_inventory_cycle = Mock()
        runner.wait = Mock(side_effect=EndMonitor())
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        self.assertEqual(desktop.begin_calls, [])
        self.assertEqual(desktop.end_calls, [])
        runner._run_inventory_cycle.assert_not_called()

    def test_background_activation_lost_focus_rechecks_before_cycle(self):
        desktop = BackgroundDesktop()
        runner = self.monitor_runner(desktop)
        runner.profile["focus_mode"] = "background_cycle"
        runner._run_inventory_cycle = Mock()
        begin = desktop.begin_background_cycle
        def inactive_after_begin(handle):
            token = begin(handle)
            desktop.is_active = False
            return token
        desktop.begin_background_cycle = inactive_after_begin
        with self.assertRaisesRegex(RuntimeError, "не стало активным"):
            runner.monitor(1800)
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])
        runner._run_inventory_cycle.assert_not_called()

    def test_background_restoration_waits_one_second_before_cycle(self):
        desktop = BackgroundDesktop()
        runner = self.monitor_runner(desktop)
        runner.profile["focus_mode"] = "background_cycle"
        calls = []
        runner._run_inventory_cycle = Mock(side_effect=lambda **kwargs: calls.append(("cycle", kwargs)) or False)
        def wait(seconds):
            calls.append(("wait", seconds))
            if seconds == 1800 and len(calls) > 1:
                raise EndMonitor()
        runner.wait = wait
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        self.assertEqual(calls, [("wait", 1.0), ("cycle", {}), ("wait", 1800.0)])
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])

    def test_f8_during_restored_window_wait_cancels_before_cycle_and_cleans_up(self):
        desktop = BackgroundDesktop()
        runner = self.monitor_runner(desktop)
        runner.profile["focus_mode"] = "background_cycle"
        runner._run_inventory_cycle = Mock()
        def wait(seconds):
            if seconds == 1800:
                return
            self.assertEqual(seconds, 1.0)
            runner.stop.set()
        runner.wait = wait
        with self.assertRaises(InterruptedError):
            runner.monitor(1800)
        runner._run_inventory_cycle.assert_not_called()
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])

    def test_focus_loss_during_restored_window_wait_cancels_before_cycle(self):
        desktop = BackgroundDesktop()
        runner = self.monitor_runner(desktop)
        runner.profile["focus_mode"] = "background_cycle"
        runner._run_inventory_cycle = Mock()
        def wait(seconds):
            if seconds == 1800:
                return
            self.assertEqual(seconds, 1.0)
            desktop.is_active = False
        runner.wait = wait
        with self.assertRaisesRegex(RuntimeError, "во время отрисовки"):
            runner.monitor(1800)
        runner._run_inventory_cycle.assert_not_called()
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])

    def test_default_afk_does_not_add_restore_render_wait(self):
        runner = self.monitor_runner(BackgroundDesktop(active=True))
        runner._run_inventory_cycle = Mock(return_value=False)
        runner.wait = Mock(side_effect=EndMonitor())
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        runner.wait.assert_called_once_with(1800.0)

    def test_background_denied_restore_is_reported_and_has_no_retry(self):
        desktop = BackgroundDesktop()
        desktop.restore_success = False
        runner = self.monitor_runner(desktop)
        runner.profile["focus_mode"] = "background_cycle"
        runner._run_inventory_cycle = Mock(return_value=False)
        runner.wait = Mock(side_effect=EndMonitor())
        with self.assertRaises(EndMonitor):
            runner.monitor(1800)
        self.assertEqual(desktop.end_calls, [(5, desktop.token)])
        runner.log.assert_called_once_with("Windows не разрешила активировать прежнее окно после проверки.")

    def test_restore_error_cannot_hide_transfer_failure(self):
        desktop = BackgroundDesktop()
        runner = self.monitor_runner(desktop)
        runner.profile["focus_mode"] = "background_cycle"
        runner._run_inventory_cycle = Mock(side_effect=RuntimeError("transfer uncertainty"))
        desktop.end_background_cycle = Mock(side_effect=RuntimeError("restore denied"))
        with self.assertRaisesRegex(RuntimeError, "transfer uncertainty"):
            runner.monitor(1800)
        desktop.end_background_cycle.assert_called_once_with(5, desktop.token)
        runner.log.assert_called_once_with("Не удалось восстановить прежнее окно: restore denied")


class RuntimePanelIntegrationTests(PanelFixture, unittest.TestCase):
    def test_prepare_returns_native_frame_and_reanchors_without_persisting(self):
        original = copy.deepcopy(self.profile)
        image = self.image(inv_offset=(4, 3), store_offset=(6, 5))
        desktop = PanelDesktop([image])
        runner = Runner(desktop, 7, self.profile, self.directory)
        runner.wait = lambda _: None
        self.assertEqual(runner.prepare_panels().size, (200, 140))
        self.assertEqual(runner.inv.rect.as_box(), (24, 33, 72, 57))
        self.assertEqual(runner.store.rect.as_box(), (116, 35, 164, 59))
        self.assertEqual(self.profile, original)
        self.assertTrue(runner.profile["supervised_ok"])
        self.assertTrue(runner.profile["include_gems"])
        self.assertEqual(runner.profile["interval"], 60)
        self.assertEqual(desktop.inputs, [])

    def test_changed_client_does_not_adopt_geometry_sort_transfer_or_revoke_saved_proof(self):
        original = copy.deepcopy(self.profile)
        image = self.image(inv_offset=(4, 3), store_offset=(6, 5), size=(210, 150))
        desktop = PanelDesktop([image])
        runner = Runner(desktop, 7, self.profile, self.directory)
        runner.sort_inventory = Mock()
        with self.assertRaisesRegex(RuntimeError, "Текущая проверка отменена"):
            runner.once()
        runner.sort_inventory.assert_not_called()
        self.assertEqual(desktop.inputs, [])
        self.assertEqual(runner.size, tuple(original["size"]))
        self.assertEqual(runner.inv.rect.as_box(), tuple(original["inventory"]["rect"]))
        self.assertEqual(self.profile, original)
        self.assertTrue(runner.profile["supervised_ok"])


if __name__ == "__main__":
    unittest.main()
