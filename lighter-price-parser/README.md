# lighter-price-parser

Консольный парсер цен с биржи [Lighter](https://lighter.xyz) в реальном времени.
Подключается к публичному WebSocket, подписывается на канал `market_stats/all`
и выводит или пишет в CSV mark, index, last trade и лучшие bid/ask
при каждом изменении цены.

Ключи, аккаунт и авторизация не нужны: рыночные данные Lighter публичные.

## Быстрый старт

Нужен [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/FOMOcorp/67-trading-platform.git
cd 67-trading-platform/lighter-price-parser
uv run parser.py -s ETH BTC
```

uv сам поставит подходящий Python и зависимости. Окружение вручную создавать не нужно.

<details>
<summary>Без uv (обычный pip)</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install "websockets>=14.0"
python parser.py -s ETH BTC
```
</details>

## Запуск в Docker

Парсер входит в общий `docker-compose.yml` в корне репозитория. Он пишет все рынки
в `/opt/67-trading-data/lighter/prices.csv` на хосте:

```bash
docker compose up -d --build lighter-price-parser
```

Чтобы писать только нужные рынки, поменяйте `command` в `docker-compose.yml`,
например `["--csv", "/app/data/prices.csv", "--quiet", "-s", "ETH", "BTC"]`.

## Примеры

```bash
uv run parser.py                         # все рынки
uv run parser.py -s ETH BTC SOL          # только выбранные символы
uv run parser.py -s ETH --csv eth.csv    # плюс запись изменений в CSV
uv run parser.py --once                  # один снимок цен и выход
uv run parser.py --testnet               # тестовая сеть
```

Пример вывода:

```
2026-10-20T15:59:52.838+00:00  ETH          mark=2440.40  index=2441.22  last=2440.49  bid=2440.37  ask=2440.51
```

## Параметры

| Флаг | Что делает |
| --- | --- |
| `-s, --symbols` | фильтр по символам (`ETH BTC SOL`) |
| `--csv FILE` | дописывать изменения в CSV-файл |
| `--once` | получить один снимок и выйти |
| `--testnet` | подключиться к testnet |
| `--no-readonly` | отключить read-only подключение (см. ниже) |
| `--quiet` | не печатать строки в консоль |
| `--proxy URL` | прокси для WebSocket: `socks5://user:pass@host:1080` или `http://host:3128`; по умолчанию из `PROXY_URL` |
| `--metrics-port PORT` | отдавать метрики Prometheus на `:PORT/metrics` |
| `--debug` | показывать неопознанные сообщения сервера |

### Колонки CSV

`ts_utc, symbol, market_id, mark_price, index_price, last_trade_price,
best_bid_price, best_ask_price, mid_price, current_funding_rate,
daily_price_change, daily_quote_token_volume`

## Метрики

С `--metrics-port PORT` (в Docker это порт 9102) на `/metrics` доступны:

| Метрика | Что показывает |
| --- | --- |
| `lighter_ws_connected` | 1, если WebSocket подключён |
| `lighter_reconnects_total{reason}` | переподключения: `connection_lost`, `http_429`, `http_5xx`… |
| `lighter_frames_received_total` | сообщения от сервера |
| `lighter_rows_written_total` | изменения цен (строки CSV) |
| `lighter_last_update_timestamp_seconds` | время последнего изменения цены |
| `lighter_markets_seen` | рынки, по которым пришли цены |
| `lighter_csv_size_bytes` | размер CSV |

Пример алерта: `time() - lighter_last_update_timestamp_seconds > 300`.

## Read-only режим

По умолчанию скрипт подключается к `wss://mainnet.zklighter.elliot.ai/stream?readonly=true`.
Это официальный режим Lighter только для чтения рыночных данных. Он работает
и в регионах, где торговля недоступна (иначе сервер отвечает ошибкой 20558
«restricted jurisdiction»). Парсеру больше ничего не нужно, поэтому
`--no-readonly` пригодится только для отладки.

## Как это устроено

- Ping раз в 60 секунд: сервер закрывает соединение после 2 минут тишины.
- При обрыве скрипт переподключается с паузой от 1 до 30 секунд.
- При фатальной ошибке (например, доступ запрещён) скрипт сразу выходит с понятным сообщением и не долбит сервер.
- Один WebSocket-канал на все рынки укладывается в [лимиты](https://apidocs.lighter.xyz/docs/rate-limits).
  REST для стандартных аккаунтов ограничен 60 запросами в минуту.

Документация API: <https://apidocs.lighter.xyz/docs/market-data>

