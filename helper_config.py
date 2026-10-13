"""Validated local settings for the automatic scheduler and transfer filters."""
from __future__ import annotations

import copy
import math

MIN_INTERVAL = 60
DEFAULT_INTERVAL = 1800
MAX_INTERVAL = 86400
IDLE_SECONDS = 600


def default_transfer_filters():
    return {str(tier): {"enabled": tier >= 3}
            for tier in range(1, 7)}


def validate_interval(value):
    if isinstance(value, bool):
        raise ValueError("Интервал: от 60 до 86400 секунд (1 минута — 24 часа).")
    try:
        interval = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError("Интервал: от 60 до 86400 секунд (1 минута — 24 часа).") from None
    if not math.isfinite(interval) or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise ValueError("Интервал: от 60 до 86400 секунд (1 минута — 24 часа).")
    return interval


def validate_transfer_filters(value):
    if not isinstance(value, dict) or any(key not in {str(n) for n in range(1, 7)} for key in value):
        raise ValueError("Фильтры должны содержать только тиры 1–6.")
    result = default_transfer_filters()
    # A supplied mapping is an explicit selection; omitted tiers are disabled.
    for entry in result.values():
        entry["enabled"] = False
    for key, entry in value.items():
        if (not isinstance(entry, dict) or "enabled" not in entry
                or set(entry) - {"enabled", "min_level", "max_level"}):
            raise ValueError("Для каждого тира нужен выбор: включён или выключен.")
        if not isinstance(entry["enabled"], bool):
            raise ValueError("Выбор тира должен быть включён или выключен.")
        # Historical level fields are discarded; only tier selection remains.
        # T1/T2 remain recognised but are never transferable, including when
        # an older profile explicitly enabled them.
        result[key] = {"enabled": entry["enabled"] and int(key) >= 3}
    return result


def migrate_profile(profile):
    """Return independent settings; never change user's calibration or files."""
    if not isinstance(profile, dict):
        raise ValueError("Некорректный профиль.")
    result = copy.deepcopy(profile)
    if "include_gems" in result and not isinstance(result["include_gems"], bool):
        raise ValueError("Историческая настройка самоцветов должна быть логическим значением.")
    old = result.get("interval", DEFAULT_INTERVAL)
    # Only valid historical short intervals receive the new minimum.
    if not isinstance(old, bool):
        try:
            numeric = float(old)
        except (TypeError, ValueError):
            numeric = math.nan
        if math.isfinite(numeric) and 5 <= numeric < MIN_INTERVAL:
            old = MIN_INTERVAL
    result["interval"] = validate_interval(old)
    old_filters = result.get("transfer_filters", default_transfer_filters())
    result["transfer_filters"] = validate_transfer_filters(old_filters)
    if any(entry.get("enabled") is True and
           (entry.get("min_level") is not None or entry.get("max_level") is not None)
           for entry in old_filters.values()):
        # Removing an effective old range broadens eligible items once.
        result["supervised_ok"] = False
    if result.get("include_gems", True) is False:
        result["supervised_ok"] = False
    result["include_gems"] = True
    result["scheduler_mode"] = "automatic"
    result["config_version"] = 3
    return result
