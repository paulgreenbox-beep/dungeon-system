# Storage policy: EDITABLE / CAS / PACK

## 1. Назначение

Политика определяет, какие файлы остаются обычными файлами проекта, какие хранятся через CAS, а какие объединяются в переносимые Dungeon Packs.

Цели:

```text
не раздувать Git tree;
не хранить тысячи дублирующихся frozen files;
сохранять normal Git diff для editable artifacts;
дедуплицировать тяжёлые artifacts;
позволять выборочное восстановление;
позволять переносить набор artifacts одним файлом;
не терять provenance и integrity.
```

Главный принцип:

> **Editable stays visible. Immutable goes to CAS. Portable goes to Pack.**

## 2. Три формы хранения

```text
EDITABLE
    обычные файлы

CAS
    immutable content-addressed objects

PACK
    logical или portable container объектов
```

`EDITABLE` используется для того, что человек или агент регулярно читает, изменяет и ревьюит. `CAS` используется для immutable или тяжёлых artifacts. `PACK` используется для объединения набора CAS objects в логическую группу или переносимый архив.

## 2.1 Три независимые оси

`Structural State`, `Retention Class` и `Storage State` существуют независимо. Смешивать их нельзя: они отвечают на разные вопросы.

```text
Structural State:
    SKLT / RARE
    что реализовано в структуре проекта

Retention Class:
    PERMANENT / CACHE / EPHEMERAL, операционно HOT
    насколько важно сохранять эти bytes

Storage State:
    LIVE / CAS / PACKED / ARCHIVED / OFFLINE / MISSING / CORRUPT
    где и в какой физической форме сейчас лежат bytes
```

В `.sklt` эти оси представлены разными необязательными полями и не подменяют друг друга:

| Поле | Ось | Значения |
|---|---|---|
| `state` | Structural State | `SKLT`, `RARE` |
| `retention` | Retention Class | `PERMANENT`, `CACHE`, `EPHEMERAL`, `HOT` |
| `storage_state` | Storage State | `LIVE`, `CAS`, `PACKED`, `ARCHIVED`, `OFFLINE`, `MISSING`, `CORRUPT` |
| `flags` | Operational | `PINNED`, `SEALED`, `GC_CANDIDATE`, `RESTORE_REQUIRED`, `REPLICATED` |

Structural source files по определению `LIVE`; поле `storage_state` для них необязательно и по умолчанию равно `LIVE`. `HOT` в Retention Class — операционное состояние working set, а не класс хранения наравне с `PERMANENT`.

Оси валидируются раздельно: `INVALID_STORAGE_STATE`, `INVALID_RETENTION_CLASS`, `INVALID_STORAGE_FLAG`, `INVALID_STATE`. Ни одна из них не выводится из другой.

## 2.2 Значения Storage State

```text
LIVE
    обычный доступный файл рабочего проекта: current .sklt, .glif, docs/*.md,
    .exp, docs/.grade, current manifests. Не требует CAS restore и pack extraction

CAS
    отдельный blob, hash проверен, объект сразу извлекаем

PACKED
    часть Logical Pack или Portable Pack; для Logical Pack bytes могут оставаться CAS objects.
    PACKED не обязательно означает COLD

ARCHIVED
    сохранён, но перемещён из active working storage в cold или archive storage;
    retrieval может требовать restore

OFFLINE
    система знает hash, pack и location descriptor, но physical storage сейчас недоступен

MISSING
    manifest ссылается на artifact, но доступной или зарегистрированной копии нет

CORRUPT
    bytes существуют, но SHA256 не совпадает с declared hash, либо pack не проходит verification
```

`OFFLINE` отличается от `MISSING`: при `OFFLINE` известно, где объект должен находиться. `CORRUPT` никогда не превращается автоматически в валидное состояние — нужно restore, re-import, rebuild или пометка evidence как broken.

## 2.3 Operational flags

Флаги не раздувают Storage State и хранятся отдельно:

```text
PINNED
    нельзя автоматически перемещать или удалять: critical evidence, release artifact,
    benchmark baseline, current accepted snapshot

SEALED
    содержимое зафиксировано и не меняется

GC_CANDIDATE
    удовлетворяет условиям автоочистки: retention CACHE, unreachable, не pinned, не sealed

RESTORE_REQUIRED
    для использования требуется restore

REPLICATED
    существует несколько physical locations; replica_count считается отдельно
```

## 2.4 Storage lifecycle

