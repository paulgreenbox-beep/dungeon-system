# v0.4.1 hardening: public technical summary

Release: **v0.4.1-rc1**, prerelease. **Independent re-audit pending.**

The original v0.4 independent audit failed. The following remediation and local
verification do not rewrite that result or assign independent acceptance.
This summary omits private project identities, paths, captured binaries, raw
execution evidence, original audit fixtures and installation environments.

## Findings and remediation

| Finding | Earlier behavior | v0.4.1 remediation | Local regression result |
|---|---|---|---|
| B-01: output escape | A refresh could write through an output junction before returning failure. | Exact approved physical root, all-output preflight, identity leases, staged publication, caught-failure rollback/residual reporting. | Junction/canary containment and actual writer tests passed; hardlink races tested with native Windows operations. |
| B-02: contradictory Task Trace | A positive CAS report could overrule skipped/unknown/not-run or failing Trace checks. | A shared evaluator requires matching passing typed checks, task completion and actual result. | Contradictory claims cannot establish effective VERIFIED. |
| B-03: execution-type substitution | A unit check could satisfy a runtime obligation without exact executable evidence. | Match id/type/scope; runtime/UI/executable checks need the applicable path and build/source/revision hashes. | Unit substitution and missing/mismatched executable evidence rejected. |
| B-04: structured PASS vs raw FAIL | Integrity/seal could be mistaken for truthful, current proof. | Separate integrity from current proof; only a narrow versioned raw adapter supplies positive correspondence. Unknown/extra unaccounted receipts fail closed. | Conflicting reports block VERIFIED; unsupported receipts remain incomplete. |
| B-05: nested logical IDs | IDs returned by discovery could fail later lookup. | Shared ID grammar and hashed metadata keys; do not treat IDs as paths. | Project/module/capability round-trip and unsupported-kind capability error tested. |
| B-06: stale SKLT obligation | Responsibility changes could leave old proof apparently current. | Capture the whole canonical UTF-8 SKLT obligation digest; re-evaluate freshness without rewriting manifests/seals. | Matching synthetic supported proof becomes STALE after mutation; original sealed bytes remain unchanged. |

In the original B-06 replay the unsupported earlier receipt was already
INCOMPLETE before mutation; that replay alone is not a VERIFIED-to-STALE proof.
A separate synthetic supported-adapter test establishes the evaluator transition.
Synthetic test receipts establish plumbing only, never factual runtime acceptance.

## Recorded local gates

The final hardening gate recorded on 2026-10-12 ran **234 tests: 233 PASS,
0 FAIL, 0 ERROR, 1 SKIP** (Windows symlink creation privilege, WinError 1314).
The pre-publication baseline repeated on 2026-10-15 produced the same counts.
Junction and hardlink tests ran independently of the symlink skip.

The real installed Windows launcher reported server version 0.4.1, 30 tools and
six packaged Skills. Fresh read-only Codex sessions exposed 30 tools and passed
probe/status/inspect/read-SKLT calls. Their empty approved-root list denied
writes. Standard MCP `_meta.progressToken` compatibility has regression coverage.

One cleanup investigation preserved an early 99-pass/1-error repeat. A bounded
WinError 145 retry then passed 100/100 cases without suppressing exhausted errors
or leaving fixture roots. The transient file's creator was not established.

The source publication does not ship private-packet replays or their payloads.
Two such tests explicitly skip when their optional packets are absent. Public
synthetic consistency/security regressions remain runnable. Fresh publication
test counts and scan results are reported with the release; they must not be
silently substituted for the historical counts above.

Source-publication preparation also retained failed rollback/cleanup runs,
including a 100-case diagnostic with four failures and a separate snapshot
rename failure (WinError 5). Unexpected temporary entries were observed, but
their creator and the rename failure's cause were not established. A proposed
runtime cleanup retry was withdrawn rather than changing failure propagation
as part of publication. The runtime implementation remains unchanged.

Test tree snapshots now inspect each enumerated entry with `lstat` and ignore
only names that are already absent. Existing files/directories and inspection
errors remain visible; deterministic regressions cover those distinctions.
This corrects a possible false-positive window, not proof of the cause of
every earlier failure. Legacy fixtures use the existing strict bounded
WinError 145 cleanup helper. Verification fixtures are kept outside the source
checkout. Final fresh gates are reported separately; passing them does not
erase earlier intermittent Windows failures or establish independent acceptance.

The final source-publication runs on 2026-10-15 exercised 249 cases each:
local 248 PASS / 1 SKIP; exact public sources 246 PASS / 3 SKIP; both had
0 FAIL and 0 ERROR, with test inputs unchanged during execution. The extra
public skips are the two intentionally absent private packets. Actual isolated
Codex marketplace installation exposed 30 tools and six Skills and passed a real
MCP project read with writes disabled. These are fresh local publication checks,
not an independent re-audit.

## Limits

- Hardened core writes are Windows-only; read navigation remains portable.
- Publication/rollback is not a crash-safe multi-file transaction or protection
  against storage owners changing ACLs, privileged handles or preexisting
  writable mappings. Snapshot child correspondence is checked after rename.
- SHA-256/seals prove stored byte identity, not executor authenticity, physical
  GPU execution or full GUI/application acceptance.
- Unversioned/native legacy receipts cannot establish positive current proof.
- Evidence identity is project/content based, not absolute-checkout-path based;
  a byte-identical clone with the same project identity is not a separate
  authenticated origin.
- The public sources contain no external Toolkit runtime or other project's
  implementation. Optional capabilities require their owner's tooling/guides.

See [SECURITY.md](../../SECURITY.md), [the adapter contract](../proof-adapters-v041.md)
and [CONTRIBUTING.md](../../CONTRIBUTING.md). Independent remediation acceptance
must be performed separately.
