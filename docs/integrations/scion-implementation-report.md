# DUNGEON-SCION-VCS-001 — Implementation report

Дата: 2026-10-15. Статус: **PARTIAL / EXPERIMENTAL**.

> Исторический отчёт Epic 001, а не текущий статус runtime. Его ограничения `SCION_RUNTIME_NOT_TESTED` и неактивированного Codex впоследствии частично заменены результатами Epic 002 от 2026-10-16: см. [runtime/activation report](scion-runtime-002-report.md). Исходные результаты, включая неудачные попытки, сохранены ниже без переименования в PASS. Production Windows/WSL transport и независимая приёмка по-прежнему не подтверждены.

Добавлен экспериментальный optional backend, а не подтверждённая runtime-интеграция или релиз. `SCION_RUNTIME_NOT_TESTED`. Независимая приёмка не выполнена. Исходный проект остаётся Git-only; Scion не включён в его manifest. Commit, push, release, установка клиента и обновление активного Codex cache не выполнялись.

## 1. Изученный Scion

[CarterT27/scion](https://github.com/CarterT27/scion/tree/7d445a0744e30f4dcb36a794b0ce42648157e1b9), workspace `0.1.0`, commit `7d445a0744e30f4dcb36a794b0ce42648157e1b9`, Apache-2.0, Rust 1.98. Изучены README, релевантные разделы SPEC, CLI arguments/queries, working-copy implementation, JSON v1 schemas, proto services и тестовая инфраструктура. Локальный upstream checkout хранится только в игнорируемом `artifacts/scion-vcs-001/upstream`; он не изменён и не включён в распространяемый Dungeon tree.

## 2. Реальные CLI/API contracts

Адаптер использует upstream CLI, а не выдуманный HTTP API:

```text
scion -C <scope-root> --json show <revision>
scion -C <scope-root> --json log --limit <1..100> --rev <revision>
```

Проверяются схемы `scion.cli.show.v1`, `scion.cli.log.v1` и error envelope, вложенные ID, ограничения результата и корректность JSON. Вызовы этих команд проверены с mock subprocess; реальный Scion CLI не запускался. Прямой gRPC client не добавлен.

Upstream `status` может восстанавливать pending operations и записывать индекс, а working-copy `diff` открывает изменяемое состояние/cache. Поэтому эти команды **не вызываются** через read-only поверхность. Локальный `.scion/state` читается без recovery; base записывается как `LOCAL_WORKING_COPY_BASE`, не как текущий server head.

## 3. Изменённые файлы Dungeon

Всего 43 добавленных/изменённых публичных файла, включая этот отчёт; точный перечень приведён ниже. Реализация разделена на backend, orchestration, additive provenance и CLI/MCP transport. Релизный номер пакета оставлен `0.4.1`: это неопубликованные изменения после `v0.4.1-rc1`, не содержимое старого тега.

```text
.gitignore
README.md
docs/agent-guides.md
docs/architecture.md
docs/evidence-manifest-v1.schema.json
docs/module-isolation-git-scope.md
docs/project.md
docs/vcs-backends.md
docs/cronica/2026-10-15-scion-vcs-001.md
docs/integrations/scion.md
docs/integrations/scion-runtime-check.md
docs/integrations/scion-implementation-report.md
scoping/git_scope.sklt
tests/test_vcs_backends.py
tests/test_vcs_provenance.py
tests/test_vcs_schema.py
tests/test_vcs_service.py
tests/test_vcs_transport.py
tools/project_structure/cli.py
tools/project_structure/vcs_backends.py
tools/project_structure/vcs_service.py
tools/project_structure/v03_evidence.py
tools/project_structure/v03_manifest.py
tools/project_structure/v03_schema.py
tools/project_structure/v03_snapshots.py
tools/project_structure/v03_validation.py
tools/project_structure/v041_proof.py
tools/project_structure/v04_projects.py
tools/project_structure/v04_sklt.py
tools/project_structure/v04_trace.py
tools/publication/verify.py
tools/codex-plugin/dungeon-system/.codex-plugin/plugin.json
tools/codex-plugin/dungeon-system/README.md
tools/codex-plugin/dungeon-system/mcp/server.py
tools/codex-plugin/dungeon-system/references/scion.md
tools/codex-plugin/dungeon-system/skills/dungeon-adoption/SKILL.md
tools/codex-plugin/dungeon-system/skills/dungeon-evidence/SKILL.md
tools/codex-plugin/dungeon-system/skills/dungeon-navigate/SKILL.md
tools/codex-plugin/dungeon-system/skills/dungeon-review/SKILL.md
tools/codex-plugin/dungeon-system/skills/dungeon-structure/SKILL.md
tools/codex-plugin/dungeon-system/skills/dungeon-task-trace/SKILL.md
tools/codex-plugin/dungeon-system/tests/test_v041_transport.py
tools/codex-plugin/dungeon-system/tests/test_v04_mcp.py
```

Навыки получили краткие backend-specific инструкции и один общий reference, без загрузки полного Scion SPEC в обычную навигацию. Их имена, количество и структурные состояния сохранены. Существующий `scoping.git_scope` расширен без смены ObjectId; derived projections пересчитаны инструментом. Glyf, AI Revit, RudraOffice и Rust_Makepad_Toolkit не изменялись.

## 4. VCS configuration

Опциональный фрагмент `.project/project.json`:

```json
{
  "version_scopes": [
    {
      "id": "scion-lab",
      "backend": "SCION",
      "root": "lab",
      "repository": "lab",
      "mode": "MONOREPO",
      "default_ref": "main",
      "endpoint_profile": "LAB"
    }
  ]
}
```

Это пример, не выполненная миграция. Scope IDs стабильны; SKLT `git_scope` остаётся совместимой ссылкой. Пути относительные и contained; неизвестные поля и credentials в manifest отвергаются. Implicit Git `main` остаётся, если ни legacy `git_scopes`, ни `version_scopes` явно не объявляют ID `main`.

Значения endpoint/token/CA/transport передаются только через process environment: `DUNGEON_SCION_LAB_ENDPOINT`, `DUNGEON_SCION_LAB_TOKEN`, `DUNGEON_SCION_LAB_CA_FILE`, `DUNGEON_SCION_LAB_TRANSPORT`. Требуется явный token; автоматический login и обмен сохранённого GitHub credential отключены. Токен не передаётся в argv. Для Linux MCP оператор отдельно разрешает нужные переменные в `env_vars`: bundled configuration пока пересылает только `DUNGEON_PROJECT_ROOT` и `DUNGEON_PYTHON`. См. [настройку Scion](scion.md).

## 5. Совместимость Git

Git остаётся default backend. Legacy manifests, GitScope, SKLT `git_scope`, старые snapshot schema 1 и Git-only Evidence читаются без миграции. Root `main` Git Evidence продолжает использовать прежний receipt path даже при наличии Scion scope в другом месте. ObjectId/LevelId и EXP/grade не зависят от backend.

На disposable настоящих Git repos проверены status/history/inspect/diff, staged/unstaged/untracked, detached HEAD, contained nested repository, отсутствие ancestor fallback и защита от ambient Git overrides. CLI `vcs-status --scope main` также проверен на исходном проекте: PASS, backend GIT, фактический HEAD `16d4e9875b3340329547d2647647155d3dec5f4d`, dirty state DIRTY. Ничего не commit/push. Новый Git adapter дополнительно ограничивает subprocess и metadata paths; функциональная совместимость не означает побайтовую идентичность нового VCS JSON старым командам.

## 6. Что реализовано для Scion

Явная конфигурация и availability, безопасное чтение local base, typed revision, bounded `show`/`log`, раздельные ошибки client/auth/server/schema и планирование. Командные контракты и отказы протестированы mock-ами. Status не доказывает cleanliness: `dirty_state=UNKNOWN`, результат PARTIAL. Pending operations делают revision неизвестной; recovery не выполняется. Working-copy diff — `UNSUPPORTED_OPERATION`, не mock-обещание runtime-поддержки.

## 7. Git и Scion в одном Dungeon

Каждый VersionScope получает отдельный backend и observation. Отказ Scion не подменяется Git и не уничтожает успешный Git result: агрегат становится PARTIAL. Source refs разрешаются через scope объекта, а не root Git SHA. Исправлен проверенный регрессией случай, когда legacy Git SHA мог ошибочно показать CURRENT для объекта, переназначенного Scion.

Sparse plan возвращает только явно объявленные source paths в границах выбранного scope. Это не dependency closure и не checkout. Migration plan перечисляет объекты, уровни, refs и консервативный набор локальных snapshots/evidence для review; он не импортирует историю и не меняет Git.

## 8. Snapshots

Существующая schema 1 дополнена необязательными `revisions_schema: 1` и `revisions`, keyed by scope ID. Запись включает backend, scope_id, revision, ref, availability, dirty_state и observed_at. Scion ID — `c1` + 64 lowercase hex BLAKE3; Git ID — 40/64 hex; CAS — `sha256:` namespace.

Наблюдения последовательные: `PER_SCOPE_OBSERVATIONS_NOT_ATOMIC`. Старые manifests остаются допустимыми. Создан локальный структурный checkpoint `scion-vcs-001-experimental`; он фиксирует реальный Git scope, не выдуманный Scion runtime. Mixed Git/Scion snapshot и чтение старого формата проверены synthetic fixtures.

## 9. Evidence provenance

Необязательный top-level `source_revision` schema 1 содержит `scopes` с typed observations. Старые Git-only manifests сохраняют прежний формат. При typed capture требуются source files: их фактические bytes независимо хешируются и сохраняются в Dungeon SHA-256 CAS. Published JSON Schema обновлена и проверена на реальном captured payload, неправильных namespaces и управляющих символах.

Неизвестная revision/dirty state не становится CURRENT/VERIFIED. Неизменность CAS и актуальность source — разные проверки. Все typed Scion/nonlegacy VersionScope execution receipts пока fail closed: `VCS_PROOF_ADAPTER_UNSUPPORTED`. Старое Git Evidence не может верифицировать объект после смены его backend на Scion. Нового execution-proof adapter в этом Epic нет.

## 10. Windows / WSL

Native Windows и WSL execution для Scion сейчас `UNSUPPORTED_PLATFORM`: проверенного контракта перевода путей и containment нет. Нативная Linux query-ветка реализована, но её Scion runtime не проверен.

В существующем Ubuntu WSL2 обнаружены Rust/Cargo 1.93.1 при upstream minimum 1.98, отсутствуют `rustup`, `protoc` и Scion executable. Установка toolchain, сервисов или клиента не выполнялась. Найден минимальный upstream CLI + in-memory TestCluster smoke, но он **не запускался** из-за prerequisites. См. [точный runtime check](scion-runtime-check.md). Это не PASS и не доказательство Scion integration.

## 11. Unit и integration tests

Окончательный gate `artifacts/scion-vcs-001/regression-final-v3`: **312 тестов, 311 PASS, 1 SKIP, 0 FAIL, 0 ERROR**, successful, inputs unchanged. Core: 288 run / 287 PASS / 1 SKIP; plugin: 24/24 PASS. Пропущен реальный symlink test из-за WinError 1314 (нет привилегии Windows); он не считается PASS. JSON Schema зависимости доступны, schema test не пропущен. Исходники/tests/transport не менялись между before/after input hashes.

Добавлены 63 VCS-теста: backend 30, provenance 16, service 8, transport 8, published schema 1. Настоящий stdio MCP тест вызывает новый VCS status с disposable Git repo и пустым writable allowlist; bytes проекта остаются неизменными. Настоящий Scion integration smoke: NOT_RUN / `SCION_RUNTIME_NOT_TESTED`.

Предыдущие попытки не скрыты. `regression-final` (до добавления schema test): 310 PASS, 1 SKIP, 0 FAIL/ERROR, inputs unchanged. `regression-final-v2`: 310 PASS, 1 ERROR в teardown `test_status_json_output`, 1 SKIP; ошибка — TemporaryDirectory cleanup оставил root. Отдельный повтор этого legacy теста, `test_stale_grade_is_reported` и новой schema проверки: 3/3 PASS. В более раннем прямом delegated прогоне также была cleanup ERROR в `test_stale_grade_is_reported`; targeted repeat прошёл. Cleanup helper не менялся ради зелёного результата. Начальный development gate с менявшимися inputs не считается приёмкой.

Локальные логи и машинные receipts находятся в игнорируемом `artifacts/scion-vcs-001/`; они не являются независимым аудитом и не прикладываются к релизу.

## 12. Security regression и остаточные ограничения

Проверены explicit roots/empty allowlist, traversal, reparse guards, unsafe metadata/config, backend namespace mismatch, invalid/partial/duplicate JSON, timeout, kill owned process tree, combined output budget, отсутствие implicit checkout/commit/land/login и secret-safe diagnostics. Бюджет subprocess: 10 секунд и 1 MiB совокупного stdout/stderr. Windows symlink privilege test отдельно отмечается SKIP, не PASS; mock reparse tests не заменяют его.

Статус структуры: PASS, 0 errors, 0 warnings, 22 SKLT, 0 RARE; object index, EXP, grade и checkpoint CURRENT. Основной DungeonStore остаётся NOT_INITIALIZED: 0 CAS objects, 0 manifests. `git diff --check` не обнаружил whitespace errors; есть только предупреждения Git о line endings. Bounded scan всех 43 изменённых/добавленных публичных файлов, включая этот отчёт: **0 findings** (secrets/private paths/generated artifacts). Сканер — bounded content scan, не гарантия отсутствия любого секрета и не новый аудит всей Git history.

Git/config execution не является sandbox для произвольного враждебного repository configuration. Полной защиты от concurrent filesystem swaps или server/executor authenticity не заявлено. Независимая security приёмка pending.

Обнаружена и исправлена ошибка ownership в раннем тестовом fixture: относительный store вышел из его TemporaryDirectory в общий TEMP/store. Он мог оставить 11-байтный synthetic CAS blob `f05a65413338d4d74cc2e7115db156601cfcb7838936e091cb74c7ecf0609277` без секретов. Fixture теперь использует собственный parent с project/store и проходит тесты. Общий TEMP/store не удалялся: чужое содержимое и возможный residual сохранены. Неопознанные остатки ранних неудачных cleanup также не удалялись вручную. В отчёт не включены абсолютные пользовательские пути.

## 13. Фактический Codex MCP discovery

Windows launcher и настоящий stdio `tools/list` исходного пакета: **34 tools / 6 Skills**, исходные 30 имён сохранены, 4 новых `dungeon_vcs_status`, `dungeon_vcs_inspect`, `dungeon_vcs_history`, `dungeon_vcs_diff` объявлены read-only. Smoke `mcp-final-v2/result.json`: successful; MANAGED root, write disabled, empty allowlist write rejected, source/package hashes unchanged. Actual VCS dispatch дополнительно покрыт stdio test из раздела 11.

В активной сессии Codex фактически callable **30 Dungeon tools, 0 новых VCS tools**. Установленный plugin cache не обновлялся, app не перезагружался. Нельзя заявлять, что новые инструменты уже доступны активным агентам. Обновление установленного пакета и повторный app discovery остаются отдельным шагом.

## 14. Unsupported / следующий этап

Реальный Scion runtime smoke и Linux runtime validation; Windows/WSL transport; безопасный Scion status/working-copy diff; current cleanliness/server-head proof; sparse execution/checkout (Phase 3); commit/land (Phase 4); автоматический install/login/import/migration; dependency closure; cross-backend atomicity; typed execution-proof adapter; обновление установленного Codex cache. Bidirectional Git/Scion sync не обещается. Git history и source identities сохранены.

## 15. Лицензирование

Корневой MIT LICENSE Dungeon не изменялся. Scion остаётся внешней optional Apache-2.0 capability; его source, schemas и binaries не скопированы в публикуемый tree. Добавлены собственные Python adapters и ссылки на upstream. В этом изменении нет vendored Scion, для которого следовало бы переносить upstream NOTICE. Если позднее появится vendoring/distribution Scion, нужны отдельная проверка Apache notices/NOTICE и сохранение upstream лицензии, а не переобъявление его исключительно под MIT.

**Итог:** Dungeon owns structure; VCS owns source history; CAS owns immutable execution artifacts. Git by default, Scion by explicit choice. Готовность runtime и независимая проверка не заявляются.
