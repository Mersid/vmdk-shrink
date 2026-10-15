"""Tests for manifest parsing, diffing, lease parsing, VMX generation, autorun."""

from __future__ import annotations

from pathlib import Path

import pytest

from vmdk_shrink.helper_boot import normalize_mac, parse_leases
from vmdk_shrink.helper_vmx import static_mac, write_helper_vmx
from vmdk_shrink.iso_patch import IsoError, append_boot_param
from vmdk_shrink.manifest import diff, load, parse_manifest_text
from vmdk_shrink.paths import HostPaths

H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64
H4 = "d" * 64


def write_manifest(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_parse_manifest_strips_mount_prefix() -> None:
    m = parse_manifest_text(f"{H1}  /mnt/data/etc/hosts\n{H2}  /mnt/data/boot/vmlinuz\n")
    assert m.entries == {"/etc/hosts": H1, "/boot/vmlinuz": H2}


def test_parse_manifest_rejects_malformed() -> None:
    import pytest

    from vmdk_shrink.guest import GuestError

    with pytest.raises(GuestError):
        parse_manifest_text("not-a-manifest-line\n")


def test_diff_categories() -> None:
    before = parse_manifest_text(f"{H1}  /same\n{H2}  /changed\n{H3}  /gone\n")
    after = parse_manifest_text(f"{H1}  /same\n{H4}  /changed\n{H4}  /added\n")
    d = diff(before, after)
    assert d.changed == ["/changed"]
    assert d.removed == ["/gone"]
    assert d.added == ["/added"]
    assert not d.identical


def test_diff_identical() -> None:
    m = parse_manifest_text(f"{H1}  /x\n")
    assert diff(m, m).identical


def test_load_roundtrip(tmp_path: Path) -> None:
    p = write_manifest(tmp_path / "m.txt", [f"{H1}  /a", f"{H2}  /b"])
    assert load(p).entries == {"/a": H1, "/b": H2}


LEASES = """
lease 192.168.223.129 {
  starts 3 2026/10/04 22:10:01;
  ends 3 2026/10/04 22:20:01;
  hardware ethernet 00:50:56:2d:70:01;
}
lease 192.168.223.130 {
  starts 3 2026/10/04 22:15:01;
  hardware ethernet 00:50:56:aa:bb:cc;
}
lease 192.168.223.131 {
  hardware ethernet 00:50:56-2D-70-01;
}
"""


def test_parse_leases_finds_static_mac(tmp_path: Path) -> None:
    f = tmp_path / "vmnetdhcp.leases"
    f.write_text(LEASES, encoding="utf-8")
    rec = parse_leases(f, static_mac())
    assert rec is not None and rec.ip == "192.168.223.131"  # later block wins


def test_parse_leases_missing(tmp_path: Path) -> None:
    f = tmp_path / "nope.leases"
    assert parse_leases(f, static_mac()) is None


def test_normalize_mac() -> None:
    assert normalize_mac("00-50-56-2D-70-01") == "00:50:56:2d:70:01"


def test_helper_vmx_contents(tmp_path: Path) -> None:
    vmx = write_helper_vmx(
        tmp_path,
        iso_path=Path("C:/iso/x.iso"),
        scratch_path=Path("C:/vms/scratch.vmdk"),
        attach=(Path("C:/vms/data.vmdk"), Path("C:/vms/boot.vmdk")),
        vm_name="shrink-helper",
        mac_suffix="2d:70:01",
    )
    text = vmx.read_text(encoding="utf-8")
    assert 'displayName = "shrink-helper"' in text
    assert 'ethernet0.address = "00:50:56:2d:70:01"' in text
    assert 'scsi0:0.fileName = "C:\\iso\\x.iso"' in text
    assert 'scsi0:1.fileName = "C:\\vms\\scratch.vmdk"' in text
    assert 'scsi0:2.fileName = "C:\\vms\\data.vmdk"' in text
    assert 'scsi0:3.fileName = "C:\\vms\\boot.vmdk"' in text
    assert 'RemoteDisplay.vnc.enabled = "TRUE"' in text
    assert 'bios.bootOrder = "cdrom,hdd"' in text


SLACK = b"# " + b"#" * 46 + b"\n"


def test_append_boot_param_crlf_and_padding() -> None:
    cfg = (
        b"DEFAULT select\r\nLABEL sys\r\n"
        b"APPEND archisobasedir=x quiet\r\nLABEL other\r\nAPPEND foo\r\n"
        + SLACK.replace(b"\n", b"\r\n")
    )
    out = append_boot_param(cfg, "rootpass=pw")
    assert len(out) == len(cfg)
    lines = out.decode("utf-8").splitlines()
    assert "APPEND archisobasedir=x quiet rootpass=pw" in lines
    assert out.startswith(b"DEFAULT select\r\n")
    # the pad line is an unterminated trailing comment
    assert out.count(b"\r\n") == cfg.count(b"\r\n") - 1


def test_append_boot_param_lf_only() -> None:
    cfg = b"APPEND archisobasedir=sysresccd iomem=relaxed\n" + SLACK
    out = append_boot_param(cfg, "rootpass=pw")
    assert len(out) == len(cfg)
    assert out.startswith(b"APPEND archisobasedir=sysresccd iomem=relaxed rootpass=pw\n")


def test_append_boot_param_falls_back_to_first_append() -> None:
    cfg = b"FOO bar\nAPPEND some args\n" + SLACK
    out = append_boot_param(cfg, "rootpass=pw")
    assert b"APPEND some args rootpass=pw" in out
    assert len(out) == len(cfg)


def test_append_boot_param_no_slack_raises() -> None:
    cfg = b"APPEND archisobasedir=x\n# short\n"
    with pytest.raises(IsoError):
        append_boot_param(cfg, "rootpass=a-much-longer-password-than-the-slack-allows")


def test_host_paths_missing_tools(tmp_path: Path) -> None:
    paths = HostPaths(workstation_dir=tmp_path)
    assert len(paths.require_tools()) == 2
