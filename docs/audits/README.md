# Public audit documentation

[v0.4.1 hardening summary](public-hardening-v041.md) preserves the technical
findings, fixes and verification boundaries of the local audit without
distributing private project files, machine paths, captures or evidence packets.

The original audit failed. Local remediation gates passed; **independent
re-audit is pending**. A release candidate is not independent acceptance.

Private originals remain with their owner and are deliberately not part of the
Git history or release assets. Two optional original-packet replay tests skip
explicitly when those private inputs are absent; synthetic regression tests
are public and do not stand in for real application/GPU execution.
