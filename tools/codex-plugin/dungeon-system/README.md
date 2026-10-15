# Dungeon System — Codex MCP plugin

Codex MCP plugin for structural navigation and evidence in existing repositories.

Copyright © 2026 Киселюк Павел Геннадьевич. Licensed under the MIT License; see [the bundled license](LICENSE).

> **Release status:** tag `v0.4.1-rc1` is a prerelease. Independent re-audit is pending; this release is **not independently verified**.

## Package contents

- `mcp/server.py` implements the MCP bridge with the Python standard library and imports the project CLI from `tools/project_structure` in the full source clone.
- `scripts/launch_mcp.cmd` selects a Python executable on Windows; `DUNGEON_PYTHON` can select it explicitly.
- `skills/` contains six Codex skills: `dungeon-adoption`, `dungeon-evidence`, `dungeon-navigate`, `dungeon-review`, `dungeon-structure`, and `dungeon-task-trace`.
- The plugin preserves all 30 existing MCP tool names.

This package requires a **full clone** of the public repository and Python 3.11+:

```powershell
git clone https://github.com/paulgreenbox-beep/dungeon-system.git Dungeon-System
Set-Location Dungeon-System
git checkout v0.4.1-rc1
```

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
