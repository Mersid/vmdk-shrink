from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from vmdk_shrink.executor import ExecError, ExecResult
from vmdk_shrink.manifest import collect, load, parse_manifest_text

HASH = "a" * 64


@pytest.mark.parametrize(
    "name", [" leading", "trailing ", "line\nbreak", "carriage\rreturn", "back\\slash", "*star"]
)
def test_null_records_preserve_names(name: str) -> None:
    manifest = parse_manifest_text(f"{HASH}  /mnt/data/{name}\0")
    assert manifest.entries == {f"/{name}": HASH}


def test_mount_prefix_requires_path_boundary() -> None:
    assert parse_manifest_text(f"{HASH}  /mnt/database/a\n").entries == {"/mnt/database/a": HASH}
    assert parse_manifest_text(
        f"{HASH}  /mnt/data-other/a\n", strip_prefix="/mnt/data"
    ).entries == {"/mnt/data-other/a": HASH}


@pytest.mark.parametrize(
    "text", [f"{HASH}  /a\0junk", f"{HASH}  /a\n{HASH}  /a\n", f"\\{HASH}  /a\\nb\n"]
)
def test_rejects_truncation_duplicates_and_escaped_legacy(text: str) -> None:
    with pytest.raises(ExecError):
        parse_manifest_text(text)


def test_load_preserves_invalid_utf8_and_crlf_filename(tmp_path: Path) -> None:
    path = tmp_path / "manifest.raw"
    path.write_bytes(HASH.encode() + b"  /mnt/data/\xff\r\n \0")
    assert load(path).entries == {"/\udcff\r\n ": HASH}


class HashExecutor:
    def __init__(self, payload: bytes, *, count: str = "1", code: int = 0) -> None:
        self.payload = payload
        self.count = count
        self.code = code
        self.commands: list[str] = []

    def run(self, command: str, *, timeout: float = 120, check: bool = True) -> ExecResult:
        self.commands.append(command)
        return ExecResult(command, 0, self.count)

    def stream(self, command: str, *, timeout: float = 7200, chunk_cb: object = None) -> ExecResult:
        self.commands.append(command)
        encoded = base64.encodebytes(self.payload).decode("ascii")
        assert callable(chunk_cb)
        for i in range(0, len(encoded), 7):
            chunk_cb(encoded[i : i + 7])
        return ExecResult(command, self.code, encoded)


def test_collect_transports_bytes_and_uses_null_safe_tools(tmp_path: Path) -> None:
    payload = HASH.encode() + b"  /mnt/data/\xff\n \0"
    ex = HashExecutor(payload)
    manifest = collect(ex, out_path=tmp_path / "m.txt", extra_mounts=("/mnt/data1",))
    assert manifest.entries == {"/\udcff\n ": HASH}
    assert (tmp_path / "m.raw").read_bytes() == payload
    assert all("-print0" in cmd for cmd in ex.commands)
    assert "sha256sum -z" in ex.commands[-1]
    assert "base64" in ex.commands[-1]
    assert "pipefail" in ex.commands[-1]
    assert "awk" not in ex.commands[-1]


def test_collect_runs_shards_in_isolated_background_jobs(tmp_path: Path) -> None:
    ex = HashExecutor(f"{HASH}  /mnt/data/a\0".encode())
    collect(ex, out_path=tmp_path / "m", extra_mounts=("/mnt/data1",))
    script = shlex.split(ex.commands[-1])[-1]
    assert "mktemp -d" in script
    assert "trap" in script and "EXIT" in script
    assert script.count('pids+=("$!")') == 2
    assert 'wait "$pid"' in script
    assert script.index('wait "$pid"') < script.index('cat -- "${outputs[@]}"')
    assert '"$tmpdir/0.raw"' in script
    assert '"$tmpdir/1.raw"' in script


