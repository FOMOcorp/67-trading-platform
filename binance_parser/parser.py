"""Collect free Binance spot and perpetual data to CSV or ClickHouse."""
import argparse
import asyncio
from dataclasses import replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'collectors'))
from app.config import load_settings
from app.main import run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-s', '--symbols', nargs='+', help='BTCUSDT ETHUSDT ...')
    parser.add_argument('--output', help='CSV output root; default data/live')
    parser.add_argument('--storage', choices=['csv','clickhouse'])
    parser.add_argument('--once', action='store_true', help='Collect one cycle and exit')
    args = parser.parse_args(argv)
    settings = load_settings()
    changes = {}
    if args.symbols:
        import re
        symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
        if any(not re.fullmatch(r'[A-Z0-9]{5,30}', s) for s in symbols):
            parser.error('Use full Binance symbols, e.g. BTCUSDT')
        changes['symbols'] = symbols
    if args.output:
        changes['output_dir'] = args.output
    if args.storage:
        changes['storage'] = args.storage
    try:
        return asyncio.run(run(replace(settings, **changes), once=args.once))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f'Collection failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
