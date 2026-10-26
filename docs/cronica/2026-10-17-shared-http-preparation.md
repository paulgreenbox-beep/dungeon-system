# Shared local MCP transport preparation — 2026-10-17

## Intent

Prepare direct Streamable HTTP connections for multiple Codex clients instead
of a separate Dungeon stdio Python process per connection. Existing tool
handlers, schemas and structural authority remain canonical. This request does
not authorize switching the installed plugin, editing managed caches/global
Codex settings, stopping live tasks, removing/archiving files, or running tests
and builds.

## Source changes

- Add an opt-in HTTP coordinator, a bounded dispatcher and reusable spawn
  workers. No per-chat proxy or automatic launcher activation is introduced.
- Add immutable request-scoped client capabilities to the existing bridge;
  stdio keeps its existing environment-based defaults and the same tool list.
- Require explicit HTTP `project_root`; retain physical-root and core write
  guards. Do not change per-request cwd/environment or retain project caches.
- Serialize each project's calls, with a service-wide exclusive mutation
  barrier across projects/CAS. Existing CLI/stdin processes are outside this
  scheduler; migration must quiesce independent writers before HTTP writes.
- Bound connections, queue, sessions, per-client/session work, deadlines,
  response history and worker restarts. Read cancellation uses only owned
  workers/process trees. A started mutator is never force-killed automatically.
- Retain UNKNOWN outcomes after write deadlines, quarantine unexpected write
  worker exits, and expose authenticated health/drain/operation inspection.
- A separate admin drain closes admissions while keeping operation inspection
  available. Operators resolve unknown outcomes before a separate stop request;
  bounded in-memory operation history is not a durable recovery log.
- Supply optional dependency and config templates plus a migration/rollback
  guide. Keep the actual plugin manifest/config and launcher unchanged.

## Verification boundary

Source inspection and whitespace/diff review only. No tests, builds, Python
imports/compilation checks, package installation, HTTP launch, client switch or
process-reduction measurement is claimed. The old hardening/Scion gates do not
verify this new transport. Future gates are listed in
`docs/integrations/dungeon-shared-http.md`.

The default target is one coordinator and two workers on Windows, not a
measured process count. POSIX multiprocessing may add a resource-tracker helper;
backend VCS queries can temporarily spawn their own bounded command processes.
No current runtime evidence, coverage grade, SKLT state, projection, snapshot,
release status or installed tool catalog is upgraded by these source changes.
