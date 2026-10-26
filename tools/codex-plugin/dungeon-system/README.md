# Dungeon System — Codex MCP plugin

Codex MCP plugin for structural navigation and evidence in existing repositories.

Copyright © 2026 Киселюк Павел Геннадьевич. Licensed under the MIT License; see [the bundled license](LICENSE).

> **Source status: v0.4.2-rc1 — Runtime-tested / Experimental / Independent audit pending.** The existing `v0.4.1-rc1` tag remains the older prerelease; no new Release or tag is created for this source update.

## Package contents

Optional opt-in transport: `mcp/http_service.py` provides a shared localhost
Streamable HTTP coordinator with a fixed process pool, scoped client sessions
and a bounded queue. The same `server.py` handlers and schemas are reused.
The stdio launcher and `.mcp.json` remain the default and are unchanged.
HTTP requires `requirements-http.txt`. A dedicated Windows environment passed
48 MCP tests, including a real two-client read-only HTTP canary; see the
[bounded validation report](../../../docs/integrations/dungeon-shared-http-validation.md).
No Codex settings or live MCP connections were switched. Do not migrate until the
[HTTP acceptance/migration guide](../../../docs/integrations/dungeon-shared-http.md)
has been followed by an operator. Examples are templates, not active settings.

- `mcp/server.py` implements the MCP bridge with the Python standard library and imports the project CLI from `tools/project_structure` in the full source clone.
- `scripts/launch_mcp.cmd` selects a Python executable on Windows; `DUNGEON_PYTHON` can select it explicitly.
- `skills/` contains six Codex skills: `dungeon-adoption`, `dungeon-evidence`, `dungeon-navigate`, `dungeon-review`, `dungeon-structure`, and `dungeon-task-trace`.
- The `0.4.2-rc1` source candidate declares 34 stdio MCP tools: the original 30 plus four experimental, read-only VersionScope queries. It retains six skills. The public `v0.4.1-rc1` tag remains at 30 tools. Source discovery is not installed-session discovery; see the [runtime report](../../../docs/integrations/scion-runtime-002-report.md) for the separately verified activation result.

This package requires a **full clone** of the public repository and Python 3.11+:

```powershell
git clone https://github.com/paulgreenbox-beep/dungeon-system.git Dungeon-System
Set-Location Dungeon-System
git checkout main
```

The existing `v0.4.1-rc1` tag is the 30-tool baseline; check it out only to reproduce that older version. The 34-tool count above applies to the `0.4.2-rc1` source on `main`. The recorded installed `0.4.2-rc1` package and a fresh Codex CLI session separately exposed 34 tools; an already-open chat may retain an older catalog. See the [runtime/activation report](../../../docs/integrations/scion-runtime-002-report.md). Source publication is not a new Release/tag or independent acceptance.

A plugin copied or installed into a Codex cache does not contain the CLI source that the MCP server imports. Keep the clone available and set `DUNGEON_PROJECT_ROOT` to its absolute root even when Codex loads the plugin from a cache. The plugin declares `env_vars` to forward that variable and optional `DUNGEON_PYTHON` through Codex's environment filter; it contains no machine-specific checkout value. For every other repository, pass that repository's absolute `project_root` explicitly.

## Configure the clone root

Before starting Codex, persist the source clone root in your per-user environment. Run this PowerShell snippet from the clone root:

```powershell
$dungeonHome = (Get-Location).Path
[Environment]::SetEnvironmentVariable('DUNGEON_PROJECT_ROOT', $dungeonHome, 'User')
[Environment]::SetEnvironmentVariable('DUNGEON_WRITABLE_ROOTS', '[]', 'User')

# Optional: select Python for the Windows launcher.
$pythonPath = (Get-Command python).Source
[Environment]::SetEnvironmentVariable('DUNGEON_PYTHON', $pythonPath, 'User')
```

