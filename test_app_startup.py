"""Explicit Start parsing and normal lifecycle dispatch; no Tk/native input."""
import argparse
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from app_startup import startup_options, schedule_explicit_start


class ExplicitStartTests(unittest.TestCase):
    def application(self, **profile):
        app = SimpleNamespace(profile=profile, interval=Mock(), after=Mock(), start=Mock())
        app.safe = lambda action: action()
        return app

    def test_normal_and_portable_launches_never_start_or_change_saved_interval(self):
        for arguments in ([], ["--portable"]):
            app = self.application(interval=9000)
            schedule_explicit_start(app, startup_options(arguments))
            app.interval.set.assert_not_called()
            app.after.assert_not_called()
            app.start.assert_not_called()

    def test_explicit_start_uses_existing_start_after_window_creation(self):
        for seconds in (60, 14400, 86400):
            app = self.application()
            schedule_explicit_start(app, startup_options(["--start", "--interval", str(seconds)]))
            app.interval.set.assert_called_once_with(str(seconds))
            app.start.assert_not_called()
            delay, callback = app.after.call_args.args
            self.assertEqual(delay, 750)
            callback()
            app.start.assert_called_once_with(True)

    def test_start_without_interval_preserves_saved_setting(self):
        app = self.application(interval=9000)
        schedule_explicit_start(app, startup_options(["--start"]))
        app.interval.set.assert_not_called()
        app.after.assert_called_once()

    def test_command_cannot_resume_a_full_store(self):
        app = self.application(storage_full=True)
        schedule_explicit_start(app, startup_options(["--start", "--interval", "60"]))
        app.after.assert_not_called()
        app.interval.set.assert_not_called()

    def test_invalid_or_implicit_start_arguments_cannot_schedule_input(self):
        for arguments in (["--interval", "14400"], ["--start", "--interval", "59"],
                          ["--start", "--interval", "86401"], ["--start", "--interval", "nan"]):
            with self.subTest(arguments=arguments), redirect_stderr(StringIO()), self.assertRaises((SystemExit, ValueError)):
                startup_options(arguments)
