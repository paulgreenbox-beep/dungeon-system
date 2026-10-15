# Evidence Store MVP

Evidence links an execution to the structural object, Git state, structural snapshot, Toolkit capability and exact artifact bytes.

~~~text
Git + SKLT/object_id + snapshot
              ↓
       Evidence Manifest
              ↓
         SHA-256 refs
              ↓
    external local Content Store
~~~

## Storage boundary

- evidence/manifests/ and evidence/seals/ contain small JSON files stored with the project.
- CAS bytes live outside the project, under the configured evidence_store.root.
- .project/project.json currently uses ../DungeonStore, resolved relative to the project root. Change this value to move the store; object IDs and manifests do not change.
- CAS path layout: <root>/cas/sha256/<first-2>/<next-2>/<full-sha256>.
- snapshots/ stores structural state. It never stores container images, binaries, logs, screenshots or other runtime payloads.
- Dungeon System records execution references. The Toolkit remains the owner of container setup and execution.

Sealed manifests retain verifiable references to their CAS bytes. The MVP has no garbage collector or deletion command, so CAS objects remain in the external store until an operator removes them. HOT/CACHE/EPHEMERAL lifecycle classification is deferred; callers must not submit ephemeral files to CAS.

## Capture lifecycle

1. Run the documented Toolkit command and collect only the artifacts worth keeping.
2. Put each exact file in CAS:

   ~~~text
   python tools/project-structure.py cas-put <file> --json
   ~~~

   The command prints a sha256:<hash> reference. Repeated identical bytes reuse the same object. A conflicting/corrupt object is never overwritten.
3. Create a small draft JSON outside the project and pass it to:

   ~~~text
   python tools/project-structure.py evidence-create <draft.json> --json
   ~~~

   The draft contains id, objects, execution, inputs, artifacts, result, and optionally structure.snapshot. The tool adds project ID, current Git commit/dirty state when Git is available, current object states and a timestamp. Every object must exist in current SKLT; each snapshot and CAS ref must exist.
4. Verify exact bytes and linkage:

   ~~~text
   python tools/project-structure.py evidence-verify EV-20260923-001 --json
   ~~~
5. Seal meaningful evidence:

   ~~~text
   python tools/project-structure.py evidence-seal EV-20260923-001 --json
   ~~~

   A seal stores the exact manifest hash and captured Git commit. Manifests and seals are append-only through the CLI. A new run receives a new evidence ID.

Retrieve bytes without overwriting an existing destination:

~~~text
python tools/project-structure.py cas-get sha256:<hash> --output <new-file>
python tools/project-structure.py cas-verify sha256:<hash>
python tools/project-structure.py cas-status
~~~

To inspect or find evidence:

~~~text
python tools/project-structure.py evidence-inspect EV-20260923-001
python tools/project-structure.py evidence-for renderer.settings
~~~

status reports store availability, blob count/bytes, manifests, seals and unsealed count. A missing/unavailable CAS does not make structural validation fail; an explicit evidence verify/seal operation does fail if its referenced bytes cannot be verified.

## Draft format

~~~json
{
  "id": "EV-20260923-001",
  "objects": ["renderer.settings"],
  "structure": {"snapshot": "before-settings-test"},
  "execution": {
    "provider": "rust_makepad_toolkit",
    "type": "container",
    "id": "docker-linux-level0"
  },
  "inputs": {},
  "artifacts": {
    "stdout": "sha256:<64 lowercase hex characters>"
  },
  "result": {"status": "PASS"}
}
~~~

Use provider/type/ID values listed by the current Rust_Makepad_Toolkit/docs/INDEX.md and its linked guides. The known container key is docker-linux-level0; do not invent IDs. Evidence verification reports an unresolved external catalog as a warning and a known catalog with an unknown container key as an error.

The stored manifest format is `dungeon.evidence.v1`; its JSON Schema is `evidence-manifest-v1.schema.json`. A draft file is the local DRAFT stage and is not yet a captured record. The CLI reports CAPTURED, VERIFIED, SEALED, or BROKEN for persisted records. These integrity labels do not replace current proof evaluation.

## v0.4: Task, source and exact-build applicability

Existing `dungeon.evidence.v1` records remain valid. Integrity verification of an old
manifest does **not** automatically grant Prototype/Product `VERIFIED` coverage.
The optional fields below connect an execution to a Task Trace and its current implementation:

~~~json
{
  "task_id": "RTX-0042",
  "scope": "PROTOTYPE",
  "build": {"path": "target/app.exe"},
  "source": {
    "revision": "<current full Git commit>",
    "files": ["src/render.rs", "tests/render_test.rs"]
  }
}
~~~

These are additions to a complete draft, not a standalone evidence manifest.
Capture computes each file's actual SHA-256, captures source/build bytes in CAS
and rejects supplied hashes or revision values that disagree. Source entries in
the stored manifest include `path`, bare hexadecimal `sha256`, and a `cas` reference.
`inputs.executable` points to the exact build bytes. Paths stay project-relative.

