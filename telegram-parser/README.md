# telegram-parser

Парсер новых сообщений из Telegram-каналов и групп, которые лежат в папке `crypto`
вашего аккаунта. Работает через user API (MTProto, [Telethon](https://docs.telethon.dev))
и пишет каждое сообщение в локальную SQLite.

Состав папки перечитывается раз в 5 минут: если добавить канал или группу в папку
в Telegram, парсер подхватит его без перезапуска.

## Подготовка

1. Получите `api_id` и `api_hash` на <https://my.telegram.org> → API development tools.
2. В Telegram создайте папку `crypto` и добавьте в неё каналы и группы.
3. Скопируйте `.env.example` в `.env` и заполните ключи:

```bash
cp .env.example .env
```

`.env` и `data/` (сессия и база) в git не попадают.

> Файл `data/telegram.session` даёт полный доступ к аккаунту Telegram.
> Не передавайте его и не коммитьте.

## Запуск в Docker

Все парсеры запускаются общим `docker-compose.yml` из корня репозитория.
Команды ниже выполняются оттуда.

```bash
# 1. один раз: вход по номеру и коду из Telegram (и паролю 2FA, если включён)
docker compose run --rm telegram-parser login

# 2. показать весь состав папки и что из него парсится
docker compose run --rm telegram-parser channels

# 3. запустить парсер в фоне
docker compose up -d --build telegram-parser
docker compose logs -f telegram-parser
```

База лежит на хосте в `/opt/67-trading-data/telegram-parser/messages.db`.
Сессия хранится отдельно: `$TG_SESSION_DIR/telegram.session`, по умолчанию
`/opt/67-trading-secrets/telegram-parser/telegram.session`. `TG_SESSION_DIR` задаётся
в `.env` в корне репозитория или в переменных GitHub для деплоя.

## Запуск без Docker

Нужен [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv run --env-file .env python -m tg_parser login
uv run --env-file .env python -m tg_parser channels
uv run --env-file .env python -m tg_parser
```

## Прокси

Задаётся переменной `PROXY_URL` в `.env`:

| Тип | Пример |
| --- | --- |
| SOCKS5 | `socks5://user:pass@host:1080` |
| SOCKS4 | `socks4://host:1080` |
| HTTP | `http://user:pass@host:3128` |
| MTProxy | `mtproto://<secret>@host:443` |

Спецсимволы в логине и пароле кодируйте через `%` (`@` → `%40`, `:` → `%3A`).

Прокси, запущенный на самой хост-машине, из контейнера доступен
как `host.docker.internal`, например `socks5://host.docker.internal:1080`.
В корневом `docker-compose.yml` для этого уже прописан `extra_hosts`.

Если прокси нужен и для сборки образа (скачивание зависимостей):

```bash
docker compose build --build-arg HTTPS_PROXY=http://host:3128
```

## Переменные окружения

| Переменная | По умолчанию | Что задаёт |
| --- | --- | --- |
| `TG_API_ID`, `TG_API_HASH` | — | ключи приложения, обязательны |
| `TG_PHONE` | — | номер для `login`; если пусто, спросит |
| `TG_FOLDER` | `crypto` | название папки, без учёта регистра |
| `PROXY_URL` | — | прокси, см. выше |
| `TG_SESSION_PATH` | `data/telegram.session` | файл сессии |
| `DB_PATH` | `data/messages.db` | файл SQLite |
| `FOLDER_REFRESH_SECONDS` | `300` | как часто перечитывать папку |
| `CATCHUP_LIMIT` | `500` | сколько пропущенных сообщений на чат добирать после перезапуска |
| `HISTORY_DAYS` | `90` | глубина загрузки истории в днях; `0` — не грузить, `-1` — вся история |
| `HISTORY_DELAY_SECONDS` | `5` | пауза между запросами истории (плюс случайные 0–50%) |
| `METRICS_PORT` | `9101` | порт метрик Prometheus, `0` — выключить |
| `LOG_LEVEL` | `INFO` | уровень логов |

## Что сохраняется

Таблица `messages`, ключ `(chat_id, message_id)`, дубликаты игнорируются:

| Колонка | Что хранит |
| --- | --- |
| `chat_id` | id чата в формате Bot API (`-100…` у каналов и супергрупп, `-…` у обычных групп) |
| `message_id` | id сообщения внутри чата |
| `chat_type` | `channel`, `supergroup` или `group` |
| `chat_title`, `chat_username` | название и `@username` чата на момент сохранения |
| `sender_id`, `sender_username`, `sender_name` | автор; у постов канала это сам канал |
| `date` | время публикации, UTC, ISO 8601 |
| `text` | текст или подпись к медиа |
| `media_type` | класс вложения Telethon (`MessageMediaPhoto`, `MessageMediaDocument`, …), сами файлы не скачиваются |
| `grouped_id` | общий id сообщений одного альбома |
| `reply_to_msg_id` | id сообщения, на которое это ответ |
| `views`, `forwards` | просмотры и пересылки на момент получения (в группах пусто) |
| `received_at` | когда парсер сохранил сообщение, UTC |

```bash
sqlite3 data/messages.db "SELECT date, chat_title, sender_name, substr(text, 1, 80) FROM messages ORDER BY date DESC LIMIT 20"
```

База первой версии (только каналы, колонки `channel_*`) мигрируется
автоматически при запуске, старые строки получают `chat_type = 'channel'`.

## Как это устроено

- Парсятся каналы, супергруппы и обычные группы из папки: добавленные вручную
  и попавшие по настройкам папки («Каналы», «Группы»). Исключённые чаты
  и, при соответствующей настройке папки, архив пропускаются.
- Личные чаты, боты и «Избранное» не парсятся, даже если лежат в папке.
  `channels` показывает их в отдельном блоке.
- Настройки «скрывать прочитанные» и «без уведомлений» не учитываются,
  иначе чат выпадал бы из парсинга после каждого прочтения.
- После перезапуска парсер добирает сообщения, пропущенные с момента
  последнего сохранённого (до `CATCHUP_LIMIT` на чат).
  Сервисные сообщения («закрепил», «вступил в группу») не сохраняются.
- Параллельно с прослушиванием в фоне грузится история за `HISTORY_DAYS` дней, см. ниже.
- При обрыве связи Telethon переподключается бесконечно, раз в 5 секунд.
  Контейнер перезапускается при падении (`restart: unless-stopped`).

## Загрузка истории

Пока парсер слушает новые сообщения, отдельная фоновая задача медленно
листает историю чатов назад:

- один запрос за раз, до 100 сообщений, затем пауза `HISTORY_DELAY_SECONDS`
  со случайным разбросом. При 5 с это около 70 тыс. сообщений в час на все чаты;
- чаты обходятся по кругу, по одной пачке, поэтому история набирается во всех сразу;
- на `FloodWait` от Telegram задача ждёт указанное время и продолжает;
- позиция для каждого чата хранится в таблице `history_state`, после перезапуска
  загрузка идёт с того же места. Если увеличить `HISTORY_DAYS`, она продолжится вглубь;
- новые чаты в папке подхватываются автоматически.

Прогресс виден в логах:

```
[Whale Alert] история: +100, дошли до 2026-08-14
[Whale Alert] история: +37, дошли до 2026-07-25, готово
```

## Метрики

На `http://<host>:METRICS_PORT/metrics`:

| Метрика | Что показывает |
| --- | --- |
| `tg_messages_saved_total{chat_type, source}` | сохранённые сообщения; `source`: `live`, `catchup`, `history` |
| `tg_last_message_timestamp_seconds` | когда сохранено последнее live-сообщение |
| `tg_delivery_delay_seconds` | задержка от публикации до сохранения (live) |
| `tg_tracked_chats{kind}` | сколько каналов и групп парсится |
| `tg_connected` | 1, если есть соединение с Telegram |
| `tg_history_pending_chats` | чаты, у которых ещё догружается история |
| `tg_history_oldest_message_timestamp_seconds{chat}` | докуда догружена история чата |
| `tg_flood_waits_total`, `tg_flood_wait_seconds_total` | FloodWait от Telegram: если растёт, увеличьте `HISTORY_DELAY_SECONDS` |
| `tg_errors_total{stage}` | ошибки обновления папки и загрузки истории |
| `tg_db_size_bytes` | размер SQLite вместе с WAL |

Пример алерта: `time() - tg_last_message_timestamp_seconds > 3600`
(порог зависит от того, как часто пишут ваши каналы).

## Тесты

```bash
uv run --group dev pytest
```
