# dzsc

`dzsc` — Python CLI-инструмент из этого репозитория для one-shot автоматизации debug-стадий в проектах Doczilla (`*.clm` и другие Gradle-проекты).

Репозиторий:
- Локальный путь: `/Users/idfedorov09/my_prog/work_folder/doczilla/scripts`
- Remote: `git@github.com:idfedorov09/dzsc.git`

## Что делает инструмент

`dzsc` запускает упорядоченный pipeline стадий для целевого Doczilla-проекта.

Встроенные стадии:
- `dz_source_maps`: запускает `generateDebugJsSourceMap` через временный Gradle hook.
- `inject_agentation`: собирает/инжектит agentation overlay в debug HTML.
- `remove_agentation`: удаляет managed-snippet из debug HTML и директорию overlay.
- `agentation_status`: показывает текущий статус (hooks/snippet/bundle).
- `schema_list`: показывает локальные docker-compose Postgres-схемы и Doczilla-метаданные.
- `schema_current`: показывает текущую runtime-схему Doczilla из живой JVM.
- `schema_switch`: переключает живую Doczilla JVM на другую Postgres-схему через временный attach hook.

## One-shot контракт

Временная интеграция всегда временная:
- временные payload-файлы создаются в `<project>/.dzsc/run/<run-id>/...`;
- временный managed block в `build.gradle` откатывается после стадии;
- временные директории run очищаются при выходе (в том числе при ошибках).

Что остаётся как целевой результат:
- source maps и другие build-артефакты Gradle;
- overlay bundle и инжект в HTML после `inject_agentation`;
- runtime-состояние JVM после `schema_switch` (временные hook-файлы удаляются).

## Установка

Требование: установлен `uv`.

### Локальная editable-установка (рекомендуется для разработки)

```bash
cd /Users/idfedorov09/my_prog/work_folder/doczilla/scripts
uv tool install --editable --force .
```

Эта команда ставит бинарь `dzsc` (обычно в `~/.local/bin/dzsc`).

### Переустановка после изменений зависимостей/entrypoint

```bash
cd /Users/idfedorov09/my_prog/work_folder/doczilla/scripts
uv tool install --editable --force .
```

Если менялся только код в `src/dzsc/*`, editable-установка обычно подхватывает изменения сразу.

## Использование

### Показать доступные стадии

```bash
dzsc stages list
```

### Запуск pipeline (строгий синтаксис `-stage`)

```bash
dzsc --project /path/to/pro.doczilla.clm \
  -stage dz_source_maps \
  -stage inject_agentation
```

Если вы уже в корне проекта:

```bash
dzsc -stage dz_source_maps -stage inject_agentation
```

## Опции CLI

Глобальные опции (`dzsc run ...` или shorthand с `-stage`):
- `--project <dir>`: корень целевого проекта (по умолчанию текущая директория);
- `--python <path>`: путь к Python-интерпретатору;
- `--verbose`: подробный режим.

Stage-local опции:

### `dz_source_maps`
- `--sourcemap-config <file>`
- `--concat-source-root <value>` (можно передавать несколько раз)
- `--local-project-search-root <value>` (можно передавать несколько раз)

Пример:

```bash
dzsc --project /path/to/pro.doczilla.clm \
  -stage dz_source_maps \
  --sourcemap-config /path/to/frontend_debug_sourcemap.yml \
  --concat-source-root src/main/js \
  --concat-source-root src/js
```

### `inject_agentation`
- `--debug-path <file>` (по умолчанию `target/web/debug.html`)

### `remove_agentation`
- `--debug-path <file>` (по умолчанию `target/web/debug.html`)
- `--overlay-dir <dir>` (по умолчанию `target/web/debug/agentation`)

### `agentation_status`
- `--debug-path <file>` (по умолчанию `target/web/debug.html`)
- `--overlay-dir <dir>` (по умолчанию `target/web/debug/agentation`)

### `schema_list`
- `--compose-file <file>` (по умолчанию ищется от `--project` вверх)
- `--doczilla-service <name>` (по умолчанию `doczilla`)
- `--postgres-service <name>` (по умолчанию `postgres`)

Пример:

```bash
dzsc --project /path/to/pro.doczilla.clm -stage schema_list
```

`last activity` считается как максимум из последнего изменения данных (`CreatedAt`/`ModifiedAt`) и mtime security-log. Read-only использование из Postgres без дополнительной телеметрии не видно.

### `schema_current`
- `--compose-file <file>` (по умолчанию ищется от `--project` вверх)
- `--doczilla-service <name>` (по умолчанию `doczilla`)

Пример:

```bash
dzsc --project /path/to/pro.doczilla.clm -stage schema_current
```

### `schema_switch`
- `--schema <name>`: целевая Doczilla-схема.
- `--yes`: подтверждение, что можно сбросить активные сессии и соединения.
- `--compose-file <file>` (по умолчанию ищется от `--project` вверх)
- `--doczilla-service <name>` (по умолчанию `doczilla`)
- `--postgres-service <name>` (по умолчанию `postgres`)

Пример:

```bash
dzsc --project /path/to/pro.doczilla.clm -stage schema_switch --schema public --yes
```

`schema_switch` компилирует временный Java attach payload внутри контейнера `doczilla`, меняет `ServerConfig.databaseSchema` и system property в живой JVM, чистит сессии/websocket state/DB connections и перезапускает scheduler для новой схемы. Файлы payload создаются в `.dzsc/run/<run-id>` и `/tmp` контейнера только на время команды.

## Текущие static payload-файлы

Шаблоны payload хранятся в package static resources:
- `src/dzsc/static/gradle/z8-debug-sourcemaps.gradle`
- `src/dzsc/static/gradle/agentation-debug-overlay.gradle`
- `src/dzsc/static/config/frontend_debug_sourcemap.yml`

## Как добавить свою stage-логику

### 1. Создать модуль стадии

Добавьте файл, например:
- `src/dzsc/stages_custom.py`

Функция стадии:
- `def my_stage(ctx: StageRunContext) -> int: ...`

Объявление стадии через декоратор:
- `@stage("my_stage_id", "Описание", aliases=(...))`

Ориентиры:
- `src/dzsc/stages_frontend.py`
- `src/dzsc/stages_agentation.py`

### 2. Зарегистрировать стадию в registry

Измените:
- `src/dzsc/builtin.py`

Добавьте stage-объект в `StageRegistry([...])`.

### 3. Добавить stage-local CLI-опции (опционально)

Измените:
- `src/dzsc/cli.py`

Добавьте опции вашей стадии в `STAGE_OPTION_MAP` (маппинг `--опция` -> поле контекста).

Если нужны новые поля контекста, расширьте:
- `src/dzsc/sdk.py` (`StageRunContext`)

### 4. Сохранять one-shot поведение

Для новых стадий соблюдайте тот же шаблон:
- подготовить baseline-состояние;
- применить временные изменения;
- выполнить задачу;
- восстановить baseline в `finally`;
- удалить временные каталоги в `.dzsc/run/...`.

## Важно

- Legacy-синтаксис `--stages ...` не поддерживается в strict-режиме.
- Канонический вызов pipeline: `-stage <stage-id> [stage-options]`.
