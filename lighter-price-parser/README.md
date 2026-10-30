# lighter-price-parser

Рыночные данные биржи [Lighter](https://lighter.xyz):

| Скрипт | Что делает |
| --- | --- |
| `parser.py` | цены в реальном времени: подключается к публичному WebSocket, подписывается на `market_stats/all` и при каждом изменении цены выводит или пишет в CSV mark, index, last trade и лучшие bid/ask |
| [`candles.py`](#исторические-свечи-ohlcv-candlespy) | разовая выгрузка исторических свечей OHLCV за любой период в CSV, для EDA |

Ключи, аккаунт и авторизация не нужны: рыночные данные Lighter публичные.

## Быстрый старт

Нужен [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/FOMOcorp/67-trading-platform.git
cd 67-trading-platform/lighter-price-parser
uv run parser.py -s ETH BTC
```

uv сам поставит подходящий Python и зависимости. Окружение вручную создавать не нужно.

Исторические свечи для анализа:

```bash
uv run candles.py -m ETH -r 1h -s 2025-10-01 -o eth_1h.csv
```

<details>
<summary>Без uv (обычный pip)</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install "websockets>=14.0"
python parser.py -s ETH BTC
```
</details>

## Исторические свечи (OHLCV): `candles.py`

Выгружает свечи через REST-эндпоинт
[`/api/v1/candles`](https://apidocs.lighter.xyz/reference/candles) в CSV.
Работает на стандартной библиотеке Python, `python-socks` нужен только для `--proxy`.

```bash
uv run candles.py -m ETH -s 2025-09-01                          # часовые свечи с 1 сентября до сейчас
uv run candles.py -m ETH -r 1m -s 2025-10-01 -e 2025-10-08      # минутные за неделю
uv run candles.py -m BTC -r 1d -s 2025-01-01 -o data/btc_1d.csv # дневные в свой файл
uv run candles.py -m 0 -r 15m -s 2025-10-20T12:00               # по market_id
uv run candles.py --list-markets                                # все рынки и их market_id
```

| Флаг | Что делает | По умолчанию |
| --- | --- | --- |
| `-m, --market` | монета (`ETH`, `BTC`, `LINK/USDC`) или `market_id` | обязательный |
| `-r, --resolution` | таймфрейм: `1m 5m 15m 30m 1h 4h 12h 1d` | `1h` |
| `-s, --start` | с какой даты | обязательный |
| `-e, --end` | до какой даты (не включая) | сейчас |
| `-o, --output` | CSV-файл | `lighter_<монета>_<таймфрейм>_<с>_<по>.csv` |
| `--include-partial` | оставить последнюю, ещё не закрытую свечу | выкл. |
| `--list-markets` | вывести рынки и выйти | |
| `--proxy URL` | прокси, как у `parser.py`: `socks5://user:pass@host:1080`, `socks4://…`, `http://…` | из `PROXY_URL` |
| `--testnet` | testnet вместо mainnet | |
| `--delay` | пауза между запросами, с | `1.1` |

Форматы дат: `2025-10-01`, `2025-10-01T12:30`, `"2025-10-01 12:30:00"`,
`2025-10-01T12:30:00+03:00`, unix-время в секундах или миллисекундах.
**Время без часового пояса считается UTC.** Диапазон полуоткрытый: `[start, end)`.

### Колонки CSV

| Колонка | Описание |
| --- | --- |
| `timestamp` | время открытия свечи, unix ms (UTC) |
| `datetime_utc` | то же в виде `YYYY-MM-DD HH:MM:SS` |
| `open`, `high`, `low`, `close` | цены |
| `volume_base` | объём в базовой монете |
| `volume_quote` | объём в USDC |
| `last_trade_id` | id последней сделки в свече |

Цены и объёмы записываются ровно в том виде, в каком их отдаёт API, без ошибок округления float.

```python
import pandas as pd
df = pd.read_csv("eth_1h.csv", parse_dates=["datetime_utc"])
```

### Что важно знать для EDA

- **Пропуски.** Если за период не было ни одной сделки, API свечу не возвращает. Скрипт
  пишет число таких пропусков в конце работы. Для равномерной сетки сделайте
  `df.set_index("datetime_utc").asfreq("1h")` и заполните пропуски по своему правилу.
- **Незакрытая свеча.** Последняя свеча текущего периода ещё меняется, поэтому по умолчанию
  она отбрасывается. Чтобы её оставить, добавьте `--include-partial`.
- **Глубина истории.** Mainnet Lighter запущен 17 января 2025. Если указать более раннюю
  дату, выгрузка просто начнётся с первой доступной свечи.
- **Скорость.** API отдаёт до 500 свечей за запрос, а без аккаунта действует лимит 60 запросов
  в минуту. Год минутных свечей (~525 тыс.) скачивается примерно за 20 минут, год часовых
  займёт около 20 секунд. Если упереться в лимит (HTTP 429), скрипт подождёт и повторит запрос.
- **Доступ.** Если Lighter отвечает «restricted jurisdiction», укажите прокси (`--proxy`
  или `PROXY_URL`). У REST, в отличие от WebSocket, нет read-only режима.

### Как устроена выгрузка

API возвращает свечи с `start <= t < end`, но не больше 500 последних в диапазоне.
Поэтому скрипт идёт от конца периода к началу: каждый следующий запрос заканчивается
на первой свече предыдущего. `count_back` всегда равен 1: это *минимальное* число свечей
в ответе, и при большем значении API подмешивает свечи раньше `start`.

## Запуск в Docker

Парсер входит в общий `docker-compose.yml` в корне репозитория. Он пишет все рынки
в `/opt/67-trading-data/lighter/prices.csv` на хосте:

```bash
docker compose up -d --build lighter-price-parser
```

Чтобы писать только нужные рынки, поменяйте `command` в `docker-compose.yml`,
например `["--csv", "/app/data/prices.csv", "--quiet", "-s", "ETH", "BTC"]`.

## parser.py: примеры

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

## parser.py: параметры

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

