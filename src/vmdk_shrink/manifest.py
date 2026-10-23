"""File-hash manifests: collected in the container, diffed on the host.

The manifest compares every regular file's SHA-256 before and after the
operation. It does not verify filesystem metadata or establish crash safety.

NUL records retain arbitrary filename bytes. ASCII transport encoding keeps
the manifest safe through both container and SSH text executor interfaces.
"""

from __future__ import annotations

import base64
import binascii
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .executor import ExecError, StreamExecutor
from .progress import PhaseBar

MOUNT = "/mnt/data"
_HASH_LINE = re.compile(r"([0-9a-fA-F]{64}) [ *](.+)", re.DOTALL)
_MOUNT_PREFIX = re.compile(r"^/mnt/data[0-9]*(?=/)")


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


def parse_manifest_text(
    text: str, *, strip_prefix: str | None = None, mount_prefixes: tuple[str, ...] | None = None
) -> Manifest:
    """Parse NUL-delimited sha256sum output, or unescaped legacy lines.

    Parallel hashing uses several mounts (``/mnt/data0``, ``/mnt/data1``…) of
    the same filesystem; paths are stored without the mount prefix so the two
    manifests are comparable.
    Names are never stripped or newline-normalized. Legacy escaped records
    are rejected rather than silently compared under corrupted filenames.
    """
    entries: dict[str, str] = {}
    null_format = "\0" in text
    if null_format and not text.endswith("\0"):
        raise ExecError("manifest parse: truncated NUL record")
    if text and not null_format and not text.endswith("\n"):
        raise ExecError("manifest parse: unterminated legacy record or truncated NUL manifest")
    records = text.split("\0")[:-1] if null_format else text.split("\n")
    for line in records:
        if not line and not null_format:
            continue
        if not null_format and (line.startswith("\\") or "\r" in line):
            raise ExecError(
                "manifest parse: ambiguous escaped/CR legacy record; recollect baseline"
            )
        m = _HASH_LINE.fullmatch(line)
        if not m:
            raise ExecError("manifest parse: malformed record (legacy escapes are unsupported)")
        digest, path = m.groups()
        prefixes = mount_prefixes if mount_prefixes is not None else (strip_prefix or "",)
        prefix = next(
            (p.rstrip("/") for p in prefixes if p and path.startswith(p.rstrip("/") + "/")),
            None,
        )
        if prefix is not None:
            path = path[len(prefix) :]
        elif mount_prefixes is None:
            path = _MOUNT_PREFIX.sub("", path, count=1)
        if path in entries:
            raise ExecError(f"manifest parse: duplicate normalized path {path!r}")
        entries[path] = digest.lower()
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
    the sorted file list is split round-robin across mounts. Shards hash
    concurrently into separate temporary raw files, requiring guest temporary
    space for the entire manifest. All jobs must succeed before their outputs
    are concatenated, preventing interleaved records or partial success.
    File progress measures the final transfer, not live hashing throughput;
    no progress updates arrive while the shard jobs are hashing.
    Base64 protects filename bytes even with a lossy text-only SSH executor.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_file = out_path.with_suffix(".raw")

    mounts = [mount, *extra_mounts]
    parts = []
    for i, m in enumerate(mounts):
        select = ""
        if len(mounts) > 1:
            select = (
                "LC_ALL=C sort -z | (index=0; while IFS= read -r -d '' path; do "
                f'if (( index % {len(mounts)} == {i} )); then printf "%s\\0" "$path"; fi; '
                "index=$((index + 1)); done) | "
            )
        parts.append(
            f"(find {shlex.quote(m)} -xdev -type f -print0 | "
            f'{select}xargs -0 -r -n 200 sha256sum -z) > "$tmpdir/{i}.raw" & '
            f'pids+=("$!"); outputs+=("$tmpdir/{i}.raw")'
        )
    script = (
        "set -e; tmpdir=$(mktemp -d); trap 'rm -rf -- \"$tmpdir\"' EXIT; "
        "pids=(); outputs=(); " + "; ".join(parts) + '; status=0; for pid in "${pids[@]}"; do '
        'if wait "$pid"; then :; else status=1; fi; done; '
        'if (( status != 0 )); then exit "$status"; fi; '
        'cat -- "${outputs[@]}" | base64'
    )
    cmd = "bash -o pipefail -c " + shlex.quote(script)

    # coverage guard: the round-robin split must not silently drop files
    count_script = f"find {shlex.quote(mount)} -xdev -type f -print0 | tr -cd '\\0' | wc -c"
    count = ex.run("bash -o pipefail -c " + shlex.quote(count_script), timeout=3600).output.strip()
    if not count.isascii() or not count.isdigit():
        raise ExecError("manifest coverage: invalid file count")
    expected = int(count)

    if bar is not None and expected >= 0:
        bar.start_files("hashing", expected)

    pending = ""

    def _on_chunk(chunk: str) -> object:
        nonlocal pending
        pending += chunk.replace("\n", "").replace("\r", "")
        end = len(pending) // 4 * 4
        try:
            decoded = base64.b64decode(pending[:end], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ExecError("hashing: invalid base64 transport") from exc
        pending = pending[end:]
        sink.write(decoded)
        done = decoded.count(b"\0")
        if done and bar is not None:
            bar.bump_files(done)
        return None

    with raw_file.open("wb") as sink:
        result = ex.stream(cmd, timeout=14400, chunk_cb=_on_chunk)
        if result.exit_code != 0:
            raise ExecError(f"hashing failed rc={result.exit_code}")
        if pending:
            raise ExecError("hashing: truncated base64 transport")
    if bar is not None:
        bar.finish()

    manifest = parse_manifest_text(
        raw_file.read_bytes().decode("utf-8", errors="surrogateescape"),
        mount_prefixes=tuple(mounts),
    )
    if expected >= 0 and len(manifest) != expected:
        raise ExecError(
            f"manifest coverage mismatch: hashed {len(manifest)} files, filesystem has {expected}"
        )
    return manifest


def load(path: Path) -> Manifest:
    return parse_manifest_text(path.read_bytes().decode("utf-8", errors="surrogateescape"))


def diff(before: Manifest, after: Manifest) -> ManifestDiff:
    b, a = before.entries, after.entries
    return ManifestDiff(
        changed=sorted(p for p in (b.keys() & a.keys()) if b[p] != a[p]),
        removed=sorted(b.keys() - a.keys()),
        added=sorted(a.keys() - b.keys()),
    )
