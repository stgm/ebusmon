"""
Server-Sent Events fan-out.

One queue per connected browser. The poll loop broadcasts from its own thread
while Flask request threads register and unregister, so the client list is
guarded by its own lock — deliberately not the data lock, which protects the
readings and is held across longer sections.
"""
import queue
import threading

# A slow or stalled client fills its queue rather than blocking the poll loop;
# once full it is dropped and the browser reconnects.
QUEUE_SIZE = 50

_lock = threading.Lock()
_clients: list[queue.Queue] = []


def register() -> queue.Queue:
    q = queue.Queue(maxsize=QUEUE_SIZE)
    with _lock:
        _clients.append(q)
    return q


def unregister(q: queue.Queue):
    with _lock:
        try:
            _clients.remove(q)
        except ValueError:
            pass


def client_count() -> int:
    with _lock:
        return len(_clients)


def broadcast(payload: str):
    """Send to every client, dropping any whose queue is full."""
    with _lock:
        targets = list(_clients)

    dead = []
    for q in targets:
        try:
            q.put_nowait(payload)
        except Exception:
            dead.append(q)

    for q in dead:
        unregister(q)
