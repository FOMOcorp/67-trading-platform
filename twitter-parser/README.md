# twitter-parser

Собирает посты Twitter/X-аккаунтов из списка `TW_ACCOUNTS` через [twitterapi.io](https://twitterapi.io)
и пишет их в локальную SQLite. Аккаунт X и OAuth не нужны — только ключ twitterapi.io.

Парсер делает две вещи:

- **новые твиты** — раз в `POLL_SECONDS` (по умолчанию 5 минут) спрашивает у каждого аккаунта
  твиты новее самого свежего сохранённого;
- **историю** — в фоне листает старые твиты вглубь, до `HISTORY_DAYS` дней назад
  (по умолчанию год, `-1` — вся доступная история). Позиция хранится в базе,
  после перезапуска загрузка продолжается с того же места.

Оба режима работают через поиск `GET /twitter/tweet/advanced_search` с запросами вида
`from:<аккаунт> -filter:replies since_time:… until_time:…`. Ретвиты поиск не возвращает,
цитаты и треды — да. Ответы другим пользователям включаются через `INCLUDE_REPLIES=true`.

## Стоимость

twitterapi.io берёт ~$0.15 за 1000 твитов, минимум $0.00015 за запрос (даже пустой).
Одна страница — до ~20 твитов.

- история: 10 аккаунтов × 3000 твитов ≈ 30 000 твитов ≈ **$4.5** один раз;
- опрос новых: 10 аккаунтов × 288 опросов в сутки ≈ 2900 запросов ≈ **$0.45 в сутки**
  плюс сами новые твиты. Реже опрос — дешевле: `POLL_SECONDS=900` даёт ~$0.15 в сутки.

Баланс — в [кабинете](https://twitterapi.io/dashboard). Когда кредиты кончаются (HTTP 402),
парсер останавливается с ошибкой.

## Настройка

```bash
cp .env.example .env    # TWITTERAPI_IO_KEY и TW_ACCOUNTS
```

| Переменная | По умолчанию | Описание |
| --- | --- | --- |
| `TWITTERAPI_IO_KEY` | — | ключ API, обязателен |
| `TW_ACCOUNTS` | — | аккаунты через запятую, `@` можно не писать |
| `PROXY_URL` | — | `socks5://…` или `http://…`, пусто — без прокси |
| `DB_PATH` | `data/tweets.db` | путь к базе |
| `INCLUDE_REPLIES` | `false` | сохранять ответы аккаунта другим пользователям |
| `POLL_SECONDS` | `300` | интервал проверки новых твитов, минимум 60 |
| `POLL_MAX_PAGES` | `10` | максимум страниц новых твитов за одну проверку |
| `HISTORY_DAYS` | `365` | глубина истории в днях; `0` — не грузить, `-1` — вся |
| `HISTORY_DELAY_SECONDS` | `1` | пауза между запросами истории (плюс 0–50%) |
| `METRICS_PORT` | `9103` | порт метрик Prometheus, `0` — выключить |
| `LOG_LEVEL` | `INFO` | уровень логов |

Добавить аккаунт — допишите его в `TW_ACCOUNTS` и перезапустите: история для него
начнёт грузиться с текущего момента. Убрать — сохранённые твиты останутся в базе.

## Запуск

Локально:

```bash
uv sync
uv run python -m twitter_parser          # опрос + история, Ctrl+C — остановить
uv run python -m twitter_parser stats    # сколько твитов сохранено по аккаунтам
```

В Docker (из корня репозитория):

```bash
docker compose up -d --build twitter-parser
docker compose run --rm twitter-parser stats
```

Тесты (без сети):

```bash
uv run --group dev pytest
```

## База

Таблица `tweets`, ключ — `tweet_id`. Повторно один твит не сохраняется.

| Колонка | Описание |
| --- | --- |
| `tweet_id` | ID твита |
| `account` | отслеживаемый аккаунт (в нижнем регистре) |
| `author_id`, `author_username`, `author_name` | автор |
| `created_at` | время публикации, ISO 8601 в UTC |
| `text`, `lang`, `url` | текст, язык, ссылка |
| `is_reply`, `in_reply_to_id`, `conversation_id` | ответ и тред |
| `quoted_tweet_id`, `retweeted_tweet_id` | цитируемый твит |
| `like_count`, `retweet_count`, `reply_count`, `quote_count`, `view_count`, `bookmark_count` | счётчики на момент сохранения, не обновляются |
| `raw` | полный JSON твита из API: медиа, ссылки, хэштеги, вложенные твиты |
| `received_at` | когда сохранён |

Таблица `history_state` — докуда догружена история каждого аккаунта.

```bash
sqlite3 data/tweets.db "SELECT account, created_at, text FROM tweets ORDER BY created_at DESC LIMIT 10"
```

## Метрики

`http://<host>:9103/metrics`, префикс `tw_`:

| Метрика | Описание |
| --- | --- |
| `tw_tweets_saved_total{source}` | сохранённые твиты: `live` (новые) и `history` |
| `tw_last_tweet_timestamp_seconds` | когда сохранён последний новый твит |
| `tw_delivery_delay_seconds` | задержка от публикации до сохранения новых твитов |
| `tw_last_poll_timestamp_seconds` | когда закончился последний опрос |
| `tw_tracked_accounts` | число аккаунтов |
| `tw_history_pending_accounts` | у скольких аккаунтов ещё грузится история |
| `tw_history_oldest_tweet_timestamp_seconds{account}` | докуда догружена история |
| `tw_api_requests_total{status}` | запросы к API по HTTP-статусу (`network` — ошибка сети) |
| `tw_errors_total{stage}` | ошибки: `poll`, `history` |
| `tw_db_size_bytes` | размер базы вместе с WAL |

## Ошибки

- `429` и `5xx` повторяются до 5 раз с паузой (учитывается `Retry-After`).
- `401` (неверный ключ), `402` (нет кредитов) и `4xx` (неверный запрос) останавливают парсер:
  повтор их не исправит. В Docker контейнер перезапустится и снова упадёт — смотрите логи.
- Пустая страница истории не сразу означает конец: история помечается законченной
  после трёх пустых ответов подряд.
