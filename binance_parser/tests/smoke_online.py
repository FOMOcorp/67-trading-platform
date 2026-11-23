"""Opt-in real Binance smoke test. Downloads one day and one live cycle."""
import argparse
import asyncio
import csv
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'collectors'))
import download_history as history
from app.config import Settings
from app.main import run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    root=Path(args.output).resolve()
    options=argparse.Namespace(output=str(root/'history'),start='2025-01-01',end='2025-01-02',
        symbols=['BTCUSDT'],datasets=list(history.DEFAULT),years=5,retry_missing=True)
    result=history.run(options)
    if result:
        raise AssertionError(f'Historical coverage is incomplete: exit {result}')
    summary={}
    for kind in history.DEFAULT:
        file=root/'history/BTCUSDT'/kind/'2025-01-01.csv.gz'
        rows=list(history.read_partition(file))
        assert rows,kind
        summary[kind]=len(rows)
        if kind in history.PRICE_KINDS or kind=='basis':
            assert len(rows)==1440,(kind,len(rows))
        if kind in ('spot','futures'):
            for row in rows:
                assert Decimal(row['volume_base']) == Decimal(row['taker_buy_base_volume'])+Decimal(row['taker_sell_base_volume'])
                assert Decimal(row['volume_quote']) == Decimal(row['taker_buy_quote_volume'])+Decimal(row['taker_sell_quote_volume'])
                assert int(row['available_at'])==int(row['timestamp'])+60000
    # Second run verifies checksums and resumes completed outputs without duplication.
    assert history.run(options)==0
    dataset=root/'history/BTCUSDT_dataset_1m.csv'
    with dataset.open(encoding='utf-8',newline='') as stream:
        reader=csv.DictReader(stream)
        fields=reader.fieldnames
        assert len(list(reader))==1440
    assert {'taker_buy_base_volume','taker_sell_base_volume','number_of_trades',
            'open_interest_base','funding_rate'} <= set(fields)
    assert not any(k.startswith(('futures_','mark_','index_','premium_')) for k in fields)
    result=asyncio.run(run(Settings(output_dir=str(root/'live')),once=True))
    assert result==0,('live exit',result)
    files=list((root/'live').rglob('*.csv'))
    tables={p.relative_to(root/'live').parts[0] for p in files}
    assert {'instruments','candles','tickers','funding_rates','open_interest'} <= tables,tables
    assert not {'funding_snapshots','basis'} & tables,tables
    candle_files=list((root/'live/candles').rglob('*.csv'))
    assert candle_files and all(p.parent.name=='spot' for p in candle_files)
    report=dict(history_rows=summary,live_tables=sorted(tables),live_files=len(files),status='passed')
    (root/'smoke_report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
