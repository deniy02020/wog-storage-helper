"""Bounded, platform-neutral policy for a positively proved inventory walk.

Recognition and every input stay in the native runner. Tier labels, identical
icons, unchanged full screenshots and an accepted wheel receipt alone do not
establish either endpoint of an inventory. The caller supplies explicit fresh
endpoint/position evidence derived from the viewport, under the same native
window, header and activity guards as a transfer.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable, Collection

from core import CellState, GridScan, GridSpec, Rect
from inventory_motion_policy import MAXIMUM_WHEEL_ROW_FRACTION


@dataclass(frozen=True)
class InventoryEndpointProof:
    kind: str
    direction: str
    geometry_identity: str
    confirmations: int
    # For calibrated clamps this is the positively observed physical movement
    # of the SAME wheel command away from a limit, measured from separators.
    # It must be sub-row, so identical cloned rows cannot conceal its motion.
    calibrated_step_pixels: float | None = None


@dataclass(frozen=True)
class InventoryWalkObservation:
    scan: GridScan
    geometry_identity: str
    # A proved scroll position relative to the reset top, not a digest or a
    # number inferred from how many wheel inputs were attempted.
    offset_pixels: float
    row_pitch_pixels: float
    top_proof: InventoryEndpointProof | None = None
    bottom_proof: InventoryEndpointProof | None = None


@dataclass(frozen=True)
class InventoryWalkResult:
    outcome: str
    transfers: int
    scrolls: int
    resets: int

    @property
    def complete(self) -> bool:
        return self.outcome == "complete"


def _number(value, *, positive=False):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def _validate_observation(observation):
    if not isinstance(observation, InventoryWalkObservation):
        raise ValueError("Inventory viewport observation must be explicit")
    scan = observation.scan
    if (not isinstance(scan, GridScan) or len(scan.cells) != scan.grid.count
            or scan.grid.rows < 1 or scan.grid.columns < 1
            or any(cell.index != index or cell.rect != scan.grid.cell_rect(index)
                   for index, cell in enumerate(scan.cells))):
        raise ValueError("Inventory viewport scan is incomplete")
    if (not isinstance(observation.geometry_identity, str)
            or not observation.geometry_identity
            or not _number(observation.offset_pixels)
            or not _number(observation.row_pitch_pixels, positive=True)
            or observation.row_pitch_pixels > scan.grid.rect.height):
        raise ValueError("Inventory viewport position or geometry is unproved")
    return observation


def _endpoint(observation, direction):
    proof = observation.top_proof if direction == "top" else observation.bottom_proof
    if proof is None:
        return False
    if (not isinstance(proof, InventoryEndpointProof)
            or proof.direction != direction
            or proof.geometry_identity != observation.geometry_identity
            or isinstance(proof.confirmations, bool)
            or not isinstance(proof.confirmations, int)
            or proof.confirmations < 2):
        raise ValueError("Inventory endpoint proof is incomplete or belongs to another layout")
    if proof.kind == "scrollbar":
        if proof.calibrated_step_pixels is not None:
            raise ValueError("Scrollbar proof must not invent a calibrated wheel response")
    elif proof.kind == "calibrated_subrow_clamp":
        step = proof.calibrated_step_pixels
        if (not _number(step, positive=True)
                or not max(1.0, observation.row_pitch_pixels * .03) <= step
                < observation.row_pitch_pixels * MAXIMUM_WHEEL_ROW_FRACTION):
            raise ValueError("Unchanged row-sized wheel motion cannot prove an endpoint")
    else:
        raise ValueError("Inventory endpoint must be visually proved, not inferred from identical items")
    if direction == "top" and observation.offset_pixels != 0:
        raise ValueError("A proved reset top must have zero offset")
    return True


def certify_sorted_prefix(scan: GridScan, selected_tiers: Collection[int],
                          *, require_boundary: bool = True) -> bool:
    """Validate known descending compacted cells under an external Sort proof.

    This predicate does NOT prove a game Sort action or an absolute Top. The
    native caller must independently provide that fresh epoch/position proof.
    No selected item may remain in this prefix. By default completion also
    needs a known tier BELOW the minimum selected tier, or an empty suffix.
    A higher unselected tier alone cannot hide a lower selected tier. Unknown
    cells BEFORE a lower-tier boundary cannot certify completion. After that
    boundary the externally proved fresh descending Sort already places them
    below every selected tier; they need no guessed tier. Known contradictions
    anywhere in the visible grid still invalidate the sorted order. Validation
    mode (require_boundary=False) checks the same prefix without claiming end.
    """
    try:
        supplied = tuple(selected_tiers)
    except TypeError:
        return False
    if (not isinstance(require_boundary, bool) or not supplied
            or any(isinstance(tier, bool) or not isinstance(tier, int) or not 1 <= tier <= 6
                   for tier in supplied)
            or not isinstance(scan, GridScan) or not isinstance(scan.grid, GridSpec)
            or not isinstance(scan.grid.rect, Rect)
            or any(isinstance(count, bool) or not isinstance(count, int) or count < 1
                   for count in (scan.grid.rows, scan.grid.columns))
            or scan.grid.rect.width < scan.grid.columns or scan.grid.rect.height < scan.grid.rows
            or len(scan.cells) != scan.grid.count):
        return False
    selected = frozenset(supplied)
    previous, empty_suffix, lower_sentinel = 7, False, False
    for index, cell in enumerate(scan.cells):
        if (not isinstance(cell, CellState) or cell.index != index
                or cell.rect != scan.grid.cell_rect(index)):
            return False
        if cell.empty is True:
            if cell.tier is not None:
                return False
            empty_suffix = True
            continue
        tier = cell.tier
        if (tier is None and (cell.empty is False or cell.empty is None)
                and lower_sentinel and not empty_suffix):
            # The boundary, not an unknown classification, proves exclusion.
            # Do not use this predicate without its fresh Sort and full Top
            # contract; the unsorted walk retains its unknown-cell semantics.
            continue
        if (cell.empty is not False or empty_suffix
                or isinstance(tier, bool) or not isinstance(tier, int) or not 1 <= tier <= 6
                or tier > previous or tier in selected):
            return False
        previous = tier
        lower_sentinel = lower_sentinel or tier < min(selected)
    return not require_boundary or lower_sentinel or empty_suffix


def certify_full_inventory_top(observation: InventoryWalkObservation, viewport: Rect,
                               rows: int, columns: int, scale: float) -> bool:
    """Check the complete first grid at an independently proved absolute Top.

    The native recognizer supplies COMPLETE input rows after all separator and
    per-cell guards. Fractional raster pitch may inset that grid by a few
    pixels inside the header-bound viewport. Exact rectangle equality would
    reject a genuine eight-row grid, even though no complete row was dropped.
    The allowance is limited to the recognizer's existing three-pixel neutral
    separator tail plus 1.8-pixel fitted phase, at the proved native scale.
    Seven-row/partial views, sideways crops and a changed pitch never prove the
    beginning. This geometric predicate does not prove Sort or item ordering.
    """
    if (not isinstance(viewport, Rect)
            or any(isinstance(count, bool) or not isinstance(count, int) or count < 1
                   for count in (rows, columns))
            or not _number(scale, positive=True) or not .35 <= scale <= 1.5):
        return False
    if viewport.width < columns or viewport.height < rows:
        return False
    observation = _validate_observation(observation)
    if not _endpoint(observation, "top"):
        return False
    grid = observation.scan.grid
    if (grid.rows != rows or grid.columns != columns
            or grid.rect.left != viewport.left or grid.rect.right != viewport.right):
        return False
    top = grid.rect.top - viewport.top
    bottom = viewport.bottom - grid.rect.bottom
    raster_margin = max(1, math.ceil(4.8 * scale))
    nominal_pitch = viewport.height / rows
    pitch_tolerance = max(.75, nominal_pitch * .0125)
    return (0 <= top <= raster_margin and 0 <= bottom <= raster_margin
            and abs(observation.row_pitch_pixels - nominal_pitch) <= 1e-7
            and abs(grid.rect.height / rows - nominal_pitch) <= pitch_tolerance)


def run_inventory_walk(
    *,
    reset_to_top: Callable[[], InventoryWalkObservation],
    inventory_snapshot: Callable[[], InventoryWalkObservation],
    transfer: Callable[..., bool] | None = None,
    transfer_batch: Callable[..., int] | None = None,
    sorted_boundary: Callable[[InventoryWalkObservation], bool] | None = None,
    sorted_epoch: Callable[[], int] | None = None,
    sorted_top: Callable[[InventoryWalkObservation], bool] | None = None,
    scroll_down: Callable[[InventoryWalkObservation], InventoryWalkObservation],
    selected_tiers: Collection[int],
    check_stop: Callable[[], None],
    pause: Callable[[float], None],
    max_scrolls: int = 256,
    max_transfers: int = 512,
    max_resets: int = 514,
    max_seconds: float = 900.,
    clock: Callable[[], float] = time.monotonic,
) -> InventoryWalkResult:
    """Drain each proved overlapping view from top to a positive bottom.

    ``reset_to_top`` and ``scroll_down`` must send only bounded guarded wheel
    inputs, park outside the viewport, reacquire a settled frame, and return
    fresh scan/position proofs. ``scroll_down`` must never sort or skip a full
    unseen viewport. ``transfer(sort=False)`` returns true only after the
    existing independent source/destination verification succeeds. Any thrown
    Stop, takeover, stale geometry, full storage or unverified-input exception
    propagates unchanged, with no retry or cleanup input from this policy.

    A batch callback may independently verify several moves under native
    guards, honoring max_transfers=remaining_budget, and return their strict
    nonnegative integer count. It takes precedence over the legacy boolean
    callback. One proved Top reset follows each nonzero batch, rather than
    resetting between individually verified moves within that batch.

    Every verified transfer or batch restarts at a positively proved top. This prevents
    game compaction from moving a new selected item into an already inspected
    prefix. Unchanged semantic labels, identical icons or no detected movement
    never imply bottom. A no-motion view without an explicit endpoint proof is
    ``endpoint_unconfirmed``. Unknown item tiers do not block a guarded scroll
    past the viewport or a separately verified transfer of a known item. They
    are accumulated over the current complete pass and never mean a finished
    inventory. Verified progress restarts a fresh pass at the proved top, so
    uncertainty from a stale pre-compaction view cannot contaminate a new pass.

    An optional sorted_boundary callback supplies independent fresh Sort-epoch
    evidence. It runs only after a zero-move callback and a fresh validated scan
    in a pass without unknowns, or at an independently complete known Top with
    a positive Sort epoch. That stronger current Top certificate can supersede
    an older uncertain view; uncertainty still blocks ordinary bottom proof.
    A positive known descending sentinel/empty
    certificate permits skipping an empty tail, followed by a fresh proved
    Top/no-selected/no-unknown validation. A complete certified Top uses a pure
    snapshot; a scrolled certificate retains ONE original reset. Only transient
    unknown final cells may receive up to three fresh observations in total,
    with bounded pauses and the same Sort epoch/geometry/full-Top proof. Scrolled prefixes still need
    every ordinary overlap check since the proved Top; labels alone never sort.
    An optional sorted_epoch reader names only guarded fresh Sort generations.
    It must return a monotonic nonnegative integer. Advancing it clears a known
    ordering contradiction only on a fresh proved Top. Advancing an epoch alone
    never clears unknown observations or substitutes for a complete known Top.
    A native sorted_top callback additionally proves that the final Top scan
    includes the complete beginning, rather than only lower complete rows below
    a clipped first row. Failure leaves completion unconfirmed, without input.

    Before completion the caller checks a fresh independently proved Top view.
    Ordinary bottom completion still resets Top; a sorted complete Top can be
    captured again without repeating input.
    New selected items there restart the walk. Limits are operational bounds,
    not invented game inventory or storage limits.
    """
    selected = frozenset(selected_tiers)
    if any(isinstance(tier, bool) or not isinstance(tier, int) or not 1 <= tier <= 6
           for tier in selected):
        raise ValueError("Selected tiers must be integers T1 through T6")
    for value in (max_scrolls, max_transfers, max_resets):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("Inventory walk limits must be positive integers")
    if not _number(max_seconds, positive=True):
        raise ValueError("Inventory walk duration must be positive and finite")
    if not callable(transfer_batch if transfer_batch is not None else transfer):
        raise ValueError("Inventory walk needs a verified transfer callback")
    if sorted_boundary is not None and not callable(sorted_boundary):
        raise ValueError("Sorted inventory boundary proof must be callable")
    if sorted_epoch is not None and not callable(sorted_epoch):
        raise ValueError("Sorted inventory epoch proof must be callable")
    if sorted_top is not None and not callable(sorted_top):
        raise ValueError("Sorted inventory Top proof must be callable")

    start = clock()
    transfers = scrolls = resets = 0
    geometry = None
    pass_unknown = False
    pass_sorted_possible = True

    def read_sorted_epoch():
        epoch = sorted_epoch()
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("Sorted inventory epoch must be a nonnegative integer")
        return epoch

    epoch = read_sorted_epoch() if sorted_epoch is not None else None

    def observe_sorted_epoch(view):
        nonlocal epoch, pass_sorted_possible
        if sorted_epoch is None:
            return
        fresh_epoch = read_sorted_epoch()
        if fresh_epoch < epoch:
            raise ValueError("Sorted inventory epoch must not move backwards")
        if fresh_epoch > epoch:
            if not _endpoint(view, "top"):
                raise ValueError("A fresh Sort epoch needs a positively proved Top")
            epoch = fresh_epoch
            pass_sorted_possible = True

    def result(outcome):
        return InventoryWalkResult(outcome, transfers, scrolls, resets)

    def timed_out():
        return clock() - start >= max_seconds

    def check_view(view):
        nonlocal geometry
        check_stop()
        view = _validate_observation(view)
        if geometry is None:
            geometry = view.geometry_identity
        elif view.geometry_identity != geometry:
            raise RuntimeError("Inventory layout changed during the walk")
        observe_sorted_epoch(view)
        return view

    def reset():
        nonlocal resets
        check_stop()
        if resets >= max_resets or timed_out():
            return None
        resets += 1
        view = check_view(reset_to_top())
        if not _endpoint(view, "top"):
            return False
        return view

    def eligible(view):
        return any(cell.empty is False and cell.tier in selected for cell in view.scan.cells)

    def observe_sorted_prefix(view):
        nonlocal pass_sorted_possible
        # A contradictory known prefix cannot become a Sort certificate just
        # because a later read changes its labels.  Selected views are handled
        # by independently verified transfers, which start a fresh pass.
        if (not eligible(view) and not view.scan.unknown_count
                and not certify_sorted_prefix(view.scan, selected, require_boundary=False)):
            pass_sorted_possible = False

    def sorted_final_top(certified_view, already_full_top):
        """Recheck fresh pixels; never repeat input for an uncertain final scan."""
        expected_epoch = epoch
        pure_proof = (sorted_top is not None and sorted_epoch is not None
                      and expected_epoch > 0)
        check_stop()
        if timed_out():
            return None
        if pure_proof and _endpoint(certified_view, "top"):
            full_top = already_full_top
            if full_top is None:
                full_top = sorted_top(certified_view)
                if not isinstance(full_top, bool):
                    raise ValueError("Sorted Top result must be a verified boolean")
                check_stop()
                if timed_out():
                    return None
            final_top = (check_view(inventory_snapshot()) if full_top else reset())
        else:
            final_top = reset()
        for attempt in range(3):
            if final_top is None or final_top is False:
                return final_top
            check_stop()
            if timed_out():
                return None
            if sorted_epoch is not None and epoch != expected_epoch:
                raise RuntimeError("Inventory Sort generation changed during final readback")
            if final_top.row_pitch_pixels != certified_view.row_pitch_pixels:
                raise RuntimeError("Inventory row pitch changed during final readback")
            if not _endpoint(final_top, "top"):
                return False
            if sorted_top is not None:
                full_top = sorted_top(final_top)
                if not isinstance(full_top, bool):
                    raise ValueError("Sorted Top result must be a verified boolean")
                check_stop()
                if timed_out():
                    return None
                if not full_top:
                    return False
            if (eligible(final_top) or not final_top.scan.unknown_count or not pure_proof
                    or certify_sorted_prefix(final_top.scan,selected)):
                return final_top
            if attempt == 2:
                return final_top
            pause(.12)
            check_stop()
            if timed_out():
                return None
            final_top = check_view(inventory_snapshot())

    view = reset()
    if view is None:
        return result("bounded")
    if view is False:
        return result("top_unconfirmed")
    pass_unknown = bool(view.scan.unknown_count)
    observe_sorted_prefix(view)
    while True:
        check_stop()
        if timed_out() or transfers >= max_transfers:
            return result("bounded")
        # A step owns a fresh pre-input scan and all transfer verification; a
        # false result cannot be interpreted using the older view's contents.
        if transfer_batch is not None:
            remaining = max_transfers - transfers
            moved = transfer_batch(sort=False, max_transfers=remaining)
            if isinstance(moved, bool) or not isinstance(moved, int) or not 0 <= moved <= remaining:
                raise ValueError("Transfer batch must report a bounded verified nonnegative integer")
        else:
            moved = transfer(sort=False)
            if not isinstance(moved, bool):
                raise ValueError("Transfer result must mean a verified boolean outcome")
            moved = int(moved)
        check_stop()
        if moved:
            transfers += moved
            if transfer_batch is not None and (timed_out() or transfers >= max_transfers):
                return result("bounded")
            pause(.4)
            view = reset()
            if view is None:
                return result("bounded")
            if view is False:
                return result("top_unconfirmed")
            pass_unknown = bool(view.scan.unknown_count)
            if sorted_epoch is None:
                pass_sorted_possible = True
            observe_sorted_prefix(view)
            continue
        if timed_out() or transfers >= max_transfers:
            return result("bounded")
        fresh = check_view(inventory_snapshot())
        if timed_out():
            return result("bounded")
        if (fresh.offset_pixels != view.offset_pixels
                or fresh.row_pitch_pixels != view.row_pitch_pixels):
            raise RuntimeError("Inventory position changed during the transfer check")
        if eligible(fresh):
            raise RuntimeError("A selected visible item remains without a verified transfer")
        pass_unknown = pass_unknown or bool(fresh.scan.unknown_count)
        view = fresh
        observe_sorted_prefix(view)
        sorted_possible = (sorted_boundary is not None and pass_sorted_possible
                           and (sorted_epoch is None or epoch > 0))
        sorted_known = not pass_unknown
        full_top_at_certificate = None
        if (sorted_possible and pass_unknown and sorted_top is not None
                and sorted_epoch is not None and epoch > 0
                and _endpoint(view, "top")
                and certify_sorted_prefix(view.scan, selected)):
            # The post-transfer Top may contain temporary uncertain occupancy.
            # A fresh complete known sorted beginning proves the entire lower
            # tail independently of that stale view. Do not clear pass_unknown:
            # a partial/scrolled view or a rejected Sort certificate still needs
            # the original conservative whole-walk proof.
            check_stop()
            sorted_known = sorted_top(view)
            if not isinstance(sorted_known, bool):
                raise ValueError("Sorted Top result must be a verified boolean")
            check_stop()
            if timed_out():
                return result("bounded")
            full_top_at_certificate = sorted_known
        if sorted_possible and sorted_known:
            check_stop()
            certified = sorted_boundary(view)
            if not isinstance(certified, bool):
                raise ValueError("Sorted boundary result must be a verified boolean")
            check_stop()
            if timed_out():
                return result("bounded")
            if certified:
                if not certify_sorted_prefix(view.scan, selected):
                    raise ValueError("Sorted boundary contradicts the recorded known prefix")
                final_top = sorted_final_top(view, full_top_at_certificate)
                if final_top is None:
                    return result("bounded")
                if final_top is False:
                    return result("top_unconfirmed")
                if eligible(final_top):
                    view = final_top
                    pass_unknown = bool(final_top.scan.unknown_count)
                    continue
                if (final_top.scan.unknown_count
                        and not certify_sorted_prefix(final_top.scan,selected)):
                    return result("unknown")
                if not certify_sorted_prefix(final_top.scan, selected, require_boundary=False):
                    # This shortcut relies on the fresh game's Sort epoch.
                    # Its final Top must still agree with that compacted
                    # descending prefix.  Ordinary full-bottom proof remains
                    # available without a sorted-tail assumption.
                    view = final_top
                    pass_unknown = False
                    pass_sorted_possible = False
                    continue
                return result("complete")
        if _endpoint(view, "bottom"):
            final_top = reset()
            if final_top is None:
                return result("bounded")
            if final_top is False:
                return result("top_unconfirmed")
            if timed_out():
                return result("bounded")
            if eligible(final_top):
                view = final_top
                pass_unknown = bool(final_top.scan.unknown_count)
                continue
            if pass_unknown or final_top.scan.unknown_count:
                return result("unknown")
            return result("complete")
        if scrolls >= max_scrolls or timed_out():
            return result("bounded")
        check_stop()
        scrolls += 1
        following = check_view(scroll_down(view))
        if following.row_pitch_pixels != view.row_pitch_pixels:
            raise RuntimeError("Inventory row pitch changed during scrolling")
        advance = following.offset_pixels - view.offset_pixels
        # Reserve one complete row of overlap. Pixel motion cannot be inferred
        # from tier labels or an attempted-wheel count, even for identical rows.
        maximum = min(view.scan.grid.rect.height, following.scan.grid.rect.height)
        maximum -= view.row_pitch_pixels
        if advance < 0 or advance > maximum:
            raise RuntimeError("Inventory scrolling skipped uninspected rows")
        if advance == 0 and not _endpoint(following, "bottom"):
            return result("endpoint_unconfirmed")
        view = following
        pass_unknown = pass_unknown or bool(view.scan.unknown_count)
        observe_sorted_prefix(view)
        pause(.2)
