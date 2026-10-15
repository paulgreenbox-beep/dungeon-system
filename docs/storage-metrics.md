# Storage metrics and archival states

Метрики отвечают на вопросы: где физически лежит artifact, сколько он занимает, насколько работает deduplication, что можно очистить и какие packs требуют проверки. Документ дополняет [storage-policy.md](storage-policy.md): там оси и правила, здесь измерение.

Storage lifecycle — отдельная система состояния и не смешивается со structural state.

## 1. Storage manifest

Derived registry, индекс, а не authority:

```text
.project/storage/state.json
```

```json
{
  "format": "dungeon.storage.v1",
  "objects": {
    "sha256:abc": {
      "logical_size": 1200344,
      "stored_size": 381221,
      "retention": "PERMANENT",
      "storage_state": "ARCHIVED",
      "locations": [
        {
          "tier": "COLD",
          "store": "archive-01"
        }
      ],
      "ref_count": 4,
      "pack_refs": [
        "PACK-0042"
      ],
      "verified": true
    }
  }
}
```

Файл полностью rebuildable: если `state.json` удалён, Collector восстанавливает его из CAS scan и pack manifests. Дополнительно допускаются маленькие snapshots для исторического анализа:

```text
.project/storage/snapshots/
```

Они содержат только числа и не содержат physical files.

## 2. Collector

Сбор статистики идёт поверх CAS, packs, manifests и location descriptors:

```text
CAS + Packs + Manifests + Locations
                ↓
        Storage Metrics
                ↓
 logical / physical
 dedup / compression
 hot / cold
 archive
 integrity
 GC candidates
                ↓
        Project status
```

Collector может быть отдельным внутренним модулем или namespace `project_metrics.storage.*` внутри `ProjectMetricsMCP`. Отдельный authority не нужен.

## 3. Метрики

```text
logical_bytes
physical_bytes
cas_objects
packs
portable_packs
archived_objects
archived_bytes
hot_bytes
cold_bytes
cache_bytes
permanent_bytes
missing_objects
corrupt_objects
offline_objects
gc_candidates
```

## 4. Формулы

```text
logical_bytes
    сколько бы занимали все logical references без deduplication

physical_bytes
    реально занято после deduplication, compression и Pack storage

dedup_ratio =
    logical_bytes / unique_bytes

dedup_saved_bytes =
    logical_bytes - unique_bytes

compression_ratio =
    uncompressed_bytes / stored_bytes
    только для Packed и Archived objects

saved_bytes =
    logical_bytes - physical_bytes

saving_percent =
    saved_bytes / logical_bytes
```

Compression saving и dedup saving не смешиваются. Отсутствие access tracking означает, что метрика не выдумывается: `restore_count` и `last_restore_at` остаются неизвестными, а не нулевыми.

## 5. Статистика по ссылкам и пакам

```text
ref_count per blob
objects_ref_1
objects_ref_2_10
objects_ref_10_plus
max_ref_count
average_ref_count

pack_count
sealed_pack_count
draft_pack_count
archived_pack_count
objects_per_pack_avg
objects_per_pack_max
logical_bytes_per_pack
physical_bytes_per_pack
```

Высокий `ref_count` показывает наиболее переиспользуемые artifacts.

## 6. Статистика по tier и retention

По tier `HOT`, `COLD`, `OFFLINE` показываются `object_count`, `bytes`, `percentage`.

По retention:

```text
PERMANENT
CACHE
EPHEMERAL
```

`EPHEMERAL` в CAS должен быть равен нулю. Если Collector находит CAS object с `retention = EPHEMERAL`, это `STORAGE_POLICY_VIOLATION`.

## 7. Integrity statistics

```text
verified_objects
unverified_objects
missing_objects
corrupt_objects
broken_pack_refs
```

`CORRUPT` и `MISSING` никогда не считаются здоровым состоянием и не чинятся автоматически.

## 8. Evidence statistics

Если CAS связан с Evidence:

```text
evidence_count
sealed_evidence
unsealed_evidence
evidence_bytes
evidence_archived_bytes
evidence_per_object
```

Группировка по Toolkit capability и container допускается, если данные доступны.

## 9. Статистика по объектам

Для объекта проекта показывается projection, а не exact CAS ownership:

```text
renderer.settings

evidence:
    14 manifests

CAS:
    logical: 3.2 GB
    physical contribution: 1.1 GB

packs:
    3

latest evidence:
    EV-0042
```

## 10. API

