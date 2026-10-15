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

- MCP writes require an explicit absolute physical `project_root` exactly
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
