"""Free Binance spot / USD-M history. Daily gzip CSVs, exact numbers, explicit gaps."""
from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import time
import zipfile
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from market_data import CANDLE_FIELDS, candle_values, decimal_value, milliseconds
from file_io import replace_file, save_json

UTC = timezone.utc
LOG = logging.getLogger('binance.history')
DEFAULT = ('spot', 'metrics', 'funding')
DATASETS = DEFAULT + ('mark', 'index', 'premium', 'futures', 'basis', 'spot_trades', 'futures_trades')
PRICE_KINDS = ('spot', 'futures', 'mark', 'index', 'premium')
STAMP = ['timestamp', 'datetime_utc']
METRICS = {
    'sum_open_interest': 'open_interest_base',
    'sum_open_interest_value': 'open_interest_quote',
    'count_long_short_ratio': 'global_long_short_ratio',
    'count_toptrader_long_short_ratio': 'top_trader_accounts_ratio',
    'sum_toptrader_long_short_ratio': 'top_trader_positions_ratio',
    'sum_taker_long_short_vol_ratio': 'taker_long_short_volume_ratio',
}
FIELDS = {
    'spot': CANDLE_FIELDS + ['available_at'],
    'futures': CANDLE_FIELDS + ['available_at'],
    'metrics': STAMP + ['symbol', *METRICS.values()],
    'funding': STAMP + ['symbol', 'funding_rate', 'mark_price', 'rate_type'],
    **{k: STAMP + ['open', 'high', 'low', 'close', 'available_at'] for k in ('mark', 'index', 'premium')},
    **{k: STAMP + ['aggregate_trade_id', 'price', 'quantity', 'quote_quantity',
                  'first_trade_id', 'last_trade_id', 'is_buyer_maker', 'taker_side']
       for k in ('spot_trades', 'futures_trades')},
    'basis': STAMP + ['available_at', 'spot_close', 'futures_close', 'mark_price',
                     'index_price', 'premium_index', 'futures_spot_basis_bps',
                     'mark_index_gap_bps', 'spot_trade_imbalance', 'futures_trade_imbalance'],
}
_missing_archives = set()


def iso(stamp):
    return datetime.fromtimestamp(stamp/1000, UTC).isoformat()


def atomic_json(path, value):
    save_json(path, value)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def download(url, path, missing_ok=False):
    if path.exists():
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.part')
    for attempt in range(6):
        try:
            request = Request(url, headers={'User-Agent': 'binance-data-parser/2.0'})
            with urlopen(request, timeout=60) as response, temp.open('wb') as stream:
                shutil.copyfileobj(response, stream, length=1024*1024)
                stream.flush()
                os.fsync(stream.fileno())
            replace_file(temp, path)
            return True
        except HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return False
            if exc.code in (401, 403, 451):
                raise PermissionError(f'HTTP {exc.code}: Binance denied access; no paid fallback used') from None
            if exc.code not in (418, 429, 500, 502, 503, 504) or attempt == 5:
                raise
            delay = max(2**attempt, float(exc.headers.get('Retry-After', '0')))
        except (URLError, TimeoutError, OSError):
            if attempt == 5:
                raise
            delay = 2**attempt
        LOG.warning('Request failed; retry in %ss: %s', delay, url)
        time.sleep(delay)


def archive(symbol, kind, day, cache):
    spot = kind in ('spot', 'spot_trades')
    prefix = 'spot' if spot else 'futures/um'
    folder = {'spot': 'klines', 'futures': 'klines', 'metrics': 'metrics',
              'mark': 'markPriceKlines', 'index': 'indexPriceKlines',
              'premium': 'premiumIndexKlines', 'spot_trades': 'aggTrades',
              'futures_trades': 'aggTrades'}[kind]
    candle = kind in PRICE_KINDS
    options = [('daily', day.strftime('%Y-%m-%d'))]
    month_end = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    if candle and month_end <= datetime.now(UTC):
        options.insert(0, ('monthly', day.strftime('%Y-%m')))
    for period, label in options:
        middle = '/1m' if candle else ''
        name = f'{symbol}-{"1m" if candle else folder}-{label}.zip'
        relative = f'{prefix}/{period}/{folder}/{symbol}{middle}/{name}'
        path = cache / relative
        if str(path) in _missing_archives:
            continue
        url = 'https://data.binance.vision/data/' + relative
        if not download(url, path, missing_ok=True):
            _missing_archives.add(str(path))
            continue
        checksum = path.with_name(path.name + '.CHECKSUM')
        download(url + '.CHECKSUM', checksum)
        if sha256(path) != checksum.read_text(encoding='utf-8').split()[0]:
            raise ValueError(f'Archive checksum mismatch: {path}; remove cached ZIP and checksum before retrying')
        return path
    return None


