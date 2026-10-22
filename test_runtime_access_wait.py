"""Foreground-return cooldown tests use fakes only; no native UI or input."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
from runtime import Runner


class AccessWaitTests(unittest.TestCase):
    def runner(self, states, *, stop=False):
        tick=[100.]
        desktop=SimpleNamespace(active=Mock(side_effect=states),
            activate_for_start=Mock(side_effect=AssertionError('wait must not activate')))
        runner=Runner.__new__(Runner)
        runner.desktop=desktop;runner.handle=7
        runner.check_stop=Mock()
        waits=[]
        def wait(seconds):
            waits.append(seconds)
            tick[0]+=seconds
            if stop:raise InterruptedError('stopped')
        runner.wait=wait
        return runner,desktop,tick,waits

    def test_background_to_foreground_resumes_without_waiting_an_hour(self):
        runner,desktop,tick,waits=self.runner([False,False,True])
        with patch('runtime.time.monotonic',side_effect=lambda:tick[0]):
            self.assertTrue(runner._wait_for_access(3600))
        self.assertEqual(waits,[1.])
        desktop.activate_for_start.assert_not_called()

    def test_already_active_failure_needs_a_real_leave_and_return(self):
        runner,desktop,tick,waits=self.runner([True,True,False,True])
        with patch('runtime.time.monotonic',side_effect=lambda:tick[0]):
            self.assertTrue(runner._wait_for_access(3600))
        self.assertEqual(waits,[1.,1.])
        desktop.activate_for_start.assert_not_called()

    def test_unchanged_foreground_failure_keeps_cooldown(self):
        runner,desktop,tick,waits=self.runner([True]*5)
        with patch('runtime.time.monotonic',side_effect=lambda:tick[0]):
            self.assertFalse(runner._wait_for_access(3))
        self.assertEqual(waits,[1.,1.,1.])
        desktop.activate_for_start.assert_not_called()

    def test_background_activity_never_activates_game(self):
        runner,desktop,tick,waits=self.runner([False]*5)
        with patch('runtime.time.monotonic',side_effect=lambda:tick[0]):
            self.assertFalse(runner._wait_for_access(3))
        self.assertEqual(waits,[1.,1.,1.])
        desktop.activate_for_start.assert_not_called()

    def test_stop_interrupts_without_foreground_input(self):
        runner,desktop,tick,waits=self.runner([False,False],stop=True)
        with patch('runtime.time.monotonic',side_effect=lambda:tick[0]),self.assertRaises(InterruptedError):
            runner._wait_for_access(3600)
        self.assertEqual(waits,[1.])
        desktop.activate_for_start.assert_not_called()


if __name__=='__main__':unittest.main()
