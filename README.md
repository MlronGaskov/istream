# ISTREAM

ISTREAM - исследовательский прототип синхронизации фиксированных диапазонов упорядоченного результата SQL-запроса в реальном времени.

## Структура проекта

```text
istream/
├── clients/
│   └── test_client/
├── db/
│   └── migrations/
├── docs/
│   ├── research/
│   │   ├── archive/
│   │   │   ├── paging_research2.png
│   │   │   └── research.md
│   │   └── approaches_overview.md
│   └── specification/
│       ├── events_specification.md
│       ├── formal_model.md
│       └── invariants_and_reference_validation.md
├── implementations/
│   └── istream/
├── tests/
│   └── integration/
├── workloads/
│   ├── generator/
│   └── scenarios/
├── compose.yaml
└── README.md
```

- `clients/test_client/` - однодиапазонный и многодиапазонный Python-клиенты.
- `db/migrations/` - схема БД, журнал событий и управление сроком хранения истории.
- `docs/specification/` - формальная модель, инварианты и спецификация событий ISTREAM.
- `docs/research/` - обзор подходов; в `archive/` находятся предыдущие исследовательские материалы.
- `implementations/istream/` - серверная реализация Fetch/Watch.
- `tests/integration/` - интеграционные проверки.
- `workloads/generator/` - генератор данных и изменений, `workloads/scenarios/` - сценарии нагрузки.

## Запуск

Требуются Docker и Docker Compose.

```bash
cp .env.example .env
docker compose up -d --build postgres istream
```

Сервер доступен по адресу `http://localhost:8080`. Для первоначального заполнения базы:

```bash
docker compose run --rm generator seed --rows 20 --seed 42
```

Пример запроса первых пяти записей:

```bash
curl -s http://localhost:8080/fetch/first \
  -H 'Content-Type: application/json' \
  -d '{"n":5}'
```

## Генератор изменений

Сначала создаётся детерминированный план операций:

```bash
docker compose run --rm generator plan \
  --initial-rows 20 \
  --count 50 \
  --interval-ms 200 \
  --seed 42 \
  --output /scenarios/test.jsonl
```

Затем он выполняется:

```bash
docker compose run --rm generator run --input /scenarios/test.jsonl
```

План рассчитан на состояние базы после `seed --rows 20 --seed 42`. Перед повторным выполнением сценария нужно повторить `seed`. Генератор выполняет обычные SQL-операции и не взаимодействует с протоколом ISTREAM напрямую.

## Проверка синхронизации

В первом терминале запусти клиента:

```bash
docker compose run --rm --build multi-client \
  --ranges 3 \
  --rows 3 \
  --gap 2 \
  --seconds 25 \
  --disconnect-after 3 \
  --offline-seconds 5 \
  --verify
```

После появления информации о диапазонах во втором терминале запусти генератор:

```bash
docker compose run --rm generator run --input /scenarios/test.jsonl
```

Клиент наблюдает три диапазона, имитирует отключение, воспроизводит пропущенные изменения и сравнивает результат с PostgreSQL. Для корректной финальной проверки генератор должен завершиться до проверки `--verify`.

Однодиапазонный клиент запускается аналогично через сервис `test-client` с параметром `--rows` вместо `--ranges` и `--gap`.

## Тесты

Интеграционные тесты запускаются в отдельном окружении с базой `istream_test`:

```bash
docker compose --profile test run --rm --build integration-tests
```

Они проверяют получение диапазонов, обработку `delta`, Replay, восстановление после `history-expired`, синхронизацию нескольких фрагментов и соответствие состоянию PostgreSQL.