def archive_records(path):
    with zipfile.ZipFile(path) as archive_file:
        names = [n for n in archive_file.namelist() if n.endswith('.csv')]
        if len(names) != 1:
            raise ValueError('Expected one CSV in archive')
        with archive_file.open(names[0]) as raw, io.TextIOWrapper(raw, encoding='utf-8-sig', newline='') as stream:
            yield from csv.reader(stream)


@lru_cache(maxsize=1)
def price_archive_records(path):
    # A monthly candle file is read once while its days are partitioned.
    days = {}
    for row in archive_records(path):
        if not row or row[0] in ('open_time', 'open_timestamp'):
            continue
        bucket = milliseconds(row[0]) // 86_400_000 * 86_400_000
        days.setdefault(bucket, []).append(row)
    return days


def price_rows(records, kind):
    for item in records:
        if not item or item[0] in ('open_time', 'open_timestamp'):
            continue
        if len(item) < 7:
            raise ValueError('Incomplete price row')
        stamp = milliseconds(item[0])
        if stamp % 60_000:
            raise ValueError('Price timestamp is not minute-aligned')
        values = candle_values(item) if kind in ('spot', 'futures') else dict(zip(
            ['open', 'high', 'low', 'close'], item[1:5]))
        if kind not in ('spot', 'futures'):
            numbers = list(map(decimal_value, item[1:5]))
            if kind != 'premium' and min(numbers) <= 0:
                raise ValueError('Nonpositive mark/index price')
            if numbers[2] > min(numbers[0], numbers[3]) or numbers[1] < max(numbers[0], numbers[3]):
                raise ValueError('Invalid price OHLC')
        yield dict(timestamp=stamp, datetime_utc=iso(stamp), available_at=stamp+60_000, **values)


def metric_rows(records, symbol):
    header = next(records, None)
    if not header or not {'create_time', 'symbol', 'sum_open_interest', 'sum_open_interest_value'} <= set(header):
        raise ValueError('Unexpected metrics header')
    selected = {}
    source_order = []
    duplicates = 0
    for values in records:
        if not values:
            continue
        if len(values) != len(header):
            raise ValueError('Incomplete metrics row')
        item = dict(zip(header, values))
        if item['symbol'] != symbol:
            raise ValueError('Metrics symbol does not match request')
        dt = datetime.fromisoformat(item['create_time'].replace('Z', '+00:00'))
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        stamp = int(dt.timestamp()*1000)
        for field in ('sum_open_interest', 'sum_open_interest_value'):
            if decimal_value(item[field]) < 0:
                raise ValueError('Negative open interest')
        row = dict(timestamp=stamp, datetime_utc=iso(stamp), symbol=symbol,
                   **{out: item.get(source, '') for source, out in METRICS.items()})
        if stamp in selected:
            if selected[stamp] != row:
                raise ValueError(f'Conflicting metrics at the same timestamp: {symbol} {iso(stamp)}')
            duplicates += 1
        selected[stamp] = row
        source_order.append(stamp)
    if source_order != sorted(source_order):
        LOG.info('%s metrics archive reordered chronologically (%s unique observations)', symbol, len(selected))
    if duplicates:
        LOG.info('%s metrics: removed %s identical duplicate observations', symbol, duplicates)
    for stamp in sorted(selected):
        yield selected[stamp]


def trade_rows(records):
    previous = None
    for item in records:
        if not item or item[0] in ('agg_trade_id', 'aggregate_trade_id'):
            continue
        if len(item) < 7:
            raise ValueError('Incomplete aggregate trade')
        identifier = int(item[0])
        if previous is not None and identifier <= previous:
            raise ValueError('Aggregate trade IDs are not increasing')
        previous = identifier
        maker = str(item[6]).lower()
        if maker not in ('true', 'false'):
            raise ValueError('Invalid buyer-maker flag')
        price, quantity = decimal_value(item[1]), decimal_value(item[2])
        if price <= 0 or quantity <= 0:
            raise ValueError('Nonpositive trade price/quantity')
        stamp = milliseconds(item[5])
        yield dict(timestamp=stamp, datetime_utc=iso(stamp), aggregate_trade_id=identifier,
                   price=item[1], quantity=item[2], quote_quantity=format(price*quantity, 'f'),
                   first_trade_id=item[3], last_trade_id=item[4], is_buyer_maker=maker,
                   taker_side='SELL' if maker == 'true' else 'BUY')


