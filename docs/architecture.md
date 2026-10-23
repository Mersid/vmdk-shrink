# Architecture and dependency decisions

## Why retain two transports?

They are alternatives for Linux block I/O, not duplicate implementations of ext
resizing. `qemu-nbd` in Docker needs no helper guest but Windows file sharing is a
known write-performance/stall bottleneck. A VMware sidecar uses the native VMDK
stack, trading better I/O for guest provisioning/network/lifecycle complexity.
Historical measurements are in `transport-design.md` and
`hash-transport-benchmarks.md`; they are not current guarantees.

Decision: use the container throughout by default, retain sidecar as an explicit
advanced option, and have hashes follow the selected filesystem transport. The
VMware disk utility remains required for compaction; this does **not** mean a
second virtualization environment is booted on the default path. A Docker-free
sidecar would require native `qemu-img`, an alternative cloud-init preparation
mechanism and guest-based discovery/final geometry repair. That new backend is
not implemented or implied by a flag.

Removed unused TCP-NBD and seed-ISO dependencies. Added the actual `sfdisk`
package explicitly. Both transports share filesystem and manifest semantics.

## Boundaries

- Feature workflows: `shrink.py`, `compact.py`; compaction returns a structured
  `CompactionResult`, while refusal/failure raises an exception.
- Operation evidence: `checkpoints.py`; atomic persistence and strict pre-mutation
  resume policy. `safety.py` owns offline/operation gates; `discovery.py` owns disk
  selection. These modules do not render CLI widgets.
- Verification: `manifest.py`; NUL records, base64 text transport, coverage and
  symmetric content comparisons. It works with either executor.
- Disk format: `vmdk_parser.py`, `vmdk_descriptor.py`, `graintrim.py`; supported
  format invariants, metadata-preserving edits and conservative ownership checks.
- Filesystems: `fsops.py`, `reclaim.py`; external Linux-tool semantics and geometry.
- Runtime adapters: `container.py`, `nbd.py`, `sidecar/`, `vdiskmanager.py`;
  `execution.py` shares output-independent host deadlines.
- Presentation: `cli.py`, `console.py`, `progress.py`, `presentation.py`.
  Plans/confirmation stay in the CLI. Rendering uses timestamps, aligned status
  labels and restrained cyan/blue accents inspired by the supplied videos, with
  the original Rich progress bars restored by preference. The original partial-cell
  gap may therefore be visible. No full-screen framework or new Python
  dependencies were added. Errors use stderr; JSON avoids decorative output.

## Deliberately deferred

The physical feature-package migration and complete removal of Rich/progress
references from workflow/runtime APIs are not finished. Existing import paths
are retained while safety behavior changes are tested. A later behavior-neutral
move can group `shrink/{service,checkpoints}`, `compact/{service,result}`,
`verification/`, `disk_image/`, `filesystems/`, `runtime/` and `presentation/`.
Do not duplicate implementations or add a framework just to create directories.

Real disk interruption, native sidecar boot and bootability validation remain
separate integration work. In-place writes are not crash-atomic; a matched
regular-file manifest must never be marketed as a general zero-data-loss guarantee.