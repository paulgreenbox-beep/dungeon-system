# 0009 — Source-only publication under MIT

```text
id: CRONICA-0009
date: 2026-10-15
epic: DUNGEON-GITHUB-PUBLICATION-001
affected_modules:
    - storage
affected_object_ids:
    - storage.cas
evidence_ids: []
```

Prepare the existing v0.4.1 sources as the `v0.4.1-rc1` public release candidate.
The author confirmed the MIT copyright notice. This changes distribution and
documentation, not structural identity, proof authority or the runtime architecture.

Publish authored code, SKLT and portable technical guides. Keep CAS, packs,
captures, installed caches/backups, private audit originals/packets and generated
indexes local. Preserve the original failed audit; publish an independently
written sanitized summary rather than editing or redistributing private evidence.

Remove machine-specific configuration and require an explicitly configured full
source checkout for the cached plugin backend. The empty write allowlist remains
read-only. The launcher checks the documented Python minimum. Two optional private
packet replays skip explicitly when those packets are absent; public synthetic
regressions remain real tests, not substitutes for historical runtime evidence.

Verify actual staged Git blobs for secrets, private paths, binaries and size, not
only ignore rules. Test the exact staged source export and its copied/installed
MCP/Skills package before public push. Keep raw local verification receipts out of
the release. Independent re-audit remains pending; the release is not independently
verified.

Publication testing exposed intermittent rollback/cleanup failures and a snapshot
rename WinError 5. A proposed runtime cleanup retry was withdrawn after review;
the runtime implementation is preserved. Test snapshots ignore only absent
enumeration entries and retain real residuals and inspection errors, with
deterministic regressions. Legacy fixtures use the existing strict cleanup
helper. Keep prior failures and unexpected temporary entries without attributing
them to a process without evidence. Final passing gates do not explain every
intermittent failure. The data model, proof authority and MCP surface do not
change. Explicit `env_vars` forwarding is
required for a cached plugin's backend root; a tool-list response alone does not
prove that its tools can actually read a project.