def api_rows(kind, symbol, start, end, cache):
    endpoint = {'spot': '/api/v3/klines', 'futures': '/fapi/v1/klines',
                'mark': '/fapi/v1/markPriceKlines', 'index': '/fapi/v1/indexPriceKlines',
                'premium': '/fapi/v1/premiumIndexKlines', 'funding': '/fapi/v1/fundingRate'}[kind]
    base = 'https://data-api.binance.vision' if kind == 'spot' else 'https://fapi.binance.com'
    cursor = start
    while cursor < end:
        params = {'pair' if kind == 'index' else 'symbol': symbol, 'startTime': cursor,
                  'endTime': end-1, 'limit': 1000}
        if kind != 'funding':
            params['interval'] = '1m'
        path = cache / 'rest' / symbol / kind / f'{cursor}-{end}.json'
        if not path.exists():
            time.sleep(0.7)
        download(base + endpoint + '?' + urlencode(params), path)
        page = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(page, list):
            raise ValueError(f'Unexpected Binance {kind} response')
        if not page:
            break
        stamps = [int(r['fundingTime']) if kind == 'funding' else milliseconds(r[0]) for r in page]
        if stamps != sorted(set(stamps)) or stamps[-1] < cursor:
            raise ValueError('API pagination did not advance')
        yield from page
        cursor = stamps[-1] + (1 if kind == 'funding' else 60_000)


def funding_rows(items, symbol):
    for item in items:
        if item.get('symbol') != symbol:
            raise ValueError('Funding symbol does not match request')
        decimal_value(item['fundingRate'])
        stamp = int(item['fundingTime'])
        yield dict(timestamp=stamp, datetime_utc=iso(stamp), symbol=symbol,
                   funding_rate=item['fundingRate'], mark_price=item.get('markPrice', ''),
                   rate_type=item.get('rateType', 'Regular'))


def read_partition(path):
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as stream:
        yield from csv.DictReader(stream)


def basis_rows(root, symbol, day):
    tables = {}
    for kind in PRICE_KINDS:
        path = root / symbol / kind / f'{day:%Y-%m-%d}.csv.gz'
        tables[kind] = {int(r['timestamp']): r for r in read_partition(path)} if path.exists() else {}
    def gap(a, b):
        return format((decimal_value(a)/decimal_value(b)-1)*10_000, 'f') if a and b else ''
    for stamp in sorted(set(tables['spot']) | set(tables['futures'])):
        row = {k: t.get(stamp, {}) for k, t in tables.items()}
        prices = {k: r.get('close', '') for k, r in row.items()}
        yield dict(timestamp=stamp, datetime_utc=iso(stamp), available_at=stamp+60_000,
                   spot_close=prices['spot'], futures_close=prices['futures'], mark_price=prices['mark'],
                   index_price=prices['index'], premium_index=prices['premium'],
                   futures_spot_basis_bps=gap(prices['futures'], prices['spot']),
                   mark_index_gap_bps=gap(prices['mark'], prices['index']),
                   spot_trade_imbalance=row['spot'].get('trade_imbalance', ''),
                   futures_trade_imbalance=row['futures'].get('trade_imbalance', ''))


def save_rows(path, kind, rows, start, end):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    count, first, last, previous = 0, None, None, None
    observed = set()
    incomplete = 0
    previous_id = None
    trade_gaps = []
    with gzip.open(temp, 'wt', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS[kind])
        writer.writeheader()
        for row in rows:
            stamp = int(row['timestamp'])
            if not start <= stamp < end:
                continue
            if kind not in ('spot_trades', 'futures_trades') and previous is not None and stamp <= previous:
                raise ValueError('Source timestamps are not strictly increasing')
            if previous is not None and stamp < previous:
                raise ValueError('Source timestamps moved backwards')
            previous = stamp
            if kind in ('spot_trades', 'futures_trades'):
                identifier = int(row['aggregate_trade_id'])
                if previous_id is not None and identifier != previous_id+1:
                    trade_gaps.append([previous_id+1, identifier-1])
                previous_id = identifier
            writer.writerow(row)
            count += 1
            first = stamp if first is None else first
            last = stamp
            if kind in (*PRICE_KINDS, 'basis', 'metrics'):
                observed.add(stamp)
            if kind == 'basis' and any(row[k] == '' for k in ['futures_spot_basis_bps', 'mark_index_gap_bps', 'premium_index']):
                incomplete += 1
    replace_file(temp, path)
    result = dict(rows=count, first_timestamp=first, last_timestamp=last, sha256=sha256(path))
    step = 60_000 if kind in (*PRICE_KINDS, 'basis') else 300_000 if kind == 'metrics' else None
    if step:
        result['missing_timestamps'] = [t for t in range(start, end, step) if t not in observed]
    if kind == 'basis':
        result['incomplete_rows'] = incomplete
    if kind in ('spot_trades', 'futures_trades'):
        result['missing_aggregate_id_ranges'] = trade_gaps
    return result