Restart Codex after changing per-user environment values. Keeping `DUNGEON_WRITABLE_ROOTS` as `[]` preserves read-only behavior.

### Optional Scion profile (experimental)

Scion is an experimental Linux adapter path. Bounded real-CLI/TestCluster gates A-C passed in a disposable native-ext4 fixture under Ubuntu WSL; this does not establish production readiness or general WSL/DrvFS support. The separate Windows mirror capture remained safely unsealed because the source revision was unknown. The bundled `.mcp.json` `env_vars` allowlist forwards only `DUNGEON_PROJECT_ROOT` and `DUNGEON_PYTHON`. If an operator configures a Scion profile for a Linux MCP server, explicitly add the required per-profile names to that server's `env_vars` list, for example:

```json
"env_vars": [
  "DUNGEON_PROJECT_ROOT",
  "DUNGEON_PYTHON",
  "DUNGEON_SCION_LAB_ENDPOINT",
  "DUNGEON_SCION_LAB_TOKEN",
  "DUNGEON_SCION_LAB_CA_FILE",
  "DUNGEON_SCION_LAB_TRANSPORT"
]
```

Then provide the endpoint, token, optional CA path, and Linux-native transport to the MCP process through the operator's secure environment/secret mechanism. Without the explicit MCP allowlist, variables in the parent Codex environment do not reach the child server. `DUNGEON_SCION_LAB_DISTRIBUTION` is reserved and unused. Never commit secret values or place a token in command arguments; no OpenAI API key is required. See [Scion integration and its runtime check](../../../docs/integrations/scion.md).

## Install options

### Manual MCP registration

Run from the full clone root in PowerShell:

```powershell
$dungeonHome = (Get-Location).Path
$env:DUNGEON_PROJECT_ROOT = $dungeonHome
$env:DUNGEON_WRITABLE_ROOTS = '[]'

codex mcp add dungeon-system --env "DUNGEON_PROJECT_ROOT=$dungeonHome" --env 'DUNGEON_WRITABLE_ROOTS=[]' -- python -B "$dungeonHome/tools/codex-plugin/dungeon-system/mcp/server.py"
```

### Repository-local plugin marketplace

After the repository-local `.agents/plugins/marketplace.json` registers `dungeon-system-source`, run from the clone root:

```powershell
codex plugin marketplace add .
codex plugin add dungeon-system@dungeon-system-source
```

These are Codex CLI instructions. No marketplace UI installation receipt is claimed for this prerelease. Configure `DUNGEON_PROJECT_ROOT` before starting the Codex app so the server can resolve the full clone.

### Skills

Plugin installation supplies the six Skills and the `references/` pages together.
Manual MCP registration supplies tools only. Do not copy just the six Skill
directories, because their relative links require the package layout. Use one
MCP installation option, not duplicate registrations under the same server name.

## Use the MCP tools

Start by calling `dungeon_project_probe` with an absolute `project_root`. The project modes are `NOT_DUNGEON`, `SKLT_ONLY`, `MANAGED`, `INVALID_MANAGED`, and `UNAVAILABLE`. The probe is diagnostic and does not initialize project files.

For navigation, select summaries and objects before inspecting one bounded SKLT:

```text
dungeon_project_summary
  → dungeon_list_modules
  → dungeon_list_objects
  → dungeon_inspect_object
```

Preserve each parser-returned `object_id` exactly. It is a logical identity, not a filesystem path. Use the declared source references and source-link audit; file existence does not establish semantic correspondence.

- Adoption planning is read-only. Applying a reviewed plan requires explicit accepted module IDs.
- Task Trace records a development task, its object/source/test references, checks, evidence, and result. It does not schedule agents.
- Prototype and Product coverage are separate, default to `UNKNOWN`, and do not change SKLT/RARE, EXP, or grade.
- Evidence manifests and seals identify stored evidence. CAS bytes are external to this repository; immutable hashes alone do not show that evidence applies to current source or a current build.

