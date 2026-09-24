"""Single-flight synchronous execution without blocking the event loop.

Timeout/cancellation ends waiting, not the running computation. A daemon worker
owns its slot until the operation actually returns; no work is queued.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from concurrent.futures import Future
from threading import Condition, Lock, Thread
from time import monotonic

from lawyer_agent.application.model_gateway import (
    ModelProviderBusy,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from lawyer_agent.domain.model_gateway import EmbeddingVector

_Vectors = tuple[EmbeddingVector, ...]


def _timeout(value: float, *, allow_zero: bool = False) -> None:
    try:
        valid = (
            type(value) in (int, float)
            and math.isfinite(value)
            and (value >= 0 if allow_zero else value > 0)
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("embedding wait timeout must be a finite positive number")


def _consume_result[Result](future: asyncio.Future[Result]) -> None:
    if not future.cancelled():
        future.exception()


def _bridge[Result](future: Future[Result]) -> asyncio.Future[Result]:
    # A bridge belongs to one waiter only; cancelling it never cancels the worker future.
    loop = asyncio.get_running_loop()
    bridge: asyncio.Future[Result] = loop.create_future()
    bridge.add_done_callback(_consume_result)

    def deliver(source: Future[Result]) -> None:
        if bridge.done():
            return
        if source.cancelled():
            bridge.cancel()
            return
        error = source.exception()
        if error is not None:
            bridge.set_exception(error)
            # The loop may stop before done callbacks run; awaiting still raises this error.
            bridge.exception()
        else:
            bridge.set_result(source.result())

    def completed(source: Future[Result]) -> None:
        # Consume failures even when the destination loop no longer exists.
        if not source.cancelled():
            source.exception()
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(deliver, source)
        except RuntimeError:
            # close() may race the check immediately above.
            if not loop.is_closed():
                raise

    future.add_done_callback(completed)
    return bridge


class BoundedEmbeddingRunner:
    def __init__(self) -> None:
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._active: Future[_Vectors] | None = None
        self._pending: tuple[Callable[[], _Vectors], Future[_Vectors]] | None = None
        self._worker: Thread | None = None
        self._worker_done: Future[None] | None = None
        self._closed = False

    async def run(self, operation: Callable[[], _Vectors], *, timeout_seconds: float) -> _Vectors:
        _timeout(timeout_seconds)
        if not callable(operation):
            raise ValueError("embedding operation must be callable")
        with self._lock:
            if self._closed:
                raise ModelProviderUnavailable("embedding runner is closed")
            if self._active is not None:
                raise ModelProviderBusy("embedding runner is busy")
            future: Future[_Vectors] = Future()
            self._active = future
            self._pending = (operation, future)
            if self._worker is None:
                try:
                    worker_done: Future[None] = Future()
                    worker = Thread(
                        target=self._work,
                        args=(worker_done,),
                        daemon=True,
                        name="lawyer-embedding",
                    )
                    self._worker = worker
                    self._worker_done = worker_done
                    worker.start()
                except Exception:  # noqa: BLE001 - thread allocation and startup boundary
                    self._worker = None
                    self._worker_done = None
                    self._pending = None
                    self._active = None
                    raise ModelProviderUnavailable("embedding worker could not start") from None
            self._condition.notify()
        try:
            return await asyncio.wait_for(asyncio.shield(_bridge(future)), timeout_seconds)
        except TimeoutError:
            raise ModelProviderTimeout("embedding wait timed out") from None

    def _work(self, worker_done: Future[None]) -> None:
        try:
            while True:
                with self._condition:
                    while self._pending is None and not self._closed:
                        self._condition.wait()
                    if self._pending is None:
                        return
                    operation, future = self._pending
                    self._pending = None
                try:
                    result = operation()
                except BaseException as error:  # keep the worker usable after operation failure
                    with self._condition:
                        if self._active is future:
                            self._active = None
                        self._condition.notify_all()
                    future.set_exception(error)
                else:
                    with self._condition:
                        if self._active is future:
                            self._active = None
                        self._condition.notify_all()
                    future.set_result(result)
        finally:
            worker_done.set_result(None)

    async def aclose(self, *, timeout_seconds: float = 5.0) -> bool:
        _timeout(timeout_seconds, allow_zero=True)
        deadline = monotonic() + timeout_seconds
        with self._condition:
            self._closed = True
            worker = self._worker
            self._condition.notify_all()
        if worker is None:
            return True
        while worker.is_alive():
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(remaining, 0.01))
        return True
