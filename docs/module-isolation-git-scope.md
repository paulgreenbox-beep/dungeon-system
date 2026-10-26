# Module isolation and GitScope policy

Политика определяет, как логические модули и уровни Dungeon System соотносятся с VCS history scopes, source boundaries, dependencies, snapshots и evidence. GitScope остаётся совместимым представлением Git-конфигурации.

> **Level определяет логическую границу. GitScope определяет границу истории. Они могут совпадать, но не обязаны.**

## 1. Почему не 1 Level = 1 repository

По умолчанию Dungeon System не предполагает `1 Level = 1 Git repository`. Размер сам по себе не является причиной выделения repository. Если модули меняются одной задачей, их дешевле держать в общем history.

Признаки реальной независимости, при которых разделение оправдано:

```text
отдельный release lifecycle
отдельное versioning
отдельная CI
отдельные permissions
отдельные maintainers
независимый deployment
редкие cross-repository atomic changes
reuse другими projects
стабильный public API boundary
```

Не являются причиной: количество файлов, объём LOC, важность модуля, наличие GPU или большое число SKLT.

## 2. Независимые identity

```text
DungeonId
LevelId
ObjectId
GitScopeId
VersionScopeId
RepositoryId
```

`LevelId` и `ObjectId` не меняются при переносе модуля в отдельный repository. Меняются только GitScope, repository location и physical path. Pathname никогда не является identity.

## 3. GitScope

```text
id
mode
repository
root
default_branch
optional remote
```

```json
{
  "id": "main",
  "mode": "MONOREPO",
  "repository": ".",
  "root": "."
}
```

```json
{
  "id": "compiler",
  "mode": "REPOSITORY",
  "repository": "../compiler",
  "root": ".",
  "default_branch": "main"
}
```

Режимы:

```text
MONOREPO
    уровень часть основного repository; у каждого уровня своя logical identity
    при общем Git history

REPOSITORY
    уровень или группа уровней в отдельном repository; это не создаёт новый Dungeon,
    оба repository остаются частями одного Project

EXTERNAL
    repository, library или tooling, которым Dungeon не владеет полностью;
    хранится reference, revision и integration refs, но не обычный внутренний module
```

Объявление хранится в `.project/project.json`:

```json
{
  "git_scopes": [
    {
      "id": "main",
      "mode": "MONOREPO",
      "repository": "."
    },
    {
      "id": "compiler",
      "mode": "REPOSITORY",
      "repository": "../compiler"
    }
  ]
}
```

### VersionScope compatibility

Новая версия не переименовывает существующий GitScope и не меняет SKLT field git_scope. Это стабильный логический указатель на history scope; для Git он продолжает разрешаться через legacy git_scopes. Implicit Git scope `main` сохраняется только если ни `git_scopes`, ни `version_scopes` явно не объявляет scope с ID `main`.

Опциональный version_scopes расширяет manifest без удаления git_scopes. Пример содержит один сохранённый Git scope и отдельный opt-in Scion scope:

