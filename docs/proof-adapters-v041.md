# v0.4.1 current-proof adapter

This page describes the narrow positive raw-evidence adapter implemented by
`tools/project_structure/v041_proof.py`. It does not define a new Evidence
Manifest schema. The manifest/seal and CAS integrity workflow remains in
[Evidence Store](evidence.md); this adapter separately recomputes whether a
sealed record supports a current Prototype/Product proof.

Every JSON snippet on this page is a contract template with placeholders, not
captured evidence or a report of an actual execution.

## Supported raw format

The only positive raw adapter is `toolkit-reliability-v2`: the versioned JSON
bundle emitted by Rust_Makepad_Toolkit reliability with all of these exact
identifiers:

~~~json
{
  "schema_version": 2,
  "tool_version": "ai-ui-test-tool-auto-probe",
  "contract_version": "reliability-v2",
  "actions": [
    {
      "action_id": "<action-id>",
      "session_id": "<session-id>",
      "timestamp_unix_ms": 1,
      "action": "assert_visible",
      "target": "<automation-id>",
      "result": {
        "success": true,
        "operation": "assert_visible",
        "action_id": "<action-id>",
        "phase": "complete",
        "application_healthy": true,
        "result": {
          "assertion": "visible",
          "passed": true,
          "control": {
            "automation_id": "<automation-id>",
            "visible": true
          }
        }
      }
    }
  ]
}
~~~

The real bounded `actions` array contains Toolkit ActionRecords. Each record
needs a unique `action_id`, non-empty `session_id`, non-negative integer
`timestamp_unix_ms`, an `action`, and structured `result`. The evaluator checks
every ActionRecord for malformed identity, explicit failures/errors, and
unhealthy application/runtime state. Each bound record additionally needs the
canonical complete-success response described below; an arbitrary JSON document
or text log is not positive proof.

## Manifest and report binding

The captured execution report is the CAS object referenced by
`artifacts.result`. It carries the execution result fields already described in
[Evidence Store](evidence.md), plus the adapter proof envelope. The exact raw CAS
reference in `proof.raw` must also occur in a non-result artifact entry in the
sealed manifest:

~~~json
{
  "artifacts": {
    "result": "sha256:<result-json-cas-digest>",
    "toolkit-raw": "sha256:<raw-reliability-json-cas-digest>"
  }
}
~~~

The CAS result report carries the evidence envelope and this `proof` binding:

~~~json
{
  "objects": ["renderer.control"],
  "task_id": "<task-id>",
  "scope": "PROTOTYPE",
  "execution": {
    "provider": "rust_makepad_toolkit",
    "type": "container",
    "id": "<catalogued-Toolkit-id>"
  },
  "source": {
    "revision": "<full-current-Git-commit>",
    "files": [{"path": "src/control.rs", "sha256": "<64-lowercase-hex>"}]
  },
  "build": {"path": "target/app.exe", "sha256": "<64-lowercase-hex>"},
  "result": {
    "status": "PASS",
    "checks": [
      {"id": "control-visible", "type": "ui", "scope": "PROTOTYPE", "status": "PASS"}
    ]
  },
  "proof": {
    "adapter": "toolkit-reliability-v2",
    "schema_version": 2,
    "raw": "sha256:<raw-reliability-json-cas-digest>",
    "bindings": [
      {
        "id": "control-visible",
        "type": "ui",
        "scope": "PROTOTYPE",
        "action_id": "<action-id>",
        "operation": "assert_visible"
      }
    ]
  }
}
~~~

For each structured check, `id`, `type`, and `scope` must match exactly across
the required check, performed Task Trace check, captured result, requested
object/scope, and raw binding. Each binding must point to one distinct existing
ActionRecord; `operation` must equal that record's `action`. Bindings must cover
all reported claims without duplicates. The CAS report's task, scope, execution,
build, object set, source revision, and captured source hashes must also agree
with the sealed manifest.

