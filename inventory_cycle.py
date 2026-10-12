"""Bounded visible-inventory drain with a fresh guarded sort between passes.

The caller owns recognition and every native input guard.  A successful step
means an independently verified transfer; an exception is never retried here.
No full screenshot or animation pixels are used as a completion fingerprint.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Collection

from core import GridScan


@dataclass(frozen=True)
class InventoryCycleResult:
    outcome: str
    transfers: int
    sorts: int

    @property
    def complete(self) -> bool:
        return self.outcome == "complete"


def _fingerprint(scan: GridScan) -> tuple[tuple[bool | None, int | None], ...]:
    """Use all recognised cell identities, retaining unknown explicitly."""
    if (not isinstance(scan, GridScan) or len(scan.cells) != scan.grid.count
            or any(cell.index != index for index, cell in enumerate(scan.cells))):
        raise ValueError("Inventory observation is incomplete")
    return tuple((cell.empty, cell.tier) for cell in scan.cells)


def run_inventory_cycle(
    *,
    transfer: Callable[..., bool],
    inventory_snapshot: Callable[[], GridScan],
    selected_tiers: Collection[int],
    check_stop: Callable[[], None],
    pause: Callable[[float], None],
    sort_available: bool,
    max_transfers: int = 512,
    max_sorts: int = 64,
    max_seconds: float = 900.0,
    clock: Callable[[], float] = time.monotonic,
) -> InventoryCycleResult:
    """Drain, compact, and repeat without treating hidden uncertainty as empty.

    ``transfer(sort=True)`` must perform the ordinary guarded in-game sort
    before its fresh inventory scan.  Its true return must mean a verified
    transfer, and ``inventory_snapshot`` must return the final scan used by
    that step.  Unverified input, full storage, cancelled input, and changed
    geometry therefore propagate from the caller without automatic retries.

    Completion requires two identical, fully recognised, candidate-free
    observations immediately after sorting, with visible empty capacity.  It
    relies on the game's sort compacting hidden inventory into that capacity.
    A full grid of excluded tiers cannot establish anything about hidden rows
    and yields ``hidden_unconfirmed``.  Unknown cells similarly never establish
    completion.  New drops can keep making progress, so operational limits
    bound this cycle independently of the game's inventory or storage limits.
    """
    selected = frozenset(selected_tiers)
    if any(isinstance(tier, bool) or not isinstance(tier, int) or not 1 <= tier <= 6
           for tier in selected):
        raise ValueError("Selected tiers must be integers T1 through T6")
    for value in (max_transfers, max_sorts):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("Cycle limits must be positive integers")
    if (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
            or not 0 < max_seconds < float("inf")):
        raise ValueError("Cycle duration must be positive and finite")
    if not isinstance(sort_available, bool):
        raise ValueError("Sort availability must be confirmed")

    start = clock()
    transfers = sorts = 0
    need_sort = sort_available
    previous_no_progress = None
    while True:
        check_stop()
        if (transfers >= max_transfers or clock() - start >= max_seconds
                or (need_sort and sorts >= max_sorts)):
            return InventoryCycleResult("bounded", transfers, sorts)
        did_sort = need_sort
        if did_sort:
            sorts += 1
        moved = transfer(sort=did_sort)
        if not isinstance(moved, bool):
            raise ValueError("Transfer result must be confirmed")
        if moved:
            transfers += 1
            # Identical tier labels may represent newly compacted items.
            # Actual verified progress always starts a new exhaustion check.
            previous_no_progress = None
            need_sort = False
            pause(.4)
            continue

        check_stop()
        scan = inventory_snapshot()
        fingerprint = _fingerprint(scan)
        if any(cell.empty is False and cell.tier in selected for cell in scan.cells):
            # A false step with an eligible visible item is not exhaustion.
            # The native runner should instead report its blocking reason.
            raise RuntimeError("A selected inventory item remains without a verified transfer")
        if not sort_available:
            return InventoryCycleResult("sort_unavailable", transfers, sorts)
        if did_sort:
            if fingerprint == previous_no_progress:
                if scan.unknown_count:
                    return InventoryCycleResult("unknown", transfers, sorts)
                if not scan.empty_count:
                    return InventoryCycleResult("hidden_unconfirmed", transfers, sorts)
                return InventoryCycleResult("complete", transfers, sorts)
            previous_no_progress = fingerprint
        else:
            # A visible-only drain cannot confirm that hidden rows are empty.
            previous_no_progress = None
        need_sort = True
        pause(.4)
