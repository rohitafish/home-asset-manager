"""Pins probes/kasa.py's total-time deadline on _recv_exact -- see the
comment in probes/kasa.py: the socket's own per-recv() timeout doesn't bound
a slow-dripping response, since every recv() that returns >=1 byte resets
it. Uses a real local TCP server thread that drips response bytes slowly,
rather than mocking the socket module, so this exercises the actual
recv()/settimeout() interaction the fix depends on.

Also pins run()'s must-never-raise contract against a well-formed-JSON-but-
wrong-shape response -- see probes/base.py's ProbeOutcome contract.

_decrypt/_redact are pure and tested directly with no socket at all --
_redact especially, since it's a privacy control (strips geolocation and
cloud account identifiers before anything is persisted, per this repo's PII
policy) and had no test of its own before this.
"""

import json
import socket
import threading
import time

from probes.kasa import _decrypt, _encrypt, _redact, run


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


def _one_shot_server(ready: list, wire_bytes: bytes) -> None:
    """One-shot TCP server on an ephemeral 127.0.0.1 port: accepts one
    connection, discards the request, sends `wire_bytes` immediately, then
    closes. No drip -- for tests that aren't about timing."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    ready.append(srv.getsockname()[1])
    try:
        conn, _ = srv.accept()
    except OSError:
        return
    try:
        conn.recv(4096)
        conn.sendall(wire_bytes)
    except OSError:
        pass
    finally:
        conn.close()
        srv.close()


def _run_against_payload(monkeypatch, payload_obj):
    wire_bytes = _encrypt(json.dumps(payload_obj).encode())
    ready: list[int] = []
    server = threading.Thread(target=_one_shot_server, args=(ready, wire_bytes), daemon=True)
    server.start()
    while not ready:
        time.sleep(0.01)
    port = ready[0]

    real_create_connection = socket.create_connection
    monkeypatch.setattr(
        socket, "create_connection",
        lambda addr, timeout=None: real_create_connection(("127.0.0.1", port), timeout=timeout),
    )
    outcome = run("127.0.0.1", timeout=2)
    server.join(timeout=2)
    return outcome


def test_run_survives_a_wrong_shape_top_level_payload(monkeypatch):
    """Regression test: well-formed JSON that isn't a dict at all (payload.get
    would raise AttributeError) must not escape run() -- probes/base.py's
    contract is "must never raise", not "never raise on a dict"."""
    outcome = _run_against_payload(monkeypatch, [1, 2, 3])

    assert outcome.ok is False
    assert "unexpected shape" in outcome.summary


# -- _decrypt / _redact (pure, no socket) -------------------------------


def test_decrypt_round_trips_through_encrypt():
    plaintext = b'{"system":{"get_sysinfo":{}}}'
    # _encrypt prefixes a 4-byte big-endian length; _decrypt only ever sees
    # the ciphertext that follows it (the length is consumed separately by
    # run(), see _recv_exact's 4-byte read).
    ciphertext = _encrypt(plaintext)[4:]
    assert _decrypt(ciphertext) == plaintext


def test_decrypt_empty_ciphertext():
    assert _decrypt(b"") == b""


def test_redact_strips_top_level_keys():
    redacted = _redact({"alias": "Living Room Plug", "latitude_i": 51.5, "longitude_i": -0.1})
    assert redacted == {"alias": "Living Room Plug"}


def test_redact_strips_cloud_identifiers():
    redacted = _redact({"model": "HS110", "deviceId": "abc123", "hwId": "def456", "oemId": "ghi789"})
    assert redacted == {"model": "HS110"}


def test_redact_recurses_into_nested_dicts():
    redacted = _redact({"outer": {"latitude": 51.5, "alias": "kept"}})
    assert redacted == {"outer": {"alias": "kept"}}


def test_redact_recurses_into_lists_of_dicts():
    # Mirrors sysinfo["children"], the shape a multi-outlet strip sends.
    redacted = _redact([{"id": 1, "hwId": "secret"}, {"id": 2, "alias": "kept"}])
    assert redacted == [{"id": 1}, {"id": 2, "alias": "kept"}]


def test_redact_leaves_scalars_and_non_redacted_keys_untouched():
    assert _redact("plain string") == "plain string"
    assert _redact(42) == 42
    assert _redact(None) is None


def test_run_never_leaks_redacted_fields_end_to_end(monkeypatch):
    # Full run() round trip (real local TCP server, same as the other tests
    # in this file) confirms the redaction actually reaches the public
    # facts/raw a caller sees, not just that _redact() works in isolation.
    outcome = _run_against_payload(
        monkeypatch,
        {
            "system": {
                "get_sysinfo": {
                    "alias": "Living Room Plug",
                    "model": "HS110",
                    "latitude_i": 515000,
                    "longitude_i": -1000,
                    "deviceId": "should-not-appear",
                    "children": [{"id": "0", "alias": "child", "hwId": "also-should-not-appear"}],
                }
            }
        },
    )

    assert outcome.ok is True
    assert "latitude_i" not in outcome.facts
    assert "deviceId" not in outcome.raw
    assert "hwId" not in outcome.raw
    assert "should-not-appear" not in outcome.raw
    assert outcome.facts["alias"] == "Living Room Plug"
    assert outcome.facts["children"] == [{"id": "0", "alias": "child", "state": None}]


def test_run_survives_a_wrong_shape_children_list(monkeypatch):
    """Regression test: sysinfo.get("children") being a list of non-dicts
    (c.get(...) would raise AttributeError) must not escape run() either --
    this is the specific line the original bug report called out."""
    outcome = _run_against_payload(
        monkeypatch,
        {"system": {"get_sysinfo": {"alias": "plug", "children": ["not", "a", "dict"]}}},
    )

    assert outcome.ok is False
    assert "unexpected shape" in outcome.summary
