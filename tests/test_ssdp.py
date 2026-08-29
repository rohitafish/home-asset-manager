"""Tests for probes/ssdp.py's SSRF guard on the device-supplied LOCATION
header. SSDP is the unconditional fallback probe, so this URL is fully
attacker-controlled; _safe_location_url is what keeps the follow-up fetch
pointed at the probed device and nowhere else. Pure function -- no HTTP here.

Also pins the total-time deadline on the LOCATION-document fetch itself --
see the comment in probes/ssdp.py: httpx.Client(timeout=timeout) applies
that timeout to each individual read, not the whole streamed body, so a
device dripping the response slowly never trips it (or the byte cap, which
only bounds memory, not time). That test uses a real local UDP responder
(for the M-SEARCH probe.run() sends) and a real slow-dripping TCP/HTTP
server (for the LOCATION fetch) rather than mocking httpx, so it exercises
the actual iter_bytes()/timeout interaction the fix depends on.
"""

import socket
import threading
import time

import pytest

from probes.ssdp import _parse_headers, _safe_location_url, run

PROBED_IP = "192.168.1.50"


# -- _parse_headers (pure, no I/O) -------------------------------------------


def test_parse_headers_extracts_known_fields():
    response = (
        "HTTP/1.1 200 OK\r\n"
        "LOCATION: http://192.168.1.50:1400/xml/device_description.xml\r\n"
        "SERVER: Linux UPnP/1.0 Sonos/86.8-78270\r\n"
        "ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n"
        "USN: uuid:RINCON_AABBCCDDEEFF01400::urn:schemas-upnp-org:device:ZonePlayer:1\r\n"
        "\r\n"
    )
    headers = _parse_headers(response)
    assert headers["LOCATION"] == "http://192.168.1.50:1400/xml/device_description.xml"
    assert headers["SERVER"] == "Linux UPnP/1.0 Sonos/86.8-78270"
    assert headers["ST"] == "urn:schemas-upnp-org:device:ZonePlayer:1"


def test_parse_headers_skips_the_status_line():
    # `.split("\r\n")[1:]` drops line 0 -- confirm the status line never
    # ends up misparsed as a header (it has no ":" so it's naturally
    # skipped, but pin the behaviour rather than rely on that being true).
    headers = _parse_headers("HTTP/1.1 200 OK\r\nSERVER: test\r\n\r\n")
    assert "HTTP/1.1 200 OK" not in headers
    assert headers == {"SERVER": "test"}


def test_parse_headers_keys_are_uppercased():
    headers = _parse_headers("HTTP/1.1 200 OK\r\nserver: lowercase-sent\r\n")
    assert headers["SERVER"] == "lowercase-sent"
    assert "server" not in headers


def test_parse_headers_splits_only_on_the_first_colon():
    # LOCATION's value is itself a URL containing colons (http://host:port) --
    # `.partition(":")` must keep everything after the first one, not
    # truncate at "http" or "host".
    headers = _parse_headers("HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.50:1400/desc.xml\r\n")
    assert headers["LOCATION"] == "http://192.168.1.50:1400/desc.xml"


def test_parse_headers_last_duplicate_wins():
    headers = _parse_headers("HTTP/1.1 200 OK\r\nSERVER: first\r\nSERVER: second\r\n")
    assert headers["SERVER"] == "second"


def test_parse_headers_ignores_lines_with_no_colon():
    headers = _parse_headers("HTTP/1.1 200 OK\r\nnot a header line\r\nSERVER: test\r\n")
    assert headers == {"SERVER": "test"}


def test_parse_headers_empty_response():
    assert _parse_headers("") == {}


def test_parse_headers_strips_surrounding_whitespace():
    headers = _parse_headers("HTTP/1.1 200 OK\r\nSERVER:   test-with-padding   \r\n")
    assert headers["SERVER"] == "test-with-padding"


