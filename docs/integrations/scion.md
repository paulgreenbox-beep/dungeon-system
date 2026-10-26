# Scion integration

**Integration status: EXPERIMENTAL, bounded Linux runtime tested.** Mock coverage is supplemented by a real-CLI/TestCluster gate on Linux (A-C PASS). This fixture does not establish production readiness, general WSL/DrvFS support, or current proof for the Windows capture. Activation gate D and the exact Epic status are recorded separately in the runtime report. Do not report INDEPENDENT_VERIFIED.

## Pinned upstream

This guide is pinned to [CarterT27/scion commit 7d445a0744e30f4dcb36a794b0ce42648157e1b9](https://github.com/CarterT27/scion/tree/7d445a0744e30f4dcb36a794b0ce42648157e1b9), workspace version 0.1.0, Apache License 2.0, Rust 1.98. The upstream README describes an independent clean-room Rust implementation, not Anthropic's internal system. Consult the [pinned README](https://github.com/CarterT27/scion/blob/7d445a0744e30f4dcb36a794b0ce42648157e1b9/README.md), [pinned SPEC](https://github.com/CarterT27/scion/blob/7d445a0744e30f4dcb36a794b0ce42648157e1b9/SPEC.md), and [v1 schemas](https://github.com/CarterT27/scion/tree/7d445a0744e30f4dcb36a794b0ce42648157e1b9/schemas/v1) when an implementation question requires them. Dungeon is MIT-licensed; this integration consumes the upstream CLI contract and does not copy/vendor the Scion server, binaries, or schemas. No OpenAI API key is needed; authenticated access uses only the operator-provided Scion token.

| Component | License / pin | Boundary |
| --- | --- | --- |
| Dungeon System | MIT | Adapter and documentation source. |
| Scion upstream | Apache-2.0, commit `7d445a0744e30f4dcb36a794b0ce42648157e1b9` | External CLI contract; upstream checkout is unchanged and not vendored. |

For the dated feasibility history and bounded runtime result, including exact pass/fail boundaries, see the [Scion runtime check](scion-runtime-check.md) and [runtime-002 report](scion-runtime-002-report.md).

At this pin, Scion targets a monorepo and Unix clients. A server deployment requires CockroachDB, S3-compatible object storage, TLS, signing keys, and access policy. Offline operation, Git protocol compatibility, and bidirectional Git synchronization are not supported. Git import is one-way. Scion's source object store is distinct from Dungeon CAS.

## Scope configuration

Add an explicit entry under version_scopes only when a project intentionally selects Scion. Keep existing git_scopes and SKLT git_scope references unchanged. The stable scope ID connects those legacy references to the selected backend; it does not rename an ObjectID or LevelID.

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

Only the declared fields are accepted. Root and repository are contained, project-relative paths. endpoint_profile names an environment profile; it carries no endpoint secret, token, executable, or shell command. Existing Git configuration remains the default when no Scion scope is declared. The implicit Git scope `main` remains only when neither `git_scopes` nor `version_scopes` explicitly declares scope ID `main`.

## Environment and platform

Configuration is supplied outside the project manifest. A LAB profile uses these process environment variables:

~~~text
DUNGEON_SCION_LAB_ENDPOINT
DUNGEON_SCION_LAB_TOKEN
DUNGEON_SCION_LAB_CA_FILE
DUNGEON_SCION_LAB_TRANSPORT
~~~

The current adapter consumes the endpoint, explicit token, optional CA file, and transport configuration on Linux only. `DUNGEON_SCION_LAB_DISTRIBUTION` is reserved and unused; it does not enable WSL. The bundled Codex MCP configuration currently forwards only `DUNGEON_PROJECT_ROOT` and `DUNGEON_PYTHON`. An operator using a Linux MCP server must explicitly allowlist the needed per-profile variables in that server's MCP `env_vars` configuration; otherwise the child server will not receive the profile even if the variables exist in the app's parent environment. For example, add these names (and only the profiles actually needed) to the `env_vars` array:

~~~json
[
  "DUNGEON_SCION_LAB_ENDPOINT",
  "DUNGEON_SCION_LAB_TOKEN",
  "DUNGEON_SCION_LAB_CA_FILE",
  "DUNGEON_SCION_LAB_TRANSPORT"
]
~~~

Supply values to the MCP process through the operator's secure environment/secret mechanism. Never commit values, place a token in command arguments, or write it to project/plugin configuration. No OpenAI API key is required. The adapter does not run Scion login or fall back to saved credential files.

Native Windows execution and a supported WSL/DrvFS adapter remain unsupported. The bounded test below ran Linux queries in Ubuntu WSL against a disposable fixture on native ext4; it does not verify a general WSL transport or path-translation/containment contract. A separate DrvFS fixture attempt failed during checkout (`stat written file: No such file or directory`) before show/log; the cause was not diagnosed. An earlier setup attempt also failed before queries and is not included as a pass. The adapter does not install Scion, start a production server or daemon, or alter a distribution. The 2026-10-15 prerequisite observation (Rust 1.93.1, no CLI) is historical; the private, checksum-verified runtime setup used Rust 1.98.0 and `protoc` 32.1.

## Runtime verification — 2026-10-16

The bounded gates A-C passed against the pinned upstream commit; activation gate D is recorded separately. These results are fixture evidence, not a production deployment or independent acceptance:

| Gate | Result | Evidence boundary |
| --- | --- | --- |
| A — prerequisites | PASS | Rust 1.98.0 and `protoc` 32.1 verified for the pinned build. |
| B — upstream executable | PASS | Locked `scion` build and the real-CLI help/`whoami` smoke against upstream `TestCluster`; final tested CLI SHA-256 `e9233886e8fa970af79fcf6e8dcbd44f45b0f33b25da67953f6a87698bf52d7f`. |
| C — Dungeon Linux gate | PASS | 29/29 checks against a disposable three-changeset TestCluster fixture. Real CLI `show`/`log` JSON and `cat` byte comparisons passed; backend and mixed-outage behavior and a bounded real-CLI TCP-blackhole timeout were exercised. Fault-injection negatives are labeled `ADAPTER_FAULT_INJECTION_NOT_RUNTIME_PROOF`, not runtime proof. |
| D — package activation | See runtime report | Requires installed launcher and fresh-session discovery, not source metadata alone. |

Changesets in fixture order (oldest to newest): `c14df68cfab35724856a0a1bf184bfc451c958dc5b9968cb6c5a1beb6c5f930f5b`, `c126bd0e41dd1918fcb28b4f8717275de197f91cbfdae5f9d11566666ef55f9611`, `c1023926a37f2c46cebaf9b1b8200372139d86c214b4a9aac81d3a099298697870`. The passing Linux gate ran against the disposable native-ext4 fixture, not a production repository or service. The DrvFS setup-failure observation above is not generalized into a root-cause claim.

The separate Windows mirror capture reported `CAPTURED_UNSEALED` and exited 1 because sealing was correctly refused with `SOURCE_REVISION_UNKNOWN`; this is the expected fail-closed outcome, not a Linux runtime-gate failure. The Scion scope's typed local base was `c1023926…`, dirty state remained `UNKNOWN`, and availability was `UNSUPPORTED_PLATFORM`; a nested Git storage scope was separately observed clean/available. Source CAS integrity was `VERIFIED`, but the manifest had no seal and proof was `STALE`/`verified=false`, including `VCS_PROOF_ADAPTER_UNSUPPORTED`. CAS integrity and a typed local base do not establish current Scion proof.

Sanitized local receipts: `artifacts/scion-runtime-002/bootstrap-01.json`, `build-03/result.json`, `run-04/linux-gate-01/result.json`, and `run-04/capture.json`. The [runtime-002 report](scion-runtime-002-report.md) records the exact acceptance result.

## Read-only operations and current limits

The adapter uses the real CLI's JSON v1 query forms for bounded show and log:

~~~sh
scion -C <scope-root> --json show <revision>
scion -C <scope-root> --json log --limit <1..100> --rev <revision>
~~~

The result must match the pinned scion.cli.show.v1 or scion.cli.log.v1 schema. The adapter reads the exact scope's local .scion/state and records its base/tracking ref as LOCAL_WORKING_COPY_BASE. That local base is not proof of the current server ref, freshness, or clean working tree. Pending operations invalidate the observation; recovery is disabled. Show/log query results therefore do not make dirty_state known, and the overall Scion observation remains PARTIAL.

Scion status is not executed through the read-only Dungeon surface: upstream status can recover pending operations and write the local index. Scion working-copy diff is also disabled because it opens mutable working-copy state. Dungeon reports PARTIAL/UNKNOWN or UNSUPPORTED_OPERATION instead of invoking those commands. Do not treat this as proof that a checkout is clean.

The Scion revision is a typed changeset ID: c1 followed by 64 lowercase hexadecimal BLAKE3 digits. Do not normalize it as a Git 40/64-hex commit or a Dungeon CAS sha256 reference. The Git adapter remains independent.

## Snapshot and evidence linkage

Snapshot schema 1 gains optional revisions_schema: 1 and a revisions object keyed by VersionScope ID. Each entry is a typed observation; old schema-1 snapshots remain valid. Capturing several backends is sequential and explicitly non-atomic.

Evidence may include top-level source_revision schema 1 with typed observations for each relevant scope. Legacy Git manifests remain supported. The accepted legacy receipt path for root `main` Git remains unchanged even if Scion is configured elsewhere; that configuration does not convert legacy Git receipts into typed provenance. Typed Scion and nonlegacy VersionScope receipts fail closed with `VCS_PROOF_ADAPTER_UNSUPPORTED` until a compatible receipt adapter exists. Every explicitly declared source file included in the evidence capture is hashed and stored in Dungeon CAS independently of the server base. This does not make evidence VERIFIED: any UNKNOWN revision or dirty state blocks a current-proof claim.

## Planning and unsupported phases

The CLI sparse-plan derives an exact path list from the selected object's declared source refs. It does not compute dependency closure or perform sparse checkout. The CLI migration-plan inventories impact and references for review; it does not call the Scion Git importer or rewrite the Git repository. Import is one-way and no two-way synchronization is promised.

Sparse working-copy execution (Phase 3) and explicit commit/land mutations (Phase 4) remain unsupported. The four MCP tools are read-only query names; none expose checkout, commit, land, login, or migration.

The local source package declares 34 stdio tools, including four VCS queries. Installed-cache and fresh-session counts must be checked independently; an existing chat can retain an older catalog. See the runtime report for actual activation evidence. No release or push is implied.
