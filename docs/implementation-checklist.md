# Safety and CLI improvement checklist

This tracks the source-review findings. Checked items have code/tests or an explicit
documented decision; it is not a claim of crash safety or a substitute for backups.

## Safety and correctness

- [x] Durable NBD writes; no automatic retries after interrupted filesystem surgery.
- [x] Verify filesystem surgery before host edits; final hashes after compaction,
      orphan deletion only after matching final hashes. Retain evidence on failure.
- [x] Preserve descriptor metadata; reject snapshots and unsupported layouts.
- [x] Preserve GPT partition identity, type, name, and attributes; validate before resizing.
- [x] Structured compaction result and failing CLI status on refusal/errors.
- [x] Atomic per-disk checkpoints bound to identity, target, manifest, and stage.
- [x] Silent-process streaming deadlines for Docker, SSH, and VMware compaction.
- [x] Filename-safe manifest enumeration, transport, parsing, and persistence.
- [x] Unequal extent geometry and conservative local orphan ownership checks.
- [x] Singleton free-block lists.
- [x] Validate writable sparse formats and synchronize redundant grain tables.
- [x] Shared descriptor-based disk discovery and footprint accounting; ambiguous selection fails.
- [x] Narrow failure handling; fail closed on unsafe lifecycle state. Atomic Docker-daemon
      ownership reservation serializes kernel-global NBD work; no automatic stale-container sweep.
- [x] Sidecar NBD stride, verified shutdown, lifecycle/image locks, and explicit guest tools.
- [x] Custom Dockerfile selection and installed entry-point initialization.

## Architecture, dependencies, and UX

- [x] Command-specific `doctor` checks and documented transport/dependency decision.
- [x] Focused safety/checkpoint/discovery/display modules; VMware adapter emits progress
      values instead of rendering. Shared host streaming deadline implementation.
- [x] Standardized help, validated choices, confirmation, no-color, JSON and error status.
- [x] Review all three video references; restrained cohesive CLI presentation.
- [x] Restore original Rich bar style by preference, replacing the whole-cell gap
      workaround; regression-test both displays. Retain corrected progress units and
      phase-relative byte estimates. The original partial-cell gap may be visible.
- [x] Correct safety guarantees, runtime requirements, and historical documentation.
- [ ] Physical feature-package migration and complete decoupling of workflow/runtime
      progress APIs from Rich. Existing import paths intentionally retained during safety work.
- [ ] Docker-free sidecar. Current default does not boot VMware; sidecar explicitly
      opts into native VMware I/O but still uses Docker preparation/discovery.

## Validation and follow-up

- [x] Focused regressions demonstrated before fixes; full unit suite, lint/type and CLI checks.
- [x] Disposable 3 GiB disk: normal compact and NBD shrink to 1 GiB filesystem / 2 GiB
      split-sparse capacity; six unusual-filename files and GPT identity verified afterward.
      Both redundant grain-table copies exercised; one orphan deleted after final verdict.
- [ ] Live sidecar boot/shrink integration, deliberate interruption/crash recovery,
      large-disk performance, and bootability checks. Not inferred from unit results.

No user VM disks were modified during validation. Real workloads still need full
backups. In-place writes and paired grain-table updates are not crash-atomic.
Manifest shards run concurrently into separate guest temporary files to prevent
record interleaving; counts currently update during final transfer, not live hashing.
Sidecar subnet parsing still expects English Windows adapter labels. Ownership
checks are local, not a proof against every descriptor elsewhere on the computer.