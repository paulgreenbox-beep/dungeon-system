# Shared local Streamable HTTP MCP: migration and acceptance

> **Status: BOUNDED WINDOWS TWO-CLIENT CANARY PASSED — NO CODEX CUTOVER.** On 2026-10-17 a dedicated environment passed 48 MCP tests, including two independent authenticated HTTP clients on synthetic read-only projects and real spawned-worker lifecycle checks with a controlled backend. See the [validation report](dungeon-shared-http-validation.md) for the exact boundary. This is not independent verification or live-write acceptance. Codex settings, installed plugin/cache and live MCPs remain unchanged; the test service was stopped.

## Scope and current route

The current connection remains stdio: the installed plugin's `.mcp.json` → `scripts/launch_mcp.cmd` → `mcp/server.py`. The matching source paths in this checkout are under `tools/codex-plugin/dungeon-system/`. Keep that launcher, its stdio configuration, and the installed plugin/skills unchanged until the explicit cutover gate below. This is not a per-chat proxy design.

The shared service is a separate entry point: `mcp/http_service.py serve --config <ABSOLUTE_PRIVATE_JSON>`. The bounded canary exercised this entry point and verified parity with all 34 existing tool schemas and `server._handle`. It does not change the default stdio route. Production writes and live Codex integration still require the acceptance gates below.

`mcp/...` below is shorthand relative to the plugin package, not the clone root.
For real operator commands use the absolute script path, independent of cwd.
The prepared HTTP files are working-tree changes, not part of public tag
`v0.4.1-rc1`; an isolated acceptance clone must contain the reviewed prepared
source as well as the full backend before these commands can be used.

## Proposed service contract

- One coordinator with two persistent `multiprocessing` **spawn** workers by default (`workers` allowed range 1–4). The tested Windows venv launch had two dispatcher workers but **five live processes in its service tree** (launch PID plus four descendants), including Python launch indirection. Three was a design target, not the measured result. Client-count scaling and memory savings were not measured. POSIX helper-process behavior remains untested.
- Blocking IPC pipe reads/writes use a fixed `ThreadPoolExecutor` of `2 * workers` threads (four at the default), keeping pipe I/O off the HTTP event loop. It is service-wide, not per-client, and these threads are not additional Python processes.
- Optional HTTP dependency: `requirements-http.txt` pins `aiohttp==3.14.5`; multiprocessing is from the Python standard library. PyPI lists the release's license expression as `Apache-2.0 AND MIT` ([aiohttp 3.14.5 on PyPI](https://pypi.org/project/aiohttp/3.14.5/)). Do not vendor the dependency.
- Bind to `127.0.0.1`; the example uses port `8768`, but the configured port may change. Accept `Host` only as `127.0.0.1:<configured-port>` or `localhost:<configured-port>`, using that same configured port. Validate a supplied `Origin` against the corresponding loopback origins and reject an invalid one with HTTP 403. The MCP endpoint is `/mcp`; POST accepts one JSON-RPC message and returns JSON only, GET returns 405 (no SSE), and DELETE closes only the caller's owned session. The POST client must still send the MCP-required `Accept: application/json, text/event-stream` header. The transport's JSON response and 405 GET option are covered by the [Streamable HTTP specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports); local binding, Origin validation, and authentication are security requirements there as well.
- Session and operation IDs are collision-checked with up to four generation attempts. Session-ID exhaustion returns 503 without replacing an existing session; operation-ID exhaustion rejects admission and marks the dispatcher unready. Force both collision paths in future tests. Enforce the configured idle/session/request/connection limits below. `GET /healthz` accepts any valid client or admin credential. Both operation lookup routes require the client's Bearer credentials **and the original `MCP-Session-Id`**, and return only work owned by that principal/session. `GET /operations/<id>` reads by `operation_id`; `GET /operations?request_id=<URL-encoded-JSON-scalar>` locates the operation for the original JSON-RPC request ID if the final response/header was lost. Both use the same bounded history; it is not a durable recovery log.
- The `health` CLI uses the admin credential from the service config; this does not make `GET /healthz` admin-only. Only `POST /admin/drain`, `POST /admin/shutdown`, and CLI `drain`/`stop` are admin-only. `POST /admin/drain` and CLI `drain` close admissions, mark queued jobs not started, cancel reads, and defer started writes; they do not request process exit. Health stays available to any valid client/admin, and bounded operation history stays available to the original client principal/session. `POST /admin/shutdown` and CLI `stop` begin draining and request process exit after writes settle. Future CLI forms are `python mcp/http_service.py health --config "<ABSOLUTE_PRIVATE_JSON>"`, `python mcp/http_service.py drain --config "<ABSOLUTE_PRIVATE_JSON>"`, and `python mcp/http_service.py stop --config "<ABSOLUTE_PRIVATE_JSON>"`. Stop never force-kills an active mutator.

