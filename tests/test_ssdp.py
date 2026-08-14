"""Tests for probes/ssdp.py's SSRF guard on the device-supplied LOCATION
header. SSDP is the unconditional fallback probe, so this URL is fully
attacker-controlled; _safe_location_url is what keeps the follow-up fetch
pointed at the probed device and nowhere else. Pure function -- no HTTP here.
"""

import pytest

from probes.ssdp import _safe_location_url

PROBED_IP = "192.168.1.50"


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
