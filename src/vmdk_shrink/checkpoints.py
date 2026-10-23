"""Atomic, disk-bound shrink evidence; never automatically resume mutation."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .safety import SafetyError
from .vmdk_parser import extent_list


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def disk_identity(vmdk: Path) -> dict[str, Any]:
    files = [vmdk, *(vmdk.parent / name for _, name in extent_list(vmdk))]
    file_state = []
    for path in files:
        stat = path.stat()
        file_state.append(
            {
                "name": os.path.normcase(path.name),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
                "inode": stat.st_ino,
                "device": stat.st_dev,
            }
        )
    return {
        "descriptor": os.path.normcase(str(vmdk.resolve())),
        "sha256": digest(vmdk),
        "files": file_state,
    }


@dataclass
class Checkpoint:
    path: Path
    state: dict[str, Any]

    @property
    def directory(self) -> Path:
        operation = self.state.get("operation")
        if (
            not isinstance(operation, str)
            or len(operation) != 32
            or any(c not in "0123456789abcdef" for c in operation)
        ):
            raise SafetyError("invalid checkpoint operation identifier")
        return self.path.parent / operation

    @property
    def baseline(self) -> Path:
        return self.directory / "before.raw"

    def save(self, stage: str, **fields: Any) -> None:
        self.state.update(fields, stage=stage)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.state, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def baseline_ready(self, vmdk: Path) -> None:
        self.save(
            "baseline_ready", identity=disk_identity(vmdk), baseline_sha256=digest(self.baseline)
        )


def prepare(vmdk: Path, *, target_gib: int, mode: str, hash_via: str, resume: bool) -> Checkpoint:
    key = hashlib.sha256(os.path.normcase(str(vmdk.resolve())).encode("utf-8")).hexdigest()[:16]
    root = vmdk.parent / ".vmdk-shrink" / key
    root.mkdir(parents=True, exist_ok=True)
    path = root / "checkpoint.json"
    parameters = {"target_gib": target_gib, "mode": mode, "hash_via": hash_via}
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SafetyError(f"invalid checkpoint: {path}; preserve it for recovery") from exc
        checkpoint = Checkpoint(path, state)
        if not isinstance(state, dict):
            raise SafetyError("checkpoint must be a JSON object")
        if resume:
            if state.get("version") != 1 or state.get("stage") != "baseline_ready":
                raise SafetyError(
                    "resume is allowed only before mutation; interrupted surgery needs "
                    "manual recovery from a full backup, not an automatic retry"
                )
            if state.get("parameters") != parameters or state.get("identity") != disk_identity(
                vmdk
            ):
                raise SafetyError("checkpoint disk identity or operation parameters do not match")
            if not checkpoint.baseline.is_file() or digest(checkpoint.baseline) != state.get(
                "baseline_sha256"
            ):
                raise SafetyError("baseline manifest is missing or incomplete/modified")
            return checkpoint
        if state.get("stage") not in ("complete", "baseline_pending"):
            raise SafetyError(
                f"unfinished operation recorded in {path}; use --resume only for a "
                "completed pre-mutation baseline, or recover/archive the state manually"
            )
    elif resume:
        raise SafetyError("no disk-bound checkpoint exists; legacy manifests cannot be resumed")
    checkpoint = Checkpoint(
        path,
        {
            "version": 1,
            "operation": uuid.uuid4().hex,
            "parameters": parameters,
            "identity": disk_identity(vmdk),
        },
    )
    checkpoint.directory.mkdir()
    checkpoint.save("baseline_pending")
    return checkpoint
