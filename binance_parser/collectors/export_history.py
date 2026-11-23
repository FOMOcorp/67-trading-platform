"""Export Binance 1-minute OHLCV to CSV without ClickHouse or dependencies."""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from market_data import CANDLE_FIELDS, candle_values
from file_io import replace_file, save_json

UTC = timezone.utc
MINUTE = 60_000
FIELDS = CANDLE_FIELDS
LOG = logging.getLogger("history")


def parse_date(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    result = result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    if result.second or result.microsecond:
        raise ValueError("Dates must be aligned to a minute")
    return result


def milliseconds(value):
    value = int(value)
    # Binance Spot archives switched to microseconds in January 2025.
    return value // 1000 if value >= 100_000_000_000_000 else value


def iso(value):
    return datetime.fromtimestamp(value / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")


def get_bytes(url, missing_ok=False):
    for attempt in range(6):
        try:
            with urlopen(Request(url, headers={"User-Agent": "crypto-data-mvp-history/1.0"}), timeout=90) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return None
            if exc.code not in (418, 429, 500, 502, 503, 504) or attempt == 5:
                raise
            delay = max(2 ** attempt, float(exc.headers.get("Retry-After", 0)))
        except (URLError, TimeoutError, OSError):
            if attempt == 5:
                raise
            delay = 2 ** attempt
        LOG.warning("Download failed; retry in %ss: %s", delay, url)
        time.sleep(delay)


def archive_rows(market, symbol, period, label):
    prefix = "spot" if market == "spot" else "futures/um"
    url = (f"https://data.binance.vision/data/{prefix}/{period}/klines/"
           f"{symbol}/1m/{symbol}-1m-{label}.zip")
    payload = get_bytes(url, missing_ok=True)
    if payload is None:
        return None
    checksum = get_bytes(url + ".CHECKSUM")
    if hashlib.sha256(payload).hexdigest() != checksum.decode().split()[0]:
        raise ValueError(f"Checksum mismatch: {url}")
    rows = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"Unexpected archive contents: {url}")
        with archive.open(members[0]) as raw:
            for row in csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig")):
                if row and row[0] in ("open_time", "open_timestamp"):
                    continue
                if row:
                    rows.append(row)
    return rows


def api_rows(market, symbol, start, end):
    base = ("https://api.binance.com/api/v3/klines" if market == "spot"
            else "https://fapi.binance.com/fapi/v1/klines")
    rows = []
    cursor = start
    while cursor < end:
        query = urlencode(dict(symbol=symbol, interval="1m", startTime=cursor,
                               endTime=end - 1, limit=1000))
        page = json.loads(get_bytes(base + "?" + query))
        if not isinstance(page, list):
            raise ValueError(f"Unexpected Binance response: {page}")
        if not page:
            break
        following = milliseconds(page[-1][0]) + MINUTE
        if following <= cursor:
            raise ValueError("Binance pagination did not advance")
        rows.extend(page)
        cursor = following
        time.sleep(0.2)
    return rows


def next_month(dt):
    return (dt.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0)


def batches(market, symbol, start, end):
    cursor = datetime.fromtimestamp(start / 1000, UTC)
    finish = datetime.fromtimestamp(end / 1000, UTC)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor < finish:
        month_end = next_month(cursor)
        # A monthly archive also covers a partial first/last month in the past.
        if month_end <= today:
            stop = min(month_end, finish)
            rows = archive_rows(market, symbol, "monthly", cursor.strftime("%Y-%m"))
            if rows is not None:
                yield int(cursor.timestamp() * 1000), int(stop.timestamp() * 1000), rows
                cursor = stop
                continue
        stop = min(cursor.replace(hour=0, minute=0) + timedelta(days=1), finish)
        rows = None
        if cursor < today:
            rows = archive_rows(market, symbol, "daily", cursor.strftime("%Y-%m-%d"))
        lo, hi = int(cursor.timestamp() * 1000), int(stop.timestamp() * 1000)
        if rows is None:
            LOG.info("Using REST API: %s to %s", iso(lo), iso(hi))
            rows = api_rows(market, symbol, lo, hi)
        yield lo, hi, rows
        cursor = stop


def normalize(rows, start, end, allow_gaps=False):
    selected = {}
    for row in rows:
        if len(row) < 11:
            raise ValueError("Incomplete candle row")
        stamp = milliseconds(row[0])
        if stamp % MINUTE:
            raise ValueError(f"Candle timestamp is not minute-aligned: {stamp}")
        if start <= stamp < end:
            values = dict(timestamp=stamp, datetime_utc=iso(stamp), **candle_values(row))
            result = [values[field] for field in FIELDS]
            if stamp in selected and selected[stamp] != result:
                raise ValueError(f"Conflicting duplicate candle: {iso(stamp)}")
            selected[stamp] = result
    expected = (end - start) // MINUTE
    if len(selected) != expected and not allow_gaps:
        raise ValueError(f"Missing candles in {iso(start)} .. {iso(end)}: "
                         f"expected {expected}, received {len(selected)}. Export stopped; no data invented.")
    return [selected[key] for key in sorted(selected)]


def missing_ranges(rows, start, end):
    gaps = []
    cursor = start
    for row in rows:
        if row[0] > cursor:
            gaps.append(dict(start=iso(cursor), end_exclusive=iso(row[0]),
                             minutes=(row[0] - cursor) // MINUTE))
        cursor = row[0] + MINUTE
    if cursor < end:
        gaps.append(dict(start=iso(cursor), end_exclusive=iso(end), minutes=(end - cursor) // MINUTE))
    return gaps


def save_state(path, state):
    save_json(path, state)


def export(args):
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".part")
    progress = output.with_name(output.name + ".progress.json")
    if output.exists():
        raise ValueError(f"Output already exists (choose another --output): {output}")
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    state = json.loads(progress.read_text(encoding="utf-8")) if progress.exists() else None
    end = parse_date(args.end) if args.end else (
        datetime.fromtimestamp(state["end"] / 1000, UTC) if state else now)
    default_start = end.replace(year=end.year - args.years,
                                day=min(end.day, calendar.monthrange(end.year - args.years, end.month)[1]))
    start = parse_date(args.start) if args.start else (
        datetime.fromtimestamp(state["start"] / 1000, UTC) if state else default_start)
    lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if not lo < hi <= int(now.timestamp() * 1000):
        raise ValueError("Require start < end <= current closed minute (UTC)")
    config = dict(start=lo, end=hi, symbol=args.symbol, market=args.market, years=args.years,
                  allow_gaps=args.allow_gaps, schema_version=2)
    if state:
        if any(state.get(key) != value for key, value in config.items()):
            raise ValueError("Resume options differ from saved export; use original options or another --output")
        if not partial.exists() or partial.stat().st_size < state["offset"]:
            raise ValueError("Partial CSV is missing or truncated; use another --output")
    else:
        if partial.exists():
            raise ValueError(f"Partial CSV without checkpoint exists: {partial}; use another --output")
        with partial.open("x", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerow(FIELDS)
            stream.flush()
            os.fsync(stream.fileno())
        state = dict(config, next=lo, count=0, offset=partial.stat().st_size, gaps=[])
        save_state(progress, state)
    LOG.info("%s %s 1m: %s to %s UTC (end excluded)", args.market, args.symbol, iso(lo), iso(hi))
    # Roll back bytes from an interrupted, uncommitted batch before appending.
    with partial.open("r+b") as stream:
        stream.truncate(state["offset"])
        stream.seek(state["offset"])
        for batch_start, batch_end, rows in batches(args.market, args.symbol, state["next"], hi):
            normalized = normalize(rows, batch_start, batch_end, args.allow_gaps)
            gaps = missing_ranges(normalized, batch_start, batch_end)
            if gaps:
                LOG.warning("Source is missing %s minutes: %s", sum(g["minutes"] for g in gaps), gaps)
            buffer = io.StringIO(newline="")
            csv.writer(buffer).writerows(normalized)
            stream.write(buffer.getvalue().encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
            state.update(next=batch_end, count=state["count"] + len(normalized), offset=stream.tell())
            state["gaps"].extend(gaps)
            save_state(progress, state)
            LOG.info("Saved %s candles (%.1f%%), through %s UTC", state["count"],
                     100 * (batch_end - lo) / (hi - lo), iso(batch_end - MINUTE))
    missing = sum(g["minutes"] for g in state["gaps"])
    if state["count"] + missing != (hi - lo) // MINUTE:
        raise ValueError("Final candle count does not match requested range")
    save_state(output.with_name(output.name + ".metadata.json"), dict(
        source="Binance public klines", **config, rows=state["count"], missing_minutes=missing,
        gaps_utc=state["gaps"], last_trade_id=None, timestamp_unit="milliseconds"))
    replace_file(partial, output)
    progress.unlink()
    LOG.info("Complete: %s (%s candles)", output, state["count"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--market", choices=["spot", "futures"], default="spot")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--start", help="UTC ISO date/time, inclusive")
    parser.add_argument("--end", help="UTC ISO date/time, exclusive; default: current minute")
    parser.add_argument("--output", help='CSV path; otherwise derived from symbol and market')
    parser.add_argument("--allow-gaps", action="store_true",
                        help="Keep actual source candles and report missing minutes in metadata; never fill gaps")
    args = parser.parse_args(argv)
    args.symbol = args.symbol.upper()
    if not args.output:
        suffix = 'custom' if args.start or args.end else f'{args.years}y'
        args.output = f'data/{args.symbol}_{args.market}_1m_{suffix}.csv'
    if not re.fullmatch(r"[A-Z0-9]{5,30}", args.symbol):
        parser.error("Invalid symbol")
    if not 1 <= args.years <= 20:
        parser.error("--years must be between 1 and 20")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        export(args)
    except KeyboardInterrupt:
        LOG.warning("Interrupted. Run the same command to resume.")
        return 130
    except Exception as exc:
        LOG.error("%s. Run the same command to resume after resolving the error.", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
