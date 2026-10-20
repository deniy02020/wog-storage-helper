"""Serialized reconnect timing. Generic confirmation buttons are never evidence."""
from __future__ import annotations

from dataclasses import dataclass
import math

RETRY_SECONDS = 300.0
HEALTH_POLL_SECONDS = 10.0
CONNECTION_LOST = "Соединение потеряно. Следующая попытка через 5 минут."
CONNECTION_RETRY = "Пробую восстановить соединение."
CONNECTION_RESTORED = "Соединение восстановлено. Проверяю инвентарь заново."
CONNECTION_PENDING = "Ожидаю возвращения в игру."
OFFLINE_REWARD = "Закрываю окно офлайн-наград."
TRANSFER_INTERRUPTED = "Соединение прервало перенос. Его результат не подтверждён; инвентарь будет проверен заново."


class ConnectionLost(RuntimeError):
    """Raised only by a recognized disconnect message, never by layout failure."""


@dataclass
class ReconnectPolicy:
    disconnected: bool = False
    next_attempt_at: float | None = None
    attempts: int = 0

    @staticmethod
    def _time(now):
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("A finite monotonic time is required")

    def lost(self, now):
        """The first and all subsequent attempts wait five minutes, without drift."""
        self._time(now)
        if self.disconnected:
            return False
        self.disconnected = True
        self.next_attempt_at = now + RETRY_SECONDS
        self.attempts = 0
        return True

    def due(self, now):
        self._time(now)
        return (self.disconnected and self.next_attempt_at is not None
                and now >= self.next_attempt_at)

    def issued(self, now):
        """Reserve the cooldown BEFORE input, including an ambiguous native result."""
        if not self.due(now):
            raise ValueError("A reconnect attempt is not due")
        self.attempts += 1
        self.next_attempt_at = now + RETRY_SECONDS

    def restored(self):
        if not self.disconnected:
            return False
        self.disconnected = False
        self.next_attempt_at = None
        return True
