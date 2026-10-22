"""Product selection and launch validation without Tk or any native input."""
from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

import app
import dashboard
from dashboard import ProductApp, parse_interval, select_game_window
from i18n import SUPPORTED_LOCALES, get_locale, set_locale, tr
from windows import WindowInfo


def window(handle=1, title="War of Genesis Idle Loot", executable=r"C:\Games\Genesis.exe"):
    return WindowInfo(handle, title, executable)


class DashboardSelectionTests(unittest.TestCase):
    def test_complete_title_aliases_and_exact_executable_are_supported(self):
        for title in ("War of Genesis Idle Loot", "War of Genesis: Idle Loot",
                      "  WAR  OF GENESIS  IDLE LOOT "):
            with self.subTest(title=title):
                game = window(title=title, executable=r"D:\Steam\GENESIS.EXE")
                selection = select_game_window([game])
                self.assertEqual(selection.state, "ready")
                self.assertEqual(selection.window, game)

    def test_unrelated_executables_and_partial_titles_cannot_be_selected(self):
        unrelated = [window(executable=r"C:\Tools\FakeGenesis.exe"),
                     window(executable=""), window(executable=r"C:\Tools\helper.exe"),
                     window(title="Genesis"), window(title="War of Genesis Idle Loot — Console"),
                     window(title="Another War of Genesis Idle Loot")]
        result = select_game_window(unrelated)
        self.assertEqual(result.state, "missing")
        self.assertIsNone(result.window)

    def test_multiple_game_windows_are_ambiguous_without_arbitrary_selection(self):
        result = select_game_window([window(1), window(2)])
        self.assertEqual(result.state, "ambiguous")
        self.assertIsNone(result.window)

    def test_no_game_is_missing(self):
        self.assertEqual(select_game_window([]).state, "missing")

    def fake_product(self, windows):
        desktop = SimpleNamespace(windows=Mock(return_value=windows),
                                  activate_for_start=Mock(), capture=Mock(), click=Mock(), move=Mock())
        state = SimpleNamespace(_dashboard_ready=False, worker=None, desktop=desktop, profile={},
                                window_choice=MagicMock(), handle=999,
                                save_interval=Mock(), save_focus_mode=Mock(), _update_dashboard=Mock())
        state.refresh_windows = lambda: ProductApp.refresh_windows(state)
        return state

    def test_launch_refreshes_exact_game_and_discovery_never_sends_input(self):
        state = self.fake_product([window(99, title="Other window"), window(2)])
        with patch.object(app.App, "start") as start:
            ProductApp.start(state, True)
        self.assertEqual(state.handle, 2)
        start.assert_called_once_with(state, True)
        for operation in (state.desktop.activate_for_start, state.desktop.capture,
                          state.desktop.click, state.desktop.move):
            operation.assert_not_called()

    def test_missing_or_ambiguous_game_cancels_start_before_profile_or_input_changes(self):
        for windows in ([], [window(1), window(2)]):
            with self.subTest(count=len(windows)):
                state = self.fake_product(windows)
                with patch.object(app.App, "start") as start:
                    with self.assertRaises(ValueError):
                        ProductApp.start(state, True)
                self.assertIsNone(state.handle)
                start.assert_not_called()
                state.save_interval.assert_not_called()
                state.save_focus_mode.assert_not_called()
                state.desktop.activate_for_start.assert_not_called()

    def test_start_commits_pending_wheel_selection_before_profile_validation(self):
        state = self.fake_product([window(2)])
        state.interval_choice = SimpleNamespace(commit_pending=Mock())
        with patch.object(app.App, "start") as start:
            ProductApp.start(state, True)
        state.interval_choice.commit_pending.assert_called_once_with(notify=False)
        state.save_interval.assert_called_once()
        start.assert_called_once_with(state, True)


class DashboardIntervalTests(unittest.TestCase):
    def test_presets_bounds_and_decimal_input_are_accepted(self):
        for text, expected in (("60", 60), ("1800", 1800), ("3600", 3600), ("86400", 86400),
                               (" 1800,5 ", 1800.5)):
            with self.subTest(value=text):
                self.assertEqual(parse_interval(text), expected)

    def test_nonfinite_invalid_and_outside_range_intervals_are_rejected(self):
        for value in ("", "abc", "nan", "inf", "-inf", "59.9", "86400.1", None, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_interval(value)


class DashboardSettingsTests(unittest.TestCase):
    def test_settings_show_the_current_version_in_every_supported_language(self):
        previous_locale = get_locale()
        self.addCleanup(set_locale, previous_locale)
        state = SimpleNamespace(worker=None, language_choice=Mock(), _animations_enabled=False)
        with patch.object(dashboard.tk, "Toplevel"), patch.object(dashboard.tk, "Frame"), \
                patch.object(dashboard.tk, "Label") as labels, patch.object(dashboard.ttk, "Combobox"), \
                patch.object(dashboard, "AnimatedButton"), patch.object(dashboard, "apply_icon"):
            for locale in SUPPORTED_LOCALES:
                with self.subTest(locale=locale):
                    set_locale(locale)
                    labels.reset_mock()
                    ProductApp.show_settings(state)
                    displayed = [call.kwargs.get("text") for call in labels.call_args_list]
                    self.assertIn(tr("about.version", version="1.6"), displayed)


if __name__ == "__main__":
    unittest.main()
