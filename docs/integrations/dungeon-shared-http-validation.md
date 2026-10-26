# Shared HTTP MCP — bounded Windows validation, 2026-10-17

Status: **BOUNDED TWO-CLIENT CANARY PASSED; NO LIVE CUTOVER.** Not an
independent audit, release acceptance or `INDEPENDENTLY VERIFIED` claim.

## Environment and scope

Python 3.12.14, Windows, dedicated ignored environment under
`artifacts/shared-http-validation-2026-10-17/.venv`. Optional coordinator
dependency `aiohttp==3.14.5` was installed there only. `pip check` passed.
The source server identifies as `0.4.2-rc1`; MCP protocol `2025-11-25`.

The current fullclone provided backend code. Two disposable SKLT_ONLY projects
were the HTTP targets, each with a distinct client credential, exact root and
session. Both had empty writable/input-root grants. The real DungeonStore and
other user repositories were not modified. Tokens were generated in memory,
passed in the owned service's environment and checked absent from its config
and sanitized report. No persistent credential or Codex configuration was added.

## Executed checks

The final plugin-wide unittest run passed **48 tests in 16.927 seconds**:

| Check set | Tests | Result |
| --- | ---: | --- |
| HTTP dispatcher/config/frontend units | 20 | PASS |
| Real two-client HTTP canary | 1 scenario / 21 checks | PASS |
| Real spawned-worker lifecycle with controlled backend | 3 | PASS |
| Existing multi-project routing regression | 5 | PASS |
| Existing STDIO transport and integration regression | 19 | PASS |

The real wire scenario verified initialization of distinct sessions; 34 tool
schemas byte-for-structure equal to the STDIO registry; concurrent probes on
the two exact roots; rejection of unknown bearer, foreign session/root,
missing root and ungranted writes; owner-scoped operation ID/request ID lookup;
duplicate RPC-ID rejection; GET `/mcp` returning 405; client/admin separation;
second service refusal under the active source-home lease; owned session DELETE;
and authenticated health plus drain/stop.

The controlled real-process tests verified Windows Job Object cleanup of a
read worker's child and safe worker replacement; deferred write cancellation,
UNKNOWN deadline response and retained barrier until actual fixture completion;
and readiness quarantine/no replay following an unexpected synthetic mutator
exit. They do not execute real Dungeon/CAS mutation phases.

Forced operation/session ID collisions, retry budgets and exhaustion without
record replacement were additionally tested with deterministic units. Config
tests cover every numeric bound, booleans, duplicate/unknown fields, wrong
source home, unsafe/relative roots, non-subset write grants and credential rules.

## Observed lifecycle, not a three-process promise

At authenticated readiness, the dispatcher reported two live workers. OS
observation recorded the service launch PID plus **four live descendants**:
five processes in the owned Windows launch tree, not the proposed three.
This includes launch indirection; the harness clients ran in their own test
process and are not included. Held process handles and creation times were used
to avoid confusing a reused PID with an owned process.

Admin drain and stop returned accepted; the service exited with code 0. All
four observed descendants had exited after stop. The test service is not left
running. No RAM saving, many-chat scaling, or POSIX topology result is claimed.

Early canary attempts exposed defects in the test harness, not a proven service
failure: completion status was written to an instance rather than the shared
class, and CIM microsecond creation times were compared as if they had the
100-nanosecond precision of a Windows process handle. Both were corrected, a
nonempty descendant-observation assertion was added, and the full suite reran.
Initial sandbox attempts also blocked asyncio's socketpair before application
execution; approved local-socket execution passed.

## Source identity and local receipt

These are working-tree hashes, not a new commit/release:

```text
mcp/server.py       1d0574bad513bd53fb00be74c4fd36b3fad9f444a12bc2c4ec089744ad5bb091
mcp/http_service.py 599fb99fc0a1ef28827ca0a1102c4aafba33b7131531597e0c15b75fe76d5a4d
mcp/http_runtime.py e78a5669ca1b4c09d7709e08a6dd708535baf8676d6838e3cd0da11b504c24d6
tool schema JSON   a02712dd38852163a505bbe72dc03f1d1b577edc951db1d9bada3851d545596f
```

The sanitized local receipt is
`artifacts/shared-http-validation-2026-10-17/two-client-report.json` (ignored,
not a release attachment or sealed Evidence/CAS packet). It records the two
clients, all 21 checks, source hashes and final lifecycle observations.

Installed runtime packages were aiohttp 3.14.5, aiohappyeyeballs 2.7.1,
aiosignal 1.4.0, attrs 26.1.0, frozenlist 1.8.0, idna 3.20, multidict 7.1.0,
propcache 0.5.4, typing_extensions 4.16.0 and yarl 1.25.1. Their distribution
metadata declares Apache-2.0/MIT, PSF-2.0, MIT or BSD-3-Clause licenses as
applicable; no dependency was vendored or relicensed. This is not a dependency
vulnerability audit or a transitive hash lock.

## Explicitly still pending

- Two actual Codex clients, plugin override and live-route switch: **NOT RUN**.
- Real HTTP writes, shared-CAS contention, mutation-phase faults and mixed
  legacy-writer cutover: **NOT RUN**; live-write acceptance stays closed.
- HTTP-wire active cancellation/204 client compatibility, timeout/UNKNOWN
  recovery, all saturation/TTL cases and arbitrary crash recovery: not fully
  accepted. Dispatcher units/controlled OS tests are not substitutes.
- Dependency advisory review, independent hardening audit, POSIX runtime and
  comparative process/RAM/load measurements remain pending.

Source `.mcp.json` and `launch_mcp.cmd` were unchanged. SHA-256 checks of the
Codex config, installed plugin MCP config and installed launcher were unchanged
across this work. No installed cache, live MCP connection or default launcher
was switched. SKLT, grades, coverage, snapshots and derived indexes were not
upgraded by these tests. Follow the remaining [migration gates](dungeon-shared-http.md)
before any separate live-cutover request.