```text
LIVE
  ↓
CAS
  ↓
PACKED
  ↓
ARCHIVED
```

Возврат:

```text
ARCHIVED
   ↓ restore
CAS
  ↓ materialize
LIVE
```

Отклонения:

```text
ARCHIVED
   ↓ storage отключён
OFFLINE

потерянные bytes
   ↓
MISSING

несовпадение hash
   ↓
CORRUPT
```

## 2.5 Archive policy

Автоматическая архивация разрешена только для artifacts, которые:

```text
не являются current editable structural files;
имеют CAS identity;
прошли verify;
не находятся в active working set;
имеют известный retention class
```

Запрещено автоматически архивировать текущие `*.sklt`, `*.glif`, `source`, `docs/project.md`, `docs/map.md`, `.project/project.json`. Их historical copies архивировать можно.

Archive и GC — разные вещи: `ARCHIVE` сохраняет bytes и перемещает их в cold storage, `GC` физически удаляет. Artifact может быть `ARCHIVE_CANDIDATE`, но не `GC_CANDIDATE`, если он `PERMANENT`.

## 2.6 Archive rule

Archive выполняется только в порядке:

```text
validate object
   ↓
ensure CAS copy exists
   ↓
verify hash
   ↓
ensure retention policy allows archive
   ↓
write archive location
   ↓
verify archive copy
   ↓
update storage registry
   ↓
only then remove the hot copy
```

Главное правило: **copy → verify → register → remove old location.** Удаление или перенос с последующей надеждой на успех архива запрещены.

## 2.7 GC rule

Перед физическим удалением:

```text
not reachable
AND retention == CACHE
AND not pinned
AND not required by sealed evidence
AND not required by sealed pack
```

Сначала выполняется GC dry-run. Archive candidates и GC candidates вычисляются детерминированно, но Collector никогда не архивирует и не удаляет сам: действие решает policy или агент.

## 2.8 Pack archive states

Portable Pack может быть перемещён в cold storage. Его состояния `LOCAL_PACK`, `ARCHIVED_PACK`, `OFFLINE_PACK`, `BROKEN_PACK` отображаются в общую ось Storage State как `PACKED`, `ARCHIVED`, `OFFLINE`, `CORRUPT`. Вторая несовместимая система состояний не создаётся.

Pack lifecycle и Storage State — независимые оси:

```text
Pack lifecycle:
    DRAFT → VERIFIED → SEALED → ARCHIVED

Storage State:
    CAS / PACKED / ARCHIVED / OFFLINE / CORRUPT
```

## 3. Что обязательно остаётся обычными файлами

По умолчанию не упаковываются:

```text
current *.sklt
current *.glif
source code
tests
docs/*.md
.project/project.json
small manifests
Cronica entries
library descriptors
configuration
recipes
```

Причина: Git должен видеть semantic diff. Например, изменение

```diff
- depends_on: renderer.old
+ depends_on: renderer.resources
```

должно оставаться обычным Git diff. Запрещено скрывать текущую structural model внутри binary archive без отдельной причины.

## 4. Что должно уходить из Git tree

Большие generated или immutable artifacts не должны постоянно жить непосредственно в Git tree:

```text
screenshots
videos
large logs
runtime captures
compiler output
executables
PDB
OBJ
LLVM IR
ASM
large generated reports
frozen asset collections
container execution outputs
large snapshot payloads
temporary extracted evidence
```

Для них используется CAS или Logical Pack, ссылающийся на CAS.

## 5. CAS

CAS — Content-Addressable Storage. Каждый object определяется содержимым:

```text
SHA256(bytes)
```

Object identity: `sha256:<hash>`. Физический layout:

```text
DungeonStore/
└─ cas/
   └─ sha256/
      └─ ab/
         └─ cd/
            └─ abcdef...
```

Одинаковые bytes физически хранятся один раз.

## 6. CAS invariants

CAS object:

```text
immutable;
content-addressed;
verify-by-hash;
deduplicated;
not identified by filename;
not silently overwritten.
```

Если `SHA256(new_bytes)` совпадает с существующим hash, новая физическая копия не требуется. Если bytes отличаются, создаётся новый object с новым hash.

## 7. Human filename не является identity

Имя `renderer-output.log` является только metadata. Настоящая physical identity: `sha256:...`. Manifest может сохранять:

```json
{
  "name": "renderer-output.log",
  "blob": "sha256:..."
}
```

Переименование logical filename не меняет CAS object.

## 8. Logical Dungeon Pack