### Private JSON configuration template

This is a template only, not a file to create during this task. Replace every path placeholder with a normalized absolute path before use; `home_root` must be the exact absolute fullclone root containing the service source. Each client `project_roots` entry must be an explicitly allowed absolute project root.

```json
{
  "schema_version": 1,
  "home_root": "<ABSOLUTE_FULLCLONE_ROOT>",
  "host": "127.0.0.1",
  "port": 8768,
  "workers": 2,
  "queue_limit": 16,
  "request_timeout_sec": 45,
  "body_timeout_sec": 5,
  "startup_timeout_sec": 15,
  "shutdown_grace_sec": 30,
  "session_idle_sec": 900,
  "max_sessions": 64,
  "max_sessions_per_client": 8,
  "max_requests_per_session": 2048,
  "max_connections": 48,
  "max_http_requests": 32,
  "per_client_limit": 8,
  "per_session_limit": 4,
  "history_limit": 32,
  "history_ttl_sec": 300,
  "admin_token_env_var": "DUNGEON_HTTP_ADMIN_TOKEN",
  "clients": [
    {
      "id": "codex-local",
      "token_env_var": "DUNGEON_HTTP_CODEX_TOKEN",
      "project_roots": ["<ABSOLUTE_ALLOWED_PROJECT_ROOT>"],
      "writable_roots": [],
      "input_roots": []
    }
  ]
}
```

The admin and client token values are mandatory, different, and at least 32 characters each. Credential environment-variable names must use the dedicated `DUNGEON_HTTP_` prefix (maximum 128 characters); the examples above already comply. Do not reuse `GITHUB_TOKEN` or Scion token variables as HTTP credential names. Put only the environment-variable names in the service JSON and separate Codex TOML; supply values through the protected environment. After loading config, the service removes only these credential names from its own coordinator environment once, before spawning workers. Token values remain in coordinator config and are not passed through worker/backend-child environments. This is not per-client environment routing and does not change persistent User or Machine environment settings. Never put token values in config, command arguments, logs, screenshots, or this repository.

Store the private **service JSON** at `.project/http-service.local.json`; the current ignore rule explicitly covers the basename `http-service.local.json` (`**/http-service.local.json`). Do not generalize this to arbitrary `*.local.json` names. Codex MCP settings are separate operator TOML, not this service JSON. Verify the exact service path is ignored before creating it.