@pytest.mark.parametrize(
    "location",
    [
        "http://192.168.1.50:1400/xml/device_description.xml",
        "https://192.168.1.50:443/desc.xml",
        "http://192.168.1.50/desc",
    ],
)
def test_accepts_the_probed_host_over_http_s(location):
    assert _safe_location_url(location, PROBED_IP) is True


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1:8000/assets",        # localhost service
        "http://169.254.169.254/latest/meta",  # cloud metadata
        "http://192.168.1.99/desc",            # a different LAN host
        "http://evil.example/desc",            # arbitrary external host
        "file:///etc/passwd",                  # non-http scheme
        "gopher://192.168.1.50/",              # non-http scheme
        "not a url",
    ],
)
def test_rejects_anything_but_the_probed_host(location):
    assert _safe_location_url(location, PROBED_IP) is False


# -- total-time deadline on the LOCATION fetch (probe.run(), not the pure
# _safe_location_url guard above) -------------------------------------------

_HOST = "127.0.0.1"
_SSDP_PORT = 1900  # hardcoded destination in probes/ssdp.py's run() -- not configurable


def _ssdp_responder(location: str) -> None:
    """One-shot UDP responder standing in for a real SSDP device: replies to
    the single M-SEARCH datagram run() sends with a LOCATION header pointing
    at our slow HTTP server, then exits."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2)
    sock.bind((_HOST, _SSDP_PORT))
    try:
        _, addr = sock.recvfrom(4096)
    except OSError:
        return
    response = (
        "HTTP/1.1 200 OK\r\n"
        f"LOCATION: {location}\r\n"
        "ST: upnp:rootdevice\r\n"
        "SERVER: test-stand-in\r\n"
        "USN: uuid:test::upnp:rootdevice\r\n"
        "\r\n"
    ).encode()
    sock.sendto(response, addr)
    sock.close()


def _drip_http_server(ready: list, body: bytes, byte_delay: float) -> None:
    """One-shot raw HTTP/1.1 server on an ephemeral 127.0.0.1 port: replies
    200 with a Content-Length header, then writes `body` one byte at a time
    with `byte_delay` seconds between bytes."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind((_HOST, 0))
    srv.listen(1)
    ready.append(srv.getsockname()[1])
    try:
        conn, _ = srv.accept()
    except OSError:
        return
    try:
        conn.recv(4096)  # drain the request line/headers; content doesn't matter
        header = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Content-Type: text/xml\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        conn.sendall(header)
        for b in body:
            conn.sendall(bytes([b]))
            time.sleep(byte_delay)
    except OSError:
        pass
    finally:
        conn.close()
        srv.close()


def test_run_bounds_total_time_on_slow_dripping_location_fetch():
    """The core regression test: a LOCATION document arriving one byte at a
    time, each individual chunk within httpx's per-read timeout, must still
    be bounded by the overall probe timeout -- not allowed to run for as
    long as the device cares to drip (up to _MAX_LOCATION_BYTES, pre-fix)."""
    body = b"<root><device><friendlyName>x</friendlyName></device></root>"

    ready: list[int] = []
    # Each byte arrives well inside the 0.5s overall timeout used below, but
    # len(body) bytes * 0.05s is well past the deadline this test pins.
    http_server = threading.Thread(
        target=_drip_http_server, args=(ready, body, 0.05), daemon=True
    )
    http_server.start()
    while not ready:
        time.sleep(0.01)
    port = ready[0]

    ssdp_server = threading.Thread(
        target=_ssdp_responder, args=(f"http://{_HOST}:{port}/desc.xml",), daemon=True
    )
    ssdp_server.start()

    start = time.monotonic()
    outcome = run(_HOST, timeout=0.5)
    elapsed = time.monotonic() - start

    # Generous slack over the 0.5s deadline, but nowhere near the ~3s the
    # full drip (60+ bytes * 0.05s) would take if only the per-read httpx
    # timeout applied.
    assert elapsed < 1.5, f"took {elapsed:.2f}s -- deadline was not enforced"
    # The M-SEARCH itself succeeded (SERVER/ST/USN came back), even though
    # the LOCATION fetch was cut short by the deadline.
    assert outcome.facts.get("server") == "test-stand-in"

    ssdp_server.join(timeout=2)
    http_server.join(timeout=2)
