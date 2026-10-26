# DUNGEON-SCION-RUNTIME-002 — bounded real CLI and Dungeon gate

~~~text
id: CRONICA-SCION-RUNTIME-002
date: 2026-10-16
epic: DUNGEON-SCION-RUNTIME-002
affected_modules:
    - scoping
affected_object_ids:
    - scoping.git_scope
evidence_ids: []
~~~

Added a bounded runtime follow-up for the experimental Scion VersionScope integration, retaining the earlier VCS-001 entry as historical. The upstream pin remained `7d445a0744e30f4dcb36a794b0ce42648157e1b9`; the pinned upstream checkout was not edited.

Runtime gates A-C passed: verified Rust 1.98.0/`protoc` 32.1 prerequisites; the built upstream CLI and in-memory `TestCluster` smoke; and 29/29 Linux Dungeon checks against a disposable three-changeset fixture. The gate exercised real CLI `show`/`log` and `cat` byte comparisons plus bounded outage/timeout behavior. Fault-injection negatives remain explicitly non-runtime-proof. The fixture ran on native ext4 in Ubuntu WSL; it is not production access or general WSL/DrvFS support. A DrvFS attempt failed before queries with `stat written file: No such file or directory`; its cause remains undiagnosed.

The separate Windows mirror capture was `CAPTURED_UNSEALED`; sealing correctly refused with `SOURCE_REVISION_UNKNOWN`. Captured source CAS integrity was verified, but proof remained stale/unverified and typed provenance failed closed with `VCS_PROOF_ADAPTER_UNSUPPORTED`. The local Scion base and CAS hashes do not establish current source proof. At capture time activation D was pending and the old installed cache had 30 tools; subsequent activation evidence is recorded separately.

No project structure, production checkout, or upstream source was changed. This disposable fixture is not a root-project snapshot, so `evidence_ids` is empty. Related snapshot: none. See the [runtime record](../integrations/scion-runtime-check.md) and [runtime report](../integrations/scion-runtime-002-report.md).
