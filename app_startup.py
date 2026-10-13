"""Explicit command-line Start; ordinary launches always leave the timer off."""
from __future__ import annotations
import argparse
from helper_config import validate_interval


def startup_options(argv):
    parser = argparse.ArgumentParser(prog="WoGStorageHelper")
    parser.add_argument("--portable", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--interval", type=validate_interval)
    options = parser.parse_args(argv)
    if options.interval is not None and not options.start:
        parser.error("--interval requires an explicit --start")
    return options


def schedule_explicit_start(application, options):
    if not options.start:
        return
    # A full-store pause still requires the user's explicit space-freed action.
    if application.profile.get("storage_full") is True:
        return
    if options.interval is not None:
        application.interval.set(f"{options.interval:g}")
    # Use the same validated Start action, activation, F8 and worker lifecycle.
    application.after(750, lambda: application.safe(lambda: application.start(True)))
