"""Stream downloaded daily partitions into one ordinary minute CSV per symbol."""
import csv
import gzip
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from file_io import replace_file, save_json
from market_data import CANDLE_FIELDS

LOG = logging.getLogger('binance.dataset')
METRIC_FIELDS = ['open_interest_base','open_interest_quote']
FIELDS = CANDLE_FIELDS + ['symbol','available_at',*METRIC_FIELDS,'oi_timestamp','oi_age_seconds',
    'funding_rate','funding_timestamp','funding_age_seconds']


def partitions(root,symbol,kind,state):
    for path in sorted((root/symbol/kind).glob('*.csv.gz')):
        key=f'{symbol}/{kind}/{path.name[:-7]}'
        entry=state.get('entries',{}).get(key)
        if entry and entry['status'] not in ('complete','partial'):
            continue
        with gzip.open(path,'rt',encoding='utf-8',newline='') as stream:
            yield from csv.DictReader(stream)


class Cursor:
    def __init__(self,rows):
        self.rows=iter(rows)
        self.next=next(self.rows,None)
        self.last=None
        self.previous=-1

    def through(self,stamp):
        while self.next is not None and int(self.next['timestamp'])<=stamp:
            current=int(self.next['timestamp'])
            if current<=self.previous:
                raise ValueError('Dataset input timestamps must be strictly increasing')
            self.previous=current
            self.last=self.next
            self.next=next(self.rows,None)
        return self.last


def build(root,symbol,state=None,oi_max_age_minutes=15,funding_max_age_hours=24):
    import math
    if any(not math.isfinite(v) or v < 0 for v in (oi_max_age_minutes,funding_max_age_hours)):
        raise ValueError('Maximum ages must be finite and nonnegative')
    root=Path(root).resolve()
    state=state or json.loads((root/'coverage.json').read_text(encoding='utf-8'))
    output=root/f'{symbol}_dataset_1m.csv'
    temporary=output.with_name(output.name+'.part')
    cursors={k:Cursor(partitions(root,symbol,k,state)) for k in ('metrics','funding')}
    count=0
    missing={k:0 for k in cursors}
    first=last=None
    previous=-1
    with temporary.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=FIELDS)
        writer.writeheader()
        for spot in partitions(root,symbol,'spot',state):
            stamp=int(spot['timestamp'])
            if stamp<=previous:
                raise ValueError('Spot timestamps must be strictly increasing')
            previous=stamp
            available=stamp+60000
            row={k:spot.get(k,'') for k in CANDLE_FIELDS}
            row.update(symbol=symbol,available_at=available)
            for kind,max_age in [('metrics',oi_max_age_minutes*60000),('funding',funding_max_age_hours*3600000)]:
                value=cursors[kind].through(available)
                if value:
                    age=(available-int(value['timestamp']))/1000
                    prefix='oi' if kind=='metrics' else 'funding'
                    row.update({prefix+'_timestamp':value['timestamp'],prefix+'_age_seconds':age})
                if value and (max_age==0 or available-int(value['timestamp'])<=max_age):
                    if kind=='metrics':
                        row.update({f:value.get(f,'') for f in METRIC_FIELDS})
                    else:
                        row.update(funding_rate=value['funding_rate'])
                else:
                    missing[kind]+=1
            writer.writerow(row)
            count+=1
            first=stamp if first is None else first
            last=stamp
            if count%250000==0:
                LOG.info('%s dataset: %s rows',symbol,count)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    if not count:
        raise ValueError(f'No downloaded spot candles for {symbol}; existing dataset was not replaced')
    replace_file(temporary,output)
    report=dict(symbol=symbol,rows=count,columns=FIELDS,first_timestamp=first,last_timestamp=last,
        missing_rows=missing,coverage_summary=state.get('summary'),created_utc=datetime.now(timezone.utc).isoformat(),
        alignment=dict(oi_max_age_minutes=oi_max_age_minutes,funding_max_age_hours=funding_max_age_hours,
                       expiry_disabled_value=0,decision_time='minute close'),
        notes=['One row per downloaded spot minute; no invented candles.',
               'Only spot OHLCV/trade flow, OI and settled funding are included.',
               'available_at is minute close; last_trade_id is unavailable in klines.',
               'OI/funding backward as-of at available_at using exchange event times, not guaranteed publication times.',
               'Maximum ages are explicit in alignment. Stale values are blank; their timestamp and age are retained.',
               'Dataset may be partial while source download is incomplete; consult coverage.json and missing_rows.'])
    save_json(output.with_name(output.name+'.metadata.json'),report)
    LOG.info('Ready: %s (%s rows)',output,count)
    return report