Основной тип Pack — Logical Pack. Logical Pack не обязан быть одним физическим архивом: это небольшой manifest, ссылающийся на CAS objects.

```json
{
  "format": "dungeon.pack.v1",
  "id": "snapshot-renderer-0042",
  "type": "logical",
  "objects": [
    {
      "path": "renderer/settings.sklt",
      "blob": "sha256:..."
    },
    {
      "path": "renderer/settings.glif",
      "blob": "sha256:..."
    }
  ]
}
```

## 9. Logical Pack — основной режим

Для обычной работы используется Logical Pack + CAS, а не новый `.zip`/`.tar` при каждом snapshot или run. Это обеспечивает deduplication, small manifests, fast indexing, selective restore и shared blobs между snapshots.

## 10. Пример deduplication

Два snapshot:

```text
Snapshot A: A B C D
Snapshot B: A B C E
```

CAS физически хранит `A B C D E`, а не `A B C D` и `A B C E` повторно. Неизменившиеся structural artifacts не копируются.

## 11. Portable Dungeon Pack

Для переноса или offline archive используется Portable Pack. Рекомендуемый physical format: `tar + zstd`, расширение `.dpack`, например `renderer-evidence-0042.dpack`.

## 12. Что содержит `.dpack`

```text
pack.dpack
├─ manifest.json
├─ index.json
└─ objects/
   └─ sha256/
      └─ ...
```

Portable pack содержит manifest, referenced CAS objects, integrity metadata и optional index. Он должен быть самодостаточным для import в другой Dungeon Store.

## 13. Portable Pack не является основным storage format

`.dpack` используется для transfer, archive, cold storage, external handoff и offline backup. Он не используется как постоянная замена обычному CAS во время активной разработки.

## 14. Export lifecycle

```text
Logical Pack
    ↓
resolve referenced hashes
    ↓
verify CAS objects
    ↓
collect exact objects
    ↓
build deterministic archive
    ↓
write .dpack
    ↓
verify archive
```

Broken или missing refs не экспортируются как успешный pack.

## 15. Import lifecycle

```text
.dpack
   ↓
read manifest
   ↓
validate format
   ↓
verify hashes
   ↓
write missing blobs into CAS
   ↓
deduplicate existing blobs
   ↓
register logical pack
```

Import не создаёт вторые физические копии уже существующих CAS objects.

## 16. Selective extraction

Полная распаковка Pack не требуется для просмотра одного объекта. Поддерживаются операции уровня:

```text
inspect pack
list pack
extract object
extract path
extract all
```

## 17. Temporary extraction

Распакованные working files по умолчанию считаются EPHEMERAL, например `DungeonStore/tmp/extract-...`. После завершения работы они могут быть удалены. Temporary unpack directory не превращается в permanent evidence tree.

## 18. Git policy

Git хранит:

```text
Pack manifests
Evidence manifests
Seals
Cronica
SKLT
GLIF
source
tests
small structural snapshots
configuration
```

Git не хранит:

```text
large .dpack
large CAS blobs
temporary unpack directories
runtime artifact trees
generated container filesystems
```

Исключение допускается, только если artifact маленький, осмысленный, reviewable и действительно нужен как source или golden fixture.

## 19. `.dpack` и Git

По умолчанию `*.dpack` исключён из Git. Если нужен versioned portable pack, Git хранит его descriptor:

```json
{
  "id": "renderer-release-evidence",
  "pack_hash": "sha256:...",
  "size": 481234567,
  "storage": "cold"
}
```

а не сам многогигабайтный archive.

## 20. Snapshot policy

Structural Snapshot использует Logical Pack для frozen copies. В Git остаётся только маленький snapshot manifest. Полные копии вида

```text
snapshots/snapshot-001/
    thousands of duplicated files
```

не создаются, если те же данные выразимы CAS refs.

## 21. Evidence policy

Execution Evidence использует CAS:

```text
Evidence Manifest
    ↓
stdout hash
screenshot hash
log hash
binary hash
capture hash
    ↓
CAS
```

Каталоги вида `evidence/run-001/` с полными копиями artifacts не создаются.

## 22. Container output policy

Runtime container из `Rust_Makepad_Toolkit` может создавать большое количество файлов. После завершения execution:

```text
container
   ↓
declared output selection
   ↓
classify
   ↓
PERMANENT / CACHE / EPHEMERAL
```

Только необходимые outputs помещаются в CAS. Весь container filesystem в CAS не помещается автоматически.

