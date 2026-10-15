"""File-hash manifests: collected in the guest, diffed on the host.

The manifest is the lossless proof: every regular file's SHA-256 before and
after the operation, diffed at the end. 400k files / ~430 GiB hashes in ~18 min.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .guest import Guest, GuestError

MOUNT = "/mnt/data"
_HASH_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(.+)$")


@dataclass
class Manifest:
    entries: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass
class ManifestDiff:
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not (self.changed or self.removed or self.added)

    def summary(self) -> str:
        return f"{len(self.changed)} changed, {len(self.removed)} missing, {len(self.added)} added"


def parse_manifest_text(text: str, *, strip_prefix: str = MOUNT) -> Manifest:
    """Parse ``sha256sum`` output; strip the mount prefix from paths."""
    entries: dict[str, str] = {}
    malformed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _HASH_LINE.match(line.strip())
        if not m:
            malformed += 1
            continue
        digest, path = m.groups()
        if strip_prefix and path.startswith(strip_prefix):
            path = path[len(strip_prefix) :]
        entries[path] = digest.lower()
    if malformed:
        raise GuestError(f"manifest parse: {malformed} malformed lines")
    return Manifest(entries=entries)


def collect(guest: Guest, *, mount: str = MOUNT, out_path: Path) -> Manifest:
    """Hash every regular file under ``mount`` inside the guest, streaming to disk."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_file = out_path.with_suffix(".raw")
    cmd = f"find {mount} -xdev -type f -print0 | xargs -0 -n 200 -P 4 sha256sum 2>/dev/null"

    with raw_file.open("w", encoding="utf-8", newline="\n") as sink:
        result = guest.stream(cmd, timeout=7200, chunk_cb=sink.write)
        if result.exit_code != 0:
            raise GuestError(f"hashing failed rc={result.exit_code}")

    manifest = parse_manifest_text(raw_file.read_text(encoding="utf-8", errors="replace"))
    return manifest


def load(path: Path) -> Manifest:
    return parse_manifest_text(path.read_text(encoding="utf-8", errors="replace"))


def diff(before: Manifest, after: Manifest) -> ManifestDiff:
    b, a = before.entries, after.entries
    return ManifestDiff(
        changed=sorted(p for p in (b.keys() & a.keys()) if b[p] != a[p]),
        removed=sorted(b.keys() - a.keys()),
        added=sorted(a.keys() - b.keys()),
    )
