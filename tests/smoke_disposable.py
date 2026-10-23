r"""Explicit integration check. Creates/mutates ONLY its own fresh project directory.

Run: uv run python tests\smoke_disposable.py
Requires Docker Desktop/Linux NBD and VMware's disk utility. Does not boot a VM.
Failure retains the disposable fixture for diagnosis; success removes it.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from vmdk_shrink.compact import compact_disk
from vmdk_shrink.manifest import diff
from vmdk_shrink.prerequisites import require_prerequisites
from vmdk_shrink.session import attach_ro, attach_rw, hash_all, open_session
from vmdk_shrink.shrink import shrink
from vmdk_shrink.vmdk_parser import grain_stats


def main() -> None:
    require_prerequisites("shrink")
    root = Path(
        tempfile.mkdtemp(prefix=".integration-vmdk-", dir=Path(__file__).resolve().parents[1])
    )
    descriptor = root / "data.vmdk"
    print(f"Disposable integration fixture: {root}")
    try:
        with open_session(root) as session:
            ex = session.executor
            ex.run("qemu-img create -f vmdk -o subformat=twoGbMaxExtentSparse /work/data.vmdk 3G")
            disk = attach_rw(session, descriptor)
            ex.run(
                f"sgdisk -o -n 1:2048:+2500M -t 1:8300 -c '1:Integration data' "
                f"--attributes=1:set:2 {disk.device}"
            )
            session.nbd.rescan(disk)
            ex.run(f"mkfs.ext4 -F {disk.partition(1)}", timeout=120)
            ex.run(f"mkdir -p /mnt/fixture; mount {disk.partition(1)} /mnt/fixture")
            try:
                ex.run(
                    "set -e; cd /mnt/fixture; "
                    "printf 'payload' > normal; "
                    "printf 'newline' > $'line\\nbreak'; "
                    "printf 'backslash' > 'back\\slash'; "
                    "printf 'space' > 'trailing '; "
                    "printf 'carriage' > $'carriage\\rreturn'; "
                    "printf 'raw' > $'raw\\xff'; "
                    "dd if=/dev/urandom of=removed bs=1M count=16 status=none; sync; "
                    "rm removed; sync"
                )
            finally:
                ex.run("umount /mnt/fixture")
            before_table = json.loads(ex.run(f"sfdisk --json {disk.device}").output)
            session.nbd.disconnect()
            baseline = hash_all(session, descriptor, out_path=root / "initial", replicas=2)
        result = compact_disk(descriptor)
        assert result.disks == (descriptor,)
        assert shrink(root, 1, data_vmdk=descriptor, replicas=2) == 0
        assert grain_stats(descriptor).capacity_bytes < 3 * 2**30
        with open_session(root) as session:
            disk = attach_ro(session, descriptor, index=0)
            after_table = json.loads(session.executor.run(f"sfdisk --json {disk.device}").output)
            session.nbd.disconnect()
            final = hash_all(session, descriptor, out_path=root / "final", replicas=2)
        before = before_table["partitiontable"]["partitions"][0]
        after = after_table["partitiontable"]["partitions"][0]
        for field in ("start", "type", "uuid", "name", "attrs"):
            assert before.get(field) == after.get(field), field
        assert after["size"] < before["size"]
        assert diff(baseline, final).identical
        assert len(final) == 6
        print("PASS: compact + shrink, unusual filenames, final contents and GPT identity verified")
    except BaseException:
        print(f"FAILED; disposable evidence retained at {root}")
        raise
    else:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
