# 0004 — Git / CAS / Evidence MVP

```text
id: CRONICA-0004
date: 2026-09-23
affected_modules: []
affected_object_ids: []
snapshot_before: v03-baseline
snapshot_after: evidence-store-mvp
evidence_ids: []
```

Добавлен локальный внешний CAS по SHA-256, JSON Evidence Manifest, проверка ссылок и hash seal, CLI для capture/verify/retrieve, а также ссылки на Evidence IDs из structural snapshots и Cronica. `evidence_store.root` настраивается в `.project/project.json`; текущий путь относительный и не закрепляет конкретный диск.

Git commit и dirty state записываются, если project root находится в Git checkout. Текущий рабочий каталог Dungeon System не содержит `.git`, поэтому реальные executions в этом каталоге получают `null` для Git-полей. Репозиторий не инициализировался в рамках этой задачи.

Причина: сохранить exact execution bytes и их происхождение без хранения тяжёлых runtime outputs в Git или structural snapshots.

Граница MVP: container execution остаётся у Rust_Makepad_Toolkit. Remote CAS, artifact lifecycle classes, GC и SQLite search index не реализуются.