## 23. Artifact classes

Все тяжёлые artifacts классифицируются как:

```text
PERMANENT
CACHE
EPHEMERAL
```

Дополнительно operational working set может считаться:

```text
HOT
```

### 24. PERMANENT

Хранится без автоматического удаления:

```text
sealed evidence
important failure capture
accepted screenshots
final benchmark evidence
release artifact
structural evidence referenced by Cronica
```

### 25. CACHE

Может быть воспроизведён:

```text
compiled binaries
intermediate IR
converted assets
build outputs
reproducible generated data
```

CACHE может очищаться автоматически при необходимости.

### 26. EPHEMERAL

Вообще не помещается в CAS:

```text
tmp
scratch
target
linker temp
container scratch
temporary extraction
temporary worktrees
```

### 27. HOT

`HOT` означает не storage authority, а текущий working set:

```text
current repository
recent evidence
recent CAS blobs
active extracted pack
```

HOT objects могут физически жить на быстром storage.

## 28. Pack manifest

Каждый Pack имеет manifest минимум с:

```text
format
pack_id
created_at
content hash / manifest hash
object count
objects
logical paths
artifact classes if relevant
```

Если pack связан с проектом, желательно также:

```text
project_id
Git commit
structural snapshot
object_ids
evidence_ids
```

## 29. Pack identity

Pack identity не зависит только от filename. Имя `renderer-final.dpack` не является достаточной identity: нужны `pack_id` и integrity hash.

## 30. Immutable Pack rule

Logical Pack после seal становится immutable. Portable `.dpack`, соответствующий sealed logical pack, также immutable. Если содержимое изменилось, создаётся new pack_id / new version. Старый sealed Pack не переписывается.

## 31. Pack sealing

Модель состояний:

```text
DRAFT
   ↓
VERIFIED
   ↓
SEALED
```

После `SEALED` manifest не меняется, CAS refs не меняются, declared hashes не меняются. Это состояние не связано с SKLT или RARE.

## 32. Verification

Перед использованием sealed Pack Tool проверяет:

```text
manifest validity
referenced blobs exist
blob SHA256 matches
object count
portable archive hash if applicable
```

Наличие файла не считается достаточным доказательством integrity.

## 33. CAS location

CAS configurable, например:

```json
{
  "storage": {
    "cas_root": "C:\\DungeonStore\\cas"
  }
}
```

Путь не является identity объектов. CAS можно перенести, сохранив hashes.

## 34. HOT / COLD storage

Разрешается разделять физическое storage:

```text
HOT CAS
    recent objects
    fast disk

COLD CAS
    old permanent objects
    large archived evidence
```

Logical refs не меняются: `sha256:X`. Местоположение — storage concern, а не structural identity.

## 35. Missing CAS object

Если manifest содержит `sha256:X`, но object недоступен, это `MISSING_CAS_OBJECT`. Молча считать Pack valid нельзя. Если blob существует в cold/offline store, состояние может быть отдельным — `OFFLINE` — если система способна это определить.

## 36. CLI

Рекомендуемые операции:

```text
project-structure cas put <file>
project-structure cas get <hash>
project-structure cas verify <hash>
project-structure cas status

project-structure pack create <name> ...
project-structure pack inspect <pack>
project-structure pack list <pack>
project-structure pack extract <pack> [selector]
project-structure pack verify <pack>
project-structure pack export <pack> <file.dpack>
project-structure pack import <file.dpack>
```

Не обязательно реализовывать весь CLI одновременно. Текущий CLI использует top-level hyphenated форму (`cas-put`, `cas-get`, `cas-verify`, `cas-status`); приведение к иерархической форме — отдельное решение, а не свершившийся факт.

## 37. Первый implementation gate

MVP Pack/CAS считается готовым, когда работают:

```text
CAS put
CAS get
CAS verify

Logical Pack create
Logical Pack inspect
Logical Pack verify

Portable Pack export
Portable Pack import

selective extraction
```

и есть тесты deduplication.

## 38. Validation

Dungeon validation обнаруживает:

```text
INVALID_PACK_MANIFEST
DUPLICATE_PACK_ID
MISSING_CAS_OBJECT
CAS_HASH_MISMATCH
BROKEN_PACK_REF
INVALID_PORTABLE_PACK
SEALED_PACK_MUTATED
```

Недоступный optional cold store не ломает structural validation проекта, если текущая задача его не использует.

## 39. GC

