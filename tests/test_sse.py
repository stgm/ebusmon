import pytest

import sse


@pytest.fixture(autouse=True)
def clean_registry():
    """sse holds module-level state, so reset it between tests."""
    sse._clients.clear()
    yield
    sse._clients.clear()


def test_register_returns_a_queue_and_counts_it():
    q = sse.register()
    assert sse.client_count() == 1
    assert q.empty()


def test_broadcast_reaches_every_client():
    a, b = sse.register(), sse.register()
    sse.broadcast("hello")
    assert a.get_nowait() == "hello"
    assert b.get_nowait() == "hello"


def test_unregister_stops_delivery():
    q = sse.register()
    sse.unregister(q)
    sse.broadcast("hello")
    assert sse.client_count() == 0
    assert q.empty()


def test_unregister_is_idempotent():
    """The stream's finally block can run after the client was already dropped."""
    q = sse.register()
    sse.unregister(q)
    sse.unregister(q)
    assert sse.client_count() == 0


def test_a_client_that_stops_reading_is_dropped():
    """A stalled browser must not block the poll loop, so it is dropped when full."""
    q = sse.register()
    for _ in range(sse.QUEUE_SIZE):
        sse.broadcast("x")
    assert sse.client_count() == 1

    sse.broadcast("overflow")
    assert sse.client_count() == 0


def test_one_dead_client_does_not_stop_the_others():
    stalled, healthy = sse.register(), sse.register()
    for _ in range(sse.QUEUE_SIZE):
        sse.broadcast("x")
    # drain the healthy one so only `stalled` is full
    while not healthy.empty():
        healthy.get_nowait()

    sse.broadcast("last")
    assert healthy.get_nowait() == "last"
    assert sse.client_count() == 1


def test_broadcast_with_no_clients_is_a_noop():
    sse.broadcast("nobody listening")
    assert sse.client_count() == 0