~~~json
{
  "git_scopes": [
    {"id": "main", "mode": "MONOREPO", "repository": ".", "root": "."}
  ],
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
~~~

VersionScope fields are strict; unknown fields are invalid. Both root and repository are project-relative and may not escape the project. endpoint_profile is an environment-profile name, never a token or credential. A missing backend on a version-scope entry defaults to Git. Git remains the default backend.

One Level may map to a Git or Scion VersionScope, and one scope may serve multiple Levels. A VCS boundary does not redefine isolation, LevelId, ObjectId, SKLT identity, or GLIF object_id. The full experimental operation matrix is in [VCS backends](vcs-backends.md).

## 4. Isolation

`isolation` — архитектурная независимость, а не Grade и не Git mode.

```text
SHARED
    обычная часть общего repository, сильные внутренние связи допустимы

BOUNDED
    явная architectural boundary при возможном monorepo: stable LevelId,
    declared dependencies, controlled public surface, нет произвольного
    cross-module file access. Рекомендуемый промежуточный режим

ISOLATED
    сильная независимость: свой GitScope, repository, CI и release,
    явный external API. Наличие отдельного repository само по себе
    не доказывает качество

EXTERNAL
    вне authority текущего Dungeon: только dependency или ref
```

`BOUNDED` может жить в `MONOREPO`, `ISOLATED` обычно живёт в `REPOSITORY`, но это не тождество. Escalation `SHARED → BOUNDED → ISOLATED` и de-escalation возможны, но не являются автоматической progression system: переход происходит по реальной необходимости.

## 5. Dependency policy

Cross-level и cross-scope зависимости объявляются явно:

```text
depends_on:
    renderer.resources
```

```json
{
  "type": "object",
  "id": "renderer.resources"
}
```

Неявная зависимость вида «module A просто полез в files module B» запрещена без structural registration.

Зависимость, пересекающая GitScope, не является ошибкой сама по себе, но должна быть explicit: известны source level, target level, source GitScope, target GitScope и dependency type.

Validation обнаруживает:

```text
UNKNOWN_GIT_SCOPE
INVALID_GIT_SCOPE_MODE
DUPLICATE_GIT_SCOPE
INVALID_CROSS_SCOPE_DEPENDENCY
```

Полный source graph ради этой проверки не строится.

## 6. Revision и dirty state

Каждый GitScope сообщает revision и состояние:

```text
CLEAN
DIRTY
UNAVAILABLE
UNKNOWN
```

Snapshot или sealed evidence фиксирует `DIRTY` явно. Dirty state не превращается в clean revision по догадке. Для reproducible или sealed evidence предпочтителен `CLEAN` либо отдельный source snapshot или evidence artifact.

Snapshot всего Dungeon фиксирует набор revisions, а не один commit:

```json
{
  "snapshot_id": "S-0042",
  "git": {
    "main": {
      "commit": "abc123"
    },
    "compiler": {
      "commit": "991abc"
    }
  }
}
```

Evidence Manifest фиксирует только relevant GitScopes: если run не зависел от repository, его revision не записывается. Dependencies должны быть воспроизводимы.

Typed revisions carry backend and scope identity. A Scion changeset ID is BLAKE3-namespaced and is not a Git object ID or Dungeon CAS SHA-256. For Scion the local state file identifies a working-copy base, not a clean or current remote revision; until a safe status path exists, dirty state remains UNKNOWN.

## 7. Миграция

Перенос уровня между scopes сохраняет:

```text
LevelId
ObjectId
SKLT ID
GLIF object_id
evidence references
Cronica history
```

и обновляет:

```text
repository refs
physical paths
GitScope mapping
source refs when required
```

Инвариант миграции: object identity до и после совпадает. Git migration не порождает новый logical object.

Dungeon System не переписывает историю Git самостоятельно. Он хранит migration metadata:

```text
old repository
old commit
new repository
new commit
migration event
```

Фактическое переписывание истории выполняет Git tooling.

## 8. Shared changes и atomicity

Если одна задача меняет несколько GitScopes, это один logical change set и несколько Git histories:

```json
{
  "change_set": "CS-0042",
  "repositories": {
    "main": "abc123",
    "compiler": "def456"
  }
}
```

Это association, а не атомарная Git-транзакция. Dungeon System не утверждает, что commits нескольких repository физически атомарны: корректная semantics — logical coordinated state. Новый VCS поверх Git не создаётся.

The same rule applies across VCS backends: a multi-scope snapshot stores per-scope observations and does not imply cross-backend atomicity.

## 9. Cronica

Meaningful cross-module или repository changes отражаются в Cronica с указанием affected levels и изменений revisions:

```text
Affected Levels:
    renderer
    compiler

Git changes:
    main:
        abc123 → def456

    compiler:
        991abc → 223def
```

Вынос уровня из monorepo — meaningful architectural change:

```text
renderer:
    old GitScope: main
    new GitScope: renderer
```

## 10. Agent navigation

`inspect <object_id>` должен возвращать:

```text
Object:
    renderer.settings

Level:
    renderer

Isolation:
    BOUNDED

GitScope:
    main

Repository:
    .

Path:
    renderer/

Git revision:
    abc123

SKLT:
    renderer/settings.sklt

Source:
    renderer/src/settings.rs
```

Workflow поиска источника:

```text
object_id
   ↓
Level
   ↓
GitScope
   ↓
repository
   ↓
source ref
```

Агент не ищет объект по всем repositories, если Dungeon уже знает его GitScope.

`project-structure status` показывает по каждому scope `mode`, число levels, `revision` и `state`, если данные доступны.

## 11. Independence от Git topology

```text
EXP и docs/.grade
    считаются по levels, objects и SKLT, а не по repositories.
    Перенос renderer в отдельный repository не меняет смысл renderer/.exp

CAS identity
    sha256(bytes) не зависит от Git repository.
    Один и тот же artifact из двух scopes может быть одним CAS object

Logical Pack
    может содержать evidence от нескольких repositories;
    blobs всё равно дедуплицируются
```

## 12. Libraries и внешние системы

`libs/` могут иметь `GitScope EXTERNAL`, если library является Git dependency. Не каждая library обязана иметь собственный GitScope: binary или static external dependency может иметь только descriptor, version или hash и CAS ref.

`Rust_Makepad_Toolkit` логично зарегистрировать как GitScope `rust_makepad_toolkit` с mode `EXTERNAL`. Dungeon хранит repository ref, revision, docs INDEX ref и tool или container refs, но Toolkit остаётся отдельной системой.

## 13. Isolation signals

Полезные сигналы:

```text
change coupling
release coupling
ownership
API stability
reuse
permissions
CI independence
deployment independence
dependency direction
```

Позже можно собирать cross-module и cross-scope change frequency, dependency count, reverse dependencies и repository coupling. Tool показывает эти данные, но не принимает architectural decision автоматически. Например, если renderer и ui меняются вместе в 92% осмысленных изменений, разделять их по repositories невыгодно; если compiler меняется независимо в 95% случаев, это аргумент за isolation.

## 14. Backward compatibility

Проект без `git_scopes` трактуется как legacy:

```text
all internal Levels
    ↓
GitScope main
    ↓
current repository
```

После materialization manifest становится explicit.

## 15. Рекомендуемый default

```text
один Dungeon
один main Git repository
несколько logical Levels
```

Git дробить заранее не следует. Сначала создаются logical module boundaries, typed dependencies, SKLT и object identity. Отдельные GitScopes выделяются только там, где history действительно должна расходиться.

## 16. Events

```text
GIT_SCOPE_REGISTERED
GIT_SCOPE_CHANGED

LEVEL_GIT_SCOPE_CHANGED

REPOSITORY_REVISION_CAPTURED

GIT_SCOPE_DIRTY_DETECTED
GIT_SCOPE_UNAVAILABLE

CROSS_SCOPE_DEPENDENCY_ADDED
CROSS_SCOPE_DEPENDENCY_REMOVED
```

## 17. Главные invariants

**ISO-01** `LevelId` is independent from Git repository.

**ISO-02** `ObjectId` survives repository migration.

**ISO-03** GitScope defines history boundary, not logical identity.

**ISO-04** One repository may contain many Levels.

**ISO-05** One Dungeon may contain many Git repositories.

**ISO-06** Cross-GitScope dependencies must be explicit.

**ISO-07** EXP/Grade do not depend on Git topology.

**ISO-08** CAS identity does not depend on Git topology.

**ISO-09** Snapshots capture revisions of all relevant GitScopes.

**ISO-10** Separate repository is not automatically better isolation.

**ISO-11** Isolation decisions are driven by lifecycle and boundaries, not file count.

**ISO-12** Git migration must preserve Dungeon stable identities.

## 18. Итоговая формула

```text
Level
    = logical boundary

Isolation
    = architectural independence

GitScope
    = history boundary

Repository
    = physical Git storage

Snapshot
    = coordinated project state

CAS
    = immutable artifact storage
```

> **Separate structure first. Separate Git only where history truly needs to diverge.**
