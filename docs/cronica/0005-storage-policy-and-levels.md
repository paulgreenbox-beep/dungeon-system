# 0005 — Storage policy, levels and storage module

```text
id: CRONICA-0005
date: 2026-09-25
affected_modules:
    - storage
affected_object_ids:
    - storage.cas
    - storage.git_policy
    - storage.artifact_classes
    - storage.logical_pack
    - storage.pack_seal
    - storage.selective_extract
    - storage.portable_pack
    - storage.cas_gc
snapshot_before: evidence-store-mvp
snapshot_after: storage-policy-baseline
evidence_ids: []
```

Добавлена обязательная storage policy в `docs/storage-policy.md`: три формы хранения (EDITABLE / CAS / PACK), граница с Git, CAS invariants, Logical Pack и Portable `.dpack`, artifact classes `PERMANENT` / `CACHE` / `EPHEMERAL` / `HOT`, sealing, verification, HOT/COLD, reachability-based GC, selective extraction, validation codes и 12 инвариантов `PACK-01`..`PACK-12`.

Введён первый project module `storage/` с восемью SKLT-объектами: CAS, Git policy, artifact classes, logical pack, pack seal, selective extract, portable pack, CAS GC. Каждый объект получил необязательное поле `level` и распределён по уровням L0–L3 согласно `docs/levels-and-modules-plan.md`.

Инструмент: в `model.py` добавлены `VALID_LEVELS` и `OPTIONAL_FIELDS`, в `validator.py` — проверка `UNKNOWN_LEVEL`. Поле `level` необязательное, поэтому существующие записи без него остаются валидными; `state` по-прежнему ограничен значениями `SKLT` и `RARE`.

Причина: CAS был реализован, но не был описан как структурная сущность, а policy, Pack, sealing, selective extraction и GC существовали только как намерение. Теперь границы и порядок реализации зафиксированы в SKLT, а не в переписке.

Граница: реализация Pack-команд, pack manifest schema, validation codes и GC в этом изменении не выполняется. Документ политики создан без ссылок из `docs/project.md` и `docs/architecture.md` по решению для этой задачи; ссылки можно добавить отдельным решением.
