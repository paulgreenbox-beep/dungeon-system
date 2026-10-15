# 0006 — Storage states, metrics and GitScope

```text
id: CRONICA-0006
date: 2026-09-25
affected_modules:
    - storage
    - scoping
affected_object_ids:
    - storage.storage_state
    - storage.storage_manifest
    - storage.archive
    - storage.restore
    - storage.storage_metrics
    - storage.storage_events
    - storage.storage_health
    - scoping.git_scope
    - scoping.level_identity
    - scoping.isolation
    - scoping.cross_scope_dependency
    - scoping.snapshot_revisions
    - scoping.scope_migration
    - scoping.scope_metrics
snapshot_before: storage-policy-baseline
snapshot_after: storage-states-and-gitscope
evidence_ids: []
```

Добавлены две политики и новый модуль `scoping/`.

Первая — `docs/storage-policy.md` дополнена тремя независимыми осями. `Structural State` (`SKLT`, `RARE`) отвечает «что реализовано», `Retention Class` (`PERMANENT`, `CACHE`, `EPHEMERAL`, `HOT`) — «насколько важно сохранять», `Storage State` (`LIVE`, `CAS`, `PACKED`, `ARCHIVED`, `OFFLINE`, `MISSING`, `CORRUPT`) — «где и в какой форме лежат bytes». Operational flags `PINNED`, `SEALED`, `GC_CANDIDATE`, `RESTORE_REQUIRED`, `REPLICATED` вынесены из Storage State. Зафиксированы storage lifecycle, archive policy, archive rule `copy → verify → register → remove`, GC rule с dry-run, различие archive и GC кандидатов и отображение состояний portable pack в общую ось. Вторая — `docs/storage-metrics.md`: derived storage registry, Collector, набор метрик, формулы `dedup_ratio` и `compression_ratio`, alert states, storage events и два gate.

Третья — `docs/module-isolation-git-scope.md`: `GitScope` с режимами `MONOREPO`, `REPOSITORY`, `EXTERNAL`, независимость `LevelId` и `ObjectId` от repository и pathname, isolation `SHARED`/`BOUNDED`/`ISOLATED`/`EXTERNAL` как архитектурная независимость, явные cross-scope зависимости, revisions и dirty state в snapshot, identity-preserving миграция, `logical change set` вместо ложной атомарности, 12 инвариантов `ISO-01`..`ISO-12`.

Инструмент: в `model.py` добавлены `STORAGE_STATES`, `RETENTION_CLASSES`, `STORAGE_FLAGS`, `GIT_SCOPE_MODES`, `ISOLATION_LEVELS` и свойства записи; в `validator.py` — `INVALID_STORAGE_STATE`, `INVALID_RETENTION_CLASS`, `INVALID_STORAGE_FLAG`, `INVALID_ISOLATION_LEVEL`; в `v03_manifest.py` — реестр `git_scopes` с `INVALID_GIT_SCOPE_MODE` и `DUPLICATE_GIT_SCOPE`, плюс legacy default `main` `MONOREPO`; в `v03_validation.py` — `UNKNOWN_GIT_SCOPE` и секции `git_scopes` и `storage_axes` в `status`; в `v03_index.py` — `storage_state`, `retention`, `flags`, `git_scope`, `isolation`. Добавлен уровень `L4` (observability).

Причина: `state` в SKLT отвечал только «что реализовано», а проекту не хватало независимых ответов на вопросы «где лежат bytes», «насколько важно их сохранять», «во сколько обходится хранение» и «в какой Git history живёт уровень». Смешение этих осей приводит к неверным решениям об очистке и к поломке identity при миграции.

Граница: Pack-команды, storage Collector, snapshot revisions, capture Git и migration tooling не реализованы. Политика описывает требуемые сроки, валидация проверяет только форму и принадлежность объявленным scope.
