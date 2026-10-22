"""File-hash manifests: collected in the container, diffed on the host.

The manifest is the lossless proof: every regular file's SHA-256 before and
after the operation, diffed at the end.

A single qemu-nbd connection caps around 100 MB/s (latency-bound through the
bind mount), so hashing fans out over several read-only NBD replicas of the
same VMDK — the bind-mount ceiling (~230 MB/s) is reached at 4 connections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .executor import ExecError, StreamExecutor
from .progress import PhaseBar

MOUNT = "/mnt/data"
_HASH_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(.+)$")
_MOUNT_PREFIX = re.compile(r"^/mnt/data\d*")


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


def parse_manifest_text(text: str, *, strip_prefix: str | None = None) -> Manifest:
    """Parse ``sha256sum`` output; normalize ``/mnt/data*`` mount prefixes.

    Parallel hashing uses several mounts (``/mnt/data0``, ``/mnt/data1``…) of
    the same filesystem; paths are stored without the mount prefix so the two
    manifests are comparable.
    """
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
        else:
            path = _MOUNT_PREFIX.sub("", path, count=1)
        entries[path] = digest.lower()
    if malformed:
        raise ExecError(f"manifest parse: {malformed} malformed lines")
    return Manifest(entries=entries)


def collect(
    ex: StreamExecutor,
    *,
    mount: str = MOUNT,
    out_path: Path,
    extra_mounts: tuple[str, ...] = (),
    bar: PhaseBar | None = None,
) -> Manifest:
    """Hash every regular file under ``mount`` (plus RO replicas), stream to host.

    ``extra_mounts`` are additional read-only mounts of the SAME filesystem;
    the file list is split round-robin across all mounts so hashing saturates
    the bind-mount throughput instead of one NBD connection.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_file = out_path.with_suffix(".raw")

    mounts = [mount, *extra_mounts]
    if len(mounts) == 1:
        cmd = f"find {mount} -xdev -type f | xargs -d '\\n' -n 200 -P 4 sha256sum 2>/dev/null"
    else:
        # round-robin the file list across replicas; newline-delimited (the
        # manifest is line-based, and mawk cannot use NUL as RS).
        parts = []
        for i, m in enumerate(mounts):
            parts.append(
                f"find {m} -xdev -type f | awk 'NR % {len(mounts)} == {i}' "
                f"| xargs -d '\\n' -n 200 -P 4 sha256sum 2>/dev/null &"
            )
        cmd = " ".join(parts) + " wait"

    # coverage guard: the round-robin split must not silently drop files
    count = ex.run(f"find {mount} -xdev -type f | wc -l", timeout=3600).output.strip()
    expected = int(count) if count.isdigit() else -1

    if bar is not None and expected >= 0:
        bar.start_files("hashing", expected)

    def _on_chunk(chunk: str) -> object:
        sink.write(chunk)
        done = chunk.count("\n")
        if done and bar is not None:
            bar.bump_files(done)
        return None

    with raw_file.open("w", encoding="utf-8", newline="\n") as sink:
        result = ex.stream(cmd, timeout=14400, chunk_cb=_on_chunk)
        if result.exit_code != 0:
            raise ExecError(f"hashing failed rc={result.exit_code}")
    if bar is not None:
        bar.finish()

    manifest = parse_manifest_text(raw_file.read_text(encoding="utf-8", errors="replace"))
    if expected >= 0 and len(manifest) != expected:
        raise ExecError(
            f"manifest coverage mismatch: hashed {len(manifest)} files, filesystem has {expected}"
        )
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
