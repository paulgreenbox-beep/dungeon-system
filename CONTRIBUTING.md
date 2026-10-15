# Contributing

This release candidate is open for focused fixes, tests and documentation.
Independent re-audit is pending; do not promote local test results to an
independent security or runtime acceptance claim.

## Workflow

1. Read `docs/project.md` and `docs/map.md`. Probe the explicit project root,
   select an object, and read its SKLT before broad source inspection.
2. Keep changes small. Preserve `object_id`, the 30 MCP tool names and the six
   Skill names unless a separately reviewed compatibility change is intended.
3. Keep SKLT/RARE structural states separate from Prototype/Product coverage.
   Do not manually edit `.exp`, `.grade`, object indexes or state projections.
4. Test a change at the narrowest useful level, then run the full checks below.
   Add synthetic regression fixtures; never copy private evidence or another
   project's source into tests.
5. Explain meaningful structural changes in Cronica. Regenerate projections
   with the tool only, validate again, and take a local structural snapshot
   when appropriate. Generated/local snapshots are not publication artifacts.

## Development checks

Python 3.11+ is required. Windows is required for hardened writes and their
native filesystem tests. Read-only navigation also works on other platforms;
do not turn unsupported security checks into a false PASS.

```text
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -t . -v
python -m unittest discover -s tools/codex-plugin/dungeon-system/tests -v
python tools/project-structure.py validate --json
```

Use the repository's public verification helper for the clean-package MCP,
Skill and publication checks described in the README. Report PASS, FAIL, ERROR
and SKIP separately, including privilege/platform/private-fixture skip reasons.
Application/GPU execution is not implied by these checks.

## Pull requests and licensing

Describe the problem, scope, verification and remaining limits. Do not include
tokens, machine-specific configuration, binaries, CAS blobs, packs, private
audit packets, installed plugin caches or backup trees. Review the actual Git
diff and staged file list, not only `.gitignore`.

Contributions must be yours to license under the repository's MIT License.
Identify any third-party material and retain its required notices. Do not
relicense external projects or imply their endorsement. Report vulnerabilities
privately as described in [SECURITY.md](SECURITY.md).
