# Scion runtime check — DUNGEON-SCION-VCS-001

Status: `SCION_RUNTIME_NOT_TESTED`
Audit date: 2026-10-15
Scope: read-only feasibility check against the pinned upstream clone, using the existing Ubuntu WSL2 environment only.

This section is the historical VCS-001 feasibility result and is intentionally preserved. Its `SCION_RUNTIME_NOT_TESTED` status was accurate on 2026-10-15; the separate 2026-10-16 runtime follow-up below supersedes it for the bounded fixture only, not for production readiness or Windows/WSL support.

## Pin and environment

- Upstream HEAD: `7d445a0744e30f4dcb36a794b0ce42648157e1b9`.
- Workspace package version/license: `0.1.0`, `Apache-2.0`.
- The upstream pins Rust `1.98.0` in `rust-toolchain.toml` and declares `rust-version = "1.98"` in its workspace manifest.
- Ubuntu WSL2 reports kernel `6.18.54.1-microsoft-standard-WSL2`. Its only visible compiler/toolchain is `/usr/bin/rustc` and `/usr/bin/cargo`, both version `1.93.1`; `rustup` is absent. No `scion` executable is on PATH, and the clone has no executable at `target/debug/scion` or `target/release/scion`.
- `protoc` is not on PATH. `crates/scion-proto/build.rs` requires it to build generated gRPC code.

The installed compiler is below the upstream minimum, and the required protocol compiler is also absent. No compiler or tool was installed or updated during this check.

## Cheapest existing CLI plus in-memory server candidate

The narrowest existing test that combines the actual `scion` executable with the in-memory test server is:

```sh
cd "<SCION_CHECKOUT>"
CARGO_TARGET_DIR=/tmp/scion-vcs-001-target cargo test -p scion-cli --test cli_smoke binary_help_usage_and_whoami -- --exact
```

`binary_help_usage_and_whoami` invokes `scion` through `assert_cmd`; its `whoami` case authenticates against `TestCluster`. The harness selects the default memory backend with the daemon disabled. It starts an in-process Scion server and FakeGitHub, binds loopback sockets, and creates temporary home/work directories. It does not require CockroachDB, S3, Docker, or a separately installed Scion CLI. The isolated target directory in the command keeps Cargo build output outside the upstream clone.

This test was not run because the compiler and `protoc` prerequisites are unmet.
The bounded feasibility check did not start the in-process server or fake service.
Registry-cache availability was not checked. This candidate exercises authentication;
it does not by itself cover the full Dungeon object/snapshot/status/history/diff scenario.

For broader command coverage, `human_output_of_every_command` uses the same in-memory cluster but calls `scion_cli::run` in-process rather than spawning the built CLI executable. `scion-testkit` has no `examples/` directory in this pin; its tests and the frozen `testdata/golden` vectors are not a substitute for the executable-plus-server smoke above. The `scion-conformance` examples are load-generation/benchmark programs, not smoke fixtures.

## Inspected upstream paths

- `rust-toolchain.toml`, `Cargo.toml`, `Cargo.lock`
- `crates/scion-proto/build.rs`
- `crates/scion-cli/tests/cli_smoke.rs`
- `crates/scion-cli/tests/cli_support/mod.rs`
- `crates/scion-conformance/src/cluster.rs`
- `crates/scion-server/src/testing.rs`
- `crates/scion-testkit/src/fake_github.rs`
- `crates/scion-testkit/tests/`, `testdata/README.md`, `testdata/golden/`

## Follow-up — DUNGEON-SCION-RUNTIME-002

Date: 2026-10-16. Pinned upstream remains `7d445a0744e30f4dcb36a794b0ce42648157e1b9`; no upstream files were changed. The original VCS-001 record above remains intact.

### Gate results

- **A — prerequisites: PASS.** Rust 1.98.0 and `protoc` 32.1 were verified for the pinned build.
- **B — upstream real CLI: PASS.** The locked `scion` binary built, and its help plus `whoami` smoke passed against the upstream in-memory `TestCluster`. The final tested binary SHA-256 is `e9233886e8fa970af79fcf6e8dcbd44f45b0f33b25da67953f6a87698bf52d7f`.
- **C — Dungeon Linux runtime gate: PASS.** All 29 checks passed against a disposable TestCluster with three revisions. Real external-CLI `show`/`log` output and the `show-raw.json` / `log-raw.json` files were checked; `cat` output matched working-copy file bytes. Backend behavior, mixed outage, and the real CLI's 10-second TCP-blackhole timeout were exercised. Negative fault-injection cases are explicitly `ADAPTER_FAULT_INJECTION_NOT_RUNTIME_PROOF`.
- **D — candidate activation: separate gate.** See the [runtime report](scion-runtime-002-report.md) for installed-cache and fresh-session evidence; source tool counts do not establish active-session discovery.

Fixture revisions, oldest to newest: `c14df68cfab35724856a0a1bf184bfc451c958dc5b9968cb6c5a1beb6c5f930f5b`, `c126bd0e41dd1918fcb28b4f8717275de197f91cbfdae5f9d11566666ef55f9611`, `c1023926a37f2c46cebaf9b1b8200372139d86c214b4a9aac81d3a099298697870`. The helper and gate used a disposable fixture on native ext4 within Ubuntu WSL. This does not validate production access, a general WSL transport, or DrvFS. A DrvFS checkout attempt stopped at `stat written file: No such file or directory` before queries; the cause is undiagnosed. A prior setup failure also did not reach show/log.

### Windows capture boundary

The separate Windows mirror capture returned `CAPTURED_UNSEALED` with exit 1 because the writer safely refused sealing on `SOURCE_REVISION_UNKNOWN`. This is expected fail-closed behavior, not a failure of the Linux runtime gate. The Scion observation had local base `c1023926…`, `dirty_state=UNKNOWN`, and `availability=UNSUPPORTED_PLATFORM`. The nested Git storage scope was separately `CLEAN`/available. Captured source bytes had verified CAS integrity, but the receipt was unsealed, proof state was `STALE` with `verified=false`, and typed Scion provenance failed closed with `VCS_PROOF_ADAPTER_UNSUPPORTED`. A local base and CAS integrity do not prove the current remote revision or current source applicability.

The fixture was disposable and did not contact a production service or capture a production project's source. Sanitized receipts are under `artifacts/scion-runtime-002/`: `bootstrap-01.json`, `build-03/result.json`, `run-04/linux-gate-01/result.json`, and `run-04/capture.json`. See the [runtime-002 report](scion-runtime-002-report.md) for the complete acceptance outcome.
