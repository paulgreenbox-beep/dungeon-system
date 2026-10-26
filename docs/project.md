# Dungeon System — project orientation

Dungeon System хранит структурное описание проекта и связанные UI-проекции. Structural authority остаётся в `.sklt`; operational metadata, object index и stale detection не превращают систему в workflow engine.

## Рекомендуемый порядок работы агента

```text
project probe (explicit root)
    ↓
NOT_DUNGEON / SKLT_ONLY / MANAGED
    ↓
adoption plan → explicit accepted apply, если нужны managed operations
    ↓
project summary → module → object_id
    ↓
inspect → SKLT → Task Trace
    ↓
declared GLIF, source/test refs → source-link audit
    ↓
declared VersionScope → backend-supported revision/history queries
    ↓
Rust_Makepad_Toolkit/docs/INDEX.md, если нужна external capability
    ↓
Toolkit verification → Evidence / CAS
    ↓
Prototype / Product coverage
    ↓
validate
    ↓
exp / index if stale
    ↓
snapshot-diff / Cronica for a meaningful checkpoint
```

Для задачи, где нужен Toolkit container:

```text
status → inspect → SKLT → execution ref
      → Rust_Makepad_Toolkit/docs/INDEX.md
      → ContainerWorker guide
      → documented Toolkit command
      → collect report/evidence
      → validate structural result
```

Агент не придумывает команду запуска контейнера и не переносит его runtime в Dungeon System. Runtime failure контейнера не маскируется structural правками.

## Где что находится

VersionScope описывает backend истории исходников; Git остаётся default. Для Scion используй только подтверждённые read-only show/log queries и сохраняй неизвестное состояние как UNKNOWN. Подробные ограничения, формат revisions, snapshots и evidence собраны в [VCS backends](vcs-backends.md) и [Scion integration](integrations/scion.md). Runtime-002 gates A-C прошли на disposable Linux/TestCluster fixture; gate D подтверждён installed launcher и новой Codex CLI-сессией с 34 tools. Windows capture остаётся `CAPTURED_UNSEALED` из-за `SOURCE_REVISION_UNKNOWN`, а не подтверждением текущего proof: см. [runtime report](integrations/scion-runtime-002-report.md).

- MCP v0.4 и onboarding: [plugin guide](../tools/codex-plugin/dungeon-system/README.md).
- `project-probe` диагностирует также `INVALID_MANAGED` и `UNAVAILABLE`; выдаёт structured issues и next steps.
- В `SKLT_ONLY` каталог строится в памяти из explicit IDs и declared symbols, без записи managed index. `object_id` — logical identity, не путь: сохраняй parser-returned ID byte-for-byte, включая квалифицированный ID вложенной декларации; pathname identity не создаётся. Повторы identity являются конфликтами.
- Task Trace — небольшая связь задачи, объектов, source/test/evidence; назначенный агент остаётся внешней ссылкой. Metadata не управляет выполнением задачи.
- Prototype/Product coverage — независимые оценки с `UNKNOWN` по умолчанию. Они не меняют SKLT/RARE или derived grade. Seal/CAS integrity не равна текущей применимости: proof проверяется отдельно на текущих SKLT, Task Trace, source и build.

- .project/project.json → evidence_store — путь к внешнему local CAS.
- evidence/manifests/ и evidence/seals/ — небольшие append-only execution records в Git.
- snapshots/ может ссылаться на Evidence Manifest IDs; runtime payloads лежат в CAS.

- `.project/project.json` — manifest scanner-а.
- `.project/objects.json` — derived index объектов.
- `.project/state.json` — deterministic hashes и состояния projections.
- `docs/map.md` — крупная navigation map.
- `docs/modules.md` — registry project modules.
- `docs/architecture.md` — structural и external-boundary rules.
- `docs/cronica/` — почему структура менялась.
- `snapshots/` — frozen structural states, не container images и не runtime logs.
- `libs/` — external/reusable libraries, не обычные project modules.
- `<module>/*.sklt` — structural source of truth.
- `<module>/*.glif` — UI projections через `object_id`.

## External execution tooling

```text
Provider: Rust_Makepad_Toolkit
Canonical documentation:
<TOOLKIT_ROOT>/docs/INDEX.md
Current catalog identity:
docker-linux-level0 / ContainerEnvironment Level 0
```

