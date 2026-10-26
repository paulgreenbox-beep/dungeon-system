---
name: dungeon-task-trace
description: Link a development task to SKLT objects, source files, tests and execution evidence, and record separate Prototype/Product coverage. Use for tracking actual implementation or verification, without scheduling agents.
---

# Trace implementation

Do not mark coverage `VERIFIED` from runtime-002's local base or CAS integrity: the Windows receipt is unsealed and typed Scion proof remains unsupported. The A-C Linux result is bounded to that fixture; check installed-session activation separately in the runtime report.

Probe and inspect the selected object before recording links. Use `dungeon_task_trace_get` / `dungeon_task_trace_list` to reuse an existing task. A write needs the explicit writable root and an authorized task; `assigned_agent` is only an external reference, not a new role or dispatch action. Record known source/test refs, required checks, performed checks, evidence refs and confirmed result. Use the returned hash as `expected_hash` when updating an existing trace.

Every write requires the caller-supplied exact absolute physical root to match an approved allowlist entry. The default allowlist is `[]`; no write inherits the default checkout. Read-only calls remain cross-platform, but writes fail closed where the Windows core writer lease is unavailable. Reject original UNC paths, `..`, and junction/reparse traversal before normalization.

Use `dungeon_source_link_audit` to check declared files and recorded hashes. Existing bytes do not establish semantic correspondence; unknown symbols remain `UNKNOWN`.

For new proof-capable traces, use typed required-check identity (`id`, `type`, `scope`), keep it consistent with object/task/coverage scope, and record each performed check as `PASS`; use task `status: PASS` and `actual_result: PASS`. The proof evaluator retains successful legacy task-status markers, but never accepts a missing/non-passing actual result or a failed performed check. Proof must match the captured structured result and supported raw bindings; see the [v0.4.1 proof adapter](../../references/proof-adapters-v041.md).

Record Prototype and Product independently with `dungeon_coverage_set`. Use `UNKNOWN` for insufficient data, `NOT_STARTED` only with a justified absence observation, and `PARTIAL` for established partial work. Code present does not justify `VERIFIED`. That state requires applicable immutable evidence, object/task linkage, current implementation identity and EXE SHA-256 when testing an executable. Re-read `dungeon_coverage_get` after source/build changes and resolve mismatches. Coverage never changes SKLT/RARE or EXP/grade.

Immutable CAS/seals and current proof status are separate: proof states are `VERIFIED`, `INCOMPLETE`, `CONFLICT`, `STALE`, and `INVALID`. Unknown formats, missing raw receipts, or unaccounted additional artifact refs are `PROOF_UNVERIFIED`, not `VERIFIED`. New evidence captures the full SKLT obligation hash; SKLT edits stale current proof while old manifests and seals remain unchanged. Prototype and Product remain independent.

For typed source revisions and the VersionScope receipt boundary, see the [Scion reference](../../references/scion.md). Do not treat a typed Scion revision or local base as current proof.
