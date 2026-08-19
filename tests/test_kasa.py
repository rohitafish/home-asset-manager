"""Pins probes/kasa.py's total-time deadline on _recv_exact -- see the
comment in probes/kasa.py: the socket's own per-recv() timeout doesn't bound
a slow-dripping response, since every recv() that returns >=1 byte resets
it. Uses a real local TCP server thread that drips response bytes slowly,
rather than mocking the socket module, so this exercises the actual
recv()/settimeout() interaction the fix depends on.
"""

import socket
import threading
import time

from probes.kasa import run


def _drip_server(ready: list, body: bytes, byte_delay: float) -> None:
    """One-shot TCP server on an ephemeral 127.0.0.1 port: accepts one
    connection, discards whatever the client sends, then writes `body` one
    byte at a time with `byte_delay` seconds between bytes. Appends the
    bound port to `ready` so the test thread can connect once listening."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    ready.append(srv.getsockname()[1])
    try:
        conn, _ = srv.accept()
    except OSError:
        return
    try:
        conn.recv(4096)  # drain the probe's request; content doesn't matter
        for b in body:
            conn.sendall(bytes([b]))
            time.sleep(byte_delay)
    except OSError:
        pass
    finally:
        conn.close()
        srv.close()


def test_run_bounds_total_time_on_slow_dripping_response(monkeypatch):
    """The core regression test: a response arriving one byte at a time,
    each individual byte within the per-op timeout, must still be bounded
    by the overall probe timeout -- not allowed to run for as long as the
    device cares to drip (up to 65536 bytes, i.e. hours, pre-fix)."""
    # 4-byte length prefix declaring a 6-byte body, then the 6 body bytes --
    # none of it needs to decrypt to anything real, since the deadline must
    # fire during the length-prefix read, before any decryption is reached.
    payload = (6).to_bytes(4, "big") + b"xxxxxx"

    ready: list[int] = []
    # Each byte arrives well inside the 0.5s overall timeout used below, but
    # 10 bytes * 0.15s = 1.5s total -- past the deadline this test pins.
    server = threading.Thread(target=_drip_server, args=(ready, payload, 0.15), daemon=True)
    server.start()
    while not ready:
        time.sleep(0.01)
    port = ready[0]

    real_create_connection = socket.create_connection
    monkeypatch.setattr(
        socket, "create_connection",
        lambda addr, timeout=None: real_create_connection(("127.0.0.1", port), timeout=timeout),
    )

    start = time.monotonic()
    outcome = run("127.0.0.1", timeout=0.5)
    elapsed = time.monotonic() - start

    assert outcome.ok is False
    # Generous slack over the 0.5s deadline, but nowhere near the ~1.5s the
    # full drip would take if only the per-recv() timeout applied.
    assert elapsed < 1.2, f"took {elapsed:.2f}s -- deadline was not enforced"
    server.join(timeout=2)
