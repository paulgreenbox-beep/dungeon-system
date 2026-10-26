# Shared HTTP MCP — bounded execution checkpoint, 2026-10-17

The user separately authorized building the prepared HTTP variant and checking
two clients, while explicitly retaining Codex settings and live MCP routes.
This supersedes the execution prohibition of the earlier preparation task; it
does not authorize a client cutover or real project/CAS write grants.

A dedicated ignored Python environment was provisioned. Forty-eight plugin
tests passed, including two real wire clients on distinct synthetic read-only
projects, 34-schema parity, isolation and authenticated drain/stop. Three real
spawned-worker tests used a controlled synthetic backend for cancellation,
UNKNOWN/deferred write completion and crash quarantine. The canary harness was
corrected for class/instance completion state and Windows timestamp precision,
then the entire suite was rerun.

The observed Windows service launch tree was five processes with two dispatcher
workers, not the design target of three. The test service exited normally and
its four observed descendants had exited. No scaling/RAM benefit is asserted.

The [validation report](../integrations/dungeon-shared-http-validation.md) records
the scope, source hashes, package versions and remaining gates. Live HTTP
writes/CAS acceptance, actual Codex clients and independent audit remain pending.
Default source/installed launchers and Codex configuration were not switched;
no SKLT/state/projection/snapshot/coverage authority was changed.
