---
name: dungeon-navigate
description: Find a module, SKLT object and declared source or GLIF links in an existing repository. Works in SKLT_ONLY and MANAGED; use for structural navigation and impact questions.
---

# Navigate a SKLT project

Call `dungeon_project_probe` with the intended absolute `project_root`, and keep that root on every call. `dungeon_connect_project` is a compatibility alias. `NOT_DUNGEON` gets actionable adoption steps; `INVALID_MANAGED` needs configuration repair; `UNAVAILABLE` needs a corrected accessible root.

In `SKLT_ONLY` or `MANAGED`, narrow context through `dungeon_project_summary` → `dungeon_list_modules` → `dungeon_list_objects` → `dungeon_inspect_object`. Follow pagination offsets instead of requesting every Skeleton. Inspect reports the identity origin: explicit IDs stay intact; qualified declared symbols come from SKLT text and are never derived from pathname. Duplicates must be resolved before relying on an identity. File-level fallback remains `dungeon_list_sklt` / `dungeon_read_sklt`.

Treat every `object_id` as a logical value, never as a filesystem path. Pass the exact parser-returned ID—including nested qualified declarations—unchanged to object-aware calls; do not split, normalize, or derive it from a filename.

Read one SKLT before source inspection, then follow its declared dependencies/source refs and only GLIF linked by `object_id`. Request `include_glif: true` for relevant UI. `dungeon_source_link_audit` checks link freshness; semantic correspondence may remain `UNKNOWN`. Follow the selected project's external UI guide for actual behavior.

Report root, mode and relevant object IDs. Read Task Trace and separate Prototype/Product coverage when implementation readiness matters. Missing evidence remains unknown. Browsing never requires adoption or index regeneration.
