# Levels and modules plan

План отвечает на два вопроса: в каком порядке появляются storage-механизмы и где проходит граница между уровнями. Storage-механизмы описаны в [storage-policy.md](storage-policy.md); здесь только последовательность и критерии выхода.

## Как это выражено в структуре

| Что | Где | Кто валидирует |
|---|---|---|
| Допустимые уровни | `VALID_LEVELS` в `tools/project_structure/model.py` | `UNKNOWN_LEVEL` в `validator.py` |
| Принадлежность объекта уровню | поле `level` в `.sklt` | тот же validator |
| Storage State и Retention Class | `STORAGE_STATES`, `RETENTION_CLASSES`, `STORAGE_FLAGS` в `model.py` | `INVALID_STORAGE_STATE`, `INVALID_RETENTION_CLASS`, `INVALID_STORAGE_FLAG` |
| Isolation | `ISOLATION_LEVELS` в `model.py` | `INVALID_ISOLATION_LEVEL` |
| GitScope registry | `git_scopes` в `.project/project.json` | `INVALID_GIT_SCOPE_MODE`, `DUPLICATE_GIT_SCOPE` |
| Ссылка объекта на scope | поле `git_scope` в `.sklt` | `UNKNOWN_GIT_SCOPE` в `v03_validation.py` |
| Человеческое описание уровня | этот документ | не валидируется |
| Состав модуля | директория верхнего уровня | scanner |

Поле `level` необязательное: существующие записи без него остаются валидными. Уровень — это порядок реализации, а не зрелость и не состояние материализации; `state` по-прежнему принимает только `SKLT` и `RARE`.

Три оси состояний независимы: `state` отвечает «что реализовано», `storage_state` — «где лежат bytes», `retention` — «насколько важно сохранить». Подробности в [storage-policy.md](storage-policy.md).

## Уровни

### L0 — Storage and identity foundation

Что: адресация байтов, граница с Git и независимость identity от repository.

- `storage.cas` — immutable content-addressed objects, `sha256:<hash>`, deduplication, verify-by-hash
- `storage.git_policy` — что остаётся reviewable в Git, а что покидает Git tree
- `scoping.git_scope` — реестр Git history boundaries с режимами `MONOREPO`, `REPOSITORY`, `EXTERNAL`
- `scoping.level_identity` — `LevelId` и `ObjectId` не зависят от pathname и repository

Почему первым: без CAS нечего паковать, без Git-политики новая подсистема раздувает дерево, а без разделения identity и history любая миграция ломает object ids. CAS уже реализован в `tools/project_structure/v03_cas.py`; объект фиксирует его как структурную сущность. `git_scopes` объявлен в манифесте, legacy-проект без него трактуется как `main` `MONOREPO`.

Критерии выхода:

- CAS object не перезаписывается, конфликт байтов даёт новый hash
- ссылка на несуществующий object обнаруживается, а не считается валидной
- список категорий «уходит в CAS» зафиксирован и не расширяется по умолчанию
- `git_scope` объекта проверяется против реестра, неизвестный scope даёт `UNKNOWN_GIT_SCOPE`
- перенос уровня не требует менять `LevelId` и `ObjectId`

### L1 — Storage model

Что: язык группировки и жизненный цикл артефактов.

- `storage.artifact_classes` — `PERMANENT`, `CACHE`, `EPHEMERAL`, `HOT`
- `storage.logical_pack` — маленький manifest со ссылками на CAS, без копирования байтов
- `storage.storage_state` — третья независимая ось: `LIVE`, `CAS`, `PACKED`, `ARCHIVED`, `OFFLINE`, `MISSING`, `CORRUPT`
- `storage.storage_manifest` — derived registry с retention, storage state, locations, `ref_count`
- `scoping.isolation` — `SHARED`, `BOUNDED`, `ISOLATED`, `EXTERNAL`

Почему вторым: Pack без классификации артефактов превращает CAS в свалку, а классификация без Pack ничего не группирует. Storage State без retention class неоднозначен: нельзя решить, что можно чистить. Isolation без объявленного GitScope не имеет смысла.

Критерии выхода:

- два pack с общим содержимым физически хранят объект один раз
- артефакт класса `EPHEMERAL` не попадает в CAS
- manifest pack не содержит байтов, только ссылки
- `storage_state`, `retention` и `flags` валидируются раздельно и не выводятся друг из друга
- `storage_manifest` удаляется и полностью пересобирается из CAS и pack manifests
- `isolation` не выводится из Git mode автоматически

### L2 — Pack and cross-scope operations

Что: операции над упакованным набором и явные межобластные зависимости.

- `storage.pack_seal` — `DRAFT` → `VERIFIED` → `SEALED`, после `SEALED` только append-only
- `storage.selective_extract` — inspect, list, extract по селектору без полной распаковки
- `storage.portable_pack` — `.dpack` для переноса, архива и offline handoff
- `scoping.cross_scope_dependency` — cross-scope зависимость объявлена и атрибутирована по scope
- `scoping.snapshot_revisions` — snapshot и evidence фиксируют revisions всех relevant scopes и явный dirty state

Почему третьим: sealing и selective extraction нужны раньше переноса. Pack, который нельзя проверить частично, непригоден для работы; pack, который нельзя проверить целиком, нельзя архивировать. Cross-scope зависимость требует существующего реестра scopes, а snapshot с revisions — и scopes, и явных зависимостей.

