"""discovery/local_host.py's two platform-specific readers, which until the
Linux port were untested. The Darwin paths are exercised through a stubbed
subprocess; the Linux paths through a fixture tree standing in for sysfs.
"""

from pathlib import Path

import pytest

from discovery import local_host


def _fake_run(stdout: str):
    class _Result:
        def __init__(self):
            self.stdout = stdout

    def run(*args, **kwargs):
        return _Result()

    return run


# --------------------------------------------------------------- hardware


def test_darwin_reads_system_profiler(monkeypatch):
    monkeypatch.setattr(local_host.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        local_host.subprocess,
        "run",
        _fake_run(
            '{"SPHardwareDataType":[{"serial_number":"C07XYZ","machine_model":"Macmini9,1",'
            '"model_number":"MGNR3B/A","machine_name":"Mac mini"}]}'
        ),
    )
    assert local_host.collect_local_hardware() == {
        "serial_number": "C07XYZ",
        "model_identifier": "Macmini9,1",
        "model_number": "MGNR3B/A",
        "model": "Mac mini",
    }


def test_darwin_read_failure_is_none_not_an_exception(monkeypatch):
    monkeypatch.setattr(local_host.platform, "system", lambda: "Darwin")

    def boom(*a, **k):
        raise RuntimeError("system_profiler exploded")

    monkeypatch.setattr(local_host.subprocess, "run", boom)
    assert local_host.collect_local_hardware() is None


def _dmi_tree(tmp_path: Path, **attrs) -> Path:
    d = tmp_path / "dmi"
    d.mkdir()
    for name, value in attrs.items():
        (d / name).write_text(value + "\n")
    return d


def test_linux_reads_dmi_for_apple_hardware(monkeypatch, tmp_path):
    monkeypatch.setattr(local_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        local_host,
        "_DMI_DIR",
        _dmi_tree(
            tmp_path,
            sys_vendor="Apple Inc.",
            product_name="MacBookPro11,1",
            product_serial="C02ABC",
        ),
    )
    assert local_host.collect_local_hardware() == {
        "serial_number": "C02ABC",
        "model_identifier": "MacBookPro11,1",
        "model_number": None,  # not in DMI; left untouched on the asset
        "model": "MacBook Pro",
    }


def test_linux_non_apple_vendor_labels_vendor_and_product(monkeypatch, tmp_path):
    monkeypatch.setattr(local_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        local_host,
        "_DMI_DIR",
        _dmi_tree(tmp_path, sys_vendor="LENOVO", product_name="20XW"),
    )
    hw = local_host.collect_local_hardware()
    assert hw["model"] == "LENOVO 20XW"
    assert hw["serial_number"] is None


def test_linux_root_only_serial_falls_back_to_dmidecode(monkeypatch, tmp_path):
    monkeypatch.setattr(local_host.platform, "system", lambda: "Linux")
    dmi = _dmi_tree(
        tmp_path,
        sys_vendor="Apple Inc.",
        product_name="MacBookPro11,1",
        product_serial="secret",
    )
    (dmi / "product_serial").chmod(0o000)
    monkeypatch.setattr(local_host, "_DMI_DIR", dmi)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _fake_run("C02FROMDMIDECODE\n")()

    monkeypatch.setattr(local_host.subprocess, "run", fake_run)
    try:
        hw = local_host.collect_local_hardware()
    finally:
        (dmi / "product_serial").chmod(0o600)
    if hw["serial_number"] is None:
        pytest.skip("running as root: the 0000-mode file was readable anyway")
    assert hw["serial_number"] == "C02FROMDMIDECODE"
    assert calls and calls[0][:2] == ["sudo", "-n"]


def test_linux_without_dmi_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(local_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(local_host, "_DMI_DIR", tmp_path / "missing")
    assert local_host.collect_local_hardware() is None


def test_other_platforms_are_skipped(monkeypatch):
    monkeypatch.setattr(local_host.platform, "system", lambda: "Windows")
    assert local_host.collect_local_hardware() is None
    assert local_host.local_macs() == []


# ------------------------------------------------------------------- MACs


def test_darwin_macs_skip_locally_administered(monkeypatch):
    monkeypatch.setattr(local_host.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        local_host.subprocess,
        "run",
        _fake_run(
            "en0: flags=8863\n\tether 00:00:5e:00:53:02\n"
            "awdl0: flags=8943\n\tether 6a:d1:33:19:8f:1d\n"  # 0x6a: LA bit set
            "bridge0:\n\tether 00:00:00:00:00:00\n"
        ),
    )
    assert local_host.local_macs() == ["00:00:5e:00:53:02"]


def _net_tree(tmp_path: Path, ifaces: dict[str, tuple[str, bool]]) -> Path:
    """ifaces: name -> (mac, is_physical)."""
    net = tmp_path / "net"
    net.mkdir()
    for name, (mac, physical) in ifaces.items():
        d = net / name
        d.mkdir()
        (d / "address").write_text(mac + "\n")
        if physical:
            (d / "device").mkdir()
    return net


def test_linux_macs_take_physical_nics_only(monkeypatch, tmp_path):
    monkeypatch.setattr(local_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        local_host,
        "_NET_DIR",
        _net_tree(
            tmp_path,
            {
                "docker0": (
                    "86:aa:93:bc:47:6b",
                    False,
                ),  # Docker bridge: the false join
                "veth1a2b": ("aa:bb:cc:dd:ee:01", False),
                "br-9f8e": ("02:42:ac:11:00:01", False),
                "lo": ("00:00:00:00:00:00", False),
                "ens9": ("00:00:5e:00:53:01", True),
                "wlp3s0": ("00:00:5e:00:53:03", True),
            },
        ),
    )
    assert local_host.local_macs() == ["00:00:5e:00:53:01", "00:00:5e:00:53:03"]


def test_linux_macs_skip_randomized_physical(monkeypatch, tmp_path):
    monkeypatch.setattr(local_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        local_host,
        "_NET_DIR",
        _net_tree(tmp_path, {"wlan0": ("7a:31:c1:b7:51:42", True)}),
    )
    assert local_host.local_macs() == []  # 0x7a has the locally-administered bit


def test_linux_macs_missing_sysfs_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(local_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(local_host, "_NET_DIR", tmp_path / "nope")
    assert local_host.local_macs() == []
