# Explicit Scion runtime gate

These operator-only helpers are never imported or invoked by ordinary Dungeon
navigation/MCP. They use disposable fixtures, the pinned external Scion commit,
an isolated Linux toolchain and real external CLI processes. No production VCS
mutation or Windows/WSL transport is enabled.

1. On x86_64 Linux, run `bootstrap.py --upstream <CLEAN_PINNED_CHECKOUT>
   --receipt <NEW_BOOTSTRAP_JSON>`. For a Windows-created CRLF checkout, add
   `--source-crlf`. Only official Rust/protoc downloads with verified checksums
   are installed under a private `/var/tmp/dungeon-scion-runtime-002-*` root.
2. Run `build.py --bootstrap <JSON> --source-checkout <CLEAN_PINNED_CHECKOUT>
   --output <NEW_BUILD_DIRECTORY>`, with the same explicit CRLF flag if needed.
   It checks all tracked source bytes, builds only the CLI and runs the narrow
   real CLI/TestCluster upstream smoke. A failed source/build gate is not PASS.
3. Run `prepare_cluster.py --bootstrap <JSON> --output <NEW_HELPER_BUILD_DIR>`.
   The standalone helper is compiled offline against the private upstream copy.
   It does not patch upstream or launch a server automatically.
4. Create an owned 0700 private run directory, then launch the helper binary
   with `--fixture-root <PRIVATE_RUN>/project --context <PRIVATE_RUN>/context.json
   --stop-file <SHARED_RUN>/stop --binary <BUILT_SCION>`. It explicitly creates
   three real changesets and performs a real CLI checkout. Wait for READY and
   keep the process alive. Its lifetime is bounded to 15 minutes. Do not publish
   the private context: it contains a disposable test token.
5. For the existing Windows-only writer, use `fixture_transfer.py export` to
   make an explicit byte-checked mirror in `artifacts/scion-runtime-002/<RUN>/project`.
   `windows_capture.py --fixture-root <WINDOWS_MIRROR> --scion-revision <ACTUAL_C1>
   --output <NEW_CAPTURE_JSON>` creates only the disposable managed fixture,
   nested Git commit, snapshot and CAS/source Evidence. `CAPTURED_UNSEALED` exit 1
   means the expected UNKNOWN-provenance seal refusal; it is not execution proof.
6. `fixture_transfer.py import-metadata` copies only the generated metadata and
   real nested Git repo back to the original ext4 fixture, preserving its actual
   `.scion/state`. This explicit setup copy is not a production WSL bridge.
7. Run `linux_gate.py --context <PRIVATE_CONTEXT> --binary-sha256 <FINAL_CLI_SHA256>
   --output <NEW_RECEIPT_DIRECTORY>`. It checks actual show/log/cat, backend/service,
   mixed VCS queries, real negative cases and a bounded transport timeout. Fault
   injections are separately labeled and never count as real server responses.
8. Create the owned stop sentinel even when a verification step fails; wait for
   the helper to exit and check its loopback listener is closed. Preserve failed
   attempts and raw receipts. Do not stop unrelated WSL processes or distributions.

All output names must be new. No recursive cleanup of unknown paths is performed.
CAS/seal integrity, source revision availability and execution-proof acceptance
remain separate. The Scion proof adapter stays unsupported. See
[the runtime report](../../docs/integrations/scion-runtime-002-report.md) for the
recorded result and [Scion integration](../../docs/integrations/scion.md) for
platform limitations. Package activation occurs only after runtime/regression
gates, with a verified installed-cache backup and actual fresh-session discovery.
