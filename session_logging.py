"""Persistent session diagnostics without replacing existing logging handlers."""
from __future__ import annotations

import logging
import threading
from pathlib import Path


class _SessionFileHandler(logging.FileHandler):
    """A handler owned by Helper, separate from dependency/host handlers."""


_configure_lock = threading.RLock()
_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def configure_session_logging(profile_dir: str | Path, *,
                              logger: logging.Logger | None = None) -> logging.FileHandler:
    """Attach one append-only UTF-8 profile log, even if logging is configured.

    ``basicConfig`` does nothing after another module installs a root handler.
    Explicit ownership permits repeated app initialization and profile changes
    without dropping console handlers or duplicating Helper's file records.
    Opening a new destination must succeed before retiring an owned old one.
    """
    target = Path(profile_dir).resolve() / "helper.log"
    root = logging.getLogger() if logger is None else logger
    with _configure_lock:
        owned = [handler for handler in root.handlers
                 if isinstance(handler, _SessionFileHandler)]
        current = next((handler for handler in owned
                        if Path(handler.baseFilename).resolve() == target
                        and not handler._closed), None)
        if current is None:
            target.parent.mkdir(parents=True, exist_ok=True)
            current = _SessionFileHandler(target, mode="a", encoding="utf-8",
                                          errors="backslashreplace")
            current.setLevel(logging.INFO)
            current.setFormatter(logging.Formatter(_FORMAT))
            root.addHandler(current)
        for handler in owned:
            if handler is not current:
                root.removeHandler(handler)
                handler.close()
        # Keep an existing DEBUG policy. INFO must reach the profile handler
        # even if a dependency's earlier basicConfig left root at WARNING.
        if root.level > logging.INFO:
            root.setLevel(logging.INFO)
        return current