def parse_date(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    if dt.hour or dt.minute or dt.second or dt.microsecond:
        raise ValueError('Use UTC midnight dates: YYYY-MM-DD')
    return dt


def run(args):
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / 'coverage.json'
    saved = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else None
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    end = parse_date(args.end) if args.end else parse_date(saved['config']['end']) if saved else today
    year = end.year-args.years
    default = end.replace(year=year, day=min(end.day, calendar.monthrange(year, end.month)[1]))
    start = parse_date(args.start) if args.start else parse_date(saved['config']['start']) if saved else default
    if not start < end <= today:
        raise ValueError('Require start < end <= today UTC; end is exclusive')
    config = dict(start=start.date().isoformat(), end=end.date().isoformat(), symbols=args.symbols,
                  datasets=args.datasets, schema_version=2)
    if saved and saved['config'] != config:
        previous = saved['config']
        same_range = all(previous.get(k)==config[k] for k in ('start','end','symbols','schema_version'))
        removed = set(previous['datasets'])-set(args.datasets)
        if same_range and removed and removed <= {'futures','basis','mark','index','premium'} and set(args.datasets)<=set(previous['datasets']):
            saved.setdefault('retired_entries', {}).update({k:v for k,v in saved['entries'].items() if k.split('/')[1] not in args.datasets})
            saved['config'] = config
            saved['entries'] = {k:v for k,v in saved['entries'].items() if k.split('/')[1] in args.datasets}
            LOG.info('Using spot + OI + funding; completed downloads retained, excluded sources left on disk')
        else:
            raise ValueError('Options differ from saved run; choose another --output')
    state = saved or dict(config=config, entries={})
    atomic_json(manifest_path, state)
    cache = root / 'source_cache'
    interrupted = False
    try:
        for symbol in args.symbols:
            for kind in args.datasets:
                day = start
                while day < end:
                    label = f'{symbol}/{kind}/{day:%Y-%m-%d}'
                    path = root / symbol / kind / f'{day:%Y-%m-%d}.csv.gz'
                    old = state['entries'].get(label)
                    if old and old['status'] in ('complete', 'partial') and path.exists() and not (args.retry_missing and old['status'] == 'partial'):
                        if sha256(path) != old['sha256']:
                            raise ValueError(f'Completed output changed or corrupted: {label}')
                        day += timedelta(days=1)
                        continue
                    if old and old['status'] == 'missing' and not args.retry_missing:
                        day += timedelta(days=1)
                        continue
                    lo, hi = int(day.timestamp()*1000), int((day+timedelta(days=1)).timestamp()*1000)
                    try:
                        if kind == 'basis':
                            dependencies = [state['entries'].get(f'{symbol}/{k}/{day:%Y-%m-%d}', {}).get('status') for k in PRICE_KINDS]
                            if any(s not in ('complete', 'partial') for s in dependencies):
                                raise FileNotFoundError('A basis source is unavailable; source gaps are retained')
                            rows = basis_rows(root, symbol, day)
                        elif kind == 'funding':
                            month = day.replace(day=1)
                            stop = min((day.replace(day=28)+timedelta(days=4)).replace(day=1), end)
                            rows = funding_rows(api_rows(kind, symbol, int(month.timestamp()*1000), int(stop.timestamp()*1000), cache), symbol)
                        else:
                            source = archive(symbol, kind, day, cache)
                            if source is None:
                                if kind not in PRICE_KINDS:
                                    raise FileNotFoundError('No free Binance archive for this date; history was not fabricated')
                                records = api_rows(kind, symbol, lo, hi, cache)
                            else:
                                records = iter(price_archive_records(source).get(lo, ())) if kind in PRICE_KINDS else archive_records(source)
                            rows = price_rows(records, kind) if kind in PRICE_KINDS else metric_rows(records, symbol) if kind == 'metrics' else trade_rows(records)
                        details = save_rows(path, kind, rows, lo, hi)
                        partial = not details['rows'] or bool(details.get('missing_timestamps') or details.get('incomplete_rows') or details.get('missing_aggregate_id_ranges'))
                        state['entries'][label] = dict(status='partial' if partial else 'complete', **details)
                        LOG.info('%s: %s rows (%s)', label, details['rows'], state['entries'][label]['status'])
                    except FileNotFoundError as exc:
                        state['entries'][label] = dict(status='missing', reason=str(exc))
                        LOG.warning('%s: %s', label, exc)
                    except (PermissionError, URLError, OSError) as exc:
                        state['entries'][label] = dict(status='error', reason=f'{type(exc).__name__}: {exc}')
                        LOG.error('%s: %s; stopping this dataset for resume', label, exc)
                        break
                    except Exception as exc:
                        state['entries'][label] = dict(status='error', reason=f'{type(exc).__name__}: {exc}')
                        LOG.error('%s: %s', label, exc)
                    finally:
                        atomic_json(manifest_path, state)
                    day += timedelta(days=1)
    except KeyboardInterrupt:
        interrupted = True
        LOG.warning('Interrupted; repeat the same command to resume')
    counts = {}
    for entry in state['entries'].values():
        counts[entry['status']] = counts.get(entry['status'], 0)+1
    expected = (end-start).days*len(args.symbols)*len(args.datasets)
    state['summary'] = dict(expected_partitions=expected, statuses=counts,
                           unprocessed_partitions=expected-len(state['entries']))
    state['notes'] = [
        'Only free public Binance endpoints/archives; no API key or paid provider.',
        'Candles and basis timestamp = minute open; available_at = next minute boundary.',
        'OI/ratios retain native 5m observations; funding retains actual settlement timestamps.',
        'Exchange event time is not proof of publication time; account for reporting delay in ML.',
        'No interpolation, no future filling. Missing/partial archives are explicit.',
        'Funding completeness cannot be inferred from a fixed 8h grid; intervals can change.',
    ]
    atomic_json(manifest_path, state)
    LOG.info('Coverage: %s', state['summary'])
    if not interrupted and not getattr(args, 'no_csv', False) and 'spot' in args.datasets:
        from build_dataset import build
        for symbol in args.symbols:
            build(root, symbol, state,
                  oi_max_age_minutes=getattr(args, 'oi_max_age_minutes', 15),
                  funding_max_age_hours=getattr(args, 'funding_max_age_hours', 24))
    return 130 if interrupted else 0 if counts.get('complete', 0) == expected else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbols', default='BTCUSDT', help='Comma-separated symbols')
    parser.add_argument('--datasets', default=','.join(DEFAULT), help='Comma-separated: '+','.join(DATASETS))
    parser.add_argument('--years', type=int, default=5)
    parser.add_argument('--start', help='UTC YYYY-MM-DD inclusive')
    parser.add_argument('--end', help='UTC YYYY-MM-DD exclusive; default today')
    parser.add_argument('--output', default='data/history')
    parser.add_argument('--retry-missing', action='store_true')
    parser.add_argument('--csv-only', action='store_true', help='Build ordinary dataset CSVs from downloaded files; no network')
    parser.add_argument('--no-csv', action='store_true', help='Skip the automatic final dataset CSV')
    parser.add_argument('--oi-max-age-minutes', type=float, default=15,
                        help='Maximum OI age in final CSV; default 15 minutes, 0 disables expiry')
    parser.add_argument('--funding-max-age-hours', type=float, default=24,
                        help='Maximum settled funding age in final CSV; default 24 hours, 0 disables expiry')
    args = parser.parse_args(argv)
    args.symbols = list(dict.fromkeys(s.strip().upper() for s in args.symbols.split(',') if s.strip()))
    selected = {s.strip() for s in args.datasets.split(',') if s.strip()}
    if not selected or selected-set(DATASETS):
        parser.error('Unknown dataset; all supported datasets are free')
    if 'basis' in selected:
        selected.update(PRICE_KINDS)
    args.datasets = [k for k in DATASETS if k in selected]
    if not args.symbols or any(not re.fullmatch(r'[A-Z0-9]{5,30}', s) for s in args.symbols):
        parser.error('Invalid symbols')
    if not 1 <= args.years <= 20:
        parser.error('--years must be 1..20')
    import math
    if any(not math.isfinite(v) or v < 0 for v in (args.oi_max_age_minutes, args.funding_max_age_hours)):
        parser.error('Maximum ages must be finite and nonnegative')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        if args.csv_only:
            from build_dataset import build
            root = Path(args.output).resolve()
            saved = json.loads((root/'coverage.json').read_text(encoding='utf-8'))
            for symbol in saved['config']['symbols']:
                build(root, symbol, saved, oi_max_age_minutes=args.oi_max_age_minutes,
                      funding_max_age_hours=args.funding_max_age_hours)
            return 0
        return run(args)
    except (ValueError, OSError) as exc:
        LOG.error('%s', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
