# Libraries

`libs/` предназначен для готовых подключаемых библиотек и reusable components. Он не является обычным project module и не учитывается в `.exp`.

Автоматического скачивания, package manager и управления версиями здесь нет.

## Descriptor

Каждая подключённая библиотека может иметь `libs/<library>/lib.json`:

```json
{
  "id": "example.library",
  "version": "1.0.0",
  "source": "external",
  "purpose": "short description",
  "license": "unknown",
  "used_by": []
}
```

`id` должен быть стабильным. Из `.sklt` можно сослаться на библиотеку через optional field:

```text
libraries:
    example.library
```

Tool проверяет `UNKNOWN_LIBRARY`, `DUPLICATE_LIBRARY_ID` и `INVALID_LIBRARY_DESCRIPTOR`, но не скачивает зависимости.
