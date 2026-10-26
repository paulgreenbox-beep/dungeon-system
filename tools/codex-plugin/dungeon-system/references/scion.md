# Scion VersionScope reference

Use this page only when a selected project's declared VersionScope uses Scion. Git stays the default, and legacy GitScope IDs plus SKLT git_scope references remain stable.

- Select the exact scope and report its backend and availability. Never silently substitute Git.
- Scion inspect/history use bounded upstream show/log JSON v1 queries. Their local working-copy base does not establish current server state or cleanliness.
- Do not run Scion status or working-copy diff as read-only operations. Status may recover pending work and write the index; diff may access mutable working-copy metadata. Preserve dirty_state UNKNOWN and any PARTIAL/UNSUPPORTED result.
- A Scion c1 plus 64-hex BLAKE3 changeset ID is distinct from Git object IDs and Dungeon CAS sha256 hashes.
- Legacy receipts for root `main` Git remain accepted on the existing Git path even when Scion is configured elsewhere; that does not convert them to typed provenance. Typed Scion/nonlegacy VersionScope receipts fail closed with `VCS_PROOF_ADAPTER_UNSUPPORTED`. Typed source_revision plus captured source-file CAS hashes is provenance, not VERIFIED proof.
- Sparse plans use exact declared source refs, without transitive closure or checkout. Migration plans report impact and do not migrate.
- Native Windows and WSL are unsupported by the current adapter. WSL requires a future verified path-translation and containment contract.
- Runtime-002 gates A-C passed against a disposable three-changeset TestCluster fixture on Linux/native ext4; this is not production or general Windows/WSL/DrvFS runtime acceptance. Installed-session activation is a separate gate; consult the runtime report for its current result.
- The Windows mirror receipt is `CAPTURED_UNSEALED`: `SOURCE_REVISION_UNKNOWN` blocks sealing, while captured-byte CAS integrity is a separate claim. A local Scion base cannot establish current proof; typed receipt handling remains `VCS_PROOF_ADAPTER_UNSUPPORTED`.
- The bundled MCP `env_vars` forwards only `DUNGEON_PROJECT_ROOT` and `DUNGEON_PYTHON`; operators must explicitly allowlist needed per-profile Scion variables for the MCP process. Never commit token values or pass a token in argv. `DUNGEON_SCION_<PROFILE>_DISTRIBUTION` is reserved and unused.

In a full source checkout, read [VCS backend policy](../../../../docs/vcs-backends.md), [Scion integration](../../../../docs/integrations/scion.md), and the dated [runtime check](../../../../docs/integrations/scion-runtime-check.md). If this skill is loaded from an installed plugin cache where those repository-relative links do not resolve, use the configured full Dungeon source checkout at `DUNGEON_PROJECT_ROOT` (as set up in the package README) and open those files beneath that root. Do not resolve these links against the current target/foreign project's `project_root`. These Dungeon notes are bounded; consult the [pinned Scion README](https://github.com/CarterT27/scion/blob/7d445a0744e30f4dcb36a794b0ce42648157e1b9/README.md) and [SPEC](https://github.com/CarterT27/scion/blob/7d445a0744e30f4dcb36a794b0ce42648157e1b9/SPEC.md) only when the task requires an upstream contract detail.
