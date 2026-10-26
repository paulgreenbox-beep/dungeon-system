# Dungeon System

Dungeon System is a source-first structural navigation and evidence tool for existing repositories. Its CLI and Codex MCP plugin help inspect declared project structure, connect tasks to source and tests, and keep implementation evidence distinct from structural declarations.

Copyright © 2026 Киселюк Павел Геннадьевич. Licensed under the MIT License; see [LICENSE](LICENSE).

> **Source status: v0.4.2-rc1 — Runtime-tested / Experimental / Independent audit pending.** This is a source update on `main`, not a new GitHub Release or tag. It has **not** been independently verified.

## What it contains

- 34 MCP tools in the source package: the 30 existing tools plus four experimental, read-only VCS queries for declared VersionScopes.
- Six Codex skills: `dungeon-adoption`, `dungeon-evidence`, `dungeon-navigate`, `dungeon-review`, `dungeon-structure`, and `dungeon-task-trace`.
- A Python CLI and MCP server implemented with the Python standard library and this repository's own `tools/project_structure` package.

An **optional shared localhost Streamable HTTP service** is included as an
opt-in experimental route with one coordinator and a
bounded worker pool. It reuses the same 34-tool registry and requires the
separate optional HTTP dependency. A **bounded Windows two-client canary and
48 MCP tests passed**; see the [validation report](docs/integrations/dungeon-shared-http-validation.md).
This is not independent verification or live-write acceptance. The installed
plugin, Codex settings and default stdio configuration remain unchanged.
See [migration, rollback and required acceptance checks](docs/integrations/dungeon-shared-http.md).

The existing `v0.4.1-rc1` tag remains the 30-tool baseline. The `0.4.2-rc1` source candidate adds four experimental VCS queries. Bounded real Scion CLI/TestCluster and Dungeon backend checks passed on native Linux/ext4; the installed launcher and a fresh Codex CLI session exposed 34 tools. Existing chats may retain an older catalog. See the [runtime and activation report](docs/integrations/scion-runtime-002-report.md) for the exact gates and limitations. Production Windows/WSL Scion transport and the Scion execution-proof adapter remain unsupported. Git stays the default; Scion is optional and read-only. Source publication is not production acceptance or independent verification.

The full source clone is required. The MCP server imports the CLI implementation from the clone, so an installed plugin cache by itself is not a complete installation. Set `DUNGEON_PROJECT_ROOT` to the absolute root of the full clone, including when Codex loads the plugin from its cache. For every other repository, pass its own absolute `project_root` explicitly.

The source server reports `0.4.2-rc1`; the existing `v0.4.1-rc1` source-distribution tag retains server version `0.4.1`.

## Architecture and boundaries

`SKLT` files are the structural source of truth. `GLIF` files are UI projections linked by exact logical `object_id` values. The in-memory navigation path is:

```text
project probe → mode → module → object_id → SKLT
                                      ├─ Task Trace → declared source/test refs
                                      ├─ Prototype and Product coverage
                                      └─ execution evidence → manifest/seal → external CAS
```

- `NOT_DUNGEON`, `SKLT_ONLY`, and `MANAGED` describe observed project modes; `INVALID_MANAGED` and `UNAVAILABLE` report problems.
- Adoption planning is read-only. Applying an adoption plan requires an explicit accepted set of modules.
- Task Trace records work and references; it does not schedule agents.
- Prototype and Product coverage are separate from SKLT/RARE, EXP, and grade.
- Evidence manifests and seals are small records. Immutable evidence bytes live in an external content-addressed store; CAS integrity alone does not establish that evidence applies to current source.
- Rust_Makepad_Toolkit is an external execution boundary. Its code, runtime, and images are not included or endorsed by this repository.

