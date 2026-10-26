# Version-control backends

**Status: PARTIAL / EXPERIMENTAL.** Git remains the default. Scion is opt-in, unreleased, and not independently verified. In addition to mock coverage, the bounded Linux/TestCluster runtime gate passed; this does not establish production readiness or Windows/WSL transport support. See the [runtime check](integrations/scion-runtime-check.md).

This page describes the shared VersionScope model. For the pinned upstream behavior, local environment, and Scion operation limits, see [Scion integration](integrations/scion.md).

## VersionScope and compatibility

A VersionScope selects the source-history backend for a project-relative root. It is independent of Dungeon structure: a Level may share a scope with other Levels, and changing a backend does not change LevelId, ObjectId, SKLT identity, GLIF object_id, Cronica references, EXP, or grade.

Existing git_scopes remain valid and are not renamed or rewritten. SKLT git_scope values remain compatibility pointers to stable scope IDs. Dungeon retains the implicit Git scope `main` only when neither `git_scopes` nor `version_scopes` explicitly declares a scope with ID `main`. Git stays the fallback for legacy manifests only; an unavailable declared backend is reported as unavailable or partial and is never silently switched.

The optional version_scopes entry is additive. Its accepted fields are id, backend, root, repository, mode, default_ref, and endpoint_profile. Unknown fields are invalid. Root and repository must be project-relative local paths; credentials, command strings, and external roots are not manifest fields. Existing git_scopes may remain beside version_scopes.

~~~json
{
  "version_scopes": [
    {
      "id": "scion-lab",
      "backend": "SCION",
      "root": "lab",
      "repository": "lab",
      "mode": "MONOREPO",
      "default_ref": "main",
      "endpoint_profile": "LAB"
    }
  ]
}
~~~

This is a documentation example only; the project manifest is unchanged by this documentation work.

## Current query surface

The CLI exposes bounded queries and plans:

~~~powershell
python tools/project-structure.py vcs-status --scope scion-lab --root "<PROJECT_ROOT>" --json
python tools/project-structure.py vcs-inspect --scope scion-lab --revision "<REVISION>" --root "<PROJECT_ROOT>" --json
python tools/project-structure.py vcs-history --scope scion-lab --revision main --limit 20 --root "<PROJECT_ROOT>" --json
python tools/project-structure.py vcs-diff --scope scion-lab --root "<PROJECT_ROOT>" --json
python tools/project-structure.py vcs-sparse-plan --scope scion-lab --object-id "<OBJECT_ID>" --root "<PROJECT_ROOT>" --json
python tools/project-structure.py vcs-migration-plan --target-backend SCION --root "<PROJECT_ROOT>" --json
~~~

The read-only MCP surface adds four tools while preserving the original 30 names: dungeon_vcs_status, dungeon_vcs_inspect, dungeon_vcs_history, and dungeon_vcs_diff. These tools inspect declared scopes; they do not initialize a repository, log in, create a checkout, or mutate a remote ref.

| Backend operation | Git | Scion in this experimental adapter |
| --- | --- | --- |
| Status | Supported bounded observation | Partial; local base only, dirty state UNKNOWN. Upstream status is not run. |
| Inspect | Supported | Bounded show query using Scion CLI JSON v1; local base is not proof of the current remote ref. |
| History | Supported, bounded | Bounded log query using Scion CLI JSON v1; local base is not proof of the current remote ref. |
| Diff | Supported by Git adapter | Unsupported as a safe read-only query; upstream working-copy access may write or recover local metadata. |
| Sparse plan | Declared-source selection | Same plan-only behavior; no transitive closure and no checkout. |
| Migration plan | Read-only impact inventory | Read-only impact inventory; it does not import, migrate, or rewrite references. |

For Scion, inspect/history can return useful server query data while the enclosing observation remains PARTIAL and dirty_state remains UNKNOWN. A missing or stale local base is never replaced with a guessed current revision. An unavailable endpoint/client does not change Git behavior.

Sparse plans list only exact source paths declared for the selected ObjectID, after checking scope containment. They do not compute transitive dependencies and do not create or modify a working copy. Migration plans report affected objects/levels and references to review; they do not execute Git import or alter source, snapshots, or evidence.

## Typed revisions, snapshots, and evidence

Every source revision is namespaced by backend and scope. A Scion changeset ID is a BLAKE3 identifier in the c1 plus 64 lowercase hexadecimal digits form. It is not a Git object ID, even where Git uses a 64-character hash, and it is not a Dungeon CAS SHA-256 reference.

Snapshot manifests keep their existing schema_version 1 format. The optional additive extension is revisions_schema: 1 plus revisions keyed by scope ID, where each value is a typed revision record. Old schema-1 snapshots without these fields remain valid. Observations across scopes are sequential and do not promise cross-backend atomicity.

Evidence may add a top-level typed source_revision with schema_version 1 and a scopes map. This is optional for old Git-only evidence; existing Git provenance remains readable. For typed source provenance, each explicitly captured source file is hashed and stored independently in Dungeon CAS as SHA-256 input, so local file capture is not inferred from a clean server base.

Legacy evidence for the root `main` Git scope remains on the existing Git receipt path and is still accepted even when a Scion scope is configured elsewhere; that configuration does not convert legacy Git receipts into typed source revisions. Typed Scion or other nonlegacy VersionScope receipts fail closed with `VCS_PROOF_ADAPTER_UNSUPPORTED` until a compatible receipt adapter is implemented. Typed provenance does not itself establish current proof, and UNKNOWN Scion revision or dirty state cannot be promoted to VERIFIED. CAS integrity, source-file capture, revision identity, and current proof remain separate claims.

## Safety and release boundary

No cross-backend transaction is implied. Dungeon snapshots record coordinated per-scope observations, not one physical atomic commit. Scion's object store remains separate from Dungeon CAS, which continues to hold execution artifacts and evidence bytes.

The optional integration is scoped to the pinned Scion revision and platform limits in [the Scion guide](integrations/scion.md). Phase 3 sparse working-copy execution and Phase 4 commit/land mutations are unsupported. No auto-install, server/daemon startup, checkout, credential login, import, commit, or land occurs during ordinary Dungeon inspection.

The `0.4.2-rc1` candidate declares 34 stdio tools: the 30-tool public `v0.4.1-rc1` baseline plus four VCS queries, with six skills. The recorded runtime-002 regression passed 323 of 325 cases; two Windows cases were skipped, with no failures/errors and inputs unchanged. Installed-launcher discovery and a fresh Codex CLI session separately confirmed 34 tools and actual VCS calls. Existing chats can retain an older catalog; the existing tag still has 30. See the [activation report](integrations/scion-runtime-002-report.md). Publishing this experimental source on `main` does not create a new Release/tag or establish independent acceptance.