CAS objects нельзя удалять просто потому, что они старые. GC работает через reachability.

Roots:

```text
sealed Packs
sealed Evidence
accepted Snapshots
explicit pins
Cronica refs where configured as persistent
```

Алгоритм:

```text
roots
 ↓
referenced hashes
 ↓
reachable
```

Автоматически разрешено удалять только unreachable CACHE. Reachable PERMANENT не удаляется автоматически.

## 40. Permanent run directories forbidden

Жёсткий invariant: **PERMANENT RUN DIRECTORY = FORBIDDEN**. Запрещён anti-pattern:

```text
runs/
├─ run-001/
│  ├─ build/
│  ├─ logs/
│  ├─ screenshots/
│  └─ binaries/
├─ run-002/
└─ ...
```

Правильная модель:

```text
evidence/manifests/EV-001.json
packs/manifests/PACK-001.json

DungeonStore/
└─ cas/
```

## 41. Permanent snapshot trees discouraged

Аналогично избегать:

```text
snapshots/
├─ snapshot-001/full-copy/
├─ snapshot-002/full-copy/
└─ snapshot-003/full-copy/
```

Предпочтительно:

```text
snapshots/
├─ snapshot-001.json
├─ snapshot-002.json
└─ snapshot-003.json
```

где payload references идут через CAS.

## 42. Agent rule

Перед созданием большого набора файлов агент определяет:

```text
Это editable?
    → обычный файл

Это immutable/reusable artifact?
    → CAS

Это группа CAS objects?
    → Logical Pack

Нужно перенести группу наружу?
    → Portable .dpack

Это temporary?
    → EPHEMERAL
```

## 43. Agent MUST NOT

Агент не должен:

```text
создавать permanent directories с thousands generated files;
копировать одинаковые frozen artifacts между snapshots;
класть крупный .dpack в Git без явного требования;
использовать archive filename как identity;
изменять sealed Pack;
удалять CAS object без reachability check;
сохранять весь container filesystem без необходимости;
считать распакованную temporary copy authority.
```

## 44. Agent SHOULD

Агент должен:

```text
сохранять editable artifacts обычными файлами;
использовать SHA256 для CAS identity;
verify blob после записи;
использовать manifests для logical grouping;
extract только необходимые objects;
cleanup temporary extraction;
сохранять provenance;
создавать новый Pack вместо изменения sealed Pack.
```

## 45. Связь с structural model

```text
Project
  ↓
Module
  ↓
Object
  ↓
SKLT ↔ GLIF
  ↓
source / tool / container refs
  ↓
execution
  ↓
Evidence Manifest
  ↓
Logical Pack
  ↓
CAS
```

Если нужен перенос:

```text
Logical Pack
     ↓
export
     ↓
Portable .dpack
```

## 46. Разделение сущностей

```text
Git
    editable history

Cronica
    semantic history

Snapshot
    structural checkpoint

Evidence Manifest
    execution history

Logical Pack
    artifact grouping

CAS
    physical immutable bytes

.dpack
    portable transport/archive
```

Ни одна из этих сущностей не заменяет остальные.

## 47. Главные инварианты

**PACK-01** Editable structural artifacts remain visible to Git.

**PACK-02** Large immutable artifacts do not live directly in Git.

**PACK-03** CAS identity is derived from exact bytes.

**PACK-04** CAS objects are immutable.

**PACK-05** Duplicate bytes are stored once.

**PACK-06** Logical Packs reference CAS objects instead of copying them.

**PACK-07** Portable Packs are transport artifacts, not primary working storage.

**PACK-08** Sealed Packs are append-only.

**PACK-09** Temporary extraction is not authority.

**PACK-10** Permanent run directories are forbidden.

**PACK-11** GC is reachability-based.

**PACK-12** Current SKLT/GLIF/source remain directly reviewable.

## 48. Итоговая модель

```text
                         Git
                          │
             ┌────────────┴────────────┐
             ▼                         ▼
       editable files              manifests
       SKLT / GLIF                Pack/Evidence
       source/docs                     │
                                       ▼
                                 Logical Pack
                                       │
                            ┌──────────┴─────────┐
                            ▼                    ▼
                         sha256:A             sha256:B
                            │                    │
                            └─────────┬──────────┘
                                      ▼
                                     CAS
                                      │
                          optional export/import
                                      │
                                      ▼
                                  *.dpack
```

## Final rule

> **Edit files normally. Store immutable bytes once. Group them by manifest. Pack them only for transport.**
