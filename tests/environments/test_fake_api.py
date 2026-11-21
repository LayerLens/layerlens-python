from __future__ import annotations

import threading
from typing import Any, List
from concurrent.futures import ThreadPoolExecutor

import httpx

from .conftest import BASE, FakeAPI, ok

CALLERS = 3


class _CountedTogether(List[Any]):
    """A reply queue that hands each reader of its length the length as it was when they
    read it, only once every caller has read it twice (once per check the fake makes), or
    after 0.1 s: the switch between checking the queue and taking from it, made certain."""

    def __init__(self, replies: List[Any]) -> None:
        super().__init__(replies)
        self.readers = 0
        self.all_reading = threading.Event()
        self.counting = threading.Lock()

    def __len__(self) -> int:
        length = super().__len__()
        with self.counting:
            self.readers += 1
            if self.readers >= 2 * CALLERS:
                self.all_reading.set()
        self.all_reading.wait(0.1)
        return length


def test_concurrent_calls_each_take_the_next_reply_and_the_last_one_repeats() -> None:
    api = FakeAPI()
    api.routes[("GET", "/x")] = _CountedTogether([ok({"i": 0}), ok({"i": 1})])
    together = threading.Barrier(CALLERS)

    def call(_: int) -> httpx.Response:
        together.wait()
        return api.handle(httpx.Request("GET", f"https://api.test{BASE}/x"))

    with ThreadPoolExecutor(CALLERS) as pool:
        replies = list(pool.map(call, range(CALLERS)))

    assert sorted(r.json()["data"]["i"] for r in replies) == [0, 1, 1]
    assert len(api.requests) == CALLERS
