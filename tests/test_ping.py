"""Tests for probes/ping.py.

_parse_ping_output is pure string -> ProbeOutcome transformation, tested here
against captured macOS `ping` transcripts kept in tests/fixtures/ -- this is
the first place this repo keeps a sample on disk rather than as an inline
string constant (see PR discussion: no captured real-world output existed
anywhere in the suite before this). Fixture IPs are private (192.168.1.x),
matching this repo's existing test convention and scripts/check-pii.sh's
RFC1918 exclusion, even though the transcript shape itself was captured from
a real local run.

run() itself is tested only at the subprocess-mechanics level (does it call
`ping` with the right flags, does it convert an exception into a failed
ProbeOutcome rather than raising) -- the parsing it delegates to is covered
exhaustively via _parse_ping_output instead, so these tests don't need to
duplicate every parsing case behind a subprocess.run monkeypatch.
"""

import subprocess
from pathlib import Path

from probes.ping import _parse_ping_output, applies_to, run

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPLY_OUTPUT = (FIXTURES / "ping_reply.txt").read_text()
NO_REPLY_OUTPUT = (FIXTURES / "ping_no_reply.txt").read_text()


class _Iface:
    def __init__(self, ip):
        self.ip = ip


def test_applies_to_any_interface_with_an_ip():
    assert applies_to(None, [_Iface("192.168.1.50")], []) is True


def test_applies_to_false_with_no_ip_on_any_interface():
    assert applies_to(None, [_Iface(None), _Iface("")], []) is False


def test_applies_to_false_with_no_interfaces():
    assert applies_to(None, [], []) is False


def test_parse_reply_extracts_rtt_and_ttl():
    outcome = _parse_ping_output(REPLY_OUTPUT, returncode=0, ip="192.168.1.50")
    assert outcome.ok is True
    assert outcome.facts["rtt_ms"] == "3.271"
    assert outcome.facts["ttl"] == "64"
    assert outcome.facts["packet_loss"] == "0.0%"
    assert outcome.summary == "Replied in 3.271 ms (ttl 64)."
    assert "192.168.1.50" in outcome.raw


def test_parse_reply_with_no_rtt_or_ttl_still_reports_replied():
    # The regexes not matching (a differently-formatted ping build, an
    # unexpected locale) shouldn't turn a successful reply into a failure --
    # the facts dict is just sparser.
    outcome = _parse_ping_output("64 bytes from 192.168.1.50", returncode=0, ip="192.168.1.50")
    assert outcome.ok is True
    assert outcome.summary == "Replied."
    assert "rtt_ms" not in outcome.facts
    assert "ttl" not in outcome.facts


def test_parse_no_reply_is_not_ok_and_reports_100_percent_loss():
    outcome = _parse_ping_output(NO_REPLY_OUTPUT, returncode=2, ip="192.168.1.99")
    assert outcome.ok is False
    assert outcome.facts == {"packet_loss": "100%"}
    assert "192.168.1.99" in outcome.summary
    assert "192.168.1.99" in outcome.raw


def test_parse_no_reply_hardcodes_100_percent_regardless_of_the_loss_regex():
    # A nonzero returncode is what decides failure, not the packet-loss
    # regex -- even if the output text claimed some other loss percentage
    # (a malformed/truncated capture), a failed run is reported as 100%.
    outcome = _parse_ping_output("50.0% packet loss", returncode=1, ip="192.168.1.99")
    assert outcome.ok is False
    assert outcome.facts == {"packet_loss": "100%"}


def test_parse_empty_output_on_failure_has_no_raw():
    outcome = _parse_ping_output("   ", returncode=2, ip="192.168.1.99")
    assert outcome.raw is None


def test_run_skips_ipv6_without_shelling_out(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("subprocess.run should not be called for an IPv6 literal")

    monkeypatch.setattr(subprocess, "run", _boom)
    outcome = run("fe80::1")
    assert outcome.ok is False
    assert "IPv6" in outcome.summary


def test_run_reports_missing_ping_binary(monkeypatch):
    monkeypatch.setattr("probes.ping.shutil.which", lambda _name: None)
    outcome = run("192.168.1.50")
    assert outcome.ok is False
    assert "ping" in outcome.summary.lower()


def test_run_never_raises_on_a_subprocess_error(monkeypatch):
    # A probe must never raise -- see probes/base.py. OSError (e.g. the
    # resolved ping_path vanishing between shutil.which and subprocess.run)
    # is exactly the class of failure the try/except in run() exists for.
    monkeypatch.setattr("probes.ping.shutil.which", lambda _name: "/sbin/ping")

    def _raise(*a, **k):
        raise OSError("no such file or directory")

    monkeypatch.setattr(subprocess, "run", _raise)
    outcome = run("192.168.1.50")
    assert outcome.ok is False
    assert "Could not run ping" in outcome.summary


def test_run_passes_the_captured_reply_through_to_the_parser(monkeypatch):
    # End-to-end sanity check that run() actually wires subprocess output
    # into _parse_ping_output, using a real captured transcript -- the
    # parsing cases themselves are covered directly above.
    monkeypatch.setattr("probes.ping.shutil.which", lambda _name: "/sbin/ping")

    captured_args = {}

    def _fake_run(args, **kwargs):
        captured_args["args"] = args
        return subprocess.CompletedProcess(args, returncode=0, stdout=REPLY_OUTPUT, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    outcome = run("192.168.1.50", timeout=1.5)

    assert outcome.ok is True
    assert outcome.facts["rtt_ms"] == "3.271"
    # -W is milliseconds on macOS -- confirms the timeout*1000 conversion,
    # not just that *some* flag was passed.
    assert captured_args["args"] == ["/sbin/ping", "-c", "1", "-W", "1500", "-n", "192.168.1.50"]