New Task Traces should use `status: PASS`, `actual_result: PASS`, and
`status: PASS` on every performed check. The current reader still accepts
successful legacy task-status markers (`DONE`, `COMPLETED`, `VERIFIED`), but the
actual result and required performed checks must pass. New required checks should
always declare typed `id`/`type`/`scope`; an old string-only check is insufficient
unless one consistent type can be established from matching typed trace and
report checks.

Every additional non-result artifact reference must be accounted for by a
supported correspondence. An extra reference without one fails closed as
`PROOF_UNVERIFIED`; do not treat unrecognized artifacts as harmless proof input.

## Accepted assertion operations

Bindings can positively correspond only to the canonical assertions implemented
by the adapter:

- `assert_visible`: the canonical result says `passed: true`, identifies the
  target automation ID, and observes that control as visible.
- `assert_interactable`: the same target identity is present and the structured
  interactability observation is true.
- `assert_valid_bounds`: the target is identified and observed width and height
  are finite positive numbers.
- `windows_assert_automation_ids`: the expected-ID list is non-empty, the
  missing-ID list is empty, and the observed actual count is positive.

The canonical ActionRecord response must be successful and complete, identify
the same operation and action ID, report `application_healthy: true`, and verify
any required postcondition. A failed assertion or unhealthy application is a
conflict; missing/unknown structured observations are incomplete. Runtime checks
(`runtime`, `executable`, or `ui`) also need the exact applicable build path and
SHA-256. These narrow assertions do not prove a separate test type just because
the report labels it that way.

## Fail-closed states and limits

Current proof state is distinct from Evidence Manifest integrity and seal state:

- `VERIFIED` — all required links, hashes, current SKLT obligation, Task Trace,
  result and supported raw bindings agree and pass.
- `INCOMPLETE` — required evidence or a supported interpretation is missing.
- `CONFLICT` — typed bindings, results, statuses, or observations disagree.
- `STALE` — a captured source/build/revision or SKLT obligation no longer matches.
- `INVALID` — a record or raw receipt is malformed or fails integrity validation.

The evaluator keeps the existing `status` compatibility field (`PASS` only when
`proof_state` is `VERIFIED`, otherwise `FAIL`) and returns `issues`, passing
check IDs, and the observed manifest/seal hashes. When a previously recorded
coverage value is `VERIFIED` but current proof is not, coverage inspection keeps
that historical `recorded_state` and reports effective coverage as `UNKNOWN`
with the current `proof_state`; it does not rewrite the stored coverage record.

An unknown adapter/schema, absent raw CAS receipt, or unaccounted artifact gives
`proof_state: INCOMPLETE` with `PROOF_UNVERIFIED` and cannot establish
`VERIFIED`. Unversioned legacy/native receipts—including native GPU trails—remain
incomplete. Three narrow negative signatures may veto a claim: a check line with
`FAIL`, `FAILED`, `SKIPPED`, `UNKNOWN`, or `NOT_RUN`; a nonzero “failed” test
summary; or `GPU NOT_RUN`. These signatures never supply positive proof; other
unknown text is not interpreted as a pass.
Changing an obligated SKLT makes the current proof stale. The earlier manifest
and seal stay unchanged and `SEALED`; create new evidence for the changed
obligation rather than rewriting history.

The obligation digest covers the complete UTF-8 SKLT, removing only an initial
BOM and canonicalizing CR/LF line endings; fields, comments, and other content
are not discarded. A historical manifest without this captured hash is
incomplete for current proof and is never backfilled in place.

The adapter checks correspondence to the documented Toolkit actions, not who
operated the executor, physical GPU execution, full GUI acceptance, or semantic
completeness. Synthetic receipts used in tests validate only this plumbing and
are not real application executions. Prototype and Product remain independent;
proof evaluation does not change SKLT/RARE, EXP, or grade.

For the authoritative Toolkit receipt contract, consult its current
`docs/INDEX.md`, `RELIABILITY.md`, `src/contract.rs`, and `reliability.rs`. This
guide intentionally does not replace those sources or expand the adapter's
supported format.
