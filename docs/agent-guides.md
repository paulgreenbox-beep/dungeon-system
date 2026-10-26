# External operational guides

## Optional version-control backends

Git remains the default. Before a Scion query, inspect the explicitly declared
VersionScope with `dungeon_vcs_status`. Read [VCS backends](vcs-backends.md) and
the small [Scion guide](integrations/scion.md) only when that backend is involved.
Unavailable means unavailable: do not invent a revision, silently use Git,
start a daemon, log in, checkout, recover, commit or land. A sparse plan contains
only declared source refs, not a verified transitive build closure. Backend
availability, source hashes and runtime proof are separate facts.

## Rust_Makepad_Toolkit

**Canonical entry point**

`<TOOLKIT_ROOT>/docs/INDEX.md`, where `<TOOLKIT_ROOT>` is an explicitly configured
external Toolkit checkout. No particular developer's checkout is required or
distributed with Dungeon System. Register a guide only when you have access to it.

Перед использованием любой capability Rust_Makepad_Toolkit агент сначала открывает этот `INDEX.md`, а затем выбирает минимальный актуальный специализированный guide.

**Выбор guide**

- `.glif`, UI inspection, UI interaction и UI testing: `AI_UI_TEST_TOOL_AGENT_GUIDE.md`, подтверждённый текущим `INDEX.md`.
- execution environments и scheduler: `prototypes/ai-ui-test-tool-auto-probe/scheduler/README.md`.
- Container Level 0: `prototypes/ai-ui-test-tool-auto-probe/scheduler/container/README.md` и текущий `environment-catalog.json`.
- reliability/evidence: `prototypes/ai-ui-test-tool-auto-probe/RELIABILITY.md`.

**Container boundary**

Текущий documented container — `docker-linux-level0`, `ContainerEnvironment Level 0`. Он запускает только публичный `ai-ui-test run`, disposable, без network, GUI, UIA, physical input, GPU и independent desktop. Dungeon System не запускает, не планирует и не настраивает контейнеры.

**Agent boundary**

```text
Dungeon System
    ↓
object_id / SKLT / GLIF / external refs
    ↓
Rust_Makepad_Toolkit/docs/INDEX.md
    ↓
актуальный guide и documented execution tool
```

## Evidence capture

После documented Toolkit execution, если результат важен для сохранения, агент отправляет выбранные артефакты в CAS, создаёт Evidence Manifest, проверяет hashes и seal-ит значимую запись. Сначала сохраняет и проверяет bytes, потом очищает временное execution workspace. Lifecycle и команды описаны в [docs/evidence.md](evidence.md).

Содержимое внешних guides не копируется в Dungeon System. `Rust_Makepad_Toolkit` не изменяется без отдельной задачи.

## Project adoption and implementation trace

Перед broad source exploration используй `dungeon_project_probe` с абсолютным root. Для `NOT_DUNGEON` прочитай `dungeon_adoption_plan`; применение плана — отдельное явно запрошенное действие. `SKLT_ONLY` поддерживает summary → module → object → bounded SKLT без adoption. Основной контракт плагина и примеры: [MCP guide](../tools/codex-plugin/dungeon-system/README.md).

Проверь Task Trace: object IDs, declared source/test refs, required/performed checks и evidence. Для executable tests сохраняй identity проверенной программы: Git revision, EXE SHA-256, Toolkit capability, result и CAS refs. Proof должен включать typed required `id`, execution `type` и `scope`, согласованные с объектом и Task Trace; required performed checks, task result и actual structured execution result должны совпадать и быть `PASS`. Текстовый `PASS` не переводит coverage в `VERIFIED`. Если current build/source не совпадает с captured identity, показывай mismatch и проводи новую соответствующую проверку через Toolkit.

Sealed CAS доказывает неизменность сохранённых bytes, но не актуальность результата. Текущая проверка отдельно возвращает `VERIFIED`, `INCOMPLETE`, `CONFLICT`, `STALE` или `INVALID`; unknown format или отсутствующие raw bytes дают `PROOF_UNVERIFIED` и запрещают вывод `VERIFIED`.

Для единственного положительного raw adapter используй [implemented v0.4.1 contract](proof-adapters-v041.md); он принимает только versioned Toolkit `reliability-v2` и не подтверждает executor authenticity или full GPU/UI acceptance. Не выводи PASS из неизвестного receipt или произвольного text/JSON.

Для каждого proof obligation захватывай hash SKLT при создании нового evidence. Изменение такого SKLT делает текущий proof `STALE`; старый manifest/seal остаётся неизменным `SEALED`, новая проверка создаёт новую запись. Не переписывай исторический audit/report от 2026-10-10: его `FAIL` остаётся фактом. Целевой повторный аудит должен быть независимым; собственный `INDEPENDENT_VERIFIED` не является финальной независимой оценкой.

Prototype и Product оцениваются отдельно. Ни наличие исходника, ни имя capability, ни SKLT state не доказывают readiness, и coverage не изменяет `SKLT/RARE`, EXP или grade. Source-link audit подтверждает path/hash и честно оставляет semantic aspects unknown без анализа символов.