Git remains the default history backend. Optional VersionScopes preserve existing GitScope names and SKLT references; the experimental Scion backend does not change the 22 existing ObjectIDs. Scion read-only revision and history queries, typed snapshot/evidence provenance, platform limits, and unsupported operations are documented in [VCS backends](docs/vcs-backends.md) and the [Scion integration guide](docs/integrations/scion.md).

## Source install and Codex setup

Obtain the **full public source clone**, check out tag `v0.4.1-rc1`, and run commands from the clone root (Python 3.11+):

```powershell
git clone https://github.com/paulgreenbox-beep/dungeon-system.git Dungeon-System
Set-Location Dungeon-System
git checkout v0.4.1-rc1
```

This is a prerelease tag.

Generated projections and local snapshots are intentionally absent. On Windows,
bootstrap the included managed example explicitly before asking for a clean
status/validation result:

```powershell
python tools/project-structure.py refresh --json
python tools/project-structure.py snapshot initial-local --json
python tools/project-structure.py status --json
```

On other platforms use read-only probing/navigation; hardened writes fail closed.

Before starting the Codex app, persist the clone root and read-only default in your per-user environment. Run this PowerShell snippet from the clone root:

```powershell
$dungeonHome = (Get-Location).Path
[Environment]::SetEnvironmentVariable('DUNGEON_PROJECT_ROOT', $dungeonHome, 'User')
[Environment]::SetEnvironmentVariable('DUNGEON_WRITABLE_ROOTS', '[]', 'User')

# Optional: select the Python executable used by the Windows launcher.
$pythonPath = (Get-Command python).Source
[Environment]::SetEnvironmentVariable('DUNGEON_PYTHON', $pythonPath, 'User')
```

Restart Codex after changing per-user environment values. The public plugin currently forwards only `DUNGEON_PROJECT_ROOT` and optional `DUNGEON_PYTHON` through `env_vars`; an operator enabling the experimental Scion integration must explicitly allowlist the required per-profile variables in MCP configuration, or the Linux MCP server will not receive them. Keep tokens in secure process environment, never in committed configuration. See the [Scion setup and runtime limits](docs/integrations/scion.md). The plugin never embeds a machine-specific clone path, and its configured writable-root list stays `[]` by default.

### Manual MCP registration

From the clone root in PowerShell:

```powershell
$dungeonHome = (Get-Location).Path
$env:DUNGEON_PROJECT_ROOT = $dungeonHome
$env:DUNGEON_WRITABLE_ROOTS = '[]'

codex mcp add dungeon-system --env "DUNGEON_PROJECT_ROOT=$dungeonHome" --env 'DUNGEON_WRITABLE_ROOTS=[]' -- python -B "$dungeonHome/tools/codex-plugin/dungeon-system/mcp/server.py"
```

### Plugin marketplace registration

The repository-local marketplace manifest registers `dungeon-system-source` in
`.agents/plugins/marketplace.json`. From the clone root:

```powershell
codex plugin marketplace add .
codex plugin add dungeon-system@dungeon-system-source
```

These are documented CLI steps; no Codex marketplace UI installation receipt is claimed for this prerelease.

### Skills

The plugin installation supplies all six Skills and their bundled reference
pages together. Manual MCP registration alone supplies tools, not Skills.
Do not copy only the six Skill directories: their relative reference links need
the package layout. Do not install both MCP options under the same server name.

## CLI

Run from the clone root. Read-only examples:

```powershell
python tools/project-structure.py status --json
python tools/project-structure.py scan --json
python tools/project-structure.py validate --json
python tools/project-structure.py project-probe --root '<ABSOLUTE_PROJECT_ROOT>' --json
python tools/project-structure.py modules --root '<ABSOLUTE_PROJECT_ROOT>' --json
python tools/project-structure.py objects --module '<MODULE>' --root '<ABSOLUTE_PROJECT_ROOT>' --json
python tools/project-structure.py source-link-audit --object-id '<OBJECT_ID>' --root '<ABSOLUTE_PROJECT_ROOT>' --json
```

To regenerate derived projections for the clone, then inspect the result:

