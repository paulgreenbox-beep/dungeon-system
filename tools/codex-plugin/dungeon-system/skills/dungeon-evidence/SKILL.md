---
name: dungeon-evidence
description: Capture, inspect or verify Dungeon System execution evidence and CAS hashes for a known object. Use for tool or container result provenance, not ordinary structural navigation.
---

# Preserve and check evidence

First use `dungeon_project_probe` and pass that absolute `project_root` on every call. Evidence/CAS commands require MANAGED (legacy project_mode DUNGEON). SKLT_ONLY can read/write authorized Task Trace metadata but needs explicit adoption for its CAS Evidence Store. For an existing record, use evidence_for/inspect then evidence_verify; report integrity state with manifest ID and failed hashes. CAS availability is separate from structural validation and current-build applicability.

For a new execution, first inspect the object and its SKLT. If UI or a Toolkit container is involved, read the linked GLIF and the current guide selected from `Rust_Makepad_Toolkit/docs/INDEX.md`; Dungeon System does not run the container. Capture only the chosen exact output files with `dungeon_cas_put` using absolute file paths, then check each returned reference with `dungeon_cas_verify`.

Create a draft JSON as specified in the selected project's `docs/evidence.md` with the real `object_id`, Toolkit provider/type/ID, CAS references and result. Pass its absolute path and an explicit writable `project_root` to `dungeon_evidence_create`, verify the new manifest, and use `dungeon_evidence_seal` for a meaningful record. Never capture bytes into a different project's CAS. Clean up the execution workspace only after the referenced bytes and seal are verified. A rerun gets a new evidence ID. If Git metadata is unavailable, report the manifest's `null` Git fields rather than inventing a commit.

For executable tests capture the exact EXE SHA-256 and preserve task, source and revision linkage using the documented optional build/provenance fields. Attach the immutable manifest to Task Trace, then use coverage_set only when the evidence applies to that scope and current build. A bare textual PASS or an integrity seal is insufficient for VERIFIED coverage. A mismatch requires a new corresponding test and evidence ID.

For v0.4.1 current-proof evaluation, read [the implemented adapter contract](../../references/proof-adapters-v041.md). New proof requires matching typed `id`/`type`/`scope`, Task Trace performed checks and actual result `PASS`, plus the versioned Toolkit reliability-v2 raw CAS receipt and supported bindings. Unknown/legacy native receipts, missing raw bytes, or unaccounted additional non-result artifacts fail closed as `PROOF_UNVERIFIED`; do not claim `VERIFIED` from a seal alone. The current proof state (`VERIFIED`, `INCOMPLETE`, `CONFLICT`, `STALE`, `INVALID`) is separate from immutable manifest/CAS integrity. Capture each SKLT obligation hash in new evidence; later SKLT changes make current proof stale without rewriting old sealed manifests.

Read-only evidence inspection remains cross-platform. Writes require an exact explicitly approved physical root (`[]` authorizes none) and fail closed where the Windows writer lease is unavailable. Windows staging/rollback covers caught failures, not crashes or power loss.
