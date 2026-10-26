# Local fixture archive — 2026-10-17

## Why

At the user's request, completed disposable hardening-v0.4.1 repro workspaces
were separated from active project material. The six selected runs contained
repeated test payloads and were not referenced by a current launcher using
their fixed timestamp paths. A Windows process command-line check found no
matching active replay; this was not an exhaustive open-handle inspection.

## What changed

Six directories were relocated from `artifacts/hardening-v041/` to the local,
Git-ignored `archive/2026-10-17/`, keeping their original basenames:

- `original-repros-20261012T121008-0d7e1e9c`
- `original-repros-20261012T113334-38cf1d38`
- `original-repros-20261012T120054-4556b72c`
- `original-repros-20261012T102541-9f13f767`
- `original-repros-20261012T102835-73a54987`
- `original-repros-20261012T102452-87bfadf8`

The retained inventory is 2,231 regular files, 1,348,821,446 logical bytes and
nine junctions. Every regular file's SHA-256, size and modification time, plus
directory entries and reparse metadata, matched before and after relocation.
The local `manifest.before.json` records original/new paths and the complete
inventory; `receipt.json` records the completed verification. The archive
README documents restoration.

For this same-volume housekeeping operation, whole-directory renames avoided
a copy/delete data-loss window. No recursive deletion, compression or fixture
content rewriting was performed. Moving within the same drive reclaims zero
bytes; physical allocation and potential compression savings were not measured.

## Preservation and limits

Junctions were not traversed or rewritten. Their absolute targets and frozen
reports still refer to the original paths. Restore a complete run to its old
location before replay; the archive is cold storage, not a runnable fixture
root. Archived bytes remain retained pending independent re-audit or a separate
explicit retention decision. This relocation does not authorize their deletion.

Current source, MCP/plugin files, plugin backups, the canonical original audit
packet, summaries, test dependencies and unique Scion runtime receipts remain
in place. External projects, installed plugin caches, WSL and DungeonStore were
not changed. Existing user worktree edits were preserved.

Initial post-move validation detected historical fixture SKLT under the new
top-level archive directory. The canonical `.project/project.json` ignore list
was extended with `archive`, matching the existing exclusion of `artifacts`.
Derived projections are regenerated through the normal CLI, never patched by
hand. The private archive date directory is also excluded from Git.

SKLT IDs, modules, dependencies, coverage and architecture remain unchanged.
This is a local storage housekeeping record, not verification of the modeled
Dungeon archive component or runtime/release acceptance.

## Final checks

- All six archived inventories passed a separate full SHA-256/link-metadata
  verification after relocation.
- `exp` and `index` completed successfully; structural validation is PASS with
  zero errors, two modules and 22 objects.
- The optional `local-fixture-archive-2026-10-17` snapshot attempt failed with
  Windows access denied (`PUBLISH_FAILED`, WinError 5). No valid new snapshot
  was created; the previous valid snapshot remains selected. Validation has
  one `PROJECT_CHANGED_SINCE_SNAPSHOT` warning for the changed ignore list.
- The failed attempt's residue consisted only of two empty directories. These
  were moved without deletion to the private archive's
  `snapshot-failed-attempt/`, with a separate verified inventory record.
