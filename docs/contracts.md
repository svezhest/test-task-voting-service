# Контракты реализации

Что именно реализуем и как это проверяется. Всё, чего здесь и в остальных `docs/` нет, — вопрос, а не решение исполнителя.

## Структура репозитория

```
app/            Python-пакет: все сервисы
  record.py     бинарный формат записи Kafka
  dedup.py      чистая математика дедупликации
  admin.py      FastAPI: админка
  ingest.py     FastAPI: приём голосов
  stage1.py     этап 1 (процесс)
  stage2.py     этап 2 (процесс)
web/            страница опроса: index.html, vote.js, fp.js (без сборки)
nginx/          nginx.conf
sql/schema.sql  схема Postgres, грузится при старте контейнера
tests/          тесты (пишет отдельный агент)
Dockerfile      один образ на все Python-сервисы, сервис выбирается командой
docker-compose.yml, Makefile, pyproject.toml
```

Библиотеки: FastAPI, uvicorn, aiokafka (с lz4), psycopg 3, numpy, scipy, pytest, httpx. Менеджер пакетов — uv.

## Стенд

Всё доступно через nginx на `http://localhost:8090`:
- `/p/{poll_id}` → `web/index.html` (для любого `poll_id`);
- `/p/{poll_id}/config.json` → файл, который публикует админка;
- `/api/` → приём;
- `/admin/` → админка.

Kafka (Redpanda): топики `votes_raw` и `votes_by_ip`, по 8 партиций, создаются при старте стенда.

### Переменные окружения

| Переменная | По умолчанию на стенде | Смысл |
|---|---|---|
| `DATABASE_URL` | — | Postgres |
| `KAFKA_BOOTSTRAP` | — | Redpanda |
| `ADMIN_TOKEN` | `dev-token` | Токен админки |
| `TRUST_XFF` | `1` | Брать IP клиента из первого значения `X-Forwarded-For`. На стенде включено, чтобы тесты могли задавать IP. |
| `DELIVERY_TIMEOUT_S` | `3` | Сколько приём ждёт подтверждения Kafka, потом `503` |
| `WEB_ROOT` | `/srv/web` | Куда админка публикует `p/{id}/config.json` (общий том с nginx) |

## Время

- `close_at = window_end + grace_s + DELIVERY_TIMEOUT_S`.
- Приём принимает голос, если `window_start ≤ now ≤ window_end + grace_s`. Иначе `410`.
- `R = floor((window_end + grace_s − window_start) / 5 с)`.
- Потолок на IP за опрос: `5000 × ceil((window_end + grace_s − window_start) / 60 с)`.

## Жизненный цикл и кто что делает

1. Админка: `POST /admin/polls` → `draft`; `activate` → `active`. При активации генерирует соль (32 случайных байта) и пишет `WEB_ROOT/p/{id}/config.json`: `{id, question, type, options: [{idx, label}], window_start, window_end, grace_s}`.
2. Приём раз в секунду читает из Postgres опросы в статусе `active` (с солью). Если Postgres недоступен — работает на последнем прочитанном.
3. Этап 1 читает `votes_raw` непрерывно:
   - для каждого опроса держит в памяти множество `voter_id` на каждую партицию; первый голос `voter_id` пересылает в `votes_by_ip` (ключ — `ip_hmac`), повторы не пересылает;
   - раз в секунду пишет в `stage_progress` (stage = 1) число всех голосов опроса в партиции (`votes`) и `end_offset` — по правилу «только если offset больше»;
   - после `close_at`, когда его позиция в партиции дошла до конца партиции, делает `flush` продюсера и ставит `done_at`;
   - при старте читает партиции с offset начала самого раннего окна среди опросов не в статусе `final` (`offsets_for_times`) и не коммитит offset. Повторная пересылка после рестарта безвредна: этап 2 игнорирует повторный `voter_id`.
4. Этап 2 — цикл:
   - находит опрос `active` с `now > close_at`, ставит `counting` и обнуляет соль;
   - ждёт `done_at` этапа 1 по всем 8 партициям `votes_raw`;
   - для каждой партиции `votes_by_ip`: читает записи опроса от `offsets_for_times(window_start)` до конца партиции, повторный `voter_id` внутри партиции пропускает;
   - проход 1, слияние, проход 2 — по [architecture_deduplication.md](architecture_deduplication.md). В проходе 2 голос засчитывается, если на его ключе засчитано меньше `L` и на его `ip_hmac` засчитано меньше потолка на IP; иначе он «сверх лимита»;
   - у `multi` засчитанный голос даёт +1 каждому своему варианту;
   - пишет `results` (по правилу `last_offset`) и ставит `final`.

   Процесс обрабатывает партиции `p`, где `p % STAGE2_WORKERS == STAGE2_WORKER_INDEX` (на стенде один воркер: `1` и `0`). Барьер между проходами — через `fp_counts` в Postgres: проход 2 начинается, когда в `fp_counts` есть блоки всех 8 партиций. Слияние делает каждый воркер сам (результат одинаковый).

## Итоги

- `received` — все голоса опроса в `votes_raw`: сумма `stage_progress.votes` этапа 1.
- `total` по варианту — голоса после этапа 1 (один на `voter_id`), до лимитов.
- `counted` по варианту — засчитанные после лимитов.
- `counted` и `total` в корне ответа — число голосов (не сумма по вариантам: у `multi` один голос может дать +1 нескольким вариантам).
- `over_limit_share = 1 − counted / total`.

## Отпечаток и IP на приёме

- Клиент шлёт в `fp` объект стабильных компонентов по [fingerprint.md](fingerprint.md).
- `fp_hash` = первые 8 байт `blake2b` от `json.dumps(fp, sort_keys=True, separators=(",", ":"))`.
- `ip_hmac` = первые 16 байт `HMAC-SHA256(соль, упакованный IPv4 или первые 8 байт IPv6)`.

## Модули для модульных тестов

### `app/record.py`

```python
@dataclass(frozen=True)
class Vote:
    poll_id: uuid.UUID
    options: int          # битовая маска, бит i = вариант idx i
    received_at_ms: int
    ip_hmac: bytes        # 16 байт
    fp_hash: bytes        # 8 байт
    voter_id: uuid.UUID

def encode(v: Vote) -> bytes      # ровно 73 байта, формат в api.md
def decode(b: bytes) -> Vote      # ValueError при неверной длине или версии
```

### `app/dedup.py`

```python
def poisson_limit(lam: float) -> int
    # N = 1 + Q_Poisson(lam; 0.9999); lam >= 0

def estimate_people(d: int, p: numpy.ndarray) -> float
    # n̂: решение D = Σ_f (1 − (1 − p_f)^n) по массиву долей p всех отпечатков.
    # d = 0 → 0; d = 1 → 1. d должно быть меньше len(p), иначе ValueError.

def key_limit(n_limit: int, distinct_voters: int, r: int) -> int
    # L = max(N, V − R)

def repeat_budget(window_seconds: float) -> int
    # R = floor(window_seconds / 5)

def ip_ceiling(window_seconds: float) -> int
    # 5000 × ceil(window_seconds / 60)
```

## Сквозные тесты

Работают против поднятого стенда только через HTTP (`http://localhost:8090`, токен `dev-token`).
- IP клиента задаётся заголовком `X-Forwarded-For`.
- Короткие окна: тест создаёт опрос с окном, которое начинается сейчас и длится несколько секунд, `grace_s` = 0.
- Итог ждут, опрашивая `GET /admin/polls/{id}/results` до `status = final`. Разумный таймаут — 60 с после конца окна.
