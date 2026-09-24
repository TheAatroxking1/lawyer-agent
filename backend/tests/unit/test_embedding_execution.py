import asyncio
import gc
import threading
import time
from concurrent.futures import Future

import pytest

from lawyer_agent.application.model_gateway import (
    ModelProviderBusy,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from lawyer_agent.domain.model_gateway import EmbeddingVector
from lawyer_agent.infrastructure.providers.embedding_execution import BoundedEmbeddingRunner


def vectors():
    return (EmbeddingVector((1.0, 0.0), 2),)


def blocked_operation(started, release, loop, *, fail=False):
    def operation():
        assert threading.current_thread().daemon
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2), "test must release worker"
        if fail:
            raise RuntimeError("synthetic late error")
        return vectors()

    return operation


async def await_available(runner):
    async def attempt():
        while True:
            try:
                return await runner.run(vectors, timeout_seconds=1)
            except ModelProviderBusy:
                await asyncio.sleep(0)

    return await asyncio.wait_for(attempt(), 1)


async def test_serial_operations_reuse_one_owned_worker_thread():
    runner = BoundedEmbeddingRunner()
    workers = []

    def operation():
        workers.append(threading.current_thread())
        return vectors()

    assert await runner.run(operation, timeout_seconds=1) == vectors()
    assert await runner.run(operation, timeout_seconds=1) == vectors()
    assert workers[0] is workers[1]
    assert workers[0].daemon
    assert await runner.aclose()
    assert not workers[0].is_alive()


async def test_operation_failure_keeps_same_worker_available():
    runner = BoundedEmbeddingRunner()
    workers = []

    def fail():
        workers.append(threading.current_thread())
        raise RuntimeError("synthetic operation failure")

    def succeed():
        workers.append(threading.current_thread())
        return vectors()

    with pytest.raises(RuntimeError, match="synthetic operation failure"):
        await runner.run(fail, timeout_seconds=1)
    assert await runner.run(succeed, timeout_seconds=1) == vectors()
    assert workers[0] is workers[1]
    assert await runner.aclose()


async def test_runner_keeps_event_loop_responsive_and_rejects_concurrency(monkeypatch):
    runner = BoundedEmbeddingRunner()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "run_in_executor", lambda *args: pytest.fail("no default executor"))
    started, release = asyncio.Event(), threading.Event()
    task = asyncio.create_task(
        runner.run(blocked_operation(started, release, loop), timeout_seconds=1)
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(ModelProviderBusy):
            await runner.run(vectors, timeout_seconds=1)
        heartbeat = asyncio.Event()
        loop.call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), 1)
        assert not task.done()
    finally:
        release.set()
    assert await task == vectors()
    assert await runner.run(vectors, timeout_seconds=1) == vectors()
    assert await runner.aclose()


