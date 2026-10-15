# Cronica

Cronica хранит смысл существенных структурных изменений проекта.

- Git хранит физическую историю файлов.
- Старые записи Cronica не переписываются задним числом.
- Новая запись может уточнить или supersede старое решение.
- Cronica не является database/event-sourcing системой.

## Минимальный формат записи

- ID / date
- title
- what changed
- why
- affected modules / object_ids
- structural result
- known limitations
- related snapshot, если есть
- `evidence_ids: [EV-...]`, если есть связанное execution evidence; `validate` проверит ссылки на manifests
