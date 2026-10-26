---
name: dungeon-structure
description: Make or review structural changes in a managed Dungeon project, or safely inspect a SKLT-only project's own structure. Use for SKLT, GLIF, module, dependency, level or storage changes.
---

# Change project structure

Runtime-002 fixture evidence does not authorize changing a target checkout, scope, or structure. Preserve unsupported-platform observations and unknown source state; use only the bounded read-only query contract unless a separate task explicitly authorizes a change.

Use `dungeon_project_probe`, then pass that absolute `project_root` on every MCP call. Begin with that project's status and orientation; select summary → module → object before broad inspection. All write tools need an explicit root.

Every write needs the caller-supplied exact physical root in the approved allowlist. The default is `[]` (no writable root), including for the default checkout. Reject raw UNC, `..`, and junction/reparse traversal before path normalization. Read-only inspection remains cross-platform; the core writer lease is Windows-only and writes fail closed elsewhere. Windows writes stage and roll back caught failures but are not crash-safe multi-file transactions.

For a managed Dungeon project, inspect affected objects by `object_id` and read their SKLT before broad source inspection. Preserve each object's identity across moves or renames; GLIF is linked through `object_id`. Edit only requested structural sources with normal file tools. `.exp`, `docs/.grade`, `.project/objects.json` and `.project/state.json` are derived. Run `dungeon_validate`; after structural changes, use `dungeon_refresh` only when the selected root is explicitly writable, then validate again. For a meaningful checkpoint, add Cronica and create a named snapshot for that same root. For storage or Git scope fields, use that project's policy docs.

For `SKLT_ONLY`, module/object navigation, source audit and Task Trace work before adoption. Dungeon refresh, snapshots and CAS Evidence Store require MANAGED. Follow its own SKLT guide for requested source edits. Use read-only adoption_plan and explicitly accepted adoption_apply only if managed operations are part of the task; never initialize an index just to browse.

Prototype/Product coverage and current-proof records are not structural edits. Keep the scopes independent and never change SKLT/RARE, EXP, or grade to represent coverage. Preserve parser-returned logical object IDs exactly; never use them as paths.

The MCP write tools require an explicit root and an administrator-configured writable-root allowlist. A snapshot is not a Git backup or runtime capture.

For VersionScope declarations, keep legacy `git_scopes` and SKLT `git_scope` references intact; see the [Scion reference](../../references/scion.md) for the additive experimental backend and its boundaries.
