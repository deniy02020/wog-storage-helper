"""Wheel evidence bounds for the game's observed half-row scroll response."""

# Native delta31 and delta15 both moved about32 pixels with a64.4px pitch.
# Keep more than40% of a row of overlap and reject whole-row aliases via RGB.
MAXIMUM_WHEEL_ROW_FRACTION = .60

# Bulk walking is a separate proof. It never expands the modulo-phase policy
# above: only unique before/after RGB correspondence may establish its offset.
MAXIMUM_BULK_ROWS = 5
MINIMUM_BULK_OVERLAP_ROWS = 2
MAXIMUM_BULK_PULSES = 10


class InventoryMotionUnobservable(RuntimeError):
    """Healthy bounded motion has no unique texture evidence yet."""
