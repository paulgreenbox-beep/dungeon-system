---
name: dungeon-review
description: Review structural consistency, stale source links, task evidence and verified Prototype/Product coverage in an existing Dungeon or SKLT-only project. Use for readiness reviews and linkage audits.
---

# Review structure and proof

Keep mock coverage, upstream executable smoke, the 29-check disposable Linux/TestCluster gate, and Windows mirror capture distinct. Runtime-002 A-C passed; Windows capture was safely unsealed. Consult the [dated runtime record](../../../../../docs/integrations/scion-runtime-check.md) and its separate activation result, not a fixture as production acceptance.

Probe the explicit root, select the relevant module/object and read its SKLT. In MANAGED use `dungeon_validate`; in SKLT_ONLY inspect catalog issues without generating managed artifacts. Review duplicate identities, declared dependencies and GLIF links.

Run `dungeon_source_link_audit` for the selected objects. Report path/hash CURRENT, STALE or MISSING separately from UNKNOWN symbol/semantic correspondence. Review Task Trace required/performed checks and evidence refs, then `dungeon_coverage_get` for both scopes. A sealed record protects stored bytes; current build/source applicability still needs checking. A textual PASS or SKLT state alone cannot establish runtime readiness.

When proof matters, distinguish immutable CAS/seal integrity from the current proof state (`VERIFIED`, `INCOMPLETE`, `CONFLICT`, `STALE`, `INVALID`). Unknown formats, missing raw receipts, or extra unaccounted non-result artifact refs are `PROOF_UNVERIFIED`, never positive proof. Check exact typed `id`/`type`/`scope` matching and require passing Task Trace checks and actual result. Read the [v0.4.1 adapter contract](../../references/proof-adapters-v041.md) before interpreting Toolkit reliability receipts; it does not claim executor authenticity or full GPU/UI acceptance.

Keep Prototype and Product conclusions independent; neither changes SKLT/RARE, EXP, or grade. The 2026-10-10 audit remains a historical `FAIL` and must not be rewritten. A new target re-audit must be independently performed; a self-reported `INDEPENDENT_VERIFIED` is not final independent acceptance.

For execution/UI evidence follow the selected project's Toolkit guide and current Toolkit index. Report confirmed results and unresolved aspects with concrete file/object/evidence IDs. Review requests are read-only unless the user also asks for a change.

For Scion-backed scopes, use the [Scion reference](../../references/scion.md). A local base or typed revision is not proof of current server state; preserve UNKNOWN/PARTIAL and the typed-receipt adapter failure instead of claiming VERIFIED.
