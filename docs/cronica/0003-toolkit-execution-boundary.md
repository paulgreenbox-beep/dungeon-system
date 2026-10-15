# 0003 — Toolkit execution boundary

```text
id: CRONICA-0003
date: 2026-08-22
affected_modules: []
affected_object_ids: []
toolkit_capability: rust_makepad_toolkit / ContainerEnvironment Level 0
snapshot_before: v03-baseline
snapshot_after: not-created
```

Dungeon System теперь ссылается на актуальный `Rust_Makepad_Toolkit/docs/INDEX.md` как на canonical external entry point. Optional SKLT `execution` refs используют provider/kind/id и не дублируют Toolkit catalog или runtime lifecycle.

Текущий documented container identity — `docker-linux-level0`. Container Level 0 выполняет публичный `ai-ui-test run` через disposable isolated environment; он не является GUI/UIA/physical-input runtime. Snapshot остаётся structural snapshot и не содержит container image или runtime logs.

Причина: агенту нужно связать structural intent с documented execution capability, сохранив границу `Dungeon System = what/where/structure` и `Rust_Makepad_Toolkit = how/execution`.

Known limitation: external catalog проверяется read-only, когда Toolkit checkout доступен по актуальной sibling-root/configured root. Иначе ref получает `UNRESOLVED_TOOLKIT_REF` warning и не блокирует structural validation.