## CLI

Run from the full clone root. For another project, provide its absolute root:

```powershell
python tools/project-structure.py project-probe --root '<ABSOLUTE_PROJECT_ROOT>' --json
python tools/project-structure.py project-summary --root '<ABSOLUTE_PROJECT_ROOT>' --json
python tools/project-structure.py modules --root '<ABSOLUTE_PROJECT_ROOT>' --json
python tools/project-structure.py objects --module '<MODULE>' --root '<ABSOLUTE_PROJECT_ROOT>' --json
python tools/project-structure.py source-link-audit --object-id '<OBJECT_ID>' --root '<ABSOLUTE_PROJECT_ROOT>' --json
python tools/project-structure.py task-trace-list --root '<ABSOLUTE_PROJECT_ROOT>' --json
```

To regenerate the clone's derived projections and then review them:

```powershell
python tools/project-structure.py refresh --root "$dungeonHome" --json
python tools/project-structure.py status --root "$dungeonHome" --json
python tools/project-structure.py validate --root "$dungeonHome" --json
python tools/project-structure.py snapshot '<NAME>' --root "$dungeonHome" --json
```

`refresh` writes derived projections and the object index. It does not create a snapshot; snapshots require the explicit `snapshot` command. Generated indexes, projections, snapshots, and publication-check output are not shipped as source content.

## Tests and release checks

Install development dependencies and run the two source test suites from the clone root:

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -t . -v
python -m unittest discover -s tools/codex-plugin/dungeon-system/tests -v
```

Checks requiring two original private audit-packet inputs report `SKIP` when those inputs are absent from a public clone. The public source does not fabricate replacement inputs.

The October 12 hardening gate and October 15 local baseline each ran 234 cases:
233 PASS, no FAIL/ERROR, one Windows symlink-privilege SKIP. These historical
totals exclude new publication checks and public-clone private-packet skips.
Independent re-audit is pending. See the [public hardening summary](../../../docs/audits/public-hardening-v041.md).

Regression-01 for the `0.4.2-rc1` candidate ran 322 cases: 320 PASS, two
SKIP (Windows error 1314 in the original case and the new plugin-backup case),
zero FAIL/ERROR; inputs were unchanged. The 16/16 pre-activation cache backup
verification is recorded at
`tools/codex-plugin/backups/dungeon-system-before-activation-20261016T082600Z`.
Installed-cache and fresh-session discovery are separate checks. See
the [runtime check](../../../docs/integrations/scion-runtime-check.md) and
[runtime report](../../../docs/integrations/scion-runtime-002-report.md).

The public release-check workflow is:

```powershell
python tools/publication/verify.py test --output artifacts/publication-tests
python tools/publication/verify.py smoke --output artifacts/publication-smoke
python tools/publication/verify.py scan --staged --output artifacts/publication-staged-scan
```

## Security and limits

- Read-only discovery and navigation are portable. Hardened writes are Windows-only.
- All eight MCP write tools require a caller-supplied absolute physical `project_root` exactly matching an explicitly configured writable-root entry. The default allowlist `[]` authorizes no writes, and writes never inherit the default clone root.
- Leave `DUNGEON_WRITABLE_ROOTS=[]` for read-only use. An explicit writable-root configuration is an authorization boundary, not a convenience default.
- Writers reject unsafe root/path forms and stage changes. Caught failures are rolled back, but writes are not crash-safe multi-file transactions and do not defend against privileged hostile writers.
- Structural declarations and evidence links do not prove runtime correctness, semantic source correspondence, or product readiness.
- Rust_Makepad_Toolkit is an external execution boundary; its code, runtime, and images are not distributed or endorsed here.

See [SECURITY](../../../SECURITY.md), [CONTRIBUTING](../../../CONTRIBUTING.md), [third-party notices](../../../THIRD_PARTY_NOTICES.md), and the [root project guide](../../../README.md).
