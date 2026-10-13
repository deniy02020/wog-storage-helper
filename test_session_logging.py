"""Real file readback for prior-handler, retry and traceback diagnostics."""
import io
import logging
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from session_logging import configure_session_logging


class SessionLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        # Isolate tests from the test runner's real root handlers/verbosity.
        self.logger = logging.Logger("helper-session-test", level=logging.WARNING)
        self.logger.propagate = False
        self.console = io.StringIO()
        self.console_handler = logging.StreamHandler(self.console)
        self.console_handler.setFormatter(logging.Formatter("host:%(message)s"))
        self.logger.addHandler(self.console_handler)

    def tearDown(self):
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            handler.close()
        self.temporary.cleanup()

    def configure(self, directory=None):
        return configure_session_logging(directory or self.directory, logger=self.logger)

    def contents(self, directory=None):
        for handler in self.logger.handlers:
            handler.flush()
        return ((directory or self.directory) / "helper.log").read_text(encoding="utf-8")

    def test_prior_console_handler_cannot_suppress_utf8_session_file(self):
        formatter = self.console_handler.formatter
        self.configure()
        self.logger.info("Сумка изменилась. Проверяю заново.")
        self.assertIn("INFO Сумка изменилась. Проверяю заново.", self.contents())
        self.assertEqual(self.console.getvalue(), "host:Сумка изменилась. Проверяю заново.\n")
        self.assertIn(self.console_handler, self.logger.handlers)
        self.assertIs(self.console_handler.formatter, formatter)
        self.assertFalse(self.console_handler._closed)

    def test_repeated_configuration_has_one_file_record_and_same_handler(self):
        first = self.configure()
        second = self.configure(self.directory / ".")
        self.assertIs(first, second)
        self.assertEqual(len(self.logger.handlers), 2)
        self.logger.info("Only one session record")
        self.assertEqual(self.contents().count("Only one session record"), 1)

    def test_configuration_uses_root_logger_by_default(self):
        with patch("session_logging.logging.getLogger", return_value=self.logger) as root:
            handler = configure_session_logging(self.directory)
        root.assert_called_once_with()
        self.assertIn(handler, self.logger.handlers)
        self.logger.error("Root error")
        self.assertIn("ERROR Root error", self.contents())

    def test_existing_log_is_appended_and_missing_profile_directory_is_created(self):
        profile = self.directory / "new-profile"
        self.configure(profile)
        self.logger.info("First session")
        first_contents = self.contents(profile)
        handler = next(handler for handler in self.logger.handlers
                       if isinstance(handler, logging.FileHandler))
        self.logger.removeHandler(handler)
        handler.close()
        self.configure(profile)
        self.logger.info("Second session")
        self.assertTrue(self.contents(profile).startswith(first_contents))
        self.assertEqual(self.contents(profile).count("First session"), 1)
        self.assertEqual(self.contents(profile).count("Second session"), 1)

    def test_changing_profile_retires_only_owned_handler(self):
        external = logging.FileHandler(self.directory / "host.log", encoding="utf-8")
        self.logger.addHandler(external)
        old = self.configure()
        self.logger.info("Old profile")
        destination = self.directory / "other-profile"
        new = self.configure(destination)
        self.assertIsNot(old, new)
        self.assertTrue(old._closed)
        self.assertNotIn(old, self.logger.handlers)
        self.assertIn(external, self.logger.handlers)
        self.assertFalse(external._closed)
        self.logger.info("New profile")
        self.assertNotIn("New profile", self.contents())
        self.assertNotIn("Old profile", self.contents(destination))
        external.flush()
        self.assertIn("Old profile", (self.directory / "host.log").read_text(encoding="utf-8"))
        self.assertIn("New profile", (self.directory / "host.log").read_text(encoding="utf-8"))

    def test_failed_new_destination_preserves_current_log_and_existing_handlers(self):
        current = self.configure()
        blocked = self.directory / "not-a-directory"
        blocked.write_text("Existing user file", encoding="utf-8")
        with self.assertRaises(OSError):
            self.configure(blocked)
        self.assertIn(current, self.logger.handlers)
        self.assertFalse(current._closed)
        self.logger.info("Still logged")
        self.assertIn("Still logged", self.contents())
        self.assertEqual(blocked.read_text(encoding="utf-8"), "Existing user file")

    def test_debug_policy_is_preserved_and_file_keeps_info_or_higher(self):
        self.logger.setLevel(logging.DEBUG)
        self.configure()
        self.assertEqual(self.logger.level, logging.DEBUG)
        self.logger.debug("Host debug")
        self.logger.info("Helper info")
        self.assertIn("Host debug", self.console.getvalue())
        self.assertNotIn("Host debug", self.contents())
        self.assertIn("Helper info", self.contents())

    def test_error_preserves_chained_traceback_and_cyrillic_cause(self):
        self.configure()
        try:
            try:
                raise ValueError("Граница ячейки временно перекрыта")
            except ValueError as cause:
                raise RuntimeError("Layout unavailable") from cause
        except RuntimeError:
            self.logger.exception("Cycle failed")
        contents = self.contents()
        self.assertIn("ERROR Cycle failed", contents)
        self.assertIn("Traceback (most recent call last):", contents)
        self.assertIn("ValueError: Граница ячейки временно перекрыта", contents)
        self.assertIn("RuntimeError: Layout unavailable", contents)
        self.assertIn("direct cause", contents)

    def test_concurrent_reconfiguration_does_not_add_duplicate_handlers(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            handlers = list(executor.map(lambda _: self.configure(), range(12)))
        self.assertTrue(all(handler is handlers[0] for handler in handlers))
        self.assertEqual(len(self.logger.handlers), 2)
        self.logger.info("Concurrent setup completed")
        self.assertEqual(self.contents().count("Concurrent setup completed"), 1)


if __name__ == "__main__":
    unittest.main()
