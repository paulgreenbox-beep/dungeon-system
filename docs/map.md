# Project map

Карта отражает structural layer и его внешнюю execution boundary. Конкретные project modules появляются после добавления `.sklt` в верхнеуровневые области проекта.

```mermaid
flowchart LR
    Project[Dungeon System]
    Docs[docs/\norientation and rules]
    Modules[Project modules\n*.sklt + *.glif]
    Core[Structural CLI\nscan / validate / exp / inspect]
    Snapshots[snapshots/\nfrozen structural views]
    Evidence[evidence/\nmanifests + seals]
    CAS[External local CAS\nSHA-256 exact bytes]
    Libs[libs/\nexternal reusable libraries]
    Toolkit[Rust_Makepad_Toolkit\ndocs/INDEX.md]
    Exec[AI UI Test Tool\nLocalWorker / ContainerWorker Level 0]

    Project --> Docs
    Project --> Modules
    Core --> Modules
    Modules --> Docs
    Modules --> Snapshots
    Snapshots -. evidence IDs .-> Evidence
    Evidence --> CAS
    Modules --> Libs
    Modules -. optional execution refs .-> Toolkit
    Toolkit --> Exec
```

Dungeon System фиксирует **what / where / structure**. Toolkit выполняет **how / execution capability**. Внутренности Toolkit, его container runtime и image lifecycle не являются частью этой карты.

Git хранит SKLT/GLIF, snapshots, Cronica и небольшие Evidence Manifests. Evidence ссылается на immutable CAS bytes; runtime payloads не копируются в `snapshots/`.
