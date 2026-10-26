---
name: dungeon-adoption
description: Connect an ordinary repository or SKLT_ONLY project to managed Dungeon operations using a read-only adoption plan and explicit apply. Use when project detection fails or the user asks to onboard a repository.
---

# Adopt an existing project

A disposable Scion/TestCluster runtime fixture is not evidence that the selected target project is adopted, current, or production-connected. Preserve the fixture boundary and separate Windows/WSL support limits.

Probe the explicit absolute root with `dungeon_project_probe`. Use `dungeon_adoption_plan` before any mutation. Review its observed Git state, existing SKLT/GLIF identities, candidate modules, conflicts, input fingerprint and unresolved issues. Source directories are observations, not inferred architecture or confirmed responsibilities.

`can_plan_adoption` is not `can_adopt`. Inspect `adoption_blockers`; named declaration dialects may be readable but need a reviewed canonical adapter before managed adoption. A repository without SKLT can receive an empty managed shell; it does not gain an invented object graph.

Apply only when the user's task explicitly includes adoption and the accepted module set is concrete. Pass the unchanged reviewed plan, accepted module IDs and the same root to `dungeon_adoption_apply`. The root must be configured writable; do not silently expand that setting. Changed inputs, duplicate IDs or conflicting existing files require a new plan or explicit repair.

All writes require the caller's exact absolute physical root to match an approved allowlist entry. The default allowlist is `[]` (no writable roots); a default checkout never supplies implicit write authority. Reject original UNC paths, `..`, and junction/reparse traversal before normalization. Read-only discovery remains cross-platform; the writer lease is Windows-only and writes fail closed elsewhere. Windows publication stages changes and rolls back caught failures, but is not crash-safe.

Check the returned managed state, validation and unresolved issues. Repeat the same accepted plan only to recover or confirm an idempotent outcome. Existing SKLT IDs and files must survive, and adoption must not create Git or invent source/API dependencies. A dialect the adapter cannot safely manage remains readable in SKLT_ONLY with actionable blockers.

Object IDs are logical identifiers, never file paths. Preserve parser-returned IDs exactly, including nested qualified declarations; do not derive identities from filenames or use them in filesystem operations.

For optional Scion VersionScopes, use the [Scion reference](../../references/scion.md). Migration plans are read-only impact inventories; do not infer or execute a Git import or migration.