```text
project_metrics.storage.status
project_metrics.storage.stats
project_metrics.storage.inspect_object
project_metrics.storage.inspect_pack
project_metrics.storage.archive_candidates
project_metrics.storage.gc_candidates
project_metrics.storage.verify
```

`storage.status` возвращает сводку:

```json
{
  "status": "HEALTHY",
  "cas": {
    "objects": 4821,
    "logical_bytes": 128400000000,
    "unique_bytes": 36400000000,
    "physical_bytes": 21800000000,
    "dedup_ratio": 3.52,
    "compression_ratio": 1.67
  },
  "storage": {
    "hot_bytes": 4800000000,
    "cold_bytes": 17000000000,
    "offline_bytes": 0
  },
  "integrity": {
    "missing": 0,
    "corrupt": 0,
    "unverified": 2
  },
  "gc": {
    "candidates": 84,
    "reclaimable_bytes": 2900000000
  }
}
```

Человекочитаемый вывод `project-structure status` показывает компактно: objects, logical, physical, saved, dedup, HOT, COLD, archived, missing, corrupt, GC candidates.

## 11. Alert states

```text
HEALTHY
    integrity checks пройдены

PRESSURE
    storage budget близок к лимиту

DEGRADED
    часть archive storage offline

INCOMPLETE
    часть metadata отсутствует

CORRUPT
    обнаружен hash mismatch
```

## 12. Events

```text
CAS_OBJECT_ADDED
CAS_OBJECT_VERIFIED

PACK_CREATED
PACK_VERIFIED
PACK_SEALED

OBJECT_ARCHIVED
OBJECT_RESTORED

PACK_ARCHIVED
PACK_RESTORED

STORAGE_OFFLINE
STORAGE_ONLINE

CAS_OBJECT_MISSING
CAS_OBJECT_CORRUPT

GC_CANDIDATE_DETECTED
CAS_OBJECT_GC_DELETED

STORAGE_STATS_RECALCULATED
```

Минимальные поля события:

```text
event
hash or pack_id
old_state
new_state
bytes
storage_location
reason
actor
timestamp
git_ref if relevant
```

Текущий append-only `events.jsonl` не перепаковывается целиком при каждом event. Когда файл становится большим, разрешена segment rotation:

```text
events/
├─ current.jsonl
└─ archive/
   ├─ events-0001.pack.json
   ├─ events-0002.pack.json
   └─ ...
```

Текущий segment остаётся `LIVE`, старые immutable segments уходят в CAS, затем в Logical Pack и `ARCHIVED`.

## 13. Storage statistics не являются authority

`dedup_ratio`, `physical_bytes`, `cold_bytes` всегда derived. Удаление stats-файла не должно приводить к потере данных — только к пересчёту.

## 14. Budgets и trends

Позже добавляются thresholds:

```text
HOT_STORAGE_BUDGET
CACHE_STORAGE_BUDGET
MAX_UNVERIFIED_OBJECTS
MIN_FREE_SPACE
```

Collector может вернуть `HOT_STORAGE_PRESSURE`, но не удаляет permanent artifacts. Trends по CAS growth, logical growth, physical growth, dedup efficiency, archive growth, cache growth и reclaimable growth показывают, реально ли архитектура экономит место.

## 15. Первый gate

Minimum Storage Metrics MVP:

1. Storage State enum
2. Retention Class отдельно от Storage State
3. CAS object statistics
4. Pack statistics
5. Logical vs physical bytes
6. Dedup saving
7. Compression saving
8. HOT/COLD/ARCHIVED breakdown
9. Missing/corrupt counters
10. GC candidate calculation
11. Archive candidate calculation
12. `storage.status`
13. JSON output
14. Storage events
15. Rebuildable statistics

## 16. Второй gate

Позже и не в первом проходе:

```text
automatic archive policy
HOT/COLD migration
LRU for CACHE
offline store support
replica policy
storage budgets
trend reports
SQLite acceleration
```

## 17. Agent rules

Агент не решает вручную, что удалять и что цело. Для этого есть Collector. Агент может request archive, request restore, request verify, inspect statistics и request GC dry-run.

Collector рекомендует archive candidates по детерминированным критериям, например `SEALED AND age > threshold AND not HOT AND not pinned AND verified`, но не архивирует сам только потому, что artifact старый.

## 18. Итоговая формула

```text
Structural State
    = что реализовано

Retention Class
    = насколько важно сохранить

Storage State
    = где и в какой форме лежат bytes

Storage Metrics
    = фактическая стоимость хранения
```

> **Never confuse project progress with storage lifecycle.**
>
> **Store once. Pack logically. Archive safely. Measure everything.**