```powershell
python tools/project-structure.py refresh --root "$dungeonHome" --json
python tools/project-structure.py status --root "$dungeonHome" --json
python tools/project-structure.py validate --root "$dungeonHome" --json
python tools/project-structure.py snapshot '<NAME>' --root "$dungeonHome" --json
```

`refresh` writes derived projections and the object index. Snapshots are created separately with the explicit `snapshot` command. Generated indexes, projections, snapshots, and publication-check output are not shipped as source-release content.

## Tests and public-source verification

Install development dependencies only when running tests:

```powershell
python -m pip install -r requirements-dev.txt -r tools/codex-plugin/dungeon-system/requirements-http.txt
python -m unittest discover -s tests -t . -v
python -m unittest discover -s tools/codex-plugin/dungeon-system/tests -v
```

The optional HTTP requirements are included here because the complete plugin
suite launches a disposable two-client HTTP canary. They are not required for
the default STDIO runtime. Tests need permission to use local loopback sockets.

Two checks requiring original private audit-packet inputs report `SKIP` when those inputs are absent from a public clone. The public source does not fabricate replacement packets.

The October 27, 2026 update checks ran 349 tests: 347 PASS / 2 SKIP locally,
and 345 PASS / 4 SKIP on the exported public source, with no FAIL/ERROR and
unchanged test inputs. All 48 plugin tests passed, including the real two-client
HTTP canary; source-launcher smoke confirmed 34 tools and six Skills. The skips
are two Windows symlink-privilege cases and, in the public copy only, two private
audit-packet replays. An initial public-copy run exposed two test assumptions:
a preexisting local artifact directory and immediate worker reaping after an
UNKNOWN response. Both tests were corrected and both full suites rerun; the
earlier failed receipts remain local. This is not an independent audit or a new
Scion runtime build; the dated real Scion checks are recorded separately above.

The recorded hardening gate ran on October 12, 2026. An October 15 local baseline repeated the same counts: 234 cases, 233 `PASS`, and one `SKIP` because Windows symbolic-link creation privilege was unavailable. This is not an independent re-audit. These historical totals exclude the new publication-check tests and do not describe a public clone missing the two private packets. See the [public hardening summary](docs/audits/public-hardening-v041.md).

Fresh source-publication verification on October 15 ran 249 cases: the local
checkout produced 248 PASS / 1 SKIP; the exact public source produced 246 PASS /
3 SKIP, with no failures or errors. The public skips are the two private-packet
replays and Windows symlink privilege. Actual isolated Codex installation,
30-tool/6-Skill discovery and a real project read also passed. Earlier intermittent
Windows cleanup and snapshot-rename failures remain documented in the public
summary; these passing runs do not establish their complete cause or independent
acceptance.

The public release-check workflow is:

```powershell
python tools/publication/verify.py test --output artifacts/publication-tests
python tools/publication/verify.py smoke --output artifacts/publication-smoke
python tools/publication/verify.py scan --staged --output artifacts/publication-staged-scan
```

## Limits and security

- Read-only discovery and navigation are portable. Hardened writes are Windows-only.
- All eight MCP write tools require a caller-supplied absolute physical `project_root` that exactly matches an explicitly configured writable-root entry. The default allowlist `[]` authorizes no writes; writes never inherit the default clone root.
- For read-only use, leave `DUNGEON_WRITABLE_ROOTS=[]`. Do not add a root unless you intend to authorize writes there.
- Structural links do not establish semantic correspondence or runtime correctness. Unknown evidence stays unknown.
- Windows writers reject unsafe path forms and stage changes, but do not claim crash-safe multi-file transactions or protection against privileged hostile writers.

Read [SECURITY](SECURITY.md), [CONTRIBUTING](CONTRIBUTING.md), and [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md). The [plugin guide](tools/codex-plugin/dungeon-system/README.md) covers Codex setup and MCP behavior.
