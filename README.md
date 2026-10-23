# TrustMeBroItsAlpha: торговые сигналы для криптовалют на основе текстовых и рыночных данных

## Описание

Цель проекта — построить платформу, которая прогнозирует движение цен криптовалют и принимает торговые решения. Сигналы для моделей берутся из текстовых и рыночных данных. Текстовые данные собираются из новостей, постов с криптофорумов, твитов и сообщений Telegram-каналов. Рыночные данные — это OHLCV-данные торгуемых активов.

Платформа должна автоматически:
- собирать рыночные и текстовые данные из выбранных источников;
- очищать и преобразовывать их в признаки;
- дообучать модели на новых данных и подбирать гиперпараметры;
- выдавать прогнозы через API и отслеживать качество моделей.

## Команда
- Тимур Кудаев
- Шамиль Авзалов
- Дмитрий Крючков

## План (согласно чекпоинтам годового проекта)

1. Обсуждение проекта, составление плана.
2. Составление списка источников для парсинга, создание парсеров для каждого из источников, развёртывание СУБД с резервным копированием данных, формирование первого набора признаков, анализ данных.
3. Создание ML-модели, подбор гиперпараметров, автоматизация шагов через Airflow.
4. Создание инференс-сервиса на FastAPI и мониторинга моделей.
5. Улучшение моделей и сервиса.
6. Добавление нейронных сетей, сравнение с другими решениями.
7. Финальные доработки.
## Запуск парсеров

Все парсеры поднимаются общим `docker-compose.yml`. Данные складываются на хосте
в `/opt/67-trading-data`, у каждого парсера своя поддиректория:

| Сервис | Данные |
| --- | --- |
| `telegram-parser` | `/opt/67-trading-data/telegram-parser/messages.db`; сессия Telegram отдельно, в `TG_SESSION_DIR` (по умолчанию `/opt/67-trading-secrets/telegram-parser/`) |
| `lighter-price-parser` | `/opt/67-trading-data/lighter/prices.csv` |

На macOS добавьте `/opt/67-trading-data` в Docker Desktop → Settings → Resources → File sharing.

```bash
cp .env.example .env                                    # при необходимости поменять METRICS_BIND и TG_SESSION_DIR
cp telegram-parser/.env.example telegram-parser/.env    # ключи Telegram
docker compose run --rm telegram-parser login           # один раз
docker compose up -d --build
```

Подробности в README каждого парсера.

### Метрики

Оба парсера отдают метрики Prometheus:

| Сервис | Адрес | Префикс |
| --- | --- | --- |
| `telegram-parser` | `http://<host>:9101/metrics` | `tg_` |
| `lighter-price-parser` | `http://<host>:9102/metrics` | `lighter_` |

По умолчанию порты открыты только на `127.0.0.1`. Если Prometheus на другой машине
или в другой docker-сети, задайте `METRICS_BIND=0.0.0.0` и закройте порты файрволом
от всех, кроме Prometheus: в метриках есть названия чатов.

```yaml
scrape_configs:
  - job_name: trading-parsers
    static_configs:
      - targets: ["<host>:9101", "<host>:9102"]
```

### Дашборды Grafana

Готовые дашборды лежат в `grafana/dashboards/`, у каждого сервиса своя папка:

| Файл | Дашборд |
| --- | --- |
| `grafana/dashboards/telegram-parser/telegram-parser.json` | Telegram parser: соединение, поток сообщений, задержка, история, FloodWait, ошибки, размер базы |
| `grafana/dashboards/lighter-price-parser/lighter-price-parser.json` | Lighter price parser: WebSocket, изменения цен, переподключения, размер CSV |

В каждом дашборде вверху выбираются источник Prometheus и инстанс, поэтому к вашему
Prometheus ничего привязывать не нужно.

**Импорт вручную:** Dashboards → New → Import → загрузите JSON и выберите папку.

**Provisioning:** каждая поддиректория станет отдельной папкой в Grafana.

```bash
sudo mkdir -p /var/lib/grafana/dashboards/trading-parsers
sudo cp -r grafana/dashboards/* /var/lib/grafana/dashboards/trading-parsers/
sudo cp grafana/provisioning/dashboards/trading-parsers.yml /etc/grafana/provisioning/dashboards/
sudo systemctl restart grafana-server
```

## Деплой

Тесты запускаются автоматически на push и pull request (`lighter-parser.yml`, `telegram-parser.yml`).
Сборка и деплой запускаются вручную: **Actions → Deploy parsers → Run workflow**.
Можно выбрать ветку и что деплоить: `all`, `telegram-parser` или `lighter-price-parser`.
Workflow (`deploy.yml`) выполняется на self-hosted раннере: собирает образы через
`docker compose build`, поднимает контейнеры через `docker compose up -d` и проверяет,
что они запустились.

### Подготовка сервера

1. Поставьте Docker с плагином compose и
   [self-hosted раннер](https://docs.github.com/en/actions/hosting-your-own-runners)
   (Settings → Actions → Runners → New self-hosted runner).
   Пользователь раннера должен состоять в группе `docker`.
2. Положите сессию Telegram в `TG_SESSION_DIR/telegram.session`
   (по умолчанию `/opt/67-trading-secrets/telegram-parser/telegram.session`).
   Её можно скопировать с машины, где уже выполнен `login`:

   ```bash
   ssh server 'sudo install -d -m 700 /opt/67-trading-secrets/telegram-parser'
   scp telegram-parser/data/telegram.session server:/tmp/
   ssh server 'sudo mv /tmp/telegram.session /opt/67-trading-secrets/telegram-parser/ && sudo chmod 600 /opt/67-trading-secrets/telegram-parser/telegram.session'
   ```

   Пользователь раннера должен иметь право читать эту директорию: деплой проверяет,
   что файл на месте. Либо сделайте его владельцем директории, либо выдайте права группе.

   > Не запускайте парсер с одной и той же сессией одновременно на двух машинах:
   > Telegram может отозвать её, и вход придётся повторить. Перед копированием
   > остановите локальный парсер.

### Ключи Telegram на сервере

`TG_API_ID` и `TG_API_HASH` в GitHub не хранятся. Workflow берёт их из окружения
процесса раннера. Раннер, установленный как сервис (`svc.sh`), запускается systemd
и **не читает `/etc/environment`**. Задайте ключи в файле `.env` в директории раннера:

```bash
cd ~/actions-runner
cat >> .env <<'ENV'
TG_API_ID=123456
TG_API_HASH=abcdef0123456789
ENV
chmod 600 .env
sudo ./svc.sh stop && sudo ./svc.sh start
```

Раннер читает этот файл при старте и передаёт переменные каждому job.

### Настройки в GitHub

Settings → Environments → New environment → `production`. Там же можно включить
подтверждение деплоя (Required reviewers). Переменные и секреты уровня репозитория
тоже подходят.

| Тип | Имя | Что это |
| --- | --- | --- |
| Variable | `METRICS_BIND` | интерфейс для портов метрик, по умолчанию `127.0.0.1` |
| Variable или Secret | `TG_SESSION_DIR` | директория с `telegram.session` на сервере, по умолчанию `/opt/67-trading-secrets/telegram-parser` |
| Variable или Secret | `PROXY_URL` | прокси для обоих парсеров, если нужен; с паролем — в секрете |
| Variable | `TG_FOLDER`, `HISTORY_DAYS`, `HISTORY_DELAY_SECONDS`, `LOG_LEVEL` | необязательные настройки `telegram-parser`, см. его README |

`telegram-parser/.env` собирается из этих значений на время деплоя и потом удаляется.
