# Structural architecture rules

- `SKLT` — structural source of truth.
- `GLIF` — UI projection, связанная с SKLT через `object_id`.
- `object_id` — logical identity, not a pathname. Preserve the parser-returned ID exactly, including qualified nested declarations; never use it as a filesystem path or derive it from a filename.
- `.exp`, `docs/.grade`, `.project/objects.json` и `.project/state.json` — derived artifacts.
- В текущем MVP существуют только состояния `SKLT` и `RARE`.
- `libs/` содержит external/reusable dependencies и не является обычным project module.
- `docs/`, `snapshots/`, `libs/`, `tools/` и `tests/` не учитываются в EXP.

## External execution boundary

External execution capabilities are referenced, not reimplemented.

`Dungeon System` хранит optional stable refs на provider/kind/id и проверяет их structural format. `Rust_Makepad_Toolkit` владеет actual UI inspection, Script API, scheduler, ContainerEnvironment, runtime lifecycle, permissions, catalogs, reports и evidence.

Актуальная точка входа Toolkit:

`<TOOLKIT_ROOT>/docs/INDEX.md` (an external checkout configured by its owner;
not included in this repository).

Текущий documented container key — `docker-linux-level0`. Container Level 0 запускается только через documented public runner и не доказывает GUI/UIA/physical input/Makepad GUI capabilities.

Dungeon System не реализует agent roles, runtime workflow, package manager, library downloader, AST/LSP extractor, Git replacement, container scheduler, registry, image management, VM или sandbox.

## Git, snapshots, evidence and CAS

- Git хранит редактируемые structural/source files, small evidence manifests и seals.
- Cronica объясняет, почему менялась структура.
- Snapshot фиксирует structural state и может хранить evidence IDs.
- Evidence Manifest связывает objects, Git, snapshot, Toolkit capability, result и CAS references.
- Внешний local CAS хранит immutable exact bytes, доступные по SHA-256.
- Sealed manifest и seal append-only; CAS bytes не перезаписываются.
- Derived search indexes остаются восстанавливаемыми и не становятся authority.
- Постоянные execution directories в проекте запрещены; cleanup выполняется после CAS capture и verify.

Настройки store находятся в .project/project.json под evidence_store. CAS недоступность сама по себе не делает structural validation неуспешной.

## Plugin v0.4 project boundary

Project modes `NOT_DUNGEON`, `SKLT_ONLY`, `MANAGED`, `INVALID_MANAGED`, `UNAVAILABLE` описывают подключение, а не Grade. Legacy MCP `project_mode: DUNGEON` сохраняется как alias для нового `mode: MANAGED`.

Read-only discovery/catalog/inspection не сохраняет индекс. SKLT-only adapter использует explicit identity либо qualified declared symbol из SKLT, с явным `identity_origin`; pathname identity не создаётся. Обнаруженные duplicates блокируют однозначное обращение к объекту. Unsupported dialect остаётся доступным через bounded file navigation и получает конкретные adoption blockers.

Adoption — явная операция по fingerprinted plan: принятые modules, сохранение существующих IDs и SKLT, минимальный manifest, derived projections и validation. Ни architecture, ни API/dependencies из source автоматически не создаются. Повторное применение не дублирует записи; конфликтующие данные требуют нового плана.

Task Trace и coverage — небольшие implementation metadata, отделённые от derived object index. `assigned_agent` ссылается на внешнего исполнителя и не создаёт authority. Task Trace не планирует и не запускает работу. Proof acceptance requires typed identity fields (`id`, execution `type`, and `scope`) to match the selected object, Task Trace and requested coverage scope; required performed checks, task result and actual structured execution result must each report `PASS`.

Prototype и Product имеют независимые `UNKNOWN/NOT_STARTED/PARTIAL/IMPLEMENTED/VERIFIED`. Их изменение не меняет `SKLT/RARE`, `.exp` или `.grade`. `NOT_STARTED` требует подтверждённого отсутствия реализации. Coverage `VERIFIED` требует проверяемого evidence с object/task linkage и применимостью к текущему source/revision/build; для проверки EXE обязателен его SHA-256. Immutable CAS/seal integrity is distinct from current proof state (`VERIFIED`, `INCOMPLETE`, `CONFLICT`, `STALE`, or `INVALID`). Unknown evidence formats or unavailable raw bytes yield `PROOF_UNVERIFIED`, never a verified result. A changed obligated SKLT makes current proof stale; the old manifest remains sealed and unchanged.

Read-only discovery/navigation remains cross-platform. All eight MCP write tools require an explicit absolute physical `project_root` matching an administrator-approved writable root. The default allowlist is `[]`; no write inherits the default checkout. Reject raw UNC input, `..`, and junction/reparse traversal before normalization. The core writer lease is Windows-only and writes fail closed elsewhere. Windows writers pin and preflight destinations, stage updates and roll back caught failures, but do not provide crash-safe multi-file transactions.

Source-link audit различает existence/hash и semantic correspondence. Без AST/LSP semantic и symbol aspects остаются `UNKNOWN`. Tool не исправляет ссылки по догадке. The original 2026-10-10 audit remains a historical `FAIL` and is not rewritten; a later re-audit must be independent, and a component's self-reported `INDEPENDENT_VERIFIED` is not its own final independent acceptance.

The Windows boundary covers ordinary filesystem opens. New stages receive a
temporary deny-only FILE_WRITE_ATTRIBUTES ACE at creation, with inherited
permissions retained. Only that injected ACE is removed via the owned handle,
after data completion; subsequent data writes are prohibited. It is not a
boundary against owner WRITE_DAC, privileged handle duplication or preexisting
writable mappings. Snapshot root identity remains leased during native rename,
but child handles must close; post-rename identity/content/tree checks detect
controlled substitutions, not an atomic hostile-writer whole-tree transaction.