@pytest.mark.parametrize("end", ["timeout", "cancel"])
@pytest.mark.parametrize("late_failure", [False, True])
async def test_timeout_and_cancel_hold_capacity_and_consume_late_errors(end, late_failure):
    runner = BoundedEmbeddingRunner()
    loop = asyncio.get_running_loop()
    errors = []
    original_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    started, release = asyncio.Event(), threading.Event()
    task = asyncio.create_task(
        runner.run(
            blocked_operation(started, release, loop, fail=late_failure),
            timeout_seconds=0.03 if end == "timeout" else 1,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        if end == "cancel":
            task.cancel()
        with pytest.raises(ModelProviderTimeout if end == "timeout" else asyncio.CancelledError):
            await task
        with pytest.raises(ModelProviderBusy):
            await runner.run(vectors, timeout_seconds=1)
        release.set()
        assert await await_available(runner) == vectors()
        assert await runner.aclose()
        await asyncio.sleep(0)
        assert errors == []
    finally:
        release.set()
        loop.set_exception_handler(original_handler)


async def test_close_is_bounded_irreversible_and_reports_drain():
    runner = BoundedEmbeddingRunner()
    started, release = asyncio.Event(), threading.Event()
    task = asyncio.create_task(
        runner.run(
            blocked_operation(started, release, asyncio.get_running_loop()),
            timeout_seconds=1,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert await runner.aclose(timeout_seconds=0.01) is False
        with pytest.raises(ModelProviderUnavailable):
            await runner.run(vectors, timeout_seconds=1)
    finally:
        release.set()
    assert await task == vectors()
    assert await runner.aclose() is True
    assert await runner.aclose() is True


async def test_zero_timeout_reports_live_worker_then_eventual_termination():
    runner = BoundedEmbeddingRunner()
    started, release = asyncio.Event(), threading.Event()
    worker = []
    loop = asyncio.get_running_loop()

    def operation():
        worker.append(threading.current_thread())
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        return vectors()

    task = asyncio.create_task(runner.run(operation, timeout_seconds=1))
    try:
        await asyncio.wait_for(started.wait(), 1)
        close_started = time.monotonic()
        assert await runner.aclose(timeout_seconds=0) is False
        assert time.monotonic() - close_started < 0.05
        assert worker[0].is_alive()
        with pytest.raises(ModelProviderUnavailable):
            await runner.run(vectors, timeout_seconds=1)
    finally:
        release.set()
    assert await task == vectors()
    assert await runner.aclose(timeout_seconds=1) is True
    assert not worker[0].is_alive()


async def test_close_budget_covers_thread_local_exit_cleanup_and_keeps_loop_responsive():
    runner = BoundedEmbeddingRunner()
    local = threading.local()
    cleanup_started = threading.Event()
    cleanup_release = threading.Event()
    worker = []

    class ExitCleanup:
        def __del__(self):
            cleanup_started.set()
            assert cleanup_release.wait(2), "test must release thread-local cleanup"

    def operation():
        worker.append(threading.current_thread())
        local.value = ExitCleanup()
        return vectors()

    assert await runner.run(operation, timeout_seconds=1) == vectors()
    release_timer = threading.Timer(0.3, cleanup_release.set)
    release_timer.start()
    heartbeat_at = []

    async def heartbeat():
        await asyncio.sleep(0.01)
        heartbeat_at.append(time.monotonic())

    started_at = time.monotonic()
    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        closed = await runner.aclose(timeout_seconds=0.05)
        await heartbeat_task
        assert cleanup_started.is_set()
        assert closed is False
        assert worker[0].is_alive()
        assert heartbeat_at[0] - started_at < 0.15
    finally:
        cleanup_release.set()
        release_timer.cancel()
        await heartbeat_task

    assert await runner.aclose(timeout_seconds=1) is True
    assert not worker[0].is_alive()


async def test_close_before_first_run_finishes_without_creating_worker(monkeypatch):
    starts = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: starts.append(self))
    runner = BoundedEmbeddingRunner()
    assert await runner.aclose(timeout_seconds=0) is True
    assert starts == []


async def test_cancelled_close_still_forbids_new_work():
    runner = BoundedEmbeddingRunner()
    started, release = asyncio.Event(), threading.Event()
    task = asyncio.create_task(
        runner.run(
            blocked_operation(started, release, asyncio.get_running_loop()),
            timeout_seconds=1,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        close = asyncio.create_task(runner.aclose())
        await asyncio.sleep(0)
        close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close
        with pytest.raises(ModelProviderUnavailable):
            await runner.run(vectors, timeout_seconds=1)
    finally:
        release.set()
    await task
    assert await runner.aclose()


def test_runner_can_be_reused_across_event_loops():
    runner = BoundedEmbeddingRunner()
    assert asyncio.run(runner.run(vectors, timeout_seconds=1)) == vectors()
    assert asyncio.run(runner.run(vectors, timeout_seconds=1)) == vectors()
    assert asyncio.run(runner.aclose())


def test_late_completion_after_original_loop_closes_is_safe():
    runner = BoundedEmbeddingRunner()
    release = threading.Event()

    def operation():
        assert release.wait(2)
        raise RuntimeError("late after closed loop")

    async def first_loop():
        with pytest.raises(ModelProviderTimeout):
            await runner.run(operation, timeout_seconds=0.01)

    try:
        asyncio.run(first_loop())
        release.set()
        assert asyncio.run(await_available(runner)) == vectors()
        assert asyncio.run(runner.aclose())
    finally:
        release.set()


def test_loop_closing_between_check_and_callback_schedule_is_safe(monkeypatch, caplog):
    from lawyer_agent.infrastructure.providers.embedding_execution import _bridge

    loop = asyncio.new_event_loop()
    source = Future()

    async def prepare_bridge():
        return _bridge(source)

    destination = loop.run_until_complete(prepare_bridge())
    original = loop.call_soon_threadsafe

    def close_before_scheduling(*args, **kwargs):
        # Deterministically place closure between the worker's check and callback scheduling.
        loop.close()
        return original(*args, **kwargs)

    monkeypatch.setattr(loop, "call_soon_threadsafe", close_before_scheduling)
    try:
        source.set_exception(RuntimeError("synthetic late failure"))
        assert source.done() and not source.cancelled()
        assert not destination.done()
        assert not [record for record in caplog.records if record.name == "concurrent.futures"]
    finally:
        loop.close()


async def test_cancelled_bridge_does_not_cancel_worker_future_or_warn_on_late_error():
    from lawyer_agent.infrastructure.providers.embedding_execution import _bridge

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    source = Future()
    try:
        destination = _bridge(source)
        destination.cancel()
        await asyncio.sleep(0)
        assert not source.cancelled() and not source.done()
        source.set_exception(RuntimeError("synthetic late failure"))
        await asyncio.sleep(0)
        assert source.done() and not source.cancelled()
        assert not errors
    finally:
        loop.set_exception_handler(previous_handler)


def test_delivered_error_is_consumed_before_loop_can_stop_and_close():
    from lawyer_agent.infrastructure.providers.embedding_execution import _bridge

    loop = asyncio.new_event_loop()
    errors = []
    loop.set_exception_handler(lambda loop, context: errors.append(context.get("message")))
    source = Future()

    async def prepare_bridge(worker_future):
        return _bridge(worker_future)

    destination = loop.run_until_complete(prepare_bridge(source))
    source.set_exception(RuntimeError("synthetic delivered failure"))
    loop.call_soon(loop.stop)
    try:
        # deliver sets the exception and queues its done callback; stop prevents the next turn.
        loop.run_forever()
        assert destination.done()
    finally:
        loop.close()
    del destination, source
    gc.collect()
    assert not errors


async def test_thread_start_failure_releases_capacity(monkeypatch):
    runner = BoundedEmbeddingRunner()
    original = threading.Thread.start
    monkeypatch.setattr(
        threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("start"))
    )
    with pytest.raises(ModelProviderUnavailable):
        await runner.run(vectors, timeout_seconds=1)
    monkeypatch.setattr(threading.Thread, "start", original)
    assert await runner.run(vectors, timeout_seconds=1) == vectors()
    assert await runner.aclose()


async def test_thread_construction_failure_releases_capacity(monkeypatch):
    import lawyer_agent.infrastructure.providers.embedding_execution as execution

    runner = BoundedEmbeddingRunner()
    original = execution.Thread
    monkeypatch.setattr(
        execution,
        "Thread",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("construct")),
    )
    with pytest.raises(ModelProviderUnavailable):
        await runner.run(vectors, timeout_seconds=1)
    monkeypatch.setattr(execution, "Thread", original)
    assert await runner.run(vectors, timeout_seconds=1) == vectors()
    assert await runner.aclose()


@pytest.mark.parametrize("budget", [True, 0, -1, float("inf"), float("nan"), "1"])
async def test_run_rejects_invalid_timeout_without_starting_work(budget):
    runner = BoundedEmbeddingRunner()
    with pytest.raises(ValueError):
        await runner.run(lambda: pytest.fail("must not execute"), timeout_seconds=budget)
    assert await runner.aclose()
