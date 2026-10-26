# DUNGEON-SCION-RUNTIME-002 — Real runtime report

Дата: 2026-10-16. Итог: **IMPLEMENTED / RUNTIME-TESTED — READY_FOR_INDEPENDENT_REAUDIT**.

Это проверка реализации исполнителем, не независимая приёмка. Gates A–C подтверждены реальными disposable fixtures; Gate D — установленным launcher и настоящей новой Codex CLI-сессией. GitHub push, production migration и изменение MIT LICENSE не выполнялись.

## 1. Scion commit/version

Внешний [CarterT27/scion 0.1.0](https://github.com/CarterT27/scion/tree/7d445a0744e30f4dcb36a794b0ce42648157e1b9), commit `7d445a0744e30f4dcb36a794b0ce42648157e1b9`. Upstream исходники не изменялись. Чистый Windows checkout и его private build copy сверены: 596 tracked files, aggregate byte digest `e271202c079cbc694485a602f35d37e6e822b73e1ccf0e6f98849ad055e7fb60`. Git text policy Windows `core.autocrlf=true` была явно учтена, а не принята за изменение исходников.

## 2. Toolchain / platform

На native Linux в существующей Ubuntu WSL2: `rustc 1.98.0 (88d9e12ae 2026-08-18)`, `cargo 1.98.0 (797e8a9bc 2026-08-05)`, `libprotoc 32.1`; kernel `6.18.54.1-microsoft-standard-WSL2`, x86_64. Установлены в отдельный owned mode-0700 workspace, не вместо системного Rust 1.93.1. Применены `--no-modify-path` и private Cargo/rustup roots; sudo, Docker, CockroachDB, MinIO, системные службы и глобальные Windows settings не использовались. Rustup installer и protoc archive сверены с официальными SHA-256.

Сборка выбрана по минимальному достаточному пути: `cargo build --locked -p scion-cli --bin scion`, затем upstream `binary_help_usage_and_whoami` с настоящим executable и TestCluster. Не запускался ненужный full-workspace/all-targets gate. Повтор `build-03` использует закрытый список переменных окружения, без ambient credentials, и прошёл с exit 0.

## 3. Фактический binary SHA-256

Runtime executable: `<PRIVATE_LINUX_WORKSPACE>/target/debug/scion`, 21 982 112 bytes.

```text
e9233886e8fa970af79fcf6e8dcbd44f45b0f33b25da67953f6a87698bf52d7f
```

Первый dev build имел другой SHA-256 (`3e350b1a72696625aa661cac1c54b42bc0e578e537b2c4e7602acc699c548ced`): Cargo test profile пересобрал тот же путь. Оба наблюдения сохранены; runtime gate требует именно окончательный hash. Не использовался старый бинарник без успешной сборки. Контрольные суммы отдельного fixture helper записаны в его build receipts, не выдаются за Scion binary hash.

## 4. Реальный TestCluster

Отдельный Rust helper использует публичные upstream `TestCluster`, `ClusterOpts::default().with_daemon(false)`, `GenRepo` и canonical seed RPC. Это in-memory metadata/object store, настоящий loopback TLS/gRPC server и реальные Scion objects. Helper создаёт линейную историю из трёх changesets и отдельный ref `runtime-fixture`; production server не используется.

Working copy создаётся **внешним Scion CLI checkout** на native ext4. `.scion/state` не сочиняется тестом. Тестовые credentials передаются только через child environment и private mode-0600 context на ext4; в repo receipts их нет. После проверок helper завершился с exit 0, loopback listener проверенно закрыт. Private toolchain/binaries и fixtures сохранены для воспроизводимости; чужие временные папки не удалялись.

## 5. Настоящие revision IDs

От старой к новой:

```text
c14df68cfab35724856a0a1bf184bfc451c958dc5b9968cb6c5a1beb6c5f930f5b
c126bd0e41dd1918fcb28b4f8717275de197f91cbfdae5f9d11566666ef55f9611
c1023926a37f2c46cebaf9b1b8200372139d86c214b4a9aac81d3a099298697870
```

Actual checkout содержит `renderer/settings.rs` с `QUALITY = 2` и `renderer/frame.rs` с `render_frame() -> 2`. Настоящий `scion cat` подтвердил точное совпадение server bytes с checkout bytes; эти же bytes были экспортированы для CAS capture.

## 6. Show / log receipts

Реально выполнены:

```text
scion -C <EXT4_CHECKOUT> --json show <ACTUAL_LATEST_REVISION>
scion -C <EXT4_CHECKOUT> --json log --limit 3 --rev <ACTUAL_LATEST_REVISION>
```

Обе команды exit 0. Сохранены точные stdout bytes `show-raw.json` / `log-raw.json`, их SHA-256 и читаемые JSON views. Проверены schema v1, ID, ordering, parent links и limits 1/2/3. Нормализация использует настоящий Python backend и bounded subprocess, не mock.

Локальные runtime receipts: `artifacts/scion-runtime-002/run-04/linux-gate-01/`. Это игнорируемая папка, не release asset и не публичный evidence packet.

## 7. Dungeon backend integration

`TESTED_WITH_REAL_SCION`: 29 assertions. Среди них 14 real CLI/server assertions, 4 Dungeon real-backend assertions, реальные transport outage/timeout, built-binary binding и persistent filesystem inventory check. Три adapter rejections и четыре malformed/schema/namespace fault injections явно отделены от runtime proof.

Проверены VersionScope → vcs_service → ScionBackend → actual executable → actual TLS server → JSON → typed revision. Реально проверены неизвестная revision (raw `NOT_FOUND`), неправильный token (`AUTH_REQUIRED`), закрытый loopback endpoint и stalled TCP peer с deadline/termination. Malformed JSON, unsupported schema, unexpected output и wrong backend identity проверяются как **ADAPTER_FAULT_INJECTION_NOT_RUNTIME_PROOF**, а не как реальные ответы Scion. Git fallback не используется.

Успешный query оставляет aggregate PARTIAL там, где неизвестна cleanliness. `LOCAL_WORKING_COPY_BASE` не приравнивается к server HEAD; `dirty_state=UNKNOWN`. Read-only queries не изменили проверенную inventory файлов, directories и symlinks; это before/after evidence, не полный syscall audit.

## 8. Mixed Git / Scion snapshot

В отдельной fixture: `main` SCION scope root `.` и `storage` GIT scope root `storage`. Настоящий nested Git commit:

```text
31f973ccba57b623bf9f676cde6b0daab30faf60
```

Windows writer создал настоящий `windows-scion-runtime-002` snapshot с этим Git SHA и actual latest Scion `c1…` ID. ObjectIDs — `renderer.settings`, `storage.database`; schema 1 сохранена, revisions extension typed. `PER_SCOPE_OBSERVATIONS_NOT_ATOMIC` сохранён. Во время настоящего Scion outage Git history оставался AVAILABLE; aggregate PARTIAL, никакой подстановки Git SHA вместо Scion ID.

Snapshot создан только над disposable mirror. Это не snapshot production Dungeon и не доказательство Windows Scion runtime. В Scion observation честно записаны `UNSUPPORTED_PLATFORM` и UNKNOWN dirty state.

## 9. Typed Evidence / CAS

Штатный Windows writer создал `EV-DUNGEON-SCION-RUNTIME-002` и source_revision с actual latest `c1…` ID. Source bytes подтверждены отдельно:

```text
sha256:4dff975abf8070ff65ecf179c5e9050bf9d0a3107c789417e6f7535dcd3ca000
```

CAS integrity VERIFIED. Scion BLAKE3 changeset ID не смешан с SHA-256 CAS. Capture status `CAPTURED_UNSEALED`, helper exit 1: seal ожидаемо отказал с `SOURCE_REVISION_UNKNOWN`. Proof остаётся STALE/unverified и содержит `VCS_PROOF_ADAPTER_UNSUPPORTED`; также отсутствует execution Task Trace/receipt. Это подтверждение fail-closed, **не execution Evidence acceptance**. Никакой proof guard не отключался.

## 10. Windows / WSL status

Production native Windows / WSL adapter по-прежнему `UNSUPPORTED_PLATFORM`. Прямой Scion checkout на `/mnt/c` в attempts 02/03 завершился `NOT_FOUND: stat written file: No such file or directory`; общая root cause Scion/DrvFS не объявляется установленной. Native ext4 checkout в attempt 04 прошёл без изменений Scion.

Для Gate C явно экспортирована побайтово сверенная копия настоящего ext4 checkout в Windows disposable mirror, без token/context. Windows writer создал metadata/capture; эти metadata и настоящий nested Git repo затем скопированы в исходную ext4 fixture для mixed backend queries. Actual `.scion/state` не менялся. `fixture_transfer.py` — операторский disposable setup, **не production WSL bridge**, не скрытая запись read-only MCP. Полная Windows/WSL transport/security поддержка не заявляется.

## 11. Unit / integration / security totals

- Заключительный frozen-code regression `regression-final-v2`: **325 run, 323 PASS, 2 SKIP, 0 FAIL, 0 ERROR**, inputs unchanged. Core 301/299 PASS/2 SKIP; plugin 24/24 PASS. Предыдущие полные gates по 322 теста также прошли; три добавленных parser tests используют явно синтетические NOTREAL fixtures для qualified names, отсутствия actual calls и FAIL в structured result. Это unit-проверки парсера, не runtime proof.
- Оба SKIP — real Windows symlink privilege WinError 1314; mock reparse tests проходят, но не заменяют пропущенные реальные проверки.
- Upstream real CLI/TestCluster smoke: 1 PASS, exit 0.
- Custom real pipeline: 29 assertions PASS с перечисленным в разделе 7 разделением происхождения.
- Дополнительный native-Linux installed-MCP → real Scion gate: 14 assertions PASS, четыре реальные VCS calls; server/tool versions и binary hashes связаны с уже проверенной сборкой/установкой.
- Source package stdio smoke: version `0.4.2-rc1`, 34 tools, 6 Skills, empty allowlist write denied, source/package unchanged.
- Security: explicit roots, traversal/reparse, bounded output at read time, owned process groups/deadlines, sanitized diagnostics, credential-free receipts, no implicit checkout/commit/land в MCP. Actual checkout/seed/fixture Git commit выполнялись только явно в тестовой infrastructure.

Bounded content scan всех 60 изменённых/добавленных публичных файлов: 0 findings. Два первоначальных scanner hits оказались synthetic global-config canaries в тестах; обозначены `test_mock_` без ослабления asserts или scanner rules, затем повторён весь regression gate. Это не новый аудит всей Git history и не гарантия отсутствия любых классов секретов. Snapshot/CAS/private contexts/builds/backup не входят в публичный tree. `git diff --check` не выявил whitespace errors; сохраняются только существующие line-ending warnings Git.

Неудачные попытки сохранены: 01 — helper создавал уже существующий `main`; 02/03 — local checkout error на DrvFS; source-check attempt build-02 остановлен **до сборки** из-за несовпадения Windows/Linux Git text policy. Исправлялись свои helpers/проверки, не Scion. build-03 и runtime-04 подтверждены отдельно; предыдущие failures не переименованы в PASS. Исторические cleanup failures Epic 001 остаются в его отчёте.

## 12. Codex MCP discovery / activation

Source `0.4.2-rc1` expose 34 tools, включая четыре существующих VCS queries. Перед установкой old cache `0.4.1` скопирован и проверен: 16/16 files, inventory digest `c57342dded09993e7ca0a8dea2e04cf167b74956379b878705763a3233c5a12c`. Backup находится в игнорируемом `tools/codex-plugin/backups/`.

**Gate D PASS.** Установлен новый personal package/cache `0.4.2-rc1`, без удаления old `0.4.1` и без смены plugin identity. Versioned source stage и installed cache совпали побайтово (18 files). Единственное ожидаемое отличие от portable source — `.mcp.json` с локальным `DUNGEON_PROJECT_ROOT`; writable roots остались `[]`. Остальные personal marketplace entries не менялись; исходный catalog сохранён рядом с backup.

Installed launcher smoke: actual initialize/tools/list — 34 tools, 6 Skills, version 0.4.2-rc1; четыре legacy read calls PASS, empty-allowlist write denied, source/package unchanged. В новой authenticated ephemeral Codex CLI-сессии (`gpt-6-luna`, max, read-only) реально обнаружены те же 34 logical names. В event stream подтверждены пять completed MCP calls с result PASS: project_probe и все четыре VCS tools. Inventory нового клиента точно совпал с actual installed tools/list. Эти VCS calls проверяли default Git scope; Windows Scion transport не объявляется поддержанным.

Первая новая CLI-сессия честно увидела 0 tools из-за неверно процитированного CLI config key в нашем driver; сохранена как NOT_ACCEPTED. Вторая сессия реально имела 34 tools и успешные calls, но первоначальный comparator не учитывал квалифицированный `mcp__dungeon_system__` prefix. Оригинальный receipt сохранён; отдельный `validated-result.json` воспроизводимо проверяет logical-name normalization и реальные MCP result statuses из сохранённых events, а не исправляет серверный результат. CLI также выдал несвязанные предупреждения global skill UTF-8/model catalog refresh; target MCP calls завершились успешно. Не менялись чужие skills или настройки моделей.

Активный родительский desktop chat сохраняет старый catalog: 30 tools. Для него нужен reload/new chat; он не выдаётся за новую проверенную CLI-сессию. Официальный [порядок local plugin refresh](https://developers.openai.com/plugins/build/plugins) также отделяет installed copy от source. Локальные receipts: `activation/package-hashes.json`, `installed-smoke-01/result.json`, `codex-session-02/events.jsonl`, `codex-session-02/validated-result.json` под `artifacts/scion-runtime-002/`.

Отдельно проверена не только Git ветка: installed `mcp/server.py` запущен нативным Linux Python против нового настоящего TestCluster (`run-05`). Actual `tools/list` вернул 34 tools/version 0.4.2-rc1; все четыре explicit-root VCS calls использовали SCION и actual `c1023926…` base. Inspect/history — AVAILABLE с UNKNOWN dirty state; status/diff — PARTIAL/UNKNOWN, diff UNSUPPORTED. 14 assertions PASS, inventory/source/state не изменились, server/helper exits 0 и listener закрыт. Это прямой installed-MCP/native-Linux test, не ещё одна Codex-сессия и не production WSL bridge. Санитизированные requests/responses/result сохранены в `run-05/linux-mcp-gate-01/` под ignored artifacts; token не копировался. Hash установленного server.py совпал с `activation/package-hashes.json`, Scion binary SHA — с `build-03`.

## 13. Шесть Skills

Сохранены dungeon-adoption, dungeon-evidence, dungeon-navigate, dungeon-review, dungeon-structure, dungeon-task-trace. YAML/frontmatter и ровно шесть имён проверены source smoke. Общий Scion reference сохраняет границы local base, UNKNOWN, supported query, no implicit writes и fail-closed proof. Полный Scion SPEC не включён в каждый context.

## 14. Изменённые файлы этого Epic

Новое: `tools/scion_runtime/bootstrap.py`, `build.py`, `prepare_cluster.py`, `linux_gate.py`, `linux_mcp_gate.py`, `fixture_transfer.py`, `windows_capture.py`, `plugin_activation.py`, `codex_session_check.py`, `README.md`; `cluster/Cargo.toml.in`, `cluster/src/main.rs`; `tests/test_scion_runtime_capture.py`, `tests/test_scion_plugin_activation.py`, `tests/test_scion_runtime_session.py`; этот отчёт и Cronica `2026-10-16-scion-runtime-002.md`.

Обновлены source plugin manifest / MCP VERSION / transport version assertion, publication smoke version comparison, VCS/Scion/runtime/project/architecture/plugin docs, общий Scion reference и relevant Skills. Не переписывались core VCS architecture, GitScope, stable IDs, snapshot/Evidence formats, CAS authority, Windows writer guard или proof adapters. Предыдущие неопубликованные изменения Epic 001 сохранены. Upstream source и production foreign projects не менялись.

## 15. Unsupported / licensing

Production Windows/WSL transport, safe working-copy status/diff, sparse checkout execution, Codex commit/land, production migration/import/server deployment, cross-VCS atomicity и typed execution-proof adapter остаются unsupported. Real query не доказывает source application behavior или clean checkout. Никакой Git replacement/workflow engine не добавлен.

Dungeon MIT LICENSE не менялся. Scion остаётся внешней optional Apache-2.0 capability, без vendoring upstream source/binaries в Git. Private toolchain, downloaded archives, builds, CAS и runtime packets находятся вне публичных исходников или в ignored artifacts.

## 16. Acceptance gates

| Gate | Result | Основание |
| --- | --- | --- |
| A — Real Scion | PASS | Собранный pinned CLI, real TLS TestCluster, три actual revisions, show/log JSON v1 |
| B — Dungeon backend | PASS | Actual subprocess/backend/service, typed IDs, outage isolation, stable object identity |
| C — Snapshot / Evidence | PASS, provenance only | Actual mixed revisions и CAS bytes; unknown/seal/proof fail closed; Windows-only writer не обходился |
| D — Codex plugin | PASS | Installed launcher 34/6, fresh authenticated Codex CLI 34 tools, actual probe + four VCS calls PASS |

## 17. Точный статус

**IMPLEMENTED / RUNTIME-TESTED — READY_FOR_INDEPENDENT_REAUDIT.** A–D пройдены в указанных границах. Статус **не** `INDEPENDENTLY VERIFIED`: самостоятельная runtime-проверка исполнителя не заменяет отдельного аудитора. Native Windows/WSL transport и execution-proof adapter остаются unsupported и не входят в доказанную область.
