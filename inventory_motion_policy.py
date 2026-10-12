"""Wheel evidence bounds for the game's observed half-row scroll response."""

# Native delta31 and delta15 both moved about32 pixels with a64.4px pitch.
# Keep more than40% of a row of overlap and reject whole-row aliases via RGB.
MAXIMUM_WHEEL_ROW_FRACTION = .60


class InventoryMotionUnobservable(RuntimeError):
    """Healthy bounded motion has no unique texture evidence yet."""