Dungeon System отвечает на вопросы **что** и **где**: object, module, SKLT, GLIF, source и capability reference. Toolkit отвечает на вопросы **чем** и **как** выполнить UI/tool/container operation.

## Evidence Store

evidence/manifests/ связывает object_id, Git state или typed source revision, snapshot, Toolkit execution ref, CAS inputs/artifacts и result. Тяжёлые точные байты лежат во внешнем CAS; в Git остаются manifests и seals. Store root задаётся в .project/project.json, сейчас относительным путём ../DungeonStore. Недоступность CAS не меняет результат structural validation.

Начальный workflow и формат: [docs/evidence.md](evidence.md). CAS immutable по SHA-256. Evidence seal фиксирует hash manifest; после seal новая попытка получает новый ID. Snapshot хранит только структурное состояние и ссылки на evidence IDs.

Cronica-запись может содержать `evidence_ids: [EV-...]`; `validate` проверяет, что каждый такой manifest существует. Для checkpoint с evidence используй `snapshot <name> --evidence-id EV-...`.

Optional SKLT execution reference:

```text
execution:
    provider: rust_makepad_toolkit
    kind: container
    id: docker-linux-level0
    purpose: ai-ui-test
```

В reference хранится только stable provider/kind/id/purpose. Image digest, mounts, limits, permissions, lifecycle и command details принадлежат Toolkit catalog/guides.

## Structural commands (backward compatible)

```text
python tools/project-structure.py status [--json]
python tools/project-structure.py scan [--json]
python tools/project-structure.py validate [--json]
python tools/project-structure.py exp [--json]
python tools/project-structure.py index [--json]
python tools/project-structure.py inspect <object_id> [--json]
python tools/project-structure.py snapshot <name> [--json]
python tools/project-structure.py snapshot-diff <old> <new> [--json]
```

Evidence/CAS commands описаны в [docs/evidence.md](evidence.md): `cas-put`, `cas-get`, `cas-verify`, `cas-status`, `evidence-create`, `evidence-inspect`, `evidence-verify`, `evidence-seal`, `evidence-for`.

`validate` возвращает FAIL только при `ERROR`; unresolved external refs являются `WARNING`, а malformed refs и неизвестный container key из доступного каталога — `ERROR`. JSON содержит стабильные `status`, `errors`, `warnings`, `issues`, а object index сериализует execution refs typed-полями `type`, `provider`, `id`.

## Existing repository onboarding

`project-probe → adoption-plan` не меняют файлов. Reviewed plan сохраняет наблюдённые IDs и fingerprint исходных данных. `adoption-apply` принимает явный набор modules, блокирует конфликты и изменившиеся inputs, генерирует projections и выполняет validation. Повторное применение должно возвращать тот же результат без дублирования. Git создаётся только отдельным действием пользователя.

Для AI Revit сначала выбери module/object в `SKLT_ONLY`, прочитай SKLT и объявленные ссылки. Если source RTX/CUDA не объявлен, добавление ссылки и Task Trace требует подтверждённого сопоставления с конкретным source, а не вывода из имени файла. Подтверждённая частичная реализация может иметь Prototype `PARTIAL`, при недостатке данных Product остаётся `UNKNOWN`. Это пример workflow, не утверждение о проверенном состоянии AI Revit.

`can_plan_adoption` не означает `can_adopt`: v0.4 application поддерживает канонический SKLT. Declaration dialect можно читать и связывать с задачами, но переход в MANAGED требует рассмотренного адаптера или канонических SKLT. Для repo без SKLT возможен только минимальный managed shell с пустым индексом; структурные объекты не выводятся автоматически из source.

## Write boundary

All eight MCP write tools require a caller-supplied absolute physical `project_root` that exactly matches an administrator-approved writable root. The default allowlist is `[]`: it grants no write access, including to the default checkout. A read default is never inherited by a write. Reject original UNC input, any `..` component, and any route through a junction/reparse point before normalizing or resolving the path.

Read-only discovery/navigation remains cross-platform. The core writer lease is Windows-only, so writes fail closed on unsupported platforms. On Windows, writers pin and preflight the resolved destination, stage changes, and roll back failures they catch. This limits path redirection and handles ordinary caught errors; it is not an atomic or crash-safe multi-file transaction. Do not claim recovery after process termination, power loss, or an uncaught failure.

Never convert an `object_id` to a filesystem path. Pass exact logical IDs—including nested parser-returned declarations—only to object-aware APIs.
