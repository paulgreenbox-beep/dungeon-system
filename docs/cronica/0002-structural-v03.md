# 0002 — Structural reliability v0.3

```text
id: CRONICA-0002
date: 2026-08-21
affected_modules: []
affected_object_ids: []
snapshot_before: initial
snapshot_after: v03-baseline
```

v0.3 добавляет manifest, schema checks, object index, source refs, deterministic projection metadata, status/JSON output, severity-aware validation, configurable ignore rules и structural snapshot diff. Structural authority не изменилась: `.sklt` остаётся source of truth, а `.exp`, `docs/.grade` и object index пересоздаются.