`input_roots: []` means no additional input directories: the selected project root is allowed for inputs, while other `project_roots` belonging to that client are not implicitly available to this operation. Each job policy contains only its selected project root plus separately operator-approved `input_roots`. `writable_roots: []` grants no writable project roots, so write tools fail closed; writes outside an explicitly writable project root are prohibited. The sample is therefore read-only until an operator separately approves and configures writable roots (which must be a subset of that client's project roots).

### Request isolation and path policy

Every tool request carries an explicit `project_root`. The HTTP control plane performs lexical rejection of missing roots, aliases, `..`, and UNC forms plus allowlist/routing checks without probing the selected target tree. Physical-root and junction/reparse validation runs inside the bounded worker before the backend operation. Build an immutable per-job `RequestPolicy` containing only the selected project root, its explicit write capability if approved, and configured additional `input_roots`; install it in a worker-local `ContextVar`, then reset it in `finally`. Other client projects are not implicitly visible as inputs. Do not implement client routing by changing process `cwd` or environment variables. Do not add a project-global mutable cache.

## Serialization and failure boundary

Within this service, operations for the same project are sequential. Every write takes one **service-wide exclusive barrier**, including shared-CAS mutations and their associated publish/metadata steps; preserve the existing core locks as additional protections, not substitutes for this barrier. This gives one serialization boundary for writes admitted by this coordinator, including writes to different projects that share CAS.

That boundary is **service-wide, not interprocess coordination with legacy CLI/STDIO** or any other process. Existing core locks do not establish one cross-process transaction over a complete project operation plus shared CAS. Therefore, before enabling any HTTP write during migration, quiesce and gate all legacy CLI/STDIO writers against the same project/CAS; do not allow mixed writers. The singleton lease and exclusive bind prevent a second copy of this HTTP service; they do not serialize legacy writers.

Cancellation and timeout promises are deliberately narrow:

- A queued **read** may be withdrawn before dispatch; when an active read is cancelled, recycle its worker using the owned process-group/Windows Job cleanup path. Limit worker restarts to **3 per rolling minute service-wide**. If the budget is exceeded, mark the service unready and require a manual restart only after operator review; do not keep restarting automatically. Count a read cancelled only after queue removal or active-worker cleanup is confirmed; client disconnect alone is not proof that work stopped.
- Never kill a **write job** because of request cancellation, timeout, or drain. Once a write has started, cancellation is deferred. At the 45-second service deadline, return `UNKNOWN` with an `operation_id`; keep the exclusive write barrier until the mutator actually finishes. The 55-second Codex tool timeout is a client-side ceiling, not proof that the write was aborted.
- Never automatically retry or replay an unknown write. After a disconnect or lost final `Dungeon-Operation-Id` header, first query `GET /operations?request_id=<URL-encoded-JSON-scalar>` with the original client credentials and MCP session; if it returns an operation ID, query `GET /operations/<id>` with the same identity. If lookup finds no record, history expires, or the outcome remains unknown, stop further writes and use manual recovery/reconciliation; do not resend the tool call or infer success/failure from the lost HTTP response.
- If a mutator crashes, quarantine service readiness and admit no more work until explicit operator recovery. Neither process termination nor the OS lease proves crash-safe commit/rollback. A 30-second shutdown grace expiry leaves the service DRAINING/unready while a write is unresolved; it is not permission to kill the mutator or remove its lock.

Hold a singleton OS lease at `<home_root>/.project/http-service.lock` for the service lifetime and bind exclusively to the configured loopback address. A stale/missing lease is not by itself evidence that an interrupted write is safe to replay. Recovery after crash requires explicit reconciliation.

## Migration procedure — future, operator-controlled

There is no active migration. Do not run these steps as part of this documentation change.

1. **Build and prove in an isolated fullclone first.** The main implementation files are now present, but still need review and acceptance. Verify them and the optional dependency without changing the stdio launcher/config. Run the acceptance checklist below against an isolated fullclone, including failure and concurrency cases. Do not use the live fullclone for mutating acceptance.
2. **Prepare private configuration and credentials.** Use the service JSON path `.project/http-service.local.json`, verify that exact path is ignored, and keep Codex MCP settings in a separate operator TOML. Set distinct 32+ character admin/client tokens through dedicated `DUNGEON_HTTP_*` environment names; do not reuse `GITHUB_TOKEN` or Scion token variables. Confirm `home_root` exactly matches the fullclone containing the service source, all allowed paths are explicit, and the host is loopback only. Do not store token values in either config.
3. **Complete existing work and freeze legacy writes.** Let current plugin tasks finish. Before any HTTP write can be admitted, ensure no stdio MCP, CLI, or other process can write the project or shared CAS. A read-only canary is acceptable; mixed writers are not. If this cannot be enforced, do not cut over.
4. **Start and canary the shared service without changing the default route.** The future invocation is `python mcp/http_service.py serve --config "<ABSOLUTE_PRIVATE_JSON>"`. Check authenticated admin health, service readiness, and a read-only MCP canary. Do not claim success from process presence alone. Keep the plugin installed and its skills available.
5. **Switch Codex only after the canary and existing tasks are complete.** First verify the exact plugin ID in Codex UI/config; never guess it. The official Codex docs support a user-config override under `plugins.<plugin>.mcp_servers.<server>` ([Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)). Only then may the operator disable the old plugin MCP server, for example:

   ```toml
   [plugins."<exact-plugin-id>".mcp_servers.dungeon-system]
   enabled = false
   ```

   Do not edit the installed plugin, managed cache, or plugin files. Then add the shared HTTP server to the operator's Codex config and reload the client:

   ```toml
   [mcp_servers.dungeon_shared]
   url = "http://127.0.0.1:8768/mcp"
   bearer_token_env_var = "DUNGEON_HTTP_CODEX_TOKEN"
   startup_timeout_sec = 15
   tool_timeout_sec = 55
   ```

   This is a future example, not a present setting. `8768` is the example/default port; if the private service config uses another port, match the URL. Keep the source `.mcp.json` → launcher → stdio path unchanged so it remains a deliberate rollback route.
6. **Recheck before enabling writes.** Confirm only the intended HTTP route is exposed to Codex, tool names and all 34 schemas match the stdio implementation, session/auth/policy isolation passes, and the legacy-writer freeze still holds. Admit writes only after every write/CAS barrier and unknown-operation gate below passes. No automatic fallback to stdio is allowed while a write is active or unresolved.

### Rollback — future, drain before routing back

1. **Phase 1 — drain, do not stop.** Use admin `drain --config` or `POST /admin/drain`. This closes admissions, marks queued work not started, cancels reads, and defers started writes; it does not set the service stop event. Keep the service running and preserve the legacy-writer freeze. Authenticated health remains available; operation/history lookups remain available to the original client principal using its Bearer token and original `MCP-Session-Id`.
2. **Resolve outcomes while history is live.** Poll health and operations. For a lost response/header, use `GET /operations?request_id=<URL-encoded-JSON-scalar>`, then `GET /operations/<id>` with the original client identity/session. Let every started write settle and resolve its final result; never retry/replay `UNKNOWN`. If history expires, an outcome remains unknown, or a mutator crashed, keep writers gated and manually reconcile project plus shared CAS before restoring any route. History is bounded/ephemeral, not crash-safe.
3. **Phase 2 — stop only after resolution.** Once writes have settled and required operation results have been retrieved/reconciled, use admin `stop --config` or `POST /admin/shutdown`; then verify process exit and lifetime-lease release before routing back. Stop begins graceful exit after writers settle. Direct stop or SIGTERM is also graceful, but the service exits after settling and its in-memory history is lost; neither proves an `UNKNOWN` result reached the caller. Do not kill a mutator or remove a live lease.
4. Remove the HTTP server entry from the operator's Codex config and restore the old plugin MCP server by removing the override or setting `enabled = true`, using the same verified plugin ID. Reload Codex and confirm the stdio route is the only route for these tools. Keep the plugin and its skills installed; do not hand-edit managed cache. Revoke/rotate HTTP tokens after rollback, updating only the protected environment when separately authorized.
5. If service failure released the lease unexpectedly, do **not** treat that as a clean stop. Keep all writers gated until an operator reconciles the last operation and CAS/project state; only then may stdio writes resume.

## Acceptance checklist — required before any live write cutover

These boxes are complete live-cutover gates, not individual unit-test results.
The bounded partial results in the validation report do not clear any combined
live-write/Codex acceptance gate below.

### Reproducible preflight

Use an operator-selected Python in a separately provisioned service environment and a reviewed fullclone. The recorded run used a dedicated ignored venv and only synthetic target projects; it did not grant writes to this checkout or its real CAS. Do not reuse the general work environment or its pip cache; `--no-cache-dir` keeps the optional install out of a shared cache, and `-B` avoids bytecode-cache writes. Substitute both absolute paths below. The first two unit/regression commands do not launch the service; the subsequent tests explicitly create and stop owned test processes.

```powershell
$OperatorServicePython = '<ABSOLUTE_PATH_TO_DEDICATED_SERVICE_ENV>\Scripts\python.exe'
$IsolatedFullclone = '<ABSOLUTE_PATH_TO_ISOLATED_FULLCLONE>'

& $OperatorServicePython -m pip install --only-binary=:all: --no-cache-dir -r "${IsolatedFullclone}\tools\codex-plugin\dungeon-system\requirements-http.txt"
& $OperatorServicePython -B "${IsolatedFullclone}\tools\codex-plugin\dungeon-system\tests\test_shared_http.py"
& $OperatorServicePython -B "${IsolatedFullclone}\tools\codex-plugin\dungeon-system\tests\test_multi_project.py"
& $OperatorServicePython -B "${IsolatedFullclone}\tools\codex-plugin\dungeon-system\tests\test_shared_http_worker_lifecycle.py"
& $OperatorServicePython -B "${IsolatedFullclone}\tools\codex-plugin\dungeon-system\tests\test_shared_http_integration.py"
```

The real HTTP integration test uses two independent wire clients, not two Codex
chats. It grants no writes, uses temporary projects and random credentials, and
requests authenticated drain/stop before checking owned process exit. Optional
`DUNGEON_HTTP_INTEGRATION_REPORT` names an absolute sanitized JSON report path.
The worker-lifecycle test uses real OS processes with a controlled synthetic
backend; it does not establish real CAS transaction/crash safety. Windows
environments that block local sockets can prevent asyncio initialization before
the tests reach application code; run in an explicitly approved local-socket
environment rather than treating such a stall as a transport result.

After those gates and explicit operator approval, use absolute paths for the
read-only service canary. Prepare the private JSON/secure environment first.
These are future commands, not actions performed by this task:

```powershell
$HttpService = "${IsolatedFullclone}\tools\codex-plugin\dungeon-system\mcp\http_service.py"
$ServiceConfig = "${IsolatedFullclone}\.project\http-service.local.json"
& $OperatorServicePython -B $HttpService serve --config $ServiceConfig

# In another operator shell with the same protected credential environment:
& $OperatorServicePython -B $HttpService health --config $ServiceConfig
& $OperatorServicePython -B $HttpService drain --config $ServiceConfig
# Resolve/reconcile write outcomes before requesting final exit:
& $OperatorServicePython -B $HttpService stop --config $ServiceConfig
```

Before any acceptance claim, add/perform forced collision cases for both ID generators: collisions retry within the four-attempt budget; exhaustion returns the documented failure, marks operation admission unready where applicable, and never overwrites an existing session or operation record.

- [ ] **Source and dependency:** HTTP frontend is separate; stdio defaults remain byte-for-byte/configuration-equivalent; all 34 tool schemas and `_handle` behavior are mapped; optional dependency is pinned exactly, installed only in the intended environment, license reviewed, and not vendored.
- [ ] **Config and secrets:** schema version and every bound/default above validate; malformed, duplicate, relative, or mismatched roots fail closed; `.project/http-service.local.json` is ignored (without assuming generic `.local.json` names are); service JSON and Codex operator TOML remain distinct; tokens are distinct, present, 32+ characters, and use dedicated `DUNGEON_HTTP_*` names; only those names are removed from the coordinator's own environment after config load and before worker spawn; User/Machine environment remains unchanged; no secret value appears in config, argv, logs, errors, worker or backend-child environments.
- [ ] **Process ownership/singleton:** spawn workers start under the coordinator; Windows process topology matches the three-process plan; POSIX helper-process behavior (including possible `resource_tracker`) is measured and reported separately; the fixed `2 * workers` IPC thread pool keeps blocking pipe I/O off the event loop and is shared across clients; startup timeout and partial-start cleanup are bounded; second service is refused by both lifetime OS lease and exclusive bind; lease is retained for the lifetime and not silently stolen after crash.
- [ ] **HTTP/MCP:** authenticated POST accepts one JSON-RPC message and returns JSON; protocol version and required Accept handling are correct; body-read timeout is bounded at 5 seconds; GET `/mcp` returns 405 without SSE; DELETE closes only the owned session; Host accepts only `127.0.0.1:<configured-port>` or `localhost:<configured-port>`; invalid supplied Origin returns 403; missing/invalid Bearer is rejected; `/healthz` accepts any valid client/admin credential; only drain/shutdown/stop reject client credentials (CLI health uses admin config); cancelled JSON-RPC requests receive a transport-compliant outcome. Current source can return HTTP 204 when a cancelled read yields `None`; verify the protocol outcome before acceptance.
- [ ] **Session isolation/limits:** random session IDs, principal binding, forced collision/retry/exhaustion behavior for both session and operation IDs without overwriting existing records, DELETE ownership, idle expiry, per-client/per-session/session/request/connection/HTTP limits, and history limit/TTL are enforced under concurrent requests. Both `/operations/<id>` and `/operations?request_id=...` require the original client principal and MCP session; the query value preserves JSON-scalar ID type, recovers the owned operation after a simulated lost final response/header, and cannot reveal another session's work.
- [ ] **Path and policy isolation:** the HTTP control plane performs lexical allowlist/routing without target-tree I/O; physical root and junction/reparse checks occur inside the bounded worker before backend operation; per-job policy contains only selected project root plus approved `input_roots`; other client projects are not implicit inputs; empty `input_roots` means no additional directories; empty `writable_roots` denies all write tools and writes outside explicit writable roots are rejected; no per-client cwd/env mutation or cross-request `ContextVar` leakage occurs in persistent workers; no global project cache is introduced.
- [ ] **Serialization/CAS:** same-project operations do not overlap; writes across projects are globally exclusive within this service only; the service-wide barrier is not interprocess coordination with legacy CLI/STDIO; a write remains exclusive through shared-CAS and publish/metadata completion; existing core locks remain active; fault injection at each write phase does not report a false success.
- [ ] **Mixed-writer gate:** a controlled cutover check proves all legacy stdio/CLI writers are quiesced before the first HTTP write; if that cannot be guaranteed, the live write gate stays closed.
- [ ] **Cancel/deadline/crash:** queued reads are removed before dispatch and cancelled active-read workers are recycled only after owned cleanup; restart accounting is service-wide and allows at most 3 worker restarts in a rolling minute; exceeding budget makes readiness unready and requires manual restart after review; write jobs are never killed for cancellation/timeout/drain; deadline yields `UNKNOWN` plus an operation ID while retaining the barrier; disconnect/lost-header recovery uses the two authenticated operation lookup routes and never resends the call; history expiry is handled as unresolved/manual recovery; mutator crash quarantines readiness and blocks admissions pending explicit recovery.
- [ ] **Drain/rollback:** admin drain/CLI `drain` closes admissions but leaves health and client-owned operation history available; verify queued jobs/read cancellation and deferred active writes. Then retrieve/resolve `UNKNOWN` outcomes before admin shutdown/CLI `stop`; no active mutator is killed; grace expiry stays unready/draining; stop/SIGTERM exits only after writers settle and loses in-memory history; rollback does not re-enable stdio until all writes are finished or explicitly reconciled and the HTTP service/lease state is verified.
- [ ] **Codex canary:** exact plugin ID is verified from UI/config; old plugin MCP is disabled only after its tasks finish and the canary passes; the HTTP config uses the documented env-var token reference; after reload, only the intended route is active. Skills/plugin remain installed and managed cache is untouched.
- [ ] **Operator evidence:** record config identity (never token values), service/source version, canary tool/schema results, readiness, active-operation/drain outcome, and the cutover/rollback decision. Do not label source review, test runs, canary, or static checks as one another.

## References

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [MCP Streamable HTTP transports, version 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [aiohttp 3.14.5 package metadata and license](https://pypi.org/project/aiohttp/3.14.5/)