For current proof verification, put a structured JSON execution report in CAS and refer
to it through `artifacts.result`. Its envelope must match the manifest:

~~~json
{
  "objects": ["rendering.cuda_pipeline"],
  "task_id": "RTX-0042",
  "scope": "PROTOTYPE",
  "execution": {
    "provider": "rust_makepad_toolkit",
    "type": "container",
    "id": "<real Toolkit capability/container ID>"
  },
  "source": {
    "revision": "<current full Git commit>",
    "files": [{"path": "src/render.rs", "sha256": "<64 lowercase hex>"}]
  },
  "build": {"path": "target/app.exe", "sha256": "<64 lowercase hex>"},
  "result": {
    "status": "PASS",
    "checks": [{"id": "smoke", "status": "PASS", "type": "executable"}]
  }
}
~~~

This is a contract example, not a reported test result. The report must include all
captured source/test files. Resolve execution IDs from the Toolkit guide; Dungeon
does not run the test or manufacture its result. A bare textual `PASS`, including
one stored in CAS, is insufficient. Executable/runtime/UI checks require a build
path and exact EXE hash; source-only unit checks can omit `build`.

For a new proof obligation, required checks use typed `id`, `type`, and `scope`
identity and must agree across the requested object/coverage, Task Trace, report,
and raw bindings. Every required performed check, the task result, and the actual
structured execution result must be `PASS`; new Task Traces should use
`status: PASS` and `actual_result: PASS`. Successful legacy task-status aliases
remain readable, but do not waive the required actual result or passing checks.

The current proof evaluator reports `VERIFIED`, `INCOMPLETE`, `CONFLICT`,
`STALE`, or `INVALID` separately from immutable CAS/seal integrity. An unknown
adapter/format, missing raw receipt, or unaccounted additional non-result artifact
reference fails closed as `PROOF_UNVERIFIED`; it cannot support `VERIFIED`.
Unknown legacy/native receipts, including native GPU trails, are incomplete, not
positive proof. The only positive raw adapter and its bindings are specified in
[Proof adapters v0.4.1](proof-adapters-v041.md); use that guide rather than
inferring an adapter shape from arbitrary JSON or text.

New evidence captures the full SKLT obligation hash for each linked object. If
that SKLT changes, current proof becomes `STALE`; the previous Evidence Manifest
and seal remain immutable and `SEALED`. Create new evidence for a new attempt;
never rewrite an old manifest or seal. Prototype and Product remain independent,
and proof/coverage updates never modify structural `SKLT/RARE`, EXP, or grade.

The original 2026-10-10 audit remains a historical `FAIL`; do not rewrite its
packet or report. A target re-audit must be independently performed and reviewed.
A component's own `INDEPENDENT_VERIFIED` label is not final independent
acceptance. CAS/seal proves stored-byte integrity, not executor authenticity,
full GPU/UI acceptance, or semantic completeness.

Trace records live in `.project/task-traces/`; explicit Prototype/Product
observations live in `.project/coverage/`. They are small implementation metadata,
not structural authorities or derived EXP inputs. Updating a trace requires its
previous returned hash. Allowlisted SKLT-only projects can use these records
without creating a Dungeon manifest or index. CAS capture still requires managed
store configuration. Missing Git/store/proof leaves verification unavailable.

Source Link Audit checks declared paths, optional hashes and revisions. File
freshness and symbol/semantic correspondence are reported separately; symbol and
semantic aspects remain `UNKNOWN` without AST/LSP support. It never repairs refs
by guessing.

## Snapshot and Cronica references

Link existing evidence IDs when creating a meaningful structural snapshot:

~~~text
python tools/project-structure.py snapshot after-settings-test --evidence-id EV-20260923-001
~~~

Snapshots store only evidence IDs. Cronica may record the same IDs, object IDs, Git commit, Toolkit capability and reason. Do not paste runtime logs into Cronica or snapshots.

In a Cronica entry, use an explicit metadata field so references can be checked:

~~~text
evidence_ids: [EV-20260923-001, EV-20260923-002]
~~~

`validate` reports invalid or missing Evidence Manifest references in both snapshots and Cronica entries. Empty fields (`[]`) are allowed for structural checkpoints without execution evidence.

## Deliberately deferred

The MVP does not implement artifact classes (`HOT`, `PERMANENT`, `CACHE`, `EPHEMERAL`), garbage collection, SQLite search indexing, remote storage or Toolkit execution. CAS objects are content-addressed and never overwritten by the CLI; there is no automatic deletion path. Manifests and seals are append-only through the writer API; pathname-based chmod is deliberately not used as a security boundary. Seal verification detects changed stored bytes but does not authenticate an executor or stop an administrator modifying storage outside Dungeon.
