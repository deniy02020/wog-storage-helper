"""Confirm storage capacity across two complete, freshly guarded sweeps."""
from __future__ import annotations


STORAGE_FULL_MESSAGE = "Все доступные страницы проверены дважды. Хранилище заполнено; сортировка и таймер остановлены."


class StorageFull(RuntimeError):
    """Every accessible page is independently confirmed occupied twice."""


class StorageCapacityUnknown(RuntimeError):
    """No free slot was proved, but unknown cells cannot establish fullness."""


def find_storage_capacity(page_ids, *, inspect_page, check_stop):
    """Visit 1..N twice, returning the first observation with confirmed space.

    inspect_page owns fresh frame, page identity, native input and selection
    verification. An exception propagates without repeating an uncertain click.
    Storage tier recognition is irrelevant: occupied is empty=False, while
    empty=None remains unknown. Purchase controls are never page IDs.
    """
    ids = tuple(page_ids)
    if (not ids or ids != tuple(range(1, len(ids) + 1)) or len(ids) > 6
            or any(isinstance(value, bool) or not isinstance(value, int) for value in ids)):
        raise ValueError("Storage pages must be contiguous confirmed IDs 1..6")
    uncertain = False
    for _sweep in range(2):
        for page_id in ids:
            check_stop()
            result = inspect_page(page_id)
            check_stop()
            store = result[2]
            if len(store.cells) != store.grid.count:
                raise StorageCapacityUnknown("Не все ячейки хранилища распознаны. Работа остановлена.")
            if store.empty_count:
                return result
            uncertain |= any(cell.empty is not False for cell in store.cells)
    if uncertain:
        raise StorageCapacityUnknown("Свободных ячеек не найдено, но заполненность хранилища не подтверждена. Работа остановлена.")
    raise StorageFull(STORAGE_FULL_MESSAGE)
