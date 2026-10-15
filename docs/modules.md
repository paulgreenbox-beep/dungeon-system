# Project modules registry

Обычный project module — верхнеуровневая область, содержащая `.sklt` и/или `.glif`. `docs/`, `snapshots/`, `libs/`, `tools/` и `tests/` не являются обычными modules и не входят в EXP.

## Текущее состояние

| Module | Purpose | Location | SKLT | RARE | Major object_ids | Dependencies |
|---|---|---|---:|---:|---|---|
| storage | EDITABLE / CAS / PACK storage policy, storage states, retention classes, archival, metrics and health | `storage/` | 15 | 0 | `storage.cas`, `storage.git_policy`, `storage.artifact_classes`, `storage.logical_pack`, `storage.pack_seal`, `storage.selective_extract`, `storage.portable_pack`, `storage.cas_gc`, `storage.storage_state`, `storage.storage_manifest`, `storage.archive`, `storage.restore`, `storage.storage_metrics`, `storage.storage_events`, `storage.storage_health` | внутренние object dependencies; внешних нет |
| scoping | GitScope history boundaries, level identity, isolation, cross-scope dependencies, snapshot revisions, migration | `scoping/` | 7 | 0 | `scoping.git_scope`, `scoping.level_identity`, `scoping.isolation`, `scoping.cross_scope_dependency`, `scoping.snapshot_revisions`, `scoping.scope_migration`, `scoping.scope_metrics` | внутренние object dependencies; внешних нет |

Уровни и порядок реализации описаны в [levels-and-modules-plan.md](levels-and-modules-plan.md). Правила хранения и состояния — в [storage-policy.md](storage-policy.md) и [storage-metrics.md](storage-metrics.md). Границы history и модулей — в [module-isolation-git-scope.md](module-isolation-git-scope.md).

Machine-derived значения обновляются Tool-командами `exp` и `index`. Эта Markdown-страница не заменяет `.sklt` и не является source of truth для EXP.
