"""Reading a project's whole models or benchmarks list, a page at a time."""

from __future__ import annotations

from typing import Any, Set, Dict, List, TypeVar, Callable, Optional, Awaitable

from ._constants import API_MAX_PAGE_SIZE

T = TypeVar("T")

# The project's models and benchmarks lists answer 20 rows a page unless asked for more, and 100
# at most. One page of the default size was all ``get()`` returned, and ``add()`` / ``remove()``
# PATCHed the project with it: every row past the 20th was dropped from the project.
PAGE_SIZE = API_MAX_PAGE_SIZE


def page_params(page: int) -> Dict[str, str]:
    return {"page": str(page), "page_size": str(PAGE_SIZE)}


def _row_id(row: Any) -> str:
    return str(getattr(row, "id", None))


def _new_rows(batch: List[T], seen: Set[str]) -> List[T]:
    fresh = [row for row in batch if _row_id(row) not in seen]
    seen.update(_row_id(row) for row in fresh)
    return fresh


def every_page(fetch_page: Callable[[int], Optional[List[T]]]) -> List[T]:
    """Every row of a list: pages of ``PAGE_SIZE`` until one comes back short.

    A page that brings no row not seen before also ends it, so an API that ignores ``page`` can't
    make it loop.
    """
    rows: List[T] = []
    seen: Set[str] = set()
    page = 1
    while True:
        batch = fetch_page(page) or []
        fresh = _new_rows(batch, seen)
        rows.extend(fresh)
        if len(batch) < PAGE_SIZE or not fresh:
            return rows
        page += 1


async def every_page_async(fetch_page: Callable[[int], Awaitable[Optional[List[T]]]]) -> List[T]:
    """:func:`every_page` for an async page reader."""
    rows: List[T] = []
    seen: Set[str] = set()
    page = 1
    while True:
        batch = await fetch_page(page) or []
        fresh = _new_rows(batch, seen)
        rows.extend(fresh)
        if len(batch) < PAGE_SIZE or not fresh:
            return rows
        page += 1
