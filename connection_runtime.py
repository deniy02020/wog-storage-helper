"""One worker owns reconnect and inventory input; no background click thread."""
from __future__ import annotations

import time

from connection_vision import DisconnectRecognizer, modal_candidates, reward_candidates, session_candidates
from reconnection import (ConnectionLost, ReconnectPolicy, HEALTH_POLL_SECONDS,
    CONNECTION_LOST, CONNECTION_RETRY, CONNECTION_RESTORED, CONNECTION_PENDING,
    TRANSFER_INTERRUPTED, OFFLINE_REWARD)


class ConnectionMonitor:
    def __init__(self, runner, recognizer=None, clock=time.monotonic, settle_seconds=3.0):
        self.runner = runner
        self.recognizer = recognizer or DisconnectRecognizer()
        self.clock = clock
        self.policy = ReconnectPolicy()
        self.cycle_deadline = None
        self.last_background_probe = self.clock()
        self.settle_seconds = settle_seconds
        self.online_seen_at = None
        self.reward_input_issued = False
        self.session_input_issued = False

    def sort_wait_remaining(self):
        return max(0.0, (self.cycle_deadline or self.clock()) - self.clock())

    def close(self):
        self.recognizer.close()

    def _lost(self):
        self.online_seen_at = None
        if self.policy.lost(self.clock()):
            self.runner.log(CONNECTION_LOST)
        self.runner.state('reconnecting', CONNECTION_LOST, self.policy.next_attempt_at)

    def _return_pending(self):
        self.online_seen_at = None
        self.policy.lost(self.clock())
        self.runner.state('reconnecting', CONNECTION_PENDING, self.policy.next_attempt_at)

    def guard_frame(self, image):
        self.runner.check_stop()
        if self.recognizer.detect(image, self.runner.check_stop) is not None:
            self._lost()
            raise ConnectionLost(CONNECTION_LOST)
        if self.recognizer.detect_reward(image, self.runner.check_stop) is not None:
            self._return_pending()
            raise ConnectionLost(CONNECTION_PENDING)
        if (reward_candidates(image) or modal_candidates(image) or session_candidates(image)):
            # An unreadable reward overlay must hold the inventory workflow;
            # it does not grant permission to press its button.
            self._return_pending()
            raise ConnectionLost(CONNECTION_PENDING)

    def idle_probe(self):
        """Foreground only; an hourly sort must not delay disconnect detection."""
        runner = self.runner
        if not runner.desktop.active(runner.handle):
            return False
        image = runner.desktop.capture(runner.handle)
        if self.recognizer.detect(image, runner.check_stop) is not None:
            self._lost()
            return True
        if self.recognizer.detect_session(image,runner.check_stop) is not None:
            self._lost()
            return True
        if (self.recognizer.detect_reward(image, runner.check_stop) is not None
                or reward_candidates(image)):
            self._return_pending()
            return True
        return False

    def _gameplay_visible(self, image):
        """Positive live-UI evidence, not just disappearance of the error popup."""
        if (modal_candidates(image) or reward_candidates(image) or session_candidates(image)
                or self.recognizer.detect_reward(image,self.runner.check_stop) is not None):
            return False
        controller = self.runner.panel_controller
        if controller is None:
            return False
        if any(rect is not None for rect in controller.locate(image)):
            return True
        steps = controller.controls.get('inventory_open', ())
        return bool(steps and controller.locate_control(image, *steps[0]) is not None)

    def step(self):
        """Called only after the scheduler's existing foreground/idle gate."""
        runner = self.runner
        runner.check_stop()
        image = runner.desktop.capture(runner.handle)
        proof = self.recognizer.detect(image, runner.check_stop)
        if proof is not None:
            self._lost()
            if self.policy.due(self.clock()):
                # The exact message, paired buttons, location and client size
                # must survive a second independent capture before any input.
                runner.wait(.15)
                fresh = runner.desktop.capture(runner.handle)
                current = self.recognizer.detect(fresh, runner.check_stop)
                if current != proof:
                    return False
                runner.check_stop()
                self.policy.issued(self.clock())
                runner.state('reconnecting', CONNECTION_RETRY, self.policy.next_attempt_at)
                runner.log(CONNECTION_RETRY)
                runner.desktop.click(runner.handle, current.point, fresh.size)
            return False
        session = self.recognizer.detect_session(image,runner.check_stop)
        if session is not None:
            self._lost()
            due = self.policy.due(self.clock())
            # An expired-session response to an already issued reconnect is
            # the next step of that attempt. A direct session expiry waits
            # the ordinary five minutes. Never rapidly repeat this button.
            if due or (self.policy.attempts > 0 and not self.session_input_issued):
                runner.wait(.15)
                fresh = runner.desktop.capture(runner.handle)
                current = self.recognizer.detect_session(fresh,runner.check_stop)
                if current != session:
                    return False
                runner.check_stop()
                if due:
                    self.policy.issued(self.clock())
                self.session_input_issued = True
                runner.log(CONNECTION_RETRY)
                runner.state('reconnecting',CONNECTION_PENDING,self.policy.next_attempt_at)
                runner.desktop.click(runner.handle,current.point,fresh.size)
            return False
        reward = self.recognizer.detect_reward(image, runner.check_stop)
        if reward is not None:
            self._return_pending()
            if not self.reward_input_issued:
                runner.wait(.15)
                fresh = runner.desktop.capture(runner.handle)
                current = self.recognizer.detect_reward(fresh,runner.check_stop)
                if current != reward:
                    return False
                runner.check_stop()
                self.reward_input_issued = True
                runner.log(OFFLINE_REWARD)
                runner.state('reconnecting',OFFLINE_REWARD,self.policy.next_attempt_at)
                runner.desktop.click(runner.handle,current.point,fresh.size)
            return False
        if not self.policy.disconnected:
            return True
        runner.state('reconnecting', CONNECTION_PENDING, self.policy.next_attempt_at)
        if not self._gameplay_visible(image):
            self.online_seen_at = None
            return False
        if self.online_seen_at is None:
            self.online_seen_at = self.clock()
        if self.clock() - self.online_seen_at < self.settle_seconds:
            return False
        runner.wait(.2)
        fresh = runner.desktop.capture(runner.handle)
        if (fresh.size != image.size or self.recognizer.detect(fresh, runner.check_stop) is not None
                or not self._gameplay_visible(fresh)):
            return False
        # A new connection epoch discards the old inventory decision entirely.
        # An interrupted transfer is NOT counted or retried. The next cycle
        # obtains new headers, independent item grids and transfer baselines.
        if getattr(runner, '_transfer_input_issued', False) or runner._panel_open_input_pending():
            runner.log(TRANSFER_INTERRUPTED)
        runner.reset_after_reconnection()
        self.policy.restored()
        self.reward_input_issued = False
        self.session_input_issued = False
        self.online_seen_at = None
        self.cycle_deadline = None
        runner.log(CONNECTION_RESTORED)
        runner.state('working', CONNECTION_RESTORED)
        return True

    def wait_between_cycles(self, seconds):
        runner = self.runner
        deadline = self.clock() + seconds
        self.cycle_deadline = deadline
        while True:
            runner.check_stop()
            if self.policy.disconnected:
                return
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            runner.wait(min(remaining, HEALTH_POLL_SECONDS))
            if self.idle_probe():
                return
            idle = getattr(runner.desktop, 'idle_seconds', None)
            if (not runner.desktop.active(runner.handle) and idle is not None and idle() >= 600
                    and self.clock() - self.last_background_probe >= 60):
                # Ask the ordinary scheduler to acquire an idle token for a
                # health-only observation. Keep the original sort deadline.
                self.last_background_probe = self.clock()
                return
