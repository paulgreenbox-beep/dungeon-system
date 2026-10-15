# 0001 — Structural MVP

- **ID / date:** 0001 / 2026-08-19
- **Title:** Initial SKLT/GLIF structural MVP
- **What changed:** добавлены parser и validation для `.sklt`, parser `.glif`, linkage через `object_id`, состояния `SKLT` и `RARE`, модульные `.exp`, глобальный `docs/.grade` и CLI-команды `scan`, `validate`, `exp`, `inspect`.
- **Why:** нужен минимальный structural layer, который отделяет описание структуры от UI projection и автоматически показывает материализацию по модулям.
- **Affected modules / object_ids:** проектный registry; конкретные modules/object_ids на момент записи отсутствуют.
- **Structural result:** `.sklt` остаются source of truth; `.exp` и `docs/.grade` являются derived projections.
- **Known limitations:** текущий MVP не реализует runtime agents, Git integration, Reality extraction, package manager или полноценную semantic comparison.
- **Related snapshot:** отсутствует.