Критерии выхода:

- sealed pack не меняется; изменение содержимого даёт новый `pack_id`
- один объект извлекается без распаковки остальных
- export и import round-trip сохраняют ссылки и hashes, а существующие blobs не дублируются
- зависимость через границу scopes видна как edge с source и target scope
- snapshot при `DIRTY` repository помечает dirty явно и не заявляет clean revision

### L3 — Stewardship and migration

Что: жизненный цикл физического хранения и перемещение уровней между scopes.

- `storage.archive` — `copy → verify → register → remove old location`
- `storage.restore` — возврат из архива в hot storage и materialization в `LIVE`
- `storage.cas_gc` — сборка мусора по достижимости от sealed и pinned roots
- `scoping.scope_migration` — перенос уровня между scopes с сохранением identity

Почему четвёртым: archive и GC опасны, пока нет sealing, классификации и registry. Миграция identity возможна только когда реестр scopes и revisions уже существуют.

Критерии выхода:

- удаляются только unreachable `CACHE`
- reachable `PERMANENT` и sealed packs не удаляются ни при каком раскладе
- cold store может быть недоступен, и structural validation от этого не падает
- горячая копия удаляется только после verified archive и обновления registry
- миграция сохраняет `LevelId`, `ObjectId` и SKLT id, а историю Git не переписывает

### L4 — Observability

Что: измерение и сигналы, без изменения структуры.

- `storage.storage_metrics` — logical и physical bytes, dedup, compression, tier breakdown, integrity
- `storage.storage_events` — append-only события переходов storage state
- `storage.storage_health` — `HEALTHY`, `PRESSURE`, `DEGRADED`, `INCOMPLETE`, `CORRUPT` и списки кандидатов
- `scoping.scope_metrics` — advisory change coupling и dependency signals

Почему последним: метрики без модели состояний считают мусор, а health без событий не объясняет переход. Collector только измеряет и советует: он не архивирует и не удаляет.

Критерии выхода:

- удаление stats не приводит к потере данных, только к пересчёту
- `EPHEMERAL` в CAS даёт `STORAGE_POLICY_VIOLATION`
- отсутствие access tracking даёт «неизвестно», а не ноль
- archive candidates и GC candidates вычисляются детерминированно и не выполняются автоматически
- coupling signals не инициируют миграцию сами

## Модули

| Module | Objects | Levels | Зависимости |
|---|---:|---|---|
| `storage` | 15 | L0, L1, L2, L3, L4 | внутри модуля: `storage.cas` → `storage.artifact_classes` / `storage.git_policy` → `storage.logical_pack` → `storage.pack_seal` / `storage.selective_extract` → `storage.portable_pack`; `storage.cas_gc` — от `storage.artifact_classes` и `storage.pack_seal`; `storage.storage_state` и `storage.storage_manifest` → `storage.archive` / `storage.restore` → `storage.storage_metrics` / `storage.storage_events` / `storage.storage_health` |
| `scoping` | 7 | L0, L1, L2, L3, L4 | `scoping.git_scope` → `scoping.level_identity` / `scoping.isolation` → `scoping.cross_scope_dependency` / `scoping.snapshot_revisions` → `scoping.scope_migration` / `scoping.scope_metrics` |

Порядок сборки по объектам:

```text
storage.cas
  ├─ storage.git_policy
  └─ storage.artifact_classes
        ├─ storage.logical_pack
        │     ├─ storage.pack_seal
        │     │     ├─ storage.portable_pack
        │     │     └─ storage.cas_gc
        │     └─ storage.selective_extract
        └─ storage.storage_state
              └─ storage.storage_manifest
                    ├─ storage.archive
                    │     └─ storage.restore
                    └─ storage.storage_events

storage.pack_seal + storage.storage_manifest
  └─ storage.storage_metrics
        └─ storage.storage_health

scoping.git_scope
  ├─ scoping.level_identity
  └─ scoping.isolation
        └─ scoping.cross_scope_dependency
              └─ scoping.snapshot_revisions
                    └─ scoping.scope_migration
                    └─ scoping.scope_metrics
```

## Границы

Вне плана:

- Remote CAS и синхронизация между машинами
- SQLite или иной search index по CAS
- Хранение контейнерных образов
- Перенос structural model в portable pack: pack группирует immutable artifacts, а не SKLT-источник истины
- Автоматическая архивация и HOT/COLD миграция: только рекомендация кандидатов
- Переписывание Git history: принадлежит Git tooling
- Автоматическое решение об isolation по метрикам связи

## Что должно измениться в инструменте

Уже реализовано и проверяется: `level`, `storage_state`, `retention`, `flags`, `git_scope`, `isolation`, реестр `git_scopes` в манифесте, вывод этих осей в object index и в `status`.

Pack-команды, pack-валидация, Collector, snapshot revisions и capture Git остаются незавершёнными. Когда они появятся, каждому объекту L2–L4 потребуется:

- CLI-команда в `build_parser()` и ветка в `main()`
- pack manifest schema рядом с `docs/evidence-manifest-v1.schema.json`
- storage manifest schema для `.project/storage/state.json`
- validation codes из `docs/storage-policy.md` и `docs/module-isolation-git-scope.md` в `severity_for()`
- тесты deduplication, sealing, selective extraction, archive round-trip, GC dry-run и cross-scope validation
