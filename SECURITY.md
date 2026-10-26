# Security policy

## Release status

`v0.4.1-rc1` is a prerelease. The local v0.4.1 hardening gate passed;
independent re-audit is pending. No independently verified status is claimed.
Only the current release candidate receives best-effort security fixes.

## Reporting a vulnerability

Use **Report a vulnerability** in this repository's GitHub Security tab:
<https://github.com/paulgreenbox-beep/dungeon-system/security/advisories/new>.
Do not put credentials, private project files, CAS payloads or working exploits
in a public issue. If private reporting is unavailable, open a minimal issue
requesting a private reporting channel, without sensitive details.

Include the version, operating system, affected command/tool, expected behavior,
and a minimal synthetic reproduction. Remove private paths and secrets. Do not
attach local execution environments or third-party source without permission.
No response-time or bounty commitment is made.

## Security boundaries

- Default STDIO MCP writes require an explicit absolute physical `project_root` exactly
  matching `DUNGEON_WRITABLE_ROOTS`. The default `[]` permits no writes.
- The backend also enforces output boundaries; do not treat client approval
  settings or a path-string prefix as filesystem containment.
- Read-only navigation is portable. Hardened core writes are Windows-only;
  unsupported platforms fail closed. Reparse/junction aliases, traversal, UNC
  write roots and hard-linked output leaves are rejected.
- Windows writers preflight destinations, pin identities, stage bytes and
  roll back failures they catch. This is **not** a crash-safe multi-file
  transaction or a defense against an administrator/storage owner changing
  DACLs, privileged handles or preexisting writable mappings. Snapshot child
  correspondence is checked after rename, not atomically throughout the gap.
- A CAS hash or seal establishes byte integrity, not execution authenticity.
  Current proof separately checks Task Trace, typed checks, raw correspondence,
  source/build/revision and SKLT obligation freshness. Unsupported receipts
  cannot establish `VERIFIED`.
- The narrow Toolkit adapter does not authenticate the executor, prove physical
  GPU execution, or establish complete GUI/application acceptance.

Use synthetic fixtures and explicitly approved roots for testing. Never run
adversarial write tests against someone else's project or production storage.

See [the public hardening summary](docs/audits/public-hardening-v041.md) and
[the proof adapter contract](docs/proof-adapters-v041.md).

## Optional shared HTTP transport (bounded canary, not independently verified)

The optional HTTP coordinator passed a bounded Windows two-client read-only
canary and controlled worker-lifecycle tests; see the
[validation report](docs/integrations/dungeon-shared-http-validation.md).
It is not covered by the previous stdio hardening gate or independently audited.
Real HTTP/CAS writes and live Codex cutover remain unaccepted. Before enabling it,
perform the separate checks in the [HTTP migration guide](docs/integrations/dungeon-shared-http.md).

- Bind is restricted to IPv4 loopback. Bearer credentials are mandatory;
  Host/Origin validation, bounded connections/bodies/queue/history, session
  ownership and per-client exact project roots are additional controls.
- HTTP write permissions come only from the operator's private client policy,
  defaulting to no writable roots; ambient stdio write grants are not inherited.
  Admin shutdown credentials cannot be used as an MCP tool capability. Do not
  give multiple trust domains the same client credential.
- Use dedicated `DUNGEON_HTTP_` credential environment names. After reading its
  private configuration, the coordinator removes these names from its own
  environment before spawning workers. Authentication secrets stay in the
  coordinator, not in worker or VCS subprocess environments. No persistent OS
  environment or per-client backend environment is changed.
- The HTTP service is not an OS sandbox or a remotely exposed multi-user
  service. Operators approve registered projects, their manifests/CAS and input
  roots; use the same trusted local account. Do not expose it through a tunnel
  or proxy or forward arbitrary credentials/environment from clients.
- The shared scheduler serializes a project's operations and fences all writes
  across its pool, including shared CAS. It does not add a common transaction
  lock to independent CLI/legacy stdio processes. Quiesce legacy writers before
  enabling HTTP writes; retain existing core path/publication guards.
- Queued work can be cancelled. Running reads use owned process-tree cleanup.
  Started writes are not force-killed: cancellation is deferred, deadlines
  return UNKNOWN, and locks remain held until actual completion. An unexpected
  mutator exit quarantines readiness. Neither disconnect nor timeout proves
  rollback. Do not replay a write whose outcome is unknown.
- Shutdown drains rather than killing a mutator after its grace deadline.
  An indefinitely blocked writer can keep the service draining; no hard-stop
  or crash-recovery guarantee is made. Operator recovery must inspect actual
  project/CAS state before restarting or retrying mutations.
- Prefer two-phase shutdown: admin `drain` closes admissions but keeps the
  service and bounded operation history available; resolve write outcomes,
  then use admin `stop`. The in-memory history is not durable and disappears
  after exit or TTL expiry. A direct stop/signal does not acknowledge that an
  UNKNOWN result was received by its caller; reconcile manually if necessary.
- `aiohttp` is an optional separately installed dependency, not vendored source.
  The pinned release identifies its license as `Apache-2.0 AND MIT`; review
  transitive dependency licenses, advisories and an installation lock before
  deployment. The source repository's MIT license does not relicense dependencies.