@pytest.mark.parametrize("failure", ["", "hash", "find", "sort", "cat"])
def test_hash_shell_waits_checks_status_and_cleans_up(tmp_path: Path, failure: str) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the hashing shell integration test")
    ex = HashExecutor(f"{HASH}  /mnt/data/a\0".encode())
    collect(ex, out_path=tmp_path / "m", extra_mounts=("/mnt/data1",))
    script = shlex.split(ex.commands[-1])[-1]
    # Fixtures replace filesystem reads, not the shell's background jobs, waits,
    # redirections, pipefail handling, temporary files, or base64 transport.
    harness = r"""
set -o pipefail
root=$(command mktemp -d) || exit 99
trap 'command rm -rf -- "$root"' EXIT
mktemp() { command mktemp -d "$root/shards.XXXXXX"; }
find() {
    printf '%s\0' "$1/a" "$1/b"
    if [[ $failure == find && $1 == /mnt/data ]]; then return 17; fi
}
sort() {
    command sort "$@"
    if [[ $failure == sort ]]; then return 18; fi
}
xargs() {
    local files=() file shard i
    mapfile -d '' -t files
    ((${#files[@]})) || return 0
    shard=${files[0]##*/}
    touch "$root/$shard.started"
    # A serial implementation cannot pass this rendezvous.
    for ((i=0; i<100; i++)); do
        [[ -f $root/a.started && -f $root/b.started ]] && break
        sleep 0.02
    done
    [[ -f $root/a.started && -f $root/b.started ]] || return 19
    if [[ $shard == b ]]; then sleep 0.1; fi
    touch "$root/$shard.done"
    if [[ $failure == hash && $shard == a ]]; then return 20; fi
    for file in "${files[@]}"; do printf '%s  %s\0' "$digest" "$file"; done
}
cat() {
    if [[ $failure == cat ]]; then return 21; fi
    command cat "$@"
}
(
__SCRIPT__
)
status=$?
[[ -f $root/a.done && -f $root/b.done ]] || exit 97
if compgen -G "$root/shards.*" >/dev/null; then exit 98; fi
exit "$status"
""".replace("__SCRIPT__", script)
    env = dict(os.environ, failure=failure, digest=HASH)
    env["PATH"] = str(Path(bash).parent) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        [bash, "-c", harness], capture_output=True, timeout=15, env=env, check=False
    )
    assert result.returncode not in (97, 98), result.stderr
    if failure:
        assert result.returncode != 0, result.stderr
        assert result.stdout == b""
    else:
        assert result.returncode == 0, result.stderr
        raw = base64.b64decode(result.stdout, validate=False).decode()
        assert parse_manifest_text(raw).entries == {"/a": HASH, "/b": HASH}


@pytest.mark.parametrize("count,code", [("not a count", 0), ("2", 0), ("1", 1)])
def test_collect_rejects_bad_count_coverage_and_hash_errors(
    tmp_path: Path, count: str, code: int
) -> None:
    ex = HashExecutor(f"{HASH}  /mnt/data/a\0".encode(), count=count, code=code)
    with pytest.raises(ExecError):
        collect(ex, out_path=tmp_path / "m.txt")


def test_legacy_crlf_is_rejected_as_ambiguous() -> None:
    with pytest.raises(ExecError, match="ambiguous"):
        parse_manifest_text(f"{HASH}  /a\r\n")


def test_unterminated_manifest_is_rejected() -> None:
    with pytest.raises(ExecError, match="unterminated"):
        parse_manifest_text(f"{HASH}  /a")


def test_progress_counts_null_records_not_filename_newlines(tmp_path: Path) -> None:
    class Progress:
        def __init__(self) -> None:
            self.total = 0
            self.done = 0
            self.finished = False

        def start_files(self, phase: str, total: int) -> None:
            self.total = total

        def bump_files(self, count: int) -> None:
            self.done += count

        def finish(self) -> None:
            self.finished = True

    progress = Progress()
    payload = f"{HASH}  /mnt/data/a\n\n\0{HASH}  /mnt/data/b\0".encode()
    collect(HashExecutor(payload, count="2"), out_path=tmp_path / "m", bar=progress)
    assert progress.total == progress.done == 2
    assert progress.finished
